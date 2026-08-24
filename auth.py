"""Framework-independent authentication primitives for the local operator service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_ROLES = ("admin", "contract-owner", "reviewer")
DEFAULT_COOKIE_NAME = "codex_skills_session"
REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION FAILED]"
MAX_REDACTION_INPUT = 1_000_000
MAX_CREDENTIAL_FILE_BYTES = 16_384
SAFE_HTTP_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


class AuthError(Exception):
    """Base class for authentication and authorization failures."""


class AuthenticationError(AuthError):
    """The supplied credential is absent, invalid, or expired."""


class CsrfError(AuthError):
    """A cookie-authenticated request failed CSRF validation."""


class AuthorizationError(AuthError):
    """The actor does not have every required role."""


class AuthConfigurationError(AuthError):
    """Persisted authentication state is malformed or unsafe."""


@dataclass(frozen=True)
class Actor:
    uuid: str
    operator_name: str
    roles: tuple[str, ...]

    @property
    def operatorName(self) -> str:  # noqa: N802 - mirrors the persisted/API field.
        return self.operator_name

    def as_dict(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "operatorName": self.operator_name,
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class LoginResult:
    actor: Actor
    cookie_value: str
    csrf_token: str
    expires_at: float
    set_cookie: str

    @property
    def session_token(self) -> str:
        return self.cookie_value


@dataclass(frozen=True)
class AuthContext:
    actor: Actor
    credential: str
    session_expires_at: float | None = None


@dataclass(frozen=True)
class _Session:
    csrf_token: str
    expires_at: float


def secure_file_permissions(path: str | os.PathLike[str]) -> None:
    """Set a regular file to mode 0600 without following symbolic links."""

    file_path = os.fspath(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(file_path, flags)
    except OSError as exc:
        raise AuthConfigurationError(f"cannot securely open credential file: {file_path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AuthConfigurationError(f"credential path is not a regular file: {file_path}")
        if file_stat.st_nlink != 1:
            raise AuthConfigurationError(f"credential file must not have hard links: {file_path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise AuthConfigurationError(f"credential file is owned by another user: {file_path}")
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise AuthConfigurationError(f"credential file does not have mode 0600: {file_path}")
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, content: str) -> bool:
    """Create and fsync a mode-0600 file, returning False if it already exists."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        payload = content.encode("utf-8")
        position = 0
        while position < len(payload):
            position += os.write(descriptor, payload[position:])
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise AuthConfigurationError(f"credential file does not have mode 0600: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _read_secure_text(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthConfigurationError(f"cannot read credential file: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise AuthConfigurationError(f"credential file is unsafe: {path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise AuthConfigurationError(f"credential file is owned by another user: {path}")
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise AuthConfigurationError(f"credential file does not have mode 0600: {path}")
        if file_stat.st_size > MAX_CREDENTIAL_FILE_BYTES:
            raise AuthConfigurationError(f"credential file is too large: {path}")
        payload = os.read(descriptor, MAX_CREDENTIAL_FILE_BYTES + 1)
        if len(payload) > MAX_CREDENTIAL_FILE_BYTES:
            raise AuthConfigurationError(f"credential file is too large: {path}")
        return payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuthConfigurationError(f"credential file is unreadable: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class AuthService:
    """Single-operator access-token and in-memory session service.

    ``initialize`` is idempotent and must be called before authentication. The
    token is persisted, while browser sessions intentionally disappear when the
    process exits.
    """

    def __init__(
        self,
        token_file: str | os.PathLike[str],
        actor_file: str | os.PathLike[str],
        *,
        operator_name: str = "local-operator",
        session_ttl_seconds: int = 8 * 60 * 60,
        cookie_name: str = DEFAULT_COOKIE_NAME,
        secure_cookie: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not operator_name.strip():
            raise ValueError("operator_name must not be empty")
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", cookie_name):
            raise ValueError("cookie_name is invalid")
        self.token_file = Path(token_file)
        self.actor_file = Path(actor_file)
        self.operator_name = operator_name.strip()
        self.session_ttl_seconds = session_ttl_seconds
        self.cookie_name = cookie_name
        self.secure_cookie = secure_cookie
        self._clock = clock
        self._token: str | None = None
        self._actor: Actor | None = None
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    @property
    def actor(self) -> Actor:
        self._require_initialized()
        assert self._actor is not None
        return self._actor

    def initialize(self) -> Actor:
        """Create or load the stable access token and local actor."""

        with self._lock:
            self._prepare_parent(self.token_file)
            self._prepare_parent(self.actor_file)
            generated_token = secrets.token_urlsafe(32)
            _write_exclusive(self.token_file, generated_token + "\n")
            token = _read_secure_text(self.token_file).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise AuthConfigurationError("access token is not a 256-bit URL-safe token")

            actor_payload = {
                "uuid": str(uuid_module.uuid4()),
                "operatorName": self.operator_name,
                "roles": list(DEFAULT_ROLES),
            }
            _write_exclusive(
                self.actor_file,
                json.dumps(actor_payload, ensure_ascii=True, indent=2) + "\n",
            )
            actor = self._load_actor(_read_secure_text(self.actor_file))
            if (self._token is not None and self._token != token) or (
                self._actor is not None and self._actor != actor
            ):
                self._sessions.clear()
            self._token = token
            self._actor = actor
            return actor

    def login(self, access_token: str) -> LoginResult:
        """Exchange the long-lived access token for a memory-only session."""

        self._verify_access_token(access_token)
        now = self._clock()
        cookie_value = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + self.session_ttl_seconds
        with self._lock:
            self._purge_expired(now)
            self._sessions[cookie_value] = _Session(csrf_token, expires_at)
        return LoginResult(
            actor=self.actor,
            cookie_value=cookie_value,
            csrf_token=csrf_token,
            expires_at=expires_at,
            set_cookie=self.build_session_cookie(cookie_value),
        )

    def authenticate_bearer(self, authorization: str | None) -> AuthContext:
        """Authenticate an HTTP Authorization header."""

        if not isinstance(authorization, str):
            raise AuthenticationError("Bearer authorization is required")
        parts = authorization.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthenticationError("Bearer authorization is malformed")
        self._verify_access_token(parts[1])
        return AuthContext(self.actor, "bearer")

    def authenticate_session(
        self,
        cookie_value: str | None,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = True,
    ) -> AuthContext:
        """Authenticate a raw session cookie value and optionally verify CSRF."""

        if not isinstance(cookie_value, str) or not cookie_value:
            raise AuthenticationError("session cookie is required")
        now = self._clock()
        with self._lock:
            session = self._sessions.get(cookie_value)
            if session is None:
                raise AuthenticationError("session is invalid")
            if session.expires_at <= now:
                self._sessions.pop(cookie_value, None)
                raise AuthenticationError("session has expired")
            if require_csrf and (
                not isinstance(csrf_token, str)
                or not hmac.compare_digest(session.csrf_token, csrf_token)
            ):
                raise CsrfError("CSRF token is invalid")
        return AuthContext(self.actor, "session", session.expires_at)

    def authenticate_request(
        self,
        *,
        authorization: str | None = None,
        cookie: str | None = None,
        cookie_header: str | None = None,
        csrf_token: str | None = None,
        method: str | None = None,
        require_csrf: bool | None = None,
        required_roles: Iterable[str] = (),
    ) -> AuthContext:
        """Authenticate a request, preferring Bearer credentials over cookies.

        Bearer authentication is not vulnerable to browser CSRF and therefore
        deliberately bypasses the CSRF requirement.
        """

        if authorization:
            context = self.authenticate_bearer(authorization)
        else:
            if require_csrf is None:
                require_csrf = not isinstance(method, str) or method.upper() not in SAFE_HTTP_METHODS
            value = cookie if cookie is not None else self.cookie_value(cookie_header)
            context = self.authenticate_session(
                value,
                csrf_token=csrf_token,
                require_csrf=require_csrf,
            )
        self.require_roles(context.actor, required_roles)
        return context

    authenticate = authenticate_request

    def logout(self, cookie_value: str | None) -> None:
        if isinstance(cookie_value, str):
            with self._lock:
                self._sessions.pop(cookie_value, None)

    def revoke_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()

    def purge_expired_sessions(self) -> int:
        with self._lock:
            return self._purge_expired(self._clock())

    def require_roles(self, actor: Actor, roles: Iterable[str]) -> None:
        required = frozenset(roles)
        missing = required.difference(actor.roles)
        if missing:
            raise AuthorizationError(f"actor lacks required roles: {', '.join(sorted(missing))}")

    def build_session_cookie(self, cookie_value: str, *, clear: bool = False) -> str:
        """Build a Set-Cookie value with strict browser-side protections."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie_value):
            raise ValueError("cookie_value is invalid")
        max_age = 0 if clear else self.session_ttl_seconds
        parts = [
            f"{self.cookie_name}={cookie_value}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_session_cookie(self) -> str:
        return self.build_session_cookie("A" * 43, clear=True)

    def cookie_value(self, cookie_header: str | None) -> str | None:
        if not isinstance(cookie_header, str) or not cookie_header:
            return None
        parsed = SimpleCookie()
        try:
            parsed.load(cookie_header)
        except CookieError:
            return None
        morsel = parsed.get(self.cookie_name)
        return morsel.value if morsel is not None else None

    def token_fingerprint(self) -> str:
        """Return a non-secret identifier useful for diagnostics."""

        self._require_initialized()
        assert self._token is not None
        return hashlib.sha256(self._token.encode("ascii")).hexdigest()[:12]

    def _verify_access_token(self, candidate: str) -> None:
        self._require_initialized()
        assert self._token is not None
        if not isinstance(candidate, str) or not hmac.compare_digest(self._token, candidate):
            raise AuthenticationError("access token is invalid")

    def _require_initialized(self) -> None:
        if self._token is None or self._actor is None:
            raise AuthConfigurationError("authentication service is not initialized")

    def _purge_expired(self, now: float) -> int:
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
        return len(expired)

    @staticmethod
    def _prepare_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_stat = path.parent.stat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise AuthConfigurationError(f"credential parent is not a directory: {path.parent}")
        if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
            raise AuthConfigurationError(f"credential parent is owned by another user: {path.parent}")
        if os.name == "posix" and parent_stat.st_mode & 0o022:
            raise AuthConfigurationError(f"credential parent is group/world writable: {path.parent}")

    @staticmethod
    def _load_actor(raw: str) -> Actor:
        try:
            payload = json.loads(raw)
            actor_uuid = str(payload["uuid"])
            uuid_module.UUID(actor_uuid)
            operator_name = payload["operatorName"]
            roles = payload["roles"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthConfigurationError("actor configuration is malformed") from exc
        if not isinstance(operator_name, str) or not operator_name.strip():
            raise AuthConfigurationError("actor operatorName is invalid")
        if (
            not isinstance(roles, list)
            or any(not isinstance(role, str) or not role for role in roles)
            or not set(DEFAULT_ROLES).issubset(roles)
        ):
            raise AuthConfigurationError("actor roles are invalid")
        return Actor(actor_uuid, operator_name.strip(), tuple(dict.fromkeys(roles)))


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_AUTHORIZATION_RE = re.compile(
    r"(?im)(\bauthorization\s*:\s*(?:bearer|basic|token|apikey)\s+)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/-]+)")
_LABELED_SECRET_RE = re.compile(
    r'''(?ix)
    (["']?\b(?:token|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|
       private[_-]?key|client[_-]?secret|secret(?:[_-]?key)?|password|passwd)\b["']?
       \s*(?:[:=]\s*|\s+))
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^,;\r\n&}]+)
    ''',
)
_URL_PASSWORD_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|"
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r")(?![A-Za-z0-9_])"
)
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def redact_sensitive(value: object) -> str:
    """Redact common credentials, returning no source text when safety is uncertain.

    Unsupported values, oversized input, invalid Unicode, and unexpected scanner
    failures all return a fixed fail-closed marker instead of the original data.
    """

    if not isinstance(value, str) or len(value) > MAX_REDACTION_INPUT:
        return REDACTION_FAILED
    try:
        value.encode("utf-8")
        redacted = _PRIVATE_KEY_RE.sub(REDACTED, value)
        redacted = _AUTHORIZATION_RE.sub(lambda match: match.group(1) + REDACTED, redacted)
        redacted = _BEARER_RE.sub(lambda match: match.group(1) + REDACTED, redacted)
        redacted = _LABELED_SECRET_RE.sub(lambda match: match.group(1) + REDACTED, redacted)
        redacted = _URL_PASSWORD_RE.sub(lambda match: match.group(1) + REDACTED + match.group(3), redacted)
        redacted = _KNOWN_TOKEN_RE.sub(REDACTED, redacted)

        return _OPAQUE_TOKEN_RE.sub(REDACTED, redacted)
    except Exception:
        return REDACTION_FAILED


redact_secrets = redact_sensitive
