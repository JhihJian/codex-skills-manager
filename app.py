from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from session_logs import (
    compact_snippet,
    context_role_label,
    extract_message_text,
    normalize_context_text,
    read_session_index as read_codex_session_index,
    session_files as list_codex_session_files,
    session_id_from_path,
)
from usage_stats import UsageStatsService


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PUBLIC_DIR = BASE_DIR / "public"
SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_SKILLS_REPO_DIR = (BASE_DIR.parent / "codex-skills-library").resolve()
SKILLS_REPO_DIR = Path(os.environ.get("CODEX_SKILLS_REPO_DIR", DEFAULT_SKILLS_REPO_DIR)).expanduser().resolve()
SKILLS_REPO_URL = os.environ.get("CODEX_SKILLS_REPO_URL", "").strip()
LIBRARY_DIR = (SKILLS_REPO_DIR / "skills").resolve()
SKILLS_DB_FILE = (SKILLS_REPO_DIR / "codex-skills-manager.sqlite3").resolve()
REGISTRY_KEY = "skills-registry"
LEGACY_LIBRARY_DIR = BASE_DIR / "skills-library"
LEGACY_REGISTRY_FILE = DATA_DIR / "skills-registry.json"
AUDIT_FILE = DATA_DIR / "audit-log.jsonl"
USAGE_STATS_FILE = DATA_DIR / "usage-stats.json"

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
CODEX_SKILLS_DIR = (CODEX_HOME / "skills").resolve()
CODEX_SESSIONS_DIR = (CODEX_HOME / "sessions").resolve()
CODEX_ARCHIVED_SESSIONS_DIR = (CODEX_HOME / "archived_sessions").resolve()
SESSION_INDEX_FILE = CODEX_HOME / "session_index.jsonl"
INSTALLER_SCRIPT = (
    CODEX_SKILLS_DIR
    / ".system"
    / "skill-installer"
    / "scripts"
    / "install-skill-from-github.py"
).resolve()

DEFAULT_CATEGORIES = [
    "未分类",
    "开发",
    "写作",
    "文档",
    "测试",
    "部署",
    "研究",
    "运维",
]


def env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value

