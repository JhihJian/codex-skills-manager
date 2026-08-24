import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from effect_store import EffectStore, ImmutableSnapshotError, RevisionConflict, SCHEMA_VERSION


class EffectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "effect.sqlite3"
        self.store = EffectStore(self.db_path, busy_timeout_ms=2345)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def create_generation(self, key: str = "g1") -> dict:
        log_file = self.store.upsert_log_file("pi", "stable-log")
        return self.store.upsert_generation(log_file["id"], key, "pi-v3")

    def create_event(self, fingerprint: str = "event-1") -> dict:
        return self.store.upsert_event(
            fingerprint,
            source="pi",
            session_family="family-1",
            event_type="message",
            payload_hash=f"hash-{fingerprint}",
            payload={"text": fingerprint},
        )

    def create_review(self) -> tuple[dict, dict, dict, dict]:
        actor = self.store.create_actor("Reviewer", roles=["reviewer"])
        case = self.store.create_task_case("case-1", task_type="coding")
        assessment = self.store.create_assessment_revision(
            case["id"],
            expected_revision=0,
            case_revision=1,
            contract_version_id="contract-v1",
            assessability="assessable",
            automated_verdict="pass",
        )
        review = self.store.create_review_task(case["id"], assessment["id"], "calibration")
        return actor, case, assessment, review

    def test_migration_is_idempotent_and_schema_is_complete(self) -> None:
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        tables = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected = {
            "scan_runs", "log_files", "log_file_generations", "log_file_locations",
            "file_checkpoints", "canonical_events", "event_provenance", "sessions",
            "session_edges", "task_episodes", "task_cases", "task_facts",
            "task_classifications", "skill_invocations", "tool_calls", "tool_results",
            "artifacts", "evidence_items", "attribution_links", "check_runs",
            "semantic_reviews", "outcome_assessments", "review_tasks", "manual_decisions",
            "corrections", "exceptions", "actors", "metric_snapshots",
            "metric_snapshot_cases", "prospective_events",
        }
        self.assertTrue(expected <= tables)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1
        )

    def test_failed_migration_rolls_back_ddl(self) -> None:
        broken_schema = "CREATE TABLE migration_probe(id INTEGER); THIS IS NOT SQL;"
        with patch("effect_store.SCHEMA", broken_schema), self.assertRaises(sqlite3.OperationalError):
            self.store.migrate()
        probe = self.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
        ).fetchone()
        self.assertIsNone(probe)

    def test_event_can_have_multiple_provenances_and_orphan_state_tracks_deletion(self) -> None:
        generation_1 = self.create_generation("g1")
        generation_2 = self.create_generation("g2")
        event = self.create_event()
        self.assertEqual(event["orphaned"], 1)
        self.store.upsert_provenance(event["id"], generation_1["id"], 10, line_number=2)
        self.store.upsert_provenance(event["id"], generation_2["id"], 20, line_number=3)
        self.assertEqual(self.store.get_event(event["id"])["provenance_count"], 2)

        self.store.delete_generation(generation_1["id"])
        current = self.store.get_event(event["id"])
        self.assertEqual(current["provenance_count"], 1)
        self.assertEqual(current["orphaned"], 0)

        self.store.delete_generation(generation_2["id"])
        current = self.store.get_event(event["id"])
        self.assertEqual(current["provenance_count"], 0)
        self.assertEqual(current["orphaned"], 1)

        generation_3 = self.create_generation("g3")
        self.store.upsert_provenance(event["id"], generation_3["id"], 0)
        self.assertEqual(self.store.get_event(event["id"])["orphaned"], 0)

    def test_generation_deletion_does_not_delete_manual_decision(self) -> None:
        generation = self.create_generation()
        event = self.create_event()
        self.store.upsert_provenance(event["id"], generation["id"], 0)
        actor, _case, _assessment, review = self.create_review()
        decision = self.store.write_manual_decision(
            review["id"], actor_id=actor["id"], expected_revision=0,
            verdict="pass", reason_code="verified",
            binding={"eventFingerprint": event["event_fingerprint"]},
        )

        self.assertTrue(self.store.delete_generation(generation["id"]))
        persisted = self.store.connection.execute(
            "SELECT id FROM manual_decisions WHERE id = ?", (decision["id"],)
        ).fetchone()
        self.assertIsNotNone(persisted)
        self.assertEqual(self.store.get_event(event["id"])["orphaned"], 1)

    def test_assessment_and_manual_decision_revision_conflicts(self) -> None:
        actor, case, _assessment, review = self.create_review()
        with self.assertRaises(RevisionConflict):
            self.store.create_assessment_revision(case["id"], expected_revision=0)

        first = self.store.write_manual_decision(
            review["id"], actor_id=actor["id"], expected_revision=0,
            verdict="partial", reason_code="partial-evidence",
        )
        self.assertEqual(first["revision"], 1)
        with self.assertRaises(RevisionConflict):
            self.store.write_manual_decision(
                review["id"], actor_id=actor["id"], expected_revision=0,
                verdict="pass", reason_code="stale-tab",
            )
        second = self.store.write_manual_decision(
            review["id"], actor_id=actor["id"], expected_revision=1,
            verdict="pass", reason_code="more-evidence",
        )
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["supersedes_id"], first["id"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM manual_decisions WHERE review_task_id = ?", (review["id"],)
            ).fetchone()[0], 2,
        )
        self.assertEqual(second["binding"]["assessmentRevision"], 1)

    def test_review_task_rejects_assessment_from_another_case(self) -> None:
        _actor, _case, assessment, _review = self.create_review()
        other = self.store.create_task_case("case-2")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_review_task(other["id"], assessment["id"], "wrong-case")

    def test_correction_and_exception_use_expected_revision(self) -> None:
        actor, case, assessment, _review = self.create_review()
        correction = self.store.append_correction(
            case["id"], actor_id=actor["id"], expected_revision=0,
            correction_type="attribution", reason_code="wrong-skill",
        )
        self.assertEqual(correction["revision"], 1)
        with self.assertRaises(RevisionConflict):
            self.store.append_correction(
                case["id"], actor_id=actor["id"], expected_revision=0,
                correction_type="duplicate", reason_code="stale",
            )
        exception = self.store.append_exception(
            case["id"], assessment_id=assessment["id"], actor_id=actor["id"],
            expected_revision=0, reason_code="approved-deviation",
        )
        self.assertEqual(exception["revision"], 1)

    def test_metric_snapshot_is_immutable(self) -> None:
        _actor, case, assessment, _review = self.create_review()
        snapshot = self.store.create_metric_snapshot(
            cutoff_at="2026-08-24T12:00:00Z",
            coverage_status="complete",
            dimensions={"skill": "demo"},
            versions={"parser": "v1", "contract": "v1"},
            cases=[{
                "task_case_id": case["id"],
                "task_case_revision": 1,
                "assessment_id": assessment["id"],
                "assessment_revision": 1,
                "skill_id": "demo",
                "skill_sha256": "abc",
                "contract_version_id": "contract-v1",
                "task_type": "coding",
                "attribution_kind": "primary",
                "effective_verdict": "pass",
                "metric_eligible": True,
            }],
        )
        self.assertEqual(snapshot["sealed"], 1)
        self.assertEqual(len(snapshot["cases"]), 1)

        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute(
                "UPDATE metric_snapshots SET coverage_status = 'partial' WHERE id = ?", (snapshot["id"],)
            )
        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute(
                "UPDATE metric_snapshot_cases SET effective_verdict = 'fail' WHERE snapshot_id = ?",
                (snapshot["id"],),
            )
        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute("DELETE FROM metric_snapshots WHERE id = ?", (snapshot["id"],))

    def test_empty_metric_snapshot_cannot_be_replaced(self) -> None:
        snapshot = self.store.create_metric_snapshot(
            cutoff_at="2026-08-24T12:00:00Z",
            coverage_status="complete",
            dimensions={},
            versions={},
            cases=[],
        )
        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute(
                """INSERT OR REPLACE INTO metric_snapshots(
                       id, cutoff_at, coverage_status, dimensions_json, versions_json,
                       summary_json, sealed, created_at
                   ) VALUES (?, ?, ?, '{}', '{}', '{}', 1, ?)""",
                (snapshot["id"], "later", "partial", "later"),
            )
        external = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                external.execute(
                    """INSERT OR REPLACE INTO metric_snapshots(
                           id, cutoff_at, coverage_status, dimensions_json, versions_json,
                           summary_json, sealed, created_at
                       ) VALUES (?, ?, ?, '{}', '{}', '{}', 1, ?)""",
                    (snapshot["id"], "later", "partial", "later"),
                )
        finally:
            external.close()

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not available on Windows")
    def test_wal_foreign_keys_busy_timeout_and_permissions(self) -> None:
        self.assertEqual(str(self.store.pragma("journal_mode")).lower(), "wal")
        self.assertEqual(self.store.pragma("foreign_keys"), 1)
        self.assertEqual(self.store.pragma("busy_timeout"), 2345)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
        wal_path = Path(f"{self.db_path}-wal")
        self.assertTrue(wal_path.exists())
        self.assertEqual(wal_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.upsert_generation("missing", "generation", "v1")

    def test_scan_run_pagination_and_overview(self) -> None:
        scan = self.store.create_scan_run("pi")
        finished = self.store.finish_scan_run(
            scan["id"], discovered_files=2, indexed_files=2, indexed_bytes=123,
            coverage_status="complete",
        )
        self.assertEqual(finished["status"], "completed")
        for index in range(3):
            self.create_event(f"event-{index}")
        first_page = self.store.list_events(limit=2)
        second_page = self.store.list_events(limit=2, cursor=first_page["next_cursor"])
        self.assertEqual(len(first_page["items"]), 2)
        self.assertEqual(len(second_page["items"]), 1)
        self.assertIsNone(second_page["next_cursor"])
        overview = self.store.overview()
        self.assertEqual(overview["event_count"], 3)
        self.assertEqual(overview["latest_scan"]["id"], scan["id"])


if __name__ == "__main__":
    unittest.main()