"""房间内嵌 Web 控制台的 REST 接口。

只暴露「本房间」的日志读取与房间域命令执行，权限复用 ``routes.rooms`` 的房间
角色判定（房主/房间管理员/系统管理员）。日志与命令结果都写入房间日志，实现
与 Web 控制台、房间归档三处一致。
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, request, session

from trpg_server import room_ai_changes, room_interlude, room_rules
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
_DESTRUCTIVE_AFTER_ARCHIVE = {
    ("archive", ""),
    ("log", "clear"),
    ("narrative", "reset"),
    ("narrative", "unlock"),
    ("narrative", "trigger"),
    ("narrative", "rebuild-state"),
    ("narrative", "insert"),
    ("narrative", "say"),
    # 战斗轮：任何推进/改变战斗状态的命令都会改写房间持久化状态。
    ("combat", "start"),
    ("combat", "join"),
    ("combat", "next"),
    ("combat", "end-turn"),
    ("combat", "endturn"),
    ("combat", "delay"),
    ("combat", "disengage"),
    ("combat", "reload"),
    ("combat", "ready"),
    ("combat", "cast"),
    ("combat", "damage"),
    ("combat", "heal"),
    ("combat", "treat"),
    ("combat", "end"),
}


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
        actor_rank=_current_rank(info),
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
    # 控制台进入幕间与聊天命令保持同样的后续动作：返回开场提示，由前端复用聊天
    # 主链路触发一次 AI KP 回合。AI 不可用只影响开场白，不会回滚已进入的幕间。
    extra: dict[str, object] = {}
    if result.ok and _parse_command(command) == ("interlude", "start"):
        extra["kp_prompt"] = room_interlude.interlude_opening_prompt()
    return success_response(
        {"ok": result.ok, "message": result.message, **extra}, "Room command executed"
    )


@bp.route("/api/rooms/<room_id>/console/ai-changes", methods=["GET"])
def list_room_ai_changes(room_id):
    """AI 写卡记录只对房主 / 房间管理员 / 系统管理员可见。"""
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
    return success_response(
        {"records": room_ai_changes.list_records(info)},
        "Room AI change records loaded successfully",
    )


@bp.route(
    "/api/rooms/<room_id>/console/ai-changes/<record_id>/rollback",
    methods=["POST"],
)
def rollback_room_ai_change(room_id, record_id):
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
            "Archived room cannot roll back character changes", 409, "Room archived"
        )
    try:
        result = room_ai_changes.rollback(
            room_dir,
            info,
            record_id,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
        )
    except room_rules.RuleOperationError as exc:
        return error_response(exc.message, 400, exc.code)
    resolved_room_id = info.get("id") or room_id
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit(
            "room_members_changed",
            {"room_id": resolved_room_id},
            room=resolved_room_id,
        )
    record = result.get("record") or {}
    return success_response(
        {
            "ok": True,
            "already_rolled_back": bool(result.get("already_rolled_back")),
            "removed_changes": result.get("removed_changes") or 0,
            "record": record,
        },
        (
            "该改动已回滚过"
            if result.get("already_rolled_back")
            else "已回滚 AI 写卡改动"
        ),
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
