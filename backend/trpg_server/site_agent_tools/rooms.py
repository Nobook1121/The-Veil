"""房间相关工具：快照、角色记录、房间记忆与剧本知识检索。

房间类工具只在服务端确认用户当前停留在该房间（socket 在线成员）时才会暴露。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server.agents.knowledge_base import search as knowledge_search
from trpg_server.agents.memory import read_room_memory
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.logging_config import user_action_text
from trpg_server.room_log import log_room_event
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema
from trpg_server.settings import ROOMS_DIR, SCENARIOS_DIR

logger = logging.getLogger(__name__)


def _info(context: ToolContext) -> dict[str, Any]:
    room_dir = context.room_dir()
    if room_dir is None:
        return {}
    data = read_json(room_dir / "info.json", default={})
    return data if isinstance(data, dict) else {}


def _is_room_manager(actor: Any, info: dict[str, Any]) -> bool:
    if actor.is_admin or str(info.get("creator_id")) == str(actor.user_id):
        return True
    for member in info.get("members") or []:
        if isinstance(member, dict) and str(member.get("user_id")) == str(actor.user_id):
            if bool(member.get("is_active")) and str(member.get("room_role")) in {"owner", "admin"}:
                return True
    return False


def _check_manager(actor: Any, info: dict[str, Any]) -> str | None:
    if not _is_room_manager(actor, info):
        return "只有房主或房间管理员可以管理房间消息记录"
    return None


def _messages(room_dir: Path) -> list[dict[str, Any]]:
    data = read_json(room_dir / "messages.json", default=[])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _message_view(message: dict[str, Any], index: int) -> dict[str, Any]:
    content = str(message.get("content") or "")
    return {
        "index": index,
        "id": message.get("id") or "",
        "type": message.get("type") or "",
        "sender_name": message.get("sender_name") or "",
        "content": content if len(content) <= 500 else f"{content[:500]}…",
        "time": message.get("time") or "",
        "created_at": message.get("created_at") or "",
    }


def _emit_messages_changed(context: ToolContext) -> None:
    try:
        socketio = current_app.extensions.get("socketio")
        if socketio and context.room_id:
            socketio.emit("room_messages_changed", {"room_id": context.room_id}, room=context.room_id)
    except Exception:
        pass



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


def get_message_history(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    room_dir = context.room_dir()
    if room_dir is None:
        return ToolResult.failure("请先进入房间")
    messages = _messages(room_dir)
    query = str(arguments.get("query") or "").strip().lower()
    sender = str(arguments.get("sender") or "").strip().lower()
    limit = max(1, min(int(arguments.get("limit") or 20), 50))
    matched: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        view = _message_view(message, index)
        if query and query not in view["content"].lower():
            continue
        if sender and sender not in view["sender_name"].lower():
            continue
        matched.append(view)
    selected = matched[-limit:]
    return ToolResult.success(
        {
            "room_id": context.room_id,
            "total_messages": len(messages),
            "total_matches": len(matched),
            "items": selected,
            "truncated": len(matched) > len(selected),
        },
        summary=f"读取房间消息：匹配 {len(matched)} 条，返回最近 {len(selected)} 条",
    )


def _find_message(messages: list[dict[str, Any]], message_id: str) -> tuple[int, dict[str, Any]] | None:
    for index, message in enumerate(messages):
        if str(message.get("id") or "") == message_id:
            return index, message
    return None


def _manager_error(actor: Any, context: ToolContext) -> str | None:
    return _check_manager(actor, _info(context))


def preview_delete_message(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    denied = _manager_error(actor, context)
    if denied:
        return ToolResult.failure(denied)
    room_dir = context.room_dir()
    if room_dir is None:
        return ToolResult.failure("请先进入房间")
    message_id = str(arguments.get("message_id") or "").strip()
    messages = _messages(room_dir)
    found = _find_message(messages, message_id)
    if not found:
        return ToolResult.failure("没有找到这条消息，可能已被删除")
    index, message = found
    view = _message_view(message, index)
    summary = f"将删除 {view['sender_name'] or '未知发送者'} 在 {view['created_at'] or view['time']} 的消息"
    return ToolResult.success(
        {"message": view, "summary": summary, "apply_args": {"message_id": message_id}},
        summary=summary,
    )


def apply_delete_message(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    denied = _manager_error(actor, context)
    if denied:
        return ToolResult.failure(denied)
    room_dir = context.room_dir()
    if room_dir is None:
        return ToolResult.failure("请先进入房间")
    message_id = str(arguments.get("message_id") or "").strip()
    messages = _messages(room_dir)
    found = _find_message(messages, message_id)
    if not found:
        return ToolResult.failure("没有找到这条消息，可能已被删除")
    index, message = found
    messages.pop(index)
    write_json_atomic(room_dir / "messages.json", messages)
    log_room_event(
        room_dir,
        user_action_text(actor.username, "通过网站 Agent 删除了一条房间消息"),
        actor=actor.username,
        room_id=context.room_id,
        logger_=__import__("logging").getLogger(__name__),
        用户ID=actor.user_id,
        消息ID=message_id,
        消息类型=message.get("type") or "",
        发送者=message.get("sender_name") or "",
    )
    _emit_messages_changed(context)
    return ToolResult.success(
        {"message_id": message_id, "remaining": len(messages)},
        summary=f"已删除消息，房间剩余 {len(messages)} 条消息",
    )


def preview_clear_messages(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    denied = _manager_error(actor, context)
    if denied:
        return ToolResult.failure(denied)
    room_dir = context.room_dir()
    if room_dir is None:
        return ToolResult.failure("请先进入房间")
    messages = _messages(room_dir)
    summary = f"将清空房间的全部 {len(messages)} 条消息记录，且无法恢复"
    return ToolResult.success(
        {"total_messages": len(messages), "summary": summary, "apply_args": {}},
        summary=summary,
    )


def apply_clear_messages(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    denied = _manager_error(actor, context)
    if denied:
        return ToolResult.failure(denied)
    room_dir = context.room_dir()
    if room_dir is None:
        return ToolResult.failure("请先进入房间")
    total = len(_messages(room_dir))
    write_json_atomic(room_dir / "messages.json", [])
    write_json_atomic(
        room_dir / "autosave.json",
        {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "messages": []},
    )
    log_room_event(
        room_dir,
        user_action_text(actor.username, "通过网站 Agent 清空了房间消息记录"),
        actor=actor.username,
        room_id=context.room_id,
        logger_=logger,
        用户ID=actor.user_id,
        消息数量=total,
    )
    _emit_messages_changed(context)
    return ToolResult.success({"cleared": total}, summary=f"已清空 {total} 条房间消息")


def _check_message_target(actor: Any, arguments: dict[str, Any]) -> str | None:
    # message_id 由预览时生成；这里只校验格式，房主/管理员身份在 preview/apply 内校验。
    if not str(arguments.get("message_id") or "").strip():
        return "缺少 message_id"
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
    ToolSpec(
        name="room.get_message_history",
        description="读取或搜索当前房间的消息记录（可按关键词、发送者过滤，最多返回最近 50 条）。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 200},
                "sender": {"type": "string", "maxLength": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        scope="room",
        required_permission="rooms.runtime_records",
        handler=get_message_history,
    ),
    ToolSpec(
        name="room.delete_message",
        description="删除当前房间的一条消息记录。仅房主或房间管理员可用，执行前会生成待确认操作。",
        parameters=schema({"message_id": {"type": "string", "maxLength": 80}}, ["message_id"]),
        risk="danger",
        scope="room",
        required_permission="rooms.runtime_records",
        preview=preview_delete_message,
        apply=apply_delete_message,
        check=_check_message_target,
    ),
    ToolSpec(
        name="room.clear_messages",
        description="清空当前房间的全部消息记录。仅房主或房间管理员可用，执行前会生成待确认操作。",
        parameters=schema({}),
        risk="danger",
        scope="room",
        required_permission="rooms.runtime_records",
        preview=preview_clear_messages,
        apply=apply_clear_messages,
    ),
]
