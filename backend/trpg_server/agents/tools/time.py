"""AI KP 的只读时钟工具。

时间推进只能由房主 / 房间管理员通过房间控制台完成；AI 侧不提供任何推进
入口，避免模型自行跳时间。这个工具只返回当前游戏时间与累计推进量。
"""

from __future__ import annotations

from typing import Any

from trpg_server import room_clock
from trpg_server.agents.tools.base import AgentTool
from trpg_server.room_rules import load_room_info


def game_clock_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir = getattr(context, "room_dir", None)
    if room_dir is None:
        return {"error": "当前不在可用的房间目录中"}
    info = load_room_info(room_dir)
    if not room_clock.is_enabled(info):
        return {
            "enabled": False,
            "summary": "本房间未启用游戏时钟；需要时间时请向房主 / 管理员确认。",
        }
    clock = room_clock.read_clock(info)
    if clock is None:
        return {
            "enabled": True,
            "initialized": False,
            "summary": "游戏时钟已启用但尚未初始化；首次推进后才有具体时间。",
        }
    return {
        "enabled": True,
        "initialized": True,
        **clock,
        "summary": room_clock.describe(info),
    }


GAME_CLOCK_TOOL = AgentTool(
    name="room.game_clock",
    description=(
        "只读查询本房间的游戏时间（服务器时间口径）与累计推进量。"
        "你不能推进或修改游戏时间；需要时间流逝时请让房主 / 管理员在房间控制台执行 time advance。"
    ),
    parameters={"type": "object", "properties": {}},
    handler=game_clock_tool,
)

GAME_CLOCK_TOOL_NAMES = frozenset({GAME_CLOCK_TOOL.name})
GAME_CLOCK_TOOLS = (GAME_CLOCK_TOOL,)
