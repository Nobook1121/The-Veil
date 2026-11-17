"""D&D 5e（SRD 5.1）判定数学：属性调整值、熟练加值与检定名解析。

本模块只做纯计算，不掷骰、不写存储；掷骰与展示信封由 ``checks.py`` 组装。
所有名称同时接受英文键、英文全名与中文名，便于 AI 与玩家用自然语言调用。
"""

from __future__ import annotations

from typing import Any, Mapping

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

ABILITY_LABELS = {
    "str": "力量",
    "dex": "敏捷",
    "con": "体质",
    "int": "智力",
    "wis": "感知",
    "cha": "魅力",
}

_ABILITY_ALIASES = {
    "str": "str",
    "strength": "str",
    "力量": "str",
    "dex": "dex",
    "dexterity": "dex",
    "敏捷": "dex",
    "con": "con",
    "constitution": "con",
    "体质": "con",
    "int": "int",
    "intelligence": "int",
    "智力": "int",
    "wis": "wis",
    "wisdom": "wis",
    "感知": "wis",
    "cha": "cha",
    "charisma": "cha",
    "魅力": "cha",
}

# 18 个 SRD 技能：id → (主属性, 中文名, 英文名)。
SKILLS: dict[str, tuple[str, str, str]] = {
    "acrobatics": ("dex", "体操", "Acrobatics"),
    "animal-handling": ("wis", "驯兽", "Animal Handling"),
    "arcana": ("int", "奥秘", "Arcana"),
    "athletics": ("str", "运动", "Athletics"),
    "deception": ("cha", "欺瞒", "Deception"),
    "history": ("int", "历史", "History"),
    "insight": ("wis", "洞悉", "Insight"),
    "intimidation": ("cha", "威吓", "Intimidation"),
    "investigation": ("int", "调查", "Investigation"),
    "medicine": ("wis", "医药", "Medicine"),
    "nature": ("int", "自然", "Nature"),
    "perception": ("wis", "察觉", "Perception"),
    "performance": ("cha", "表演", "Performance"),
    "persuasion": ("cha", "游说", "Persuasion"),
    "religion": ("int", "宗教", "Religion"),
    "sleight-of-hand": ("dex", "巧手", "Sleight of Hand"),
    "stealth": ("dex", "隐匿", "Stealth"),
    "survival": ("wis", "求生", "Survival"),
}

_SKILL_ALIASES: dict[str, str] = {}
for _skill_id, (_ability, _zh, _en) in SKILLS.items():
    _SKILL_ALIASES[_skill_id] = _skill_id
    _SKILL_ALIASES[_skill_id.replace("-", " ")] = _skill_id
    _SKILL_ALIASES[_zh] = _skill_id
    _SKILL_ALIASES[_en.casefold()] = _skill_id

_SAVE_MARKERS = ("豁免", "豁免检定", "saving throw", "save", "saving")


def _text(value: Any) -> str:
    return str(value or "").strip()


def ability_key(value: Any) -> str:
    """把属性名归一成 ``str/dex/...``；无法识别时返回空串。"""
    raw = _text(value)
    if not raw:
        return ""
    return _ABILITY_ALIASES.get(raw.casefold(), _ABILITY_ALIASES.get(raw, ""))


