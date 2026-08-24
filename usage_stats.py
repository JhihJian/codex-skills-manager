from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from session_logs import (
    compact_snippet,
    extract_message_text,
    normalize_bool,
    parse_positive_int,
    parse_timestamp,
    pi_session_family,
    read_session_index,
    session_id_from_path,
    source_session_files,
    text_from_content,
)


FALSE_VALUES = {"0", "false", "no", "off"}
VALID_SCOPES = {"enabled", "managed", "all"}
USAGE_STATS_VERSION = 2


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSE_VALUES


def env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def normalize_scope(value: Any, default: str = "enabled") -> str:
    scope = str(value or default).strip().lower()
    return scope if scope in VALID_SCOPES else default


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_config_from_env() -> dict[str, Any]:
    scope = normalize_scope(os.environ.get("CODEX_SKILL_USAGE_STATS_SCOPE"), "all")
    return {
        "enabled": env_bool("CODEX_SKILL_USAGE_STATS_ENABLED", True),
        "staleDays": env_int("CODEX_SKILL_USAGE_STALE_DAYS", 30, minimum=1, maximum=3650),
        "maxFiles": env_int("CODEX_SKILL_USAGE_MAX_FILES", 1000, minimum=1, maximum=10000),
        "scope": scope,
        "includeSystem": env_bool("CODEX_SKILL_USAGE_STATS_INCLUDE_SYSTEM", True),
        "dailyEnabled": env_bool("CODEX_SKILL_USAGE_DAILY_ENABLED", True),
        "dailyHour": env_int("CODEX_SKILL_USAGE_DAILY_HOUR", 3, minimum=0, maximum=23),
        "dailyMinute": env_int("CODEX_SKILL_USAGE_DAILY_MINUTE", 0, minimum=0, maximum=59),
    }


def sanitize_config(raw: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = defaults or default_config_from_env()
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": normalize_bool(raw.get("enabled"), default=bool(defaults["enabled"])),
        "staleDays": parse_positive_int(raw.get("staleDays"), int(defaults["staleDays"]), minimum=1, maximum=3650),
        "maxFiles": parse_positive_int(raw.get("maxFiles"), int(defaults["maxFiles"]), minimum=1, maximum=10000),
        "scope": normalize_scope(raw.get("scope"), str(defaults["scope"])),
        "includeSystem": normalize_bool(raw.get("includeSystem"), default=bool(defaults["includeSystem"])),
        "dailyEnabled": normalize_bool(raw.get("dailyEnabled"), default=bool(defaults["dailyEnabled"])),
        "dailyHour": parse_positive_int(raw.get("dailyHour"), int(defaults["dailyHour"]), minimum=0, maximum=23),
        "dailyMinute": parse_positive_int(raw.get("dailyMinute"), int(defaults["dailyMinute"]), minimum=0, maximum=59),
    }


