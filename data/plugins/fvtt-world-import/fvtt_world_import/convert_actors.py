"""FVTT dnd5e actor 文档 → 本仓库 dnd5e 角色卡 / 怪物 statblock（纯函数）。

映射依据：``docs/plans/fvtt-world-import-actor-mapping.md``（两包真实数据实测定稿）。

三类产物（路由规则见映射表 §6）：

- ``type=character`` → dnd5e 角色卡载荷（对齐 ``dnd5e_plugin.schema`` 的
  ``sheet`` 字段；信封由核心 ``character_service.apply_save`` 补齐打包）；
- ``type=npc`` → 剧本 NPC 模块（``module_type=npc`` + ``statblock`` 数值块，
  对齐 ``dnd5e_plugin.statblock.combatant_from_module`` 的消费形态，
  ``content`` 是给 AI DM 的可读 statblock 文本）；
- ``type=vehicle`` → 降级条目（无战斗数值，映射表降级清单 #16）。

降级策略：映射表 §4 的 17 条逐项执行；每条降级带稳定 code 进
``degradations`` 计数，人读文本进 ``warnings``。

本模块不落任何数据、不 import 任何规则集插件——法术位 slot 表经
``slot_table(class_identifier, level)`` 回调注入（dnd5e-srd classes.json），
武器代码中文名经 ``weapon_name(code)`` 回调注入（dnd5e-srd equipment.json）。

唯一例外：物品效果的 kiwee 深库补强（``default_kiwee_item_lookup``）会在函数
内部**惰性**打开 ``dnd5e_plugin.kiwee`` 的 sqlite（不 import 时零开销），并且
任何失败都返回 ``None`` 静默跳过——补强是加分项，不能阻断导入。调用方也可以
直接传 ``item_lookup`` 回调覆盖它（测试走这条）。
"""

from __future__ import annotations

import html as _html
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fvtt_world_import.armor_calc import ability_modifier, armor_class

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

#: FVTT 技能缩写 → 我们的 18 技能 id（映射表 §2.4，与 rules.py 逐位对齐）。
SKILL_IDS: dict[str, str] = {
    "acr": "acrobatics",
    "ani": "animal-handling",
    "arc": "arcana",
    "ath": "athletics",
    "dec": "deception",
    "his": "history",
    "ins": "insight",
    "itm": "intimidation",
    "inv": "investigation",
    "med": "medicine",
    "nat": "nature",
    "prc": "perception",
    "prf": "performance",
    "per": "persuasion",
    "rel": "religion",
    "slt": "sleight-of-hand",
    "ste": "stealth",
    "sur": "survival",
}

SIZE_MAP: dict[str, str] = {
    "tiny": "tiny",
    "sm": "small",
    "med": "medium",
    "lg": "large",
    "hug": "huge",
    "gar": "gargantuan",
}
SIZE_ZH: dict[str, str] = {
    "tiny": "微型",
    "small": "小型",
    "medium": "中型",
    "large": "大型",
    "huge": "巨型",
    "gargantuan": "超巨型",
}

#: DMG 挑战等级 → 经验值表（FVTT 不存 xp，按表推导）。
CR_XP: dict[float, int] = {
    0.0: 10,
    0.125: 25,
    0.25: 50,
    0.5: 100,
    1.0: 200,
    2.0: 450,
    3.0: 700,
    4.0: 1100,
    5.0: 1800,
    6.0: 2300,
    7.0: 2900,
    8.0: 3900,
    9.0: 5000,
    10.0: 5900,
    11.0: 7200,
    12.0: 8400,
    13.0: 10000,
    14.0: 11500,
    15.0: 13000,
    16.0: 15000,
    17.0: 18000,
    18.0: 20000,
    19.0: 22000,
    20.0: 25000,
    21.0: 33000,
    22.0: 41000,
    23.0: 50000,
    24.0: 62000,
    25.0: 75000,
    26.0: 90000,
    27.0: 105000,
    28.0: 120000,
    29.0: 135000,
    30.0: 155000,
}

ARMOR_PROF_ZH: dict[str, str] = {
    "lgt": "轻甲",
    "med": "中甲",
    "hvl": "重甲",
    "hvy": "重甲",
    "shl": "盾牌",
}

#: 武器熟练代码兜底表（优先走 ``weapon_name`` 回调查 dnd5e-srd equipment）。
WEAPON_PROF_ZH: dict[str, str] = {
    "sim": "简单武器",
    "mar": "军用武器",
    "club": "木棒",
    "dagger": "匕首",
    "dart": "飞镖",
    "javelin": "标枪",
    "mace": "钉头锤",
    "quarterstaff": "长棍",
    "scimitar": "弯刀",
    "sickle": "镰刀",
    "spear": "长矛",
    "sling": "投石索",
    "shortsword": "短剑",
    "longsword": "长剑",
    "rapier": "细剑",
    "handcrossbow": "手弩",
    "lightcrossbow": "轻弩",
    "heavycrossbow": "重弩",
    "shortbow": "短弓",
    "longbow": "长弓",
    "battleaxe": "战斧",
    "greataxe": "巨斧",
    "greatsword": "巨剑",
    "warhammer": "战锤",
}

LANGUAGE_ZH: dict[str, str] = {
    "common": "通用语",
    "undercommon": "地下通用语",
    "elvish": "精灵语",
    "dwarvish": "矮人语",
    "druidic": "德鲁伊语",
    "halfling": "半身人语",
    "orc": "兽人语",
    "goblin": "地精语",
    "giant": "巨人语",
    "gnomish": "侏儒语",
    "celestial": "天界语",
    "infernal": "地狱语",
    "draconic": "龙语",
    "abyssal": "深渊语",
    "primordial": "原初语",
    "sylvan": "森林语",
    "deep-speech": "深潜语",
    "thieves-cant": "盗贼黑话",
    "auran": "气族语",
    "aquan": "水族语",
    "ignan": "火族语",
    "terran": "土族语",
}

TOOL_ZH: dict[str, str] = {
    "herb": "草药师工具包",
    "herbalism": "草药师工具包",
    "lyre": "里拉琴",
    "lute": "鲁特琴",
    "flute": "长笛",
    "drum": "鼓",
    "horn": "号角",
    "thieves": "盗贼工具",
    "alchemist": "炼金师工具",
    "poisoner": "毒师工具",
    "brewer": "酿造师工具",
    "calligrapher": "书法家工具",
    "carpenter": "木匠工具",
    "cartographer": "制图师工具",
    "cobbler": "鞋匠工具",
    "cook": "厨师工具",
    "glassblower": "玻璃工工具",
    "jeweler": "珠宝匠工具",
    "leatherworker": "皮匠工具",
    "mason": "石匠工具",
    "painter": "画家工具",
    "potter": "陶工工具",
    "smith": "铁匠工具",
    "tinker": "修补匠工具",
    "weaver": "织工工具",
    "woodworker": "木工工具",
    "dice": "骰子组",
    "navigator": "领航工具",
    "disguise": "易容工具",
    "forgery": "伪造工具",
}

DAMAGE_ZH: dict[str, str] = {
    "bludgeoning": "钝击",
    "piercing": "穿刺",
    "slashing": "砍击",
    "fire": "火焰",
    "poison": "毒素",
    "necrotic": "死蚀",
    "radiant": "光耀",
    "psychic": "心灵",
    "cold": "冷冻",
    "lightning": "闪电",
    "thunder": "雷鸣",
    "acid": "酸蚀",
    "force": "原力",
}

CONDITION_ZH: dict[str, str] = {
    "blinded": "目盲",
    "charmed": "魅惑",
    "deafened": "耳聋",
    "exhaustion": "力竭",
    "frightened": "恐惧",
    "grappled": "擒抱",
    "incapacitated": "失能",
    "invisible": "隐形",
    "paralyzed": "麻痹",
    "petrified": "石化",
    "poisoned": "中毒",
    "prone": "倒地",
    "restrained": "束缚",
    "stunned": "震慑",
    "unconscious": "昏迷",
}

CREATURE_TYPE_ZH: dict[str, str] = {
    "aberration": "异怪",
    "beast": "野兽",
    "celestial": "天界生物",
    "construct": "构装体",
    "dragon": "龙类",
    "elemental": "元素生物",
    "fey": "精类",
    "fiend": "邪魔",
    "giant": "巨人",
    "humanoid": "人形生物",
    "monstrosity": "怪物",
    "ooze": "软泥怪",
    "plant": "植物",
    "undead": "不死生物",
}

#: 魔契师契约槽位环阶（L1-4→1 环…，映射表 §2.8 pact）。
PACT_RING_LEVELS: tuple[tuple[int, int], ...] = ((4, 1), (10, 2), (16, 3), (20, 4))

# FVTT 文档内链：[[/item .id]]{label} / @UUID[...]{label} → 只保留 label 文本。
_DOC_LINK_RE = re.compile(r"\[\[/[^\]]*\]\]\{([^}]*)\}")
_UUID_LINK_RE = re.compile(r"@UUID\[[^\]]*\]\{([^}]*)\}")
_EMBED_LINK_RE = re.compile(r"@Embed\[[^\]]*\]\{([^}]*)\}")
# 双语名主名：剥掉尾部 ASCII 段（「法师 Wizard」→「法师」）。
_ASCII_SUFFIX_RE = re.compile(r"\s+[A-Za-z][A-Za-z0-9 ,.'’/()&+:-]*$")

EQUIPMENT_TYPES = ("weapon", "equipment", "consumable", "loot", "tool", "container")
EQUIPMENT_TYPE_ZH: dict[str, str] = {
    "weapon": "武器",
    "equipment": "装备",
    "consumable": "消耗品",
    "loot": "杂物",
    "tool": "工具",
    "container": "容器",
}

PRESET_PREFIX = "预设角色"

MAX_NOTES_CHARS = 6000


# --- 物品效果（FVTT Active Effect）转写 ------------------------------------
#
# 世界包里的 ``!actors.items.effects!`` 文档是 Foundry 的 Active Effect：
# ``transfer``（是否随物品生效）、``disabled``（物品效果自身开关）、
# ``changes``（``[{key, mode, value}]`` 路径式数值修正）。这里只做形态转写，
# **不改写语义**；真正的路径结算在 ``dnd5e_plugin.effect_changes``。
#
# 支持范围的镜像表：dnd5e 插件是运行期才装配的，导入器不能硬依赖它，因此
# 维护一份同口径的键清单用于出告警。tests/test_fvtt_world_import_effects.py
# 会拿真包对账「导入器判为支持 = 引擎真能结算」，两边不许漂移。
EFFECT_NUMBER_KEYS = frozenset(
    {
        "system.attributes.ac.bonus",
        "system.attributes.init.bonus",
        "system.bonuses.abilities.save",
        "system.bonuses.abilities.check",
        "system.bonuses.abilities.ability_check",
        "system.bonuses.abilities.saving_throw",
        "system.bonuses.mwak.attack",
        "system.bonuses.mwak.damage",
        "system.bonuses.rwak.attack",
        "system.bonuses.rwak.damage",
        "system.bonuses.msak.attack",
        "system.bonuses.rsak.attack",
        "system.bonuses.spell.dc",
    }
)
_ABILITY = r"(str|dex|con|int|wis|cha)"
EFFECT_NUMBER_PATTERNS = (
    re.compile(rf"^system\.abilities\.{_ABILITY}\.(check|save)\.roll\.mode$"),
    re.compile(rf"^system\.bonuses\.abilities\.(save|saving_throw)\.{_ABILITY}$"),
    re.compile(rf"^system\.bonuses\.abilities\.(check|ability_check)\.{_ABILITY}$"),
    re.compile(r"^system\.bonuses\.skills\.[a-z-]+\.check$"),
)
EFFECT_LIST_KEYS = frozenset(
    {
        "system.traits.dr.value",
        "system.traits.dv.value",
        "system.traits.di.value",
        "system.traits.ci.value",
        "system.traits.languages.value",
    }
)
EFFECT_TEXT_KEYS = frozenset({"system.attributes.ac.calc", "name"})

