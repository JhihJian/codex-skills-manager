"""统计 Codex skills 注入时的 SKILL.md token 占用。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


_CJK_PATTERN = re.compile(r"[\u2e80-\u2fff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ENCODER = None
_ENCODER_METHOD = "estimate:unicode"
_ENCODER_READY = False


def _load_encoder() -> Any:
    """按需加载可选的 tiktoken，避免把它变成项目运行时硬依赖。"""
    global _ENCODER, _ENCODER_METHOD, _ENCODER_READY
    if _ENCODER_READY:
        return _ENCODER
    _ENCODER_READY = True
    try:
        import tiktoken

        for encoding_name in ("o200k_base", "cl100k_base"):
            try:
                _ENCODER = tiktoken.get_encoding(encoding_name)
                _ENCODER_METHOD = f"tiktoken:{encoding_name}"
                break
            except (LookupError, ValueError):
                continue
    except (ImportError, OSError):
        pass
    return _ENCODER


def estimate_token_count(text: str) -> int:
    """在没有 tiktoken 时给出中英文混合 Markdown 的稳定估算。"""
    total = 0
    last = 0
    for match in _CJK_PATTERN.finditer(text):
        western = text[last : match.start()]
        total += math.ceil(len(western.encode("utf-8")) / 4) if western else 0
        total += 1
        last = match.end()
    western = text[last:]
    total += math.ceil(len(western.encode("utf-8")) / 4) if western else 0
    return total


def count_tokens(text: str) -> tuple[int, str]:
    encoder = _load_encoder()
    if encoder is not None:
        return len(encoder.encode(text, disallowed_special=())), _ENCODER_METHOD
    return estimate_token_count(text), _ENCODER_METHOD


def skill_catalog_line(name: str, entry: dict[str, Any], path: Path | None) -> str:
    """近似 Codex 启动时注入的技能索引行，不包含完整 SKILL.md。"""
    description = str(entry.get("description") or entry.get("title") or "").strip()
    line = f"- {name}"
    if description:
        line += f": {description}"
    if path is not None:
        line += f" (file: {path})"
    return f"{line}\n"


def skill_markdown_path(entry: dict[str, Any], allowed_roots: tuple[Path, ...] = ()) -> Path | None:
    """优先使用 Codex 已启用副本，确保统计的是实际注入文件。"""
    candidates: list[Path] = []
    if entry.get("codexPath"):
        candidates.append(Path(str(entry["codexPath"])) / "SKILL.md")
    if entry.get("skillMdPath"):
        candidates.append(Path(str(entry["skillMdPath"])))
    if entry.get("libraryPath"):
        candidates.append(Path(str(entry["libraryPath"])) / "SKILL.md")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.name.lower() != "skill.md":
            resolved = resolved / "SKILL.md"
        if allowed_roots and not any(
            resolved == root.resolve() or root.resolve() in resolved.parents for root in allowed_roots
        ):
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def calculate_skill_token_usage(
    skills: dict[str, dict[str, Any]], allowed_roots: tuple[Path, ...] = ()
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    total_lazy_tokens = 0
    counted = 0
    lazy_counted = 0
    errors = 0

    for name, skill in sorted(skills.items(), key=lambda item: item[0].lower()):
        item: dict[str, Any] = {
            "enabled": bool(skill.get("enabled")),
            "counted": False,
            "tokens": 0,
            "lazyTokens": 0,
            "lazyCounted": False,
            "characters": 0,
            "bytes": 0,
            "path": "",
            "error": "",
        }
        if not item["enabled"]:
            item["reason"] = "未启用"
        else:
            path = skill_markdown_path(skill, allowed_roots)
            catalog_tokens, method = count_tokens(skill_catalog_line(name, skill, path))
            item["tokens"] = catalog_tokens
            item["method"] = method
            item["catalogText"] = "名称、描述和文件位置"
            item["counted"] = True
            total_tokens += catalog_tokens
            counted += 1
            if path is None:
                item["error"] = "未找到 SKILL.md"
                errors += 1
            else:
                item["path"] = str(path)
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                    lazy_tokens, _ = count_tokens(text)
                    item["lazyCounted"] = True
                    item["lazyTokens"] = lazy_tokens
                    item["characters"] = len(text)
                    item["bytes"] = len(text.encode("utf-8"))
                    total_lazy_tokens += lazy_tokens
                    lazy_counted += 1
                except OSError as exc:
                    item["error"] = f"读取失败：{exc}"
                    errors += 1
        by_name[name] = item
        entries.append({"name": name, **item})

    methods = {item.get("method") for item in entries if item.get("method")}
    method = next(iter(methods), _ENCODER_METHOD)
    return {
        "enabledSkillCount": sum(1 for item in entries if item["enabled"]),
        "countedSkillCount": counted,
        "totalTokens": total_tokens,
        "lazyCountedSkillCount": lazy_counted,
        "totalLazyTokens": total_lazy_tokens,
        "errors": errors,
        "method": method,
        "unit": "token",
        "scope": "enabled-catalog",
        "lazyLoadScope": "enabled-skill-md",
        "entries": entries,
        "byName": by_name,
    }
