"""AI KP 的幕间入口：只能提议，不能自行拍板进入或结束幕间。"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool, ToolExecutionError
from trpg_server.room_interlude import InterludeError, add_review, request_interlude
from trpg_server.room_rules import load_room_info


def _room_dir(context: Any):
    return getattr(context, "room_dir", None)


def _require_room(context: Any):
    room_dir = _room_dir(context)
    if room_dir is None:
        raise ToolExecutionError("当前不在可用的房间目录中")
    return room_dir, load_room_info(room_dir)


def request_interlude_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir, info = _require_room(context)
    try:
        record = request_interlude(
            room_dir,
            info,
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
            source="ai",
            reason=str(arguments.get("reason") or "").strip(),
        )
    except InterludeError as exc:
        return {"error": exc.message}
    return {
        "status": "requested",
        "message": "已登记进入幕间的提议；必须由房主或房间管理员确认后才会进入幕间。",
        "request": record,
    }


def save_interlude_review_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir, info = _require_room(context)
    content = str(arguments.get("content") or "").strip()
    if not content:
        raise ToolExecutionError("content is required")
    try:
        record = add_review(
            room_dir,
            info,
            content=content,
            topic=str(arguments.get("topic") or ""),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
            source="ai",
        )
    except InterludeError as exc:
        return {"error": exc.message}
    return {"status": "saved", "review": record}


REQUEST_INTERLUDE_TOOL = AgentTool(
    name="room.request_interlude",
    description=(
        "剧情已经结束时，向房主 / 管理员提议进入幕间。你只能提议，不能自行进入幕间；"
        "玩家明确表示要结束剧情或进入幕间时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "提议理由，简述剧情为何已经结束"},
        },
        "required": ["reason"],
    },
    handler=request_interlude_tool,
)

SAVE_INTERLUDE_REVIEW_TOOL = AgentTool(
    name="room.save_interlude_review",
    description=(
        "幕间期间保存一段复盘记录。内容必须基于当前提供的已落盘摘要 / 关键帧，"
        "不得编造未出现过的剧情。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "复盘主题，例如「调查过程」「角色成长」"},
            "content": {"type": "string", "description": "复盘正文，直接面向玩家"},
        },
        "required": ["content"],
    },
    handler=save_interlude_review_tool,
)


INTERLUDE_TOOLS = [REQUEST_INTERLUDE_TOOL, SAVE_INTERLUDE_REVIEW_TOOL]
INTERLUDE_TOOL_NAMES = [tool.name for tool in INTERLUDE_TOOLS]

__all__ = [
    "INTERLUDE_TOOLS",
    "INTERLUDE_TOOL_NAMES",
    "REQUEST_INTERLUDE_TOOL",
    "SAVE_INTERLUDE_REVIEW_TOOL",
]
