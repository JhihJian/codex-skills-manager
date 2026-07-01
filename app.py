from __future__ import annotations

import argparse
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


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
DEFAULT_USAGE_STALE_DAYS = max(1, int(os.environ.get("CODEX_SKILL_USAGE_STALE_DAYS", "30") or "30"))
USAGE_REVIEW_MAX_FILES = max(1, int(os.environ.get("CODEX_SKILL_USAGE_MAX_FILES", "1000") or "1000"))
USAGE_STATS_DAILY_ENABLED = os.environ.get("CODEX_SKILL_USAGE_DAILY_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
USAGE_STATS_DAILY_HOUR = env_int("CODEX_SKILL_USAGE_DAILY_HOUR", 3, minimum=0, maximum=23)
USAGE_STATS_DAILY_MINUTE = env_int("CODEX_SKILL_USAGE_DAILY_MINUTE", 0, minimum=0, maximum=59)
USAGE_STATS_SCOPE = os.environ.get("CODEX_SKILL_USAGE_STATS_SCOPE", "all").strip().lower()
if USAGE_STATS_SCOPE not in {"enabled", "managed", "all"}:
    USAGE_STATS_SCOPE = "all"
USAGE_STATS_INCLUDE_SYSTEM = os.environ.get("CODEX_SKILL_USAGE_STATS_INCLUDE_SYSTEM", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
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

REGISTRY_LOCK = threading.RLock()
USAGE_STATS_LOCK = threading.RLock()
USAGE_STATS_REFRESH_LOCK = threading.Lock()
USAGE_STATS_REFRESHING = threading.Event()
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


def append_audit(action: str, payload: dict[str, Any]) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {"time": now_iso(), "action": action, **payload}
    with AUDIT_FILE.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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
    usage_stats = read_usage_stats()
    usage_by_name = {item.get("name"): item for item in usage_stats.get("entries", []) if item.get("name")}
    for skill in skills:
        usage_item = usage_by_name.get(skill.get("name"))
        skill["usage"] = skill_usage_summary(usage_item)
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
        "usageStats": usage_stats_summary(usage_stats),
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
        save_registry(registry)
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
        save_registry(registry)
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
    repo = str(body.get("repo") or "").strip()
    paths = normalize_list(body.get("path") or body.get("paths"))
    ref = str(body.get("ref") or "").strip()
    name = str(body.get("name") or "").strip()
    method = str(body.get("method") or "auto").strip()

    if source:
        if re.match(r"^[a-zA-Z]:\\|^\\\\|^/|^\.", source):
            installed = install_from_local_path(Path(source), preferred_name=name or None)
            return installed, {"mode": "local-path", "codex": health}
        cmd.extend(["--url", source])
    elif repo:
        cmd.extend(["--repo", repo])
    else:
        raise ApiError("需要提供 GitHub URL、repo/path 或本地技能目录。")

    for skill_path in paths:
        cmd.extend(["--path", skill_path])
    if ref:
        cmd.extend(["--ref", ref])
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
    return installed, {"mode": "github", "command": cmd, "stdout": stdout, "stderr": stderr, "codex": health}


def install_skill(body: dict[str, Any]) -> dict[str, Any]:
    installed, details = run_installer(body)
    registry = sync_registry()
    source_payload = {
        "type": details["mode"],
        "source": body.get("source") or body.get("url") or body.get("repo") or "",
        "path": body.get("path") or body.get("paths") or "",
        "ref": body.get("ref") or "",
        "installedAt": now_iso(),
        "via": "local-codex-skill-installer",
    }
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
    append_audit("enable-skill", {"skill": name, "from": str(source), "to": str(dest)})
    registry = sync_registry()
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
        append_audit("disable-skill", {"skill": name, "removed": str(dest)})
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
    if repo_url:
        settings["skillsRepoUrl"] = repo_url
    elif "skillsRepoUrl" in body or "url" in body:
        settings.pop("skillsRepoUrl", None)
    if repo_dir:
        settings["skillsRepoDir"] = repo_dir
    elif "skillsRepoDir" in body or "dir" in body:
        settings.pop("skillsRepoDir", None)
    write_settings(settings)
    apply_repository_settings(settings)
    ensure_skills_repository()
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


def read_session_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not SESSION_INDEX_FILE.exists():
        return index
    with SESSION_INDEX_FILE.open("r", encoding="utf-8", errors="replace") as f:
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


def session_files(limit: int = 400) -> list[Path]:
    files: list[Path] = []
    for root in (CODEX_SESSIONS_DIR, CODEX_ARCHIVED_SESSIONS_DIR):
        if root.exists():
            files.extend([p for p in root.rglob("*.jsonl") if p.is_file()])
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def session_id_from_path(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})", path.name)
    return match.group(1) if match else path.stem


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
SKILL_READ_COMMAND_PATTERN = re.compile(
    r"(?i)\b(Get-Content|gc|type|cat|rg|Select-String|sed|read_text|readText|readFile|open)\b"
)
READ_RESOURCE_TOOLS = {"read_mcp_resource"}


def skill_names_from_skill_paths(text: str) -> set[str]:
    normalized = text.replace("/", "\\")
    names: set[str] = set()
    for pattern in SKILL_PATH_PATTERNS:
        for match in pattern.finditer(normalized):
            try:
                names.add(safe_skill_name(match.group(1)))
            except ApiError:
                continue
    return names


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
    tool_name = str(payload.get("name") or "").strip()
    if tool_name in READ_RESOURCE_TOOLS:
        return True
    return bool(SKILL_READ_COMMAND_PATTERN.search(text))


def extract_skill_read_evidence(item: dict[str, Any]) -> tuple[set[str], str]:
    if item.get("type") != "response_item":
        return set(), ""
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload.get("type") != "function_call":
        return set(), ""
    text = function_call_text(payload)
    names = skill_names_from_skill_paths(text)
    if not names or not looks_like_skill_read_call(payload, text):
        return set(), ""
    return names, text


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
    session_id = str(evidence.get("sessionId") or "").strip()
    if session_id:
        bucket.setdefault(f"{kind}Sessions", set()).add(session_id)
    if event_time:
        bucket.setdefault(f"{kind}Days", set()).add(event_time.astimezone().date().isoformat())
    last_key = f"last{kind.title()}At"
    current_last = parse_timestamp(bucket.get(last_key))
    if event_time and (current_last is None or event_time > current_last):
        bucket[last_key] = event_time.isoformat()
    examples = bucket.setdefault(list_key, [])
    if len(examples) < max_examples:
        examples.append(evidence)


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


def review_skill_usage(
    body: dict[str, Any] | None = None,
    *,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = body or {}
    stale_days = parse_positive_int(body.get("staleDays"), DEFAULT_USAGE_STALE_DAYS, minimum=1, maximum=3650)
    max_files = parse_positive_int(body.get("maxFiles"), USAGE_REVIEW_MAX_FILES, minimum=1, maximum=10000)
    scope = str(body.get("scope") or "enabled").strip().lower()
    if scope not in {"enabled", "managed", "all"}:
        scope = "enabled"
    include_system = normalize_bool(body.get("includeSystem"), default=True)

    if registry is None:
        registry = read_registry_state()
    skills = scoped_usage_skills(registry, scope, include_system)
    aliases = build_skill_alias_map({"skills": skills})
    usage: dict[str, dict[str, Any]] = {name: {} for name in skills}
    index = read_session_index()
    scanned_files = 0
    scanned_lines = 0

    for file_path in session_files(limit=max_files):
        scanned_files += 1
        fallback_session_id = session_id_from_path(file_path)
        session_id = fallback_session_id
        meta = index.get(session_id, {})
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for line_number, line in enumerate(f, 1):
                    scanned_lines += 1
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("type") == "session_meta":
                        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                        session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                        meta = index.get(session_id, meta)
                        continue

                    event_time = item.get("timestamp", "")
                    title = meta.get("thread_name") or file_path.stem
                    raw_names, call_text = extract_skill_read_evidence(item)
                    for raw_name in raw_names:
                        name = canonical_skill_name(raw_name, aliases)
                        if not name or name not in usage:
                            continue
                        add_usage_evidence(
                            usage[name],
                            "confirmed",
                            {
                                "type": "skill-file-read",
                                "confidence": "confirmed",
                                "time": event_time,
                                "sessionId": session_id,
                                "title": title,
                                "path": str(file_path),
                                "line": line_number,
                                "snippet": compact_snippet(call_text, raw_name, width=300),
                            },
                        )

                    for name, alias, text in extract_skill_announcements(item, {"skills": skills}):
                        if name not in usage:
                            continue
                        add_usage_evidence(
                            usage[name],
                            "announcement",
                            {
                                "type": "assistant-announcement",
                                "confidence": "supporting",
                                "time": event_time,
                                "sessionId": session_id,
                                "title": title,
                                "path": str(file_path),
                                "line": line_number,
                                "snippet": compact_snippet(text, alias, width=300),
                            },
                        )
        except OSError:
            continue

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
    return {
        "reviewedAt": now_iso(),
        "staleDays": stale_days,
        "scope": scope,
        "includeSystem": include_system,
        "stats": stats,
        "entries": entries,
        "scan": {
            "maxFiles": max_files,
            "scannedFiles": scanned_files,
            "scannedLines": scanned_lines,
            "sessions": str(CODEX_SESSIONS_DIR),
            "archivedSessions": str(CODEX_ARCHIVED_SESSIONS_DIR),
        },
        "evidencePolicy": "只把助手执行过程中的 SKILL.md 读取工具调用计为真实使用证据；助手明确使用声明仅作为辅助证据。已排除 session_meta、developer 技能列表、用户普通提及和上下文关键词命中。",
    }


def default_usage_stats_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "reviewedAt": "",
        "staleDays": DEFAULT_USAGE_STALE_DAYS,
        "scope": USAGE_STATS_SCOPE,
        "includeSystem": USAGE_STATS_INCLUDE_SYSTEM,
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
            "maxFiles": USAGE_REVIEW_MAX_FILES,
            "scannedFiles": 0,
            "scannedLines": 0,
            "sessions": str(CODEX_SESSIONS_DIR),
            "archivedSessions": str(CODEX_ARCHIVED_SESSIONS_DIR),
        },
        "evidencePolicy": "尚未生成使用统计缓存。",
    }


