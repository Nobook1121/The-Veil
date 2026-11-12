"""房间成员路由：绑定角色卡、移除成员、转让房主、退出、调整房管。"""
from flask import request
from flask import session
from trpg_server.logging_config import user_action_text
from trpg_server.responses import error_response
from trpg_server.responses import success_response
from trpg_server.room_card_occupancy import card_binding_lock
from trpg_server.room_card_occupancy import card_id_of
from trpg_server.room_log import log_room_event
from trpg_server.room_ops.access import (
    _assign_room_owner,
    _can_access,
    _can_bind_character_card,
    _can_manage_members,
    _denied,
    _earliest_active_member,
    _emit_room_members_changed,
    _find_member,
    _is_active_member,
    _is_elevated,
    _require_login,
)
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.cards import (
    _bind_character,
    _card_in_use_response,
    _find_card_conflict,
    _pending_room_changes,
)
from trpg_server.room_ops.constants import (
    ROOM_MEMBER_REMOVED,
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    ROOM_ROLE_OWNER,
)
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _find_room,
    _read_room,
    _timestamp,
    _write_room,
)
from trpg_server.room_ops.views import _room_summary
from trpg_server.room_ops.visibility import visible_messages

@bp.route("/api/rooms/<room_id>/members/<user_id>/character", methods=["PUT"])
def bind_room_member_character(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if str(target.get("user_id")) != str(session["user_id"]) and not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    character_card = data.get("character_card")
    if not _can_bind_character_card(character_card, target):
        return _denied(room_id)
    if not _bind_character(target, character_card):
        return error_response("Character card is required", 400, "Character card is required")
    # 换卡同样是「绑定」，要在同一把占用锁里校验并落盘，否则先换卡再加入别的房间会绕过校验。
    with card_binding_lock():
        conflict = _find_card_conflict(target.get("character_card"), exclude_room_id=room_id)
        if conflict:
            return _card_in_use_response(conflict)
        _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "绑定了房间角色卡"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        角色卡ID=target.get("character_card", {}).get("id"),
        角色名=target.get("character_card", {}).get("name"),
    )
    data = _room_summary(info)
    data["messages"] = visible_messages(room_dir, info, session.get("user_id"))
    return success_response(data, "Character bound successfully")

@bp.route("/api/rooms/<room_id>/members/<user_id>", methods=["DELETE"])
def delete_room_member(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner cannot be removed", 400, "Room owner cannot be removed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    target["status"] = ROOM_MEMBER_REMOVED
    target["is_active"] = False
    target["removed_at"] = _timestamp()
    if target.get("room_role") == ROOM_ROLE_ADMIN:
        target["room_role"] = ROOM_ROLE_MEMBER
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "移除了房间成员"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        房间名=info.get("name"),
    )
    return success_response(_room_summary(info), "Player removed")

@bp.route("/api/rooms/<room_id>/owner", methods=["PUT"])
def transfer_room_owner(room_id):
    """显式移交房主：房主本人或站点管理员把房主交给另一位活跃成员。

    移交后原房主降为普通成员，双方都继续留在房间；目标必须是当前活跃成员，
    不能是自己，也不能是当前房主。
    """
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, _ = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")

    data = request.get_json(silent=True) or {}
    target_user_id = str(data.get("user_id") or "").strip()
    if not target_user_id:
        return error_response("Target user is required", 400, "invalid_target")

    requester_id = str(session["user_id"])
    if target_user_id == requester_id:
        return error_response("Cannot transfer room ownership to yourself", 400, "cannot_transfer_to_self")

    # 与加入/换卡/退出共用同一把占用锁，避免读到并改写中间态。
    with card_binding_lock():
        info = _read_room(room_dir)
        current_owner_id = str(info.get("creator_id") or "")
        if not (_is_elevated() or current_owner_id == requester_id):
            return _denied(room_id)
        if target_user_id == current_owner_id:
            return error_response("Target is already the room owner", 400, "already_room_owner")
        target = _find_member(info, user_id=target_user_id, active_only=True)
        if not target:
            return error_response("Player not found in room", 404, "Player not found in room")
        _assign_room_owner(info, target, previous_user_id=current_owner_id)
        _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), f"把房主移交给了 {target.get('username') or target_user_id}"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        原房主=current_owner_id,
        新房主=target_user_id,
        房间名=info.get("name"),
    )
    _emit_room_members_changed(info.get("id") or room_id)
    return success_response(_room_summary(info), "Room owner transferred")

