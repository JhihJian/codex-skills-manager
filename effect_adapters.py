from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from auth import REDACTION_FAILED, redact_sensitive
from feedback_detector import DETECTOR_VERSION as FEEDBACK_DETECTOR_VERSION
from feedback_detector import detect_assistant_claims, detect_user_feedback


ADAPTER_VERSION = "effect-adapters-v1"
DEFAULT_TEXT_LIMIT = 4096
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:authorization|cookie|password|passwd|secret|token|api[-_]?key|private[-_]?key|access[-_]?key)"
)

_SKILL_BLOCK = re.compile(
    r'<skill\s+name="(?P<name>[^"]+)"\s+location="(?P<location>[^"]+)"[^>]*>',
    re.IGNORECASE,
)
_PARALLEL_TOOLS = {"parallel", "multi_tool_use", "multi_tool_use.parallel"}


@dataclass(frozen=True)
class NormalizedEvent:
    source: str
    event_type: str
    fingerprint: str
    timestamp: str = ""
    event_id: str = ""
    session_id: str = ""
    session_family: str = ""
    parent_id: str = ""
    parent_session_id: str = ""
    fork_from_id: str = ""
    role: str = ""
    text: str = ""
    call_id: str = ""
    tool_name: str = ""
    args: Any = None
    result: Any = None
    outcome: str = ""
    is_error: bool = False
    is_cancelled: bool = False
    is_blocked: bool = False
    payload_hash: str = ""
    payload: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.update(error=self.is_error, cancelled=self.is_cancelled, blocked=self.is_blocked)
        return value

    def __getitem__(self, key: str) -> Any:
        aliases = {"error": "is_error", "cancelled": "is_cancelled", "blocked": "is_blocked", "type": "event_type"}
        key = aliases.get(key, key)
        return getattr(self, key)

    @property
    def error(self) -> bool:
        return self.is_error

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled

    @property
    def blocked(self) -> bool:
        return self.is_blocked


