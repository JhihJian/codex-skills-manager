"""Deterministic negative-feedback and process-anomaly detection.

The module is deliberately independent from persistence and web concerns.  Its
public functions return frozen dataclasses that ``outcome_reviews`` can project
into revisions and evidence records without retaining unredacted source text.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from auth import REDACTION_FAILED, redact_sensitive


DETECTOR_VERSION = "feedback-v5"
SPAN_PARSER_VERSION = "feedback-span-v1"
MAX_EXCERPT_LENGTH = 512


@dataclass(frozen=True)
class EvidenceSpan:
    event_id: str
    block_index: int
    start: int
    end: int
    origin: str
    excerpt_hash: str
    redacted_excerpt: str | None
    protocol_locator: str
    truncated: bool = False
    redaction_status: str = "clean"
    parser_version: str = SPAN_PARSER_VERSION


@dataclass(frozen=True)
class ConfidenceAdjustment:
    reason: str
    value: float


@dataclass(frozen=True)
class FeedbackCandidate:
    category: str
    severity: str
    confidence: float
    adjustments: tuple[ConfidenceAdjustment, ...]
    span: EvidenceSpan
    channel: str = "user-feedback"
    authority: str = "user"


@dataclass(frozen=True)
class ProcessPlan:
    mode: str
    planned_count: int
    started_count: int
    completed_count: int
    failed_before_start_count: int
    requested_agents: tuple[str, ...]
    available_agents: tuple[str, ...]
    expected_result_ids: tuple[str, ...]
    child_session_ids: tuple[str, ...]
    returned_result_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProcessAnomaly:
    category: str
    severity: str
    confidence: float
    tool_name: str
    reason: str
    plan: ProcessPlan
    result_indexes: tuple[int, ...] = ()
    channel: str = "process-anomaly"
    authority: str = "tool"


_SKILL_OPEN = re.compile(r"<skill\b[^>]*>", re.IGNORECASE)
_SKILL_CLOSE = re.compile(r"</skill\s*>", re.IGNORECASE)
_SKILL_TOKEN = re.compile(r"</?skill\b", re.IGNORECASE)
_INJECTED_OPEN = re.compile(
    r"<(?P<tag>subagent_notification|environment_context|system[-_]reminder|"
    r"developer[-_]message|user[-_]instructions|turn[-_]aborted|compacted[-_]context)\b[^>]*>",
    re.IGNORECASE,
)
_SYSTEM_ENVELOPE = re.compile(
    r"(?im)^\s*(?:You are working on a Linear ticket|Goal mode is active|"
    r"Continue the active /goal|<goal_objective>|<!--\s*pi-goal-continuation:|"
    r"This is an unattended orchestration session\b)"
)
_ATTRIBUTE = re.compile(r"([A-Za-z_][\w:.-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_FENCE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[^\n]*(?:\n|$)")
_MARKDOWN_QUOTE = re.compile(r"(?m)^[ \t]*>[^\n]*(?:\n|$)")
_INDENTED_CODE = re.compile(r"(?m)^(?: {4}|\t)[^\n]*(?:\n|$)")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_LOG_LINE = re.compile(
    r"(?im)^[^\n]*(?:错误日志(?:如下)?|日志(?:里|中)?(?:写着|显示|如下)|log\s*(?:output)?\s*:|"
    r"traceback\s*\(|stack\s*trace\s*:|\[(?:error|fatal|warn)\]|"
    r"^(?:error|fatal|warn|debug|info)\s*[: ]|"
    r"\d{4}-\d{2}-\d{2}[T ][0-9:]{5})[^\n]*(?:\n|$)"
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
)
_URL_CREDENTIAL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_WINDOWS_PATH = re.compile(r"(?i)(?<!\w)[A-Z]:\\(?:[^\s<>:\"|?*]+\\)*[^\s<>:\"|?*]*")
_UNIX_PATH = re.compile(r"(?<![\w:/])/(?:[^\s\"'<>/]+/)+[^\s\"'<>/]*")
_HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{24,}={0,2}(?![A-Za-z0-9_-]))(?=[^\s]*[A-Z])(?=[^\s]*[a-z])(?=[^\s]*\d)[A-Za-z0-9_-]+={0,2}")

_POSITIVE_NEGATION = re.compile(
    r"(?i)(?:没(?:有)?问题|现在(?:可以|正常)了|不再报错|不报错了|已经修复|"
    r"no (?:more )?(?:errors?|issues?|problems?)|no longer fails?|"
    r"doesn't (?:fail|error) anymore|works? now|not bad|all good)"
)
_HYPOTHETICAL = re.compile(
    r"(?i)(?:如果|假如|假设|示例|例如).*(?:失败|不行|错误|回滚)|"
    r"\b(?:if|when|unless)\b[^.!?\n]*(?:fail|error|wrong|rollback)"
)
_CANCEL = re.compile(r"(?i)(?:先别|暂(?:停|缓)|停止|别继续|不用继续|\b(?:cancel|pause|stop)\b(?:\s+(?:now|for now))?)")
_INSTRUCTION = re.compile(
    r"(?i)^\s*(?:请)?(?:不要|无需|禁止|别|不得|不允许)|"
    r"^\s*(?:please\s+)?(?:do not|don't|must not|never)\b"
)
_SCOPE_CHANGE = re.compile(r"(?i)(?:再|另外|顺便)(?:增加|新增|加上|补充)|\b(?:also add|additionally add|one more)\b")
_PREFERENCE_CHANGE = re.compile(r"(?i)(?:换成|改成)(?:绿色|蓝色|红色|另一种|别的)|\b(?:prefer|i(?:'d| would) rather)\b")
_RETROSPECTIVE = re.compile(
    r"(?i)(?:你(?:刚才|之前|漏了|遗漏了|没|未|不应该|不该)|刚才|之前|改完|实现后|结果|"
    r"还是|仍然|依旧|漏了|遗漏了|没按要求|没有按要求|"
    r"\byou\s+(?:missed|omitted|didn't|did not|failed to|shouldn't have|should not have)\b|"
    r"\b(?:previous|last (?:answer|change)|after (?:the )?(?:change|implementation)|still|again|missed|omitted|not as requested)\b)"
)
_REQUEST_OR_BACKGROUND = re.compile(
    r"(?i)^\s*[,，]?\s*(?:\\?[-*+]\s*)?(?:@codex\s+review\b|希望|目标\s*[：:]?|当前|项目|系统|已接入|"
    r"重点(?:看|检查|发现)|用于(?:检查|发现)|请(?:用|检查|审查|确保)|测试覆盖\s*[：:]?|"
    r"(?:please|ensure|then ensure|goal\s*:|objective\s*:|the (?:project|system)|we need|i want|"
    r"do not|don't|operate\b|use the\b)\b)"
)

_EXTERNAL = re.compile(
    r"(?i)(?:客户|验收平台|审批|业务系统|外部验收).{0,24}(?:失败|拒绝|不通过|未通过|仍判定失败)|"
    r"\b(?:customer|client|acceptance (?:platform|system)|approval system)\b.{0,40}\b(?:reject|fail|not pass)"
)
_PROCESS = re.compile(
    r"(?i)(?:你不应该|你不该|擅自|越权|没有按要求.{0,16}(?:并行|执行|测试|验证)|"
    r"(?:首轮|要求).{0,16}(?:没|未)(?:有)?执行|(?:没有|没|未)(?:做|跑)?(?:测试|验证)|"
    r"\b(?:you should not|you shouldn't|should not have|shouldn't have|failed to|didn't|did not)\b.{0,30}\b(?:run|test|verify|follow|execute|dispatch|modif(?:y|ied)|chang(?:e|ed)))"
)
_REQUIREMENT = re.compile(
    r"(?i)(?:漏了|遗漏|缺少|没(?:有)?做|未实现|没按要求|没有按要求|不符合要求|违反要求|"
    r"\b(?:missed|missing|omitted|didn't implement|did not implement|not as requested|doesn't meet (?:the )?requirements?)\b)"
)
_REWORK = re.compile(r"(?i)(?:撤销|回退|重新做|重做|返工|改回来|\b(?:undo|revert|redo|start over|do it again)\b)")
_DEFECT = re.compile(
    r"(?i)(?:(?:还是|仍然|依旧).{0,24}(?:失败|不行|不能|无法|没反应|报错|错误|崩溃|[45]\d\d)|"
    r"(?:按钮|页面|接口|测试|构建|数据|保存|部署).{0,18}(?:失败|不能|无法|没反应|报错|错误|崩溃|不对|[45]\d\d)|"
    r"(?:失败|崩溃|报错|返回\s*[45]\d\d)|"
    r"\b(?:still|again)\b.{0,35}\b(?:fails?|failed|broken|wrong|error|cannot|can't|doesn't|does not|[45]\d\d)\b|"
    r"\b(?:button|page|api|test|build|data|save|deploy)\b.{0,30}\b(?:fails?|failed|broken|doesn't work|does not work|returns? [45]\d\d|is wrong)\b)"
)
_REJECTION = re.compile(
    r"(?i)(?:完全不对|不对|不能用|不可用|不接受|结果错了|方案错了|"
    r"\b(?:completely wrong|not correct|this is wrong|can't use (?:this|it)|cannot use (?:this|it)|unusable|i reject)\b)"
)
_WEAK = re.compile(r"(?i)(?:不太好|需要优化|有待改进|\b(?:not very good|needs improvement|could be better)\b)")
_POSITIVE = re.compile(r"(?i)(?:功能可以|可以用|不错|很好|\b(?:works?|good|fine|correct)\b)")
_EXPLICIT_OBJECT = re.compile(
    r"(?i)(?:结果|方案|代码|按钮|页面|接口|测试|构建|数据|标题|权限|校验|部署|"
    r"\b(?:result|answer|solution|code|button|page|api|test|build|data|title|permission|deployment)\b)"
)
_STRUCTURED_SYMPTOM = re.compile(r"(?i)(?:\b[45]\d\d\b|\b[A-Z][A-Z0-9_]+Error\b|exit\s*code\s*[1-9]\d*)")
_ADDED_REQUIREMENT = re.compile(r"(?i)(?:再|另外|还要|新增|增加|\b(?:also|additionally|one more)\b)")
_CRITICAL = re.compile(r"(?i)(?:安全越权|数据(?:丢失|删除)|生产不可用|误删|\b(?:data loss|security breach|production (?:down|unavailable))\b)")
_HIGH = re.compile(r"(?i)(?:核心|主要流程|验收.{0,8}失败|生产|\b[45]\d\d\b|\b(?:core|critical path|acceptance failed)\b)")
_ASSISTANT_SELF_CRITIQUE = re.compile(
    r"(?i)(?:我(?:刚才|之前)?(?:判断|理解|修改|实现)?(?:错了|有误)|我(?:漏了|遗漏了|没有测试|没有验证)|"
    r"(?:抱歉|对不起).{0,20}(?:错|遗漏|没有完成)|"
    r"\b(?:i was wrong|my (?:answer|change|implementation) was wrong|i missed|i forgot|i did not (?:test|verify))\b)"
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "none", "null", "off"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def redact_excerpt(text: str, *, limit: int = MAX_EXCERPT_LENGTH) -> tuple[str | None, bool, str]:
    """Return a bounded excerpt without retaining common credential forms."""
    try:
        redacted = redact_sensitive(text)
        if redacted == REDACTION_FAILED:
            return None, False, "failed"
        redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", redacted)
        redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
        redacted = _SECRET_VALUE.sub(lambda match: match.group(0)[: match.start(1) - match.start()] + "[REDACTED]", redacted)
        redacted = _URL_CREDENTIAL.sub(r"\1[REDACTED]@", redacted)
        redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
        redacted = _WINDOWS_PATH.sub("[REDACTED_PATH]", redacted)
        redacted = _UNIX_PATH.sub("[REDACTED_PATH]", redacted)
        redacted = _HIGH_ENTROPY.sub("[REDACTED_TOKEN]", redacted)
    except (MemoryError, RecursionError, RuntimeError, re.error):
        return None, False, "failed"
    status = "redacted" if redacted != text else "clean"
    truncated = len(redacted) > limit
    if truncated:
        suffix = "...[truncated]"
        redacted = redacted[: max(0, limit - len(suffix))] + suffix
    return redacted, truncated, status


def _make_span(text: str, *, event_id: str, block_index: int, start: int, end: int, origin: str) -> EvidenceSpan:
    raw = text[start:end]
    excerpt, truncated, status = redact_excerpt(raw)
    return EvidenceSpan(
        event_id=event_id,
        block_index=block_index,
        start=start,
        end=end,
        origin=origin,
        excerpt_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        redacted_excerpt=excerpt,
        protocol_locator=f"content[{block_index}].text:{start}-{end}",
        truncated=truncated,
        redaction_status=status,
    )


def _text_blocks(blocks: str | Sequence[Any]) -> list[tuple[int, str]]:
    if isinstance(blocks, str):
        return [(0, blocks)]
    projected: list[tuple[int, str]] = []
    for index, block in enumerate(blocks):
        if isinstance(block, str):
            projected.append((index, block))
        elif isinstance(block, Mapping) and str(block.get("type") or "text").lower() in {
            "text", "input_text", "output_text"
        }:
            value = block.get("text", block.get("input_text", block.get("output_text", "")))
            projected.append((index, str(value or "")))
    return projected


def _valid_skill_open(tag: str) -> bool:
    attrs = tag[tag.lower().find("skill") + 5:-1]
    position = 0
    values: dict[str, str] = {}
    while position < len(attrs):
        whitespace = re.match(r"\s+", attrs[position:])
        if whitespace:
            position += whitespace.end()
            continue
        match = _ATTRIBUTE.match(attrs, position)
        if not match:
            return False
        values[match.group(1).lower()] = match.group(3)
        position = match.end()
    return bool(values.get("name") and values.get("location"))


def _skill_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    while cursor < len(text):
        token = _SKILL_TOKEN.search(text, cursor)
        if token is None:
            if cursor < len(text):
                ranges.append((cursor, len(text), "user-authored"))
            break
        if token.start() > cursor:
            ranges.append((cursor, token.start(), "user-authored"))
        if text[token.start():].lower().startswith("</skill"):
            # This can be a continuation of a cross-block injection.  The
            # complete block is ambiguous and therefore fails closed.
            return [(0, len(text), "unknown-origin")]
        opening = _SKILL_OPEN.match(text, token.start())
        if opening is None or not _valid_skill_open(opening.group(0)):
            ranges.append((token.start(), len(text), "unknown-origin"))
            break
        closing = _SKILL_CLOSE.search(text, opening.end())
        nested = _SKILL_TOKEN.search(text, opening.end())
        if closing is None or (nested is not None and nested.start() < closing.start()):
            ranges.append((token.start(), len(text), "unknown-origin"))
            break
        ranges.append((token.start(), closing.end(), "skill-injected"))
        cursor = closing.end()
    if not text:
        return []
    if not ranges:
        return [(0, len(text), "user-authored")]
    return ranges


def _injected_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    while opening := _INJECTED_OPEN.search(text, cursor):
        tag = opening.group("tag")
        closing = re.search(rf"</{re.escape(tag)}\s*>", text[opening.end():], re.IGNORECASE)
        if closing is None:
            ranges.append((opening.start(), len(text), "unknown-origin"))
            break
        end = opening.end() + closing.end()
        ranges.append((opening.start(), end, "system-injected"))
        cursor = end
    return ranges


def _overlay_origin_ranges(
    base: Sequence[tuple[int, int, str]], overlays: Sequence[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    if not overlays:
        return list(base)
    boundaries = sorted({point for start, end, _origin in [*base, *overlays] for point in (start, end)})
    result: list[tuple[int, int, str]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        overlay = next((origin for left, right, origin in overlays if left <= start and end <= right), None)
        origin = overlay or next((origin for left, right, origin in base if left <= start and end <= right), None)
        if origin is None:
            continue
        if result and result[-1][1] == start and result[-1][2] == origin:
            result[-1] = (result[-1][0], end, origin)
        else:
            result.append((start, end, origin))
    return result


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted((start, end) for start, end in ranges if end > start):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _quoted_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    fragment = text[start:end]
    ranges: list[tuple[int, int]] = []
    fence_cursor = 0
    while True:
        opening = _FENCE.search(fragment, fence_cursor)
        if opening is None:
            break
        marker = re.escape(opening.group(1)[0]) + "{" + str(len(opening.group(1))) + ",}"
        closing = re.search(r"(?m)^[ \t]*" + marker + r"[ \t]*(?:\n|$)", fragment[opening.end():])
        fence_end = len(fragment) if closing is None else opening.end() + closing.end()
        ranges.append((start + opening.start(), start + fence_end))
        fence_cursor = fence_end
    for pattern in (_MARKDOWN_QUOTE, _INDENTED_CODE, _LOG_LINE, _INLINE_CODE):
        ranges.extend((start + match.start(), start + match.end()) for match in pattern.finditer(fragment))
    return _merge_ranges(ranges)


def _split_user_quotes(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    quotes = _quoted_ranges(text, start, end)
    if not quotes:
        return [(start, end, "user-authored")]
    result: list[tuple[int, int, str]] = []
    cursor = start
    for quote_start, quote_end in quotes:
        if quote_start > cursor:
            result.append((cursor, quote_start, "user-authored"))
        result.append((quote_start, quote_end, "quoted"))
        cursor = quote_end
    if cursor < end:
        result.append((cursor, end, "user-authored"))
    return result


def extract_content_spans(blocks: str | Sequence[Any], *, event_id: str = "") -> tuple[EvidenceSpan, ...]:
    """Project Pi text blocks into fail-closed origin spans.

    Skill tags never cross content-block boundaries.  Malformed, nested, or
    unclosed blocks become ``unknown-origin`` from the bad token to block end.
    Markdown quotes, fenced/inline code, and obvious log quotation lines are
    returned as ``quoted`` spans and are therefore auditable but not scanned.
    """
    spans: list[EvidenceSpan] = []
    for block_index, text in _text_blocks(blocks):
        if _SYSTEM_ENVELOPE.search(text):
            spans.append(_make_span(
                text, event_id=event_id, block_index=block_index,
                start=0, end=len(text), origin="system-injected",
            ))
            continue
        ranges = _overlay_origin_ranges(_skill_ranges(text), _injected_ranges(text))
        for start, end, origin in ranges:
            ranges = _split_user_quotes(text, start, end) if origin == "user-authored" else [(start, end, origin)]
            spans.extend(
                _make_span(text, event_id=event_id, block_index=block_index, start=part_start, end=part_end, origin=part_origin)
                for part_start, part_end, part_origin in ranges if part_end > part_start
            )
    return tuple(spans)


def _sentence_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    fragment = text[start:end]
    result: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(r"[。！？!?;；\n]+|(?<=[A-Za-z])\.(?=\s|$)", fragment):
        if match.start() > cursor:
            result.append((start + cursor, start + match.start()))
        cursor = match.end()
    if cursor < len(fragment):
        result.append((start + cursor, end))
    return [(left, right) for left, right in result if text[left:right].strip()]


def _severity(category: str, text: str) -> str:
    if _CRITICAL.search(text):
        return "critical"
    if _HIGH.search(text) or category in {"result-rejection", "external-negative-acceptance"}:
        return "high"
    if category == "mixed-or-unclear" and _WEAK.search(text):
        return "low"
    return "medium"


def _classify(text: str, *, has_previous_result: bool, target_ambiguous: bool) -> tuple[str, str, float, tuple[ConfidenceAdjustment, ...]] | None:
    stripped = text.strip()
    if not stripped:
        return None
    negative_hint = any(pattern.search(stripped) for pattern in (
        _EXTERNAL, _PROCESS, _REQUIREMENT, _REWORK, _DEFECT, _REJECTION, _WEAK,
    ))
    positive_negation = bool(_POSITIVE_NEGATION.search(stripped))
    adversative_negative = bool(
        re.search(r"(?i)(?:但|但是|不过|然而|\bbut\b|\bhowever\b)", stripped)
        and negative_hint
    )
    if (positive_negation and not adversative_negative) or _HYPOTHETICAL.search(stripped) or _CANCEL.search(stripped):
        return None
    retrospective = _RETROSPECTIVE.search(stripped)
    if _INSTRUCTION.search(stripped) and not retrospective:
        return None
    if _REQUEST_OR_BACKGROUND.search(stripped) and not retrospective:
        return None

    patterns = (
        ("external-negative-acceptance", _EXTERNAL, 0.93),
        ("process-critique", _PROCESS, 0.92),
        ("requirement-gap", _REQUIREMENT, 0.90),
        ("rework-correction", _REWORK, 0.88),
        ("observed-defect", _DEFECT, 0.93),
        ("result-rejection", _REJECTION, 0.95),
        ("mixed-or-unclear", _WEAK, 0.60),
    )
    match = next(((category, confidence) for category, pattern, confidence in patterns if pattern.search(stripped)), None)
    if match is None:
        return None
    category, confidence = match
    if category == "requirement-gap" and not retrospective and re.match(
        r"(?i)^\s*(?:\\?[-*+]\s*)?(?:缺少|missing\b)", stripped
    ):
        return None
    negative_pattern = any(pattern.search(stripped) for _category, pattern, _base in patterns[:-1])
    positive = (_POSITIVE.search(stripped) or _POSITIVE_NEGATION.search(stripped)) and not re.search(
        r"(?i)(?:doesn't|does not|didn't|did not|not)\s+work", stripped
    )
    if negative_pattern and positive:
        category, confidence = "mixed-or-unclear", 0.72

    # Pure additions and preference changes remain outside the feedback list.
    if category == "mixed-or-unclear" and (_SCOPE_CHANGE.search(stripped) or _PREFERENCE_CHANGE.search(stripped)):
        return None

    adjustments: list[ConfidenceAdjustment] = []
    if _EXPLICIT_OBJECT.search(stripped):
        adjustments.append(ConfidenceAdjustment("explicit-object-reference", 0.05))
    if _STRUCTURED_SYMPTOM.search(stripped):
        adjustments.append(ConfidenceAdjustment("structured-symptom", 0.03))
    if _ADDED_REQUIREMENT.search(stripped):
        adjustments.append(ConfidenceAdjustment("contains-added-requirement", -0.10))
    short_rejection = category == "result-rejection" and len(stripped) <= 12
    if short_rejection and not has_previous_result:
        adjustments.append(ConfidenceAdjustment("missing-previous-result", -0.20))
    for adjustment in adjustments:
        confidence += adjustment.value
    confidence = min(1.0, max(0.0, confidence))
    if target_ambiguous and confidence > 0.79:
        adjustments.append(ConfidenceAdjustment("ambiguous-target-cap", round(0.79 - confidence, 2)))
        confidence = 0.79
    return category, _severity(category, stripped), round(confidence, 2), tuple(adjustments)


def detect_user_feedback(
    blocks: str | Sequence[Any],
    *,
    event_id: str = "",
    has_previous_result: bool = True,
    target_ambiguous: bool = False,
) -> tuple[FeedbackCandidate, ...]:
    """Detect user-authored negative-feedback candidates in source order."""
    block_text = dict(_text_blocks(blocks))
    candidates: list[FeedbackCandidate] = []
    seen: set[tuple[int, int, int, str]] = set()
    for projected in extract_content_spans(blocks, event_id=event_id):
        if projected.origin != "user-authored":
            continue
        text = block_text[projected.block_index]
        for start, end in _sentence_ranges(text, projected.start, projected.end):
            classified = _classify(
                text[start:end], has_previous_result=has_previous_result, target_ambiguous=target_ambiguous
            )
            if classified is None:
                continue
            category, severity, confidence, adjustments = classified
            key = (projected.block_index, start, end, category)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(FeedbackCandidate(
                category=category,
                severity=severity,
                confidence=confidence,
                adjustments=adjustments,
                span=_make_span(
                    text, event_id=event_id, block_index=projected.block_index,
                    start=start, end=end, origin="user-authored",
                ),
                authority="external" if category == "external-negative-acceptance" else "user",
            ))
    return tuple(candidates)


def detect_assistant_claims(
    blocks: str | Sequence[Any], *, event_id: str = ""
) -> tuple[FeedbackCandidate, ...]:
    """Project assistant self-critique as zero-weight search clues."""
    candidates: list[FeedbackCandidate] = []
    for block_index, text in _text_blocks(blocks):
        excluded = _merge_ranges(
            _quoted_ranges(text, 0, len(text))
            + [(start, end) for start, end, _origin in _injected_ranges(text)]
            + [(start, end) for start, end, origin in _skill_ranges(text) if origin != "user-authored"]
        )
        for start, end in _sentence_ranges(text, 0, len(text)):
            if any(left < end and start < right for left, right in excluded):
                continue
            if not _ASSISTANT_SELF_CRITIQUE.search(text[start:end]):
                continue
            candidates.append(FeedbackCandidate(
                category="assistant-self-assessment",
                severity="low",
                confidence=0.65,
                adjustments=(),
                span=_make_span(
                    text, event_id=event_id, block_index=block_index,
                    start=start, end=end, origin="assistant-authored",
                ),
                channel="assistant-claim",
                authority="assistant",
            ))
    return tuple(candidates)


def is_positive_resolution(blocks: str | Sequence[Any]) -> bool:
    """Return true only for explicit user acceptance outside injected or quoted spans."""
    block_text = dict(_text_blocks(blocks))
    for span in extract_content_spans(blocks):
        if span.origin != "user-authored":
            continue
        text = block_text[span.block_index][span.start:span.end]
        negative_hint = any(pattern.search(text) for pattern in (
            _EXTERNAL, _PROCESS, _REQUIREMENT, _REWORK, _DEFECT, _REJECTION, _WEAK,
        ))
        adversative_negative = bool(
            re.search(r"(?i)(?:但|但是|不过|然而|\bbut\b|\bhowever\b)", text)
            and negative_hint
        )
        if _POSITIVE_NEGATION.search(text) and not adversative_negative:
            return True
    return False


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(str(item) for item in value if str(item))


def _first(container: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in container:
            return container[name]
    return None


def _result_turns(result: Mapping[str, Any]) -> int:
    usage = _mapping(result.get("usage"))
    return _int(_first(usage, "turns", "turnCount", "turn_count"), _int(result.get("turns")))


def _child_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
    values = _string_tuple(_first(result, "childSessionIds", "child_session_ids", "childSessions"))
    single = _first(result, "childSessionId", "child_session_id", "sessionId", "session_id")
    return values + ((str(single),) if single else ())


def _returned_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
    values = _string_tuple(_first(result, "resultIds", "result_ids", "returnedResultIds", "returned_result_ids"))
    single = _first(result, "resultId", "result_id", "returnedResultId", "returned_result_id")
    return values + ((str(single),) if single else ())


def _available_from_stderr(results: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    found: list[str] = []
    for result in results:
        stderr = str(result.get("stderr") or "")
        match = re.search(r"(?i)available agents?\s*:\s*([^\n.]+)", stderr)
        if match:
            found.extend(part.strip(" \t'\"`") for part in re.split(r"[,\s]+", match.group(1)) if part.strip(" \t'\"`"))
    return tuple(dict.fromkeys(found))


def normalize_process_plan(
    tool_name: str,
    args: Any = None,
    details: Any = None,
    *,
    available_agents: Sequence[str] = (),
) -> ProcessPlan:
    """Normalize subagent single/parallel/chain calls and their Pi details."""
    arguments = _mapping(args)
    detail_map = _mapping(details)
    if isinstance(detail_map.get("details"), Mapping):
        detail_map = dict(detail_map["details"])
    results = _items(detail_map.get("results"))

    if isinstance(arguments.get("tasks"), (list, tuple)):
        mode, planned_items = "parallel", _items(arguments.get("tasks"))
    elif isinstance(arguments.get("chain"), (list, tuple)):
        mode, planned_items = "chain", _items(arguments.get("chain"))
    elif arguments.get("agent") is not None:
        mode, planned_items = "single", [arguments]
    else:
        mode = str(detail_map.get("mode") or ("single" if "subagent" in tool_name.lower() else "tool"))
        planned_items = []

    requested = tuple(str(item.get("agent") or "") for item in planned_items if item.get("agent"))
    if not requested:
        requested = _string_tuple(_first(detail_map, "requestedAgents", "requested_agents"))
    planned = len(planned_items) or _int(_first(detail_map, "plannedCount", "planned_count"))
    if planned == 0 and results and "subagent" in tool_name.lower():
        planned = len(results)

    expected = tuple(
        str(value) for item in planned_items
        if (value := _first(item, "expectedResultId", "expected_result_id", "resultId", "result_id"))
    )
    expected += _string_tuple(_first(detail_map, "expectedResultIds", "expected_result_ids"))

    child_ids = _string_tuple(_first(detail_map, "childSessionIds", "child_session_ids"))
    returned_ids = _string_tuple(_first(detail_map, "returnedResultIds", "returned_result_ids", "resultIds", "result_ids"))
    started = completed = failed_before_start = 0
    for result in results:
        children = _child_ids(result)
        returned = _returned_ids(result)
        child_ids += children
        returned_ids += returned
        turns = _result_turns(result)
        messages = result.get("messages")
        message_count = (
            len(messages) if isinstance(messages, (list, tuple))
            else _int(_first(result, "messageCount", "message_count", "messages"))
        )
        exit_present = "exitCode" in result or "exit_code" in result
        exit_code = _int(_first(result, "exitCode", "exit_code")) if exit_present else None
        unknown = str(_first(result, "agentSource", "agent_source") or "").lower() == "unknown"
        began = bool(turns > 0 or message_count > 0 or children or (exit_code == 0 and not unknown))
        if began:
            started += 1
            if returned or (exit_present and exit_code == 0) or _bool(_first(result, "completed", "resultReturned", "result_returned")):
                completed += 1
        elif exit_present and exit_code != 0:
            failed_before_start += 1

    supplied_values = () if isinstance(available_agents, str) else available_agents
    supplied = tuple(str(item) for item in supplied_values if str(item))
    supplied += _string_tuple(_first(detail_map, "availableAgents", "available_agents"))
    supplied += _available_from_stderr(results)
    available = tuple(dict.fromkeys(supplied))
    return ProcessPlan(
        mode=mode,
        planned_count=max(0, planned),
        started_count=started,
        completed_count=completed,
        failed_before_start_count=failed_before_start,
        requested_agents=requested,
        available_agents=available,
        expected_result_ids=tuple(dict.fromkeys(expected)),
        child_session_ids=tuple(dict.fromkeys(child_ids)),
        returned_result_ids=tuple(dict.fromkeys(returned_ids)),
    )


def _status_text(outcome: Any, details: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    pieces: list[str] = []
    if isinstance(outcome, Mapping):
        pieces.extend(str(_first(outcome, "status", "outcome", "error") or "") for _ in range(1))
        if _bool(_first(outcome, "blocked", "denied")):
            pieces.append("blocked")
        if _bool(_first(outcome, "timeout", "timedOut", "timed_out")):
            pieces.append("timeout")
        if _bool(_first(outcome, "isError", "is_error")):
            pieces.append("error")
    elif outcome is not None:
        pieces.append(str(outcome))
    pieces.append(str(_first(details, "status", "outcome", "error") or ""))
    for result in results:
        pieces.append(str(_first(result, "status", "outcome", "error") or ""))
        exit_present = "exitCode" in result or "exit_code" in result
        if not exit_present or _int(_first(result, "exitCode", "exit_code")) != 0:
            pieces.append(str(result.get("stderr") or ""))
    return " ".join(pieces).lower()


def detect_process_anomalies(
    tool_name: str,
    args: Any = None,
    details: Any = None,
    outcome: Any = None,
    *,
    available_agents: Sequence[str] = (),
    episode_closed: bool = True,
) -> tuple[ProcessAnomaly, ...]:
    """Detect structured tool and subagent anomalies.

    Nested ``exitCode``, ``agentSource``, and ``usage.turns`` remain
    authoritative even when an outer result says ``isError=false``.
    """
    detail_map = _mapping(details)
    if isinstance(detail_map.get("details"), Mapping):
        detail_map = dict(detail_map["details"])
    results = _items(detail_map.get("results"))
    plan = normalize_process_plan(tool_name, args, detail_map, available_agents=available_agents)
    status = _status_text(outcome, detail_map, results)
    anomalies: list[ProcessAnomaly] = []

    unknown_indexes = tuple(
        index for index, result in enumerate(results)
        if str(_first(result, "agentSource", "agent_source") or "").lower() == "unknown"
        or "unknown agent" in str(result.get("stderr") or "").lower()
    )
    if unknown_indexes:
        anomalies.append(ProcessAnomaly(
            "agent-unavailable", "high", 1.0, tool_name,
            "requested agent is unavailable", plan, unknown_indexes,
        ))

    result_failures = tuple(
        index for index, result in enumerate(results)
        if ("exitCode" in result or "exit_code" in result)
        and _int(_first(result, "exitCode", "exit_code")) != 0
    )
    all_failed_without_start = bool(
        plan.planned_count > 0
        and len(results) >= plan.planned_count
        and plan.started_count == 0
        and not plan.child_session_ids
        and all(
            ("exitCode" in result or "exit_code" in result)
            and _int(_first(result, "exitCode", "exit_code")) != 0
            and _result_turns(result) == 0
            for result in results
        )
    )
    if all_failed_without_start:
        anomalies.append(ProcessAnomaly(
            "dispatch-not-executed", "high", 1.0, tool_name,
            "all planned agents failed before their first turn", plan, tuple(range(len(results))),
        ))
    elif 0 < plan.started_count < plan.planned_count:
        anomalies.append(ProcessAnomaly(
            "partial-dispatch", "high", 1.0, tool_name,
            "only part of the process plan started", plan,
        ))

    missing_child_result = False
    if episode_closed and plan.child_session_ids:
        if plan.expected_result_ids:
            missing_child_result = bool(set(plan.expected_result_ids) - set(plan.returned_result_ids))
        else:
            missing_child_result = len(plan.returned_result_ids) < len(plan.child_session_ids)
    if missing_child_result:
        anomalies.append(ProcessAnomaly(
            "child-result-missing", "high", 1.0, tool_name,
            "a started child session has no expected structured result", plan,
        ))

    blocked = bool(re.search(r"\b(?:blocked|denied|permission denied|policy rejected)\b", status))
    timeout = bool(re.search(r"\b(?:timeout|timed out|timed_out)\b", status))
    explicit_missing = bool(re.search(r"\b(?:result[-_ ]missing|missing result|no result)\b", status))
    outer_error = isinstance(outcome, Mapping) and _bool(_first(outcome, "isError", "is_error", "error"))
    top_exit_failure = (
        ("exitCode" in detail_map or "exit_code" in detail_map)
        and _int(_first(detail_map, "exitCode", "exit_code")) != 0
    )
    generic_error = outer_error or top_exit_failure or bool(result_failures) or bool(
        re.search(r"\b(?:error|failed|failure)\b", status)
    )

    if blocked:
        anomalies.append(ProcessAnomaly("tool-blocked", "high", 1.0, tool_name, "tool execution was blocked", plan))
    elif timeout:
        anomalies.append(ProcessAnomaly("tool-timeout", "high", 1.0, tool_name, "tool execution timed out", plan))
    elif generic_error and not unknown_indexes:
        anomalies.append(ProcessAnomaly(
            "tool-error", "high", 1.0, tool_name,
            "structured tool status reports an error", plan, result_failures,
        ))

    result_absent = (outcome is None or outcome == "") and not detail_map
    if episode_closed and (explicit_missing or result_absent):
        anomalies.append(ProcessAnomaly("result-missing", "high", 1.0, tool_name, "tool call has no result", plan))

    # Preserve deterministic category ordering and avoid equivalent duplicate signals.
    unique: dict[str, ProcessAnomaly] = {}
    for anomaly in anomalies:
        unique.setdefault(anomaly.category, anomaly)
    order = (
        "agent-unavailable", "dispatch-not-executed", "partial-dispatch",
        "child-result-missing", "tool-blocked", "tool-timeout", "tool-error", "result-missing",
    )
    return tuple(unique[category] for category in order if category in unique)


__all__ = [
    "ConfidenceAdjustment",
    "DETECTOR_VERSION",
    "EvidenceSpan",
    "FeedbackCandidate",
    "MAX_EXCERPT_LENGTH",
    "ProcessAnomaly",
    "ProcessPlan",
    "SPAN_PARSER_VERSION",
    "detect_process_anomalies",
    "detect_assistant_claims",
    "detect_user_feedback",
    "extract_content_spans",
    "is_positive_resolution",
    "normalize_process_plan",
    "redact_excerpt",
]