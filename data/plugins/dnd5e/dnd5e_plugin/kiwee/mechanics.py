"""kiwee 条目 → 结构化数值契约（规则引擎与 UI 详情页共用的提取层）。

**只做数据提取，不接战斗管线**（下一刀再消费）。所有字段都来自 5etools
原始条目与 datatag 标记：取不到就给 ``None``/空串，绝不猜、绝不造值。

法术（:func:`spell_mechanics`）::

    {name, eng_name, level, school, school_code, casting_time, range_ft,
     range_text, components, duration, save_ability, save_ability_zh,
     damage: [{dice, type, type_zh}], scaling: {base, slot_range, per_level,
     text} | None, area: {shape, size_ft}, concentration, ritual,
     deep_link, text}

怪物（:func:`monster_mechanics`）::

    {name, eng_name, ac, ac_from, hp: {average, formula}, cr, cr_numeric,
     size, type, alignment, speed, abilities, passive, languages, deep_link,
     text}

验收锚点（调研文档 §3.1 火球术）：``damage[0].dice == "8d6"``、
``damage[0].type == "fire"``、``save_ability == "dex"``、
``area == {"shape": "sphere", "size_ft": 20}``。
"""

from __future__ import annotations

import re
from typing import Any

from .datatags import parse_entries, render_plain_text

# 5etools 学派码 → 中文（上游渲染口径）。
SCHOOL_ZH: dict[str, str] = {
    "A": "防护",
    "C": "咒法",
    "D": "预言",
    "E": "附魔",
    "V": "塑能",
    "I": "幻术",
    "N": "死灵",
    "T": "变化",
    "P": "灵能",
}

ABILITY_ZH: dict[str, str] = {
    "str": "力量",
    "dex": "敏捷",
    "con": "体质",
    "int": "智力",
    "wis": "感知",
    "cha": "魅力",
}
# savingThrow 里写全名（dexterity）时归一成三字母码，与 datatag / 角色卡一致。
ABILITY_ALIASES: dict[str, str] = {
    "strength": "str",
    "dexterity": "dex",
    "constitution": "con",
    "intelligence": "int",
    "wisdom": "wis",
    "charisma": "cha",
}

DAMAGE_TYPE_ZH: dict[str, str] = {
    "acid": "强酸",
    "bludgeoning": "钝击",
    "cold": "冰霜",
    "fire": "火焰",
    "force": "力场",
    "lightning": "闪电",
    "necrotic": "黯蚀",
    "piercing": "穿刺",
    "poison": "毒素",
    "psychic": "心灵",
    "radiant": "光耀",
    "slashing": "挥砍",
    "thunder": "雷鸣",
}

TIME_UNIT_ZH: dict[str, str] = {
    "action": "动作",
    "bonus": "附赠动作",
    "reaction": "反应",
    "round": "轮",
    "minute": "分钟",
    "hour": "小时",
    "day": "天",
}

DURATION_ZH: dict[str, str] = {
    "instant": "立即",
    "permanent": "永久",
    "special": "特殊",
    "unlimited": "无限",
}

RANGE_ZH: dict[str, str] = {
    "self": "自身",
    "touch": "触碰",
    "sight": "视野",
    "unlimited": "无限",
    "special": "特殊",
}

# 5etools areaTags 首字母 → 引擎用的区域形状名（英文，便于战斗管线判定）。
AREA_SHAPE: dict[str, str] = {
    "S": "sphere",
    "R": "rectangle",
    "C": "cylinder",
    "L": "line",
    "Q": "cube",
    "Y": "cone",
    "H": "hemisphere",
    "N": "cone",
}

# 怪物正文分散在多个块里；不合并成一坨就会漏掉动作/特性（KP 最需要的那半）。
MONSTER_TEXT_BLOCKS = ("entries", "trait", "action", "reaction", "bonus", "legendary", "mythic")

GENERIC_KEYS = (
    "level",
    "school",
    "rarity",
    "attunement",
    "type",
    "size",
    "cr",
    "ac",
    "hp",
    "time",
    "range",
    "components",
    "duration",
    "damageInflict",
    "savingThrow",
    "areaTags",
    "prerequisite",
    "weaponCategory",
    "property",
    "dmg1",
    "dmg2",
    "dmgType",
    "value",
    "weight",
)

