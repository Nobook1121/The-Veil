"""dnd5e 法术结构化字段的统一解析与升环计算。

数据源仍来自 ``dnd5e-srd`` 的 spell 条目。新字段（activation / range_spec /
duration_spec / target_spec / area_spec / resolution / upcast）缺失时，从旧字段
（casting_time / range / duration / targets / scaling 等）推断，保证旧数据包与
旧存档不用迁移也能读。

升环不做成逐法术分支，而是把 ``upcast.rules``（或旧 ``scaling``）归一化成规则
表，由 :func:`apply_upcast` 统一处理伤害骰、治疗骰、飞弹数、目标数、持续时间
与区域尺寸。
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

_ACTIVATION_TYPES = {"action", "bonus_action", "reaction", "legendary", "special"}
_RANGE_TYPES = {"self", "touch", "ranged", "unlimited", "special"}
_DURATION_UNITS = {"instant", "round", "minute", "hour", "day", "permanent", "special"}
_TARGET_TYPES = {
    "self",
    "creature",
    "object",
    "humanoid",
    "beast",
    "undead",
    "construct",
    "any",
}
_TARGET_SCOPES = {"self", "single", "multi", "area"}
_UPCAST_KINDS = {
    "damage_dice",
    "healing_dice",
    "projectiles",
    "targets",
    "duration",
    "area_size",
}
_DICE_RE = re.compile(r"(\d+)\s*d\s*(\d+)", re.IGNORECASE)
_DISTANCE_RE = re.compile(r"(\d+)\s*(?:feet|foot|ft\.?|尺|英尺)", re.IGNORECASE)

FEET_PER_CELL = 5


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


def activation_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("activation")
    raw = raw if isinstance(raw, Mapping) else {}
    kind = _text(raw.get("type")).casefold().replace(" ", "_")
    text = _text(entry.get("casting_time")).casefold()
    if not kind:
        if "bonus action" in text:
            kind = "bonus_action"
        elif "reaction" in text:
            kind = "reaction"
        elif "legendary" in text:
            kind = "legendary"
        elif "special" in text:
            kind = "special"
        else:
            kind = "action"
    if kind not in _ACTIVATION_TYPES:
        kind = "action"
    return {
        "type": kind,
        "trigger": _text(raw.get("trigger")),
        "ritual": bool(entry.get("ritual")),
    }


def range_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("range_spec")
    if isinstance(raw, Mapping) and _text(raw.get("type")):
        kind = _text(raw.get("type")).casefold()
        if kind not in _RANGE_TYPES:
            kind = "special"
        return {"type": kind, "feet": _int(raw.get("feet")) or None}
    text = _text(entry.get("range")).casefold()
    if text in {"self", "自身"}:
        return {"type": "self", "feet": None}
    if text in {"touch", "触碰", "接触"}:
        return {"type": "touch", "feet": FEET_PER_CELL}
    match = _DISTANCE_RE.search(text)
    if match:
        return {"type": "ranged", "feet": max(1, int(match.group(1)))}
    if "sight" in text or "视线" in text:
        return {"type": "unlimited", "feet": None}
    return {"type": "special", "feet": None}


def duration_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("duration_spec")
    if isinstance(raw, Mapping) and _text(raw.get("unit")):
        unit = _text(raw.get("unit")).casefold()
        if unit not in _DURATION_UNITS:
            unit = "special"
        return {
            "value": max(0, _int(raw.get("value"))),
            "unit": unit,
            "concentration": bool(raw.get("concentration", entry.get("concentration"))),
            "anchor": _text(raw.get("anchor")) or _infer_anchor(unit, bool(entry.get("concentration"))),
        }
    text = _text(entry.get("duration")).casefold()
    concentration = bool(entry.get("concentration"))
    if not text or "instantaneous" in text:
        return {"value": 0, "unit": "instant", "concentration": concentration, "anchor": "instant"}
    match = re.search(r"(\d+)\s*(round|minute|hour|day)", text)
    if match:
        unit = match.group(2)
        value = max(1, int(match.group(1)))
    elif "permanent" in text:
        unit, value = "permanent", 0
    else:
        unit, value = "special", 0
    return {
        "value": value,
        "unit": unit,
        "concentration": concentration,
        "anchor": _infer_anchor(unit, concentration),
    }


def _infer_anchor(unit: str, concentration: bool) -> str:
    if unit == "instant":
        return "instant"
    if unit == "permanent":
        return "permanent"
    if concentration:
        return "turn_end"
    if unit == "round":
        return "round_end"
    if unit in {"minute", "hour", "day"}:
        return "time_advance"
    return "special"


def target_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("target_spec")
    raw = raw if isinstance(raw, Mapping) else {}
    legacy = entry.get("targets") if isinstance(entry.get("targets"), Mapping) else {}
    kind = _text(raw.get("type") or legacy.get("type") or "creature").casefold()
    if kind not in _TARGET_TYPES:
        kind = "creature"
    scope = _text(raw.get("scope")).casefold()
    if scope not in _TARGET_SCOPES:
        if kind == "self":
            scope = "self"
        elif entry.get("area"):
            scope = "area"
        elif _int(legacy.get("count"), 1) > 1 or _int(raw.get("count"), 1) > 1:
            scope = "multi"
        else:
            scope = "single"
    count = raw.get("count", legacy.get("count"))
    count = _int(count) if count not in (None, "") else None
    exclude = raw.get("exclude_types")
    return {
        "type": kind,
        "count": max(0, count) if count is not None else None,
        "voluntary": bool(raw.get("voluntary", False)),
        "visible": bool(raw.get("visible", True)),
        "scope": scope,
        "exclude_types": [
            _text(item).casefold() for item in exclude or [] if _text(item)
        ],
    }


def area_spec(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = entry.get("area_spec")
    raw = raw if isinstance(raw, Mapping) else {}
    legacy = entry.get("area") if isinstance(entry.get("area"), Mapping) else {}
    if not raw and not legacy:
        return None
    shape = _text(raw.get("shape") or legacy.get("shape") or "sphere").casefold()
    size = raw.get("size_ft", legacy.get("radius_ft", legacy.get("size_ft")))
    return {
        "shape": shape,
        "size_ft": max(0, _int(size)),
        "moves_with_caster": bool(raw.get("moves_with_caster", False)),
    }


def resolution_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("resolution")
    raw = raw if isinstance(raw, Mapping) else {}
    kind = _text(raw.get("kind")).casefold()
    save = entry.get("save") if isinstance(entry.get("save"), Mapping) else None
    attack = entry.get("attack") if isinstance(entry.get("attack"), Mapping) else None
    if not kind:
        if save:
            kind = "save"
        elif attack:
            kind = "attack"
        elif isinstance(entry.get("healing"), Mapping):
            kind = "healing"
        elif entry.get("damage"):
            kind = "automatic"
        else:
            kind = "automatic"
    return {
        "kind": kind,
        "ability": _text(raw.get("ability") or (save or {}).get("ability")).casefold(),
        "half_on_success": bool(
            raw.get("half_on_success", (save or {}).get("half_on_success", False))
        ),
        "attack_type": _text(raw.get("attack_type") or (attack or {}).get("type")).casefold(),
    }


def upcast_rules(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把新 ``upcast.rules`` 与旧 ``scaling`` 归一化成同一张规则表。"""
    rules: list[dict[str, Any]] = []
    raw = entry.get("upcast")
    raw_rules = raw.get("rules") if isinstance(raw, Mapping) else None
    if isinstance(raw_rules, list):
        for row in raw_rules:
            if not isinstance(row, Mapping):
                continue
            kind = _text(row.get("kind")).casefold()
            if kind not in _UPCAST_KINDS:
                continue
            rule = dict(row)
            rule["kind"] = kind
            rules.append(rule)
    scaling = entry.get("scaling") if isinstance(entry.get("scaling"), Mapping) else None
    if scaling and len(rules) == 0:
        scaling_type = _text(scaling.get("type")).casefold()
        if scaling_type == "slot_level":
            if scaling.get("per_level"):
                rules.append({"kind": "damage_dice", "per_level": scaling.get("per_level")})
            if _int(scaling.get("projectiles_per_level")):
                rules.append(
                    {"kind": "projectiles", "per_level": _int(scaling.get("projectiles_per_level"))}
                )
            if _int(scaling.get("targets_per_level")):
                rules.append(
                    {"kind": "targets", "per_level": _int(scaling.get("targets_per_level"))}
                )
        elif scaling_type == "cantrip_level":
            thresholds = []
            for step in scaling.get("thresholds") or []:
                if not isinstance(step, Mapping):
                    continue
                thresholds.append(
                    {
                        "level": max(1, _int(step.get("level"), 1)),
                        "value": _text(step.get("dice") or step.get("value")),
                    }
                )
            if thresholds:
                rules.append({"kind": "damage_dice", "thresholds": thresholds})
    return rules


