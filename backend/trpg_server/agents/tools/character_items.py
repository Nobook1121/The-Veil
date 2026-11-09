"""KP 工具：通过 function 调用给角色的房间快照追加物品 / 武器。

与玩家命令 ``/item``、房间控制台 ``rule item`` 共用 :mod:`trpg_server.room_rules`，
只写本房间快照；持久角色卡仅在房间归档时合并。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_ai_changes, room_rules
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
        item_name = str(arguments.get("name") or "").strip()
        item_kind = str(arguments.get("kind") or "item").strip().lower()
        amount = max(1, min(999, room_rules._as_int(arguments.get("quantity"), 1)))
        conflict = room_ai_changes.rollback_conflict(
            info,
            member,
            field="weapon" if item_kind == "weapon" else "item",
            name=item_name,
            kind=item_kind,
            amount=amount,
        )
        if conflict is not None:
            current = room_ai_changes.current_value(
                info, member, "weapon" if item_kind == "weapon" else "item", item_name
            )
            return {
                "ok": True,
                "skipped": True,
                "rolled_back": True,
                "current": current,
                "summary": (
                    f"房主已回滚该物品发放（{item_name} ×{amount}），当前持有 ×{current or 0}，"
                    "以当前角色卡为准，不要重试。"
                ),
            }
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

    record = result.get("record") if isinstance(result.get("record"), dict) else {}
    before_row = record.get("before_row")
    before_quantity = (
        room_rules._as_int(before_row.get("quantity"), 1)
        if isinstance(before_row, dict)
        else 0
    )
    label = "武器" if result["kind"] == "weapon" else "物品"
    after_quantity = 1 if result["kind"] == "weapon" else before_quantity + result["quantity"]
    room_ai_changes.record_change(
        context.room_dir,
        info,
        member,
        tool="character.add_item",
        field="weapon" if result["kind"] == "weapon" else "item",
        name=str(result["item"].get("name") or item_name),
        before=before_quantity,
        after=after_quantity,
        record_id=str(record.get("id") or ""),
        kind=str(result["kind"]),
        amount=result["quantity"],
        before_row=before_row if isinstance(before_row, dict) else None,
        reason=str(arguments.get("reason") or "KP 发放"),
        actor=getattr(context, "username", None) or "AI KP",
        actor_id=getattr(context, "user_id", None),
    )
    return {
        "ok": True,
        "player_name": result["player_name"],
        "item": result["item"],
        "kind": result["kind"],
        "quantity": result["quantity"],
        "record_id": record.get("id") or "",
        "permanent": False,
        "summary": (
            f"{result['player_name']} 获得{label}「{result['item'].get('name')}」×{result['quantity']}"
            "（仅房间快照，归档后永久化）"
        ),
    }


ADD_CHARACTER_ITEM_TOOL = AgentTool(
    name="character.add_item",
    description=(
        "Give a room player an item or weapon. The change stays in the room snapshot and is "
        "merged into the persistent character card only when the room is archived. "
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
        },
        "required": ["player_name", "name"],
    },
    handler=execute_add_character_item,
)
