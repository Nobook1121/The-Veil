"""房间内嵌 Web 控制台的 REST 接口。

只暴露「本房间」的日志读取与房间域命令执行，权限复用 ``routes.rooms`` 的房间
角色判定（房主/房间管理员/系统管理员）。日志与命令结果都写入房间日志，实现
与 Web 控制台、房间归档三处一致。
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, request, session

from trpg_server.responses import error_response, success_response
from trpg_server.room_console_commands import (
    ROOM_COMMAND_WHITELIST,
    execute_room_command,
    room_command_help_text,
)
from trpg_server.room_log import (
    append_room_log,
    clear_room_log,
    read_room_log,
    room_console_settings,
)
from trpg_server.routes.rooms import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    ROOM_ROLE_OWNER,
    _denied,
    _find_member,
    _find_room,
    _is_elevated,
    _require_login,
    _room_permission,
)

bp = Blueprint("room_console", __name__)
logger = logging.getLogger(__name__)

_ROLE_RANK = {"": 0, ROOM_ROLE_MEMBER: 1, ROOM_ROLE_ADMIN: 2, ROOM_ROLE_OWNER: 3}
_ROLE_BY_NAME = {"member": 1, "admin": 2, "owner": 3}
_MAX_LOG_LIMIT = 500
# 已归档房间禁止的破坏性房间命令（domain, subcommand）。
_DESTRUCTIVE_AFTER_ARCHIVE = {("archive", ""), ("log", "clear")}


def _settings():
    return room_console_settings(current_app.config.get("CONFIG_DIR"))


def _current_rank(info):
    if _is_elevated():
        return _ROLE_RANK[ROOM_ROLE_OWNER]
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _ROLE_RANK.get(_room_permission(member, info), 0)


def _required_rank(role_name, default):
    return _ROLE_BY_NAME.get(str(role_name or default), _ROLE_BY_NAME[default])


def _parse_command(command):
    parts = str(command or "").split()
    domain = parts[0].lower() if parts else ""
    sub = parts[1].lower() if len(parts) > 1 else ""
    return domain, sub


def _broadcaster():
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return None

    def _send(room_id, entry):
        socketio.emit(
            "room_console_log",
            {"room_id": room_id, "type": "append", "entry": entry},
            room=room_id,
        )

    return _send


def _load_room_or_error(room_id):
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return None, None, error_response("Room not found", 404, "Room not found")
    return room_dir, info, None


@bp.route("/api/rooms/<room_id>/console/log", methods=["GET"])
def get_room_console_log(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room_or_error(room_id)
    if error:
        return error
    settings = _settings()
    if not settings["enabled"]:
        return error_response("Room console is disabled", 403, "Room console disabled")
    if _current_rank(info) < _required_rank(settings["view_role"], "member"):
        return _denied(room_id)

    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    data = read_room_log(
        room_dir,
        limit=max(1, min(limit, _MAX_LOG_LIMIT)),
        level=request.args.get("level"),
        search=request.args.get("search") or "",
    )
    return success_response(data, "Room console log loaded successfully")


@bp.route("/api/rooms/<room_id>/console/commands", methods=["GET"])
def get_room_console_commands(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room_or_error(room_id)
    if error:
        return error
    if _current_rank(info) < _required_rank(_settings()["view_role"], "member"):
        return _denied(room_id)
    return success_response(
        {"help": room_command_help_text(), "domains": sorted(ROOM_COMMAND_WHITELIST)},
        "Room console commands loaded successfully",
    )


@bp.route("/api/rooms/<room_id>/console/command", methods=["POST"])
def run_room_console_command(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room_or_error(room_id)
    if error:
        return error
    settings = _settings()
    if not settings["enabled"]:
        return error_response("Room console is disabled", 403, "Room console disabled")
    if _current_rank(info) < _required_rank(settings["execute_role"], "admin"):
        return _denied(room_id)

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command") or "").strip()
    if not command:
        return error_response("Please provide a command", 400, "No command provided")
    if info.get("archived") and _parse_command(command) in _DESTRUCTIVE_AFTER_ARCHIVE:
        return error_response(
            "Archived room cannot run destructive commands",
            409,
            "Room archived",
        )

    broadcast = _broadcaster()
    actor = session.get("username")
    resolved_room_id = info.get("id") or room_id
    append_room_log(
        room_dir,
        f"$ {command}",
        source="command",
        actor=actor,
        room_id=resolved_room_id,
        broadcast=broadcast,
    )
    result = execute_room_command(
        command,
        room_id=resolved_room_id,
        room_dir=room_dir,
        rooms_dir=current_app.config.get("ROOMS_DIR"),
        app=current_app._get_current_object(),
        actor=actor,
        actor_id=session.get("user_id"),
    )
    append_room_log(
        room_dir,
        result.message,
        source="console",
        level="info" if result.ok else "warning",
        actor=actor,
        room_id=resolved_room_id,
        broadcast=broadcast,
    )
    return success_response(
        {"ok": result.ok, "message": result.message}, "Room command executed"
    )


@bp.route("/api/rooms/<room_id>/console/log", methods=["DELETE"])
def clear_room_console_log(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room_or_error(room_id)
    if error:
        return error
    settings = _settings()
    if not settings["enabled"]:
        return error_response("Room console is disabled", 403, "Room console disabled")
    if _current_rank(info) < _required_rank(settings["execute_role"], "admin"):
        return _denied(room_id)
    if info.get("archived"):
        return error_response(
            "Archived room cannot clear the console log", 409, "Room archived"
        )
    count = clear_room_log(room_dir)
    return success_response({"cleared": count}, "Room console log cleared successfully")
