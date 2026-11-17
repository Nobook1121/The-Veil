"""AI 托管与 AI 玩家路由：添加/接管 AI 玩家、切换托管、触发 AI 行动。"""
from __future__ import annotations

import threading

from flask import current_app, request, session

from trpg_server.agents.player_agent import run_player_turn
from trpg_server.json_store import write_json_atomic
from trpg_server.logging_config import user_action_text
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.room_card_occupancy import card_binding_lock
from trpg_server.room_log import log_room_event
from trpg_server.room_ops.access import (
    _can_access,
    _can_bind_character_card,
    _can_manage_members,
    _denied,
    _emit_room_members_changed,
    _find_member,
    _require_login,
)
from trpg_server.room_ops.ai_players import (
    ai_controlled,
    cooldown_remaining,
    is_ai_player,
    make_ai_player_member,
    mark_turn_started,
    persist_player_turn,
    set_control_mode,
    turn_lock,
)
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.cards import _find_card_conflict
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import _find_room, _read_room, _timestamp, _write_room
from trpg_server.room_ops.views import _room_summary

_HISTORY_LOCK = threading.Lock()


def _room_not_archived(info):
    return not bool(info.get("archived"))


def _emit_new_messages(room_id, messages):
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    for message in messages:
        socketio.emit("new_message", message, room=str(room_id))


def _append_to_kp_history(room_id, member, content):
    """把 AI 玩家发言写入 KP 的房间历史，保证下一轮 KP 能看到这一行动。"""
    text = str(content or "").strip()
    if not text:
        return
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    display = str(card.get("name") or member.get("username") or "玩家")
    speaker = {
        "user_id": member.get("user_id"),
        "username": str(member.get("username") or display),
        "character_name": str(card.get("name") or ""),
        "ai_controlled": True,
    }
    from trpg_server.routes.chat_helpers import _load_history

    with _HISTORY_LOCK:
        history_file, history = _load_history(None, room_id=room_id, agent_id="kp")
        history = history if isinstance(history, list) else []
        history.append({"role": "user", "content": f"{display}：{text}", "speaker": speaker})
        write_json_atomic(history_file, history[-40:])


@bp.route("/api/rooms/<room_id>/ai-players", methods=["POST"])
def create_ai_player(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if not _room_not_archived(info):
        return error_response("Room is archived", 403, "Room is archived")

    data = request.get_json(silent=True) or {}
    character_card = data.get("character_card")
    probe_target = {"is_ai_player": True, "user_id": "", "username": ""}
    if not _can_bind_character_card(character_card, probe_target, info):
        return _denied(room_id)

    member = make_ai_player_member(
        character_card,
        created_by=session.get("user_id"),
        username=data.get("username"),
        avatar=str(data.get("avatar") or ""),
    )
    if member is None:
        return error_response("Character card is required", 400, "Character card is required")

    with card_binding_lock():
        fresh = _read_room(room_dir)
        if not _room_not_archived(fresh):
            return error_response("Room is archived", 403, "Room is archived")
        if not _can_manage_members(fresh):
            return _denied(room_id)
        conflict = _find_card_conflict(member.get("character_card"), exclude_room_id=room_id)
        if conflict:
            from trpg_server.room_ops.cards import _card_in_use_response

            return _card_in_use_response(conflict)
        fresh.setdefault("members", []).append(member)
        _write_room(room_dir, fresh)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "添加了 AI 玩家"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        角色卡ID=(member.get("character_card") or {}).get("id"),
        角色名=(member.get("character_card") or {}).get("name"),
    )
    _emit_room_members_changed(room_id)
    return success_response(_room_summary(fresh), "AI player created", 201)


@bp.route("/api/rooms/<room_id>/members/<user_id>/ai-control", methods=["PUT"])
def update_member_ai_control(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _room_not_archived(info):
        return error_response("Room is archived", 403, "Room is archived")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    is_self = str(user_id) == str(session.get("user_id"))
    if not is_self and not _can_manage_members(info):
        return _denied(room_id)
    if is_ai_player(target):
        return error_response("AI player is always AI-controlled", 400, "ai_player_always_ai")
    if not target.get("character_card"):
        return error_response("Bind a character card before enabling AI hosting", 400, "character_card_required")

    data = request.get_json(silent=True) or {}
    mode = "ai" if bool(data.get("enabled")) else "human"
    with card_binding_lock():
        fresh = _read_room(room_dir)
        fresh_target = _find_member(fresh, user_id=user_id, active_only=True)
        if not fresh_target:
            return error_response("Player not found in room", 404, "Player not found in room")
        if not set_control_mode(fresh_target, mode, actor=session.get("user_id")):
            return error_response("Invalid control mode", 400, "invalid_control_mode")
        if mode == "ai":
            fresh_target["ai_control"]["enabled_at"] = _timestamp()
            fresh_target["ai_control"]["enabled_by"] = session.get("user_id")
        _write_room(room_dir, fresh)

    action = "开启了 AI 托管" if mode == "ai" else "收回了角色控制权"
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), action),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        控制权=mode,
    )
    _emit_room_members_changed(room_id)
    return success_response(_room_summary(fresh), "AI control updated")