@bp.route("/api/rooms/<room_id>/leave", methods=["POST"])
def leave_room(room_id):
    """玩家自助退出房间：只移除自己，不放宽房主/管理员的移除权限。

    退出即释放角色卡占用（占用按活跃成员现算）；若还有未归档的待写回改动，
    必须先由前端二次确认再带 ``confirm_pending`` 重试，避免静默丢弃。
    房主未显式移交就退出时，自动移交给加入时间最早的活跃成员；没有其他
    活跃成员时允许退出，房间保留。
    """
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, _ = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")

    data = request.get_json(silent=True) or {}
    confirm_pending = bool(data.get("confirm_pending"))
    user_id = session["user_id"]
    heir = None

    # 与加入/换卡共用同一把占用锁：退出写盘与别处的占用校验串行，避免并发读到中间态。
    with card_binding_lock():
        info = _read_room(room_dir)
        member = _find_member(info, user_id=user_id, active_only=True)
        # 重复退出保持幂等：已经不在房间里时直接按成功返回。
        if not member:
            return success_response({"room_id": info.get("id"), "already_left": True}, "Already left room")

        others = [
            row
            for row in info.get("members", [])
            if _is_active_member(row) and str(row.get("user_id")) != str(user_id)
        ]
        # 房主未显式移交就退出：自动交给加入时间最早的活跃成员，并在房间日志里说明原因。
        if str(info.get("creator_id")) == str(user_id) and others:
            heir = _earliest_active_member(info, exclude_user_id=user_id)
            if heir is not None:
                _assign_room_owner(info, heir, previous_user_id=user_id)

        pending_count = _pending_room_changes(member)
        if pending_count and not confirm_pending:
            return error_response(
                "本房间内的改动尚未归档，退出后不会生效",
                409,
                "pending_changes_unarchived",
                pending_changes=pending_count,
            )

        card_id = card_id_of(member.get("character_card"))
        member["status"] = ROOM_MEMBER_REMOVED
        member["is_active"] = False
        member["removed_at"] = _timestamp()
        if member.get("room_role") in {ROOM_ROLE_ADMIN, ROOM_ROLE_OWNER}:
            member["room_role"] = ROOM_ROLE_MEMBER
        _write_room(room_dir, info)

    heir_username = str((heir or {}).get("username") or "")
    leave_action = (
        f"退出了房间，房主自动移交给加入时间最早的活跃成员 {heir_username}"
        if heir
        else "退出了房间"
    )
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), leave_action),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=user_id,
        房间名=info.get("name"),
        角色卡ID=card_id,
        未归档改动=pending_count,
        新房主=heir_username,
    )
    _emit_room_members_changed(info.get("id") or room_id)
    return success_response(
        {
            "room_id": info.get("id"),
            "pending_changes": pending_count,
            "released_card_id": card_id,
            "owner_transferred_to": str((heir or {}).get("user_id") or "") or None,
        },
        "Left room",
    )

@bp.route("/api/rooms/<room_id>/members/<user_id>/role", methods=["PUT"])
def update_room_member_role(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner role cannot be changed", 400, "Room owner role cannot be changed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    data = request.get_json(silent=True) or {}
    role = data.get("room_role")
    if role not in {ROOM_ROLE_ADMIN, ROOM_ROLE_MEMBER}:
        return error_response("Invalid room role", 400, "Invalid room role")
    target["room_role"] = role
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "更改了房间成员权限"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        权限=role,
    )
    return success_response(_room_summary(info), "Room role updated")
