"""可见性与信息控制（设计稿 §3.1 / §5.3 / §6.3）。

``apply_redaction`` 是防剧透的唯一入口：玩家侧渲染、AI 侧 prompt、接口返回
都必须经过它，保证「被烧掉的段落」不会从任何一条通道泄露。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

PLAYER_MASK = "█"
AI_MASK = "████"
VISIBILITY_MODES = ("hidden", "revealed", "partial")


def visibility_mode(spec: Mapping[str, Any] | None) -> str:
    visibility = (spec or {}).get("visibility")
    mode = str((visibility or {}).get("mode") or "hidden").strip().lower()
    return mode if mode in VISIBILITY_MODES else "hidden"


def revealed_to(spec: Mapping[str, Any] | None) -> list[str]:
    visibility = (spec or {}).get("visibility")
    raw = (visibility or {}).get("revealed_to")
    if not isinstance(raw, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def has_redaction(spec: Mapping[str, Any] | None) -> bool:
    """是否存在有效（能落到真实区间）的 redaction。"""
    return bool(_valid_spans((spec or {}).get("redaction"), (spec or {}).get("fields")))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_spans(
    redaction: Any, fields: Any
) -> dict[str, list[tuple[int, int, str]]]:
    """把 redaction 规整成 ``{field: [(start, end, mode), ...]}``。

    越界区间裁剪到字段长度，空区间与未知字段直接跳过；同一字段内按起点
    从后往前排序，保证替换时不会互相移位。
    """
    if not isinstance(redaction, (list, tuple)) or not isinstance(fields, Mapping):
        return {}
    grouped: dict[str, list[tuple[int, int, str]]] = {}
    for item in redaction:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or "").strip()
        if field not in fields:
            continue
        text = fields.get(field)
        length = len("" if text is None else str(text))
        start = _as_int(item.get("start"))
        end = _as_int(item.get("end"))
        if start is None or end is None:
            continue
        start = max(0, min(start, length))
        end = max(0, min(end, length))
        if end <= start:
            continue
        mode = str(item.get("mode") or "burn").strip().lower()
        grouped.setdefault(field, []).append((start, end, mode))
    for spans in grouped.values():
        spans.sort(key=lambda span: (span[0], span[1]), reverse=True)
    return grouped


def _mask_text(text: str, spans: Iterable[tuple[int, int, str]], mask: str) -> str:
    value = text
    for start, end, _mode in spans:
        width = end - start
        replacement = mask * width if len(mask) == 1 else mask
        value = value[:start] + replacement + value[end:]
    return value


def apply_redaction(
    spec: Mapping[str, Any],
    *,
    mask: str = PLAYER_MASK,
    fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """返回脱敏后的 spec 深拷贝；``fields`` 限定只处理指定字段。"""
    result = copy.deepcopy(dict(spec or {}))
    source_fields = result.get("fields")
    if not isinstance(source_fields, Mapping):
        return result
    grouped = _valid_spans(result.get("redaction"), source_fields)
    if fields is not None:
        allowed = {str(name) for name in fields}
        grouped = {key: value for key, value in grouped.items() if key in allowed}
    if not grouped:
        return result
    effective_mask = str(mask or PLAYER_MASK)
    fields_copy = dict(source_fields)
    for field, spans in grouped.items():
        raw = fields_copy.get(field)
        text = "" if raw is None else str(raw)
        fields_copy[field] = _mask_text(text, spans, effective_mask)
    result["fields"] = fields_copy
    return result


def redacted_for_ai(spec: Mapping[str, Any]) -> dict[str, Any]:
    """AI 侧视图：被遮处统一替换为 ``████``（§6.3）。"""
    return apply_redaction(spec, mask=AI_MASK)


def can_view(spec: Mapping[str, Any] | None, user_id: Any, *, is_kp: bool) -> bool:
    """``hidden`` 仅 KP；``revealed`` 房间成员均可；``partial`` 命中名单可见。"""
    if not isinstance(spec, Mapping):
        return False
    if is_kp:
        return True
    mode = visibility_mode(spec)
    if mode == "revealed":
        return True
    if mode == "partial":
        target = str(user_id if user_id is not None else "").strip()
        return bool(target) and target in revealed_to(spec)
    return False


def visible_spec(
    spec: Mapping[str, Any] | None, user_id: Any, *, is_kp: bool
) -> dict[str, Any] | None:
    """按可见性返回 spec；不可见返回 ``None``（调用方据此回 404，不泄露存在性）。"""
    if not can_view(spec, user_id, is_kp=is_kp):
        return None
    result = copy.deepcopy(dict(spec or {}))
    if is_kp:
        return result
    result = apply_redaction(result)
    result["redacted"] = has_redaction(spec)
    return result


def message_visible_to(
    metadata: Mapping[str, Any] | None, user_id: Any, *, is_kp: bool = False
) -> bool:
    """``prop`` 消息是否对当前查看者可见。

    ``revealed_to`` 为空表示投递给全体；非空时只有名单内的玩家与 KP 可见。
    其他类型的消息不受影响，由调用方只对 ``type == "prop"`` 使用。
    """
    if not isinstance(metadata, Mapping):
        return True
    raw = metadata.get("revealed_to")
    if not isinstance(raw, (list, tuple, set)) or not raw:
        return True
    if is_kp:
        return True
    target = str(user_id if user_id is not None else "").strip()
    return bool(target) and target in {str(item).strip() for item in raw}


def filter_messages(
    messages: Iterable[Mapping[str, Any]] | None,
    user_id: Any,
    *,
    is_kp: bool = False,
) -> list[Mapping[str, Any]]:
    """按收件人过滤消息列表；只处理 ``type == "prop"``，其余原样保留。"""
    result: list[Mapping[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        if str(message.get("type") or "") == "prop" and not message_visible_to(
            message.get("metadata") if isinstance(message.get("metadata"), Mapping) else None,
            user_id,
            is_kp=is_kp,
        ):
            continue
        result.append(message)
    return result


__all__ = [
    "AI_MASK",
    "PLAYER_MASK",
    "VISIBILITY_MODES",
    "apply_redaction",
    "can_view",
    "filter_messages",
    "has_redaction",
    "message_visible_to",
    "redacted_for_ai",
    "revealed_to",
    "visibility_mode",
    "visible_spec",
]
