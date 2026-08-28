import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from effect_store import EffectStore, EffectStoreError, ImmutableSnapshotError
from feedback_service import FeedbackService
from quality_service import SkillQualityService


NOW = "2026-08-26T00:00:00Z"


class SkillQualityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EffectStore(Path(self.tmp.name) / "effects.sqlite3")
        self.feedback = FeedbackService(self.store, formal_scope_fingerprint="quality-scope")
        self.service = SkillQualityService(self.store, None, self.feedback)
        self.admin = self.store.create_actor("Admin", roles=["admin", "reviewer", "trial_user"])
        self.trial = self.store.create_actor("Trial", roles=["trial_user"])
        self.case = self.store.create_task_case("quality-case", task_type="coding")
        self.invocation = self._invocation("quality-skill", "a" * 64, "direct")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _invocation(self, skill_id: str, sha: str | None, attribution: str, suffix: str = "",
        *, load_status: str = "loaded", validity: str = "valid", invocation_kind: str = "business-use",
    ) -> str:
        event = self.store.upsert_event(
            f"event-{skill_id}-{sha}-{suffix}", source="pi", session_family="quality-family",
            event_type="user_message", payload_hash=f"hash-{skill_id}-{sha}-{suffix}",
            payload={"text": "完成一个编码任务", "metadata": {}}, protocol_time=NOW,
        )
        session = self.store.create_session("pi", f"session-{skill_id}-{sha}-{suffix}", session_family="quality-family")
        episode_id = f"episode-{skill_id}-{sha}-{suffix}"
        invocation_id = f"invocation-{skill_id}-{sha}-{suffix}"
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO task_episodes(id, episode_fingerprint, session_id, start_event_id,
                       end_event_id, goal_text, process_state, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, '完成一个编码任务', 'indexed', '{}', ?, ?)""",
                (episode_id, episode_id, session["id"], event["id"], event["id"], NOW, NOW),
            )
            self.store.execute(
                "INSERT OR IGNORE INTO task_case_episodes(task_case_id, task_episode_id, relationship) VALUES (?, ?, 'primary')",
                (self.case["id"], episode_id),
            )
            self.store.execute(
                """INSERT INTO skill_invocations(id, invocation_fingerprint, task_episode_id, event_id,
                       skill_id, skill_sha256, invocation_kind, load_status, validity, created_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
                (invocation_id, invocation_id, episode_id, event["id"], skill_id, sha,
                 invocation_kind, load_status, validity, NOW),
            )
            self.store.execute(
                """INSERT INTO attribution_links(id, task_case_id, skill_invocation_id,
                       attribution_kind, confidence, status, created_at)
                   VALUES (?, ?, ?, ?, 1, 'active', ?)""",
                (f"link-{invocation_id}", self.case["id"], invocation_id, attribution, NOW),
            )
        return invocation_id

    def _complete_scan(self) -> dict:
        scan = self.store.create_scan_run(
            "pi", metadata={"scopeKind": "configured-catalog", "scopeFingerprint": "quality-scope"},
        )
        return self.store.finish_scan_run(scan["id"], coverage_status="complete")

    def test_assignment_is_required_and_judgment_freezes_evidence(self) -> None:
        with self.assertRaisesRegex(EffectStoreError, "active trial assignment"):
            self.service.submit_judgment(
                self.invocation, actor_id=self.trial["id"], expected_revision=0,
                verdict="helpful", attribution_relation="direct-skill-use",
            )
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        self.assertEqual((judgment["revision"], judgment["aggregation_eligibility"]), (1, "aggregate-eligible"))
        snapshot = judgment["evidence_snapshot"]
        self.assertEqual((snapshot["skill_sha256"], snapshot["contract_version_id"]), ("a" * 64, "no-contract"))
        self.assertFalse(snapshot["evidence"]["relatedFeedback"]["contentAvailable"])
        self.assertNotIn("excerpt", json.dumps(snapshot["evidence"]).lower())

    def test_new_judgment_supersedes_current_and_shared_is_not_aggregate_eligible(self) -> None:
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        first = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        second = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=1,
            verdict="cannot-judge", attribution_relation="cannot-attribute",
        )
        first_row = self.store.execute("SELECT is_current FROM skill_use_judgments WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual((first_row["is_current"], second["revision"], second["aggregation_eligibility"]), (0, 2, "individual-only"))
        shared = self._invocation("shared-skill", "b" * 64, "shared")
        self.service.assign(shared, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judged = self.service.submit_judgment(
            shared, actor_id=self.trial["id"], expected_revision=0,
            verdict="not-helpful", attribution_relation="direct-skill-use", reason_code="incomplete-result",
        )
        self.assertEqual(judged["aggregation_eligibility"], "shared-only")

    def test_withdrawal_is_an_append_only_revision(self) -> None:
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        first = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        withdrawn = self.service.withdraw_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=1,
        )
        self.assertEqual((withdrawn["revision"], withdrawn["verdict"], withdrawn["supersedes_id"]),
                         (2, "withdrawn", first["id"]))

    def test_not_helpful_referral_converts_to_independent_trial_feedback_signal(self) -> None:
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="not-helpful", attribution_relation="direct-skill-use", reason_code="incomplete-result",
        )
        referral = judgment["referral"]
        self.assertEqual(referral["status"], "pending-review")
        resolved = self.service.decide_referral(
            referral["id"], actor_id=self.admin["id"], action="convert", reason_code="reviewed",
        )
        self.assertEqual(resolved["status"], "converted")
        revision = self.store.execute(
            """SELECT channel, source FROM feedback_signal_revisions WHERE feedback_signal_id=?""",
            (resolved["feedback_signal_id"],),
        ).fetchone()
        self.assertEqual((revision["channel"], revision["source"]), ("trial-experience", "trial-judgment"))
        self.assertEqual(self.store.execute(
            "SELECT COUNT(*) FROM outcome_assessments WHERE subject_key LIKE 'feedback:%'"
        ).fetchone()[0], 0)
        self.assertEqual(self.feedback.feedback_snapshot_candidates(), [])
        history = self.service.judgments(self.invocation, actor_id=self.trial["id"])["items"][0]
        self.assertEqual(history["referral"], {"status": "converted"})
        self.assertNotIn("reviewer_actor_id", json.dumps(history))

    def test_assignment_snapshot_becomes_stale_and_trial_history_hides_reviewer_data(self) -> None:
        assignment = self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        self.store.execute("UPDATE task_cases SET current_revision=2 WHERE id=?", (self.case["id"],))
        with self.assertRaisesRegex(EffectStoreError, "evidence is stale"):
            self.service.submit_judgment(
                self.invocation, actor_id=self.trial["id"], expected_revision=0,
                verdict="helpful", attribution_relation="direct-skill-use",
            )
        self.assertTrue(self.service.assignments(self.trial["id"])["items"][0]["evidence_stale"])
        self.assertEqual(assignment["status"], "active")

    def test_expired_assignment_is_retained_as_read_only(self) -> None:
        self.service.assign(
            self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"],
            expires_at="2020-01-01T00:00:00Z",
        )
        assignment = self.service.assignments(self.trial["id"])["items"][0]
        self.assertTrue((assignment["evidence_expired"], assignment["evidence_stale"]))
        with self.assertRaisesRegex(EffectStoreError, "active trial assignment"):
            self.service.submit_judgment(
                self.invocation, actor_id=self.trial["id"], expected_revision=0,
                verdict="helpful", attribution_relation="direct-skill-use",
            )

    def test_quality_snapshot_deduplicates_same_actor_case_version(self) -> None:
        self.store.create_assessment_revision(
            self.case["id"], expected_revision=0, case_revision=1,
            skill_invocation_id=self.invocation, skill_id="quality-skill", skill_sha256="a" * 64,
            attribution_kind="direct", contract_version_id="contract-quality",
            assessability="assessable", automated_verdict="pass", freshness="current",
        )
        second = self._invocation("quality-skill", "a" * 64, "direct", suffix="second")
        self._complete_scan()
        for invocation in (self.invocation, second):
            self.service.assign(invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
            judgment = self.service.submit_judgment(
                invocation, actor_id=self.trial["id"], expected_revision=0,
                verdict="helpful", attribution_relation="direct-skill-use",
            )
            self.store.execute(
                "UPDATE skill_use_judgments SET contract_version_id='contract-quality' WHERE id=?", (judgment["id"],)
            )
            self.store.execute(
                "UPDATE use_evidence_snapshots SET contract_version_id='contract-quality' WHERE id=?",
                (judgment["evidence_snapshot_id"],),
            )
        snapshot = self.service.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self.assertEqual(snapshot["summary"]["eligible"], 1)
        self.assertEqual(sum(item["metric_eligible"] for item in snapshot["items"]), 1)

    def test_superseded_judgment_referral_cannot_convert(self) -> None:
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        first = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="not-helpful", attribution_relation="direct-skill-use", reason_code="incomplete-result",
        )
        self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=1,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        with self.assertRaisesRegex(Exception, "superseded"):
            self.service.decide_referral(
                first["referral"]["id"], actor_id=self.admin["id"], action="convert", reason_code="reviewed",
            )

    def test_partial_coverage_is_not_publishable_and_complete_snapshot_is_immutable(self) -> None:
        self.store.create_assessment_revision(
            self.case["id"], expected_revision=0, case_revision=1,
            skill_invocation_id=self.invocation, skill_id="quality-skill", skill_sha256="a" * 64,
            attribution_kind="direct", contract_version_id="contract-quality",
            assessability="assessable", automated_verdict="pass", freshness="current",
        )
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        partial = self.store.create_scan_run("pi", metadata={"scopeKind": "configured-catalog", "scopeFingerprint": "quality-scope"})
        self.store.finish_scan_run(partial["id"], coverage_status="partial")
        detail = self.service.detail("quality-skill", "a" * 64)
        self.assertEqual(detail["quality_status"], "not-publishable")
        with self.assertRaisesRegex(EffectStoreError, "complete latest scan"):
            self.service.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self._complete_scan()
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=1,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        snapshot = self.service.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self.assertEqual((snapshot["sealed"], snapshot["summary"]["eligible"]), (1, 1))
        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute("UPDATE skill_quality_snapshot_items SET metric_eligible=0 WHERE snapshot_id=?", (snapshot["id"],))

    def test_compare_refuses_partial_or_missing_formal_scope(self) -> None:
        other = self._invocation("other-skill", "c" * 64, "direct")
        partial = self.store.create_scan_run("pi", metadata={"scopeKind": "configured-catalog", "scopeFingerprint": "quality-scope"})
        self.store.finish_scan_run(partial["id"], coverage_status="partial")
        compared = self.service.compare([
            {"skillId": "quality-skill", "sha": "a" * 64},
            {"skillId": "other-skill", "sha": "c" * 64},
        ])
        self.assertFalse(compared["comparable"])
        self.assertIn("comparison-requires-complete-shared-scope", compared["reasons"])

    def test_formal_results_do_not_mix_contract_versions(self) -> None:
        scan = self._complete_scan()
        snapshot_id = "formal-quality-snapshot"
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO metric_snapshots(id, scan_run_id, cutoff_at, coverage_status,
                       dimensions_json, versions_json, summary_json, sealed, created_at)
                   VALUES (?, ?, ?, 'complete', ?, '{}', '{}', 0, ?)""",
                (snapshot_id, scan["id"], NOW,
                 json.dumps({"scanScopeFingerprint": "quality-scope"}), NOW),
            )
            for contract_id, verdict in (("contract-a", "pass"), ("contract-b", "fail")):
                self.store.execute(
                    """INSERT INTO metric_snapshot_cases(
                           snapshot_id, task_case_id, task_case_revision, skill_id, skill_sha256,
                           skill_sha256_key, contract_version_id, contract_version_key, task_type,
                           attribution_kind, case_anchor_invocation_id, effective_verdict,
                           metric_eligible, frozen_json)
                       VALUES (?, ?, 1, 'quality-skill', ?, ?, ?, ?, 'coding', 'direct', ?, ?, 1, '{}')""",
                    (snapshot_id, self.case["id"], "a" * 64, "a" * 64, contract_id,
                     contract_id, self.invocation, verdict),
                )
            self.store.execute("UPDATE metric_snapshots SET sealed=1 WHERE id=?", (snapshot_id,))
        detail = self.service.detail("quality-skill", "a" * 64)
        self.assertTrue(detail["formal_results"]["mixed_contracts"])
        self.assertEqual(detail["formal_results"]["eligible_cases"], 0)
        self.assertIn("multiple-statistical-keys", detail["blocking_reasons"])
        self.assertIsNone(self.service._formal_results(
            "quality-skill", "a" * 64, attribution="shared", coverage=detail["coverage"],
        )["snapshot_id"])

    def test_skill_quality_does_not_use_another_skill_targeted_snapshot(self) -> None:
        scan = self._complete_scan()
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO metric_snapshots(id, scan_run_id, cutoff_at, coverage_status,
                       dimensions_json, versions_json, summary_json, sealed, created_at)
                   VALUES ('other-skill-snapshot', ?, ?, 'complete', ?, '{}', '{}', 1, ?)""",
                (scan["id"], NOW, json.dumps({"scanScopeFingerprint": "quality-scope", "skillId": "other-skill"}), NOW),
            )
        formal = self.service._formal_results("quality-skill", "a" * 64, coverage=self.service._coverage())
        self.assertIsNone(formal["snapshot_id"])

    def test_quality_snapshot_excludes_changed_case_revision(self) -> None:
        self.store.create_assessment_revision(
            self.case["id"], expected_revision=0, case_revision=1,
            skill_invocation_id=self.invocation, skill_id="quality-skill", skill_sha256="a" * 64,
            attribution_kind="direct", contract_version_id="contract-quality",
            assessability="assessable", automated_verdict="pass", freshness="current",
        )
        self._complete_scan()
        self.service.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = self.service.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        self.store.execute("UPDATE skill_use_judgments SET contract_version_id='contract-quality' WHERE id=?", (judgment["id"],))
        self.store.execute("UPDATE use_evidence_snapshots SET contract_version_id='contract-quality' WHERE id=?", (judgment["evidence_snapshot_id"],))
        self.store.execute("UPDATE task_cases SET current_revision=2 WHERE id=?", (self.case["id"],))
        snapshot = self.service.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self.assertEqual(snapshot["summary"]["eligible"], 0)
        self.assertEqual(snapshot["items"][0]["exclusion_reason"], "evidence-case-revision-stale")

    def test_quality_snapshot_read_rejects_later_invalid_invocation(self) -> None:
        scoped = SkillQualityService(
            self.store, None, self.feedback, eligible_skill_versions={"quality-skill": "a" * 64},
        )
        self._complete_scan()
        scoped.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = scoped.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        self.store.execute("UPDATE skill_use_judgments SET contract_version_id='contract-quality' WHERE id=?", (judgment["id"],))
        self.store.execute("UPDATE use_evidence_snapshots SET contract_version_id='contract-quality' WHERE id=?", (judgment["evidence_snapshot_id"],))
        snapshot = scoped.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self.assertEqual(scoped.quality_snapshot(snapshot["id"])["id"], snapshot["id"])
        self.store.execute("UPDATE skill_invocations SET load_status='pending' WHERE id=?", (self.invocation,))
        with self.assertRaises(KeyError):
            scoped.quality_snapshot(snapshot["id"])

    def test_directory_excludes_parser_placeholder_skill_ids(self) -> None:
        placeholder = self._invocation("*", "d" * 64, "direct")
        self.assertTrue(placeholder)
        directory = self.service.directory()
        self.assertNotIn("*", [item["skill_id"] for item in directory["items"]])

    def test_directory_task_type_counts_only_matching_cases(self) -> None:
        deploy = self.store.create_task_case("deploy-case", task_type="deploy")
        episode_id = self.store.execute(
            "SELECT task_episode_id FROM skill_invocations WHERE id=?", (self.invocation,)
        ).fetchone()[0]
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO task_case_episodes(task_case_id, task_episode_id, relationship) VALUES (?, ?, 'related')",
                (deploy["id"], episode_id),
            )
            self.store.execute(
                """INSERT INTO attribution_links(id, task_case_id, skill_invocation_id,
                       attribution_kind, confidence, status, created_at)
                   VALUES ('deploy-link', ?, ?, 'direct', 1, 'active', ?)""",
                (deploy["id"], self.invocation, NOW),
            )
        directory = self.service.directory(task_type="coding")
        item = next(row for row in directory["items"] if row["skill_id"] == "quality-skill")
        self.assertEqual(item["case_count"], 1)

    def test_directory_cursor_does_not_skip_colon_skill_ids(self) -> None:
        self._invocation("a", "e" * 64, "direct")
        self._invocation("a:", "f" * 64, "direct")
        first = self.service.directory(limit=1)
        self.assertEqual(first["items"][0]["skill_id"], "a")
        second = self.service.directory(limit=10, cursor=first["next_cursor"])
        self.assertIn("a:", [item["skill_id"] for item in second["items"]])

    def test_current_enabled_scope_excludes_disabled_and_non_loaded_invocations(self) -> None:
        disabled = self._invocation("disabled-skill", "b" * 64, "direct")
        pending = self._invocation("pending-skill", "c" * 64, "direct", load_status="pending")
        maintenance = self._invocation("maintenance-skill", "d" * 64, "direct", invocation_kind="skill-maintenance")
        unknown = self._invocation("unknown-skill", None, "direct")
        scoped = SkillQualityService(
            self.store, None, self.feedback,
            eligible_skill_versions={"quality-skill": "a" * 64, "pending-skill": "c" * 64, "unknown-skill": "e" * 64},
        )
        self.assertEqual([item["skill_id"] for item in scoped.directory()["items"]], ["quality-skill"])
        with self.assertRaises(KeyError):
            scoped.detail("disabled-skill", "b" * 64)
        for invocation in (disabled, pending, maintenance, unknown):
            with self.assertRaises((KeyError, EffectStoreError)):
                scoped.assign(invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        scoped.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = scoped.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="not-helpful", attribution_relation="direct-skill-use", reason_code="incomplete-result",
        )
        scoped.eligible_skill_versions.clear()
        self.assertEqual(scoped.assignments(self.trial["id"])["items"], [])
        self.assertEqual(scoped.judgments(self.invocation, actor_id=self.trial["id"])["items"], [])
        with self.assertRaisesRegex(EffectStoreError, "outside the current quality scope"):
            scoped.decide_referral(
                judgment["referral"]["id"], actor_id=self.admin["id"], action="close", reason_code="reviewed",
            )

    def test_enabled_skill_keeps_historical_loaded_versions(self) -> None:
        scoped = SkillQualityService(
            self.store, None, self.feedback, eligible_skill_versions={"quality-skill": "z" * 64},
        )
        directory = scoped.directory()
        item = next(row for row in directory["items"] if row["skill_id"] == "quality-skill")
        self.assertEqual((item["skill_sha256"], item["current_enabled_sha"], item["is_current_enabled_version"]),
                         ("a" * 64, "z" * 64, False))

    def test_enabled_skill_historical_version_is_retained_in_quality_snapshot(self) -> None:
        scoped = SkillQualityService(
            self.store, None, self.feedback, eligible_skill_versions={"quality-skill": "z" * 64},
        )
        self._complete_scan()
        scoped.assign(self.invocation, actor_id=self.trial["id"], assigned_by_actor_id=self.admin["id"])
        judgment = scoped.submit_judgment(
            self.invocation, actor_id=self.trial["id"], expected_revision=0,
            verdict="helpful", attribution_relation="direct-skill-use",
        )
        self.store.execute("UPDATE skill_use_judgments SET contract_version_id='contract-history' WHERE id=?", (judgment["id"],))
        self.store.execute("UPDATE use_evidence_snapshots SET contract_version_id='contract-history' WHERE id=?", (judgment["evidence_snapshot_id"],))
        snapshot = scoped.seal_snapshot(expected_scope_fingerprint="quality-scope")
        self.assertEqual(snapshot["items"][0]["skill_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()