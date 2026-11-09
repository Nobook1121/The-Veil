"""KP 工具：修改房间快照里的技能值与基础属性。

与房间控制台 ``rule skill`` / ``rule attr`` 共用 :mod:`trpg_server.room_rules`，
默认只写本房间快照并登记统一待写回队列；``persist=true`` 才立即写回持久角色卡。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_rules
from trpg_server.agents.tools.base import AgentTool


def _int_argument(arguments: dict[str, Any], key: str) -> int | None:
    value = arguments.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _execute_change(kind: str, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    value = _int_argument(arguments, "value")
    delta = _int_argument(arguments, "delta")
    if value is None and delta is None:
        return {"ok": False, "error": "value or delta is required"}
    target = str(arguments.get("name") or "").strip()
    if not target:
        return {"ok": False, "error": "name is required"}
    try:
        member = room_rules.resolve_member(
            info,
            arguments.get("player_name") or arguments.get("playerName"),
            # KP 工具与房主同级：可以调整任意玩家的数值。
            actor_id=getattr(context, "user_id", None),
            actor_rank=3,
        )
        common = {
            "value": None if delta is not None else value,
            "delta": delta,
            "reason": str(arguments.get("reason") or "KP 调整"),
            "actor": getattr(context, "username", None) or "AI KP",
            "actor_id": getattr(context, "user_id", None),
            "source": "ai_tool",
        }
        if kind == "skill":
            result = room_rules.apply_skill_change(
                context.room_dir,
                info,
                member,
                name=target,
                **common,
            )
        else:
            result = room_rules.apply_attribute_change(
                context.room_dir,
                info,
                member,
                key=target,
                **common,
            )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}

    persisted = False
    if arguments.get("persist"):
        from trpg_server.routes.characters import character_service_actor

        try:
            room_rules.persist_member_card(member, actor=character_service_actor())
            persisted = True
        except room_rules.RuleOperationError as exc:
            result["persist_error"] = exc.message
    room_rules.save_room_info(context.room_dir, info)
    label = "技能" if kind == "skill" else "属性"
    name = result.get("skill") if kind == "skill" else result.get("key")
    return {
        "ok": True,
        "player_name": result["player_name"],
        label: name,
        "before": result["before"],
        "after": result["after"],
        "delta": result["delta"],
        "persisted": persisted,
        "summary": f"{result['player_name']} 的{label}「{name}」{result['before']} → {result['after']}",
    }


def execute_set_character_skill(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _execute_change("skill", arguments, context)


def execute_set_character_attribute(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _execute_change("attr", arguments, context)


_SET_COMMON_PROPERTIES = {
    "player_name": {
        "type": "string",
        "description": "Room member's login username or character card name.",
    },
    "value": {"type": "integer", "description": "Absolute new value."},
    "delta": {"type": "integer", "description": "Relative change; takes precedence over value when set."},
    "reason": {"type": "string", "description": "Why the value changes (kept in the audit log)."},
    "persist": {"type": "boolean", "description": "Also write to the persistent character card."},
}

SET_CHARACTER_SKILL_TOOL = AgentTool(
    name="character.set_skill",
    description=(
        "Change a room player's skill value in the room snapshot. Use value for an absolute "
        "target or delta for a relative change. Writes to the room snapshot only by default; "
        "set persist=true to also save to the persistent character card."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_SET_COMMON_PROPERTIES,
            "name": {"type": "string", "description": "Skill name on the character card, e.g. 侦查."},
        },
        "required": ["player_name", "name"],
    },
    handler=execute_set_character_skill,
)

SET_CHARACTER_ATTRIBUTE_TOOL = AgentTool(
    name="character.set_attribute",
    description=(
        "Change a room player's base attribute (STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC or the "
        "Chinese name) in the room snapshot. Use value for an absolute target or delta for a "
        "relative change. Writes to the room snapshot only by default; set persist=true to also "
        "save to the persistent character card."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_SET_COMMON_PROPERTIES,
            "name": {
                "type": "string",
                "description": "Attribute key, e.g. STR, DEX, INT, EDU, LUC or 力量 / 敏捷 / 智力.",
            },
        },
        "required": ["player_name", "name"],
    },
    handler=execute_set_character_attribute,
)