_RADIUS_RE = re.compile(r"半径\s*(\d+)\s*尺")
_LINE_RE = re.compile(r"(\d+)\s*尺\s*长")
_TEXT_LIMIT = 2000


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# --- 正文 -------------------------------------------------------------------


def render_entry_text(category: str, raw: Any) -> str:
    """条目正文纯文本（datatag 已解析）：法术含升环段，怪物含动作/特性。"""
    data = _as_dict(raw)
    if category == "monster":
        blocks = [data.get(key) for key in MONSTER_TEXT_BLOCKS if data.get(key)]
        return render_plain_text(blocks)
    if category == "spell":
        blocks = [data.get("entries"), data.get("entriesHigherLevel")]
        return render_plain_text([block for block in blocks if block])
    return render_plain_text(data.get("entries"))


# --- 法术 -------------------------------------------------------------------


def spell_mechanics(raw: Any) -> dict[str, Any]:
    """法术条目的结构化数值（伤害/豁免/升环/区域/施法距离）。"""
    data = _as_dict(raw)
    base = parse_entries(data.get("entries"))
    higher = parse_entries(data.get("entriesHigherLevel"))
    marks = [*base["marks"], *higher["marks"]]
    text = base["text"]
    if higher["text"]:
        text = f"{text}\n{higher['text']}" if text else higher["text"]

    school_code = str(data.get("school") or "").strip().upper()
    damage_types = [str(item).strip() for item in (data.get("damageInflict") or []) if str(item or "").strip()]
    damage: list[dict[str, str]] = []
    for index, mark in enumerate(mark for mark in marks if mark.get("type") == "damage"):
        expr = str(mark.get("expr") or "").strip()
        if not expr:
            continue
        damage_type = damage_types[index] if index < len(damage_types) else (damage_types[0] if damage_types else "")
        damage.append(
            {
                "dice": expr,
                "type": damage_type,
                "type_zh": DAMAGE_TYPE_ZH.get(damage_type, damage_type),
            }
        )

    abilities = [str(item).strip().lower() for item in (data.get("savingThrow") or []) if str(item or "").strip()]
    save_ability = _ability_code(abilities[0]) if abilities else _ability_from_marks(marks)
    range_ft, range_text = _range(data.get("range"))
    duration_item = _duration(data.get("duration"))

    return {
        "name": str(data.get("name") or ""),
        "eng_name": str(data.get("ENG_name") or ""),
        "level": _as_int(data.get("level")),
        "school": SCHOOL_ZH.get(school_code, school_code),
        "school_code": school_code,
        "casting_time": _casting_time(data.get("time")),
        "range_ft": range_ft,
        "range_text": range_text,
        "components": _components(data.get("components")),
        "duration": duration_item["text"],
        "concentration": duration_item["concentration"],
        "ritual": bool(data.get("meta", {}).get("ritual")) if isinstance(data.get("meta"), dict) else False,
        "save_ability": save_ability,
        "save_ability_zh": ABILITY_ZH.get(save_ability, save_ability),
        "damage": damage,
        "scaling": _scaling(marks, higher["text"]),
        "area": _area(data, text, damage_types),
        "text": text[:_TEXT_LIMIT],
    }


def _plain(value: Any) -> str:
    """字符串或字符串列表 → 单行纯文本（datatag 一并解析）。"""
    if isinstance(value, list):
        pieces = [render_plain_text(item) for item in value if str(item or "").strip()]
        return "、".join(piece for piece in pieces if piece)
    if value in (None, ""):
        return ""
    return render_plain_text(value)


_ALIGNMENT_ZH: dict[tuple[str, ...], str] = {
    ("L", "G"): "守序善良",
    ("N", "G"): "中立善良",
    ("C", "G"): "混乱善良",
    ("L", "N"): "守序中立",
    ("N", "N"): "绝对中立",
    ("C", "N"): "混乱中立",
    ("L", "E"): "守序邪恶",
    ("N", "E"): "中立邪恶",
    ("C", "E"): "混乱邪恶",
    ("N",): "绝对中立",
    ("U",): "无阵营",
    ("A",): "任意阵营",
}


