from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


class EffectStoreError(RuntimeError):
    """Base error for the effect index."""


class RevisionConflict(EffectStoreError):
    """Raised when an optimistic write used a stale revision."""


class ImmutableSnapshotError(EffectStoreError):
    """Raised when an immutable metric snapshot is changed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(value: Any, default: Any = None) -> str:
    if value is None:
        value = {} if default is None else default
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'incremental',
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_files INTEGER NOT NULL DEFAULT 0 CHECK (discovered_files >= 0),
    indexed_files INTEGER NOT NULL DEFAULT 0 CHECK (indexed_files >= 0),
    pending_files INTEGER NOT NULL DEFAULT 0 CHECK (pending_files >= 0),
    failed_files INTEGER NOT NULL DEFAULT 0 CHECK (failed_files >= 0),
    indexed_bytes INTEGER NOT NULL DEFAULT 0 CHECK (indexed_bytes >= 0),
    coverage_start TEXT,
    coverage_end TEXT,
    coverage_status TEXT NOT NULL DEFAULT 'unknown',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS log_files (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    session_header_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    deleted_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, stable_key)
);

CREATE TABLE IF NOT EXISTS log_file_generations (
    id TEXT PRIMARY KEY,
    log_file_id TEXT NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
    generation_key TEXT NOT NULL,
    scan_run_id TEXT REFERENCES scan_runs(id) ON DELETE SET NULL,
    parser_version TEXT NOT NULL,
    session_header_id TEXT,
    device TEXT,
    inode TEXT,
    observed_size INTEGER NOT NULL DEFAULT 0 CHECK (observed_size >= 0),
    observed_mtime_ns INTEGER,
    header_hash TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (log_file_id, generation_key)
);

CREATE TABLE IF NOT EXISTS log_file_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id TEXT NOT NULL REFERENCES log_file_generations(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    UNIQUE (generation_id, path)
);

CREATE TABLE IF NOT EXISTS file_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id TEXT NOT NULL REFERENCES log_file_generations(id) ON DELETE CASCADE,
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    line_number INTEGER NOT NULL DEFAULT 0 CHECK (line_number >= 0),
    prefix_hash TEXT,
    cursor_window_hash TEXT,
    sparse_hashes_json TEXT NOT NULL DEFAULT '[]',
    complete_line INTEGER NOT NULL DEFAULT 1 CHECK (complete_line IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (generation_id, byte_offset)
);

CREATE TABLE IF NOT EXISTS canonical_events (
    id TEXT PRIMARY KEY,
    event_fingerprint TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    session_family TEXT NOT NULL,
    source_event_id TEXT,
    event_type TEXT NOT NULL,
    protocol_time TEXT,
    parent_event_id TEXT,
    call_id TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    orphaned INTEGER NOT NULL DEFAULT 1 CHECK (orphaned IN (0, 1)),
    first_seen_scan_id TEXT REFERENCES scan_runs(id) ON DELETE SET NULL,
    last_seen_scan_id TEXT REFERENCES scan_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(id) ON DELETE CASCADE,
    generation_id TEXT NOT NULL REFERENCES log_file_generations(id) ON DELETE CASCADE,
    byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
    byte_end INTEGER CHECK (byte_end IS NULL OR byte_end >= byte_start),
    line_number INTEGER CHECK (line_number IS NULL OR line_number >= 1),
    locator_json TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    UNIQUE (event_id, generation_id, byte_start)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    session_family TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    title TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, source_session_id)
);

CREATE TABLE IF NOT EXISTS session_edges (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    child_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    event_id TEXT REFERENCES canonical_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (parent_session_id, child_session_id, edge_type),
    CHECK (parent_session_id <> child_session_id)
);

CREATE TABLE IF NOT EXISTS task_episodes (
    id TEXT PRIMARY KEY,
    episode_fingerprint TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    start_event_id TEXT REFERENCES canonical_events(id) ON DELETE SET NULL,
    end_event_id TEXT REFERENCES canonical_events(id) ON DELETE SET NULL,
    goal_text TEXT,
    process_state TEXT NOT NULL DEFAULT 'discovered',
    invalidated_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_cases (
    id TEXT PRIMARY KEY,
    case_fingerprint TEXT NOT NULL UNIQUE,
    task_type TEXT,
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
    current_assessment_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_assessment_revision >= 0),
    invalidated_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_case_episodes (
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    task_episode_id TEXT NOT NULL REFERENCES task_episodes(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL DEFAULT 'primary',
    PRIMARY KEY (task_case_id, task_episode_id)
);

CREATE TABLE IF NOT EXISTS task_facts (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    case_revision INTEGER NOT NULL CHECK (case_revision >= 1),
    fact_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    evidence_event_id TEXT REFERENCES canonical_events(id) ON DELETE SET NULL,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_classifications (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    profile_version TEXT NOT NULL,
    applicability TEXT NOT NULL DEFAULT 'unknown',
    task_type TEXT,
    classification_json TEXT NOT NULL DEFAULT '{}',
    actor_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (task_case_id, revision)
);

CREATE TABLE IF NOT EXISTS skill_invocations (
    id TEXT PRIMARY KEY,
    invocation_fingerprint TEXT NOT NULL UNIQUE,
    task_episode_id TEXT NOT NULL REFERENCES task_episodes(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES canonical_events(id) ON DELETE RESTRICT,
    skill_id TEXT NOT NULL,
    skill_sha256 TEXT,
    skill_path TEXT,
    invocation_kind TEXT NOT NULL DEFAULT 'business-use',
    load_status TEXT NOT NULL DEFAULT 'unknown',
    validity TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    call_fingerprint TEXT NOT NULL UNIQUE,
    task_episode_id TEXT NOT NULL REFERENCES task_episodes(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES canonical_events(id) ON DELETE RESTRICT,
    call_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_hash TEXT,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    called_at TEXT
);

CREATE TABLE IF NOT EXISTS tool_results (
    id TEXT PRIMARY KEY,
    result_fingerprint TEXT NOT NULL UNIQUE,
    tool_call_id TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
    event_id TEXT REFERENCES canonical_events(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'unknown',
    exit_code INTEGER,
    output_hash TEXT,
    excerpt TEXT,
    completed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    artifact_fingerprint TEXT NOT NULL UNIQUE,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    case_revision INTEGER NOT NULL CHECK (case_revision >= 1),
    artifact_type TEXT NOT NULL,
    selector TEXT,
    content_hash TEXT,
    freshness TEXT NOT NULL DEFAULT 'unknown',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id TEXT PRIMARY KEY,
    evidence_fingerprint TEXT NOT NULL UNIQUE,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    event_id TEXT REFERENCES canonical_events(id) ON DELETE RESTRICT,
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    evidence_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    excerpt TEXT,
    validity TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attribution_links (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    skill_invocation_id TEXT NOT NULL REFERENCES skill_invocations(id) ON DELETE CASCADE,
    evidence_id TEXT REFERENCES evidence_items(id) ON DELETE SET NULL,
    attribution_kind TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    UNIQUE (task_case_id, skill_invocation_id, evidence_id, attribution_kind)
);

CREATE TABLE IF NOT EXISTS check_runs (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    checker_id TEXT NOT NULL,
    checker_version TEXT NOT NULL,
    approval_version TEXT,
    status TEXT NOT NULL,
    assertion_outcome TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    freshness TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS semantic_reviews (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    case_revision INTEGER NOT NULL CHECK (case_revision >= 1),
    model_id TEXT NOT NULL,
    model_version TEXT,
    prompt_version TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL,
    review_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcome_assessments (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    case_revision INTEGER NOT NULL CHECK (case_revision >= 1),
    contract_version_id TEXT,
    classification_revision INTEGER,
    parser_version TEXT,
    checker_version TEXT,
    model_version TEXT,
    prompt_version TEXT,
    rubric_version TEXT,
    process_state TEXT NOT NULL DEFAULT 'discovered',
    assessability TEXT NOT NULL DEFAULT 'needs-evidence',
    automated_verdict TEXT NOT NULL DEFAULT 'unset',
    conflict_state TEXT NOT NULL DEFAULT 'none',
    freshness TEXT NOT NULL DEFAULT 'unknown',
    rationale_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (task_case_id, revision),
    UNIQUE (id, task_case_id)
);

CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    external_subject TEXT UNIQUE,
    display_name TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_tasks (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE CASCADE,
    assessment_id TEXT NOT NULL,
    queue_reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by_actor_id TEXT REFERENCES actors(id) ON DELETE SET NULL,
    current_decision_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_decision_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (assessment_id, task_case_id)
        REFERENCES outcome_assessments(id, task_case_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS manual_decisions (
    id TEXT PRIMARY KEY,
    review_task_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE RESTRICT,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE RESTRICT,
    assessment_id TEXT NOT NULL REFERENCES outcome_assessments(id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    action TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'unset',
    reason_code TEXT NOT NULL,
    note TEXT,
    binding_json TEXT NOT NULL DEFAULT '{}',
    supersedes_id TEXT REFERENCES manual_decisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (review_task_id, revision)
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE RESTRICT,
    assessment_id TEXT REFERENCES outcome_assessments(id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    correction_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    supersedes_id TEXT REFERENCES corrections(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (task_case_id, revision)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id TEXT PRIMARY KEY,
    task_case_id TEXT NOT NULL REFERENCES task_cases(id) ON DELETE RESTRICT,
    assessment_id TEXT NOT NULL REFERENCES outcome_assessments(id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    reason_code TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    supersedes_id TEXT REFERENCES exceptions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (task_case_id, revision)
);

CREATE TABLE IF NOT EXISTS prospective_events (
    id TEXT PRIMARY KEY,
    event_fingerprint TEXT NOT NULL UNIQUE,
    task_case_id TEXT REFERENCES task_cases(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    consumed_event_id TEXT REFERENCES canonical_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id TEXT PRIMARY KEY,
    scan_run_id TEXT REFERENCES scan_runs(id) ON DELETE RESTRICT,
    cutoff_at TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshot_cases (
    snapshot_id TEXT NOT NULL REFERENCES metric_snapshots(id) ON DELETE RESTRICT,
    task_case_id TEXT NOT NULL,
    task_case_revision INTEGER NOT NULL,
    assessment_id TEXT,
    assessment_revision INTEGER,
    manual_decision_id TEXT,
    manual_decision_revision INTEGER,
    skill_id TEXT NOT NULL,
    skill_sha256 TEXT,
    contract_version_id TEXT,
    task_type TEXT,
    attribution_kind TEXT NOT NULL,
    effective_verdict TEXT NOT NULL,
    metric_eligible INTEGER NOT NULL CHECK (metric_eligible IN (0, 1)),
    exclusion_reason TEXT,
    frozen_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, task_case_id, skill_id, attribution_kind)
);

CREATE INDEX IF NOT EXISTS idx_generations_file ON log_file_generations(log_file_id, status);
CREATE INDEX IF NOT EXISTS idx_locations_path ON log_file_locations(path, is_current);
CREATE INDEX IF NOT EXISTS idx_checkpoints_generation ON file_checkpoints(generation_id, byte_offset DESC);
CREATE INDEX IF NOT EXISTS idx_events_time ON canonical_events(protocol_time, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON canonical_events(event_type, protocol_time);
CREATE INDEX IF NOT EXISTS idx_events_orphaned ON canonical_events(orphaned) WHERE orphaned = 1;
CREATE INDEX IF NOT EXISTS idx_provenance_generation ON event_provenance(generation_id);
CREATE INDEX IF NOT EXISTS idx_cases_type ON task_cases(task_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_invocations_skill ON skill_invocations(skill_id, skill_sha256);
CREATE INDEX IF NOT EXISTS idx_assessments_case ON outcome_assessments(task_case_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_review_queue ON review_tasks(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_manual_case ON manual_decisions(task_case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_cutoff ON metric_snapshots(cutoff_at, id);

CREATE TRIGGER IF NOT EXISTS provenance_insert_restores_event
AFTER INSERT ON event_provenance
BEGIN
    UPDATE canonical_events SET orphaned = 0, updated_at = NEW.observed_at WHERE id = NEW.event_id;
END;

CREATE TRIGGER IF NOT EXISTS provenance_delete_orphans_event
AFTER DELETE ON event_provenance
WHEN NOT EXISTS (SELECT 1 FROM event_provenance WHERE event_id = OLD.event_id)
BEGIN
    UPDATE canonical_events SET orphaned = 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = OLD.event_id;
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_no_update
BEFORE UPDATE ON metric_snapshots
WHEN OLD.sealed = 1 OR NEW.sealed <> 1
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_no_replace
BEFORE INSERT ON metric_snapshots
WHEN EXISTS (SELECT 1 FROM metric_snapshots WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_no_delete
BEFORE DELETE ON metric_snapshots
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_case_only_before_seal
BEFORE INSERT ON metric_snapshot_cases
WHEN (SELECT sealed FROM metric_snapshots WHERE id = NEW.snapshot_id) <> 0
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_case_no_update
BEFORE UPDATE ON metric_snapshot_cases
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshot_case_no_delete
BEFORE DELETE ON metric_snapshot_cases
BEGIN
    SELECT RAISE(ABORT, 'metric snapshot is immutable');
END;
"""