#: 引擎模式枚举（与 Foundry ActiveEffect.MODES 同号）。
EFFECT_MODE_NAMES = {0: "custom", 1: "multiply", 2: "add", 3: "downgrade", 4: "upgrade", 5: "override"}

#: 已知但引擎不结算的字段族 → 出处说明（告警文案用）。
EFFECT_UNSUPPORTED_HINTS = {
    "flags.": "模块/系统标志（midi-qol、dnd5e flags）",
    "ATL.": "Token 外观（ATL）",
    "activities[": "物品活动（dnd5e v6 Activities）",
    "items[": "物品数组",
    "effects[": "嵌套效果",
}

# --- kiwee 深库补强：5etools item 结构化字段 → 引擎 changes ----------------
#
# 映射口径（左侧是 5etools item 字段，右侧是引擎能结算的路径）：
#
#   bonusAc            → system.attributes.ac.bonus         （add）
#   bonusSavingThrow   → system.bonuses.abilities.save      （add）
#   bonusWeapon        → mwak/rwak 的 attack + damage       （add，武器类物品）
#   bonusWeaponAttack  → mwak/rwak.attack                   （add）
#   bonusWeaponDamage  → mwak/rwak.damage                   （add）
#   bonusSpellAttack   → msak/rsak.attack                   （add）
#   resist             → system.traits.dr.value             （列表 add）
#   vulnerable         → system.traits.dv.value             （列表 add）
#   immune             → system.traits.di.value             （列表 add）
#   conditionImmune    → system.traits.ci.value             （列表 add）
#
# 值形如 ``"+1"``（字符串，可能带符号）；列表是中文伤害/条件名，按下表翻回
# 英文键（战斗层的伤害类型与条件是英文小写）。查不到、字段为空或该 key 已被
# 世界包 effect 覆盖时跳过——补强只填空缺，不叠加。
KIWEE_ITEM_CHANGE_FIELDS: dict[str, tuple[str, ...]] = {
    "bonusAc": ("system.attributes.ac.bonus",),
    "bonusSavingThrow": ("system.bonuses.abilities.save",),
    "bonusWeapon": (
        "system.bonuses.mwak.attack",
        "system.bonuses.mwak.damage",
        "system.bonuses.rwak.attack",
        "system.bonuses.rwak.damage",
    ),
    "bonusWeaponAttack": ("system.bonuses.mwak.attack", "system.bonuses.rwak.attack"),
    "bonusWeaponDamage": ("system.bonuses.mwak.damage", "system.bonuses.rwak.damage"),
    "bonusSpellAttack": ("system.bonuses.msak.attack", "system.bonuses.rsak.attack"),
}
KIWEE_LIST_CHANGE_FIELDS: dict[str, str] = {
    "resist": "system.traits.dr.value",
    "vulnerable": "system.traits.dv.value",
    "immune": "system.traits.di.value",
    "conditionImmune": "system.traits.ci.value",
}
#: kiwee 中文伤害类型 → 战斗层使用的英文键（srd equipment/damage 口径）。
KIWEE_DAMAGE_TYPES_ZH = {
    "强酸": "acid",
    "钝击": "bludgeoning",
    "寒冷": "cold",
    "冷冻": "cold",
    "火焰": "fire",
    "力场": "force",
    "闪电": "lightning",
    "黯蚀": "necrotic",
    "穿刺": "piercing",
    "毒素": "poison",
    "毒": "poison",
    "心灵": "psychic",
    "光耀": "radiant",
    "挥砍": "slashing",
    "雷鸣": "thunder",
    "非魔法武器攻击": "bludgeoning,piercing,slashing",
}
#: kiwee 中文状态 → 5e 条件键。
KIWEE_CONDITIONS_ZH = {
    "目盲": "blinded",
    "魅惑": "charmed",
    "耳聋": "deafened",
    "力竭": "exhaustion",
    "恐慌": "frightened",
    "受擒": "grappled",
    "失能": "incapacitated",
    "隐形": "invisible",
    "麻痹": "paralyzed",
    "石化": "petrified",
    "中毒": "poisoned",
    "倒地": "prone",
    "束缚": "restrained",
    "震慑": "stunned",
    "昏迷": "unconscious",
}


class _Warn:
    """降级/告警收集器：``(code, text)`` 列表 + 计数。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, code: str, text: str) -> None:
        self.items.append((code, text))

    def texts(self) -> list[str]:
        return [text for _code, text in self.items]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for code, _text in self.items:
            result[code] = result.get(code, 0) + 1
        return result

    def extend(self, other: "_Warn") -> None:
        self.items.extend(other.items)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result


def _as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _main_name(name: Any) -> str:
    """双语名主名：剥尾部 ASCII 段；纯 ASCII 名保持原样。"""
    text = str(name or "").strip()
    if not text:
        return ""
    stripped = _ASCII_SUFFIX_RE.sub("", text).strip()
    return stripped or text


def _clean_text(value: Any) -> str:
    """HTML / FVTT 内链 → 纯文本（降级清单 #14：只留 label，不解析引用）。"""
    text = str(value or "")
    text = _DOC_LINK_RE.sub(r"\1", text)
    text = _UUID_LINK_RE.sub(r"\1", text)
    text = _EMBED_LINK_RE.sub(r"\1", text)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*li\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first_int(*values: Any, default: int | None = None) -> int | None:
    for value in values:
        result = _as_int_or_none(value)
        if result is not None:
            return result
    return default


def _fraction_cr(cr: Any) -> str:
    """数值 CR → 字符串（0.25 → "1/4"；字符串原样兼容，映射表待实测 #5）。"""
    if isinstance(cr, str):
        text = cr.strip()
        if not text:
            return ""
        try:
            return _fraction_cr(float(text))
        except ValueError:
            return text
    value = _as_float(cr, -1.0)
    if value < 0:
        return ""
    known = {0.125: "1/8", 0.25: "1/4", 0.5: "1/2"}
    if value in known:
        return known[value]
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _cr_number(cr: Any) -> float:
    if isinstance(cr, str):
        text = cr.strip().lower()
        table = {"1/8": 0.125, "1/4": 0.25, "1/2": 0.5}
        if text in table:
            return table[text]
        return _as_float(text, 0.0)
    return _as_float(cr, 0.0)


def proficiency_for_cr(cr: Any) -> int:
    """NPC 熟练加值 = ``2 + (max(1, ceil(CR)) - 1) // 4``（两包全量对账）。"""
    import math

    value = max(1.0, math.ceil(_cr_number(cr)))
    return 2 + (int(value) - 1) // 4


def xp_for_cr(cr: Any) -> int:
    value = _cr_number(cr)
    if value in CR_XP:
        return CR_XP[value]
    return CR_XP.get(float(int(value)), 10)


# -- 共用子结构 -----------------------------------------------------------


def _abilities(system: Mapping[str, Any]) -> dict[str, int]:
    source = _mapping(system.get("abilities"))
    result: dict[str, int] = {}
    for key in ABILITY_KEYS:
        row = _mapping(source.get(key))
        result[key] = _as_int(row.get("value"), 10)
    return result


def _save_proficiencies(system: Mapping[str, Any]) -> list[str]:
    source = _mapping(system.get("abilities"))
    return [key for key in ABILITY_KEYS if _as_int(_mapping(source.get(key)).get("proficient")) > 0]


def _ability_score_note(abilities: Mapping[str, int]) -> str:
    return "、".join(f"{key.upper()} {abilities[key]}({ability_modifier(abilities[key]):+d})" for key in ABILITY_KEYS)


def _languages(system: Mapping[str, Any], warn: _Warn) -> list[str]:
    traits = _mapping(system.get("traits"))
    source = _mapping(traits.get("languages"))
    result: list[str] = []
    for code in source.get("value") or []:
        text = str(code or "").strip()
        if not text or text == "custom":
            continue  # custom 是「看 custom 字段」的标记位，不是语言。
        if text in LANGUAGE_ZH:
            result.append(LANGUAGE_ZH[text])
        else:
            result.append(text)
            warn.add("language.unknown_code", f"语言代码「{text}」不在对照表，按原文导入。")
    custom = str(source.get("custom") or "").strip()
    if custom:
        result.append(custom)
    return result


def _speeds(movement: Mapping[str, Any]) -> dict[str, int]:
    """``{"walk": 30, "climb": 20, ...}``（字符串数值 → int，0 剔除）。"""
    result: dict[str, int] = {}
    for key in ("walk", "burrow", "climb", "fly", "swim"):
        value = _as_int_or_none(movement.get(key))
        if value is not None and value > 0:
            result[key] = value
    return result


def _speed_label(speeds: Mapping[str, int]) -> str:
    order = ("walk", "burrow", "climb", "fly", "swim")
    zh = {"walk": "步行", "burrow": "挖掘", "climb": "攀爬", "fly": "飞行", "swim": "游泳"}
    parts = []
    for key in order:
        if key in speeds:
            parts.append(f"{zh[key]} {speeds[key]} 尺")
    return "，".join(parts)


def _currency_row(system: Mapping[str, Any]) -> dict[str, Any] | None:
    """货币没有专门字段 → 合成一条装备行（降级清单 #5）。"""
    source = _mapping(system.get("currency"))
    parts = []
    for coin in ("pp", "gp", "ep", "sp", "cp"):
        value = _as_int(source.get(coin), 0)
        if value > 0:
            parts.append(f"{value} {coin}")
    if not parts:
        return None
    return {"name": "货币", "quantity": 1, "weight": 0, "notes": "、".join(parts)}


def _statuses_note(effect_docs: list[Mapping[str, Any]]) -> str:
    """actor 级 effect 的 ``statuses`` → 「当前状态」一行（静态卡不带运行态）。"""
    names: list[str] = []
    for doc in effect_docs or []:
        for status in doc.get("statuses") or []:
            text = str(status or "").strip()
            if text and text not in names:
                names.append(CONDITION_ZH.get(text, text))
    return "、".join(names)