def skill_key(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    return _SKILL_ALIASES.get(raw.casefold(), "")


def _strip_check_suffix(text: str) -> str:
    raw = text.strip()
    for suffix in ("检定", "掷骰", "check", "Check", "CHECK"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
    return raw


def _is_save_text(text: str) -> bool:
    low = text.casefold()
    return any(low.endswith(marker) or f"{marker} " in low for marker in _SAVE_MARKERS)


def _strip_save_marker(text: str) -> str:
    raw = _strip_check_suffix(text)
    low = raw.casefold()
    for marker in ("豁免检定", "豁免", "saving throw", "saving", "save"):
        if low.endswith(marker):
            return raw[: -len(marker)].strip()
    return raw


def parse_check(name: Any) -> dict[str, str] | None:
    """解析检定名。

    返回 ``{"kind": "save"|"skill"|"check", "ability": ..., "skill": ...}``；
    无法识别时返回 ``None``。豁免优先于技能与属性。
    """
    raw = _text(name)
    if not raw:
        return None
    if _is_save_text(raw):
        ability = ability_key(_strip_save_marker(raw))
        return {"kind": "save", "ability": ability} if ability else None
    base = _strip_check_suffix(raw)
    skill = skill_key(base)
    if skill:
        return {"kind": "skill", "skill": skill, "ability": SKILLS[skill][0]}
    ability = ability_key(base)
    if ability:
        return {"kind": "check", "ability": ability}
    return None


def check_label(parsed: Mapping[str, str]) -> str:
    kind = str(parsed.get("kind") or "check")
    if kind == "save":
        return f"{ABILITY_LABELS.get(str(parsed.get('ability')), '')}豁免"
    skill = str(parsed.get("skill") or "")
    if skill and skill in SKILLS:
        return f"{SKILLS[skill][1]}检定"
    return f"{ABILITY_LABELS.get(str(parsed.get('ability')), '')}检定"


def ability_modifier(score: Any) -> int:
    try:
        value = int(score)
    except (TypeError, ValueError):
        value = 10
    return (value - 10) // 2


def proficiency_bonus(level: Any) -> int:
    try:
        value = int(level)
    except (TypeError, ValueError):
        value = 1
    value = max(1, min(20, value))
    return 2 + (value - 1) // 4


def sheet_of(card: Mapping[str, Any] | None) -> dict[str, Any]:
    """读取卡面：优先 ``sheet``，并回填旧卡放在顶层的同名字段。"""
    data = card if isinstance(card, Mapping) else {}
    sheet = data.get("sheet")
    result = dict(sheet) if isinstance(sheet, Mapping) else {}
    for key, value in data.items():
        result.setdefault(str(key), value)
    return result


def _normalized_names(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item or "").strip().casefold() for item in value if str(item or "").strip()}


def ability_score(sheet: Mapping[str, Any], key: str) -> int:
    abilities = sheet.get("abilities")
    if not isinstance(abilities, Mapping):
        abilities = sheet.get("attributes")
    if isinstance(abilities, Mapping):
        for raw_key, raw_value in abilities.items():
            if ability_key(raw_key) == key:
                try:
                    return int(raw_value)
                except (TypeError, ValueError):
                    break
    if key == "str" and sheet.get("STR") is not None:
        try:
            return int(sheet.get("STR"))
        except (TypeError, ValueError):
            return 10
    return 10


def proficiency_flags(sheet: Mapping[str, Any], parsed: Mapping[str, str]) -> tuple[bool, bool]:
    """返回 ``(proficient, expertise)``；非熟练时 expertise 恒为 ``False``。"""
    profs = sheet.get("proficiencies")
    profs = profs if isinstance(profs, Mapping) else {}
    kind = str(parsed.get("kind") or "check")
    if kind == "save":
        ability = str(parsed.get("ability") or "")
        names = _normalized_names(profs.get("saves"))
        return ability.casefold() in names, False
    skill = str(parsed.get("skill") or "")
    if skill:
        names = _normalized_names(profs.get("skills"))
        expertise = _normalized_names(profs.get("expertise"))
        return skill in names or skill in expertise, skill in expertise
    return False, False


def check_bonus(
    sheet: Mapping[str, Any],
    parsed: Mapping[str, str],
    *,
    extra_modifier: int = 0,
) -> tuple[int, dict[str, Any]]:
    """计算一次检定/豁免的总加值，并返回展示所需的拆解。"""
    ability = str(parsed.get("ability") or "")
    level = sheet.get("level", 1)
    prof_bonus = proficiency_bonus(level)
    proficient, expertise = proficiency_flags(sheet, parsed)
    applied_prof = prof_bonus * (2 if expertise else 1) if proficient else 0
    ability_mod = ability_modifier(ability_score(sheet, ability)) if ability else 0
    total = ability_mod + applied_prof + int(extra_modifier or 0)
    return total, {
        "ability": ability,
        "skill": str(parsed.get("skill") or ""),
        "save": str(parsed.get("kind") or "") == "save",
        "proficient": proficient,
        "expertise": expertise,
        "ability_modifier": ability_mod,
        "proficiency_bonus": applied_prof,
        "extra_modifier": int(extra_modifier or 0),
    }
