"""房间幕间与自动存档路由：幕间请求/开始/结算/复核/结束、存档节点。"""
from flask import current_app
from flask import request
from flask import session
from trpg_server import room_interlude
from trpg_server.json_store import read_json
from trpg_server.responses import error_response
from trpg_server.responses import success_response
from trpg_server.room_ops.access import _can_access, _can_manage, _denied, _require_login
from trpg_server.room_ops.autosave import _write_room_autosave
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import _find_room, _get_rooms_dir, _read_messages
from trpg_server.room_ops.visibility import filter_payload_messages

@bp.route("/api/rooms/<room_id>/autosave", methods=["POST"])
def save_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = _write_room_autosave(
        room_dir,
        room_id,
        _read_messages(room_dir),
        username=session.get("username"),
        user_id=session.get("user_id"),
    )
    return success_response(autosave, "Room autosave saved successfully")

@bp.route("/api/rooms/<room_id>/autosave", methods=["GET"])
def load_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = read_json(room_dir / "autosave.json", default={"messages": []})
    # 自动存档文件保留全量消息（恢复时用），响应按当前查看者过滤（B2）。
    return success_response(
        filter_payload_messages(autosave, info), "Room autosave loaded successfully"
    )

def _interlude_payload(info):
    state = room_interlude.interlude_state(info)
    return {
        "state": room_interlude.snapshot(info),
        "settlement": state.get("settlement") or {},
        "reviews": state.get("reviews") or [],
        "can_manage": _can_manage(info),
    }

def _emit_interlude_updated(room_id, info):
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit(
            "room_interlude_updated",
            {"room_id": room_id, "interlude": room_interlude.snapshot(info)},
            room=str(room_id),
        )

@bp.route("/api/rooms/<room_id>/interlude", methods=["GET"])
def get_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    return success_response(_interlude_payload(info), "Interlude loaded")

@bp.route("/api/rooms/<room_id>/interlude/request", methods=["POST"])
def request_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    try:
        record = room_interlude.request_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source=str(data.get("source") or "player"),
            reason=str(data.get("reason") or ""),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude request failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(_interlude_payload(info) | {"request": record}, "Interlude requested")

@bp.route("/api/rooms/<room_id>/interlude/start", methods=["POST"])
def start_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    credit_events = data.get("credit_events") if isinstance(data.get("credit_events"), list) else []
    try:
        outcome = room_interlude.start_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            credit_events=credit_events,
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude start failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(
        {
            **_interlude_payload(info),
            "settlement": outcome.get("settlement") or {},
            "kp_prompt": room_interlude.interlude_opening_prompt(),
        },
        "Interlude started",
    )

@bp.route("/api/rooms/<room_id>/interlude/settle", methods=["POST"])
def settle_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    credit_events = data.get("credit_events") if isinstance(data.get("credit_events"), list) else []
    try:
        settlement = room_interlude.settle_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            credit_events=credit_events,
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude settle failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(
        {**_interlude_payload(info), "settlement": settlement},
        "Interlude settled",
    )

@bp.route("/api/rooms/<room_id>/interlude/review", methods=["POST"])
def add_room_interlude_review(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    try:
        record = room_interlude.add_review(
            room_dir,
            info,
            content=str(data.get("content") or ""),
            topic=str(data.get("topic") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source=str(data.get("source") or "player"),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude review failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response({**_interlude_payload(info), "review": record}, "Interlude review saved")

@bp.route("/api/rooms/<room_id>/interlude/end", methods=["POST"])
def end_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    confirmed = bool(data.get("confirm")) or request.args.get("confirm") in {"1", "true", "yes"}
    if not confirmed:
        return error_response(
            "Confirmation required",
            409,
            "结束幕间会自动归档房间。确认后请再次提交，并带上 confirm=true。",
        )
    try:
        room_interlude.end_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude end failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    from trpg_server import room_admin

    try:
        archive = room_admin.archive_room(_get_rooms_dir(), room_id, app=current_app._get_current_object())
    except Exception as exc:  # noqa: BLE001 - 幕间已结束，归档失败需明确上报
        logger.exception("Failed to archive room %s after interlude", room_id)
        return error_response("Room archive failed", 500, f"幕间已结束，但自动归档失败：{exc}")
    return success_response(
        {**_interlude_payload(info), "archive": {"archived_at": archive.get("archived_at")}},
        "Interlude ended and room archived",
    )
