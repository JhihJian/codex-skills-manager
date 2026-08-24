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


if __name__ == "__main__":
    unittest.main()