def read_usage_stats() -> dict[str, Any]:
    with USAGE_STATS_LOCK:
        payload = read_json(USAGE_STATS_FILE, default_usage_stats_payload())
    if not isinstance(payload, dict):
        payload = default_usage_stats_payload()
    payload.setdefault("version", 1)
    payload.setdefault("reviewedAt", "")
    payload.setdefault("staleDays", DEFAULT_USAGE_STALE_DAYS)
    payload.setdefault("scope", USAGE_STATS_SCOPE)
    payload.setdefault("includeSystem", USAGE_STATS_INCLUDE_SYSTEM)
    payload.setdefault("stats", {})
    payload.setdefault("entries", [])
    payload.setdefault("scan", {})
    payload.setdefault("evidencePolicy", "")
    return payload


def write_usage_stats(payload: dict[str, Any]) -> None:
    with USAGE_STATS_LOCK:
        value = {"version": 1, **payload}
        write_json(USAGE_STATS_FILE, value)


def skill_usage_summary(item: dict[str, Any] | None) -> dict[str, Any]:
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


def usage_stats_summary(payload: dict[str, Any]) -> dict[str, Any]:
    reviewed_at = str(payload.get("reviewedAt") or "")
    reviewed_dt = parse_timestamp(reviewed_at)
    now = datetime.now(timezone.utc)
    age_hours = None
    if reviewed_dt:
        age_hours = max(0, round((now - reviewed_dt).total_seconds() / 3600, 1))
    return {
        "reviewedAt": reviewed_at,
        "ageHours": age_hours,
        "staleDays": payload.get("staleDays", DEFAULT_USAGE_STALE_DAYS),
        "scope": payload.get("scope") or USAGE_STATS_SCOPE,
        "includeSystem": bool(payload.get("includeSystem", USAGE_STATS_INCLUDE_SYSTEM)),
        "stats": payload.get("stats") or {},
        "scan": payload.get("scan") or {},
        "refreshing": USAGE_STATS_REFRESHING.is_set(),
        "dailyEnabled": USAGE_STATS_DAILY_ENABLED,
        "dailyTime": f"{USAGE_STATS_DAILY_HOUR:02d}:{USAGE_STATS_DAILY_MINUTE:02d}",
    }


