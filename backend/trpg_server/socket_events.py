import logging

from flask import current_app, request, session
from flask_socketio import disconnect, emit, join_room, leave_room

from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.room_log import log_room_event, set_room_log_broadcaster
from trpg_server.security import (
    ACTIVE_SESSION_REGISTRY_KEY,
    SESSION_TOKEN_KEY,
    USER_ROOM_MEMBERSHIP_KEY,
)

logger = logging.getLogger(__name__)

# 记录每个用户当前加入的 Socket.IO 房间，便于断线时通知相关房间刷新成员在线状态。
# 常量定义在 security 中（房间内在线/自动存档判定也要读它），这里直接复用避免重复。


def register_socket_events(socketio):
    if getattr(socketio, "_trpg_events_registered", False):
        return

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

    def _notify_room_members_changed(room_id):
        # 广播「成员列表已变化」：客户端据此重新拉取房间，刷新在线状态与角色配色。
        if room_id:
            socketio.emit("room_members_changed", {"room_id": room_id}, room=room_id)

    def _session_is_current():
        if "user_id" not in session:
            return False
        active_sessions = current_app.config.setdefault(ACTIVE_SESSION_REGISTRY_KEY, {})
        active_token = active_sessions.get(str(session["user_id"]))
        return not active_token or session.get(SESSION_TOKEN_KEY) == active_token

    def _replace_active_socket():
        active_sockets = current_app.config.setdefault("ACTIVE_SOCKET_SESSIONS", {})
        user_key = str(session["user_id"])
        previous_sid = active_sockets.get(user_key)
        if previous_sid and previous_sid != request.sid:
            socketio.emit("session_expired", room=f"user:{user_key}", namespace="/")
        active_sockets[user_key] = request.sid

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
            active_sockets = current_app.config.setdefault("ACTIVE_SOCKET_SESSIONS", {})
            if active_sockets.get(user_key) == request.sid:
                active_sockets.pop(user_key, None)
            # 断开后该用户变为离线，通知其所在房间刷新成员在线状态。
            membership = _room_membership()
            for room_id in membership.pop(user_key, set()):
                _notify_room_members_changed(room_id)
        logger.debug("WebSocket client disconnected")

    @socketio.on("join_room")
    def handle_join_room(data):
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return

        room_id = (data or {}).get("room_id")
        if room_id:
            join_room(room_id)
            if "user_id" in session:
                membership = _room_membership()
                membership.setdefault(str(session["user_id"]), set()).add(room_id)
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
        room_id = (data or {}).get("room_id")
        if room_id:
            leave_room(room_id)
            if "user_id" in session:
                membership = _room_membership()
                membership.get(str(session["user_id"]), set()).discard(room_id)
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
        message = payload.get("message") or payload
        room_id = payload.get("room_id")
        if room_id:
            emit("new_message", payload, room=room_id, include_self=False)
        else:
            emit("new_message", payload, broadcast=True, include_self=False)

    @socketio.on("typing")
    def handle_typing(data):
        if not _session_is_current():
            emit("session_expired")
            disconnect()
            return

        room_id = (data or {}).get("room_id")
        if room_id:
            emit("user_typing", data, room=room_id, include_self=False)
        else:
            emit("user_typing", data, broadcast=True, include_self=False)

    socketio._trpg_events_registered = True
