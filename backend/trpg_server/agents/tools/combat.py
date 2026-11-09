"""COC7 战斗轮工具。

实现规则书第六章（第 86–110 页）的核心战斗流程：DEX 先攻顺序、近战对抗
（反击/闪避）、极限成功与贯穿、战技、射击难度与故障、护甲、重伤/濒死/死亡
以及急救/医学。骰点、成功等级、展示元数据与日志全部复用 ``dice.py`` 的
既有函数，状态写入沿用房间 ``info.json`` / ``state.json`` 的原子写模式。
"""

from __future__ import annotations

import logging
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
from trpg_server.combat.contracts import DEFAULT_SKIPPED_STATUSES
from trpg_server.combat.registry import (
    build_and_damage_bonus,
    get_ruleset,
    level_rank,
)
from trpg_server.combat.scheduler import TurnScheduler
from trpg_server.combat.settings import CombatSettings, resolve_settings
from trpg_server.json_store import write_json_atomic
from trpg_server.room_log import log_room_event
from trpg_server.ruleset.capabilities import combat_rules_of
from trpg_server.ruleset.registry import get_plugin

logger = logging.getLogger(__name__)

# 失去行动能力的默认状态集合；具体规则集可用 skipped_statuses() 覆盖。
SKIPPED_STATUS = DEFAULT_SKIPPED_STATUSES

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
            "success": level_rank().get(level, 0) > 0,
            "success_level": level,
            "bonus_dice": 0,
            "penalty_dice": 0,
        },
        mode="percentile",
        check_name=label,
        target=max(1, int(target or 1)),
        roller=roller,
    )


def _default_ruleset_id() -> str:
    """默认插件 id；注册表为空时返回空串，由战斗注册表给出不可用错误。"""
    try:
        from trpg_server.ruleset import default_ruleset_id

        return default_ruleset_id()
    except RuntimeError:
        return ""


def _combat_settings(context: Any, info: dict[str, Any] | None = None) -> CombatSettings:
    if info is None:
        info = context.room_info() if hasattr(context, "room_info") else None
    return resolve_settings(info)


def _coc7_room_options(context: Any, info: dict[str, Any] | None = None):
    """读取 CoC7 房规开关；旧房间或读取失败时按全部关闭处理（核心只搬运）。"""
    if info is None:
        info = context.room_info() if hasattr(context, "room_info") else None
    try:
        from trpg_server.ruleset.coc7.options import resolve_options

        return resolve_options(info)
    except Exception:  # pragma: no cover - 仅在规则包损坏时触发
        return None


def _combat_detail_rules(settings: CombatSettings):
    """按当前规则集取战斗细节纯规则；规则集未声明该能力时返回 None。"""
    try:
        return combat_rules_of(get_plugin(settings.ruleset_id))
    except Exception:  # pragma: no cover - 仅在注册表为空时触发
        return None


def _combat_ruleset(combat: dict[str, Any], settings: CombatSettings):
    return get_ruleset(str(combat.get("ruleset") or settings.ruleset_id or ""))


def _is_casting(combat: dict[str, Any], name: str) -> bool:
    round_no = int(combat.get("round") or 1)
    for record in combat.get("casting") or []:
        if not isinstance(record, dict) or record.get("caster") != name:
            continue
        if int(record.get("resolve_round") or 0) >= round_no:
            return True
    return False


def _scheduler(
    combat: dict[str, Any],
    context: Any,
    settings: CombatSettings | None = None,
) -> TurnScheduler:
    settings = settings or _combat_settings(context)
    thresholds = _context_thresholds(context) if context is not None else (1, 96)
    return TurnScheduler(
        _combat_ruleset(combat, settings),
        settings,
        roll_d100=lambda: _percentile_roll(_roller)[0],
        thresholds=thresholds,
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
                "actions_remaining": int(combatant.get("actions_remaining") or 0),
                "actions_per_round": int(combatant.get("actions_per_round") or 1),
                "reactions_this_round": int(combatant.get("reactions_this_round") or 0),
                "ready_weapon": bool(combatant.get("ready_weapon")),
                "disengaged": bool(combatant.get("disengaged")),
                "dying": bool(combatant.get("dying")),
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
        "ruleset": combat.get("ruleset") or _default_ruleset_id(),
    }


def _append_log(
    combat: dict[str, Any],
    text: str,
    context: Any = None,
    *,
    actor: Any = None,
    mirror: bool = True,
) -> None:
    """写战斗面板日志；``mirror`` 时同步一条到房间日志（source=combat）。

    攻击/射击/伤害/治疗已经通过 ``_log_roll_result`` 写入房间日志，调用方传
    ``mirror=False``，避免同一事件在房间日志里出现两次。
    """
    log = combat.get("log")
    if not isinstance(log, list):
        log = []
        combat["log"] = log
    log.append({"time": _timestamp(), "text": text[:500]})
    del log[:-20]
    room_dir = getattr(context, "room_dir", None) if context is not None else None
    if mirror and room_dir:
        # 玩家可见的战斗记录；服务器控制台只在管理员开启镜像时才会收到。
        log_room_event(
            room_dir,
            text[:500],
            source="combat",
            actor=actor,
            room_id=getattr(context, "room_id", None),
            logger_=logger,
        )


