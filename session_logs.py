from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INDEXED_MESSAGE_ROLES = {"user", "assistant"}
INDEXED_CONTENT_TYPES = {"", "text", "input_text", "output_text"}
LOW_VALUE_ITEM_TYPES = {
    "function_call",
    "function_call_output",
    "tool_call",
    "tool_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "mcp_call",
    "mcp_tool_call",
    "mcp_tool_call_output",
    "web_search_call",
    "file_search_call",
    "computer_call",
    "computer_call_output",
    "reasoning",
}
LOW_VALUE_TEXT_MARKERS = (
    "dom snapshot",
    "aria snapshot",
    "page snapshot",
    "browser automation",
    "### page state",
    "### ran playwright",
    "mcp__browser",
    "tool_call",
    "tool call",
    "tool output",
    "function_call",
    "function call",
    "function output",
    "multi_tool_use.parallel",
    "exec_command",
    "chunk id:",
    "wall time:",
    "process exited with code",
    "original token count",
)


@dataclass(frozen=True)
class SessionLogFile:
    source: str
    path: Path
    modified_at: float


def normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def parse_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    number = max(minimum, number)
    if maximum is not None:
        number = min(number, maximum)
    return number


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_session_index(session_index_file: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not session_index_file.exists():
        return index
    with session_index_file.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("id"):
                index[item["id"]] = item
    return index


def session_files(sessions_dir: Path, archived_sessions_dir: Path, limit: int = 400) -> list[Path]:
    return session_files_from_roots((sessions_dir, archived_sessions_dir), limit=limit)


def session_files_from_roots(roots: tuple[Path, ...], limit: int = 400) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend([p for p in root.rglob("*.jsonl") if p.is_file()])
    files.sort(key=safe_mtime, reverse=True)
    return files[:limit]


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def source_session_files(
    codex_sessions_dir: Path,
    codex_archived_sessions_dir: Path,
    pi_sessions_dir: Path,
    *,
    limit_per_source: int = 400,
) -> list[SessionLogFile]:
    selected = [
        SessionLogFile("codex", path, safe_mtime(path))
        for path in session_files(codex_sessions_dir, codex_archived_sessions_dir, limit=limit_per_source)
    ]
    selected.extend(
        SessionLogFile("pi", path, safe_mtime(path))
        for path in session_files_from_roots((pi_sessions_dir,), limit=limit_per_source)
    )
    selected.sort(key=lambda item: item.modified_at, reverse=True)
    return selected


def session_id_from_path(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})", path.name)
    return match.group(1) if match else path.stem


def pi_session_family(path: Path, parent_session: str = "", cache: dict[Path, str] | None = None) -> str:
    cache = cache if cache is not None else {}
    resolved = path.expanduser().resolve()
    if resolved in cache:
        return cache[resolved]
    cache[resolved] = str(resolved)
    parent = parent_session.strip()
    if not parent:
        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as f:
                header = json.loads(f.readline())
            if header.get("type") == "session":
                parent = str(header.get("parentSession") or "").strip()
        except (OSError, json.JSONDecodeError):
            return str(resolved)
    if not parent:
        return str(resolved)
    parent_path = Path(parent).expanduser()
    if not parent_path.is_absolute():
        parent_path = resolved.parent / parent_path
    family = pi_session_family(parent_path, cache=cache)
    cache[resolved] = family
    return family


def normalize_message_role(role: str) -> str:
    if role == "user_message":
        return "user"
    if role == "agent_message":
        return "assistant"
    if role in INDEXED_MESSAGE_ROLES:
        return role
    return ""


def context_role_label(role: str) -> str:
    return {"user": "用户", "assistant": "助手"}.get(role, role or "正文")


def normalize_context_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return text_from_content([content])
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type not in INDEXED_CONTENT_TYPES:
                    continue
                parts.append(str(item.get("text") or item.get("input_text") or item.get("output_text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return ""


def looks_like_json_blob(text: str) -> bool:
    clean = text.strip()
    if len(clean) < 800:
        return False
    if clean[0] in "{[":
        try:
            json.loads(clean)
            return True
        except json.JSONDecodeError:
            pass
    punctuation = sum(clean.count(ch) for ch in '{}[]":,')
    return punctuation / max(len(clean), 1) > 0.18 and clean.count('"') > 30


def looks_like_dom_snapshot(text: str) -> bool:
    if len(text) < 500:
        return False
    if len(re.findall(r"\[ref=e\d+\]", text)) >= 3:
        return True
    snapshot_lines = re.findall(r"(?m)^\s*-\s+[a-z][\w-]*(?:\s+\"[^\"]*\")?\s+\[", text)
    return len(snapshot_lines) >= 5


def is_low_value_context_text(text: str) -> bool:
    clean = text.strip()
    if not clean:
        return True
    lower = clean.lower()
    if any(marker in lower for marker in LOW_VALUE_TEXT_MARKERS):
        return True
    return looks_like_json_blob(clean) or looks_like_dom_snapshot(clean)


def extract_message_text(item: dict[str, Any]) -> tuple[str, str]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    item_type = item.get("type", "")
    if item_type == "session_meta":
        return "", "meta"
    if item_type == "turn_context":
        return "", "context"
    if item_type == "response_item":
        payload_type = str(payload.get("type") or "").lower()
        if payload_type in LOW_VALUE_ITEM_TYPES:
            return "", payload_type
        if payload.get("type") == "message":
            role = normalize_message_role(str(payload.get("role") or ""))
            if not role:
                return "", str(payload.get("role") or "message")
            text = text_from_content(payload.get("content"))
            if is_low_value_context_text(text):
                return "", role
            return text, role
        return "", str(payload.get("type") or item_type)
    if item_type == "event_msg":
        if payload.get("type") in {"user_message", "agent_message"}:
            role = normalize_message_role(str(payload.get("type") or ""))
            text = str(payload.get("message") or "")
            if is_low_value_context_text(text):
                return "", role
            return text, role
        return "", str(payload.get("type") or item_type)
    if item_type == "message":
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        role = normalize_message_role(str(message.get("role") or ""))
        if not role:
            return "", str(message.get("role") or item_type)
        text = text_from_content(message.get("content"))
        if is_low_value_context_text(text):
            return "", role
        return text, role
    return "", item_type


def compact_snippet(text: str, keyword: str, width: int = 260) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= width:
        return clean
    pos = clean.lower().find(keyword.lower())
    if pos < 0:
        return clean[: width - 1] + "…"
    start = max(0, pos - width // 3)
    end = min(len(clean), start + width)
    prefix = "…" if start else ""
    suffix = "…" if end < len(clean) else ""
    return prefix + clean[start:end] + suffix
