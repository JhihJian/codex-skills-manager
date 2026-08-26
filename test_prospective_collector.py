import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from effect_store import EffectStore
from outcome_checkers import BubblewrapCheckerRunner
from prospective_collector import (
    ArtifactCollectionRejected,
    ArtifactSelector,
    CheckerExecutionRejected,
    CollectorError,
    CollectorValidationError,
    ProspectiveCollector,
)


class ProspectiveCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.store = EffectStore(Path(self.temporary.name) / "effects.sqlite3")
        self.collector = ProspectiveCollector(
            self.store,
            allowed_sources=["pi"],
            allowed_roots=[self.root],
            collector_version="collector-test-1",
            max_event_bytes=1024,
            max_artifact_bytes=1024,
            max_manifest_bytes=4096,
            max_artifacts=10,
            environment={"profile": "test"},
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def event(**changes: object) -> dict:
        value = {
            "schema_version": 1,
            "event_id": "event-1",
            "source": "pi",
            "event_type": "skill.invoked",
            "occurred_at": "2026-08-24T10:00:00Z",
            "payload": {"status": "started"},
            "project_id": "project-1",
            "skill_id": "skill-1",
        }
        value.update(changes)
        return value

    def test_event_schema_size_source_and_redaction(self) -> None:
        event = self.event(
            payload={
                "authorization": "Bearer super-secret-token-value-123456",
                "password": "short-secret",
                "access_key": "short-access-key",
            }
        )
        saved = self.collector.record_event(event)
        stored = self.store.execute(
            "SELECT payload_hash, payload_json FROM prospective_events WHERE id = ?", (saved["id"],)
        ).fetchone()
        self.assertNotIn("super-secret", stored["payload_json"])
        self.assertNotIn("short-secret", stored["payload_json"])
        self.assertNotIn("short-access-key", stored["payload_json"])
        self.assertEqual(
            stored["payload_hash"],
            hashlib.sha256(
                json.dumps(event["payload"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

        invalid_events = [
            self.event(extra="unknown"),
            self.event(schema_version=1.0),
            self.event(source="codex"),
            self.event(occurred_at="2026-08-24T10:00:00"),
            self.event(occurred_at="2026-08-24 10:00:00+00:00"),
            self.event(payload={"bad": object()}),
            self.event(payload={"large": "x" * 2000}),
        ]
        for invalid in invalid_events:
            with self.subTest(event=invalid), self.assertRaises(CollectorValidationError):
                self.collector.record_event(invalid)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM prospective_events").fetchone()[0], 1)
        with self.assertRaises(CollectorValidationError):
            ProspectiveCollector(self.store, allowed_sources="pi")

    def test_event_replay_is_idempotent_but_conflicting_id_is_rejected(self) -> None:
        first = self.collector.record_event(self.event())
        replay = self.collector.record_event(self.event())
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(CollectorValidationError):
            self.collector.record_event(self.event(payload={"status": "different"}))
        with self.assertRaises(CollectorValidationError):
            self.collector.record_event(self.event(event_type="skill.finished"))
        with self.assertRaises(CollectorValidationError):
            self.collector.record_event(self.event(project_id="another-project"))
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM prospective_events").fetchone()[0], 1)

    def test_collects_all_phases_with_hash_size_version_and_environment(self) -> None:
        (self.root / "report.md").write_text("accepted\n", encoding="utf-8")
        selector = ArtifactSelector(
            self.root,
            ("**/*.md",),
            project_id="project-1",
            skill_id="skill-1",
        )
        manifests = [
            self.collector.collect_manifest(
                selector,
                phase,
                observation_group_id="invocation-1",
            )
            for phase in ("before-invocation", "after-artifacts", "after-check")
        ]
        artifact = manifests[0]["artifacts"][0]
        self.assertEqual(artifact["path"], "report.md")
        self.assertEqual(artifact["size"], 9)
        self.assertEqual(artifact["sha256"], hashlib.sha256(b"accepted\n").hexdigest())
        self.assertEqual(manifests[0]["collectorVersion"], "collector-test-1")
        self.assertRegex(manifests[0]["environmentFingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.store.execute("SELECT COUNT(*) FROM artifact_manifests").fetchone()[0], 3
        )

    def test_checker_binding_requires_matching_case_workspace_and_projects_artifacts(self) -> None:
        case = self.store.create_task_case("checker-case")
        (self.root / "report.md").write_text("report", encoding="utf-8")
        expected = self.collector.collect_manifest(
            ArtifactSelector(self.root, ("*.md",), project_id="project-1", skill_id="skill-1"),
            "after-artifacts", task_case_id=case["id"], observation_group_id="check-1",
        )
        self.assertEqual(json.loads(self.store.execute(
            "SELECT metadata_json FROM task_cases WHERE id=?", (case["id"],)
        ).fetchone()[0])["projectId"], "project-1")
        binding = self.collector.checker_binding(expected["id"], case["id"], self.root)
        approval = self.collector.authorize_checker_options(
            binding, "document-artifact", {"path": "report.md"}
        )
        self.assertEqual(approval["approvalVersion"], "document-exists-v1")
        with self.assertRaises(CollectorValidationError):
            self.collector.authorize_checker_options(
                binding, "document-artifact", {"path": "unselected.md"}
            )
        with self.assertRaisesRegex(CollectorValidationError, "every workspace file"):
            self.collector.authorize_checker_options(binding, "gradle-summary", {"tasks": ["test"]})
        self.assertEqual(binding["artifactId"], self.store.execute(
            "SELECT id FROM artifacts WHERE task_case_id=? AND artifact_type='manifest'", (case["id"],)
        ).fetchone()[0])
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        collector = ProspectiveCollector(
            self.store, allowed_sources=["pi"], allowed_roots=[self.root, other],
            collector_version="collector-test-1", environment={"profile": "test"},
        )
        with self.assertRaises(CollectorValidationError):
            collector.checker_binding(expected["id"], case["id"], other)
        observed, comparison = self.collector.collect_after_check(binding)
        self.assertEqual((observed["phase"], comparison["freshness"]), ("after-check", "current"))
        self.assertEqual(self.store.execute(
            "SELECT COUNT(*) FROM artifacts WHERE task_case_id=?", (case["id"],)
        ).fetchone()[0], 3)
        actor = self.store.create_actor("Reviewer", roles=["reviewer"])
        self.store.append_correction(
            case["id"], actor_id=actor["id"], expected_revision=0,
            correction_type="task-type", reason_code="revision-changed",
        )
        with self.assertRaisesRegex(CollectorValidationError, "older task case revision"):
            self.collector.checker_binding(expected["id"], case["id"], self.root)

    def test_manifest_rejects_escape_symlinks_special_files_and_budgets(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(ArtifactSelector(outside), "after-artifacts")

        (self.root / "link.txt").symlink_to(outside / "secret.txt")
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(ArtifactSelector(self.root), "after-artifacts")
        (self.root / "link.txt").unlink()

        nested = self.root / "nested"
        nested.mkdir()
        backup = self.root / "nested-backup"
        original_secure_root = self.collector._secure_root

        def replace_after_validation(raw_root: object, allowed_roots: object) -> Path:
            resolved = original_secure_root(raw_root, allowed_roots)
            nested.rename(backup)
            nested.symlink_to(outside, target_is_directory=True)
            return resolved

        try:
            with patch.object(self.collector, "_secure_root", side_effect=replace_after_validation):
                with self.assertRaises(ArtifactCollectionRejected):
                    self.collector.collect_manifest(ArtifactSelector(nested), "after-artifacts")
        finally:
            if nested.is_symlink():
                nested.unlink()
            backup.rename(nested)

        (self.root / "large.txt").write_bytes(b"x" * 1025)
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(ArtifactSelector(self.root), "after-artifacts")
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(
                ArtifactSelector(self.root, ("../*.txt",)), "after-artifacts"
            )
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(
                {"root": self.root, "globs": "*.txt"}, "after-artifacts"
            )
        with self.assertRaises(ArtifactCollectionRejected):
            self.collector.collect_manifest(
                {"root": 7, "globs": ["*.txt"]}, "after-artifacts"
            )
        (self.root / "another.bin").write_bytes(b"x")
        limited = ProspectiveCollector(
            self.store,
            allowed_sources=["pi"],
            allowed_roots=[self.root],
            max_walk_entries=1,
        )
        with self.assertRaises(ArtifactCollectionRejected):
            limited.collect_manifest(ArtifactSelector(self.root, ("*.never",)), "after-artifacts")

    def test_manifest_drift_reports_added_removed_and_modified(self) -> None:
        report = self.root / "report.md"
        report.write_text("one", encoding="utf-8")
        selector = ArtifactSelector(self.root, ("*.md",))
        expected = self.collector.collect_manifest(
            selector,
            "after-artifacts",
            observation_group_id="check-1",
        )
        report.write_text("two", encoding="utf-8")
        (self.root / "new.md").write_text("new", encoding="utf-8")
        observed = self.collector.collect_manifest(
            selector,
            "after-check",
            observation_group_id="check-1",
        )
        comparison = self.collector.compare_manifests(expected["id"], observed["id"])
        self.assertEqual(comparison["freshness"], "stale")
        self.assertEqual(comparison["modified"], ["report.md"])
        self.assertEqual(comparison["added"], ["new.md"])

        stable_expected = self.collector.collect_manifest(
            selector,
            "after-artifacts",
            observation_group_id="check-2",
        )
        stable_observed = self.collector.collect_manifest(
            selector,
            "after-check",
            observation_group_id="check-2",
        )
        self.assertEqual(
            self.collector.compare_manifests(
                stable_expected["id"], stable_observed["id"]
            )["freshness"],
            "current",
        )

        stored = self.store.execute(
            "SELECT manifest_json FROM artifact_manifests WHERE id = ?",
            (stable_observed["id"],),
        ).fetchone()
        tampered = json.loads(stored["manifest_json"])
        tampered["artifacts"][0]["size"] += 1
        with self.store.transaction():
            self.store.execute(
                "UPDATE artifact_manifests SET manifest_json = ? WHERE id = ?",
                (json.dumps(tampered, sort_keys=True, separators=(",", ":")), stable_observed["id"]),
            )
        with self.assertRaises(CollectorValidationError):
            self.collector.compare_manifests(stable_expected["id"], stable_observed["id"])

        changed_environment = ProspectiveCollector(
            self.store,
            allowed_sources=["pi"],
            allowed_roots=[self.root],
            environment={"profile": "different"},
        ).collect_manifest(
            selector,
            "after-check",
            observation_group_id="check-2",
        )
        environment_comparison = self.collector.compare_manifests(
            stable_expected["id"], changed_environment["id"]
        )
        self.assertEqual(environment_comparison["freshness"], "unknown")
        self.assertEqual(environment_comparison["reason"], "environment-mismatch")

    def test_manifest_drift_invalidates_current_assessment_and_reopens_queue(self) -> None:
        case = self.store.create_task_case("drift-case")
        assessment = self.store.create_assessment_revision(
            case["id"], expected_revision=0, contract_version_id="contract-v1",
            assessability="assessable", automated_verdict="pass", freshness="current",
        )
        review = self.store.create_review_task(case["id"], assessment["id"], "calibration")
        report = self.root / "report.md"
        report.write_text("before", encoding="utf-8")
        selector = ArtifactSelector(self.root, ("*.md",))
        before = self.collector.collect_manifest(
            selector, "after-artifacts", task_case_id=case["id"], observation_group_id="drift",
        )
        report.write_text("after", encoding="utf-8")
        after = self.collector.collect_manifest(
            selector, "after-check", task_case_id=case["id"], observation_group_id="drift",
        )
        result = self.collector.compare_manifests(before["id"], after["id"])
        self.assertEqual(result["freshness"], "stale")
        current = self.store.execute(
            "SELECT freshness, process_state, is_current FROM outcome_assessments WHERE id=?",
            (assessment["id"],),
        ).fetchone()
        self.assertEqual(tuple(current), ("stale", "invalidated", 0))
        queue = self.store.execute(
            "SELECT status, queue_reason FROM review_tasks WHERE id=?", (review["id"],)
        ).fetchone()
        self.assertEqual(tuple(queue), ("open", "artifact-drift"))

    def test_manifest_freshness_is_scoped_and_replay_cannot_revive_stale_check(self) -> None:
        case = self.store.create_task_case("scoped-drift")
        report = self.root / "report.md"
        report.write_text("before", encoding="utf-8")
        selector = ArtifactSelector(self.root, ("*.md",))
        expected_a = self.collector.collect_manifest(
            selector, "after-artifacts", task_case_id=case["id"], observation_group_id="group-a",
        )
        expected_b = self.collector.collect_manifest(
            selector, "after-artifacts", task_case_id=case["id"], observation_group_id="group-b",
        )
        observed_b = self.collector.collect_manifest(
            selector, "after-check", task_case_id=case["id"], observation_group_id="group-b",
        )
        self.assertEqual(self.collector.compare_manifests(expected_b["id"], observed_b["id"])["freshness"], "current")
        binding_a = self.collector.checker_binding(expected_a["id"], case["id"], self.root)
        binding_b = self.collector.checker_binding(expected_b["id"], case["id"], self.root)
        with self.store.transaction():
            for check_id, artifact_id in (("check-a", binding_a["artifactId"]), ("check-b", binding_b["artifactId"])):
                self.store.execute(
                    """INSERT INTO check_runs(id, task_case_id, artifact_id, checker_id, checker_version,
                           approval_version, status, assertion_outcome, result_json,
                           started_at, finished_at, freshness)
                       VALUES (?, ?, ?, 'document-artifact', '1', 'local-admin-v1', 'finished',
                           'assertion-pass', '{}', ?, ?, 'current')""",
                    (check_id, case["id"], artifact_id, "2026-08-24T00:00:00Z", "2026-08-24T00:00:01Z"),
                )
        report.write_text("after", encoding="utf-8")
        observed_a = self.collector.collect_manifest(
            selector, "after-check", task_case_id=case["id"], observation_group_id="group-a",
        )
        self.assertEqual(self.collector.compare_manifests(expected_a["id"], observed_a["id"])["freshness"], "stale")
        self.collector.compare_manifests(expected_b["id"], observed_b["id"])
        states = dict(self.store.execute("SELECT id, freshness FROM check_runs").fetchall())
        self.assertEqual(states, {"check-a": "stale", "check-b": "current"})

    def test_replacing_after_artifacts_revokes_removed_files_checks_and_assessment(self) -> None:
        case = self.store.create_task_case("replaced-observation")
        report = self.root / "report.md"
        report.write_text("present", encoding="utf-8")
        expected = self.collector.collect_manifest(
            ArtifactSelector(self.root, ("*.md",)), "after-artifacts",
            task_case_id=case["id"], observation_group_id="replace-group",
        )
        binding = self.collector.checker_binding(expected["id"], case["id"], self.root)
        assessment = self.store.create_assessment_revision(
            case["id"], expected_revision=0, contract_version_id="contract-v1",
            assessability="assessable", automated_verdict="pass", freshness="current",
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO check_runs(id, task_case_id, case_revision, artifact_id,
                       checker_id, checker_version, approval_version, status, assertion_outcome,
                       result_json, started_at, finished_at, freshness)
                   VALUES ('old-check', ?, 1, ?, 'document-artifact', '1',
                       'document-exists-v1', 'finished', 'assertion-pass', '{}', ?, ?, 'current')""",
                (case["id"], binding["artifactId"], "2026-08-24T00:00:00Z", "2026-08-24T00:00:01Z"),
            )
        report.unlink()
        replacement = self.collector.collect_manifest(
            ArtifactSelector(self.root, ("*.md",)), "after-artifacts",
            task_case_id=case["id"], observation_group_id="replace-group",
        )
        self.assertEqual(self.store.execute(
            "SELECT freshness FROM check_runs WHERE id='old-check'"
        ).fetchone()[0], "stale")
        self.assertEqual(self.store.execute(
            "SELECT is_current FROM outcome_assessments WHERE id=?", (assessment["id"],)
        ).fetchone()[0], 0)
        self.assertEqual(self.store.execute(
            "SELECT freshness FROM artifacts WHERE artifact_type='file' AND task_case_id=?",
            (case["id"],),
        ).fetchone()[0], "stale")
        report.write_text("present", encoding="utf-8")
        with self.assertRaisesRegex(CollectorValidationError, "superseded"):
            self.collector.checker_binding(expected["id"], case["id"], self.root)
        current_binding = self.collector.checker_binding(replacement["id"], case["id"], self.root)
        self.assertEqual(current_binding["id"], replacement["id"])

    def test_checker_is_never_called_without_every_authorization_boundary(self) -> None:
        with self.assertRaises(CollectorValidationError):
            ProspectiveCollector(
                self.store,
                allowed_sources=["pi"],
                allowed_roots=[self.root],
                checker_runner=Mock(),
            )
        runner = BubblewrapCheckerRunner(
            bwrap_path="/missing/bwrap",
            allowed_workspace_roots=[self.root],
        )
        collector = ProspectiveCollector(
            self.store,
            allowed_sources=["pi"],
            allowed_roots=[self.root],
            checker_runner=runner,
            checker_allowlist=["trusted-checker"],
            allowed_workspace_roots=[self.root],
        )
        with patch.object(runner, "run", return_value={"outcome": "assertion-pass"}) as run:
            with self.assertRaises(CheckerExecutionRejected):
                collector.run_checker("trusted-checker", self.root)
            with self.assertRaises(CheckerExecutionRejected):
                collector.run_checker("untrusted-checker", self.root, authorized=True)
            outside = Path(self.temporary.name) / "outside-workspace"
            outside.mkdir()
            with self.assertRaises(CheckerExecutionRejected):
                collector.run_checker("trusted-checker", outside, authorized=True)
            with self.assertRaises(CheckerExecutionRejected):
                collector.run_checker(
                    "trusted-checker", self.root, authorized=True, timeout_seconds=float("nan")
                )
            run.assert_not_called()

    def test_authorized_checker_uses_runner_and_redacts_entire_result(self) -> None:
        runner = BubblewrapCheckerRunner(
            bwrap_path="/missing/bwrap",
            allowed_workspace_roots=[self.root],
        )
        checker_result = {
            "outcome": "assertion-fail",
            "stdout": "token=very-secret-value",
            "nested": {
                "detail": "Authorization: Bearer hidden-secret-value",
                "password": "short-secret",
            },
        }
        collector = ProspectiveCollector(
            self.store,
            allowed_sources=["pi"],
            allowed_roots=[self.root],
            checker_runner=runner,
            checker_allowlist=["trusted-checker"],
            allowed_workspace_roots=[self.root],
        )
        with patch.object(runner, "run", return_value=checker_result) as run:
            result = collector.run_checker("trusted-checker", self.root, authorized=True)
            run.assert_called_once()
            self.assertNotIn("very-secret", result["stdout"])
            self.assertNotIn("hidden-secret", result["nested"]["detail"])
            self.assertNotIn("short-secret", result["nested"]["password"])
            run.return_value = {"outcome": "assertion-pass", "unsafe": ("secret",)}
            with self.assertRaises(CollectorError):
                collector.run_checker("trusted-checker", self.root, authorized=True)

    def test_cleanup_filters_records_and_retains_hashed_audit_summary(self) -> None:
        self.collector.record_event(self.event())
        (self.root / "report.md").write_text("report", encoding="utf-8")
        self.collector.collect_manifest(
            ArtifactSelector(self.root, project_id="project-1", skill_id="skill-1"),
            "after-artifacts",
        )
        self.collector.record_event(
            self.event(event_id="event-2", project_id="project-2", skill_id="skill-2")
        )
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        result = self.collector.cleanup(
            older_than=cutoff,
            project_id="project-1",
            skill_id="skill-1",
        )
        self.assertEqual(result["deletedEvents"], 1)
        self.assertEqual(result["deletedManifests"], 1)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM prospective_events").fetchone()[0], 1)
        audit = self.store.execute("SELECT * FROM prospective_cleanup_audits").fetchone()
        self.assertEqual(audit["deleted_event_count"], 1)
        self.assertEqual(
            audit["project_id_hash"], hashlib.sha256(b"project-1").hexdigest()
        )
        self.assertNotIn("project-1", json.dumps(dict(audit)))

    def test_cleanup_infers_project_from_bound_task_case(self) -> None:
        case = self.store.create_task_case("cleanup-bound", metadata={"projectId": "bound-project"})
        self.collector.record_event(self.event(
            event_id="bound-event", project_id=None, skill_id=None, task_case_id=case["id"],
            payload={"text": "sensitive bound content"},
        ))
        (self.root / "bound.md").write_text("bound", encoding="utf-8")
        self.collector.collect_manifest(
            ArtifactSelector(self.root, ("*.md",)), "after-artifacts",
            task_case_id=case["id"], observation_group_id="bound-cleanup",
        )
        self.assertEqual(self.collector.materialize_cleanup_context(), 2)
        with self.store.transaction():
            self.store.execute(
                "UPDATE task_cases SET metadata_json=? WHERE id=?",
                (json.dumps({"purged": True}), case["id"]),
            )
        fresh_collector = ProspectiveCollector(
            self.store, allowed_sources=["pi"], allowed_roots=[self.root],
            collector_version="collector-test-1", environment={"profile": "test"},
        )
        result = fresh_collector.cleanup(project_id="bound-project")
        self.assertEqual(result["deletedEvents"], 1)
        self.assertEqual(result["deletedManifests"], 1)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM prospective_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()