class EffectStore:
    """SQLite-backed index for normalized skill-effect data."""

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self._transaction_depth = 0
        self.connection = sqlite3.connect(
            self.path,
            timeout=max(busy_timeout_ms, 0) / 1000,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA recursive_triggers = ON")
        self.connection.execute(f"PRAGMA busy_timeout = {max(int(busy_timeout_ms), 0)}")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._secure_files()
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.migrate()
        self._secure_files()

    def __enter__(self) -> EffectStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection:
            self._secure_files()
            self.connection.close()

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            try:
                candidate.chmod(0o600)
            except FileNotFoundError:
                pass

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        depth = self._transaction_depth
        savepoint = f"effect_store_{depth}"
        self._transaction_depth += 1
        try:
            if depth == 0:
                self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                self.connection.execute(f"SAVEPOINT {savepoint}")
            yield self.connection
            if depth == 0:
                self.connection.commit()
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            if depth == 0:
                self.connection.rollback()
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        finally:
            self._transaction_depth -= 1
            if depth == 0:
                self._secure_files()

    def migrate(self) -> int:
        if self._transaction_depth:
            raise EffectStoreError("migration cannot run inside another transaction")
        script = f"""
        BEGIN IMMEDIATE;
        {SCHEMA}
        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
            VALUES ({SCHEMA_VERSION}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
        PRAGMA user_version = {SCHEMA_VERSION};
        COMMIT;
        """
        try:
            self.connection.executescript(script)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        finally:
            self._secure_files()
        return SCHEMA_VERSION

    def pragma(self, name: str) -> Any:
        if name not in {"journal_mode", "foreign_keys", "busy_timeout", "user_version"}:
            raise ValueError(f"unsupported pragma: {name}")
        row = self.connection.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None

    def create_scan_run(
        self,
        source: str,
        *,
        mode: str = "incremental",
        scan_run_id: str | None = None,
        metadata: Any = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        run_id = scan_run_id or _new_id()
        with self.transaction():
            self.connection.execute(
                "INSERT INTO scan_runs(id, source, mode, started_at, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, source, mode, started_at or _utc_now(), _json(metadata)),
            )
        return self.get_scan_run(run_id)

    def finish_scan_run(
        self,
        scan_run_id: str,
        *,
        status: str = "completed",
        discovered_files: int = 0,
        indexed_files: int = 0,
        pending_files: int = 0,
        failed_files: int = 0,
        indexed_bytes: int = 0,
        coverage_start: str | None = None,
        coverage_end: str | None = None,
        coverage_status: str = "complete",
        errors: Any = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        if status == "running":
            raise ValueError("a finished scan run cannot remain running")
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE scan_runs SET status = ?, finished_at = ?, discovered_files = ?, indexed_files = ?,
                    pending_files = ?, failed_files = ?, indexed_bytes = ?, coverage_start = ?,
                    coverage_end = ?, coverage_status = ?, error_json = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    finished_at or _utc_now(),
                    discovered_files,
                    indexed_files,
                    pending_files,
                    failed_files,
                    indexed_bytes,
                    coverage_start,
                    coverage_end,
                    coverage_status,
                    _json(errors, []),
                    scan_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise EffectStoreError(f"scan run is missing or already finished: {scan_run_id}")
        return self.get_scan_run(scan_run_id)

    def get_scan_run(self, scan_run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
        if row is None:
            raise KeyError(scan_run_id)
        return self._row(row)

    def upsert_log_file(
        self,
        source: str,
        stable_key: str,
        *,
        log_file_id: str | None = None,
        session_header_id: str | None = None,
        metadata: Any = None,
        seen_at: str | None = None,
    ) -> dict[str, Any]:
        now = seen_at or _utc_now()
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO log_files(id, source, stable_key, session_header_id, first_seen_at, last_seen_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, stable_key) DO UPDATE SET
                    session_header_id = COALESCE(excluded.session_header_id, log_files.session_header_id),
                    last_seen_at = excluded.last_seen_at, deleted_at = NULL,
                    metadata_json = excluded.metadata_json
                """,
                (log_file_id or _new_id(), source, stable_key, session_header_id, now, now, _json(metadata)),
            )
            row = self.connection.execute(
                "SELECT * FROM log_files WHERE source = ? AND stable_key = ?", (source, stable_key)
            ).fetchone()
        return self._row(row)

    def upsert_generation(
        self,
        log_file_id: str,
        generation_key: str,
        parser_version: str,
        *,
        generation_id: str | None = None,
        scan_run_id: str | None = None,
        session_header_id: str | None = None,
        device: str | None = None,
        inode: str | None = None,
        observed_size: int = 0,
        observed_mtime_ns: int | None = None,
        header_hash: str | None = None,
        status: str = "active",
        metadata: Any = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        now = observed_at or _utc_now()
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO log_file_generations(
                    id, log_file_id, generation_key, scan_run_id, parser_version, session_header_id,
                    device, inode, observed_size, observed_mtime_ns, header_hash, started_at, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(log_file_id, generation_key) DO UPDATE SET
                    scan_run_id = COALESCE(excluded.scan_run_id, log_file_generations.scan_run_id),
                    parser_version = excluded.parser_version,
                    session_header_id = COALESCE(excluded.session_header_id, log_file_generations.session_header_id),
                    device = COALESCE(excluded.device, log_file_generations.device),
                    inode = COALESCE(excluded.inode, log_file_generations.inode),
                    observed_size = excluded.observed_size,
                    observed_mtime_ns = excluded.observed_mtime_ns,
                    header_hash = COALESCE(excluded.header_hash, log_file_generations.header_hash),
                    status = excluded.status, metadata_json = excluded.metadata_json
                """,
                (
                    generation_id or _new_id(), log_file_id, generation_key, scan_run_id, parser_version,
                    session_header_id, device, inode, observed_size, observed_mtime_ns, header_hash, now,
                    status, _json(metadata),
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM log_file_generations WHERE log_file_id = ? AND generation_key = ?",
                (log_file_id, generation_key),
            ).fetchone()
        return self._row(row)

    def upsert_location(
        self, generation_id: str, path: str | os.PathLike[str], *, observed_at: str | None = None
    ) -> dict[str, Any]:
        now = observed_at or _utc_now()
        normalized = str(Path(path).expanduser().resolve())
        with self.transaction():
            self.connection.execute(
                "UPDATE log_file_locations SET is_current = 0 WHERE generation_id = ? AND path <> ?",
                (generation_id, normalized),
            )
            self.connection.execute(
                """
                INSERT INTO log_file_locations(generation_id, path, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(generation_id, path) DO UPDATE SET last_seen_at = excluded.last_seen_at, is_current = 1
                """,
                (generation_id, normalized, now, now),
            )
            row = self.connection.execute(
                "SELECT * FROM log_file_locations WHERE generation_id = ? AND path = ?", (generation_id, normalized)
            ).fetchone()
        return self._row(row)

    def save_checkpoint(
        self,
        generation_id: str,
        byte_offset: int,
        *,
        line_number: int = 0,
        prefix_hash: str | None = None,
        cursor_window_hash: str | None = None,
        sparse_hashes: Any = None,
        complete_line: bool = True,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO file_checkpoints(
                    generation_id, byte_offset, line_number, prefix_hash, cursor_window_hash,
                    sparse_hashes_json, complete_line, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(generation_id, byte_offset) DO UPDATE SET
                    line_number = excluded.line_number, prefix_hash = excluded.prefix_hash,
                    cursor_window_hash = excluded.cursor_window_hash,
                    sparse_hashes_json = excluded.sparse_hashes_json,
                    complete_line = excluded.complete_line, created_at = excluded.created_at
                """,
                (
                    generation_id, byte_offset, line_number, prefix_hash, cursor_window_hash,
                    _json(sparse_hashes, []), int(complete_line), created_at or _utc_now(),
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM file_checkpoints WHERE generation_id = ? AND byte_offset = ?",
                (generation_id, byte_offset),
            ).fetchone()
        return self._row(row)

    def latest_checkpoint(self, generation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM file_checkpoints WHERE generation_id = ? ORDER BY byte_offset DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        return self._row(row) if row else None

    def upsert_event(
        self,
        event_fingerprint: str,
        *,
        source: str,
        session_family: str,
        event_type: str,
        payload_hash: str,
        payload: Any,
        event_id: str | None = None,
        source_event_id: str | None = None,
        protocol_time: str | None = None,
        parent_event_id: str | None = None,
        call_id: str | None = None,
        scan_run_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        now = observed_at or _utc_now()
        with self.transaction():
            existing = self.connection.execute(
                """SELECT source, session_family, event_type, payload_hash
                   FROM canonical_events WHERE event_fingerprint = ?""",
                (event_fingerprint,),
            ).fetchone()
            identity = (source, session_family, event_type, payload_hash)
            if existing is not None and tuple(existing) != identity:
                raise EffectStoreError(f"canonical event fingerprint conflict: {event_fingerprint}")
            self.connection.execute(
                """
                INSERT INTO canonical_events(
                    id, event_fingerprint, source, session_family, source_event_id, event_type,
                    protocol_time, parent_event_id, call_id, payload_hash, payload_json,
                    first_seen_scan_id, last_seen_scan_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_fingerprint) DO UPDATE SET
                    source_event_id = COALESCE(excluded.source_event_id, canonical_events.source_event_id),
                    protocol_time = COALESCE(excluded.protocol_time, canonical_events.protocol_time),
                    parent_event_id = COALESCE(excluded.parent_event_id, canonical_events.parent_event_id),
                    call_id = COALESCE(excluded.call_id, canonical_events.call_id),
                    last_seen_scan_id = COALESCE(excluded.last_seen_scan_id, canonical_events.last_seen_scan_id),
                    updated_at = excluded.updated_at
                """,
                (
                    event_id or _new_id(), event_fingerprint, source, session_family, source_event_id,
                    event_type, protocol_time, parent_event_id, call_id, payload_hash, _json(payload),
                    scan_run_id, scan_run_id, now, now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM canonical_events WHERE event_fingerprint = ?", (event_fingerprint,)
            ).fetchone()
        return self._row(row)

    def upsert_provenance(
        self,
        event_id: str,
        generation_id: str,
        byte_start: int,
        *,
        byte_end: int | None = None,
        line_number: int | None = None,
        locator: Any = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO event_provenance(
                    event_id, generation_id, byte_start, byte_end, line_number, locator_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, generation_id, byte_start) DO UPDATE SET
                    byte_end = excluded.byte_end, line_number = excluded.line_number,
                    locator_json = excluded.locator_json, observed_at = excluded.observed_at
                """,
                (event_id, generation_id, byte_start, byte_end, line_number, _json(locator), observed_at or _utc_now()),
            )
            row = self.connection.execute(
                """SELECT * FROM event_provenance
                   WHERE event_id = ? AND generation_id = ? AND byte_start = ?""",
                (event_id, generation_id, byte_start),
            ).fetchone()
        return self._row(row)

    def delete_generation(self, generation_id: str) -> bool:
        """Delete derived generation data while retaining canonical and manual records."""
        with self.transaction():
            cursor = self.connection.execute("DELETE FROM log_file_generations WHERE id = ?", (generation_id,))
        return cursor.rowcount == 1

    def get_event(self, event_id_or_fingerprint: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT e.*, COUNT(p.id) AS provenance_count
            FROM canonical_events e LEFT JOIN event_provenance p ON p.event_id = e.id
            WHERE e.id = ? OR e.event_fingerprint = ? GROUP BY e.id
            """,
            (event_id_or_fingerprint, event_id_or_fingerprint),
        ).fetchone()
        if row is None:
            raise KeyError(event_id_or_fingerprint)
        return self._row(row)

    def list_events(
        self,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        source: str | None = None,
        event_type: str | None = None,
        orphaned: bool | None = None,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), MAX_PAGE_SIZE)
        conditions: list[str] = []
        params: list[Any] = []
        if cursor:
            conditions.append("e.id > ?")
            params.append(cursor)
        if source:
            conditions.append("e.source = ?")
            params.append(source)
        if event_type:
            conditions.append("e.event_type = ?")
            params.append(event_type)
        if orphaned is not None:
            conditions.append("e.orphaned = ?")
            params.append(int(orphaned))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.connection.execute(
            f"""
            SELECT e.*, COUNT(p.id) AS provenance_count
            FROM canonical_events e LEFT JOIN event_provenance p ON p.event_id = e.id
            {where} GROUP BY e.id ORDER BY e.id LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        return {
            "items": [self._row(row) for row in selected],
            "next_cursor": selected[-1]["id"] if has_more and selected else None,
        }

    def create_actor(
        self,
        display_name: str,
        *,
        actor_id: str | None = None,
        external_subject: str | None = None,
        roles: Sequence[str] = (),
    ) -> dict[str, Any]:
        now = _utc_now()
        actor_id = actor_id or _new_id()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO actors(id, external_subject, display_name, roles_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (actor_id, external_subject, display_name, _json(list(roles), []), now, now),
            )
        return self._get("actors", actor_id)

    def create_session(
        self, source: str, source_session_id: str, *, session_family: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        session_id = session_id or _new_id()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO sessions(id, source, session_family, source_session_id)
                   VALUES (?, ?, ?, ?)""",
                (session_id, source, session_family or source_session_id, source_session_id),
            )
        return self._get("sessions", session_id)

    def create_task_case(
        self, case_fingerprint: str, *, task_type: str | None = None, task_case_id: str | None = None, metadata: Any = None
    ) -> dict[str, Any]:
        task_case_id = task_case_id or _new_id()
        now = _utc_now()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO task_cases(id, case_fingerprint, task_type, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_case_id, case_fingerprint, task_type, _json(metadata), now, now),
            )
        return self._get("task_cases", task_case_id)

    def create_assessment_revision(
        self,
        task_case_id: str,
        *,
        expected_revision: int,
        case_revision: int | None = None,
        assessment_id: str | None = None,
        contract_version_id: str | None = None,
        classification_revision: int | None = None,
        parser_version: str | None = None,
        checker_version: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        rubric_version: str | None = None,
        process_state: str = "discovered",
        assessability: str = "needs-evidence",
        automated_verdict: str = "unset",
        conflict_state: str = "none",
        freshness: str = "unknown",
        rationale: Any = None,
    ) -> dict[str, Any]:
        assessment_id = assessment_id or _new_id()
        next_revision = expected_revision + 1
        with self.transaction():
            case = self.connection.execute(
                "SELECT current_revision, current_assessment_revision FROM task_cases WHERE id = ?", (task_case_id,)
            ).fetchone()
            if case is None:
                raise KeyError(task_case_id)
            if case["current_assessment_revision"] != expected_revision:
                raise RevisionConflict(
                    f"assessment revision conflict: expected {expected_revision}, "
                    f"current {case['current_assessment_revision']}"
                )
            selected_case_revision = case["current_revision"] if case_revision is None else case_revision
            if selected_case_revision != case["current_revision"]:
                raise RevisionConflict(
                    f"task case revision conflict: selected {selected_case_revision}, current {case['current_revision']}"
                )
            self.connection.execute(
                """
                INSERT INTO outcome_assessments(
                    id, task_case_id, revision, case_revision, contract_version_id, classification_revision,
                    parser_version, checker_version, model_version, prompt_version, rubric_version,
                    process_state, assessability, automated_verdict, conflict_state, freshness,
                    rationale_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id, task_case_id, next_revision, selected_case_revision,
                    contract_version_id, classification_revision, parser_version, checker_version,
                    model_version, prompt_version, rubric_version, process_state, assessability,
                    automated_verdict, conflict_state, freshness, _json(rationale), _utc_now(),
                ),
            )
            cursor = self.connection.execute(
                """UPDATE task_cases SET current_assessment_revision = ?, updated_at = ?
                   WHERE id = ? AND current_assessment_revision = ?""",
                (next_revision, _utc_now(), task_case_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("assessment revision changed during write")
        return self._get("outcome_assessments", assessment_id)

    revise_assessment = create_assessment_revision

    def create_review_task(
        self,
        task_case_id: str,
        assessment_id: str,
        queue_reason: str,
        *,
        review_task_id: str | None = None,
    ) -> dict[str, Any]:
        review_task_id = review_task_id or _new_id()
        now = _utc_now()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO review_tasks(
                       id, task_case_id, assessment_id, queue_reason, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (review_task_id, task_case_id, assessment_id, queue_reason, now, now),
            )
        return self._get("review_tasks", review_task_id)

    def write_manual_decision(
        self,
        review_task_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        action: str = "decision",
        verdict: str = "unset",
        reason_code: str,
        note: str | None = None,
        binding: Any = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        decision_id = decision_id or _new_id()
        next_revision = expected_revision + 1
        with self.transaction():
            task = self.connection.execute(
                "SELECT * FROM review_tasks WHERE id = ?", (review_task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(review_task_id)
            if task["current_decision_revision"] != expected_revision:
                raise RevisionConflict(
                    f"manual decision revision conflict: expected {expected_revision}, "
                    f"current {task['current_decision_revision']}"
                )
            assessment = self.connection.execute(
                "SELECT * FROM outcome_assessments WHERE id = ?", (task["assessment_id"],)
            ).fetchone()
            if assessment is None:
                raise EffectStoreError("review task has no assessment")
            previous = self.connection.execute(
                "SELECT id FROM manual_decisions WHERE review_task_id = ? ORDER BY revision DESC LIMIT 1",
                (review_task_id,),
            ).fetchone()
            binding_value = dict(binding or {})
            binding_value["assessmentRevision"] = assessment["revision"]
            binding_value["caseRevision"] = assessment["case_revision"]
            binding_value["contractVersionId"] = assessment["contract_version_id"]
            self.connection.execute(
                """
                INSERT INTO manual_decisions(
                    id, review_task_id, task_case_id, assessment_id, actor_id, revision,
                    action, verdict, reason_code, note, binding_json, supersedes_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id, review_task_id, task["task_case_id"], task["assessment_id"], actor_id,
                    next_revision, action, verdict, reason_code, note, _json(binding_value),
                    previous["id"] if previous else None, _utc_now(),
                ),
            )
            cursor = self.connection.execute(
                """UPDATE review_tasks SET current_decision_revision = ?, status = ?, updated_at = ?
                   WHERE id = ? AND current_decision_revision = ?""",
                (
                    next_revision, "open" if action == "withdraw" else "decided", _utc_now(),
                    review_task_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("manual decision revision changed during write")
        return self._get("manual_decisions", decision_id)

    append_manual_decision = write_manual_decision

    def append_correction(
        self,
        task_case_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        correction_type: str,
        reason_code: str,
        assessment_id: str | None = None,
        payload: Any = None,
        correction_id: str | None = None,
    ) -> dict[str, Any]:
        correction_id = correction_id or _new_id()
        next_revision = expected_revision + 1
        with self.transaction():
            current = self.connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM corrections WHERE task_case_id = ?",
                (task_case_id,),
            ).fetchone()[0]
            if current != expected_revision:
                raise RevisionConflict(
                    f"correction revision conflict: expected {expected_revision}, current {current}"
                )
            previous = self.connection.execute(
                "SELECT id FROM corrections WHERE task_case_id = ? ORDER BY revision DESC LIMIT 1",
                (task_case_id,),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO corrections(
                       id, task_case_id, assessment_id, actor_id, revision, correction_type,
                       reason_code, payload_json, supersedes_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    correction_id, task_case_id, assessment_id, actor_id, next_revision,
                    correction_type, reason_code, _json(payload), previous["id"] if previous else None,
                    _utc_now(),
                ),
            )
        row = self.connection.execute("SELECT * FROM corrections WHERE id = ?", (correction_id,)).fetchone()
        return self._row(row)

    write_correction = append_correction

    def append_exception(
        self,
        task_case_id: str,
        *,
        assessment_id: str,
        actor_id: str,
        expected_revision: int,
        reason_code: str,
        scope: Any = None,
        expires_at: str | None = None,
        exception_id: str | None = None,
    ) -> dict[str, Any]:
        exception_id = exception_id or _new_id()
        next_revision = expected_revision + 1
        with self.transaction():
            assessment = self.connection.execute(
                "SELECT task_case_id FROM outcome_assessments WHERE id = ?", (assessment_id,)
            ).fetchone()
            if assessment is None:
                raise KeyError(assessment_id)
            if assessment["task_case_id"] != task_case_id:
                raise EffectStoreError("exception assessment belongs to another task case")
            current = self.connection.execute(
                'SELECT COALESCE(MAX(revision), 0) FROM "exceptions" WHERE task_case_id = ?',
                (task_case_id,),
            ).fetchone()[0]
            if current != expected_revision:
                raise RevisionConflict(
                    f"exception revision conflict: expected {expected_revision}, current {current}"
                )
            previous = self.connection.execute(
                'SELECT id FROM "exceptions" WHERE task_case_id = ? ORDER BY revision DESC LIMIT 1',
                (task_case_id,),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO exceptions(
                       id, task_case_id, assessment_id, actor_id, revision, reason_code,
                       scope_json, expires_at, supersedes_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    exception_id, task_case_id, assessment_id, actor_id, next_revision, reason_code,
                    _json(scope), expires_at, previous["id"] if previous else None, _utc_now(),
                ),
            )
        row = self.connection.execute('SELECT * FROM "exceptions" WHERE id = ?', (exception_id,)).fetchone()
        return self._row(row)

    write_exception = append_exception

    def list_review_tasks(
        self, *, status: str | None = None, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), MAX_PAGE_SIZE)
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if cursor:
            conditions.append("id > ?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.connection.execute(
            f"SELECT * FROM review_tasks {where} ORDER BY id LIMIT ?", (*params, limit + 1)
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [self._row(row) for row in rows],
            "next_cursor": rows[-1]["id"] if has_more and rows else None,
        }

    def overview(self) -> dict[str, Any]:
        with self.transaction(immediate=False):
            row = self.connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_events) AS event_count,
                    (SELECT COUNT(*) FROM canonical_events WHERE orphaned = 1) AS orphaned_event_count,
                    (SELECT COUNT(*) FROM log_file_generations) AS generation_count,
                    (SELECT COUNT(*) FROM task_cases WHERE invalidated_at IS NULL) AS task_case_count,
                    (SELECT COUNT(*) FROM review_tasks WHERE status = 'open') AS open_review_count,
                    (SELECT COUNT(*) FROM outcome_assessments a JOIN task_cases c ON c.id = a.task_case_id
                        WHERE a.revision = c.current_assessment_revision AND a.assessability = 'assessable')
                        AS assessable_count,
                    (SELECT COUNT(*) FROM outcome_assessments a JOIN task_cases c ON c.id = a.task_case_id
                        WHERE a.revision = c.current_assessment_revision AND a.assessability = 'needs-evidence')
                        AS needs_evidence_count,
                    (SELECT COUNT(*) FROM manual_decisions) AS manual_decision_count
                """
            ).fetchone()
            latest = self.connection.execute(
                "SELECT * FROM scan_runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
        result = self._row(row)
        result["latest_scan"] = self._row(latest) if latest else None
        return result

    get_overview = overview

    def create_metric_snapshot(
        self,
        *,
        cutoff_at: str,
        coverage_status: str,
        dimensions: Any,
        versions: Any,
        cases: Iterable[Mapping[str, Any]],
        scan_run_id: str | None = None,
        summary: Any = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot_id = snapshot_id or _new_id()
        with self.transaction():
            self.connection.execute(
                """INSERT INTO metric_snapshots(
                       id, scan_run_id, cutoff_at, coverage_status, dimensions_json,
                       versions_json, summary_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, scan_run_id, cutoff_at, coverage_status, _json(dimensions),
                    _json(versions), _json(summary), _utc_now(),
                ),
            )
            for case in cases:
                self.connection.execute(
                    """
                    INSERT INTO metric_snapshot_cases(
                        snapshot_id, task_case_id, task_case_revision, assessment_id,
                        assessment_revision, manual_decision_id, manual_decision_revision,
                        skill_id, skill_sha256, contract_version_id, task_type, attribution_kind,
                        effective_verdict, metric_eligible, exclusion_reason, frozen_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id, case["task_case_id"], case["task_case_revision"],
                        case.get("assessment_id"), case.get("assessment_revision"),
                        case.get("manual_decision_id"), case.get("manual_decision_revision"),
                        case["skill_id"], case.get("skill_sha256"), case.get("contract_version_id"),
                        case.get("task_type"), case["attribution_kind"], case["effective_verdict"],
                        int(bool(case["metric_eligible"])), case.get("exclusion_reason"),
                        _json(case.get("frozen")),
                    ),
                )
            self.connection.execute("UPDATE metric_snapshots SET sealed = 1 WHERE id = ?", (snapshot_id,))
        return self.get_metric_snapshot(snapshot_id)

    def get_metric_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = self.connection.execute(
            "SELECT * FROM metric_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise KeyError(snapshot_id)
        cases = self.connection.execute(
            """SELECT * FROM metric_snapshot_cases WHERE snapshot_id = ?
               ORDER BY task_case_id, skill_id, attribution_kind""",
            (snapshot_id,),
        ).fetchall()
        result = self._row(snapshot)
        result["cases"] = [self._row(row) for row in cases]
        return result

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Execute SQL for adapters; writes should be enclosed in ``transaction``."""
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.IntegrityError as exc:
            if "metric snapshot is immutable" in str(exc):
                raise ImmutableSnapshotError(str(exc)) from exc
            raise

    def _get(self, table: str, row_id: str) -> dict[str, Any]:
        allowed = {
            "actors", "sessions", "task_cases", "outcome_assessments", "review_tasks", "manual_decisions"
        }
        if table not in allowed:
            raise ValueError(table)
        row = self.connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(row_id)
        return self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json") and isinstance(result[key], str):
                try:
                    result[key[:-5]] = json.loads(result.pop(key))
                except json.JSONDecodeError:
                    pass
        return result


def open_effect_store(
    path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000
) -> EffectStore:
    return EffectStore(path, busy_timeout_ms=busy_timeout_ms)
