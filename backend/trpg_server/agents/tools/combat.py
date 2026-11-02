"""COC7 战斗轮工具。

实现规则书第六章（第 86–110 页）的核心战斗流程：DEX 先攻顺序、近战对抗
（反击/闪避）、极限成功与贯穿、战技、射击难度与故障、护甲、重伤/濒死/死亡
以及急救/医学。骰点、成功等级、展示元数据与日志全部复用 ``dice.py`` 的
既有函数，状态写入沿用房间 ``info.json`` / ``state.json`` 的原子写模式。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any

from trpg_server import weapon_catalog
from trpg_server.agents.room_state import load_room_state, save_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.dice import (
    _context_thresholds,
    _dice_count,
    _dice_roll_metadata,
    _find_member,
    _lookup_check_value,
    _log_roll_result,
    _percentile_roll,
    _result_message,
    _success_level,
)
from trpg_server.json_store import write_json_atomic

LEVEL_RANK = {"fumble": -1, "failure": 0, "regular": 1, "hard": 2, "extreme": 3, "critical": 4}
SKIPPED_STATUS = {"unconscious", "dying", "dead"}
MAX_BURST = 10

UNARMED_WEAPON = {
    "name": "徒手",
    "skill": "格斗（斗殴）",
    "skillKey": "fighting",
    "specialtyKey": "brawl",
    "damage": "1D3",
    "range": "接触",
    "impale": False,
    "attacks": "1",
    "ammo": "N/A",
    "malfunction": "N/A",
}


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip()


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _roller(sides: int) -> int:
    return random.randint(1, sides)


# ---------------------------------------------------------------------------
# 战斗状态读写
# ---------------------------------------------------------------------------
def _load_combat(context: Any) -> dict[str, Any] | None:
    room_dir = getattr(context, "room_dir", None)
    if not room_dir:
        return None
    combat = load_room_state(room_dir).get("combat")
    if isinstance(combat, dict) and combat.get("active"):
        return combat
    return None


_COMBAT_BROADCASTER: Any = None


def register_broadcaster(fn: Any) -> None:
    """注入战斗状态广播函数：``fn(event, payload)``。"""
    global _COMBAT_BROADCASTER
    _COMBAT_BROADCASTER = fn


def _broadcast_state(combat: dict[str, Any] | None) -> None:
    broadcaster = _COMBAT_BROADCASTER
    if broadcaster is None:
        return
    state = _combat_summary_state(combat) if isinstance(combat, dict) and combat.get("active") else {"active": False}
    payload = {
        **state,
        "room_id": (combat or {}).get("room_id") if isinstance(combat, dict) else None,
        "log": (combat or {}).get("log") if isinstance(combat, dict) else [],
    }
    try:
        broadcaster("combat_updated", payload)
    except Exception:  # noqa: BLE001 - 广播失败不能影响战斗结算
        pass


def _broadcast_dice(context: Any, meta: dict[str, Any]) -> None:
    """把服务端（NPC）掷出的骰子广播给前端骰桌，按房间设置播放动画。"""
    broadcaster = _COMBAT_BROADCASTER
    if broadcaster is None:
        return
    payload = {"room_id": str(getattr(context, "room_id", "") or ""), "meta": meta}
    try:
        broadcaster("combat_dice", payload)
    except Exception:  # noqa: BLE001 - 展示失败不能影响战斗结算
        pass


def _percentile_meta(
    roll: int,
    level: str,
    *,
    label: str,
    target: int,
    roller: str,
) -> dict[str, Any]:
    return _dice_roll_metadata(
        {
            "roll": int(roll),
            "rolls": [int(roll)],
            "success": LEVEL_RANK.get(level, 0) > 0,
            "success_level": level,
            "bonus_dice": 0,
            "penalty_dice": 0,
        },
        mode="percentile",
        check_name=label,
        target=max(1, int(target or 1)),
        roller=roller,
    )


def _save_combat(context: Any, combat: dict[str, Any]) -> None:
    room_dir = getattr(context, "room_dir", None)
    if not room_dir:
        return
    state = load_room_state(room_dir)
    state["combat"] = combat
    save_room_state(room_dir, state)
    combat["room_id"] = str(getattr(context, "room_id", "") or "")
    _broadcast_state(combat)


def _combat_summary_state(combat: dict[str, Any]) -> dict[str, Any]:
    order = combat.get("order") or []
    current = order[combat.get("turn")] if 0 <= int(combat.get("turn") or 0) < len(order) else None
    participants = []
    for name in order:
        combatant = (combat.get("combatants") or {}).get(name) or {}
        participants.append(
            {
                "name": name,
                "user_id": combatant.get("user_id"),
                "is_player": bool(combatant.get("is_player")),
                "side": combatant.get("side"),
                "dex": combatant.get("dex"),
                "initiative": combatant.get("initiative"),
                "hp": combatant.get("hp"),
                "max_hp": combatant.get("max_hp"),
                "status": combatant.get("status"),
                "armor": combatant.get("armor"),
                "major_wound": bool(combatant.get("major_wound")),
                "used_dodge": bool(combatant.get("used_dodge")),
                "used_counter": bool(combatant.get("used_counter")),
                "surprised": _is_surprised(combat, combatant),
            }
        )
    return {
        "active": bool(combat.get("active")),
        "round": combat.get("round"),
        "turn": combat.get("turn"),
        "current": current,
        "order": participants,
        "surprised": combat.get("surprised") or [],
    }


def _append_log(combat: dict[str, Any], text: str) -> None:
    log = combat.get("log")
    if not isinstance(log, list):
        log = []
        combat["log"] = log
    log.append({"time": _timestamp(), "text": text[:500]})
    del log[:-20]


# ---------------------------------------------------------------------------
# 角色数值
# ---------------------------------------------------------------------------
def build_and_damage_bonus(str_value: int, siz_value: int) -> tuple[int, str, list[tuple[int, int]]]:
    """按 COC7 体格表由 STR+SIZ 推导（体格, 伤害加值文本, DB 骰点）。"""
    total = max(0, str_value) + max(0, siz_value)
    if total <= 64:
        return -2, "-2", []
    if total <= 84:
        return -1, "-1", []
    if total <= 124:
        return 0, "0", []
    if total <= 164:
        return 1, "+1D4", [(1, 4)]
    if total <= 204:
        return 2, "+1D6", [(1, 6)]
    extra = max(0, (total - 205) // 80)
    return 3 + extra, f"+{2 + extra}D6", [(2 + extra, 6)]


def _card_attributes(card: dict[str, Any]) -> dict[str, Any]:
    attributes = card.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _card_max_hp(card: dict[str, Any]) -> int:
    state_values = (card.get("maxHp"), card.get("max_hp"))
    for value in state_values:
        if value not in (None, ""):
            return max(1, _as_int(value, 1))
    attrs = _card_attributes(card)
    con = _as_int(attrs.get("CON"), 0)
    siz = _as_int(attrs.get("SIZ"), 0)
    return max(1, (con + siz) // 10) if con or siz else 1


def _card_hp(card: dict[str, Any], state: dict[str, Any]) -> tuple[int, int]:
    max_hp = _as_int(state.get("max_hp"), _card_max_hp(card)) or _card_max_hp(card)
    current = state.get("current_hp")
    if current in (None, ""):
        current = card.get("currentHp", card.get("current_hp", max_hp))
    return max(0, min(max_hp, _as_int(current, max_hp))), max_hp


def _combatant_from_member(member: dict[str, Any]) -> dict[str, Any]:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    attrs = _card_attributes(card)
    dex = max(0, _as_int(attrs.get("DEX"), _as_int(card.get("dex"), 0)))
    strength = max(0, _as_int(attrs.get("STR"), _as_int(card.get("str"), 0)))
    size = max(0, _as_int(attrs.get("SIZ"), _as_int(card.get("siz"), 0)))
    constitution = max(0, _as_int(attrs.get("CON"), _as_int(card.get("con"), 50)))
    build, damage_bonus, db_terms = build_and_damage_bonus(strength, size)
    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    hp, max_hp = _card_hp(card, state)
    names = [str(member.get("username") or ""), str(card.get("name") or "")]
    canonical = str(card.get("name") or member.get("username") or member.get("user_id") or "调查员").strip()
    status = "active"
    if state.get("dead") or (card.get("status") or {}).get("dead"):
        status = "dead"
    elif state.get("dying"):
        status = "dying"
    elif state.get("unconscious") or (card.get("status") or {}).get("unconscious"):
        status = "unconscious"
    return {
        "name": canonical,
        "aliases": [name for name in names if name],
        "user_id": member.get("user_id"),
        "is_player": True,
        "side": "player",
        "dex": dex,
        "initiative": dex,
        "str": strength,
        "siz": size,
        "con": constitution,
        "build": build,
        "damage_bonus": damage_bonus,
        "db_terms": [list(term) for term in db_terms],
        "hp": hp,
        "max_hp": max_hp,
        "armor": 0,
        "status": status,
        "major_wound": bool(state.get("major_wound")),
        "dying": status == "dying",
        "unconscious": status == "unconscious",
        "dead": status == "dead",
        "used_dodge": False,
        "used_counter": False,
        "first_aid_used": bool(state.get("first_aid_used")),
        "ammo": {},
        "conditions": [],
    }


def _combatant_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    name = _as_text(spec.get("name") or spec.get("player_name"))
    if not name:
        raise ValueError("participants 中的 NPC 条目缺少 name")
    dex = max(0, _as_int(spec.get("dex"), 50))
    strength = max(0, _as_int(spec.get("str"), 50))
    size = max(0, _as_int(spec.get("siz"), 50))
    constitution = max(0, _as_int(spec.get("con"), 50))
    build_value = spec.get("build")
    if build_value in (None, ""):
        build, damage_bonus, db_terms = build_and_damage_bonus(strength, size)
    else:
        build = _as_int(build_value, 0)
        _, damage_bonus, db_terms = build_and_damage_bonus(strength, size)
        if spec.get("damageBonus"):
            damage_bonus = _as_text(spec.get("damageBonus"))
    max_hp = max(1, _as_int(spec.get("max_hp", spec.get("maxHp")), max(1, (constitution + size) // 10)))
    hp = max(0, min(max_hp, _as_int(spec.get("hp", spec.get("current_hp")), max_hp)))
    status = _as_text(spec.get("status"), "active") or "active"
    if status not in {"active", "unconscious", "dying", "dead"}:
        status = "active"
    skills = _skill_map(spec.get("skills"))
    weapons = weapon_catalog.sanitize_character_weapons(spec.get("weapons"))
    return {
        "name": name,
        "aliases": [name],
        "user_id": None,
        "is_player": False,
        "side": _as_text(spec.get("side"), "enemy") or "enemy",
        "dex": dex,
        "initiative": dex,
        "str": strength,
        "siz": size,
        "con": constitution,
        "build": build,
        "damage_bonus": damage_bonus,
        "db_terms": [list(term) for term in db_terms],
        "hp": hp,
        "max_hp": max_hp,
        "armor": max(0, _as_int(spec.get("armor"), 0)),
        "status": status,
        "major_wound": status in {"dying", "dead"} or bool(spec.get("major_wound")),
        "dying": status == "dying",
        "unconscious": status == "unconscious",
        "dead": status == "dead",
        "used_dodge": False,
        "used_counter": False,
        "first_aid_used": False,
        "ammo": {},
        "conditions": [],
        "skills": skills,
        "weapons": weapons,
    }


def _unique_name(combatants: dict[str, Any], name: str) -> str:
    if name not in combatants:
        return name
    index = 2
    while f"{name}（{index}）" in combatants:
        index += 1
    return f"{name}（{index}）"


NPC_ONLY_FIELDS = {"skills", "weapons", "hp", "max_hp", "maxHp", "current_hp", "armor", "str", "siz", "con", "build", "side"}


def _is_npc_entry(entry: dict[str, Any]) -> bool:
    """区分 participants 中的 NPC 对象与玩家对象。"""
    if entry.get("is_npc") is True:
        return True
    if entry.get("player_name") or entry.get("is_player") is True:
        return False
    return any(key in entry for key in NPC_ONLY_FIELDS)


def _skill_map(value: Any) -> dict[str, int]:
    """技能统一成 {名称: 值}：兼容玩家卡的 dict 与模块卡/导入数据的数组。"""
    if isinstance(value, dict):
        return {str(name): _as_int(number, 0) for name, number in value.items() if str(name).strip()}
    result: dict[str, int] = {}
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        name = _as_text(entry.get("name") or entry.get("title"))
        number = entry.get("base") if entry.get("base") is not None else entry.get("value")
        if name:
            result[name] = _as_int(number, 0)
    return result


def _card_number(battle: dict[str, Any], attributes: dict[str, Any], battle_key: str, attr_key: str, fallback: int) -> int:
    candidates = [(battle, battle_key), (attributes, attr_key or battle_key.upper()), (attributes, battle_key.upper())]
    if attr_key:
        candidates.append((battle, attr_key))
    for source, key in candidates:
        value = source.get(key) if isinstance(source, dict) else None
        if value not in (None, ""):
            number = _as_int(value, -1)
            if number >= 0:
                return number
    return fallback


def _scenario_module(context: Any, module_id: str) -> dict[str, Any] | None:
    """按 id 从当前房间绑定的剧本读取 npc/monster 模块卡。"""
    scenarios_dir = getattr(context, "scenarios_dir", None)
    if not scenarios_dir or not module_id:
        return None
    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else {}
    scenario_id = info.get("scenario_id") if isinstance(info, dict) else None
    if scenario_id in (None, ""):
        return None
    from trpg_server.scenario_store import load_scenario_by_id

    _, scenario = load_scenario_by_id(Path(scenarios_dir), scenario_id, (info or {}).get("scenario_version"))
    modules = scenario.get("modules") if isinstance(scenario, dict) else None
    for module in modules if isinstance(modules, list) else []:
        if isinstance(module, dict) and str(module.get("id")) == module_id:
            return module
    return None


def _module_combat_spec(module: dict[str, Any]) -> dict[str, Any]:
    """把剧本里的 npc/monster 模块卡转换成 combat.start 的参与者对象。"""
    attributes = module.get("attributes") if isinstance(module.get("attributes"), dict) else {}
    battle = module.get("battle") if isinstance(module.get("battle"), dict) else {}
    module_type = _as_text(module.get("module_type")).lower()
    constitution = _card_number(battle, attributes, "con", "CON", 50)
    size = _card_number(battle, attributes, "siz", "SIZ", 50)
    max_hp = _card_number(battle, attributes, "max_hp", "", -1)
    if max_hp < 1:
        max_hp = _card_number(battle, attributes, "hp", "HP", -1)
    if max_hp < 1:
        max_hp = max(1, (constitution + size) // 10)
    hp = max(0, min(max_hp, _card_number(battle, attributes, "hp", "HP", max_hp)))
    skills = module.get("skills")
    if not skills and isinstance(battle.get("skills"), (dict, list)):
        skills = battle.get("skills")
    weapons = module.get("weapons")
    if not weapons and isinstance(battle.get("weapons"), list):
        weapons = battle.get("weapons")
    return {
        "module_id": _as_text(module.get("id")),
        "name": _as_text(module.get("title") or module.get("code"), "未知敌人")[:80],
        "is_npc": True,
        "side": "enemy" if module_type == "monster" else "npc",
        "dex": _card_number(battle, attributes, "dex", "DEX", 50),
        "str": _card_number(battle, attributes, "str", "STR", 50),
        "siz": size,
        "con": constitution,
        "hp": max(0, min(max_hp, hp)),
        "max_hp": max_hp,
        "armor": _card_number(battle, attributes, "armor", "", 0),
        "skills": _skill_map(skills),
        "weapons": weapons if isinstance(weapons, list) else [],
    }


def _resolve_combatant(combat: dict[str, Any], name: Any) -> dict[str, Any] | None:
    key = _as_text(name).casefold()
    if not key:
        return None
    for combatant in (combat.get("combatants") or {}).values():
        if str(combatant.get("name") or "").casefold() == key:
            return combatant
        aliases = {str(item).casefold() for item in combatant.get("aliases") or [] if str(item).strip()}
        if key in aliases:
            return combatant
    return None


# ---------------------------------------------------------------------------
# 技能与武器
# ---------------------------------------------------------------------------
def _player_card(combatant: dict[str, Any], context: Any) -> dict[str, Any] | None:
    if not combatant.get("is_player"):
        return None
    member = _find_member(str(combatant.get("name") or ""), context)
    if not member:
        return None
    card = member.get("character_card")
    return card if isinstance(card, dict) else None


def _npc_skill(combatant: dict[str, Any], requested: str) -> int | None:
    skills = combatant.get("skills") if isinstance(combatant.get("skills"), dict) else {}
    key = requested.casefold()
    for name, value in skills.items():
        text = str(name).casefold()
        if text == key or key in text or text in key:
            return _as_int(value, 0)
    return None


def _resolve_skill_value(
    combatant: dict[str, Any],
    card: dict[str, Any] | None,
    *,
    requested: str = "",
    skill_key: str = "",
    specialty_key: str = "",
    names: tuple[str, ...] = (),
    default: int = 0,
) -> tuple[int, str]:
    """返回（技能值, 展示名）。优先精确专精，其次技能键，最后按名称匹配。"""
    skills = card.get("skills") if isinstance(card, dict) and isinstance(card.get("skills"), list) else []
    if requested:
        value = _lookup_check_value(card, requested) if isinstance(card, dict) else _npc_skill(combatant, requested)
        if value is not None and value >= 0:
            return value, requested
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        key = _as_text(skill.get("skillKey")).casefold()
        specialty = _as_text(skill.get("specialtyKey")).casefold()
        if skill_key and key == skill_key.casefold() and (not specialty_key or specialty == specialty_key.casefold()):
            return _as_int(skill.get("value"), default), _as_text(skill.get("name"), requested or skill_key)
    for name in names:
        value = _lookup_check_value(card, name) if isinstance(card, dict) else _npc_skill(combatant, name)
        if value is not None and value >= 0:
            return value, name
    if not card and requested:
        value = _npc_skill(combatant, requested)
        if value is not None:
            return value, requested
    return default, requested or (names[0] if names else "格斗")


MELEE_SKILL_NAMES = ("格斗（斗殴）", "格斗（斗殴技能）", "斗殴", "格斗", "fencing", "brawl", "fighting")
RANGED_SKILL_NAMES = ("射击（手枪）", "射击（步枪）", "射击（霰弹枪）", "射击", "枪械", "firearms")
DODGE_SKILL_NAMES = ("闪避", "dodge")


def _weapon_rows(combatant: dict[str, Any], card: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(card, dict):
        for key in ("weapons", "customWeapons"):
            value = card.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
    if isinstance(combatant.get("weapons"), list):
        rows.extend(item for item in combatant["weapons"] if isinstance(item, dict))
    return rows


def _find_weapon(combatant: dict[str, Any], card: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    rows = _weapon_rows(combatant, card)
    if not _as_text(name):
        return rows[0] if rows else None
    key = _as_text(name).casefold()
    for row in rows:
        if _as_text(row.get("name")).casefold() == key:
            return row
    for row in rows:
        if key and key in _as_text(row.get("name")).casefold():
            return row
    return None


def _first_melee_weapon(combatant: dict[str, Any], card: dict[str, Any] | None) -> dict[str, Any] | None:
    """反击时优先使用目标的第一件近战武器；没有则返回 None（按徒手结算）。"""
    for row in _weapon_rows(combatant, card):
        yards = weapon_catalog.weapon_rules(row).get("rangeYards")
        if yards is None or yards <= 0:
            return row
    return None


def _damage_roller(terms: list[tuple[int, int]]) -> tuple[int, list[dict[str, Any]]]:
    total = 0
    detail = []
    for count, sides in terms:
        count = max(0, int(count))
        sides = max(1, int(sides))
        rolls = [_roller(sides) for _ in range(count)] or [0]
        subtotal = sum(rolls) if count else 0
        total += subtotal
        detail.append({"count": count, "sides": sides, "rolls": rolls, "total": subtotal})
    return total, detail


def _max_roll(terms: list[tuple[int, int]]) -> int:
    return sum(max(0, int(count)) * max(1, int(sides)) for count, sides in terms)


def _compute_damage(
    weapon: dict[str, Any],
    db_terms: list[list[int]],
    *,
    extreme: bool,
    melee: bool,
    segment_index: int = 0,
) -> tuple[int, str]:
    """按武器伤害、伤害加值与极限成功/贯穿规则计算伤害。"""
    rules = weapon_catalog.weapon_rules(weapon)
    parsed = rules["damage"]
    segments = parsed.get("segments") or [{"terms": [(1, 3)], "fixed": 0}]
    segment = segments[min(max(0, segment_index), len(segments) - 1)]
    terms = [tuple(term) for term in segment.get("terms") or []]
    fixed = _as_int(segment.get("fixed"), 0)
    impale = bool(rules.get("impale"))

    add_db = (melee or bool(parsed.get("db"))) and not bool(parsed.get("half_db"))
    half_db = bool(parsed.get("half_db"))
    db = [tuple(term) for term in db_terms if len(term) == 2]
    if half_db:
        db = [(max(0, count) // 2, sides) for count, sides in db]
    if not add_db and not half_db:
        db = []

    if extreme and not impale:
        total = _max_roll(terms) + fixed + _max_roll(db)
        return max(0, total), f"极限成功·满伤害（{parsed.get('raw')}{' +DB' if db else ''}）"
    if extreme and impale:
        extra, _ = _damage_roller(terms)
        total = _max_roll(terms) + fixed + extra + _max_roll(db)
        return max(0, total), f"极限成功·贯穿满伤害并追加一次武器伤害骰 {terms}"
    rolled, detail = _damage_roller(terms)
    db_rolled, _ = _damage_roller(db)
    total = rolled + fixed + db_rolled
    detail_text = "，".join(f"{item['count']}D{item['sides']}={item['rolls']}" for item in detail)
    if db:
        detail_text += f"；DB={db_rolled}"
    return max(0, total), detail_text


# ---------------------------------------------------------------------------
# 伤害结算
# ---------------------------------------------------------------------------
def _persist_player_state(context: Any, combatant: dict[str, Any]) -> None:
    if not combatant.get("is_player") or not getattr(context, "room_dir", None):
        return
    info = context.room_info()
    members = info.get("members") if isinstance(info.get("members"), list) else []
    for member in members:
        if not isinstance(member, dict) or member.get("user_id") != combatant.get("user_id"):
            continue
        state = member.setdefault("character_state", {})
        if not isinstance(state, dict):
            state = {}
            member["character_state"] = state
        state["max_hp"] = combatant.get("max_hp")
        state["current_hp"] = combatant.get("hp")
        state["major_wound"] = bool(combatant.get("major_wound"))
        state["dying"] = bool(combatant.get("dying"))
        state["unconscious"] = bool(combatant.get("unconscious"))
        state["dead"] = bool(combatant.get("dead"))
        state["first_aid_used"] = bool(combatant.get("first_aid_used"))
        card = member.get("character_card")
        if isinstance(card, dict):
            card["currentHp"] = combatant.get("hp")
            card["current_hp"] = combatant.get("hp")
            card["maxHp"] = combatant.get("max_hp")
            status = card.get("status") if isinstance(card.get("status"), dict) else {}
            status["majorWound"] = bool(combatant.get("major_wound"))
            status["unconscious"] = bool(combatant.get("unconscious"))
            status["dead"] = bool(combatant.get("dead"))
            card["status"] = status
        break
    write_json_atomic(Path(context.room_dir) / "info.json", info)


def _apply_damage(
    context: Any,
    combatant: dict[str, Any],
    amount: int,
    *,
    armor: int | None = None,
    ignore_armor: bool = False,
) -> dict[str, Any]:
    """结算一次伤害：护甲、重伤、昏迷、濒死与死亡。"""
    damage = max(0, int(amount))
    armor_value = max(0, int(armor if armor is not None else combatant.get("armor") or 0))
    effective = damage if ignore_armor else max(0, damage - armor_value)
    before = int(combatant.get("hp") or 0)
    max_hp = max(1, int(combatant.get("max_hp") or 1))
    result: dict[str, Any] = {
        "damage": damage,
        "armor": 0 if ignore_armor else armor_value,
        "effective": effective,
        "hp_before": before,
        "major_wound": bool(combatant.get("major_wound")),
    }
    if combatant.get("dead"):
        combatant["hp"] = 0
        result.update({"hp_after": 0, "dead": True, "dying": bool(combatant.get("dying")), "unconscious": True})
        result["note"] = "目标已经死亡，伤害不再改变状态"
        return result
    if effective > max_hp:
        combatant.update({"hp": 0, "dead": True, "dying": False, "unconscious": True})
        result.update({"hp_after": 0, "dead": True, "dying": False, "unconscious": True, "major_wound": True})
        result["note"] = "单次伤害超过最大生命值，目标死亡"
        _persist_player_state(context, combatant)
        return result

    after = max(0, before - effective)
    combatant["hp"] = after
    major = effective >= max_hp / 2 and effective > 0
    if major:
        combatant["major_wound"] = True
    dying = unconscious = False
    con_roll = con_level = None
    if after <= 0:
        if major or combatant.get("major_wound"):
            dying = True
        else:
            unconscious = True
    elif major:
        con_value = max(1, min(99, int(combatant.get("con") or 50)))
        con_roll, _ = _percentile_roll(_roller)
        critical, fumble = _context_thresholds(context)
        con_level = _success_level(con_roll, con_value, critical, fumble)
        if LEVEL_RANK.get(con_level, 0) <= 0:
            unconscious = True
    if dying:
        combatant["dying"] = True
        combatant["unconscious"] = True
    elif unconscious:
        combatant["unconscious"] = True
    result.update(
        {
            "hp_after": after,
            "major_wound": bool(combatant.get("major_wound")),
            "dying": bool(combatant.get("dying")),
            "unconscious": bool(combatant.get("unconscious")),
            "dead": bool(combatant.get("dead")),
            "con_check": None
            if con_roll is None
            else {"roll": con_roll, "value": combatant.get("con"), "level": con_level},
        }
    )
    _persist_player_state(context, combatant)
    return result


def _format_damage_result(target: str, result: dict[str, Any]) -> str:
    text = (
        f"{target} 受到 {result['effective']} 点伤害"
        f"（原伤害 {result['damage']}，护甲 {result['armor']}），HP {result['hp_before']}→{result['hp_after']}"
    )
    if result.get("major_wound"):
        text += "，触发重伤"
    if result.get("con_check"):
        check = result["con_check"]
        text += f"，重伤体质检定 d100={check['roll']} / {check['value']}（{check['level']}）"
    if result.get("dead"):
        text += "，死亡"
    elif result.get("dying"):
        text += "，陷入濒死"
    elif result.get("unconscious"):
        text += "，昏迷"
    if result.get("note"):
        text += f"；{result['note']}"
    return text


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def start_combat(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not getattr(context, "room_dir", None):
        return {"error": "combat tools require a room context"}
    raw = arguments.get("participants")
    entries = raw if isinstance(raw, list) else []
    combatants: dict[str, Any] = {}
    try:
        module_refs = arguments.get("module_ids")
        refs = [str(item).strip() for item in module_refs] if isinstance(module_refs, list) else []
        if arguments.get("module_id"):
            refs.insert(0, _as_text(arguments.get("module_id")))
        for module_id in refs:
            module = _scenario_module(context, module_id)
            if not module:
                return {"error": f"scenario module {module_id} was not found"}
            spec = _module_combat_spec(module)
            if not spec["module_id"]:
                spec["module_id"] = module_id
            combatant = _combatant_from_spec(spec)
            key = _unique_name(combatants, combatant["name"])
            combatant["name"] = key
            combatants[key] = combatant
        if entries:
            for entry in entries:
                module_id = _as_text(entry.get("module_id")) if isinstance(entry, dict) else ""
                if module_id:
                    module = _scenario_module(context, module_id)
                    if not module:
                        return {"error": f"scenario module {module_id} was not found"}
                    spec = _module_combat_spec(module)
                    if not spec["module_id"]:
                        spec["module_id"] = module_id
                    for key in ("name", "side", "hp"):
                        value = entry.get(key)
                        if value not in (None, ""):
                            spec[key] = value
                    combatant = _combatant_from_spec(spec)
                    key = _unique_name(combatants, combatant["name"])
                    combatant["name"] = key
                    combatants[key] = combatant
                elif isinstance(entry, dict) and _is_npc_entry(entry):
                    combatant = _combatant_from_spec(entry)
                    key = _unique_name(combatants, combatant["name"])
                    combatant["name"] = key
                    combatants[key] = combatant
                else:
                    name = entry if isinstance(entry, str) else _as_text(entry.get("name") or entry.get("player_name"))
                    member = _find_member(name, context)
                    if not member:
                        return {"error": f"player {name} was not found in current room"}
                    combatant = _combatant_from_member(member)
                    key = _unique_name(combatants, combatant["name"])
                    combatant["name"] = key
                    combatants[key] = combatant
        elif not refs:
            members = context.room_info().get("members")
            for member in members if isinstance(members, list) else []:
                if not isinstance(member, dict) or not isinstance(member.get("character_card"), dict):
                    continue
                if member.get("is_active", True) is False or member.get("status", "active") == "removed":
                    continue
                combatant = _combatant_from_member(member)
                key = _unique_name(combatants, combatant["name"])
                combatant["name"] = key
                combatants[key] = combatant
    except ValueError as exc:
        return {"error": str(exc)}
    if not combatants:
        return {"error": "没有可加入战斗的参与者（请传入 participants 或确保房间成员已绑定角色卡）"}

    if arguments.get("initiative_check"):
        critical, fumble = _context_thresholds(context)
        for combatant in combatants.values():
            roll, _ = _percentile_roll(_roller)
            level = _success_level(roll, max(1, int(combatant.get("dex") or 1)), critical, fumble)
            combatant["initiative"] = LEVEL_RANK.get(level, 0) * 1000 + int(combatant.get("dex") or 0)
            combatant["initiative_roll"] = roll

    order = sorted(
        combatants.keys(),
        key=lambda name: (
            -int(combatants[name].get("initiative") or 0),
            -int(combatants[name].get("dex") or 0),
            -int((_resolve_skill_value(combatants[name], _player_card(combatants[name], context), names=MELEE_SKILL_NAMES)[0]) or 0),
        ),
    )
    combat = {
        "active": True,
        "round": 1,
        "turn": 0,
        "order": order,
        "combatants": combatants,
        "surprised": [str(name) for name in arguments.get("surprised") or [] if str(name).strip()],
        "started_at": _timestamp(),
        "log": [],
    }
    _append_log(combat, "战斗开始：" + " → ".join(order))
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    summary = (
        f"战斗开始，第 1 轮。先攻顺序："
        + " → ".join(f"{item['name']}(DEX {item['dex']})" for item in state["order"])
        + f"。当前行动者：{state['current']}。"
    )
    return {**state, "summary": summary}


def combat_state(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"active": False, "summary": "当前没有进行中的战斗。"}
    state = _combat_summary_state(combat)
    lines = [
        f"{item['name']}：HP {item['hp']}/{item['max_hp']}，{item['status']}"
        + ("（重伤）" if item.get("major_wound") else "")
        + ("（被突袭）" if item.get("surprised") else "")
        + ("（已闪避）" if item.get("used_dodge") else "")
        + ("（已反击）" if item.get("used_counter") else "")
        for item in state["order"]
    ]
    state["summary"] = f"第 {state['round']} 轮，当前行动者 {state['current']}；" + "；".join(lines)
    return state


def _surprised_set(combat: dict[str, Any]) -> set[str]:
    return {str(name).casefold() for name in combat.get("surprised") or [] if str(name).strip()}


def _is_surprised(combat: dict[str, Any], combatant: dict[str, Any]) -> bool:
    """被突袭者在第 1 轮不能行动，也不能闪避/反击。"""
    if int(combat.get("round") or 1) != 1:
        return False
    names = {str(combatant.get("name") or "").casefold()}
    names.update(str(item).casefold() for item in combatant.get("aliases") or [] if str(item).strip())
    return bool(names & _surprised_set(combat))


def _advance(combat: dict[str, Any]) -> dict[str, Any]:
    order = combat.get("order") or []
    if not order:
        return combat
    turn = int(combat.get("turn") or 0)
    for _ in range(len(order)):
        turn += 1
        if turn >= len(order):
            turn = 0
            combat["round"] = int(combat.get("round") or 1) + 1
            for combatant in (combat.get("combatants") or {}).values():
                combatant["used_dodge"] = False
                combatant["used_counter"] = False
        current = (combat.get("combatants") or {}).get(order[turn]) or {}
        if str(current.get("status") or "active") in SKIPPED_STATUS:
            continue
        if _is_surprised(combat, current):
            # 突袭轮：被突袭者失去本次行动，进入下一位或下一轮。
            continue
        break
    combat["turn"] = turn
    return combat


def next_turn(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    combat = _advance(combat)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    summary = f"进入第 {state['round']} 轮，当前行动者：{state['current']}。"
    return {**state, "summary": summary}


def _opposed_rolls(
    context: Any,
    attacker_value: int,
    defender_value: int,
):
    critical, fumble = _context_thresholds(context)
    attack_roll, attack_candidates = _percentile_roll(_roller)
    defense_roll, defense_candidates = _percentile_roll(_roller)
    attack_level = _success_level(attack_roll, max(1, attacker_value), critical, fumble)
    defense_level = _success_level(defense_roll, max(1, defender_value), critical, fumble)
    return {
        "attack_roll": attack_roll,
        "attack_candidates": attack_candidates,
        "attack_level": attack_level,
        "defense_roll": defense_roll,
        "defense_candidates": defense_candidates,
        "defense_level": defense_level,
    }


def attack_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    attacker = _resolve_combatant(combat, arguments.get("attacker"))
    target = _resolve_combatant(combat, arguments.get("target"))
    if not attacker or not target:
        return {"error": "attacker 或 target 不在当前战斗中"}
    if attacker is target:
        return {"error": "attacker 与 target 不能是同一个人"}
    if attacker.get("dead") or attacker.get("unconscious") or attacker.get("dying"):
        return {"error": f"{attacker.get('name')} 当前无法行动（{attacker.get('status')}）"}
    card = _player_card(attacker, context)
    weapon_name = _as_text(arguments.get("weapon"))
    weapon = _find_weapon(attacker, card, weapon_name) if weapon_name else None
    if weapon_name and weapon is None:
        return {"error": f"{attacker.get('name')} 的角色卡上没有武器“{weapon_name}”"}
    if weapon is None:
        weapon = UNARMED_WEAPON
    rules = weapon_catalog.weapon_rules(weapon)
    range_yards = rules.get("rangeYards")
    melee = range_yards is None or range_yards <= 0
    if arguments.get("mode"):
        melee = _as_text(arguments.get("mode")).lower() != "ranged"

    skill_value, skill_label = _resolve_skill_value(
        attacker,
        card,
        requested=_as_text(arguments.get("skill")),
        skill_key=_as_text(weapon.get("skillKey")),
        specialty_key=_as_text(weapon.get("specialtyKey")),
        names=MELEE_SKILL_NAMES if melee else RANGED_SKILL_NAMES,
        default=25 if melee else 20,
    )
    if not melee:
        return {"error": "远程攻击请使用 combat.shoot，combat.attack 只处理近战与战技"}

    maneuver = _as_text(arguments.get("maneuver")).lower()
    defense = _as_text(arguments.get("defense"), "dodge").lower() or "dodge"
    if defense not in {"dodge", "counter", "none"}:
        return {"error": "defense 只能是 dodge、counter 或 none"}

    surprised_target = _is_surprised(combat, target)
    if surprised_target and defense != "none":
        # 突袭轮不能闪避或反击，视为不防御。
        defense = "none"

    # 寡不敌众：目标本轮已经防御过一次，后续近战攻击获得奖励骰。
    bonus = _dice_count(arguments, "bonus_dice", "bonus")
    penalty = _dice_count(arguments, "penalty_dice", "penalty")
    if defense != "none" and (target.get("used_dodge") or target.get("used_counter")):
        bonus = min(2, bonus + 1)
    if bonus and penalty:
        return {"error": "bonus_dice 与 penalty_dice 不能同时使用"}

    target_card = _player_card(target, context)
    if defense == "counter":
        defense_value, defense_label = _resolve_skill_value(
            target, target_card, names=MELEE_SKILL_NAMES, default=25
        )
    elif defense == "dodge":
        defense_value, defense_label = _resolve_skill_value(
            target, target_card, names=DODGE_SKILL_NAMES, default=max(1, int(target.get("dex") or 0) // 2)
        )
    else:
        defense_value, defense_label = 0, "不防御"

    # 战技：目标体格更大时按级别施加惩罚骰；差距 3 级及以上无效。
    if maneuver:
        difference = int(target.get("build") or 0) - int(attacker.get("build") or 0)
        if difference >= 3:
            return {"error": f"目标体格比攻击者大 {difference} 级，战技无效"}
        if difference >= 1:
            penalty = min(2, penalty + difference)
        if penalty and bonus:
            return {"error": "bonus_dice 与 penalty_dice 不能同时使用"}

    rolled = _opposed_rolls(context, skill_value, defense_value if defense != "none" else 0)
    if defense != "none":
        rolled["attack_roll"], rolled["attack_candidates"] = _percentile_roll(_roller, bonus, penalty)
        critical, fumble = _context_thresholds(context)
        rolled["attack_level"] = _success_level(
            rolled["attack_roll"], max(1, skill_value), critical, fumble
        )
    # 战斗 UI：玩家自己掷出的攻击骰面以 attack_roll 传入，覆盖服务端随机结果。
    if arguments.get("attack_roll") not in (None, ""):
        try:
            player_roll = max(1, min(100, int(arguments.get("attack_roll"))))
        except (TypeError, ValueError):
            return {"error": "attack_roll 必须是 1-100 的整数"}
        critical, fumble = _context_thresholds(context)
        rolled["attack_roll"] = player_roll
        rolled["attack_candidates"] = []
        rolled["attack_level"] = _success_level(player_roll, max(1, skill_value), critical, fumble)
    attack_rank = LEVEL_RANK.get(rolled["attack_level"], 0)
    defense_rank = LEVEL_RANK.get(rolled["defense_level"], 0)

    hit = False
    counter_hit = False
    if defense == "none":
        hit = attack_rank > 0
    elif attack_rank > defense_rank:
        hit = True
    elif attack_rank < defense_rank:
        counter_hit = defense == "counter" and defense_rank > 0
    else:
        if defense == "counter":
            hit = attack_rank > 0
        else:
            hit = False

    target_status = []
    if attack_rank <= 0 and not counter_hit:
        target_status.append("本次攻击未命中")
    if defense != "none" and defense_rank > 0:
        target_status.append(f"目标{('闪避' if defense == 'dodge' else '反击')}成功")
    target.setdefault("used_dodge", False)
    target.setdefault("used_counter", False)
    if defense == "dodge":
        target["used_dodge"] = True
    elif defense == "counter":
        target["used_counter"] = True

    damage_result = None
    if hit:
        extreme = rolled["attack_level"] == "extreme" or rolled["attack_level"] == "critical"
        value, detail = _compute_damage(weapon, attacker.get("db_terms") or [], extreme=extreme, melee=melee)
        damage_result = _apply_damage(
            context,
            target,
            value,
            armor=_as_int(arguments.get("armor"), target.get("armor") or 0)
            if "armor" in arguments
            else target.get("armor") or 0,
            ignore_armor=bool(arguments.get("ignore_armor")),
        )
        damage_result["damage_detail"] = detail
        damage_result["weapon"] = weapon.get("name")
    elif counter_hit:
        counter_weapon = _first_melee_weapon(target, target_card) or UNARMED_WEAPON
        value, detail = _compute_damage(
            counter_weapon, target.get("db_terms") or [], extreme=False, melee=True
        )
        damage_result = _apply_damage(
            context, attacker, value, armor=attacker.get("armor") or 0, ignore_armor=False
        )
        damage_result["damage_detail"] = detail
        damage_result["weapon"] = counter_weapon.get("name")
        damage_result["counter"] = True

    if maneuver and hit:
        effect = maneuver
        conditions = target.setdefault("conditions", [])
        if effect not in conditions:
            conditions.append(effect)
        target["used_dodge"] = True

    # 攻击即消耗本回合：自动推进到下一行动者，避免手动「推进回合」被反复点击。
    _advance(combat)

    parts = [
        f"{attacker.get('name')} 使用{weapon.get('name')}攻击 {target.get('name')}",
        f"攻击 {skill_label} d100={rolled['attack_roll']} / {skill_value}（{rolled['attack_level']}）",
    ]
    if defense != "none":
        parts.append(f"目标 {defense_label} d100={rolled['defense_roll']} / {defense_value}（{rolled['defense_level']}）")
    elif surprised_target:
        parts.append(f"{target.get('name')} 处于突袭轮，无法闪避或反击")
    if rolled["attack_candidates"]:
        parts.append(f"{'奖励骰' if bonus else '惩罚骰'}{rolled['attack_candidates']} 取 {rolled['attack_roll']}")
    if damage_result:
        if counter_hit:
            parts.append(f"{target.get('name')} 反击命中，{attacker.get('name')} 受到伤害")
        parts.append(_format_damage_result(str(target.get('name') if not counter_hit else attacker.get('name')), damage_result))
        parts.append(f"伤害明细：{damage_result.get('damage_detail')}")
    elif attack_rank <= 0 and defense_rank <= 0:
        parts.append("双方均失败，本回合没有伤害")
    elif not hit and not counter_hit:
        parts.append("攻击被防御化解")
    if maneuver and hit:
        parts.append(f"战技成功：{maneuver}")
    summary = "；".join(parts)

    _append_log(combat, summary)
    _save_combat(context, combat)
    # 防御骰由服务端掷出，日志里只有文字；这里补一次骰桌动画，
    # 让玩家能像看自己的骰子一样看到 NPC（或自己）的闪避/反击过程。
    # NPC 主动攻击的骰子已包含在工具消息的 dice_roll 元数据里，不再重复广播。
    if defense != "none":
        _broadcast_dice(
            context,
            _percentile_meta(
                rolled["defense_roll"],
                rolled["defense_level"],
                label=defense_label,
                target=defense_value,
                roller=str(target.get("name") or ""),
            ),
        )
    result = {
        "attacker": attacker.get("name"),
        "target": target.get("name"),
        "weapon": weapon.get("name"),
        "skill": skill_label,
        "skill_value": skill_value,
        "roll": rolled["attack_roll"],
        "rolls": rolled["attack_candidates"] or [rolled["attack_roll"]],
        "success": hit,
        "success_level": rolled["attack_level"],
        "defense": defense,
        "defense_roll": rolled["defense_roll"],
        "defense_level": rolled["defense_level"],
        "damage": damage_result,
        "summary": summary,
    }
    result["visible_message"] = _result_message(
        summary,
        "combat.attack",
        check_type="combat_attack",
        attacker=attacker.get("name"),
        target=target.get("name"),
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=skill_label,
                target=skill_value,
                roller=str(attacker.get("name") or ""),
            )
        },
    )
    _log_roll_result(result, context, tool_name="combat.attack", actor=str(attacker.get("name") or ""))
    return result


def shoot_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    attacker = _resolve_combatant(combat, arguments.get("attacker"))
    target = _resolve_combatant(combat, arguments.get("target"))
    if not attacker or not target:
        return {"error": "attacker 或 target 不在当前战斗中"}
    if attacker.get("dead") or attacker.get("unconscious") or attacker.get("dying"):
        return {"error": f"{attacker.get('name')} 当前无法行动（{attacker.get('status')}）"}
    card = _player_card(attacker, context)
    weapon = _find_weapon(attacker, card, _as_text(arguments.get("weapon")))
    if weapon is None:
        return {"error": "射击需要指定角色卡上已有的远程武器"}
    rules = weapon_catalog.weapon_rules(weapon)
    base_range = rules.get("rangeYards")
    if base_range is None or base_range <= 0:
        return {"error": f"{weapon.get('name')} 不是远程武器"}

    range_text = _as_text(arguments.get("range"))
    difficulty = ""
    distance = None
    if range_text in {"regular", "normal", "hard", "extreme", "普通", "困难", "极难"}:
        difficulty = {"normal": "regular", "普通": "regular"}.get(range_text, range_text)
    elif range_text:
        try:
            distance = float(range_text)
        except ValueError:
            distance = None
    if difficulty:
        pass
    elif distance is not None:
        if distance <= base_range:
            difficulty = "regular"
        elif distance <= base_range * 2:
            difficulty = "hard"
        elif distance <= base_range * 4:
            difficulty = "extreme"
        else:
            return {"error": f"距离 {distance} 码超出 {weapon.get('name')} 的四倍射程"}
    else:
        difficulty = "regular"

    skill_value, skill_label = _resolve_skill_value(
        attacker,
        card,
        requested=_as_text(arguments.get("skill")),
        skill_key=_as_text(weapon.get("skillKey")),
        specialty_key=_as_text(weapon.get("specialtyKey")),
        names=RANGED_SKILL_NAMES,
        default=20,
    )
    bonus = _dice_count(arguments, "bonus_dice", "bonus")
    penalty = _dice_count(arguments, "penalty_dice", "penalty")
    if arguments.get("aim"):
        bonus += 1
    if arguments.get("point_blank"):
        bonus += 1
    if arguments.get("target_moving"):
        penalty += 1
    if arguments.get("cover"):
        penalty += 1
    build_value = int(attacker.get("build") or 0)
    if build_value <= -2:
        penalty += 1
    elif build_value >= 4:
        bonus += 1
    if penalty:
        bonus = 0
    if bonus > 2:
        bonus = 2
    if penalty > 2:
        penalty = 2

    bursts = _as_int(arguments.get("burst"), 1)
    bursts = max(1, min(MAX_BURST, bursts))
    critical, fumble = _context_thresholds(context)
    threshold = skill_value
    if difficulty == "hard":
        threshold = skill_value // 2
    elif difficulty == "extreme":
        threshold = skill_value // 5
    malfunction = rules.get("malfunction")
    ammo_tracker = attacker.get("ammo") if isinstance(attacker.get("ammo"), dict) else {}
    ammo_key = str(weapon.get("name") or "")
    capacity = rules.get("ammoCapacity")
    current_ammo = ammo_tracker.get(ammo_key)
    if current_ammo is None:
        current_ammo = capacity
    shots = []
    total_damage = 0
    malfunctioned = False
    for index in range(bursts):
        if isinstance(current_ammo, int) and current_ammo <= 0:
            shots.append({"shot": index + 1, "error": "弹药耗尽，需要装填"})
            break
        shot_bonus = bonus
        shot_penalty = penalty
        if index > 0:
            if shot_penalty < 2:
                shot_penalty += 1
                shot_bonus = 0
            else:
                if difficulty == "regular":
                    threshold = skill_value // 2
                    difficulty = "hard"
                elif difficulty == "hard":
                    threshold = skill_value // 5
                    difficulty = "extreme"
        roll, candidates = _percentile_roll(_roller, shot_bonus, shot_penalty)
        # 第一发可由玩家自己掷骰（战斗 UI 单发射击）。
        if index == 0 and arguments.get("attack_roll") not in (None, ""):
            try:
                roll = max(1, min(100, int(arguments.get("attack_roll"))))
                candidates = []
            except (TypeError, ValueError):
                return {"error": "attack_roll 必须是 1-100 的整数"}
        if isinstance(current_ammo, int):
            current_ammo -= 1
        level = _success_level(roll, max(1, skill_value), critical, fumble)
        shot: dict[str, Any] = {
            "shot": index + 1,
            "roll": roll,
            "candidates": candidates,
            "level": level,
            "bonus_dice": shot_bonus,
            "penalty_dice": shot_penalty,
        }
        if malfunction and roll >= malfunction:
            shot["malfunction"] = True
            malfunctioned = True
            shots.append(shot)
            break
        success = (roll <= threshold or level == "critical") and level != "fumble"
        shot["hit"] = success
        if success:
            extreme = level in {"extreme", "critical"}
            segment_index = 0 if difficulty == "regular" else 1 if difficulty == "hard" else 2
            value, detail = _compute_damage(
                weapon,
                attacker.get("db_terms") or [],
                extreme=extreme,
                melee=False,
                segment_index=segment_index,
            )
            applied = _apply_damage(
                context,
                target,
                value,
                armor=target.get("armor") or 0,
                ignore_armor=bool(arguments.get("ignore_armor")),
            )
            total_damage += applied["effective"]
            shot["damage"] = applied
            shot["damage_detail"] = detail
        shots.append(shot)
    if isinstance(current_ammo, int):
        attacker["ammo"] = {**ammo_tracker, ammo_key: current_ammo}

    # 一次射击消耗本回合，自动推进到下一位行动者。
    _advance(combat)

    parts = [
        f"{attacker.get('name')} 使用{weapon.get('name')}射击 {target.get('name')}",
        f"距离 {distance if distance is not None else '未指定'} 码，{ { 'regular': '普通', 'hard': '困难', 'extreme': '极难' }.get(difficulty, difficulty) }难度，阈值 {threshold}",
        f"射击技能 {skill_label} {skill_value}",
    ]
    for shot in shots:
        if shot.get("error"):
            parts.append(f"第 {shot['shot']} 发：{shot['error']}")
            continue
        if shot.get("malfunction"):
            parts.append(f"第 {shot['shot']} 发：d100={shot['roll']} ≥ 故障值 {malfunction}，武器故障")
            continue
        text = f"第 {shot['shot']} 发：d100={shot['roll']}（{shot['level']}）"
        if shot["candidates"]:
            text += f" 取 {shot['roll']}"
        if shot.get("hit"):
            applied = shot["damage"]
            text += f" 命中，伤害 {applied['effective']}（{shot['damage_detail']}），HP {applied['hp_before']}→{applied['hp_after']}"
            if applied.get("dead"):
                text += "，死亡"
            elif applied.get("dying"):
                text += "，濒死"
            elif applied.get("unconscious"):
                text += "，昏迷"
        else:
            text += " 未命中"
        parts.append(text)
    if malfunctioned:
        parts.append("武器已故障，需要维修或更换")
    summary = "；".join(parts)
    _append_log(combat, summary)
    _save_combat(context, combat)
    # 射击骰已包含在工具消息的 dice_roll 元数据里，前端照常播放动画，无需重复广播。
    result = {
        "attacker": attacker.get("name"),
        "target": target.get("name"),
        "weapon": weapon.get("name"),
        "difficulty": difficulty,
        "threshold": threshold,
        "skill": skill_label,
        "shots": shots,
        "total_damage": total_damage,
        "malfunction": malfunctioned,
        "summary": summary,
    }
    last_roll = next((shot for shot in shots if "roll" in shot), None)
    if last_roll:
        result.update(
            {
                "roll": last_roll["roll"],
                "rolls": last_roll.get("candidates") or [last_roll["roll"]],
                "success": bool(last_roll.get("hit")),
                "success_level": last_roll["level"],
            }
        )
    result["visible_message"] = _result_message(
        summary,
        "combat.shoot",
        check_type="combat_shoot",
        attacker=attacker.get("name"),
        target=target.get("name"),
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=skill_label,
                difficulty=difficulty,
                target=threshold,
                roller=str(attacker.get("name") or ""),
            )
        }
        if last_roll
        else {},
    )
    _log_roll_result(result, context, tool_name="combat.shoot", actor=str(attacker.get("name") or ""))
    return result


def damage_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    target = _resolve_combatant(combat, arguments.get("target"))
    if not target:
        return {"error": "target 不在当前战斗中"}
    amount = _as_int(arguments.get("amount"), 0)
    expression = _as_text(arguments.get("dice"))
    detail = None
    if expression:
        parsed = weapon_catalog.parse_damage(expression)
        terms = [tuple(term) for term in parsed["segments"][0]["terms"]]
        amount, detail = _damage_roller(terms)
        amount += _as_int(parsed["segments"][0].get("fixed"), 0)
    if amount <= 0:
        return {"error": "amount 或 dice 必须给出正数伤害"}
    applied = _apply_damage(
        context,
        target,
        amount,
        armor=_as_int(arguments.get("armor"), target.get("armor") or 0)
        if "armor" in arguments
        else target.get("armor") or 0,
        ignore_armor=bool(arguments.get("ignore_armor")),
    )
    applied["damage_detail"] = detail
    summary = _format_damage_result(str(target.get("name")), applied)
    _append_log(combat, summary)
    _save_combat(context, combat)
    result = {"target": target.get("name"), **applied, "summary": summary}
    result["visible_message"] = _result_message(
        summary, "combat.damage", check_type="combat_damage", target=target.get("name")
    )
    _log_roll_result(result, context, tool_name="combat.damage", actor=str(target.get("name") or ""))
    return result


def treat_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    target = _resolve_combatant(combat, arguments.get("target"))
    if not target:
        return {"error": "target 不在当前战斗中"}
    if target.get("dead"):
        return {"error": f"{target.get('name')} 已经死亡，无法急救或医学治疗"}
    method = _as_text(arguments.get("method"), "first_aid").lower()
    if method not in {"first_aid", "medicine", "急救", "医学"}:
        return {"error": "method 只能是 first_aid 或 medicine"}
    if method == "急救":
        method = "first_aid"
    if method == "医学":
        method = "medicine"
    within_hour = arguments.get("within_hour") is not False
    if method == "first_aid" and not within_hour:
        return {"error": "急救必须在受伤后 1 小时内进行"}
    if method == "first_aid" and target.get("first_aid_used"):
        return {"error": "该目标已经接受过急救，本次伤势不能再次急救"}
    healer = target
    healer_name = _as_text(arguments.get("healer"))
    if healer_name:
        healer = _resolve_combatant(combat, healer_name)
        if not healer:
            return {"error": f"healer {healer_name} 不在当前战斗中"}
        if healer.get("dead") or healer.get("unconscious") or healer.get("dying"):
            return {"error": f"{healer.get('name')} 当前无法进行急救或医学治疗（{healer.get('status')}）"}
    healer_card = _player_card(healer, context)
    skill_names = ("急救",) if method == "first_aid" else ("医学",)
    skill_value, skill_label = _resolve_skill_value(
        healer,
        healer_card,
        names=skill_names,
        default=30 if method == "first_aid" else 1,
    )
    success = arguments.get("success")
    roll = level = candidates = None
    if isinstance(success, bool):
        pass
    else:
        critical, fumble = _context_thresholds(context)
        difficulty = "regular"
        if method == "medicine" and not within_hour:
            difficulty = "hard"
        threshold = skill_value // 2 if difficulty == "hard" else skill_value
        roll, candidates = _percentile_roll(_roller)
        level = _success_level(roll, max(1, skill_value), critical, fumble)
        success = (roll <= threshold or level == "critical") and level != "fumble"
    healed = 0
    if success:
        if method == "first_aid":
            healed = 1
            target["first_aid_used"] = True
            if target.get("dying"):
                target["dying"] = False
                target["unconscious"] = True
                target["stable"] = True
        else:
            healed, _ = _damage_roller([(1, 3)])
        target["hp"] = min(int(target.get("max_hp") or 1), int(target.get("hp") or 0) + healed)
        _persist_player_state(context, target)
    parts = [
        f"{healer.get('name')} 对 {target.get('name')} 进行{'急救' if method == 'first_aid' else '医学治疗'}（{skill_label} {skill_value}）"
        if healer is not target
        else f"{target.get('name')} 接受{'急救' if method == 'first_aid' else '医学治疗'}（{skill_label} {skill_value}）"
    ]
    if roll is not None:
        parts.append(f"d100={roll} / {skill_value}（{level}）")
        if candidates:
            parts.append(f"取 {roll}")
    parts.append(("成功" if success else "失败") + (f"，恢复 {healed} HP，当前 HP {target.get('hp')}/{target.get('max_hp')}" if success else "，没有恢复 HP"))
    if target.get("stable"):
        parts.append("伤势已稳定，濒死状态解除")
    summary = "；".join(parts)
    _append_log(combat, summary)
    _save_combat(context, combat)
    result = {
        "target": target.get("name"),
        "healer": healer.get("name"),
        "method": method,
        "skill": skill_label,
        "skill_value": skill_value,
        "roll": roll,
        "success": bool(success),
        "success_level": level,
        "healed": healed,
        "hp": target.get("hp"),
        "max_hp": target.get("max_hp"),
        "summary": summary,
    }
    result["visible_message"] = _result_message(
        summary, "combat.treat", check_type="combat_treat", target=target.get("name")
    )
    _log_roll_result(result, context, tool_name="combat.treat", actor=str(target.get("name") or ""))
    return result


def end_combat(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗"}
    combat["active"] = False
    combat["ended_at"] = _timestamp()
    _append_log(combat, "战斗结束")
    _save_combat(context, combat)
    return {"active": False, "round": combat.get("round"), "summary": "战斗结束。"}


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
COMBAT_START_TOOL = AgentTool(
    name="combat.start",
    description=(
        "开始一轮 COC7 战斗：建立参战者、按 DEX 排序（可选先攻检定）、记录回合与 HP。"
        "房间成员自动读取角色卡；NPC/怪物优先用剧本模块卡：传 module_id，"
        "或 participants 中的对象直接给出 dex/hp/skills/weapons/armor。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "剧本中 npc/monster 模块 id；按模块卡数值自动建参"},
            "module_ids": {"type": "array", "items": {"type": "string"}, "description": "多个 NPC/怪物模块 id"},
            "participants": {
                "type": "array",
                "description": "参战者；字符串表示房间玩家，对象表示玩家或 NPC",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "player_name": {"type": "string"},
                                "module_id": {"type": "string"},
                                "is_npc": {"type": "boolean"},
                                "side": {"type": "string"},
                                "dex": {"type": "integer"},
                                "str": {"type": "integer"},
                                "siz": {"type": "integer"},
                                "con": {"type": "integer"},
                                "build": {"type": "integer"},
                                "hp": {"type": "integer"},
                                "max_hp": {"type": "integer"},
                                "armor": {"type": "integer"},
                                "skills": {"type": "object"},
                                "weapons": {"type": "array"},
                            },
                        },
                    ]
                },
            },
            "initiative_check": {"type": "boolean", "description": "是否用敏捷检定代替纯 DEX 排序"},
            "surprised": {"type": "array", "items": {"type": "string"}, "description": "被突袭的参战者"},
        },
    },
    handler=start_combat,
)

COMBAT_STATE_TOOL = AgentTool(
    name="combat.state",
    description="查看当前战斗轮次、行动顺序、当前行动者、HP/重伤/昏迷/濒死与每轮防御状态。",
    parameters={"type": "object", "properties": {}},
    handler=combat_state,
)

COMBAT_NEXT_TOOL = AgentTool(
    name="combat.next",
    description="推进到下一位行动者；到达顺序末尾时轮次 +1，并重置闪避/反击等每轮状态。",
    parameters={"type": "object", "properties": {}},
    handler=next_turn,
)

COMBAT_ATTACK_TOOL = AgentTool(
    name="combat.attack",
    description=(
        "近战攻击或战技：攻击者与目标做格斗对抗检定，目标可选择反击(counter)或闪避(dodge)。"
        "自动处理成功等级比较、极限成功满伤害/贯穿、伤害加值、护甲、重伤/昏迷/濒死。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "attacker": {"type": "string"},
            "target": {"type": "string"},
            "weapon": {"type": "string", "description": "角色卡上的武器名；省略则徒手"},
            "skill": {"type": "string", "description": "覆盖使用的技能名（默认按武器技能）"},
            "defense": {"type": "string", "enum": ["dodge", "counter", "none"]},
            "maneuver": {
                "type": "string",
                "description": "战技类型，如 disarm/trip/pin/escape（缴械/推倒/压制/脱离）",
            },
            "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 2},
            "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 2},
            "ignore_armor": {"type": "boolean"},
        },
        "required": ["attacker", "target"],
    },
    handler=attack_combatant,
)

COMBAT_SHOOT_TOOL = AgentTool(
    name="combat.shoot",
    description=(
        "远程射击：按武器射程自动判定普通/困难/极难，支持瞄准、抵近、掩体、移动目标、体格修正、"
        "连发与全自动、武器故障、贯穿伤害。目标不做闪避对抗。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "attacker": {"type": "string"},
            "target": {"type": "string"},
            "weapon": {"type": "string"},
            "range": {"type": "string", "description": "距离（码）或普通/困难/极难"},
            "aim": {"type": "boolean", "description": "上一轮已瞄准"},
            "point_blank": {"type": "boolean", "description": "抵近射击（DEX/5 英尺内）"},
            "cover": {"type": "boolean", "description": "目标处于掩体/部分遮蔽"},
            "target_moving": {"type": "boolean", "description": "目标 MOV 8+ 高速移动"},
            "burst": {"type": "integer", "minimum": 1, "maximum": 10},
            "skill": {"type": "string"},
            "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 2},
            "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 2},
            "ignore_armor": {"type": "boolean"},
        },
        "required": ["attacker", "target", "weapon"],
    },
    handler=shoot_combatant,
)

COMBAT_DAMAGE_TOOL = AgentTool(
    name="combat.damage",
    description="对战斗中的目标直接结算伤害（坠落、火烧、毒、法术等），自动处理护甲、重伤、昏迷、濒死与死亡。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "amount": {"type": "integer"},
            "dice": {"type": "string", "description": "伤害骰表达式，如 1D6、2D10"},
            "armor": {"type": "integer"},
            "ignore_armor": {"type": "boolean"},
        },
        "required": ["target"],
    },
    handler=damage_combatant,
)

COMBAT_TREAT_TOOL = AgentTool(
    name="combat.treat",
    description=(
        "急救或医学治疗：急救限受伤后 1 小时内且每次伤势只能一次，成功 +1 HP 并稳定濒死；"
        "医学成功 +1D3 HP。未传 success 时自动做对应技能检定。healer 为施救者（默认由目标本人施救）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "healer": {"type": "string", "description": "施救者；省略则用目标本人的技能"},
            "method": {"type": "string", "enum": ["first_aid", "medicine"]},
            "within_hour": {"type": "boolean"},
            "success": {"type": "boolean", "description": "已有检定结果时直接传入"},
        },
        "required": ["target", "method"],
    },
    handler=treat_combatant,
)

COMBAT_END_TOOL = AgentTool(
    name="combat.end",
    description="结束当前战斗轮并保存战斗记录。",
    parameters={"type": "object", "properties": {}},
    handler=end_combat,
)

COMBAT_TOOLS = [
    COMBAT_START_TOOL,
    COMBAT_STATE_TOOL,
    COMBAT_NEXT_TOOL,
    COMBAT_ATTACK_TOOL,
    COMBAT_SHOOT_TOOL,
    COMBAT_DAMAGE_TOOL,
    COMBAT_TREAT_TOOL,
    COMBAT_END_TOOL,
]
COMBAT_TOOL_NAMES = [tool.name for tool in COMBAT_TOOLS]
