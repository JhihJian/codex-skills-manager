"""Trusted prospective event, artifact, and checker collection protocol."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import math
import os
import platform
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from auth import REDACTED, REDACTION_FAILED, redact_sensitive
from effect_store import EffectStore
from outcome_checkers import BubblewrapCheckerRunner, CheckerCommandRejected


COLLECTOR_VERSION = "1.0.0"
EVENT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_PHASES = frozenset({"before-invocation", "after-artifacts", "after-check"})

_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "source",
        "event_type",
        "occurred_at",
        "payload",
        "task_case_id",
        "project_id",
        "skill_id",
    }
)
_EVENT_REQUIRED_KEYS = frozenset(
    {"schema_version", "event_id", "source", "event_type", "occurred_at", "payload"}
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "setcookie",
        "apikey",
        "accesskey",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "password",
        "passwd",
        "privatekey",
        "clientsecret",
        "secret",
        "secretkey",
        "credential",
        "credentials",
    }
)


class CollectorError(RuntimeError):
    """Base error for prospective collection."""


class CollectorValidationError(CollectorError, ValueError):
    """The input does not satisfy the collector protocol."""


class ArtifactCollectionRejected(CollectorError, ValueError):
    """An artifact selector or filesystem entry is unsafe."""


class CheckerExecutionRejected(CollectorError, ValueError):
    """A checker request was not explicitly authorized or scoped."""


@dataclass(frozen=True)
class ArtifactSelector:
    """A regular-file selection rooted in an explicitly allowed directory."""

    root: str | Path
    globs: tuple[str, ...] = ("**/*",)
    project_id: str | None = None
    skill_id: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value or len(value) > 64 or not _RFC3339_RE.fullmatch(value):
        raise CollectorValidationError(f"{field} must be a non-empty RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CollectorValidationError(f"{field} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollectorValidationError(f"{field} must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    normalized = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed, normalized


def _validate_identifier(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or not _IDENTIFIER_RE.fullmatch(value):
        raise CollectorValidationError(f"{field} is invalid")
    return value


def _validate_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > 10_000:
        raise CollectorValidationError("payload contains too many values")
    if depth > 20:
        raise CollectorValidationError("payload nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CollectorValidationError("payload contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CollectorValidationError("payload object keys must be strings")
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    raise CollectorValidationError(f"payload contains unsupported type: {type(value).__name__}")


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return REDACTION_FAILED
            safe_key = redact_sensitive(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            key_is_sensitive = normalized_key in _SENSITIVE_KEY_NAMES or normalized_key.endswith("token")
            redacted[safe_key] = REDACTED if key_is_sensitive else _redact_json(item)
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return REDACTION_FAILED


def _path_has_symlink(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _matches(relative: str, patterns: Sequence[str]) -> bool:
    path = PurePosixPath(relative)
    for pattern in patterns:
        if path.match(pattern) or (pattern.startswith("**/") and path.match(pattern[3:])):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(path.name, pattern):
            return True
    return False


class ProspectiveCollector:
    """Validate and persist trusted prospective observations."""

    def __init__(
        self,
        store: EffectStore,
        *,
        allowed_sources: Sequence[str],
        allowed_roots: Sequence[str | Path] = (),
        allowed_artifact_roots: Sequence[str | Path] | None = None,
        checker_runner: BubblewrapCheckerRunner | None = None,
        checker_allowlist: Sequence[str] = (),
        allowed_workspace_roots: Sequence[str | Path] | None = None,
        collector_version: str = COLLECTOR_VERSION,
        max_event_bytes: int = 64 * 1024,
        max_artifact_bytes: int = 64 * 1024 * 1024,
        max_manifest_bytes: int = 256 * 1024 * 1024,
        max_artifacts: int = 10_000,
        max_walk_entries: int = 100_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.collector_version = _validate_identifier(collector_version, "collector_version", maximum=128)
        if isinstance(allowed_sources, (str, bytes)):
            raise CollectorValidationError("allowed_sources must be a sequence of source identifiers")
        self.allowed_sources = frozenset(
            _validate_identifier(source, "allowed_sources", maximum=64) for source in allowed_sources
        )
        if not self.allowed_sources:
            raise CollectorValidationError("allowed_sources must not be empty")

        if allowed_artifact_roots is not None and allowed_roots:
            raise CollectorValidationError("configure only one artifact root option")
        artifact_roots = allowed_artifact_roots if allowed_artifact_roots is not None else allowed_roots
        self.allowed_roots = self._normalize_configured_roots(artifact_roots)
        self._allowed_root_identities: dict[Path, tuple[int, int]] = {}
        for root in self.allowed_roots:
            root_metadata = root.stat()
            self._allowed_root_identities[root] = (root_metadata.st_dev, root_metadata.st_ino)
        workspace_roots = artifact_roots if allowed_workspace_roots is None else allowed_workspace_roots
        self.allowed_workspace_roots = self._normalize_configured_roots(workspace_roots)
        if checker_runner is not None and type(checker_runner) is not BubblewrapCheckerRunner:
            raise CollectorValidationError("checker_runner must be a BubblewrapCheckerRunner")
        self.checker_runner = checker_runner
        if isinstance(checker_allowlist, (str, bytes)):
            raise CollectorValidationError("checker_allowlist must be a sequence of checker identifiers")
        self.checker_allowlist = frozenset(
            _validate_identifier(checker, "checker_allowlist", maximum=128) for checker in checker_allowlist
        )
        self.max_event_bytes = self._positive_limit(max_event_bytes, "max_event_bytes")
        self.max_artifact_bytes = self._positive_limit(max_artifact_bytes, "max_artifact_bytes")
        self.max_manifest_bytes = self._positive_limit(max_manifest_bytes, "max_manifest_bytes")
        self.max_artifacts = self._positive_limit(max_artifacts, "max_artifacts")
        self.max_walk_entries = self._positive_limit(max_walk_entries, "max_walk_entries")
        self.environment_fingerprint = self._environment_fingerprint(environment)
        self._cleanup_manifest_projects: dict[str, str] = {}
        self._ensure_audit_table()

    @staticmethod
    def _positive_limit(value: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CollectorValidationError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _normalize_configured_roots(roots: Sequence[str | Path]) -> tuple[Path, ...]:
        if isinstance(roots, (str, bytes, Path)):
            raise CollectorValidationError("configured roots must be a sequence of paths")
        normalized: list[Path] = []
        for raw_root in roots:
            root = Path(raw_root).expanduser()
            if _path_has_symlink(root):
                raise ArtifactCollectionRejected(f"configured root contains a symbolic link: {root}")
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise ArtifactCollectionRejected(f"configured root is unavailable: {root}") from exc
            if not resolved.is_dir():
                raise ArtifactCollectionRejected(f"configured root is not a directory: {root}")
            if resolved not in normalized:
                normalized.append(resolved)
        return tuple(normalized)

    @staticmethod
    def _environment_fingerprint(environment: Mapping[str, str] | None) -> str:
        supplied = dict(environment or {})
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in supplied.items()):
            raise CollectorValidationError("environment fingerprint inputs must be strings")
        material = {
            "os": os.name,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "declared": {key: supplied[key] for key in sorted(supplied)},
        }
        return _sha256_json(material)

    def _ensure_audit_table(self) -> None:
        with self.store.transaction():
            self.store.execute(
                """
                CREATE TABLE IF NOT EXISTS prospective_cleanup_audits (
                    id TEXT PRIMARY KEY,
                    requested_at TEXT NOT NULL,
                    older_than TEXT,
                    project_id_hash TEXT,
                    skill_id_hash TEXT,
                    deleted_event_count INTEGER NOT NULL CHECK (deleted_event_count >= 0),
                    deleted_manifest_count INTEGER NOT NULL CHECK (deleted_manifest_count >= 0),
                    criteria_sha256 TEXT NOT NULL
                )
                """
            )

    def record_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise CollectorValidationError("event must be an object")
        keys = frozenset(event)
        if not all(isinstance(key, str) for key in keys):
            raise CollectorValidationError("event keys must be strings")
        missing = _EVENT_REQUIRED_KEYS - keys
        unknown = keys - _EVENT_KEYS
        if missing or unknown:
            raise CollectorValidationError(
                f"event schema mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if type(event["schema_version"]) is not int or event["schema_version"] != EVENT_SCHEMA_VERSION:
            raise CollectorValidationError("unsupported event schema_version")
        event_id = _validate_identifier(event["event_id"], "event_id")
        source = _validate_identifier(event["source"], "source", maximum=64)
        if source not in self.allowed_sources:
            raise CollectorValidationError("event source is not trusted")
        event_type = event["event_type"]
        if not isinstance(event_type, str) or len(event_type) > 96 or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise CollectorValidationError("event_type is invalid")
        _occurred, occurred_at = _parse_timestamp(event["occurred_at"], "occurred_at")
        payload = event["payload"]
        _validate_json(payload)
        for optional in ("task_case_id", "project_id", "skill_id"):
            if optional in event and event[optional] is not None:
                _validate_identifier(event[optional], optional)
        canonical_input = dict(event)
        canonical_input["occurred_at"] = occurred_at
        try:
            encoded = _canonical_json(canonical_input).encode("utf-8")
        except (UnicodeEncodeError, ValueError) as exc:
            raise CollectorValidationError("event is not valid UTF-8 JSON") from exc
        if len(encoded) > self.max_event_bytes:
            raise CollectorValidationError("event exceeds the byte limit")

        payload_hash = _sha256_json(payload)
        fingerprint = _sha256_json(
            {
                "schemaVersion": EVENT_SCHEMA_VERSION,
                "source": source,
                "eventId": event_id,
            }
        )
        stored_payload = {
            "schemaVersion": EVENT_SCHEMA_VERSION,
            "eventId": event_id,
            "context": {
                "projectId": event.get("project_id"),
                "skillId": event.get("skill_id"),
            },
            "data": _redact_json(payload),
        }
        task_case_id = event.get("task_case_id")
        created_at = _utc_now()
        row_id = str(uuid.uuid4())
        with self.store.transaction():
            existing = self.store.execute(
                "SELECT * FROM prospective_events WHERE event_fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"])
                existing_context = existing_payload.get("context", {})
                if (
                    existing["source"] != source
                    or existing["event_type"] != event_type
                    or existing["occurred_at"] != occurred_at
                    or existing["payload_hash"] != payload_hash
                    or existing["task_case_id"] != task_case_id
                    or existing_context.get("projectId") != event.get("project_id")
                    or existing_context.get("skillId") != event.get("skill_id")
                ):
                    raise CollectorValidationError("event_id was reused with different content")
                return self._event_row(existing)
            self.store.execute(
                """
                INSERT INTO prospective_events(
                    id, event_fingerprint, task_case_id, source, event_type, occurred_at,
                    payload_hash, payload_json, consumed_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    row_id,
                    fingerprint,
                    task_case_id,
                    source,
                    event_type,
                    occurred_at,
                    payload_hash,
                    _canonical_json(stored_payload),
                    created_at,
                ),
            )
            row = self.store.execute("SELECT * FROM prospective_events WHERE id = ?", (row_id,)).fetchone()
        return self._event_row(row)

    collect_event = record_event
    ingest_event = record_event

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    @staticmethod
    def _validate_globs(globs: Sequence[str]) -> tuple[str, ...]:
        if isinstance(globs, (str, bytes)) or not isinstance(globs, Sequence) or not globs:
            raise ArtifactCollectionRejected("selector globs must be a non-empty sequence")
        normalized: list[str] = []
        for pattern in globs:
            if (
                not isinstance(pattern, str)
                or not pattern
                or len(pattern) > 512
                or "\x00" in pattern
                or "\\" in pattern
                or pattern.startswith("/")
                or any(part == ".." for part in pattern.split("/"))
            ):
                raise ArtifactCollectionRejected("selector contains an unsafe glob")
            normalized.append(pattern)
        return tuple(dict.fromkeys(normalized))

    def _selector(self, selector: ArtifactSelector | Mapping[str, Any]) -> ArtifactSelector:
        if isinstance(selector, Mapping):
            allowed = {"root", "globs", "project_id", "skill_id"}
            if set(selector) - allowed or "root" not in selector or "globs" not in selector:
                raise ArtifactCollectionRejected("artifact selector schema is invalid")
            raw_globs = selector["globs"]
            if isinstance(raw_globs, (str, bytes)) or not isinstance(raw_globs, Sequence):
                raise ArtifactCollectionRejected("selector globs must be a non-empty sequence")
            selector = ArtifactSelector(
                root=selector["root"],
                globs=tuple(raw_globs),
                project_id=selector.get("project_id"),
                skill_id=selector.get("skill_id"),
            )
        if not isinstance(selector, ArtifactSelector):
            raise ArtifactCollectionRejected("selector must be an ArtifactSelector")
        if isinstance(selector.root, bytes) or not isinstance(selector.root, (str, os.PathLike)):
            raise ArtifactCollectionRejected("selector root must be a filesystem path")
        globs = self._validate_globs(selector.globs)
        for field, value in (("project_id", selector.project_id), ("skill_id", selector.skill_id)):
            if value is not None:
                _validate_identifier(value, field)
        return ArtifactSelector(selector.root, globs, selector.project_id, selector.skill_id)

    def _secure_root(self, raw_root: str | Path, allowed_roots: Sequence[Path]) -> Path:
        try:
            root = Path(raw_root).expanduser()
        except (TypeError, ValueError) as exc:
            raise ArtifactCollectionRejected("artifact root is invalid") from exc
        if _path_has_symlink(root):
            raise ArtifactCollectionRejected("artifact root contains a symbolic link")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ArtifactCollectionRejected("artifact root is unavailable") from exc
        if not resolved.is_dir():
            raise ArtifactCollectionRejected("artifact root is not a directory")
        if not allowed_roots or not _within(resolved, allowed_roots):
            raise ArtifactCollectionRejected("artifact root is outside the allowed roots")
        return resolved

    def _open_artifact_root(self, root: Path) -> int:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        candidates = sorted(
            (allowed for allowed in self.allowed_roots if root == allowed or allowed in root.parents),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        if not candidates:
            raise ArtifactCollectionRejected("artifact root is outside the allowed roots")
        allowed = candidates[0]
        current_fd: int | None = None
        try:
            current_fd = os.open(allowed, os.O_RDONLY | directory_flag | no_follow)
            metadata = os.fstat(current_fd)
            if (metadata.st_dev, metadata.st_ino) != self._allowed_root_identities[allowed]:
                raise ArtifactCollectionRejected("configured artifact root identity changed")
            for component in root.relative_to(allowed).parts:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | directory_flag | no_follow,
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except ArtifactCollectionRejected:
            if current_fd is not None:
                os.close(current_fd)
            raise
        except OSError as exc:
            if current_fd is not None:
                os.close(current_fd)
            raise ArtifactCollectionRejected("artifact root changed or contains a symbolic link") from exc

    def _collect_entries(self, root: Path, patterns: Sequence[str]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        total_bytes = 0
        visited_entries = 0
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        non_block = getattr(os, "O_NONBLOCK", 0)
        root_fd = self._open_artifact_root(root)

        def reject_walk_error(error: OSError) -> None:
            raise ArtifactCollectionRejected("artifact tree changed while being collected") from error

        try:
            for directory, directory_names, file_names, directory_fd in os.fwalk(
                ".", dir_fd=root_fd, follow_symlinks=False, onerror=reject_walk_error
            ):
                relative_directory = Path(directory).relative_to(".")
                for name in directory_names:
                    visited_entries += 1
                    if visited_entries > self.max_walk_entries:
                        raise ArtifactCollectionRejected("selector traversal exceeds the entry limit")
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise ArtifactCollectionRejected(
                            f"selector root contains an unsafe directory entry: {relative_directory / name}"
                        )
                for name in file_names:
                    visited_entries += 1
                    if visited_entries > self.max_walk_entries:
                        raise ArtifactCollectionRejected("selector traversal exceeds the entry limit")
                    relative = (relative_directory / name).as_posix()
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ArtifactCollectionRejected(f"selector root contains an unsafe entry: {relative}")
                    if not _matches(relative, patterns):
                        continue
                    if len(entries) >= self.max_artifacts:
                        raise ArtifactCollectionRejected("manifest exceeds the artifact count limit")
                    if metadata.st_size > self.max_artifact_bytes:
                        raise ArtifactCollectionRejected(f"artifact exceeds the per-file byte limit: {relative}")
                    try:
                        descriptor = os.open(name, os.O_RDONLY | no_follow | non_block, dir_fd=directory_fd)
                    except OSError as exc:
                        raise ArtifactCollectionRejected(f"artifact changed or is unsafe: {relative}") from exc
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        opened = os.fstat(descriptor)
                        if not stat.S_ISREG(opened.st_mode):
                            raise ArtifactCollectionRejected(f"artifact is not a regular file: {relative}")
                        while chunk := os.read(descriptor, 1024 * 1024):
                            size += len(chunk)
                            total_bytes += len(chunk)
                            if size > self.max_artifact_bytes:
                                raise ArtifactCollectionRejected(
                                    f"artifact exceeds the per-file byte limit: {relative}"
                                )
                            if total_bytes > self.max_manifest_bytes:
                                raise ArtifactCollectionRejected("manifest exceeds the total byte limit")
                            digest.update(chunk)
                        after = os.fstat(descriptor)
                        identity_before = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                        if identity_before != identity_after or size != after.st_size:
                            raise ArtifactCollectionRejected(f"artifact changed while being collected: {relative}")
                    finally:
                        os.close(descriptor)
                    entries.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
        finally:
            os.close(root_fd)
        return sorted(entries, key=lambda item: item["path"])

    @staticmethod
    def _text_excerpt(root: Path, relative: str, limit: int = 16 * 1024) -> str | None:
        parts = PurePosixPath(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(root, os.O_RDONLY | directory_flag | no_follow)
        file_fd: int | None = None
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | directory_flag | no_follow, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | no_follow, dir_fd=directory_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                return None
            data = os.read(file_fd, limit + 1)
        except (OSError, UnicodeError):
            return None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory_fd)
        if len(data) > limit or b"\x00" in data:
            return None
        try:
            return redact_sensitive(data.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    def collect_manifest(
        self,
        selector: ArtifactSelector | Mapping[str, Any],
        phase: str,
        *,
        task_case_id: str | None = None,
        observation_group_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(phase, str) or phase not in MANIFEST_PHASES:
            raise CollectorValidationError("manifest phase is invalid")
        if task_case_id is not None:
            task_case_id = _validate_identifier(task_case_id, "task_case_id")
        if observation_group_id is not None:
            observation_group_id = _validate_identifier(
                observation_group_id, "observation_group_id"
            )
        case_revision: int | None = None
        if task_case_id is not None:
            case = self.store.execute(
                "SELECT current_revision, metadata_json FROM task_cases WHERE id=? AND invalidated_at IS NULL",
                (task_case_id,),
            ).fetchone()
            if case is None:
                raise KeyError(task_case_id)
            case_revision = int(case["current_revision"])
        selected = self._selector(selector)
        if task_case_id is not None and selected.project_id is not None:
            case_metadata = json.loads(case["metadata_json"])
            existing_project = case_metadata.get("projectId")
            if existing_project not in {None, selected.project_id}:
                raise CollectorValidationError("manifest project does not match its task case")
            case_metadata["projectId"] = selected.project_id
        else:
            case_metadata = None
        root = self._secure_root(selected.root, self.allowed_roots)
        entries = self._collect_entries(root, selected.globs)
        root_path_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        observed_at = _utc_now()
        manifest = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "phase": phase,
            "observedAt": observed_at,
            "collectorVersion": self.collector_version,
            "environmentFingerprint": self.environment_fingerprint,
            "rootPathHash": root_path_hash,
            "selector": {
                "globs": list(selected.globs),
                "projectId": selected.project_id,
                "skillId": selected.skill_id,
            },
            "taskCaseId": task_case_id,
            "caseRevision": case_revision,
            "observationGroupId": observation_group_id,
            "artifacts": entries,
        }
        manifest_sha256 = _sha256_json(manifest)
        manifest["manifestSha256"] = manifest_sha256
        row_id = str(uuid.uuid4())
        with self.store.transaction():
            if task_case_id is not None and case_metadata is not None:
                self.store.execute(
                    "UPDATE task_cases SET metadata_json=?, updated_at=? WHERE id=?",
                    (_canonical_json(case_metadata), observed_at, task_case_id),
                )
            self.store.execute(
                """
                INSERT INTO artifact_manifests(
                    id, task_case_id, phase, collector_version, environment_fingerprint,
                    root_path_hash, manifest_json, manifest_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    task_case_id,
                    phase,
                    self.collector_version,
                    self.environment_fingerprint,
                    root_path_hash,
                    _canonical_json(manifest),
                    manifest_sha256,
                    observed_at,
                ),
            )
            if task_case_id is not None:
                identity = observation_group_id or row_id
                if phase == "after-artifacts" and observation_group_id:
                    prior_manifest = self.store.execute(
                        """SELECT id FROM artifacts WHERE task_case_id=? AND case_revision=?
                           AND artifact_type='manifest'
                           AND json_extract(metadata_json, '$.observationGroupId')=? LIMIT 1""",
                        (task_case_id, case_revision, observation_group_id),
                    ).fetchone()
                    if prior_manifest is not None:
                        self.store.execute(
                            """UPDATE artifacts SET freshness='stale' WHERE task_case_id=?
                               AND case_revision=?
                               AND json_extract(metadata_json, '$.observationGroupId')=?""",
                            (task_case_id, case_revision, observation_group_id),
                        )
                        self.store.execute(
                            """UPDATE check_runs SET freshness='stale' WHERE task_case_id=?
                               AND case_revision=? AND artifact_id IN (
                                 SELECT id FROM artifacts WHERE task_case_id=? AND case_revision=?
                                   AND json_extract(metadata_json, '$.observationGroupId')=?
                               )""",
                            (task_case_id, case_revision, task_case_id, case_revision, observation_group_id),
                        )
                        assessments = self.store.execute(
                            """SELECT id FROM outcome_assessments WHERE task_case_id=?
                               AND case_revision=? AND is_current=1""",
                            (task_case_id, case_revision),
                        ).fetchall()
                        self.store.execute(
                            """UPDATE outcome_assessments SET is_current=0, freshness='stale',
                                   process_state='invalidated' WHERE task_case_id=?
                                   AND case_revision=? AND is_current=1""",
                            (task_case_id, case_revision),
                        )
                        for assessment in assessments:
                            task = self.store.execute(
                                "SELECT id FROM review_tasks WHERE assessment_id=? ORDER BY created_at DESC LIMIT 1",
                                (assessment["id"],),
                            ).fetchone()
                            if task is None:
                                self.store.create_review_task(
                                    task_case_id, assessment["id"], "artifact-observation-replaced"
                                )
                            else:
                                self.store.execute(
                                    """UPDATE review_tasks SET status='open',
                                           queue_reason='artifact-observation-replaced', updated_at=? WHERE id=?""",
                                    (_utc_now(), task["id"]),
                                )
                manifest_artifact_id = _sha256_json([
                    "manifest-artifact", task_case_id, case_revision, identity, phase,
                ])
                self.store.execute(
                    """INSERT INTO artifacts(id, artifact_fingerprint, task_case_id, case_revision,
                           artifact_type, selector, content_hash, freshness, metadata_json, created_at)
                       VALUES (?, ?, ?, ?, 'manifest', ?, ?, 'current', ?, ?)
                       ON CONFLICT(artifact_fingerprint) DO UPDATE SET
                         content_hash=excluded.content_hash, freshness='current', metadata_json=excluded.metadata_json""",
                    (
                        manifest_artifact_id, manifest_artifact_id, task_case_id, case_revision,
                        _canonical_json(manifest["selector"]), manifest_sha256,
                        _canonical_json({"kind": "manifest", "manifestId": row_id, "phase": phase,
                                         "observationGroupId": observation_group_id,
                                         "caseRevision": case_revision}), observed_at,
                    ),
                )
                for entry in entries:
                    artifact_id = _sha256_json([
                        "file-artifact", task_case_id, case_revision, identity, entry["path"],
                    ])
                    self.store.execute(
                        """INSERT INTO artifacts(id, artifact_fingerprint, task_case_id, case_revision,
                               artifact_type, selector, content_hash, freshness, metadata_json, created_at)
                           VALUES (?, ?, ?, ?, 'file', ?, ?, 'current', ?, ?)
                           ON CONFLICT(artifact_fingerprint) DO UPDATE SET
                             content_hash=excluded.content_hash, freshness='current', metadata_json=excluded.metadata_json""",
                        (
                            artifact_id, artifact_id, task_case_id, case_revision,
                            entry["path"], entry["sha256"],
                            _canonical_json({"kind": "file", "path": entry["path"], "size": entry["size"],
                                             "excerpt": self._text_excerpt(root, entry["path"]),
                                             "manifestId": row_id, "observationGroupId": observation_group_id,
                                             "caseRevision": case_revision}),
                            observed_at,
                        ),
                    )
        return {"id": row_id, "task_case_id": task_case_id, **manifest}

    def _load_manifest(self, manifest_id: str) -> dict[str, Any]:
        if not isinstance(manifest_id, str):
            raise CollectorValidationError("trusted manifest comparison requires stored manifest IDs")
        row = self.store.execute("SELECT * FROM artifact_manifests WHERE id = ?", (manifest_id,)).fetchone()
        if row is None:
            raise KeyError(manifest_id)
        manifest = json.loads(row["manifest_json"])
        if (
            manifest.get("phase") != row["phase"]
            or manifest.get("collectorVersion") != row["collector_version"]
            or manifest.get("environmentFingerprint") != row["environment_fingerprint"]
            or manifest.get("rootPathHash") != row["root_path_hash"]
            or manifest.get("manifestSha256") != row["manifest_sha256"]
            or manifest.get("taskCaseId") != row["task_case_id"]
        ):
            raise CollectorValidationError("stored manifest bindings are inconsistent")
        manifest["id"] = row["id"]
        manifest["task_case_id"] = row["task_case_id"]
        return manifest

    def checker_binding(
        self, manifest_id: str, task_case_id: str, workspace: str | Path
    ) -> dict[str, Any]:
        manifest = self._validate_manifest(self._load_manifest(manifest_id))
        if manifest["phase"] != "after-artifacts":
            raise CollectorValidationError("checker requires an after-artifacts manifest")
        if manifest["taskCaseId"] != task_case_id:
            raise CollectorValidationError("checker manifest belongs to another task case")
        if not manifest["observationGroupId"]:
            raise CollectorValidationError("checker manifest requires an observation group")
        root = self._secure_root(workspace, self.allowed_roots)
        if hashlib.sha256(str(root).encode("utf-8")).hexdigest() != manifest["rootPathHash"]:
            raise CollectorValidationError("checker workspace does not match manifest root")
        if manifest["environmentFingerprint"] != self.environment_fingerprint:
            raise CollectorValidationError("checker manifest environment is stale")
        case = self.store.execute(
            "SELECT current_revision FROM task_cases WHERE id=? AND invalidated_at IS NULL",
            (task_case_id,),
        ).fetchone()
        if case is None:
            raise CollectorValidationError("checker task case is not current")
        if int(case["current_revision"]) != manifest["caseRevision"]:
            raise CollectorValidationError("checker manifest belongs to an older task case revision")
        artifact_id = _sha256_json([
            "manifest-artifact", task_case_id, manifest["caseRevision"],
            manifest["observationGroupId"], "after-artifacts",
        ])
        current_artifact = self.store.execute(
            "SELECT content_hash, freshness, metadata_json FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if current_artifact is None:
            raise CollectorValidationError("checker manifest artifact is missing")
        try:
            artifact_metadata = json.loads(current_artifact["metadata_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollectorValidationError("checker manifest artifact metadata is invalid") from exc
        if (
            current_artifact["freshness"] != "current"
            or current_artifact["content_hash"] != manifest["manifestSha256"]
            or artifact_metadata.get("manifestId") != manifest["id"]
        ):
            raise CollectorValidationError("checker manifest has been superseded")
        return {
            **manifest, "workspace": root, "artifactId": artifact_id,
            "caseRevision": manifest["caseRevision"],
        }

    def collect_after_check(self, binding: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        selector = binding["selector"]
        observed = self.collect_manifest(
            ArtifactSelector(
                binding["workspace"], tuple(selector["globs"]),
                project_id=selector.get("projectId"), skill_id=selector.get("skillId"),
            ),
            "after-check", task_case_id=binding["taskCaseId"],
            observation_group_id=binding["observationGroupId"],
        )
        return observed, self.compare_manifests(binding["id"], observed["id"])

    def authorize_checker_options(
        self, binding: Mapping[str, Any], checker_id: str, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(options, Mapping):
            raise CollectorValidationError("checker options must be an object")
        if checker_id == "document-artifact":
            allowed = {"path"}
            if set(options) - allowed:
                raise CollectorValidationError("document checker contains unapproved options")
            raw_path = str(options.get("path") or "")
            normalized = PurePosixPath(raw_path.replace("\\", "/")).as_posix()
            selected_paths = {str(item["path"]) for item in binding["artifacts"]}
            if (
                not normalized or normalized.startswith(("/", "../"))
                or normalized not in selected_paths
                or not any(fnmatch.fnmatch(normalized, pattern) for pattern in binding["selector"]["globs"])
            ):
                raise CollectorValidationError("document checker path is not covered by its manifest")
        elif checker_id == "gradle-summary":
            if set(options) - {"tasks", "skipped_policy"}:
                raise CollectorValidationError("Gradle checker contains unapproved options")
            if "*" not in binding["selector"]["globs"]:
                raise CollectorValidationError("Gradle checker manifest must cover every workspace file")
            selected_paths = {str(item["path"]) for item in binding["artifacts"]}
            if not selected_paths & {
                "gradlew", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
            }:
                raise CollectorValidationError("Gradle checker manifest does not cover a Gradle project")
        else:
            raise CollectorValidationError("checker has no approved manifest binding profile")
        approval_version = (
            "document-exists-v1" if checker_id == "document-artifact" else "gradle-advisory-v1"
        )
        return {
            "approvalVersion": approval_version, "checkerId": checker_id,
            "optionsSha256": _sha256_json(options), "manifestSha256": binding["manifestSha256"],
        }

    def compare_manifests(
        self,
        expected: str,
        observed: str,
    ) -> dict[str, Any]:
        before = self._validate_manifest(self._load_manifest(expected))
        after = self._validate_manifest(self._load_manifest(observed))
        if before.get("id") is not None and before.get("id") == after.get("id"):
            return self._unknown_comparison("same-observation")
        if before["phase"] != "after-artifacts" or after["phase"] != "after-check":
            return self._unknown_comparison("phase-mismatch")
        before_time, _ = _parse_timestamp(before["observedAt"], "observedAt")
        after_time, _ = _parse_timestamp(after["observedAt"], "observedAt")
        if after_time < before_time:
            return self._unknown_comparison("observation-order-invalid")
        if before["taskCaseId"] != after["taskCaseId"]:
            return self._unknown_comparison("task-case-mismatch")
        if before["caseRevision"] != after["caseRevision"]:
            return self._unknown_comparison("case-revision-mismatch")
        if (
            before["observationGroupId"] is None
            or before["observationGroupId"] != after["observationGroupId"]
        ):
            return self._unknown_comparison("observation-group-mismatch")
        if before["rootPathHash"] != after["rootPathHash"] or before["selector"] != after["selector"]:
            return self._unknown_comparison("selector-mismatch")
        if before["environmentFingerprint"] != after["environmentFingerprint"]:
            return self._unknown_comparison("environment-mismatch", environment_changed=True)
        before_entries = self._manifest_entries(before["artifacts"])
        after_entries = self._manifest_entries(after["artifacts"])
        added = sorted(after_entries.keys() - before_entries.keys())
        removed = sorted(before_entries.keys() - after_entries.keys())
        modified = sorted(
            path
            for path in before_entries.keys() & after_entries.keys()
            if before_entries[path] != after_entries[path]
        )
        stale = bool(added or removed or modified)
        result = {
            "freshness": "stale" if stale else "current",
            "reason": "artifact-drift" if stale else None,
            "added": added,
            "removed": removed,
            "modified": modified,
            "expectedManifestSha256": before["manifestSha256"],
            "observedManifestSha256": after["manifestSha256"],
            "environmentChanged": False,
        }
        task_case_id = before.get("taskCaseId")
        if task_case_id:
            with self.store.transaction():
                if stale:
                    group_id = before["observationGroupId"]
                    self.store.execute(
                        """UPDATE artifacts SET freshness='stale' WHERE task_case_id=?
                           AND json_extract(metadata_json, '$.observationGroupId')=?""",
                        (task_case_id, group_id),
                    )
                    self.store.execute(
                        """UPDATE check_runs SET freshness='stale' WHERE task_case_id=?
                           AND artifact_id IN (
                             SELECT id FROM artifacts WHERE task_case_id=?
                               AND json_extract(metadata_json, '$.observationGroupId')=?
                           )""",
                        (task_case_id, task_case_id, group_id),
                    )
                    current_assessments = self.store.execute(
                        "SELECT id FROM outcome_assessments WHERE task_case_id=? AND is_current=1",
                        (task_case_id,),
                    ).fetchall()
                    self.store.execute(
                        """UPDATE outcome_assessments SET freshness='stale',
                               process_state='invalidated', is_current=0
                           WHERE task_case_id=? AND is_current=1""",
                        (task_case_id,),
                    )
                    self.store.execute(
                        """UPDATE review_tasks SET status='open', queue_reason='artifact-drift',
                               updated_at=? WHERE task_case_id=?""",
                        (_utc_now(), task_case_id),
                    )
                    for assessment in current_assessments:
                        queued = self.store.execute(
                            "SELECT id FROM review_tasks WHERE assessment_id=? AND status='open'",
                            (assessment["id"],),
                        ).fetchone()
                        if queued is None:
                            self.store.create_review_task(
                                task_case_id, assessment["id"], "artifact-drift"
                            )
        return result

    @staticmethod
    def _unknown_comparison(reason: str, *, environment_changed: bool = False) -> dict[str, Any]:
        return {
            "freshness": "unknown",
            "reason": reason,
            "added": [],
            "removed": [],
            "modified": [],
            "environmentChanged": environment_changed,
        }

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        body_keys = {
            "schemaVersion",
            "phase",
            "observedAt",
            "collectorVersion",
            "environmentFingerprint",
            "rootPathHash",
            "selector",
            "taskCaseId",
            "caseRevision",
            "observationGroupId",
            "artifacts",
            "manifestSha256",
        }
        wrapper_keys = {"id", "task_case_id"}
        if set(manifest) - body_keys - wrapper_keys or not body_keys <= set(manifest):
            raise CollectorValidationError("manifest schema is invalid")
        value = dict(manifest)
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
            raise CollectorValidationError("manifest schema version is invalid")
        if value["phase"] not in MANIFEST_PHASES:
            raise CollectorValidationError("manifest phase is invalid")
        _parse_timestamp(value["observedAt"], "observedAt")
        _validate_identifier(value["collectorVersion"], "collectorVersion", maximum=128)
        for field in ("environmentFingerprint", "rootPathHash", "manifestSha256"):
            if not isinstance(value[field], str) or not _SHA256_RE.fullmatch(value[field]):
                raise CollectorValidationError(f"manifest {field} is invalid")
        selector = value["selector"]
        if not isinstance(selector, Mapping) or set(selector) != {"globs", "projectId", "skillId"}:
            raise CollectorValidationError("manifest selector is invalid")
        self._validate_globs(selector["globs"])
        for field in ("projectId", "skillId"):
            if selector[field] is not None:
                _validate_identifier(selector[field], field)
        if value["taskCaseId"] is not None:
            _validate_identifier(value["taskCaseId"], "taskCaseId")
            if isinstance(value["caseRevision"], bool) or not isinstance(value["caseRevision"], int) or value["caseRevision"] < 1:
                raise CollectorValidationError("manifest caseRevision is invalid")
        elif value["caseRevision"] is not None:
            raise CollectorValidationError("manifest caseRevision requires a taskCaseId")
        if value["observationGroupId"] is not None:
            _validate_identifier(value["observationGroupId"], "observationGroupId")
        if "task_case_id" in value and value["task_case_id"] != value["taskCaseId"]:
            raise CollectorValidationError("manifest task case binding is inconsistent")
        self._manifest_entries(value["artifacts"])
        digest_body = {key: value[key] for key in body_keys if key != "manifestSha256"}
        expected_digest = _sha256_json(digest_body)
        if not hmac.compare_digest(value["manifestSha256"], expected_digest):
            raise CollectorValidationError("manifest digest is invalid")
        return value

    @staticmethod
    def _manifest_entries(entries: Any) -> dict[str, tuple[str, int]]:
        if not isinstance(entries, list):
            raise CollectorValidationError("manifest artifacts must be a list")
        result: dict[str, tuple[str, int]] = {}
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"path", "sha256", "size"}
                or not isinstance(entry["path"], str)
                or not isinstance(entry["sha256"], str)
                or not _SHA256_RE.fullmatch(entry["sha256"])
                or isinstance(entry["size"], bool)
                or not isinstance(entry["size"], int)
                or entry["size"] < 0
                or entry["path"] in result
            ):
                raise CollectorValidationError("manifest contains an invalid artifact")
            result[entry["path"]] = (entry["sha256"], entry["size"])
        return result

    def run_checker(
        self,
        checker_id: str,
        workspace: str | Path,
        *,
        authorized: bool = False,
        timeout_seconds: float | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        if authorized is not True:
            raise CheckerExecutionRejected("checker execution requires explicit authorization")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise CheckerExecutionRejected("checker timeout must be a positive finite number")
        checker_id = _validate_identifier(checker_id, "checker_id", maximum=128)
        if checker_id not in self.checker_allowlist:
            raise CheckerExecutionRejected("checker is not in the collector allowlist")
        if self.checker_runner is None:
            raise CheckerExecutionRejected("no BubblewrapCheckerRunner is configured")
        if not self.allowed_workspace_roots:
            raise CheckerExecutionRejected("allowed workspace roots are not configured")
        try:
            source = self._secure_root(workspace, self.allowed_workspace_roots)
        except ArtifactCollectionRejected as exc:
            raise CheckerExecutionRejected(str(exc)) from exc
        runner_roots = tuple(getattr(self.checker_runner, "allowed_workspace_roots", ()))
        if not runner_roots or not _within(source, runner_roots):
            raise CheckerExecutionRejected("runner allowed workspace roots do not authorize this workspace")
        if any(key in options for key in ("command", "cmd", "shell")):
            raise CheckerExecutionRejected("arbitrary checker commands are forbidden")
        try:
            result = self.checker_runner.run(
                checker_id,
                source,
                timeout_seconds=timeout_seconds,
                **options,
            )
        except CheckerCommandRejected:
            raise
        if not isinstance(result, Mapping):
            raise CollectorError("checker runner returned a non-object result")
        result_object = dict(result)
        try:
            _validate_json(result_object)
            result_bytes = len(_canonical_json(result_object).encode("utf-8"))
        except (CollectorValidationError, UnicodeEncodeError, ValueError) as exc:
            raise CollectorError("checker runner returned an unsafe result") from exc
        result_limit = max(self.max_event_bytes, self.checker_runner.max_output_bytes + 64 * 1024)
        if result_bytes > result_limit:
            raise CollectorError("checker runner result exceeds the byte limit")
        return _redact_json(result_object)

    def cleanup(
        self,
        *,
        older_than: str | datetime | None = None,
        project_id: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        if older_than is None and project_id is None and skill_id is None:
            raise CollectorValidationError("cleanup requires at least one filter")
        cutoff: datetime | None = None
        cutoff_text: str | None = None
        if isinstance(older_than, datetime):
            if older_than.tzinfo is None or older_than.utcoffset() is None:
                raise CollectorValidationError("older_than must include a timezone")
            cutoff = older_than.astimezone(timezone.utc)
            cutoff_text = cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z")
        elif older_than is not None:
            cutoff, cutoff_text = _parse_timestamp(older_than, "older_than")
        if project_id is not None:
            project_id = _validate_identifier(project_id, "project_id")
        if skill_id is not None:
            skill_id = _validate_identifier(skill_id, "skill_id")

        event_ids: list[str] = []
        manifest_ids: list[str] = []
        requested_at = _utc_now()
        criteria = {"olderThan": cutoff_text, "projectId": project_id, "skillId": skill_id}
        audit_id = str(uuid.uuid4())
        with self.store.transaction():
            for row in self.store.execute(
                "SELECT id, task_case_id, payload_json, created_at FROM prospective_events"
            ).fetchall():
                payload = json.loads(row["payload_json"])
                context = dict(payload.get("context", {})) if isinstance(payload, dict) else {}
                if row["task_case_id"] and skill_id is not None and self.store.execute(
                    """SELECT 1 FROM attribution_links l JOIN skill_invocations i
                       ON i.id=l.skill_invocation_id WHERE l.task_case_id=?
                       AND l.status='active' AND i.skill_id<>? LIMIT 1""",
                    (row["task_case_id"], skill_id),
                ).fetchone() is not None:
                    continue
                if row["task_case_id"] and skill_id is not None and not context.get("skillId"):
                    linked = self.store.execute(
                        """SELECT 1 FROM attribution_links l JOIN skill_invocations i
                           ON i.id=l.skill_invocation_id WHERE l.task_case_id=? AND i.skill_id=? LIMIT 1""",
                        (row["task_case_id"], skill_id),
                    ).fetchone()
                    if linked is not None:
                        context["skillId"] = skill_id
                if row["task_case_id"] and project_id is not None and not context.get("projectId"):
                    case = self.store.execute(
                        "SELECT json_extract(metadata_json, '$.projectId') FROM task_cases WHERE id=?",
                        (row["task_case_id"],),
                    ).fetchone()
                    if case is not None and case[0] == project_id:
                        context["projectId"] = project_id
                if self._cleanup_matches(row["created_at"], context, cutoff, project_id, skill_id):
                    event_ids.append(row["id"])
            for row in self.store.execute(
                "SELECT id, task_case_id, manifest_json, created_at FROM artifact_manifests"
            ).fetchall():
                manifest = json.loads(row["manifest_json"])
                selector = dict(manifest.get("selector", {})) if isinstance(manifest, dict) else {}
                frozen_project = self._cleanup_manifest_projects.get(row["id"])
                if frozen_project and not selector.get("projectId"):
                    selector["projectId"] = frozen_project
                if row["task_case_id"] and skill_id is not None and self.store.execute(
                    """SELECT 1 FROM attribution_links l JOIN skill_invocations i
                       ON i.id=l.skill_invocation_id WHERE l.task_case_id=?
                       AND l.status='active' AND i.skill_id<>? LIMIT 1""",
                    (row["task_case_id"], skill_id),
                ).fetchone() is not None:
                    continue
                if row["task_case_id"] and skill_id is not None and not selector.get("skillId"):
                    linked = self.store.execute(
                        """SELECT 1 FROM attribution_links l JOIN skill_invocations i
                           ON i.id=l.skill_invocation_id WHERE l.task_case_id=? AND i.skill_id=? LIMIT 1""",
                        (row["task_case_id"], skill_id),
                    ).fetchone()
                    if linked is not None:
                        selector["skillId"] = skill_id
                if row["task_case_id"] and project_id is not None and not selector.get("projectId"):
                    case = self.store.execute(
                        "SELECT json_extract(metadata_json, '$.projectId') FROM task_cases WHERE id=?",
                        (row["task_case_id"],),
                    ).fetchone()
                    if case is not None and case[0] == project_id:
                        selector["projectId"] = project_id
                if self._cleanup_matches(row["created_at"], selector, cutoff, project_id, skill_id):
                    manifest_ids.append(row["id"])
            deleted_events = self._delete_rows("prospective_events", event_ids)
            deleted_manifests = self._delete_rows("artifact_manifests", manifest_ids)
            self.store.execute(
                """
                INSERT INTO prospective_cleanup_audits(
                    id, requested_at, older_than, project_id_hash, skill_id_hash,
                    deleted_event_count, deleted_manifest_count, criteria_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    requested_at,
                    cutoff_text,
                    hashlib.sha256(project_id.encode("utf-8")).hexdigest() if project_id else None,
                    hashlib.sha256(skill_id.encode("utf-8")).hexdigest() if skill_id else None,
                    deleted_events,
                    deleted_manifests,
                    _sha256_json(criteria),
                ),
            )
        return {
            "auditId": audit_id,
            "requestedAt": requested_at,
            "deletedEvents": deleted_events,
            "deletedManifests": deleted_manifests,
            "criteriaSha256": _sha256_json(criteria),
        }

    def materialize_cleanup_context(self) -> int:
        """Freeze case-derived project context before derived cleanup mutates cases."""
        updated = 0
        with self.store.transaction():
            rows = self.store.execute(
                """SELECT e.id, e.payload_json, c.metadata_json FROM prospective_events e
                   JOIN task_cases c ON c.id=e.task_case_id"""
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                context = payload.get("context") if isinstance(payload, dict) else None
                case_metadata = json.loads(row["metadata_json"])
                project_id = case_metadata.get("projectId")
                if not isinstance(context, dict) or context.get("projectId") is not None or not project_id:
                    continue
                context["projectId"] = project_id
                self.store.execute(
                    "UPDATE prospective_events SET payload_json=? WHERE id=?",
                    (_canonical_json(payload), row["id"]),
                )
                updated += 1
            manifests = self.store.execute(
                """SELECT m.id, m.manifest_json, c.metadata_json FROM artifact_manifests m
                   JOIN task_cases c ON c.id=m.task_case_id"""
            ).fetchall()
            for row in manifests:
                manifest = json.loads(row["manifest_json"])
                selector = manifest.get("selector") if isinstance(manifest, dict) else None
                project_id = json.loads(row["metadata_json"]).get("projectId")
                if isinstance(selector, dict) and not selector.get("projectId") and project_id:
                    self._cleanup_manifest_projects[row["id"]] = project_id
        return updated

    clear = cleanup

    def _delete_rows(self, table: str, row_ids: Sequence[str]) -> int:
        if table not in {"prospective_events", "artifact_manifests"}:
            raise ValueError(table)
        deleted = 0
        for offset in range(0, len(row_ids), 500):
            batch = row_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            cursor = self.store.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", batch)
            deleted += cursor.rowcount
        return deleted

    @staticmethod
    def _cleanup_matches(
        created_at: str,
        context: Mapping[str, Any],
        cutoff: datetime | None,
        project_id: str | None,
        skill_id: str | None,
    ) -> bool:
        if cutoff is not None:
            created, _normalized = _parse_timestamp(created_at, "created_at")
            if created >= cutoff:
                return False
        if project_id is not None and context.get("projectId") != project_id:
            return False
        if skill_id is not None and context.get("skillId") != skill_id:
            return False
        return True

__all__ = [
    "ArtifactCollectionRejected",
    "ArtifactSelector",
    "CheckerExecutionRejected",
    "COLLECTOR_VERSION",
    "CollectorError",
    "CollectorValidationError",
    "EVENT_SCHEMA_VERSION",
    "MANIFEST_PHASES",
    "ProspectiveCollector",
]