AUTO_CLASSIFY_ENABLED = os.environ.get("CODEX_SKILL_AUTO_CLASSIFY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
CLASSIFY_TIMEOUT_SECONDS = int(os.environ.get("CODEX_SKILL_CLASSIFY_TIMEOUT", "240") or "240")
CLASSIFY_BATCH_SIZE = max(1, int(os.environ.get("CODEX_SKILL_CLASSIFY_BATCH_SIZE", "24") or "24"))
CLASSIFY_PREVIEW_CHARS = max(400, int(os.environ.get("CODEX_SKILL_CLASSIFY_PREVIEW_CHARS", "1800") or "1800"))
AUTO_LOCALIZE_ENABLED = os.environ.get("CODEX_SKILL_AUTO_LOCALIZE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
LOCALIZE_TIMEOUT_SECONDS = int(os.environ.get("CODEX_SKILL_LOCALIZE_TIMEOUT", "240") or "240")
LOCALIZE_BATCH_SIZE = max(1, int(os.environ.get("CODEX_SKILL_LOCALIZE_BATCH_SIZE", "24") or "24"))
LOCALIZE_PREVIEW_CHARS = max(400, int(os.environ.get("CODEX_SKILL_LOCALIZE_PREVIEW_CHARS", "2200") or "2200"))
SKILL_VERSIONING_ENABLED = os.environ.get("CODEX_SKILL_VERSIONING_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
SKILL_VERSION_COMMIT_DELAY_SECONDS = env_int(
    "CODEX_SKILL_VERSION_COMMIT_DELAY_SECONDS",
    3600,
    minimum=60,
)
SKILL_VERSION_SCAN_INTERVAL_SECONDS = env_int(
    "CODEX_SKILL_VERSION_SCAN_INTERVAL_SECONDS",
    300,
    minimum=30,
)
SKILL_VERSION_AUTO_PUSH_ENABLED = os.environ.get("CODEX_SKILL_VERSION_AUTO_PUSH", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MAX_LIFECYCLE_EVENTS = 50
TOGGLE_AUDIT_ACTIONS = {"enable-skill": "enable", "disable-skill": "disable"}
GITHUB_API_USER_AGENT = "codex-skills-manager"
MAX_GITHUB_PARENT_SKILL_SCAN = 200

REGISTRY_LOCK = threading.RLock()
SKILL_VERSION_LOCK = threading.RLock()
SKILL_VERSION_PENDING_SINCE: datetime | None = None
SKILL_VERSION_LAST_SIGNATURE = ""
SKILL_VERSION_COMMITTING = threading.Event()


class ApiError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class SkillScan:
    name: str
    path: Path
    system: bool
    metadata: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for folder in (DATA_DIR, LIBRARY_DIR, SKILLS_DB_FILE.parent, PUBLIC_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    CODEX_SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def ensure_skills_db() -> None:
    SKILLS_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SKILLS_DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def read_registry_from_db(default: dict[str, Any]) -> dict[str, Any]:
    ensure_skills_db()
    with sqlite3.connect(SKILLS_DB_FILE) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (REGISTRY_KEY,)).fetchone()
    if not row:
        return default
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return default
    return payload if isinstance(payload, dict) else default


def write_registry_to_db(registry: dict[str, Any]) -> None:
    ensure_skills_db()
    payload = json.dumps(registry, ensure_ascii=False, indent=2)
    with sqlite3.connect(SKILLS_DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO app_state(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (REGISTRY_KEY, payload, now_iso()),
        )
        conn.commit()


def read_settings() -> dict[str, Any]:
    payload = read_json(SETTINGS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def write_settings(payload: dict[str, Any]) -> None:
    write_json(SETTINGS_FILE, payload)


def apply_repository_settings(settings: dict[str, Any] | None = None) -> None:
    global SKILLS_REPO_DIR, SKILLS_REPO_URL, LIBRARY_DIR, SKILLS_DB_FILE
    settings = settings if settings is not None else read_settings()
    configured_dir = str(settings.get("skillsRepoDir") or os.environ.get("CODEX_SKILLS_REPO_DIR") or DEFAULT_SKILLS_REPO_DIR).strip()
    configured_url = str(settings.get("skillsRepoUrl") or os.environ.get("CODEX_SKILLS_REPO_URL") or "").strip()
    SKILLS_REPO_DIR = Path(configured_dir).expanduser().resolve()
    SKILLS_REPO_URL = configured_url
    LIBRARY_DIR = (SKILLS_REPO_DIR / "skills").resolve()
    SKILLS_DB_FILE = (SKILLS_REPO_DIR / "codex-skills-manager.sqlite3").resolve()


apply_repository_settings()


def repository_values(settings: dict[str, Any]) -> tuple[Path, str]:
    configured_dir = str(settings.get("skillsRepoDir") or os.environ.get("CODEX_SKILLS_REPO_DIR") or DEFAULT_SKILLS_REPO_DIR).strip()
    configured_url = str(settings.get("skillsRepoUrl") or os.environ.get("CODEX_SKILLS_REPO_URL") or "").strip()
    return Path(configured_dir).expanduser().resolve(), configured_url


def repository_layout_markers(path: Path) -> set[str]:
    markers: set[str] = set()
    if (path / "skills").exists():
        markers.add("skills")
    if (path / "skills-library").exists():
        markers.add("legacy-skills-library")
    if (path / "codex-skills-manager.sqlite3").exists():
        markers.add("database")
    if (path / ".git").exists():
        markers.add("git")
    return markers


def has_repository_metadata(path: Path) -> bool:
    db_path = path / "codex-skills-manager.sqlite3"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", ("repository",)).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return False
    return payload.get("layout") == "skills-plus-sqlite"


def directory_child_names(path: Path) -> set[str]:
    try:
        return {child.name for child in path.iterdir()}
    except OSError:
        return set()


def looks_like_skills_repository(path: Path) -> bool:
    if has_repository_metadata(path):
        return True
    names = directory_child_names(path)
    if not names:
        return True
    allowed = {
        ".git",
        ".gitignore",
        "README",
        "README.md",
        "LICENSE",
        "LICENSE.md",
        "skills",
        "skills-library",
        "codex-skills-manager.sqlite3",
    }
    if names.issubset(allowed) and ({"skills", "skills-library", "codex-skills-manager.sqlite3"} & names or names <= {".git", ".gitignore", "README", "README.md", "LICENSE", "LICENSE.md"}):
        return True
    return False


def validate_skills_repository_dir(path: Path) -> None:
    path = path.expanduser().resolve()
    if path == path.parent:
        raise ApiError("skills 仓库路径不能是磁盘根目录。")
    home = Path.home().resolve()
    if path == home:
        raise ApiError("skills 仓库路径不能是用户 Home 目录。")
    if path == CODEX_HOME or is_relative_to(path, CODEX_HOME):
        raise ApiError("skills 仓库路径不能位于 .codex 目录内。")
    if path == BASE_DIR:
        raise ApiError("skills 仓库路径不能是当前管理器项目根目录。")
    if path.exists() and not path.is_dir():
        raise ApiError(f"skills 仓库路径不是目录：{path}")
    if path.exists() and (path / ".git").exists() and not looks_like_skills_repository(path):
        raise ApiError("该路径是已有 Git 仓库，但不像 Codex skills 仓库，已拒绝写入。")
    if path.exists() and not (path / ".git").exists() and directory_child_names(path) and not looks_like_skills_repository(path):
        raise ApiError("该路径已包含非 skills 仓库文件，已拒绝写入。")


def repository_health() -> dict[str, Any]:
    errors: list[str] = []
    db_open = False
    if SKILLS_DB_FILE.exists():
        try:
            with sqlite3.connect(SKILLS_DB_FILE) as conn:
                conn.execute("SELECT 1").fetchone()
            db_open = True
        except sqlite3.Error as exc:
            errors.append(f"SQLite 无法打开：{exc}")
    else:
        db_open = False
    if not SKILLS_REPO_DIR.exists():
        errors.append("skills 仓库目录不存在")
    elif not SKILLS_REPO_DIR.is_dir():
        errors.append("skills 仓库路径不是目录")
    return {
        "exists": SKILLS_REPO_DIR.exists(),
        "git": (SKILLS_REPO_DIR / ".git").exists(),
        "skillsDir": LIBRARY_DIR.exists(),
        "database": SKILLS_DB_FILE.exists(),
        "databaseOpen": db_open,
        "markers": sorted(repository_layout_markers(SKILLS_REPO_DIR)) if SKILLS_REPO_DIR.exists() else [],
        "errors": errors,
    }


def parse_iso_timestamp(value: Any) -> datetime | None:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_later_timestamp(candidate: Any, current: Any) -> bool:
    candidate_dt = parse_iso_timestamp(candidate)
    if candidate_dt is None:
        return False
    current_dt = parse_iso_timestamp(current)
    return current_dt is None or candidate_dt > current_dt


def append_audit(action: str, payload: dict[str, Any], *, event_time: str | None = None) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {"time": event_time or now_iso(), "action": action, **payload}
    with AUDIT_FILE.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_audit_events(limit: int | None = None) -> list[dict[str, Any]]:
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def normalize_lifecycle_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        occurred_at = str(item.get("time") or "").strip()
        if action not in {"enable", "disable"} or not occurred_at:
            continue
        event = dict(item)
        event["action"] = action
        event["time"] = occurred_at
        events.append(event)

    def event_key(item: dict[str, Any]) -> datetime:
        return parse_iso_timestamp(item.get("time")) or datetime.min.replace(tzinfo=timezone.utc)

    return sorted(events, key=event_key)[-MAX_LIFECYCLE_EVENTS:]


def normalize_skill_lifecycle(value: Any) -> dict[str, Any]:
    lifecycle = dict(value) if isinstance(value, dict) else {}
    lifecycle["events"] = normalize_lifecycle_events(lifecycle.get("events"))
    for event in lifecycle["events"]:
        action = event.get("action")
        occurred_at = event.get("time")
        if action == "enable" and is_later_timestamp(occurred_at, lifecycle.get("lastEnabledAt")):
            lifecycle["lastEnabledAt"] = occurred_at
        if action == "disable" and is_later_timestamp(occurred_at, lifecycle.get("lastDisabledAt")):
            lifecycle["lastDisabledAt"] = occurred_at
        if is_later_timestamp(occurred_at, lifecycle.get("lastActionAt")):
            lifecycle["lastAction"] = action
            lifecycle["lastActionAt"] = occurred_at
    return lifecycle


def record_skill_lifecycle(registry: dict[str, Any], name: str, action: str, occurred_at: str, details: dict[str, Any] | None = None) -> None:
    entry = registry.get("skills", {}).get(name)
    if not entry or action not in {"enable", "disable"}:
        return
    lifecycle = normalize_skill_lifecycle(entry.get("lifecycle"))
    event: dict[str, Any] = {"time": occurred_at, "action": action}
    if details:
        event.update(details)
    lifecycle["events"] = normalize_lifecycle_events([*lifecycle.get("events", []), event])
    lifecycle["lastAction"] = action
    lifecycle["lastActionAt"] = occurred_at
    if action == "enable":
        lifecycle["lastEnabledAt"] = occurred_at
    else:
        lifecycle["lastDisabledAt"] = occurred_at
    entry["lifecycle"] = lifecycle
    entry["updatedAt"] = occurred_at


def audit_lifecycle_by_skill() -> dict[str, dict[str, Any]]:
    lifecycle_by_skill: dict[str, dict[str, Any]] = {}
    for event in read_audit_events():
        action = TOGGLE_AUDIT_ACTIONS.get(str(event.get("action") or ""))
        if not action:
            continue
        name = str(event.get("skill") or "").strip()
        occurred_at = str(event.get("time") or "").strip()
        if not name or not occurred_at:
            continue
        lifecycle = lifecycle_by_skill.setdefault(name, {"events": []})
        lifecycle["events"].append({"time": occurred_at, "action": action, "source": "audit-log"})
        if action == "enable" and is_later_timestamp(occurred_at, lifecycle.get("lastEnabledAt")):
            lifecycle["lastEnabledAt"] = occurred_at
        if action == "disable" and is_later_timestamp(occurred_at, lifecycle.get("lastDisabledAt")):
            lifecycle["lastDisabledAt"] = occurred_at
        if is_later_timestamp(occurred_at, lifecycle.get("lastActionAt")):
            lifecycle["lastAction"] = action
            lifecycle["lastActionAt"] = occurred_at
    return {name: normalize_skill_lifecycle(lifecycle) for name, lifecycle in lifecycle_by_skill.items()}


def merge_skill_lifecycle(primary: Any, fallback: Any) -> dict[str, Any]:
    lifecycle = normalize_skill_lifecycle(primary)
    audit_lifecycle = normalize_skill_lifecycle(fallback)
    for key in ("lastEnabledAt", "lastDisabledAt"):
        if is_later_timestamp(audit_lifecycle.get(key), lifecycle.get(key)):
            lifecycle[key] = audit_lifecycle[key]
    if is_later_timestamp(audit_lifecycle.get("lastActionAt"), lifecycle.get("lastActionAt")):
        lifecycle["lastAction"] = audit_lifecycle.get("lastAction")
        lifecycle["lastActionAt"] = audit_lifecycle.get("lastActionAt")

    events: dict[tuple[str, str], dict[str, Any]] = {}
    for event in [*lifecycle.get("events", []), *audit_lifecycle.get("events", [])]:
        key = (str(event.get("time") or ""), str(event.get("action") or ""))
        if key[0] and key[1]:
            events[key] = event
    lifecycle["events"] = normalize_lifecycle_events(list(events.values()))
    return lifecycle


def disabled_duration_seconds(lifecycle: dict[str, Any], *, enabled: bool) -> int | None:
    if enabled:
        return None
    disabled_at = parse_iso_timestamp(lifecycle.get("lastDisabledAt"))
    if disabled_at is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - disabled_at).total_seconds()))


def load_registry() -> dict[str, Any]:
    with REGISTRY_LOCK:
        registry = read_registry_from_db({"version": 1, "updatedAt": now_iso(), "categories": DEFAULT_CATEGORIES, "skills": {}})
    registry.setdefault("version", 1)
    registry.setdefault("updatedAt", now_iso())
    registry.setdefault("categories", list(DEFAULT_CATEGORIES))
    registry.setdefault("skills", {})
    for category in DEFAULT_CATEGORIES:
        if category not in registry["categories"]:
            registry["categories"].append(category)
    return registry


def save_registry(registry: dict[str, Any]) -> None:
    with REGISTRY_LOCK:
        registry["updatedAt"] = now_iso()
        write_registry_to_db(registry)


def merge_categories(target: dict[str, Any], categories: list[Any]) -> None:
    target.setdefault("categories", list(DEFAULT_CATEGORIES))
    for category in DEFAULT_CATEGORIES:
        if category not in target["categories"]:
            target["categories"].append(category)
    for category in categories:
        text = str(category or "").strip()
        if text and text not in target["categories"]:
            target["categories"].append(text)


def stable_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def merge_classification_registry(
    source_registry: dict[str, Any],
    names: list[str],
    *,
    force: bool,
    baseline: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    latest = sync_registry(adopt_extra=False, save=False)
    merge_categories(latest, list(source_registry.get("categories") or []))
    baseline = baseline or {}
    applied: list[str] = []
    skipped: list[str] = []
    for name in names:
        source = source_registry.get("skills", {}).get(name)
        target = latest.get("skills", {}).get(name)
        if not source or not target:
            skipped.append(name)
            continue
        initial = baseline.get(name)
        if initial and str(target.get("category") or "未分类") != str(initial.get("category") or "未分类"):
            skipped.append(name)
            continue
        if not force and target.get("category") and target.get("category") != "未分类":
            skipped.append(name)
            continue
        category = str(source.get("category") or "").strip() or "未分类"
        if category == "未分类":
            skipped.append(name)
            continue
        target["category"] = category
        if category not in latest["categories"]:
            latest["categories"].append(category)
        for key in ("tags", "dependencies", "autoClassifiedAt", "autoClassification"):
            if key in source:
                target[key] = source[key]
        auto = source.get("autoClassification") if isinstance(source.get("autoClassification"), dict) else {}
        reason = str(auto.get("reason") or "").strip()
        if reason:
            marker = f"[自动分类] {reason}"
            previous = str(target.get("notes") or "").strip()
            target["notes"] = marker if not previous else previous if marker in previous else f"{previous}\n{marker}"
        target["updatedAt"] = now_iso()
        applied.append(name)
    if applied:
        save_registry(latest)
    return latest, applied, skipped


def merge_localization_registry(
    source_registry: dict[str, Any],
    names: list[str],
    *,
    force: bool,
    baseline: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    latest = sync_registry(adopt_extra=False, save=False)
    baseline = baseline or {}
    applied: list[str] = []
    skipped: list[str] = []
    for name in names:
        source = source_registry.get("skills", {}).get(name)
        target = latest.get("skills", {}).get(name)
        localized = source.get("localized") if isinstance(source, dict) and isinstance(source.get("localized"), dict) else {}
        if not source or not target or not localized:
            skipped.append(name)
            continue
        current = target.get("localized") if isinstance(target.get("localized"), dict) else {}
        if name in baseline and stable_json(current) != baseline[name]:
            skipped.append(name)
            continue
        if not force and str(current.get("zhName") or "").strip() and str(current.get("zhTrigger") or "").strip():
            skipped.append(name)
            continue
        if not str(localized.get("zhName") or "").strip() or not str(localized.get("zhTrigger") or "").strip():
            skipped.append(name)
            continue
        target["localized"] = dict(localized)
        target["localizedAt"] = source.get("localizedAt") or localized.get("updatedAt") or localized.get("generatedAt") or now_iso()
        target["updatedAt"] = now_iso()
        applied.append(name)
    if applied:
        save_registry(latest)
    return latest, applied, skipped


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_skill_name(name: str) -> str:
    name = unquote(str(name)).strip()
    if not name or name in {".", ".."}:
        raise ApiError("技能名称不能为空。")
    if "/" in name or "\\" in name:
        raise ApiError("技能名称不能包含路径分隔符。")
    if Path(name).name != name:
        raise ApiError("技能名称不是合法的目录名。")
    return name


def safe_child(root: Path, name: str) -> Path:
    name = safe_skill_name(name)
    candidate = (root / name).resolve()
    if not is_relative_to(candidate, root.resolve()):
        raise ApiError("目标路径越界，已拒绝操作。", HTTPStatus.FORBIDDEN)
    return candidate


usage_stats_service = UsageStatsService(
    stats_file=USAGE_STATS_FILE,
    sessions_dir=CODEX_SESSIONS_DIR,
    archived_sessions_dir=CODEX_ARCHIVED_SESSIONS_DIR,
    session_index_file=SESSION_INDEX_FILE,
    read_settings=read_settings,
    write_settings=write_settings,
    read_registry_state=lambda: read_registry_state(),
    append_audit=append_audit,
    safe_skill_name=safe_skill_name,
)


def copytree_clean(src: Path, dest: Path) -> None:
    src = src.resolve()
    dest = dest.resolve()
    if not src.exists() or not src.is_dir():
        raise ApiError(f"源目录不存在：{src}")
    if dest.exists():
        raise ApiError(f"目标目录已存在：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))


def copytree_merge_missing(src: Path, dest: Path) -> int:
    src = src.resolve()
    dest = dest.resolve()
    copied = 0
    if not src.exists() or not src.is_dir():
        return copied
    for child in src.rglob("*"):
        if ".git" in child.parts or "__pycache__" in child.parts:
            continue
        relative = child.relative_to(src)
        target = dest / relative
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target)
        copied += 1
    return copied


def run_git_at(path: Path, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def clone_skills_repository_if_needed() -> bool:
    if not SKILLS_REPO_URL or SKILLS_REPO_DIR.exists():
        return False
    SKILLS_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", SKILLS_REPO_URL, str(SKILLS_REPO_DIR)],
        cwd=str(SKILLS_REPO_DIR.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if result.returncode != 0:
        raise ApiError(f"克隆 skills 仓库失败：{(result.stderr or result.stdout).strip()}")
    return True


def ensure_git_remote(url: str) -> None:
    if not url:
        return
    current = run_git_at(SKILLS_REPO_DIR, ["remote", "get-url", "origin"], timeout=30)
    if current.returncode == 0:
        if current.stdout.strip() != url:
            result = run_git_at(SKILLS_REPO_DIR, ["remote", "set-url", "origin", url], timeout=30)
            if result.returncode != 0:
                raise ApiError(f"更新 skills 仓库 origin 失败：{(result.stderr or result.stdout).strip()}")
        return
    result = run_git_at(SKILLS_REPO_DIR, ["remote", "add", "origin", url], timeout=30)
    if result.returncode != 0:
        raise ApiError(f"设置 skills 仓库 origin 失败：{(result.stderr or result.stdout).strip()}")


def write_repository_metadata() -> None:
    payload = json.dumps(
        {
            "remote": SKILLS_REPO_URL,
            "layout": "skills-plus-sqlite",
            "skillsDir": str(LIBRARY_DIR),
            "database": str(SKILLS_DB_FILE),
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(SKILLS_DB_FILE) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", ("repository",)).fetchone()
        if row and row[0] == payload:
            return
        conn.execute(
            """
            INSERT INTO metadata(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("repository", payload, now_iso()),
        )
        conn.commit()


def ensure_skills_repository() -> None:
    cloned = clone_skills_repository_if_needed()
    ensure_dirs()
    migrated_files = 0
    if LEGACY_LIBRARY_DIR.exists() and not any(LIBRARY_DIR.iterdir()):
        migrated_files += copytree_merge_missing(LEGACY_LIBRARY_DIR, LIBRARY_DIR)
    legacy_external_library = SKILLS_REPO_DIR / "skills-library"
    if legacy_external_library.exists() and not any(LIBRARY_DIR.iterdir()):
        migrated_files += copytree_merge_missing(legacy_external_library, LIBRARY_DIR)

    ensure_skills_db()
    if LEGACY_REGISTRY_FILE.exists():
        existing_registry = read_registry_from_db({})
        if not existing_registry:
            legacy_registry = read_json(LEGACY_REGISTRY_FILE, {})
            if isinstance(legacy_registry, dict) and legacy_registry:
                write_registry_to_db(legacy_registry)
                migrated_files += 1

    if not (SKILLS_REPO_DIR / ".git").exists():
        init_result = run_git_at(SKILLS_REPO_DIR, ["init"], timeout=60)
        if init_result.returncode != 0:
            raise ApiError(f"初始化技能库 Git 仓库失败：{(init_result.stderr or init_result.stdout).strip()}")
    ensure_git_remote(SKILLS_REPO_URL)

    gitignore = SKILLS_REPO_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("skills/**/node_modules/\n.DS_Store\nThumbs.db\n", encoding="utf-8", newline="\n")

    write_repository_metadata()

    if migrated_files or cloned:
        append_audit(
            "initialize-skills-repository",
            {
                "from": str(LEGACY_LIBRARY_DIR),
                "to": str(SKILLS_REPO_DIR),
                "files": migrated_files,
                "remote": SKILLS_REPO_URL,
                "cloned": cloned,
            },
        )


def parse_frontmatter(text: str) -> dict[str, Any]:
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def read_skill_metadata(path: Path, fallback_name: str) -> dict[str, Any]:
    skill_md = path / "SKILL.md"
    metadata: dict[str, Any] = {
        "name": fallback_name,
        "title": fallback_name,
        "description": "",
        "skillMdPath": str(skill_md) if skill_md.exists() else "",
    }
    if not skill_md.exists():
        return metadata
    try:
        text = skill_md.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return metadata
    frontmatter = parse_frontmatter(text)
    name = str(frontmatter.get("name") or fallback_name).strip()
    description = str(frontmatter.get("description") or "").strip()
    short_description = str(frontmatter.get("short-description") or "").strip()
    metadata.update(
        {
            "name": name or fallback_name,
            "title": name or fallback_name,
            "description": description or short_description,
            "frontmatter": frontmatter,
            "skillMdPreview": trim_preview(text),
        }
    )
    return metadata


def trim_preview(text: str, max_lines: int = 48, max_chars: int = 5000) -> str:
    preview = "\n".join(text.lstrip("\ufeff").splitlines()[:max_lines])
    if len(preview) <= max_chars:
        return preview
    return preview[:max_chars].rstrip() + "\n..."


def scan_library_skills() -> dict[str, SkillScan]:
    scans: dict[str, SkillScan] = {}
    if not LIBRARY_DIR.exists():
        return scans
    for child in sorted(LIBRARY_DIR.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and (child / "SKILL.md").exists():
            scans[child.name] = SkillScan(
                name=child.name,
                path=child.resolve(),
                system=False,
                metadata=read_skill_metadata(child, child.name),
            )
    return scans


def scan_codex_skills() -> dict[str, SkillScan]:
    scans: dict[str, SkillScan] = {}
    if not CODEX_SKILLS_DIR.exists():
        return scans
    for child in sorted(CODEX_SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.name == ".system":
            for system_child in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                if system_child.is_dir() and (system_child / "SKILL.md").exists():
                    key = system_child.name
                    scans[key] = SkillScan(
                        name=key,
                        path=system_child.resolve(),
                        system=True,
                        metadata=read_skill_metadata(system_child, key),
                    )
            continue
        if (child / "SKILL.md").exists():
            scans[child.name] = SkillScan(
                name=child.name,
                path=child.resolve(),
                system=False,
                metadata=read_skill_metadata(child, child.name),
            )
    return scans


def merge_skill_entry(
    existing: dict[str, Any] | None,
    name: str,
    metadata: dict[str, Any],
    *,
    library_path: Path | None,
    codex_path: Path | None,
    enabled: bool,
    system: bool,
    managed: bool,
    source: dict[str, Any] | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    existing = dict(existing or {})
    existing.setdefault("name", name)
    existing["name"] = name
    existing["title"] = metadata.get("title") or existing.get("title") or name
    existing["description"] = metadata.get("description") or existing.get("description") or ""
    existing["category"] = existing.get("category") or "未分类"
    existing.setdefault("tags", [])
    existing.setdefault("dependencies", [])
    existing.setdefault("notes", "")
    existing["lifecycle"] = normalize_skill_lifecycle(existing.get("lifecycle"))
    existing["libraryPath"] = str(library_path) if library_path else ""
    existing["codexPath"] = str(codex_path) if codex_path else ""
    existing["enabled"] = enabled
    existing["system"] = system
    existing["managed"] = managed and not system
    existing["status"] = status
    existing["skillMdPath"] = metadata.get("skillMdPath", "")
    existing["skillMdPreview"] = metadata.get("skillMdPreview", existing.get("skillMdPreview", ""))
    existing["frontmatter"] = metadata.get("frontmatter", existing.get("frontmatter", {}))
    existing["lastSyncedAt"] = now_iso()
    if source is not None or not existing.get("source"):
        existing["source"] = source or {"type": "project-library"}
    return existing


def sync_registry(*, adopt_extra: bool = True, save: bool = True) -> dict[str, Any]:
    with REGISTRY_LOCK:
        if adopt_extra or save:
            ensure_dirs()
        registry = load_registry()
        skills: dict[str, dict[str, Any]] = registry["skills"]
        library = scan_library_skills()
        active = scan_codex_skills()

        for name, scan in library.items():
            active_scan = active.get(name)
            source = skills.get(name, {}).get("source") or {"type": "project-library", "path": str(scan.path)}
            skills[name] = merge_skill_entry(
                skills.get(name),
                name,
                scan.metadata,
                library_path=scan.path,
                codex_path=active_scan.path if active_scan else None,
                enabled=active_scan is not None,
                system=False,
                managed=True,
                source=source,
            )

        for name, scan in active.items():
            if scan.system:
                skills[name] = merge_skill_entry(
                    skills.get(name),
                    name,
                    scan.metadata,
                    library_path=None,
                    codex_path=scan.path,
                    enabled=True,
                    system=True,
                    managed=False,
                    source={"type": "codex-system", "path": str(scan.path)},
                )
                continue

            library_path = safe_child(LIBRARY_DIR, name)
            if not library_path.exists():
                if not adopt_extra:
                    source = skills.get(name, {}).get("source") or {
                        "type": "codex-home-extra",
                        "path": str(scan.path),
                    }
                    skills[name] = merge_skill_entry(
                        skills.get(name),
                        name,
                        scan.metadata,
                        library_path=None,
                        codex_path=scan.path,
                        enabled=True,
                        system=False,
                        managed=False,
                        source=source,
                    )
                    continue
                copytree_clean(scan.path, library_path)
                append_audit(
                    "adopt-extra-skill",
                    {"skill": name, "from": str(scan.path), "to": str(library_path)},
                )
            metadata = read_skill_metadata(library_path, name)
            source = skills.get(name, {}).get("source") or {
                "type": "codex-home-adopted",
                "path": str(scan.path),
                "adoptedAt": now_iso(),
            }
            skills[name] = merge_skill_entry(
                skills.get(name),
                name,
                metadata,
                library_path=library_path,
                codex_path=scan.path,
                enabled=True,
                system=False,
                managed=True,
                source=source,
            )

        known_names = set(skills)
        for name in known_names:
            entry = skills[name]
            if entry.get("system"):
                if name not in active:
                    entry["enabled"] = False
                    entry["status"] = "missing"
                continue
            library_path = safe_child(LIBRARY_DIR, name)
            codex_path = safe_child(CODEX_SKILLS_DIR, name)
            if not library_path.exists() and not codex_path.exists():
                entry["enabled"] = False
                entry["managed"] = bool(entry.get("managed"))
                entry["status"] = "missing"
                entry["lastSyncedAt"] = now_iso()

        if save:
            save_registry(registry)
        return registry


def read_registry_state() -> dict[str, Any]:
    return sync_registry(adopt_extra=False, save=False)


def registry_view(registry: dict[str, Any]) -> dict[str, Any]:
    skills = [dict(item) for item in registry.get("skills", {}).values()]
    skills.sort(key=lambda item: (not item.get("enabled", False), item.get("category", ""), item.get("name", "")))
    usage_stats = usage_stats_service.read_stats()
    usage_by_name = {item.get("name"): item for item in usage_stats.get("entries", []) if item.get("name")}
    audit_lifecycle = audit_lifecycle_by_skill()
    for skill in skills:
        usage_item = usage_by_name.get(skill.get("name"))
        skill["usage"] = usage_stats_service.skill_summary(usage_item)
        lifecycle = merge_skill_lifecycle(skill.get("lifecycle"), audit_lifecycle.get(str(skill.get("name") or "")))
        lifecycle["disabledSeconds"] = disabled_duration_seconds(lifecycle, enabled=bool(skill.get("enabled")))
        skill["lifecycle"] = lifecycle
    localized_count = len(
        [
            s
            for s in skills
            if isinstance(s.get("localized"), dict)
            and str(s["localized"].get("zhName") or "").strip()
            and str(s["localized"].get("zhTrigger") or "").strip()
        ]
    )
    return {
        "version": registry.get("version", 1),
        "updatedAt": registry.get("updatedAt"),
        "categories": registry.get("categories", DEFAULT_CATEGORIES),
        "skills": skills,
        "paths": {
            "projectRoot": str(BASE_DIR),
            "skillsRepo": str(SKILLS_REPO_DIR),
            "library": str(LIBRARY_DIR),
            "database": str(SKILLS_DB_FILE),
            "remote": SKILLS_REPO_URL,
            "codexHome": str(CODEX_HOME),
            "codexSkills": str(CODEX_SKILLS_DIR),
            "sessions": str(CODEX_SESSIONS_DIR),
            "archivedSessions": str(CODEX_ARCHIVED_SESSIONS_DIR),
        },
        "codex": codex_health(),
        "stats": {
            "total": len(skills),
            "enabled": len([s for s in skills if s.get("enabled")]),
            "managed": len([s for s in skills if s.get("managed")]),
            "system": len([s for s in skills if s.get("system")]),
            "missing": len([s for s in skills if s.get("status") == "missing"]),
            "unclassified": len([s for s in skills if (s.get("category") or "未分类") == "未分类"]),
            "localized": localized_count,
            "unlocalized": len(skills) - localized_count,
        },
        "usageStats": usage_stats_service.summary(usage_stats),
        "versioning": skill_version_pending_state(),
    }


def codex_health() -> dict[str, Any]:
    command = resolve_codex_command()
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "version": "", "error": str(exc)}
    output = (completed.stdout or completed.stderr or "").strip()
    return {
        "available": completed.returncode == 0,
        "version": output,
        "error": "" if completed.returncode == 0 else output,
        "command": command,
    }


def resolve_codex_command() -> str:
    if os.name == "nt":
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        for entry in path_entries:
            if not entry:
                continue
            for filename in ("codex.cmd", "codex.exe", "codex.ps1"):
                candidate = Path(entry) / filename
                if candidate.exists():
                    return str(candidate)
    return shutil.which("codex") or "codex"


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value or ""))


def guess_source_language(*values: str) -> str:
    text = " ".join(value for value in values if value).strip()
    if not text:
        return "unknown"
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    if cjk_count and latin_count:
        return "mixed"
    if cjk_count:
        return "zh"
    if latin_count:
        return "en"
    return "unknown"


def skill_classification_candidates(
    registry: dict[str, Any],
    *,
    names: list[str] | None = None,
    force: bool = False,
    include_system: bool = True,
) -> list[dict[str, Any]]:
    requested = {safe_skill_name(name) for name in names} if names else None
    candidates: list[dict[str, Any]] = []
    for name, entry in registry.get("skills", {}).items():
        if requested is not None and name not in requested:
            continue
        if entry.get("status") == "missing":
            continue
        if entry.get("system") and not include_system:
            continue
        if not force and entry.get("category") and entry.get("category") != "未分类":
            continue
        preview = str(entry.get("skillMdPreview") or "")
        candidates.append(
            {
                "name": name,
                "title": entry.get("title") or name,
                "description": entry.get("description") or "",
                "frontmatter": entry.get("frontmatter") or {},
                "preview": preview[:CLASSIFY_PREVIEW_CHARS],
                "currentCategory": entry.get("category") or "未分类",
                "currentTags": entry.get("tags") or [],
                "currentDependencies": entry.get("dependencies") or [],
            }
        )
    candidates.sort(key=lambda item: item["name"].lower())
    return candidates


def classification_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "skills": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                    "required": ["name", "category", "tags", "dependencies", "notes"],
                },
            }
        },
        "required": ["skills"],
    }


def build_classification_prompt(categories: list[str], skills: list[dict[str, Any]]) -> str:
    category_text = "、".join(categories)
    payload = json.dumps({"categories": categories, "skills": skills}, ensure_ascii=False, indent=2)
    return f"""你是 Codex skills 管理器的本地分类器。请仅根据输入的 skill 元数据识别分类，不要读取文件、不要调用工具、不要改写项目。

可用分类：{category_text}

分类要求：
- 每个 skill 必须返回一条结果，name 必须与输入完全一致。
- category 优先使用已有分类；确实不合适时可以创建一个简短中文新分类，但不要使用“未分类”。
- tags 返回 1-5 个简短中文标签。
- dependencies 只填写从描述中能明确看出的其它 skill 名称；不确定则返回空数组。
- notes 用一句中文说明分类依据，控制在 80 字以内。
- 只输出符合 schema 的 JSON，不要输出 Markdown。

输入：
{payload}
"""


def parse_classification_output(text: str) -> dict[str, Any]:
    clean = text.strip()
    if not clean:
        raise ApiError("Codex 分类没有返回内容。", HTTPStatus.INTERNAL_SERVER_ERROR)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.S)
        if not match:
            raise ApiError("Codex 分类返回内容不是 JSON。", HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ApiError(f"Codex 分类 JSON 解析失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def run_codex_classification(skills: list[dict[str, Any]], categories: list[str]) -> dict[str, Any]:
    health = codex_health()
    if not health["available"]:
        raise ApiError(f"本地 codex 不可用：{health.get('error') or 'codex --version 失败'}")

    command = str(health.get("command") or resolve_codex_command())
    prompt = build_classification_prompt(categories, skills)
    with tempfile.TemporaryDirectory(prefix="codex-skill-classify-") as tmp_dir:
        schema_path = Path(tmp_dir) / "schema.json"
        output_path = Path(tmp_dir) / "classification.json"
        write_json(schema_path, classification_schema())
        cmd = [
            command,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLASSIFY_TIMEOUT_SECONDS,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        output_text = ""
        if output_path.exists():
            output_text = output_path.read_text(encoding="utf-8-sig", errors="replace")
        elif stdout:
            output_text = stdout
        if completed.returncode != 0:
            detail = "\n".join(part for part in [stdout, stderr] if part).strip()
            raise ApiError(f"Codex 分类失败：{detail or completed.returncode}", HTTPStatus.INTERNAL_SERVER_ERROR)
        return parse_classification_output(output_text)


def apply_classification_result(
    registry: dict[str, Any],
    result: dict[str, Any],
    *,
    force: bool = False,
    reason: str = "manual",
) -> dict[str, Any]:
    by_name = registry.get("skills", {})
    changed: list[str] = []
    skipped: list[str] = []
    for item in result.get("skills", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in by_name:
            skipped.append(name or "<empty>")
            continue
        entry = by_name[name]
        if not force and entry.get("category") and entry.get("category") != "未分类":
            skipped.append(name)
            continue
        category = str(item.get("category") or "").strip() or "未分类"
        if category == "未分类":
            skipped.append(name)
            continue
        entry["category"] = category
        if category not in registry["categories"]:
            registry["categories"].append(category)
        tags = normalize_list(item.get("tags"))[:5]
        if tags:
            entry["tags"] = tags
        dependencies = normalize_list(item.get("dependencies"))
        if dependencies:
            entry["dependencies"] = dependencies
        note = str(item.get("notes") or "").strip()
        if note:
            previous = str(entry.get("notes") or "").strip()
            marker = f"[自动分类] {note}"
            entry["notes"] = marker if not previous else previous if marker in previous else f"{previous}\n{marker}"
        entry["autoClassifiedAt"] = now_iso()
        entry["autoClassification"] = {
            "category": category,
            "tags": tags,
            "dependencies": dependencies,
            "reason": note,
            "source": "local-codex",
            "mode": reason,
        }
        entry["updatedAt"] = now_iso()
        changed.append(name)
    return {"changed": changed, "skipped": skipped}


def classify_skills(
    body: dict[str, Any] | None = None,
    *,
    registry: dict[str, Any] | None = None,
    names: list[str] | None = None,
    force: bool = False,
    reason: str = "manual",
    save: bool = True,
) -> dict[str, Any]:
    body = body or {}
    if not AUTO_CLASSIFY_ENABLED and not normalize_bool(body.get("enabled"), default=False):
        return {
            "message": "自动分类已通过 CODEX_SKILL_AUTO_CLASSIFY 关闭。",
            "classified": [],
            "skipped": [],
            "state": registry_view(registry or read_registry_state()),
        }

    if "force" in body:
        force = normalize_bool(body.get("force"), default=force)
    if names is None and body.get("names") is not None:
        names = normalize_list(body.get("names"))
    include_system = normalize_bool(body.get("includeSystem"), default=True)

    owns_registry = registry is None
    if registry is None:
        registry = sync_registry(adopt_extra=False, save=False)
    categories = list(registry.get("categories") or DEFAULT_CATEGORIES)
    candidates = skill_classification_candidates(
        registry,
        names=names,
        force=force,
        include_system=include_system,
    )
    baseline_classification = {
        item["name"]: {
            "category": registry.get("skills", {}).get(item["name"], {}).get("category"),
        }
        for item in candidates
    }
    if not candidates:
        view = registry_view(registry)
        return {"message": "没有需要自动分类的技能。", "classified": [], "skipped": [], "errors": [], "state": view}

    classified: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for index in range(0, len(candidates), CLASSIFY_BATCH_SIZE):
        batch = candidates[index : index + CLASSIFY_BATCH_SIZE]
        try:
            result = run_codex_classification(batch, categories)
            applied = apply_classification_result(registry, result, force=force, reason=reason)
            classified.extend(applied["changed"])
            skipped.extend(applied["skipped"])
            categories = list(registry.get("categories") or DEFAULT_CATEGORIES)
        except (ApiError, subprocess.SubprocessError, OSError) as exc:
            names_text = ", ".join(item["name"] for item in batch)
            errors.append(f"{names_text}: {exc}")

    if classified and save:
        registry, applied, merge_skipped = merge_classification_registry(
            registry,
            classified,
            force=force,
            baseline=baseline_classification,
        )
        classified = applied
        skipped.extend(merge_skipped)
    if save and (classified or errors):
        append_audit(
            "auto-classify-skills",
            {"skills": classified, "skipped": skipped, "errors": errors, "reason": reason, "force": force},
        )
    message_parts = []
    if classified:
        message_parts.append(f"已自动分类 {len(classified)} 个技能。")
    if skipped:
        message_parts.append(f"跳过 {len(skipped)} 个技能。")
    if errors:
        message_parts.append(f"失败 {len(errors)} 批。")
    if owns_registry and not classified and save:
        registry = sync_registry(adopt_extra=False, save=False)
    return {
        "message": "".join(message_parts) or "没有分类变更。",
        "classified": classified,
        "skipped": skipped,
        "errors": errors,
        "state": registry_view(registry),
    }


def auto_classify_registry_after_change(
    registry: dict[str, Any],
    *,
    names: list[str] | None = None,
    reason: str,
) -> dict[str, Any]:
    if not AUTO_CLASSIFY_ENABLED:
        return {"classified": [], "skipped": [], "errors": []}
    try:
        result = classify_skills(registry=registry, names=names, force=False, reason=reason, save=True)
        result.pop("state", None)
        return result
    except Exception as exc:  # noqa: BLE001
        append_audit("auto-classify-skills-failed", {"skills": names or [], "error": str(exc), "reason": reason})
        return {"classified": [], "skipped": [], "errors": [str(exc)]}


def skill_localization_candidates(
    registry: dict[str, Any],
    *,
    names: list[str] | None = None,
    force: bool = False,
    include_system: bool = True,
    only_english: bool = False,
) -> list[dict[str, Any]]:
    requested = {safe_skill_name(name) for name in names} if names else None
    candidates: list[dict[str, Any]] = []
    for name, entry in registry.get("skills", {}).items():
        if requested is not None and name not in requested:
            continue
        if entry.get("status") == "missing":
            continue
        if entry.get("system") and not include_system:
            continue

        localized = entry.get("localized") if isinstance(entry.get("localized"), dict) else {}
        has_localized = bool(
            str(localized.get("zhName") or "").strip()
            and str(localized.get("zhTrigger") or "").strip()
        )
        if not force and has_localized:
            continue

        title = str(entry.get("title") or name).strip()
        description = str(entry.get("description") or "").strip()
        preview = str(entry.get("skillMdPreview") or "")
        source_language = guess_source_language(name, title, description, preview[:800])
        title_or_name_has_cjk = has_cjk(title) or has_cjk(name)
        if only_english and source_language == "zh" and title_or_name_has_cjk:
            continue

        candidates.append(
            {
                "name": name,
                "title": title or name,
                "description": description,
                "frontmatter": entry.get("frontmatter") or {},
                "preview": preview[:LOCALIZE_PREVIEW_CHARS],
                "category": entry.get("category") or "未分类",
                "tags": entry.get("tags") or [],
                "currentLocalized": localized,
                "sourceLanguage": source_language,
            }
        )
    candidates.sort(key=lambda item: item["name"].lower())
    return candidates


def localization_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "skills": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "zhName": {"type": "string"},
                        "zhTrigger": {"type": "string"},
                        "sourceLanguage": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["name", "zhName", "zhTrigger", "sourceLanguage", "notes"],
                },
            }
        },
        "required": ["skills"],
    }


def build_localization_prompt(skills: list[dict[str, Any]]) -> str:
    payload = json.dumps({"skills": skills}, ensure_ascii=False, indent=2)
    return f"""你是 Codex skills 管理器的本地中文本地化助手。请仅根据输入的 skill 元数据生成中文可读名称和中文触发条件，不要读取文件、不要调用工具、不要改写项目。

输出要求：
- 每个 skill 必须返回一条结果，name 必须与输入完全一致。
- zhName 是面向中文用户快速扫描的名称，建议 4-14 个汉字；可以保留必要英文产品名，例如 OpenAI、Gradle、PDF、Obsidian。
- zhTrigger 是中文触发条件摘要，用一句话说明“什么时候应该使用这个 skill”，建议 40-120 字。
- 如果原文已经是中文或中英混合，保留核心术语并补足更顺口的中文表达；不要硬翻专有名词。
- 不要把 zhName 写成目录名，不要改变原始 skill 名称。
- notes 用一句中文说明生成依据，控制在 80 字以内。
- sourceLanguage 从输入判断，优先返回 en、zh、mixed、unknown 之一。
- 只输出符合 schema 的 JSON，不要输出 Markdown。

输入：
{payload}
"""


def parse_localization_output(text: str) -> dict[str, Any]:
    clean = text.strip()
    if not clean:
        raise ApiError("Codex 中文本地化没有返回内容。", HTTPStatus.INTERNAL_SERVER_ERROR)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.S)
        if not match:
            raise ApiError("Codex 中文本地化返回内容不是 JSON。", HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ApiError(f"Codex 中文本地化 JSON 解析失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def run_codex_localization(skills: list[dict[str, Any]]) -> dict[str, Any]:
    health = codex_health()
    if not health["available"]:
        raise ApiError(f"本地 codex 不可用：{health.get('error') or 'codex --version 失败'}")

    command = str(health.get("command") or resolve_codex_command())
    prompt = build_localization_prompt(skills)
    with tempfile.TemporaryDirectory(prefix="codex-skill-localize-") as tmp_dir:
        schema_path = Path(tmp_dir) / "schema.json"
        output_path = Path(tmp_dir) / "localization.json"
        write_json(schema_path, localization_schema())
        cmd = [
            command,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LOCALIZE_TIMEOUT_SECONDS,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        output_text = ""
        if output_path.exists():
            output_text = output_path.read_text(encoding="utf-8-sig", errors="replace")
        elif stdout:
            output_text = stdout
        if completed.returncode != 0:
            detail = "\n".join(part for part in [stdout, stderr] if part).strip()
            raise ApiError(f"Codex 中文本地化失败：{detail or completed.returncode}", HTTPStatus.INTERNAL_SERVER_ERROR)
        return parse_localization_output(output_text)


def apply_localization_result(
    registry: dict[str, Any],
    result: dict[str, Any],
    *,
    force: bool = False,
    reason: str = "manual",
) -> dict[str, Any]:
    by_name = registry.get("skills", {})
    changed: list[str] = []
    skipped: list[str] = []
    for item in result.get("skills", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in by_name:
            skipped.append(name or "<empty>")
            continue
        entry = by_name[name]
        current = entry.get("localized") if isinstance(entry.get("localized"), dict) else {}
        if not force and str(current.get("zhName") or "").strip() and str(current.get("zhTrigger") or "").strip():
            skipped.append(name)
            continue
        zh_name = str(item.get("zhName") or "").strip()
        zh_trigger = str(item.get("zhTrigger") or "").strip()
        if not zh_name or not zh_trigger:
            skipped.append(name)
            continue
        notes = str(item.get("notes") or "").strip()
        source_language = str(item.get("sourceLanguage") or "").strip() or guess_source_language(
            str(entry.get("title") or name),
            str(entry.get("description") or ""),
        )
        entry["localized"] = {
            "zhName": zh_name,
            "zhTrigger": zh_trigger,
            "sourceLanguage": source_language,
            "notes": notes,
            "source": "local-codex",
            "mode": reason,
            "generatedAt": now_iso(),
        }
        entry["localizedAt"] = entry["localized"]["generatedAt"]
        entry["updatedAt"] = now_iso()
        changed.append(name)
    return {"changed": changed, "skipped": skipped}


def localize_skills(
    body: dict[str, Any] | None = None,
    *,
    registry: dict[str, Any] | None = None,
    names: list[str] | None = None,
    force: bool = False,
    reason: str = "manual",
    save: bool = True,
) -> dict[str, Any]:
    body = body or {}
    if not AUTO_LOCALIZE_ENABLED and not normalize_bool(body.get("enabled"), default=False):
        return {
            "message": "中文本地化已通过 CODEX_SKILL_AUTO_LOCALIZE 关闭。",
            "localized": [],
            "skipped": [],
            "errors": [],
            "state": registry_view(registry or read_registry_state()),
        }

    if "force" in body:
        force = normalize_bool(body.get("force"), default=force)
    if names is None and body.get("names") is not None:
        names = normalize_list(body.get("names"))
    include_system = normalize_bool(body.get("includeSystem"), default=True)
    only_english = normalize_bool(body.get("onlyEnglish"), default=False)

    owns_registry = registry is None
    if registry is None:
        registry = sync_registry(adopt_extra=False, save=False)
    candidates = skill_localization_candidates(
        registry,
        names=names,
        force=force,
        include_system=include_system,
        only_english=only_english,
    )
    baseline_localization = {
        item["name"]: stable_json(registry.get("skills", {}).get(item["name"], {}).get("localized"))
        for item in candidates
    }
    if not candidates:
        view = registry_view(registry)
        return {
            "message": "没有需要生成中文名称和触发条件的技能。",
            "localized": [],
            "skipped": [],
            "errors": [],
            "state": view,
        }

    localized: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for index in range(0, len(candidates), LOCALIZE_BATCH_SIZE):
        batch = candidates[index : index + LOCALIZE_BATCH_SIZE]
        try:
            result = run_codex_localization(batch)
            applied = apply_localization_result(registry, result, force=force, reason=reason)
            localized.extend(applied["changed"])
            skipped.extend(applied["skipped"])
        except (ApiError, subprocess.SubprocessError, OSError) as exc:
            names_text = ", ".join(item["name"] for item in batch)
            errors.append(f"{names_text}: {exc}")

    if localized and save:
        registry, applied, merge_skipped = merge_localization_registry(
            registry,
            localized,
            force=force,
            baseline=baseline_localization,
        )
        localized = applied
        skipped.extend(merge_skipped)
    if save and (localized or errors):
        append_audit(
            "auto-localize-skills",
            {"skills": localized, "skipped": skipped, "errors": errors, "reason": reason, "force": force},
        )
    message_parts = []
    if localized:
        message_parts.append(f"已生成 {len(localized)} 个技能的中文信息。")
    if skipped:
        message_parts.append(f"跳过 {len(skipped)} 个技能。")
    if errors:
        message_parts.append(f"失败 {len(errors)} 批。")
    if owns_registry and not localized and save:
        registry = sync_registry(adopt_extra=False, save=False)
    return {
        "message": "".join(message_parts) or "没有中文信息变更。",
        "localized": localized,
        "skipped": skipped,
        "errors": errors,
        "state": registry_view(registry),
    }


def auto_localize_registry_after_change(
    registry: dict[str, Any],
    *,
    names: list[str] | None = None,
    reason: str,
) -> dict[str, Any]:
    if not AUTO_LOCALIZE_ENABLED:
        return {"localized": [], "skipped": [], "errors": []}
    try:
        result = localize_skills(registry=registry, names=names, force=False, reason=reason, save=True)
        result.pop("state", None)
        return result
    except Exception as exc:  # noqa: BLE001
        append_audit("auto-localize-skills-failed", {"skills": names or [], "error": str(exc), "reason": reason})
        return {"localized": [], "skipped": [], "errors": [str(exc)]}


def parse_github_install_url(url: str, default_ref: str) -> tuple[str, str, str | None] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    ref = default_ref or "main"
    repo_path = ""
    if len(parts) > 2:
        if parts[2] in {"tree", "blob"}:
            if len(parts) < 4:
                return f"{owner}/{repo}", ref, None
            ref = parts[3]
            repo_path = "/".join(parts[4:])
        else:
            repo_path = "/".join(parts[2:])
    return f"{owner}/{repo}", ref, repo_path or None


def github_install_target(
    *,
    source: str,
    repo: str,
    paths: list[str],
    ref: str,
) -> tuple[str, str, list[str]] | None:
    effective_ref = ref or "main"
    if source:
        parsed = parse_github_install_url(source, effective_ref)
        if not parsed:
            return None
        repo_slug, url_ref, url_path = parsed
        return repo_slug, url_ref, list(paths) if paths else ([url_path] if url_path else [])

    if not repo:
        return None
    if "://" in repo:
        parsed = parse_github_install_url(repo, effective_ref)
        if not parsed:
            return None
        repo_slug, url_ref, url_path = parsed
        return repo_slug, url_ref, list(paths) if paths else ([url_path] if url_path else [])

    repo_parts = [part for part in repo.split("/") if part]
    if len(repo_parts) != 2:
        return None
    return f"{repo_parts[0]}/{repo_parts[1]}", effective_ref, list(paths)


def normalize_repo_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return ""
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ApiError("repo 内路径必须是相对目录，不能包含 . 或 ..。")
    return "/".join(parts)


def repo_path_basename(path: str) -> str:
    return normalize_repo_path(path).rsplit("/", 1)[-1]


def github_api_json(url: str, *, not_found: Any = None) -> Any:
    headers = {"User-Agent": GITHUB_API_USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return not_found
        raise ApiError(f"读取 GitHub 失败：HTTP {exc.code}", HTTPStatus.BAD_GATEWAY) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"读取 GitHub 失败：{exc.reason}", HTTPStatus.BAD_GATEWAY) from exc
    return json.loads(payload)


def github_contents(repo: str, path: str, ref: str) -> Any:
    encoded_path = urllib.parse.quote(normalize_repo_path(path), safe="/")
    encoded_ref = urllib.parse.quote(ref or "main", safe="")
    suffix = f"/{encoded_path}" if encoded_path else ""
    url = f"https://api.github.com/repos/{repo}/contents{suffix}?ref={encoded_ref}"
    return github_api_json(url, not_found=None)


def github_latest_commit_for_path(repo: str, path: str, ref: str) -> dict[str, Any]:
    encoded_path = urllib.parse.quote(normalize_repo_path(path), safe="/")
    encoded_ref = urllib.parse.quote(ref or "main", safe="")
    url = f"https://api.github.com/repos/{repo}/commits?path={encoded_path}&sha={encoded_ref}&per_page=1"
    commits = github_api_json(url, not_found=[])
    if not isinstance(commits, list) or not commits:
        return {}
    commit = commits[0] if isinstance(commits[0], dict) else {}
    commit_body = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    author = commit_body.get("author") if isinstance(commit_body.get("author"), dict) else {}
    committer = commit_body.get("committer") if isinstance(commit_body.get("committer"), dict) else {}
    return {
        "sha": commit.get("sha") or "",
        "shortSha": str(commit.get("sha") or "")[:7],
        "date": author.get("date") or committer.get("date") or "",
        "message": str(commit_body.get("message") or "").splitlines()[0] if commit_body.get("message") else "",
        "url": commit.get("html_url") or "",
    }


def github_tree_map(repo: str, ref: str) -> dict[str, dict[str, Any]]:
    encoded_ref = urllib.parse.quote(ref or "main", safe="")
    url = f"https://api.github.com/repos/{repo}/git/trees/{encoded_ref}?recursive=1"
    payload = github_api_json(url, not_found={})
    tree = payload.get("tree") if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(tree, list):
        return result
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = normalize_repo_path(str(item.get("path") or ""))
        if path:
            result[path] = item
    return result


def github_repo_metadata(repo: str) -> dict[str, Any]:
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/", 1))
    url = f"https://api.github.com/repos/{encoded_repo}"
    payload = github_api_json(url, not_found={})
    if not isinstance(payload, dict):
        return {}
    return {
        "pushedAt": payload.get("pushed_at") or "",
        "updatedAt": payload.get("updated_at") or "",
        "defaultBranch": payload.get("default_branch") or "",
    }


def decode_github_file(item: Any) -> bytes:
    if not isinstance(item, dict) or item.get("type") != "file":
        raise ApiError("GitHub 返回的目标不是文件。")
    content = str(item.get("content") or "")
    encoding = str(item.get("encoding") or "").lower()
    if encoding != "base64" or not content:
        raise ApiError("GitHub 文件内容不可直接读取。")
    return base64.b64decode(content.replace("\n", "").encode("ascii"))


def git_blob_sha(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("utf-8")
    return hashlib.sha1(header + payload).hexdigest()


def text_from_bytes(payload: bytes) -> str:
    return payload.decode("utf-8-sig", errors="replace").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def github_directory_has_skill_md(repo: str, path: str, ref: str) -> bool:
    contents = github_contents(repo, path, ref)
    if not isinstance(contents, list):
        return False
    return any(
        str(item.get("type") or "") == "file" and str(item.get("name") or "").lower() == "skill.md"
        for item in contents
    )


def discover_github_skill_paths_from_api(repo: str, path: str, ref: str) -> tuple[list[str], bool]:
    normalized = normalize_repo_path(path)
    contents = github_contents(repo, normalized, ref)
    if not isinstance(contents, list):
        return [normalized], False
    if any(
        str(item.get("type") or "") == "file" and str(item.get("name") or "").lower() == "skill.md"
        for item in contents
    ):
        return [normalized], False

    child_dirs = [
        str(item.get("path") or "").strip()
        for item in contents
        if str(item.get("type") or "") == "dir" and str(item.get("path") or "").strip()
    ]
    discovered: list[str] = []
    for child_path in child_dirs[:MAX_GITHUB_PARENT_SKILL_SCAN]:
        if github_directory_has_skill_md(repo, child_path, ref):
            discovered.append(normalize_repo_path(child_path))
    return discovered or [normalized], bool(discovered)


def archive_skill_paths_from_names(names: list[str], path: str) -> tuple[list[str], bool]:
    normalized = normalize_repo_path(path)
    target_skill_file = f"{normalized}/SKILL.md".lower()
    direct_children: set[str] = set()
    has_skill_md = False
    for name in names:
        parts = [part for part in name.split("/") if part]
        if len(parts) < 3:
            continue
        relative = "/".join(parts[1:])
        if relative.lower() == target_skill_file:
            has_skill_md = True
            continue
        prefix = f"{normalized}/"
        if not relative.lower().startswith(prefix.lower()) or not relative.lower().endswith("/skill.md"):
            continue
        child_parts = relative[len(prefix) :].split("/")
        if len(child_parts) == 2 and child_parts[1].lower() == "skill.md":
            direct_children.add(f"{normalized}/{child_parts[0]}")
    if has_skill_md:
        return [normalized], False
    discovered = sorted(direct_children)
    return discovered or [normalized], bool(discovered)


def discover_github_skill_paths_from_archive(repo: str, path: str, ref: str) -> tuple[list[str], bool]:
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/", 1))
    encoded_ref = urllib.parse.quote(ref or "main", safe="")
    request = urllib.request.Request(
        f"https://codeload.github.com/{encoded_repo}/zip/{encoded_ref}",
        headers={"User-Agent": GITHUB_API_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"读取 GitHub 源码归档失败：HTTP {exc.code}", HTTPStatus.BAD_GATEWAY) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"读取 GitHub 源码归档失败：{exc.reason}", HTTPStatus.BAD_GATEWAY) from exc
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return archive_skill_paths_from_names(archive.namelist(), path)
    except zipfile.BadZipFile as exc:
        raise ApiError("GitHub 源码归档格式无效。", HTTPStatus.BAD_GATEWAY) from exc


def discover_github_skill_paths(repo: str, path: str, ref: str) -> tuple[list[str], bool, str]:
    try:
        paths, is_parent = discover_github_skill_paths_from_api(repo, path, ref)
        return paths, is_parent, "github-api"
    except ApiError as exc:
        if "HTTP 403" not in exc.message:
            raise
        paths, is_parent = discover_github_skill_paths_from_archive(repo, path, ref)
        return paths, is_parent, "github-archive"


def expand_github_install_paths(
    target: tuple[str, str, list[str]] | None,
) -> tuple[list[str], dict[str, Any]]:
    if not target:
        return [], {}
    repo, ref, paths = target
    if not paths:
        return [], {}

    expanded: list[str] = []
    expanded_from: list[str] = []
    discovery_methods: set[str] = set()
    for path in paths:
        discovered, is_parent, discovery_method = discover_github_skill_paths(repo, path, ref)
        expanded.extend(discovered)
        discovery_methods.add(discovery_method)
        if is_parent:
            expanded_from.append(normalize_repo_path(path))

    deduped: list[str] = []
    seen: set[str] = set()
    for path in expanded:
        normalized = normalize_repo_path(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)

    details: dict[str, Any] = {"paths": deduped, "discovery": ",".join(sorted(discovery_methods))}
    if expanded_from:
        installable: list[str] = []
        skipped_existing: list[str] = []
        for path in deduped:
            skill_name = safe_skill_name(repo_path_basename(path))
            if safe_child(LIBRARY_DIR, skill_name).exists():
                skipped_existing.append(skill_name)
                continue
            installable.append(path)
        if not installable and skipped_existing:
            raise ApiError(f"目录下技能均已安装：{', '.join(skipped_existing)}")
        details["expandedFrom"] = expanded_from
        details["skippedExisting"] = skipped_existing
        details["paths"] = installable
        return installable, details
    return deduped, details


def github_repo_from_source_value(source_value: str, ref: str) -> tuple[str, str, str | None] | None:
    value = str(source_value or "").strip()
    if not value:
        return None
    if "://" in value:
        return parse_github_install_url(value, ref)
    parts = [part for part in value.split("/") if part]
    if len(parts) == 2 and all(re.match(r"^[\w.-]+$", part) for part in parts):
        return f"{parts[0]}/{parts[1]}", ref or "main", None
    return None


def github_source_for_skill(name: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    if source.get("type") != "github":
        return None
    ref = str(source.get("ref") or "main").strip() or "main"
    parsed = github_repo_from_source_value(str(source.get("source") or ""), ref)
    if not parsed:
        return None
    repo, parsed_ref, url_path = parsed
    ref = str(source.get("ref") or parsed_ref or "main").strip() or "main"
    paths = normalize_list(source.get("path") or source.get("paths"))
    if not paths and url_path:
        paths = [url_path]
    normalized_paths = [normalize_repo_path(path) for path in paths if normalize_repo_path(path)]

    skill_path = ""
    for path in normalized_paths:
        if repo_path_basename(path) == name:
            skill_path = path
            break
    if not skill_path and len(normalized_paths) == 1:
        skill_path = normalized_paths[0]
    if not skill_path and url_path and repo_path_basename(url_path) == name:
        skill_path = normalize_repo_path(url_path)
    if not skill_path:
        return {
            "name": name,
            "repo": repo,
            "ref": ref,
            "path": "",
            "source": str(source.get("source") or ""),
            "error": "无法从来源信息推导该 skill 的 repo 内路径。",
        }
    return {
        "name": name,
        "repo": repo,
        "ref": ref,
        "path": skill_path,
        "source": str(source.get("source") or ""),
        "installedAt": str(source.get("installedAt") or ""),
    }


def read_local_skill_file_bytes(name: str, entry: dict[str, Any]) -> tuple[bytes, str]:
    candidates: list[Path] = []
    if entry.get("libraryPath"):
        candidates.append(Path(str(entry["libraryPath"])) / "SKILL.md")
    if entry.get("skillMdPath"):
        candidates.append(Path(str(entry["skillMdPath"])))
    for candidate in candidates:
        resolved = candidate.resolve()
        if not is_relative_to(resolved, LIBRARY_DIR) or not resolved.exists() or not resolved.is_file():
            continue
        return resolved.read_bytes(), str(resolved)
    raise ApiError(f"未找到 {name} 的本地 SKILL.md。", HTTPStatus.NOT_FOUND)


def compare_github_skill(
    name: str,
    entry: dict[str, Any],
    *,
    include_text: bool = False,
    include_commit: bool = False,
    remote_tree: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = github_source_for_skill(name, entry)
    if not source:
        return {"name": name, "status": "not-github", "hasUpdate": False, "error": "该技能不是 GitHub 来源。"}
    if source.get("error"):
        return {**source, "status": "unknown", "hasUpdate": False}

    remote_skill_md = f"{source['path']}/SKILL.md"
    local_bytes, local_path = read_local_skill_file_bytes(name, entry)
    local_sha = git_blob_sha(local_bytes)
    remote_item: dict[str, Any] | None = None
    remote_bytes: bytes | None = None
    if remote_tree is not None:
        remote_item = remote_tree.get(remote_skill_md)
        if not remote_item:
            return {
                **source,
                "status": "missing-remote",
                "hasUpdate": False,
                "localPath": local_path,
                "localSha": local_sha,
                "remotePath": remote_skill_md,
                "error": "GitHub 上未找到该技能的 SKILL.md。",
            }
    if remote_tree is None or include_text:
        remote_contents = github_contents(source["repo"], remote_skill_md, source["ref"])
        if not isinstance(remote_contents, dict):
            return {
                **source,
                "status": "missing-remote",
                "hasUpdate": False,
                "localPath": local_path,
                "localSha": local_sha,
                "remotePath": remote_skill_md,
                "error": "GitHub 上未找到该技能的 SKILL.md。",
            }
        remote_item = remote_contents
        remote_bytes = decode_github_file(remote_contents)
    remote_sha = str((remote_item or {}).get("sha") or (git_blob_sha(remote_bytes) if remote_bytes is not None else ""))
    same = local_sha == remote_sha if remote_bytes is None else local_bytes == remote_bytes
    commit = github_latest_commit_for_path(source["repo"], remote_skill_md, source["ref"]) if include_commit else {}
    result = {
        **source,
        "status": "up-to-date" if same else "updated",
        "hasUpdate": not same,
        "localPath": local_path,
        "localSha": local_sha,
        "remotePath": remote_skill_md,
        "remoteSha": remote_sha,
        "remoteUrl": (remote_item or {}).get("html_url") or f"https://github.com/{source['repo']}/blob/{source['ref']}/{remote_skill_md}",
        "remoteUpdatedAt": commit.get("date") or "",
        "remoteCommit": commit,
    }
    if include_text:
        result["localText"] = text_from_bytes(local_bytes)
        result["remoteText"] = text_from_bytes(remote_bytes or b"")
    return result


def github_sources_view() -> dict[str, Any]:
    registry = read_registry_state()
    groups: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(registry.get("skills", {}).items(), key=lambda item: item[0].lower()):
        source = github_source_for_skill(name, entry)
        if not source:
            continue
        key = f"{source.get('repo') or 'unknown'}@{source.get('ref') or 'main'}"
        group = groups.setdefault(
            key,
            {
                "key": key,
                "repo": source.get("repo") or "",
                "ref": source.get("ref") or "main",
                "source": source.get("source") or "",
                "url": f"https://github.com/{source.get('repo')}" if source.get("repo") else "",
                "skills": [],
                "_entries": [],
            },
        )
        group["_entries"].append({"name": name, "entry": entry, "source": source})

    for group in groups.values():
        remote_tree: dict[str, dict[str, Any]] = {}
        tree_error = ""
        if group.get("repo"):
            try:
                group["remote"] = github_repo_metadata(str(group.get("repo") or ""))
                remote_tree = github_tree_map(str(group.get("repo") or ""), str(group.get("ref") or "main"))
            except (ApiError, OSError, ValueError, json.JSONDecodeError) as exc:
                tree_error = str(exc)
        for bundled in group.pop("_entries", []):
            name = bundled["name"]
            entry = bundled["entry"]
            source = bundled["source"]
            if tree_error:
                item = {**source, "status": "error", "hasUpdate": False, "error": tree_error}
            else:
                try:
                    item = compare_github_skill(name, entry, remote_tree=remote_tree)
                except (ApiError, OSError, ValueError, json.JSONDecodeError) as exc:
                    item = {**source, "status": "error", "hasUpdate": False, "error": str(exc)}
            item["title"] = entry.get("title") or name
            item["enabled"] = bool(entry.get("enabled"))
            item["category"] = entry.get("category") or "未分类"
            group["skills"].append(item)

    repositories = list(groups.values())
    for group in repositories:
        skills = group.get("skills", [])
        group["counts"] = {
            "total": len(skills),
            "updated": len([item for item in skills if item.get("status") == "updated"]),
            "ok": len([item for item in skills if item.get("status") == "up-to-date"]),
            "issues": len([item for item in skills if item.get("status") not in {"updated", "up-to-date"}]),
        }
    repositories.sort(key=lambda item: str(item.get("repo") or "").lower())
    return {"checkedAt": now_iso(), "repositories": repositories}


def read_skill_remote_diff(name: str) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = read_registry_state()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)
    comparison = compare_github_skill(name, entry, include_text=True)
    if comparison.get("status") in {"not-github", "unknown", "missing-remote", "error"}:
        raise ApiError(str(comparison.get("error") or "该技能无法读取 GitHub 远端内容。"), HTTPStatus.BAD_REQUEST)
    diff_lines = difflib.unified_diff(
        str(comparison.pop("localText", "")).splitlines(),
        str(comparison.pop("remoteText", "")).splitlines(),
        fromfile=f"local/skills/{name}/SKILL.md",
        tofile=f"github/{comparison['repo']}/{comparison['remotePath']}",
        lineterm="",
    )
    return {
        "skill": name,
        "comparison": comparison,
        "diff": "\n".join(diff_lines),
    }


def install_from_local_path(source_path: Path, preferred_name: str | None = None) -> list[str]:
    source_path = source_path.expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise ApiError(f"本地路径不存在或不是目录：{source_path}")

    candidates: list[Path]
    if (source_path / "SKILL.md").exists():
        candidates = [source_path]
    else:
        candidates = [p for p in source_path.iterdir() if p.is_dir() and (p / "SKILL.md").exists()]
    if not candidates:
        raise ApiError("本地路径下没有找到包含 SKILL.md 的技能目录。")

    installed: list[str] = []
    for candidate in candidates:
        name = safe_skill_name(preferred_name or candidate.name)
        dest = safe_child(LIBRARY_DIR, name)
        copytree_clean(candidate, dest)
        installed.append(name)
    return installed


def run_installer(body: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    health = codex_health()
    if not health["available"]:
        raise ApiError(f"本地 codex 不可用：{health.get('error') or 'codex --version 失败'}")
    if not INSTALLER_SCRIPT.exists():
        raise ApiError(f"Codex skill-installer 脚本不存在：{INSTALLER_SCRIPT}")

    before = {p.name for p in LIBRARY_DIR.iterdir() if p.is_dir()} if LIBRARY_DIR.exists() else set()
    cmd = [sys.executable, str(INSTALLER_SCRIPT), "--dest", str(LIBRARY_DIR)]

    source = str(body.get("source") or body.get("url") or "").strip()
    name = str(body.get("name") or "").strip()
    method = str(body.get("method") or "auto").strip()
    install_details: dict[str, Any] = {}

    target = github_install_target(source=source, repo="", paths=[], ref="") if source else None
    if not target:
        raise ApiError("请提供 GitHub tree 地址，例如 https://github.com/iOfficeAI/OfficeCLI/tree/main/skills。")
    repo, ref, paths = target
    if "/tree/" not in urlparse(source).path or not paths:
        raise ApiError("GitHub 地址必须指向包含技能目录的 tree 路径，例如 https://github.com/iOfficeAI/OfficeCLI/tree/main/skills。")

    expanded_paths, install_details = expand_github_install_paths((repo, ref, paths))
    if not expanded_paths:
        raise ApiError("GitHub tree 地址未指向可安装的技能目录。")
    install_details.update({"repo": repo, "ref": ref, "paths": expanded_paths})
    cmd.extend(["--repo", repo, "--path", *expanded_paths, "--ref", ref])
    if name:
        safe_skill_name(name)
        cmd.extend(["--name", name])
    if method in {"auto", "download", "git"}:
        cmd.extend(["--method", method])

    completed = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        raise ApiError(
            "安装失败：\n" + "\n".join(part for part in [stdout, stderr] if part),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    after = {p.name for p in LIBRARY_DIR.iterdir() if p.is_dir()}
    installed = sorted(after - before)
    if name and name in after and name not in installed:
        installed.append(name)
    if not installed:
        installed = sorted(after)
    details = {"mode": "github", "command": cmd, "stdout": stdout, "stderr": stderr, "codex": health}
    details.update(install_details)
    return installed, details


def install_skill(body: dict[str, Any]) -> dict[str, Any]:
    installed, details = run_installer(body)
    registry = sync_registry()
    source_payload = {
        "type": details["mode"],
        "source": body.get("source") or body.get("url") or body.get("repo") or "",
        "path": details.get("paths") or "",
        "ref": details.get("ref") or "",
        "installedAt": now_iso(),
        "via": "local-codex-skill-installer",
    }
    if details.get("expandedFrom"):
        source_payload["expandedFrom"] = details["expandedFrom"]
    if details.get("skippedExisting"):
        source_payload["skippedExisting"] = details["skippedExisting"]
    category = str(body.get("category") or "").strip()
    notes = str(body.get("notes") or "").strip()
    for name in installed:
        entry = registry["skills"].get(name)
        if not entry:
            continue
        entry["source"] = source_payload
        if category:
            entry["category"] = category
            if category not in registry["categories"]:
                registry["categories"].append(category)
        if notes:
            entry["notes"] = notes
    save_registry(registry)
    append_audit("install-skill", {"skills": installed, "source": source_payload})
    classification = {"classified": [], "skipped": [], "errors": []}
    if not category:
        classification = auto_classify_registry_after_change(registry, names=installed, reason="install")
    localization = auto_localize_registry_after_change(registry, names=installed, reason="install")
    return {
        "installed": installed,
        "details": details,
        "classification": classification,
        "localization": localization,
        "state": registry_view(registry),
    }


def enable_skill(name: str) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = sync_registry()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)
    if entry.get("system"):
        return {"message": "系统技能已由 Codex 管理。", "state": registry_view(registry)}
    source = safe_child(LIBRARY_DIR, name)
    dest = safe_child(CODEX_SKILLS_DIR, name)
    if not source.exists():
        raise ApiError("项目技能库中缺少该技能目录，无法启用。")
    if dest.exists():
        registry = sync_registry()
        return {"message": "该技能已经存在于 .codex/skills。", "state": registry_view(registry)}
    copytree_clean(source, dest)
    toggle_at = now_iso()
    append_audit("enable-skill", {"skill": name, "from": str(source), "to": str(dest)}, event_time=toggle_at)
    registry = sync_registry()
    record_skill_lifecycle(registry, name, "enable", toggle_at, {"from": str(source), "to": str(dest)})
    save_registry(registry)
    return {"message": "已启用。重启 Codex 后新技能会被会话加载。", "state": registry_view(registry)}


def disable_skill(name: str) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = sync_registry()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)
    if entry.get("system"):
        raise ApiError("系统技能不能在这里停用。")
    dest = safe_child(CODEX_SKILLS_DIR, name)
    if dest.exists():
        if not is_relative_to(dest, CODEX_SKILLS_DIR):
            raise ApiError("目标路径越界，已拒绝操作。", HTTPStatus.FORBIDDEN)
        shutil.rmtree(dest)
        toggle_at = now_iso()
        append_audit("disable-skill", {"skill": name, "removed": str(dest)}, event_time=toggle_at)
        registry = sync_registry()
        record_skill_lifecycle(registry, name, "disable", toggle_at, {"removed": str(dest)})
        save_registry(registry)
        return {"message": "已停用。项目技能库中的副本仍然保留。", "state": registry_view(registry)}
    registry = sync_registry()
    return {"message": "已停用。项目技能库中的副本仍然保留。", "state": registry_view(registry)}


def update_skill(name: str, body: dict[str, Any]) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = sync_registry()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)

    if "category" in body:
        category = str(body.get("category") or "未分类").strip() or "未分类"
        entry["category"] = category
        if category not in registry["categories"]:
            registry["categories"].append(category)
    if "tags" in body:
        entry["tags"] = normalize_list(body.get("tags"))
    if "dependencies" in body:
        entry["dependencies"] = normalize_list(body.get("dependencies"))
    if "notes" in body:
        entry["notes"] = str(body.get("notes") or "")
    if "localized" in body and isinstance(body.get("localized"), dict):
        localized_body = body["localized"]
        current_localized = entry.get("localized") if isinstance(entry.get("localized"), dict) else {}
        zh_name = str(localized_body.get("zhName") or "").strip()
        zh_trigger = str(localized_body.get("zhTrigger") or "").strip()
        if zh_name or zh_trigger:
            current_localized.update(
                {
                    "zhName": zh_name,
                    "zhTrigger": zh_trigger,
                    "sourceLanguage": str(localized_body.get("sourceLanguage") or current_localized.get("sourceLanguage") or "").strip()
                    or guess_source_language(str(entry.get("title") or name), str(entry.get("description") or "")),
                    "notes": str(localized_body.get("notes") or current_localized.get("notes") or "").strip(),
                    "source": "manual",
                    "mode": "manual-edit",
                    "generatedAt": str(current_localized.get("generatedAt") or now_iso()),
                    "updatedAt": now_iso(),
                }
            )
            entry["localized"] = current_localized
            entry["localizedAt"] = current_localized["updatedAt"]
        else:
            entry.pop("localized", None)
            entry.pop("localizedAt", None)
    if "source" in body and isinstance(body.get("source"), dict):
        current = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        current.update(body["source"])
        entry["source"] = current

    entry["updatedAt"] = now_iso()
    save_registry(registry)
    append_audit("update-skill", {"skill": name})
    return {"message": "已保存。", "state": registry_view(registry)}


def read_skill_markdown(name: str) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = read_registry_state()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)

    candidates: list[Path] = []
    if entry.get("skillMdPath"):
        candidates.append(Path(str(entry["skillMdPath"])))
    for key in ("libraryPath", "codexPath"):
        if entry.get(key):
            candidates.append(Path(str(entry[key])) / "SKILL.md")

    for candidate in candidates:
        skill_md = candidate if candidate.name.lower() == "skill.md" else candidate / "SKILL.md"
        resolved = skill_md.resolve()
        if not (is_relative_to(resolved, LIBRARY_DIR) or is_relative_to(resolved, CODEX_SKILLS_DIR)):
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            markdown = resolved.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
        except OSError as exc:
            raise ApiError(f"读取 SKILL.md 失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc
        return {"skill": name, "path": str(resolved), "markdown": markdown}

    preview = str(entry.get("skillMdPreview") or "")
    if preview:
        return {"skill": name, "path": str(entry.get("skillMdPath") or ""), "markdown": preview}
    raise ApiError("未找到该技能的 SKILL.md。", HTTPStatus.NOT_FOUND)


def git_command(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(SKILLS_REPO_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def normalize_git_path(path: str) -> str:
    return path.strip().strip('"').replace("\\", "/")


def managed_version_paths() -> list[str]:
    return ["skills", "codex-skills-manager.sqlite3", ".gitignore"]


def changed_managed_paths() -> list[dict[str, str]]:
    completed = git_command(
        ["status", "--porcelain=v1", "--untracked-files=all", "--", *managed_version_paths()],
        timeout=30,
    )
    if completed.returncode != 0:
        raise ApiError(f"读取 Git 状态失败：{(completed.stderr or completed.stdout).strip()}")
    changes: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path_text = line[3:] if len(line) > 3 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        path_text = normalize_git_path(path_text)
        if not path_text:
            continue
        changes.append({"status": status.strip() or "M", "path": path_text})
    return changes


def changed_paths_for_skill(name: str) -> list[dict[str, str]]:
    changes = changed_managed_paths()
    wanted = safe_skill_name(name)
    return [change for change in changes if skill_from_managed_path(change["path"]) in {wanted, "registry", "repository"}]


def skill_from_managed_path(path: str) -> str:
    normalized = normalize_git_path(path)
    prefix = "skills/"
    if normalized.startswith(prefix):
        rest = normalized[len(prefix) :]
        return rest.split("/", 1)[0] if rest else ""
    if normalized == "codex-skills-manager.sqlite3":
        return "registry"
    if normalized == ".gitignore":
        return "repository"
    return ""


def summarize_managed_changes(changes: list[dict[str, str]]) -> dict[str, Any]:
    skills: dict[str, dict[str, Any]] = {}
    registry_changed = False
    repository_changed = False
    for change in changes:
        skill = skill_from_managed_path(change["path"])
        if skill == "registry":
            registry_changed = True
            continue
        if skill == "repository":
            repository_changed = True
            continue
        if not skill:
            continue
        item = skills.setdefault(skill, {"name": skill, "files": 0, "statuses": set()})
        item["files"] += 1
        item["statuses"].add(change["status"])
    items = []
    for item in skills.values():
        items.append(
            {
                "name": item["name"],
                "files": item["files"],
                "statuses": sorted(item["statuses"]),
            }
        )
    items.sort(key=lambda item: item["name"].lower())
    return {
        "skills": items,
        "registryChanged": registry_changed,
        "repositoryChanged": repository_changed,
        "changedFiles": len(changes),
    }


def managed_changes_signature(changes: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for change in changes:
        path = normalize_git_path(change["path"])
        file_path = (SKILLS_REPO_DIR / path).resolve()
        stat_text = "missing"
        if is_relative_to(file_path, SKILLS_REPO_DIR) and file_path.exists() and file_path.is_file():
            stat = file_path.stat()
            stat_text = f"{stat.st_size}:{stat.st_mtime_ns}"
        parts.append(f"{change['status']} {path} {stat_text}")
    return "\n".join(parts)


def skill_version_pending_state() -> dict[str, Any]:
    if not SKILL_VERSIONING_ENABLED:
        return {
            "enabled": False,
            "pending": False,
            "message": "技能版本自动提交已关闭。",
            "delaySeconds": SKILL_VERSION_COMMIT_DELAY_SECONDS,
            "scanIntervalSeconds": SKILL_VERSION_SCAN_INTERVAL_SECONDS,
        }
    try:
        changes = changed_managed_paths()
    except (ApiError, subprocess.SubprocessError, OSError) as exc:
        return {
            "enabled": True,
            "pending": False,
            "error": str(exc),
            "delaySeconds": SKILL_VERSION_COMMIT_DELAY_SECONDS,
            "scanIntervalSeconds": SKILL_VERSION_SCAN_INTERVAL_SECONDS,
            "committing": SKILL_VERSION_COMMITTING.is_set(),
        }
    summary = summarize_managed_changes(changes)
    with SKILL_VERSION_LOCK:
        pending_since = SKILL_VERSION_PENDING_SINCE
    age_seconds = None
    due_at = ""
    if pending_since:
        now = datetime.now(timezone.utc)
        age_seconds = max(0, int((now - pending_since).total_seconds()))
        due_at = (pending_since + timedelta(seconds=SKILL_VERSION_COMMIT_DELAY_SECONDS)).astimezone().isoformat(timespec="seconds")
    return {
        "enabled": True,
        "pending": bool(changes),
        "pendingSince": pending_since.astimezone().isoformat(timespec="seconds") if pending_since else "",
        "ageSeconds": age_seconds,
        "dueAt": due_at,
        "delaySeconds": SKILL_VERSION_COMMIT_DELAY_SECONDS,
        "scanIntervalSeconds": SKILL_VERSION_SCAN_INTERVAL_SECONDS,
        "committing": SKILL_VERSION_COMMITTING.is_set(),
        **summary,
    }


def build_skill_version_commit_message(summary: dict[str, Any]) -> str:
    skills = [item["name"] for item in summary.get("skills", []) if item.get("name")]
    registry_changed = bool(summary.get("registryChanged"))
    repository_changed = bool(summary.get("repositoryChanged"))
    if skills:
        title_names = ", ".join(skills[:3])
        if len(skills) > 3:
            title_names += f" 等 {len(skills)} 个"
        title = f"chore(skills): 记录 {title_names} 的技能版本"
    elif registry_changed:
        title = "chore(skills): 记录技能登记信息版本"
    else:
        title = "chore(skills): 记录技能版本"
    lines = [title, ""]
    if skills:
        lines.append("变更技能：")
        for item in summary.get("skills", []):
            lines.append(f"- {item['name']}（{item['files']} 个文件）")
    if registry_changed:
        if skills:
            lines.append("")
        lines.append("包含技能登记信息更新。")
    if repository_changed:
        if skills or registry_changed:
            lines.append("")
        lines.append("包含 skills 仓库配置文件更新。")
    return "\n".join(lines).strip()


def commit_managed_skill_changes(*, reason: str = "auto") -> dict[str, Any]:
    if not SKILL_VERSIONING_ENABLED:
        return {"committed": False, "message": "技能版本自动提交已关闭。"}
    if SKILL_VERSION_COMMITTING.is_set():
        return {"committed": False, "message": "已有技能版本提交正在执行。"}
    SKILL_VERSION_COMMITTING.set()
    try:
        changes = changed_managed_paths()
        if not changes:
            with SKILL_VERSION_LOCK:
                global SKILL_VERSION_PENDING_SINCE, SKILL_VERSION_LAST_SIGNATURE
                SKILL_VERSION_PENDING_SINCE = None
                SKILL_VERSION_LAST_SIGNATURE = ""
            return {"committed": False, "message": "没有技能变更需要提交。"}

        summary = summarize_managed_changes(changes)
        add_result = git_command(["add", "--", *managed_version_paths()], timeout=60)
        if add_result.returncode != 0:
            raise ApiError(f"暂存技能变更失败：{(add_result.stderr or add_result.stdout).strip()}")

        diff_result = git_command(["diff", "--cached", "--quiet", "--", *managed_version_paths()], timeout=60)
        if diff_result.returncode == 0:
            with SKILL_VERSION_LOCK:
                SKILL_VERSION_PENDING_SINCE = None
                SKILL_VERSION_LAST_SIGNATURE = ""
            return {"committed": False, "message": "技能变更暂存后没有实际差异。", **summary}
        if diff_result.returncode not in {0, 1}:
            raise ApiError(f"检查暂存差异失败：{(diff_result.stderr or diff_result.stdout).strip()}")

        message = build_skill_version_commit_message(summary)
        commit_result = git_command(["commit", "-m", message, "--", *managed_version_paths()], timeout=120)
        if commit_result.returncode != 0:
            raise ApiError(f"提交技能版本失败：{(commit_result.stderr or commit_result.stdout).strip()}")

        commit_hash_result = git_command(["rev-parse", "--short", "HEAD"], timeout=30)
        commit_hash = commit_hash_result.stdout.strip() if commit_hash_result.returncode == 0 else ""
        push_result = push_skill_repository()
        append_audit(
            "auto-commit-skill-version",
            {
                "reason": reason,
                "commit": commit_hash,
                "skills": [item["name"] for item in summary.get("skills", [])],
                "registryChanged": summary.get("registryChanged", False),
                "repositoryChanged": summary.get("repositoryChanged", False),
                "changedFiles": summary.get("changedFiles", 0),
                "push": push_result,
            },
        )
        with SKILL_VERSION_LOCK:
            SKILL_VERSION_PENDING_SINCE = None
            SKILL_VERSION_LAST_SIGNATURE = ""
        return {
            "committed": True,
            "commit": commit_hash,
            "message": message,
            "push": push_result,
            **summary,
        }
    finally:
        SKILL_VERSION_COMMITTING.clear()


def inspect_skill_version_changes() -> dict[str, Any]:
    try:
        changes = changed_managed_paths()
    except Exception as exc:  # noqa: BLE001
        append_audit("skill-version-status-failed", {"error": str(exc)})
        return {}
    signature = managed_changes_signature(changes)
    now = datetime.now(timezone.utc)
    with SKILL_VERSION_LOCK:
        global SKILL_VERSION_PENDING_SINCE, SKILL_VERSION_LAST_SIGNATURE
        if not changes:
            SKILL_VERSION_PENDING_SINCE = None
            SKILL_VERSION_LAST_SIGNATURE = ""
            return {"pending": False}
        if signature != SKILL_VERSION_LAST_SIGNATURE or SKILL_VERSION_PENDING_SINCE is None:
            SKILL_VERSION_LAST_SIGNATURE = signature
            SKILL_VERSION_PENDING_SINCE = now
            append_audit(
                "skill-version-change-detected",
                {
                    "changedFiles": len(changes),
                    **summarize_managed_changes(changes),
                },
            )
            return {"pending": True, "reset": True}
        age = (now - SKILL_VERSION_PENDING_SINCE).total_seconds()
    if age >= SKILL_VERSION_COMMIT_DELAY_SECONDS:
        try:
            return commit_managed_skill_changes(reason="quiet-period")
        except Exception as exc:  # noqa: BLE001
            append_audit("auto-commit-skill-version-failed", {"error": str(exc)})
            return {"committed": False, "error": str(exc)}
    return {"pending": True}


def skill_version_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(SKILL_VERSION_SCAN_INTERVAL_SECONDS):
        inspect_skill_version_changes()


def git_log_exists() -> bool:
    completed = git_command(["rev-parse", "--verify", "HEAD"], timeout=30)
    return completed.returncode == 0


def current_git_branch() -> str:
    result = git_command(["branch", "--show-current"], timeout=30)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def push_skill_repository() -> dict[str, Any]:
    if not SKILL_VERSION_AUTO_PUSH_ENABLED:
        return {"pushed": False, "message": "技能仓库自动 push 已关闭。"}
    remote = git_command(["remote", "get-url", "origin"], timeout=30)
    if remote.returncode != 0 or not remote.stdout.strip():
        return {"pushed": False, "message": "技能仓库未配置 origin。"}
    branch = current_git_branch()
    result = git_command(["push", "-u", "origin", branch], timeout=180)
    if result.returncode != 0:
        return {"pushed": False, "branch": branch, "error": (result.stderr or result.stdout).strip()}
    return {"pushed": True, "branch": branch, "remote": remote.stdout.strip()}


def parse_git_numstat(lines: list[str]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], normalize_git_path(parts[2])
        files.append(
            {
                "path": path,
                "skill": skill_from_managed_path(path),
                "added": None if added == "-" else int(added or 0),
                "deleted": None if deleted == "-" else int(deleted or 0),
            }
        )
    return files


def read_file_for_diff(path: str) -> str:
    normalized = normalize_git_path(path)
    candidate = (SKILLS_REPO_DIR / normalized).resolve()
    if not is_relative_to(candidate, SKILLS_REPO_DIR) or not candidate.exists() or not candidate.is_file():
        return ""
    try:
        return candidate.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def read_pending_diff(path: str) -> dict[str, Any]:
    normalized = normalize_git_path(path)
    changes = changed_managed_paths()
    if normalized not in {change["path"] for change in changes}:
        raise ApiError("该文件没有未提交变更。", HTTPStatus.NOT_FOUND)
    if not any(normalized == managed or normalized.startswith(f"{managed}/") for managed in managed_version_paths()):
        raise ApiError("只能查看受管 skills 仓库文件的 diff。", HTTPStatus.FORBIDDEN)

    status = next((change["status"] for change in changes if change["path"] == normalized), "")
    diff_text = ""
    if status[:1] != "?" and status[1:2] != " ":
        staged = git_command(["diff", "--cached", "--", normalized], timeout=60)
        if staged.returncode != 0:
            raise ApiError(f"读取暂存 diff 失败：{(staged.stderr or staged.stdout).strip()}")
        diff_text = staged.stdout
    if status[:1] != " " and "??" not in status:
        result = git_command(["diff", "--", normalized], timeout=60)
        if result.returncode != 0:
            raise ApiError(f"读取 diff 失败：{(result.stderr or result.stdout).strip()}")
        diff_text = f"{diff_text}\n{result.stdout}".strip()
    if not diff_text:
        content = read_file_for_diff(normalized)
        header = f"diff --git a/{normalized} b/{normalized}\nnew file mode 100644\n--- /dev/null\n+++ b/{normalized}\n"
        body = "".join(f"+{line}\n" for line in content.splitlines())
        diff_text = header + body
    return {
        "path": normalized,
        "status": status,
        "skill": skill_from_managed_path(normalized),
        "diff": diff_text,
    }


def read_skill_pending_changes(name: str) -> dict[str, Any]:
    name = safe_skill_name(name)
    changes = changed_paths_for_skill(name)
    files = []
    for change in changes:
        path = change["path"]
        skill = skill_from_managed_path(path)
        file_path = (SKILLS_REPO_DIR / path).resolve()
        size = file_path.stat().st_size if is_relative_to(file_path, SKILLS_REPO_DIR) and file_path.exists() and file_path.is_file() else None
        files.append(
            {
                "path": path,
                "status": change["status"],
                "skill": skill,
                "size": size,
            }
        )
    return {
        "skill": name,
        "files": files,
        "count": len(files),
        "summary": summarize_managed_changes(changes),
    }


def read_skill_history(name: str, *, limit: int = 40) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = read_registry_state()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)
    if not git_log_exists():
        return {
            "skill": name,
            "versions": [],
            "pending": skill_version_pending_state(),
            "pendingChanges": read_skill_pending_changes(name),
            "message": "当前仓库还没有提交记录，首次提交后会显示版本历史。",
        }

    paths = [f"skills/{name}", "codex-skills-manager.sqlite3"]
    completed = git_command(
        [
            "log",
            f"--max-count={max(1, min(limit, 200))}",
            "--date=iso-strict",
            "--pretty=format:__COMMIT__%x1f%H%x1f%h%x1f%ad%x1f%an%x1f%s",
            "--numstat",
            "--",
            *paths,
        ],
        timeout=60,
    )
    if completed.returncode != 0:
        raise ApiError(f"读取技能版本历史失败：{(completed.stderr or completed.stdout).strip()}")

    versions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    numstat_lines: list[str] = []
    for line in completed.stdout.splitlines():
        if line.startswith("__COMMIT__"):
            if current is not None:
                current["files"] = parse_git_numstat(numstat_lines)
                versions.append(current)
            parts = line.removeprefix("__COMMIT__").split("\x1f")
            while len(parts) < 6:
                parts.append("")
            current = {
                "hash": parts[1],
                "shortHash": parts[2],
                "date": parts[3],
                "author": parts[4],
                "subject": parts[5],
            }
            numstat_lines = []
            continue
        if current is not None and line.strip():
            numstat_lines.append(line)
    if current is not None:
        current["files"] = parse_git_numstat(numstat_lines)
        versions.append(current)

    return {
        "skill": name,
        "versions": versions,
        "pending": skill_version_pending_state(),
        "pendingChanges": read_skill_pending_changes(name),
    }


def read_version_status() -> dict[str, Any]:
    return {
        "versioning": skill_version_pending_state(),
        "historyAvailable": git_log_exists(),
        "repository": {
            "path": str(SKILLS_REPO_DIR),
            "skills": str(LIBRARY_DIR),
            "database": str(SKILLS_DB_FILE),
            "remote": SKILLS_REPO_URL,
            "autoPush": SKILL_VERSION_AUTO_PUSH_ENABLED,
        },
    }


def repository_config_view() -> dict[str, Any]:
    remote = git_command(["remote", "get-url", "origin"], timeout=30)
    branch = current_git_branch() if (SKILLS_REPO_DIR / ".git").exists() else ""
    return {
        "skillsRepoUrl": SKILLS_REPO_URL,
        "skillsRepoDir": str(SKILLS_REPO_DIR),
        "skillsDir": str(LIBRARY_DIR),
        "database": str(SKILLS_DB_FILE),
        "remote": remote.stdout.strip() if remote.returncode == 0 else "",
        "branch": branch,
        "exists": SKILLS_REPO_DIR.exists(),
        "git": (SKILLS_REPO_DIR / ".git").exists(),
        "autoPush": SKILL_VERSION_AUTO_PUSH_ENABLED,
        "versioning": skill_version_pending_state(),
    }


def update_repository_config(body: dict[str, Any]) -> dict[str, Any]:
    settings = read_settings()
    repo_url = str(body.get("skillsRepoUrl") or body.get("url") or "").strip()
    repo_dir = str(body.get("skillsRepoDir") or body.get("dir") or "").strip()
    previous_dir = SKILLS_REPO_DIR
    previous_url = SKILLS_REPO_URL
    previous_library = LIBRARY_DIR
    previous_db = SKILLS_DB_FILE
    candidate_settings = dict(settings)
    if repo_url:
        candidate_settings["skillsRepoUrl"] = repo_url
    elif "skillsRepoUrl" in body or "url" in body:
        candidate_settings.pop("skillsRepoUrl", None)
    if repo_dir:
        candidate_settings["skillsRepoDir"] = repo_dir
    elif "skillsRepoDir" in body or "dir" in body:
        candidate_settings.pop("skillsRepoDir", None)
    candidate_dir, _ = repository_values(candidate_settings)
    validate_skills_repository_dir(candidate_dir)
    try:
        apply_repository_settings(candidate_settings)
        ensure_skills_repository()
    except Exception:
        globals()["SKILLS_REPO_DIR"] = previous_dir
        globals()["SKILLS_REPO_URL"] = previous_url
        globals()["LIBRARY_DIR"] = previous_library
        globals()["SKILLS_DB_FILE"] = previous_db
        raise
    write_settings(candidate_settings)
    with SKILL_VERSION_LOCK:
        global SKILL_VERSION_PENDING_SINCE, SKILL_VERSION_LAST_SIGNATURE
        SKILL_VERSION_PENDING_SINCE = None
        SKILL_VERSION_LAST_SIGNATURE = ""
    append_audit("update-skills-repository-config", repository_config_view())
    registry = sync_registry(adopt_extra=False, save=False)
    return {
        "message": "skills 仓库配置已保存。",
        "repository": repository_config_view(),
        "state": registry_view(registry),
    }


def test_repository_config() -> dict[str, Any]:
    ensure_skills_repository()
    commit_result = commit_managed_skill_changes(reason="manual-test")
    if not commit_result.get("committed"):
        push_result = push_skill_repository()
        commit_result["push"] = push_result
    return {
        "message": "skills 仓库测试完成。",
        "repository": repository_config_view(),
        "result": commit_result,
    }


def health_view() -> dict[str, Any]:
    repo = repository_health()
    ok = not repo["errors"]
    return {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "version": "0.1",
        "projectRoot": str(BASE_DIR),
        "skillsRepo": str(SKILLS_REPO_DIR),
        "repository": repo,
        "background": {
            "usageStatsRefreshing": usage_stats_service.is_refreshing(),
            "versionCommitPending": SKILL_VERSION_PENDING_SINCE is not None,
            "versionCommitting": SKILL_VERSION_COMMITTING.is_set(),
        },
        "time": now_iso(),
    }


def settings_view() -> dict[str, Any]:
    return {
        "repository": repository_config_view(),
        "usageStats": usage_stats_service.config(),
        "versioning": {
            "enabled": SKILL_VERSIONING_ENABLED,
            "autoPush": SKILL_VERSION_AUTO_PUSH_ENABLED,
            "delaySeconds": SKILL_VERSION_COMMIT_DELAY_SECONDS,
            "scanIntervalSeconds": SKILL_VERSION_SCAN_INTERVAL_SECONDS,
        },
        "paths": {
            "settings": str(SETTINGS_FILE),
            "usageStats": str(USAGE_STATS_FILE),
            "sessions": str(CODEX_SESSIONS_DIR),
            "archivedSessions": str(CODEX_ARCHIVED_SESSIONS_DIR),
        },
    }


def update_app_settings(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("usageStats") if isinstance(body.get("usageStats"), dict) else body
    usage_stats_service.update_config(payload)
    return {"message": "设置已保存。", **settings_view()}



def read_session_index() -> dict[str, dict[str, Any]]:
    return read_codex_session_index(SESSION_INDEX_FILE)


def session_files(limit: int = 400) -> list[Path]:
    return list_codex_session_files(CODEX_SESSIONS_DIR, CODEX_ARCHIVED_SESSIONS_DIR, limit=limit)


def search_skill_contexts(name: str, query: dict[str, list[str]]) -> dict[str, Any]:
    name = safe_skill_name(name)
    registry = read_registry_state()
    entry = registry["skills"].get(name)
    if not entry:
        raise ApiError("未找到该技能。", HTTPStatus.NOT_FOUND)

    extra_query = (query.get("q") or [""])[0].strip()
    max_files = int((query.get("maxFiles") or ["400"])[0])
    limit = int((query.get("limit") or ["60"])[0])
    keywords = {name.lower()}
    frontmatter_name = str(entry.get("frontmatter", {}).get("name") or "").strip().lower()
    if frontmatter_name:
        keywords.add(frontmatter_name)
    if extra_query:
        keywords.add(extra_query.lower())

    index = read_session_index()
    results: list[dict[str, Any]] = []
    matched_sessions: set[str] = set()
    seen_contexts: set[tuple[str, str, str]] = set()

    for file_path in session_files(limit=max_files):
        if len(results) >= limit:
            break
        dedupe_session_id = session_id_from_path(file_path)
        session_id = dedupe_session_id
        snippets: list[dict[str, Any]] = []
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for line_number, line in enumerate(f, 1):
                    lower_line = line.lower()
                    if not any(keyword in lower_line for keyword in keywords):
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("type") == "session_meta":
                        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                        session_id = str(payload.get("id") or session_id)
                        continue
                    text, role = extract_message_text(item)
                    if not text:
                        continue
                    lower_text = text.lower()
                    match_keyword = next((k for k in keywords if k in lower_text), "")
                    if not match_keyword:
                        continue
                    normalized_text = normalize_context_text(text)
                    dedupe_key = (dedupe_session_id, role, normalized_text)
                    if dedupe_key in seen_contexts:
                        continue
                    seen_contexts.add(dedupe_key)
                    snippets.append(
                        {
                            "line": line_number,
                            "time": item.get("timestamp", ""),
                            "role": role,
                            "roleLabel": context_role_label(role),
                            "keyword": match_keyword,
                            "text": compact_snippet(text, match_keyword),
                        }
                    )
                    if len(snippets) >= 4:
                        break
        except OSError:
            continue

        if not snippets:
            continue
        meta = index.get(session_id, {})
        matched_sessions.add(session_id)
        results.append(
            {
                "sessionId": session_id,
                "title": meta.get("thread_name") or file_path.stem,
                "updatedAt": meta.get("updated_at") or datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                "path": str(file_path),
                "snippets": snippets,
            }
        )

    return {
        "skill": name,
        "query": extra_query,
        "matchedSessionCount": len(matched_sessions),
        "results": results,
        "summary": "仅展示去重后的用户/助手正文，已过滤工具调用、函数输出、DOM 快照、浏览器自动化日志和长 JSON 输出。",
    }


def read_audit(limit: int = 100) -> list[dict[str, Any]]:
    events = read_audit_events(limit=limit)
    events.reverse()
    return events


class Handler(SimpleHTTPRequestHandler):
    server_version = "CodexSkillManager/0.1"

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        request_path = unquote(parsed.path)
        if request_path == "/":
            request_path = "/index.html"
        candidate = (PUBLIC_DIR / request_path.lstrip("/")).resolve()
        if not is_relative_to(candidate, PUBLIC_DIR):
            return str(PUBLIC_DIR / "index.html")
        return str(candidate)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8-sig")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"请求 JSON 不合法：{exc}")

    def handle_api(self, method: str) -> bool:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            return False
        try:
            if method == "GET" and path == "/api/health":
                self.send_json(health_view())
                return True
            if method == "GET" and path == "/api/state":
                self.send_json(registry_view(read_registry_state()))
                return True
            if method == "GET" and path == "/api/audit":
                self.send_json({"events": read_audit()})
                return True
            if method == "GET" and path == "/api/versioning":
                self.send_json(read_version_status())
                return True
            if method == "GET" and path == "/api/settings":
                self.send_json(settings_view())
                return True
            if method == "PUT" and path == "/api/settings":
                self.send_json(update_app_settings(self.read_body()))
                return True
            if method == "GET" and path == "/api/repository":
                self.send_json(repository_config_view())
                return True
            if method == "PUT" and path == "/api/repository":
                self.send_json(update_repository_config(self.read_body()))
                return True
            if method == "POST" and path == "/api/repository/test":
                self.send_json(test_repository_config())
                return True
            if method == "GET" and path == "/api/diff":
                diff_path = (query.get("path") or [""])[0]
                self.send_json(read_pending_diff(diff_path))
                return True
            if method == "GET" and path == "/api/sources/github":
                self.send_json(github_sources_view())
                return True
            if method == "POST" and path == "/api/sync":
                registry = sync_registry()
                classification = auto_classify_registry_after_change(registry, reason="sync")
                localization = auto_localize_registry_after_change(registry, reason="sync")
                self.send_json({"classification": classification, "localization": localization, "state": registry_view(registry)})
                return True
            if method == "POST" and path == "/api/classify":
                self.send_json(classify_skills(self.read_body()))
                return True
            if method == "POST" and path == "/api/localize":
                self.send_json(localize_skills(self.read_body()))
                return True
            if method == "POST" and path == "/api/reviews/usage":
                self.send_json(usage_stats_service.review(self.read_body()))
                return True
            if method == "GET" and path == "/api/usage-stats":
                self.send_json(usage_stats_service.read_stats())
                return True
            if method == "POST" and path == "/api/usage-stats/refresh":
                self.send_json(usage_stats_service.refresh(reason="manual", body=self.read_body()))
                return True
            if method == "POST" and path == "/api/install":
                self.send_json(install_skill(self.read_body()))
                return True

            skill_match = re.match(r"^/api/skills/([^/]+)(?:/(enable|disable|contexts|markdown|history|remote-diff|classify|localize))?$", path)
            if skill_match:
                skill_name = skill_match.group(1)
                action = skill_match.group(2)
                if method == "POST" and action == "enable":
                    self.send_json(enable_skill(skill_name))
                    return True
                if method == "POST" and action == "disable":
                    self.send_json(disable_skill(skill_name))
                    return True
                if method == "PUT" and action is None:
                    self.send_json(update_skill(skill_name, self.read_body()))
                    return True
                if method == "GET" and action == "markdown":
                    self.send_json(read_skill_markdown(skill_name))
                    return True
                if method == "GET" and action == "contexts":
                    self.send_json(search_skill_contexts(skill_name, query))
                    return True
                if method == "GET" and action == "history":
                    limit = int((query.get("limit") or ["40"])[0])
                    self.send_json(read_skill_history(skill_name, limit=limit))
                    return True
                if method == "GET" and action == "remote-diff":
                    self.send_json(read_skill_remote_diff(skill_name))
                    return True
                if method == "POST" and action == "classify":
                    body = self.read_body()
                    body["names"] = [safe_skill_name(skill_name)]
                    body.setdefault("force", True)
                    self.send_json(classify_skills(body, reason="single"))
                    return True
                if method == "POST" and action == "localize":
                    body = self.read_body()
                    body["names"] = [safe_skill_name(skill_name)]
                    body.setdefault("force", True)
                    self.send_json(localize_skills(body, reason="single"))
                    return True

            raise ApiError("接口不存在。", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
            return True
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return True

    def do_GET(self) -> None:
        if self.handle_api("GET"):
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.handle_api("POST"):
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        if self.handle_api("PUT"):
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex skills 本地管理页面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ensure_skills_repository()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    usage_scheduler_stop = threading.Event()
    skill_version_scheduler_stop = threading.Event()
    threading.Thread(
        target=usage_stats_service.scheduler,
        args=(usage_scheduler_stop,),
        name="usage-stats-scheduler",
        daemon=True,
    ).start()
    if usage_stats_service.needs_startup_refresh():
        usage_stats_service.start_refresh_thread("startup")
    if SKILL_VERSIONING_ENABLED:
        inspect_skill_version_changes()
        threading.Thread(
            target=skill_version_scheduler,
            args=(skill_version_scheduler_stop,),
            name="skill-version-scheduler",
            daemon=True,
        ).start()
    print(f"Codex skills manager: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        usage_scheduler_stop.set()
        skill_version_scheduler_stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
