"""PropSpec 字段规整（设计稿 §3.1）。

REST 路由、AI 工具与剧本导入各自以前都有一份规整逻辑，已经出现漂移
（红区裁剪时机、颜色校验缺失等）。这里收敛成唯一实现：

- ``strict=True``：请求体非法时抛 :class:`NormalizeError`，由路由映射成
  ``prop.invalid_body`` / ``prop.invalid_fields``；
- ``strict=False``：AI 工具用，非法条目直接跳过，尽量让回合继续。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from . import policy

STYLE_KEYS = ("paper", "ink", "accent", "font")
WEAR_NAMES = ("stain", "fold", "burn", "tear")
SOURCE_KINDS = {"ai", "manual", "imported"}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


class NormalizeError(ValueError):
    """请求体字段非法。"""


def normalize_title(raw: Any, fallback: str = "") -> str:
    title = str(raw or "").strip() or fallback
    return title[:200]


def normalize_style(
    raw: Any, previous: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """只保留已知 style 键；``ink`` / ``accent`` 非法颜色直接丢弃（渲染层再回退默认）。"""
    base = dict(previous or {})
    if not isinstance(raw, Mapping):
        return base
    for key in STYLE_KEYS:
        if key not in raw:
            continue
        value = str(raw.get(key) or "").strip()
        if not value:
            base.pop(key, None)
            continue
        if key in {"ink", "accent"} and not _COLOR_RE.match(value):
            base.pop(key, None)
            continue
        base[key] = value[:40]
    return base


def normalize_wear(
    raw: Any, previous: Mapping[str, Any] | None = None
) -> dict[str, float]:
    base = (
        {name: _clamp(previous.get(name)) for name in WEAR_NAMES} if previous else {}
    )
    if not isinstance(raw, Mapping):
        return base
    for name in WEAR_NAMES:
        if name in raw:
            base[name] = _clamp(raw.get(name))
    return base


def normalize_redaction(
    raw: Any,
    *,
    fields: Mapping[str, Any] | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    if raw is None:
        if strict:
            raise NormalizeError("redaction 必须是数组")
        return []
    if not isinstance(raw, (list, tuple)):
        if strict:
            raise NormalizeError("redaction 必须是数组")
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            if strict:
                raise NormalizeError("redaction 元素必须是对象")
            continue
        field = str(entry.get("field") or "").strip()
        if not field:
            if strict:
                raise NormalizeError("redaction 缺少 field")
            continue
        if fields is not None and field not in fields:
            continue
        try:
            start = int(entry.get("start"))
            end = int(entry.get("end"))
        except (TypeError, ValueError) as exc:
            if strict:
                raise NormalizeError("redaction 的 start/end 必须是整数") from exc
            continue
        if end <= start:
            continue
        mode = str(entry.get("mode") or "burn").strip().lower()
        if mode not in {"burn", "ink"}:
            mode = "burn"
        items.append({"field": field, "start": start, "end": end, "mode": mode})
    return items


def clamp_redaction(
    spec: Mapping[str, Any], items: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """按当前字段长度裁剪区间，越界/空区间直接丢弃。"""
    fields = spec.get("fields") if isinstance(spec.get("fields"), Mapping) else {}
    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or "")
        text = fields.get(field)
        if text is None:
            continue
        length = len(str(text))
        try:
            start = int(item.get("start") or 0)
            end = int(item.get("end") or 0)
        except (TypeError, ValueError):
            continue
        start = max(0, min(start, length))
        end = max(0, min(end, length))
        if end <= start:
            continue
        kept.append({**dict(item), "field": field, "start": start, "end": end})
    return kept


def normalize_visibility(
    raw: Any, previous: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if raw is None:
        return dict(previous or {"mode": "hidden", "revealed_to": []})
    if not isinstance(raw, Mapping):
        raise NormalizeError("visibility 必须是对象")
    mode = str(raw.get("mode") or "hidden").strip().lower()
    if mode not in policy.VISIBILITY_MODES:
        raise NormalizeError("visibility.mode 非法")
    recipients = raw.get("revealed_to")
    ids: list[int] = []
    if mode == "partial":
        if not isinstance(recipients, (list, tuple)) or not recipients:
            raise NormalizeError("partial 可见性需要 revealed_to")
        for item in recipients:
            try:
                ids.append(int(item))
            except (TypeError, ValueError) as exc:
                raise NormalizeError("revealed_to 必须是用户 id 数组") from exc
    return {"mode": mode, "revealed_to": ids}


def normalize_source(raw: Any) -> dict[str, Any]:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    kind = str(source.get("kind") or "manual").strip().lower()
    if kind not in SOURCE_KINDS:
        kind = "manual"
    return {
        "kind": kind,
        "clue_id": source.get("clue_id"),
        "scenario_id": source.get("scenario_id"),
        "ai_request_id": source.get("ai_request_id"),
    }


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "NormalizeError",
    "SOURCE_KINDS",
    "STYLE_KEYS",
    "WEAR_NAMES",
    "clamp_redaction",
    "normalize_redaction",
    "normalize_source",
    "normalize_style",
    "normalize_title",
    "normalize_visibility",
    "normalize_wear",
]