@dataclass(frozen=True)
class TaskEpisode:
    episode_id: str
    source: str
    session_id: str
    session_family: str
    start_fingerprint: str
    end_fingerprint: str
    event_fingerprints: tuple[str, ...]
    user_text: str = ""
    started_at: str = ""
    ended_at: str = ""
    continuation_of: str = ""
    outcome: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskFact:
    predicate: str
    value: str
    evidence_fingerprint: str
    source_kind: str = "deterministic-parser"
    producer_version: str = ADAPTER_VERSION
    confidence: float = 1.0
    status: str = "accepted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SessionEdge:
    source_session_id: str
    target_session_id: str
    relation: str
    evidence_fingerprint: str
    session_family: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_value(line: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(line, Mapping):
        return dict(line)
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("JSONL event must be an object")
    return value


def _redact_text(value: str, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    try:
        text = redact_sensitive(value)
    except Exception:
        text = REDACTION_FAILED
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def redact_value(value: Any, *, text_limit: int = DEFAULT_TEXT_LIMIT) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact_value(item, text_limit=text_limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, text_limit=text_limit) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                return redact_value(parsed, text_limit=text_limit)
        return _redact_text(value, text_limit)
    return value


def _feedback_candidates(content: Any, event_id: str) -> list[dict[str, Any]]:
    try:
        return [asdict(candidate) for candidate in detect_user_feedback(content, event_id=event_id)]
    except (MemoryError, RecursionError, RuntimeError, TypeError, ValueError):
        return []


def _assistant_claims(content: Any, event_id: str) -> list[dict[str, Any]]:
    try:
        return [asdict(candidate) for candidate in detect_assistant_claims(content, event_id=event_id)]
    except (MemoryError, RecursionError, RuntimeError, TypeError, ValueError):
        return []


def _process_details(value: Any, *, text_limit: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    selected: dict[str, Any] = {}
    for key in ("mode", "agentScope", "projectAgentsDir", "status", "exitCode", "resultId", "childSessionId"):
        if key in value:
            selected[key] = value[key]
    results: list[dict[str, Any]] = []
    if isinstance(value.get("results"), list):
        for result in value["results"]:
            if not isinstance(result, Mapping):
                continue
            item = {
                key: result[key] for key in (
                    "agent", "agentSource", "exitCode", "stderr", "resultId",
                    "childSessionId", "sessionId", "threadId", "status", "outcome",
                    "error", "completed", "resultReturned",
                ) if key in result
            }
            if isinstance(result.get("messages"), list):
                item["messageCount"] = len(result["messages"])
            if isinstance(result.get("usage"), Mapping):
                item["usage"] = {
                    key: result["usage"].get(key) for key in (
                        "turns", "input", "output", "contextTokens"
                    ) if key in result["usage"]
                }
            results.append(item)
    selected["results"] = results
    return redact_value(selected, text_limit=text_limit)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def event_fingerprint(
    *,
    source: str,
    session_family: str,
    event_type: str,
    event_id: str = "",
    timestamp: str = "",
    parent_id: str = "",
    call_id: str = "",
    payload: Any = None,
) -> str:
    if event_id:
        identity = ["protocol-id", source, session_family, event_type, event_id]
    else:
        identity = [
            "content",
            source,
            session_family,
            event_type,
            timestamp,
            parent_id,
            call_id,
            # Hash the pre-redaction value. Only this digest is retained, so
            # distinct secrets keep distinct identities without being stored.
            _hash(payload),
        ]
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def _text_content(content: Any, *, text_limit: int) -> str:
    if isinstance(content, str):
        return _redact_text(content, text_limit)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and str(block.get("type") or "").lower() in {
            "text",
            "input_text",
            "output_text",
        }:
            parts.append(str(block.get("text") or block.get("input_text") or block.get("output_text") or ""))
    return _redact_text("\n".join(part for part in parts if part), text_limit)


def _arguments(value: Any, *, text_limit: int) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return redact_value(value, text_limit=text_limit)


def _result_status(payload: Mapping[str, Any], result: Any) -> tuple[str, bool, bool, bool]:
    def flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"", "0", "false", "no", "off", "none", "null"}:
            return False
        return True

    status = str(payload.get("status") or payload.get("outcome") or "").strip().lower()
    result_status = result if isinstance(result, Mapping) else {}
    is_error = any(flag(container.get(key)) for container in (payload, result_status) for key in ("isError", "is_error", "error"))
    is_cancelled = any(
        flag(container.get(key)) for container in (payload, result_status) for key in ("cancelled", "canceled")
    ) or status in {
        "cancelled",
        "canceled",
        "timeout",
        "timed_out",
    }
    is_blocked = any(flag(container.get("blocked")) for container in (payload, result_status)) or status in {
        "blocked",
        "denied",
        "rejected",
    }
    if status in {"error", "failed", "failure"}:
        is_error = True
    if is_blocked:
        return "blocked", is_error, is_cancelled, True
    if is_cancelled:
        return "cancelled", is_error, True, False
    if is_error:
        return "error", True, False, False
    # A returned result, including a shell exit code of zero, is not proof that the task succeeded.
    return "returned", False, False, False


def _event(
    *,
    source: str,
    event_type: str,
    timestamp: str,
    event_id: str,
    session_id: str,
    session_family: str,
    parent_id: str = "",
    fingerprint_payload: Any = None,
    **values: Any,
) -> NormalizedEvent:
    call_id = str(values.get("call_id") or "")
    normalized_payload = redact_value(fingerprint_payload)
    fingerprint = event_fingerprint(
        source=source,
        session_family=session_family,
        event_type=event_type,
        event_id=event_id,
        timestamp=timestamp,
        parent_id=parent_id,
        call_id=call_id,
        payload=fingerprint_payload,
    )
    return NormalizedEvent(
        source=source,
        event_type=event_type,
        fingerprint=fingerprint,
        timestamp=timestamp,
        event_id=event_id,
        session_id=session_id,
        session_family=session_family,
        parent_id=parent_id,
        payload_hash=_hash(normalized_payload),
        payload=normalized_payload,
        **values,
    )


def _nested_tool_calls(
    *,
    source: str,
    args: Any,
    timestamp: str,
    session_id: str,
    session_family: str,
    wrapper_call_id: str,
    text_limit: int,
) -> list[NormalizedEvent]:
    if not isinstance(args, Mapping) or not isinstance(args.get("tool_uses"), list):
        return []
    events: list[NormalizedEvent] = []
    for nested in args["tool_uses"]:
        if not isinstance(nested, Mapping):
            continue
        name = str(nested.get("recipient_name") or nested.get("name") or "")
        parameters = nested.get("parameters", nested.get("arguments"))
        explicit_id = str(nested.get("id") or nested.get("call_id") or "")
        call_id = explicit_id or f"{wrapper_call_id}:nested:{_hash([name, redact_value(parameters)])[:16]}"
        event_id = explicit_id
        events.append(
            _event(
                source=source,
                event_type="tool_call",
                timestamp=timestamp,
                event_id=event_id,
                session_id=session_id,
                session_family=session_family,
                parent_id=wrapper_call_id,
                call_id=call_id,
                tool_name=name,
                args=_arguments(parameters, text_limit=text_limit),
                metadata={"parallel_wrapper": wrapper_call_id},
                fingerprint_payload={"name": name, "arguments": parameters},
            )
        )
    return events


def parse_codex_jsonl_line(
    line: str | bytes | Mapping[str, Any],
    *,
    session_id: str = "",
    session_family: str = "",
    text_limit: int = DEFAULT_TEXT_LIMIT,
) -> list[NormalizedEvent]:
    item = _json_value(line)
    raw_type = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    timestamp = str(item.get("timestamp") or payload.get("timestamp") or "")
    outer_id = str(item.get("id") or "")

    if raw_type == "session_meta":
        current = str(payload.get("id") or payload.get("session_id") or session_id)
        parent = str(payload.get("parent_thread_id") or "")
        family = session_family or parent or current
        source_info = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        fork_from = str(payload.get("forked_from") or payload.get("fork_from_id") or "")
        relation = "parent"
        if fork_from or "fork" in str(payload.get("thread_source") or "").lower():
            relation = "fork"
        return [
            _event(
                source="codex",
                event_type="session_meta",
                timestamp=timestamp,
                event_id=current,
                session_id=current,
                session_family=family,
                parent_session_id=parent,
                fork_from_id=fork_from,
                metadata={
                    "relation": relation,
                    "cwd": _redact_text(str(payload.get("cwd") or ""), text_limit),
                    "thread_source": str(payload.get("thread_source") or ""),
                    "source": redact_value(source_info, text_limit=text_limit),
                },
                fingerprint_payload=payload,
            )
        ]

    family = session_family or session_id
    if raw_type == "response_item":
        payload_type = str(payload.get("type") or "")
        payload_id = str(payload.get("id") or outer_id)
        if payload_type == "message":
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                return []
            event_type = f"{role}_message"
            return [
                _event(
                    source="codex",
                    event_type=event_type,
                    timestamp=timestamp,
                    event_id=payload_id,
                    session_id=session_id,
                    session_family=family,
                    role=role,
                    text=_text_content(payload.get("content"), text_limit=text_limit),
                    metadata={
                        "feedback_candidates": _feedback_candidates(payload.get("content"), payload_id)
                        if role == "user" else [],
                        "assistant_claims": _assistant_claims(payload.get("content"), payload_id)
                        if role == "assistant" else [],
                        "feedback_detector_version": FEEDBACK_DETECTOR_VERSION,
                        "protocol_type": payload_type,
                    },
                    fingerprint_payload=payload,
                )
            ]
        if payload_type in {"function_call", "tool_call", "custom_tool_call", "mcp_tool_call"}:
            name = str(payload.get("name") or payload.get("tool_name") or "")
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or payload_id)
            args = _arguments(payload.get("arguments", payload.get("input")), text_limit=text_limit)
            event = _event(
                source="codex",
                event_type="tool_call",
                timestamp=timestamp,
                event_id=payload_id,
                session_id=session_id,
                session_family=family,
                call_id=call_id,
                tool_name=name,
                args=args,
                metadata={"protocol_type": payload_type},
                fingerprint_payload=payload,
            )
            if name.lower() in _PARALLEL_TOOLS:
                return [event, *_nested_tool_calls(
                    source="codex",
                    args=args,
                    timestamp=timestamp,
                    session_id=session_id,
                    session_family=family,
                    wrapper_call_id=call_id,
                    text_limit=text_limit,
                )]
            return [event]
        if payload_type in {"function_call_output", "tool_call_output", "custom_tool_call_output", "mcp_tool_call_output"}:
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "")
            result = redact_value(payload.get("output", payload.get("result")), text_limit=text_limit)
            outcome, is_error, is_cancelled, is_blocked = _result_status(payload, result)
            return [
                _event(
                    source="codex",
                    event_type="tool_result",
                    timestamp=timestamp,
                    event_id=payload_id,
                    session_id=session_id,
                    session_family=family,
                    call_id=call_id,
                    tool_name=str(payload.get("name") or payload.get("tool_name") or ""),
                    result=result,
                    outcome=outcome,
                    is_error=is_error,
                    is_cancelled=is_cancelled,
                    is_blocked=is_blocked,
                    metadata={"protocol_type": payload_type},
                    fingerprint_payload=payload,
                )
            ]
        return []

    if raw_type == "event_msg":
        payload_type = str(payload.get("type") or "")
        role = {"user_message": "user", "agent_message": "assistant"}.get(payload_type, "")
        if not role:
            return []
        event_id = str(payload.get("id") or payload.get("event_id") or outer_id)
        text = str(payload.get("message") or payload.get("text") or "")
        return [
            _event(
                source="codex",
                event_type=f"{role}_message",
                timestamp=timestamp,
                event_id=event_id,
                session_id=session_id,
                session_family=family,
                role=role,
                text=_redact_text(text, text_limit),
                metadata={
                    "protocol_type": payload_type,
                    "feedback_candidates": _feedback_candidates(text, event_id) if role == "user" else [],
                    "assistant_claims": _assistant_claims(text, event_id) if role == "assistant" else [],
                    "feedback_detector_version": FEEDBACK_DETECTOR_VERSION,
                },
                fingerprint_payload=payload,
            )
        ]
    return []


