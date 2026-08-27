import http.client
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from auth import AuthService


class AppAuthOutcomeApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.codex_logs = self.root / "codex-logs"
        self.codex_logs.mkdir()
        self.codex_archived_logs = self.root / "codex-archived"
        self.codex_archived_logs.mkdir()
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.auth = AuthService(self.root / "token", self.root / "actor.json")
        self.auth.initialize()
        self.patches = [
            patch.object(app, "AUTH_SERVICE", self.auth),
            patch.object(app, "EFFECT_DB_FILE", self.root / "effects.sqlite3"),
            patch.object(app, "SKILLS_DB_FILE", self.root / "skills.sqlite3"),
            patch.object(app, "LIBRARY_DIR", self.skills),
            patch.object(app, "PI_SESSIONS_DIR", self.logs),
            patch.object(app, "CODEX_SESSIONS_DIR", self.codex_logs),
            patch.object(app, "CODEX_ARCHIVED_SESSIONS_DIR", self.codex_archived_logs),
            patch.object(app, "configured_check_roots", return_value=(self.root,)),
        ]
        for current in self.patches:
            current.start()
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for current in reversed(self.patches):
            current.stop()
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        selected_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, payload, selected_headers)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if raw else None
        headers_result = dict(response.getheaders())
        connection.close()
        return response.status, result, headers_result

    def login(self):
        token = (self.root / "token").read_text().strip()
        status, payload, headers = self.request("POST", "/api/auth/login", {"token": token})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], payload["csrfToken"]

    def test_sensitive_get_requires_login_and_status_does_not(self):
        self.assertEqual(
            app.Handler.required_roles("POST", "/api/review-tasks/example/claim"),
            ("reviewer",),
        )
        self.assertEqual(
            app.Handler.required_roles("GET", "/api/feedback-signals/example"),
            ("reviewer",),
        )
        self.assertEqual(
            app.Handler.required_roles("GET", "/api/task-cases/example"),
            ("reviewer",),
        )
        self.assertEqual(app.Handler.required_roles("GET", "/api/effect-events"), ("reviewer",))
        self.assertEqual(app.Handler.required_roles("GET", "/api/review-tasks"), ("reviewer",))
        self.assertEqual(app.Handler.required_roles("GET", "/api/skill-use-events"), ("reviewer",))
        self.assertEqual(app.Handler.required_roles("GET", "/api/effect-metrics"), ("reviewer",))
        status, payload, _headers = self.request("GET", "/api/auth/status")
        self.assertEqual((status, payload["authenticated"]), (200, False))
        status, payload, _headers = self.request("GET", "/api/effect-overview")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication-required")

        cookie, _csrf = self.login()
        status, payload, headers = self.request(
            "GET", "/api/effect-overview", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertIn("event_count", payload)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_cookie_writes_require_csrf_and_scan_is_reachable(self):
        cookie, csrf = self.login()
        body = {
            "sources": {"pi": str(self.logs)},
            "budgetBytes": 4096,
            "budgetSeconds": 2,
        }
        status, payload, _headers = self.request(
            "POST", "/api/effect-scan", body, headers={"Cookie": cookie}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "forbidden")

        status, payload, _headers = self.request(
            "POST", "/api/effect-scan", body,
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["coverage_status"], "complete")
        status, rejected, _headers = self.request(
            "POST", "/api/effect-metric-snapshots", {},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 400)
        self.assertIn("configured catalog", rejected["error"])
        status, calibration, _headers = self.request(
            "POST", "/api/calibration-profiles",
            {"contractVersionId": "contract-v1", "taskType": "test", "source": "pi",
             "modelVersion": "model-v1", "promptVersion": "prompt-v1", "rubricVersion": "rubric-v1",
             "sampleCount": 9999, "majorTaskSampleCount": 9999, "passPrecisionLowerBound": 1},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertEqual((calibration["sample_count"], calibration["eligible"]), (0, False))
        self.assertTrue(calibration["metrics"]["derived"])

    def test_bad_login_is_unauthorized_and_logout_clears_cookie(self):
        status, payload, _headers = self.request("POST", "/api/auth/login", {"token": "bad"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication-required")
        cookie, csrf = self.login()
        status, payload, headers = self.request(
            "POST", "/api/auth/logout", {},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual((status, payload["authenticated"]), (200, False))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_configured_missing_archive_root_keeps_scan_partial_and_in_scope(self):
        self.codex_archived_logs.rmdir()
        cookie, csrf = self.login()
        status, scan, _headers = self.request(
            "POST", "/api/effect-scan", {"budgetBytes": 100000, "budgetSeconds": 2},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertEqual((scan["status"], scan["coverage_status"]), ("partial", "partial"))
        self.assertGreaterEqual(scan["failed_files"], 1)
        self.assertEqual(
            scan["metadata"]["scopeFingerprint"], app.configured_session_scope_fingerprint(),
        )

    def test_cleanup_run_records_failure_and_can_be_retried_idempotently(self):
        cookie, csrf = self.login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        body = {"cleanupRunId": "cleanup-run-test", "olderThan": "2020-01-01T00:00:00Z"}
        with patch("feedback_service.FeedbackService.cleanup", side_effect=RuntimeError("failed stage")):
            status, _failed, _response_headers = self.request(
                "POST", "/api/effect-data/cleanup", body, headers=headers,
            )
        self.assertEqual(status, 500)
        status, run, _response_headers = self.request(
            "GET", "/api/effect-data/cleanup-runs/cleanup-run-test", headers={"Cookie": cookie},
        )
        self.assertEqual((status, run["status"], run["stage"]), (200, "failed", "feedback"))
        status, completed, _response_headers = self.request(
            "POST", "/api/effect-data/cleanup", body, headers=headers,
        )
        self.assertEqual((status, completed["cleanupRunId"]), (200, "cleanup-run-test"))
        status, replay, _response_headers = self.request(
            "POST", "/api/effect-data/cleanup", body, headers=headers,
        )
        self.assertEqual((status, replay), (200, completed))

    def test_quality_api_assigns_and_records_trial_judgment(self):
        skill = self.skills / "trial-quality" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("# trial quality\n", encoding="utf-8")
        session = self.logs / "trial-quality.jsonl"
        session.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in [
            {"type": "session", "id": "trial-quality-session", "timestamp": "2026-08-26T00:00:00Z"},
            {"type": "message", "id": "trial-quality-user", "timestamp": "2026-08-26T00:00:01Z", "message": {
                "role": "user", "content": [{"type": "text", "text": "完成报告"}],
            }},
            {"type": "message", "id": "trial-quality-skill", "timestamp": "2026-08-26T00:00:02Z", "message": {
                "role": "user", "content": [{"type": "text", "text":
                    f'<skill name="trial-quality" location="{skill}">{skill.read_text()}</skill>'}],
            }},
        ]), encoding="utf-8")
        cookie, csrf = self.login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        status, scan, _headers = self.request(
            "POST", "/api/effect-scan", {"budgetBytes": 100000, "budgetSeconds": 2}, headers=headers,
        )
        self.assertEqual((status, scan["coverage_status"]), (200, "complete"))
        status, directory, _headers = self.request("GET", "/api/skill-quality?skillId=trial-quality", headers={"Cookie": cookie})
        self.assertEqual((status, len(directory["items"])), (200, 1))
        self.assertEqual(directory["items"][0]["quality_status"], "evidence-insufficient")
        status, filtered, _headers = self.request(
            "GET", "/api/skill-quality?skillId=trial-quality&source=pi&from=2020-01-01T00:00:00Z&to=2030-01-01T00:00:00Z",
            headers={"Cookie": cookie},
        )
        self.assertEqual((status, len(filtered["items"])), (200, 1))
        status, invalid_source, _headers = self.request(
            "GET", "/api/skill-quality?source=unknown", headers={"Cookie": cookie},
        )
        self.assertEqual(status, 400)
        self.assertIn("source", invalid_source["error"])
        status, uses, _headers = self.request("GET", "/api/skill-use-events?skill=trial-quality", headers={"Cookie": cookie})
        self.assertEqual((status, len(uses["items"])), (200, 1))
        invocation_id = uses["items"][0]["id"]
        status, assignment, _headers = self.request(
            "POST", "/api/skill-use-judgment-assignments", {"skillInvocationId": invocation_id}, headers=headers,
        )
        self.assertEqual((status, assignment["status"]), (200, "active"))
        status, trial_users, _headers = self.request("GET", "/api/trial-users", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(self.auth.actor.uuid, [item["id"] for item in trial_users["items"]])
        status, judgment, _headers = self.request(
            "POST", "/api/skill-use-judgments", {
                "skillInvocationId": invocation_id, "expectedRevision": 0,
                "verdict": "helpful", "attributionRelation": "direct-skill-use",
            }, headers=headers,
        )
        self.assertEqual((status, judgment["revision"], judgment["verdict"]), (200, 1, "helpful"))
        status, assignments, _headers = self.request(
            "GET", "/api/skill-use-judgment-assignments/current", headers={"Cookie": cookie},
        )
        self.assertEqual((status, assignments["items"][0]["skill_invocation_id"]), (200, invocation_id))

    def test_cleanup_rejects_invalid_criteria_before_creating_run(self):
        cookie, csrf = self.login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        status, _payload, _response_headers = self.request(
            "POST", "/api/effect-data/cleanup",
            {"cleanupRunId": "invalid-cleanup", "olderThan": "2020-01-01"},
            headers=headers,
        )
        self.assertEqual(status, 400)
        status, _missing, _response_headers = self.request(
            "GET", "/api/effect-data/cleanup-runs/invalid-cleanup",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 404)

    def test_cleanup_retry_resumes_failed_stage_without_repeating_completed_stages(self):
        cookie, csrf = self.login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        body = {"cleanupRunId": "cleanup-run-resume", "olderThan": "2020-01-01T00:00:00Z"}

        class FakeCollector:
            cleanup_calls = 0

            def materialize_cleanup_context(self):
                return {"updated": 1}

            def cleanup(self, **_criteria):
                self.cleanup_calls += 1
                if self.cleanup_calls == 1:
                    raise RuntimeError("prospective failed")
                return {"deleted": 3}

        collector = FakeCollector()
        with (
            patch("app.prospective_collector", return_value=collector),
            patch("feedback_service.FeedbackService.cleanup", return_value={"deleted": 1}) as feedback,
            patch("outcome_reviews.OutcomeReviewService.cleanup_derived_data", return_value={"deleted": 2}) as outcome,
        ):
            status, _failed, _response_headers = self.request(
                "POST", "/api/effect-data/cleanup", body, headers=headers,
            )
            self.assertEqual(status, 500)
            status, completed, _response_headers = self.request(
                "POST", "/api/effect-data/cleanup", body, headers=headers,
            )
        self.assertEqual(status, 200)
        self.assertEqual((feedback.call_count, outcome.call_count, collector.cleanup_calls), (1, 1, 2))
        self.assertEqual(
            (completed["feedback"]["deleted"], completed["derived"]["deleted"],
             completed["prospective"]["deleted"]),
            (1, 2, 3),
        )

    def test_http_review_vertical_flow(self):
        skill_dir = self.skills / "http-review"
        skill_dir.mkdir()
        skill_text = "# http-review\n"
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(skill_text, encoding="utf-8")
        skill_sha = hashlib.sha256(skill_text.encode()).hexdigest()
        session = self.logs / "review.jsonl"
        events = [
            {"type": "session", "id": "http-session", "timestamp": "2026-08-24T00:00:00Z"},
            {"type": "message", "id": "user", "timestamp": "2026-08-24T00:00:01Z", "message": {
                "role": "user", "content": [{"type": "text", "text": "write report.md"}],
            }},
            {"type": "message", "id": "load", "timestamp": "2026-08-24T00:00:02Z", "message": {
                "role": "user", "content": [{"type": "text", "text":
                    f'<skill name="http-review" location="{skill_file}">{skill_text}</skill>'}],
            }},
        ]
        session.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        cookie, csrf = self.login()
        auth_headers = {"Cookie": cookie, "X-CSRF-Token": csrf}

        status, _scan, _headers = self.request(
            "POST", "/api/effect-scan",
            {"budgetBytes": 100000, "budgetSeconds": 2},
            headers=auth_headers,
        )
        self.assertEqual(status, 200)
        status, uses, _headers = self.request(
            "GET", "/api/skill-use-events?skill=http-review", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        invocation = uses["items"][0]
        self.assertEqual(invocation["skill_sha256"], skill_sha)

        contract_body = {
            "applicability": {}, "requirements": [], "artifacts": [],
            "governance": {"singleOperatorApproved": True},
        }
        status, draft, _headers = self.request(
            "POST", "/api/skills/http-review/outcome-contracts",
            {"skillSha256": skill_sha, "contract": contract_body}, headers=auth_headers,
        )
        self.assertEqual(status, 201)
        status, active, _headers = self.request(
            "POST", f"/api/outcome-contracts/{draft['id']}/publish", {}, headers=auth_headers,
        )
        self.assertEqual((status, active["status"]), (200, "active"))

        case_id = invocation["task_case_id"]
        report = self.root / "report.md"
        report.write_text("verified report", encoding="utf-8")
        status, manifest, _headers = self.request(
            "POST", "/api/artifact-manifests",
            {"root": str(self.root), "globs": ["report.md"], "phase": "after-artifacts",
             "taskCaseId": case_id, "observationGroupId": "http-check"},
            headers=auth_headers,
        )
        self.assertEqual(status, 201)
        status, check, _headers = self.request(
            "POST", "/api/outcome-checks/run",
            {"authorized": True, "taskCaseId": case_id, "checkerId": "document-artifact",
             "manifestId": manifest["id"], "workspace": str(self.root),
             "options": {"path": "report.md"}},
            headers=auth_headers,
        )
        self.assertEqual((status, check["freshness"]), (200, "current"))
        self.assertEqual(check["result"]["expected_manifest_id"], manifest["id"])
        status, assessment, _headers = self.request(
            "POST", f"/api/task-cases/{case_id}/review",
            {"skillInvocationId": invocation["id"]}, headers=auth_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(assessment["contract_version_id"], active["id"])
        review = assessment["review_task"]
        self.assertIsNotNone(review)
        status, claimed, _headers = self.request(
            "POST", f"/api/review-tasks/{review['id']}/claim", {}, headers=auth_headers,
        )
        self.assertEqual((status, claimed["claimed_by_actor_id"]), (200, self.auth.actor.uuid))
        status, decision, _headers = self.request(
            "PUT", f"/api/review-tasks/{review['id']}/disposition",
            {"expectedRevision": 0, "disposition": "needs-evidence", "reasonCode": "http-verified"},
            headers=auth_headers,
        )
        self.assertEqual((status, decision["revision"]), (200, 1))
        status, detail, _headers = self.request(
            "GET", f"/api/task-cases/{case_id}", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["decisions"][0]["reason_code"], "http-verified")
        self.assertEqual(detail["current_outcome"]["effectiveVerdict"], "needs-evidence")

        status, snapshot, _headers = self.request(
            "POST", "/api/effect-metric-snapshots",
            {"coverageStatus": "forged", "versions": {"parserVersion": "forged"}, "scanRunId": "forged"},
            headers=auth_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["coverage_status"], "complete")
        self.assertNotEqual(snapshot["versions"]["parserVersion"], "forged")
        self.assertEqual(snapshot["scan_run_id"], _scan["id"])

        status, next_manifest, _headers = self.request(
            "POST", "/api/artifact-manifests",
            {"root": str(self.root), "globs": ["report.md"], "phase": "after-artifacts",
             "taskCaseId": case_id, "observationGroupId": "http-check-2"},
            headers=auth_headers,
        )
        self.assertEqual(status, 201)
        status, _new_check, _headers = self.request(
            "POST", "/api/outcome-checks/run",
            {"authorized": True, "taskCaseId": case_id, "checkerId": "document-artifact",
             "manifestId": next_manifest["id"], "workspace": str(self.root),
             "options": {"path": "report.md"}},
            headers=auth_headers,
        )
        self.assertEqual(status, 200)
        status, invalidated, _headers = self.request(
            "GET", f"/api/task-cases/{case_id}", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(invalidated["current_outcome"])
        self.assertEqual(invalidated["assessments"][-1]["process_state"], "invalidated")

    def test_http_negative_feedback_vertical_flow(self):
        session = self.logs / "negative-feedback.jsonl"
        events = [
            {"type": "session", "id": "feedback-session", "timestamp": "2026-08-24T00:00:00Z"},
            {"type": "message", "id": "goal", "timestamp": "2026-08-24T00:00:01Z",
             "message": {"role": "user", "content": "实现权限校验"}},
            {"type": "message", "id": "answer", "timestamp": "2026-08-24T00:00:02Z",
             "message": {"role": "assistant", "content": "已经完成"}},
            {"type": "message", "id": "feedback", "timestamp": "2026-08-24T00:00:03Z",
             "message": {"role": "user", "content": "还是不行，权限校验漏了。"}},
        ]
        session.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")
        cookie, csrf = self.login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
        status, scan, _response_headers = self.request(
            "POST", "/api/effect-scan", {"budgetBytes": 100000, "budgetSeconds": 2},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(scan["feedback"]["newSignals"], 1)
        status, listing, _response_headers = self.request(
            "GET", "/api/feedback-signals?channel=user-feedback&processState=queued&limit=20",
            headers={"Cookie": cookie},
        )
        self.assertEqual((status, len(listing["items"])), (200, 1))
        signal = listing["items"][0]
        status, rejected_decision, _response_headers = self.request(
            "PUT", f"/api/review-tasks/{signal['review_task_id']}/decision",
            {"expectedRevision": 0, "verdict": "fail", "reasonCode": "wrong-api"},
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertIn("feedback Action API", rejected_decision["error"])
        status, rejected_claim, _response_headers = self.request(
            "POST", f"/api/review-tasks/{signal['review_task_id']}/claim", {},
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertIn("feedback Action API", rejected_claim["error"])
        status, detail, _response_headers = self.request(
            "GET", f"/api/feedback-signals/{signal['id']}", headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        target = next(item for item in detail["targets"] if item["machine_status"] == "candidate")
        class FakeFeedbackModel:
            model_id = "fake-feedback"
            model_version = "fake-v1"

            def review(self, request, output_schema):
                lock_available = []
                def probe_lock():
                    acquired = app.OUTCOME_WRITE_LOCK.acquire(blocking=False)
                    lock_available.append(acquired)
                    if acquired:
                        app.OUTCOME_WRITE_LOCK.release()
                probe = threading.Thread(target=probe_lock)
                probe.start()
                probe.join(timeout=1)
                if lock_available != [True]:
                    raise AssertionError("semantic model executed while outcome write lock was held")
                payload = request["payload"]
                return {
                    "schema_version": "1.0", "category": "requirement-gap",
                    "severity": "high", "confidence": 0.95,
                    "span_ids": [payload["evidence_spans"][0]["id"]],
                    "target_id": payload["target_ids"][0], "rationale": "missing requirement",
                    "prompt_injection_detected": False,
                }

        with patch.object(app, "CodexFeedbackModel", return_value=FakeFeedbackModel()):
            status, semantic, _response_headers = self.request(
                "POST", f"/api/feedback-signals/{signal['id']}/semantic-classify", {},
                headers=headers,
            )
        self.assertEqual((status, semantic["verdict"]), (200, "needs-human"))
        self.assertEqual(semantic["reason"], "calibration-profile-missing")
        status, claimed, _response_headers = self.request(
            "POST", f"/api/feedback-signals/{signal['id']}/claim",
            {"expectedRevision": detail["current_action_revision"]}, headers=headers,
        )
        self.assertEqual((status, claimed["action"]), (200, "claimed"))
        status, confirmed, _response_headers = self.request(
            "POST", f"/api/feedback-signals/{signal['id']}/actions",
            {"expectedRevision": claimed["revision"], "action": "confirm",
             "reasonCode": "user-confirmed", "targetId": target["id"]}, headers=headers,
        )
        self.assertEqual((status, confirmed["action"]), (200, "confirm"))
        status, clusters, _response_headers = self.request(
            "GET", "/api/feedback-clusters", headers={"Cookie": cookie},
        )
        self.assertEqual((status, len(clusters["items"])), (200, 1))
        status, profile, _response_headers = self.request(
            "POST", "/api/feedback-calibration-profiles", {}, headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(profile["items"])
        self.assertTrue(all(not item["eligible"] for item in profile["items"]))
        status, snapshot, _response_headers = self.request(
            "POST", "/api/feedback-metric-snapshots", {}, headers=headers,
        )
        self.assertEqual((status, snapshot["sealed"]), (200, 1))
        self.assertEqual(snapshot["dimensions"]["metricKind"], "session-negative-feedback")
        status, feedback_snapshots, _response_headers = self.request(
            "GET", "/api/feedback-metric-snapshots", headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(feedback_snapshots["items"][0]["id"], snapshot["id"])
        self.assertIn("item_count", feedback_snapshots["items"][0])
        self.assertNotIn("items", feedback_snapshots["items"][0])
        status, outcome_metrics, _response_headers = self.request(
            "GET", "/api/effect-metrics", headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(snapshot["id"], [item["id"] for item in outcome_metrics["snapshots"]])
        target_case = target["context_task_case_id"]
        status, case_detail, _response_headers = self.request(
            "GET", f"/api/task-cases/{target_case}", headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(case_detail["feedback"][0]["id"], signal["id"])


if __name__ == "__main__":
    unittest.main()