"""KP 工具：通过 function 调用给角色的房间快照追加物品 / 武器。

与玩家命令 ``/item``、房间控制台 ``rule item`` 共用 :mod:`trpg_server.room_rules`，
默认只写本房间快照；``persist=true`` 才写回持久角色卡。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_rules
from trpg_server.agents.tools.base import AgentTool


def execute_add_character_item(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    try:
        member = room_rules.resolve_member(
            info,
            arguments.get("player_name") or arguments.get("playerName"),
            # KP 工具与房主同级：可以对任意玩家发放物品。
            actor_id=getattr(context, "user_id", None),
            actor_rank=3,
        )
        result = room_rules.add_item(
            context.room_dir,
            info,
            member,
            name=str(arguments.get("name") or ""),
            kind=str(arguments.get("kind") or "item"),
            quantity=arguments.get("quantity", 1),
            note=str(arguments.get("note") or ""),
            damage=str(arguments.get("damage") or ""),
            skill=str(arguments.get("skill") or ""),
            reason=str(arguments.get("reason") or "KP 发放"),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
            source="ai_tool",
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}

    persisted = False
    if arguments.get("persist"):
        from trpg_server.routes.characters import character_service_actor

        try:
            room_rules.persist_member_card(
                member,
                actor=character_service_actor(),
                item_record_id=(result.get("record") or {}).get("id"),
            )
            persisted = True
        except room_rules.RuleOperationError as exc:
            result["persist_error"] = exc.message
    room_rules.save_room_info(context.room_dir, info)
    label = "武器" if result["kind"] == "weapon" else "物品"
    return {
        "ok": True,
        "player_name": result["player_name"],
        "item": result["item"],
        "kind": result["kind"],
        "quantity": result["quantity"],
        "persisted": persisted,
        "summary": f"{result['player_name']} 获得{label}「{result['item'].get('name')}」×{result['quantity']}",
    }


ADD_CHARACTER_ITEM_TOOL = AgentTool(
    name="character.add_item",
    description=(
        "Give a room player an item or weapon. Writes to the room snapshot only by default; "
        "set persist=true to also save it to the player's persistent character card. "
        "Use kind='weapon' for weapons (optional damage/skill), otherwise kind='item'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {
                "type": "string",
                "description": "Room member's login username or character card name.",
            },
            "name": {"type": "string", "description": "Item or weapon name."},
            "kind": {
                "type": "string",
                "enum": ["item", "weapon"],
                "description": "item (default) or weapon.",
            },
            "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
            "note": {"type": "string"},
            "damage": {"type": "string", "description": "Weapon damage, e.g. 1d6+1."},
            "skill": {"type": "string", "description": "Weapon skill, e.g. 格斗(斗殴)."},
            "reason": {"type": "string", "description": "Why the player receives it (kept in the audit log)."},
            "persist": {"type": "boolean", "description": "Also write to the persistent character card."},
        },
        "required": ["player_name", "name"],
    },
    handler=execute_add_character_item,
)
