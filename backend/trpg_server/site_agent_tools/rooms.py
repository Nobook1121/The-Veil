"""房间相关工具：快照、角色记录、房间记忆与剧本知识检索。

房间类工具只在服务端确认用户当前停留在该房间（socket 在线成员）时才会暴露。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server.agents.knowledge_base import search as knowledge_search
from trpg_server.agents.memory import read_room_memory
from trpg_server.json_store import read_json
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema
from trpg_server.settings import ROOMS_DIR, SCENARIOS_DIR


def _info(context: ToolContext) -> dict[str, Any]:
    room_dir = context.room_dir()
    if room_dir is None:
        return {}
    data = read_json(room_dir / "info.json", default={})
    return data if isinstance(data, dict) else {}


def _member_summary(member: dict[str, Any]) -> dict[str, Any]:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    return {
        "user_id": member.get("user_id"),
        "username": member.get("username"),
        "room_role": member.get("room_role"),
        "is_active": bool(member.get("is_active")),
        "character_id": card.get("id"),
        "character_name": card.get("name"),
        "current_hp": state.get("current_hp"),
        "current_san": state.get("current_san"),
    }


def get_room_snapshot(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    info = _info(context)
    members = info.get("members") if isinstance(info.get("members"), list) else []
    snapshot = {
        "room_id": info.get("id") or context.room_id,
        "title": info.get("name"),
        "scenario_id": info.get("scenario_id"),
        "scenario_title": info.get("scenario_title"),
        "creator_name": info.get("creator_name"),
        "members": [_member_summary(member) for member in members if isinstance(member, dict)],
        "updated_at": info.get("updated_at"),
    }
    return ToolResult.success(snapshot, summary=f"读取房间「{snapshot['title']}」快照")


def get_character_record(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    info = _info(context)
    target_id = str(arguments.get("target_user_id") or actor.user_id)
    members = info.get("members") if isinstance(info.get("members"), list) else []
    for member in members:
        if not isinstance(member, dict) or str(member.get("user_id")) != target_id:
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
        return ToolResult.success(
            {"username": member.get("username"), "character_card": card, "character_state": state},
            summary=f"读取 {member.get('username')} 的角色记录",
        )
    return ToolResult.failure("该用户不在当前房间或没有角色记录")


def get_memory(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    result = read_room_memory(
        {"query": arguments.get("query") or "", "limit": arguments.get("limit") or 10},
        context,
    )
    if isinstance(result, dict) and result.get("error"):
        return ToolResult.failure(str(result["error"]))
    return ToolResult.success(result, summary="读取房间记忆")


def scenario_search(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    info = _info(context)
    is_room_owner = str(info.get("creator_id")) == str(actor.user_id)
    for member in info.get("members") or []:
        if isinstance(member, dict) and str(member.get("user_id")) == str(actor.user_id):
            is_room_owner = is_room_owner or str(member.get("room_role")) == "owner"
    audience = "kp" if (is_room_owner or actor.is_admin) else "player"
    results = knowledge_search(
        str(context.room_id),
        str(arguments.get("query") or ""),
        rooms_dir=Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR)),
        scenarios_dir=Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)),
        top_k=max(1, min(int(arguments.get("top_k") or 5), 8)),
        audience=audience,
    )
    return ToolResult.success(
        {"items": results, "audience": audience},
        summary=f"检索到 {len(results)} 条剧本知识",
    )


def _check_other_member(actor: Any, arguments: dict[str, Any]) -> str | None:
    target = str(arguments.get("target_user_id") or actor.user_id)
    if target != str(actor.user_id) and not actor.has("rooms.manage_members"):
        return "只有房间管理员可以读取其他成员的角色记录"
    return None


ROOM_TOOLS = [
    ToolSpec(
        name="room.get_room_snapshot",
        description="读取当前房间的标题、剧本与成员摘要（不包含邮箱、令牌等敏感字段）。",
        parameters=schema({}),
        risk="read",
        scope="room",
        handler=get_room_snapshot,
    ),
    ToolSpec(
        name="room.get_character_record",
        description="读取当前房间内自己（或管理员指定成员）的角色卡与状态。",
        parameters=schema(
            {
                "target_user_id": {"type": "integer"},
            }
        ),
        risk="read",
        scope="room",
        required_permission="rooms.runtime_records",
        handler=get_character_record,
        check=_check_other_member,
    ),
    ToolSpec(
        name="room.get_memory",
        description="读取当前房间的 Agent 长期记忆，可按关键词过滤。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 120},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        scope="room",
        required_permission="rooms.runtime_records",
        handler=get_memory,
    ),
    ToolSpec(
        name="room.scenario_search",
        description="在当前房间绑定的剧本知识库中检索片段；普通玩家只能看到玩家可见内容。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 300},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            ["query"],
        ),
        risk="read",
        scope="room",
        handler=scenario_search,
    ),
]