class UsageStatsService:
    def __init__(
        self,
        *,
        stats_file: Path,
        sessions_dir: Path,
        archived_sessions_dir: Path,
        pi_sessions_dir: Path,
        session_index_file: Path,
        read_settings: Callable[[], dict[str, Any]],
        write_settings: Callable[[dict[str, Any]], None],
        read_registry_state: Callable[[], dict[str, Any]],
        append_audit: Callable[[str, dict[str, Any]], None],
        safe_skill_name: Callable[[str], str],
    ) -> None:
        self.stats_file = stats_file
        self.sessions_dir = sessions_dir
        self.archived_sessions_dir = archived_sessions_dir
        self.pi_sessions_dir = pi_sessions_dir
        self.session_index_file = session_index_file
        self.read_settings = read_settings
        self.write_settings = write_settings
        self.read_registry_state = read_registry_state
        self.append_audit = append_audit
        self.safe_skill_name = safe_skill_name
        self.defaults = default_config_from_env()
        self._stats_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._refreshing = threading.Event()

    def config(self) -> dict[str, Any]:
        settings = self.read_settings()
        return sanitize_config(settings.get("usageStats"), self.defaults)

    def update_config(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.read_settings()
        current = self.config()
        requested = body.get("usageStats") if isinstance(body.get("usageStats"), dict) else body
        updated = sanitize_config({**current, **(requested if isinstance(requested, dict) else {})}, self.defaults)
        settings["usageStats"] = updated
        self.write_settings(settings)
        self.append_audit("update-usage-stats-settings", updated)
        return {"message": "使用统计设置已保存。", "usageStats": updated}

    def default_payload(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config or self.config()
        return {
            "version": USAGE_STATS_VERSION,
            "reviewedAt": "",
            "staleDays": config["staleDays"],
            "scope": config["scope"],
            "includeSystem": config["includeSystem"],
            "stats": {
                "reviewed": 0,
                "active": 0,
                "stale": 0,
                "neverUsed": 0,
                "declaredOnly": 0,
                "issues": 0,
            },
            "entries": [],
            "scan": {
                "maxFiles": config["maxFiles"],
                "scannedFiles": 0,
                "scannedLines": 0,
                "sessions": str(self.sessions_dir),
                "archivedSessions": str(self.archived_sessions_dir),
                "piSessions": str(self.pi_sessions_dir),
                "sources": {
                    "codex": {"scannedFiles": 0, "scannedLines": 0},
                    "pi": {"scannedFiles": 0, "scannedLines": 0},
                },
            },
            "evidencePolicy": "尚未生成使用统计缓存。",
        }

    def disabled_payload(self) -> dict[str, Any]:
        payload = self.default_payload()
        payload["disabled"] = True
        payload["evidencePolicy"] = "使用统计功能已关闭。"
        return payload

    def read_stats(self) -> dict[str, Any]:
        config = self.config()
        if not config["enabled"]:
            return self.disabled_payload()
        with self._stats_lock:
            payload = read_json(self.stats_file, self.default_payload(config))
        if not isinstance(payload, dict):
            payload = self.default_payload(config)
        if payload.get("version") != USAGE_STATS_VERSION:
            outdated = self.default_payload(config)
            outdated["outdated"] = True
            outdated["evidencePolicy"] = "使用统计缓存版本已过期，请刷新后查看 Codex 与 Pi 的合并结果。"
            return outdated
        payload.setdefault("version", USAGE_STATS_VERSION)
        payload.setdefault("reviewedAt", "")
        payload.setdefault("staleDays", config["staleDays"])
        payload.setdefault("scope", config["scope"])
        payload.setdefault("includeSystem", config["includeSystem"])
        payload.setdefault("stats", {})
        payload.setdefault("entries", [])
        payload.setdefault("scan", {})
        payload.setdefault("evidencePolicy", "")
        return payload

    def write_stats(self, payload: dict[str, Any]) -> None:
        with self._stats_lock:
            value = {"version": USAGE_STATS_VERSION, **payload}
            write_json(self.stats_file, value)

    def is_refreshing(self) -> bool:
        return self._refreshing.is_set()

    def skill_summary(self, item: dict[str, Any] | None) -> dict[str, Any]:
        if not item:
            return {
                "status": "unknown",
                "lastUsedAt": "",
                "daysSinceLastUsed": None,
                "confirmedEvidenceCount": 0,
                "confirmedSessionCount": 0,
                "confirmedDayCount": 0,
                "announcementEvidenceCount": 0,
                "announcementSessionCount": 0,
            }
        return {
            "status": item.get("status") or "unknown",
            "lastUsedAt": item.get("lastUsedAt") or "",
            "lastAnnouncementAt": item.get("lastAnnouncementAt") or "",
            "daysSinceLastUsed": item.get("daysSinceLastUsed"),
            "confirmedEvidenceCount": int(item.get("confirmedEvidenceCount") or 0),
            "confirmedSessionCount": int(item.get("confirmedSessionCount") or 0),
            "confirmedDayCount": int(item.get("confirmedDayCount") or 0),
            "announcementEvidenceCount": int(item.get("announcementEvidenceCount") or 0),
            "announcementSessionCount": int(item.get("announcementSessionCount") or 0),
        }

    def summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config()
        reviewed_at = str(payload.get("reviewedAt") or "")
        reviewed_dt = parse_timestamp(reviewed_at)
        now = datetime.now(timezone.utc)
        age_hours = None
        if reviewed_dt:
            age_hours = max(0, round((now - reviewed_dt).total_seconds() / 3600, 1))
        return {
            "enabled": bool(config["enabled"]),
            "reviewedAt": reviewed_at,
            "ageHours": age_hours,
            "staleDays": payload.get("staleDays", config["staleDays"]),
            "scope": payload.get("scope") or config["scope"],
            "includeSystem": bool(payload.get("includeSystem", config["includeSystem"])),
            "stats": payload.get("stats") or {},
            "scan": payload.get("scan") or {},
            "refreshing": self._refreshing.is_set(),
            "dailyEnabled": bool(config["dailyEnabled"]),
            "dailyTime": f"{int(config['dailyHour']):02d}:{int(config['dailyMinute']):02d}",
        }

    def review(self, body: dict[str, Any] | None = None, *, registry: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        config = self.config()
        stale_days = parse_positive_int(body.get("staleDays"), config["staleDays"], minimum=1, maximum=3650)
        max_files = parse_positive_int(body.get("maxFiles"), config["maxFiles"], minimum=1, maximum=10000)
        scope = normalize_scope(body.get("scope"), "enabled")
        include_system = normalize_bool(body.get("includeSystem"), default=True)

        if registry is None:
            registry = self.read_registry_state()
        skills = scoped_usage_skills(registry, scope, include_system)
        aliases = build_skill_alias_map({"skills": skills})
        usage: dict[str, dict[str, Any]] = {name: {} for name in skills}
        index = read_session_index(self.session_index_file)
        scanned_files = 0
        scanned_lines = 0
        source_scan = {
            "codex": {"scannedFiles": 0, "scannedLines": 0},
            "pi": {"scannedFiles": 0, "scannedLines": 0},
        }
        seen_events: set[tuple[str, str, str]] = set()
        pi_family_cache: dict[Path, str] = {}

        for source_file in source_session_files(
            self.sessions_dir,
            self.archived_sessions_dir,
            self.pi_sessions_dir,
            limit_per_source=max_files,
        ):
            source = source_file.source
            file_path = source_file.path
            scanned_files += 1
            source_scan[source]["scannedFiles"] += 1
            fallback_session_id = session_id_from_path(file_path)
            session_id = fallback_session_id
            session_family = pi_session_family(file_path, cache=pi_family_cache) if source == "pi" else session_id
            meta = index.get(session_id, {})
            pi_title = ""
            first_user_text = ""
            file_evidence: list[dict[str, Any]] = []
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as f:
                    for line_number, line in enumerate(f, 1):
                        scanned_lines += 1
                        source_scan[source]["scannedLines"] += 1
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if item.get("type") == "session_meta":
                            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                            session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                            meta = index.get(session_id, meta)
                            continue
                        if source == "pi" and item.get("type") == "session":
                            session_id = str(item.get("id") or session_id)
                            continue
                        if source == "pi" and item.get("type") == "session_info":
                            pi_title = str(item.get("name") or "").strip()
                            continue

                        event_time = item.get("timestamp", "")
                        text, role = extract_message_text(item)
                        if source == "pi" and role == "user" and text and not first_user_text:
                            first_user_text = compact_snippet(text, "", width=80)
                        for read_event in self.extract_skill_read_evidence(item, source=source):
                            event_id = str(read_event.get("eventId") or item.get("id") or f"{file_path}:{line_number}")
                            event_key = (source, session_family, event_id)
                            if event_key in seen_events:
                                continue
                            seen_events.add(event_key)
                            canonical_names = {
                                name
                                for raw_name in read_event["names"]
                                if (name := canonical_skill_name(raw_name, aliases)) and name in usage
                            }
                            for name in canonical_names:
                                evidence = {
                                    "type": read_event.get("type") or "skill-file-read",
                                    "confidence": "confirmed",
                                    "source": source,
                                    "eventId": event_id,
                                    "time": event_time,
                                    "sessionId": session_id,
                                    "sessionKey": f"{source}:{session_id}",
                                    "title": file_path.stem,
                                    "path": str(file_path),
                                    "line": line_number,
                                    "snippet": compact_snippet(read_event["text"], name, width=300),
                                }
                                file_evidence.append(evidence)
                                add_usage_evidence(usage[name], "confirmed", evidence)

                        for name, alias, text in extract_skill_announcements(item, {"skills": skills}):
                            if name not in usage:
                                continue
                            announcement = {
                                "type": "assistant-announcement",
                                "confidence": "supporting",
                                "source": source,
                                "time": event_time,
                                "sessionId": session_id,
                                "sessionKey": f"{source}:{session_id}",
                                "title": file_path.stem,
                                "path": str(file_path),
                                "line": line_number,
                                "snippet": compact_snippet(text, alias, width=300),
                            }
                            file_evidence.append(announcement)
                            add_usage_evidence(usage[name], "announcement", announcement)
            except OSError:
                continue
            title = (
                (meta.get("thread_name") if source == "codex" else pi_title or first_user_text)
                or file_path.stem
            )
            for evidence in file_evidence:
                evidence["title"] = title
                evidence["sessionId"] = session_id
                evidence["sessionKey"] = f"{source}:{session_id}"

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=stale_days)
        entries = [
            build_usage_entry(name, entry, usage.get(name, {}), cutoff=cutoff, now=now)
            for name, entry in skills.items()
        ]
        status_order = {"never-used": 0, "declared-only": 1, "stale": 2, "active": 3}
        entries.sort(key=lambda item: (status_order.get(item["status"], 9), item["category"], item["name"].lower()))
        stats = {
            "reviewed": len(entries),
            "active": len([item for item in entries if item["status"] == "active"]),
            "stale": len([item for item in entries if item["status"] == "stale"]),
            "neverUsed": len([item for item in entries if item["status"] == "never-used"]),
            "declaredOnly": len([item for item in entries if item["status"] == "declared-only"]),
        }
        stats["issues"] = stats["stale"] + stats["neverUsed"] + stats["declaredOnly"]
        warnings: list[dict[str, str]] = []
        if not entries:
            warnings.append(
                {
                    "code": "empty-scope",
                    "message": "当前审查范围内没有技能，请切换范围或启用技能后再判断。",
                }
            )
        if scanned_files == 0:
            warnings.append(
                {
                    "code": "no-session-files",
                    "message": "没有扫描到会话文件，结果只能说明缺少证据，不能证明技能从未使用。",
                }
            )
        return {
            "version": USAGE_STATS_VERSION,
            "reviewedAt": now_iso(),
            "staleDays": stale_days,
            "scope": scope,
            "includeSystem": include_system,
            "stats": stats,
            "warnings": warnings,
            "entries": entries,
            "scan": {
                "maxFiles": max_files,
                "scannedFiles": scanned_files,
                "scannedLines": scanned_lines,
                "sessions": str(self.sessions_dir),
                "archivedSessions": str(self.archived_sessions_dir),
                "piSessions": str(self.pi_sessions_dir),
                "sources": source_scan,
            },
            "evidencePolicy": "合并 Codex 与 Pi 会话，把 SKILL.md 读取工具调用和 Pi /skill 命令计为技能加载证据；助手明确使用声明仅作为辅助证据。已排除系统技能列表、用户普通提及和上下文关键词命中。加载证据不表示任务成功或技能产生价值。",
        }

    def refresh(self, *, reason: str = "manual", body: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.config()
        if not config["enabled"]:
            payload = self.disabled_payload()
            payload["message"] = "使用统计功能已关闭。"
            return payload
        if not self._refresh_lock.acquire(blocking=False):
            payload = self.read_stats()
            payload["refreshing"] = True
            payload["message"] = "使用统计正在刷新。"
            return payload
        self._refreshing.set()
        try:
            request_body = {
                "staleDays": config["staleDays"],
                "maxFiles": config["maxFiles"],
                "scope": config["scope"],
                "includeSystem": config["includeSystem"],
            }
            if body:
                request_body.update(body)
            payload = self.review(request_body)
            payload["reason"] = reason
            self.write_stats(payload)
            self.append_audit(
                "refresh-usage-stats",
                {
                    "reason": reason,
                    "reviewed": payload.get("stats", {}).get("reviewed", 0),
                    "active": payload.get("stats", {}).get("active", 0),
                    "issues": payload.get("stats", {}).get("issues", 0),
                    "scannedFiles": payload.get("scan", {}).get("scannedFiles", 0),
                },
            )
            return payload
        finally:
            self._refreshing.clear()
            self._refresh_lock.release()

    def seconds_until_next_refresh(self) -> float:
        config = self.config()
        now = datetime.now().astimezone()
        target = now.replace(
            hour=int(config["dailyHour"]),
            minute=int(config["dailyMinute"]),
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return max(60.0, (target - now).total_seconds())

    def scheduler(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.seconds_until_next_refresh()):
            config = self.config()
            if not config["enabled"] or not config["dailyEnabled"]:
                continue
            try:
                self.refresh(reason="daily")
            except Exception as exc:  # noqa: BLE001
                self.append_audit("refresh-usage-stats-failed", {"reason": "daily", "error": str(exc)})

    def needs_startup_refresh(self) -> bool:
        config = self.config()
        if not config["enabled"] or not config["dailyEnabled"]:
            return False
        if not self.stats_file.exists():
            return True
        payload = self.read_stats()
        if payload.get("outdated"):
            return True
        reviewed_at = parse_timestamp(payload.get("reviewedAt"))
        if not reviewed_at:
            return True
        return datetime.now(timezone.utc) - reviewed_at > timedelta(hours=25)

    def start_refresh_thread(self, reason: str) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.refresh(reason=reason),
            name=f"usage-stats-{reason}",
            daemon=True,
        )
        thread.start()
        return thread

    def skill_names_from_skill_paths(self, text: str) -> set[str]:
        normalized = text.replace("/", "\\")
        names: set[str] = set()
        for pattern in SKILL_PATH_PATTERNS:
            for match in pattern.finditer(normalized):
                try:
                    names.add(self.safe_skill_name(match.group(1)))
                except Exception:  # noqa: BLE001
                    continue
        return names

    def extract_skill_read_evidence(self, item: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
        if source == "pi":
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            if item.get("type") != "message":
                return []
            if message.get("role") == "user":
                text = text_from_content(message.get("content"))
                match = PI_SKILL_BLOCK_PATTERN.search(text)
                if not match:
                    return []
                names = self.skill_names_from_skill_paths(match.group("location"))
                names.add(match.group("name"))
                return [
                    {
                        "type": "skill-command-load",
                        "eventId": str(item.get("id") or ""),
                        "names": names,
                        "text": match.group(0),
                    }
                ]
            if message.get("role") != "assistant":
                return []
            content = message.get("content") if isinstance(message.get("content"), list) else []
            events: list[dict[str, Any]] = []
            for index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                event_id = str(block.get("id") or f"{item.get('id') or 'message'}:{index}")
                events.extend(self.extract_pi_tool_read_events(block.get("name"), block.get("arguments"), event_id))
            return events

        if item.get("type") != "response_item":
            return []
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if payload.get("type") != "function_call":
            return []
        text = function_call_text(payload)
        names = self.skill_names_from_skill_paths(text)
        if not names or not looks_like_skill_read_call(payload, text):
            return []
        return [
            {
                "eventId": str(payload.get("call_id") or item.get("id") or ""),
                "names": names,
                "text": text,
            }
        ]

    def extract_pi_tool_read_events(self, tool_name: Any, arguments: Any, event_id: str) -> list[dict[str, Any]]:
        name = str(tool_name or "")
        normalized_name = name.rsplit(".", 1)[-1].lower()
        args = arguments if isinstance(arguments, dict) else {}
        if normalized_name in DIRECT_READ_TOOLS:
            text = function_call_text({"name": name, "arguments": args})
            names = self.skill_names_from_skill_paths(text)
            return [{"eventId": event_id, "names": names, "text": text}] if names else []
        if normalized_name in {"bash", "exec_command"}:
            text = function_call_text({"name": name, "arguments": args})
            names = self.skill_names_from_skill_paths(text)
            if names and SKILL_READ_COMMAND_PATTERN.search(text):
                return [{"eventId": event_id, "names": names, "text": text}]
            return []
        if normalized_name not in {"parallel", "multi_tool_use"} and name.lower() != "multi_tool_use.parallel":
            return []
        events: list[dict[str, Any]] = []
        nested_calls = args.get("tool_uses") if isinstance(args.get("tool_uses"), list) else []
        for index, nested in enumerate(nested_calls):
            if not isinstance(nested, dict):
                continue
            events.extend(
                self.extract_pi_tool_read_events(
                    nested.get("recipient_name"),
                    nested.get("parameters"),
                    f"{event_id}:{index}",
                )
            )
        return events


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def collect_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(collect_string_values(item))
        return parts
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(collect_string_values(item))
        return parts
    if value is None:
        return []
    return [str(value)]


def function_call_text(payload: dict[str, Any]) -> str:
    parts = [str(payload.get("name") or "")]
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parts.append(arguments)
        else:
            parts.extend(collect_string_values(parsed))
    else:
        parts.extend(collect_string_values(arguments))
    return "\n".join(part for part in parts if part)


SKILL_PATH_PATTERNS = [
    re.compile(r"(?i)(?:^|[\\/])skills[\\/]([^\\/\s\"'`<>|:]+)[\\/]SKILL\.md"),
    re.compile(r"(?i)(?:^|[\\/])skills[\\/]\.system[\\/]([^\\/\s\"'`<>|:]+)[\\/]SKILL\.md"),
    re.compile(r"(?i)(?:^|[\\/])skills[\\/](?!\.system[\\/])([^\\/\s\"'`<>|:]+)[\\/]SKILL\.md"),
    re.compile(r"(?i)(?:^|[\\/])plugins[\\/]cache[\\/].{1,220}?[\\/]skills[\\/]([^\\/\s\"'`<>|:]+)[\\/]SKILL\.md"),
]
PI_SKILL_BLOCK_PATTERN = re.compile(
    r'<skill\s+name="(?P<name>[^"]+)"\s+location="(?P<location>[^"]+)">',
    re.IGNORECASE,
)
SKILL_READ_COMMAND_PATTERN = re.compile(
    r"(?i)\b(Get-Content|gc|type|cat|rg|Select-String|sed|read_text|readText|readFile|open)\b"
)
READ_RESOURCE_TOOLS = {"read_mcp_resource"}
DIRECT_READ_TOOLS = {"read", "read_file", "read_text", "read_text_file", *READ_RESOURCE_TOOLS}


def build_skill_alias_map(registry: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()

    def add_alias(alias: str, name: str) -> None:
        clean = alias.strip().lower()
        if not clean:
            return
        existing = aliases.get(clean)
        if existing and existing != name:
            ambiguous.add(clean)
            aliases.pop(clean, None)
            return
        if clean not in ambiguous:
            aliases[clean] = name

    for name, entry in registry.get("skills", {}).items():
        add_alias(name, name)
        if ":" in name:
            add_alias(name.rsplit(":", 1)[-1], name)
        frontmatter_name = str(entry.get("frontmatter", {}).get("name") or "").strip()
        if frontmatter_name:
            add_alias(frontmatter_name, name)
    return aliases


def canonical_skill_name(raw_name: str, aliases: dict[str, str]) -> str | None:
    return aliases.get(raw_name.strip().lower())


def skill_aliases_for_entry(name: str, entry: dict[str, Any]) -> list[str]:
    aliases = [name]
    if ":" in name:
        aliases.append(name.rsplit(":", 1)[-1])
    frontmatter_name = str(entry.get("frontmatter", {}).get("name") or "").strip()
    if frontmatter_name:
        aliases.append(frontmatter_name)
    unique: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        key = alias.lower()
        if key not in seen:
            unique.append(alias)
            seen.add(key)
    return unique


def looks_like_skill_read_call(payload: dict[str, Any], text: str) -> bool:
    tool_name = str(payload.get("name") or "").strip().rsplit(".", 1)[-1].lower()
    if tool_name in DIRECT_READ_TOOLS:
        return True
    return bool(SKILL_READ_COMMAND_PATTERN.search(text))


def is_skill_announcement(text: str, alias: str) -> bool:
    alias_pattern = re.escape(alias)
    patterns = [
        rf"(?i)\b(?:using|use|used)\s+(?:the\s+)?`?{alias_pattern}`?(?:\s+skill)?\b",
        rf"(?:使用|调用|应用|采用|按|根据).{{0,40}}`?{alias_pattern}`?.{{0,24}}(?:skill|技能)?",
        rf"`?{alias_pattern}`?.{{0,24}}(?:skill|技能).{{0,40}}(?:使用|调用|应用|读取)",
        rf"(?i)`?{alias_pattern}`?\s+(?:skill|skills)\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def extract_skill_announcements(item: dict[str, Any], registry: dict[str, Any]) -> list[tuple[str, str, str]]:
    text, role = extract_message_text(item)
    if role != "assistant" or not text:
        return []
    matches: list[tuple[str, str, str]] = []
    for name, entry in registry.get("skills", {}).items():
        for alias in skill_aliases_for_entry(name, entry):
            if alias.lower() not in text.lower():
                continue
            if is_skill_announcement(text, alias):
                matches.append((name, alias, text))
                break
    return matches


def add_usage_evidence(
    bucket: dict[str, Any],
    kind: str,
    evidence: dict[str, Any],
    *,
    max_examples: int = 6,
) -> None:
    count_key = f"{kind}Count"
    list_key = f"{kind}Evidence"
    bucket[count_key] = int(bucket.get(count_key) or 0) + 1
    event_time = parse_timestamp(evidence.get("time"))
    session_key = str(evidence.get("sessionKey") or evidence.get("sessionId") or "").strip()
    if session_key:
        bucket.setdefault(f"{kind}Sessions", set()).add(session_key)
    if event_time:
        bucket.setdefault(f"{kind}Days", set()).add(event_time.astimezone().date().isoformat())
    last_key = f"last{kind.title()}At"
    current_last = parse_timestamp(bucket.get(last_key))
    if event_time and (current_last is None or event_time > current_last):
        bucket[last_key] = event_time.isoformat()
    examples = bucket.setdefault(list_key, [])
    examples.append(evidence)
    examples.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    del examples[max_examples:]


def scoped_usage_skills(registry: dict[str, Any], scope: str, include_system: bool) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for name, entry in registry.get("skills", {}).items():
        if entry.get("status") == "missing":
            continue
        if entry.get("system") and not include_system:
            continue
        if scope == "enabled" and not entry.get("enabled"):
            continue
        if scope == "managed" and not entry.get("managed"):
            continue
        selected[name] = entry
    return selected


def build_usage_entry(
    name: str,
    entry: dict[str, Any],
    usage: dict[str, Any],
    *,
    cutoff: datetime,
    now: datetime,
) -> dict[str, Any]:
    confirmed_at = parse_timestamp(usage.get("lastConfirmedAt"))
    announced_at = parse_timestamp(usage.get("lastAnnouncementAt"))
    if confirmed_at:
        days_since = max(0, int((now - confirmed_at).total_seconds() // 86400))
        status = "stale" if confirmed_at < cutoff else "active"
    elif usage.get("announcementCount"):
        days_since = None
        status = "declared-only"
    else:
        days_since = None
        status = "never-used"

    evidence = list(usage.get("confirmedEvidence") or [])
    announcements = list(usage.get("announcementEvidence") or [])
    evidence.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    announcements.sort(key=lambda item: str(item.get("time") or ""), reverse=True)

    return {
        "name": name,
        "title": entry.get("title") or name,
        "category": entry.get("category") or "未分类",
        "enabled": bool(entry.get("enabled")),
        "system": bool(entry.get("system")),
        "managed": bool(entry.get("managed")),
        "status": status,
        "daysSinceLastUsed": days_since,
        "lastUsedAt": confirmed_at.isoformat() if confirmed_at else "",
        "lastAnnouncementAt": announced_at.isoformat() if announced_at else "",
        "confirmedEvidenceCount": int(usage.get("confirmedCount") or 0),
        "confirmedSessionCount": len(usage.get("confirmedSessions") or []),
        "confirmedDayCount": len(usage.get("confirmedDays") or []),
        "announcementEvidenceCount": int(usage.get("announcementCount") or 0),
        "announcementSessionCount": len(usage.get("announcementSessions") or []),
        "evidence": evidence,
        "announcements": announcements,
    }
