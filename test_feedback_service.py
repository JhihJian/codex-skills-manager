import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from effect_store import EffectStore, EffectStoreError, ImmutableSnapshotError, RevisionConflict
from effect_adapters import parse_pi_jsonl_line
from feedback_detector import SPAN_PARSER_VERSION
from feedback_service import FeedbackService, _mentions_identifier


NOW = "2026-08-25T00:00:00Z"


class FeedbackServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EffectStore(Path(self.tmp.name) / "effect.sqlite3")
        self.service = FeedbackService(self.store, formal_scope_fingerprint="test-scope")
        self.target_case = self.store.create_task_case("target-case", task_type="coding")
        self.feedback_case = self.store.create_task_case("feedback-case", task_type="follow-up")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, fingerprint="feedback-event", text="这个结果完全不对", *, event_type="user_message", payload=None):
        body = payload if payload is not None else {"text": text, "metadata": {}}
        return self.store.upsert_event(
            fingerprint, source="pi", session_family="family", event_type=event_type,
            payload_hash=f"hash-{fingerprint}", payload=body, protocol_time=NOW,
        )

    def derive(self, *, text="这个结果完全不对", context=None, own_case=None, fingerprint="feedback-event"):
        event = self.event(fingerprint, text)
        context = context if context is not None else {"previous_task_case_id": self.target_case["id"]}
        return self.service.derive_user_event(
            event, own_case or self.feedback_case["id"], context,
        )[0]

    def actor(self, name="Reviewer", roles=("reviewer",)):
        return self.store.create_actor(name, roles=roles)

    def candidate(self, *, confidence=0.96, category="result-rejection"):
        return {
            "category": category, "severity": "high", "confidence": confidence,
            "authority": "user", "channel": "user-feedback", "adjustments": [],
            "span": {
                "event_id": "source", "block_index": 0, "start": 0, "end": 4,
                "origin": "user-authored", "excerpt_hash": "a" * 64,
                "redacted_excerpt": "完全不对", "protocol_locator": "content[0].text:0-4",
                "redaction_status": "clean", "truncated": False,
            },
        }

    def episode(self, case_id=None, event_id=None, episode_id="episode"):
        case_id = case_id or self.target_case["id"]
        event_id = event_id or self.event("episode-event", "work")["id"]
        session = self.store.create_session("pi", f"session-{episode_id}", session_family="family")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO task_episodes(
                       id, episode_fingerprint, session_id, start_event_id, end_event_id,
                       process_state, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'indexed', '{}', ?, ?)""",
                (episode_id, episode_id, session["id"], event_id, event_id, NOW, NOW),
            )
            self.store.execute(
                "INSERT INTO task_case_episodes(task_case_id, task_episode_id, relationship) VALUES (?, ?, 'primary')",
                (case_id, episode_id),
            )
        return episode_id

    def skill(self, skill_id="demo", *, case_id=None, attribution="direct", invocation_id="skill-invocation"):
        event = self.event(f"event-{invocation_id}", "use skill")
        episode = self.episode(case_id=case_id, event_id=event["id"], episode_id=f"episode-{invocation_id}")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO skill_invocations(
                       id, invocation_fingerprint, task_episode_id, event_id, skill_id,
                       skill_sha256, invocation_kind, load_status, validity, created_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, 'business-use', 'loaded', 'valid', ?, '{}')""",
                (invocation_id, invocation_id, episode, event["id"], skill_id, "b" * 64, NOW),
            )
            self.store.execute(
                """INSERT INTO attribution_links(
                       id, task_case_id, skill_invocation_id, attribution_kind,
                       confidence, status, created_at)
                   VALUES (?, ?, ?, ?, 1, 'active', ?)""",
                (f"link-{invocation_id}", case_id or self.target_case["id"], invocation_id, attribution, NOW),
            )
        return invocation_id

    def tool(self, *, result_payload=None):
        call_event = self.event("tool-call-event", "call", event_type="tool_call")
        result_event = self.event(
            "tool-result-event", "result", event_type="tool_result",
            payload=result_payload or {"result": {"exitCode": 1}, "outcome": {"isError": False}},
        )
        episode = self.episode(event_id=call_event["id"], episode_id="tool-episode")
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO tool_calls(
                       id, call_fingerprint, task_episode_id, event_id, call_id,
                       tool_name, arguments_json, called_at)
                   VALUES ('tool-call', 'tool-call', ?, ?, 'call-1', 'bash', '{}', ?)""",
                (episode, call_event["id"], NOW),
            )
            self.store.execute(
                """INSERT INTO tool_results(
                       id, result_fingerprint, tool_call_id, event_id, status,
                       exit_code, output_hash, completed_at, metadata_json)
                   VALUES ('tool-result', 'tool-result', 'tool-call', ?, 'error', 1, 'hash', ?, '{}')""",
                (result_event["id"], NOW),
            )
        return call_event, result_event

    def confirm(self, signal, actor=None):
        actor = actor or self.actor()
        target = next(item for item in signal["targets"] if item["machine_status"] == "candidate")
        action = self.service.append_action(
            signal["id"], "confirm", actor_id=actor["id"],
            expected_revision=signal["current_action_revision"], reason_code="confirmed",
            target_id=target["id"],
        )
        return actor, action, self.service.get_signal(signal["id"])

    def test_reviewer_action_note_is_redacted_and_cleanup_purges_body(self):
        signal = self.derive()
        actor = self.actor()
        target = next(item for item in signal["targets"] if item["machine_status"] == "candidate")
        action = self.service.append_action(
            signal["id"], "confirm", actor_id=actor["id"],
            expected_revision=signal["current_action_revision"], reason_code="confirmed",
            target_id=target["id"], note="token=secret 请复查",
        )
        self.assertNotIn("secret", action["note"])
        self.assertTrue(action["note"])
        self.service.cleanup(older_than="2027-01-01T00:00:00Z")
        stored = self.store.execute(
            "SELECT note, binding_json FROM feedback_actions WHERE id=?", (action["id"],),
        ).fetchone()
        self.assertIsNone(stored["note"])
        self.assertTrue(json.loads(stored["binding_json"])["noteHash"])

    def test_reviewer_reason_code_rejects_sensitive_or_free_text(self):
        signal = self.derive()
        actor = self.actor()
        for reason in ("token=secret", "/home/private/key", "需要人工复查", "UPPER_CASE"):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "machine-readable"):
                self.service.append_action(
                    signal["id"], "exclude", actor_id=actor["id"],
                    expected_revision=signal["current_action_revision"], reason_code=reason,
                )

    def test_manual_problem_discovery_requires_explicit_direct_association_and_stays_separate(self):
        actor = self.actor("Reporter")
        invocation = self.skill("discovery-skill", invocation_id="manual-discovery-invocation")
        with self.assertRaisesRegex(ValueError, "直接有关"):
            self.service.submit_problem_discovery(
                actor_id=actor["id"], skill_invocation_id=invocation,
                category="observed-defect", description="技能示例无法执行，任务无法继续完成。",
                direct_association=True, association_reason="太短",
            )
        submitted = self.service.submit_problem_discovery(
            actor_id=actor["id"], skill_invocation_id=invocation,
            category="observed-defect", description="技能示例无法执行，任务无法继续完成。",
            direct_association=True, association_reason="示例命令直接输出错误，无法继续执行。",
        )
        signal = submitted["signal"]
        revision = next(item for item in signal["machine_revisions"] if item["is_current"])
        self.assertEqual((submitted["association"], revision["channel"]), ("direct-association-candidate", "skill-problem-discovery"))
        self.assertEqual(signal["targets"][0]["target_kind"], "skill-invocation")
        self.assertEqual(signal["current_process_state"], "queued")
        self.assertEqual(self.service.list_signals(channel="skill-problem-discovery")["items"][0]["id"], signal["id"])

    def test_manual_problem_discovery_without_a_usage_record_stays_pending_association(self):
        actor = self.actor("Reporter")
        submitted = self.service.submit_problem_discovery(
            actor_id=actor["id"], category="requirement-gap",
            description="输出遗漏了关键步骤，无法确认后续操作是否安全。",
        )
        signal = submitted["signal"]
        self.assertEqual((submitted["association"], signal["current_process_state"], signal["targets"]), ("pending-association", "candidate", []))
        event = self.store.execute("SELECT orphaned FROM canonical_events WHERE id=?", (signal["feedback_event_id"],)).fetchone()
        self.assertEqual(event["orphaned"], 0)

    def test_manual_problem_discovery_redacts_sensitive_text_and_is_excluded_from_formal_candidates(self):
        actor = self.actor("Reporter")
        submitted = self.service.submit_problem_discovery(
            actor_id=actor["id"], category="observed-defect",
            description="token=super-secret-value 导致技能命令无法执行，请核实。",
        )
        serialized = json.dumps(submitted, ensure_ascii=False)
        stored = self.store.execute(
            """SELECT event.payload_json, revision.redacted_excerpt, revision.metadata_json
               FROM feedback_signals signal
               JOIN canonical_events event ON event.id=signal.feedback_event_id
               JOIN feedback_signal_revisions revision ON revision.id=signal.current_machine_revision_id
               WHERE signal.id=?""", (submitted["signal"]["id"],),
        ).fetchone()
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("super-secret-value", "".join(str(value) for value in stored))
        candidates = self.service.feedback_snapshot_candidates(coverage_status="complete")
        self.assertNotIn(submitted["signal"]["id"], {item["feedback_signal_id"] for item in candidates})

    def test_explicit_user_acceptance_resolves_confirmed_feedback(self):
        signal = self.derive()
        actor, _action, confirmed = self.confirm(signal)
        self.assertEqual(confirmed["current_resolution_state"], "action-required")
        fixing = self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"],
            expected_revision=confirmed["current_action_revision"], reason_code="fixing",
        )
        self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"],
            expected_revision=fixing["revision"], reason_code="verify",
        )
        acceptance = self.store.upsert_event(
            "acceptance-event", source="pi", session_family="family",
            event_type="user_message", payload_hash="acceptance-hash",
            payload={"text": "没问题，现在可以了", "metadata": {}},
            protocol_time="2026-08-25T00:01:00Z",
        )
        result = self.service.derive_user_event(
            acceptance, self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        self.assertEqual(result, [])
        resolved = self.service.get_signal(signal["id"])
        self.assertEqual(resolved["current_resolution_state"], "resolved-verified")
        self.assertEqual(resolved["actions"][-1]["action"], "resolve-verified")
        evidence = self.store.execute(
            "SELECT * FROM evidence_items WHERE event_id=? AND evidence_type='user-acceptance'",
            (acceptance["id"],),
        ).fetchone()
        self.assertEqual((evidence["polarity"], evidence["validity"]), ("positive", "valid"))

    def test_negative_or_mixed_user_message_cannot_verify_resolution(self):
        signal = self.derive()
        actor, _action, confirmed = self.confirm(signal)
        fixing = self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"],
            expected_revision=confirmed["current_action_revision"], reason_code="fixing",
        )
        waiting = self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"],
            expected_revision=fixing["revision"], reason_code="verify",
        )
        with self.assertRaisesRegex(EffectStoreError, "locatable verification evidence"):
            self.service.append_action(
                signal["id"], "resolve", actor_id=actor["id"],
                expected_revision=waiting["revision"], reason_code="forged",
                binding={"userAcceptanceEventId": signal["feedback_event_id"]},
            )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO evidence_items(
                       id, evidence_fingerprint, task_case_id, evidence_type, content_hash,
                       locator_json, validity, polarity, category, created_at)
                   VALUES ('negative-verification', 'negative-verification', ?, 'verification',
                       'negative-hash', '{}', 'valid', 'negative', 'verification', ?)""",
                (self.target_case["id"], NOW),
            )
        with self.assertRaisesRegex(EffectStoreError, "locatable verification evidence"):
            self.service.append_action(
                signal["id"], "resolve-verified", actor_id=actor["id"],
                expected_revision=waiting["revision"], reason_code="negative-evidence",
                binding={"evidenceId": "negative-verification"},
            )
        mixed = self.store.upsert_event(
            "mixed-acceptance", source="pi", session_family="family",
            event_type="user_message", payload_hash="mixed-hash",
            payload={"text": "没问题，但页面还是报错", "metadata": {}},
            protocol_time="2026-08-25T00:02:00Z",
        )
        derived = self.service.derive_user_event(
            mixed, self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        self.assertEqual(derived[0]["machine_revisions"][0]["category"], "mixed-or-unclear")
        self.assertEqual(
            self.service.get_signal(signal["id"])["current_resolution_state"],
            "awaiting-verification",
        )

    def test_protocol_aliases_with_same_time_and_span_deduplicate(self):
        first = dict(self.event(
            "response-item-copy", payload={
                "source": "codex", "text": "这个结果完全不对",
                "metadata": {"protocol_type": "message"},
            },
        ))
        second = dict(self.event(
            "event-msg-copy", payload={
                "source": "codex", "text": "这个结果完全不对",
                "metadata": {"protocol_type": "user_message"},
            },
        ))
        first["source"] = second["source"] = "codex"
        derived_first = self.service.derive_user_event(
            first, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )
        derived_second = self.service.derive_user_event(
            second, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )
        self.assertEqual(derived_first[0]["id"], derived_second[0]["id"])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0], 1)

    def test_legacy_codex_aliases_with_one_millisecond_skew_deduplicate(self):
        first = self.store.upsert_event(
            "legacy-response", source="codex", session_family="codex-family",
            event_type="user_message", payload_hash="legacy-one",
            payload={"text": "这个结果完全不对", "metadata": {}},
            protocol_time="2026-08-25T00:00:00.000Z",
        )
        second = self.store.upsert_event(
            "legacy-event-msg", source="codex", session_family="codex-family",
            event_type="user_message", payload_hash="legacy-two",
            payload={"text": "这个结果完全不对", "metadata": {}},
            protocol_time="2026-08-25T00:00:00.001Z",
        )
        one = self.service.derive_user_event(first, self.feedback_case["id"], {
            "previous_task_case_id": self.target_case["id"],
        })[0]
        two = self.service.derive_user_event(second, self.feedback_case["id"], {
            "previous_task_case_id": self.target_case["id"],
        })[0]
        self.assertEqual(one["id"], two["id"])

    def test_rebuild_supersedes_signal_that_no_longer_matches_detector(self):
        event = self.event("old-machine-match", "这个结果完全不对")
        signal = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        with patch("feedback_service.detect_user_feedback", return_value=()):
            self.service._derive_stored_event(event)
        current = self.store.execute(
            "SELECT current_machine_revision_id, current_process_state FROM feedback_signals WHERE id=?",
            (signal["id"],),
        ).fetchone()
        self.assertEqual(tuple(current), (None, "superseded"))
        self.assertEqual(self.store.execute(
            "SELECT status FROM review_tasks WHERE feedback_signal_id=?", (signal["id"],)
        ).fetchone()[0], "superseded")
        self.assertEqual(self.store.execute(
            "SELECT action FROM feedback_actions WHERE feedback_signal_id=? ORDER BY revision DESC LIMIT 1",
            (signal["id"],),
        ).fetchone()[0], "superseded")

    def test_truncated_old_source_requires_reparse_and_preserves_old_revision(self):
        old_service = FeedbackService(self.store, detector_version="feedback-v2")
        event = self.event(
            "truncated-old-event", payload={
                "text": "这个结果完全不对...[truncated]",
                "metadata": {"feedback_detector_version": "feedback-v2"},
            },
        )
        signal = old_service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.service._derive_stored_event(event)
        current = self.service.get_signal(signal["id"])
        self.assertEqual(len(current["machine_revisions"]), 1)
        self.assertEqual(current["machine_revisions"][0]["detector_version"], "feedback-v2")
        self.assertEqual(current["current_process_state"], "needs-evidence")
        self.assertEqual(current["actions"][-1]["reason_code"], "source-reparse-required")

    def test_targeted_source_reparse_recovers_feedback_beyond_display_truncation(self):
        sessions = Path(self.tmp.name) / "sessions"
        sessions.mkdir()
        source = sessions / "late-feedback.jsonl"
        raw_item = {
            "type": "message", "id": "late-feedback", "message": {
                "role": "user", "content": "普通说明" * 1200 + "。按钮还是没反应",
            },
        }
        raw = (json.dumps(raw_item, ensure_ascii=False) + "\n").encode("utf-8")
        source.write_bytes(raw)
        projected = parse_pi_jsonl_line(raw_item, session_id="", session_family="family")[0]
        event = self.store.upsert_event(
            projected.fingerprint, source="pi", session_family="family",
            source_event_id="late-feedback", event_type="user_message",
            payload_hash=projected.payload_hash, payload={
                "text": ("普通说明" * 800)[:4080] + "...[truncated]",
                "metadata": {"feedback_detector_version": "feedback-v2"},
            }, protocol_time=NOW,
        )
        self.episode(case_id=self.feedback_case["id"], event_id=event["id"], episode_id="late-feedback-episode")
        log = self.store.upsert_log_file("pi", "late-feedback-log")
        source_stat = source.stat()
        generation = self.store.upsert_generation(
            log["id"], "late-feedback-generation", "test-v1",
            device=str(source_stat.st_dev), inode=str(source_stat.st_ino),
            observed_size=source_stat.st_size, observed_mtime_ns=source_stat.st_mtime_ns,
        )
        self.store.upsert_location(generation["id"], source)
        self.store.upsert_provenance(
            event["id"], generation["id"], 0, byte_end=len(raw), line_number=1,
            locator={"rawLineSha256": __import__("hashlib").sha256(raw).hexdigest()},
        )
        result = self.service.reparse_truncated_sources([sessions])
        self.assertEqual((result["updated"], result["remaining"]), (1, 0))
        refreshed = self.store.get_event(event["id"])
        self.assertEqual(refreshed["payload"]["metadata"]["feedback_detector_version"], self.service.detector_version)
        signal = self.store.execute(
            """SELECT r.category, r.redacted_excerpt FROM feedback_signal_revisions r
               JOIN feedback_signals s ON s.id=r.feedback_signal_id
               WHERE s.feedback_event_id=? AND r.is_current=1""", (event["id"],),
        ).fetchone()
        self.assertEqual(signal["category"], "observed-defect")
        self.assertIn("按钮还是没反应", signal["redacted_excerpt"])

    def test_source_reparse_fails_closed_when_source_event_id_changed(self):
        sessions = Path(self.tmp.name) / "mismatched-sessions"
        sessions.mkdir()
        source = sessions / "mismatch.jsonl"
        raw_item = {"type": "message", "id": "different-id", "message": {
            "role": "user", "content": "说明" * 2100 + "按钮还是没反应",
        }}
        raw = (json.dumps(raw_item, ensure_ascii=False) + "\n").encode("utf-8")
        source.write_bytes(raw)
        projected = parse_pi_jsonl_line(raw_item, session_id="", session_family="family")[0]
        event = self.store.upsert_event(
            projected.fingerprint, source="pi", session_family="family",
            source_event_id="expected-id", event_type="user_message",
            payload_hash=projected.payload_hash,
            payload={"text": "说明" * 800 + "...[truncated]", "metadata": {}},
            protocol_time=NOW,
        )
        log = self.store.upsert_log_file("pi", "mismatch-log")
        source_stat = source.stat()
        generation = self.store.upsert_generation(
            log["id"], "mismatch-generation", "test-v1",
            device=str(source_stat.st_dev), inode=str(source_stat.st_ino),
            observed_size=source_stat.st_size, observed_mtime_ns=source_stat.st_mtime_ns,
        )
        self.store.upsert_location(generation["id"], source)
        self.store.upsert_provenance(
            event["id"], generation["id"], 0, byte_end=len(raw), line_number=1,
            locator={"rawLineSha256": __import__("hashlib").sha256(raw).hexdigest()},
        )
        result = self.service.reparse_truncated_sources([sessions])
        self.assertEqual((result["updated"], result["failed"], result["remaining"]), (0, 1, 1))
        state = self.store.execute(
            "SELECT status, stats_json FROM feedback_derivation_state WHERE detector_id=?",
            (self.service.detector_id,),
        ).fetchone()
        self.assertEqual(state["status"], "needs-source-reparse")
        self.assertEqual(json.loads(state["stats_json"])["sourceReparseRequired"], 1)
        self.assertEqual(json.loads(state["stats_json"])["sourceReparseFailures"][0]["eventId"], event["id"])
        self.assertNotEqual(
            self.store.get_event(event["id"])["payload"]["metadata"].get("feedback_detector_version"),
            self.service.detector_version,
        )

    def test_source_reparse_requires_raw_line_hash_and_rejects_changed_bytes(self):
        for mode in ("missing-hash", "changed-bytes"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.tmp.name) as directory:
                sessions = Path(directory)
                source = sessions / "source.jsonl"
                raw_item = {"type": "message", "id": f"source-{mode}", "message": {
                    "role": "user", "content": "说明" * 2100 + "按钮还是没反应",
                }}
                raw = (json.dumps(raw_item, ensure_ascii=False) + "\n").encode("utf-8")
                source.write_bytes(raw)
                projected = parse_pi_jsonl_line(raw_item, session_id="", session_family=mode)[0]
                event = self.store.upsert_event(
                    projected.fingerprint, source="pi", session_family=mode,
                    source_event_id=raw_item["id"], event_type="user_message",
                    payload_hash=projected.payload_hash,
                    payload={"text": "说明" * 800 + "...[truncated]", "metadata": {}},
                    protocol_time=NOW,
                )
                log = self.store.upsert_log_file("pi", f"source-log-{mode}")
                source_stat = source.stat()
                generation = self.store.upsert_generation(
                    log["id"], f"source-generation-{mode}", "test-v1",
                    device=str(source_stat.st_dev), inode=str(source_stat.st_ino),
                    observed_size=source_stat.st_size, observed_mtime_ns=source_stat.st_mtime_ns,
                )
                self.store.upsert_location(generation["id"], source)
                locator = {} if mode == "missing-hash" else {
                    "rawLineSha256": __import__("hashlib").sha256(raw).hexdigest(),
                }
                self.store.upsert_provenance(
                    event["id"], generation["id"], 0, byte_end=len(raw),
                    line_number=1, locator=locator,
                )
                if mode == "changed-bytes":
                    source.write_bytes(raw.replace("按钮还是没反应".encode(), "按钮依然没反应".encode()))
                result = self.service.reparse_truncated_sources([sessions])
                self.assertEqual(result["updated"], 0)
                self.assertIn(event["id"], [failure["eventId"] for failure in result["failures"]])
                self.assertEqual(self.service._derivation_state()["status"], "needs-source-reparse")

    def test_source_root_failure_is_persisted_after_rebuild_reset(self):
        self.service.rebuild(max_events=0, max_seconds=0)
        result = self.service.reparse_truncated_sources([Path(self.tmp.name) / "missing-root"])
        self.assertTrue(result["pending"])
        state = self.service._derivation_state()
        stats = json.loads(state["stats_json"])
        self.assertEqual(state["status"], "needs-source-reparse")
        self.assertEqual(stats["sourceReparseRequired"], 1)
        self.assertEqual(stats["sourceReparseFailures"][0]["eventId"], "<source-root>")

    def test_machine_revision_and_snapshot_bind_span_parser_version(self):
        signal = self.derive()
        self.confirm(signal)
        revision = self.store.execute(
            "SELECT span_parser_version FROM feedback_signal_revisions WHERE id=?",
            (signal["current_machine_revision_id"],),
        ).fetchone()
        self.assertEqual(revision["span_parser_version"], SPAN_PARSER_VERSION)
        candidate = self.service.feedback_snapshot_candidates()[0]
        self.assertEqual(candidate["frozen"]["spanParserVersion"], SPAN_PARSER_VERSION)
        self.store.execute(
            "UPDATE feedback_signal_revisions SET span_parser_version='span-parser-old' WHERE id=?",
            (signal["current_machine_revision_id"],),
        )
        stale = self.service.feedback_snapshot_candidates()[0]
        self.assertEqual(stale["exclusion_reason"], "span-parser-stale")

    def test_resolver_upgrade_rederives_machine_revision_and_snapshot_rejects_stale(self):
        signal = self.derive()
        old_revision_id = signal["current_machine_revision_id"]
        self.make_live(self.store.get_event(signal["feedback_event_id"]))
        upgraded = FeedbackService(
            self.store, resolver_version="feedback-target-v-next",
            formal_scope_fingerprint="test-scope",
        )
        rebuilt = upgraded.bootstrap(max_events=100, max_seconds=2)
        self.assertFalse(rebuilt["pending"])
        current = upgraded.get_signal(signal["id"])
        self.assertNotEqual(current["current_machine_revision_id"], old_revision_id)
        revision = self.store.execute(
            "SELECT resolver_version FROM feedback_signal_revisions WHERE id=?",
            (current["current_machine_revision_id"],),
        ).fetchone()
        self.assertEqual(revision["resolver_version"], "feedback-target-v-next")
        self.assertEqual(
            json.loads(upgraded._derivation_state()["stats_json"])["resolverVersion"],
            "feedback-target-v-next",
        )
        self.store.execute(
            "UPDATE feedback_signal_revisions SET resolver_version='feedback-target-old' WHERE id=?",
            (current["current_machine_revision_id"],),
        )
        stale = upgraded.feedback_snapshot_candidates()[0]
        self.assertEqual(stale["exclusion_reason"], "target-resolver-stale")

    def test_semantic_review_is_persisted_and_classified_output_creates_revision(self):
        signal = self.derive()
        self.confirm(signal)
        payload, profile = self.service.semantic_payload(
            signal["id"], model_version="model-v1",
            prompt_version="prompt-v1", rubric_version="rubric-v1",
        )
        self.assertIsNone(profile)
        target_id = payload["target_ids"][0]
        review = {
            "schema_version": "1.0", "verdict": "classified", "reason": "calibrated",
            "feedback_signal_id": signal["id"],
            "current_machine_revision_id": payload["current_machine_revision_id"],
            "current_machine_revision": payload["current_machine_revision"],
            "current_action_revision": payload["current_action_revision"],
            "category": "requirement-gap", "severity": "high", "confidence": 0.91,
            "span_ids": [payload["evidence_spans"][0]["id"]], "target_id": target_id,
            "rationale": "The result omitted a requirement.",
            "prompt_injection_detected": False, "model_id": "model",
            "model_version": "model-v1", "prompt_version": "prompt-v1",
            "rubric_version": "rubric-v1", "calibration_profile_id": None,
            "version_tuple": payload["version_tuple"],
        }
        with patch.object(self.service, "_persist_candidate", side_effect=RuntimeError("apply failed")):
            with self.assertRaisesRegex(RuntimeError, "apply failed"):
                self.service.apply_semantic_review(review)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM feedback_semantic_reviews").fetchone()[0], 0)
        applied = self.service.apply_semantic_review(review)
        current = self.service.get_signal(signal["id"])
        self.assertEqual(applied["appliedSignal"]["id"], signal["id"])
        self.assertEqual(current["machine_revisions"][-1]["category"], "requirement-gap")
        self.assertEqual(current["semantic_reviews"][0]["verdict"], "classified")
        self.assertEqual(current["current_resolution_state"], "unreviewed")
        self.assertEqual(current["review_task"]["status"], "open")
        self.assertIn("machine-classification-changed", [item["reason_code"] for item in current["actions"]])

    def verification_evidence(self, evidence_id="verification-evidence"):
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO evidence_items(
                       id, evidence_fingerprint, task_case_id, evidence_type, content_hash,
                       locator_json, validity, polarity, category, rule_id,
                       producer_version, created_at)
                   VALUES (?, ?, ?, 'verification', 'verified-hash', '{"review":"manual"}', 'valid',
                           'positive', 'verification', 'trusted-reviewer-verification',
                           'trusted-reviewer-v1', ?)""",
                (evidence_id, evidence_id, self.target_case["id"], NOW),
            )
        return evidence_id

    def complete_scan(self, coverage="complete"):
        scan = self.store.create_scan_run(
            "pi", metadata={"scopeKind": "configured-catalog", "scopeFingerprint": "test-scope"}
        )
        log = self.store.upsert_log_file("pi", f"formal-scan-{scan['id']}")
        generation = self.store.upsert_generation(
            log["id"], f"formal-generation-{scan['id']}", "test-v1",
            scan_run_id=scan["id"],
        )
        for index, row in enumerate(self.store.execute(
            "SELECT feedback_event_id FROM feedback_signals ORDER BY id"
        ).fetchall()):
            self.store.upsert_provenance(row["feedback_event_id"], generation["id"], index)
        finished = self.store.finish_scan_run(scan["id"], coverage_status=coverage)
        cursor = self.store.execute(
            "SELECT COALESCE(MAX(id),0) FROM effect_derivation_changes"
        ).fetchone()[0]
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO feedback_derivation_state(
                       detector_id, detector_version, change_cursor, bootstrap_complete,
                       last_scan_run_id, status, stats_json, updated_at)
                   VALUES (?, ?, ?, 1, ?, 'ready', json_object('resolverVersion', ?), ?)""",
                (self.service.detector_id, self.service.detector_version, cursor, scan["id"],
                 self.service.resolver_version, NOW),
            )
        return finished

    def make_live(self, *events):
        log_file = self.store.upsert_log_file("pi", "feedback-test-log")
        generation = self.store.upsert_generation(log_file["id"], "feedback-generation", "test-v1")
        for index, event in enumerate(events):
            self.store.upsert_provenance(event["id"], generation["id"], index * 10)

    # Derivation and target resolution
    def test_requires_effect_store(self):
        with self.assertRaises(TypeError):
            FeedbackService("db")

    def test_explicit_target_name_requires_identifier_boundaries(self):
        self.assertTrue(_mentions_identifier("read 工具还是失败", "read"))
        self.assertFalse(_mentions_identifier("tool already failed", "read"))
        self.assertFalse(_mentions_identifier("bread tool failed", "read"))
        self.assertFalse(_mentions_identifier("functions.read failed", "read"))
        self.assertFalse(_mentions_identifier("read-file failed", "read"))
        self.assertFalse(_mentions_identifier("demo-extra 技能失败", "demo"))

    def test_text_fallback_persists_signal(self):
        signal = self.derive()
        self.assertEqual(signal["machine_revisions"][0]["category"], "result-rejection")

    def test_adapter_candidate_is_preferred(self):
        payload = {"text": "neutral", "metadata": {
            "feedback_detector_version": self.service.detector_version,
            "feedback_candidates": [self.candidate(category="requirement-gap")],
        }}
        event = self.event(payload=payload)
        signal = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertEqual(signal["machine_revisions"][0]["category"], "requirement-gap")

    def test_no_target_caps_confidence_and_does_not_queue(self):
        signal = self.derive(context={})
        self.assertLess(signal["machine_revisions"][0]["confidence"], 0.85)
        self.assertIsNone(signal["review_task"])

    def test_feedback_case_and_target_case_are_separate(self):
        signal = self.derive()
        self.assertEqual(signal["feedback_case_id"], self.feedback_case["id"])
        self.assertEqual(signal["targets"][0]["target_task_case_id"], self.target_case["id"])

    def test_explicit_task_target_wins_over_previous(self):
        other = self.store.create_task_case("other")
        signal = self.derive(context={
            "target_task_case_id": other["id"],
            "previous_task_case_id": self.target_case["id"],
        })
        self.assertEqual(signal["targets"][0]["target_task_case_id"], other["id"])

    def test_explicit_assistant_event_target(self):
        assistant = self.event("assistant", "answer", event_type="assistant_message")
        self.make_live(assistant)
        signal = self.derive(context={"target_event_id": assistant["id"]})
        self.assertEqual(signal["targets"][0]["target_kind"], "assistant-result")

    def test_explicit_skill_name_resolves_invocation(self):
        invocation = self.skill("demo")
        signal = self.derive(text="demo 技能的结果完全不对")
        self.assertEqual((signal["targets"][0]["target_kind"], signal["targets"][0]["skill_invocation_id"]),
                         ("skill-invocation", invocation))

    def test_skill_name_does_not_resolve_across_session_family(self):
        invocation = self.skill("remote-skill")
        with self.store.transaction():
            self.store.execute(
                """UPDATE sessions SET session_family='other-family' WHERE id=(
                     SELECT ep.session_id FROM skill_invocations i JOIN task_episodes ep
                       ON ep.id=i.task_episode_id WHERE i.id=?)""", (invocation,),
            )
        signal = self.derive(text="remote-skill 技能完全不对")
        self.assertNotIn("skill-invocation", {item["target_kind"] for item in signal["targets"]})

    def test_skill_name_does_not_cross_sibling_sessions_in_same_family(self):
        self.skill("sibling-skill")
        feedback_event = self.event("same-family-feedback", "sibling-skill 技能完全不对")
        self.episode(
            case_id=self.feedback_case["id"], event_id=feedback_event["id"],
            episode_id="feedback-own-episode",
        )
        signal = self.service.derive_user_event(
            feedback_event, self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertNotIn("skill-invocation", {item["target_kind"] for item in signal["targets"]})

    def test_explicit_tool_name_resolves_call(self):
        self.tool()
        signal = self.derive(text="bash 工具的结果完全不对")
        self.assertEqual(signal["targets"][0]["target_kind"], "tool-call")

    def test_invalid_explicit_target_fails_closed(self):
        signal = self.derive(context={"tool_call_id": "missing"})
        self.assertEqual(signal["targets"], [])
        self.assertIsNone(signal["review_task"])

    def test_repeated_derivation_is_idempotent(self):
        first = self.derive()
        second = self.derive()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(second["machine_revisions"]), 1)
        self.assertEqual(len(second["actions"]), 2)

    def test_detector_upgrade_adds_machine_revision(self):
        first = self.derive()
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        event = self.store.get_event(first["feedback_event_id"])
        second = upgraded.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertEqual(len(second["machine_revisions"]), 2)
        self.assertEqual(sum(item["is_current"] for item in second["machine_revisions"]), 1)

    def test_upgrade_supersedes_old_targets(self):
        first = self.derive()
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        upgraded.derive_user_event(
            self.store.get_event(first["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        statuses = [row[0] for row in self.store.execute(
            "SELECT machine_status FROM feedback_targets WHERE feedback_signal_id=? ORDER BY created_at",
            (first["id"],),
        ).fetchall()]
        self.assertEqual(statuses, ["superseded", "candidate"])

    def test_old_machine_target_cannot_be_reconfirmed(self):
        signal = self.derive()
        actor, _action, _current = self.confirm(signal)
        old_target = signal["targets"][0]
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        current = upgraded.derive_user_event(
            self.store.get_event(signal["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )[0]
        with self.assertRaises(RevisionConflict):
            upgraded.append_action(
                signal["id"], "confirm", actor_id=actor["id"],
                expected_revision=current["current_action_revision"], reason_code="stale-target",
                target_id=old_target["id"],
            )

    def test_detector_rollback_reuses_existing_revision(self):
        first = self.derive()
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        event = self.store.get_event(first["feedback_event_id"])
        upgraded.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )
        rolled_back = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertEqual(len(rolled_back["machine_revisions"]), 2)
        current = next(item for item in rolled_back["machine_revisions"] if item["is_current"])
        self.assertEqual(current["detector_version"], "feedback-v5")

    def test_current_revision_pointer_is_consistent(self):
        signal = self.derive()
        row = self.store.execute(
            """SELECT r.is_current FROM feedback_signals s JOIN feedback_signal_revisions r
               ON r.id=s.current_machine_revision_id WHERE s.id=?""", (signal["id"],),
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_high_confidence_target_creates_evidence_assessment_review(self):
        signal = self.derive()
        self.assertIsNotNone(signal["evidence"])
        self.assertEqual(signal["review_task"]["feedback_signal_id"], signal["id"])
        assessment = self.store.execute(
            "SELECT subject_key, skill_id FROM outcome_assessments WHERE id=?",
            (signal["review_task"]["assessment_id"],),
        ).fetchone()
        self.assertEqual(tuple(assessment), (f"feedback:{signal['id']}", None))
        self.assertEqual(signal["review_task"]["task_case_id"], self.feedback_case["id"])

    def test_confirm_reprojects_evidence_from_feedback_case_to_target_case(self):
        signal = self.derive()
        original_evidence = signal["evidence"]["id"]
        _actor, _action, current = self.confirm(signal)
        self.assertEqual(current["review_task"]["task_case_id"], self.target_case["id"])
        self.assertNotEqual(current["evidence"]["id"], original_evidence)
        self.assertEqual(current["evidence"]["task_case_id"], self.target_case["id"])

    def test_machine_rederive_keeps_evidence_on_confirmed_target_case(self):
        signal = self.derive()
        self.confirm(signal)
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        current = upgraded.derive_user_event(
            self.store.get_event(signal["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertEqual((current["review_task"]["task_case_id"], current["evidence"]["task_case_id"]),
                         (self.target_case["id"], self.target_case["id"]))

    def test_low_confidence_candidate_does_not_create_assessment(self):
        payload = {"metadata": {
            "feedback_detector_version": self.service.detector_version,
            "feedback_candidates": [self.candidate(confidence=0.7)],
        }}
        event = self.event(payload=payload)
        signal = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.assertIsNone(signal["review_task"])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM outcome_assessments").fetchone()[0], 0)

    def test_feedback_assessment_does_not_advance_skill_assessment_revision(self):
        assessment = self.store.create_assessment_revision(
            self.target_case["id"], expected_revision=0, skill_id="demo",
            assessability="assessable", automated_verdict="pass",
        )
        self.derive()
        case = self.store.execute(
            "SELECT current_assessment_revision FROM task_cases WHERE id=?", (self.target_case["id"],),
        ).fetchone()
        self.assertEqual(case[0], assessment["revision"])
        self.assertEqual(self.store.execute(
            "SELECT is_current FROM outcome_assessments WHERE id=?", (assessment["id"],)
        ).fetchone()[0], 1)

    def test_priority_uses_severity_and_channel(self):
        user = self.derive()
        self.assertEqual(user["review_task"]["priority"], 90)
        self.assertGreater(FeedbackService._priority("critical", "process-anomaly"),
                           FeedbackService._priority("high", "user-feedback"))

    # Process anomalies
    def test_unknown_agent_creates_two_process_signals(self):
        payload = {"result": {"results": [{
            "agent": "designer", "agentSource": "unknown", "exitCode": 1,
            "stderr": "Unknown agent: designer", "messages": [], "usage": {"turns": 0},
        }]}, "outcome": {"isError": False}}
        event = self.event("unknown-agent", event_type="tool_result", payload=payload)
        found = self.service.derive_process_result(
            event, {"tool_name": "subagent", "args": {"agent": "designer"}},
            {"own_case_id": self.feedback_case["id"], "target_task_case_id": self.target_case["id"]},
        )
        self.assertEqual([item["machine_revisions"][0]["category"] for item in found],
                         ["agent-unavailable", "dispatch-not-executed"])

    def test_matching_complete_process_retry_resolves_initial_dispatch_anomalies(self):
        failure = self.event(
            "retry-failure", event_type="tool_result",
            payload={"result": {"results": [{
                "agent": "designer", "agentSource": "unknown", "exitCode": 1,
                "stderr": "Unknown agent: designer", "messages": [], "usage": {"turns": 0},
            }]}, "outcome": {"isError": False}},
        )
        signals = self.service.derive_process_result(
            failure, {"tool_name": "subagent", "args": {"agent": "designer", "expectedResultId": "done"}},
            {"own_case_id": self.target_case["id"], "target_task_case_id": self.target_case["id"]},
        )
        actor = self.actor("Process reviewer")
        for signal in signals:
            _actor, _confirmed, current = self.confirm(signal, actor)
            fixing = self.service.append_action(
                signal["id"], "start-fix", actor_id=actor["id"],
                expected_revision=current["current_action_revision"], reason_code="retrying",
            )
            self.service.append_action(
                signal["id"], "request-verification", actor_id=actor["id"],
                expected_revision=fixing["revision"], reason_code="retry-ready",
            )
        success = self.event(
            "retry-success", event_type="tool_result",
            payload={"result": {"results": [{
                "agent": "designer", "agentSource": "user", "exitCode": 0,
                "messages": [{"role": "assistant"}], "usage": {"turns": 2},
                "resultId": "done",
            }]}, "outcome": {"isError": False}},
        )
        result = self.service.derive_process_result(
            success, {"tool_name": "subagent", "args": {"agent": "designer", "expectedResultId": "done"}},
            {"own_case_id": self.target_case["id"]},
        )
        self.assertEqual(result, [])
        for signal in signals:
            current = self.service.get_signal(signal["id"])
            self.assertEqual(current["current_resolution_state"], "resolved-verified")
            self.assertEqual(current["actions"][-1]["reason_code"], "process-plan-completed")

    def test_same_shape_retry_without_expected_result_ids_does_not_auto_resolve(self):
        failure = self.event(
            "unbound-retry-failure", event_type="tool_result",
            payload={"result": {"results": [{
                "agent": "designer", "agentSource": "unknown", "exitCode": 1,
                "stderr": "Unknown agent", "messages": [], "usage": {"turns": 0},
            }]}, "outcome": {"isError": False}},
        )
        signals = self.service.derive_process_result(
            failure, {"tool_name": "subagent", "args": {"agent": "designer"}},
            {"own_case_id": self.target_case["id"], "target_task_case_id": self.target_case["id"]},
        )
        success = self.event(
            "unbound-retry-success", event_type="tool_result",
            payload={"result": {"results": [{
                "agent": "designer", "agentSource": "user", "exitCode": 0,
                "messages": [{"role": "assistant"}], "usage": {"turns": 1},
            }]}, "outcome": {"isError": False}},
        )
        self.service.derive_process_result(
            success, {"tool_name": "subagent", "args": {"agent": "designer"}},
            {"own_case_id": self.target_case["id"]},
        )
        self.assertTrue(all(
            self.service.get_signal(signal["id"])["current_resolution_state"] == "unreviewed"
            for signal in signals
        ))

    def test_outer_success_does_not_hide_nested_failure(self):
        event = self.event(
            "nested-failure", event_type="tool_result",
            payload={"result": {"results": [{"exitCode": 2, "usage": {"turns": 1}}]},
                     "outcome": {"isError": False}},
        )
        found = self.service.derive_process_result(
            event, {"tool_name": "subagent", "args": {"agent": "worker"}},
            {"target_task_case_id": self.target_case["id"]},
        )
        self.assertEqual(found[0]["machine_revisions"][0]["category"], "tool-error")

    def test_successful_process_result_is_clean(self):
        event = self.event(
            "clean-result", event_type="tool_result",
            payload={"result": {"exitCode": 0}, "outcome": {"isError": False}},
        )
        self.assertEqual(self.service.derive_process_result(event, {"tool_name": "bash"}, {}), [])

    def test_process_result_targets_stored_tool_result(self):
        _call, result = self.tool()
        found = self.service.derive_process_result(
            result, {"tool_name": "bash", "stored_tool_call_id": "tool-call"},
            {"own_case_id": self.feedback_case["id"]},
        )
        self.assertEqual(found[0]["targets"][0]["target_kind"], "tool-result")

    def test_detected_and_queued_actions_are_append_only(self):
        signal = self.derive()
        self.assertEqual([item["action"] for item in signal["actions"]], ["detected", "queued"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute("UPDATE feedback_actions SET reason_code='changed' WHERE feedback_signal_id=?", (signal["id"],))

    # Reviewer workflow and optimistic concurrency
    def test_non_reviewer_cannot_claim(self):
        signal = self.derive()
        viewer = self.actor("Viewer", ())
        with self.assertRaises(EffectStoreError):
            self.service.claim(signal["id"], actor_id=viewer["id"], expected_revision=2)

    def test_claim_updates_queue_and_action(self):
        signal = self.derive()
        actor = self.actor()
        action = self.service.claim(signal["id"], actor_id=actor["id"], expected_revision=2)
        current = self.service.get_signal(signal["id"])
        self.assertEqual((action["action"], current["review_task"]["claimed_by_actor_id"]),
                         ("claimed", actor["id"]))

    def test_claim_rejects_stale_revision(self):
        signal = self.derive()
        with self.assertRaises(RevisionConflict):
            self.service.claim(signal["id"], actor_id=self.actor()["id"], expected_revision=1)

    def test_claimed_signal_rejects_other_reviewer(self):
        signal = self.derive()
        first = self.actor("First")
        second = self.actor("Second")
        self.service.claim(signal["id"], actor_id=first["id"], expected_revision=2)
        with self.assertRaises(RevisionConflict):
            self.service.append_action(
                signal["id"], "exclude", actor_id=second["id"], expected_revision=3,
                reason_code="not-owner",
            )

    def test_confirm_sets_target_and_resolution(self):
        signal = self.derive()
        _actor, action, current = self.confirm(signal)
        self.assertEqual(action["target_id"], current["current_confirmed_target_id"])
        self.assertEqual(current["current_resolution_state"], "action-required")

    def test_exclude_closes_review_as_false_positive(self):
        signal = self.derive()
        action = self.service.append_action(
            signal["id"], "exclude", actor_id=self.actor()["id"], expected_revision=2,
            reason_code="instructional-negation", note="sensitive note",
        )
        current = self.service.get_signal(signal["id"])
        self.assertEqual((current["current_resolution_state"], current["review_task"]["status"]),
                         ("false-positive", "decided"))
        self.assertEqual(action["note"], "sensitive note")

    def test_retarget_uses_target_from_same_signal(self):
        assistant = self.event("retarget-assistant", "answer", event_type="assistant_message")
        self.make_live(assistant)
        signal = self.derive(context={
            "target_task_case_id": self.target_case["id"], "target_event_id": assistant["id"],
        })
        target = next(item for item in signal["targets"] if item["target_kind"] == "assistant-result")
        self.service.append_action(
            signal["id"], "retarget", actor_id=self.actor()["id"],
            expected_revision=signal["current_action_revision"],
            reason_code="wrong-target", target_id=target["id"],
        )
        self.assertEqual(self.service.get_signal(signal["id"])["current_confirmed_target_id"], target["id"])

    def test_ambiguous_targets_stay_below_queue_threshold(self):
        assistant = self.event("ambiguous-assistant", "answer", event_type="assistant_message")
        self.make_live(assistant)
        signal = self.derive(context={
            "target_task_case_id": self.target_case["id"], "target_event_id": assistant["id"],
        })
        self.assertEqual(signal["machine_revisions"][0]["confidence"], 0.79)
        self.assertIsNone(signal["review_task"])

    def test_full_fix_verification_resolution_and_reopen_workflow(self):
        signal = self.derive()
        actor, _confirmed, current = self.confirm(signal)
        verification_id = self.verification_evidence("workflow-verification")
        for action, expected, resolution in (
            ("start-fix", 3, "fix-in-progress"),
            ("request-verification", 4, "awaiting-verification"),
            ("resolve-verified", 5, "resolved-verified"),
            ("reopen", 6, "unreviewed"),
        ):
            self.service.append_action(
                signal["id"], action, actor_id=actor["id"], expected_revision=expected,
                reason_code=action,
                binding={"evidenceId": verification_id} if action == "resolve-verified" else None,
            )
            self.assertEqual(self.service.get_signal(signal["id"])["current_resolution_state"], resolution)

    def test_verified_resolution_requires_locatable_evidence(self):
        signal = self.derive()
        actor, _confirmed, _current = self.confirm(signal)
        self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"], expected_revision=3,
            reason_code="started",
        )
        self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"], expected_revision=4,
            reason_code="verify",
        )
        with self.assertRaises(EffectStoreError):
            self.service.append_action(
                signal["id"], "resolve-verified", actor_id=actor["id"], expected_revision=5,
                reason_code="missing-evidence",
            )

    def test_negative_feedback_evidence_cannot_verify_resolution(self):
        signal = self.derive()
        actor, _confirmed, _current = self.confirm(signal)
        self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"], expected_revision=3,
            reason_code="started",
        )
        self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"], expected_revision=4,
            reason_code="verify",
        )
        with self.assertRaises(EffectStoreError):
            self.service.append_action(
                signal["id"], "resolve-verified", actor_id=actor["id"], expected_revision=5,
                reason_code="wrong-polarity", binding={"evidenceId": signal["evidence"]["id"]},
            )

    def test_resolve_alias_without_evidence_is_unverified(self):
        signal = self.derive()
        actor, _confirmed, _current = self.confirm(signal)
        self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"], expected_revision=3,
            reason_code="started",
        )
        self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"], expected_revision=4,
            reason_code="verify",
        )
        action = self.service.append_action(
            signal["id"], "resolve", actor_id=actor["id"], expected_revision=5,
            reason_code="manual-close",
        )
        self.assertEqual((action["action"], action["to_resolution_state"]),
                         ("resolve-unverified", "resolved-unverified"))

    def test_action_rejects_stale_revision(self):
        signal = self.derive()
        actor = self.actor()
        self.service.append_action(
            signal["id"], "exclude", actor_id=actor["id"], expected_revision=2,
            reason_code="false-positive",
        )
        with self.assertRaises(RevisionConflict):
            self.service.append_action(
                signal["id"], "reopen", actor_id=actor["id"], expected_revision=2,
                reason_code="stale",
            )

    def test_action_rejects_invalid_state_transition(self):
        signal = self.derive()
        with self.assertRaises(EffectStoreError):
            self.service.append_action(
                signal["id"], "resolve-verified", actor_id=self.actor()["id"],
                expected_revision=2, reason_code="skipped-verification",
            )

    def test_action_rejects_foreign_target(self):
        one = self.derive()
        two = self.derive(fingerprint="second-feedback")
        with self.assertRaises(EffectStoreError):
            self.service.append_action(
                one["id"], "confirm", actor_id=self.actor()["id"], expected_revision=2,
                reason_code="foreign", target_id=two["targets"][0]["id"],
            )

    # Queries, cluster, derivation state
    def test_list_signals_filters_and_paginates(self):
        self.derive(fingerprint="one")
        self.derive(text="权限校验漏了", fingerprint="two")
        found = self.service.list_signals(category="requirement-gap", limit=1)
        self.assertEqual(len(found["items"]), 1)
        self.assertEqual(found["items"][0]["category"], "requirement-gap")

    def test_get_signal_includes_timeline_and_evidence(self):
        signal = self.derive()
        detail = self.service.get_signal(signal["id"])
        self.assertEqual(len(detail["actions"]), 2)
        self.assertEqual(detail["evidence"]["evidence_type"], "session-negative-feedback")

    def test_case_feedback_finds_feedback_and_target_cases(self):
        signal = self.derive()
        self.assertEqual(self.service.case_feedback(self.feedback_case["id"])[0]["id"], signal["id"])
        self.assertEqual(self.service.case_feedback(self.target_case["id"])[0]["id"], signal["id"])

    def test_overview_separates_channels_and_states(self):
        signal = self.derive()
        self.service.append_action(
            signal["id"], "exclude", actor_id=self.actor()["id"], expected_revision=2,
            reason_code="false-positive",
        )
        overview = self.service.overview()
        self.assertEqual((overview["total"], overview["user_feedback"], overview["false_positives"]), (1, 1, 1))

    def test_cluster_groups_equivalent_signals(self):
        actor = self.actor()
        first = self.derive(fingerprint="cluster-one")
        second = self.derive(fingerprint="cluster-two")
        self.confirm(first, actor)
        self.confirm(second, actor)
        clusters = self.service.clusters()["items"]
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["member_count"], 2)

    def test_bootstrap_derives_existing_events(self):
        event = self.event("bootstrap-event")
        self.make_live(event)
        outcome = self.service.bootstrap(max_events=10)
        self.assertTrue(outcome["bootstrapComplete"])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0], 1)

    def test_process_changes_is_idempotent(self):
        self.event("change-event")
        first = self.service.process_changes(max_changes=20)
        second = self.service.process_changes(max_changes=20)
        self.assertGreaterEqual(first["processed"], 0)
        self.assertEqual(second["processed"], 0)

    def test_event_orphaning_marks_signal_and_queue(self):
        event = self.event("orphan-event")
        signal = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.service.bootstrap()
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('event-orphaned','canonical-event',?,'{}',?)""", (event["id"], NOW),
            )
        self.service.process_changes()
        current = self.service.get_signal(signal["id"])
        self.assertEqual((current["machine_revisions"][-1]["orphaned"], current["current_process_state"]),
                         (1, "orphaned"))

    def test_reactivation_restores_same_signal_without_new_revision(self):
        event = self.event("reactivate-event")
        signal = self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )[0]
        self.service._orphan_event(event["id"])
        self.service.derive_user_event(
            event, self.feedback_case["id"], {"previous_task_case_id": self.target_case["id"]},
        )
        current = self.service.get_signal(signal["id"])
        self.assertEqual(len(current["machine_revisions"]), 1)
        self.assertEqual(current["machine_revisions"][0]["orphaned"], 0)
        self.assertIn("reactivated", [item["action"] for item in current["actions"]])

    def test_target_event_provenance_loss_and_restore_reopens_target_review(self):
        target_event = self.event("target-assistant", "answer", event_type="assistant_message")
        self.make_live(target_event)
        signal = self.derive(context={"target_event_id": target_event["id"]})
        actor, _action, confirmed = self.confirm(signal)
        generation_id = self.store.execute(
            "SELECT generation_id FROM event_provenance WHERE event_id=?", (target_event["id"],)
        ).fetchone()[0]
        self.store.delete_generation(generation_id)
        self.service.process_changes()
        orphaned = self.service.get_signal(signal["id"])
        selected = next(item for item in orphaned["targets"] if item["id"] == confirmed["current_confirmed_target_id"])
        self.assertEqual((selected["machine_status"], orphaned["current_process_state"]), ("orphaned", "triaged"))
        replacement = self.store.upsert_generation(
            self.store.upsert_log_file("pi", "feedback-target-restore")["id"],
            "feedback-target-restore", "test-v1",
        )
        self.store.upsert_provenance(target_event["id"], replacement["id"], 0)
        self.service.process_changes()
        restored = self.service.get_signal(signal["id"])
        selected = next(item for item in restored["targets"] if item["id"] == confirmed["current_confirmed_target_id"])
        self.assertEqual(selected["machine_status"], "candidate")
        self.assertEqual(restored["review_task"]["status"], "open")
        self.assertEqual(restored["actions"][-1]["reason_code"], "target-provenance-restored")

    def test_tool_result_target_restores_by_stable_id_after_rebuild(self):
        call_event, result_event = self.tool()
        self.make_live(call_event, result_event)
        signal = self.service.derive_process_result(
            result_event, {"tool_name": "bash", "stored_tool_call_id": "tool-call"},
            {"own_case_id": self.target_case["id"]},
        )[0]
        self.confirm(signal)
        self.service.bootstrap()
        with self.store.transaction():
            self.store.execute("DELETE FROM tool_results WHERE id='tool-result'")
        self.service.process_changes()
        orphaned = self.service.get_signal(signal["id"])
        target = next(item for item in orphaned["targets"] if item["target_kind"] == "tool-result")
        self.assertEqual((target["machine_status"], target["tool_result_id"]), ("orphaned", None))
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO tool_results(
                       id, result_fingerprint, tool_call_id, event_id, status,
                       exit_code, output_hash, completed_at, metadata_json)
                   VALUES ('tool-result', 'tool-result-restored', 'tool-call', ?,
                       'error', 1, 'hash', ?, '{}')""",
                (result_event["id"], NOW),
            )
        self.service.process_changes()
        restored = self.service.get_signal(signal["id"])
        target = next(item for item in restored["targets"] if item["target_kind"] == "tool-result")
        self.assertEqual((target["machine_status"], target["tool_result_id"]), ("candidate", "tool-result"))
        self.assertEqual(restored["review_task"]["queue_reason"], "target-restored-review")

    def test_target_invalidation_requeues_signal(self):
        signal = self.derive()
        self.service.bootstrap()
        target = signal["targets"][0]
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('target-invalidated','feedback-target',?,'{}',?)""",
                (target["id"], NOW),
            )
        self.service.process_changes()
        current = self.service.get_signal(signal["id"])
        self.assertEqual((current["targets"][0]["machine_status"], current["current_process_state"]),
                         ("orphaned", "triaged"))

    def test_case_invalidation_orphans_related_target(self):
        signal = self.derive()
        self.service.bootstrap()
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('case-invalidated','task-case',?,'{}',?)""",
                (self.target_case["id"], NOW),
            )
        self.service.process_changes()
        self.assertEqual(self.service.get_signal(signal["id"])["targets"][0]["machine_status"],
                         "orphaned")
        with self.store.transaction():
            self.store.execute(
                "UPDATE task_cases SET invalidated_at=NULL WHERE id=?", (self.target_case["id"],),
            )
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('case-reactivated','task-case',?,'{}',?)""",
                (self.target_case["id"], NOW),
            )
        self.service.process_changes()
        restored = self.service.get_signal(signal["id"])
        self.assertEqual(restored["targets"][0]["machine_status"], "candidate")
        self.assertEqual(restored["review_task"]["queue_reason"], "target-restored-review")

    def test_generation_deletion_emits_skill_target_invalidation(self):
        invocation = self.skill("generation-skill")
        invocation_event = self.store.execute(
            """SELECT e.* FROM skill_invocations i JOIN canonical_events e ON e.id=i.event_id
               WHERE i.id=?""", (invocation,),
        ).fetchone()
        self.make_live(invocation_event)
        signal = self.derive(
            text="generation-skill 技能完全不对",
            context={"skill_invocation_id": invocation},
        )
        self.confirm(signal)
        self.service.bootstrap()
        generation_id = self.store.execute(
            "SELECT generation_id FROM event_provenance WHERE event_id=?",
            (invocation_event["id"],),
        ).fetchone()[0]
        self.store.delete_generation(generation_id)
        self.assertIsNotNone(self.store.execute(
            """SELECT 1 FROM effect_derivation_changes
               WHERE change_type='target-invalidated' AND entity_id=?""", (invocation,),
        ).fetchone())
        self.service.process_changes()
        current = self.service.get_signal(signal["id"])
        self.assertEqual(current["targets"][0]["machine_status"], "orphaned")
        self.assertEqual(current["review_task"]["status"], "open")
        with self.store.transaction():
            self.store.execute(
                "UPDATE skill_invocations SET validity='valid' WHERE id=?", (invocation,),
            )
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('target-reactivated','skill-invocation',?,'{}',?)""",
                (invocation, NOW),
            )
        self.service.process_changes()
        restored = self.service.get_signal(signal["id"])
        self.assertEqual(restored["targets"][0]["machine_status"], "candidate")
        self.assertEqual(restored["review_task"]["queue_reason"], "target-restored-review")

    def test_change_budget_reports_pending(self):
        first = self.event("budget-one")
        second = self.event("budget-two")
        self.make_live(first, second)
        outcome = self.service.bootstrap(max_events=1)
        self.assertTrue(outcome["pending"])

    def test_bootstrap_budget_resumes_after_last_event(self):
        first = self.event("resume-one")
        second = self.event("resume-two")
        self.make_live(first, second)
        self.assertTrue(self.service.bootstrap(max_events=1)["pending"])
        completed = self.service.bootstrap(max_events=1)
        self.assertFalse(completed["pending"])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0], 2)

    def test_machine_rederive_preserves_resolved_human_projection(self):
        signal = self.derive()
        actor, _confirmed, _current = self.confirm(signal)
        verification_id = self.verification_evidence("rederive-verification")
        for action, revision in (
            ("start-fix", 3), ("request-verification", 4), ("resolve-verified", 5),
        ):
            self.service.append_action(
                signal["id"], action, actor_id=actor["id"], expected_revision=revision,
                reason_code=action,
                binding={"evidenceId": verification_id} if action == "resolve-verified" else None,
            )
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        upgraded.derive_user_event(
            self.store.get_event(signal["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        current = upgraded.get_signal(signal["id"])
        self.assertEqual((current["current_process_state"], current["current_resolution_state"]),
                         ("closed", "resolved-verified"))

    # Cleanup, calibration, immutable formal snapshots
    def test_cleanup_by_case_redacts_machine_body(self):
        signal = self.derive()
        outcome = self.service.cleanup(task_case_id=self.feedback_case["id"])
        current = self.service.get_signal(signal["id"])
        self.assertEqual(outcome["signals"], 1)
        self.assertIsNone(current["machine_revisions"][0]["redacted_excerpt"])
        self.assertEqual(current["targets"][0]["evidence"], {})

    def test_cleanup_prevents_cluster_recreation(self):
        signal = self.derive()
        self.confirm(signal)
        self.assertEqual(len(self.service.clusters()["items"]), 1)
        self.service.cleanup(task_case_id=self.feedback_case["id"])
        self.assertEqual(self.service.clusters()["items"], [])

    def test_orphan_signal_is_removed_from_cluster_projection(self):
        signal = self.derive()
        self.confirm(signal)
        self.assertEqual(len(self.service.clusters()["items"]), 1)
        self.service._orphan_event(signal["feedback_event_id"])
        self.assertEqual(self.service.clusters()["items"], [])

    def test_cleanup_retains_action_reason_and_revision(self):
        signal = self.derive()
        self.service.append_action(
            signal["id"], "exclude", actor_id=self.actor()["id"], expected_revision=2,
            reason_code="retained-reason", note="sensitive",
        )
        self.service.cleanup(task_case_id=self.feedback_case["id"])
        action = self.store.execute(
            "SELECT revision, reason_code, note FROM feedback_actions WHERE feedback_signal_id=? ORDER BY revision DESC",
            (signal["id"],),
        ).fetchone()
        self.assertEqual(tuple(action), (3, "retained-reason", None))

    def test_cleanup_by_skill_uses_explicit_skill_target(self):
        invocation = self.skill("cleanup-skill")
        signal = self.derive(text="cleanup-skill 技能完全不对", context={"skill_invocation_id": invocation})
        self.assertEqual(self.service.cleanup(skill_id="cleanup-skill")["signals"], 1)
        self.assertIsNone(self.service.get_signal(signal["id"])["machine_revisions"][0]["redacted_excerpt"])

    def test_cleanup_by_skill_follows_target_case_attribution(self):
        self.skill("case-skill")
        signal = self.derive()
        self.assertEqual(self.service.cleanup(skill_id="case-skill")["signals"], 1)
        self.assertIsNone(self.service.get_signal(signal["id"])["machine_revisions"][0]["redacted_excerpt"])

    def test_cleanup_by_skill_retains_feedback_for_shared_multi_skill_case(self):
        self.skill("shared-a", attribution="shared", invocation_id="shared-a-invocation")
        self.skill("shared-b", attribution="shared", invocation_id="shared-b-invocation")
        signal = self.derive()
        result = self.service.cleanup(skill_id="shared-a")
        self.assertEqual(result["signals"], 0)
        self.assertIsNotNone(
            self.service.get_signal(signal["id"])["machine_revisions"][0]["redacted_excerpt"]
        )

    def test_cleanup_by_target_project(self):
        with self.store.transaction():
            self.store.execute(
                "UPDATE task_cases SET metadata_json=? WHERE id=?",
                (json.dumps({"projectId": "project-one"}), self.target_case["id"]),
            )
        signal = self.derive()
        self.assertEqual(self.service.cleanup(project_id="project-one")["signals"], 1)
        self.assertIsNone(self.service.get_signal(signal["id"])["machine_revisions"][0]["redacted_excerpt"])

    def test_cleanup_purges_all_feedback_evidence_revisions_not_only_current_trigger(self):
        signal = self.derive()
        actor, _confirmed, current = self.confirm(signal)
        started = self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"],
            expected_revision=current["current_action_revision"], reason_code="fixing",
        )
        waiting = self.service.append_action(
            signal["id"], "request-verification", actor_id=actor["id"],
            expected_revision=started["revision"], reason_code="verify",
        )
        verification_id = self.verification_evidence("cleanup-verification-evidence")
        self.service.append_action(
            signal["id"], "resolve-verified", actor_id=actor["id"],
            expected_revision=waiting["revision"], reason_code="verified",
            binding={"evidenceId": verification_id},
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO evidence_items(
                       id, evidence_fingerprint, task_case_id, event_id, evidence_type,
                       content_hash, locator_json, excerpt, validity, created_at)
                   VALUES ('older-feedback-evidence', 'older-feedback-evidence', ?, ?,
                       'session-negative-feedback', 'hash', '{"line":1}', 'old sensitive excerpt',
                       'valid', ?)""",
                (self.feedback_case["id"], signal["feedback_event_id"], NOW),
            )
        result = self.service.cleanup(task_case_id=self.feedback_case["id"])
        self.assertGreaterEqual(result["evidenceItems"], 2)
        rows = self.store.execute(
            """SELECT excerpt, locator_json, validity FROM evidence_items
               WHERE event_id=? AND evidence_type='session-negative-feedback'""",
            (signal["feedback_event_id"],),
        ).fetchall()
        self.assertTrue(all(
            (row["excerpt"], row["locator_json"], row["validity"]) == (None, "{}", "purged")
            for row in rows
        ))
        verification = self.store.execute(
            "SELECT excerpt, locator_json, validity FROM evidence_items WHERE id=?",
            (verification_id,),
        ).fetchone()
        self.assertEqual(tuple(verification), (None, "{}", "purged"))

    def test_cleanup_by_time(self):
        signal = self.derive()
        self.assertEqual(self.service.cleanup(older_than="2027-01-01T00:00:00Z")["signals"], 1)
        self.assertIsNone(self.service.get_signal(signal["id"])["machine_revisions"][0]["redacted_excerpt"])

    def test_calibration_profile_is_ineligible_below_threshold(self):
        profile = self.service.build_calibration_profile()
        self.assertEqual((profile["sample_count"], profile["eligible"]), (0, 0))

    def test_calibration_profile_is_idempotent(self):
        first = self.service.build_calibration_profile()
        second = self.service.build_calibration_profile()
        self.assertEqual(first["id"], second["id"])

    def test_calibration_only_counts_actions_bound_to_current_machine_revision(self):
        signal = self.derive()
        self.confirm(signal)
        current_profile = self.service.build_calibration_profile(
            language="zh", category="result-rejection",
        )
        self.assertEqual(current_profile["sample_count"], 1)
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        upgraded.derive_user_event(
            self.store.get_event(signal["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        stale_profile = upgraded.build_calibration_profile(
            language="zh", category="result-rejection",
        )
        self.assertEqual(stale_profile["sample_count"], 0)

    def test_unconfirmed_snapshot_rejects_profile_from_other_category_or_model(self):
        signal = self.derive()
        self.complete_scan()
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO feedback_calibration_profiles(
                       id, detector_version, resolver_version, language, category,
                       model_version, prompt_version, rubric_version, corpus_sha256,
                       sample_count, major_category_sample_count, precision_lower_bound,
                       target_accuracy_lower_bound, eligible, metrics_json, created_at)
                   VALUES ('wrong-profile', ?, ?, 'zh', 'observed-defect',
                       'other-model', 'other-prompt', 'other-rubric', 'corpus',
                       200, 30, .99, .99, 1, '{}', ?)""",
                (self.service.detector_version, self.service.resolver_version, NOW),
            )
        snapshot = self.service.create_feedback_snapshot(calibration_profile_id="wrong-profile")
        item = next(item for item in snapshot["items"] if item["feedback_signal_id"] == signal["id"])
        self.assertEqual((item["metric_eligible"], item["exclusion_reason"]), (0, "target-unconfirmed"))
        self.assertIsNone(item["calibration_profile_id"])
        self.assertEqual(snapshot["versions"]["calibrationProfileIds"], [])

    def test_partial_snapshot_excludes_every_signal(self):
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan("partial")
        snapshot = self.service.create_feedback_snapshot()
        self.assertEqual(snapshot["items"][0]["metric_eligible"], 0)
        self.assertEqual(snapshot["items"][0]["exclusion_reason"], "coverage-incomplete")

    def test_snapshot_rejects_pending_derivation_and_historical_cutoff(self):
        self.derive()
        self.complete_scan()
        with self.store.transaction():
            self.store.execute(
                "UPDATE feedback_derivation_state SET status='pending' WHERE detector_id=?",
                (self.service.detector_id,),
            )
        with self.assertRaisesRegex(EffectStoreError, "derivation must be complete"):
            self.service.create_feedback_snapshot()
        with self.store.transaction():
            self.store.execute(
                "UPDATE feedback_derivation_state SET status='ready' WHERE detector_id=?",
                (self.service.detector_id,),
            )
            self.store.execute(
                """INSERT INTO effect_derivation_changes(
                       change_type, entity_kind, entity_id, binding_json, created_at)
                   VALUES ('event-available','canonical-event','pending-event','{}',?)""",
                (NOW,),
            )
        with self.assertRaisesRegex(EffectStoreError, "changes are pending"):
            self.service.create_feedback_snapshot()
        with self.store.transaction():
            self.store.execute(
                """UPDATE feedback_derivation_state SET change_cursor=(
                     SELECT COALESCE(MAX(id),0) FROM effect_derivation_changes
                   ) WHERE detector_id=?""", (self.service.detector_id,),
            )
        with self.assertRaisesRegex(ValueError, "current server time"):
            self.service.create_feedback_snapshot(cutoff_at="2020-01-01T00:00:00Z")
        with self.assertRaisesRegex(EffectStoreError, "scope is stale"):
            self.service.create_feedback_snapshot(expected_scope_fingerprint="changed-scope")

    def test_ad_hoc_scan_after_configured_scan_blocks_formal_snapshot(self):
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan()
        ad_hoc = self.store.create_scan_run(
            "pi", metadata={"scopeKind": "ad-hoc", "scopeFingerprint": "other-scope"},
        )
        self.store.finish_scan_run(ad_hoc["id"], coverage_status="complete")
        with self.assertRaisesRegex(EffectStoreError, "latest configured scan"):
            self.service.create_feedback_snapshot()

    def test_partial_scan_cannot_be_overridden_to_complete(self):
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan("partial")
        snapshot = self.service.create_feedback_snapshot(coverage_status="complete")
        self.assertEqual((snapshot["coverage_status"], snapshot["items"][0]["exclusion_reason"]),
                         ("partial", "coverage-incomplete"))

    def test_confirmed_signal_is_formal_snapshot_candidate(self):
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan()
        snapshot = self.service.create_feedback_snapshot()
        item = snapshot["items"][0]
        self.assertEqual((snapshot["sealed"], item["metric_eligible"]), (1, 1))
        self.assertEqual(item["signal_machine_revision"], 1)

    def test_snapshot_excludes_confirmation_bound_to_old_machine_revision(self):
        signal = self.derive()
        self.confirm(signal)
        upgraded = FeedbackService(self.store, detector_version="feedback-v6", formal_scope_fingerprint="test-scope")
        upgraded.derive_user_event(
            self.store.get_event(signal["feedback_event_id"]), self.feedback_case["id"],
            {"previous_task_case_id": self.target_case["id"]},
        )
        self.complete_scan()
        with self.store.transaction():
            self.store.execute(
                "UPDATE feedback_derivation_state SET detector_version=? WHERE detector_id=?",
                (upgraded.detector_version, upgraded.detector_id),
            )
        snapshot = upgraded.create_feedback_snapshot()
        self.assertEqual(snapshot["items"][0]["exclusion_reason"],
                         "human-review-revision-stale")

    def test_feedback_snapshot_is_immutable(self):
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan()
        snapshot = self.service.create_feedback_snapshot()
        with self.assertRaises(ImmutableSnapshotError):
            self.store.execute(
                "UPDATE feedback_metric_snapshot_items SET resolution_state='changed' WHERE snapshot_id=?",
                (snapshot["id"],),
            )

    def test_snapshot_does_not_follow_later_signal_actions(self):
        signal = self.derive()
        actor, _action, current = self.confirm(signal)
        self.complete_scan()
        snapshot = self.service.create_feedback_snapshot()
        self.service.append_action(
            signal["id"], "start-fix", actor_id=actor["id"],
            expected_revision=current["current_action_revision"], reason_code="started",
        )
        frozen = self.service.get_feedback_snapshot(snapshot["id"])["items"][0]
        self.assertEqual((frozen["action_revision"], frozen["resolution_state"]),
                         (3, "action-required"))

    def test_snapshot_freezes_all_shared_attributions(self):
        self.skill("first", attribution="shared", invocation_id="first-invocation")
        self.skill("second", attribution="shared", invocation_id="second-invocation")
        signal = self.derive()
        self.confirm(signal)
        self.complete_scan()
        snapshot = self.service.create_feedback_snapshot()
        self.assertEqual(len(snapshot["attributions"]), 2)
        self.assertEqual({row["attribution_kind"] for row in snapshot["attributions"]}, {"shared"})

    def test_snapshot_attribution_excludes_invalid_unloaded_or_unknown_skill_version(self):
        invocation = self.skill("invalid-snapshot-skill")
        signal = self.derive(
            text="invalid-snapshot-skill 技能完全不对",
            context={"skill_invocation_id": invocation},
        )
        self.confirm(signal)
        self.complete_scan()
        with self.store.transaction():
            self.store.execute(
                """UPDATE skill_invocations SET validity='invalid', load_status='error',
                       skill_sha256=NULL WHERE id=?""", (invocation,),
            )
        snapshot = self.service.create_feedback_snapshot()
        attribution = snapshot["attributions"][0]
        self.assertEqual((attribution["metric_eligible"], attribution["exclusion_reason"]), (0, "invocation-invalid"))

    def test_explicit_skill_without_attribution_does_not_create_skill_metric_row(self):
        invocation = self.skill("unattributed")
        with self.store.transaction():
            self.store.execute("DELETE FROM attribution_links WHERE skill_invocation_id=?", (invocation,))
        signal = self.derive(
            text="unattributed 技能完全不对", context={"skill_invocation_id": invocation},
        )
        self.confirm(signal)
        self.complete_scan()
        snapshot = self.service.create_feedback_snapshot()
        self.assertEqual(snapshot["attributions"], [])


if __name__ == "__main__":
    unittest.main()