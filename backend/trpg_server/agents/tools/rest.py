"""休息、生命骰与同调的 AI 工具入口。

这些工具本身是规则集中立的：真正的数值与判定由房间规则集插件提供。
没有 ``rest`` 能力的规则集（CoC7）不会下发这些工具，直接调用也会得到明确错误。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool, ToolExecutionError
from trpg_server import room_rest
from trpg_server.room_rules import RuleOperationError, load_room_info


def _require_room(context: Any):
    room_dir = getattr(context, "room_dir", None)
    if room_dir is None:
        raise ToolExecutionError("当前不在可用的房间目录中")
    return room_dir, load_room_info(room_dir)


def _member(info: dict[str, Any], name: Any, context: Any) -> dict[str, Any]:
    from trpg_server.room_spells import resolve_member

    member, error = resolve_member(info, name, context, role="角色")
    if member is None:
        raise ToolExecutionError(error)
    return member


def take_rest_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir, info = _require_room(context)
    try:
        return room_rest.take_rest(
            room_dir,
            info,
            kind=str(arguments.get("kind") or "short"),
            hit_dice=int(arguments.get("hit_dice") or 1),
            members=arguments.get("members"),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
        )
    except RuleOperationError as exc:
        return {"error": exc.message, "code": exc.code}
    except ValueError as exc:
        return {"error": str(exc)}


def attune_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir, info = _require_room(context)
    member = _member(info, arguments.get("player_name"), context)
    try:
        return room_rest.attune(
            room_dir,
            info,
            member,
            arguments.get("item"),
            during_rest=bool(arguments.get("during_rest")),
        )
    except RuleOperationError as exc:
        return {"error": exc.message, "code": exc.code}


def unattune_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir, info = _require_room(context)
    member = _member(info, arguments.get("player_name"), context)
    try:
        return room_rest.unattune(room_dir, info, member, arguments.get("item"))
    except RuleOperationError as exc:
        return {"error": exc.message, "code": exc.code}


TAKE_REST_TOOL = AgentTool(
    name="rest.take",
    description=(
        "进行短休或长休。短休消耗生命骰恢复 HP；长休恢复 HP、一半生命骰、"
        "全部法术位并降低力竭。具体数值由当前规则集结算，不要自行编造恢复量。"
        "长休前请确认队伍确实有休息的时间与安全环境。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["short", "long"], "description": "short 短休 / long 长休"},
            "hit_dice": {"type": "integer", "minimum": 0, "description": "短休消耗的生命骰数量，默认 1"},
            "members": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参与休息的成员名；省略表示全队",
            },
        },
        "required": ["kind"],
    },
    handler=take_rest_tool,
    rulesets=("dnd5e",),
)

ATTUNE_TOOL = AgentTool(
    name="equipment.attune",
    description=(
        "在短休或长休期间与一件魔法装备建立同调。同调有数量上限，"
        "且只对需要同调的装备有效；具体上限与规则由当前规则集决定。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "角色名或玩家名；省略表示调用者本人"},
            "item": {"type": "string", "description": "装备 id 或名称"},
            "during_rest": {"type": "boolean", "description": "是否正处于休息期间"},
        },
        "required": ["item"],
    },
    handler=attune_tool,
    rulesets=("dnd5e",),
)

UNATTUNE_TOOL = AgentTool(
    name="equipment.unattune",
    description="解除一件装备的同调，立即生效。",
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "角色名或玩家名；省略表示调用者本人"},
            "item": {"type": "string", "description": "装备 id 或名称"},
        },
        "required": ["item"],
    },
    handler=unattune_tool,
    rulesets=("dnd5e",),
)


REST_TOOLS = [TAKE_REST_TOOL, ATTUNE_TOOL, UNATTUNE_TOOL]
REST_TOOL_NAMES = [tool.name for tool in REST_TOOLS]

__all__ = ["ATTUNE_TOOL", "REST_TOOLS", "REST_TOOL_NAMES", "TAKE_REST_TOOL", "UNATTUNE_TOOL"]
