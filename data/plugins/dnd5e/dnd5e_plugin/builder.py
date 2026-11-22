"""D&D 5e（SRD 5.1）建卡计算：购点 / 掷骰 / 标准数组 / 种族加值 / 1 级 HP。

建卡向导（前端 ``rulesets/dnd5e/character-wizard.ts``）与后续的 AI 建卡工具
共用同一份规则口径，因此规则数字**只在这里定一次**：

- 购点：27 分，8–15，成本表见 :data:`POINT_BUY_COST`；
- 标准数组：15/14/13/12/10/8（不消耗购点，是另一条独立路径）；
- 掷骰：4d6 去最低，一次六组；
- 种族加值叠加在基础值之上（半精灵这类「自选 +1」由调用方给出具体加值）；
- 1 级 HP = 生命骰满值 + 体质调整值（至少 1）；
- 被动感知 = 10 + 察觉加值；熟练加值按等级查表。

``build_sheet`` 产出与卡面（``frontend/src/rulesets/dnd5e/character-sheet.ts``）
同形的 sheet；``preview`` 是**只读速览**，其口径必须与插件
:meth:`dnd5e_plugin.schema.Dnd5eCharacterSchema.derive` 完全一致——卡面保存后的
派生值就是这一份，两边不一致会让玩家看到「预览一套、存档另一套」。
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Mapping, Sequence

from dnd5e_plugin.rules import ABILITY_KEYS, SKILLS, ability_modifier, proficiency_bonus

# --- 购点 -------------------------------------------------------------------

# SRD 5.1 购点成本表：8 = 0 分，15 = 9 分；8–15 之外不可购买。
POINT_BUY_COST: dict[int, int] = {
    8: 0,
    9: 1,
    10: 2,
    11: 3,
    12: 4,
    13: 5,
    14: 7,
    15: 9,
}
POINT_BUY_BUDGET = 27
POINT_BUY_FLOOR = 8
POINT_BUY_CEIL = 15

# 标准数组：与购点互斥的另一条分配路径，六值互不相同、一次分完。
STANDARD_ARRAY: tuple[int, ...] = (15, 14, 13, 12, 10, 8)

# 掷骰模式：一次掷六组，每组 4d6 去最低。
ROLL_DICE_COUNT = 4
ROLL_DICE_FACES = 6
ROLL_POOL_SIZE = 6

# 属性值上限与下限（与插件 schema 的 _normalize_abilities 一致）。
ABILITY_FLOOR = 1
ABILITY_CEILING = 30


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def clamp_point_buy(value: Any) -> int:
    """把购点输入夹到 8–15：越界不报错，直接夹紧（UI 直接调这个）。"""
    return max(POINT_BUY_FLOOR, min(POINT_BUY_CEIL, _as_int(value, POINT_BUY_FLOOR)))


def point_buy_cost(score: Any) -> int:
    """单属性在购点模式下的花费；越界按 8 处理（成本 0）。"""
    return POINT_BUY_COST[clamp_point_buy(score)]


def point_buy_spent(scores: Mapping[str, Any]) -> int:
    """购点已花费点数；缺项按 8（0 分）计。"""
    source = scores if isinstance(scores, Mapping) else {}
    return sum(point_buy_cost(source.get(key, POINT_BUY_FLOOR)) for key in ABILITY_KEYS)


def point_buy_remaining(scores: Mapping[str, Any], budget: int = POINT_BUY_BUDGET) -> int:
    """购点余额；可以为负（UI 用它标红提示超额）。"""
    return _as_int(budget, POINT_BUY_BUDGET) - point_buy_spent(scores)


def point_buy_affordable(scores: Mapping[str, Any], key: str, budget: int = POINT_BUY_BUDGET) -> bool:
    """能否把 ``key`` 再加 1 点（不越界、不超预算）。"""
    current = clamp_point_buy((scores or {}).get(key, POINT_BUY_FLOOR))
    if current >= POINT_BUY_CEIL:
        return False
    bumped = dict(scores or {})
    bumped[key] = current + 1
    return point_buy_spent(bumped) <= _as_int(budget, POINT_BUY_BUDGET)


def standard_array(index: int) -> int:
    """标准数组按位取值；越界回落到最小值。"""
    position = max(0, min(len(STANDARD_ARRAY) - 1, _as_int(index, 0)))
    return STANDARD_ARRAY[position]


# --- 掷骰 -------------------------------------------------------------------


def roll_4d6_drop_lowest(dice: Sequence[int]) -> int:
    """4d6 去最低：入参必须是四颗骰子的点数（顺序无关）。"""
    values = sorted((_as_int(item, 1) for item in dice), reverse=True)
    if len(values) < ROLL_DICE_COUNT:
        return sum(values)
    return sum(values[:3])


def roll_pool(rng: random.Random | Any | None = None, size: int = ROLL_POOL_SIZE) -> list[int]:
    """掷出一池六组 4d6 去最低；``rng`` 只要求有 ``randint``（测试可注入）。"""
    dice_random = rng if rng is not None and hasattr(rng, "randint") else random.SystemRandom()
    pool: list[int] = []
    for _ in range(max(1, _as_int(size, ROLL_POOL_SIZE))):
        dice = [dice_random.randint(1, ROLL_DICE_FACES) for _ in range(ROLL_DICE_COUNT)]
        pool.append(roll_4d6_drop_lowest(dice))
    return pool


# --- 种族加值 ---------------------------------------------------------------


def normalize_bonuses(bonuses: Mapping[str, Any] | None) -> dict[str, int]:
    """把种族加值整理成 ``{ability: int}``；非法键/值一律丢弃。"""
    source = bonuses if isinstance(bonuses, Mapping) else {}
    result: dict[str, int] = {}
    for raw_key, raw_value in source.items():
        key = str(raw_key or "").strip().lower()
        if key not in ABILITY_KEYS:
            continue
        value = _as_int(raw_value, 0)
        if value:
            result[key] = value
    return result


def with_race_bonuses(base: Mapping[str, Any], bonuses: Mapping[str, Any] | None) -> dict[str, int]:
    """基础值 + 种族加值 = 总值；结果夹到 1–30（与 schema 归一化同口径）。"""
    source = base if isinstance(base, Mapping) else {}
    stacked = normalize_bonuses(bonuses)
    result: dict[str, int] = {}
    for key in ABILITY_KEYS:
        value = _as_int(source.get(key, 10), 10) + stacked.get(key, 0)
        result[key] = max(ABILITY_FLOOR, min(ABILITY_CEILING, value))
    return result


# --- 派生（与 schema.derive 同口径）-----------------------------------------


def level_one_hit_points(hit_die: Any, con_modifier: Any) -> int:
    """1 级 HP = 生命骰满值 + 体质调整值，至少 1。"""
    return max(1, _as_int(hit_die, 8) + _as_int(con_modifier, 0))


def armor_class_from_dex(dex_score: Any) -> int:
    """无甲 AC = 10 + 敏捷调整值（与 schema._armor_class 的兜底一致）。"""
    return 10 + ability_modifier(dex_score)


def passive_perception(abilities: Mapping[str, Any], proficient: bool = False, expertise: bool = False, level: int = 1) -> int:
    """被动感知 = 10 + 察觉加值（熟练时再加熟练加值，专精翻倍）。"""
    bonus = ability_modifier((abilities or {}).get("wis", 10))
    if proficient or expertise:
        multiplier = 2 if expertise else 1
        bonus += proficiency_bonus(level) * multiplier
    return 10 + bonus


def spellcasting_preview(abilities: Mapping[str, Any], ability: Any, level: int = 1) -> dict[str, int] | None:
    """法术豁免 DC / 攻击加值；非施法者（无施法属性）返回 ``None``。"""
    key = str(ability or "").strip().lower()
    if key not in ABILITY_KEYS:
        return None
    bonus = proficiency_bonus(level) + ability_modifier((abilities or {}).get(key, 10))
    return {"spellSaveDC": 8 + bonus, "spellAttackBonus": bonus}


def spell_slots_at_level_one(caster: bool, pact_caster: bool = False) -> dict[str, dict[str, int]]:
    """1 级法术位：完整施法者 2 个 1 环；邪术师（契约魔法）1 个 1 环。"""
    if not caster:
        return {}
    maximum = 1 if pact_caster else 2
    return {"1": {"max": maximum, "used": 0}}


def preview(sheet: Mapping[str, Any]) -> dict[str, Any]:
    """只读速览：六属性/调整值、HP/AC/熟练/被动感知/法术 DC 与法术位。

    口径与 :meth:`dnd5e_plugin.schema.Dnd5eCharacterSchema.derive` 一一对应，
    差异由 ``tests/test_dnd5e_builder.py`` 的对照用例守住。
    """
    source = sheet if isinstance(sheet, Mapping) else {}
    abilities = source.get("abilities") if isinstance(source.get("abilities"), Mapping) else {}
    scores = {key: max(ABILITY_FLOOR, min(ABILITY_CEILING, _as_int(abilities.get(key, 10), 10))) for key in ABILITY_KEYS}
    level = max(1, min(20, _as_int(source.get("level", 1), 1)))
    modifiers = {key: ability_modifier(scores[key]) for key in ABILITY_KEYS}

    hp = source.get("hp") if isinstance(source.get("hp"), Mapping) else {}
    max_hp = max(1, _as_int(hp.get("max", 1), 1))

    proficiencies = source.get("proficiencies") if isinstance(source.get("proficiencies"), Mapping) else {}
    skills = {str(item).strip().lower() for item in (proficiencies.get("skills") or []) if str(item).strip()}
    expertise = {str(item).strip().lower() for item in (proficiencies.get("expertise") or []) if str(item).strip()}

    casting = source.get("spellcasting") if isinstance(source.get("spellcasting"), Mapping) else {}
    spell = spellcasting_preview(scores, casting.get("ability"), level)
    slots = casting.get("slots") if isinstance(casting.get("slots"), Mapping) else {}

    return {
        "abilities": scores,
        "modifiers": modifiers,
        "maxHp": max_hp,
        "armorClass": armor_class_from_dex(scores["dex"]),
        "proficiencyBonus": proficiency_bonus(level),
        "initiative": modifiers["dex"],
        "passivePerception": passive_perception(scores, "perception" in skills, "perception" in expertise, level),
        "spellSaveDC": spell["spellSaveDC"] if spell else None,
        "spellAttackBonus": spell["spellAttackBonus"] if spell else None,
        "spellSlots": {str(key): dict(value) for key, value in slots.items() if isinstance(value, Mapping)},
    }


# --- 产出 sheet -------------------------------------------------------------


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_skill_id(value: Any) -> str:
    """技能 id 归一：``Animal Handling`` / ``animal_handling`` → ``animal-handling``。"""
    key = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if not key:
        return ""
    candidate = key.replace(" ", "-")
    if candidate in SKILLS:
        return candidate
    # 中文技能名（旧卡写法）也接受，但存进去的必须是英文 id。
    for skill_id, (_ability, label, _eng) in SKILLS.items():
        if key == label:
            return skill_id
    return candidate


def build_sheet(
    *,
    race: str = "",
    class_name: str = "",
    background: str = "",
    abilities: Mapping[str, Any] | None = None,
    hit_die: int = 8,
    skills: Iterable[str] = (),
    saves: Iterable[str] = (),
    armor: Iterable[str] = (),
    weapons: Iterable[str] = (),
    tools: Iterable[str] = (),
    languages: Iterable[str] = (),
    equipment: Iterable[Mapping[str, Any]] = (),
    speed: int = 30,
    spellcasting_ability: str = "",
    caster: bool = False,
    pact_caster: bool = False,
    level: int = 1,
    xp: int = 0,
    bio: str = "",
) -> dict[str, Any]:
    """按向导选择产出 1 级 sheet；保存时由插件 schema 再做一次归一与派生。

    ``armorClass`` **故意不写**：卡面保存时由 ``schema.pack`` 按 10 + 敏捷调整值
    补上，写了反而会把「改了敏捷但 AC 不动」的坑固化进存档。
    """
    scores = {key: max(ABILITY_FLOOR, min(ABILITY_CEILING, _as_int((abilities or {}).get(key, 10), 10))) for key in ABILITY_KEYS}
    level_value = max(1, min(20, _as_int(level, 1)))
    die = max(4, _as_int(hit_die, 8))
    max_hp = level_one_hit_points(die, ability_modifier(scores["con"]))
    ability_key = str(spellcasting_ability or "").strip().lower()
    casting: dict[str, Any] = {
        "ability": ability_key if ability_key in ABILITY_KEYS else "",
        "known": [],
        "prepared": [],
        "slots": spell_slots_at_level_one(caster and ability_key in ABILITY_KEYS, pact_caster),
    }
    # 熟练列表统一存英文 id：卡面与插件的判断都按 id 走（中文写法不会生效）。
    skill_ids: list[str] = []
    for value in skills:
        skill_id = normalize_skill_id(value)
        if skill_id and skill_id in SKILLS and skill_id not in skill_ids:
            skill_ids.append(skill_id)
    save_ids: list[str] = []
    for value in saves:
        key = str(value or "").strip().lower()
        if key in ABILITY_KEYS and key not in save_ids:
            save_ids.append(key)
    rows: list[dict[str, Any]] = []
    for item in equipment:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "quantity": max(1, _as_int(item.get("quantity", 1), 1)),
                "weight": max(0, _as_int(item.get("weight", 0), 0)),
                "equipped": item.get("equipped") is True,
            }
        )
    return {
        "level": level_value,
        "class": str(class_name or "").strip(),
        "subclass": "",
        "race": str(race or "").strip(),
        "background": str(background or "").strip(),
        "alignment": "",
        "xp": max(0, _as_int(xp, 0)),
        "abilities": scores,
        "proficiencies": {
            "saves": save_ids,
            "skills": skill_ids,
            "expertise": [],
            "armor": _string_list(armor),
            "weapons": _string_list(weapons),
            "tools": _string_list(tools),
            "languages": _string_list(languages),
        },
        "hp": {"max": max_hp, "current": max_hp, "temp": 0},
        "speed": max(0, _as_int(speed, 30)),
        "hitDice": f"{level_value}d{die}",
        "hit_dice": {"die": die, "max": level_value, "used": 0},
        "spellcasting": casting,
        "equipment": rows,
        "features": [],
        "notes": "",
        "bio": str(bio or ""),
        "appearance": "",
        "token": "",
        "status": [],
        "deathSaves": {"success": 0, "failure": 0},
        "exhaustion": 0,
        "inspiration": False,
    }
