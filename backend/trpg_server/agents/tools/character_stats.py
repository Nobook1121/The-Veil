"""KP 工具：修改房间快照里的技能值与基础属性。

与房间控制台 ``rule skill`` / ``rule attr`` 共用 :mod:`trpg_server.room_rules`，
只写本房间快照并登记统一待写回队列；持久角色卡仅在房间归档时合并。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_ai_changes, room_rules
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
        # 房主已回滚过的相同改动不再执行，避免模型换措辞后反复改回去。
        conflict = room_ai_changes.rollback_conflict(
            info,
            member,
            field=kind,
            name=target,
            value=None if delta is not None else value,
            delta=delta,
        )
        if conflict is not None:
            current = room_ai_changes.current_value(info, member, kind, target)
            return {
                "ok": True,
                "skipped": True,
                "rolled_back": True,
                "current": current,
                "summary": (
                    f"房主已回滚该改动，当前值为 {current}，以当前角色卡为准，不要重试。"
                ),
            }
        state_before = room_ai_changes.snapshot_resource_state(member)
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

    record = result.get("record") if isinstance(result.get("record"), dict) else {}
    label = "技能" if kind == "skill" else "属性"
    name = result.get("skill") if kind == "skill" else result.get("key")
    room_ai_changes.record_change(
        context.room_dir,
        info,
        member,
        tool="character.set_skill" if kind == "skill" else "character.set_attribute",
        field=kind,
        name=str(name or target),
        before=result["before"],
        after=result["after"],
        record_id=str(record.get("id") or ""),
        reason=str(arguments.get("reason") or "KP 调整"),
        state_before=state_before,
        actor=getattr(context, "username", None) or "AI KP",
        actor_id=getattr(context, "user_id", None),
    )
    return {
        "ok": True,
        "player_name": result["player_name"],
        label: name,
        "before": result["before"],
        "after": result["after"],
        "delta": result["delta"],
        "record_id": record.get("id") or "",
        "permanent": False,
        "summary": (
            f"{result['player_name']} 的{label}「{name}」{result['before']} → {result['after']}"
            "（仅房间快照，归档后永久化）"
        ),
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
}

SET_CHARACTER_SKILL_TOOL = AgentTool(
    name="character.set_skill",
    description=(
        "Change a room player's skill value in the room snapshot. Use value for an absolute "
        "target or delta for a relative change. The change stays in the room snapshot and is "
        "merged into the persistent character card only when the room is archived."
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
        "relative change. The change stays in the room snapshot and is merged into the persistent "
        "character card only when the room is archived."
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
