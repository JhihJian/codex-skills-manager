import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from effect_store import EffectStore, EffectStoreError, RevisionConflict
from outcome_contracts import OutcomeContractStore
from outcome_reviews import OutcomeReviewService, _safe_public
from semantic_reviewer import validate_review_input


def jsonl(items):
    return b"".join(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in items)


class OutcomeReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.store = EffectStore(self.root / "effects.sqlite3")
        self.contracts = OutcomeContractStore(self.root / "skills.sqlite3")
        self.service = OutcomeReviewService(self.store, self.contracts, skill_roots=[self.root / "skills"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def pi_header(self, session="p1"):
        return {"type": "session", "id": session, "timestamp": "2026-08-24T00:00:00Z"}

    def pi_user(self, event_id="u1", text="run tests"):
        return {"type": "message", "id": event_id, "timestamp": "2026-08-24T00:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def test_incremental_append_half_line_and_tool_pairing(self):
        path = self.logs / "pi.jsonl"
        path.write_bytes(jsonl([self.pi_header(), self.pi_user()]))
        first = self.service.scan({"pi": self.logs})
        self.assertEqual((first["status"], first["indexed_files"]), ("completed", 1))
        initial_events = self.store.overview()["event_count"]

        call = {"type": "message", "id": "a1", "timestamp": "2026-08-24T00:00:02Z",
                "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "pytest"}}]}}
        result = {"type": "message", "id": "r1", "timestamp": "2026-08-24T00:00:03Z",
                  "message": {"role": "toolResult", "toolCallId": "call-1", "content": "ok"}}
        with path.open("ab") as handle:
            handle.write(jsonl([call]))
            partial = json.dumps(result).encode("utf-8")
            handle.write(partial[: len(partial) // 2])
        second = self.service.scan({"pi": self.logs})
        self.assertEqual(second["status"], "partial")
        self.assertEqual(self.store.overview()["event_count"], initial_events + 2)
        checkpoint = self.store.execute("SELECT byte_offset FROM file_checkpoints ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertLess(checkpoint, path.stat().st_size)

        with path.open("ab") as handle:
            handle.write(partial[len(partial) // 2:] + b"\n")
        third = self.service.scan({"pi": self.logs})
        self.assertEqual(third["status"], "completed")
        paired = self.store.execute(
            "SELECT c.call_id, r.status FROM tool_calls c JOIN tool_results r ON r.tool_call_id=c.id"
        ).fetchone()
        self.assertEqual(tuple(paired), ("call-1", "returned"))
        with patch.object(Path, "open", side_effect=AssertionError("unchanged JSONL must not be opened")):
            unchanged = self.service.scan({"pi": self.logs})
        self.assertEqual(unchanged["indexed_bytes"], 0)
        self.assertEqual(
            (unchanged["status"], unchanged["failed_files"], unchanged["error"]),
            ("completed", 0, []),
        )

    def test_same_size_rewrite_move_and_delete_replace_provenance(self):
        path = self.logs / "one.jsonl"
        first_body = jsonl([self.pi_header(), self.pi_user(text="run one")])
        second_body = jsonl([self.pi_header(), self.pi_user(text="run two")])
        self.assertEqual(len(first_body), len(second_body))
        path.write_bytes(first_body)
        self.service.scan({"pi": self.logs})
        generation = self.store.execute("SELECT id FROM log_file_generations").fetchone()[0]

        moved = self.logs / "moved.jsonl"
        path.rename(moved)
        self.service.scan({"pi": self.logs})
        self.assertEqual(self.store.execute("SELECT id FROM log_file_generations").fetchone()[0], generation)

        original_times = moved.stat()
        moved.write_bytes(second_body)
        # Restoring mtime must not conceal an in-place same-size replacement.
        moved.touch()
        os.utime(moved, ns=(original_times.st_atime_ns, original_times.st_mtime_ns))
        self.service.scan({"pi": self.logs})
        self.assertNotEqual(self.store.execute("SELECT id FROM log_file_generations").fetchone()[0], generation)
        live_texts = [json.loads(row[0]).get("text") for row in self.store.execute(
            "SELECT payload_json FROM canonical_events WHERE orphaned=0 AND event_type='user_message'"
        ).fetchall()]
        self.assertEqual(live_texts, ["run two"])

        moved.unlink()
        self.service.scan({"pi": self.logs})
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM log_file_generations").fetchone()[0], 0)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM canonical_events WHERE orphaned=0").fetchone()[0], 0)

    def test_truncation_creates_new_generation(self):
        path = self.logs / "truncate.jsonl"
        path.write_bytes(jsonl([self.pi_header(), self.pi_user(), self.pi_user("u2", "follow up")]))
        self.service.scan({"pi": self.logs})
        old = self.store.execute("SELECT id FROM log_file_generations").fetchone()[0]
        path.write_bytes(jsonl([self.pi_header()]))
        self.service.scan({"pi": self.logs})
        current = self.store.execute("SELECT id FROM log_file_generations").fetchone()[0]
        self.assertNotEqual(old, current)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM canonical_events WHERE orphaned=0").fetchone()[0], 1)

    def test_missing_scan_root_is_partial_and_does_not_delete_generations(self):
        path = self.logs / "retained.jsonl"
        path.write_bytes(jsonl([self.pi_header("retained"), self.pi_user(text="keep")]))
        first = self.service.scan({"pi": self.logs})
        self.assertEqual(first["coverage_status"], "complete")
        moved = self.root / "logs-offline"
        self.logs.rename(moved)
        second = self.service.scan({"pi": self.logs})
        self.assertEqual((second["status"], second["coverage_status"]), ("partial", "partial"))
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM log_file_generations").fetchone()[0], 1)
        self.assertGreater(self.store.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE orphaned=0"
        ).fetchone()[0], 0)

    def test_rewrite_removes_orphaned_machine_derived_records(self):
        path = self.logs / "derived.jsonl"
        base = [self.pi_header("derived"), self.pi_user(text="run")]
        tool = {"type": "message", "id": "tool", "timestamp": "2026-08-24T00:00:02Z",
                "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call",
                    "name": "bash", "arguments": {"command": "pytest"}}]}}
        path.write_bytes(jsonl([*base, tool]))
        self.service.scan({"pi": path})
        self.assertGreater(self.store.execute("SELECT COUNT(*) FROM task_facts").fetchone()[0], 0)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 1)

        path.write_bytes(jsonl(base))
        self.service.scan({"pi": path})
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM task_facts").fetchone()[0], 0)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 0)

    def test_rewrite_without_read_result_revokes_loaded_invocation(self):
        skill_text = "# historical\n"
        skill = self.root / "skills" / "historical" / "SKILL.md"
        call = {"type": "response_item", "payload": {"id": "call", "type": "function_call",
                "call_id": "read", "name": "read", "arguments": json.dumps({"path": str(skill)})}}
        base = [{"type": "session_meta", "payload": {"id": "history"}},
                {"type": "response_item", "payload": {"id": "user", "type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "load"}]}}, call]
        result = {"type": "response_item", "payload": {"id": "result", "type": "function_call_output",
                  "call_id": "read", "output": skill_text}}
        path = self.logs / "history.jsonl"
        path.write_bytes(jsonl([*base, result]))
        self.service.scan({"codex": path})
        self.assertEqual(self.store.execute("SELECT load_status FROM skill_invocations").fetchone()[0], "loaded")

        path.write_bytes(jsonl(base))
        self.service.scan({"codex": path})
        invocation = self.store.execute("SELECT load_status, skill_sha256 FROM skill_invocations").fetchone()
        self.assertEqual(tuple(invocation), ("result-missing", None))
        self.assertEqual(self.service.metric_snapshot_candidates(), [])

    def test_codex_read_and_pi_invocation_capture_load_sha_and_direct_attribution(self):
        skill = self.root / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# demo\nexact contract\n", encoding="utf-8")
        sha = hashlib.sha256(skill.read_bytes()).hexdigest()
        codex = self.logs / "codex.jsonl"
        codex.write_bytes(jsonl([
            {"type": "session_meta", "payload": {"id": "c1"}},
            {"type": "response_item", "timestamp": "2026-08-24T01:00:00Z", "payload": {
                "id": "u", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "use demo"}]}},
            {"type": "response_item", "timestamp": "2026-08-24T01:00:01Z", "payload": {
                "id": "fc", "type": "function_call", "call_id": "read-demo", "name": "read",
                "arguments": json.dumps({"path": str(skill)})}},
            {"type": "response_item", "timestamp": "2026-08-24T01:00:02Z", "payload": {
                "id": "out", "type": "function_call_output", "call_id": "read-demo", "output": skill.read_text()}},
        ]))
        pi = self.logs / "pi.jsonl"
        pi.write_bytes(jsonl([
            self.pi_header("p2"),
            self.pi_user("pu", f"<skill location='{skill}' name='demo'>{skill.read_text()}</skill>"),
        ]))
        # Historical attribution comes from the logged result/payload, not the
        # mutable file as it exists when the offline scan happens.
        skill.write_text("# changed after invocation\n", encoding="utf-8")
        self.service.scan({"codex": codex, "pi": pi})
        rows = self.store.execute(
            "SELECT skill_sha256, load_status FROM skill_invocations ORDER BY created_at"
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(sha, "loaded"), (sha, "loaded")])
        self.assertEqual(self.store.execute(
            "SELECT COUNT(*) FROM attribution_links WHERE attribution_kind='direct' AND status='active'"
        ).fetchone()[0], 2)
        detail_case = self.store.execute("SELECT id FROM task_cases ORDER BY created_at LIMIT 1").fetchone()[0]
        self.assertNotIn(str(self.root), json.dumps(self.service.get_case_detail(detail_case)))
        self.assertNotIn(str(self.root), json.dumps(self.service.list_events()))
        self.assertEqual(_safe_public("type C:\\Users\\Private Folder\\SKILL.md"), "<redacted-path-text>")
        self.assertEqual(_safe_public(r"read \\server\share\Private Folder\SKILL.md"), "<redacted-path-text>")

    def test_partial_codex_read_loads_skill_but_keeps_version_unknown(self):
        skill = self.root / "skills" / "partial" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# partial\nfull body\n", encoding="utf-8")
        path = self.logs / "partial.jsonl"
        path.write_bytes(jsonl([
            {"type": "session_meta", "payload": {"id": "partial-session"}},
            {"type": "response_item", "payload": {"id": "u", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "use partial"}]}},
            {"type": "response_item", "payload": {"id": "c", "type": "function_call", "call_id": "read-part", "name": "read", "arguments": json.dumps({"path": str(skill), "offset": 2, "limit": 1})}},
            {"type": "response_item", "payload": {"id": "r", "type": "function_call_output", "call_id": "read-part", "output": "full body\n"}},
        ]))
        self.service.scan({"codex": path})
        invocation = self.store.execute(
            "SELECT load_status, skill_sha256, metadata_json FROM skill_invocations"
        ).fetchone()
        self.assertEqual((invocation["load_status"], invocation["skill_sha256"]), ("loaded", None))
        self.assertEqual(json.loads(invocation["metadata_json"])["version_unknown_reason"], "partial-read")

    def test_multi_skill_attribution_is_shared_and_skill_maintenance_is_rejected(self):
        skills = []
        for name in ("first", "second"):
            skill = self.root / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(f"# {name}\n", encoding="utf-8")
            skills.append(skill)
        blocks = "\n".join(
            f'<skill name="{item.parent.name}" location="{item}">{item.read_text()}</skill>'
            for item in skills
        )
        (self.logs / "multi.jsonl").write_bytes(jsonl([
            self.pi_header("multi"), self.pi_user(text=f"完成业务任务\n{blocks}"),
        ]))
        (self.logs / "maintenance.jsonl").write_bytes(jsonl([
            self.pi_header("maintenance"),
            self.pi_user(text=f'请评审这个技能的 SKILL.md\n<skill name="first" location="{skills[0]}">{skills[0].read_text()}</skill>'),
        ]))
        self.service.scan({"pi": self.logs})
        rows = self.store.execute(
            """SELECT s.source_session_id, i.invocation_kind, l.attribution_kind
               FROM attribution_links l JOIN skill_invocations i ON i.id=l.skill_invocation_id
               JOIN task_episodes ep ON ep.id=i.task_episode_id JOIN sessions s ON s.id=ep.session_id
               ORDER BY s.source_session_id, i.created_at, i.skill_id"""
        ).fetchall()
        by_session = {}
        for row in rows:
            by_session.setdefault(row[0], []).append((row[1], row[2]))
        self.assertEqual(by_session["multi"], [("business-use", "shared"), ("business-use", "shared")])
        self.assertEqual(by_session["maintenance"], [("skill-maintenance", "rejected")])
        cleanup = self.service.cleanup_derived_data(skill_id="first")
        self.assertGreaterEqual(cleanup["sharedSkillCasesRetained"], 1)
        self.assertIsNotNone(self.store.execute(
            """SELECT ep.goal_text FROM task_episodes ep JOIN sessions s ON s.id=ep.session_id
               WHERE s.source_session_id='multi' AND ep.goal_text IS NOT NULL LIMIT 1"""
        ).fetchone())

    def test_duplicate_same_skill_invocations_are_metric_ambiguous(self):
        _skill, invocation, case = self._scan_skill_case("duplicate-metric")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO skill_invocations(id, invocation_fingerprint, task_episode_id,
                       event_id, skill_id, skill_sha256, invocation_kind, load_status,
                       validity, created_at, metadata_json)
                   VALUES ('duplicate-invocation', 'duplicate-invocation', ?, ?, ?, ?,
                       'business-use', 'loaded', 'valid', ?, '{}')""",
                (invocation["task_episode_id"], invocation["event_id"], invocation["skill_id"],
                 invocation["skill_sha256"], "2026-08-24T02:00:03Z"),
            )
            self.store.execute(
                "UPDATE attribution_links SET attribution_kind='shared' WHERE task_case_id=?",
                (case,),
            )
            self.store.execute(
                """INSERT INTO attribution_links(id, task_case_id, skill_invocation_id,
                       attribution_kind, confidence, status, created_at)
                   VALUES ('duplicate-link', ?, 'duplicate-invocation', 'shared', 1, 'active', ?)""",
                (case, "2026-08-24T02:00:03Z"),
            )
        candidates = self.service.metric_snapshot_candidates(skill_id="duplicate-metric")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["frozen"]["governanceExclusion"], "duplicate-invocation-ambiguous")
        self.assertEqual(len(candidates[0]["frozen"]["duplicateInvocationIds"]), 2)

    def _scan_skill_case(self, skill_name="reviewed"):
        skill = self.root / "skills" / skill_name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"# {skill_name}\n", encoding="utf-8")
        path = self.logs / f"{skill_name}.jsonl"
        path.write_bytes(jsonl([
            self.pi_header(f"session-{skill_name}"), self.pi_user(text="run tests"),
            {"type": "message", "id": "load", "timestamp": "2026-08-24T02:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text":
                 f'run tests\n<skill name="{skill_name}" location="{skill}">{skill.read_text()}</skill>'}]}},
            {"type": "message", "id": "load-a", "timestamp": "2026-08-24T02:00:01Z",
             "message": {"role": "assistant", "content": "sensitive intermediate response"}},
            {"type": "message", "id": "load-b", "timestamp": "2026-08-24T02:00:02Z",
             "message": {"role": "assistant", "content": "final response"}},
        ]))
        self.service.scan({"pi": path}, scope_kind="configured-catalog")
        invocation = self.store.execute(
            "SELECT * FROM skill_invocations WHERE skill_id=?", (skill_name,)
        ).fetchone()
        case = self.store.execute(
            "SELECT task_case_id FROM attribution_links WHERE skill_invocation_id=?", (invocation["id"],)
        ).fetchone()[0]
        return skill, invocation, case

    def test_exact_contract_environment_error_and_hard_failure(self):
        _skill, invocation, case = self._scan_skill_case()
        contract = self.contracts.create_draft("reviewed", invocation["skill_sha256"], {
            "requirements": [{"id": "tests", "checker": "tests", "checkerVersion": ">=2", "parserVersion": 1, "trustLevel": "trusted", "approvalVersion": "local-admin-v1"}]
        }, "owner", approver="approver")
        contract = self.contracts.publish(contract["id"], "owner", approver="approver")
        now = "2026-08-24T02:01:00Z"
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO check_runs(id, task_case_id, checker_id, checker_version, approval_version, status,
                     assertion_outcome, result_json, started_at, finished_at, freshness)
                   VALUES ('env', ?, 'tests', '1', 'local-admin-v1', 'finished', 'infrastructure-error', ?, ?, ?, 'current')""",
                (case, json.dumps({"validity": "environment-mismatch", "lifecycle": "finished", "parser_version": 1}), now, now),
            )
        environment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((environment["automated_verdict"], environment["hard_failure"]), ("unset", 0))
        self.assertEqual(environment["contract_version_id"], contract["id"])

        with self.store.transaction():
            self.store.execute(
                """INSERT INTO check_runs(id, task_case_id, checker_id, checker_version, approval_version, status,
                     assertion_outcome, result_json, started_at, finished_at, freshness)
                   VALUES ('old-fail', ?, 'tests', '1', 'local-admin-v1', 'finished', 'assertion-fail', ?, ?, ?, 'current')""",
                (case, json.dumps({"validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "parser_version": 1,
                                   "assertions": {"total": 1, "failed": 1}}), now, now),
            )
            self.store.execute(
                """INSERT INTO check_runs(id, task_case_id, checker_id, checker_version, approval_version, status,
                     assertion_outcome, result_json, started_at, finished_at, freshness)
                   VALUES ('fail', ?, 'tests', '2', 'local-admin-v1', 'finished', 'assertion-fail', ?, ?, ?, 'current')""",
                (case, json.dumps({"validity": "valid", "lifecycle": "finished", "trust_level": "untrusted", "parser_version": 1,
                                   "assertions": {"total": 1, "failed": 1}}), now, now),
            )
        untrusted = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((untrusted["automated_verdict"], untrusted["hard_failure"]), ("unset", 0))
        with self.store.transaction():
            self.store.execute(
                "UPDATE check_runs SET approval_version='unapproved', result_json=? WHERE id='fail'",
                (json.dumps({"validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "parser_version": 1,
                             "assertions": {"total": 1, "failed": 1}}),),
            )
        unapproved = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((unapproved["automated_verdict"], unapproved["hard_failure"]), ("unset", 0))
        with self.store.transaction():
            self.store.execute(
                "UPDATE check_runs SET approval_version='local-admin-v1' WHERE id='fail'"
            )
        failed = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((failed["automated_verdict"], failed["hard_failure"]), ("fail", 1))

        self.assertEqual(failed["classification_revision"], 1)
        classification = self.store.execute(
            "SELECT task_type, profile_version FROM task_classifications WHERE task_case_id=?",
            (case,),
        ).fetchone()
        self.assertEqual(tuple(classification), ("test", "deterministic-v1"))

        missing_contract_skill, missing_invocation, missing_case = self._scan_skill_case("without-contract")
        queued = self.service.review_case(missing_case, skill_invocation_id=missing_invocation["id"])
        self.assertEqual(queued["review_task"]["queue_reason"], "contract-missing")
        self.assertTrue(missing_contract_skill.exists())
        with self.assertRaises(KeyError):
            self.service.review_case(missing_case, skill_invocation_id=invocation["id"])

    def test_required_semantic_review_blocks_deterministic_pass_and_payload_is_valid(self):
        _skill, invocation, case = self._scan_skill_case("semantic-required")
        draft = self.contracts.create_draft("semantic-required", invocation["skill_sha256"], {
            "requirements": [{"id": "tests", "checker": "tests", "checkerVersion": ">=1", "parserVersion": 1, "trustLevel": "trusted", "approvalVersion": "local-admin-v1"}],
            "semanticReview": {"required": True, "dimensions": [{"id": "quality", "description": "结果质量满足目标"}]},
        }, "owner", approver="approver")
        contract = self.contracts.publish(draft["id"], "owner", approver="approver")
        now = "2026-08-24T02:01:00Z"
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO check_runs(id, task_case_id, checker_id, checker_version,
                       approval_version, status, assertion_outcome, result_json,
                       started_at, finished_at, freshness)
                   VALUES ('semantic-pass', ?, 'tests', '1', 'local-admin-v1', 'finished',
                       'assertion-pass', ?, ?, ?, 'current')""",
                (case, json.dumps({"validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "parser_version": 1, "assertions": {"total": 1}}), now, now),
            )
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((assessment["automated_verdict"], assessment["assessability"]), ("unset", "assessable"))
        self.assertEqual(assessment["review_task"]["queue_reason"], "semantic-review-required")
        payload, current = self.service.semantic_review_payload(case)
        validate_review_input(payload)
        self.assertEqual(payload["assessment_id"], current["id"])
        self.assertEqual(payload["contract_version_id"], contract["id"])
        actor = self.store.create_actor("Revision reviewer", roles=["reviewer"])
        self.service.correction(
            case, actor_id=actor["id"], expected_revision=0,
            correction_type="task-type", reason_code="new-case-revision",
            payload={"task_type": "test"},
        )
        revised = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((revised["automated_verdict"], revised["review_task"]["queue_reason"]), ("unset", "evidence-inconclusive"))

    def test_semantic_review_cannot_replace_missing_deterministic_evidence(self):
        _skill, invocation, case = self._scan_skill_case("semantic-missing-check")
        draft = self.contracts.create_draft("semantic-missing-check", invocation["skill_sha256"], {
            "requirements": [{"id": "tests", "checker": "tests", "checkerVersion": ">=1", "parserVersion": 1, "trustLevel": "trusted", "approvalVersion": "local-admin-v1"}],
            "semanticReview": {"required": True},
        }, "owner", approver="approver")
        self.contracts.publish(draft["id"], "owner", approver="approver")
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual(assessment["review_task"]["queue_reason"], "evidence-inconclusive")
        with self.assertRaisesRegex(ValueError, "deterministic contract requirements"):
            self.service.semantic_review_payload(case, assessment_id=assessment["id"])

    def test_any_stale_bound_evidence_prevents_current_assessment_pass(self):
        _skill, invocation, case = self._scan_skill_case("stale-evidence")
        draft = self.contracts.create_draft("stale-evidence", invocation["skill_sha256"], {
            "requirements": [{"id": "tests", "checker": "tests", "checkerVersion": ">=1", "parserVersion": 1, "trustLevel": "trusted", "approvalVersion": "local-admin-v1"}],
        }, "owner", approver="approver")
        self.contracts.publish(draft["id"], "owner", approver="approver")
        result_json = json.dumps({"validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "parser_version": 1, "assertions": {"total": 1}})
        with self.store.transaction():
            for check_id, freshness in (("current-check", "current"), ("stale-check", "stale")):
                self.store.execute(
                    """INSERT INTO check_runs(id, task_case_id, case_revision, checker_id,
                           checker_version, approval_version, status, assertion_outcome,
                           result_json, started_at, finished_at, freshness)
                       VALUES (?, ?, 1, 'tests', '1', 'local-admin-v1', 'finished',
                           'assertion-pass', ?, ?, ?, ?)""",
                    (check_id, case, result_json, "2026-08-24T00:00:00Z", "2026-08-24T00:00:01Z", freshness),
                )
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual(
            (assessment["automated_verdict"], assessment["freshness"], assessment["review_task"]["queue_reason"]),
            ("unset", "stale", "stale-evidence"),
        )
        with self.store.transaction():
            self.store.execute("UPDATE check_runs SET checker_id='unrelated' WHERE id='stale-check'")
        current = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual((current["automated_verdict"], current["freshness"]), ("pass", "current"))

    def test_assessment_and_required_queue_are_atomic(self):
        _skill, invocation, case = self._scan_skill_case("atomic")
        with patch.object(self.store, "create_review_task", side_effect=RuntimeError("queue failed")):
            with self.assertRaises(RuntimeError):
                self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual(self.store.execute(
            "SELECT COUNT(*) FROM outcome_assessments WHERE task_case_id=?", (case,)
        ).fetchone()[0], 0)
        self.assertEqual(self.store.execute(
            "SELECT current_assessment_revision FROM task_cases WHERE id=?", (case,)
        ).fetchone()[0], 0)

    def test_decision_disposition_correction_exception_are_revision_safe(self):
        _skill, invocation, case = self._scan_skill_case("manual")
        draft = self.contracts.create_draft("manual", invocation["skill_sha256"], {
            "artifacts": [{"id": "report", "selector": {"kind": "file", "glob": "report.md"}, "minCount": 1}],
            "requirements": [],
        }, "owner", approver="approver")
        self.contracts.publish(draft["id"], "owner", approver="approver")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO artifacts(id, artifact_fingerprint, task_case_id, case_revision,
                       artifact_type, selector, content_hash, freshness, metadata_json, created_at)
                   VALUES ('manual-artifact', 'manual-artifact', ?, 1, 'file', 'report.md',
                       'abc', 'current', '{"kind":"file","path":"report.md"}', ?)""",
                (case, "2026-08-24T02:00:00Z"),
            )
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        self.assertEqual(assessment["assessability"], "assessable")
        review = assessment["review_task"] or self.store.create_review_task(
            case, assessment["id"], "calibration-sample"
        )
        actor = self.store.create_actor("Reviewer", roles=["reviewer", "admin"])
        first = self.service.decision(
            review["id"], actor_id=actor["id"], expected_revision=0,
            verdict="partial", reason_code="checked",
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.service.effective_projection(assessment["id"])["effective_verdict"], "partial")
        with self.assertRaises(RevisionConflict):
            self.service.disposition(
                review["id"], actor_id=actor["id"], expected_revision=0,
                disposition="needs-evidence", reason_code="stale",
            )
        disposition = self.service.disposition(
            review["id"], actor_id=actor["id"], expected_revision=1,
            disposition="needs-evidence", reason_code="more-proof",
        )
        self.assertEqual(disposition["revision"], 2)
        exception = self.service.exception(
            case, assessment_id=assessment["id"], actor_id=actor["id"],
            expected_revision=0, reason_code="approved",
        )
        self.assertEqual(exception["revision"], 1)
        self.assertEqual(self.service.effective_projection(assessment["id"])["effective_verdict"], "exception-accepted")
        correction = self.service.correction(
            case, actor_id=actor["id"], expected_revision=0,
            correction_type="task-tag", reason_code="wrong-tag", payload={"task_type": "build"},
        )
        self.assertEqual(correction["revision"], 1)
        old = self.store.execute(
            "SELECT is_current, conflict_state FROM outcome_assessments WHERE id=?", (assessment["id"],)
        ).fetchone()
        self.assertEqual(tuple(old), (0, "resolved-by-correction"))
        current_case_revision = self.store.execute(
            "SELECT current_revision FROM task_cases WHERE id=?", (case,)
        ).fetchone()[0]
        self.assertGreater(self.store.execute(
            "SELECT COUNT(*) FROM task_facts WHERE task_case_id=? AND case_revision=?",
            (case, current_case_revision),
        ).fetchone()[0], 0)
        self.assertEqual(self.store.execute(
            "SELECT task_type FROM task_cases WHERE id=?", (case,)
        ).fetchone()[0], "build")

    def test_metric_candidates_come_from_current_database_projection(self):
        _skill, invocation, case = self._scan_skill_case("metric")
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        candidates = self.service.metric_snapshot_candidates(skill_id="metric")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["assessment_id"], assessment["id"])
        self.assertEqual(candidates[0]["skill_sha256"], invocation["skill_sha256"])
        snapshot = self.service.create_metric_snapshot(
            skill_id="metric",
        )
        self.assertEqual(snapshot["sealed"], 1)
        self.assertEqual(snapshot["cases"][0]["task_case_id"], case)
        self.assertEqual(snapshot["scan_run_id"], self.store.execute(
            "SELECT id FROM scan_runs ORDER BY finished_at DESC, id DESC LIMIT 1"
        ).fetchone()[0])
        self.assertEqual(snapshot["versions"]["parserVersion"], self.service.parser_version)
        self.assertEqual(snapshot["dimensions"]["skillId"], "metric")
        self.assertIsNotNone(snapshot["dimensions"]["scanScopeFingerprint"])
        self.assertEqual(snapshot["cases"][0]["frozen"]["assessmentParserVersion"], self.service.parser_version)
        self.assertIn("classificationProfiles", snapshot["versions"])
        empty = self.root / "empty-logs"
        empty.mkdir()
        empty_scan = self.service.scan({"pi": empty}, scope_kind="configured-catalog")
        self.assertEqual(empty_scan["coverage_status"], "complete")
        isolated = self.service.create_metric_snapshot(skill_id="metric")
        self.assertEqual(isolated["scan_run_id"], empty_scan["id"])
        self.assertEqual(isolated["cases"], [])

    def test_metric_report_uses_fixed_groups_and_hides_small_sample_rates(self):
        base = {
            "skill_id": "demo", "skill_sha256": "a" * 64,
            "contract_version_id": "contract-v1", "task_type": "test",
            "attribution_kind": "direct", "metric_eligible": 1,
        }
        small = self.service.metric_snapshot_report({
            "id": "small", "coverage_status": "complete",
            "cases": [{**base, "effective_verdict": "pass"}],
        })
        self.assertIsNone(small["groups"][0]["rates"]["pass"]["rate"])
        large = self.service.metric_snapshot_report({
            "id": "large", "coverage_status": "complete",
            "cases": [
                {**base, "effective_verdict": "pass" if index < 18 else "fail"}
                for index in range(20)
            ] + [{**base, "metric_eligible": 0, "exclusion_reason": "disputed", "effective_verdict": "pass"}],
        })
        group = large["groups"][0]
        self.assertEqual(group["denominator"], 20)
        self.assertEqual(group["rates"]["pass"]["rate"], 0.9)
        self.assertEqual(large["exclusions"], {"disputed": 1})

    def test_correction_rejects_non_reviewer(self):
        _skill, _invocation, case = self._scan_skill_case("unauthorized")
        actor = self.store.create_actor("Viewer", roles=[])
        with self.assertRaises(EffectStoreError):
            self.service.correction(
                case, actor_id=actor["id"], expected_revision=0,
                correction_type="task-tag", reason_code="unauthorized",
            )

    def test_fork_creates_candidate_without_merging_cases_and_reviewer_can_share(self):
        skill = self.root / "skills" / "forked" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# forked\n", encoding="utf-8")
        parent = self.logs / "a-parent.jsonl"
        parent.write_bytes(jsonl([
            self.pi_header("parent"), self.pi_user("parent-goal", "prepare"),
            {"type": "message", "id": "parent-load", "timestamp": "2026-08-24T03:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text":
                 f'<skill name="forked" location="{skill}">{skill.read_text()}</skill>'}]}},
        ]))
        child = self.logs / "b-child.jsonl"
        child.write_bytes(jsonl([
            {"type": "session", "id": "child", "parentSession": "parent", "timestamp": "2026-08-24T03:01:00Z"},
            self.pi_user("child-goal", "continue independently"),
        ]))
        self.service.scan({"pi": self.logs})
        child_case = self.store.execute(
            """SELECT c.id FROM task_cases c JOIN task_case_episodes ce ON ce.task_case_id=c.id
               JOIN task_episodes ep ON ep.id=ce.task_episode_id JOIN sessions s ON s.id=ep.session_id
               WHERE s.source_session_id='child'"""
        ).fetchone()[0]
        candidate = self.store.execute(
            "SELECT * FROM attribution_links WHERE task_case_id=? AND attribution_kind='candidate'",
            (child_case,),
        ).fetchone()
        self.assertIsNotNone(candidate)
        parent_case = self.store.execute(
            "SELECT task_case_id FROM attribution_links WHERE skill_invocation_id=? AND attribution_kind='direct'",
            (candidate["skill_invocation_id"],),
        ).fetchone()[0]
        self.assertNotEqual(parent_case, child_case)

        actor = self.store.create_actor("Reviewer", roles=["reviewer"])
        self.service.correction(
            child_case, actor_id=actor["id"], expected_revision=0,
            correction_type="attribution", reason_code="confirmed-handoff",
            payload={"attributionId": candidate["id"], "attributionKind": "shared"},
        )
        updated = self.store.execute("SELECT attribution_kind FROM attribution_links WHERE id=?", (candidate["id"],)).fetchone()
        self.assertEqual(updated[0], "shared")

    def test_structured_subagent_result_creates_child_session_edge(self):
        path = self.logs / "subagent.jsonl"
        path.write_bytes(jsonl([
            self.pi_header("parent-agent"), self.pi_user(text="delegate task"),
            {"type": "message", "id": "call", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "sub-1", "name": "functions.subagent", "arguments": {"agent": "reviewer"}}
            ]}},
            {"type": "message", "id": "result", "message": {"role": "toolResult", "toolCallId": "sub-1", "toolName": "functions.subagent", "content": [
                {"type": "text", "text": json.dumps({"child_session_id": "child-agent"})}
            ]}},
        ]))
        self.service.scan({"pi": path})
        edge = self.store.execute(
            """SELECT parent.source_session_id, child.source_session_id, e.edge_type
               FROM session_edges e JOIN sessions parent ON parent.id=e.parent_session_id
               JOIN sessions child ON child.id=e.child_session_id"""
        ).fetchone()
        self.assertEqual(tuple(edge), ("parent-agent", "child-agent", "subagent-result"))

    def test_cleanup_purges_sensitive_derived_data_but_retains_manual_audit(self):
        _skill, invocation, case = self._scan_skill_case("cleanup")
        assessment = self.service.review_case(case, skill_invocation_id=invocation["id"])
        actor = self.store.create_actor("Reviewer", roles=["reviewer", "admin"])
        with self.store.transaction():
            self.store.execute(
                "UPDATE task_cases SET metadata_json=? WHERE id=?",
                (json.dumps({"projectId": "secret-project"}), case),
            )
            self.store.execute(
                """UPDATE sessions SET title='secret session title', metadata_json='{"private":"value"}'
                   WHERE id IN (SELECT ep.session_id FROM task_case_episodes ce
                     JOIN task_episodes ep ON ep.id=ce.task_episode_id WHERE ce.task_case_id=?)""",
                (case,),
            )
        decision = self.service.disposition(
            assessment["review_task"]["id"], actor_id=actor["id"], expected_revision=0,
            disposition="needs-evidence", reason_code="retained", note="sensitive note",
        )
        result = self.service.cleanup_derived_data(skill_id="cleanup")
        self.assertGreaterEqual(result["cases"], 1)
        persisted = self.store.execute(
            "SELECT reason_code, note FROM manual_decisions WHERE id=?", (decision["id"],)
        ).fetchone()
        self.assertEqual(tuple(persisted), ("retained", None))
        intermediate = self.store.execute(
            "SELECT payload_json FROM canonical_events WHERE source_event_id='load-a'"
        ).fetchone()[0]
        self.assertEqual(json.loads(intermediate), {"purged": True})
        header_payload = self.store.execute(
            """SELECT payload_json FROM canonical_events WHERE event_type='session_meta'
               AND session_family=(SELECT s.session_family FROM task_case_episodes ce
                 JOIN task_episodes ep ON ep.id=ce.task_episode_id JOIN sessions s ON s.id=ep.session_id
                 WHERE ce.task_case_id=? LIMIT 1)""", (case,)
        ).fetchone()[0]
        self.assertNotEqual(json.loads(header_payload), {"purged": True})
        case_metadata = self.store.execute(
            "SELECT metadata_json FROM task_cases WHERE id=?", (case,)
        ).fetchone()[0]
        self.assertEqual(json.loads(case_metadata), {"purged": True})
        session = self.store.execute(
            """SELECT title, metadata_json FROM sessions WHERE id IN (
                 SELECT ep.session_id FROM task_case_episodes ce JOIN task_episodes ep
                   ON ep.id=ce.task_episode_id WHERE ce.task_case_id=?
               ) LIMIT 1""", (case,)
        ).fetchone()
        self.assertEqual(
            (session["title"], json.loads(session["metadata_json"])),
            ("secret session title", {"private": "value"}),
        )
        current = self.store.execute(
            "SELECT invalidated_at FROM task_cases WHERE id=?", (case,)
        ).fetchone()
        self.assertIsNotNone(current[0])
        invocation_row = self.store.execute(
            "SELECT skill_path, metadata_json FROM skill_invocations WHERE id=?", (invocation["id"],)
        ).fetchone()
        self.assertIsNone(invocation_row[0])
        self.assertEqual(json.loads(invocation_row[1]), {"purged": True})
        self.assertEqual(
            self.store.execute("SELECT COUNT(*) FROM data_cleanup_audits").fetchone()[0], 1
        )

    def test_cleanup_retains_raw_episode_evidence_shared_with_unselected_case(self):
        _skill, invocation, case = self._scan_skill_case("shared-cleanup")
        other = self.store.create_task_case("unselected-shared-case")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO task_case_episodes(task_case_id, task_episode_id, relationship)
                   VALUES (?, ?, 'shared')""",
                (other["id"], invocation["task_episode_id"]),
            )
        result = self.service.cleanup_derived_data(skill_id="shared-cleanup")
        self.assertEqual(result["sharedEvidenceRetained"], 1)
        payload = self.store.execute(
            "SELECT payload_json FROM canonical_events WHERE source_event_id='load-a'"
        ).fetchone()[0]
        self.assertNotEqual(json.loads(payload), {"purged": True})
        episode = self.store.execute(
            "SELECT goal_text FROM task_episodes WHERE id=?", (invocation["task_episode_id"],)
        ).fetchone()
        self.assertIsNotNone(episode["goal_text"])


if __name__ == "__main__":
    unittest.main()