def parse_pi_jsonl_line(
    line: str | bytes | Mapping[str, Any],
    *,
    session_id: str = "",
    session_family: str = "",
    text_limit: int = DEFAULT_TEXT_LIMIT,
) -> list[NormalizedEvent]:
    item = _json_value(line)
    raw_type = str(item.get("type") or "")
    timestamp = str(item.get("timestamp") or "")
    item_id = str(item.get("id") or "")
    parent_id = str(item.get("parentId") or item.get("parent_id") or "")

    if raw_type == "session":
        current = item_id or session_id
        parent_session = str(item.get("parentSession") or item.get("parent_session") or "")
        fork_from = str(item.get("forkedFrom") or item.get("forkFrom") or item.get("fork_from_id") or "")
        family = session_family or parent_session or current
        relation = "fork" if fork_from or item.get("fork") else "parent"
        return [
            _event(
                source="pi",
                event_type="session_meta",
                timestamp=timestamp,
                event_id=item_id,
                session_id=current,
                session_family=family,
                parent_id=parent_id,
                parent_session_id=parent_session,
                fork_from_id=fork_from,
                metadata={"relation": relation, "version": item.get("version")},
                fingerprint_payload=item,
            )
        ]

    family = session_family or session_id
    if raw_type == "session_info":
        return [
            _event(
                source="pi",
                event_type="session_info",
                timestamp=timestamp,
                event_id=item_id,
                session_id=session_id,
                session_family=family,
                parent_id=parent_id,
                text=_redact_text(str(item.get("name") or item.get("title") or ""), text_limit),
                metadata=redact_value(
                    {key: value for key, value in item.items() if key not in {"type", "id", "parentId", "timestamp"}},
                    text_limit=text_limit,
                ),
                fingerprint_payload=item,
            )
        ]
    if raw_type not in {"message", "custom_message"}:
        return []

    message = item.get("message") if isinstance(item.get("message"), Mapping) else item
    role = str(message.get("role") or "")
    content = message.get("content")
    if role in {"user", "assistant"}:
        text = _text_content(content, text_limit=text_limit)
        events = [
            _event(
                source="pi",
                event_type=f"{role}_message",
                timestamp=timestamp,
                event_id=item_id,
                session_id=session_id,
                session_family=family,
                parent_id=parent_id,
                role=role,
                text=text,
                metadata={
                    "feedback_candidates": _feedback_candidates(content, item_id)
                    if role == "user" else [],
                    "assistant_claims": _assistant_claims(content, item_id)
                    if role == "assistant" else [],
                    "feedback_detector_version": FEEDBACK_DETECTOR_VERSION,
                },
                fingerprint_payload={"role": role, "content": content},
            )
        ]
        if role == "user":
            for match in _SKILL_BLOCK.finditer(text):
                skill_name = match.group("name")
                location = match.group("location")
                skill_id = f"{item_id}:skill:{skill_name}" if item_id else ""
                events.append(
                    _event(
                        source="pi",
                        event_type="skill",
                        timestamp=timestamp,
                        event_id=skill_id,
                        session_id=session_id,
                        session_family=family,
                        parent_id=item_id or parent_id,
                        text=_redact_text(match.group(0), text_limit),
                        tool_name="skill",
                        args={"name": skill_name, "location": _redact_text(location, text_limit)},
                        metadata={"skill_name": skill_name, "location": _redact_text(location, text_limit)},
                        fingerprint_payload={"name": skill_name, "location": location},
                    )
                )
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "toolCall":
                    continue
                name = str(block.get("name") or block.get("toolName") or "")
                block_id = str(block.get("id") or "")
                call_id = str(block.get("callId") or block.get("call_id") or block_id)
                args = _arguments(block.get("arguments", block.get("input")), text_limit=text_limit)
                tool_event = _event(
                    source="pi",
                    event_type="tool_call",
                    timestamp=timestamp,
                    event_id=block_id,
                    session_id=session_id,
                    session_family=family,
                    parent_id=item_id or parent_id,
                    call_id=call_id,
                    tool_name=name,
                    args=args,
                    fingerprint_payload=block,
                )
                events.append(tool_event)
                if name.lower() in _PARALLEL_TOOLS:
                    events.extend(
                        _nested_tool_calls(
                            source="pi",
                            args=args,
                            timestamp=timestamp,
                            session_id=session_id,
                            session_family=family,
                            wrapper_call_id=call_id,
                            text_limit=text_limit,
                        )
                    )
        return events

    if role in {"toolResult", "tool_result", "function"}:
        call_id = str(message.get("toolCallId") or message.get("call_id") or item.get("toolCallId") or "")
        result = redact_value(message.get("content", message.get("result")), text_limit=text_limit)
        status_payload = {**item, **message}
        outcome, is_error, is_cancelled, is_blocked = _result_status(status_payload, result)
        return [
            _event(
                source="pi",
                event_type="tool_result",
                timestamp=timestamp,
                event_id=item_id,
                session_id=session_id,
                session_family=family,
                parent_id=parent_id,
                call_id=call_id,
                tool_name=str(message.get("toolName") or message.get("name") or ""),
                result=result,
                outcome=outcome,
                is_error=is_error,
                is_cancelled=is_cancelled,
                is_blocked=is_blocked,
                metadata={
                    "process_details": _process_details(message.get("details"), text_limit=text_limit),
                    "outer_status": redact_value(
                        {key: status_payload.get(key) for key in (
                            "isError", "is_error", "error", "blocked", "cancelled", "status"
                        ) if key in status_payload},
                        text_limit=text_limit,
                    ),
                },
                fingerprint_payload=message,
            )
        ]
    return []