def describe(entry: Mapping[str, Any]) -> dict[str, Any]:
    """法术的完整结构化视图；旧字段缺失时由推断值补齐。"""
    return {
        "id": _text(entry.get("id")),
        "name": _text(entry.get("name")),
        "level": max(0, _int(entry.get("level"))),
        "school": _text(entry.get("school")),
        "activation": activation_spec(entry),
        "range": range_spec(entry),
        "duration": duration_spec(entry),
        "target": target_spec(entry),
        "area": area_spec(entry),
        "resolution": resolution_spec(entry),
        "upcast_rules": upcast_rules(entry),
        "repeat_save": _repeat_save(entry),
        "counterable": bool(entry.get("counterable", True)),
        "dispellable": bool(entry.get("dispellable", True)),
    }


def _repeat_save(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = entry.get("repeat_save")
    if not isinstance(raw, Mapping):
        return None
    return {
        "ability": _text(raw.get("ability")).casefold() or "wis",
        "timing": _text(raw.get("timing")).casefold() or "turn_end",
    }


# ---------------------------------------------------------------------------
# 升环
# ---------------------------------------------------------------------------


def _dice_terms(text: Any) -> tuple[dict[int, int], int]:
    raw = _text(text)
    terms: dict[int, int] = {}
    for count, sides in _DICE_RE.findall(raw):
        sides_value = int(sides)
        terms[sides_value] = terms.get(sides_value, 0) + int(count)
    cleaned = _DICE_RE.sub("", raw)
    fixed = sum(int(item) for item in re.findall(r"[+-]\s*\d+", cleaned))
    return terms, fixed


def add_dice(base: Any, extra: Any, times: int = 1) -> str:
    """把 ``extra`` 骰面乘 ``times`` 后并入 ``base``，返回统一骰式字符串。"""
    if times <= 0 or not _text(extra):
        return _text(base)
    terms, fixed = _dice_terms(base)
    extra_terms, extra_fixed = _dice_terms(extra)
    for sides, count in extra_terms.items():
        terms[sides] = terms.get(sides, 0) + count * times
    fixed += extra_fixed * times
    parts = [f"{count}d{sides}" for sides, count in sorted(terms.items()) if count > 0]
    if fixed:
        parts.append(f"{fixed:+d}")
    return "+".join(parts) if parts else "0"


def apply_upcast(
    entry: Mapping[str, Any], *, slot_level: int | None = None, caster_level: int = 1
) -> dict[str, Any]:
    """按有效环级解析全部升环效果；戏法按角色等级阈值。"""
    level = max(0, _int(entry.get("level")))
    use_level = level if level == 0 else max(level, _int(slot_level, level))
    damage = [
        dict(row) for row in entry.get("damage") or [] if isinstance(row, Mapping)
    ]
    healing = (
        dict(entry.get("healing")) if isinstance(entry.get("healing"), Mapping) else None
    )
    projectiles = 1
    if isinstance(entry.get("projectiles"), Mapping):
        projectiles = max(1, _int(entry.get("projectiles").get("count"), 1))
    target = target_spec(entry)
    target_count = target.get("count")
    duration = duration_spec(entry)
    area = area_spec(entry)
    notes: list[str] = []

    for rule in upcast_rules(entry):
        kind = rule["kind"]
        thresholds = rule.get("thresholds")
        if isinstance(thresholds, list) and thresholds:
            if level != 0:
                continue
            best: Mapping[str, Any] | None = None
            for step in thresholds:
                if not isinstance(step, Mapping):
                    continue
                if caster_level >= _int(step.get("level"), 99):
                    best = step
            if best is None:
                continue
            value = _text(best.get("value"))
            if kind == "damage_dice" and value:
                damage = [{"dice": value, "type": row.get("type") or "force"} for row in damage] or [
                    {"dice": value, "type": "force"}
                ]
                notes.append(f"戏法随等级提升为 {value}")
            continue
        if level == 0:
            continue
        extra = use_level - level
        if extra <= 0:
            continue
        per_level_raw = rule.get("per_level")
        per_level = _int(per_level_raw)
        if kind == "damage_dice":
            extra_dice = _text(per_level_raw)
            if not extra_dice:
                continue
            for row in damage:
                row["dice"] = add_dice(row.get("dice"), extra_dice, extra)
            notes.append(f"升环追加 {extra}×{extra_dice} 伤害")
        elif kind == "healing_dice":
            extra_dice = _text(per_level_raw)
            if not extra_dice:
                continue
            if healing is not None:
                healing["dice"] = add_dice(healing.get("dice"), extra_dice, extra)
            notes.append(f"升环追加 {extra}×{extra_dice} 治疗")
        elif kind == "projectiles":
            if per_level <= 0:
                continue
            projectiles += per_level * extra
            notes.append(f"升环追加 {per_level * extra} 枚飞弹")
        elif kind == "targets":
            if per_level <= 0:
                continue
            target_count = (target_count or 1) + per_level * extra
            notes.append(f"升环追加 {per_level * extra} 个目标")
        elif kind == "duration":
            if per_level <= 0:
                continue
            duration["value"] = _int(duration.get("value")) + per_level * extra
            duration["unit"] = _text(rule.get("unit")) or duration["unit"]
            notes.append(f"升环延长持续时间 {per_level * extra}")
        elif kind == "area_size":
            if per_level <= 0:
                continue
            if area is not None:
                area["size_ft"] = _int(area.get("size_ft")) + per_level * extra
                notes.append(f"升环扩大区域 {per_level * extra} 尺")

    return {
        "spell_level": level,
        "effective_level": use_level,
        "damage": damage,
        "healing": healing,
        "projectiles": projectiles,
        "target_count": target_count,
        "duration": duration,
        "area": area,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 目标合法性
# ---------------------------------------------------------------------------


def _creature_type(target: Mapping[str, Any]) -> str:
    return _text(target.get("creature_type")).casefold()


def _is_object(target: Mapping[str, Any]) -> bool:
    if target.get("is_object") is True:
        return True
    return _text(target.get("kind")).casefold() in {"object", "item", "prop"}


def _cell_of(holder: Mapping[str, Any]) -> tuple[float, float] | None:
    raw_x, raw_y = holder.get("cell_x"), holder.get("cell_y")
    if raw_x is None or raw_y is None:
        return None
    x, y = _number(raw_x), _number(raw_y)
    if x is None or y is None:
        return None
    return x, y


def distance_ft(caster: Mapping[str, Any], target: Mapping[str, Any]) -> int | None:
    """优先用显式距离；否则用格坐标（5e 默认 5 尺/格，Chebyshev）。"""
    explicit = _number(target.get("distance_ft"))
    if explicit is not None:
        return int(round(explicit))
    start = _cell_of(caster)
    end = _cell_of(target)
    if start is None or end is None:
        return None
    cells = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    return int(round(cells * FEET_PER_CELL))


def validate_targets(
    entry: Mapping[str, Any],
    caster: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    *,
    slot_level: int | None = None,
    caster_level: int = 1,
) -> list[str]:
    """执行目标类型、数量、自愿/可见与射程校验；无法得知的信息不臆断。"""
    descriptor = describe(entry)
    upcast = apply_upcast(entry, slot_level=slot_level, caster_level=caster_level)
    spec = descriptor["target"]
    spell_range = descriptor["range"]
    label = _text(entry.get("name")) or _text(entry.get("id")) or "该法术"
    errors: list[str] = []

    allowed_self = {
        _text(caster.get("name")).casefold(),
        _text(caster.get("user_id")).casefold(),
    }
    allowed_self.discard("")
    limit = spell_range.get("feet")
    if spell_range.get("type") == "touch":
        limit = FEET_PER_CELL

    for target in targets:
        if not isinstance(target, Mapping):
            continue
        name = _text(target.get("name")) or "目标"
        target_type = _creature_type(target)
        is_object = _is_object(target)

        if spell_range.get("type") == "self":
            if _text(target.get("name")).casefold() not in allowed_self and _text(
                target.get("user_id")
            ).casefold() not in allowed_self:
                errors.append(f"“{label}”只能作用于施法者自身，不能选择“{name}”。")
                continue
        if spec["type"] == "self" and not is_object:
            if name.casefold() not in allowed_self:
                errors.append(f"“{label}”只能作用于施法者自身，不能选择“{name}”。")
                continue
        if is_object:
            if spec["type"] not in {"object", "any"}:
                errors.append(f"“{label}”只能作用于生物，不能选择物件“{name}”。")
                continue
        elif spec["type"] == "object":
            errors.append(f"“{label}”只能作用于物件，不能选择生物“{name}”。")
            continue
        if target_type and spec["type"] not in {"creature", "any", "object", "self"}:
            if target_type != spec["type"]:
                errors.append(
                    f"“{label}”只能作用于 {spec['type']}，目标“{name}”是 {target_type}。"
                )
                continue
        if target_type and target_type in spec.get("exclude_types", []):
            errors.append(f"“{label}”对 {target_type} 无效。")
            continue
        if spec["voluntary"] and target.get("voluntary") is False:
            errors.append(f"“{label}”需要目标自愿，“{name}”拒绝了。")
            continue
        if spec["visible"] and target.get("visible") is False:
            errors.append(f"“{label}”需要能看见目标，“{name}”不可见。")
            continue
        if limit:
            measured = distance_ft(caster, target)
            if measured is not None and measured > limit:
                errors.append(
                    f"目标“{name}”超出射程（{measured} 尺 > {limit} 尺）。"
                )
                continue

    count = upcast.get("target_count")
    if count is not None and spec["scope"] in {"single", "multi"} and len(targets) > count:
        errors.append(f"“{label}”最多选择 {count} 个目标，实际选择了 {len(targets)} 个。")
    return errors


# ---------------------------------------------------------------------------
# 效果持续时间
# ---------------------------------------------------------------------------


def effect_duration(spec: Mapping[str, Any], *, noncombat: bool = False) -> dict[str, Any]:
    """把结构化持续时间转成效果引擎使用的持续时间。"""
    unit = _text(spec.get("unit"))
    value = max(0, _int(spec.get("value")))
    if noncombat:
        if unit == "minute":
            return {"unit": "minute", "remaining": max(1, value), "timing": "time_advance"}
        if unit == "hour":
            return {"unit": "minute", "remaining": max(1, value * 60), "timing": "time_advance"}
        if unit == "round":
            return {"unit": "minute", "remaining": max(1, value), "timing": "time_advance"}
        return {"unit": "permanent"}
    if unit == "round":
        return {"unit": "round", "remaining": max(1, value), "timing": "round_end"}
    if unit == "minute":
        return {"unit": "round", "remaining": max(1, value * 10), "timing": "round_end"}
    if unit in {"hour", "day"}:
        return {"unit": "scene", "remaining": 1, "timing": "scene_end"}
    return {"unit": "permanent"}


__all__ = [
    "activation_spec",
    "add_dice",
    "apply_upcast",
    "area_spec",
    "describe",
    "distance_ft",
    "duration_spec",
    "effect_duration",
    "range_spec",
    "resolution_spec",
    "target_spec",
    "upcast_rules",
    "validate_targets",
]