def _alignment(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    codes = tuple(str(item).strip().upper() for item in (value if isinstance(value, list) else []) if str(item or "").strip())
    if not codes:
        return ""
    if codes in _ALIGNMENT_ZH:
        return _ALIGNMENT_ZH[codes]
    # 三字母以上（如 ["any"] 之外的自定义）原样留码，方便调用方自行映射。
    return "".join(codes)


def _ability_code(value: str) -> str:
    text = str(value or "").strip().lower()
    return ABILITY_ALIASES.get(text, text)


def _ability_from_marks(marks: list[dict[str, Any]]) -> str:
    for mark in marks:
        if mark.get("type") == "save" and mark.get("ability"):
            return _ability_code(str(mark["ability"]))
    return ""


def _casting_time(value: Any) -> str:
    parts: list[str] = []
    for item in value if isinstance(value, list) else []:
        block = _as_dict(item)
        if not block:
            continue
        number = block.get("number") or 1
        unit = str(block.get("unit") or "").strip().lower()
        label = TIME_UNIT_ZH.get(unit, unit)
        piece = f"{number} {label}".strip() if label else str(number)
        condition = str(block.get("condition") or "").strip()
        if condition:
            piece = f"{piece}（{condition}）"
        parts.append(piece)
    return "，".join(part for part in parts if part)


def _range(value: Any) -> tuple[int | None, str]:
    block = _as_dict(value)
    distance = _as_dict(block.get("distance"))
    kind = str(distance.get("type") or "").strip().lower()
    amount = distance.get("amount")
    if kind == "feet" and _as_int(amount) is not None:
        return _as_int(amount), f"{amount} 尺"
    if kind in RANGE_ZH:
        return None, RANGE_ZH[kind]
    if amount not in (None, ""):
        unit = str(distance.get("type") or "").strip()
        return None, f"{amount} {unit}".strip()
    return None, RANGE_ZH.get(str(block.get("type") or "").strip().lower(), "")


def _duration(value: Any) -> dict[str, Any]:
    parts: list[str] = []
    concentration = False
    for item in value if isinstance(value, list) else []:
        block = _as_dict(item)
        if not block:
            continue
        kind = str(block.get("type") or "").strip().lower()
        if kind == "timed":
            detail = _as_dict(block.get("duration"))
            amount = detail.get("amount")
            unit = str(detail.get("type") or "").strip().lower()
            label = TIME_UNIT_ZH.get(unit, unit)
            piece = f"{amount} {label}".strip() if amount not in (None, "") else label
            if block.get("concentration"):
                concentration = True
                piece = f"专注，{piece}" if piece else "专注"
            parts.append(piece)
        elif kind:
            parts.append(DURATION_ZH.get(kind, kind))
    return {"text": "，".join(part for part in parts if part), "concentration": concentration}


def _components(value: Any) -> str:
    block = _as_dict(value)
    parts: list[str] = []
    if block.get("v"):
        parts.append("V")
    if block.get("s"):
        parts.append("S")
    material = block.get("m")
    if material:
        material_text = material if isinstance(material, str) else render_plain_text(material)
        parts.append(f"M（{material_text}）")
    return "、".join(parts)


def _scaling(marks: list[dict[str, Any]], higher_text: str) -> dict[str, str] | None:
    for mark in marks:
        if mark.get("type") != "scaled_damage":
            continue
        return {
            "base": str(mark.get("base") or ""),
            "slot_range": str(mark.get("slot_range") or ""),
            "per_level": str(mark.get("per_level") or ""),
            "text": higher_text[:400],
        }
    if higher_text.strip():
        return {"base": "", "slot_range": "", "per_level": "", "text": higher_text[:400]}
    return None


def _area(data: dict[str, Any], text: str, damage_types: list[str]) -> dict[str, Any]:
    tags = [str(item).strip().upper() for item in (data.get("areaTags") or []) if str(item or "").strip()]
    shape = AREA_SHAPE.get(tags[0], "") if tags else ""
    range_block = _as_dict(data.get("range"))
    if not shape:
        # 没有 areaTags 时退回施法形状（闪电束这种 line 法术就没有 areaTags）。
        candidate = str(range_block.get("type") or "").strip().lower()
        shape = candidate if candidate in {"line", "cone", "sphere", "cube", "cylinder"} else ""
    size: int | None = None
    radius = _RADIUS_RE.search(text)
    if radius:
        size = int(radius.group(1))
    elif shape == "line":
        line = _LINE_RE.search(text)
        if line:
            size = int(line.group(1))
    if size is None and shape:
        distance = _as_dict(range_block.get("distance"))
        if str(range_block.get("type") or "").strip().lower() == shape and str(distance.get("type") or "") == "feet":
            size = _as_int(distance.get("amount"))
    return {"shape": shape, "size_ft": size, "damage_types": damage_types}


# --- 怪物 -------------------------------------------------------------------


def monster_mechanics(raw: Any) -> dict[str, Any]:
    """怪物核心数值（AC/HP/CR/属性/速度），战斗管线下一刀直接吃。"""
    data = _as_dict(raw)
    ac, ac_from = _armor_class(data.get("ac"))
    hp = _as_dict(data.get("hp"))
    cr = _challenge_rating(data.get("cr"))
    abilities = {
        code: data[code] for code in ABILITY_ZH if _as_int(data.get(code)) is not None
    }
    return {
        "name": str(data.get("name") or ""),
        "eng_name": str(data.get("ENG_name") or ""),
        "ac": ac,
        "ac_from": ac_from,
        "hp": {"average": _as_int(hp.get("average")), "formula": str(hp.get("formula") or "")},
        "cr": cr,
        "cr_numeric": cr_numeric(cr),
        "size": _plain(data.get("size")),
        "type": _monster_type(data.get("type")),
        "alignment": _alignment(data.get("alignment")),
        "speed": {str(k): v for k, v in _as_dict(data.get("speed")).items()},
        "abilities": abilities,
        "passive": _as_int(data.get("passive")),
        "languages": _plain(data.get("languages")),
        "text": render_entry_text("monster", data)[:_TEXT_LIMIT],
    }


def _challenge_rating(value: Any) -> str | None:
    """CR 可能是 "1/4"，也可能是 ``{"cr": "5", "lair": "6"}``（巢穴 CR）。"""
    if isinstance(value, dict):
        value = value.get("cr") or value.get("crNumeric") or ""
    text = str(value or "").strip()
    return text or None


def _armor_class(value: Any) -> tuple[int | None, list[str]]:
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        # 5etools 两种写法都见过：{"ac": 15, "from": [...]} 与裸数值 [16]。
        if isinstance(row, int) and not isinstance(row, bool):
            return row, []
        block = _as_dict(row)
        ac = _as_int(block.get("ac"))
        if ac is None:
            continue
        sources = [_plain(item) for item in (block.get("from") or []) if str(item or "").strip()]
        return ac, [source for source in sources if source]
    return None, []


def _monster_type(value: Any) -> str:
    if isinstance(value, str):
        return value
    block = _as_dict(value)
    base = str(block.get("type") or "")
    tags = [str(item) for item in (block.get("tags") or []) if str(item or "").strip()]
    return f"{base}（{'、'.join(tags)}）" if base and tags else base


def cr_numeric(value: Any) -> float | None:
    """CR 文本 → 数值（"1/4" → 0.25，"17" → 17.0），解析不了回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "/" in text:
        left, _, right = text.partition("/")
        try:
            denominator = float(right)
            return round(float(left) / denominator, 4) if denominator else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


# --- 通用 -------------------------------------------------------------------


def entry_fields(category: str, raw: Any) -> dict[str, Any]:
    """按类目分派结构化字段；未覆盖类目给出字段白名单摘要（不猜语义）。"""
    data = _as_dict(raw)
    if category == "spell":
        return spell_mechanics(data)
    if category == "monster":
        return monster_mechanics(data)
    fields: dict[str, Any] = {}
    for key in GENERIC_KEYS:
        if key in data:
            fields[key] = data[key]
    return fields


__all__ = [
    "ABILITY_ZH",
    "AREA_SHAPE",
    "DAMAGE_TYPE_ZH",
    "RANGE_ZH",
    "SCHOOL_ZH",
    "TIME_UNIT_ZH",
    "cr_numeric",
    "entry_fields",
    "monster_mechanics",
    "render_entry_text",
    "spell_mechanics",
]