def parse_jsonl_line(
    source: str,
    line: str | bytes | Mapping[str, Any],
    **context: Any,
) -> list[NormalizedEvent]:
    normalized_source = source.strip().lower()
    if normalized_source == "codex":
        return parse_codex_jsonl_line(line, **context)
    if normalized_source == "pi":
        return parse_pi_jsonl_line(line, **context)
    raise ValueError(f"unsupported session source: {source}")


parse_codex_event = parse_codex_jsonl_line
parse_pi_event = parse_pi_jsonl_line
normalize_event_line = parse_jsonl_line


def build_task_episodes(events: Iterable[NormalizedEvent]) -> list[TaskEpisode]:
    episodes: list[TaskEpisode] = []
    current: list[NormalizedEvent] = []
    current_user = ""

    def close() -> None:
        nonlocal current, current_user
        if not current:
            return
        first, last = current[0], current[-1]
        result_outcomes = {event.outcome for event in current if event.event_type == "tool_result"}
        if "blocked" in result_outcomes:
            outcome = "blocked"
        elif "cancelled" in result_outcomes:
            outcome = "cancelled"
        elif "error" in result_outcomes:
            outcome = "error"
        else:
            outcome = "unknown"
        episode_id = hashlib.sha256(
            _canonical([first.source, first.session_family, first.session_id, first.fingerprint]).encode("utf-8")
        ).hexdigest()
        episodes.append(
            TaskEpisode(
                episode_id=episode_id,
                source=first.source,
                session_id=first.session_id,
                session_family=first.session_family,
                start_fingerprint=first.fingerprint,
                end_fingerprint=last.fingerprint,
                event_fingerprints=tuple(event.fingerprint for event in current),
                user_text=current_user,
                started_at=first.timestamp,
                ended_at=last.timestamp,
                # Time adjacency is not a structural continuation edge.
                continuation_of="",
                outcome=outcome,
            )
        )
        current = []
        current_user = ""

    for event in events:
        starts_episode = event.event_type == "user_message" or (
            event.event_type == "skill" and not current
        )
        if starts_episode and current:
            close()
        if starts_episode:
            current = [event]
            current_user = event.text
        elif current:
            current.append(event)
    close()
    return episodes


