"""CoC7 批次 5 追逐工具：会话、移动、险境与冲突。

规则数学在 :mod:`trpg_server.ruleset.coc7.chase`，状态在
:mod:`trpg_server.room_chase`；本模块只做参数搬运与房规开关校验。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_chase, room_rules
from trpg_server.agents.tools.base import AgentTool


def _options(context: Any):
    from trpg_server.ruleset.coc7.options import resolve_options

    return resolve_options(context.room_info())


def _disabled() -> dict[str, Any]:
    return {"ok": False, "error": "coc7 chase house rule is disabled"}


def _common(context: Any) -> dict[str, Any]:
    return {
        "actor": getattr(context, "username", None) or "AI KP",
        "actor_id": getattr(context, "user_id", None),
        "config_dir": getattr(context, "config_dir", None),
        "room_id": getattr(context, "room_id", None),
    }


def _participant(spec: Any, info: dict[str, Any]) -> dict[str, Any]:
    data = spec if isinstance(spec, dict) else {}
    name = str(data.get("name") or data.get("player_name") or "").strip()
    member = room_rules.find_member(info, name) if name else None
    if member:
        participant = room_chase.participant_from_member(
            member, role=str(data.get("role") or "pursuer")
        )
    else:
        participant = room_chase.participant_from_spec(
            {
                "name": name,
                "role": data.get("role"),
                "mov": data.get("mov"),
                "dex": data.get("dex"),
                "build": data.get("build"),
                "vehicle": data.get("vehicle"),
            }
        )
    return participant


def execute_chase_start(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    info = context.room_info()
    specs = arguments.get("participants")
    if not isinstance(specs, list) or len(specs) < 2:
        return {"ok": False, "error": "participants 至少需要追逐者与逃离者各一方"}
    try:
        participants = [_participant(spec, info) for spec in specs]
        chase = room_chase.start_chase(
            context.room_dir,
            info,
            participants,
            locations=[str(item) for item in (arguments.get("locations") or []) if str(item).strip()],
            actor=getattr(context, "username", None) or "AI KP",
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {
        "ok": True,
        "chase": room_chase.chase_snapshot(chase),
        "summary": room_chase.format_chase(chase),
    }


def execute_chase_status(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    chase = room_chase.get_chase(context.room_info())
    return {
        "ok": True,
        "active": bool(chase),
        "chase": room_chase.chase_snapshot(chase),
        "summary": room_chase.format_chase(chase),
    }


def execute_chase_next(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    try:
        chase = room_chase.start_round(context.room_dir, context.room_info(), actor=getattr(context, "username", None))
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "chase": room_chase.chase_snapshot(chase), "summary": room_chase.format_chase(chase)}


def execute_chase_move(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    try:
        chase = room_chase.move_participant(
            context.room_dir,
            context.room_info(),
            arguments.get("name"),
            locations=arguments.get("locations") or 1,
            accelerate=bool(arguments.get("accelerate")),
            actor=getattr(context, "username", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "chase": room_chase.chase_snapshot(chase), "summary": room_chase.format_chase(chase)}


def _check_level(arguments: dict[str, Any], context: Any, name: str, bonus: int) -> str:
    explicit = str(arguments.get("result") or "").strip().lower()
    if explicit:
        return explicit
    member = room_rules.find_member(context.room_info(), name)
    if not member:
        raise room_rules.RuleOperationError(
            "非成员参与者请用 result regular|hard|extreme|critical|failure 传入检定结果",
            code="chase_need_result",
        )
    result = room_rules.roll_check(
        context.room_dir,
        context.room_info(),
        member,
        name=str(arguments.get("skill") or "敏捷"),
        difficulty=str(arguments.get("difficulty") or "regular"),
        bonus=bonus,
        reason="追逐",
        **_common(context),
    )
    return str(result.get("success_level") or "failure")


def execute_chase_hazard(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    name = str(arguments.get("name") or arguments.get("player_name") or "").strip()
    ap_spent = int(arguments.get("ap") or 0)
    try:
        level = _check_level(arguments, context, name, min(2, max(0, ap_spent)))
        payload = room_chase.attempt_hazard(
            context.room_dir,
            context.room_info(),
            name,
            success_level=level,
            ap_spent=ap_spent,
            ap_loss=arguments.get("ap_loss") or 1,
            damage=arguments.get("damage") or 0,
            note=str(arguments.get("skill") or ""),
            actor=getattr(context, "username", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {
        "ok": True,
        "hazard": payload,
        "chase": room_chase.chase_snapshot(room_chase.get_chase(context.room_info())),
    }


def execute_chase_conflict(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).chase_enabled:
        return _disabled()
    try:
        payload = room_chase.resolve_conflict(
            context.room_dir,
            context.room_info(),
            arguments.get("attacker"),
            arguments.get("target"),
            has_firearm=bool(arguments.get("firearm")),
            maneuver_success=bool(arguments.get("maneuver_success")),
            ap_loss=arguments.get("ap_loss") or 1,
            note=str(arguments.get("note") or ""),
            actor=getattr(context, "username", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {
        "ok": True,
        "conflict": payload,
        "chase": room_chase.chase_snapshot(room_chase.get_chase(context.room_info())),
    }


CHASE_START_TOOL = AgentTool(
    name="chase.start",
    description=(
        "Start a CoC7 chase. The first participant is the pursuer and the second is the quarry; "
        "the quarry starts two locations ahead. Participants may be room members (name matches a "
        "member) or NPCs with explicit mov/dex/build. Use only when the chase house rule is enabled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "participants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string", "enum": ["pursuer", "quarry"]},
                        "mov": {"type": "integer"},
                        "dex": {"type": "integer"},
                        "build": {"type": "integer"},
                        "vehicle": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
            "locations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered chase track; omit for six generic locations.",
            },
        },
        "required": ["participants"],
    },
    handler=execute_chase_start,
)

CHASE_STATUS_TOOL = AgentTool(
    name="chase.status",
    description="Read current CoC7 chase positions, action points, obstacles and outcome.",
    parameters={"type": "object", "properties": {}},
    handler=execute_chase_status,
)

CHASE_NEXT_TOOL = AgentTool(
    name="chase.next",
    description=(
        "Advance the chase to the next round and reset action points. Action points are "
        "1 plus the participant's MOV advantage over the slowest participant."
    ),
    parameters={"type": "object", "properties": {}},
    handler=execute_chase_next,
)

CHASE_MOVE_TOOL = AgentTool(
    name="chase.move",
    description=(
        "Move a participant one location (costs one action point). Vehicles may accelerate and "
        "move two to five locations for one action point. Blocked by unpassed obstacles."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "locations": {"type": "integer", "description": "Locations to move; default 1."},
            "accelerate": {"type": "boolean", "description": "Vehicle acceleration (2-5 locations)."},
        },
        "required": ["name"],
    },
    handler=execute_chase_move,
)

CHASE_HAZARD_TOOL = AgentTool(
    name="chase.hazard",
    description=(
        "Resolve a chase hazard. Spend action points for bonus dice (1 point per die, max 2). "
        "Success or failure both advance one location; failure loses 1D3 action points and may "
        "deal the damage the Keeper assigns. Members roll automatically; NPCs need result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "skill": {"type": "string", "description": "Skill or attribute used to cross the hazard."},
            "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"]},
            "ap": {"type": "integer", "description": "Action points spent for bonus dice (0-2)."},
            "damage": {"type": "integer", "description": "Damage the Keeper assigns on failure."},
            "ap_loss": {"type": "integer", "description": "1D3 result for action point loss; default 1."},
            "result": {
                "type": "string",
                "enum": ["critical", "extreme", "hard", "regular", "failure", "fumble"],
                "description": "Required for NPC participants instead of rolling.",
            },
        },
        "required": ["name"],
    },
    handler=execute_chase_hazard,
)

CHASE_CONFLICT_TOOL = AgentTool(
    name="chase.conflict",
    description=(
        "Resolve the chase conflict gate: melee only within the same location unless a firearm is "
        "used; attacking costs one action point. A successful maneuver makes the target lose 1D3 "
        "action points. Attack damage itself is resolved with the combat tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "attacker": {"type": "string"},
            "target": {"type": "string"},
            "firearm": {"type": "boolean", "description": "Allow attacking from another location."},
            "maneuver_success": {"type": "boolean", "description": "Whether a maneuver succeeded."},
            "ap_loss": {"type": "integer", "description": "1D3 result for the target's action point loss."},
            "note": {"type": "string"},
        },
        "required": ["attacker", "target"],
    },
    handler=execute_chase_conflict,
)

CHASE_TOOLS = (
    CHASE_START_TOOL,
    CHASE_STATUS_TOOL,
    CHASE_NEXT_TOOL,
    CHASE_MOVE_TOOL,
    CHASE_HAZARD_TOOL,
    CHASE_CONFLICT_TOOL,
)

CHASE_TOOL_NAMES = tuple(tool.name for tool in CHASE_TOOLS)
