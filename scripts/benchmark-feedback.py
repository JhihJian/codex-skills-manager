#!/usr/bin/env python3
"""Generate a repeatable feedback query benchmark and JSON report."""
from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from effect_store import EffectStore
from feedback_service import FeedbackService


def now() -> str:
    return "2026-08-25T00:00:00.000000Z"


def batches(total: int, size: int = 5_000):
    for start in range(0, total, size):
        yield range(start, min(total, start + size))


def populate(path: Path, event_count: int, signal_count: int, target_count: int, action_count: int) -> int:
    with EffectStore(path, busy_timeout_ms=60_000) as store:
        case_count = max(1, min(signal_count, 10_000))
        with store.transaction():
            store.connection.executemany(
                """INSERT INTO task_cases(id, case_fingerprint, task_type, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, '{}', ?, ?)""",
                ((f"case-{i:08d}", f"case-fp-{i:08d}", ("coding", "test", "document")[i % 3], now(), now())
                 for i in range(case_count)),
            )
        for group in batches(event_count):
            with store.transaction():
                store.connection.executemany(
                    """INSERT INTO canonical_events(
                           id, event_fingerprint, source, session_family, source_event_id,
                           event_type, protocol_time, payload_hash, payload_json, orphaned,
                           created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'user_message', ?, ?, ?, 0, ?, ?)""",
                    ((f"event-{i:09d}", f"event-fp-{i:09d}", ("pi", "codex")[i % 2],
                      f"family-{i % 1000}", f"source-{i}", now(), f"hash-{i}",
                      json.dumps({"text": "result is still wrong"}, separators=(",", ":")), now(), now())
                     for i in group),
                )
        store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        baseline_bytes = path.stat().st_size
        for group in batches(signal_count):
            with store.transaction():
                store.connection.executemany(
                    """INSERT INTO feedback_signals(
                           id, logical_fingerprint, feedback_event_id, feedback_case_id,
                           current_process_state, current_resolution_state,
                           current_action_revision, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    ((f"signal-{i:08d}", f"signal-fp-{i:08d}", f"event-{i % event_count:09d}",
                      f"case-{i % case_count:08d}", ("queued", "claimed", "closed")[i % 3],
                      ("unreviewed", "action-required", "resolved-verified")[i % 3], now(), now())
                     for i in group),
                )
                store.connection.executemany(
                    """INSERT INTO feedback_signal_revisions(
                           id, feedback_signal_id, revision, revision_fingerprint, channel,
                           category, severity, authority, source, confidence, excerpt_hash,
                           redacted_excerpt, locator_json, detector_id, detector_version,
                           metadata_json, orphaned, is_current, observed_at, created_at)
                       VALUES (?, ?, 1, ?, ?, ?, ?, 'user', ?, ?, ?, 'redacted feedback', '{}',
                           'session-negative-feedback', 'feedback-v1', '{}', 0, 1, ?, ?)""",
                    ((f"revision-{i:08d}", f"signal-{i:08d}", f"revision-fp-{i:08d}",
                      ("user-feedback", "process-anomaly")[i % 2],
                      ("result-rejection", "observed-defect", "requirement-gap")[i % 3],
                      ("high", "medium", "low")[i % 3], ("pi", "codex")[i % 2],
                      0.60 + (i % 40) / 100, f"excerpt-{i}", now(), now())
                     for i in group),
                )
                store.connection.executemany(
                    "UPDATE feedback_signals SET current_machine_revision_id=? WHERE id=?",
                    ((f"revision-{i:08d}", f"signal-{i:08d}") for i in group),
                )
        actual_targets = max(signal_count, target_count)
        for group in batches(actual_targets):
            with store.transaction():
                store.connection.executemany(
                    """INSERT INTO feedback_targets(
                           id, feedback_signal_id, signal_revision_id, target_fingerprint,
                           rank, target_kind, context_task_case_id, target_task_case_id,
                           relation, confidence, machine_status, resolver_version,
                           evidence_json, created_at)
                       VALUES (?, ?, ?, ?, ?, 'task-result', ?, ?, 'previous-episode-result',
                           0.9, 'candidate', 'feedback-target-v1', '{}', ?)""",
                    ((f"target-{i:09d}", f"signal-{i % signal_count:08d}",
                      f"revision-{i % signal_count:08d}", f"target-fp-{i:09d}",
                      i // signal_count + 1, f"case-{i % case_count:08d}",
                      f"case-{i % case_count:08d}", now()) for i in group),
                )
        for group in batches(action_count):
            with store.transaction():
                store.connection.executemany(
                    """INSERT INTO feedback_actions(
                           id, feedback_signal_id, producer_kind, revision, action,
                           from_process_state, to_process_state, from_resolution_state,
                           to_resolution_state, reason_code, binding_json, created_at)
                       VALUES (?, ?, 'system', ?, 'detected', 'candidate', 'queued',
                           'unreviewed', 'unreviewed', 'benchmark', '{}', ?)""",
                    ((f"action-{i:09d}", f"signal-{i % signal_count:08d}",
                      i // signal_count + 1, now()) for i in group),
                )
        with store.transaction():
            store.connection.executemany(
                "UPDATE feedback_signals SET current_action_revision=? WHERE id=?",
                (((action_count - 1 - i) // signal_count + 1 if i < action_count else 0,
                  f"signal-{i:08d}") for i in range(signal_count)),
            )
        store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return baseline_bytes


def percentile95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20, method="inclusive")[18] if len(values) > 1 else values[0]


def query_once(path: Path, filters: dict) -> float:
    started = time.perf_counter()
    with EffectStore(path, busy_timeout_ms=60_000) as store:
        FeedbackService(store).list_signals(limit=100, **filters)
    return (time.perf_counter() - started) * 1000


def run_queries(path: Path, repetitions: int, clients: int) -> dict:
    filters = {
        "default": {}, "channel": {"channel": "user-feedback"},
        "category": {"category": "observed-defect"}, "severity": {"severity": "high"},
        "source": {"source": "pi"}, "confidence": {"min_confidence": 0.85},
        "target": {"target_kind": "task-result"},
        "process": {"process_state": "queued"},
    }
    report = {}
    for name, selected in filters.items():
        query_once(path, selected)
        values = [query_once(path, selected) for _ in range(repetitions)]
        report[name] = {"p95Ms": round(percentile95(values), 3), "medianMs": round(statistics.median(values), 3)}
    jobs = [(name, selected) for _ in range(repetitions) for name, selected in filters.items()]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=clients) as executor:
        values = list(executor.map(lambda job: query_once(path, job[1]), jobs))
    report["concurrent"] = {
        "clients": clients, "queries": len(jobs), "p95Ms": round(percentile95(values), 3),
        "wallMs": round((time.perf_counter() - started) * 1000, 3),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--signals", type=int, default=100_000)
    parser.add_argument("--targets", type=int, default=300_000)
    parser.add_argument("--actions", type=int, default=200_000)
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if min(args.events, args.signals, args.targets, args.actions, args.repetitions, args.clients) < 1:
        raise SystemExit("all benchmark sizes must be positive")
    temporary = tempfile.TemporaryDirectory() if args.database is None else None
    path = args.database or Path(temporary.name) / "feedback-benchmark.sqlite3"
    started = time.perf_counter()
    baseline_bytes = populate(path, args.events, args.signals, args.targets, args.actions)
    populate_ms = (time.perf_counter() - started) * 1000
    report = {
        "dataset": vars(args) | {"database": str(path)},
        "populateMs": round(populate_ms, 3),
        "databaseBytes": path.stat().st_size,
        "feedbackBytes": path.stat().st_size - baseline_bytes,
        "bytesPerSignal": round((path.stat().st_size - baseline_bytes) / args.signals, 2),
        "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "queries": run_queries(path, args.repetitions, args.clients),
    }
    with EffectStore(path) as store:
        report["feedbackIndexes"] = sorted(
            row["name"] for row in store.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_feedback_%'"
            ).fetchall()
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if temporary is not None:
        temporary.cleanup()


if __name__ == "__main__":
    main()