_TOOL_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gradle", re.compile(r"(?i)(?:^|[._-])gradle(?:$|[._-])|gradlew")),
    ("git", re.compile(r"(?i)(?:^|[._-])git(?:$|[._-])")),
    ("browser", re.compile(r"(?i)playwright|browser|web_search|fetch")),
    ("filesystem", re.compile(r"(?i)(?:^|[._-])(read|write|edit|apply_patch)(?:$|[._-])")),
    ("shell", re.compile(r"(?i)(?:^|[._-])(bash|shell|exec_command)(?:$|[._-])")),
    ("subagent", re.compile(r"(?i)subagent|spawn_agent|send_input")),
)
_DOCUMENT_EXTENSIONS = {".md", ".mdx", ".doc", ".docx", ".pdf", ".rst", ".txt"}
_DEPLOY_PATTERN = re.compile(r"(?i)\b(deploy|deployment|docker|kubectl|helm|systemctl|vercel|netlify|cloudflare)\b|部署")
_GRADLE_PATTERN = re.compile(r"(?i)(?:\./|\.\\)?gradlew?\b|\bgradle\b")
_TEST_PATTERN = re.compile(r"(?i)\b(tests?|pytest|unittest|jest|vitest|gradle\w*test)\b|测试")
_PATH_EXTENSION = re.compile(r"(?i)(\.[a-z0-9]{1,8})(?:\b|$)")
_NEGATION_GAP = r"[^,，。;；!！?？:：\n]{0,24}"
_NEGATED_TEST = re.compile(rf"(?i)(?:do\s+not|don't|dont|no|不要|无需|别)\s*{_NEGATION_GAP}(?:test|测试)")
_NEGATED_DEPLOY = re.compile(rf"(?i)(?:do\s+not|don't|dont|no|不要|无需|别)\s*{_NEGATION_GAP}(?:deploy|部署)")
_NEGATED_GRADLE = re.compile(rf"(?i)(?:do\s+not|don't|dont|no|不要|无需|别)\s*{_NEGATION_GAP}(?:gradle|gradlew)")


