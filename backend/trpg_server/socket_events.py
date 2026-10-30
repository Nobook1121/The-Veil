import logging
from typing import Any, cast

from flask import current_app, request, session
from flask_socketio import disconnect, emit, join_room, leave_room

from trpg_server.logging_config import user_action_text
from trpg_server.room_access import can_access, find_room
from trpg_server.room_log import log_room_event, set_room_log_broadcaster
from trpg_server.security import (
    USER_ROOM_MEMBERSHIP_KEY,
    is_user_in_room,
    session_is_current,
)

logger = logging.getLogger(__name__)

# 记录每个用户当前加入的 Socket.IO 房间，便于断线时通知相关房间刷新成员在线状态。
# 常量定义在 security 中（房间内在线/自动存档判定也要读它），这里直接复用避免重复。


def register_socket_events(socketio):
    # 每次 create_app 都会通过 init_app 重建底层 server，因此事件必须重新绑定；
    # 已注册标记会让第二个 app（测试/多实例）丢失全部 Socket 处理器。
    # 房间日志实时广播：新增条目推送给房间内在线用户，驱动内嵌 Web 控制台。
    set_room_log_broadcaster(
        lambda room_id, entry: socketio.emit(
            "room_console_log",
            {"room_id": room_id, "type": "append", "entry": entry},
            room=room_id,
        )
    )

    # 玩家投掷：广播待投掷请求、结果、取消与强制结算。
    from trpg_server.agents import player_roll

    player_roll.register_broadcaster(
        lambda event, payload: socketio.emit(event, payload, room=payload.get("room_id"))
    )

    def _room_membership():
        return current_app.config.setdefault(USER_ROOM_MEMBERSHIP_KEY, {})

    def _add_room_for_socket(user_key, sid, room_id):
        """membership 按 socket 记录，避免多标签页互相覆盖。"""
        membership = _room_membership()
        entry = membership.get(user_key)
        if not isinstance(entry, dict):
            entry = {}
            membership[user_key] = entry
        entry.setdefault(sid, set()).add(room_id)

    def _remove_room_for_socket(user_key, sid, room_id):
        membership = _room_membership()
        entry = membership.get(user_key)
        if isinstance(entry, dict):
            rooms = entry.get(sid)
            if isinstance(rooms, set):
                rooms.discard(room_id)
                if not rooms:
                    entry.pop(sid, None)
            if not entry:
                membership.pop(user_key, None)
            return
        # 兼容旧结构：按用户维护的房间集合。
        if isinstance(entry, set):
            entry.discard(room_id)
            if not entry:
                membership.pop(user_key, None)

    def _drop_socket_rooms(user_key, sid, was_active):
        membership = _room_membership()
        entry = membership.get(user_key)
        if isinstance(entry, dict):
            rooms = entry.pop(sid, set())
            if not entry:
                membership.pop(user_key, None)
            return set(rooms)
        # 兼容旧结构：新连接已接管时，旧连接的断开不应把用户标记为离线。
        return set(membership.pop(user_key, set())) if was_active else set()

    def _notify_room_members_changed(room_id):
        # 广播「成员列表已变化」：客户端据此重新拉取房间，刷新在线状态与角色配色。
        if room_id:
            socketio.emit("room_members_changed", {"room_id": room_id}, room=room_id)

    def _session_is_current():
        return session_is_current()

    def _replace_active_socket():
        active_sockets = current_app.config.setdefault("ACTIVE_SOCKET_SESSIONS", {})
        user_key = str(session["user_id"])
        previous_sid = active_sockets.get(user_key)
        # flask_socketio 握手时把 sid 注入 flask.request，但 Flask 的类型声明里没有该属性。
        if previous_sid and previous_sid != cast(Any, request).sid:
            socketio.emit("session_expired", room=f"user:{user_key}", namespace="/")
        active_sockets[user_key] = cast(Any, request).sid

    @socketio.on("connect")
    def handle_connect():
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return False
        _replace_active_socket()
        join_room(f"user:{session['user_id']}")
        logger.debug("WebSocket client connected user_id=%s", session.get("user_id"))

    @socketio.on("disconnect")
    def handle_disconnect():
        if "user_id" in session:
            user_key = str(session["user_id"])
            sid = cast(Any, request).sid
            active_sockets = current_app.config.setdefault("ACTIVE_SOCKET_SESSIONS", {})
            was_active = active_sockets.get(user_key) == sid
            if was_active:
                active_sockets.pop(user_key, None)
            # 只清理这个连接加入的房间；同一用户的其他标签页仍在线时，
            # 用户依旧算在房间内，不会在右侧栏被误判为离线。
            for room_id in _drop_socket_rooms(user_key, sid, was_active):
                _notify_room_members_changed(room_id)
        logger.debug("WebSocket client disconnected")

    @socketio.on("join_room")
    def handle_join_room(data):
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return

        room_id = str((data or {}).get("room_id") or "").strip()
        if not room_id:
            emit("room_error", {"code": "room_required", "message": "缺少房间 ID"})
            return

        room_dir, info = find_room(room_id)
        if room_dir is None or not can_access(info, session.get("user_id"), session.get("role", "USER")):
            # 不暴露房间是否存在，统一拒绝；避免任意登录用户进入他人的实时通道。
            emit("room_error", {"code": "room_access_denied", "message": "无法进入该房间"})
            return

        join_room(room_id)
        _add_room_for_socket(str(session["user_id"]), cast(Any, request).sid, room_id)
        log_room_event(
            message=user_action_text(session.get("username"), "加入了房间"),
            actor=session.get("username"),
            room_id=room_id,
            rooms_dir=current_app.config.get("ROOMS_DIR"),
            logger_=logger,
            用户ID=session.get("user_id"),
        )
        # 用户刚上线/进入房间，通知房间内其他人刷新成员列表。
        _notify_room_members_changed(room_id)

    @socketio.on("leave_room")
    def handle_leave_room(data):
        room_id = str((data or {}).get("room_id") or "").strip()
        if room_id:
            leave_room(room_id)
            if "user_id" in session:
                _remove_room_for_socket(str(session["user_id"]), cast(Any, request).sid, room_id)
            log_room_event(
                message=user_action_text(session.get("username"), "离开了房间"),
                actor=session.get("username"),
                room_id=room_id,
                rooms_dir=current_app.config.get("ROOMS_DIR"),
                logger_=logger,
                用户ID=session.get("user_id"),
            )
            _notify_room_members_changed(room_id)

    @socketio.on("send_message")
    def handle_send_message(data):
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return

        payload = data or {}
        room_id = str(payload.get("room_id") or "").strip()
        # 只允许在当前已加入的房间内发言；缺省不再广播到所有在线用户。
        if room_id and is_user_in_room(session.get("user_id"), room_id):
            socketio.emit("new_message", payload, room=room_id, include_self=False)

    @socketio.on("typing")
    def handle_typing(data):
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return

        room_id = str((data or {}).get("room_id") or "").strip()
        if room_id and is_user_in_room(session.get("user_id"), room_id):
            socketio.emit("user_typing", data, room=room_id, include_self=False)
