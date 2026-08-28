"""High-confidence outcome review orchestration for Codex and Pi JSONL logs.
The service intentionally contains no web-framework or third-party dependencies.
It normalizes logs through :mod:`effect_adapters`, persists schema-v3 records via
:class:`effect_store.EffectStore`, and evaluates only exact-SHA contracts.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from auth import REDACTED, redact_sensitive
from effect_adapters import (
    NormalizedEvent,
    event_fingerprint,
    extract_task_facts,
    parse_jsonl_line,
)
from effect_store import EffectStore, EffectStoreError, RevisionConflict
from feedback_service import FeedbackService
from outcome_contracts import OutcomeContractInterpreter, OutcomeContractStore
from quality_service import SkillQualityService
from semantic_reviewer import SCHEMA_VERSION as SEMANTIC_SCHEMA_VERSION

PARSER_VERSION = "outcome-reviews-v1"
_WINDOW = 4096
_SAMPLE = 4096
_READ_TOOLS = {"read", "read_file", "read_text", "read_text_file", "read_mcp_resource"}
_SHELL_TOOLS = {"bash", "shell", "exec_command"}
_READ_COMMAND = re.compile(r"(?i)\b(?:cat|sed|rg|type|get-content|gc|open|read_file|readtext)\b")
_SKILL_PATH = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[/\\][^\s\"'<>|]*?SKILL\.md|(?:\.{0,2}[/\\])?[^\s\"'<>|]+[/\\]SKILL\.md)",
    re.IGNORECASE,
)
_ABS_PATH = re.compile(r"(?<![\w.])/(?:[^\s\"'<>]+/)*[^\s\"'<>]*")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[A-Z]:\\)(?:[^\s\"'<>]+\\)*[^\s\"'<>]*")
_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^\s\"'<>]+")
_PI_SKILL_BLOCK = re.compile(r"<skill\b(?P<attrs>[^>]*)>(?P<body>.*?)</skill\s*>", re.IGNORECASE | re.DOTALL)
_HTML_ATTRIBUTE = re.compile(r"(?P<name>[\w:-]+)\s*=\s*(?P<quote>['\"])(?P<value>.*?)\2", re.DOTALL)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def _digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else _canonical(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def _stable_id(*parts: Any) -> str:
    return hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()

def _json(value: Any) -> str:
    return _canonical({} if value is None else value)

def _decode(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json") and isinstance(result[key], str):
            try:
                result[key[:-5]] = json.loads(result.pop(key))
            except json.JSONDecodeError:
                pass
    return result

def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)

def _safe_public(value: Any) -> Any:
    """Remove absolute filesystem paths from service query responses."""
    if isinstance(value, Mapping):
        return {
            str(_safe_public(str(key))): _safe_public(item)
            for key, item in value.items() if key != "path"
        }
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_safe_public(item) for item in value)
    if isinstance(value, str):
        if os.path.isabs(value):
            return f"<absolute-path>/{Path(value).name}"
        if _ABS_PATH.search(value) or _WINDOWS_PATH.search(value) or _UNC_PATH.search(value):
            return "<redacted-path-text>"
    return value

def _redact_for_storage(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = redact_sensitive(str(key))
            sensitive_key = re.search(r"(?i)(?:token|secret|password|api[_-]?key|authorization)", str(key))
            result[safe_key] = REDACTED if sensitive_key else _redact_for_storage(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_for_storage(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value)
    return value

class OutcomeReviewService:
    """Index logs, derive task records, and drive deterministic outcome reviews."""
    def __init__(
        self,
        store: EffectStore | str | os.PathLike[str],
        contract_store: OutcomeContractStore | str | os.PathLike[str] | None = None,
        *,
        skills_db_path: str | os.PathLike[str] | None = None,
        skill_roots: Sequence[str | os.PathLike[str]] = (),
        parser_version: str = PARSER_VERSION,
        quality_eligible_skill_versions: Mapping[str, str] | None = None,
    ) -> None:
        self._owns_store = not isinstance(store, EffectStore)
        self.store = store if isinstance(store, EffectStore) else EffectStore(store)
        contracts = contract_store or skills_db_path
        if isinstance(contracts, OutcomeContractStore):
            self.contracts = contracts
        elif contracts is not None:
            self.contracts = OutcomeContractStore(contracts)
        else:
            self.contracts = None
        self.skill_roots = tuple(Path(root).expanduser().resolve() for root in skill_roots)
        self.parser_version = str(parser_version)
        self.interpreter = OutcomeContractInterpreter()
        self.feedback = FeedbackService(self.store)
        self.quality = SkillQualityService(
            self.store, self.contracts, self.feedback,
            eligible_skill_versions=quality_eligible_skill_versions,
        )
    def close(self) -> None:
        if self._owns_store:
            self.store.close()
    def __enter__(self) -> "OutcomeReviewService":
        return self
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
    # ------------------------------------------------------------------ scan
    def scan(
        self,
        sources: Mapping[str, Any] | Iterable[Any],
        *,
        budget_bytes: int | None = None,
        budget_seconds: float | None = None,
        max_bytes: int | None = None,
        max_seconds: float | None = None,
        time_budget: float | None = None,
        budget: Mapping[str, Any] | None = None,
        mode: str = "incremental",
        scope_kind: str = "ad-hoc",
    ) -> dict[str, Any]:
        """Incrementally scan source paths under byte and elapsed-time budgets.
        ``sources`` accepts ``{"codex": path, "pi": [paths...]}``, ``(source,
        path)`` pairs, or dictionaries with ``source`` and ``path`` keys.
        Budgets apply to bytes offered to the JSONL parser. Validation reads are
        bounded fixed windows except when a same-size file has changed metadata.
        """
        configured = dict(budget or {})
        byte_limit = budget_bytes if budget_bytes is not None else (
            max_bytes if max_bytes is not None else configured.get("bytes")
        )
        seconds = budget_seconds if budget_seconds is not None else (
            time_budget if time_budget is not None else (
                max_seconds if max_seconds is not None else configured.get("seconds", configured.get("time"))
            )
        )
        if byte_limit is not None and int(byte_limit) < 0:
            raise ValueError("budget_bytes must be non-negative")
        if seconds is not None and float(seconds) < 0:
            raise ValueError("budget_seconds must be non-negative")
        entries = self._source_entries(sources)
        if scope_kind not in {"ad-hoc", "configured-catalog"}:
            raise ValueError("scope_kind must be ad-hoc or configured-catalog")
        if mode not in {"incremental", "rebuild", "full"}:
            raise ValueError("mode must be incremental, rebuild, or full")
        source_name = entries[0][0] if len({item[0] for item in entries}) == 1 else "multi"
        run = self.store.create_scan_run(
            source_name or "multi", mode=mode,
            metadata={
                "parser": self.parser_version, "scopeKind": scope_kind,
                "scopeFingerprint": _digest(sorted(
                    (source, hashlib.sha256(str(path).encode("utf-8")).hexdigest())
                    for source, path in entries
                )),
            },
        )
        feedback_before = self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0]
        started = time.monotonic()
        files, scopes, discovery_errors = self._discover(entries)
        files = self._prioritize_unindexed_files(files)
        indexed_files = indexed_bytes = failed_files = 0
        pending_files = 0
        errors: list[dict[str, Any]] = list(discovery_errors)
        discovered_paths = {str(path.resolve()) for _source, path in files}
        for index, (source, path) in enumerate(files):
            elapsed = time.monotonic() - started
            remaining = None if byte_limit is None else max(0, int(byte_limit) - indexed_bytes)
            if (seconds is not None and elapsed >= seconds) or remaining == 0:
                pending_files += len(files) - index
                break
            try:
                outcome = self._scan_file(
                    source, path, run["id"], remaining, started, seconds,
                    force_rebuild=mode in {"rebuild", "full"},
                )
                indexed_bytes += outcome["bytes"]
                if outcome["pending"]:
                    pending_files += 1
                else:
                    indexed_files += 1
            except Exception as exc:  # A bad file must not leave its scan run in "running".
                self._cleanup_failed_generation(run["id"], source, path)
                failed_files += 1
                errors.append({"source": source, "file": path.name, "error": type(exc).__name__, "message": str(exc)[:300]})
        remaining_seconds = (
            max(0.0, float(seconds) - (time.monotonic() - started))
            if seconds is not None else 1.0
        )
        missing_results = self._finalize_stale_missing_tool_results(
            max_seconds=min(1.0, remaining_seconds)
        )
        complete_enumeration = (
            not discovery_errors and pending_files == 0 and failed_files == 0
            and not missing_results["pending"]
        )
        if complete_enumeration:
            self._delete_missing(scopes, discovered_paths)
        self._derive_candidate_attributions()
        status = "completed" if complete_enumeration else ("failed" if failed_files and not indexed_files else "partial")
        finished = self.store.finish_scan_run(
            run["id"],
            status=status,
            discovered_files=len(files),
            indexed_files=indexed_files,
            pending_files=pending_files,
            failed_files=failed_files + len(discovery_errors),
            indexed_bytes=indexed_bytes,
            coverage_status="complete" if complete_enumeration else "partial",
            errors=errors,
        )
        feedback_state = self.feedback.process_changes(
            max_changes=5000,
            max_seconds=min(2.0, max(0.0, float(seconds))) if seconds is not None else 2.0,
            last_scan_run_id=run["id"],
        )
        feedback_after = self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0]
        finished["feedback"] = {
            **feedback_state,
            "newSignals": max(0, feedback_after - feedback_before),
            "missingResults": missing_results,
            "overview": self.feedback.overview(),
        }
        return _safe_public(finished)
    scan_incremental = scan

    def _prioritize_unindexed_files(self, files: Sequence[tuple[str, Path]]) -> list[tuple[str, Path]]:
        """Scan missing or incomplete checkpoints before already-complete history."""
        complete_sizes = {
            (row["source"], row["path"]): int(row["observed_size"])
            for row in self.store.execute(
                """SELECT f.source, location.path, generation.observed_size
                   FROM log_file_locations location
                   JOIN log_file_generations generation ON generation.id=location.generation_id
                   JOIN log_files f ON f.id=generation.log_file_id
                   WHERE location.is_current=1 AND generation.status='active'
                     AND generation.parser_version=?
                     AND EXISTS (SELECT 1 FROM file_checkpoints checkpoint
                                 WHERE checkpoint.generation_id=generation.id
                                   AND checkpoint.byte_offset>=generation.observed_size)""",
                (self.parser_version,),
            ).fetchall()
        }
        def is_complete(item: tuple[str, Path]) -> bool:
            try:
                observed_size = complete_sizes.get((item[0], str(item[1].resolve())))
                return observed_size is not None and observed_size >= item[1].stat().st_size
            except OSError:
                return False

        pending = [item for item in files if not is_complete(item)]
        finished = [item for item in files if is_complete(item)]
        source_recency = {
            row["source"]: str(row["last_scanned"] or "")
            for row in self.store.execute(
                """SELECT file.source, MAX(COALESCE(run.started_at, generation.started_at)) AS last_scanned
                   FROM log_files file JOIN log_file_generations generation ON generation.log_file_id=file.id
                   LEFT JOIN scan_runs run ON run.id=generation.scan_run_id
                   GROUP BY file.source"""
            ).fetchall()
        }

        def interleave(items: Sequence[tuple[str, Path]]) -> list[tuple[str, Path]]:
            buckets = {
                source: sorted((item for item in items if item[0] == source), key=lambda item: str(item[1]))
                for source in sorted({item[0] for item in items})
            }
            ordered: list[tuple[str, Path]] = []
            index = 0
            while True:
                added = False
                for source in sorted(buckets, key=lambda value: (source_recency.get(value, ""), value)):
                    if index < len(buckets[source]):
                        ordered.append(buckets[source][index])
                        added = True
                if not added:
                    return ordered
                index += 1

        return interleave(pending) + interleave(finished)
    def _source_entries(self, sources: Mapping[str, Any] | Iterable[Any]) -> list[tuple[str, Path]]:
        raw: list[tuple[Any, Any]] = []
        if isinstance(sources, Mapping):
            if "source" in sources and ("path" in sources or "root" in sources):
                raw.append((sources["source"], sources.get("path", sources.get("root"))))
            else:
                for source, paths in sources.items():
                    values = paths if isinstance(paths, (list, tuple, set)) else [paths]
                    raw.extend((source, path) for path in values)
        else:
            for item in sources:
                if isinstance(item, Mapping):
                    raw.append((item.get("source"), item.get("path", item.get("root"))))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    raw.append((item[0], item[1]))
                else:
                    raise ValueError("each scan source must include source and path")
        entries: list[tuple[str, Path]] = []
        for source, path in raw:
            normalized = str(source or "").strip().lower()
            if normalized not in {"codex", "pi"}:
                raise ValueError(f"unsupported session source: {source}")
            if path is None:
                raise ValueError("scan source path is required")
            entries.append((normalized, Path(path).expanduser().resolve()))
        if not entries:
            raise ValueError("at least one scan source is required")
        return entries
    def _discover(self, entries: Sequence[tuple[str, Path]]) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]], list[dict[str, Any]]]:
        found: dict[tuple[str, str], Path] = {}
        scopes: list[tuple[str, Path]] = []
        errors: list[dict[str, Any]] = []
        for source, root in entries:
            scopes.append((source, root))
            try:
                if root.is_file():
                    if root.suffix.lower() == ".jsonl":
                        found[(source, str(root))] = root
                elif root.is_dir():
                    for path in root.rglob("*.jsonl"):
                        if path.is_file():
                            found[(source, str(path.resolve()))] = path.resolve()
                elif root.exists():
                    errors.append({"source": source, "file": root.name, "error": "unsupported-path"})
                else:
                    errors.append({"source": source, "file": root.name, "error": "missing-root"})
            except OSError as exc:
                errors.append({"source": source, "file": root.name, "error": type(exc).__name__, "message": str(exc)[:300]})
        return [(source, found[(source, path)]) for source, path in sorted(found)], scopes, errors
    def _cleanup_failed_generation(self, scan_run_id: str, source: str, path: Path) -> None:
        rows = self.store.execute(
            """SELECT g.id FROM log_file_generations g JOIN log_files f ON f.id=g.log_file_id JOIN log_file_locations l ON l.generation_id=g.id WHERE g.scan_run_id=? AND f.source=? AND l.path=? AND NOT EXISTS (SELECT 1 FROM file_checkpoints c WHERE c.generation_id=g.id)""",
            (scan_run_id, source, str(path)),
        ).fetchall()
        for row in rows:
            self.store.delete_generation(row["id"])
    def _header(self, source: str, path: Path) -> tuple[str, str, str, str]:
        with path.open("rb") as handle:
            first = handle.readline(64 * 1024)
        complete = first if first.endswith(b"\n") else b""
        header_hash = _digest(complete) if complete else ""
        session_id = family = ""
        if complete:
            try:
                item = json.loads(complete)
            except (UnicodeDecodeError, json.JSONDecodeError):
                item = {}
            if not isinstance(item, Mapping):
                item = {}
            if source == "codex" and item.get("type") == "session_meta":
                payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
                session_id = str(payload.get("id") or payload.get("session_id") or "")
                family = str(payload.get("parent_thread_id") or session_id)
            elif source == "pi" and item.get("type") == "session":
                session_id = str(item.get("id") or "")
                family = str(item.get("parentSession") or item.get("parent_session") or session_id)
        return session_id, family, header_hash, _digest(first)
    def _scan_file(
        self,
        source: str,
        path: Path,
        scan_run_id: str,
        remaining: int | None,
        started: float,
        seconds: float | None,
        *,
        force_rebuild: bool = False,
    ) -> dict[str, Any]:
        stat = path.stat()
        old_location = self.store.execute(
            """SELECT f.*, g.id AS generation_id, g.generation_key, g.parser_version,
                      g.observed_size, g.observed_mtime_ns, g.header_hash,
                      g.metadata_json AS generation_metadata_json
               FROM log_file_locations l JOIN log_file_generations g ON g.id = l.generation_id
               JOIN log_files f ON f.id = g.log_file_id
               WHERE l.path = ? AND l.is_current = 1 AND f.source = ?
               ORDER BY g.started_at DESC LIMIT 1""",
            (str(path), source),
        ).fetchone()
        if old_location is not None and not force_rebuild and old_location["parser_version"] == self.parser_version:
            old_metadata = json.loads(old_location["generation_metadata_json"])
            old_checkpoint = self.store.latest_checkpoint(old_location["generation_id"])
            metadata_unchanged = (
                stat.st_size == old_location["observed_size"]
                and stat.st_mtime_ns == old_location["observed_mtime_ns"]
                and stat.st_ctime_ns == old_metadata.get("observed_ctime_ns")
            )
            if old_checkpoint and old_checkpoint["byte_offset"] >= stat.st_size and metadata_unchanged:
                self.store.upsert_generation(
                    old_location["id"], old_location["generation_key"], self.parser_version,
                    generation_id=old_location["generation_id"], scan_run_id=scan_run_id,
                    session_header_id=old_location["session_header_id"],
                    device=str(stat.st_dev), inode=str(stat.st_ino), observed_size=stat.st_size,
                    observed_mtime_ns=stat.st_mtime_ns, header_hash=old_location["header_hash"],
                    metadata=old_metadata,
                )
                return {"bytes": 0, "pending": False}
        session_id, family, header_hash, observed_header_hash = self._header(source, path)
        stable_key = f"session:{session_id}" if session_id else (
            old_location["stable_key"] if old_location is not None else f"inode:{stat.st_dev}:{stat.st_ino}"
        )
        log_file = self.store.upsert_log_file(source, stable_key, session_header_id=session_id or None, metadata={"kind": "jsonl"})
        generation_row = self.store.execute(
            "SELECT * FROM log_file_generations WHERE log_file_id = ? AND status = 'active' ORDER BY started_at DESC LIMIT 1",
            (log_file["id"],),
        ).fetchone()
        generation = _decode(generation_row) if generation_row else None
        checkpoint = self.store.latest_checkpoint(generation["id"]) if generation else None
        rewrite = force_rebuild or generation is None or generation["parser_version"] != self.parser_version
        metadata_changed = False
        if generation and checkpoint:
            rewrite = rewrite or stat.st_size < checkpoint["byte_offset"]
            rewrite = rewrite or bool(generation.get("header_hash") and header_hash != generation["header_hash"])
            metadata_changed = (
                stat.st_mtime_ns != generation["observed_mtime_ns"]
                or stat.st_ctime_ns != generation.get("metadata", {}).get("observed_ctime_ns")
            )
            if not rewrite and stat.st_size == generation["observed_size"] and metadata_changed and checkpoint["byte_offset"] >= stat.st_size:
                rewrite = self._file_signature(path, stat.st_size) != generation.get("metadata", {}).get("observed_hash")
            if (metadata_changed or stat.st_size != generation["observed_size"]) and not rewrite and checkpoint["byte_offset"] and not self._checkpoint_matches(path, checkpoint):
                rewrite = True
        if generation and not rewrite and checkpoint and checkpoint["byte_offset"] >= stat.st_size:
            metadata = dict(generation.get("metadata", {}))
            metadata["observed_ctime_ns"] = stat.st_ctime_ns
            self.store.upsert_generation(
                log_file["id"], generation["generation_key"], self.parser_version,
                generation_id=generation["id"], scan_run_id=scan_run_id, session_header_id=session_id or None,
                device=str(stat.st_dev), inode=str(stat.st_ino), observed_size=stat.st_size,
                observed_mtime_ns=stat.st_mtime_ns, header_hash=header_hash or observed_header_hash,
                metadata=metadata,
            )
            self.store.upsert_location(generation["id"], path)
            return {"bytes": 0, "pending": False}
        old_generation_id = generation["id"] if generation and rewrite else None
        if rewrite:
            generation_key = str(uuid.uuid4())
            generation = self.store.upsert_generation(
                log_file["id"], generation_key, self.parser_version, scan_run_id=scan_run_id,
                session_header_id=session_id or None, device=str(stat.st_dev), inode=str(stat.st_ino),
                observed_size=stat.st_size, observed_mtime_ns=stat.st_mtime_ns,
                header_hash=header_hash or observed_header_hash,
                metadata={"session_id": session_id, "session_family": family},
            )
            checkpoint = None
        assert generation is not None
        start_offset = 0 if checkpoint is None else int(checkpoint["byte_offset"])
        line_number = 0 if checkpoint is None else int(checkpoint["line_number"])
        if seconds is not None and time.monotonic() - started >= seconds:
            self.store.upsert_location(generation["id"], path)
            return {"bytes": 0, "pending": True}
        available = max(0, stat.st_size - start_offset)
        amount = available if remaining is None else min(available, remaining)
        bytes_read = completed_lines = 0
        complete_offset = start_offset
        with self.store.transaction():
            self.store.upsert_location(generation["id"], path)
            current_session = session_id or str(generation.get("metadata", {}).get("session_id") or stable_key)
            current_family = family or str(generation.get("metadata", {}).get("session_family") or current_session)
            with path.open("rb") as handle:
                handle.seek(start_offset)
                while bytes_read < amount:
                    if seconds is not None and time.monotonic() - started >= seconds:
                        break
                    raw_line = handle.readline(amount - bytes_read)
                    if not raw_line:
                        break
                    bytes_read += len(raw_line)
                    if not raw_line.endswith(b"\n"):
                        break
                    item = json.loads(raw_line)
                    events = parse_jsonl_line(
                        source, item, session_id=current_session, session_family=current_family
                    )
                    events = self._enrich_events(source, item, events)
                    for event in events:
                        if event.event_type == "session_meta":
                            current_session = event.session_id or current_session
                            current_family = event.session_family or current_family
                        canonical_event = self._persist_event(
                            event, generation["id"], complete_offset,
                            complete_offset + len(raw_line), line_number + completed_lines + 1,
                            scan_run_id, raw_line_sha256=hashlib.sha256(raw_line).hexdigest(),
                        )
                        if canonical_event["event_fingerprint"] != event.fingerprint:
                            event = replace(event, fingerprint=canonical_event["event_fingerprint"])
                        self._derive(event, canonical_event)
                    complete_offset += len(raw_line)
                    completed_lines += 1
            prefix_hash = self._prefix_hash(path, complete_offset)
            cursor_hash = self._window_hash(path, complete_offset)
            samples = self._sample_hashes(path, complete_offset)
            self.store.save_checkpoint(
                generation["id"], complete_offset, line_number=line_number + completed_lines,
                prefix_hash=prefix_hash, cursor_window_hash=cursor_hash, sparse_hashes=samples,
            )
            metadata = dict(generation.get("metadata") or {})
            metadata.update({
                "session_id": current_session, "session_family": current_family,
                "observed_ctime_ns": stat.st_ctime_ns,
            })
            if bytes_read >= available:
                metadata["observed_hash"] = self._file_signature(path, stat.st_size)
            self.store.upsert_generation(
                log_file["id"], generation["generation_key"], self.parser_version,
                generation_id=generation["id"], scan_run_id=scan_run_id,
                session_header_id=current_session or None, device=str(stat.st_dev), inode=str(stat.st_ino),
                observed_size=stat.st_size, observed_mtime_ns=stat.st_mtime_ns,
                header_hash=header_hash or observed_header_hash, metadata=metadata,
            )
            if old_generation_id:
                self.store.delete_generation(old_generation_id)
                self._reconcile_orphans()
        return {"bytes": bytes_read, "pending": complete_offset < stat.st_size}
    @staticmethod
    def _enrich_events(
        source: str, item: Mapping[str, Any], events: Sequence[NormalizedEvent]
    ) -> list[NormalizedEvent]:
        skill_hashes: dict[tuple[str, str], str] = {}
        result_sha: str | None = None
        if source == "pi":
            message = item.get("message") if isinstance(item.get("message"), Mapping) else item
            content = message.get("content")
            text = "".join(
                str(block.get("text") or "") for block in content
                if isinstance(content, list) and isinstance(block, Mapping) and block.get("type") == "text"
            ) if isinstance(content, list) else (content if isinstance(content, str) else "")
            for match in _PI_SKILL_BLOCK.finditer(text):
                attrs = {part.group("name").lower(): part.group("value") for part in _HTML_ATTRIBUTE.finditer(match.group("attrs"))}
                if attrs.get("name") and attrs.get("location"):
                    skill_hashes[(attrs["name"], attrs["location"])] = hashlib.sha256(
                        match.group("body").encode("utf-8")
                    ).hexdigest()
            if str(message.get("role") or "") in {"toolResult", "tool_result", "function"}:
                result_sha = OutcomeReviewService._exact_text_sha(content)
        elif source == "codex":
            payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
            if str(payload.get("type") or "").endswith("_call_output"):
                result_sha = OutcomeReviewService._exact_text_sha(payload.get("output", payload.get("result")))
        enriched: list[NormalizedEvent] = []
        for event in events:
            metadata = dict(event.metadata)
            if event.event_type == "skill":
                key = (str(metadata.get("skill_name") or ""), str(metadata.get("location") or ""))
                if key in skill_hashes:
                    metadata["skill_content_sha256"] = skill_hashes[key]
                    metadata["sha_source"] = "pi-invocation-payload"
            if event.event_type == "tool_result" and result_sha:
                metadata["result_content_sha256"] = result_sha
                metadata["result_complete"] = True
            enriched.append(replace(event, metadata=metadata))
        represented = {
            (str(event.metadata.get("skill_name") or ""), str(event.metadata.get("location") or ""))
            for event in enriched if event.event_type == "skill"
        }
        user = next((event for event in enriched if event.event_type == "user_message"), None)
        if user is not None:
            for (name, location), content_sha in skill_hashes.items():
                if (name, location) in represented:
                    continue
                payload = {"name": name, "location": location, "content_sha256": content_sha}
                enriched.append(replace(
                    user,
                    event_type="skill",
                    fingerprint=event_fingerprint(
                        source="pi", session_family=user.session_family, event_type="skill",
                        event_id=f"{user.event_id}:skill:{name}" if user.event_id else "",
                        timestamp=user.timestamp, parent_id=user.event_id or user.parent_id,
                        payload=payload,
                    ),
                    event_id=f"{user.event_id}:skill:{name}" if user.event_id else "",
                    parent_id=user.event_id or user.parent_id,
                    role="", text="", tool_name="skill", args={"name": name, "location": location},
                    payload_hash=_digest(payload), payload=payload,
                    metadata={
                        "skill_name": name, "location": location,
                        "skill_content_sha256": content_sha, "sha_source": "pi-invocation-payload",
                    },
                ))
        return enriched
    @staticmethod
    def _exact_text_sha(value: Any) -> str | None:
        if isinstance(value, str):
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        if isinstance(value, list) and all(
            isinstance(item, Mapping) and item.get("type") in {"text", "output_text"} for item in value
        ):
            text = "".join(str(item.get("text") or "") for item in value)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        return None
    @staticmethod
    def _file_signature(path: Path, size: int) -> str:
        return _digest({"size": size, "samples": OutcomeReviewService._sample_hashes(path, size)})
    @staticmethod
    def _prefix_hash(path: Path, offset: int) -> str:
        remaining = min(offset, 64 * 1024)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                digest.update(block)
                remaining -= len(block)
        return digest.hexdigest()
    @staticmethod
    def _window_hash(path: Path, offset: int) -> str:
        start = max(0, offset - _WINDOW)
        with path.open("rb") as handle:
            handle.seek(start)
            return _digest(handle.read(offset - start))
    @staticmethod
    def _sample_hashes(path: Path, offset: int) -> list[dict[str, Any]]:
        if offset <= 0:
            return []
        starts = {0, max(0, offset // 2 - _SAMPLE // 2), max(0, offset - _SAMPLE)}
        samples: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            for start in sorted(starts):
                handle.seek(start)
                data = handle.read(min(_SAMPLE, offset - start))
                samples.append({"offset": start, "length": len(data), "sha256": _digest(data)})
        return samples
    @staticmethod
    def _checkpoint_matches(path: Path, checkpoint: Mapping[str, Any]) -> bool:
        offset = int(checkpoint["byte_offset"])
        if OutcomeReviewService._window_hash(path, offset) != checkpoint.get("cursor_window_hash"):
            return False
        with path.open("rb") as handle:
            for sample in checkpoint.get("sparse_hashes", []):
                handle.seek(int(sample["offset"]))
                data = handle.read(int(sample["length"]))
                if _digest(data) != sample.get("sha256"):
                    return False
        return True
    def _delete_missing(self, scopes: Sequence[tuple[str, Path]], discovered: set[str]) -> None:
        rows = self.store.execute(
            """SELECT DISTINCT g.id, f.source, l.path FROM log_file_generations g JOIN log_files f ON f.id = g.log_file_id JOIN log_file_locations l ON l.generation_id = g.id AND l.is_current = 1 WHERE g.status = 'active'"""
        ).fetchall()
        for row in rows:
            location = Path(row["path"])
            covered = any(row["source"] == source and (location == root or root in location.parents) for source, root in scopes)
            if covered and str(location) not in discovered and not location.exists():
                with self.store.transaction():
                    self.store.delete_generation(row["id"])
                    self._reconcile_orphans()
    def _reconcile_orphans(self) -> None:
        self.store.execute(
            """UPDATE skill_invocations SET result_event_id=NULL, load_status='result-missing', skill_sha256=NULL WHERE result_event_id IN (SELECT id FROM canonical_events WHERE orphaned=1)"""
        )
        self.store.execute(
            """DELETE FROM tool_results WHERE event_id IN (SELECT id FROM canonical_events WHERE orphaned=1)"""
        )
        self.store.execute(
            """DELETE FROM tool_calls WHERE event_id IN (SELECT id FROM canonical_events WHERE orphaned=1)"""
        )
        self.store.execute(
            """DELETE FROM task_facts WHERE source_kind='deterministic-parser' AND evidence_event_id IN (SELECT id FROM canonical_events WHERE orphaned=1)"""
        )
        self.store.execute(
            """UPDATE attribution_links SET status='rejected' WHERE status='active' AND skill_invocation_id IN (SELECT id FROM skill_invocations WHERE validity!='valid')"""
        )
        self.store.execute(
            """UPDATE outcome_assessments SET is_current=0, process_state='invalidated' WHERE is_current=1 AND skill_invocation_id IN (SELECT id FROM skill_invocations WHERE validity!='valid')"""
        )

    def _derive_candidate_attributions(self) -> None:
        """Project structural session edges without merging independent cases."""
        rows = self.store.execute(
            """SELECT DISTINCT child_case.id AS task_case_id, i.id AS skill_invocation_id, se.id AS edge_id
               FROM session_edges se
               JOIN task_episodes child_episode ON child_episode.session_id=se.child_session_id
               JOIN task_case_episodes child_link ON child_link.task_episode_id=child_episode.id
               JOIN task_cases child_case ON child_case.id=child_link.task_case_id
               JOIN task_episodes parent_episode ON parent_episode.session_id=se.parent_session_id
               JOIN skill_invocations i ON i.task_episode_id=parent_episode.id
               WHERE child_case.invalidated_at IS NULL AND child_episode.invalidated_at IS NULL
                 AND parent_episode.invalidated_at IS NULL AND i.validity='valid' AND i.load_status='loaded'"""
        ).fetchall()
        now = _now()
        with self.store.transaction():
            for row in rows:
                exists = self.store.execute(
                    """SELECT id, status FROM attribution_links WHERE task_case_id=? AND skill_invocation_id=?
                       AND attribution_kind='candidate' LIMIT 1""",
                    (row["task_case_id"], row["skill_invocation_id"]),
                ).fetchone()
                if exists is None:
                    rejected_ids = tuple(row["id"] for row in self.store.execute(
                        """SELECT id FROM attribution_links
                           WHERE task_case_id=? AND skill_invocation_id=?
                             AND attribution_kind='rejected' AND status='rejected'
                           ORDER BY id""",
                        (row["task_case_id"], row["skill_invocation_id"]),
                    ).fetchall())
                    self.store.execute(
                        """INSERT INTO attribution_links(id, task_case_id, skill_invocation_id,
                               attribution_kind, confidence, status, created_at)
                           VALUES (?, ?, ?, 'candidate', 0.5, 'active', ?)""",
                        (
                            _stable_id(
                                "candidate-attribution-v2", row["task_case_id"],
                                row["skill_invocation_id"], row["edge_id"], rejected_ids,
                            ),
                            row["task_case_id"], row["skill_invocation_id"], now,
                        ),
                    )
                elif exists["status"] == "rejected":
                    self.store.execute(
                        "UPDATE attribution_links SET status='active' WHERE id=?", (exists["id"],),
                    )
    # --------------------------------------------------------------- derivation
    def _persist_event(
        self,
        event: NormalizedEvent,
        generation_id: str,
        byte_start: int,
        byte_end: int,
        line_number: int,
        scan_run_id: str,
        raw_line_sha256: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "role": event.role, "text": event.text, "tool_name": event.tool_name,
            "args": event.args, "result": event.result, "outcome": event.outcome,
            "error": event.is_error, "cancelled": event.is_cancelled,
            "blocked": event.is_blocked, "metadata": dict(event.metadata),
        }
        fingerprint = event.fingerprint
        existing = self.store.execute(
            "SELECT payload_hash FROM canonical_events WHERE event_fingerprint=?", (fingerprint,)
        ).fetchone()
        payload_hash = event.payload_hash or _digest(payload)
        if existing is not None and existing["payload_hash"] != payload_hash:
            fingerprint = _stable_id(
                "protocol-id-content-revision", event.source, event.session_family,
                event.event_type, event.event_id, payload_hash,
            )
        stored = self.store.upsert_event(
            fingerprint, source=event.source, session_family=event.session_family or event.session_id,
            source_event_id=event.event_id or None, event_type=event.event_type,
            protocol_time=event.timestamp or None, parent_event_id=event.parent_id or None,
            call_id=event.call_id or None, payload_hash=payload_hash,
            payload=payload, scan_run_id=scan_run_id,
        )
        self.store.upsert_provenance(
            stored["id"], generation_id, byte_start, byte_end=byte_end,
            line_number=line_number, locator={
                "line": line_number, "source": event.source,
                "rawLineSha256": raw_line_sha256,
            },
        )
        return stored
    def _derive(self, event: NormalizedEvent, stored: Mapping[str, Any]) -> None:
        source_session_id = event.session_id or event.session_family or f"unknown:{event.source}"
        family = event.session_family or source_session_id
        session_id = _stable_id("session", event.source, source_session_id)
        self.store.execute(
            """INSERT INTO sessions(id, source, session_family, source_session_id, started_at, ended_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, '{}') ON CONFLICT(source, source_session_id) DO UPDATE SET session_family=excluded.session_family, started_at=COALESCE(sessions.started_at, excluded.started_at), ended_at=CASE WHEN sessions.ended_at IS NULL OR excluded.ended_at > sessions.ended_at THEN excluded.ended_at ELSE sessions.ended_at END""",
            (session_id, event.source, family, source_session_id, event.timestamp or None, event.timestamp or None),
        )
        session_row = self.store.execute(
            "SELECT id FROM sessions WHERE source = ? AND source_session_id = ?", (event.source, source_session_id)
        ).fetchone()
        session_id = session_row["id"]
        if event.event_type == "session_info" and event.text:
            self.store.execute("UPDATE sessions SET title = ? WHERE id = ?", (event.text, session_id))
        self._derive_edge(event, stored["id"], session_id)
        episode = None
        if event.event_type == "user_message":
            self._finalize_missing_tool_results(session_id)
            episode_fp = _stable_id("episode", event.source, family, source_session_id, event.fingerprint)
            episode_id = episode_fp
            now = _now()
            self.store.execute(
                """INSERT INTO task_episodes( id, episode_fingerprint, session_id, start_event_id, end_event_id, goal_text, process_state, metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'indexed', '{}', ?, ?) ON CONFLICT(episode_fingerprint) DO UPDATE SET start_event_id=excluded.start_event_id, end_event_id=excluded.end_event_id, goal_text=COALESCE(task_episodes.goal_text, excluded.goal_text), updated_at=excluded.updated_at""",
                (episode_id, episode_fp, session_id, stored["id"], stored["id"], event.text, now, now),
            )
            episode = self.store.execute("SELECT * FROM task_episodes WHERE episode_fingerprint = ?", (episode_fp,)).fetchone()
        else:
            episode = self.store.execute(
                "SELECT * FROM task_episodes WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if episode is None and event.event_type not in {"session_meta", "session_info"}:
            episode_fp = _stable_id("episode", event.source, family, source_session_id, event.fingerprint)
            now = _now()
            self.store.execute(
                """INSERT OR IGNORE INTO task_episodes( id, episode_fingerprint, session_id, start_event_id, end_event_id, process_state, metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'indexed', '{}', ?, ?)""",
                (episode_fp, episode_fp, session_id, stored["id"], stored["id"], now, now),
            )
            episode = self.store.execute("SELECT * FROM task_episodes WHERE episode_fingerprint = ?", (episode_fp,)).fetchone()
        if episode is None:
            return
        self.store.execute("UPDATE task_episodes SET end_event_id = ?, updated_at = ? WHERE id = ?", (stored["id"], _now(), episode["id"]))
        self._restore_episode_if_live(episode["id"])
        episode = self.store.execute("SELECT * FROM task_episodes WHERE id=?", (episode["id"],)).fetchone()
        case = self._ensure_case(episode)
        self._derive_facts(case, event, stored["id"])
        feedback_event = {
            **event.to_dict(), "id": stored["id"],
            "event_fingerprint": stored["event_fingerprint"],
            "protocol_time": event.timestamp or None,
            "created_at": stored["created_at"],
        }
        if event.event_type == "user_message":
            self.feedback.derive_user_event(feedback_event, case["id"])
        elif event.event_type == "assistant_message":
            self.feedback.derive_assistant_event(feedback_event, case["id"])
        if event.event_type == "tool_call":
            self._derive_tool_call(case, episode, event, stored)
        elif event.event_type == "tool_result":
            self._derive_tool_result(event, stored, session_id)
        elif event.event_type == "skill":
            spec = self._skill_spec(event)
            if spec:
                self._derive_invocation(case, episode, event, stored, spec, "loaded")

    def _finalize_missing_tool_results(self, session_id: str) -> None:
        episode = self.store.execute(
            """SELECT * FROM task_episodes WHERE session_id=? AND invalidated_at IS NULL
               ORDER BY created_at DESC LIMIT 1""", (session_id,),
        ).fetchone()
        if episode is None:
            return
        case = self.store.execute(
            """SELECT task_case_id FROM task_case_episodes WHERE task_episode_id=?
               ORDER BY relationship='primary' DESC LIMIT 1""", (episode["id"],),
        ).fetchone()
        rows = self.store.execute(
            """SELECT c.*, e.* FROM tool_calls c JOIN canonical_events e ON e.id=c.event_id
               WHERE c.task_episode_id=?
                 AND NOT EXISTS (SELECT 1 FROM tool_results r WHERE r.tool_call_id=c.id)""",
            (episode["id"],),
        ).fetchall()
        for row in rows:
            event = _decode(row)
            event["id"] = row["event_id"]
            self.feedback.derive_process_result(
                event,
                {"stored_tool_call_id": row["id"], "tool_name": row["tool_name"],
                 "args": json.loads(row["arguments_json"]), "episode_closed": True},
                {"own_case_id": case["task_case_id"] if case else None},
            )

    def _finalize_stale_missing_tool_results(
        self, *, grace_seconds: int = 300, limit: int = 1000, max_seconds: float = 1.0,
    ) -> dict[str, int | bool]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        rows = self.store.execute(
            """SELECT c.id AS stored_tool_call_id, c.task_episode_id, c.tool_name,
                      c.arguments_json, e.id AS event_id, e.event_fingerprint, e.source,
                      e.session_family, e.protocol_time, e.payload_hash, e.payload_json,
                      e.created_at
               FROM tool_calls c JOIN canonical_events e ON e.id=c.event_id
               WHERE NOT EXISTS (SELECT 1 FROM tool_results r WHERE r.tool_call_id=c.id)
                 AND NOT EXISTS (
                   SELECT 1 FROM feedback_targets target
                   JOIN feedback_signals signal ON signal.id=target.feedback_signal_id
                   JOIN feedback_signal_revisions revision
                     ON revision.id=signal.current_machine_revision_id
                   WHERE target.tool_call_id=c.id AND target.machine_status='candidate'
                     AND revision.category='result-missing' AND revision.orphaned=0
                 )
                 AND COALESCE(c.called_at,e.protocol_time,e.created_at)<=?
               ORDER BY COALESCE(c.called_at,e.protocol_time,e.created_at), c.id LIMIT ?""",
            (cutoff, limit + 1),
        ).fetchall()
        started = time.monotonic()
        processed = signals = 0
        for row in rows[:limit]:
            if time.monotonic() - started >= max_seconds:
                break
            case = self.store.execute(
                """SELECT task_case_id FROM task_case_episodes WHERE task_episode_id=?
                   ORDER BY relationship='primary' DESC LIMIT 1""", (row["task_episode_id"],),
            ).fetchone()
            event = {
                "id": row["event_id"], "event_fingerprint": row["event_fingerprint"],
                "source": row["source"], "session_family": row["session_family"],
                "protocol_time": row["protocol_time"], "payload_hash": row["payload_hash"],
                "payload": json.loads(row["payload_json"]), "created_at": row["created_at"],
            }
            found = self.feedback.derive_process_result(
                event,
                {"stored_tool_call_id": row["stored_tool_call_id"],
                 "tool_name": row["tool_name"], "args": json.loads(row["arguments_json"]),
                 "episode_closed": True},
                {"own_case_id": case["task_case_id"] if case else None},
            )
            signals += len(found)
            processed += 1
        return {
            "processed": processed, "signals": signals,
            "pending": len(rows) > processed,
        }
    def _derive_edge(self, event: NormalizedEvent, event_id: str, child_id: str) -> None:
        if event.event_type != "session_meta":
            return
        target = event.fork_from_id or event.parent_session_id
        if not target or target == event.session_id:
            return
        parent_id = _stable_id("session", event.source, target)
        self.store.execute(
            "INSERT OR IGNORE INTO sessions(id, source, session_family, source_session_id, metadata_json) VALUES (?, ?, ?, ?, '{}')",
            (parent_id, event.source, event.session_family or target, target),
        )
        relation = "fork" if event.fork_from_id else "parent"
        edge_id = _stable_id("edge", child_id, parent_id, relation)
        self.store.execute(
            """INSERT OR IGNORE INTO session_edges( id, parent_session_id, child_session_id, edge_type, event_id, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, '{}')""",
            (edge_id, parent_id, child_id, relation, event_id, _now()),
        )
    def _ensure_case(self, episode: Mapping[str, Any]) -> dict[str, Any]:
        case_fp = _stable_id("case", episode["episode_fingerprint"])
        now = _now()
        self.store.execute(
            """INSERT OR IGNORE INTO task_cases(id, case_fingerprint, metadata_json, created_at, updated_at) VALUES (?, ?, '{}', ?, ?)""",
            (case_fp, case_fp, now, now),
        )
        self.store.execute(
            "INSERT OR IGNORE INTO task_case_episodes(task_case_id, task_episode_id, relationship) VALUES (?, ?, 'primary')",
            (case_fp, episode["id"]),
        )
        previous = self.store.execute(
            "SELECT invalidated_at FROM task_cases WHERE id=?", (case_fp,),
        ).fetchone()
        if previous is not None and previous["invalidated_at"] is not None and episode["invalidated_at"] is None:
            self.store.execute(
                "UPDATE task_cases SET invalidated_at=NULL, updated_at=? WHERE id=?",
                (now, case_fp),
            )
            self._record_derivation_change("case-reactivated", "task-case", case_fp)
        return _decode(self.store.execute("SELECT * FROM task_cases WHERE id = ?", (case_fp,)).fetchone())

    def _restore_episode_if_live(self, episode_id: str) -> bool:
        row = self.store.execute(
            "SELECT invalidated_at FROM task_episodes WHERE id=?", (episode_id,),
        ).fetchone()
        if row is None or row["invalidated_at"] is None:
            return False
        missing = self.store.execute(
            """SELECT 1 FROM task_episodes ep
               WHERE ep.id=? AND (
                 EXISTS (SELECT 1 FROM canonical_events e
                         WHERE e.id IN (ep.start_event_id, ep.end_event_id) AND e.orphaned=1)
                 OR EXISTS (SELECT 1 FROM skill_invocations i JOIN canonical_events e ON e.id=i.event_id
                            WHERE i.task_episode_id=ep.id AND e.orphaned=1)
               )""",
            (episode_id,),
        ).fetchone()
        if missing is not None:
            return False
        now = _now()
        self.store.execute(
            "UPDATE task_episodes SET invalidated_at=NULL, updated_at=? WHERE id=?",
            (now, episode_id),
        )
        self._record_derivation_change("episode-reactivated", "task-episode", episode_id)
        return True

    def _record_derivation_change(self, change_type: str, entity_kind: str, entity_id: str) -> None:
        self.store.execute(
            """INSERT INTO effect_derivation_changes(
                   change_type, entity_kind, entity_id, binding_json, created_at)
               VALUES (?, ?, ?, '{}', ?)""",
            (change_type, entity_kind, entity_id, _now()),
        )

    def _derive_facts(self, case: Mapping[str, Any], event: NormalizedEvent, event_id: str) -> None:
        for fact in extract_task_facts([event]):
            fact_id = _stable_id("fact", case["id"], fact.predicate, fact.value, fact.evidence_fingerprint)
            self.store.execute(
                """INSERT OR IGNORE INTO task_facts( id, task_case_id, case_revision, fact_type, value_json, evidence_event_id, source_kind, producer_version, status, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
                (fact_id, case["id"], case["current_revision"], fact.predicate, _json(fact.value),
                 event_id, fact.source_kind, fact.producer_version, fact.confidence, _now()),
            )
        self._refresh_classification(case["id"], case["current_revision"])

    def _refresh_classification(
        self, task_case_id: str, case_revision: int, *, actor_id: str | None = None,
        task_type_override: str | None = None,
    ) -> dict[str, Any]:
        facts = self.store.execute(
            """SELECT fact_type, value_json, source_kind, status, confidence FROM task_facts
               WHERE task_case_id=? AND case_revision=? AND status='accepted' ORDER BY id""",
            (task_case_id, case_revision),
        ).fetchall()
        tags = {
            json.loads(row["value_json"])
            for row in facts if row["fact_type"] == "task-tag"
        }
        if {"gradle", "test"} <= tags:
            task_type = "gradle-test"
        elif "gradle" in tags:
            task_type = "gradle-build"
        elif "deploy" in tags:
            task_type = "deploy"
        elif "test" in tags:
            task_type = "test"
        elif "document" in tags:
            task_type = "document"
        else:
            task_type = None
        if task_type_override is not None:
            task_type = task_type_override.strip() or None
        classification = {
            "facts": [
                {"type": row["fact_type"], "value": json.loads(row["value_json"]),
                 "sourceKind": row["source_kind"], "confidence": row["confidence"]}
                for row in facts
            ],
            "producer": "manual" if actor_id else self.parser_version,
            "manualOverride": task_type_override if actor_id else None,
        }
        self.store.execute(
            """INSERT INTO task_classifications(id, task_case_id, revision, profile_version,
                   applicability, task_type, classification_json, actor_id, created_at)
               VALUES (?, ?, ?, ?, 'unknown', ?, ?, ?, ?)
               ON CONFLICT(task_case_id, revision) DO UPDATE SET
                 profile_version=excluded.profile_version, task_type=excluded.task_type,
                 classification_json=excluded.classification_json,
                 actor_id=COALESCE(excluded.actor_id, task_classifications.actor_id)""",
            (
                _stable_id("classification", task_case_id, case_revision), task_case_id, case_revision,
                "manual-v1" if actor_id else "deterministic-v1", task_type,
                _json(classification), actor_id, _now(),
            ),
        )
        self.store.execute(
            "UPDATE task_cases SET task_type=?, updated_at=? WHERE id=?",
            (task_type, _now(), task_case_id),
        )
        return {"revision": case_revision, "task_type": task_type, "classification": classification}
    def _derive_tool_call(self, case: Mapping[str, Any], episode: Mapping[str, Any], event: NormalizedEvent, stored: Mapping[str, Any]) -> None:
        call_fp = _stable_id("call", event.fingerprint)
        self.store.execute(
            """INSERT OR IGNORE INTO tool_calls( id, call_fingerprint, task_episode_id, event_id, call_id, tool_name, arguments_hash, arguments_json, called_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (call_fp, call_fp, episode["id"], stored["id"], event.call_id or None,
             event.tool_name, _digest(event.args), _json(event.args), event.timestamp or None),
        )
        spec = self._skill_spec(event)
        if spec:
            self._derive_invocation(case, episode, event, stored, spec, "pending")
    def _derive_tool_result(self, event: NormalizedEvent, stored: Mapping[str, Any], session_id: str) -> None:
        call = self.store.execute(
            """SELECT c.* FROM tool_calls c JOIN task_episodes e ON e.id = c.task_episode_id WHERE e.session_id = ? AND c.call_id = ? ORDER BY c.called_at DESC, c.id DESC LIMIT 1""",
            (session_id, event.call_id),
        ).fetchone()
        if call is None:
            return
        result_fp = _stable_id("result", event.fingerprint)
        excerpt = event.result if isinstance(event.result, str) else _canonical(event.result)
        self.store.execute(
            """INSERT OR IGNORE INTO tool_results( id, result_fingerprint, tool_call_id, event_id, status, output_hash, excerpt, completed_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result_fp, result_fp, call["id"], stored["id"], event.outcome or "unknown",
             _digest(event.result), excerpt[:4096], event.timestamp or None,
             _json({"error": event.is_error, "blocked": event.is_blocked, "cancelled": event.is_cancelled})),
        )
        if "subagent" in str(call["tool_name"]).lower():
            source_row = self.store.execute("SELECT source FROM sessions WHERE id=?", (session_id,)).fetchone()
            for child_source_id in self._structured_child_session_ids(event.result):
                if source_row is None:
                    break
                child_id = _stable_id("session", source_row["source"], child_source_id)
                if child_id == session_id:
                    continue
                self.store.execute(
                    """INSERT OR IGNORE INTO sessions(id, source, session_family, source_session_id, metadata_json)
                       VALUES (?, ?, ?, ?, '{}')""",
                    (child_id, source_row["source"], child_source_id, child_source_id),
                )
                self.store.execute(
                    """INSERT OR IGNORE INTO session_edges(id, parent_session_id, child_session_id,
                           edge_type, event_id, created_at, metadata_json)
                       VALUES (?, ?, ?, 'subagent-result', ?, ?, '{}')""",
                    (_stable_id("edge", session_id, child_id, "subagent-result"), session_id, child_id, stored["id"], _now()),
                )
        case = self.store.execute(
            """SELECT task_case_id FROM task_case_episodes WHERE task_episode_id=?
               ORDER BY relationship='primary' DESC LIMIT 1""", (call["task_episode_id"],),
        ).fetchone()
        feedback_event = {
            **event.to_dict(), "id": stored["id"],
            "event_fingerprint": stored["event_fingerprint"],
            "protocol_time": event.timestamp or None,
            "created_at": stored["created_at"],
        }
        self.feedback.derive_process_result(
            feedback_event,
            {"stored_tool_call_id": call["id"], "tool_name": call["tool_name"],
             "args": json.loads(call["arguments_json"]), "episode_closed": True},
            {"own_case_id": case["task_case_id"] if case else None},
        )
        invocation = self.store.execute("SELECT * FROM skill_invocations WHERE event_id = ?", (call["event_id"],)).fetchone()
        if invocation is None:
            return
        status = {"returned": "loaded", "error": "error", "blocked": "blocked", "cancelled": "cancelled"}.get(event.outcome, "unknown")
        sha = invocation["skill_sha256"]
        metadata = json.loads(invocation["metadata_json"])
        try:
            call_args = json.loads(call["arguments_json"])
        except (TypeError, json.JSONDecodeError):
            call_args = {}
        is_partial_read = isinstance(call_args, Mapping) and any(
            key in call_args and call_args[key] is not None and call_args[key] != "" and call_args[key] != 0
            for key in ("offset", "limit")
        )
        result_text = excerpt.lower()
        output_truncated = any(marker in result_text for marker in (
            "[truncated]", "output truncated", "contents truncated", "remaining lines", "more lines in file",
        ))
        if status == "loaded" and event.metadata.get("result_complete") and not is_partial_read and not output_truncated:
            sha = event.metadata.get("result_content_sha256")
            metadata["sha_source"] = "tool-result"
        elif status == "loaded" and (is_partial_read or output_truncated):
            sha = None
            metadata["sha_source"] = "partial-tool-result"
            metadata["version_unknown_reason"] = "partial-read" if is_partial_read else "truncated-output"
        self.store.execute(
            "UPDATE skill_invocations SET result_event_id = ?, load_status = ?, skill_sha256 = ?, metadata_json = ? WHERE id = ?",
            (stored["id"], status, sha, _json(metadata), invocation["id"]),
        )

    @staticmethod
    def _structured_child_session_ids(value: Any) -> set[str]:
        identifiers: set[str] = set()
        keys = {"child_session_id", "childSessionId", "session_id", "sessionId", "thread_id", "threadId"}

        def visit(item: Any) -> None:
            if isinstance(item, Mapping):
                for key, nested in item.items():
                    if key in keys and isinstance(nested, str) and nested.strip():
                        identifiers.add(nested.strip())
                    else:
                        visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)
            elif isinstance(item, str) and item.lstrip().startswith(("{", "[")):
                try:
                    visit(json.loads(item))
                except json.JSONDecodeError:
                    pass

        visit(value)
        return identifiers

    def _skill_spec(self, event: NormalizedEvent) -> dict[str, Any] | None:
        if event.event_type == "skill":
            name = str(event.metadata.get("skill_name") or "").strip()
            location = str(event.metadata.get("location") or "").strip()
            return self._resolve_skill(name, location)
        if event.event_type != "tool_call":
            return None
        tool = event.tool_name.rsplit(".", 1)[-1].lower()
        text = "\n".join(_strings(event.args))
        if tool not in _READ_TOOLS and not (tool in _SHELL_TOOLS and _READ_COMMAND.search(text)):
            return None
        match = _SKILL_PATH.search(text)
        if not match:
            return None
        path = match.group("path").rstrip(".,;:)]}")
        return self._resolve_skill(Path(path).parent.name, path)
    def _resolve_skill(self, name: str, raw_path: str) -> dict[str, Any]:
        candidate = Path(raw_path).expanduser()
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.extend(root / name / "SKILL.md" for root in self.skill_roots)
        selected: Path | None = None
        for item in candidates:
            try:
                resolved = item.resolve()
            except OSError:
                continue
            if self.skill_roots and not any(resolved == root or root in resolved.parents for root in self.skill_roots):
                continue
            if resolved.is_file() and resolved.name.lower() == "skill.md":
                selected = resolved
                break
        return {
            "skill_id": name or (selected.parent.name if selected else Path(raw_path).parent.name),
            "path": str(selected or candidate), "sha256": None,
            "sha_source": "unknown",
        }
    def _derive_invocation(
        self,
        case: Mapping[str, Any],
        episode: Mapping[str, Any],
        event: NormalizedEvent,
        stored: Mapping[str, Any],
        spec: Mapping[str, Any],
        load_status: str,
    ) -> None:
        invocation_fp = _stable_id("invocation", event.fingerprint, spec["skill_id"])
        invocation_id = invocation_fp
        invocation_sha = event.metadata.get("skill_content_sha256") or spec["sha256"]
        sha_source = event.metadata.get("sha_source") or spec["sha_source"]
        metadata = {"sha_source": sha_source, "source": event.source}
        goal = str(episode["goal_text"] or "")
        maintenance_target = re.search(
            r"(?is)(?:SKILL\.md|这个技能|该技能|技能本身).{0,40}(?:审查|评审|翻译|迁移|维护|自测|测试|review|audit|translate|migrat|maintain|self[- ]?test)|"
            r"(?:审查|评审|翻译|迁移|维护|自测|review|audit|translate|migrat|maintain|self[- ]?test).{0,40}(?:SKILL\.md|这个技能|该技能|技能本身)",
            goal,
        )
        invocation_kind = "skill-maintenance" if maintenance_target else "business-use"
        previous_invocation = self.store.execute(
            "SELECT id, validity FROM skill_invocations WHERE invocation_fingerprint=?",
            (invocation_fp,),
        ).fetchone()
        self.store.execute(
            """INSERT INTO skill_invocations( id, invocation_fingerprint, task_episode_id, event_id, skill_id, skill_sha256, skill_path, invocation_kind, load_status, validity, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?) ON CONFLICT(invocation_fingerprint) DO UPDATE SET skill_sha256=COALESCE(skill_invocations.skill_sha256, excluded.skill_sha256), skill_path=excluded.skill_path, invocation_kind=excluded.invocation_kind, load_status=CASE WHEN skill_invocations.load_status='loaded' THEN 'loaded' ELSE excluded.load_status END, validity='valid', metadata_json=excluded.metadata_json""",
            (invocation_id, invocation_fp, episode["id"], stored["id"], spec["skill_id"],
             invocation_sha, spec["path"], invocation_kind, load_status, _now(), _json(metadata)),
        )
        invocation = self.store.execute(
            "SELECT id FROM skill_invocations WHERE invocation_fingerprint = ?", (invocation_fp,)
        ).fetchone()
        if previous_invocation is not None and previous_invocation["validity"] == "orphaned":
            self._record_derivation_change("target-reactivated", "skill-invocation", invocation["id"])
        existing_links = self.store.execute(
            """SELECT attribution_kind FROM attribution_links WHERE task_case_id=? AND status='active'
               AND evidence_id IS NULL ORDER BY created_at, id""", (case["id"],)
        ).fetchall()
        attribution_kind = (
            "rejected" if invocation_kind == "skill-maintenance"
            else ("shared" if existing_links else "direct")
        )
        if attribution_kind == "shared":
            self.store.execute(
                """UPDATE attribution_links SET attribution_kind='shared'
                   WHERE task_case_id=? AND status='active' AND attribution_kind='direct'
                     AND skill_invocation_id IN (
                       SELECT id FROM skill_invocations WHERE invocation_kind='business-use'
                     )""",
                (case["id"],),
            )
        exact_link = self.store.execute(
            """SELECT id, status FROM attribution_links
               WHERE task_case_id=? AND skill_invocation_id=? AND evidence_id IS NULL
                 AND attribution_kind=? ORDER BY created_at, id LIMIT 1""",
            (case["id"], invocation["id"], attribution_kind),
        ).fetchone()
        machine_link = self.store.execute(
            """SELECT id, attribution_kind, status FROM attribution_links
               WHERE task_case_id=? AND skill_invocation_id=? AND evidence_id IS NULL
                 AND attribution_kind IN ('direct','shared','candidate')
               ORDER BY created_at, id LIMIT 1""",
            (case["id"], invocation["id"]),
        ).fetchone()
        if attribution_kind == "rejected" and exact_link is not None:
            return
        if exact_link is not None:
            self.store.execute(
                "UPDATE attribution_links SET status='active' WHERE id=?", (exact_link["id"],),
            )
        elif machine_link is not None:
            self.store.execute(
                """UPDATE attribution_links SET attribution_kind=?, status='active'
                   WHERE id=?""",
                (attribution_kind, machine_link["id"]),
            )
        else:
            rejected_ids = tuple(row["id"] for row in self.store.execute(
                """SELECT id FROM attribution_links
                   WHERE task_case_id=? AND skill_invocation_id=?
                     AND attribution_kind='rejected' AND status='rejected'
                   ORDER BY id""",
                (case["id"], invocation["id"]),
            ).fetchall())
            self.store.execute(
                """INSERT INTO attribution_links( id, task_case_id, skill_invocation_id, evidence_id, attribution_kind, confidence, status, created_at) VALUES (?, ?, ?, NULL, ?, ?, 'active', ?)""",
                (_stable_id(
                    "machine-attribution-v2", case["id"], invocation["id"],
                    attribution_kind, rejected_ids,
                ),
                 case["id"], invocation["id"], attribution_kind,
                 0.0 if attribution_kind == "rejected" else 1.0, _now()),
            )
    # ---------------------------------------------------------------- review
    def review_case(
        self,
        task_case_id: str,
        *,
        skill_invocation_id: str | None = None,
        review_mode: str = "outcome",
        contract_version_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        case = self.store.execute("SELECT * FROM task_cases WHERE id = ?", (task_case_id,)).fetchone()
        if case is None:
            raise KeyError(task_case_id)
        if skill_invocation_id:
            invocation = self.store.execute(
                """SELECT i.*, l.attribution_kind FROM skill_invocations i JOIN attribution_links l ON l.skill_invocation_id=i.id WHERE i.id=? AND l.task_case_id=? AND l.status='active' AND i.validity='valid' AND i.load_status='loaded'""",
                (skill_invocation_id, task_case_id),
            ).fetchone()
        else:
            invocation = self.store.execute(
                """SELECT i.*, l.attribution_kind FROM skill_invocations i JOIN attribution_links l ON l.skill_invocation_id=i.id WHERE l.task_case_id=? AND l.status='active' AND i.validity='valid' AND i.load_status='loaded' ORDER BY i.created_at DESC LIMIT 1""",
                (task_case_id,),
            ).fetchone()
        if invocation is None:
            raise KeyError(skill_invocation_id or f"invocation for {task_case_id}")
        contract = None
        if self.contracts is not None and invocation["skill_sha256"]:
            contract = self.contracts.select(
                invocation["skill_id"], invocation["skill_sha256"], review_mode=review_mode,
                contract_version_id=contract_version_id,
            )
        facts = [
            {"fact_type": row["fact_type"], "value": json.loads(row["value_json"]), "status": row["status"]}
            for row in self.store.execute(
                "SELECT * FROM task_facts WHERE task_case_id=? AND case_revision=? AND status='accepted' ORDER BY id",
                (task_case_id, case["current_revision"]),
            ).fetchall()
        ]
        artifacts = [
            {**_decode(row), **json.loads(row["metadata_json"])}
            for row in self.store.execute(
                "SELECT * FROM artifacts WHERE task_case_id=? AND case_revision=? ORDER BY id",
                (task_case_id, case["current_revision"]),
            ).fetchall()
        ]
        evidence = [self._check_evidence(row) for row in self.store.execute(
            """SELECT * FROM check_runs WHERE task_case_id=? AND case_revision=?
               ORDER BY started_at, id""", (task_case_id, case["current_revision"])
        ).fetchall()]
        classification = self.store.execute(
            "SELECT revision FROM task_classifications WHERE task_case_id=? AND revision=?",
            (task_case_id, case["current_revision"]),
        ).fetchone()
        queue_reason: str | None
        if not invocation["skill_sha256"]:
            result = {"applicability": "unknown", "verdict": "inconclusive", "reasons": ["skill-sha-unknown"]}
            assessability, verdict, queue_reason = "needs-evidence", "unset", "skill-sha-unknown"
        elif contract is None:
            result = {"applicability": "unknown", "verdict": "inconclusive", "reasons": ["contract-missing"]}
            assessability, verdict, queue_reason = "not-assessable", "unset", "contract-missing"
        else:
            result = self.interpreter.evaluate(contract["contract"], task_facts=facts, artifacts=artifacts, evidence=evidence)
            if result["applicability"] == "not-applicable":
                assessability, verdict, queue_reason = "not-assessable", "unset", None
            elif result["applicability"] == "unknown":
                assessability, verdict, queue_reason = "needs-evidence", "unset", "applicability-unknown"
            elif result["verdict"] == "inconclusive":
                assessability, verdict, queue_reason = "needs-evidence", "unset", "evidence-inconclusive"
            else:
                assessability, verdict = "assessable", result["verdict"]
                queue_reason = "deterministic-failure" if verdict == "fail" else None
        hard_failure = self._has_hard_failure(result, evidence)
        semantic_required = bool(
            contract and (contract["contract"].get("semanticReview") or {}).get("required") is True
        )
        deterministic_statuses = [
            item.get("status")
            for item in [*(result.get("artifact_results") or []), *(result.get("requirement_results") or [])]
        ]
        deterministic_ready = not deterministic_statuses or all(
            status == "pass" for status in deterministic_statuses
        )
        if (
            semantic_required and not hard_failure
            and result.get("applicability") == "applicable"
            and result.get("verdict") != "fail"
            and deterministic_ready
        ):
            assessability, verdict, queue_reason = "assessable", "unset", "semantic-review-required"
        bound_evidence_ids = {
            str(evidence_id)
            for requirement in result.get("requirement_results", [])
            for clause in requirement.get("clauses", [])
            for evidence_id in clause.get("evidence_ids", [])
        }
        bound_artifact_ids = {
            str(artifact_id)
            for artifact_result in result.get("artifact_results", [])
            for artifact_id in artifact_result.get("artifact_ids", [])
        }
        freshness_values = {
            str(item.get("freshness") or "unknown")
            for item in evidence if str(item.get("id")) in bound_evidence_ids
        } | {
            str(item.get("freshness") or "unknown")
            for item in artifacts if str(item.get("id")) in bound_artifact_ids
        }
        freshness = "stale" if "stale" in freshness_values else (
            "current" if "current" in freshness_values else "unknown"
        )
        if freshness == "stale":
            assessability, verdict, queue_reason = "needs-evidence", "unset", "stale-evidence"
            hard_failure = False
        subject_key = f"skill-invocation:{invocation['id']}"
        review_fp = _digest({
            "case_revision": case["current_revision"], "invocation": invocation["id"],
            "skill_sha256": invocation["skill_sha256"], "contract": contract["id"] if contract else None,
            "facts": facts, "artifacts": [item.get("content_hash") for item in artifacts],
            "evidence": evidence, "result": result,
        })
        current_revision = int(case["current_assessment_revision"] if expected_revision is None else expected_revision)
        with self.store.transaction():
            existing = self.store.execute(
                """SELECT * FROM outcome_assessments WHERE task_case_id=? AND subject_key=? AND is_current=1 ORDER BY revision DESC LIMIT 1""", (task_case_id, subject_key)
            ).fetchone()
            if existing is not None and _decode(existing).get("rationale", {}).get("reviewFingerprint") == review_fp:
                assessment = _decode(existing)
            else:
                assessment = self.store.create_assessment_revision(
                    task_case_id, expected_revision=current_revision, case_revision=case["current_revision"],
                    subject_key=subject_key, skill_invocation_id=invocation["id"],
                    attribution_kind=invocation["attribution_kind"],
                    contract_version_id=contract["id"] if contract else None,
                    classification_revision=classification["revision"] if classification else None,
                    parser_version=self.parser_version, checker_version=self._checker_versions(evidence),
                    process_state="reviewed", assessability=assessability, automated_verdict=verdict,
                    freshness=freshness, hard_failure=hard_failure,
                    rationale={"reviewFingerprint": review_fp, "contractResult": result},
                )
            self.store.execute(
                """UPDATE review_tasks SET status='superseded', updated_at=?
                   WHERE status='open' AND assessment_id IN (
                     SELECT id FROM outcome_assessments WHERE task_case_id=?
                       AND subject_key=? AND is_current=0
                   )""",
                (_now(), task_case_id, subject_key),
            )
            if queue_reason:
                found = self.store.execute(
                    "SELECT id FROM review_tasks WHERE assessment_id=? AND status='open'", (assessment["id"],)
                ).fetchone()
                if found is None:
                    self.store.create_review_task(task_case_id, assessment["id"], queue_reason)
        return self._assessment_response(assessment)
    review = review_case
    @staticmethod
    def _check_evidence(row: Any) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        lifecycle = result.get("lifecycle") or ("finished" if row["status"] in {"finished", "completed"} else row["status"])
        stored_outcome = row["assertion_outcome"]
        result_outcome = result.get("outcome")
        outcome_mismatch = bool(stored_outcome and result_outcome and stored_outcome != result_outcome)
        return {
            "id": row["id"], "checker_id": row["checker_id"], "checker_version": row["checker_version"],
            "approval_version": row["approval_version"], "lifecycle": lifecycle,
            "outcome": stored_outcome or result_outcome,
            "validity": "invalid" if outcome_mismatch else result.get("validity", "valid"),
            "trust_level": "untrusted" if outcome_mismatch else result.get("trust_level", "untrusted"),
            "parser_version": result.get("parser_version"),
            "freshness": row["freshness"], "assertions": result.get("assertions", {}),
        }
    @staticmethod
    def _checker_versions(evidence: Sequence[Mapping[str, Any]]) -> str | None:
        versions = sorted({f"{item['checker_id']}@{item['checker_version']}" for item in evidence})
        return ",".join(versions) or None
    def _has_hard_failure(
        self, result: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
    ) -> bool:
        by_id = {str(item.get("id")): item for item in evidence if item.get("id") is not None}
        for requirement in result.get("requirement_results", []):
            if requirement.get("status") != "fail":
                continue
            for detail in requirement.get("clauses", []):
                if detail.get("reason") != "assertion-fail":
                    continue
                clause = detail.get("clause") or {}
                for evidence_id in detail.get("evidence_ids", []):
                    item = by_id.get(str(evidence_id))
                    if item is None or item.get("trust_level") not in {"trusted", "sandboxed"}:
                        continue
                    probe = self.interpreter.evaluate(
                        {"requirements": [{"id": "hard-failure", **clause}]}, evidence=[item]
                    )
                    if probe.get("verdict") == "fail":
                        return True
        return False
    def _assessment_response(self, assessment: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(assessment)
        if "rationale_json" in result:
            result["rationale"] = json.loads(result.pop("rationale_json"))
        task = self.store.execute(
            "SELECT * FROM review_tasks WHERE assessment_id=? ORDER BY created_at DESC LIMIT 1", (assessment["id"],)
        ).fetchone()
        result["review_task"] = _decode(task) if task else None
        result.update(self.effective_projection(str(assessment["id"])))
        return _safe_public(result)

    def effective_projection(self, assessment_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        assessment = self.store.execute(
            """SELECT automated_verdict, assessability, conflict_state, freshness, process_state
               FROM outcome_assessments WHERE id=?""",
            (assessment_id,),
        ).fetchone()
        if assessment is None:
            raise KeyError(assessment_id)
        if assessment["freshness"] != "current" and assessment["freshness"] != "unknown":
            return {
                "effective_verdict": "needs-evidence", "effective_source": "freshness",
                "conflict_state": assessment["conflict_state"],
            }
        as_of = as_of or _now()
        exception = self.store.execute(
            """SELECT id FROM exceptions WHERE assessment_id=? AND created_at<=?
               AND (expires_at IS NULL OR expires_at>?) ORDER BY revision DESC LIMIT 1""",
            (assessment_id, as_of, as_of),
        ).fetchone()
        decision = self.store.execute(
            """SELECT d.* FROM manual_decisions d JOIN review_tasks r ON r.id=d.review_task_id
               WHERE d.assessment_id=? AND d.revision=r.current_decision_revision AND d.created_at<=?
               ORDER BY d.created_at DESC LIMIT 1""",
            (assessment_id, as_of),
        ).fetchone()
        if exception is not None:
            verdict, source = "exception-accepted", "exception"
        elif decision is not None and decision["action"] == "decision" and assessment["assessability"] == "assessable":
            verdict, source = decision["verdict"], "manual-decision"
        elif decision is not None and decision["action"] == "disposition":
            verdict, source = decision["verdict"], "manual-disposition"
        else:
            verdict, source = assessment["automated_verdict"], "automatic"
        conflict_state = assessment["conflict_state"]
        if exception is None and conflict_state == "exception-accepted":
            conflict_state = (
                "disputed" if decision is not None and decision["action"] == "decision"
                and assessment["automated_verdict"] in {"pass", "partial", "fail"}
                and decision["verdict"] != assessment["automated_verdict"] else "none"
            )
        return {
            "effective_verdict": verdict, "effective_source": source,
            "conflict_state": conflict_state,
        }

    def semantic_review_payload(
        self, task_case_id: str, *, assessment_id: str | None = None,
        skill_invocation_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        conditions = ["a.task_case_id=?", "a.is_current=1"]
        parameters: list[Any] = [task_case_id]
        if assessment_id:
            conditions.append("a.id=?")
            parameters.append(assessment_id)
        if skill_invocation_id:
            conditions.append("a.skill_invocation_id=?")
            parameters.append(skill_invocation_id)
        current_rows = self.store.execute(
            f"""SELECT a.*, c.current_assessment_revision, c.current_revision, c.task_type
                 FROM outcome_assessments a JOIN task_cases c ON c.id=a.task_case_id
                 WHERE {' AND '.join(conditions)} ORDER BY a.revision DESC""",
            parameters,
        ).fetchall()
        if not current_rows:
            raise ValueError("deterministic assessment is required before semantic review")
        if len(current_rows) != 1:
            raise ValueError("semantic review assessment is ambiguous; select an assessment or invocation")
        current = _decode(current_rows[0])
        episode = self.store.execute(
            """SELECT ep.goal_text, s.source FROM task_case_episodes ce
               JOIN task_episodes ep ON ep.id=ce.task_episode_id JOIN sessions s ON s.id=ep.session_id
               WHERE ce.task_case_id=? ORDER BY ep.created_at LIMIT 1""",
            (task_case_id,),
        ).fetchone()
        current["goal_text"] = episode["goal_text"] if episode is not None else None
        current["source"] = episode["source"] if episode is not None else "unknown"
        contract_id = current.get("contract_version_id")
        if not contract_id or self.contracts is None:
            raise ValueError("an approved exact-version contract is required for semantic review")
        contract_record = self.contracts.get(contract_id)
        if contract_record.get("status") not in {"active", "superseded"} or contract_record.get("governance_status") != "approved":
            raise ValueError("semantic review contract is not approved")
        contract = contract_record["contract"]
        semantic_rule = contract.get("semanticReview") or {}
        if semantic_rule.get("required") is not True:
            raise ValueError("the selected contract does not require semantic review")
        contract_result = (current.get("rationale") or {}).get("contractResult") or {}
        deterministic_statuses = [
            item.get("status") for item in [
                *(contract_result.get("artifact_results") or []),
                *(contract_result.get("requirement_results") or []),
            ]
        ]
        if deterministic_statuses and any(status != "pass" for status in deterministic_statuses):
            raise ValueError("deterministic contract requirements must pass before semantic review")
        dimensions = semantic_rule.get("dimensions") or [
            {"id": "goal-satisfaction", "description": "产物和检查是否满足已记录的用户目标"}
        ]
        evidence: list[dict[str, Any]] = []
        for row in self.store.execute(
            "SELECT * FROM check_runs WHERE task_case_id=? AND case_revision=? ORDER BY started_at, id",
            (task_case_id, current["current_revision"]),
        ).fetchall():
            item = self._check_evidence(row)
            outcome = item.get("outcome")
            validity = str(item.get("validity") or "untrusted")
            if item.get("freshness") == "stale":
                validity = "stale"
            elif validity not in {"valid", "environment-mismatch", "untrusted"}:
                validity = "untrusted"
            trusted = item.get("trust_level") in {"trusted", "sandboxed"}
            hard = bool(current.get("hard_failure")) and outcome == "assertion-fail" and trusted
            content = _json({
                "checker": item.get("checker_id"), "checkerVersion": item.get("checker_version"),
                "approvalVersion": item.get("approval_version"), "outcome": outcome,
                "assertions": item.get("assertions"), "validity": validity,
            })
            evidence.append({
                "id": f"check:{item['id']}", "type": "deterministic-check",
                "content_hash": _digest(content), "locator": {"check_run_id": item["id"]},
                "content": content, "polarity": "negative" if outcome == "assertion-fail" else "positive",
                "trust_level": "trusted" if trusted else "untrusted", "validity": validity,
                "assertion_outcome": outcome if outcome in {"assertion-pass", "assertion-fail", "inconclusive", "not-applicable"} else "inconclusive",
                "hard_failure": hard,
            })
        for row in self.store.execute(
            "SELECT * FROM artifacts WHERE task_case_id=? AND case_revision=? ORDER BY created_at, id",
            (task_case_id, current["current_revision"]),
        ).fetchall():
            artifact = _decode(row)
            metadata = artifact.get("metadata") or {}
            excerpt = str(metadata.get("excerpt") or "").strip()
            content = excerpt or _json({"kind": metadata.get("kind"), "contentHash": artifact.get("content_hash")})
            current_artifact = artifact.get("freshness") == "current"
            evidence.append({
                "id": f"artifact:{artifact['id']}", "type": "artifact",
                "content_hash": artifact.get("content_hash") or _digest(content),
                "locator": {"artifact_id": artifact["id"]}, "content": content,
                "polarity": "positive" if excerpt else "context",
                "trust_level": "trusted" if excerpt and current_artifact else "untrusted",
                "validity": "stale" if artifact.get("freshness") == "stale" else "valid",
                "assertion_outcome": "inconclusive", "hard_failure": False,
            })
        if not evidence:
            goal = str(current.get("goal_text") or "未记录用户目标")
            evidence.append({
                "id": f"goal:{task_case_id}", "type": "task-context", "content_hash": _digest(goal),
                "locator": {"task_case_id": task_case_id}, "content": goal, "polarity": "context",
                "trust_level": "untrusted", "validity": "untrusted",
                "assertion_outcome": "inconclusive", "hard_failure": False,
            })
        payload = {
            "schema_version": SEMANTIC_SCHEMA_VERSION, "task_case_id": task_case_id,
            "assessment_id": current["id"],
            "case_revision": int(current["current_revision"]), "contract_version_id": contract_id,
            "task_type": str(current.get("task_type") or "unknown"),
            "source": str(current.get("source") or "unknown"),
            "goal": str(current.get("goal_text") or "未记录用户目标"),
            "rubric": {"dimensions": dimensions}, "evidence": evidence,
        }
        return payload, current
    # ------------------------------------------------------------ human writes
    def decision(self, review_task_id: str, *, actor_id: str, expected_revision: int, verdict: str, reason_code: str, note: str | None = None) -> dict[str, Any]:
        if verdict not in {"pass", "partial", "fail"}:
            raise ValueError("decision verdict must be pass, partial, or fail")
        feedback_task = self.store.execute(
            "SELECT feedback_signal_id FROM review_tasks WHERE id=?", (review_task_id,)
        ).fetchone()
        if feedback_task is not None and feedback_task["feedback_signal_id"]:
            raise EffectStoreError("feedback review tasks require the feedback Action API")
        return _safe_public(self.store.write_manual_decision(
            review_task_id, actor_id=actor_id, expected_revision=expected_revision,
            action="decision", verdict=verdict, reason_code=reason_code,
            note=redact_sensitive(note) if note else None,
        ))
    submit_decision = decision
    def claim(self, review_task_id: str, *, actor_id: str) -> dict[str, Any]:
        feedback_task = self.store.execute(
            "SELECT feedback_signal_id FROM review_tasks WHERE id=?", (review_task_id,)
        ).fetchone()
        if feedback_task is not None and feedback_task["feedback_signal_id"]:
            raise EffectStoreError("feedback review tasks require the feedback Action API")
        return _safe_public(self.store.claim_review_task(review_task_id, actor_id=actor_id))
    claim_review_task = claim
    def disposition(self, review_task_id: str, *, actor_id: str, expected_revision: int, disposition: str, reason_code: str, note: str | None = None) -> dict[str, Any]:
        if disposition not in {"not-assessable", "needs-evidence"}:
            raise ValueError("disposition must be not-assessable or needs-evidence")
        feedback_task = self.store.execute(
            "SELECT feedback_signal_id FROM review_tasks WHERE id=?", (review_task_id,)
        ).fetchone()
        if feedback_task is not None and feedback_task["feedback_signal_id"]:
            raise EffectStoreError("feedback review tasks require the feedback Action API")
        with self.store.transaction():
            decision = self.store.write_manual_decision(
                review_task_id, actor_id=actor_id, expected_revision=expected_revision,
                action="disposition", verdict=disposition, reason_code=reason_code,
                note=redact_sensitive(note) if note else None,
            )
            if disposition == "needs-evidence":
                self.store.execute(
                    "UPDATE review_tasks SET status='open', queue_reason='needs-evidence', updated_at=? WHERE id=?",
                    (_now(), review_task_id),
                )
        return _safe_public(decision)
    submit_disposition = disposition
    def correction(self, task_case_id: str, *, actor_id: str, expected_revision: int, correction_type: str, reason_code: str, assessment_id: str | None = None, payload: Any = None) -> dict[str, Any]:
        actor = self.store.execute("SELECT roles_json, active FROM actors WHERE id=?", (actor_id,)).fetchone()
        if actor is None or not actor["active"] or "reviewer" not in json.loads(actor["roles_json"]):
            raise EffectStoreError("corrections require an active reviewer")
        case = self.store.execute("SELECT current_revision FROM task_cases WHERE id=?", (task_case_id,)).fetchone()
        if case is None:
            raise KeyError(task_case_id)
        if assessment_id is not None:
            assessment = self.store.execute(
                "SELECT task_case_id, is_current FROM outcome_assessments WHERE id=?", (assessment_id,)
            ).fetchone()
            if assessment is None or assessment["task_case_id"] != task_case_id:
                raise EffectStoreError("correction assessment belongs to another task case")
        prior_revision = int(case["current_revision"])
        with self.store.transaction():
            correction = self.store.append_correction(
                task_case_id, actor_id=actor_id, expected_revision=expected_revision,
                correction_type=correction_type, reason_code=reason_code,
                assessment_id=assessment_id, payload=_redact_for_storage(payload),
            )
            current_revision = prior_revision + 1
            removed = set(str(item) for item in (payload or {}).get("remove_fact_types", [])) if isinstance(payload, Mapping) else set()
            rows = self.store.execute(
                """SELECT * FROM task_facts WHERE task_case_id=? AND case_revision=? AND status='accepted' ORDER BY id""", (task_case_id, prior_revision)
            ).fetchall()
            for row in rows:
                if row["fact_type"] in removed:
                    continue
                fact_id = _stable_id("fact-revision", task_case_id, current_revision, row["id"])
                self.store.execute(
                    """INSERT INTO task_facts( id, task_case_id, case_revision, fact_type, value_json, evidence_event_id, source_kind, producer_version, status, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
                    (fact_id, task_case_id, current_revision, row["fact_type"], row["value_json"],
                     row["evidence_event_id"], row["source_kind"], row["producer_version"],
                     row["confidence"], _now()),
                )
            if isinstance(payload, Mapping) and payload.get("task_type"):
                self.store.execute(
                    "UPDATE task_cases SET task_type=?, updated_at=? WHERE id=?",
                    (str(payload["task_type"]), _now(), task_case_id),
                )
            if isinstance(payload, Mapping) and payload.get("attributionId"):
                kind = str(payload.get("attributionKind") or "")
                if kind not in {"direct", "shared", "candidate", "rejected"}:
                    raise ValueError("attributionKind must be direct, shared, candidate, or rejected")
                status = "rejected" if kind == "rejected" else "active"
                changed = self.store.execute(
                    """UPDATE attribution_links SET attribution_kind=?, status=?
                       WHERE id=? AND task_case_id=?""",
                    (kind, status, str(payload["attributionId"]), task_case_id),
                )
                if changed.rowcount != 1:
                    raise KeyError(str(payload["attributionId"]))
            override = (
                str(payload["task_type"]) if isinstance(payload, Mapping) and "task_type" in payload else None
            )
            self._refresh_classification(
                task_case_id, current_revision, actor_id=actor_id, task_type_override=override,
            )
        return _safe_public(correction)
    submit_correction = correction
    def exception(self, task_case_id: str, *, assessment_id: str, actor_id: str, expected_revision: int, reason_code: str, scope: Any = None, expires_at: str | None = None) -> dict[str, Any]:
        return _safe_public(self.store.append_exception(
            task_case_id, assessment_id=assessment_id, actor_id=actor_id,
            expected_revision=expected_revision, reason_code=reason_code,
            scope=_redact_for_storage(scope), expires_at=expires_at,
        ))
    submit_exception = exception

    def invalidate_assessments_for_evidence(
        self, task_case_id: str, case_revision: int, *, queue_reason: str
    ) -> int:
        with self.store.transaction():
            current = self.store.execute(
                """SELECT id FROM outcome_assessments WHERE task_case_id=?
                   AND case_revision=? AND is_current=1""",
                (task_case_id, case_revision),
            ).fetchall()
            self.store.execute(
                """UPDATE outcome_assessments SET is_current=0, process_state='invalidated',
                       freshness='stale' WHERE task_case_id=? AND case_revision=? AND is_current=1""",
                (task_case_id, case_revision),
            )
            for assessment in current:
                task = self.store.execute(
                    "SELECT id FROM review_tasks WHERE assessment_id=? ORDER BY created_at DESC LIMIT 1",
                    (assessment["id"],),
                ).fetchone()
                if task is None:
                    self.store.create_review_task(
                        task_case_id, assessment["id"], queue_reason
                    )
                else:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open', queue_reason=?, updated_at=?
                           WHERE id=?""",
                        (queue_reason, _now(), task["id"]),
                    )
        return len(current)
    # ---------------------------------------------------------- data governance
    def cleanup_derived_data(
        self,
        *,
        older_than: str | None = None,
        skill_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if not any((older_than, skill_id, project_id)):
            raise ValueError("cleanup requires older_than, skill_id, or project_id")
        conditions = ["1=1"]
        parameters: list[Any] = []
        if older_than:
            parsed = datetime.fromisoformat(str(older_than).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("older_than must include a timezone")
            older_than = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            conditions.append("c.created_at < ?")
            parameters.append(older_than)
            conditions.append(
                """NOT EXISTS (
                     SELECT 1 FROM artifacts recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM check_runs recent WHERE recent.task_case_id=c.id AND recent.started_at>=?
                     UNION ALL SELECT 1 FROM semantic_reviews recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM manual_decisions recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM corrections recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM exceptions recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM artifact_manifests recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM prospective_events recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM task_facts recent WHERE recent.task_case_id=c.id AND recent.created_at>=?
                     UNION ALL SELECT 1 FROM task_case_episodes ce JOIN task_episodes recent
                       ON recent.id=ce.task_episode_id WHERE ce.task_case_id=c.id AND recent.updated_at>=?
                     UNION ALL SELECT 1 FROM task_case_episodes ce JOIN tool_calls recent
                       ON recent.task_episode_id=ce.task_episode_id WHERE ce.task_case_id=c.id AND recent.called_at>=?
                     UNION ALL SELECT 1 FROM task_case_episodes ce JOIN tool_calls call
                       ON call.task_episode_id=ce.task_episode_id JOIN tool_results recent
                       ON recent.tool_call_id=call.id WHERE ce.task_case_id=c.id AND recent.completed_at>=?
                     UNION ALL SELECT 1 FROM feedback_signals signal
                       JOIN feedback_signal_revisions recent ON recent.feedback_signal_id=signal.id
                       WHERE recent.created_at>=? AND (
                         signal.feedback_case_id=c.id OR EXISTS (
                           SELECT 1 FROM feedback_targets target WHERE target.feedback_signal_id=signal.id
                             AND COALESCE(target.target_task_case_id,target.context_task_case_id)=c.id
                         )
                       )
                     UNION ALL SELECT 1 FROM feedback_actions recent
                       JOIN feedback_signals signal ON signal.id=recent.feedback_signal_id
                       WHERE recent.created_at>=? AND (
                         signal.feedback_case_id=c.id OR EXISTS (
                           SELECT 1 FROM feedback_targets target WHERE target.feedback_signal_id=signal.id
                             AND COALESCE(target.target_task_case_id,target.context_task_case_id)=c.id
                         )
                       )
                   )"""
            )
            parameters.extend([older_than] * 14)
        if skill_id:
            conditions.append(
                "EXISTS (SELECT 1 FROM attribution_links l JOIN skill_invocations i "
                "ON i.id=l.skill_invocation_id WHERE l.task_case_id=c.id AND i.skill_id=?)"
            )
            parameters.append(skill_id)
        if project_id:
            conditions.append(
                """(json_extract(c.metadata_json, '$.projectId') = ? OR EXISTS (
                     SELECT 1 FROM artifacts a JOIN artifact_manifests m
                       ON m.id=json_extract(a.metadata_json, '$.manifestId')
                     WHERE a.task_case_id=c.id
                       AND json_extract(m.manifest_json, '$.selector.projectId')=?
                   ))"""
            )
            parameters.extend((project_id, project_id))
        cases = [row[0] for row in self.store.execute(
            f"SELECT c.id FROM task_cases c WHERE {' AND '.join(conditions)}", parameters
        ).fetchall()]
        shared_skill_cases_retained = 0
        if skill_id:
            isolated_cases: list[str] = []
            for case_id in cases:
                other_skill = self.store.execute(
                    """SELECT 1 FROM attribution_links l JOIN skill_invocations i
                       ON i.id=l.skill_invocation_id WHERE l.task_case_id=?
                       AND l.status='active' AND i.skill_id<>? LIMIT 1""",
                    (case_id, skill_id),
                ).fetchone()
                if other_skill is None:
                    isolated_cases.append(case_id)
                else:
                    shared_skill_cases_retained += 1
            cases = isolated_cases
        selected_case_ids = set(cases)
        audit_id = str(uuid.uuid4())
        counts: dict[str, int] = {}
        criteria = {"olderThan": older_than, "skillId": skill_id, "projectId": project_id}
        criteria_hash = _digest(criteria)
        now = _now()
        purged = _json({"purged": True})
        shared_evidence_retained = 0
        with self.store.transaction():
            for case_id in cases:
                episode_rows = self.store.execute(
                    """SELECT ep.id, ep.session_id FROM task_case_episodes ce
                       JOIN task_episodes ep ON ep.id=ce.task_episode_id WHERE ce.task_case_id=?""",
                    (case_id,),
                ).fetchall()
                shared_episode_ids = {
                    row["id"] for row in episode_rows
                    if self.store.execute(
                        "SELECT 1 FROM task_case_episodes WHERE task_episode_id=? AND task_case_id<>? LIMIT 1",
                        (row["id"], case_id),
                    ).fetchone() is not None
                    and any(
                        linked[0] not in selected_case_ids for linked in self.store.execute(
                            "SELECT task_case_id FROM task_case_episodes WHERE task_episode_id=?",
                            (row["id"],),
                        ).fetchall()
                    )
                }
                exclusive_episode_ids = [
                    row["id"] for row in episode_rows if row["id"] not in shared_episode_ids
                ]
                shared_evidence_retained += len(shared_episode_ids)
                event_ids = [row[0] for row in self.store.execute(
                    """SELECT DISTINCT all_events.event_id FROM task_case_episodes ce
                         JOIN task_episodes ep ON ep.id=ce.task_episode_id
                         JOIN event_provenance start_p ON start_p.event_id=ep.start_event_id
                         JOIN event_provenance end_p ON end_p.event_id=ep.end_event_id
                           AND end_p.generation_id=start_p.generation_id
                         JOIN event_provenance all_events ON all_events.generation_id=start_p.generation_id
                           AND all_events.byte_start BETWEEN start_p.byte_start AND end_p.byte_start
                         WHERE ce.task_case_id=?
                       UNION SELECT start_event_id FROM task_episodes ep JOIN task_case_episodes ce
                         ON ce.task_episode_id=ep.id WHERE ce.task_case_id=? AND start_event_id IS NOT NULL
                       UNION SELECT end_event_id FROM task_episodes ep JOIN task_case_episodes ce
                         ON ce.task_episode_id=ep.id WHERE ce.task_case_id=? AND end_event_id IS NOT NULL
                       UNION SELECT event_id FROM skill_invocations i JOIN task_case_episodes ce
                         ON ce.task_episode_id=i.task_episode_id WHERE ce.task_case_id=?
                       UNION SELECT c.event_id FROM tool_calls c JOIN task_case_episodes ce
                         ON ce.task_episode_id=c.task_episode_id WHERE ce.task_case_id=?
                       UNION SELECT r.event_id FROM tool_results r JOIN tool_calls c ON c.id=r.tool_call_id
                         JOIN task_case_episodes ce ON ce.task_episode_id=c.task_episode_id
                         WHERE ce.task_case_id=? AND r.event_id IS NOT NULL
                       UNION SELECT event.id FROM task_case_episodes ce
                         JOIN task_episodes ep ON ep.id=ce.task_episode_id
                         JOIN sessions s ON s.id=ep.session_id
                         JOIN canonical_events event ON event.source=s.source
                           AND event.session_family=s.session_family
                         WHERE ce.task_case_id=? AND event.event_type IN ('session_meta', 'session_info')""",
                    (case_id, case_id, case_id, case_id, case_id, case_id, case_id),
                ).fetchall()]
                protected_event_ids: set[str] = set()
                for episode_id in shared_episode_ids:
                    protected_event_ids.update(row[0] for row in self.store.execute(
                        """SELECT ep.start_event_id FROM task_episodes ep WHERE ep.id=?
                           UNION SELECT ep.end_event_id FROM task_episodes ep WHERE ep.id=?
                           UNION SELECT event_id FROM skill_invocations WHERE task_episode_id=?
                           UNION SELECT event_id FROM tool_calls WHERE task_episode_id=?
                           UNION SELECT r.event_id FROM tool_results r JOIN tool_calls c ON c.id=r.tool_call_id
                             WHERE c.task_episode_id=? AND r.event_id IS NOT NULL
                           UNION SELECT all_events.event_id FROM task_episodes ep
                             JOIN event_provenance start_p ON start_p.event_id=ep.start_event_id
                             JOIN event_provenance end_p ON end_p.event_id=ep.end_event_id
                               AND end_p.generation_id=start_p.generation_id
                             JOIN event_provenance all_events ON all_events.generation_id=start_p.generation_id
                               AND all_events.byte_start BETWEEN start_p.byte_start AND end_p.byte_start
                             WHERE ep.id=?""",
                        (episode_id, episode_id, episode_id, episode_id, episode_id, episode_id),
                    ).fetchall() if row[0] is not None)
                shared_session_ids: set[str] = set()
                for session_id in {row["session_id"] for row in episode_rows}:
                    session_case_ids = {
                        linked[0] for linked in self.store.execute(
                            """SELECT DISTINCT ce.task_case_id FROM task_episodes ep
                               JOIN task_case_episodes ce ON ce.task_episode_id=ep.id
                               WHERE ep.session_id=?""", (session_id,),
                        ).fetchall()
                    }
                    if session_case_ids - selected_case_ids:
                        shared_session_ids.add(session_id)
                for session_id in shared_session_ids:
                    session = self.store.execute(
                        "SELECT source, session_family FROM sessions WHERE id=?", (session_id,)
                    ).fetchone()
                    if session is not None:
                        protected_event_ids.update(row[0] for row in self.store.execute(
                            """SELECT id FROM canonical_events WHERE source=? AND session_family=?
                               AND event_type IN ('session_meta', 'session_info')""",
                            (session["source"], session["session_family"]),
                        ).fetchall())
                event_ids = [event_id for event_id in event_ids if event_id not in protected_event_ids]
                for table in ("semantic_reviews", "evidence_items", "check_runs", "artifacts"):
                    cursor = self.store.execute(f"DELETE FROM {table} WHERE task_case_id=?", (case_id,))
                    counts[table] = counts.get(table, 0) + cursor.rowcount
                if exclusive_episode_ids:
                    placeholders = ",".join("?" for _ in exclusive_episode_ids)
                    self.store.execute(
                        f"""UPDATE tool_results SET excerpt=NULL, metadata_json=?
                            WHERE tool_call_id IN (SELECT id FROM tool_calls
                              WHERE task_episode_id IN ({placeholders}))""",
                        (purged, *exclusive_episode_ids),
                    )
                    self.store.execute(
                        f"UPDATE tool_calls SET arguments_json=? WHERE task_episode_id IN ({placeholders})",
                        (purged, *exclusive_episode_ids),
                    )
                    self.store.execute(
                        f"""UPDATE skill_invocations SET skill_path=NULL, metadata_json=?
                            WHERE task_episode_id IN ({placeholders})""",
                        (purged, *exclusive_episode_ids),
                    )
                    self.store.execute(
                        f"""UPDATE task_episodes SET goal_text=NULL, metadata_json=?,
                            invalidated_at=COALESCE(invalidated_at, ?), updated_at=?
                            WHERE id IN ({placeholders})""",
                        (purged, now, now, *exclusive_episode_ids),
                    )
                self.store.execute(
                    "UPDATE task_facts SET value_json='\"[PURGED]\"', status='revoked' WHERE task_case_id=?",
                    (case_id,),
                )
                self.store.execute(
                    """UPDATE outcome_assessments SET rationale_json=?, process_state='invalidated',
                       is_current=0 WHERE task_case_id=?""",
                    (purged, case_id),
                )
                self.store.execute("UPDATE manual_decisions SET note=NULL WHERE task_case_id=?", (case_id,))
                self.store.execute("UPDATE corrections SET payload_json=? WHERE task_case_id=?", (purged, case_id))
                self.store.execute('UPDATE "exceptions" SET scope_json=? WHERE task_case_id=?', (purged, case_id))
                self.store.execute(
                    "UPDATE task_classifications SET classification_json=? WHERE task_case_id=?",
                    (purged, case_id),
                )
                session_ids = {
                    row["session_id"] for row in episode_rows if row["id"] in exclusive_episode_ids
                }
                for session_id in session_ids:
                    linked_cases = {
                        row[0] for row in self.store.execute(
                            """SELECT DISTINCT ce.task_case_id FROM task_episodes ep
                               JOIN task_case_episodes ce ON ce.task_episode_id=ep.id
                               WHERE ep.session_id=?""", (session_id,),
                        ).fetchall()
                    }
                    if linked_cases - selected_case_ids:
                        continue
                    self.store.execute(
                        "UPDATE sessions SET title=NULL, metadata_json=? WHERE id=?",
                        (purged, session_id),
                    )
                    self.store.execute(
                        """UPDATE session_edges SET metadata_json=?
                           WHERE parent_session_id=? OR child_session_id=?""",
                        (purged, session_id, session_id),
                    )
                self.store.execute(
                    """UPDATE task_cases SET metadata_json=?,
                           invalidated_at=COALESCE(invalidated_at, ?), updated_at=? WHERE id=?""",
                    (purged, now, now, case_id),
                )
                for event_id in event_ids:
                    self.store.execute(
                        "UPDATE canonical_events SET payload_json=?, updated_at=? WHERE id=?",
                        (purged, now, event_id),
                    )
            summary = {
                "cases": len(cases), **counts, "manualRecordsRetained": True,
                "sharedEvidenceRetained": shared_evidence_retained,
                "sharedSkillCasesRetained": shared_skill_cases_retained,
            }
            self.store.execute(
                """INSERT INTO data_cleanup_audits(id, requested_at, older_than, skill_id_hash,
                       project_id_hash, affected_case_count, summary_json, criteria_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id, now, older_than,
                    hashlib.sha256(skill_id.encode()).hexdigest() if skill_id else None,
                    hashlib.sha256(project_id.encode()).hexdigest() if project_id else None,
                    len(cases), _json(summary), criteria_hash,
                ),
            )
        self.store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.store.execute("VACUUM")
        self.store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.store._secure_files()
        return {"auditId": audit_id, "criteriaSha256": criteria_hash, **summary}
    # ---------------------------------------------------------------- queries
    def overview(self) -> dict[str, Any]:
        result = self.store.overview()
        result["feedback"] = self.feedback.overview()
        return _safe_public(result)
    get_overview = overview
    def list_events(self, **filters: Any) -> dict[str, Any]:
        return _safe_public(self.store.list_events(**filters))
    list_skill_use_events = list_events
    def get_event(self, event_id_or_fingerprint: str) -> dict[str, Any]:
        return _safe_public(self.store.get_event(event_id_or_fingerprint))
    def get_case_detail(self, task_case_id: str) -> dict[str, Any]:
        case = self.store.execute("SELECT * FROM task_cases WHERE id=?", (task_case_id,)).fetchone()
        if case is None:
            raise KeyError(task_case_id)
        episodes = self.store.execute(
            """SELECT e.* FROM task_episodes e JOIN task_case_episodes ce ON ce.task_episode_id=e.id WHERE ce.task_case_id=? ORDER BY e.created_at, e.id""", (task_case_id,)
        ).fetchall()
        invocations = self.store.execute(
            """SELECT i.*, l.id AS attribution_id, l.attribution_kind,
                      l.status AS attribution_status FROM skill_invocations i
               JOIN attribution_links l ON l.skill_invocation_id=i.id
               WHERE l.task_case_id=? ORDER BY i.created_at, i.id""", (task_case_id,)
        ).fetchall()
        calls = self.store.execute(
            """SELECT c.*, r.id AS result_id, r.status AS result_status, r.output_hash, r.excerpt FROM tool_calls c JOIN task_case_episodes ce ON ce.task_episode_id=c.task_episode_id LEFT JOIN tool_results r ON r.tool_call_id=c.id WHERE ce.task_case_id=? ORDER BY c.called_at, c.id""", (task_case_id,)
        ).fetchall()
        assessments = self.store.execute(
            "SELECT * FROM outcome_assessments WHERE task_case_id=? ORDER BY revision", (task_case_id,)
        ).fetchall()
        facts = self.store.execute(
            "SELECT * FROM task_facts WHERE task_case_id=? ORDER BY created_at, id", (task_case_id,)
        ).fetchall()
        decisions = self.store.execute(
            "SELECT * FROM manual_decisions WHERE task_case_id=? ORDER BY revision", (task_case_id,)
        ).fetchall()
        review_tasks = self.store.execute(
            "SELECT * FROM review_tasks WHERE task_case_id=? ORDER BY created_at", (task_case_id,)
        ).fetchall()
        corrections = self.store.execute(
            "SELECT * FROM corrections WHERE task_case_id=? ORDER BY revision", (task_case_id,)
        ).fetchall()
        exceptions = self.store.execute(
            'SELECT * FROM "exceptions" WHERE task_case_id=? ORDER BY revision', (task_case_id,)
        ).fetchall()
        checks = self.store.execute(
            "SELECT * FROM check_runs WHERE task_case_id=? ORDER BY started_at, id", (task_case_id,)
        ).fetchall()
        artifacts = self.store.execute(
            "SELECT * FROM artifacts WHERE task_case_id=? ORDER BY created_at, id", (task_case_id,)
        ).fetchall()
        semantic_reviews = self.store.execute(
            "SELECT * FROM semantic_reviews WHERE task_case_id=? ORDER BY created_at, id",
            (task_case_id,),
        ).fetchall()
        provenance = self.store.execute(
            """SELECT DISTINCT e.id, e.event_fingerprint, e.event_type, e.protocol_time,
                      e.payload_hash, p.generation_id, p.line_number, p.byte_start
               FROM canonical_events e JOIN event_provenance p ON p.event_id=e.id
               WHERE e.id IN (
                 SELECT ep.start_event_id FROM task_episodes ep JOIN task_case_episodes ce
                   ON ce.task_episode_id=ep.id WHERE ce.task_case_id=?
                 UNION SELECT ep.end_event_id FROM task_episodes ep JOIN task_case_episodes ce
                   ON ce.task_episode_id=ep.id WHERE ce.task_case_id=?
                 UNION SELECT i.event_id FROM skill_invocations i JOIN task_case_episodes ce
                   ON ce.task_episode_id=i.task_episode_id WHERE ce.task_case_id=?
                 UNION SELECT c.event_id FROM tool_calls c JOIN task_case_episodes ce
                   ON ce.task_episode_id=c.task_episode_id WHERE ce.task_case_id=?
                 UNION SELECT r.event_id FROM tool_results r JOIN tool_calls c ON c.id=r.tool_call_id
                   JOIN task_case_episodes ce ON ce.task_episode_id=c.task_episode_id
                   WHERE ce.task_case_id=?
               ) ORDER BY e.protocol_time, p.generation_id, p.line_number""",
            (task_case_id, task_case_id, task_case_id, task_case_id, task_case_id),
        ).fetchall()
        result = {
            "case": _decode(case), "episodes": [_decode(row) for row in episodes],
            "invocations": [_decode(row) for row in invocations], "tool_calls": [_decode(row) for row in calls],
            "facts": [_decode(row) for row in facts], "assessments": [_decode(row) for row in assessments],
            "decisions": [_decode(row) for row in decisions],
            "review_tasks": [_decode(row) for row in review_tasks],
            "corrections": [_decode(row) for row in corrections],
            "exceptions": [_decode(row) for row in exceptions],
            "checks": [_decode(row) for row in checks],
            "artifacts": [_decode(row) for row in artifacts],
            "semantic_reviews": [_decode(row) for row in semantic_reviews],
            "feedback": self.feedback.case_feedback(task_case_id),
            "evidence": [
                {
                    "evidenceId": f"event:{row['id']}", "eventId": row["id"],
                    "fingerprint": row["event_fingerprint"], "contentHash": row["payload_hash"],
                    "type": row["event_type"], "observedAt": row["protocol_time"],
                    "locator": {"generationId": row["generation_id"], "line": row["line_number"], "byte": row["byte_start"]},
                }
                for row in provenance
            ] + [
                {
                    "evidenceId": f"check:{row['id']}", "type": "deterministic-check",
                    "contentHash": _digest(row["result_json"]),
                    "locator": {"checkRunId": row["id"]},
                }
                for row in checks
            ],
        }
        for invocation in result["invocations"]:
            invocation["skill_path"] = f"{invocation['skill_id']}/SKILL.md" if invocation.get("skill_path") else None
        for assessment in result["assessments"]:
            assessment.update(self.effective_projection(assessment["id"]))
        current = next((
            item for item in reversed(result["assessments"])
            if item.get("is_current") and not str(item.get("subject_key") or "").startswith("feedback:")
        ), None)
        result["current_outcome"] = ({
            "assessmentId": current["id"], "effectiveVerdict": current["effective_verdict"],
            "effectiveSource": current["effective_source"], "conflictState": current["conflict_state"],
        } if current else None)
        return _safe_public(result)
    case_detail = get_case_detail
    # --------------------------------------------------------------- metrics
    def metric_snapshot_candidates(
        self,
        *,
        cutoff_at: str | None = None,
        skill_id: str | None = None,
        task_type: str | None = None,
        scan_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build snapshot candidates solely from authoritative current DB rows."""
        conditions = [
            "l.status='active'", "i.validity='valid'", "i.load_status='loaded'",
            "i.invocation_kind='business-use'", "c.invalidated_at IS NULL",
        ]
        params: list[Any] = []
        if cutoff_at:
            conditions.append("i.created_at <= ?")
            params.append(cutoff_at)
            conditions.append("(a.id IS NULL OR a.created_at <= ?)")
            params.append(cutoff_at)
        if scan_run_id:
            conditions.append(
                """EXISTS (SELECT 1 FROM event_provenance p
                     JOIN log_file_generations g ON g.id=p.generation_id
                     WHERE p.event_id=i.event_id AND g.scan_run_id=?)"""
            )
            params.append(scan_run_id)
        if skill_id:
            conditions.append("i.skill_id = ?")
            params.append(skill_id)
        if task_type:
            conditions.append("c.task_type = ?")
            params.append(task_type)
        rows = self.store.execute(
            f"""SELECT c.id AS task_case_id, c.current_revision, c.task_type, i.id AS invocation_id,
                       i.skill_id, i.skill_sha256, l.attribution_kind, s.source,
                       event.protocol_time AS invocation_time,
                       a.id AS assessment_id, a.revision AS assessment_revision, a.contract_version_id,
                       a.process_state, a.assessability, a.automated_verdict, a.conflict_state, a.model_version,
                       a.prompt_version, a.rubric_version, a.classification_revision,
                       a.parser_version AS assessment_parser_version,
                       a.checker_version AS assessment_checker_version, a.rationale_json
                FROM task_cases c JOIN attribution_links l ON l.task_case_id=c.id
                JOIN skill_invocations i ON i.id=l.skill_invocation_id
                JOIN canonical_events event ON event.id=i.event_id
                JOIN task_episodes ep ON ep.id=i.task_episode_id JOIN sessions s ON s.id=ep.session_id
                LEFT JOIN outcome_assessments a ON a.task_case_id=c.id
                  AND a.skill_invocation_id=i.id AND a.is_current=1
                WHERE {' AND '.join(conditions)}
                ORDER BY c.id, i.skill_id, l.attribution_kind,
                         CASE WHEN a.id IS NOT NULL THEN 0 ELSE 1 END,
                         CASE WHEN i.skill_sha256 IS NULL THEN 1 ELSE 0 END,
                         COALESCE(event.protocol_time, i.created_at), i.id""",
            params,
        ).fetchall()
        candidates: dict[tuple[str, str, str | None, str | None, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["task_case_id"], row["skill_id"], row["skill_sha256"],
                row["contract_version_id"], row["attribution_kind"],
            )
            governance_exclusion: str | None = None
            contract_digest: str | None = None
            calibration_profile_id: str | None = None
            if not row["contract_version_id"]:
                governance_exclusion = "contract-missing"
            elif self.contracts is None:
                governance_exclusion = "contract-governance-unavailable"
            else:
                try:
                    contract = self.contracts.get(row["contract_version_id"])
                except Exception:
                    governance_exclusion = "contract-version-missing"
                else:
                    if contract.get("governance_status") != "approved" or contract.get("status") == "retired":
                        governance_exclusion = "contract-not-approved"
                    contract_digest = _digest({
                        "id": contract.get("id"), "contract": contract.get("contract"),
                        "governanceStatus": contract.get("governance_status"),
                        "approver": contract.get("approver"),
                    })
            if governance_exclusion is None and row["model_version"]:
                from semantic_reviewer import calibration_is_eligible
                try:
                    rationale = json.loads(row["rationale_json"])
                except (TypeError, json.JSONDecodeError):
                    rationale = {}
                semantic_review_id = rationale.get("semanticReviewId")
                semantic_row = self.store.execute(
                    "SELECT review_json FROM semantic_reviews WHERE id=? AND task_case_id=?",
                    (semantic_review_id, row["task_case_id"]),
                ).fetchone() if semantic_review_id else None
                semantic_record = json.loads(semantic_row["review_json"]) if semantic_row is not None else {}
                calibration_profile_id = semantic_record.get("calibration_profile_id")
                profile = self.store.execute(
                    "SELECT * FROM calibration_profiles WHERE id=?", (calibration_profile_id,)
                ).fetchone() if calibration_profile_id else None
                expected_tuple = {
                    "contract_version_id": row["contract_version_id"],
                    "task_type": row["task_type"] or "unknown", "source": row["source"],
                    "model_version": row["model_version"],
                    "prompt_version": row["prompt_version"] or "",
                    "rubric_version": row["rubric_version"] or "",
                }
                profile_metrics = json.loads(profile["metrics_json"]) if profile is not None else {}
                verdict_lower = (
                    profile["pass_precision_lower_bound"]
                    if profile is not None and row["automated_verdict"] == "pass"
                    else profile_metrics.get("precision", {}).get(
                        row["automated_verdict"], {}
                    ).get("lowerBound95", 0)
                )
                if (
                    profile is None or semantic_record.get("calibration_tuple") != expected_tuple
                    or not calibration_is_eligible(
                        profile["sample_count"], profile["major_task_sample_count"],
                        profile["pass_precision_lower_bound"],
                    )
                    or verdict_lower < 0.95
                ):
                    governance_exclusion = "semantic-calibration-ineligible"
            candidate = {
                "task_case_id": row["task_case_id"], "task_case_revision": row["current_revision"],
                "assessment_id": row["assessment_id"], "assessment_revision": row["assessment_revision"],
                "skill_id": row["skill_id"], "skill_sha256": row["skill_sha256"],
                "contract_version_id": row["contract_version_id"], "task_type": row["task_type"],
                "attribution_kind": row["attribution_kind"],
                "frozen": {
                    "skillInvocationId": row["invocation_id"], "source": "effect-store-v4",
                    "governanceExclusion": governance_exclusion,
                    "modelVersion": row["model_version"], "promptVersion": row["prompt_version"],
                    "rubricVersion": row["rubric_version"],
                    "classificationRevision": row["classification_revision"],
                    "assessmentParserVersion": row["assessment_parser_version"],
                    "assessmentCheckerVersion": row["assessment_checker_version"],
                    "calibrationProfileId": calibration_profile_id,
                    "contractDigest": contract_digest,
                },
            }
            existing = candidates.get(key)
            if existing is None:
                candidate["frozen"]["duplicateInvocationIds"] = [row["invocation_id"]]
                candidate["frozen"]["caseInvocationAnchor"] = row["invocation_id"]
                candidate["frozen"]["caseInvocationAnchorRule"] = "assessment-then-known-version-then-earliest"
                candidates[key] = candidate
            else:
                invocation_ids = existing["frozen"].setdefault("duplicateInvocationIds", [])
                invocation_ids.append(row["invocation_id"])
        return list(candidates.values())
    list_metric_candidates = metric_snapshot_candidates
    def create_metric_snapshot(
        self,
        *,
        cutoff_at: str | None = None,
        coverage_status: str | None = None,
        dimensions: Any = None,
        versions: Any = None,
        scan_run_id: str | None = None,
        summary: Any = None,
        skill_id: str | None = None,
        task_type: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        server_cutoff = _now()
        if cutoff_at is not None:
            requested = datetime.fromisoformat(str(cutoff_at).replace("Z", "+00:00"))
            if requested.tzinfo is None:
                raise ValueError("formal metric cutoff must include a timezone")
            server_time = datetime.fromisoformat(server_cutoff.replace("Z", "+00:00"))
            if abs((requested.astimezone(timezone.utc) - server_time).total_seconds()) > 5:
                raise ValueError("formal metrics can only be sealed at the current server time")
        cutoff_at = server_cutoff
        latest_scan = self.store.execute(
            """SELECT * FROM scan_runs WHERE finished_at IS NOT NULL AND finished_at<=?
               ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (cutoff_at,),
        ).fetchone()
        if latest_scan is None:
            raise ValueError("a completed scan run is required before sealing metrics")
        scan_metadata = json.loads(latest_scan["metadata_json"])
        if scan_metadata.get("scopeKind") != "configured-catalog":
            raise ValueError("formal metrics require a configured catalog scan")
        if scan_run_id is not None and scan_run_id != latest_scan["id"]:
            raise ValueError("formal metrics must use the latest scan run")
        scan_run_id = latest_scan["id"]
        coverage_status = (
            "complete" if latest_scan["status"] == "completed"
            and latest_scan["coverage_status"] == "complete" else "partial"
        )
        candidates = self.metric_snapshot_candidates(
            cutoff_at=cutoff_at, skill_id=skill_id, task_type=task_type,
            scan_run_id=scan_run_id,
        )
        dimensions = {
            "skillId": skill_id, "taskType": task_type,
            "scanScopeFingerprint": scan_metadata.get("scopeFingerprint"),
        }
        case_ids = sorted({item["task_case_id"] for item in candidates})
        checker_versions: list[str] = []
        classification_profiles: list[str] = []
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            checker_versions = sorted({
                str(row[0]) for row in self.store.execute(
                    f"""SELECT DISTINCT checker_id || '@' || checker_version || '#'
                               || COALESCE(approval_version, '') || ':'
                               || COALESCE(json_extract(result_json, '$.parser_version'), '')
                          FROM check_runs WHERE task_case_id IN ({placeholders})
                            AND freshness='current' ORDER BY 1""",
                    case_ids,
                ).fetchall()
            })
            classification_profiles = sorted({
                f"{row['profile_version']}@r{row['revision']}" for row in self.store.execute(
                    f"""SELECT DISTINCT profile_version, revision FROM task_classifications
                           WHERE task_case_id IN ({placeholders}) ORDER BY profile_version, revision""",
                    case_ids,
                ).fetchall()
            })
        versions = {
            "parserVersion": self.parser_version,
            "contractVersionIds": sorted({
                item["contract_version_id"] for item in candidates if item.get("contract_version_id")
            }),
            "checkerEvidenceTuples": checker_versions,
            "classificationProfiles": classification_profiles,
            "semanticTuples": sorted({
                _canonical([item["frozen"].get("modelVersion"), item["frozen"].get("promptVersion"),
                            item["frozen"].get("rubricVersion")])
                for item in candidates if item["frozen"].get("modelVersion")
            }),
            "calibrationProfileIds": sorted({
                item["frozen"]["calibrationProfileId"] for item in candidates
                if item["frozen"].get("calibrationProfileId")
            }),
            "contractDigests": sorted({
                item["frozen"]["contractDigest"] for item in candidates
                if item["frozen"].get("contractDigest")
            }),
        }
        generated_summary = {
            "candidateCount": len(candidates),
            "groupKeys": sorted({
                _canonical([
                    item["skill_id"], item.get("skill_sha256"), item.get("contract_version_id"),
                    item.get("task_type"), item["attribution_kind"],
                ])
                for item in candidates
            }),
        }
        snapshot = self.store.create_metric_snapshot(
            cutoff_at=cutoff_at, coverage_status=coverage_status, dimensions=dimensions,
            versions=versions, cases=candidates, scan_run_id=scan_run_id,
            summary=generated_summary, snapshot_id=snapshot_id,
        )
        snapshot["report"] = self.metric_snapshot_report(snapshot)
        return _safe_public(snapshot)

    @staticmethod
    def metric_snapshot_report(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        exclusions: dict[str, int] = {}
        for case in snapshot.get("cases", []):
            reason = case.get("exclusion_reason")
            if not case.get("metric_eligible"):
                exclusions[str(reason or "ineligible")] = exclusions.get(str(reason or "ineligible"), 0) + 1
                continue
            key = (
                case.get("skill_id"), case.get("skill_sha256"), case.get("contract_version_id"),
                case.get("task_type"), case.get("attribution_kind"),
            )
            group = groups.setdefault(key, {
                "skillId": key[0], "skillSha256": key[1], "contractVersionId": key[2],
                "taskType": key[3], "attributionKind": key[4],
                "denominator": 0, "pass": 0, "partial": 0, "fail": 0,
            })
            group["denominator"] += 1
            verdict = str(case.get("effective_verdict"))
            if verdict in {"pass", "partial", "fail"}:
                group[verdict] += 1
        rendered: list[dict[str, Any]] = []
        for group in groups.values():
            denominator = group["denominator"]
            rates: dict[str, Any] = {}
            for verdict in ("pass", "partial", "fail"):
                count = group[verdict]
                proportion = count / denominator if denominator else 0.0
                z = 1.959963984540054
                scale = 1 + z * z / denominator
                center = (proportion + z * z / (2 * denominator)) / scale
                margin = z * math.sqrt(
                    proportion * (1 - proportion) / denominator + z * z / (4 * denominator * denominator)
                ) / scale
                rates[verdict] = {
                    "count": count,
                    "rate": proportion if denominator >= 20 else None,
                    "confidence95": [max(0.0, center - margin), min(1.0, center + margin)],
                }
            group["rates"] = rates
            rendered.append(group)
        rendered.sort(key=lambda item: (
            str(item["skillId"]), str(item["contractVersionId"]),
            str(item["taskType"]), str(item["attributionKind"]),
        ))
        return {
            "snapshotId": snapshot.get("id"), "coverageStatus": snapshot.get("coverage_status"),
            "included": sum(item["denominator"] for item in rendered),
            "excluded": sum(exclusions.values()), "exclusions": exclusions, "groups": rendered,
        }

__all__ = ["OutcomeReviewService", "PARSER_VERSION", "RevisionConflict"]