def _searchable(event: NormalizedEvent) -> str:
    return "\n".join(
        part for part in (event.tool_name, event.text, _canonical(event.args) if event.args is not None else "") if part
    )


def extract_task_facts(events: Iterable[NormalizedEvent]) -> list[TaskFact]:
    facts: dict[tuple[str, str, str], TaskFact] = {}

    def add(predicate: str, value: str, event: NormalizedEvent) -> None:
        key = (predicate, value, event.fingerprint)
        facts[key] = TaskFact(predicate=predicate, value=value, evidence_fingerprint=event.fingerprint)

    for event in events:
        searchable = _searchable(event)
        user_text = event.text if event.event_type == "user_message" else ""
        negated_test = bool(user_text and _NEGATED_TEST.search(user_text))
        negated_deploy = bool(user_text and _NEGATED_DEPLOY.search(user_text))
        negated_gradle = bool(user_text and _NEGATED_GRADLE.search(user_text))
        if event.event_type == "tool_call":
            normalized_name = event.tool_name.lower()
            family = "other"
            for candidate, pattern in _TOOL_FAMILIES:
                if pattern.search(normalized_name):
                    family = candidate
                    break
            add("tool-family", family, event)
        if event.event_type not in {"user_message", "tool_call"}:
            continue
        if _GRADLE_PATTERN.search(searchable) and not negated_gradle:
            add("task-tag", "gradle", event)
        if _DEPLOY_PATTERN.search(searchable) and not negated_deploy:
            add("task-tag", "deploy", event)
        if _TEST_PATTERN.search(searchable) and not negated_test:
            add("task-tag", "test", event)
        extensions = {match.group(1).lower() for match in _PATH_EXTENSION.finditer(searchable)}
        if extensions & _DOCUMENT_EXTENSIONS:
            add("task-tag", "document", event)
        for extension in sorted(extensions):
            add("target-file-type", extension, event)
    return [facts[key] for key in sorted(facts)]


