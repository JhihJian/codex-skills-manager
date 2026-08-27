import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
from auth import (
    AuthenticationError,
    AuthorizationError,
    AuthConfigurationError,
    AuthService,
    CsrfError,
    REDACTED,
    REDACTION_FAILED,
    redact_sensitive,
    secure_file_permissions,
)


class MutableClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.token_file = self.root / "access-token"
        self.actor_file = self.root / "actor.json"
        self.clock = MutableClock()
        self.service = AuthService(
            self.token_file,
            self.actor_file,
            operator_name="测试操作者",
            session_ttl_seconds=60,
            clock=self.clock,
        )
        self.actor = self.service.initialize()
        self.access_token = self.token_file.read_text(encoding="utf-8").strip()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initialization_creates_256_bit_token_and_stable_actor_with_mode_0600(self) -> None:
        decoded = base64.urlsafe_b64decode(self.access_token + "=")
        self.assertEqual(len(decoded), 32)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.actor_file.stat().st_mode), 0o600)
        self.assertEqual(
            set(self.actor.roles),
            {"admin", "contract-owner", "reviewer", "trial_user"},
        )
        self.assertEqual(self.actor.operator_name, "测试操作者")

        original_actor = json.loads(self.actor_file.read_text(encoding="utf-8"))
        second = AuthService(self.token_file, self.actor_file, operator_name="不会覆盖")
        second_actor = second.initialize()
        self.assertEqual(second_actor.uuid, original_actor["uuid"])
        self.assertEqual(second_actor.operator_name, "测试操作者")
        self.assertEqual(self.token_file.read_text(encoding="utf-8").strip(), self.access_token)

    def test_secure_file_permissions_repairs_existing_mode(self) -> None:
        os.chmod(self.token_file, 0o644)
        secure_file_permissions(self.token_file)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

        os.chmod(self.token_file, 0o644)
        os.chmod(self.actor_file, 0o644)
        AuthService(self.token_file, self.actor_file).initialize()
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.actor_file.stat().st_mode), 0o600)

    def test_secure_file_permissions_rejects_symbolic_links(self) -> None:
        link = self.root / "token-link"
        try:
            link.symlink_to(self.token_file)
        except (NotImplementedError, OSError):
            self.skipTest("symbolic links are unavailable")
        with self.assertRaises(AuthConfigurationError):
            secure_file_permissions(link)

    def test_secure_file_permissions_rejects_hard_links(self) -> None:
        link = self.root / "token-hard-link"
        try:
            os.link(self.token_file, link)
        except OSError:
            self.skipTest("hard links are unavailable")
        with self.assertRaises(AuthConfigurationError):
            secure_file_permissions(self.token_file)

    def test_login_rejects_bad_token_and_uses_constant_time_comparison(self) -> None:
        with patch("auth.hmac.compare_digest", wraps=auth.hmac.compare_digest) as compare:
            with self.assertRaises(AuthenticationError):
                self.service.login("wrong-token")
        compare.assert_called_once_with(self.access_token, "wrong-token")

    def test_login_returns_strict_httponly_cookie_and_memory_session(self) -> None:
        login = self.service.login(self.access_token)
        self.assertEqual(login.actor, self.actor)
        self.assertIn(f"{self.service.cookie_name}={login.cookie_value}", login.set_cookie)
        self.assertIn("HttpOnly", login.set_cookie)
        self.assertIn("SameSite=Strict", login.set_cookie)
        self.assertIn("Path=/", login.set_cookie)
        context = self.service.authenticate_session(login.cookie_value, require_csrf=False)
        self.assertEqual(context.actor.uuid, self.actor.uuid)
        self.assertEqual(context.credential, "session")

    def test_cookie_header_session_requires_valid_csrf_for_writes(self) -> None:
        login = self.service.login(self.access_token)
        header = f"theme=light; {self.service.cookie_name}={login.cookie_value}"
        with self.assertRaises(CsrfError):
            self.service.authenticate_request(cookie_header=header, require_csrf=True)
        with self.assertRaises(CsrfError):
            self.service.authenticate_request(
                cookie_header=header,
                csrf_token="incorrect",
                require_csrf=True,
            )
        context = self.service.authenticate_request(
            cookie_header=header,
            csrf_token=login.csrf_token,
            require_csrf=True,
        )
        self.assertEqual(context.credential, "session")

    def test_cookie_request_derives_csrf_requirement_from_http_method_and_fails_closed(self) -> None:
        login = self.service.login(self.access_token)
        with self.assertRaises(CsrfError):
            self.service.authenticate_request(cookie=login.cookie_value)
        with self.assertRaises(CsrfError):
            self.service.authenticate_request(cookie=login.cookie_value, method="POST")
        context = self.service.authenticate_request(cookie=login.cookie_value, method="GET")
        self.assertEqual(context.credential, "session")

    def test_bearer_authentication_bypasses_csrf(self) -> None:
        context = self.service.authenticate_request(
            authorization=f"Bearer {self.access_token}",
            csrf_token="incorrect",
            require_csrf=True,
        )
        self.assertEqual(context.credential, "bearer")
        self.assertEqual(context.actor, self.actor)

    def test_session_expires_and_is_removed(self) -> None:
        login = self.service.login(self.access_token)
        self.clock.now = login.expires_at
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_session(login.cookie_value, require_csrf=False)
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_session(login.cookie_value, require_csrf=False)

    def test_required_roles_require_every_role(self) -> None:
        context = self.service.authenticate_request(
            authorization=f"Bearer {self.access_token}",
            required_roles=("reviewer", "admin"),
        )
        self.assertEqual(context.actor, self.actor)
        with self.assertRaises(AuthorizationError):
            self.service.authenticate_request(
                authorization=f"Bearer {self.access_token}",
                required_roles=("auditor",),
            )