# ---------------------------------------------------------------------------
# 角色数值
# ---------------------------------------------------------------------------
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
    power = max(0, _as_int(attrs.get("POW"), _as_int(card.get("pow"), 50)))
    luck = max(0, _as_int(attrs.get("LUCK"), _as_int(card.get("luck"), 50)))
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
        "pow": power,
        "luck": luck,
        "mp": max(0, _as_int(state.get("current_mp", state.get("magic_points", state.get("mp"))), _as_int(card.get("currentMp", card.get("maxMp")), power // 5))),
        "max_mp": max(0, _as_int(state.get("max_mp"), _as_int(card.get("maxMp"), power // 5))),
        # 房间快照里的当前理智字段是 current_san；旧代码读 sanity/san 导致战斗中恒为 0。
        "san": max(0, _as_int(state.get("current_san"), _as_int(card.get("currentSan", card.get("current_san")), 0))),
        "armor": max(0, min(99, _as_int(card.get("armor"), 0))),
        "build": build,
        "damage_bonus": damage_bonus,
        "db_terms": [list(term) for term in db_terms],
        "hp": hp,
        "max_hp": max_hp,
        "status": status,
        "major_wound": bool(state.get("major_wound")),
        "dying": status == "dying",
        "unconscious": status == "unconscious",
        "dead": status == "dead",
        "actions_base": 1,
        "actions_remaining": 1,
        "actions_per_round": 1,
        "reactions_this_round": 0,
        "ready_weapon": bool(state.get("ready_weapon")),
        "fighting": 0,
        "used_dodge": False,
        "used_counter": False,
        "first_aid_used": bool(state.get("first_aid_used")),
        "ammo": {},
        "conditions": [],
    }


def _spec_actions_base(spec: dict[str, Any], weapons: list[dict[str, Any]]) -> int:
    """NPC 每轮行动次数：显式字段优先，其次取武器表里的攻击次数。"""
    explicit = spec.get("actions_per_round", spec.get("attacks"))
    if explicit not in (None, ""):
        match = re.search(r"\d+", str(explicit))
        if match:
            return max(1, int(match.group(0)))
    counts = [
        weapon_catalog.parse_attacks(weapon.get("attacks"))["count"]
        for weapon in weapons
        if isinstance(weapon, dict)
    ]
    return max([1, *counts])


def _combatant_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    name = _as_text(spec.get("name") or spec.get("player_name"))
    if not name:
        raise ValueError("participants 中的 NPC 条目缺少 name")
    dex = max(0, _as_int(spec.get("dex"), 50))
    strength = max(0, _as_int(spec.get("str"), 50))
    size = max(0, _as_int(spec.get("siz"), 50))
    constitution = max(0, _as_int(spec.get("con"), 50))
    power = max(0, _as_int(spec.get("pow"), 50))
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
        "pow": power,
        "luck": max(0, _as_int(spec.get("luck"), 50)),
        "mp": max(0, _as_int(spec.get("mp"), power // 5)),
        "san": max(0, _as_int(spec.get("san"), power)),
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
        "actions_base": _spec_actions_base(spec, weapons),
        "actions_remaining": _spec_actions_base(spec, weapons),
        "actions_per_round": _spec_actions_base(spec, weapons),
        "reactions_this_round": 0,
        "ready_weapon": bool(spec.get("ready_weapon")),
        "fighting": 0,
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
    allow_impale: bool = True,
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

    if extreme and not (impale and allow_impale):
        total = _max_roll(terms) + fixed + _max_roll(db)
        return max(0, total), f"极限成功·满伤害（{parsed.get('raw')}{' +DB' if db else ''}）"
    if extreme and impale and allow_impale:
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
        before = {
            "hp": state.get("current_hp"),
            "mp": state.get("current_mp"),
            "san": state.get("current_san"),
        }
        state["max_hp"] = combatant.get("max_hp")
        state["current_hp"] = max(0, _as_int(combatant.get("hp"), 0))
        state["max_mp"] = max(0, _as_int(combatant.get("max_mp"), _as_int(state.get("max_mp"), 0)))
        state["current_mp"] = max(0, min(state["max_mp"], _as_int(combatant.get("mp"), state["max_mp"])))
        state["current_san"] = max(0, _as_int(combatant.get("san"), _as_int(state.get("current_san"), 0)))
        state["major_wound"] = bool(combatant.get("major_wound"))
        state["dying"] = bool(combatant.get("dying"))
        state["unconscious"] = bool(combatant.get("unconscious"))
        state["dead"] = bool(combatant.get("dead"))
        state["first_aid_used"] = bool(combatant.get("first_aid_used"))
        card = member.get("character_card")
        if isinstance(card, dict):
            card["currentHp"] = state["current_hp"]
            card["current_hp"] = state["current_hp"]
            card["maxHp"] = state["max_hp"]
            card["currentMp"] = state["current_mp"]
            card["maxMp"] = state["max_mp"]
            card["magicPoints"] = state["current_mp"]
            card["currentSan"] = state["current_san"]
            card["current_san"] = state["current_san"]
            status = card.get("status") if isinstance(card.get("status"), dict) else {}
            status["majorWound"] = bool(combatant.get("major_wound"))
            status["unconscious"] = bool(combatant.get("unconscious"))
            status["dead"] = bool(combatant.get("dead"))
            card["status"] = status
        # HP / MP 变化并入统一待写回队列，归档时写回持久卡并清标记。
        pending = []
        for kind, value in (("hp", state["current_hp"]), ("mp", state["current_mp"])):
            if before.get(kind) != value:
                pending.append({"type": kind, "value": value, "source": "combat", "reason": "战斗结算"})
        if before.get("san") != state["current_san"]:
            pending.append({"type": "san", "value": state["current_san"], "source": "combat", "reason": "战斗结算"})
        armor = max(0, min(99, _as_int(combatant.get("armor"), 0)))
        if isinstance(card, dict) and _as_int(card.get("armor"), 0) != armor:
            card["armor"] = armor
            pending.append({"type": "armor", "value": armor, "source": "combat", "reason": "战斗护甲"})
        if pending:
            try:
                from trpg_server import room_rules as _room_rules

                _room_rules.record_room_changes(member, pending)
            except Exception:  # noqa: BLE001 - 登记失败不应中断战斗
                pass
        break
    write_json_atomic(Path(context.room_dir) / "info.json", info)


def _apply_damage(
    context: Any,
    combatant: dict[str, Any],
    amount: int,
    *,
    armor: int | None = None,
    ignore_armor: bool = False,
    settings: CombatSettings | None = None,
) -> dict[str, Any]:
    """结算一次伤害；具体阈值由规则书插件决定。"""
    settings = settings or _combat_settings(context)
    ruleset = get_ruleset(str(combatant.get("ruleset") or settings.ruleset_id or ""))
    outcome = ruleset.resolve_damage(
        combatant,
        amount,
        armor=max(0, int(armor if armor is not None else combatant.get("armor") or 0)),
        ignore_armor=ignore_armor,
        roll_d100=lambda: _percentile_roll(_roller)[0],
        thresholds=_context_thresholds(context),
        settings=settings,
    )
    result: dict[str, Any] = {
        "damage": outcome.damage,
        "armor": outcome.armor,
        "effective": outcome.effective,
        "hp_before": outcome.hp_before,
        "hp_after": outcome.hp_after,
        "major_wound": outcome.major_wound,
        "dying": outcome.dying,
        "unconscious": outcome.unconscious,
        "dead": outcome.dead,
        "note": outcome.note,
        "con_check": outcome.extra.get("con_check"),
    }
    # 瞄准跟踪：受伤即失去瞄准，不需要判断房规是否开启（未开启时字段本就不存在）。
    if outcome.effective > 0 and combatant.get("aiming"):
        combatant["aiming"] = False
        combatant["aim_declared_round"] = 0
        combatant["aim_lost_reason"] = "受伤"
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

    settings = _combat_settings(context)
    ruleset = get_ruleset(settings.ruleset_id)
    for combatant in combatants.values():
        combatant["ruleset"] = ruleset.ruleset_id
        combatant["fighting"] = _resolve_skill_value(
            combatant,
            _player_card(combatant, context),
            names=MELEE_SKILL_NAMES,
            default=25,
        )[0]
        combatant["initiative"] = ruleset.roll_initiative(combatant, _roller, settings)

    if arguments.get("initiative_check"):
        # 可选先攻检定（p105）：大成功首次攻击 1 奖励骰，大失败失去首次攻击。
        critical, fumble = _context_thresholds(context)
        for combatant in combatants.values():
            roll, _ = _percentile_roll(_roller)
            level = _success_level(roll, max(1, int(combatant.get("dex") or 1)), critical, fumble)
            combatant["initiative"] = level_rank().get(level, 0) * 1000 + int(combatant.get("dex") or 0)
            combatant["initiative_roll"] = roll
            if level == "critical":
                combatant["first_attack_bonus"] = 1
            elif level == "fumble":
                combatant["lost_first_action"] = True

    order = sorted(
        combatants.keys(),
        key=lambda name: (
            -int(combatants[name].get("initiative") or 0),
            -int(ruleset.initiative_tiebreak(combatants[name]) or 0),
            -int(combatants[name].get("dex") or 0),
        ),
    )
    combat = {
        "active": True,
        "round": 1,
        "turn": 0,
        "order": order,
        "combatants": combatants,
        "surprised": [str(name) for name in arguments.get("surprised") or [] if str(name).strip()],
        "ruleset": ruleset.ruleset_id,
        "started_at": _timestamp(),
        "log": [],
    }
    _scheduler(combat, context).begin_round(combat)
    _append_log(combat, "战斗开始：" + " → ".join(order), context)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    summary = (
        f"战斗开始，第 1 轮。先攻顺序："
        + " → ".join(f"{item['name']}(DEX {item['dex']})" for item in state["order"])
        + f"。当前行动者：{state['current']}。"
    )
    return {**state, "summary": summary}


def join_combat(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """战斗中追加一名参与者，并按先攻插入顺序；不重置其他人的本轮状态。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    settings = _combat_settings(context)
    ruleset = _combat_ruleset(combat, settings)
    try:
        module_id = _as_text(arguments.get("module_id"))
        raw = arguments.get("participant")
        if module_id:
            module = _scenario_module(context, module_id)
            if not module:
                return {"error": f"scenario module {module_id} was not found"}
            spec = _module_combat_spec(module)
            if not spec["module_id"]:
                spec["module_id"] = module_id
            combatant = _combatant_from_spec(spec)
        elif isinstance(raw, dict) and (_is_npc_entry(raw) or raw.get("dex") or raw.get("hp")):
            combatant = _combatant_from_spec(raw)
        else:
            name = raw if isinstance(raw, str) else _as_text((raw or {}).get("name") if isinstance(raw, dict) else "")
            if not name:
                return {"error": "participant 必须是房间成员名、NPC 定义或给出 module_id"}
            member = _find_member(name, context)
            if not member:
                return {"error": f"player {name} was not found in current room"}
            existing_ids = {
                item.get("user_id")
                for item in (combat.get("combatants") or {}).values()
                if isinstance(item, dict)
            }
            if member.get("user_id") in existing_ids:
                return {"error": f"{name} 已经在战斗中"}
            combatant = _combatant_from_member(member)
    except ValueError as exc:
        return {"error": str(exc)}

    combatants = combat.setdefault("combatants", {})
    key = _unique_name(combatants, combatant["name"])
    combatant["name"] = key
    combatant["ruleset"] = ruleset.ruleset_id
    combatant["fighting"] = _resolve_skill_value(
        combatant,
        _player_card(combatant, context),
        names=MELEE_SKILL_NAMES,
        default=25,
    )[0]
    combatant["initiative"] = ruleset.roll_initiative(combatant, _roller, settings)
    if arguments.get("initiative_check"):
        critical, fumble = _context_thresholds(context)
        roll, _ = _percentile_roll(_roller)
        level = _success_level(
            roll, max(1, int(combatant.get("dex") or 1)), critical, fumble
        )
        combatant["initiative"] = level_rank().get(level, 0) * 1000 + int(
            combatant.get("dex") or 0
        )
        combatant["initiative_roll"] = roll
        if level == "critical":
            combatant["first_attack_bonus"] = 1
        elif level == "fumble":
            combatant["lost_first_action"] = True
    # 只初始化新加入者的本轮行动经济，避免打断正在进行的轮次。
    combatant["actions_per_round"] = ruleset.actions_per_round(combatant, settings)
    combatant["actions_remaining"] = combatant["actions_per_round"]
    combatant["reactions_this_round"] = 0
    combatant["used_dodge"] = False
    combatant["used_counter"] = False
    combatant["disengaged"] = False
    combatant["damage_taken_this_round"] = 0
    combatants[key] = combatant

    order = combat.setdefault("order", [])
    current = order[int(combat.get("turn") or 0)] if order else None
    insert_at = len(order)
    for index, existing in enumerate(order):
        other_initiative = int((combatants.get(existing) or {}).get("initiative") or 0)
        if other_initiative < int(combatant.get("initiative") or 0):
            insert_at = index
            break
    order.insert(insert_at, key)
    if current in order:
        combat["turn"] = order.index(current)
    _append_log(
        combat,
        f"{key} 加入战斗（先攻 {combatant.get('initiative')}，DEX {combatant.get('dex')}）。",
        context,
        actor=key,
    )
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    return {**state, "summary": f"{key} 加入先攻，当前行动者：{state['current']}。"}


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
    """第 1 轮内该角色是否处于被突袭状态。

    突袭是攻击修正，不是“突袭轮”：被突袭者仍然正常行动，只是首轮近战攻击
    对其自动成功或给攻击者奖励骰（p90-91，报告 §2.5）。
    """
    if int(combat.get("round") or 1) != 1:
        return False
    names = {str(combatant.get("name") or "").casefold()}
    names.update(str(item).casefold() for item in combatant.get("aliases") or [] if str(item).strip())
    return bool(names & _surprised_set(combat))


def _advance(
    combat: dict[str, Any],
    context: Any = None,
    *,
    consume_all: bool = False,
    settings: CombatSettings | None = None,
) -> dict[str, Any]:
    if not (combat.get("order") or []):
        return combat
    scheduler = _scheduler(combat, context, settings)
    for text in scheduler.advance(combat, consume_all=consume_all):
        _append_log(combat, text, context)
    return combat


def next_turn(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    combat = _advance(combat, context, consume_all=True)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    summary = f"进入第 {state['round']} 轮，当前行动者：{state['current']}。"
    return {**state, "summary": summary}


def _blocked_by_major_wound(
    context: Any,
    actor: dict[str, Any],
    settings: CombatSettings | None = None,
) -> str | None:
    """房规把重伤设为“无法行动”时，所有显著行动都要被拦住。"""
    settings = settings or _combat_settings(context)
    if settings.major_wound_penalty == "cannot_act" and actor.get("major_wound"):
        return f"{actor.get('name')} 身负重伤，按房规无法行动"
    return None


def delay_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """延迟到本轮末尾行动；若已经是本轮最后一人，则本轮不再行动（p86）。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    order = combat.get("order") or []
    turn = int(combat.get("turn") or 0)
    if not order or not 0 <= turn < len(order):
        return {"error": "当前没有可延迟的行动者"}
    settings = _combat_settings(context)
    actor = (combat.get("combatants") or {}).get(order[turn]) or {}
    blocked = _blocked_by_major_wound(context, actor, settings)
    if blocked:
        return {"error": blocked}
    if turn >= len(order) - 1:
        _append_log(combat, f"{actor.get('name')} 已无更晚的顺位，本轮放弃行动。", context)
        actor["actions_remaining"] = 0
        _advance(combat, context, consume_all=True, settings=settings)
    else:
        name = order.pop(turn)
        order.append(name)
        combat["order"] = order
        combat["turn"] = turn
        _append_log(combat, f"{actor.get('name')} 延迟到本轮末尾。", context)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    return {**state, "summary": f"{actor.get('name')} 延迟行动，当前行动者：{state['current']}。"}


def disengage_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """脱离近战：本轮标记为已脱离，不再被近战缠住（p91）。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    actor_name = _as_text(arguments.get("actor")) or str((combat.get("order") or [""])[int(combat.get("turn") or 0)])
    actor = _resolve_combatant(combat, actor_name)
    if not actor:
        return {"error": f"actor {actor_name} 不在当前战斗中"}
    settings = _combat_settings(context)
    blocked = _blocked_by_major_wound(context, actor, settings)
    if blocked:
        return {"error": blocked}
    if actor.get("disengaged"):
        return {"error": f"{actor.get('name')} 本轮已经脱离近战"}
    actor["disengaged"] = True
    # 瞄准跟踪：移动（脱离近战）会失去瞄准。
    if actor.get("aiming"):
        actor["aiming"] = False
        actor["aim_declared_round"] = 0
        actor["aim_lost_reason"] = "移动"
    _append_log(combat, f"{actor.get('name')} 脱离近战。", context)
    _advance(combat, context, consume_all=True, settings=settings)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    return {**state, "summary": f"{actor.get('name')} 脱离近战，当前行动者：{state['current']}。"}


def reload_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """装填/换弹：把指定武器弹药补满，消耗本轮行动（p97）。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    actor_name = _as_text(arguments.get("actor")) or str((combat.get("order") or [""])[int(combat.get("turn") or 0)])
    actor = _resolve_combatant(combat, actor_name)
    if not actor:
        return {"error": f"actor {actor_name} 不在当前战斗中"}
    settings = _combat_settings(context)
    blocked = _blocked_by_major_wound(context, actor, settings)
    if blocked:
        return {"error": blocked}
    card = _player_card(actor, context)
    weapon_name = _as_text(arguments.get("weapon"))
    weapon = _find_weapon(actor, card, weapon_name) if weapon_name else None
    if weapon is None:
        for row in _weapon_rows(actor, card):
            rules = weapon_catalog.weapon_rules(row)
            if rules.get("ammoCapacity"):
                weapon = row
                break
    if weapon is None:
        return {"error": "没有可装填的远程武器"}
    rules = weapon_catalog.weapon_rules(weapon)
    capacity = rules.get("ammoCapacity")
    if not capacity:
        return {"error": f"{weapon.get('name')} 没有弹匣容量"}
    key = str(weapon.get("name") or "")
    ammo = actor.get("ammo") if isinstance(actor.get("ammo"), dict) else {}
    actor["ammo"] = {**ammo, key: int(capacity)}
    _append_log(combat, f"{actor.get('name')} 为{key}装填（{capacity} 发）。", context)
    _advance(combat, context, consume_all=True, settings=settings)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    return {**state, "summary": f"{key} 已装填，当前行动者：{state['current']}。"}


def ready_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """准备武器：按房规决定是否消耗行动；准备后射击先攻按 DEX+50（p96）。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    actor_name = _as_text(arguments.get("actor")) or str((combat.get("order") or [""])[int(combat.get("turn") or 0)])
    actor = _resolve_combatant(combat, actor_name)
    if not actor:
        return {"error": f"actor {actor_name} 不在当前战斗中"}
    settings = _combat_settings(context)
    blocked = _blocked_by_major_wound(context, actor, settings)
    if blocked:
        return {"error": blocked}
    actor["ready_weapon"] = True
    cost = settings.ready_weapon_cost
    if cost in {"action", "round"}:
        _advance(combat, context, consume_all=True, settings=settings)
    _append_log(combat, f"{actor.get('name')} 准备武器（耗时：{cost}）。", context)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    return {**state, "summary": f"{actor.get('name')} 已准备武器，当前行动者：{state['current']}。"}


def cast_spell(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """战斗内施法：结算时序、MP/SAN 消耗与打断状态，具体效果交给 KP/内容层。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    caster_name = _as_text(arguments.get("caster")) or str((combat.get("order") or [""])[int(combat.get("turn") or 0)])
    caster = _resolve_combatant(combat, caster_name)
    if not caster:
        return {"error": f"caster {caster_name} 不在当前战斗中"}
    settings = _combat_settings(context)
    blocked = _blocked_by_major_wound(context, caster, settings)
    if blocked:
        return {"error": blocked}
    spell = _as_text(arguments.get("spell")) or "未命名法术"
    cast_time = _as_text(arguments.get("cast_time"), "instant").lower()
    if cast_time not in {"instant", "one_round", "two_rounds"}:
        return {"error": "cast_time 只能是 instant、one_round 或 two_rounds"}
    mp_cost = max(0, _as_int(arguments.get("mp_cost"), 0))
    san_cost = max(0, _as_int(arguments.get("san_cost"), 0))
    ruleset = _combat_ruleset(combat, settings)
    damage_before = int(caster.get("damage_taken_this_round") or 0)

    # 先付费：即使被打断，MP 与 SAN 也不退（p206）。资源扣除交给插件，
    # 这样换规则书时法术位/职业资源可以按各自的语义处理。
    mp_result = ruleset.spend_resource(caster, "mp", mp_cost)
    ruleset.spend_resource(caster, "san", san_cost)
    # 法术消耗（MP/SAN）同样要进入房间快照与统一写回链，不能只改战斗内对象。
    _persist_player_state(context, caster)
    overflow = int(mp_result.get("overflow") or 0)
    overflow_result = None
    overflow_applied = 0
    if overflow:
        overflow_result = _apply_damage(
            context, caster, overflow, ignore_armor=True, settings=settings
        )
        # 自己的 MP 溢出代价不算“被外力打断”，从打断判定里剔除。
        overflow_applied = int(overflow_result.get("effective") or 0)
    damage_after = int(caster.get("damage_taken_this_round") or 0)
    external_damage = damage_after - damage_before - overflow_applied
    timing = ruleset.spell_timing(cast_time, int(caster.get("dex") or 0))
    resolve_round = int(combat.get("round") or 1) + int(timing.get("round_offset") or 0)
    interrupted = False
    if settings.cast_interrupt == "any_damage" and (damage_before > 0 or external_damage > 0):
        interrupted = True
    elif settings.cast_interrupt == "never":
        interrupted = False
    needs_keeper_decision = settings.cast_interrupt == "keeper"
    record = {
        "caster": caster.get("name"),
        "spell": spell,
        "cast_time": cast_time,
        "resolve_round": resolve_round,
        "resolve_dex": timing.get("dex"),
        "mp_cost": mp_cost,
        "san_cost": san_cost,
        "interrupted": interrupted,
    }
    combat.setdefault("casting", []).append(record)
    _append_log(
        combat,
        f"{caster.get('name')} 施放{spell}（{cast_time}，MP {mp_cost}，SAN {san_cost}），"
        f"预计第 {resolve_round} 轮 DEX {timing.get('dex')} 生效。",
        context,
        actor=caster.get("name"),
    )
    _advance(combat, context, consume_all=True, settings=settings)
    _save_combat(context, combat)
    state = _combat_summary_state(combat)
    parts = [f"{caster.get('name')} 施放{spell}"]
    if interrupted:
        parts.append("施法被打断，但 MP/SAN 已消耗")
    elif needs_keeper_decision:
        parts.append("是否被打断由 KP 裁定")
    if overflow_result:
        parts.append(f"MP 不足，溢出 {overflow} 点直接扣除 HP")
    parts.append(f"当前行动者：{state['current']}")
    return {
        **state,
        "spell": record,
        "interrupted": interrupted,
        "needs_keeper_decision": needs_keeper_decision,
        "mp_overflow_damage": overflow_result,
        "summary": "；".join(parts) + "。",
    }


def _opposed_rolls(
    context: Any,
    attacker_value: int,
    defender_value: int,
    *,
    bonus: int = 0,
    penalty: int = 0,
):
    critical, fumble = _context_thresholds(context)
    attack_roll, attack_candidates = _percentile_roll(_roller, bonus, penalty)
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
    attacker = _resolve_combatant(combat, arguments.get("attacker") or arguments.get("actor"))
    target = _resolve_combatant(combat, arguments.get("target"))
    if not attacker or not target:
        return {"error": "attacker 或 target 不在当前战斗中"}
    if attacker is target:
        return {"error": "attacker 与 target 不能是同一个人"}
    if attacker.get("dead") or attacker.get("unconscious") or attacker.get("dying"):
        return {"error": f"{attacker.get('name')} 当前无法行动（{attacker.get('status')}）"}
    settings = _combat_settings(context)
    if settings.major_wound_penalty == "cannot_act" and attacker.get("major_wound"):
        return {"error": f"{attacker.get('name')} 身负重伤，按房规无法行动"}
    room_options = _coc7_room_options(context)
    detail_rules = _combat_detail_rules(settings)
    knockout_rule = bool(room_options and room_options.combat_knockout and detail_rules)
    hit_locations = bool(room_options and room_options.combat_hit_locations and detail_rules)
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
    # NPC 默认反击，除非正在撤退；玩家默认闪避（p87）。
    default_defense = "dodge" if target.get("is_player") else "counter"
    defense = _as_text(arguments.get("defense"), default_defense).lower() or default_defense
    if defense == "block":
        # 核心没有独立格挡；房规开启时把盾牌描述映射为反击（挡开+还击）。
        if settings.shield_action != "counter_alias":
            return {"error": "核心规则没有独立格挡动作，请使用 dodge 或 counter（反击含招架）"}
        defense = "counter"
    if defense not in {"dodge", "counter", "none"}:
        return {"error": "defense 只能是 dodge、counter 或 none"}

    # 施法当轮能否闪避/反击，规则书未规定，由房规决定（报告 §9）。
    if defense != "none" and _is_casting(combat, str(target.get("name") or "")) and not settings.cast_defense_allowed:
        defense = "none"

    ruleset = _combat_ruleset(combat, settings)

    # 先攻检定大失败：失去本轮第一次攻击（p105，可选规则）。
    if attacker.get("lost_first_action"):
        attacker["lost_first_action"] = False
        _append_log(combat, f"{attacker.get('name')} 因先攻检定大失败，失去本轮第一次攻击。", context)
        _advance(combat, context)
        _save_combat(context, combat)
        state = _combat_summary_state(combat)
        return {**state, "summary": f"{attacker.get('name')} 失去第一次攻击，当前行动者：{state['current']}。"}

    bonus = _dice_count(arguments, "bonus_dice", "bonus")
    penalty = _dice_count(arguments, "penalty_dice", "penalty")

    # 突袭是攻击修正，不是“突袭轮”（p90-91，报告 §2.5）。
    forced_surprise_hit = False
    surprised_target = _is_surprised(combat, target)
    if surprised_target:
        if settings.surprise_model == "auto_success":
            defense = "none"
            forced_surprise_hit = True
        else:
            bonus = min(2, bonus + 1)

    # 可选先攻检定大成功：首次攻击 1 奖励骰（p105）。
    if attacker.get("first_attack_bonus"):
        attacker["first_attack_bonus"] = 0
        bonus = min(2, bonus + 1)

    # 寡不敌众：目标本轮已用掉可用的反应次数后，后续近战攻击获得奖励骰（p91）。
    reaction_limit = ruleset.reaction_limit(target, settings)
    if defense != "none" and int(target.get("reactions_this_round") or 0) >= reaction_limit:
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

    rolled = _opposed_rolls(
        context,
        skill_value,
        defense_value if defense != "none" else 0,
        bonus=bonus,
        penalty=penalty,
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
    attack_rank = level_rank().get(rolled["attack_level"], 0)
    defense_rank = level_rank().get(rolled["defense_level"], 0)

    if forced_surprise_hit:
        # 规则书选项：未准备目标近战自动成功，除非攻击者大失败（p90-91）。
        hit = rolled["attack_level"] != "fumble"
        counter_hit = False
    else:
        outcome = ruleset.resolve_melee(
            rolled["attack_level"],
            rolled["defense_level"],
            defense,
        )
        hit = outcome == "hit"
        counter_hit = outcome == "counter_hit"

    target_status = []
    if not hit and not counter_hit:
        target_status.append("本次攻击未命中")
    if defense != "none" and defense_rank > 0:
        target_status.append(f"目标{('闪避' if defense == 'dodge' else '反击')}成功")
    target.setdefault("used_dodge", False)
    target.setdefault("used_counter", False)
    if defense != "none":
        target["reactions_this_round"] = int(target.get("reactions_this_round") or 0) + 1
    if defense == "dodge":
        target["used_dodge"] = True
    elif defense == "counter":
        target["used_counter"] = True

    damage_result = None
    if hit:
        extreme = rolled["attack_level"] == "extreme" or rolled["attack_level"] == "critical"
        knockout_applied = None
        if (
            knockout_rule
            and maneuver in {"knockout", "knock-out", "stun", "击晕"}
            and detail_rules.is_blunt_weapon(weapon)
        ):
            # 击晕房规：钝器战技成功只造成 1 点伤害，并让目标昏迷 1D10 轮（p.106）。
            value, detail = detail_rules.KNOCKOUT_DAMAGE, "击晕攻击（钝器战技）"
            knockout_applied = detail_rules.knockout_result(_roller(10))
        else:
            value, detail = _compute_damage(weapon, attacker.get("db_terms") or [], extreme=extreme, melee=melee)
        damage_result = _apply_damage(
            context,
            target,
            value,
            armor=_as_int(arguments.get("armor"), target.get("armor") or 0)
            if "armor" in arguments
            else target.get("armor") or 0,
            ignore_armor=bool(arguments.get("ignore_armor")),
            settings=settings,
        )
        damage_result["damage_detail"] = detail
        damage_result["weapon"] = weapon.get("name")
        if knockout_applied:
            target["unconscious"] = True
            target["status"] = "unconscious"
            target["knockout_rounds"] = knockout_applied["rounds"]
            damage_result["knockout"] = knockout_applied
        if hit_locations:
            location = detail_rules.hit_location(_roller(20))
            damage_result["hit_location"] = location
            if damage_result.get("major_wound"):
                effect = detail_rules.major_wound_effect(location.get("location"))
                if effect:
                    damage_result["major_wound_effect"] = effect
    elif counter_hit:
        counter_weapon = _first_melee_weapon(target, target_card) or UNARMED_WEAPON
        value, detail = _compute_damage(
            counter_weapon, target.get("db_terms") or [], extreme=False, melee=True
        )
        damage_result = _apply_damage(
            context,
            attacker,
            value,
            armor=attacker.get("armor") or 0,
            ignore_armor=False,
            settings=settings,
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

    # 攻击即消耗一次显著行动：还有剩余行动则留在当前行动者，否则推进。
    _advance(combat, context)

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
        if damage_result.get("knockout"):
            parts.append(f"{target.get('name')} 被击晕，昏迷 {damage_result['knockout']['rounds']} 轮")
        location = damage_result.get("hit_location")
        if location:
            parts.append(f"命中部位：{location.get('location')}")
            if damage_result.get("major_wound_effect"):
                parts.append(damage_result["major_wound_effect"])
    elif attack_rank <= 0 and defense_rank <= 0:
        parts.append("双方均失败，本回合没有伤害")
    elif not hit and not counter_hit:
        parts.append("攻击被防御化解")
    if maneuver and hit:
        parts.append(f"战技成功：{maneuver}")
    summary = "；".join(parts)

    _append_log(combat, summary, mirror=False)
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
    attacker = _resolve_combatant(combat, arguments.get("attacker") or arguments.get("actor"))
    target = _resolve_combatant(combat, arguments.get("target"))
    if not attacker or not target:
        return {"error": "attacker 或 target 不在当前战斗中"}
    if attacker.get("dead") or attacker.get("unconscious") or attacker.get("dying"):
        return {"error": f"{attacker.get('name')} 当前无法行动（{attacker.get('status')}）"}
    settings = _combat_settings(context)
    if settings.major_wound_penalty == "cannot_act" and attacker.get("major_wound"):
        return {"error": f"{attacker.get('name')} 身负重伤，按房规无法行动"}
    room_options = _coc7_room_options(context)
    detail_rules = _combat_detail_rules(settings)
    # 掩体闪避的后续代价：失去下一次攻击（由该房规在上一次射击时写入）。
    if attacker.get("lost_next_attack"):
        attacker["lost_next_attack"] = False
        _append_log(combat, f"{attacker.get('name')} 因上次攻击被掩体闪避，失去本次攻击。", context)
        _advance(combat, context)
        _save_combat(context, combat)
        state = _combat_summary_state(combat)
        return {**state, "summary": f"{attacker.get('name')} 失去下一次攻击，当前行动者：{state['current']}。"}
    # 瞄准跟踪：本轮声明瞄准，消耗一次显著行动，下一轮射击才享受奖励骰。
    if room_options and room_options.combat_aim_tracking and detail_rules and arguments.get("aim_action"):
        allowed, reason = detail_rules.can_declare_aim(attacker)
        if not allowed:
            return {"error": reason}
        attacker.update(detail_rules.declare_aim(combat.get("round")))
        _append_log(combat, f"{attacker.get('name')} 声明瞄准，下一次射击获得奖励骰。", context)
        _advance(combat, context)
        _save_combat(context, combat)
        state = _combat_summary_state(combat)
        return {
            **state,
            "aiming": True,
            "summary": f"{attacker.get('name')} 正在瞄准，下一次射击获得奖励骰；当前行动者：{state['current']}。",
        }
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
    beyond_range = False
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
            # 超出四倍射程：只有大成功（01）的贯穿能命中（p95）。
            beyond_range = True
            difficulty = "extreme"
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
    aim_tracking = bool(room_options and room_options.combat_aim_tracking and detail_rules)
    cover_dodge = bool(room_options and room_options.combat_cover_dodge and detail_rules)
    friendly_fire = bool(room_options and room_options.combat_friendly_fire and detail_rules)
    hit_locations = bool(room_options and room_options.combat_hit_locations and detail_rules)
    if aim_tracking:
        # 开启瞄准跟踪后，只有上一轮声明过瞄准才享受奖励骰。
        bonus += detail_rules.aim_bonus(attacker.get("aiming"))
    elif arguments.get("aim"):
        bonus += 1
    if arguments.get("point_blank"):
        bonus += 1
    if arguments.get("target_moving"):
        penalty += 1
    if arguments.get("cover"):
        penalty += detail_rules.cover_penalty(True) if detail_rules else 1
    in_melee = bool(arguments.get("in_melee"))
    if friendly_fire and in_melee:
        penalty += detail_rules.friendly_fire_penalty(True)
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

    requested = max(1, min(MAX_BURST, _as_int(arguments.get("burst"), 1)))
    allowance = int((rules.get("attacks") or {}).get("burst") or 1)
    if requested > 1 and allowance <= 1:
        return {"error": f"{weapon.get('name')} 不支持连发或全自动"}
    if 1 < requested <= 3 and requested > allowance:
        return {"error": f"{weapon.get('name')} 每轮最多连发 {allowance} 发"}

    critical, fumble = _context_thresholds(context)
    cover_dodge_note = ""
    if cover_dodge and arguments.get("cover"):
        target_card = _player_card(target, context)
        dodge_value, dodge_label = _resolve_skill_value(
            target,
            target_card,
            names=DODGE_SKILL_NAMES,
            default=max(1, int(target.get("dex") or 0) // 2),
        )
        dodge_roll, _ = _percentile_roll(_roller)
        dodge_level = _success_level(dodge_roll, max(1, dodge_value), critical, fumble)
        if detail_rules.attacker_loses_next_attack(dodge_level):
            attacker["lost_next_attack"] = True
            cover_dodge_note = (
                f"{target.get('name')} 借掩体闪避成功（{dodge_label} d100={dodge_roll} / {dodge_value}，{dodge_level}），"
                f"{attacker.get('name')} 失去下一次攻击"
            )
        else:
            cover_dodge_note = (
                f"{target.get('name')} 借掩体闪避失败（{dodge_label} d100={dodge_roll} / {dodge_value}，{dodge_level}）"
            )
    malfunction = rules.get("malfunction")
    ammo_tracker = attacker.get("ammo") if isinstance(attacker.get("ammo"), dict) else {}
    ammo_key = str(weapon.get("name") or "")
    capacity = rules.get("ammoCapacity")
    current_ammo = ammo_tracker.get(ammo_key)
    if current_ammo is None:
        current_ammo = capacity
    shots: list[dict[str, Any]] = []
    total_damage = 0
    malfunctioned = False
    difficulty_order = ("regular", "hard", "extreme")
    base_rank = difficulty_order.index(difficulty) if difficulty in difficulty_order else 0

    def _threshold_for(rank: int) -> int:
        if rank >= 2:
            return skill_value // 5
        if rank == 1:
            return skill_value // 2
        return skill_value

    threshold = 1 if beyond_range else _threshold_for(base_rank)

    def _resolve_shot(
        shot_no: int,
        bullets: int,
        shot_bonus: int,
        shot_penalty: int,
        diff_rank: int,
        *,
        group_no: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        nonlocal current_ammo, malfunctioned
        usable = bullets
        if isinstance(current_ammo, int):
            usable = min(usable, max(0, current_ammo))
        if usable <= 0:
            return {"shot": shot_no, "error": "弹药耗尽，需要装填"}, []
        roll, candidates = _percentile_roll(_roller, shot_bonus, shot_penalty)
        # 第一发可由玩家自己掷骰（战斗 UI 单发射击）。
        if shot_no == 1 and arguments.get("attack_roll") not in (None, ""):
            try:
                roll = max(1, min(100, int(arguments.get("attack_roll"))))
            except (TypeError, ValueError) as exc:
                raise ValueError("attack_roll 必须是 1-100 的整数") from exc
            candidates = []
        if isinstance(current_ammo, int):
            current_ammo -= usable
        level = _success_level(roll, max(1, skill_value), critical, fumble)
        record: dict[str, Any] = {
            "shot": shot_no,
            "bullets": usable,
            "roll": roll,
            "candidates": candidates,
            "level": level,
            "bonus_dice": shot_bonus,
            "penalty_dice": shot_penalty,
            "difficulty": difficulty_order[diff_rank],
        }
        if group_no is not None:
            record["group"] = group_no
        if malfunction and roll >= malfunction:
            record["malfunction"] = True
            malfunctioned = True
            return record, []
        # 射击入近战大失败：误伤同方幸运最低的同伴（房规）。
        if level == "fumble" and friendly_fire and in_melee:
            victim_name = detail_rules.friendly_fire_victim(
                combat.get("combatants") or {},
                attacker=str(attacker.get("name") or ""),
                target=str(target.get("name") or ""),
                side=attacker.get("side"),
            )
            victim = _resolve_combatant(combat, victim_name) if victim_name else None
            if victim:
                value, detail = _compute_damage(
                    weapon,
                    attacker.get("db_terms") or [],
                    extreme=False,
                    melee=False,
                    segment_index=diff_rank,
                )
                applied = _apply_damage(
                    context,
                    victim,
                    value,
                    armor=victim.get("armor") or 0,
                    settings=settings,
                )
                record["friendly_fire"] = {"victim": victim.get("name"), **applied}
                record["friendly_fire_detail"] = detail
        success = (roll <= _threshold_for(diff_rank) or level == "critical") and level != "fumble"
        if beyond_range:
            # 超四倍射程只有 01 能贯穿（p95）。
            success = level == "critical"
        record["hit"] = success
        if not success:
            return record, []
        extreme = level in {"extreme", "critical"}
        if group_no is None:
            hits = 1
            impale_count = 0
        else:
            # 全自动：常规成功一半命中，极难成功全中且前半贯穿（p97-98）。
            hits = usable if extreme else max(1, usable // 2)
            impale_count = usable // 2 if extreme and diff_rank < 2 else 0
        record["hits"] = hits
        applied_list: list[dict[str, Any]] = []
        detail = ""
        for bullet_index in range(hits):
            value, detail = _compute_damage(
                weapon,
                attacker.get("db_terms") or [],
                extreme=extreme,
                melee=False,
                segment_index=diff_rank,
                allow_impale=(group_no is None) or bullet_index < impale_count,
            )
            applied = _apply_damage(
                context,
                target,
                value,
                armor=target.get("armor") or 0,
                ignore_armor=bool(arguments.get("ignore_armor")),
                settings=settings,
            )
            applied_list.append(applied)
        if hit_locations and applied_list:
            location = detail_rules.hit_location(_roller(20))
            record["hit_location"] = location
            if any(item.get("major_wound") for item in applied_list):
                effect = detail_rules.major_wound_effect(location.get("location"))
                if effect:
                    record["major_wound_effect"] = effect
        if applied_list:
            record["damage"] = applied_list[-1]
            record["damage_detail"] = detail
            record["effective_total"] = sum(item.get("effective", 0) for item in applied_list)
            record["hp_before"] = applied_list[0]["hp_before"]
            record["hp_after"] = applied_list[-1]["hp_after"]
            record["dead"] = any(item.get("dead") for item in applied_list)
            record["dying"] = any(item.get("dying") for item in applied_list)
            record["unconscious"] = any(item.get("unconscious") for item in applied_list)
        return record, applied_list

    try:
        if requested <= 3:
            for index in range(requested):
                shot_bonus = bonus
                shot_penalty = penalty
                if index > 0:
                    if shot_penalty < 2:
                        shot_penalty += 1
                        shot_bonus = 0
                shot, applied = _resolve_shot(
                    index + 1, 1, shot_bonus, shot_penalty, base_rank
                )
                total_damage += sum(item.get("effective", 0) for item in applied)
                shots.append(shot)
                if shot.get("error") or shot.get("malfunction"):
                    break
        else:
            group_size = max(3, skill_value // 10)
            remaining = requested
            group_index = 0
            while remaining > 0:
                bullets = min(group_size, remaining)
                penalty_rank = max(0, group_index - max(0, 2 - penalty))
                shot_penalty = min(2, penalty + group_index)
                shot_bonus = 0 if shot_penalty > 0 else bonus
                shot, applied = _resolve_shot(
                    len(shots) + 1,
                    bullets,
                    shot_bonus,
                    shot_penalty,
                    min(2, base_rank + penalty_rank),
                    group_no=group_index + 1,
                )
                total_damage += sum(item.get("effective", 0) for item in applied)
                shots.append(shot)
                if shot.get("error") or shot.get("malfunction"):
                    break
                remaining -= bullets
                group_index += 1
    except ValueError as exc:
        return {"error": str(exc)}

    if isinstance(current_ammo, int):
        attacker["ammo"] = {**ammo_tracker, ammo_key: current_ammo}

    # 瞄准只服务一次射击：无论命中与否，射击后清除瞄准状态。
    if aim_tracking and attacker.get("aiming"):
        attacker.update(detail_rules.aim_consumed_patch())

    # 一次射击消耗一次显著行动；全自动分组在射击函数内部结算。
    _advance(combat, context)

    parts = [
        f"{attacker.get('name')} 使用{weapon.get('name')}射击 {target.get('name')}",
        f"距离 {distance if distance is not None else '未指定'} 码，{ { 'regular': '普通', 'hard': '困难', 'extreme': '极难' }.get(difficulty, difficulty) }难度，阈值 {threshold}",
        f"射击技能 {skill_label} {skill_value}",
    ]
    if cover_dodge_note:
        parts.append(cover_dodge_note)
    for shot in shots:
        label = f"第 {shot.get('group')} 组（{shot.get('bullets', 1)} 发）" if shot.get("group") else f"第 {shot['shot']} 发"
        if shot.get("error"):
            parts.append(f"{label}：{shot['error']}")
            continue
        if shot.get("malfunction"):
            parts.append(f"{label}：d100={shot['roll']} ≥ 故障值 {malfunction}，武器故障")
            continue
        text = f"{label}：d100={shot['roll']}（{shot['level']}）"
        if shot["candidates"]:
            text += f" 取 {shot['roll']}"
        if shot.get("hit"):
            effective_total = shot.get("effective_total")
            if effective_total is None:
                effective_total = shot["damage"]["effective"]
            text += (
                f" 命中 {shot.get('hits', 1)} 发，伤害 {effective_total}"
                f"（{shot['damage_detail']}），HP {shot['hp_before']}→{shot['hp_after']}"
            )
            if shot.get("dead"):
                text += "，死亡"
            elif shot.get("dying"):
                text += "，濒死"
            elif shot.get("unconscious"):
                text += "，昏迷"
            location = shot.get("hit_location")
            if location:
                text += f"，命中{location.get('location')}"
                if shot.get("major_wound_effect"):
                    text += f"（{shot['major_wound_effect']}）"
        else:
            text += " 未命中"
        if shot.get("friendly_fire"):
            friendly = shot["friendly_fire"]
            text += (
                f"；误伤 {friendly.get('victim')}，伤害 {friendly.get('effective')}"
                f"（{shot.get('friendly_fire_detail')}），HP {friendly.get('hp_before')}→{friendly.get('hp_after')}"
            )
        parts.append(text)
    if malfunctioned:
        parts.append("武器已故障，需要维修或更换")
    summary = "；".join(parts)
    _append_log(combat, summary, mirror=False)
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
    _append_log(combat, summary, mirror=False)
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
    same_day = arguments.get("same_day") is not False
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
        if method == "medicine" and not same_day:
            # 医学治疗非当天的伤势才是困难（p101）。
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
    _append_log(combat, summary, mirror=False)
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


def heal_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """直接恢复耐久（KP 手动结算：奖励、药品、剧情效果）。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    target = _resolve_combatant(combat, arguments.get("target") or arguments.get("name"))
    if not target:
        return {"error": f"target {arguments.get('target')} 不在当前战斗中"}
    if target.get("dead"):
        return {"error": f"{target.get('name')} 已死亡，不能恢复耐久"}
    amount = max(0, _as_int(arguments.get("amount"), 0))
    if amount <= 0:
        return {"error": "amount 必须是正整数"}
    before = int(target.get("hp") or 0)
    max_hp = max(1, int(target.get("max_hp") or 1))
    after = min(max_hp, before + amount)
    target["hp"] = after
    if after > 0 and target.get("dying"):
        target["dying"] = False
        target["stable"] = True
    if after > 0 and target.get("status") == "dying":
        target["status"] = "active"
    _persist_player_state(context, target)
    summary = f"{target.get('name')} 恢复 {after - before} 点耐久（{before} → {after}/{max_hp}）。"
    _append_log(combat, summary, context)
    _save_combat(context, combat)
    return {
        **_combat_summary_state(combat),
        "target": target.get("name"),
        "healed": after - before,
        "hp": after,
        "max_hp": max_hp,
        "summary": summary,
    }


def poison_combatant(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """毒剂结算：体质抵抗成功伤害减半；发作轮数大于 0 时只登记，不自动调度。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    room_options = _coc7_room_options(context)
    settings = _combat_settings(context)
    detail_rules = _combat_detail_rules(settings)
    if not (room_options and room_options.combat_poison and detail_rules):
        return {"error": "毒剂房规未开启，请先在房规中开启「毒剂」"}
    target = _resolve_combatant(combat, arguments.get("target"))
    if not target:
        return {"error": f"target {arguments.get('target')} 不在当前战斗中"}
    if target.get("dead"):
        return {"error": f"{target.get('name')} 已死亡，无法结算毒剂"}

    onset = max(0, _as_int(arguments.get("onset_rounds"), 0))
    if onset > 0:
        summary = (
            f"{target.get('name')} 中毒：{onset} 轮后发作。"
            "当前不做延迟调度，请在到期后再次调用 combat.poison 并令 onset_rounds=0 结算。"
        )
        _append_log(combat, summary, context)
        _save_combat(context, combat)
        return {
            **_combat_summary_state(combat),
            "target": target.get("name"),
            "pending": True,
            "onset_rounds": onset,
            "summary": summary,
        }

    potency = detail_rules.poison_penalty_dice(arguments.get("potency"))
    doses = max(1, _as_int(arguments.get("doses"), 1))
    expression = _as_text(arguments.get("damage"), "1D6") or "1D6"
    con = detail_rules.poison_check_target(target.get("con"))
    critical, fumble = _context_thresholds(context)
    roll, candidates = _percentile_roll(_roller, 0, potency)
    level = _success_level(roll, con, critical, fumble)
    resisted = detail_rules.poison_resisted(level)

    parsed = weapon_catalog.parse_damage(expression)
    terms = [tuple(term) for term in parsed["segments"][0]["terms"]]
    amount, detail = _damage_roller(terms)
    amount += _as_int(parsed["segments"][0].get("fixed"), 0)
    total = max(0, amount) * doses
    applied_amount = detail_rules.poison_damage(total, resisted=resisted)
    applied = _apply_damage(context, target, applied_amount, settings=settings)

    verdict = f"抵抗成功，伤害减半 {total}→{applied_amount}" if resisted else f"抵抗失败，承受 {applied_amount} 点伤害"
    summary = (
        f"{target.get('name')} 中毒（毒性 {potency}，CON {con}，d100={roll}，{level}）："
        f"{verdict}，耐久 {applied.get('hp_before')}→{applied.get('hp_after')}。"
    )
    _append_log(combat, summary, context)
    _save_combat(context, combat)
    return {
        **_combat_summary_state(combat),
        "target": target.get("name"),
        "check": {"roll": roll, "candidates": candidates, "level": level, "target": con},
        "potency": potency,
        "poison_damage": {"expression": expression, "detail": detail, "total": total, "applied": applied_amount},
        **applied,
        "summary": summary,
    }


def end_combat(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗"}
    combat["active"] = False
    combat["ended_at"] = _timestamp()
    _append_log(combat, "战斗结束", context)
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
                "description": "战技类型，如 disarm/trip/pin/escape/knockout（缴械/推倒/压制/脱离/击晕）",
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
            "aim": {"type": "boolean", "description": "上一轮已瞄准（未开启瞄准跟踪房规时直接给奖励骰）"},
            "aim_action": {"type": "boolean", "description": "本轮声明瞄准并消耗行动；瞄准跟踪房规开启时使用"},
            "point_blank": {"type": "boolean", "description": "抵近射击（DEX/5 英尺内）"},
            "cover": {"type": "boolean", "description": "目标处于掩体/部分遮蔽"},
            "in_melee": {"type": "boolean", "description": "目标正与同伴近战（射击入近战房规）"},
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

COMBAT_JOIN_TOOL = AgentTool(
    name="combat.join",
    description="战斗中追加参与者：按先攻插入顺序，不重置其他人的本轮行动/反应次数。",
    parameters={
        "type": "object",
        "properties": {
            "participant": {
                "type": "string",
                "description": "房间成员名；NPC 请直接传对象",
            },
            "module_id": {"type": "string", "description": "从场景模组导入的怪物/NPC"},
            "initiative_check": {"type": "boolean", "description": "是否对该参与者做可选敏捷检定"},
        },
    },
    handler=join_combat,
)

COMBAT_HEAL_TOOL = AgentTool(
    name="combat.heal",
    description="直接恢复耐久（KP 手动结算），濒死且耐久回到 1 点以上时解除濒死。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "amount": {"type": "integer", "minimum": 1},
        },
        "required": ["target", "amount"],
    },
    handler=heal_combatant,
)

COMBAT_POISON_TOOL = AgentTool(
    name="combat.poison",
    description="毒剂结算：体质抵抗成功伤害减半。onset_rounds 大于 0 时只登记发作时间，不自动调度。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "damage": {"type": "string", "description": "伤害表达式，如 1D6 或 2D10+2"},
            "potency": {"type": "integer", "minimum": 0, "maximum": 2, "description": "毒性：CON 检定的惩罚骰数"},
            "doses": {"type": "integer", "minimum": 1, "description": "剂量倍数，默认 1"},
            "onset_rounds": {
                "type": "integer",
                "minimum": 0,
                "description": "发作轮数；大于 0 时本次只登记，到期后再次调用并传 0 结算",
            },
        },
        "required": ["target", "damage"],
    },
    handler=poison_combatant,
)

COMBAT_DELAY_TOOL = AgentTool(
    name="combat.delay",
    description="延迟行动：把自己的回合挪到本轮末尾；若已经是最后一人，则本轮放弃行动。",
    parameters={"type": "object", "properties": {}},
    handler=delay_combatant,
)

COMBAT_DISENGAGE_TOOL = AgentTool(
    name="combat.disengage",
    description="脱离近战：标记本轮已脱离，消耗本次行动。核心规则无借机攻击。",
    parameters={
        "type": "object",
        "properties": {"actor": {"type": "string", "description": "省略则使用当前行动者"}},
    },
    handler=disengage_combatant,
)

COMBAT_RELOAD_TOOL = AgentTool(
    name="combat.reload",
    description="装填/换弹：把指定武器弹药补满，消耗本轮行动。",
    parameters={
        "type": "object",
        "properties": {
            "actor": {"type": "string", "description": "省略则使用当前行动者"},
            "weapon": {"type": "string", "description": "省略则装填第一把有弹匣的远程武器"},
        },
    },
    handler=reload_combatant,
)

COMBAT_READY_TOOL = AgentTool(
    name="combat.ready",
    description="准备武器：准备后射击先攻按 DEX+50；耗时由房规 combat_ready_weapon_cost 决定。",
    parameters={
        "type": "object",
        "properties": {"actor": {"type": "string", "description": "省略则使用当前行动者"}},
    },
    handler=ready_combatant,
)

COMBAT_CAST_TOOL = AgentTool(
    name="combat.cast",
    description=(
        "战斗内施法：按 instant / one_round / two_rounds 计算生效时序，扣除 MP 与 SAN，"
        "MP 不足溢出扣 HP；打断按房规 combat_cast_interrupt 处理。具体法术效果由 KP/内容层结算。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "caster": {"type": "string", "description": "省略则使用当前行动者"},
            "spell": {"type": "string"},
            "cast_time": {
                "type": "string",
                "enum": ["instant", "one_round", "two_rounds"],
                "description": "即时 = DEX+50；1 轮 = 本轮 DEX；2 轮 = 次轮 DEX",
            },
            "mp_cost": {"type": "integer", "minimum": 0},
            "san_cost": {"type": "integer", "minimum": 0},
        },
        "required": ["spell"],
    },
    handler=cast_spell,
)

COMBAT_END_TOOL = AgentTool(
    name="combat.end",
    description="结束当前战斗轮并保存战斗记录。",
    parameters={"type": "object", "properties": {}},
    handler=end_combat,
)

COMBAT_TOOLS = [
    COMBAT_START_TOOL,
    COMBAT_JOIN_TOOL,
    COMBAT_STATE_TOOL,
    COMBAT_NEXT_TOOL,
    COMBAT_DELAY_TOOL,
    COMBAT_DISENGAGE_TOOL,
    COMBAT_RELOAD_TOOL,
    COMBAT_READY_TOOL,
    COMBAT_CAST_TOOL,
    COMBAT_ATTACK_TOOL,
    COMBAT_SHOOT_TOOL,
    COMBAT_DAMAGE_TOOL,
    COMBAT_HEAL_TOOL,
    COMBAT_POISON_TOOL,
    COMBAT_TREAT_TOOL,
    COMBAT_END_TOOL,
]
COMBAT_TOOL_NAMES = [tool.name for tool in COMBAT_TOOLS]