def build_session_edges(events: Iterable[NormalizedEvent]) -> list[SessionEdge]:
    edges: dict[tuple[str, str, str], SessionEdge] = {}
    for event in events:
        if event.event_type != "session_meta" or not event.session_id:
            continue
        target = event.fork_from_id or event.parent_session_id
        if not target or target == event.session_id:
            continue
        relation = "fork" if event.fork_from_id or event.metadata.get("relation") == "fork" else "parent"
        edge = SessionEdge(
            source_session_id=event.session_id,
            target_session_id=target,
            relation=relation,
            evidence_fingerprint=event.fingerprint,
            session_family=event.session_family,
        )
        edges[(edge.source_session_id, edge.target_session_id, edge.relation)] = edge
    return [edges[key] for key in sorted(edges)]


def attach_session_context(
    events: Sequence[NormalizedEvent], *, session_id: str, session_family: str
) -> list[NormalizedEvent]:
    """Return contextualized copies when a caller learns header data after parsing."""
    contextualized: list[NormalizedEvent] = []
    for event in events:
        new_session_id = event.session_id or session_id
        new_family = event.session_family or session_family
        contextualized.append(
            replace(
                event,
                session_id=new_session_id,
                session_family=new_family,
                fingerprint=event_fingerprint(
                    source=event.source,
                    session_family=new_family,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                    parent_id=event.parent_id,
                    call_id=event.call_id,
                    payload={
                        "role": event.role,
                        "text": event.text,
                        "tool_name": event.tool_name,
                        "args": event.args,
                        "result": event.result,
                    },
                ),
            )
        )
    return contextualized