@bp.route("/api/rooms/<room_id>/members/<user_id>/ai-act", methods=["POST"])
def run_member_ai_turn(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _room_not_archived(info):
        return error_response("Room is archived", 403, "Room is archived")
    # 观战者可以看 AI 行动，但不能消耗房间的 AI 回合。
    if not _find_member(info, user_id=session.get("user_id"), active_only=True):
        return _denied(room_id)

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if not ai_controlled(target):
        return error_response("This member is not AI-controlled", 409, "not_ai_controlled")
    if not target.get("character_card"):
        return error_response("Character card is required", 400, "character_card_required")

    lock = turn_lock(room_id, user_id)
    if not lock.acquire(blocking=False):
        return error_response("AI turn already running", 409, "ai_turn_running")
    try:
        remaining = cooldown_remaining(room_id, user_id)
        if remaining > 0:
            return error_response(
                f"AI 行动过于频繁，请 {int(remaining) + 1} 秒后再试",
                429,
                "ai_turn_cooldown",
            )
        mark_turn_started(room_id, user_id)
        data = request.get_json(silent=True) or {}
        result = run_player_turn(
            room_id=str(room_id),
            room_dir=room_dir,
            info=info,
            member=target,
            prompt=str(data.get("prompt") or "").strip() or None,
        )
        if not result.get("ok"):
            return error_response(str(result.get("error") or "AI turn failed"), 400, "ai_turn_failed")
        created = persist_player_turn(
            room_dir,
            target,
            str(result.get("content") or ""),
            list(result.get("tool_messages") or []),
        )
        _append_to_kp_history(room_id, target, result.get("content"))
        _emit_new_messages(room_id, created)
        fresh = _read_room(room_dir)
        log_room_event(
            room_dir,
            f"AI 代打「{target.get('username')}」完成了一次行动",
            actor="AI 玩家",
            room_id=room_id,
            logger_=logger,
            source="ai_player",
            目标用户=user_id,
            消息数=len(created),
        )
        return success_response(
            {"messages": created, "room": _room_summary(fresh)},
            "AI turn completed",
        )
    except Exception as exc:  # noqa: BLE001 - AI 回合失败要返回可读错误，不落半条消息
        logger.exception("AI player turn failed room=%s member=%s", room_id, user_id)
        return server_error(f"AI turn failed: {exc}")
    finally:
        lock.release()


@bp.route("/api/rooms/<room_id>/ai-players/<user_id>/takeover", methods=["POST"])
def takeover_ai_player(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _room_not_archived(info):
        return error_response("Room is archived", 403, "Room is archived")

    requester_id = session.get("user_id")
    data = request.get_json(silent=True) or {}
    replace = bool(data.get("replace"))

    with card_binding_lock():
        fresh = _read_room(room_dir)
        target = _find_member(fresh, user_id=user_id, active_only=True)
        if not target or not is_ai_player(target):
            return error_response("AI player not found", 404, "AI player not found")
        requester = _find_member(fresh, user_id=requester_id, active_only=True)
        if not requester:
            return _denied(room_id)
        target_card = target.get("character_card") if isinstance(target.get("character_card"), dict) else {}
        current_card = requester.get("character_card") if isinstance(requester.get("character_card"), dict) else {}
        if current_card and str(current_card.get("id")) != str(target_card.get("id")) and not replace:
            return error_response(
                "你已绑定角色卡，确认后会用 AI 玩家的角色替换当前角色卡",
                409,
                "character_already_bound",
                current_character=current_card,
                target_character=target_card,
            )
        if target_card:
            requester["character_card"] = target_card
            if isinstance(target.get("character_state"), dict):
                requester["character_state"] = target["character_state"]
        requester["control_mode"] = "human"
        requester["ai_control"] = {
            "enabled": False,
            "updated_by": requester_id,
            "updated_at": _timestamp(),
        }
        target["status"] = "removed"
        target["is_active"] = False
        target["removed_at"] = _timestamp()
        target["taken_over_by"] = requester_id
        target["taken_over_at"] = _timestamp()
        # 角色卡与运行状态已经转给真人，避免归档时同一张卡写两次经历。
        target.pop("character_card", None)
        target.pop("character_state", None)
        _write_room(room_dir, fresh)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "接管了一个 AI 玩家角色"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=requester_id,
        AI玩家=user_id,
        角色卡ID=(requester.get("character_card") or {}).get("id"),
    )
    _emit_room_members_changed(room_id)
    return success_response(_room_summary(fresh), "AI player taken over")
