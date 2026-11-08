"""战斗轮 REST API。

战斗规则与状态结算都在 ``agents/tools/combat.py``；本模块只负责把它接到
房间前端：读取战斗面板数据、校验「谁在当前回合」，并把玩家选择的动作
转成一次「玩家自己掷骰」的攻击检定（复用 ``player_roll`` 的投掷流程）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, request, session

from trpg_server import weapon_catalog
from trpg_server.agents import player_roll
from trpg_server.agents.tools import combat
from trpg_server.agents.tools.dice import _percentile_from_faces, _plan_percentile
from trpg_server.json_store import read_json
from trpg_server.responses import error_response, success_response
from trpg_server.settings import CONFIG_DIR, SCENARIOS_DIR
from trpg_server.routes.rooms import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_OWNER,
    _can_access,
    _denied,
    _find_member,
    _find_room,
    _is_elevated,
    _require_login,
    _room_permission,
)

bp = Blueprint("combat", __name__)
logger = logging.getLogger(__name__)

MELEE_MODES = {"attack", "maneuver"}
SUPPORTED_ACTIONS = {
    "attack",
    "shoot",
    "maneuver",
    "next",
    "end",
    "delay",
    "disengage",
    "reload",
    "ready",
    "cast",
}
UTILITY_ACTIONS = {"delay", "disengage", "reload", "ready", "cast"}


class CombatRoomContext:
    """给 combat 工具用的轻量上下文；``room_info`` 每次都从磁盘读取最新状态。"""

    def __init__(self, room_dir: Path, room_id: str) -> None:
        self.room_dir = room_dir
        self.room_id = room_id
        self.config_dir = Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
        self.scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))
        self.tool_state: dict[str, Any] = {}
        self.user_id = str(session.get("user_id") or "")

    def room_info(self) -> dict[str, Any]:
        return read_json(Path(self.room_dir) / "info.json", default={})


def _context(room_dir: Path, room_id: str) -> CombatRoomContext:
    return CombatRoomContext(room_dir, room_id)


def _member_for_combatant(info: dict[str, Any], name: Any) -> dict[str, Any] | None:
    """按角色卡名/用户名在房间成员中定位该战斗参与者。"""
    expected = str(name or "").strip().casefold()
    if not expected:
        return None
    for member in (info.get("members") or []):
        if not isinstance(member, dict):
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        names = {
            str(member.get("username") or "").casefold(),
            str(member.get("character_name") or "").casefold(),
            str(card.get("name") or "").casefold(),
        }
        if expected in names:
            return member
    return None


def _owner_user_id(info: dict[str, Any], name: Any) -> str:
    member = _member_for_combatant(info, name)
    return str(member.get("user_id") or "") if member else ""


def _is_manager(info: dict[str, Any]) -> bool:
    if _is_elevated():
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _state_payload(state: dict[str, Any], info: dict[str, Any], context: CombatRoomContext) -> dict[str, Any]:
    """补充前端渲染动作按钮所需的信息。"""
    current = state.get("current")
    actor_user_id = _owner_user_id(info, current) if current else ""
    my_name = ""
    for item in state.get("order") or []:
        if item.get("is_player") and str(item.get("user_id") or "") == str(session.get("user_id") or ""):
            my_name = str(item.get("name") or "")
            break
    can_act = bool(current) and (
        str(actor_user_id) == str(session.get("user_id") or "") or _is_elevated()
    )
    # 推进回合：自己的回合可跳过；房管可推进 NPC 回合，防止替别人跳完整轮。
    can_advance = bool(current) and (
        (not actor_user_id and _is_manager(info))
        or (bool(actor_user_id) and str(actor_user_id) == str(session.get("user_id") or ""))
        or _is_elevated()
    )
    # 玩家操作自己的角色；管理员代打时操作当前行动者（可能是 NPC）。
    focus_name = (my_name if (can_act and my_name) else str(current or "")) if can_act else ""
    payload = {
        **state,
        "actor_user_id": actor_user_id,
        "my_combatant": my_name,
        # 当前回合属于自己（或管理员代打）时才能选动作。
        "can_act": can_act,
        "can_advance": can_advance,
        "is_manager": _is_manager(info),
        "pending_roll": player_roll.active_pending_info(context.room_id),
    }
    if focus_name:
        combat_state = combat._load_combat(context)
        payload["weapons"] = _weapon_options(combat_state, focus_name, info)
        payload["targets"] = [
            {"name": item.get("name"), "hp": item.get("hp"), "status": item.get("status"), "side": item.get("side")}
            for item in state.get("order") or []
            if str(item.get("name")) != focus_name
            and str(item.get("status") or "active") not in combat.SKIPPED_STATUS
        ]
    return payload


def _weapon_options(combat_state_data: dict[str, Any] | None, name: str, info: dict[str, Any]) -> list[dict[str, Any]]:
    if not combat_state_data:
        return []
    combatant = combat._resolve_combatant(combat_state_data, name)
    if not combatant:
        return []
    card = _member_card(info, name)
    rows = combat._weapon_rows(combatant, card)
    options = []
    for row in rows:
        rules = weapon_catalog.weapon_rules(row)
        options.append(
            {
                "name": str(row.get("name") or ""),
                "skill": str(row.get("skill") or ""),
                "damage": str(row.get("damage") or ""),
                "ranged": bool(rules.get("rangeYards") and rules.get("rangeYards") > 0),
            }
        )
    if not options:
        options.append({"name": combat.UNARMED_WEAPON["name"], "skill": combat.UNARMED_WEAPON["skill"], "damage": "1D3", "ranged": False})
    return options


def _member_card(info: dict[str, Any], name: str) -> dict[str, Any] | None:
    member = _member_for_combatant(info, name)
    if not member:
        return None
    card = member.get("character_card")
    return card if isinstance(card, dict) else None


def _prepare_check(context: CombatRoomContext, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """在掷骰前计算技能值与检定计划；返回 ``{error}`` 或计划信息。"""
    combat_state_data = combat._load_combat(context)
    if not combat_state_data:
        return {"error": "当前没有进行中的战斗"}
    actor = str(payload.get("actor") or "").strip()
    target = str(payload.get("target") or "").strip()
    attacker = combat._resolve_combatant(combat_state_data, actor)
    targetant = combat._resolve_combatant(combat_state_data, target)
    if not attacker:
        return {"error": "当前行动者不在战斗中"}
    if action in MELEE_MODES and not targetant:
        return {"error": "请选择一个攻击目标"}
    info = context.room_info()
    card = _member_card(info, actor)
    weapon_name = str(payload.get("weapon") or "").strip()
    weapon = combat._find_weapon(attacker, card, weapon_name) if weapon_name else None
    if weapon_name and weapon is None:
        return {"error": f"角色卡上没有武器“{weapon_name}”"}
    if weapon is None:
        weapon = combat.UNARMED_WEAPON
    if action == "shoot":
        skill_value, skill_label = combat._resolve_skill_value(
            attacker, card, requested=str(payload.get("skill") or ""),
            skill_key=str(weapon.get("skillKey") or ""),
            specialty_key=str(weapon.get("specialtyKey") or ""),
            names=combat.RANGED_SKILL_NAMES, default=20,
        )
    else:
        skill_value, skill_label = combat._resolve_skill_value(
            attacker, card, requested=str(payload.get("skill") or ""),
            skill_key=str(weapon.get("skillKey") or ""),
            specialty_key=str(weapon.get("specialtyKey") or ""),
            names=combat.MELEE_SKILL_NAMES, default=25,
        )
    bonus = combat._dice_count(payload, "bonus_dice", "bonus")
    penalty = combat._dice_count(payload, "penalty_dice", "penalty")
    if bonus and penalty:
        return {"error": "奖励骰与惩罚骰不能同时使用"}
    plan = _plan_percentile(
        check_name=skill_label,
        difficulty="regular",
        target=max(1, min(100, int(skill_value or 1))),
        bonus=bonus,
        penalty=penalty,
    )
    return {
        "plan": plan,
        "skill_value": skill_value,
        "skill_label": skill_label,
        "weapon": weapon,
        "attacker": attacker,
        "target": targetant,
        "bonus": bonus,
        "penalty": penalty,
    }


def _action_arguments(payload: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    weapon = prepared["weapon"]
    return {
        "attacker": prepared["attacker"].get("name"),
        "target": (prepared.get("target") or {}).get("name"),
        "weapon": weapon.get("name"),
        "defense": str(payload.get("defense") or "dodge"),
        "maneuver": str(payload.get("maneuver") or "").strip(),
        "bonus_dice": prepared["bonus"],
        "penalty_dice": prepared["penalty"],
        "range": payload.get("range"),
        "burst": payload.get("burst") or 1,
    }


def _run_utility_action(action: str, payload: dict[str, Any], context: CombatRoomContext) -> dict[str, Any]:
    """不经过玩家掷骰的辅助动作：延迟、脱离、装填、准备武器、施法。"""
    actor = str(payload.get("actor") or "").strip()
    if action == "delay":
        return combat.delay_combatant({}, context)
    if action == "disengage":
        return combat.disengage_combatant({"actor": actor}, context)
    if action == "ready":
        return combat.ready_combatant({"actor": actor}, context)
    if action == "reload":
        return combat.reload_combatant(
            {"actor": actor, "weapon": str(payload.get("weapon") or "").strip()},
            context,
        )
    return combat.cast_spell(
        {
            "caster": actor,
            "spell": str(payload.get("spell") or "").strip(),
            "cast_time": str(payload.get("cast_time") or "instant"),
            "mp_cost": payload.get("mp_cost") or 0,
            "san_cost": payload.get("san_cost") or 0,
        },
        context,
    )


def _run_action(action: str, payload: dict[str, Any], context: CombatRoomContext) -> dict[str, Any]:
    if action in UTILITY_ACTIONS:
        return _run_utility_action(action, payload, context)
    prepared = _prepare_check(context, action, payload)
    if prepared.get("error"):
        return prepared
    arguments = _action_arguments(payload, prepared)
    if action == "shoot":
        resolver = lambda: combat.shoot_combatant(arguments, context)
        face_resolver = lambda faces: combat.shoot_combatant(
            {**arguments, "attack_roll": _percentile_from_faces(faces, prepared["bonus"], prepared["penalty"])[0]},
            context,
        )
        tool_name = "combat.shoot"
    else:
        if action == "maneuver" and not arguments["maneuver"]:
            return {"error": "战技需要指定效果（例如 disarm / knock_down）"}
        resolver = lambda: combat.attack_combatant(arguments, context)
        face_resolver = lambda faces: combat.attack_combatant(
            {**arguments, "attack_roll": _percentile_from_faces(faces, prepared["bonus"], prepared["penalty"])[0]},
            context,
        )
        tool_name = "combat.attack"
    roller_id = session.get("user_id")
    roller_name = str(session.get("username") or "")
    request_id, error = player_roll.create_roll_request(
        context,
        {"check": prepared["plan"].get("check"), **prepared["plan"], "skill": prepared["skill_label"]},
        resolver,
        roller_user_id=roller_id,
        roller_name=roller_name,
        tool_name=tool_name,
        face_resolver=face_resolver,
    )
    if request_id is None:
        # 没有 Socket（或房间已有等待投掷）时已直接结算，返回最新状态。
        if error == "pending":
            return {"error": "房间中已有待投掷的检定，请先完成它"}
        state = combat.combat_state({}, context)
        return {"immediate": True, **_state_payload(state, context.room_info(), context)}
    return {
        "request_id": request_id,
        "plan": prepared["plan"],
        "skill_label": prepared["skill_label"],
        "skill_value": prepared["skill_value"],
        "weapon": prepared["weapon"].get("name"),
        "target": (prepared.get("target") or {}).get("name"),
    }


@bp.route("/api/rooms/<room_id>/combat", methods=["GET"])
def get_combat(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    context = _context(room_dir, room_id)
    state = combat.combat_state({}, context)
    if not state.get("active"):
        return success_response({"active": False, "order": [], "log": [], "can_act": False}, "No active combat")
    state["log"] = (combat._load_combat(context) or {}).get("log") or []
    return success_response(_state_payload(state, info, context), "Combat state loaded")


@bp.route("/api/rooms/<room_id>/combat/action", methods=["POST"])
def post_combat_action(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in SUPPORTED_ACTIONS:
        return error_response("Unsupported combat action", 400, "Unsupported action")
    context = _context(room_dir, room_id)
    state = combat.combat_state({}, context)
    if not state.get("active"):
        return error_response("No active combat", 409, "No active combat")
    current = str(state.get("current") or "")
    if action == "end":
        if not _is_manager(info):
            return error_response("Only room managers may end combat", 403, "Forbidden")
        combat.end_combat({"reason": str(payload.get("reason") or "")}, context)
        return success_response({"active": False}, "Combat ended")
    if action == "next":
        # 推进回合：玩家只能跳过自己的回合；房管只能推进 NPC 的回合，
        # 不能替其他玩家接连跳过整轮。
        actor_owner = _owner_user_id(info, current)
        is_npc_turn = not actor_owner
        is_own_turn = bool(actor_owner) and str(actor_owner) == str(session.get("user_id") or "")
        if not (is_own_turn or (_is_manager(info) and is_npc_turn) or _is_elevated()):
            return error_response("Not your turn", 403, "Not your turn")
        result = combat.next_turn({}, context)
        return success_response(_state_payload(result, info, context), "Combat advanced")
    # 攻击类动作：必须是当前行动者本人（管理员可代打）。
    actor = str(payload.get("actor") or current).strip()
    if actor != current:
        return error_response("Not your turn", 409, "Not your turn")
    actor_owner = _owner_user_id(info, actor)
    if not _is_elevated() and str(actor_owner) != str(session.get("user_id") or ""):
        return error_response("Not your turn", 403, "Not your turn")
    result = _run_action(action, {**payload, "actor": actor}, context)
    if result.get("error"):
        return error_response(result["error"], 400, "Combat action failed")
    return success_response(result, "Combat action accepted")