def refresh_usage_stats(*, reason: str = "manual", body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not USAGE_STATS_REFRESH_LOCK.acquire(blocking=False):
        payload = read_usage_stats()
        payload["refreshing"] = True
        payload["message"] = "使用统计正在刷新。"
        return payload
    USAGE_STATS_REFRESHING.set()
    try:
        request_body = {
            "staleDays": DEFAULT_USAGE_STALE_DAYS,
            "maxFiles": USAGE_REVIEW_MAX_FILES,
            "scope": USAGE_STATS_SCOPE,
            "includeSystem": USAGE_STATS_INCLUDE_SYSTEM,
        }
        if body:
            request_body.update(body)
        payload = review_skill_usage(request_body)
        payload["reason"] = reason
        write_usage_stats(payload)
        append_audit(
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
        USAGE_STATS_REFRESHING.clear()
        USAGE_STATS_REFRESH_LOCK.release()


def seconds_until_next_usage_refresh() -> float:
    now = datetime.now().astimezone()
    target = now.replace(
        hour=USAGE_STATS_DAILY_HOUR,
        minute=USAGE_STATS_DAILY_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


def usage_stats_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(seconds_until_next_usage_refresh()):
        try:
            refresh_usage_stats(reason="daily")
        except Exception as exc:  # noqa: BLE001
            append_audit("refresh-usage-stats-failed", {"reason": "daily", "error": str(exc)})


def usage_stats_needs_startup_refresh() -> bool:
    if not USAGE_STATS_DAILY_ENABLED:
        return False
    if not USAGE_STATS_FILE.exists():
        return True
    reviewed_at = parse_timestamp(read_usage_stats().get("reviewedAt"))
    if not reviewed_at:
        return True
    return datetime.now(timezone.utc) - reviewed_at > timedelta(hours=25)


def start_usage_stats_refresh_thread(reason: str) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: refresh_usage_stats(reason=reason),
        name=f"usage-stats-{reason}",
        daemon=True,
    )
    thread.start()
    return thread


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
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
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
        raw = self.rfile.read(length).decode("utf-8")
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
            if method == "GET" and path == "/api/state":
                self.send_json(registry_view(read_registry_state()))
                return True
            if method == "GET" and path == "/api/audit":
                self.send_json({"events": read_audit()})
                return True
            if method == "GET" and path == "/api/versioning":
                self.send_json(read_version_status())
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
                self.send_json(review_skill_usage(self.read_body()))
                return True
            if method == "GET" and path == "/api/usage-stats":
                self.send_json(read_usage_stats())
                return True
            if method == "POST" and path == "/api/usage-stats/refresh":
                self.send_json(refresh_usage_stats(reason="manual", body=self.read_body()))
                return True
            if method == "POST" and path == "/api/install":
                self.send_json(install_skill(self.read_body()))
                return True

            skill_match = re.match(r"^/api/skills/([^/]+)(?:/(enable|disable|contexts|markdown|history|classify|localize))?$", path)
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
    if USAGE_STATS_DAILY_ENABLED:
        threading.Thread(
            target=usage_stats_scheduler,
            args=(usage_scheduler_stop,),
            name="usage-stats-scheduler",
            daemon=True,
        ).start()
        if usage_stats_needs_startup_refresh():
            start_usage_stats_refresh_thread("startup")
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