def _feat_sections(item_docs: list[Mapping[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    """NPC 内嵌 feat 分桶：traits / actions(multiattack) / reactions(parry) / spellcasting 散文。

    识别靠 ``system.identifier``（两包中英文条目同名不同文，identifier 稳定）。
    """
    traits: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []
    reactions: list[dict[str, str]] = []
    spellcasting_notes: dict[str, str] = {}
    for item in item_docs:
        if item.get("type") != "feat":
            continue
        system = _mapping(item.get("system"))
        identifier = str(system.get("identifier") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        description = _mapping(system.get("description"))
        entry = {"name": name, "identifier": identifier, "text": _clean_text(description.get("value"))}
        if identifier == "multiattack":
            entry["type"] = "multiattack"
            actions.append(entry)
        elif identifier == "parry":
            entry["type"] = "parry"
            reactions.append(entry)
        elif identifier == "spellcasting":
            spellcasting_notes[name or "施法"] = entry["text"]
        else:
            traits.append(entry)
    return traits, actions, reactions, spellcasting_notes


def _formula_warn(warn: _Warn, code: str, where: str, value: Any) -> bool:
    """公式串守卫：空串/“0”放行；其他进警告（降级清单 #3，不求值）。"""
    text = str(value or "").strip()
    if not text or text == "0":
        return False
    warn.add(code, f"{where}的公式串「{text[:80]}」无法求值，已按文本保留。")
    return True


# -- character -----------------------------------------------------------


def _class_rows(item_docs: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in item_docs:
        if item.get("type") != "class":
            continue
        system = _mapping(item.get("system"))
        rows.append(
            {
                # 内嵌条目 _id 是复合键 <actorId>.<itemId>；details.originalClass
                # 引用的是其中的 <itemId> 段。
                "source_id": str(item.get("_id") or "").split(".", 1)[-1],
                "name": str(item.get("name") or ""),
                "identifier": str(system.get("identifier") or "").strip().lower(),
                "levels": max(1, _as_int(system.get("levels"), 1)),
                "hd": _mapping(_mapping(system.get("hd")).get("denomination")) or {},
                "hd_denomination": str(_mapping(system.get("hd")).get("denomination") or "").strip(),
                "hd_spent": _as_int(_mapping(system.get("hd")).get("spent"), 0),
                "spellcasting": _mapping(system.get("spellcasting")),
            }
        )
    return rows


def _hit_dice(classes: list[dict[str, Any]]) -> str:
    parts = []
    for row in classes:
        denomination = row["hd_denomination"]
        if not denomination:
            continue
        if not denomination.startswith("d"):
            denomination = f"d{denomination}"
        parts.append(f"{row['levels']}{denomination}")
    return "+".join(parts)


def _derive_hp_max(
    hp: Mapping[str, Any],
    classes: list[dict[str, Any]],
    con_mod: int,
    warn: _Warn,
) -> tuple[int, str]:
    """PC 的 hp.max 可为 null（FVTT 运行时推导不落盘）→ 按职业骰推导。

    规则：首个职业（``details.originalClass`` 匹配优先）1 级取骰面最大值、
    其余等级取期望（骰面/2+1）；副职业全期望；再加体质调整值 × 总等级。
    推导值低于当前值（不合理）时按当前值导入并警告。
    """
    stored = _as_int_or_none(hp.get("max"))
    if stored is not None and stored > 0:
        return max(1, stored), ""
    current = _as_int(hp.get("value"), 1)
    if not classes:
        warn.add("hp.max_fallback_value", "没有职业条目，HP 上限按当前值导入（导入即满血）。")
        return max(1, current), "HP 上限未导出且无职业条目，按当前值导入。"
    primary = classes[0]
    derived = 0
    for index, row in enumerate(classes):
        denomination = row["hd_denomination"].lstrip("d")
        sides = _as_int(denomination, 8)
        levels = row["levels"]
        average = sides // 2 + 1
        if index == 0:
            derived += sides + (levels - 1) * average
        else:
            derived += levels * average
    derived += con_mod * sum(row["levels"] for row in classes)
    if derived <= 0 or derived < current:
        warn.add(
            "hp.max_fallback_value",
            f"按职业骰推导的 HP 上限（{derived}）低于当前值（{current}），按当前值导入。",
        )
        return max(1, current), "HP 上限按当前值导入。"
    note = f"HP 上限按职业骰推导为 {derived}（FVTT 未导出 max）。"
    if derived != current:
        warn.add("hp.max_derived", note)
    return max(1, derived), note


def _pc_speed(
    system: Mapping[str, Any],
    item_docs: list[Mapping[str, Any]],
    warn: _Warn,
) -> tuple[int, str]:
    """PC 的速度在 race item 散文里（actor 上的 movement 无数值）。"""
    for item in item_docs:
        if item.get("type") != "race":
            continue
        movement = _mapping(_mapping(item.get("system")).get("movement"))
        value = _as_int_or_none(movement.get("walk"))
        if value is not None and value > 0:
            return value, f"速度取自种族条目「{_main_name(item.get('name'))}」。"
    warn.add("speed.default", "没有可解析的速度数据，按默认 30 尺导入。")
    return 30, "速度按默认 30 尺导入（源包未提供）。"


def _slot_max(
    row: Mapping[str, Any],
    table_row: list[int] | None,
    warn: _Warn,
    level: int,
) -> int:
    """``max = override ?? 我们slot表[level] ?? value``；无表职业 value 当 max + 警告。"""
    override = _as_int_or_none(row.get("override"))
    if override is not None:
        return override
    if table_row and 1 <= level <= len(table_row) and table_row[level - 1] > 0:
        return table_row[level - 1]
    value = _as_int(row.get("value"), 0)
    if value > 0:
        warn.add(
            "slots.value_as_max",
            "该职业不在法术位表内，法术位按源包剩余值导入为上限（导入即满槽）。",
        )
    return value


def _pact_ring(levels: int) -> int:
    for threshold, ring in PACT_RING_LEVELS:
        if levels <= threshold:
            return ring
    return 4


def _spellcasting(
    system: Mapping[str, Any],
    classes: list[dict[str, Any]],
    item_docs: list[Mapping[str, Any]],
    slot_table: Callable[[str, int], list[int] | None] | None,
    warn: _Warn,
) -> tuple[dict[str, Any], list[str]]:
    """施法档案：ability（actor 字段优先）+ known/prepared + slots。"""
    attributes = _mapping(system.get("attributes"))
    ability = str(attributes.get("spellcasting") or "").strip().lower()
    if not ability:
        for row in classes:
            declared = str(row["spellcasting"].get("ability") or "").strip().lower()
            if declared:
                ability = declared
                break
    known: list[str] = []
    prepared: list[str] = []
    identifiers: list[str] = []
    for item in item_docs:
        if item.get("type") != "spell":
            continue
        item_system = _mapping(item.get("system"))
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if _as_int(item_system.get("prepared")) > 0:
            prepared.append(name)
        else:
            known.append(name)
        identifier = str(item_system.get("identifier") or "").strip()
        if identifier:
            identifiers.append(identifier)
        uses = _mapping(item_system.get("uses"))
        if str(uses.get("max") or "").strip():
            warn.add(
                "spell.uses_dropped",
                f"法术「{name}」带限量使用次数（{uses.get('max')}），当前无字段承载，已降级。",
            )

    spells = _mapping(system.get("spells"))
    spell0 = _mapping(spells.get("spell0"))
    if str(spell0.get("override") or "").strip():
        warn.add("slots.cantrip_override", "0 环戏法池 override 无对应概念，已忽略。")

    slots: dict[str, dict[str, int]] = {}
    total_levels = sum(row["levels"] for row in classes)
    # 施法等级（查表用）：全施法者按总等级；半施法者按折半向上取整；
    # 其余（pact/none）没有表 → ``value`` 当 max（导入即满槽）+ 警告。
    import math

    table_levels = 0
    for row in classes:
        progression = str(row["spellcasting"].get("progression") or "").strip().lower()
        if progression == "full":
            table_levels = max(table_levels, total_levels)
        elif progression == "half":
            table_levels = max(table_levels, max(1, math.ceil(total_levels / 2)))
    for level in range(1, 10):
        row = _mapping(spells.get(f"spell{level}"))
        if not row:
            continue
        table_row = None
        if slot_table is not None and table_levels:
            for row_class in classes:
                found = slot_table(row_class["identifier"], table_levels)
                if found:
                    table_row = found
                    break
        maximum = _slot_max(row, table_row, warn, level)
        if maximum <= 0:
            continue
        value = _as_int(row.get("value"), 0)
        slots[str(level)] = {"max": maximum, "used": max(0, min(maximum, maximum - value))}

    pact_notes: list[str] = []
    pact = _mapping(spells.get("pact"))
    pact_value = _as_int(pact.get("value"), 0) or _as_int(pact.get("override"), 0)
    if pact_value > 0:
        ring = _pact_ring(total_levels)
        key = str(ring)
        existing = slots.get(key)
        if existing and existing["max"] != pact_value:
            warn.add(
                "slots.pact_conflict",
                f"契约槽（{pact_value} 个）与 {ring} 环法术位（{existing['max']} 个）不一致，取较大值。",
            )
            pact_value = max(existing["max"], pact_value)
        slots[key] = {"max": pact_value, "used": 0}
        pact_notes.append(f"契约魔法槽 {pact_value} 个（{ring} 环，短休恢复），已并入 {ring} 环法术位。")

    profile: dict[str, Any] = {}
    if ability:
        profile["ability"] = ability
    if known:
        profile["known"] = known
    if prepared:
        profile["prepared"] = prepared
    if slots:
        profile["slots"] = slots
    if identifiers:
        profile["identifiers"] = identifiers
    return profile, pact_notes


def effect_key_supported(key: Any) -> bool:
    """这个 change 的 key 是否有引擎消费方（镜像 ``effect_changes`` 的口径）。

    公开给 :mod:`fvtt_world_import.world_package` 的审计报告用——「哪些路径
    真能结算」的口径只许有一份，两边不许各写一个。
    """
    text = str(key or "").strip()
    if not text:
        return False
    if text in EFFECT_NUMBER_KEYS or text in EFFECT_LIST_KEYS or text in EFFECT_TEXT_KEYS:
        return True
    return any(pattern.match(text) for pattern in EFFECT_NUMBER_PATTERNS)


def _effect_unsupported_hint(key: Any) -> str:
    text = str(key or "").strip()
    for prefix, hint in EFFECT_UNSUPPORTED_HINTS.items():
        if text.startswith(prefix):
            return hint
    return ""


def _effect_changes_of(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``changes`` 原样转写：key/mode/value 三件套，mode 归一成整数。"""
    rows: list[dict[str, Any]] = []
    for raw in doc.get("changes") or []:
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        rows.append(
            {
                "key": key,
                "mode": _as_int(raw.get("mode"), 2),
                "value": raw.get("value"),
            }
        )
    return rows


def _effect_entry(
    doc: Mapping[str, Any],
    *,
    item_id: str,
    source: str,
    equipped: bool,
) -> dict[str, Any]:
    """单个 FVTT effect 文档 → 引擎数据形态（``dnd5e_plugin.effect_changes``）。"""
    entry: dict[str, Any] = {
        "id": str(doc.get("_id") or "").split(".")[-1],
        "name": _clean_text(doc.get("name")),
        "transfer": bool(doc.get("transfer")),
        "disabled": bool(doc.get("disabled")),
        "changes": _effect_changes_of(doc),
        "origin": {"item_id": item_id, "source": source, "equipped": bool(equipped)},
    }
    duration = _mapping(doc.get("duration"))
    kept = {
        key: _as_int(duration.get(key))
        for key in ("seconds", "rounds", "turns")
        if _as_int_or_none(duration.get(key)) is not None
    }
    if kept:
        entry["duration"] = kept
    return entry


def _split_item_effects(
    item_docs: list[Mapping[str, Any]],
    effect_docs: list[Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[Mapping[str, Any]]]:
    """把 effect 文档按父物品分组。

    复合 ``_id`` 形如 ``<actorId>.<itemId>.<effectId>``；两段的
    （``<actorId>.<effectId>``）是 actor 级效果（statuses 等），交给现有
    条件语义，不在这里重建。
    """
    by_item_id = {str(doc.get("_id") or ""): doc for doc in item_docs}
    grouped: dict[str, list[dict[str, Any]]] = {}
    actor_level: list[Mapping[str, Any]] = []
    for doc in effect_docs or []:
        if not isinstance(doc, Mapping):
            continue
        parts = str(doc.get("_id") or "").split(".")
        if len(parts) < 3:
            actor_level.append(doc)
            continue
        item_key = ".".join(parts[:2])
        item = by_item_id.get(item_key)
        if item is None:
            actor_level.append(doc)
            continue
        item_system = _mapping(item.get("system"))
        entry = _effect_entry(
            doc,
            item_id=str(item.get("_id") or parts[-2]),
            source=str(item.get("type") or ""),
            equipped=_item_effects_equipped(item),
        )
        grouped.setdefault(item_key, []).append(entry)
    return grouped, actor_level


def _item_effects_equipped(item: Mapping[str, Any]) -> bool:
    """物品效果是否随物品生效：装备/武器看 ``system.equipped``，其余按被动。

    专长、种族特性、法术这类条目在 FVTT 里没有「装备」概念，挂在角色上即
    被动生效；只有 weapon/equipment/consumable/loot/tool/container 这些
    装备类才要求 ``system.equipped``。
    """
    if str(item.get("type") or "") not in EQUIPMENT_TYPES:
        return True
    return bool(_mapping(item.get("system")).get("equipped"))


def _flatten_effects(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        result.extend(row.get("effects") or [])
    return result


def _effects_summary(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """效果小结：文档数 / change 数 / 生效候选 / 不支持键（前 5 个）。"""
    rows = [entry for entry in entries if isinstance(entry, Mapping)]
    changes = [change for entry in rows for change in entry.get("changes") or []]
    unsupported = [
        str(change.get("key") or "") for change in changes if not effect_key_supported(change.get("key"))
    ]
    top: list[str] = []
    for key in unsupported:
        if key and key not in top:
            top.append(key)
        if len(top) >= 5:
            break
    return {
        "documents": len(rows),
        "changes": len(changes),
        "transfer": sum(1 for entry in rows if entry.get("transfer")),
        "unsupported": len(unsupported),
        "unsupported_keys": top,
    }


def _warn_effects(warn: _Warn, summary: Mapping[str, Any], where: str) -> None:
    """不支持的 key 出 ``effects.unsupported`` 告警（列前 5 个 + 出处）。"""
    count = _as_int(summary.get("unsupported"), 0)
    if count <= 0:
        return
    keys = [str(key) for key in summary.get("unsupported_keys") or [] if str(key)]
    hints = []
    for key in keys:
        hint = _effect_unsupported_hint(key)
        if hint and hint not in hints:
            hints.append(hint)
    text = f"{where} {count} 条效果改动（change）没有引擎消费方，已忽略：{'、'.join(keys)}"
    if hints:
        text += f"（{'；'.join(hints)}）"
    warn.add("effects.unsupported", text)


# --- kiwee 深库补强 --------------------------------------------------------


def default_kiwee_item_lookup() -> Callable[[str], dict[str, Any] | None] | None:
    """惰性打开 dnd5e 的 kiwee 深库，返回「按名字取 item」的回调。

    深库未建、插件未装配或任何异常都返回 ``None``（静默降级）：补强是加分项，
    不该让整包导入失败。
    """
    try:
        from dnd5e_plugin.kiwee import kiwee_db_path

        db_path = kiwee_db_path()
        if not Path(db_path).is_file():
            return None
        from dnd5e_plugin.kiwee.db import open_kiwee_db

        db = open_kiwee_db(db_path)
    except Exception:  # noqa: BLE001 - 深库缺席/版本不符/导入失败一律降级
        return None
    if db is None:
        return None

    def lookup(name: str) -> dict[str, Any] | None:
        try:
            found = db.get("item", name)
        except Exception:  # noqa: BLE001 - 单条查询失败不影响导入
            return None
        if not isinstance(found, Mapping):
            return None
        raw = found.get("raw")
        if not isinstance(raw, Mapping):
            return None
        if not _kiwee_same_item(name, found, raw):
            # 词表补强**必须同名**。``db.get`` 拿不到 uid 时会退化成全文检索的
            # 首条：普通「匕首」会命中「真冰碎片（匕首）」，「皮甲」会命中
            # 「魔像皮甲」——把魔法物品的加值算到角色的凡品上是凭空改数
            # （实测卓尔法师的普通匕首会多出 +3 AC）。对不上名就当作查不到。
            return None
        return dict(raw)

    return lookup


_KIWEE_NAME_NOISE = re.compile(r"[\s·.,，。、:：;；'\"‘’“”()（）\[\]【】<>《》\-—_/+＋]+")


def _kiwee_name_key(value: Any) -> str:
    """名字归一：去空白/标点/括号 + 大小写折叠（「防护戒指 (DMG)」→「防护戒指dmg」）。"""
    return _KIWEE_NAME_NOISE.sub("", str(value or "")).casefold()


def _kiwee_same_item(name: str, found: Mapping[str, Any], raw: Mapping[str, Any]) -> bool:
    """kiwee 命中的条目是不是**同一件**物品（名字/别名/英文名逐字相等）。"""
    target = _kiwee_name_key(name)
    if not target:
        return False
    candidates: list[Any] = [
        raw.get("name"),
        found.get("name"),
        found.get("eng_name"),
        found.get("engName"),
    ]
    aliases = found.get("aliases")
    if isinstance(aliases, str):
        candidates.extend(aliases.split(","))
    elif isinstance(aliases, (list, tuple)):
        candidates.extend(aliases)
    return any(_kiwee_name_key(item) == target for item in candidates if item)


def _kiwee_bonus_value(value: Any) -> int | None:
    """``"+1"`` / ``"1"`` / ``1`` → 整数；公式或空值返回 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("+"):
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return None


def _kiwee_name_list(value: Any, table: Mapping[str, str]) -> list[str]:
    """kiwee 的中文列表字段 → 英文键列表（查不到就保留原文并转小写）。"""
    raw = value if isinstance(value, (list, tuple)) else ([value] if value else [])
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        resolved = table.get(text) or text.casefold()
        for part in str(resolved).split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return result


def _kiwee_changes(item: Mapping[str, Any], covered: set[str]) -> list[dict[str, Any]]:
    """kiwee item 的结构化加值 → 引擎 changes（跳过世界包已覆盖的 key）。"""
    changes: list[dict[str, Any]] = []
    for field, keys in KIWEE_ITEM_CHANGE_FIELDS.items():
        value = _kiwee_bonus_value(item.get(field))
        if value is None or value == 0:
            continue
        for key in keys:
            if key in covered:
                continue
            covered.add(key)
            changes.append({"key": key, "mode": 2, "value": value})
    for field, key in KIWEE_LIST_CHANGE_FIELDS.items():
        table = KIWEE_CONDITIONS_ZH if key.endswith("ci.value") else KIWEE_DAMAGE_TYPES_ZH
        values = _kiwee_name_list(item.get(field), table)
        if not values or key in covered:
            continue
        for value in values:
            changes.append({"key": key, "mode": 2, "value": value})
    return changes


def _kiwee_effect_entry(
    *,
    name: str,
    item_id: str,
    source: str,
    equipped: bool,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """补强出来的效果条目：与包内 effect 同形，``origin.source`` 标明来路。"""
    return {
        "id": f"kiwee:{item_id or name}"[:120],
        "name": f"kiwee：{name}"[:120],
        "transfer": True,
        "disabled": False,
        "changes": changes,
        "origin": {"item_id": item_id, "source": f"kiwee:{source}", "equipped": bool(equipped)},
    }


def _enrich_effects_with_kiwee(
    rows: list[dict[str, Any]],
    items_by_name: Mapping[str, dict[str, Any]],
    lookup: Callable[[str], dict[str, Any] | None],
    warn: _Warn,
) -> dict[str, int]:
    """给装备行补 kiwee 结构化加值；返回 ``{lookups, enriched}`` 计数。"""
    stats = {"lookups": 0, "enriched": 0}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        item = items_by_name.get(name) or {}
        if str(item.get("type") or "") not in EQUIPMENT_TYPES:
            continue
        stats["lookups"] += 1
        found = lookup(name)
        if not isinstance(found, Mapping):
            continue
        covered = {
            str(change.get("key") or "")
            for entry in row.get("effects") or []
            for change in entry.get("changes") or []
        }
        changes = _kiwee_changes(found, covered)
        if not changes:
            continue
        row.setdefault("effects", []).append(
            _kiwee_effect_entry(
                name=name,
                item_id=str(item.get("_id") or ""),
                source=str(item.get("type") or ""),
                equipped=bool(row.get("equipped")),
                changes=changes,
            )
        )
        stats["enriched"] += 1
    return stats


def _equipment_rows(
    system: Mapping[str, Any],
    item_docs: list[Mapping[str, Any]],
    warn: _Warn,
    *,
    effects_by_item: Mapping[str, list[dict[str, Any]]] | None = None,
    kiwee_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    kiwee_stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """装备清单行 ``{name, quantity, weight, notes, effects}``（含货币行与容器归属标注）。"""
    container_names: dict[str, str] = {}
    for item in item_docs:
        if item.get("type") == "container":
            container_names[str(item.get("_id") or "")] = _main_name(item.get("name")) or str(item.get("name") or "容器")
    effects_index = effects_by_item or {}
    # 名称 → 物品文档（kiwee 补强按名字查结构化字段）。
    items_by_name: dict[str, dict[str, Any]] = {}
    for item in item_docs:
        item_name = _main_name(item.get("name")) or str(item.get("name") or "").strip()
        if item_name:
            items_by_name.setdefault(item_name, dict(item))
    rows: list[dict[str, Any]] = []
    for item in item_docs:
        kind = str(item.get("type") or "")
        if kind not in EQUIPMENT_TYPES:
            continue
        item_system = _mapping(item.get("system"))
        name = _main_name(item.get("name")) or str(item.get("name") or "").strip()
        if not name:
            continue
        notes: list[str] = [EQUIPMENT_TYPE_ZH.get(kind, kind)]
        if item_system.get("equipped"):
            notes.append("已装备")
        attuned = bool(item_system.get("attuned"))
        attunement = str(item_system.get("attunement") or "").strip()
        if attunement:
            notes.append(f"需同调{'（已同调）' if attuned else ''}：{attunement[:60]}")
        elif attuned:
            notes.append("已同调")
        rarity = str(item_system.get("rarity") or "").strip()
        if rarity and rarity != "common":
            notes.append(rarity)
        item_type = _mapping(item_system.get("type"))
        base_item = str(item_type.get("baseItem") or "").strip()
        if base_item:
            notes.append(f"baseItem:{base_item}")
        armor = _mapping(item_system.get("armor"))
        if kind == "equipment" and _as_int_or_none(armor.get("value")) is not None:
            armor_type = str(item_type.get("value") or "")
            notes.append(f"{ARMOR_PROF_ZH.get(armor_type, armor_type)}({armor.get('value')})")
        price = _mapping(item_system.get("price"))
        price_value = _as_float(price.get("value"), 0.0)
        if price_value > 0:
            notes.append(f"{price_value:g} {str(price.get('denomination') or 'gp')}")
        parent = str(item_system.get("container") or "").strip()
        if parent and parent in container_names:
            notes.append(f"在「{container_names[parent]}」内")
        weight = _mapping(item_system.get("weight"))
        row: dict[str, Any] = {
            "name": name,
            "quantity": max(1, _as_int(item_system.get("quantity"), 1)),
            "weight": _as_float(weight.get("value"), 0.0),
            "notes": "；".join(notes),
        }
        # 只写 True：未装备的行走「缺键」分支，战斗层的
        # ``_equipment_items`` 默认按已装备处理，写 False 会悄悄改变攻击/负重口径。
        if item_system.get("equipped"):
            row["equipped"] = True
        item_effects = effects_index.get(str(item.get("_id") or ""))
        if item_effects:
            row["effects"] = [dict(entry) for entry in item_effects]
        rows.append(row)
    if kiwee_lookup is not None:
        stats = _enrich_effects_with_kiwee(rows, items_by_name, kiwee_lookup, warn)
        if kiwee_stats is not None:
            kiwee_stats.update(stats)
    currency = _currency_row(system)
    if currency is not None:
        rows.append(currency)
    return rows


def _formula_scans(system: Mapping[str, Any], warn: _Warn) -> None:
    """公式串字段巡检（样本几乎全空；非空即降级告警，降级清单 #3）。"""
    attributes = _mapping(system.get("attributes"))
    _formula_warn(warn, "formula.init_bonus", "先攻加值", _mapping(attributes.get("init")).get("bonus"))
    abilities = _mapping(system.get("abilities"))
    for key in ABILITY_KEYS:
        bonuses = _mapping(_mapping(abilities.get(key)).get("bonuses"))
        _formula_warn(warn, f"formula.ability.{key}", f"{key.upper()} 属性加值", bonuses.get("check"))
        _formula_warn(warn, f"formula.ability.{key}.save", f"{key.upper()} 豁免加值", bonuses.get("save"))
    bonuses = _mapping(system.get("bonuses"))
    for key, value in bonuses.items():
        if isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                _formula_warn(warn, f"formula.bonuses.{key}.{sub_key}", f"加值 {key}.{sub_key}", sub_value)
        else:
            _formula_warn(warn, f"formula.bonuses.{key}", f"加值 {key}", value)
    init_ability = str(attributes.get("init") and _mapping(attributes.get("init")).get("ability") or "").strip()
    if init_ability:
        warn.add("formula.init_ability", f"先攻使用 {init_ability.upper()} 而非敏捷，卡面按敏捷派生，原文已记入备注。")
    skills = _mapping(system.get("skills"))
    for abbr, row in skills.items():
        bonuses = _mapping(row.get("bonuses")) if isinstance(row, Mapping) else {}
        _formula_warn(warn, f"formula.skill.{abbr}", f"技能 {abbr} 加值", bonuses.get("check"))


def _character(
    actor: Mapping[str, Any],
    item_docs: list[Mapping[str, Any]],
    effect_docs: list[Mapping[str, Any]],
    folder_name: str,
    slot_table: Callable[[str, int], list[int] | None] | None,
    weapon_name: Callable[[str], str | None] | None,
    avatar_data_uri: Callable[[str], str | None] | None,
    kiwee_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    warn = _Warn()
    system = _mapping(actor.get("system"))
    attributes = _mapping(system.get("attributes"))
    details = _mapping(system.get("details"))
    abilities = _abilities(system)
    con_mod = ability_modifier(abilities["con"])
    classes = _class_rows(item_docs)
    # 物品级效果（``!actors.items.effects!``）：装备类的挂到 equipment 行，
    # 其余（专长/种族特性/法术）挂到角色级 effect_changes。
    effects_by_item, _actor_level_effects = _split_item_effects(item_docs, effect_docs)
    kiwee_stats: dict[str, int] = {"lookups": 0, "enriched": 0}

    level = sum(row["levels"] for row in classes)
    if not classes:
        warn.add("class.missing", "没有职业条目，按 1 级导入。")
    class_names = [_main_name(row["name"]) for row in classes]
    subclass = ""
    for item in item_docs:
        if item.get("type") == "subclass":
            subclass = subclass or _main_name(item.get("name"))

    original_class = str(details.get("originalClass") or "").strip()
    if classes and original_class:
        # originalClass 是内嵌 class 条目的 <itemId>；映射必须从职业行自身
        # 构建（item_docs 是全部条目、classes 只有职业行，zip 配对会错位）。
        by_id = {row["source_id"]: row for row in classes}
        primary = by_id.get(original_class)
        if primary is not None and classes[0] is not primary:
            classes.remove(primary)
            classes.insert(0, primary)

    race = ""
    race_ref = str(details.get("race") or "").strip()
    race_item = next((doc for doc in item_docs if doc.get("type") == "race"), None)
    if race_item is None and race_ref:
        warn.add("race.missing", f"种族引用「{race_ref}」在条目集合中找不到，种族字段留空。")
    if race_item is not None:
        race = _main_name(race_item.get("name"))
    background = ""
    background_item = next((doc for doc in item_docs if doc.get("type") == "background"), None)
    if background_item is not None:
        background = _main_name(background_item.get("name"))

    hit_dice = _hit_dice(classes)
    hd_spent = sum(row["hd_spent"] for row in classes)
    hp = _mapping(attributes.get("hp"))
    hp_max, hp_note = _derive_hp_max(hp, classes, con_mod, warn)

    ac_value, ac_note, ac_warnings = armor_class(actor, item_docs)
    warn.items.extend(ac_warnings)

    speed, speed_note = _pc_speed(system, item_docs, warn)
    speeds = _speeds(_mapping(attributes.get("movement")))

    traits = _mapping(system.get("traits"))
    armor_prof = _mapping(traits.get("armorProf"))
    weapon_prof = _mapping(traits.get("weaponProf"))
    weapon_list: list[str] = []
    for code in weapon_prof.get("value") or []:
        text = str(code or "").strip()
        if not text:
            continue
        resolved = (weapon_name(text) if weapon_name else None) or WEAPON_PROF_ZH.get(text)
        if resolved:
            weapon_list.append(resolved)
        else:
            weapon_list.append(text)
            warn.add("weapon_prof.unknown_code", f"武器熟练代码「{text}」不在对照表，按原文导入。")
    custom_weapons = str(weapon_prof.get("custom") or "").strip()
    if custom_weapons:
        weapon_list.append(custom_weapons)
    armor_list = [ARMOR_PROF_ZH.get(code, str(code)) for code in armor_prof.get("value") or [] if str(code or "").strip()]
    custom_armor = str(armor_prof.get("custom") or "").strip()
    if custom_armor:
        armor_list.append(custom_armor)

    tools_list: list[str] = []
    tools = _mapping(system.get("tools"))
    for tool_id, row in sorted(tools.items()):
        value = _as_int(_mapping(row).get("value"), 0)
        if value <= 0:
            continue
        name = TOOL_ZH.get(tool_id, tool_id)
        tools_list.append(f"{name}（expertise）" if value >= 2 else name)

    skills = _mapping(system.get("skills"))
    skill_ids: list[str] = []
    expertise_ids: list[str] = []
    for abbr, row in skills.items():
        value = _as_int(_mapping(row).get("value"), 0)
        skill_id = SKILL_IDS.get(str(abbr))
        if value <= 0 or not skill_id:
            continue
        # 映射表 §2.4：expertise 技能同时写入 skills 与 expertise 两个列表。
        if skill_id not in skill_ids:
            skill_ids.append(skill_id)
        if value >= 2 and skill_id not in expertise_ids:
            expertise_ids.append(skill_id)

    spellcasting, pact_notes = _spellcasting(system, classes, item_docs, slot_table, warn)

    notes_extra: list[str] = [f"AC {ac_value}（{ac_note}）。", speed_note]
    if hp_note:
        notes_extra.append(hp_note)
    if speeds:
        extra_speeds = {k: v for k, v in speeds.items() if k != "walk"}
        if extra_speeds:
            notes_extra.append(f"特殊速度：{_speed_label(extra_speeds)}。")
    if hd_spent:
        notes_extra.append(f"已消耗生命骰 {hd_spent} 个。")
    notes_extra.extend(pact_notes)

    _formula_scans(system, warn)

    avatar_uri = ""
    img = str(actor.get("img") or "").strip()
    if img:
        if avatar_data_uri is not None:
            avatar_uri = avatar_data_uri(img) or ""
        if not avatar_uri:
            if img.startswith("systems/"):
                warn.add("img.system_asset", "头像引用系统内置资源（不在世界包内），已留空。")
            else:
                warn.add("img.missing", "头像资源在包内找不到，已留空。")

    equipment_rows = _equipment_rows(
        system,
        item_docs,
        warn,
        effects_by_item=effects_by_item,
        kiwee_lookup=kiwee_lookup,
        kiwee_stats=kiwee_stats,
    )
    # 非装备类物品（专长/种族特性/法术）的效果挂角色级；导入时不做激活判定，
    # 交给 dnd5e_plugin.effect_changes 按 transfer/disabled/applied 自己判。
    character_effects = [
        entry
        for entries in effects_by_item.values()
        for entry in entries
        if str(_mapping(entry.get("origin")).get("source") or "") not in EQUIPMENT_TYPES
    ]
    effects_summary = _effects_summary([*character_effects, *_flatten_effects(equipment_rows)])
    effects_summary["kiwee"] = dict(kiwee_stats)
    if effects_summary["documents"]:
        _warn_effects(warn, effects_summary, "物品效果")

    stats = _stats(actor)
    sheet = {
        "level": max(1, min(20, level or 1)),
        "class": "／".join(name for name in class_names if name),
        "subclass": subclass,
        "race": race,
        "background": background,
        "alignment": str(details.get("alignment") or "").strip(),
        "abilities": abilities,
        "proficiencies": {
            "saves": _save_proficiencies(system),
            "skills": skill_ids,
            "expertise": expertise_ids,
            "armor": armor_list,
            "weapons": weapon_list,
            "tools": tools_list,
            "languages": _languages(system, warn),
        },
        "hp": {
            "current": max(0, min(hp_max, _as_int(hp.get("value"), hp_max))),
            "max": hp_max,
            "temp": max(0, _as_int(hp.get("temp"), 0)),
        },
        # 必须写算好的数值：pack() 对缺省 armorClass 一律按 10+敏捷派生。
        "armorClass": ac_value,
        "speed": speed,
        "hitDice": hit_dice,
        "equipment": equipment_rows,
        "features": [
            str(item.get("name") or "").strip()
            for item in item_docs
            if item.get("type") == "feat" and str(item.get("name") or "").strip()
        ],
        "spellcasting": spellcasting,
        "notes": _notes_text(actor, system, item_docs, effect_docs, notes_extra, warn),
        "xp": max(0, _as_int(_mapping(details.get("xp")).get("value"), 0)),
    }
    if character_effects:
        sheet["effect_changes"] = character_effects
    name = str(actor.get("name") or "").strip() or "未命名角色"
    card: dict[str, Any] = {
        "id": "",
        "name": name[:80],
        "rulesetId": "dnd5e",
        "sheet": sheet,
    }
    if avatar_uri:
        card["avatar"] = avatar_uri
    if stats.get("created"):
        card["createdAt"] = stats["created"]
    return {
        "kind": "character",
        "source_id": str(actor.get("_id") or ""),
        "name": name,
        "folder": folder_name,
        "preset": name.startswith(PRESET_PREFIX),
        "class": sheet["class"],
        "level": sheet["level"],
        "race": race,
        "has_avatar": bool(avatar_uri),
        "card": card,
        "effects": effects_summary,
        "warnings": warn.texts(),
        "degradations": warn.counts(),
    }


def _stats(actor: Mapping[str, Any]) -> dict[str, str]:
    stats = _mapping(actor.get("_stats"))
    created = ""
    modified = ""
    for key, target in (("createdTime", "created"), ("modifiedTime", "modified")):
        value = _as_int_or_none(stats.get(key))
        if value is not None:
            import datetime

            try:
                stamp = datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc)
                text = stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            except (OverflowError, OSError, ValueError):
                text = ""
            if target == "created":
                created = text
            else:
                modified = text
    return {"created": created, "modified": modified}


def _notes_text(
    actor: Mapping[str, Any],
    system: Mapping[str, Any],
    item_docs: list[Mapping[str, Any]],
    effect_docs: list[Mapping[str, Any]],
    extra: list[str],
    warn: _Warn,
) -> str:
    """PC notes：传记/人格/外观/资源池/抗性/运行态/降级说明。"""
    lines: list[str] = []
    details = _mapping(system.get("details"))
    biography = _mapping(details.get("biography"))
    for value in (biography.get("value"), biography.get("public")):
        cleaned = _clean_text(value)
        if cleaned:
            lines.append(cleaned)
    for key, zh in (
        ("trait", "特质"),
        ("ideal", "理想"),
        ("bond", "羁绊"),
        ("flaw", "缺点"),
        ("appearance", "外貌"),
        ("age", "年龄"),
        ("height", "身高"),
        ("weight", "体重"),
        ("eyes", "眼睛"),
        ("hair", "毛发"),
        ("skin", "肤色"),
        ("gender", "性别"),
        ("faith", "信仰"),
    ):
        value = str(details.get(key) or "").strip()
        if value:
            lines.append(f"{zh}：{_clean_text(value)}")
    attributes = _mapping(system.get("attributes"))
    if attributes.get("inspiration"):
        lines.append("激励：当前可用。")
    exhaustion = _as_int(attributes.get("exhaustion"), 0)
    if exhaustion > 0:
        lines.append(f"力竭 {exhaustion} 级。")
    tempmax = _as_int(_mapping(attributes.get("hp")).get("tempmax"), 0)
    if tempmax:
        lines.append(f"临时 HP 上限 +{tempmax}（源包运行态）。")
    resources = _mapping(system.get("resources"))
    for slot in ("primary", "secondary", "tertiary"):
        pool = _mapping(resources.get(slot))
        label = str(pool.get("label") or "").strip()
        if label and _as_int(pool.get("max"), 0) > 0:
            recovery = "，".join(
                text for text, flag in (("短休恢复", pool.get("sr")), ("长休恢复", pool.get("lr"))) if flag
            )
            lines.append(f"资源池「{label}」：{pool.get('value')}/{pool.get('max')}{('，' + recovery) if recovery else ''}")
    traits = _mapping(system.get("traits"))
    for key, zh in (("di", "伤害免疫"), ("dr", "伤害抗性"), ("dv", "伤害易伤"), ("ci", "状态免疫")):
        bucket = _mapping(traits.get(key))
        values = [str(item or "").strip() for item in bucket.get("value") or [] if str(item or "").strip()]
        custom = str(bucket.get("custom") or "").strip()
        if values or custom:
            lines.append(f"{zh}：{'、'.join(values + ([custom] if custom else []))}")
    statuses = _statuses_note(effect_docs)
    if statuses:
        lines.append(f"当前状态：{statuses}（导入自源包运行态）。")
    lines.extend(line for line in extra if line)
    for code, text in warn.items:
        lines.append(f"[导入降级 {code}] {text}")
    return "\n".join(lines)[:MAX_NOTES_CHARS]


# -- npc -----------------------------------------------------------------


def _damage_lists(traits: Mapping[str, Any]) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """(抗性, 易伤, 免疫, 状态免疫, 旁注) —— FVTT di/dr/dv/ci + bypasses 旁注。"""
    def bucket(key: str) -> tuple[list[str], list[str]]:
        source = _mapping(traits.get(key))
        values = [str(item or "").strip() for item in source.get("value") or [] if str(item or "").strip()]
        bypasses = [str(item or "").strip() for item in source.get("bypasses") or [] if str(item or "").strip()]
        custom = str(source.get("custom") or "").strip()
        if custom:
            values.append(custom)
        return values, bypasses

    def with_bypass(values: list[str], bypasses: list[str]) -> list[str]:
        if not bypasses:
            return values
        note = f"（非{'、'.join(bypasses)}攻击）"
        return [f"{value}{note}" for value in values]

    dr, dr_bypass = bucket("dr")
    dv, _dv_bypass = bucket("dv")
    di, di_bypass = bucket("di")
    ci, _ci_bypass = bucket("ci")
    return with_bypass(dr, []), with_bypass(dv, []), with_bypass(di, di_bypass), ci, []


def _npc_attacks(
    item_docs: list[Mapping[str, Any]],
    prof: int,
    abilities: Mapping[str, int],
    warn: _Warn,
) -> tuple[list[dict[str, Any]], list[str]]:
    """武器条目 → statblock 攻击行（伤害双源：activity.parts 优先，其次 damage.base）。"""
    rows: list[dict[str, Any]] = []
    degraded: list[str] = []
    for item in item_docs:
        if item.get("type") != "weapon":
            continue
        system = _mapping(item.get("system"))
        name = _main_name(item.get("name")) or str(item.get("name") or "").strip()
        if not name:
            continue
        attack_activity = None
        for activity in (system.get("activities") or {}).values():
            activity = _mapping(activity)
            if str(activity.get("type") or "").strip() == "attack":
                attack_activity = activity
                break
        attack = _mapping(attack_activity.get("attack")) if attack_activity else {}
        attack_type = _mapping(attack.get("type"))
        kind = "ranged" if str(attack_type.get("value") or "").strip() == "ranged" else "melee"
        properties = [str(prop or "").strip().lower() for prop in system.get("properties") or [] if str(prop or "").strip()]
        ability = str(attack.get("ability") or "").strip().lower()
        if not ability:
            # 无施法属性名的武器按默认：远程/灵巧敏捷，否则力量。
            if kind == "ranged" or "fin" in properties:
                ability = "dex"
            else:
                ability = "str"
        flat = bool(attack.get("flat"))
        if flat:
            attack_bonus = _as_int(attack.get("bonus"), 0)
            warn.add(
                "attack.flat_bonus",
                f"「{name}」的攻击加值为显式给定（+{attack_bonus}），未再推导。",
            )
        else:
            attack_bonus = ability_modifier(abilities.get(ability, 10)) + prof
            extra = _as_int_or_none(attack.get("bonus"))
            if extra is not None:
                attack_bonus += extra
            elif str(attack.get("bonus") or "").strip():
                warn.add(
                    "attack.bonus_formula",
                    f"「{name}」的攻击加值公式串「{attack.get('bonus')}」无法求值，按属性+熟练推导。",
                )

        parts = _mapping(attack_activity.get("damage")).get("parts") if attack_activity else None
        parts = [part for part in (parts or []) if isinstance(part, Mapping)]
        base = _mapping(_mapping(system.get("damage")).get("base"))
        dice = ""
        damage_type = ""
        damage_bonus = 0
        source = None
        if parts:
            first = _mapping(parts[0])
            number = _as_int(first.get("number"), 0)
            denomination = _as_int(first.get("denomination"), 0)
            if number > 0 and denomination > 0:
                dice = f"{number}d{denomination}"
                types = [str(t or "").strip() for t in first.get("types") or [] if str(t or "").strip()]
                damage_type = types[0] if types else ""
                source = "activity"
                bonus_text = str(first.get("bonus") or "").strip()
                bonus = _as_int_or_none(bonus_text)
                if bonus is not None:
                    damage_bonus = bonus
                elif bonus_text:
                    warn.add(
                        "damage.bonus_formula",
                        f"「{name}」的伤害加值公式串「{bonus_text}」无法求值，按 0 处理。",
                    )
                for extra_part in parts[1:]:
                    extra = _mapping(extra_part)
                    extra_number = _as_int(extra.get("number"), 0)
                    extra_denom = _as_int(extra.get("denomination"), 0)
                    extra_types = [str(t or "").strip() for t in extra.get("types") or [] if str(t or "").strip()]
                    if extra_number <= 0 or extra_denom <= 0:
                        continue
                    # 多段伤害只映首段，其余降级 on-hit 标签（降级清单 #9）。
                    rows_on_hit = f"on-hit:{extra_number}d{extra_denom}[{extra_types[0] if extra_types else ''}]"
                    properties.append(rows_on_hit)
                    warn.add(
                        "damage.extra_part",
                        f"「{name}」的多段伤害只映射首段，其余段（{rows_on_hit}）降级为命中附加标签。",
                    )
        if not dice:
            number = _as_int(base.get("number"), 0)
            denomination = _as_int(base.get("denomination"), 0)
            if number > 0 and denomination > 0:
                dice = f"{number}d{denomination}"
                types = [str(t or "").strip() for t in base.get("types") or [] if str(t or "").strip()]
                damage_type = types[0] if types else ""
                source = "base"
                bonus = _as_int_or_none(base.get("bonus"))
                if bonus is not None:
                    damage_bonus = bonus
                elif str(base.get("bonus") or "").strip():
                    warn.add(
                        "damage.bonus_formula",
                        f"「{name}」的伤害加值公式串「{base.get('bonus')}」无法求值，按 0 处理。",
                    )
        if not dice:
            # 组装不出伤害 → 降级 trait 文本，不丢名字（映射表 §3.2 规则 7）。
            degraded.append(name)
            warn.add(
                "attack.no_damage",
                f"「{name}」没有可解析的伤害骰，未生成攻击行，名字保留在特性文本里。",
            )
            continue
        versatile = _mapping(_mapping(system.get("damage")).get("versatile"))
        versatile_dice = ""
        versatile_number = _as_int(versatile.get("number"), 0)
        versatile_denom = _as_int(versatile.get("denomination"), 0)
        if versatile_number > 0 and versatile_denom > 0:
            versatile_dice = f"{versatile_number}d{versatile_denom}"
        row: dict[str, Any] = {
            "id": str(item.get("_id") or "").split(".")[-1] or name,
            "name": name,
            "kind": kind,
            "attack_bonus": attack_bonus,
            "damage_bonus": damage_bonus,
            "damage_dice": dice,
            "damage_type": damage_type,
            "versatile_dice": versatile_dice,
            "properties": properties,
        }
        if attack_activity:
            activity_range = _mapping(attack_activity.get("range"))
            reach = _as_int_or_none(activity_range.get("value"))
            if kind == "melee" and reach:
                row["reach_ft"] = reach
            if kind == "ranged":
                item_range = _mapping(system.get("range"))
                row["range"] = {
                    "normal_ft": _as_int(item_range.get("value"), reach or 0),
                    "long_ft": _as_int(item_range.get("long"), 0),
                }
        rows.append(row)
    return rows, degraded


def _npc_skills(system: Mapping[str, Any], abilities: Mapping[str, int], prof: int) -> dict[str, int]:
    skills = _mapping(system.get("skills"))
    result: dict[str, int] = {}
    for abbr, row in skills.items():
        value = _as_int(_mapping(row).get("value"), 0)
        skill_id = SKILL_IDS.get(str(abbr))
        if value <= 0 or not skill_id:
            continue
        declared = str(_mapping(row).get("ability") or "").strip().lower()
        ability = declared if declared in ABILITY_KEYS else "wis"
        result[skill_id] = ability_modifier(abilities[ability]) + prof * value
    return result


def _npc_senses(system: Mapping[str, Any], abilities: Mapping[str, int], prof: int) -> dict[str, Any]:
    attributes = _mapping(system.get("attributes"))
    senses = _mapping(attributes.get("senses"))
    ranges = _mapping(senses.get("ranges"))
    result: dict[str, Any] = {}
    for key in ("darkvision", "blindsight", "tremorsense", "truesight"):
        value = _as_int(ranges.get(key), 0)
        if value > 0:
            result[f"{key}_ft"] = value
    skills = _mapping(system.get("skills"))
    perception_proficiency = _as_int(_mapping(skills.get("prc")).get("value"), 0)
    result["passive_perception"] = 10 + ability_modifier(abilities["wis"]) + prof * perception_proficiency
    special = str(senses.get("special") or "").strip()
    if special:
        result["special"] = special
    return result


def _npc_slots(system: Mapping[str, Any], warn: _Warn) -> dict[str, dict[str, int]]:
    """NPC 没有 class item → ``max=value``（导入即满槽）+ 警告（映射表 §3.4）。"""
    spells = _mapping(system.get("spells"))
    slots: dict[str, dict[str, int]] = {}
    for level in range(1, 10):
        row = _mapping(spells.get(f"spell{level}"))
        value = _as_int(row.get("value"), 0)
        if value <= 0:
            continue
        slots[str(level)] = {"max": value, "used": 0}
    if slots:
        warn.add(
            "slots.npc_value_as_max",
            "NPC 法术位按源包剩余值导入为上限（导入即满槽）。",
        )
    return slots


def _npc(
    actor: Mapping[str, Any],
    item_docs: list[Mapping[str, Any]],
    effect_docs: list[Mapping[str, Any]],
    folder_name: str,
) -> dict[str, Any]:
    warn = _Warn()
    system = _mapping(actor.get("system"))
    attributes = _mapping(system.get("attributes"))
    details = _mapping(system.get("details"))
    traits = _mapping(system.get("traits"))
    abilities = _abilities(system)
    cr_number = _cr_number(details.get("cr"))
    cr = _fraction_cr(details.get("cr"))
    prof = proficiency_for_cr(cr_number)

    ac_value, ac_note, ac_warnings = armor_class(actor, item_docs)
    warn.items.extend(ac_warnings)
    hp = _mapping(attributes.get("hp"))
    max_hp = _as_int(hp.get("max"), _as_int(hp.get("value"), 1))
    speeds = _speeds(_mapping(attributes.get("movement")))
    walk = speeds.get("walk", 30)
    if walk == 30 and not speeds:
        warn.add("speed.default", "NPC 速度缺失，按默认 30 尺导入。")

    attacks, degraded_weapons = _npc_attacks(item_docs, prof, abilities, warn)
    traits_list, actions_list, reactions_list, spellcasting_notes = _feat_sections(item_docs)

    creature = _mapping(details.get("type"))
    creature_type = str(creature.get("value") or "").strip() or "humanoid"
    subtype = str(creature.get("subtype") or "").strip()
    swarm = str(creature.get("swarm") or "").strip()
    if swarm:
        warn.add(
            "type.swarm",
            f"该单位是虫群（swarm={swarm}），战斗语义按群聚处理。",
        )
    size_code = str(traits.get("size") or "med").strip().lower()
    size = SIZE_MAP.get(size_code, "medium")

    saves = _save_proficiencies(system)
    saving_throws = {key: ability_modifier(abilities[key]) + prof for key in saves}
    skills = _npc_skills(system, abilities, prof)
    senses = _npc_senses(system, abilities, prof)
    languages = _languages(system, warn)
    damage_resistances, damage_vulnerabilities, damage_immunities, condition_immunities, _bypasses = _damage_lists(traits)

    # NPC 法术条目按 prepared 分桶（两包实测 NPC 全部 prepared=1）。
    spells_known: list[str] = []
    spells_prepared: list[str] = []
    for item in item_docs:
        if item.get("type") != "spell":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if _as_int(_mapping(item.get("system")).get("prepared")) > 0:
            spells_prepared.append(name)
        else:
            spells_known.append(name)
    spellcasting: dict[str, Any] | None = None
    spell_slots: dict[str, dict[str, int]] = {}
    casting_ability = str(attributes.get("spellcasting") or "").strip().lower()
    if casting_ability or spells_known or spells_prepared or spellcasting_notes:
        notes = "\n".join(f"{key}：{value}" for key, value in spellcasting_notes.items())
        if not casting_ability:
            warn.add(
                "spellcasting.no_ability",
                "施法条目存在但 actor 未声明施法属性，施法 DC/攻击加值无法重算，仅导入法术清单。",
            )
            spellcasting = {"ability": "", "notes": notes}
        else:
            # DC/攻击加值必须重算（NEC DC14/+6、孔中尉 DC12/+4 两样本对账）。
            mod = ability_modifier(abilities.get(casting_ability, 10))
            spellcasting = {
                "ability": casting_ability,
                "spellSaveDC": 8 + prof + mod,
                "spellAttackBonus": prof + mod,
                "notes": notes,
            }
            # NPC 没有 class item → max=value（导入即满槽）+ 警告。
            spell_slots = _npc_slots(system, warn)

    resources = _mapping(system.get("resources"))
    legact = _as_int(_mapping(resources.get("legact")).get("max"), 0)
    legres = _as_int(_mapping(resources.get("legres")).get("max"), 0)
    lair = bool(_mapping(resources.get("lair")).get("value"))
    legendary_actions: list[dict[str, Any]] = []
    if legact or legres or lair:
        # 两包零实例：结构在，转换分支写好，条目识别约定待有样本后补。
        warn.add(
            "legendary.unsupported",
            f"传奇动作/传奇抗性/巢穴动作（legact={legact}, legres={legres}, lair={lair}）本期只记数值，不生成条目。",
        )

    statuses = _statuses_note(effect_docs)
    # 物品效果：NPC 的战斗数值全部走 statblock，所以效果也挂 statblock.
    # effect_changes（引擎在 combatant_from_module 里读同一份视图）。
    npc_effects_by_item, _npc_actor_level = _split_item_effects(item_docs, effect_docs)
    npc_effects = [
        entry
        for entries in npc_effects_by_item.values()
        for entry in entries
        if _mapping(entry.get("origin")).get("equipped")
    ]
    npc_effects_summary = _effects_summary(
        [entry for entries in npc_effects_by_item.values() for entry in entries]
    )
    if npc_effects_summary["documents"]:
        _warn_effects(warn, npc_effects_summary, "物品效果")
    name = str(actor.get("name") or "").strip() or "未命名怪物"
    title = name if not folder_name else f"[{folder_name}] {name}"

    statblock: dict[str, Any] = {
        "name": name,
        "abilities": abilities,
        "armorClass": ac_value,
        "ac_note": ac_note,
        "maxHp": max(1, max_hp),
        "hp": {"current": max(0, min(max_hp, _as_int(hp.get("value"), max_hp))), "max": max(1, max_hp)},
        "hp_formula": str(hp.get("formula") or "").strip(),
        "speed": {"walk": walk, **{k: v for k, v in speeds.items() if k != "walk"}},
        "speed_ft": walk,
        "size": size,
        "creature_type": creature_type,
        "subtype": subtype,
        "alignment": str(details.get("alignment") or "").strip(),
        "cr": cr,
        "xp": xp_for_cr(cr_number),
        "side": {-1: "enemy", 0: "neutral", 1: "ally"}.get(
            _as_int(_mapping(actor.get("prototypeToken")).get("disposition"), 0), "enemy"
        ),
        "saving_throws": saving_throws,
        "skills": skills,
        "senses": senses,
        "languages": languages,
        "damage_resistances": damage_resistances,
        "damage_vulnerabilities": damage_vulnerabilities,
        "damage_immunities": damage_immunities,
        "condition_immunities": condition_immunities,
    }
    if swarm:
        statblock["swarm"] = swarm
    if attacks:
        statblock["attacks"] = attacks
    if spellcasting is not None:
        # 模块契约键（statblock.combatant_from_module 消费）：
        # spellcasting{ability,spellSaveDC,spellAttackBonus} + spell_slots +
        # spells_known/prepared——核心战斗管线已按这些键消费。
        statblock["spellcasting"] = spellcasting
        if spells_known:
            statblock["spells_known"] = spells_known
        if spells_prepared:
            statblock["spells_prepared"] = spells_prepared
    if spell_slots:
        statblock["spell_slots"] = spell_slots
    if legact:
        statblock["legendary_actions_max"] = legact
    if legres:
        statblock["legendary_resistances_max"] = legres
    if npc_effects:
        statblock["effect_changes"] = npc_effects

    content = _render_statblock_text(
        name=name,
        size=size,
        creature_type=creature_type,
        subtype=subtype,
        swarm=swarm,
        alignment=statblock["alignment"],
        statblock=statblock,
        abilities=abilities,
        saving_throws=saving_throws,
        skills=skills,
        senses=senses,
        languages=languages,
        cr=cr,
        xp=statblock["xp"],
        traits=traits_list,
        actions=actions_list,
        reactions=reactions_list,
        degraded_weapons=degraded_weapons,
        spellcasting=spellcasting,
        spell_slots=spell_slots,
        spells_known=spells_known,
        spells_prepared=spells_prepared,
        spellcasting_notes=spellcasting_notes,
        legendary=(legact, legres, lair),
        statuses=statuses,
        warn=warn,
    )

    module: dict[str, Any] = {
        "id": f"fvtt-npc-{re.sub(r'[^A-Za-z0-9_-]', '', str(actor.get('_id') or ''))[:56]}",
        "module_type": "npc",
        "card_type": "npc",
        "title": title[:120],
        "summary": _npc_summary(statblock, creature_type, cr),
        "content": content,
        "visibility": "kp",
        "send_to_ai": True,
        "statblock": statblock,
        "fvtt_actor_id": str(actor.get("_id") or ""),
    }
    return {
        "kind": "npc",
        "source_id": str(actor.get("_id") or ""),
        "name": name,
        "folder": folder_name,
        "title": module["title"],
        "module": module,
        "cr": cr,
        "armor_class": ac_value,
        "max_hp": max(1, max_hp),
        "effects": npc_effects_summary,
        "warnings": warn.texts(),
        "degradations": warn.counts(),
    }


def _npc_summary(statblock: Mapping[str, Any], creature_type: str, cr: str) -> str:
    type_zh = CREATURE_TYPE_ZH.get(creature_type, creature_type)
    return (
        f"{type_zh} CR {cr or '0'} · AC {statblock['armorClass']} · "
        f"HP {statblock['maxHp']} · 速度 {statblock['speed_ft']} 尺"
    )[:200]


def _render_statblock_text(
    *,
    name: str,
    size: str,
    creature_type: str,
    subtype: str,
    swarm: str,
    alignment: str,
    statblock: Mapping[str, Any],
    abilities: Mapping[str, int],
    saving_throws: Mapping[str, int],
    skills: Mapping[str, int],
    senses: Mapping[str, Any],
    languages: list[str],
    cr: str,
    xp: int,
    traits: list[dict[str, str]],
    actions: list[dict[str, str]],
    reactions: list[dict[str, str]],
    degraded_weapons: list[str],
    spellcasting: Mapping[str, Any] | None,
    spell_slots: Mapping[str, Mapping[str, int]],
    spells_known: list[str],
    spells_prepared: list[str],
    spellcasting_notes: Mapping[str, str],
    legendary: tuple[int, int, bool],
    statuses: str,
    warn: _Warn,
) -> str:
    lines: list[str] = [f"## {name}"]
    type_parts = [SIZE_ZH.get(size, size), CREATURE_TYPE_ZH.get(creature_type, creature_type)]
    if subtype and subtype != "Any Race":
        type_parts.append(f"（{subtype}）")
    if swarm:
        type_parts.append(f"（{SIZE_ZH.get(swarm, swarm)}体虫群）")
    if alignment:
        type_parts.append(f"，{alignment}")
    lines.append("".join(type_parts))

    ac_note = str(statblock.get("ac_note") or "")
    lines.append(f"**AC {statblock['armorClass']}**" + (f"（{ac_note}）" if ac_note else ""))
    hp_line = f"**HP {statblock['maxHp']}"
    formula = str(statblock.get("hp_formula") or "").strip()
    if formula:
        hp_line += f"（{formula}）"
    hp_line += "**"
    lines.append(hp_line)
    speeds = _mapping(statblock.get("speed"))
    speed_label = _speed_label({k: int(v) for k, v in speeds.items()})
    lines.append(f"**速度** {speed_label or '步行 30 尺'}")

    lines.append(_ability_score_note(abilities))
    if saving_throws:
        lines.append(
            "**豁免** " + "、".join(f"{key.upper()} {value:+d}" for key, value in saving_throws.items())
        )
    else:
        lines.append("**豁免** 无熟练项")
    if skills:
        lines.append("**技能** " + "、".join(f"{key} {value:+d}" for key, value in skills.items()))
    sense_parts = [f"被动察觉 {senses.get('passive_perception', 10)}"]
    for key in ("darkvision", "blindsight", "tremorsense", "truesight"):
        value = _as_int(senses.get(f"{key}_ft"), 0)
        if value > 0:
            sense_parts.append(f"{ {'darkvision': '黑暗视觉', 'blindsight': '盲视', 'tremorsense': '震颤感知', 'truesight': '真视'}.get(key, key)} {value} 尺")
    special = str(senses.get("special") or "").strip()
    if special:
        sense_parts.append(special)
    lines.append("**感官** " + "，".join(sense_parts))
    lines.append(f"**语言** {'、'.join(languages) if languages else '—'}")
    lines.append(f"**挑战等级** {cr or '0'}（{xp} XP）")
    if statblock.get("damage_resistances"):
        lines.append("**伤害抗性** " + "、".join(DAMAGE_ZH.get(t, t) for t in statblock["damage_resistances"]))
    if statblock.get("damage_vulnerabilities"):
        lines.append("**伤害易伤** " + "、".join(DAMAGE_ZH.get(t, t) for t in statblock["damage_vulnerabilities"]))
    if statblock.get("damage_immunities"):
        lines.append("**伤害免疫** " + "、".join(DAMAGE_ZH.get(t, t) for t in statblock["damage_immunities"]))
    if statblock.get("condition_immunities"):
        lines.append("**状态免疫** " + "、".join(CONDITION_ZH.get(t, t) for t in statblock["condition_immunities"]))
    if statuses:
        lines.append(f"**当前状态** {statuses}（导入自源包运行态）")

    legact, legres, lair = legendary
    if legact or legres or lair:
        extras = []
        if legact:
            extras.append(f"传奇动作 {legact} 次/轮")
        if legres:
            extras.append(f"传奇抗性 {legres} 次/日")
        if lair:
            extras.append("巢穴动作")
        lines.append("**传奇** " + "，".join(extras))

    if spellcasting is not None:
        ability = str(spellcasting.get("ability") or "")
        lines.append("")
        lines.append("### 施法")
        if ability:
            ability_zh = {"str": "力量", "dex": "敏捷", "con": "体质", "int": "智力", "wis": "感知", "cha": "魅力"}.get(ability, ability)
            lines.append(
                f"施法属性 {ability_zh}（法术豁免 DC {spellcasting.get('spellSaveDC')}，"
                f"法术攻击 +{spellcasting.get('spellAttackBonus')}）"
            )
        slots = _mapping(spell_slots)
        if slots:
            lines.append("法术位：" + "，".join(f"{level} 环 {row['max']} 个" for level, row in sorted(slots.items())))
        if spells_prepared:
            lines.append("已准备法术：" + "、".join(spells_prepared))
        if spells_known:
            lines.append("已知法术：" + "、".join(spells_known))
        for key, value in spellcasting_notes.items():
            if value:
                lines.append(_clean_text(value))

    for title, entries in (("### 特性", traits), ("### 动作", actions), ("### 反应", reactions)):
        if not entries:
            continue
        lines.append("")
        lines.append(title)
        for entry in entries:
            text = entry.get("text") or ""
            lines.append(f"**{entry.get('name')}** {text}".strip())

    attack_rows = list(statblock.get("attacks") or [])
    if attack_rows:
        lines.append("")
        lines.append("### 攻击")
        for row in attack_rows:
            kind_zh = "远程武器攻击" if row.get("kind") == "ranged" else "近战武器攻击"
            reach = ""
            if row.get("kind") == "melee" and row.get("reach_ft"):
                reach = f"，触及 {row['reach_ft']} 尺"
            if row.get("kind") == "ranged":
                attack_range = _mapping(row.get("range"))
                if attack_range.get("normal_ft"):
                    reach = f"，射程 {attack_range.get('normal_ft')}/{attack_range.get('long_ft', 0)} 尺"
            damage = f"{row['damage_dice']}"
            if row.get("damage_bonus"):
                damage += f"{row['damage_bonus']:+d}"
            damage_zh = DAMAGE_ZH.get(str(row.get("damage_type") or ""), str(row.get("damage_type") or ""))
            lines.append(
                f"**{row['name']}** {kind_zh} +{row['attack_bonus']} 命中{reach}，命中 {damage} {damage_zh}伤害。"
                + (f"（双手 {row['versatile_dice']}）" if row.get("versatile_dice") else "")
            )
    if degraded_weapons:
        lines.append("")
        lines.append("（以下武器没有可解析的伤害骰，仅保留名称：" + "、".join(degraded_weapons) + "）")

    if warn.items:
        lines.append("")
        lines.append("导入降级：")
        lines.extend(f"- [导入降级 {code}] {text}" for code, text in warn.items)
    return "\n".join(lines)[:MAX_NOTES_CHARS * 2]


# -- vehicle --------------------------------------------------------------


def _vehicle(actor: Mapping[str, Any], folder_name: str) -> dict[str, Any]:
    name = str(actor.get("name") or "").strip() or "载具"
    return {
        "kind": "vehicle",
        "source_id": str(actor.get("_id") or ""),
        "name": name,
        "folder": folder_name,
        "note": "vehicle 类型没有战斗数值（HP/AC 均为空），未导入为角色卡或 statblock；如需请在剧本中手动录入为物件。",
    }


# -- 入口 -----------------------------------------------------------------


def convert_world_actors(
    index: Any,
    *,
    slot_table: Callable[[str, int], list[int] | None] | None = None,
    weapon_name: Callable[[str], str | None] | None = None,
    avatar_data_uri: Callable[[str], str | None] | None = None,
    kiwee_enrich: bool = True,
    item_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """整包 actors → {characters, npcs, vehicles, warnings, degradations, totals}。

    ``index`` 是 :class:`fvtt_world_import.world_package.ActorIndex`
    （actor 主文档 + 复合键聚合的 items/effects + folder 名）。

    ``item_lookup`` / ``kiwee_enrich`` 控制 kiwee 深库补强（默认开）：传入回调
    则按回调取 item，否则惰性打开 ``dnd5e_plugin.kiwee`` 的 sqlite；深库缺席、
    版本不符或任何异常都静默跳过。环境变量 ``TRPG_FVTT_KIWEE_ENRICH=0`` 也能
    整体关掉（站点/CI 用）。
    """
    documents: dict[str, dict[str, Any]] = dict(getattr(index, "documents") or {})
    items_by_actor: dict[str, list[dict[str, Any]]] = {
        str(key): list(value) for key, value in (getattr(index, "items") or {}).items()
    }
    effects_by_actor: dict[str, list[dict[str, Any]]] = {
        str(key): list(value) for key, value in (getattr(index, "effects") or {}).items()
    }
    # 条目级效果（``!actors.items.effects!``）与 actor 级效果走同一条通路：
    # 三层复合键 ``<actorId>.<itemId>.<effectId>``，由 ``_split_item_effects``
    # 按段数分流到物品行（两段的仍按 actor 级 / 条件语义处理）。
    for owner, rows in (getattr(index, "item_effects") or {}).items():
        effects_by_actor.setdefault(str(owner), []).extend(rows)
    folder_names: dict[str, str] = dict(getattr(index, "folders") or {})

    characters: list[dict[str, Any]] = []
    npcs: list[dict[str, Any]] = []
    vehicles: list[dict[str, Any]] = []
    degradations: dict[str, int] = {}
    warnings: list[str] = []

    # kiwee 补强开关：显式回调 > 环境变量 > 默认开。深库缺席时 lookup 为 None。
    enrich_enabled = kiwee_enrich and os.environ.get("TRPG_FVTT_KIWEE_ENRICH", "1") not in {
        "0",
        "false",
        "False",
    }
    lookup = item_lookup
    if lookup is None and enrich_enabled:
        lookup = default_kiwee_item_lookup()

    def count_degradations(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for code, value in (row.get("degradations") or {}).items():
                degradations[code] = degradations.get(code, 0) + value

    for actor_id, actor in sorted(documents.items(), key=lambda pair: str(pair[1].get("name") or pair[0])):
        kind = str(actor.get("type") or "").strip().lower()
        folder_name = folder_names.get(str(actor.get("folder") or ""), "")
        referenced = [str(ref) for ref in actor.get("items") or []]
        item_docs = items_by_actor.get(actor_id, [])
        by_item_id = {str(doc.get("_id") or "").split(".", 1)[-1]: doc for doc in item_docs}
        resolved: list[dict[str, Any]] = []
        for ref in referenced:
            doc = by_item_id.get(ref)
            if doc is None:
                warnings.append(f"条目「{ref}」引用了不存在的 actor 条目（悬空引用），已跳过。")
                degradations["items.dangling_ref"] = degradations.get("items.dangling_ref", 0) + 1
                continue
            resolved.append(doc)
        effect_docs = effects_by_actor.get(actor_id, [])
        if kind == "character":
            result = _character(
                actor,
                resolved,
                effect_docs,
                folder_name,
                slot_table,
                weapon_name,
                avatar_data_uri,
                lookup,
            )
            characters.append(result)
            warnings.extend(f"角色卡「{result['name']}」：{text}" for text in result["warnings"])
        elif kind == "npc":
            result = _npc(actor, resolved, effect_docs, folder_name)
            npcs.append(result)
            warnings.extend(f"怪物「{result['name']}」：{text}" for text in result["warnings"])
        else:
            result = _vehicle(actor, folder_name)
            vehicles.append(result)
            warnings.append(f"「{result['name']}」是 vehicle 类型，未导入（{result['note']}）")
            degradations["vehicle.skipped"] = degradations.get("vehicle.skipped", 0) + 1

    count_degradations(characters)
    count_degradations(npcs)
    totals = {
        "characters": len(characters),
        "npcs": len(npcs),
        "vehicles": len(vehicles),
        "warnings": len(warnings),
    }
    # 物品效果汇总：挂上了多少条 effect 文档、其中多少条能真生效、kiwee 补强命中。
    effect_totals = {"documents": 0, "changes": 0, "transfer": 0, "unsupported": 0}
    kiwee_total = {"lookups": 0, "enriched": 0}
    for row in [*characters, *npcs]:
        summary = row.get("effects") if isinstance(row.get("effects"), dict) else {}
        for key in effect_totals:
            effect_totals[key] += _as_int(summary.get(key), 0)
        if not lookup:
            # 深库未开/缺席时不要报「命中 0」误导调用方。
            continue
        kiwee = summary.get("kiwee") if isinstance(summary.get("kiwee"), dict) else {}
        for key in kiwee_total:
            kiwee_total[key] += _as_int(kiwee.get(key), 0)
    if lookup:
        effect_totals["kiwee_enriched"] = kiwee_total["enriched"]
        effect_totals["kiwee_lookups"] = kiwee_total["lookups"]
    totals["effects"] = effect_totals
    return {
        "characters": characters,
        "npcs": npcs,
        "vehicles": vehicles,
        "warnings": warnings,
        "degradations": degradations,
        "totals": totals,
    }


__all__ = [
    "ABILITY_KEYS",
    "EFFECT_LIST_KEYS",
    "EFFECT_MODE_NAMES",
    "EFFECT_NUMBER_KEYS",
    "EFFECT_NUMBER_PATTERNS",
    "EFFECT_TEXT_KEYS",
    "KIWEE_CONDITIONS_ZH",
    "KIWEE_DAMAGE_TYPES_ZH",
    "KIWEE_ITEM_CHANGE_FIELDS",
    "KIWEE_LIST_CHANGE_FIELDS",
    "SKILL_IDS",
    "convert_world_actors",
    "default_kiwee_item_lookup",
    "effect_key_supported",
    "proficiency_for_cr",
    "xp_for_cr",
]
