"""Persistence and review workflow for session feedback signals.

The detector remains pure in :mod:`feedback_detector`; this module owns the
transactional projection into the effect-store v6 schema.  It intentionally
does not depend on ``OutcomeReviewService`` so that the latter can call it from
its scan loop without creating an import cycle.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import redact_sensitive
from effect_store import EffectStore, EffectStoreError, RevisionConflict
from feedback_detector import (
    DETECTOR_VERSION, SPAN_PARSER_VERSION, detect_assistant_claims, detect_process_anomalies,
    detect_user_feedback, is_positive_resolution, normalize_process_plan,
)


RESOLVER_VERSION = "feedback-target-v1"
_ASSESSMENT_REVISION_BASE = 1_000_000_000
_CLOSED_RESOLUTIONS = {
    "resolved-verified", "resolved-unverified", "not-actionable",
    "false-positive", "duplicate",
}
_REVIEWER_REASON_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return _canonical({} if value is None else value)


def _mentions_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    return re.search(
        rf"(?<![\w._:/@+\-]){re.escape(identifier)}(?![\w._:/@+\-])",
        text, re.IGNORECASE,
    ) is not None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _first(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _decoded(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json") and isinstance(result[key], str):
            try:
                result[key[:-5]] = json.loads(result.pop(key))
            except json.JSONDecodeError:
                pass
    return result


def _candidate_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _plan_dict(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else _mapping(value)


class FeedbackService:
    """Store, query, and review negative-feedback and process signals."""

    def __init__(
        self,
        store: EffectStore,
        *,
        detector_id: str = "session-negative-feedback",
        detector_version: str = DETECTOR_VERSION,
        resolver_version: str = RESOLVER_VERSION,
        formal_scope_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(store, EffectStore):
            raise TypeError("FeedbackService requires an EffectStore")
        self.store = store
        self.detector_id = str(detector_id)
        self.detector_version = str(detector_version)
        self.resolver_version = str(resolver_version)
        self.formal_scope_fingerprint = formal_scope_fingerprint
        self._defer_cluster_projection = False
        self._return_full_signal = True
        self._bootstrap_reparse_count = 0

    # -------------------------------------------------------------- derivation
    def derive_user_event(
        self,
        event_row: Mapping[str, Any],
        own_case_id: str | None,
        previous_context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Persist adapter candidates, falling back to deterministic text detection."""
        event, payload, metadata = self._event_parts(event_row)
        event_id = str(event.get("id") or "")
        if not event_id:
            raise ValueError("event_row.id is required")
        context = dict(previous_context or {})
        blocks = _first(payload, "content", "text")
        if blocks is None:
            blocks = _first(event, "content", "text") or ""
        block_hint = _canonical(blocks) if not isinstance(blocks, str) else blocks
        positive_resolution = bool(re.search(
            r"(?i)(?:没问题|现在可以|现在正常|不再报错|已经修复|works? now|all good|no longer)",
            block_hint,
        )) and is_positive_resolution(blocks)
        raw_candidates = (
            _items(metadata.get("feedback_candidates"))
            if metadata.get("feedback_detector_version") == self.detector_version else []
        )
        if not raw_candidates:
            ambiguous = bool(_first(context, "target_ambiguous", "targetAmbiguous"))
            raw_candidates = list(detect_user_feedback(
                blocks, event_id=event_id, has_previous_result=True,
                target_ambiguous=ambiguous,
            ))
        if (positive_resolution or raw_candidates) and not self._context_has_target(context):
            context.update(self._previous_context(event))
        if positive_resolution and not raw_candidates:
            self._resolve_by_user_acceptance(event, context)

        persisted: list[dict[str, Any]] = []
        for raw in raw_candidates:
            candidate = _candidate_dict(raw)
            if not candidate:
                continue
            targets = self.resolve_targets(candidate, event, context, own_case_id=own_case_id)
            if not targets or self._targets_ambiguous(targets):
                candidate["confidence"] = min(float(candidate.get("confidence", 0.0)), 0.79)
            persisted.append(self._persist_candidate(
                event, own_case_id, candidate, targets,
                source=str(event.get("source") or payload.get("source") or "unknown"),
            ))
        return persisted

    derive_user_feedback = derive_user_event

    def _resolve_by_user_acceptance(
        self, event: Mapping[str, Any], context: Mapping[str, Any],
    ) -> int:
        case_id = _first(context, "previous_task_case_id", "previousTaskCaseId")
        if not case_id:
            return 0
        rows = self.store.execute(
            """SELECT s.* FROM feedback_signals s
               JOIN feedback_targets t ON t.id=s.current_confirmed_target_id
               JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               JOIN canonical_events feedback_event ON feedback_event.id=s.feedback_event_id
               WHERE r.channel='user-feedback' AND r.orphaned=0
                 AND COALESCE(t.target_task_case_id,t.context_task_case_id)=?
                 AND s.current_resolution_state='awaiting-verification'
                 AND COALESCE(feedback_event.protocol_time,feedback_event.created_at)
                     < COALESCE(?, ?)""",
            (case_id, event.get("protocol_time"), event.get("created_at") or _now()),
        ).fetchall()
        if len(rows) != 1:
            return 0
        resolved = 0
        with self.store.transaction():
            for signal in rows:
                evidence_id = _digest("feedback-user-acceptance", signal["id"], event["id"])
                self.store.execute(
                    """INSERT OR IGNORE INTO evidence_items(
                           id, evidence_fingerprint, task_case_id, event_id, evidence_type,
                           content_hash, locator_json, validity, polarity, category,
                           confidence, rule_id, producer_version, observed_at, created_at)
                       VALUES (?, ?, ?, ?, 'user-acceptance', ?, ?, 'valid', 'positive',
                           'resolved-verified', 1.0, 'explicit-user-acceptance', ?, ?, ?)""",
                    (
                        evidence_id, evidence_id, case_id, event["id"],
                        str(event.get("payload_hash") or _digest(event["id"])),
                        _json({"eventId": event["id"], "source": event.get("source")}),
                        self.detector_version, event.get("protocol_time") or _now(), _now(),
                    ),
                )
                self._append_system_action(
                    signal["id"], "resolve-verified", "explicit-user-acceptance",
                    process_state="closed", resolution_state="resolved-verified",
                    binding={"userAcceptanceEventId": event["id"], "evidenceId": evidence_id},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        "UPDATE review_tasks SET status='decided', updated_at=? WHERE id=?",
                        (_now(), review["id"]),
                    )
                self._refresh_cluster(signal["id"])
                resolved += 1
        return resolved

    def derive_assistant_event(
        self, event_row: Mapping[str, Any], own_case_id: str | None,
    ) -> list[dict[str, Any]]:
        event, payload, metadata = self._event_parts(event_row)
        event_id = str(event.get("id") or "")
        raw_candidates = (
            _items(metadata.get("assistant_claims"))
            if metadata.get("feedback_detector_version") == self.detector_version else []
        )
        if not raw_candidates:
            raw_candidates = list(detect_assistant_claims(
                _first(payload, "content", "text") or _first(event, "content", "text") or "",
                event_id=event_id,
            ))
        if not raw_candidates:
            return []
        persisted: list[dict[str, Any]] = []
        context = self._previous_context(event)
        for raw in raw_candidates:
            candidate = _candidate_dict(raw)
            targets = self.resolve_targets(candidate, event, context, own_case_id=own_case_id)
            persisted.append(self._persist_candidate(
                event, own_case_id, candidate, targets,
                source=str(event.get("source") or payload.get("source") or "unknown"),
            ))
        return persisted

    def derive_process_result(
        self,
        event_row: Mapping[str, Any],
        call_context: Mapping[str, Any] | None = None,
        target_context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Detect and persist structured process anomalies for one result event."""
        event, payload, metadata = self._event_parts(event_row)
        call = dict(call_context or {})
        target = dict(target_context or {})
        event_id = str(event.get("id") or "")
        if not event_id:
            raise ValueError("event_row.id is required")
        tool_name = str(_first(call, "tool_name", "toolName", "name") or
                        _first(payload, "tool_name", "toolName") or "unknown-tool")
        args = _first(call, "args", "arguments")
        if args is None:
            args = _first(payload, "args", "arguments")
        details = _first(metadata, "process_details", "processDetails", "details")
        if details is None:
            details = _first(payload, "details", "result")
        outcome = _first(payload, "outcome", "status")
        if outcome is None and "error" in payload:
            outcome = {"isError": bool(payload.get("error"))}
        available = _first(call, "available_agents", "availableAgents") or ()
        episode_closed = bool(_first(call, "episode_closed", "episodeClosed")
                              if _first(call, "episode_closed", "episodeClosed") is not None else True)
        anomalies = detect_process_anomalies(
            tool_name, args, details, outcome,
            available_agents=available if not isinstance(available, str) else (),
            episode_closed=episode_closed,
        )
        own_case = _first(target, "own_case_id", "ownCaseId", "feedback_case_id", "feedbackCaseId")
        context = {**call, **target}
        stored_call_id = _first(call, "stored_tool_call_id", "storedToolCallId")
        if stored_call_id:
            self._resolve_result_missing(str(stored_call_id), event_id)
        if not anomalies:
            plan = normalize_process_plan(tool_name, args, details, available_agents=available)
            self._resolve_by_process_retry(
                str(own_case) if own_case else None, tool_name, plan,
                verification_event_id=event_id,
            )
        persisted: list[dict[str, Any]] = []
        for anomaly in anomalies:
            item = asdict(anomaly)
            plan = _plan_dict(item.pop("plan", {}))
            locator = {"kind": "process-anomaly", "category": anomaly.category}
            candidate = {
                **item,
                "span": {
                    "event_id": event_id,
                    "block_index": 0,
                    "start": 0,
                    "end": 0,
                    "origin": "tool-output",
                    "excerpt_hash": _digest(event.get("payload_hash"), anomaly.category, anomaly.reason),
                    "redacted_excerpt": anomaly.reason[:512],
                    "protocol_locator": f"process:{anomaly.category}",
                    "redaction_status": "clean",
                },
                "metadata": {
                    "plan": plan, "resultIndexes": list(anomaly.result_indexes),
                    "toolName": tool_name, "reason": anomaly.reason,
                },
                "locator": locator,
            }
            targets = self.resolve_targets(candidate, event, context, own_case_id=own_case, process=True)
            if not targets or self._targets_ambiguous(targets):
                candidate["confidence"] = min(float(candidate["confidence"]), 0.79)
            persisted.append(self._persist_candidate(
                event, str(own_case) if own_case else None, candidate, targets,
                source=str(event.get("source") or "unknown"),
            ))
        return persisted

    derive_process_anomalies = derive_process_result

    def _resolve_result_missing(self, tool_call_id: str, result_event_id: str) -> int:
        rows = self.store.execute(
            """SELECT DISTINCT s.* FROM feedback_signals s
               JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               JOIN feedback_targets t ON t.feedback_signal_id=s.id
               WHERE r.category='result-missing' AND r.orphaned=0
                 AND t.tool_call_id=? AND t.signal_revision_id=s.current_machine_revision_id
                 AND s.current_resolution_state NOT IN
                   ('resolved-verified','resolved-unverified','not-actionable','false-positive','duplicate')""",
            (tool_call_id,),
        ).fetchall()
        with self.store.transaction():
            for signal in rows:
                self._append_system_action(
                    signal["id"], "resolve-verified", "tool-result-arrived",
                    process_state="closed", resolution_state="resolved-verified",
                    binding={"toolCallId": tool_call_id, "resultEventId": result_event_id},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        "UPDATE review_tasks SET status='decided', updated_at=? WHERE id=?",
                        (_now(), review["id"]),
                    )
                self._remove_cluster_memberships(signal["id"])
        return len(rows)

    def _resolve_by_process_retry(
        self, case_id: str | None, tool_name: str, plan: Any,
        *, verification_event_id: str,
    ) -> int:
        if (
            not case_id or plan.planned_count <= 0 or not plan.expected_result_ids
            or plan.started_count != plan.planned_count
            or plan.completed_count != plan.planned_count
            or set(plan.expected_result_ids) != set(plan.returned_result_ids)
        ):
            return 0
        requested = list(plan.requested_agents)
        rows = self.store.execute(
            """SELECT DISTINCT s.* FROM feedback_signals s
               JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               JOIN feedback_targets t ON t.feedback_signal_id=s.id
               WHERE r.channel='process-anomaly' AND r.orphaned=0
                 AND COALESCE(t.target_task_case_id,t.context_task_case_id)=?
                 AND json_extract(r.metadata_json, '$.toolName')=?
                 AND json_extract(r.metadata_json, '$.plan.planned_count')=?
                 AND t.signal_revision_id=s.current_machine_revision_id
                 AND t.id=s.current_confirmed_target_id
                 AND t.machine_status='candidate'
                 AND s.current_resolution_state='awaiting-verification'""",
            (case_id, tool_name, plan.planned_count),
        ).fetchall()
        resolved = 0
        with self.store.transaction():
            for signal in rows:
                revision = self.store.execute(
                    "SELECT metadata_json FROM feedback_signal_revisions WHERE id=?",
                    (signal["current_machine_revision_id"],),
                ).fetchone()
                previous_plan = _mapping(_mapping(revision["metadata_json"]).get("plan"))
                if list(previous_plan.get("requested_agents") or []) != requested:
                    continue
                if list(previous_plan.get("expected_result_ids") or []) != list(plan.expected_result_ids):
                    continue
                if previous_plan.get("mode") != plan.mode:
                    continue
                self._append_system_action(
                    signal["id"], "resolve-verified", "process-plan-completed",
                    process_state="closed", resolution_state="resolved-verified",
                    binding={
                        "verificationEventId": verification_event_id,
                        "plannedCount": plan.planned_count,
                        "startedCount": plan.started_count,
                        "completedCount": plan.completed_count,
                        "requestedAgents": requested,
                        "returnedResultIds": list(plan.returned_result_ids),
                    },
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        "UPDATE review_tasks SET status='decided', updated_at=? WHERE id=?",
                        (_now(), review["id"]),
                    )
                self._refresh_cluster(signal["id"])
                resolved += 1
        return resolved

    def resolve_targets(
        self,
        candidate: Mapping[str, Any],
        event_row: Mapping[str, Any],
        context: Mapping[str, Any] | None,
        *,
        own_case_id: str | None = None,
        process: bool = False,
    ) -> list[dict[str, Any]]:
        """Return verified target candidates in resolver priority order."""
        event_row = dict(event_row)
        ctx = dict(context or {})
        text = str(_mapping(candidate.get("span")).get("redacted_excerpt") or "")
        targets: list[dict[str, Any]] = []
        session_ids = self._session_ids_for_case(own_case_id) if own_case_id else ()
        if session_ids:
            session_filter = f"ep.session_id IN ({','.join('?' for _ in session_ids)})"
            session_params: tuple[Any, ...] = tuple(session_ids)
        else:
            session_filter = "session.session_family=?"
            session_params = (event_row.get("session_family"),)

        for raw in _items(_first(ctx, "targets", "target_candidates", "targetCandidates")):
            descriptor = self._normalize_target(_mapping(raw), own_case_id, relation="explicit-reference")
            if descriptor:
                targets.append(descriptor)

        explicit_specs = (
            ("skill-invocation", ("skill_invocation_id", "skillInvocationId")),
            ("tool-result", ("tool_result_id", "toolResultId")),
            ("tool-call", ("tool_call_id", "toolCallId")),
        )
        for kind, names in explicit_specs:
            value = _first(ctx, *names)
            values = value if isinstance(value, (list, tuple)) else ([value] if value else [])
            for identifier in values:
                descriptor = self._target_for_id(kind, str(identifier), own_case_id, "explicit-reference", 0.99)
                if descriptor:
                    targets.append(descriptor)

        # Names in authored feedback are explicit only when the actual stored
        # invocation/call name occurs in the excerpt.
        explicit_name_hint = bool(re.search(
            r"(?i)(?:\bskill\b|技能|\btool\b|工具|\b(?:bash|read|write|edit|subagent|agent)\b|代理)",
            text,
        ))
        if text and explicit_name_hint and not process:
            skill_rows = self.store.execute(
                """SELECT i.id, i.skill_id, ce.task_case_id FROM skill_invocations i
                   JOIN task_case_episodes ce ON ce.task_episode_id=i.task_episode_id
                   JOIN task_episodes ep ON ep.id=i.task_episode_id
                   JOIN sessions session ON session.id=ep.session_id
                   WHERE i.validity='valid' AND """ + session_filter + """
                   ORDER BY i.created_at DESC""", session_params,
            ).fetchall()
            for row in skill_rows:
                if row["skill_id"] and _mentions_identifier(text, str(row["skill_id"])):
                    descriptor = self._target_for_id(
                        "skill-invocation", row["id"], own_case_id,
                        "same-message-reference", 0.98,
                    )
                    if descriptor:
                        targets.append(descriptor)
            tool_rows = self.store.execute(
                """SELECT c.id, c.tool_name FROM tool_calls c
                   JOIN task_episodes ep ON ep.id=c.task_episode_id
                   JOIN sessions session ON session.id=ep.session_id
                   WHERE """ + session_filter + """ ORDER BY c.called_at DESC, c.id DESC""",
                session_params,
            ).fetchall()
            for row in tool_rows:
                name = str(row["tool_name"] or "")
                if name and _mentions_identifier(text, name):
                    descriptor = self._target_for_id(
                        "tool-call", row["id"], own_case_id,
                        "same-message-reference", 0.97,
                    )
                    if descriptor:
                        targets.append(descriptor)

        # Authored skill/tool names are more specific than a result or Case ID.
        if not targets:
            for kind, names in (
                ("assistant-result", ("target_event_id", "targetEventId", "assistant_event_id", "assistantEventId")),
                ("task-result", ("target_task_case_id", "targetTaskCaseId")),
            ):
                value = _first(ctx, *names)
                values = value if isinstance(value, (list, tuple)) else ([value] if value else [])
                for identifier in values:
                    descriptor = self._target_for_id(
                        kind, str(identifier), own_case_id, "explicit-reference", 0.99,
                    )
                    if descriptor:
                        targets.append(descriptor)

        if process and not targets:
            result_id = self._tool_result_for_event(str(event_row.get("id") or ""))
            call_id = _first(ctx, "stored_tool_call_id", "storedToolCallId")
            if result_id:
                descriptor = self._target_for_id("tool-result", result_id, own_case_id, "explicit-reference", 1.0)
                if descriptor:
                    targets.append(descriptor)
            elif call_id:
                descriptor = self._target_for_id("tool-call", str(call_id), own_case_id, "explicit-reference", 1.0)
                if descriptor:
                    targets.append(descriptor)

        if not targets:
            previous_event = _first(
                ctx, "previous_assistant_event_id", "previousAssistantEventId",
                "previous_result_event_id", "previousResultEventId",
            )
            if previous_event:
                descriptor = self._target_for_id(
                    "assistant-result", str(previous_event), own_case_id,
                    str(_first(ctx, "previous_relation", "previousRelation") or "previous-episode-result"),
                    float(_first(ctx, "previous_target_confidence", "previousTargetConfidence") or 0.92),
                )
                if descriptor:
                    targets.append(descriptor)
        if not targets:
            previous_case = _first(ctx, "previous_task_case_id", "previousTaskCaseId", "previous_case_id", "previousCaseId")
            if previous_case:
                descriptor = self._target_for_id(
                    "task-result", str(previous_case), own_case_id,
                    "previous-episode-result", 0.90,
                )
                if descriptor:
                    targets.append(descriptor)

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in targets:
            identity = self._target_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(item)
        return deduplicated

    def _normalize_target(
        self, raw: Mapping[str, Any], own_case_id: str | None, *, relation: str
    ) -> dict[str, Any] | None:
        kind = str(_first(raw, "target_kind", "targetKind", "kind") or "")
        names = {
            "task-result": ("target_task_case_id", "targetTaskCaseId", "id"),
            "assistant-result": ("target_event_id", "targetEventId", "id"),
            "process-plan": ("target_event_id", "targetEventId", "id"),
            "skill-invocation": ("skill_invocation_id", "skillInvocationId", "id"),
            "tool-call": ("tool_call_id", "toolCallId", "id"),
            "tool-result": ("tool_result_id", "toolResultId", "id"),
        }
        if kind not in names:
            return None
        identifier = _first(raw, *names[kind])
        if not identifier:
            return None
        return self._target_for_id(
            kind, str(identifier),
            str(_first(raw, "context_task_case_id", "contextTaskCaseId") or own_case_id or "") or None,
            str(_first(raw, "relation") or relation),
            float(_first(raw, "confidence") or 0.99),
        )

    def _target_for_id(
        self, kind: str, identifier: str, own_case_id: str | None,
        relation: str, confidence: float,
    ) -> dict[str, Any] | None:
        column_table = {
            "task-result": ("target_task_case_id", "task_cases"),
            "assistant-result": ("target_event_id", "canonical_events"),
            "process-plan": ("target_event_id", "canonical_events"),
            "skill-invocation": ("skill_invocation_id", "skill_invocations"),
            "tool-call": ("tool_call_id", "tool_calls"),
            "tool-result": ("tool_result_id", "tool_results"),
        }
        if kind not in column_table:
            return None
        column, table = column_table[kind]
        validity = " AND orphaned=0" if table == "canonical_events" else ""
        if self.store.execute(f"SELECT 1 FROM {table} WHERE id=?{validity}", (identifier,)).fetchone() is None:
            return None
        context_case = own_case_id
        if kind == "task-result":
            context_case = identifier
        elif kind == "skill-invocation":
            row = self.store.execute(
                """SELECT ce.task_case_id FROM skill_invocations i
                   JOIN task_case_episodes ce ON ce.task_episode_id=i.task_episode_id
                   WHERE i.id=? ORDER BY ce.relationship='primary' DESC LIMIT 1""", (identifier,),
            ).fetchone()
            context_case = row[0] if row else context_case
        elif kind == "tool-call":
            context_case = self._case_for_tool_call(identifier) or context_case
        elif kind == "tool-result":
            row = self.store.execute("SELECT tool_call_id FROM tool_results WHERE id=?", (identifier,)).fetchone()
            context_case = self._case_for_tool_call(row[0]) if row else context_case
        elif kind in {"assistant-result", "process-plan"}:
            context_case = self._case_for_event(identifier) or context_case
        result = {
            "target_kind": kind,
            "context_task_case_id": context_case,
            "target_task_case_id": None,
            "target_event_id": None,
            "skill_invocation_id": None,
            "tool_call_id": None,
            "tool_result_id": None,
            "relation": relation,
            "confidence": min(1.0, max(0.0, confidence)),
            "evidence": {"resolver": self.resolver_version, "verifiedObject": True},
        }
        result[column] = identifier
        return result

    def _persist_candidate(
        self,
        event: Mapping[str, Any],
        own_case_id: str | None,
        candidate: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
        *,
        source: str,
        queue: bool = True,
    ) -> dict[str, Any]:
        span = _mapping(candidate.get("span"))
        channel = str(candidate.get("channel") or "user-feedback")
        locator = _mapping(candidate.get("locator")) or {
            "blockIndex": int(span.get("block_index") or 0),
            "start": int(span.get("start") or 0),
            "end": int(span.get("end") or 0),
            "protocol": str(span.get("protocol_locator") or ""),
            "origin": str(span.get("origin") or "unknown-origin"),
        }
        excerpt_hash = str(span.get("excerpt_hash") or _digest(event.get("payload_hash"), locator))
        event_metadata = _mapping(event.get("metadata")) or _mapping(_mapping(event.get("payload")).get("metadata"))
        protocol_type = str(event_metadata.get("protocol_type") or "")
        is_codex_message_alias = (
            event.get("source") == "codex"
            and protocol_type in {"message", "user_message", "agent_message"}
        )
        protocol_identity = (
            (event.get("protocol_time") or event.get("timestamp"))
            if is_codex_message_alias else None
        ) or event.get("event_fingerprint") or event.get("id")
        logical_fp = _digest(
            "feedback-signal", event.get("source"), event.get("session_family"),
            protocol_identity, channel, excerpt_hash,
            locator.get("blockIndex"), locator.get("start"), locator.get("end"),
        )
        if event.get("source") == "codex" and event.get("protocol_time"):
            alias = self.store.execute(
                """SELECT s.logical_fingerprint FROM feedback_signals s
                   JOIN canonical_events existing_event ON existing_event.id=s.feedback_event_id
                   JOIN feedback_signal_revisions existing_revision
                     ON existing_revision.feedback_signal_id=s.id
                   WHERE existing_event.source='codex' AND existing_event.session_family=?
                     AND existing_event.id<>? AND existing_revision.channel=?
                     AND existing_revision.excerpt_hash=?
                     AND ABS((julianday(existing_event.protocol_time)-julianday(?))*86400000)<=2
                   ORDER BY existing_revision.is_current DESC, existing_revision.revision DESC LIMIT 1""",
                (event.get("session_family"), event.get("id"), channel, excerpt_hash,
                 event.get("protocol_time")),
            ).fetchone()
            if alias is not None:
                logical_fp = alias["logical_fingerprint"]
                is_codex_message_alias = True
        signal_id = _digest("feedback-signal-id", logical_fp)
        target_revisions = [
            (self._target_identity(item), item.get("relation"), round(float(item.get("confidence", 0)), 4))
            for item in targets
        ]
        confidence = min(1.0, max(0.0, float(candidate.get("confidence") or 0.0)))
        selected_detector_id = str(candidate.get("detector_id") or self.detector_id)
        selected_detector_version = str(candidate.get("detector_version") or self.detector_version)
        metadata = {
            "adjustments": _items(candidate.get("adjustments")),
            "redactionStatus": span.get("redaction_status"),
            "truncated": bool(span.get("truncated")),
            "language": self._language(str(span.get("redacted_excerpt") or "")),
            **_mapping(candidate.get("metadata")),
        }
        span_parser_version = SPAN_PARSER_VERSION
        revision_fp = _digest(
            "feedback-revision", signal_id, selected_detector_id, selected_detector_version,
            span_parser_version, self.resolver_version,
            candidate.get("category"), candidate.get("severity"), confidence,
            excerpt_hash, target_revisions,
        )
        now = _now()
        observed_at = str(event.get("protocol_time") or now)
        with self.store.transaction():
            existing = self.store.execute(
                "SELECT * FROM feedback_signals WHERE logical_fingerprint=?", (logical_fp,)
            ).fetchone()
            if existing is None:
                self.store.execute(
                    """INSERT INTO feedback_signals(
                           id, logical_fingerprint, feedback_event_id, feedback_case_id,
                           current_process_state, current_resolution_state,
                           created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'candidate', 'unreviewed', ?, ?)""",
                    (signal_id, logical_fp, event["id"], own_case_id, now, now),
                )
                existing = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()
            elif existing["feedback_event_id"] != event["id"] and not is_codex_message_alias:
                raise EffectStoreError("logical feedback fingerprint belongs to another event")

            same = self.store.execute(
                "SELECT * FROM feedback_signal_revisions WHERE revision_fingerprint=?",
                (revision_fp,),
            ).fetchone()
            if same is not None:
                if same["feedback_signal_id"] != signal_id:
                    raise EffectStoreError("feedback revision fingerprint conflict")
                self._reactivate_revision(signal_id, same["id"], now)
                return self.get_signal(signal_id) if self._return_full_signal else {"id": signal_id}

            old_revision_id = existing["current_machine_revision_id"]
            old_revision = self.store.execute(
                "SELECT category, severity FROM feedback_signal_revisions WHERE id=?",
                (old_revision_id,),
            ).fetchone() if old_revision_id else None
            next_revision = self.store.execute(
                "SELECT COALESCE(MAX(revision), 0)+1 FROM feedback_signal_revisions WHERE feedback_signal_id=?",
                (signal_id,),
            ).fetchone()[0]
            revision_id = _digest("feedback-revision-id", revision_fp)
            if old_revision_id:
                self.store.execute(
                    "UPDATE feedback_signal_revisions SET is_current=0 WHERE id=?", (old_revision_id,)
                )
                self.store.execute(
                    """UPDATE feedback_targets SET machine_status='superseded'
                       WHERE feedback_signal_id=? AND signal_revision_id=? AND machine_status='candidate'""",
                    (signal_id, old_revision_id),
                )
            self.store.execute(
                """INSERT INTO feedback_signal_revisions(
                       id, feedback_signal_id, revision, revision_fingerprint, channel,
                       category, severity, authority, source, confidence, excerpt_hash,
                       redacted_excerpt, locator_json, detector_id, detector_version,
                       span_parser_version, resolver_version, suppression_reason,
                       metadata_json, orphaned, is_current,
                       supersedes_id, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)""",
                (
                    revision_id, signal_id, next_revision, revision_fp, channel,
                    str(candidate.get("category") or "mixed-or-unclear"),
                    str(candidate.get("severity") or "unknown"),
                    str(candidate.get("authority") or ("tool" if channel == "process-anomaly" else "unknown")),
                    source, confidence, excerpt_hash, span.get("redacted_excerpt"),
                    _json(locator), selected_detector_id, selected_detector_version,
                    span_parser_version, self.resolver_version,
                    candidate.get("suppression_reason"), _json(metadata), old_revision_id,
                    observed_at, now,
                ),
            )
            inserted_targets = self._insert_targets(signal_id, revision_id, targets, now)
            self.store.execute(
                """UPDATE feedback_signals SET current_machine_revision_id=?,
                       feedback_case_id=COALESCE(feedback_case_id, ?), updated_at=? WHERE id=?""",
                (revision_id, own_case_id, now, signal_id),
            )
            if old_revision_id:
                self._append_system_action(
                    signal_id, "superseded", "machine-revision-superseded",
                    binding={"oldRevisionId": old_revision_id, "newRevisionId": revision_id},
                )
                confirmed = existing["current_confirmed_target_id"]
                target_changed = bool(
                    confirmed and not self._confirmed_identity_present(confirmed, inserted_targets)
                )
                classification_changed = bool(
                    old_revision and (
                        old_revision["category"] != str(candidate.get("category") or "mixed-or-unclear")
                        or old_revision["severity"] != str(candidate.get("severity") or "unknown")
                    )
                )
                if target_changed or classification_changed:
                    self._append_system_action(
                        signal_id, "target-disputed",
                        "machine-target-changed" if target_changed else "machine-classification-changed",
                        process_state="triaged", resolution_state="unreviewed",
                        binding={
                            "machineRevisionId": revision_id,
                            "targetChanged": target_changed,
                            "classificationChanged": classification_changed,
                        },
                    )
                    review = self._review_for_signal(signal_id)
                    if review:
                        self.store.execute(
                            """UPDATE review_tasks SET status='open',
                                   queue_reason='feedback-revision-changed', updated_at=? WHERE id=?""",
                            (_now(), review["id"]),
                        )
            self._append_system_action(
                signal_id, "detected", "detector-match",
                producer_kind="detector", process_state="candidate" if not old_revision_id else None,
                binding={"detectorId": selected_detector_id, "detectorVersion": selected_detector_version,
                         "machineRevisionId": revision_id},
            )
            queued = queue and confidence >= 0.85 and bool(inserted_targets)
            if queued:
                queued = self._ensure_queue(
                    signal_id, revision_id, inserted_targets[0], candidate
                ) is not None
            if queued:
                current_state = self.store.execute(
                    "SELECT current_process_state FROM feedback_signals WHERE id=?", (signal_id,)
                ).fetchone()[0]
                self._append_system_action(
                    signal_id, "queued", "high-confidence-targeted",
                    producer_kind="queue",
                    process_state="queued" if current_state in {
                        "candidate", "queued", "orphaned", "superseded"
                    } else current_state,
                    binding={"machineRevisionId": revision_id},
                )
            if not self._defer_cluster_projection:
                self._refresh_cluster(signal_id)
        return self.get_signal(signal_id) if self._return_full_signal else {"id": signal_id}

    def _insert_targets(
        self, signal_id: str, revision_id: str,
        targets: Sequence[Mapping[str, Any]], now: str,
    ) -> list[dict[str, Any]]:
        inserted: list[dict[str, Any]] = []
        for rank, item in enumerate(targets, 1):
            identity = self._target_identity(item)
            fingerprint = _digest(
                "feedback-target", signal_id, revision_id, identity,
                item.get("relation"),
            )
            target_id = _digest("feedback-target-id", fingerprint)
            self.store.execute(
                """INSERT INTO feedback_targets(
                       id, feedback_signal_id, signal_revision_id, target_fingerprint,
                       rank, target_kind, context_task_case_id, target_task_case_id,
                       target_event_id, skill_invocation_id, tool_call_id, tool_result_id,
                       relation, confidence, machine_status, resolver_version,
                       evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)""",
                (
                    target_id, signal_id, revision_id, fingerprint, rank,
                    item["target_kind"], item.get("context_task_case_id"),
                    item.get("target_task_case_id"), item.get("target_event_id"),
                    item.get("skill_invocation_id"), item.get("tool_call_id"),
                    item.get("tool_result_id"), item["relation"], item["confidence"],
                    self.resolver_version, _json(item.get("evidence")), now,
                ),
            )
            inserted.append({**dict(item), "id": target_id})
        return inserted

    def _reactivate_revision(self, signal_id: str, revision_id: str, now: str) -> None:
        revision = self.store.execute(
            "SELECT orphaned, is_current FROM feedback_signal_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        signal = self.store.execute(
            "SELECT current_machine_revision_id FROM feedback_signals WHERE id=?", (signal_id,)
        ).fetchone()
        if revision is None or signal is None:
            return
        switching_revision = signal["current_machine_revision_id"] != revision_id
        if not switching_revision and not revision["orphaned"]:
            return
        if switching_revision:
            self.store.execute(
                "UPDATE feedback_signal_revisions SET is_current=0 WHERE id=?",
                (signal["current_machine_revision_id"],),
            )
            self.store.execute(
                """UPDATE feedback_targets SET machine_status='superseded'
                   WHERE signal_revision_id=? AND machine_status='candidate'""",
                (signal["current_machine_revision_id"],),
            )
        self.store.execute(
            "UPDATE feedback_signal_revisions SET orphaned=0, is_current=1 WHERE id=?", (revision_id,)
        )
        self.store.execute(
            """UPDATE feedback_targets SET machine_status='candidate'
               WHERE signal_revision_id=? AND machine_status IN ('orphaned','superseded')""", (revision_id,)
        )
        self.store.execute(
            "UPDATE feedback_signals SET current_machine_revision_id=?, updated_at=? WHERE id=?",
            (revision_id, now, signal_id),
        )
        if switching_revision:
            self._append_system_action(
                signal_id, "superseded", "machine-revision-reactivated",
                binding={"oldRevisionId": signal["current_machine_revision_id"],
                         "newRevisionId": revision_id},
            )
            self._append_system_action(
                signal_id, "detected", "existing-machine-revision-reactivated",
                producer_kind="detector",
                binding={"machineRevisionId": revision_id, "detectorVersion": self.detector_version},
            )
            self._append_system_action(
                signal_id, "target-disputed", "reactivated-machine-revision-review",
                process_state="triaged", resolution_state="unreviewed",
                binding={"machineRevisionId": revision_id},
            )
            review = self._review_for_signal(signal_id)
            if review:
                self.store.execute(
                    """UPDATE review_tasks SET status='open',
                           queue_reason='feedback-revision-reactivated', updated_at=? WHERE id=?""",
                    (_now(), review["id"]),
                )
            return
        orphan_action = self.store.execute(
            """SELECT from_process_state FROM feedback_actions
               WHERE feedback_signal_id=? AND action='orphaned'
               ORDER BY revision DESC LIMIT 1""", (signal_id,),
        ).fetchone()
        restored_state = orphan_action["from_process_state"] if orphan_action else (
            "queued" if self._review_for_signal(signal_id) else "candidate"
        )
        self._append_system_action(
            signal_id, "reactivated", "event-provenance-restored",
            process_state=restored_state,
        )
        review = self._review_for_signal(signal_id)
        if review:
            preserve_review_status = restored_state in {"closed", "excluded"}
            self.store.execute(
                """UPDATE review_tasks SET status=CASE WHEN ? THEN status ELSE 'open' END,
                       queue_reason=CASE WHEN ? THEN queue_reason ELSE 'feedback-reactivated' END,
                       updated_at=? WHERE id=?""",
                (preserve_review_status, preserve_review_status, now, review["id"]),
            )

    # --------------------------------------------------------------- queue/actions
    def _ensure_queue(
        self, signal_id: str, revision_id: str,
        target: Mapping[str, Any], candidate: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        target = dict(target)
        candidate = dict(candidate)
        signal = self.store.execute(
            """SELECT feedback_case_id, current_confirmed_target_id
               FROM feedback_signals WHERE id=?""", (signal_id,),
        ).fetchone()
        target_is_confirmed = bool(
            signal and target.get("id") and signal["current_confirmed_target_id"] == target["id"]
        )
        if signal and signal["current_confirmed_target_id"] and not target_is_confirmed:
            confirmed_target = self.store.execute(
                "SELECT * FROM feedback_targets WHERE id=?", (signal["current_confirmed_target_id"],)
            ).fetchone()
            target_is_confirmed = bool(
                confirmed_target
                and self._target_identity(confirmed_target) == self._target_identity(target)
            )
        case_id = signal["feedback_case_id"] if signal else None
        if target_is_confirmed:
            case_id = target.get("target_task_case_id") or target.get("context_task_case_id") or case_id
        if not case_id or self.store.execute("SELECT 1 FROM task_cases WHERE id=?", (case_id,)).fetchone() is None:
            return None
        now = _now()
        revision = self.store.execute("SELECT * FROM feedback_signal_revisions WHERE id=?", (revision_id,)).fetchone()
        evidence_id = _digest("feedback-evidence", signal_id, revision_id, case_id)
        self.store.execute(
            """INSERT OR IGNORE INTO evidence_items(
                   id, evidence_fingerprint, task_case_id, event_id, evidence_type,
                   content_hash, locator_json, excerpt, validity, polarity, category,
                   confidence, rule_id, producer_version, observed_at, created_at)
               VALUES (?, ?, ?, ?, 'session-negative-feedback', ?, ?, ?, 'valid',
                       'negative', ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id, evidence_id, case_id,
                self.store.execute("SELECT feedback_event_id FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()[0],
                revision["excerpt_hash"], revision["locator_json"], revision["redacted_excerpt"],
                revision["category"], revision["confidence"], self.detector_id,
                self.detector_version, revision["observed_at"], now,
            ),
        )
        assessment = self.store.execute(
            """SELECT * FROM outcome_assessments WHERE subject_key=? AND is_current=1
               ORDER BY created_at DESC LIMIT 1""", (f"feedback:{signal_id}",),
        ).fetchone()
        if assessment is None or assessment["task_case_id"] != case_id:
            if assessment is not None:
                self.store.execute("UPDATE outcome_assessments SET is_current=0 WHERE id=?", (assessment["id"],))
            case = self.store.execute("SELECT current_revision FROM task_cases WHERE id=?", (case_id,)).fetchone()
            assessment_revision = max(
                _ASSESSMENT_REVISION_BASE,
                self.store.execute(
                    "SELECT COALESCE(MAX(revision), 0)+1 FROM outcome_assessments WHERE task_case_id=?", (case_id,)
                ).fetchone()[0],
            )
            assessment_id = _digest("feedback-assessment", signal_id, case_id)
            self.store.execute(
                """INSERT OR IGNORE INTO outcome_assessments(
                       id, task_case_id, revision, case_revision, subject_key,
                       parser_version, process_state, assessability, automated_verdict,
                       conflict_state, freshness, hard_failure, is_current,
                       rationale_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'feedback-review', 'needs-evidence',
                           'unset', 'none', 'current', 0, 1, ?, ?)""",
                (assessment_id, case_id, assessment_revision, case[0], f"feedback:{signal_id}",
                 self.detector_version, _json({"feedbackSignalId": signal_id,
                                               "machineRevisionId": revision_id}), now),
            )
            assessment = self.store.execute("SELECT * FROM outcome_assessments WHERE id=?", (assessment_id,)).fetchone()
            if assessment is not None and not assessment["is_current"]:
                self.store.execute("UPDATE outcome_assessments SET is_current=1 WHERE id=?", (assessment_id,))
                assessment = self.store.execute(
                    "SELECT * FROM outcome_assessments WHERE id=?", (assessment_id,)
                ).fetchone()
        priority = self._priority(str(candidate.get("severity") or "unknown"), str(candidate.get("channel") or "user-feedback"))
        queue_reason = "user-negative-feedback" if candidate.get("channel", "user-feedback") == "user-feedback" else str(candidate.get("category") or "process-anomaly")
        review = self._review_for_signal(signal_id)
        if review is None:
            review_id = _digest("feedback-review-task", signal_id)
            self.store.execute(
                """INSERT INTO review_tasks(
                       id, task_case_id, assessment_id, queue_reason, trigger_evidence_id,
                       feedback_signal_id, priority, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (review_id, case_id, assessment["id"], queue_reason, evidence_id,
                 signal_id, priority, now, now),
            )
            review = self.store.execute("SELECT * FROM review_tasks WHERE id=?", (review_id,)).fetchone()
        else:
            self.store.execute(
                """UPDATE review_tasks SET task_case_id=?, assessment_id=?, queue_reason=?,
                       trigger_evidence_id=?, priority=?, status=CASE WHEN status='superseded' THEN 'open' ELSE status END,
                       updated_at=? WHERE id=?""",
                (case_id, assessment["id"], queue_reason, evidence_id, priority, now, review["id"]),
            )
            review = self.store.execute("SELECT * FROM review_tasks WHERE id=?", (review["id"],)).fetchone()
        return _decoded(review)

    @staticmethod
    def _priority(severity: str, channel: str) -> int:
        base = {"critical": 100, "high": 80, "medium": 50, "low": 20, "unknown": 10}.get(severity, 10)
        return base + {"user-feedback": 10, "process-anomaly": 5, "assistant-claim": 0}.get(channel, 0)

    def claim(self, signal_or_review_id: str, *, actor_id: str, expected_revision: int) -> dict[str, Any]:
        """Atomically claim the existing review task and append a feedback Action."""
        with self.store.transaction():
            signal = self._signal_from_signal_or_review(signal_or_review_id)
            self._require_reviewer(actor_id)
            if signal["current_action_revision"] != expected_revision:
                raise RevisionConflict(
                    f"feedback action revision conflict: expected {expected_revision}, current {signal['current_action_revision']}"
                )
            review = self._review_for_signal(signal["id"])
            if review is None:
                raise EffectStoreError("feedback signal has no review task")
            if review["status"] != "open" or review["claimed_by_actor_id"] not in {None, actor_id}:
                raise RevisionConflict("feedback review task is no longer available for claim")
            self.store.execute(
                "UPDATE review_tasks SET claimed_by_actor_id=?, updated_at=? WHERE id=?",
                (actor_id, _now(), review["id"]),
            )
            action = self._append_action_row(
                signal, "claimed", actor_id, "review-claimed", None, None,
                process_state="claimed", resolution_state=signal["current_resolution_state"],
                binding={"reviewTaskId": review["id"]},
            )
        return action

    claim_signal = claim

    def append_action(
        self,
        signal_id: str,
        action: str,
        *,
        actor_id: str,
        expected_revision: int,
        reason_code: str,
        note: str | None = None,
        target_id: str | None = None,
        binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a reviewer action and update signal and queue projections."""
        if action == "resolve":
            verification_keys = {
                "evidenceId", "evidence_id", "checkRunId", "check_run_id",
                "userAcceptanceEventId", "user_acceptance_event_id",
                "externalAcceptanceId", "external_acceptance_id",
                "verificationEvidenceId", "verification_evidence_id",
            }
            action = "resolve-verified" if binding and any(
                binding.get(key) for key in verification_keys
            ) else "resolve-unverified"
        allowed = {
            "confirm", "exclude", "retarget", "mark-duplicate", "start-fix",
            "request-verification", "resolve-verified", "resolve-unverified", "reopen",
        }
        if action not in allowed:
            raise ValueError(f"unsupported feedback action: {action}")
        reason_code = str(reason_code or "").strip()
        if len(reason_code) > 64 or _REVIEWER_REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("reason_code must be a lowercase machine-readable code")
        with self.store.transaction():
            signal = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()
            if signal is None:
                raise KeyError(signal_id)
            self._require_reviewer(actor_id)
            if signal["current_action_revision"] != expected_revision:
                raise RevisionConflict(
                    f"feedback action revision conflict: expected {expected_revision}, current {signal['current_action_revision']}"
                )
            self._validate_action_transition(action, signal, binding)
            review = self._review_for_signal(signal_id)
            if review and review["claimed_by_actor_id"] not in {None, actor_id}:
                raise RevisionConflict("feedback review task is claimed by another reviewer")
            target = None
            if target_id:
                target = self.store.execute(
                    "SELECT * FROM feedback_targets WHERE id=? AND feedback_signal_id=?",
                    (target_id, signal_id),
                ).fetchone()
                if target is None:
                    raise EffectStoreError("feedback target does not belong to signal")
            if action in {"confirm", "retarget"}:
                if target is None:
                    candidates = self.store.execute(
                        """SELECT * FROM feedback_targets WHERE feedback_signal_id=?
                           AND machine_status='candidate' ORDER BY rank""", (signal_id,),
                    ).fetchall()
                    if len(candidates) != 1:
                        raise ValueError(f"{action} requires target_id")
                    target = candidates[0]
                    target_id = target["id"]
                if (
                    target["signal_revision_id"] != signal["current_machine_revision_id"]
                    or target["machine_status"] != "candidate"
                ):
                    raise RevisionConflict("feedback target is not a current machine candidate")

            process_state, resolution_state, task_status = self._action_projection(action, signal)
            safe_note = redact_sensitive(note) if note else None
            if action in {"confirm", "retarget"}:
                self.store.execute(
                    "UPDATE feedback_signals SET current_confirmed_target_id=? WHERE id=?",
                    (target_id, signal_id),
                )
                current_revision = self.store.execute(
                    "SELECT * FROM feedback_signal_revisions WHERE id=?", (signal["current_machine_revision_id"],)
                ).fetchone()
                self._ensure_queue(signal_id, current_revision["id"], target, current_revision)
                review = self._review_for_signal(signal_id)
            action_row = self._append_action_row(
                signal, action, actor_id, reason_code, safe_note, target_id,
                process_state=process_state, resolution_state=resolution_state,
                binding={
                    "machineRevisionId": signal["current_machine_revision_id"],
                    "targetId": target_id,
                    "verificationReferences": self._verification_references(binding),
                    "requestBindingHash": _digest(dict(binding)) if binding else None,
                    "noteHash": _digest(safe_note) if safe_note else None,
                },
            )
            if review:
                self.store.execute(
                    """UPDATE review_tasks SET status=?, queue_reason=?, updated_at=? WHERE id=?""",
                    (task_status, "feedback-reopened" if action == "reopen" else review["queue_reason"],
                     _now(), review["id"]),
                )
            self._refresh_cluster(signal_id)
        return action_row

    def _action_projection(self, action: str, signal: Mapping[str, Any]) -> tuple[str, str, str]:
        mapping = {
            "confirm": ("triaged", "action-required", "open"),
            "retarget": ("triaged", "action-required", "open"),
            "exclude": ("excluded", "false-positive", "decided"),
            "mark-duplicate": ("closed", "duplicate", "decided"),
            "start-fix": ("fix-in-progress", "fix-in-progress", "open"),
            "request-verification": ("awaiting-verification", "awaiting-verification", "open"),
            "resolve-verified": ("closed", "resolved-verified", "decided"),
            "resolve-unverified": ("closed", "resolved-unverified", "decided"),
            "reopen": ("queued", "unreviewed", "open"),
        }
        return mapping[action]

    def _validate_action_transition(
        self, action: str, signal: Mapping[str, Any], binding: Mapping[str, Any] | None,
    ) -> None:
        resolution = signal["current_resolution_state"]
        process = signal["current_process_state"]
        if action in {"confirm", "exclude", "retarget", "mark-duplicate"} and process not in {
            "candidate", "queued", "claimed", "triaged",
        }:
            raise EffectStoreError(f"{action} is not valid from process state {process}")
        if action == "start-fix" and resolution != "action-required":
            raise EffectStoreError("start-fix requires action-required feedback")
        if action == "request-verification" and resolution != "fix-in-progress":
            raise EffectStoreError("request-verification requires a fix in progress")
        if action in {"resolve-verified", "resolve-unverified"} and resolution != "awaiting-verification":
            raise EffectStoreError(f"{action} requires awaiting verification")
        if action == "resolve-verified":
            references = self._verification_references(binding)
            if not references or not self._verification_references_are_valid(signal["id"], references):
                raise EffectStoreError("resolve-verified requires locatable verification evidence")
        if action == "reopen" and resolution not in _CLOSED_RESOLUTIONS:
            raise EffectStoreError("reopen requires closed feedback")

    @staticmethod
    def _verification_references(binding: Mapping[str, Any] | None) -> dict[str, str]:
        if not binding:
            return {}
        aliases = {
            "evidenceId": ("evidenceId", "evidence_id"),
            "checkRunId": ("checkRunId", "check_run_id"),
            "userAcceptanceEventId": ("userAcceptanceEventId", "user_acceptance_event_id"),
            "externalAcceptanceId": ("externalAcceptanceId", "external_acceptance_id"),
            "verificationEvidenceId": ("verificationEvidenceId", "verification_evidence_id"),
        }
        result: dict[str, str] = {}
        for canonical, names in aliases.items():
            value = _first(binding, *names)
            if value:
                result[canonical] = str(value)
        return result

    def _verification_references_are_valid(
        self, signal_id: str, references: Mapping[str, str],
    ) -> bool:
        target_case = self._target_case_for_signal(signal_id)
        for key in ("evidenceId", "verificationEvidenceId", "externalAcceptanceId"):
            if key not in references:
                continue
            evidence = self.store.execute(
                "SELECT * FROM evidence_items WHERE id=?", (references[key],)
            ).fetchone()
            if evidence is None:
                continue
            positive = self._evidence_is_trusted_positive(signal_id, evidence)
            if positive and (target_case is None or evidence["task_case_id"] == target_case):
                return True
        if "checkRunId" in references:
            check = self.store.execute(
                "SELECT * FROM check_runs WHERE id=?", (references["checkRunId"],)
            ).fetchone()
            details = _mapping(check["result_json"]) if check else {}
            if check is not None and (
                check["status"] == "finished"
                and check["assertion_outcome"] == "assertion-pass"
                and check["freshness"] == "current"
                and details.get("trust_level") == "trusted"
                and (target_case is None or check["task_case_id"] == target_case)
            ):
                return True
        return False

    def _evidence_is_trusted_positive(
        self, signal_id: str, evidence: Mapping[str, Any],
    ) -> bool:
        if evidence["validity"] != "valid" or evidence["polarity"] != "positive":
            return False
        locator = _mapping(evidence["locator_json"])
        evidence_type = evidence["evidence_type"]
        if evidence_type == "user-acceptance":
            if (
                evidence["rule_id"] != "explicit-user-acceptance"
                or evidence["producer_version"] != self.detector_version
                or not evidence["event_id"] or locator.get("eventId") != evidence["event_id"]
            ):
                return False
            row = self.store.execute(
                """SELECT acceptance.*, feedback.protocol_time AS feedback_time,
                          feedback.created_at AS feedback_created,
                          feedback.session_family AS feedback_family
                   FROM canonical_events acceptance JOIN feedback_signals signal ON signal.id=?
                   JOIN canonical_events feedback ON feedback.id=signal.feedback_event_id
                   WHERE acceptance.id=?""",
                (signal_id, evidence["event_id"]),
            ).fetchone()
            if row is None or row["orphaned"] or row["event_type"] != "user_message":
                return False
            if row["session_family"] != row["feedback_family"]:
                return False
            if (row["protocol_time"] or row["created_at"]) <= (row["feedback_time"] or row["feedback_created"]):
                return False
            payload = _mapping(row["payload_json"])
            return is_positive_resolution(payload.get("text") or "")
        if evidence_type == "verification":
            return bool(
                evidence["rule_id"] in {"trusted-reviewer-verification", "trusted-artifact-verification"}
                and str(evidence["producer_version"] or "").startswith("trusted-")
                and locator
            )
        if evidence_type == "external-acceptance":
            return bool(
                evidence["rule_id"] == "trusted-external-adapter"
                and str(evidence["producer_version"] or "").startswith("trusted-")
                and locator.get("externalObjectId")
            )
        return False

    def _append_action_row(
        self, signal: Mapping[str, Any], action: str, actor_id: str | None,
        reason_code: str, note: str | None, target_id: str | None,
        *, process_state: str, resolution_state: str,
        binding: Mapping[str, Any] | None = None,
        producer_kind: str | None = None,
    ) -> dict[str, Any]:
        revision = int(signal["current_action_revision"]) + 1
        previous = self.store.execute(
            "SELECT id FROM feedback_actions WHERE feedback_signal_id=? ORDER BY revision DESC LIMIT 1",
            (signal["id"],),
        ).fetchone()
        action_id = _digest("feedback-action", signal["id"], revision, action)
        producer = producer_kind or ("reviewer" if actor_id else "system")
        now = _now()
        self.store.execute(
            """INSERT INTO feedback_actions(
                   id, feedback_signal_id, actor_id, producer_kind, revision, action,
                   from_process_state, to_process_state, from_resolution_state,
                   to_resolution_state, reason_code, note, target_id, supersedes_id,
                   binding_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_id, signal["id"], actor_id, producer, revision, action,
             signal["current_process_state"], process_state,
             signal["current_resolution_state"], resolution_state, reason_code,
             note, target_id, previous["id"] if previous else None, _json(binding), now),
        )
        updated = self.store.execute(
            """UPDATE feedback_signals SET current_process_state=?, current_resolution_state=?,
                   current_action_revision=?, updated_at=?
               WHERE id=? AND current_action_revision=?""",
            (process_state, resolution_state, revision, now, signal["id"], revision - 1),
        )
        if updated.rowcount != 1:
            raise RevisionConflict("feedback action revision changed during write")
        return _decoded(self.store.execute("SELECT * FROM feedback_actions WHERE id=?", (action_id,)).fetchone())

    def _append_system_action(
        self, signal_id: str, action: str, reason_code: str, *,
        producer_kind: str = "system", process_state: str | None = None,
        resolution_state: str | None = None, binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        signal = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()
        if signal is None:
            raise KeyError(signal_id)
        return self._append_action_row(
            signal, action, None, reason_code, None, None,
            process_state=process_state or signal["current_process_state"],
            resolution_state=resolution_state or signal["current_resolution_state"],
            binding=binding, producer_kind=producer_kind,
        )

    # ---------------------------------------------------------------- queries
    def list_signals(
        self, *, limit: int = 100, cursor: str | None = None,
        channel: str | None = None, category: str | None = None,
        severity: str | None = None, process_state: str | None = None,
        resolution_state: str | None = None, authority: str | None = None,
        source: str | None = None, min_confidence: float | None = None,
        target_kind: str | None = None, skill: str | None = None,
        claimed: bool | None = None, orphaned: bool | None = False,
    ) -> dict[str, Any]:
        limit = min(1000, max(1, int(limit)))
        conditions = ["r.is_current=1"]
        params: list[Any] = []
        if orphaned is not None:
            conditions.append("r.orphaned=?")
            params.append(int(orphaned))
        columns = {
            "channel": (channel, "r.channel"), "category": (category, "r.category"),
            "severity": (severity, "r.severity"), "process": (process_state, "s.current_process_state"),
            "resolution": (resolution_state, "s.current_resolution_state"),
            "authority": (authority, "r.authority"), "source": (source, "r.source"),
        }
        for value, column in columns.values():
            if value is not None:
                conditions.append(f"{column}=?")
                params.append(value)
        if min_confidence is not None:
            if not math.isfinite(float(min_confidence)) or not 0 <= float(min_confidence) <= 1:
                raise ValueError("min_confidence must be between 0 and 1")
            conditions.append("r.confidence>=?")
            params.append(float(min_confidence))
        if target_kind:
            conditions.append("ft.target_kind=?")
            params.append(target_kind)
        if skill:
            conditions.append("EXISTS (SELECT 1 FROM feedback_targets ft JOIN skill_invocations si ON si.id=ft.skill_invocation_id WHERE ft.feedback_signal_id=s.id AND si.skill_id=? AND (ft.machine_status='candidate' OR ft.id=s.current_confirmed_target_id))")
            params.append(skill)
        if claimed is not None:
            conditions.append("EXISTS (SELECT 1 FROM review_tasks rt WHERE rt.feedback_signal_id=s.id AND rt.claimed_by_actor_id IS " + ("NOT NULL" if claimed else "NULL") + ")")
        if cursor:
            cursor_time, separator, cursor_id = cursor.partition("|")
            if separator:
                conditions.append("(COALESCE(r.observed_at, r.created_at)< ? OR (COALESCE(r.observed_at, r.created_at)=? AND s.id<?))")
                params.extend((cursor_time, cursor_time, cursor_id))
            else:
                conditions.append("s.id<?")
                params.append(cursor)
        rows = self.store.execute(
            f"""SELECT s.*, r.channel, r.category, r.severity, r.authority, r.source,
                       r.confidence, r.redacted_excerpt, r.observed_at, r.orphaned,
                       ft.id AS display_target_id, ft.target_kind,
                       ft.context_task_case_id, ft.target_task_case_id,
                       ft.skill_invocation_id, ft.tool_call_id, ft.tool_result_id,
                       ft.relation AS target_relation, ft.confidence AS target_confidence,
                       rt.id AS review_task_id, rt.status AS review_status,
                       rt.claimed_by_actor_id, rt.priority
                FROM feedback_signals s JOIN feedback_signal_revisions r
                  ON r.id=s.current_machine_revision_id
                LEFT JOIN review_tasks rt ON rt.feedback_signal_id=s.id
                LEFT JOIN feedback_targets ft ON ft.id=COALESCE(
                  s.current_confirmed_target_id,
                  (SELECT id FROM feedback_targets candidate
                   WHERE candidate.feedback_signal_id=s.id
                     AND candidate.signal_revision_id=r.id
                     AND candidate.machine_status='candidate'
                   ORDER BY candidate.rank LIMIT 1)
                )
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(r.observed_at, r.created_at) DESC, s.id DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        selected = rows[:limit]
        result = [_decoded(row) for row in selected]
        next_cursor = None
        if more and selected:
            last = selected[-1]
            next_cursor = f"{last['observed_at'] or last['created_at']}|{last['id']}"
        return {"items": result, "next_cursor": next_cursor}

    def get_signal(self, signal_id: str) -> dict[str, Any]:
        signal = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()
        if signal is None:
            raise KeyError(signal_id)
        result = _decoded(signal)
        result["machine_revisions"] = [
            _decoded(row) for row in self.store.execute(
                "SELECT * FROM feedback_signal_revisions WHERE feedback_signal_id=? ORDER BY revision",
                (signal_id,),
            ).fetchall()
        ]
        result["targets"] = [
            _decoded(row) for row in self.store.execute(
                "SELECT * FROM feedback_targets WHERE feedback_signal_id=? ORDER BY created_at, rank",
                (signal_id,),
            ).fetchall()
        ]
        result["actions"] = [
            _decoded(row) for row in self.store.execute(
                "SELECT * FROM feedback_actions WHERE feedback_signal_id=? ORDER BY revision", (signal_id,),
            ).fetchall()
        ]
        result["semantic_reviews"] = [
            _decoded(row) for row in self.store.execute(
                """SELECT * FROM feedback_semantic_reviews
                   WHERE feedback_signal_id=? ORDER BY created_at, id""", (signal_id,),
            ).fetchall()
        ]
        review = self._review_for_signal(signal_id)
        result["review_task"] = _decoded(review) if review else None
        if review:
            evidence = self.store.execute("SELECT * FROM evidence_items WHERE id=?", (review["trigger_evidence_id"],)).fetchone()
            result["evidence"] = _decoded(evidence) if evidence else None
        else:
            result["evidence"] = None
        return result

    def case_feedback(self, task_case_id: str) -> list[dict[str, Any]]:
        rows = self.store.execute(
            """SELECT DISTINCT s.id FROM feedback_signals s
               LEFT JOIN feedback_targets t ON t.feedback_signal_id=s.id
               WHERE s.feedback_case_id=? OR t.context_task_case_id=? OR t.target_task_case_id=?
               ORDER BY s.updated_at DESC""", (task_case_id, task_case_id, task_case_id),
        ).fetchall()
        return [self.get_signal(row["id"]) for row in rows]

    get_case_feedback = case_feedback

    def overview(self) -> dict[str, Any]:
        row = self.store.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN r.channel='user-feedback' AND r.orphaned=0 THEN 1 ELSE 0 END) AS user_feedback,
                      SUM(CASE WHEN r.channel='process-anomaly' AND r.orphaned=0 THEN 1 ELSE 0 END) AS process_anomalies,
                      SUM(CASE WHEN s.current_process_state IN ('queued','claimed','triaged','needs-evidence','orphaned','action-required','fix-in-progress','awaiting-verification') THEN 1 ELSE 0 END) AS open,
                      SUM(CASE WHEN s.current_resolution_state='awaiting-verification' THEN 1 ELSE 0 END) AS awaiting_verification,
                      SUM(CASE WHEN s.current_resolution_state IN ('resolved-verified','resolved-unverified') THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN s.current_resolution_state='false-positive' THEN 1 ELSE 0 END) AS false_positives
               FROM feedback_signals s JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id"""
        ).fetchone()
        return {key: int(value or 0) for key, value in dict(row).items()}

    get_overview = overview

    def clusters(self, *, limit: int = 100) -> dict[str, Any]:
        rows = self.store.execute(
            """SELECT * FROM feedback_clusters WHERE member_count>0
               ORDER BY open_count DESC, last_observed_at DESC, id LIMIT ?""",
            (min(1000, max(1, int(limit))),),
        ).fetchall()
        return {"items": [_decoded(row) for row in rows]}

    list_clusters = clusters

    def rebuild_clusters(self) -> int:
        with self.store.transaction():
            self.store.execute("DELETE FROM feedback_cluster_members")
            self.store.execute("DELETE FROM feedback_clusters")
            ids = [row["id"] for row in self.store.execute(
                """SELECT id FROM feedback_signals
                   WHERE current_confirmed_target_id IS NOT NULL"""
            ).fetchall()]
            for signal_id in ids:
                self._refresh_cluster(signal_id)
        return len(ids)

    def _refresh_cluster(self, signal_id: str) -> None:
        row = self.store.execute(
            """SELECT s.current_resolution_state, r.category, r.observed_at, t.target_kind,
                      tc.task_type, si.skill_id, si.skill_sha256,
                      (SELECT reason_code FROM feedback_actions a WHERE a.feedback_signal_id=s.id
                       ORDER BY revision DESC LIMIT 1) AS reason_code
               FROM feedback_signals s JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               LEFT JOIN feedback_targets t ON t.id=s.current_confirmed_target_id
               LEFT JOIN task_cases tc ON tc.id=COALESCE(t.target_task_case_id,t.context_task_case_id)
               LEFT JOIN skill_invocations si ON si.id=t.skill_invocation_id
               WHERE s.id=? AND r.orphaned=0
                 AND COALESCE(r.suppression_reason,'')!='data-cleanup'
                 AND COALESCE(t.machine_status,'orphaned')!='orphaned'""",
            (signal_id,),
        ).fetchone()
        if row is None or row["target_kind"] is None:
            self._remove_cluster_memberships(signal_id)
            return
        reason = str(row["reason_code"] or row["category"]).strip().lower().replace("_", "-")
        key_parts = (row["skill_id"], row["skill_sha256"], row["task_type"], row["category"], row["target_kind"], reason)
        cluster_key = _digest("feedback-cluster", key_parts)
        cluster_id = _digest("feedback-cluster-id", cluster_key)
        now = _now()
        self.store.execute(
            """INSERT OR IGNORE INTO feedback_clusters(
                   id, cluster_key, skill_id, skill_sha256, task_type, category,
                   target_kind, normalized_reason_code, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, cluster_key, row["skill_id"], row["skill_sha256"], row["task_type"],
             row["category"], row["target_kind"], reason, now),
        )
        old_clusters = [item["cluster_id"] for item in self.store.execute(
            "SELECT cluster_id FROM feedback_cluster_members WHERE feedback_signal_id=? AND cluster_id<>?",
            (signal_id, cluster_id),
        ).fetchall()]
        self.store.execute(
            "DELETE FROM feedback_cluster_members WHERE feedback_signal_id=? AND cluster_id<>?",
            (signal_id, cluster_id),
        )
        self.store.execute(
            "INSERT OR IGNORE INTO feedback_cluster_members(cluster_id, feedback_signal_id, created_at) VALUES (?, ?, ?)",
            (cluster_id, signal_id, now),
        )
        for old_cluster_id in old_clusters:
            self.store.execute(
                """UPDATE feedback_clusters SET
                       member_count=(SELECT COUNT(*) FROM feedback_cluster_members WHERE cluster_id=?),
                       open_count=(SELECT COUNT(*) FROM feedback_cluster_members m JOIN feedback_signals s
                         ON s.id=m.feedback_signal_id WHERE m.cluster_id=? AND s.current_resolution_state NOT IN
                         ('resolved-verified','resolved-unverified','not-actionable','false-positive','duplicate')),
                       updated_at=? WHERE id=?""",
                (old_cluster_id, old_cluster_id, now, old_cluster_id),
            )
        self.store.execute(
            """UPDATE feedback_clusters SET
                   member_count=(SELECT COUNT(*) FROM feedback_cluster_members WHERE cluster_id=?),
                   open_count=(SELECT COUNT(*) FROM feedback_cluster_members m JOIN feedback_signals s
                     ON s.id=m.feedback_signal_id WHERE m.cluster_id=? AND s.current_resolution_state NOT IN
                     ('resolved-verified','resolved-unverified','not-actionable','false-positive','duplicate')),
                   first_observed_at=COALESCE(first_observed_at, ?),
                   last_observed_at=CASE WHEN last_observed_at IS NULL OR last_observed_at<? THEN ? ELSE last_observed_at END,
                   updated_at=? WHERE id=?""",
            (cluster_id, cluster_id, row["observed_at"], row["observed_at"], row["observed_at"], now, cluster_id),
        )

    def _remove_cluster_memberships(self, signal_id: str) -> None:
        cluster_ids = [row["cluster_id"] for row in self.store.execute(
            "SELECT cluster_id FROM feedback_cluster_members WHERE feedback_signal_id=?", (signal_id,)
        ).fetchall()]
        if not cluster_ids:
            return
        self.store.execute(
            "DELETE FROM feedback_cluster_members WHERE feedback_signal_id=?", (signal_id,)
        )
        now = _now()
        for cluster_id in cluster_ids:
            self.store.execute(
                """UPDATE feedback_clusters SET
                       member_count=(SELECT COUNT(*) FROM feedback_cluster_members WHERE cluster_id=?),
                       open_count=(SELECT COUNT(*) FROM feedback_cluster_members m JOIN feedback_signals s
                         ON s.id=m.feedback_signal_id WHERE m.cluster_id=? AND s.current_resolution_state NOT IN
                         ('resolved-verified','resolved-unverified','not-actionable','false-positive','duplicate')),
                       updated_at=? WHERE id=?""",
                (cluster_id, cluster_id, now, cluster_id),
            )

    # -------------------------------------------------------- change consumption
    def bootstrap(
        self, *, max_events: int = 5000, max_seconds: float = 2.0,
        last_scan_run_id: str | None = None,
    ) -> dict[str, Any]:
        if max_events < 0 or max_seconds < 0:
            raise ValueError("bootstrap budgets must be non-negative")
        state = self._derivation_state()
        saved_stats = _mapping(state["stats_json"]) if state else {}
        state_versions_current = bool(
            state and state["detector_version"] == self.detector_version
            and saved_stats.get("resolverVersion") == self.resolver_version
        )
        if state and state["bootstrap_complete"] and state_versions_current:
            return {"processed": 0, "newSignals": 0, "pending": False,
                    "bootstrapComplete": True, "changeCursor": state["change_cursor"],
                    "status": state["status"],
                    "sourceReparseRequired": int(saved_stats.get("sourceReparseRequired") or 0)}
        prior_stats = saved_stats if state_versions_current else {}
        self._bootstrap_reparse_count = 0
        last_event_id = str(prior_stats.get("lastEventId") or "")
        bootstrap_cursor = int(prior_stats.get("bootstrapChangeCursor") or self.store.execute(
            "SELECT COALESCE(MAX(id),0) FROM effect_derivation_changes"
        ).fetchone()[0])
        started = time.monotonic()
        before = self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0]
        processed = 0
        limit = max(0, int(max_events))
        rows = self.store.execute(
            "SELECT * FROM canonical_events WHERE orphaned=0 AND id>? ORDER BY id LIMIT ?",
            (last_event_id, limit + 1),
        ).fetchall()
        pending = False
        self._defer_cluster_projection = True
        self._return_full_signal = False
        try:
            with self.store.transaction():
                for row in rows:
                    if processed >= limit or time.monotonic() - started >= max_seconds:
                        pending = True
                        break
                    self._derive_stored_event(row)
                    last_event_id = row["id"]
                    processed += 1
        finally:
            self._defer_cluster_projection = False
            self._return_full_signal = True
        if not pending:
            pending = self.store.execute(
                "SELECT 1 FROM canonical_events WHERE orphaned=0 AND id>? LIMIT 1", (last_event_id,)
            ).fetchone() is not None
        if not pending:
            self._reconcile_invalid_targets()
            self.rebuild_clusters()
        cursor = bootstrap_cursor if not pending else int(state["change_cursor"] if state else 0)
        after = self.store.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0]
        stats = {
            "processed": int(prior_stats.get("processed") or 0) + processed,
            "newSignals": int(prior_stats.get("newSignals") or 0) + after - before,
            "lastEventId": last_event_id,
            "bootstrapChangeCursor": bootstrap_cursor,
            "sourceReparseRequired": int(prior_stats.get("sourceReparseRequired") or 0)
                + self._bootstrap_reparse_count,
            "resolverVersion": self.resolver_version,
        }
        final_status = "pending" if pending else (
            "needs-source-reparse" if stats["sourceReparseRequired"] else "ready"
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO feedback_derivation_state(
                       detector_id, detector_version, change_cursor, bootstrap_complete,
                       last_scan_run_id, status, stats_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(detector_id) DO UPDATE SET detector_version=excluded.detector_version,
                       change_cursor=excluded.change_cursor, bootstrap_complete=excluded.bootstrap_complete,
                       last_scan_run_id=excluded.last_scan_run_id, status=excluded.status,
                       stats_json=excluded.stats_json, updated_at=excluded.updated_at""",
                (self.detector_id, self.detector_version, cursor, int(not pending), last_scan_run_id,
                 final_status,
                 _json(stats), _now()),
            )
        return {"processed": processed, "newSignals": after - before, "pending": pending,
                "bootstrapComplete": not pending, "changeCursor": cursor,
                "status": final_status, "sourceReparseRequired": stats["sourceReparseRequired"]}

    def process_changes(
        self, *, max_changes: int = 5000, max_seconds: float = 2.0,
        last_scan_run_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction():
            return self._process_changes(
                max_changes=max_changes, max_seconds=max_seconds,
                last_scan_run_id=last_scan_run_id,
            )

    def _process_changes(
        self, *, max_changes: int = 5000, max_seconds: float = 2.0,
        last_scan_run_id: str | None = None,
    ) -> dict[str, Any]:
        if max_changes < 0 or max_seconds < 0:
            raise ValueError("change budgets must be non-negative")
        state = self._derivation_state()
        state_stats = _mapping(state["stats_json"]) if state else {}
        if (
            state is None or not state["bootstrap_complete"]
            or state["detector_version"] != self.detector_version
            or state_stats.get("resolverVersion") != self.resolver_version
        ):
            boot = self.bootstrap(max_events=max_changes, max_seconds=max_seconds,
                                  last_scan_run_id=last_scan_run_id)
            if boot["pending"]:
                return boot
            state = self._derivation_state()
        if state["status"] == "needs-source-reparse":
            saved_stats = _mapping(state["stats_json"])
            return {
                "processed": 0, "orphaned": 0, "reactivated": 0,
                "pending": False, "changeCursor": state["change_cursor"],
                "status": "needs-source-reparse",
                "sourceReparseRequired": int(saved_stats.get("sourceReparseRequired") or 0),
            }
        cursor = int(state["change_cursor"])
        rows = self.store.execute(
            "SELECT * FROM effect_derivation_changes WHERE id>? ORDER BY id LIMIT ?",
            (cursor, max(0, int(max_changes)) + 1),
        ).fetchall()
        started = time.monotonic()
        processed = orphaned = reactivated = 0
        pending = False
        for change in rows:
            if processed >= max_changes or time.monotonic() - started >= max_seconds:
                pending = True
                break
            if change["change_type"] == "target-invalidated":
                orphaned += self._invalidate_target_object(change["entity_id"])
            elif change["change_type"] == "target-reactivated":
                if change["entity_kind"] == "feedback-target":
                    reactivated += self._restore_target_id(change["entity_id"])
                else:
                    reactivated += self._restore_target_object(change["entity_id"])
            elif change["change_type"] == "case-invalidated":
                orphaned += self._invalidate_target_case(change["entity_id"])
            elif change["change_type"] == "case-reactivated":
                reactivated += self._restore_target_case(change["entity_id"])
            elif change["entity_kind"] == "canonical-event":
                if change["change_type"] == "event-orphaned":
                    orphaned += self._orphan_event(change["entity_id"])
                    orphaned += self._invalidate_target_object(change["entity_id"])
                    orphaned += self._reopen_lost_result_verifications(change["entity_id"])
                elif change["change_type"] in {"event-reactivated", "provenance-added", "event-available"}:
                    before = self.store.execute(
                        """SELECT COUNT(*) FROM feedback_signal_revisions r JOIN feedback_signals s
                           ON s.id=r.feedback_signal_id WHERE s.feedback_event_id=? AND r.orphaned=1""",
                        (change["entity_id"],),
                    ).fetchone()[0]
                    orphan_signals = self.store.execute(
                        """SELECT s.id, s.current_machine_revision_id FROM feedback_signals s
                           JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
                           WHERE s.feedback_event_id=? AND r.orphaned=1""", (change["entity_id"],),
                    ).fetchall()
                    for orphan_signal in orphan_signals:
                        self._reactivate_revision(
                            orphan_signal["id"], orphan_signal["current_machine_revision_id"], _now()
                        )
                    event = self.store.execute("SELECT * FROM canonical_events WHERE id=?", (change["entity_id"],)).fetchone()
                    if event is not None and not event["orphaned"]:
                        self._derive_stored_event(event)
                        reactivated += self._restore_target_event(change["entity_id"])
                        result = self.store.execute(
                            "SELECT tool_call_id FROM tool_results WHERE event_id=?",
                            (change["entity_id"],),
                        ).fetchone()
                        if result is not None:
                            reactivated += self._resolve_result_missing(
                                result["tool_call_id"], change["entity_id"]
                            )
                    after = self.store.execute(
                        """SELECT COUNT(*) FROM feedback_signal_revisions r JOIN feedback_signals s
                           ON s.id=r.feedback_signal_id WHERE s.feedback_event_id=? AND r.orphaned=1""",
                        (change["entity_id"],),
                    ).fetchone()[0]
                    reactivated += max(0, before - after)
            cursor = change["id"]
            processed += 1
        remaining = self.store.execute(
            "SELECT 1 FROM effect_derivation_changes WHERE id>? LIMIT 1", (cursor,)
        ).fetchone() is not None
        pending = pending or remaining
        stats = {
            "processed": processed, "orphaned": orphaned, "reactivated": reactivated,
            "pending": pending, "resolverVersion": self.resolver_version,
        }
        with self.store.transaction():
            self.store.execute(
                """UPDATE feedback_derivation_state SET detector_version=?, change_cursor=?,
                       last_scan_run_id=COALESCE(?, last_scan_run_id), status=?, stats_json=?, updated_at=?
                   WHERE detector_id=?""",
                (self.detector_version, cursor, last_scan_run_id, "pending" if pending else "ready",
                 _json(stats), _now(), self.detector_id),
            )
        return {**stats, "changeCursor": cursor}

    consume_changes = process_changes

    def rebuild(self, *, max_events: int = 5000, max_seconds: float = 2.0) -> dict[str, Any]:
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO feedback_derivation_state(
                       detector_id, detector_version, change_cursor, bootstrap_complete,
                       status, stats_json, updated_at)
                   VALUES (?, ?, 0, 0, 'pending', '{}', ?)
                   ON CONFLICT(detector_id) DO UPDATE SET detector_version=excluded.detector_version,
                       change_cursor=0, bootstrap_complete=0, status='pending',
                       stats_json='{}', updated_at=excluded.updated_at""",
                (self.detector_id, self.detector_version, _now()),
            )
        return self.bootstrap(max_events=max_events, max_seconds=max_seconds)

    rebuild_derivations = rebuild

    def reparse_truncated_sources(
        self, allowed_roots: Sequence[str | Path], *,
        max_events: int = 5000, max_seconds: float = 20.0,
        max_event_bytes: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]:
        from effect_adapters import parse_jsonl_line

        resolved_roots: list[Path] = []
        root_failures: list[dict[str, str]] = []
        for root in allowed_roots:
            try:
                resolved_roots.append(Path(root).expanduser().resolve(strict=True))
            except OSError as exc:
                root_failures.append({"eventId": "<source-root>", "error": type(exc).__name__})
        roots = tuple(resolved_roots)
        if not roots:
            if not root_failures:
                root_failures.append({"eventId": "<source-roots>", "error": "ValueError"})
            remaining = max(1, self._source_reparse_remaining())
            result = {
                "processed": 0, "updated": 0, "failed": len(root_failures),
                "failures": root_failures,
                "remaining": remaining, "pending": True,
            }
            self._persist_source_reparse_state(result)
            return result
        rows = self.store.execute(
            """SELECT e.*, p.byte_start, p.byte_end, p.locator_json AS provenance_locator_json,
                      location.path,
                      generation.device AS generation_device,
                      generation.inode AS generation_inode,
                      generation.observed_size AS generation_observed_size
               FROM canonical_events e
               JOIN event_provenance p ON p.event_id=e.id
               JOIN log_file_generations generation ON generation.id=p.generation_id
               JOIN log_file_locations location ON location.generation_id=p.generation_id
                 AND location.is_current=1
               WHERE e.orphaned=0 AND e.event_type IN ('user_message','assistant_message')
                 AND json_extract(e.payload_json, '$.text') LIKE '%...[truncated]'
                 AND COALESCE(json_extract(e.payload_json, '$.metadata.feedback_detector_version'),'')<>?
               ORDER BY e.id, p.observed_at DESC LIMIT ?""",
            (self.detector_version, max(1, int(max_events)) + 1),
        ).fetchall()
        started = time.monotonic()
        processed = updated = 0
        failures: list[dict[str, str]] = list(root_failures)
        seen: set[str] = set()
        for row in rows:
            if row["id"] in seen:
                continue
            if processed >= max_events or time.monotonic() - started >= max_seconds:
                break
            seen.add(row["id"])
            processed += 1
            descriptor: int | None = None
            try:
                path = Path(row["path"]).expanduser().resolve(strict=True)
                if not any(path == root or root in path.parents for root in roots):
                    raise ValueError("provenance path is outside allowed session roots")
                no_follow = getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, os.O_RDONLY | no_follow)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("provenance path is not a regular file")
                if row["generation_device"] and str(metadata.st_dev) != str(row["generation_device"]):
                    raise ValueError("source device no longer matches provenance generation")
                if row["generation_inode"] and str(metadata.st_ino) != str(row["generation_inode"]):
                    raise ValueError("source inode no longer matches provenance generation")
                start_offset = int(row["byte_start"])
                end_offset = int(row["byte_end"] or min(metadata.st_size, start_offset + max_event_bytes))
                length = end_offset - start_offset
                if length <= 0 or length > max_event_bytes:
                    raise ValueError("source event exceeds reparse byte limit")
                os.lseek(descriptor, start_offset, os.SEEK_SET)
                raw = os.read(descriptor, length)
                provenance_locator = _mapping(row["provenance_locator_json"])
                raw_line_sha256 = provenance_locator.get("rawLineSha256")
                if not isinstance(raw_line_sha256, str) or not raw_line_sha256:
                    raise ValueError("source provenance is missing raw line hash")
                if hashlib.sha256(raw).hexdigest() != raw_line_sha256:
                    raise ValueError("source line no longer matches provenance hash")
                item = json.loads(raw.decode("utf-8").rstrip("\r\n"))
                events = parse_jsonl_line(
                    row["source"], item,
                    session_id="", session_family=row["session_family"],
                )
                candidates = [event for event in events if event.event_type == row["event_type"]]
                if row["source_event_id"]:
                    exact = [event for event in candidates if event.event_id == row["source_event_id"]]
                    if not exact:
                        raise ValueError("source event ID no longer matches canonical provenance")
                    candidates = exact
                if not candidates:
                    raise ValueError("source event no longer projects to the canonical type")
                if candidates[0].fingerprint != row["event_fingerprint"]:
                    raise ValueError("source event fingerprint no longer matches canonical provenance")
                refreshed_payload = candidates[0].to_dict()
                with self.store.transaction():
                    self.store.execute(
                        "UPDATE canonical_events SET payload_json=?, updated_at=? WHERE id=?",
                        (_json(refreshed_payload), _now(), row["id"]),
                    )
                    refreshed = self.store.execute(
                        "SELECT * FROM canonical_events WHERE id=?", (row["id"],),
                    ).fetchone()
                    self._derive_stored_event(refreshed)
                updated += 1
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                failures.append({"eventId": row["id"], "error": type(exc).__name__})
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        remaining = self._source_reparse_remaining() + len(root_failures)
        result = {
            "processed": processed, "updated": updated, "failed": len(failures),
            "failures": failures[:100], "remaining": remaining,
            "pending": bool(remaining),
        }
        self._persist_source_reparse_state(result)
        return result

    def _source_reparse_remaining(self) -> int:
        return int(self.store.execute(
            """SELECT COUNT(DISTINCT e.id) FROM canonical_events e
               WHERE e.orphaned=0 AND e.event_type IN ('user_message','assistant_message')
                 AND json_extract(e.payload_json, '$.text') LIKE '%...[truncated]'
                 AND COALESCE(json_extract(e.payload_json, '$.metadata.feedback_detector_version'),'')<>?""",
            (self.detector_version,),
        ).fetchone()[0])

    def _persist_source_reparse_state(self, result: Mapping[str, Any]) -> None:
        state = self._derivation_state()
        prior = _mapping(state["stats_json"]) if state else {}
        remaining = int(result.get("remaining") or 0)
        failures = list(result.get("failures") or [])[:100]
        stats = {
            **prior,
            "resolverVersion": self.resolver_version,
            "sourceReparseRequired": remaining,
            "sourceReparseFailed": int(result.get("failed") or 0),
            "sourceReparseFailures": failures,
        }
        bootstrap_complete = int(state["bootstrap_complete"]) if state else 0
        status = "needs-source-reparse" if remaining else (
            "ready" if bootstrap_complete else "pending"
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO feedback_derivation_state(
                       detector_id, detector_version, change_cursor, bootstrap_complete,
                       status, stats_json, updated_at)
                   VALUES (?, ?, 0, ?, ?, ?, ?)
                   ON CONFLICT(detector_id) DO UPDATE SET
                       detector_version=excluded.detector_version,
                       status=excluded.status, stats_json=excluded.stats_json,
                       updated_at=excluded.updated_at""",
                (self.detector_id, self.detector_version, bootstrap_complete,
                 status, _json(stats), _now()),
            )

    def _invalidate_target_object(self, entity_id: str) -> int:
        targets = self.store.execute(
            """SELECT * FROM feedback_targets WHERE id=? OR target_event_id=?
               OR skill_invocation_id=? OR tool_call_id=? OR tool_result_id=?
               OR tool_call_id IN (SELECT id FROM tool_calls WHERE event_id=?)
               OR tool_result_id IN (SELECT id FROM tool_results WHERE event_id=?)""",
            (entity_id, entity_id, entity_id, entity_id, entity_id, entity_id, entity_id),
        ).fetchall()
        return self._invalidate_targets(targets, "target-provenance-invalidated")

    def _invalidate_target_case(self, task_case_id: str) -> int:
        targets = self.store.execute(
            """SELECT * FROM feedback_targets
               WHERE target_task_case_id=? OR context_task_case_id=?""",
            (task_case_id, task_case_id),
        ).fetchall()
        return self._invalidate_targets(targets, "target-case-invalidated")

    def _restore_target_object(self, entity_id: str) -> int:
        targets = self.store.execute(
            """SELECT t.* FROM feedback_targets t JOIN feedback_signals s
                 ON s.id=t.feedback_signal_id
               JOIN skill_invocations invocation ON invocation.id=t.skill_invocation_id
               WHERE t.skill_invocation_id=? AND invocation.validity='valid'
                 AND t.machine_status='orphaned'
                 AND t.signal_revision_id=s.current_machine_revision_id""",
            (entity_id,),
        ).fetchall()
        return self._restore_targets(
            targets, "target-object-restored", {"skillInvocationId": entity_id},
        )

    def _restore_target_case(self, task_case_id: str) -> int:
        targets = self.store.execute(
            """SELECT t.* FROM feedback_targets t JOIN feedback_signals s
                 ON s.id=t.feedback_signal_id
               LEFT JOIN canonical_events event ON event.id=t.target_event_id
               LEFT JOIN skill_invocations invocation ON invocation.id=t.skill_invocation_id
               LEFT JOIN task_cases target_case ON target_case.id=t.target_task_case_id
               LEFT JOIN task_cases context_case ON context_case.id=t.context_task_case_id
               WHERE (t.target_task_case_id=? OR t.context_task_case_id=?)
                 AND t.machine_status='orphaned'
                 AND t.signal_revision_id=s.current_machine_revision_id
                 AND (t.target_event_id IS NULL OR event.orphaned=0)
                 AND (t.skill_invocation_id IS NULL OR invocation.validity='valid')
                 AND (t.target_task_case_id IS NULL OR target_case.invalidated_at IS NULL)
                 AND (t.context_task_case_id IS NULL OR context_case.invalidated_at IS NULL)
                 AND (t.target_kind!='tool-call' OR t.tool_call_id IS NOT NULL)
                 AND (t.target_kind!='tool-result' OR t.tool_result_id IS NOT NULL)""",
            (task_case_id, task_case_id),
        ).fetchall()
        return self._restore_targets(
            targets, "target-case-restored", {"taskCaseId": task_case_id},
        )

    def _restore_targets(
        self, targets: Sequence[Mapping[str, Any]], reason: str,
        binding: Mapping[str, Any],
    ) -> int:
        signals: set[str] = set()
        with self.store.transaction():
            for target in targets:
                self.store.execute(
                    "UPDATE feedback_targets SET machine_status='candidate' WHERE id=?",
                    (target["id"],),
                )
                signals.add(target["feedback_signal_id"])
            for signal_id in signals:
                signal = self.store.execute(
                    "SELECT current_resolution_state FROM feedback_signals WHERE id=?",
                    (signal_id,),
                ).fetchone()
                self._append_system_action(
                    signal_id, "target-disputed", reason,
                    process_state="triaged",
                    resolution_state=signal["current_resolution_state"],
                    binding=dict(binding),
                )
                review = self._review_for_signal(signal_id)
                if review:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open',
                               queue_reason='target-restored-review', updated_at=? WHERE id=?""",
                        (_now(), review["id"]),
                    )
        return len(signals)

    def _reconcile_invalid_targets(self) -> int:
        rows = self.store.execute(
            """SELECT t.* FROM feedback_targets t
               LEFT JOIN canonical_events event ON event.id=t.target_event_id
               LEFT JOIN skill_invocations invocation ON invocation.id=t.skill_invocation_id
               LEFT JOIN task_cases target_case ON target_case.id=t.target_task_case_id
               LEFT JOIN task_cases context_case ON context_case.id=t.context_task_case_id
               WHERE t.machine_status='candidate' AND (
                 (t.target_event_id IS NOT NULL AND (event.id IS NULL OR event.orphaned=1))
                 OR (t.skill_invocation_id IS NOT NULL AND
                     (invocation.id IS NULL OR invocation.validity!='valid'))
                 OR (t.tool_call_id IS NULL AND t.target_kind='tool-call')
                 OR (t.tool_result_id IS NULL AND t.target_kind='tool-result')
                 OR (t.target_task_case_id IS NOT NULL AND
                     (target_case.id IS NULL OR target_case.invalidated_at IS NOT NULL))
                 OR (t.context_task_case_id IS NOT NULL AND
                     (context_case.id IS NULL OR context_case.invalidated_at IS NOT NULL))
               )"""
        ).fetchall()
        return self._invalidate_targets(rows, "bootstrap-target-invalid")

    def _restore_target_event(self, event_id: str) -> int:
        rows = self.store.execute(
            """SELECT t.* FROM feedback_targets t JOIN feedback_signals s
                 ON s.id=t.feedback_signal_id
               WHERE t.target_event_id=? AND t.machine_status='orphaned'
                 AND t.signal_revision_id=s.current_machine_revision_id""",
            (event_id,),
        ).fetchall()
        signals: set[str] = set()
        with self.store.transaction():
            for target in rows:
                self.store.execute(
                    "UPDATE feedback_targets SET machine_status='candidate' WHERE id=?",
                    (target["id"],),
                )
                signals.add(target["feedback_signal_id"])
            for signal_id in signals:
                signal = self.store.execute(
                    "SELECT current_resolution_state FROM feedback_signals WHERE id=?",
                    (signal_id,),
                ).fetchone()
                self._append_system_action(
                    signal_id, "target-disputed", "target-provenance-restored",
                    process_state="triaged", resolution_state=signal["current_resolution_state"],
                    binding={"targetEventId": event_id},
                )
                review = self._review_for_signal(signal_id)
                if review:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open',
                               queue_reason='target-restored-review', updated_at=? WHERE id=?""",
                        (_now(), review["id"]),
                    )
        return len(signals)

    def _restore_target_id(self, target_id: str) -> int:
        target = self.store.execute(
            """SELECT t.* FROM feedback_targets t JOIN feedback_signals s
                 ON s.id=t.feedback_signal_id
               WHERE t.id=? AND t.machine_status='orphaned'
                 AND t.signal_revision_id=s.current_machine_revision_id""",
            (target_id,),
        ).fetchone()
        if target is None:
            return 0
        object_restored = (
            (target["target_kind"] == "tool-call" and target["tool_call_id"] is not None)
            or (target["target_kind"] == "tool-result" and target["tool_result_id"] is not None)
        )
        if not object_restored:
            return 0
        with self.store.transaction():
            self.store.execute(
                "UPDATE feedback_targets SET machine_status='candidate' WHERE id=?", (target_id,),
            )
            signal = self.store.execute(
                "SELECT current_resolution_state FROM feedback_signals WHERE id=?",
                (target["feedback_signal_id"],),
            ).fetchone()
            self._append_system_action(
                target["feedback_signal_id"], "target-disputed", "target-object-restored",
                process_state="triaged", resolution_state=signal["current_resolution_state"],
                binding={"targetId": target_id},
            )
            review = self._review_for_signal(target["feedback_signal_id"])
            if review:
                self.store.execute(
                    """UPDATE review_tasks SET status='open',
                           queue_reason='target-restored-review', updated_at=? WHERE id=?""",
                    (_now(), review["id"]),
                )
        return 1

    def _invalidate_targets(self, targets: Sequence[Mapping[str, Any]], reason: str) -> int:
        changed_signals: set[str] = set()
        with self.store.transaction():
            for target in targets:
                if target["machine_status"] != "orphaned":
                    self.store.execute(
                        "UPDATE feedback_targets SET machine_status='orphaned' WHERE id=?", (target["id"],)
                    )
                changed_signals.add(target["feedback_signal_id"])
            for signal_id in changed_signals:
                signal = self.store.execute(
                    "SELECT current_resolution_state FROM feedback_signals WHERE id=?", (signal_id,)
                ).fetchone()
                self._append_system_action(
                    signal_id, "target-disputed", reason, process_state="triaged",
                    resolution_state=signal["current_resolution_state"],
                )
                review = self._review_for_signal(signal_id)
                if review:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open', queue_reason='target-invalidated',
                               updated_at=? WHERE id=?""", (_now(), review["id"]),
                    )
                self._remove_cluster_memberships(signal_id)
        return len(changed_signals)

    def _orphan_event(self, event_id: str) -> int:
        rows = self.store.execute(
            "SELECT * FROM feedback_signals WHERE feedback_event_id=?", (event_id,)
        ).fetchall()
        changed = 0
        with self.store.transaction():
            for signal in rows:
                revision_id = signal["current_machine_revision_id"]
                revision = self.store.execute("SELECT orphaned FROM feedback_signal_revisions WHERE id=?", (revision_id,)).fetchone()
                if revision is None or revision["orphaned"]:
                    continue
                self.store.execute("UPDATE feedback_signal_revisions SET orphaned=1 WHERE id=?", (revision_id,))
                self.store.execute(
                    "UPDATE feedback_targets SET machine_status='orphaned' WHERE signal_revision_id=? AND machine_status='candidate'",
                    (revision_id,),
                )
                self._append_system_action(
                    signal["id"], "orphaned", "event-provenance-lost",
                    process_state="orphaned",
                    binding={"eventId": event_id, "machineRevisionId": revision_id},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        "UPDATE review_tasks SET status='open', queue_reason='source-invalidated', updated_at=? WHERE id=?",
                        (_now(), review["id"]),
                    )
                self._remove_cluster_memberships(signal["id"])
                changed += 1
        return changed

    def _reopen_lost_result_verifications(self, event_id: str) -> int:
        rows = self.store.execute(
            """SELECT DISTINCT s.* FROM feedback_signals s JOIN feedback_actions a
                 ON a.feedback_signal_id=s.id
               JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               WHERE r.category='result-missing' AND a.action='resolve-verified'
                 AND json_extract(a.binding_json, '$.resultEventId')=?
                 AND s.current_resolution_state='resolved-verified'""",
            (event_id,),
        ).fetchall()
        with self.store.transaction():
            for signal in rows:
                self._append_system_action(
                    signal["id"], "reopen", "result-verification-lost",
                    process_state="queued", resolution_state="unreviewed",
                    binding={"resultEventId": event_id},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open',
                               queue_reason='result-verification-lost', updated_at=? WHERE id=?""",
                        (_now(), review["id"]),
                    )
        return len(rows)

    def _derive_stored_event(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        event = _decoded(row)
        own_case = self._case_for_event(event["id"])
        requires_reparse = self._event_requires_source_reparse(event)
        if requires_reparse:
            self._bootstrap_reparse_count += 1
            self._mark_source_reparse_required(event["id"])
            return []
        if event["event_type"] == "user_message":
            derived = self.derive_user_event(event, own_case)
            if not requires_reparse:
                self._supersede_absent_event_signals(event["id"], "user-feedback", derived)
            return derived
        if event["event_type"] == "assistant_message":
            derived = self.derive_assistant_event(event, own_case)
            if not requires_reparse:
                self._supersede_absent_event_signals(event["id"], "assistant-claim", derived)
            return derived
        if event["event_type"] == "tool_result":
            call = self._call_context_for_event(event)
            derived = self.derive_process_result(event, call, {"own_case_id": own_case})
            self._supersede_absent_event_signals(event["id"], "process-anomaly", derived)
            return derived
        return []

    def _event_requires_source_reparse(self, event: Mapping[str, Any]) -> bool:
        payload = _mapping(event.get("payload")) or _mapping(event.get("payload_json"))
        metadata = _mapping(payload.get("metadata"))
        text = str(payload.get("text") or "")
        return (
            text.endswith("...[truncated]")
            and metadata.get("feedback_detector_version") != self.detector_version
        )

    def _mark_source_reparse_required(self, event_id: str) -> int:
        rows = self.store.execute(
            "SELECT * FROM feedback_signals WHERE feedback_event_id=?", (event_id,),
        ).fetchall()
        changed = 0
        with self.store.transaction():
            for signal in rows:
                latest = self.store.execute(
                    """SELECT reason_code FROM feedback_actions WHERE feedback_signal_id=?
                       ORDER BY revision DESC LIMIT 1""", (signal["id"],),
                ).fetchone()
                if latest is not None and latest["reason_code"] == "source-reparse-required":
                    continue
                self._append_system_action(
                    signal["id"], "target-disputed", "source-reparse-required",
                    process_state="needs-evidence",
                    resolution_state=signal["current_resolution_state"],
                    binding={"eventId": event_id, "detectorVersion": self.detector_version},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        """UPDATE review_tasks SET status='open',
                               queue_reason='source-reparse-required', updated_at=? WHERE id=?""",
                        (_now(), review["id"]),
                    )
                changed += 1
        return changed

    def _supersede_absent_event_signals(
        self, event_id: str, channel: str, derived: Sequence[Mapping[str, Any]],
    ) -> int:
        retained = {str(item.get("id")) for item in derived if item.get("id")}
        rows = self.store.execute(
            """SELECT s.* FROM feedback_signals s JOIN feedback_signal_revisions r
                 ON r.id=s.current_machine_revision_id
               WHERE s.feedback_event_id=? AND r.channel=? AND r.detector_id=?""",
            (event_id, channel, self.detector_id),
        ).fetchall()
        changed = 0
        with self.store.transaction():
            for signal in rows:
                if signal["id"] in retained:
                    continue
                revision_id = signal["current_machine_revision_id"]
                self.store.execute(
                    "UPDATE feedback_signal_revisions SET is_current=0 WHERE id=?", (revision_id,)
                )
                self.store.execute(
                    """UPDATE feedback_targets SET machine_status='superseded'
                       WHERE signal_revision_id=? AND machine_status='candidate'""", (revision_id,),
                )
                self.store.execute(
                    """UPDATE feedback_signals SET current_machine_revision_id=NULL,
                           current_process_state='superseded', updated_at=? WHERE id=?""",
                    (_now(), signal["id"]),
                )
                self._append_system_action(
                    signal["id"], "superseded", "detector-no-longer-matches",
                    process_state="superseded",
                    binding={"eventId": event_id, "machineRevisionId": revision_id,
                             "detectorVersion": self.detector_version},
                )
                review = self._review_for_signal(signal["id"])
                if review:
                    self.store.execute(
                        "UPDATE review_tasks SET status='superseded', updated_at=? WHERE id=?",
                        (_now(), review["id"]),
                    )
                self._remove_cluster_memberships(signal["id"])
                changed += 1
        return changed

    # -------------------------------------------------------- semantic classify
    def semantic_payload(
        self, signal_id: str, *, model_version: str,
        prompt_version: str, rubric_version: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        from feedback_semantic_classifier import FEEDBACK_CATEGORIES, SCHEMA_VERSION

        signal = self.get_signal(signal_id)
        current = next((item for item in signal["machine_revisions"] if item["is_current"]), None)
        if current is None or current["orphaned"]:
            raise RevisionConflict("feedback machine revision is not current")
        targets = [
            item for item in signal["targets"]
            if item["signal_revision_id"] == current["id"] and item["machine_status"] == "candidate"
        ]
        if not targets:
            raise EffectStoreError("semantic classification requires a current target candidate")
        if not current.get("redacted_excerpt"):
            raise EffectStoreError("semantic classification requires a redacted feedback span")
        metadata = current.get("metadata") or {}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "feedback_signal_id": signal_id,
            "current_machine_revision_id": current["id"],
            "current_machine_revision": current["revision"],
            "current_action_revision": signal["current_action_revision"],
            "language": metadata.get("language") or "unknown",
            "evidence_spans": [{
                "id": f"span:{current['id']}",
                "redacted_text": current["redacted_excerpt"],
            }],
            "target_ids": [item["id"] for item in targets],
            "category_candidates": list(FEEDBACK_CATEGORIES),
            "version_tuple": {
                "detector_version": self.detector_version,
                "resolver_version": self.resolver_version,
                "model_version": model_version,
                "prompt_version": prompt_version,
                "rubric_version": rubric_version,
            },
        }
        profiles = self.store.execute(
            """SELECT * FROM feedback_calibration_profiles profile
               WHERE detector_version=? AND resolver_version=? AND language=?
                 AND model_version=? AND prompt_version=? AND rubric_version=?
                 AND NOT EXISTS (
                   SELECT 1 FROM feedback_calibration_profiles newer
                   WHERE newer.detector_version=profile.detector_version
                     AND newer.resolver_version=profile.resolver_version
                     AND newer.language=profile.language AND newer.category=profile.category
                     AND newer.model_version=profile.model_version
                     AND newer.prompt_version=profile.prompt_version
                     AND newer.rubric_version=profile.rubric_version
                     AND (newer.created_at>profile.created_at
                          OR (newer.created_at=profile.created_at AND newer.id>profile.id))
                 )
               ORDER BY category""",
            (self.detector_version, self.resolver_version, payload["language"],
             model_version, prompt_version, rubric_version),
        ).fetchall()
        if not profiles:
            calibration: dict[str, Any] | None = None
        elif len(profiles) == 1:
            calibration = _decoded(profiles[0])
        else:
            calibration = {
                "profilesByCategory": {
                    row["category"]: _decoded(row) for row in profiles
                }
            }
        return payload, calibration

    def apply_semantic_review(self, review: Mapping[str, Any]) -> dict[str, Any]:
        with self.store.transaction():
            return self._apply_semantic_review(review)

    def _apply_semantic_review(self, review: Mapping[str, Any]) -> dict[str, Any]:
        from feedback_semantic_classifier import validate_review_output

        validate_review_output(review)
        signal_id = str(review["feedback_signal_id"])
        signal = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (signal_id,)).fetchone()
        if signal is None:
            raise KeyError(signal_id)
        if signal["current_machine_revision_id"] != review["current_machine_revision_id"]:
            raise RevisionConflict("feedback changed during semantic classification")
        if signal["current_action_revision"] != review["current_action_revision"]:
            raise RevisionConflict("feedback actions changed during semantic classification")
        revision = self.store.execute(
            "SELECT * FROM feedback_signal_revisions WHERE id=? AND revision=?",
            (review["current_machine_revision_id"], review["current_machine_revision"]),
        ).fetchone()
        if revision is None or not revision["is_current"] or revision["orphaned"]:
            raise RevisionConflict("feedback machine revision is no longer current")
        target = self.store.execute(
            """SELECT * FROM feedback_targets WHERE id=? AND feedback_signal_id=?
               AND signal_revision_id=? AND machine_status='candidate'""",
            (review["target_id"], signal_id, revision["id"]),
        ).fetchone()
        if target is None:
            raise RevisionConflict("semantic target is no longer current")
        review_id = _digest(
            "feedback-semantic-review", signal_id, revision["id"],
            review["model_version"], review["prompt_version"], review["rubric_version"],
        )
        with self.store.transaction():
            current_binding = self.store.execute(
                """SELECT s.current_machine_revision_id, s.current_action_revision,
                          r.revision, r.is_current, r.orphaned
                   FROM feedback_signals s JOIN feedback_signal_revisions r
                     ON r.id=s.current_machine_revision_id WHERE s.id=?""",
                (signal_id,),
            ).fetchone()
            if (
                current_binding is None
                or current_binding["current_machine_revision_id"] != review["current_machine_revision_id"]
                or current_binding["revision"] != review["current_machine_revision"]
                or current_binding["current_action_revision"] != review["current_action_revision"]
                or not current_binding["is_current"] or current_binding["orphaned"]
            ):
                raise RevisionConflict("feedback changed during semantic classification")
            current_target = self.store.execute(
                """SELECT 1 FROM feedback_targets WHERE id=? AND feedback_signal_id=?
                   AND signal_revision_id=? AND machine_status='candidate'""",
                (review["target_id"], signal_id, review["current_machine_revision_id"]),
            ).fetchone()
            if current_target is None:
                raise RevisionConflict("semantic target changed during classification")
            self.store.execute(
                """INSERT INTO feedback_semantic_reviews(
                       id, feedback_signal_id, signal_revision_id, model_id, model_version,
                       prompt_version, rubric_version, calibration_profile_id, verdict,
                       category, severity, confidence, target_id, review_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(feedback_signal_id, signal_revision_id, model_version,
                               prompt_version, rubric_version) DO NOTHING""",
                (
                    review_id, signal_id, revision["id"], review["model_id"],
                    review["model_version"], review["prompt_version"], review["rubric_version"],
                    review.get("calibration_profile_id"), review["verdict"], review["category"],
                    review["severity"], review["confidence"], review["target_id"],
                    _json(review), _now(),
                ),
            )
            stored_review = self.store.execute(
                """SELECT * FROM feedback_semantic_reviews
                   WHERE feedback_signal_id=? AND signal_revision_id=? AND model_version=?
                     AND prompt_version=? AND rubric_version=?""",
                (signal_id, revision["id"], review["model_version"],
                 review["prompt_version"], review["rubric_version"]),
            ).fetchone()
            if stored_review["review_json"] != _json(review):
                raise EffectStoreError("semantic review tuple is immutable")
            review_id = stored_review["id"]
        if review["verdict"] != "classified":
            result = dict(review)
            result["id"] = review_id
            return result
        event = self.store.execute(
            "SELECT * FROM canonical_events WHERE id=?", (signal["feedback_event_id"],)
        ).fetchone()
        target_descriptor = {
            key: target[key] for key in (
                "target_kind", "context_task_case_id", "target_task_case_id",
                "target_event_id", "skill_invocation_id", "tool_call_id", "tool_result_id",
                "relation", "confidence",
            )
        }
        target_descriptor["evidence"] = _mapping(target["evidence_json"])
        candidate = {
            "category": review["category"], "severity": review["severity"],
            "confidence": review["confidence"], "channel": revision["channel"],
            "authority": revision["authority"], "adjustments": [],
            "detector_id": "feedback-semantic-classifier",
            "detector_version": (
                f"{review['model_version']}@{review['prompt_version']}#{review['rubric_version']}"
            ),
            "span": {
                "block_index": _mapping(revision["locator_json"]).get("blockIndex", 0),
                "start": _mapping(revision["locator_json"]).get("start", 0),
                "end": _mapping(revision["locator_json"]).get("end", 0),
                "origin": _mapping(revision["locator_json"]).get("origin", "user-authored"),
                "excerpt_hash": revision["excerpt_hash"],
                "redacted_excerpt": revision["redacted_excerpt"],
                "protocol_locator": _mapping(revision["locator_json"]).get("protocol", ""),
                "redaction_status": "redacted",
            },
            "metadata": {
                "semanticReviewId": review_id,
                "calibrationProfileId": review.get("calibration_profile_id"),
                "semanticRationale": review["rationale"],
            },
        }
        applied = self._persist_candidate(
            _decoded(event), signal["feedback_case_id"], candidate, [target_descriptor],
            source=revision["source"],
        )
        return {**dict(review), "id": review_id, "appliedSignal": applied}

    # --------------------------------------------------------------- cleanup
    def cleanup(
        self, *, task_case_id: str | None = None, skill_id: str | None = None,
        project_id: str | None = None, older_than: str | None = None,
    ) -> dict[str, Any]:
        if not any((task_case_id, skill_id, project_id, older_than)):
            raise ValueError("cleanup requires task_case_id, skill_id, project_id, or older_than")
        conditions: list[str] = []
        params: list[Any] = []
        if task_case_id:
            conditions.append("(s.feedback_case_id=? OR EXISTS (SELECT 1 FROM feedback_targets t WHERE t.feedback_signal_id=s.id AND (t.context_task_case_id=? OR t.target_task_case_id=?)))")
            params.extend((task_case_id, task_case_id, task_case_id))
        if skill_id:
            conditions.append("""EXISTS (
                SELECT 1 FROM feedback_targets t
                LEFT JOIN skill_invocations direct_i ON direct_i.id=t.skill_invocation_id
                WHERE t.feedback_signal_id=s.id AND (
                    direct_i.skill_id=? OR EXISTS (
                        SELECT 1 FROM attribution_links l JOIN skill_invocations i
                          ON i.id=l.skill_invocation_id
                        WHERE l.task_case_id=COALESCE(t.target_task_case_id,t.context_task_case_id)
                          AND l.status='active' AND i.skill_id=?
                    )
                )
            )""")
            params.extend((skill_id, skill_id))
            conditions.append("""NOT EXISTS (
                SELECT 1 FROM feedback_targets t
                JOIN attribution_links other_link
                  ON other_link.task_case_id=COALESCE(t.target_task_case_id,t.context_task_case_id)
                 AND other_link.status='active'
                JOIN skill_invocations other_skill ON other_skill.id=other_link.skill_invocation_id
                WHERE t.feedback_signal_id=s.id AND other_skill.skill_id<>?
            )""")
            params.append(skill_id)
        if project_id:
            conditions.append("""EXISTS (
                SELECT 1 FROM task_cases c WHERE json_extract(c.metadata_json,'$.projectId')=?
                  AND (c.id=s.feedback_case_id OR EXISTS (
                    SELECT 1 FROM feedback_targets t WHERE t.feedback_signal_id=s.id
                      AND c.id=COALESCE(t.target_task_case_id,t.context_task_case_id)
                  ))
            )""")
            params.append(project_id)
        if older_than:
            conditions.append("""EXISTS (
                SELECT 1 FROM feedback_signal_revisions r WHERE r.id=s.current_machine_revision_id
                  AND COALESCE(r.observed_at,r.created_at)<?
            ) AND NOT EXISTS (
                SELECT 1 FROM feedback_signal_revisions recent
                WHERE recent.feedback_signal_id=s.id AND recent.created_at>=?
            ) AND NOT EXISTS (
                SELECT 1 FROM feedback_actions recent
                WHERE recent.feedback_signal_id=s.id AND recent.created_at>=?
            )""")
            params.extend((older_than, older_than, older_than))
        ids = [row["id"] for row in self.store.execute(
            f"SELECT s.id FROM feedback_signals s WHERE {' AND '.join(conditions)}", params
        ).fetchall()]
        evidence_count = 0
        with self.store.transaction():
            for signal_id in ids:
                self.store.execute(
                    """UPDATE feedback_signal_revisions SET redacted_excerpt=NULL,
                           locator_json='{}', metadata_json='{}',
                           suppression_reason='data-cleanup'
                       WHERE feedback_signal_id=?""",
                    (signal_id,),
                )
                self.store.execute(
                    "UPDATE feedback_targets SET evidence_json='{}' WHERE feedback_signal_id=?", (signal_id,)
                )
                self.store.execute(
                    "UPDATE feedback_semantic_reviews SET review_json='{}' WHERE feedback_signal_id=?",
                    (signal_id,),
                )
                self.store.execute(
                    """UPDATE feedback_actions SET note=NULL
                       WHERE feedback_signal_id=? AND note IS NOT NULL""",
                    (signal_id,),
                )
                evidence = self.store.execute(
                    """UPDATE evidence_items SET excerpt=NULL, locator_json='{}', validity='purged'
                       WHERE id IN (
                         SELECT rt.trigger_evidence_id FROM review_tasks rt
                           WHERE rt.feedback_signal_id=?
                         UNION SELECT e.id FROM evidence_items e JOIN feedback_signals s
                           ON s.feedback_event_id=e.event_id
                           WHERE s.id=? AND e.evidence_type='session-negative-feedback'
                         UNION SELECT json_extract(a.binding_json, '$.evidenceId')
                           FROM feedback_actions a WHERE a.feedback_signal_id=?
                             AND json_extract(a.binding_json, '$.evidenceId') IS NOT NULL
                         UNION SELECT json_extract(a.binding_json, '$.verificationReferences.evidenceId')
                           FROM feedback_actions a WHERE a.feedback_signal_id=?
                             AND json_extract(a.binding_json, '$.verificationReferences.evidenceId') IS NOT NULL
                         UNION SELECT json_extract(a.binding_json, '$.verificationReferences.verificationEvidenceId')
                           FROM feedback_actions a WHERE a.feedback_signal_id=?
                             AND json_extract(a.binding_json, '$.verificationReferences.verificationEvidenceId') IS NOT NULL
                         UNION SELECT json_extract(a.binding_json, '$.verificationReferences.externalAcceptanceId')
                           FROM feedback_actions a WHERE a.feedback_signal_id=?
                             AND json_extract(a.binding_json, '$.verificationReferences.externalAcceptanceId') IS NOT NULL
                       )""",
                    (signal_id, signal_id, signal_id, signal_id, signal_id, signal_id),
                )
                evidence_count += evidence.rowcount
                self.store.execute("DELETE FROM feedback_cluster_members WHERE feedback_signal_id=?", (signal_id,))
            self.store.execute(
                """UPDATE feedback_clusters SET member_count=(SELECT COUNT(*) FROM feedback_cluster_members m WHERE m.cluster_id=feedback_clusters.id),
                       open_count=(SELECT COUNT(*) FROM feedback_cluster_members m JOIN feedback_signals s ON s.id=m.feedback_signal_id WHERE m.cluster_id=feedback_clusters.id AND s.current_resolution_state NOT IN ('resolved-verified','resolved-unverified','not-actionable','false-positive','duplicate')),
                       updated_at=?""", (_now(),),
            )
        return {"signals": len(ids), "evidenceItems": evidence_count, "actionAuditRetained": sum(
            self.store.execute("SELECT COUNT(*) FROM feedback_actions WHERE feedback_signal_id=?", (item,)).fetchone()[0]
            for item in ids
        )}

    cleanup_derived_data = cleanup

    # ------------------------------------------------------ calibration/snapshot
    def build_calibration_profile(
        self, *, language: str = "all", category: str = "all",
        model_version: str = "deterministic", prompt_version: str = "deterministic",
        rubric_version: str = "deterministic",
    ) -> dict[str, Any]:
        conditions = ["a.producer_kind='reviewer'", "a.action IN ('confirm','exclude','retarget')"]
        conditions.append("""NOT EXISTS (
            SELECT 1 FROM feedback_actions later
            WHERE later.feedback_signal_id=a.feedback_signal_id
              AND later.producer_kind='reviewer'
              AND later.action IN ('confirm','exclude','retarget')
              AND later.revision>a.revision
        )""")
        conditions.append("json_extract(a.binding_json, '$.machineRevisionId')=r.id")
        if model_version == "deterministic":
            conditions.extend((
                "r.detector_id='session-negative-feedback'",
                "r.detector_version=?",
            ))
            params: list[Any] = [self.detector_version]
        else:
            conditions.extend((
                "sr.id=json_extract(r.metadata_json, '$.semanticReviewId')",
                "sr.model_version=?", "sr.prompt_version=?", "sr.rubric_version=?",
            ))
            params = [model_version, prompt_version, rubric_version]
        if category != "all":
            conditions.append("r.category=?")
            params.append(category)
        if language != "all":
            conditions.append("json_extract(r.metadata_json, '$.language')=?")
            params.append(language)
        rows = self.store.execute(
            f"""SELECT a.id, a.action, a.feedback_signal_id, r.category
                FROM feedback_actions a JOIN feedback_signals s ON s.id=a.feedback_signal_id
                JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
                LEFT JOIN feedback_semantic_reviews sr
                  ON sr.id=json_extract(r.metadata_json, '$.semanticReviewId')
                WHERE {' AND '.join(conditions)} ORDER BY a.id""", params,
        ).fetchall()
        samples = len(rows)
        positives = sum(row["action"] in {"confirm", "retarget"} for row in rows)
        target_correct = sum(row["action"] == "confirm" for row in rows if row["action"] in {"confirm", "retarget"})
        target_total = sum(row["action"] in {"confirm", "retarget"} for row in rows)
        precision_lb = self._wilson_lower(positives, samples)
        target_lb = self._wilson_lower(target_correct, target_total)
        major_count = max(
            (sum(row["category"] == item for row in rows) for item in {row["category"] for row in rows}),
            default=0,
        )
        eligible = (
            language != "all" and category != "all"
            and samples >= 200 and major_count >= 30
            and precision_lb >= 0.95 and target_lb >= 0.95
        )
        corpus = _digest(
            model_version, prompt_version, rubric_version,
            [(row["id"], row["action"]) for row in rows],
        )
        profile_id = _digest(
            "feedback-calibration", self.detector_version, self.resolver_version,
            language, category, model_version, prompt_version, rubric_version, corpus,
        )
        metrics = {"confirmed": positives, "excluded": samples - positives,
                   "retargeted": sum(row["action"] == "retarget" for row in rows)}
        with self.store.transaction():
            self.store.execute(
                """INSERT OR IGNORE INTO feedback_calibration_profiles(
                       id, detector_version, resolver_version, language, category,
                       model_version, prompt_version, rubric_version, corpus_sha256,
                       sample_count, major_category_sample_count,
                       precision_lower_bound, target_accuracy_lower_bound, eligible,
                       metrics_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, self.detector_version, self.resolver_version, language, category,
                 model_version, prompt_version, rubric_version, corpus,
                 samples, major_count, precision_lb, target_lb, int(eligible),
                 _json(metrics), _now()),
            )
        return _decoded(self.store.execute(
            "SELECT * FROM feedback_calibration_profiles WHERE id=?", (profile_id,)
        ).fetchone())

    calibration_profile = build_calibration_profile

    def build_calibration_profiles(
        self, *, model_version: str = "deterministic",
        prompt_version: str = "deterministic", rubric_version: str = "deterministic",
    ) -> dict[str, Any]:
        rows = self.store.execute(
            """SELECT DISTINCT json_extract(r.metadata_json, '$.language') AS language,
                              r.category
               FROM feedback_signal_revisions r JOIN feedback_signals s
                 ON s.current_machine_revision_id=r.id
               WHERE language IS NOT NULL ORDER BY language, r.category"""
        ).fetchall()
        return {
            "items": [self.build_calibration_profile(
                language=row["language"], category=row["category"],
                model_version=model_version, prompt_version=prompt_version,
                rubric_version=rubric_version,
            ) for row in rows]
        }

    @staticmethod
    def _wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
        if total <= 0:
            return 0.0
        proportion = successes / total
        denominator = 1 + z * z / total
        center = proportion + z * z / (2 * total)
        margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
        return max(0.0, (center - margin) / denominator)

    @staticmethod
    def _language(text: str) -> str:
        has_cjk = any("\u4e00" <= char <= "\u9fff" for char in text)
        has_latin = any(("a" <= char.lower() <= "z") for char in text)
        if has_cjk and has_latin:
            return "mixed"
        if has_cjk:
            return "zh"
        if has_latin:
            return "en"
        return "unknown"

    def feedback_snapshot_candidates(
        self, *, coverage_status: str = "complete",
        calibration_profile_id: str | None = None, cutoff_at: str | None = None,
        scan_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = cutoff_at or _now()
        profile = None
        if calibration_profile_id:
            profile = self.store.execute(
                "SELECT * FROM feedback_calibration_profiles WHERE id=?", (calibration_profile_id,)
            ).fetchone()
            if profile is None:
                raise KeyError(calibration_profile_id)
        rows = self.store.execute(
            """SELECT s.*, r.revision AS machine_revision, r.category, r.channel,
                      r.confidence, r.detector_id, r.detector_version,
                      r.span_parser_version,
                      r.resolver_version AS revision_resolver_version, r.metadata_json,
                      r.orphaned, r.is_current,
                      t.target_kind, t.target_task_case_id, t.context_task_case_id,
                      t.resolver_version, t.machine_status AS target_status
               FROM feedback_signals s JOIN feedback_signal_revisions r ON r.id=s.current_machine_revision_id
               LEFT JOIN feedback_targets t ON t.id=s.current_confirmed_target_id
               WHERE COALESCE(r.observed_at,r.created_at)<=? AND r.created_at<=?
                 AND r.channel<>'trial-experience'
                 AND (? IS NULL OR EXISTS (
                   SELECT 1 FROM event_provenance provenance
                   JOIN log_file_generations generation ON generation.id=provenance.generation_id
                   WHERE provenance.event_id=s.feedback_event_id AND generation.scan_run_id=?
                 ))
               ORDER BY s.id""", (cutoff, cutoff, scan_run_id, scan_run_id),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            exclusion = None
            revision_metadata = _mapping(row["metadata_json"])
            language = revision_metadata.get("language") or "unknown"
            semantic_review = None
            if revision_metadata.get("semanticReviewId"):
                semantic_review = self.store.execute(
                    "SELECT * FROM feedback_semantic_reviews WHERE id=?",
                    (revision_metadata["semanticReviewId"],),
                ).fetchone()
            expected_model_tuple = (
                (semantic_review["model_version"], semantic_review["prompt_version"], semantic_review["rubric_version"])
                if semantic_review is not None else ("deterministic", "deterministic", "deterministic")
            )
            candidate_profile = profile
            if semantic_review is not None:
                candidate_profile = self.store.execute(
                    "SELECT * FROM feedback_calibration_profiles WHERE id=?",
                    (semantic_review["calibration_profile_id"],),
                ).fetchone() if semantic_review["calibration_profile_id"] else None
            elif candidate_profile is None:
                candidate_profile = self.store.execute(
                    """SELECT * FROM feedback_calibration_profiles
                       WHERE detector_version=? AND resolver_version=? AND language=?
                         AND category=? AND model_version='deterministic'
                         AND prompt_version='deterministic' AND rubric_version='deterministic'
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (self.detector_version, self.resolver_version, language, row["category"]),
                ).fetchone()
            profile_matches = bool(
                candidate_profile is not None and candidate_profile["eligible"]
                and candidate_profile["detector_version"] == self.detector_version
                and candidate_profile["resolver_version"] == self.resolver_version
                and candidate_profile["language"] == language
                and candidate_profile["category"] == row["category"]
                and (candidate_profile["model_version"], candidate_profile["prompt_version"], candidate_profile["rubric_version"])
                    == expected_model_tuple
                and (
                    semantic_review is None
                    or candidate_profile["id"] == semantic_review["calibration_profile_id"]
                )
            )
            actual_calibration_profile_id = (
                semantic_review["calibration_profile_id"]
                if semantic_review is not None else (
                    candidate_profile["id"] if profile_matches else None
                )
            )
            latest_action = self.store.execute(
                """SELECT * FROM feedback_actions WHERE feedback_signal_id=? AND created_at<=?
                   ORDER BY revision DESC LIMIT 1""", (row["id"], cutoff),
            ).fetchone()
            confirmation = self.store.execute(
                """SELECT * FROM feedback_actions WHERE feedback_signal_id=?
                   AND producer_kind='reviewer' AND action IN ('confirm','retarget')
                   AND created_at<=? ORDER BY revision DESC LIMIT 1""", (row["id"], cutoff),
            ).fetchone()
            target = None
            if confirmation is not None and confirmation["target_id"]:
                target = self.store.execute(
                    "SELECT * FROM feedback_targets WHERE id=?", (confirmation["target_id"],)
                ).fetchone()
            elif profile_matches:
                machine_targets = self.store.execute(
                    """SELECT * FROM feedback_targets WHERE feedback_signal_id=?
                       AND signal_revision_id=? AND machine_status='candidate' ORDER BY rank""",
                    (row["id"], row["current_machine_revision_id"]),
                ).fetchall()
                if len(machine_targets) == 1:
                    target = machine_targets[0]
            confirmed = target is not None
            confirmation_binding = _mapping(confirmation["binding_json"]) if confirmation else {}
            reviewer_confirmation_current = bool(
                confirmation and confirmation_binding.get("machineRevisionId") == row["current_machine_revision_id"]
            )
            resolution_state = latest_action["to_resolution_state"] if latest_action else "unreviewed"
            target_id = target["id"] if target else None
            target_status = target["machine_status"] if target else None
            resolver_version = target["resolver_version"] if target else None
            if coverage_status != "complete":
                exclusion = "coverage-incomplete"
            elif not row["is_current"] or row["orphaned"]:
                exclusion = "machine-revision-ineligible"
            elif row["span_parser_version"] != SPAN_PARSER_VERSION:
                exclusion = "span-parser-stale"
            elif row["revision_resolver_version"] != self.resolver_version:
                exclusion = "target-resolver-stale"
            elif resolution_state in {"false-positive", "duplicate", "not-actionable"}:
                exclusion = resolution_state
            elif not confirmed:
                exclusion = "target-unconfirmed"
            elif confirmation is not None and not reviewer_confirmation_current and not profile_matches:
                exclusion = "human-review-revision-stale"
            elif target_status != "candidate":
                exclusion = "target-not-current"
            elif resolver_version != self.resolver_version:
                exclusion = "target-resolver-stale"
            elif confirmation is None and not profile_matches:
                exclusion = "calibration-ineligible"
            case_id = (
                target["target_task_case_id"] or target["context_task_case_id"]
            ) if target else row["feedback_case_id"]
            case = self.store.execute("SELECT current_revision, invalidated_at FROM task_cases WHERE id=?", (case_id,)).fetchone() if case_id else None
            if exclusion is None and (case is None or case["invalidated_at"] is not None):
                exclusion = "target-case-invalid"
            action_revision = latest_action["revision"] if latest_action else None
            frozen = {
                "signalId": row["id"], "machineRevision": row["machine_revision"],
                "machineRevisionId": row["current_machine_revision_id"],
                "actionRevision": action_revision, "targetId": target_id,
                "detectorVersion": row["detector_version"],
                "spanParserVersion": row["span_parser_version"],
                "resolverVersion": row["revision_resolver_version"],
                "targetResolverVersion": resolver_version,
                "calibrationProfileId": actual_calibration_profile_id,
                "semanticVersionTuple": ({
                    "modelVersion": semantic_review["model_version"],
                    "promptVersion": semantic_review["prompt_version"],
                    "rubricVersion": semantic_review["rubric_version"],
                } if semantic_review is not None else None),
                "resolutionState": resolution_state, "category": row["category"],
                "channel": row["channel"], "confidence": row["confidence"],
            }
            candidates.append({
                "feedback_signal_id": row["id"], "signal_machine_revision": row["machine_revision"],
                "target_id": target_id, "target_status": target_status,
                "target_resolver_version": resolver_version, "action_revision": action_revision,
                "resolution_state": resolution_state, "target_task_case_id": case_id,
                "task_case_revision": case["current_revision"] if case else None,
                "calibration_profile_id": actual_calibration_profile_id,
                "metric_eligible": int(exclusion is None), "exclusion_reason": exclusion,
                "frozen": frozen,
                "attributions": self._snapshot_attributions(
                    row["id"], case_id, exclusion, target_id=target_id,
                ),
            })
        return candidates

    metric_snapshot_candidates = feedback_snapshot_candidates

    def create_feedback_snapshot(
        self, *, coverage_status: str | None = None,
        calibration_profile_id: str | None = None, cutoff_at: str | None = None,
        scan_run_id: str | None = None, snapshot_id: str | None = None,
        expected_scope_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        server_cutoff = _now()
        if cutoff_at is not None:
            requested = datetime.fromisoformat(str(cutoff_at).replace("Z", "+00:00"))
            if requested.tzinfo is None:
                raise ValueError("feedback metric cutoff must include a timezone")
            current = datetime.fromisoformat(server_cutoff.replace("Z", "+00:00"))
            if abs((requested.astimezone(timezone.utc) - current).total_seconds()) > 5:
                raise ValueError("feedback metrics can only be sealed at the current server time")
        with self.store.transaction():
            scope_fingerprint = expected_scope_fingerprint or self.formal_scope_fingerprint
            if not scope_fingerprint:
                raise ValueError("formal feedback metrics require the current scope fingerprint")
            state = self._derivation_state()
            if (
                state is None or state["detector_version"] != self.detector_version
                or _mapping(state["stats_json"]).get("resolverVersion") != self.resolver_version
                or not state["bootstrap_complete"] or state["status"] != "ready"
            ):
                raise EffectStoreError("feedback derivation must be complete before sealing metrics")
            latest_change = self.store.execute(
                "SELECT COALESCE(MAX(id),0) FROM effect_derivation_changes"
            ).fetchone()[0]
            if int(state["change_cursor"]) < int(latest_change):
                raise EffectStoreError("feedback derivation changes are pending")
            return self._create_feedback_snapshot(
                coverage_status=coverage_status,
                calibration_profile_id=calibration_profile_id,
                cutoff_at=server_cutoff, scan_run_id=scan_run_id, snapshot_id=snapshot_id,
                expected_scope_fingerprint=scope_fingerprint,
            )

    def _create_feedback_snapshot(
        self, *, coverage_status: str | None = None,
        calibration_profile_id: str | None = None, cutoff_at: str | None = None,
        scan_run_id: str | None = None, snapshot_id: str | None = None,
        expected_scope_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        cutoff = cutoff_at or _now()
        latest_scan = self.store.execute(
            """SELECT * FROM scan_runs WHERE status!='running' AND finished_at<=?
               ORDER BY finished_at DESC, id DESC LIMIT 1""", (cutoff,),
        ).fetchone()
        if scan_run_id is None:
            scan = latest_scan
            scan_run_id = scan["id"] if scan else None
            inferred_coverage = scan["coverage_status"] if scan else "unknown"
        else:
            scan = self.store.execute("SELECT * FROM scan_runs WHERE id=?", (scan_run_id,)).fetchone()
            if scan is None:
                raise KeyError(scan_run_id)
            if latest_scan is None or latest_scan["id"] != scan_run_id:
                raise EffectStoreError("formal feedback metrics require the latest completed scan")
            scan_metadata = _mapping(scan["metadata_json"])
            if scan_metadata.get("scopeKind") != "configured-catalog" or not scan["finished_at"] or scan["finished_at"] > cutoff:
                raise ValueError("formal feedback metrics require a completed configured scan before cutoff")
            inferred_coverage = (
                scan["coverage_status"]
                if scan_metadata.get("scopeKind") == "configured-catalog" else "unknown"
            )
        scan_metadata = _mapping(scan["metadata_json"]) if scan is not None else {}
        if scan_metadata.get("scopeKind") != "configured-catalog":
            raise EffectStoreError("formal feedback metrics require the latest configured scan")
        if scan_metadata.get("scopeFingerprint") != expected_scope_fingerprint:
            raise EffectStoreError("configured feedback scan scope is stale")
        coverage = inferred_coverage
        if inferred_coverage == "complete" and coverage_status not in {None, "complete"}:
            coverage = str(coverage_status)
        candidates = self.feedback_snapshot_candidates(
            coverage_status=coverage, calibration_profile_id=calibration_profile_id,
            cutoff_at=cutoff, scan_run_id=scan_run_id,
        )
        snapshot_id = snapshot_id or _digest("feedback-snapshot", cutoff, scan_run_id, calibration_profile_id, _now())
        versions = {
            "detectorVersion": self.detector_version,
            "spanParserVersions": sorted({
                item["frozen"]["spanParserVersion"] for item in candidates
            }),
            "currentSpanParserVersion": SPAN_PARSER_VERSION,
            "resolverVersions": sorted({
                item["frozen"]["resolverVersion"] for item in candidates
            }),
            "targetResolverVersions": sorted({
                item["frozen"]["targetResolverVersion"] for item in candidates
                if item["frozen"]["targetResolverVersion"]
            }),
            "currentResolverVersion": self.resolver_version,
            "requestedCalibrationProfileId": calibration_profile_id,
            "calibrationProfileIds": sorted({
                item["calibration_profile_id"] for item in candidates
                if item.get("calibration_profile_id")
            }),
            "semanticVersionTuples": sorted({
                _json(item["frozen"]["semanticVersionTuple"])
                for item in candidates if item["frozen"].get("semanticVersionTuple")
            }),
        }
        dimensions = {
            "metricKind": "session-negative-feedback", "cutoffAt": cutoff,
            "scanScopeFingerprint": expected_scope_fingerprint,
        }
        summary = {"signals": len(candidates), "eligible": sum(item["metric_eligible"] for item in candidates)}
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO metric_snapshots(
                       id, scan_run_id, cutoff_at, coverage_status, dimensions_json,
                       versions_json, summary_json, sealed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (snapshot_id, scan_run_id, cutoff, coverage, _json(dimensions), _json(versions),
                 _json(summary), _now()),
            )
            for item in candidates:
                self.store.execute(
                    """INSERT INTO feedback_metric_snapshot_items(
                           snapshot_id, feedback_signal_id, signal_machine_revision,
                           target_id, target_status, target_resolver_version, action_revision,
                           resolution_state, target_task_case_id, task_case_revision,
                           calibration_profile_id, metric_eligible, exclusion_reason, frozen_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, item["feedback_signal_id"], item["signal_machine_revision"],
                     item["target_id"], item["target_status"], item["target_resolver_version"],
                     item["action_revision"], item["resolution_state"], item["target_task_case_id"],
                     item["task_case_revision"], item["calibration_profile_id"],
                     item["metric_eligible"], item["exclusion_reason"], _json(item["frozen"])),
                )
                for attribution in item["attributions"]:
                    self.store.execute(
                        """INSERT INTO feedback_metric_snapshot_attributions(
                               snapshot_id, feedback_signal_id, skill_invocation_id, skill_id,
                               skill_sha256, attribution_kind, metric_eligible,
                               exclusion_reason, frozen_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (snapshot_id, item["feedback_signal_id"], attribution["skill_invocation_id"],
                         attribution["skill_id"], attribution["skill_sha256"],
                         attribution["attribution_kind"], attribution["metric_eligible"],
                         attribution["exclusion_reason"], _json(attribution["frozen"])),
                    )
            self.store.execute("UPDATE metric_snapshots SET sealed=1 WHERE id=?", (snapshot_id,))
        return self.get_feedback_snapshot(snapshot_id)

    create_metric_snapshot = create_feedback_snapshot

    def get_feedback_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM metric_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = _decoded(row)
        result["items"] = [_decoded(item) for item in self.store.execute(
            "SELECT * FROM feedback_metric_snapshot_items WHERE snapshot_id=? ORDER BY feedback_signal_id",
            (snapshot_id,),
        ).fetchall()]
        result["attributions"] = [_decoded(item) for item in self.store.execute(
            """SELECT * FROM feedback_metric_snapshot_attributions WHERE snapshot_id=?
               ORDER BY feedback_signal_id, skill_invocation_id""", (snapshot_id,),
        ).fetchall()]
        return result

    get_metric_snapshot = get_feedback_snapshot

    def _snapshot_attributions(
        self, signal_id: str, case_id: str | None, item_exclusion: str | None,
        *, target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        target = self.store.execute(
            """SELECT * FROM feedback_targets WHERE id=COALESCE(?,
                   (SELECT current_confirmed_target_id FROM feedback_signals WHERE id=?))""",
            (target_id, signal_id),
        ).fetchone()
        rows: list[Any] = []
        if target and target["skill_invocation_id"]:
            rows = self.store.execute(
                """SELECT i.id AS skill_invocation_id, i.skill_id, i.skill_sha256,
                          i.validity, i.load_status,
                          CASE WHEN MAX(CASE WHEN l.attribution_kind='shared' THEN 1 ELSE 0 END)=1
                               THEN 'shared' ELSE 'direct' END AS attribution_kind
                   FROM skill_invocations i JOIN attribution_links l
                     ON l.skill_invocation_id=i.id AND l.task_case_id=? AND l.status='active'
                    AND l.attribution_kind IN ('direct','shared')
                   WHERE i.id=? GROUP BY i.id, i.skill_id, i.skill_sha256, i.validity, i.load_status""",
                (case_id, target["skill_invocation_id"]),
            ).fetchall()
        elif case_id:
            rows = self.store.execute(
                """SELECT i.id AS skill_invocation_id, i.skill_id, i.skill_sha256,
                          i.validity, i.load_status,
                          CASE WHEN MAX(CASE WHEN l.attribution_kind='shared' THEN 1 ELSE 0 END)=1
                               THEN 'shared' ELSE 'direct' END AS attribution_kind
                   FROM attribution_links l JOIN skill_invocations i ON i.id=l.skill_invocation_id
                   WHERE l.task_case_id=? AND l.status='active' AND l.attribution_kind IN ('direct','shared')
                   GROUP BY i.id, i.skill_id, i.skill_sha256, i.validity, i.load_status
                   ORDER BY i.id""", (case_id,),
            ).fetchall()
        result = []
        for row in rows:
            exclusion = item_exclusion
            if exclusion is None and row["validity"] != "valid":
                exclusion = "invocation-invalid"
            elif exclusion is None and row["load_status"] != "loaded":
                exclusion = "invocation-not-loaded"
            elif exclusion is None and not row["skill_sha256"]:
                exclusion = "skill-version-unknown"
            elif exclusion is None and row["attribution_kind"] not in {"direct", "shared"}:
                exclusion = "attribution-ineligible"
            result.append({
                "skill_invocation_id": row["skill_invocation_id"], "skill_id": row["skill_id"],
                "skill_sha256": row["skill_sha256"], "attribution_kind": row["attribution_kind"],
                "metric_eligible": int(exclusion is None), "exclusion_reason": exclusion,
                "frozen": {"skillInvocationId": row["skill_invocation_id"],
                           "skillId": row["skill_id"], "skillSha256": row["skill_sha256"],
                           "attributionKind": row["attribution_kind"],
                           "validity": row["validity"], "loadStatus": row["load_status"]},
            })
        return result

    # --------------------------------------------------------------- helpers
    def _event_parts(self, event_row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        event = dict(event_row)
        payload = _mapping(event.get("payload")) or _mapping(event.get("payload_json"))
        metadata = _mapping(event.get("metadata")) or _mapping(payload.get("metadata"))
        if "payload" not in event:
            event["payload"] = payload
        return event, payload, metadata

    @staticmethod
    def _context_has_target(context: Mapping[str, Any]) -> bool:
        names = {
            "targets", "target_candidates", "targetCandidates", "skill_invocation_id",
            "skillInvocationId", "tool_result_id", "toolResultId", "tool_call_id",
            "toolCallId", "target_event_id", "targetEventId", "previous_assistant_event_id",
            "previousAssistantEventId", "previous_task_case_id", "previousTaskCaseId",
        }
        return any(context.get(name) for name in names)

    @staticmethod
    def _targets_ambiguous(targets: Sequence[Mapping[str, Any]]) -> bool:
        if len(targets) < 2:
            return False
        ordered = sorted((float(item.get("confidence") or 0.0) for item in targets), reverse=True)
        return ordered[0] - ordered[1] <= 0.05

    @staticmethod
    def _target_identity(target: Mapping[str, Any]) -> tuple[str, str]:
        target = dict(target)
        identifier = next((str(target.get(key)) for key in (
            "target_task_case_id", "target_event_id", "skill_invocation_id",
            "tool_call_id", "tool_result_id",
        ) if target.get(key)), "")
        return str(target.get("target_kind") or ""), identifier

    def _confirmed_identity_present(
        self, confirmed_id: str, targets: Sequence[Mapping[str, Any]],
    ) -> bool:
        confirmed = self.store.execute("SELECT * FROM feedback_targets WHERE id=?", (confirmed_id,)).fetchone()
        return confirmed is not None and self._target_identity(confirmed) in {
            self._target_identity(item) for item in targets
        }

    def _require_reviewer(self, actor_id: str) -> Any:
        actor = self.store.execute("SELECT * FROM actors WHERE id=?", (actor_id,)).fetchone()
        roles = set(json.loads(actor["roles_json"])) if actor else set()
        if actor is None or not actor["active"] or "reviewer" not in roles:
            raise EffectStoreError("feedback actions require an active reviewer")
        return actor

    def _signal_from_signal_or_review(self, identifier: str) -> Any:
        row = self.store.execute("SELECT * FROM feedback_signals WHERE id=?", (identifier,)).fetchone()
        if row is None:
            row = self.store.execute(
                """SELECT s.* FROM feedback_signals s JOIN review_tasks r
                   ON r.feedback_signal_id=s.id WHERE r.id=?""", (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return row

    def _review_for_signal(self, signal_id: str) -> Any:
        return self.store.execute(
            "SELECT * FROM review_tasks WHERE feedback_signal_id=? ORDER BY created_at DESC LIMIT 1",
            (signal_id,),
        ).fetchone()

    def _target_case_for_signal(self, signal_id: str) -> str | None:
        row = self.store.execute(
            """SELECT COALESCE(t.target_task_case_id,t.context_task_case_id,s.feedback_case_id)
               FROM feedback_signals s LEFT JOIN feedback_targets t
                 ON t.id=s.current_confirmed_target_id WHERE s.id=?""", (signal_id,),
        ).fetchone()
        return row[0] if row else None

    def _session_ids_for_case(self, task_case_id: str) -> tuple[str, ...]:
        return tuple(row["session_id"] for row in self.store.execute(
            """SELECT DISTINCT ep.session_id FROM task_case_episodes ce
               JOIN task_episodes ep ON ep.id=ce.task_episode_id
               WHERE ce.task_case_id=? AND ep.invalidated_at IS NULL""", (task_case_id,),
        ).fetchall())

    def _case_for_tool_call(self, tool_call_id: str) -> str | None:
        row = self.store.execute(
            """SELECT ce.task_case_id FROM tool_calls c JOIN task_case_episodes ce
               ON ce.task_episode_id=c.task_episode_id WHERE c.id=?
               ORDER BY ce.relationship='primary' DESC LIMIT 1""", (tool_call_id,),
        ).fetchone()
        return row[0] if row else None

    def _tool_result_for_event(self, event_id: str) -> str | None:
        row = self.store.execute("SELECT id FROM tool_results WHERE event_id=?", (event_id,)).fetchone()
        return row[0] if row else None

    def _case_for_event(self, event_id: str) -> str | None:
        row = self.store.execute(
            """SELECT task_case_id FROM (
                 SELECT ce.task_case_id, ce.relationship FROM task_episodes ep
                   JOIN task_case_episodes ce ON ce.task_episode_id=ep.id WHERE ep.start_event_id=?
                 UNION ALL
                 SELECT ce.task_case_id, ce.relationship FROM task_episodes ep
                   JOIN task_case_episodes ce ON ce.task_episode_id=ep.id WHERE ep.end_event_id=?
                 UNION ALL
                 SELECT ce.task_case_id, ce.relationship FROM tool_calls c
                   JOIN task_case_episodes ce ON ce.task_episode_id=c.task_episode_id WHERE c.event_id=?
                 UNION ALL
                 SELECT ce.task_case_id, ce.relationship FROM tool_results tr
                   JOIN tool_calls c ON c.id=tr.tool_call_id
                   JOIN task_case_episodes ce ON ce.task_episode_id=c.task_episode_id WHERE tr.event_id=?
                 UNION ALL
                 SELECT ce.task_case_id, ce.relationship FROM skill_invocations i
                   JOIN task_case_episodes ce ON ce.task_episode_id=i.task_episode_id WHERE i.event_id=?
               ) ORDER BY relationship='primary' DESC LIMIT 1""",
            (event_id, event_id, event_id, event_id, event_id),
        ).fetchone()
        return row[0] if row else None

    def _previous_context(self, event: Mapping[str, Any]) -> dict[str, Any]:
        candidates = self.store.execute(
            """SELECT id FROM canonical_events WHERE session_family=? AND event_type='assistant_message'
               AND id<>? AND (protocol_time<? OR (? IS NULL AND created_at<?))
               AND orphaned=0 ORDER BY COALESCE(protocol_time,created_at) DESC, id DESC LIMIT 100""",
            (event["session_family"], event["id"], event.get("protocol_time"),
             event.get("protocol_time"), event.get("created_at") or _now()),
        ).fetchall()
        own_case = self._case_for_event(event["id"])
        own_sessions = set(self._session_ids_for_case(own_case)) if own_case else set()
        previous = None
        for candidate in candidates:
            if not own_sessions:
                previous = candidate
                break
            candidate_case = self._case_for_event(candidate["id"])
            if candidate_case and own_sessions.intersection(self._session_ids_for_case(candidate_case)):
                previous = candidate
                break
        result: dict[str, Any] = {}
        if previous:
            result["previous_assistant_event_id"] = previous["id"]
            case = self._case_for_event(previous["id"])
            if case:
                result["previous_task_case_id"] = case
        elif own_sessions:
            placeholders = ",".join("?" for _ in own_sessions)
            parent = self.store.execute(
                f"""SELECT ep.end_event_id, ce.task_case_id, edge.edge_type
                     FROM session_edges edge JOIN task_episodes ep
                       ON ep.session_id=edge.parent_session_id
                     JOIN task_case_episodes ce ON ce.task_episode_id=ep.id
                     JOIN canonical_events event ON event.id=ep.end_event_id
                     WHERE edge.child_session_id IN ({placeholders})
                       AND event.event_type='assistant_message' AND event.orphaned=0
                     ORDER BY ep.updated_at DESC LIMIT 1""",
                tuple(own_sessions),
            ).fetchone()
            if parent:
                result.update({
                    "previous_assistant_event_id": parent["end_event_id"],
                    "previous_task_case_id": parent["task_case_id"],
                    "previous_relation": "structured-session-parent",
                    "previous_target_confidence": 0.88,
                })
        return result

    def _call_context_for_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        row = self.store.execute(
            """SELECT c.* FROM tool_results r JOIN tool_calls c ON c.id=r.tool_call_id
               WHERE r.event_id=? LIMIT 1""", (event["id"],),
        ).fetchone()
        if row is None:
            return {}
        return {"stored_tool_call_id": row["id"], "tool_name": row["tool_name"],
                "args": _mapping(row["arguments_json"])}

    def _derivation_state(self) -> Any:
        return self.store.execute(
            "SELECT * FROM feedback_derivation_state WHERE detector_id=?", (self.detector_id,)
        ).fetchone()


__all__ = ["FeedbackService", "RESOLVER_VERSION", "RevisionConflict"]