class RedactionTests(unittest.TestCase):
    def test_redacts_common_tokens_keys_and_url_passwords(self) -> None:
        text = "\n".join(
            (
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
                "Authorization: Token abc123",
                "credential=Bearer short-but-sensitive-token",
                'api_key="sk-abcdefghijklmnopqrstuvwxyz012345"',
                'password="correct horse battery staple"',
                "github=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                "raw=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "database=https://operator:very-secret-password@example.test/db",
                "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature012345",
                "-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----",
            )
        )
        result = redact_sensitive(text)
        self.assertGreaterEqual(result.count(REDACTED), 10)
        for secret in (
            "abcdefghijklmnopqrstuvwxyz0123456789",
            "abc123",
            "correct horse battery staple",
            "very-secret-password",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "secret material",
            "signature012345",
        ):
            self.assertNotIn(secret, result)

    def test_redacts_unlabeled_high_entropy_token(self) -> None:
        secret = "AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789"
        self.assertEqual(redact_sensitive(secret), REDACTED)
        standard_base64 = "q7+/N2/aZ9+vL4/Tx1+Qm8/rK3+Wp6/Y"
        self.assertEqual(redact_sensitive(standard_base64), REDACTED)
        padded_base64 = "q7+/N2/aZ9+vL4/Tx1+Qm8/rK3+WpQ=="
        self.assertEqual(redact_sensitive(padded_base64), REDACTED)

    def test_redaction_fails_closed_for_unsupported_or_unbounded_input(self) -> None:
        self.assertEqual(redact_sensitive(None), REDACTION_FAILED)
        self.assertEqual(
            redact_sensitive("x" * (auth.MAX_REDACTION_INPUT + 1)),
            REDACTION_FAILED,
        )
        self.assertEqual(redact_sensitive("unsafe\ud800text"), REDACTION_FAILED)

    def test_redaction_fails_closed_on_unexpected_scanner_error(self) -> None:
        with patch.object(auth, "_PRIVATE_KEY_RE") as scanner:
            scanner.sub.side_effect = RuntimeError("scanner failed")
            self.assertEqual(redact_sensitive("token=secret"), REDACTION_FAILED)


if __name__ == "__main__":
    unittest.main()