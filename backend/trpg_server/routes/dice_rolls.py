"""玩家投掷 REST 接口与动画降级日志。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Blueprint, request, session

from trpg_server.responses import error_response, success_response
from trpg_server.room_log import log_room_event
from trpg_server.agents.dice_settings import DICE_ANIMATION_LEVELS
from trpg_server.agents import player_roll
from trpg_server.agents.player_roll import result_snapshot
from trpg_server.routes.rooms import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_OWNER,
    _can_access,
    _denied,
    _find_member,
    _find_room,
    _is_elevated,
    _require_login,
    _room_permission,
)

bp = Blueprint("dice_rolls", __name__)
logger = logging.getLogger(__name__)

_DEGRADATION_LOCK = threading.Lock()
_DEGRADATION_HITS: dict[str, list[float]] = {}
_DEGRADATION_LIMIT = 30
_DEGRADATION_WINDOW = 60.0
_DEGRADATION_SESSION_SEEN: set[tuple[str, str, str, str]] = set()


def _can_manage(info: dict[str, Any]) -> bool:
    if _is_elevated() or str(info.get("creator_id")) == str(session.get("user_id")):
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _pending_state(room_id: str, request_id: str | None = None) -> dict[str, Any] | None:
    state = player_roll.get_pending(room_id, request_id)
    return state


@bp.route("/api/rooms/<room_id>/dice-requests", methods=["GET"])
def list_dice_requests(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    return success_response({"pending": _pending_state(room_id)}, "Dice request loaded successfully")


@bp.route("/api/rooms/<room_id>/dice-requests/<request_id>", methods=["GET"])
def get_dice_request(room_id, request_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    state = _pending_state(room_id, request_id)
    if state is None:
        return error_response("Dice request not found", 404, "Dice request not found")
    return success_response(state, "Dice request loaded successfully")


@bp.route("/api/rooms/<room_id>/dice-requests/<request_id>/roll", methods=["POST"])
def roll_dice_request(room_id, request_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    result, error = player_roll.resolve_roll(room_id, request_id, session.get("user_id"))
    if error == "not_found":
        return error_response("Dice request not found", 404, "Dice request not found")
    if error == "forbidden":
        log_room_event(
            _path,
            "非目标玩家尝试投掷",
            actor=session.get("username"),
            room_id=room_id,
            logger_=logger,
            请求ID=request_id,
        )
        return error_response("Only the requested player may roll", 403, "Forbidden")
    if error == "already_rolled":
        return error_response("Dice request already rolled", 409, "Already rolled")
    data = {"request_id": request_id, **result_snapshot(result or {})}
    return success_response(data, "Dice rolled successfully")


@bp.route("/api/rooms/<room_id>/dice-requests/<request_id>/complete", methods=["POST"])
def complete_dice_request(room_id, request_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    ok, error = player_roll.complete_roll(room_id, request_id, session.get("user_id"))
    if error == "forbidden":
        return error_response("Only the requested player may complete", 403, "Forbidden")
    # 请求可能已被超时/自动完成清理；对客户端保持幂等。
    return success_response({"request_id": request_id, "completed": ok}, "Dice request completed")


@bp.route("/api/rooms/<room_id>/dice-requests/<request_id>/cancel", methods=["POST"])
def cancel_dice_request(room_id, request_id):
    login_error = _require_login()
    if login_error:
        return login_error
    path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    result, error = player_roll.cancel_roll(room_id, request_id, str(session.get("username") or ""))
    if error == "not_found":
        return error_response("Dice request not found", 404, "Dice request not found")
    if error == "already_rolled":
        return error_response("Dice request already rolled", 409, "Already rolled")
    log_room_event(
        path,
        "取消了等待中的投掷",
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        请求ID=request_id,
    )
    return success_response({"request_id": request_id, "cancelled": True, "summary": (result or {}).get("summary")}, "Dice request cancelled")


@bp.route("/api/rooms/<room_id>/dice-requests/<request_id>/force", methods=["POST"])
def force_dice_request(room_id, request_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    operator = str(session.get("username") or "")
    result, error = player_roll.resolve_roll(room_id, request_id, session.get("user_id"), force=True, operator=operator)
    if error == "not_found":
        return error_response("Dice request not found", 404, "Dice request not found")
    if error == "already_rolled":
        return error_response("Dice request already rolled", 409, "Already rolled")
    data = {"request_id": request_id, "forced": True, "operator": operator, **result_snapshot(result or {})}
    return success_response(data, "Dice request forced")


def _rate_limited(room_id: str) -> bool:
    now = time.time()
    with _DEGRADATION_LOCK:
        hits = [stamp for stamp in _DEGRADATION_HITS.get(room_id, []) if now - stamp < _DEGRADATION_WINDOW]
        if len(hits) >= _DEGRADATION_LIMIT:
            _DEGRADATION_HITS[room_id] = hits
            return True
        hits.append(now)
        _DEGRADATION_HITS[room_id] = hits
        return False


@bp.route("/api/rooms/<room_id>/dice-animation/degraded", methods=["POST"])
def report_dice_degradation(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    payload = request.get_json(silent=True) or {}
    from_level = str(payload.get("from_level") or "").strip().lower()
    to_level = str(payload.get("to_level") or "").strip().lower()
    if from_level not in DICE_ANIMATION_LEVELS or to_level not in DICE_ANIMATION_LEVELS:
        return error_response("Invalid animation level", 400, "bad_level")
    reason = str(payload.get("reason") or "unknown")[:120]
    scope = "session" if str(payload.get("scope") or "").lower() == "session" else "roll"
    dice = payload.get("dice") if isinstance(payload.get("dice"), dict) else {}

    if scope == "session":
        key = (room_id, from_level, to_level, reason)
        with _DEGRADATION_LOCK:
            if key in _DEGRADATION_SESSION_SEEN:
                return success_response({"logged": False}, "Degradation already reported")
            _DEGRADATION_SESSION_SEEN.add(key)

    if _rate_limited(room_id):
        logger.warning("Dice degradation log rate limit reached for room %s; dropped entry", room_id)
        return success_response({"logged": False}, "Degradation rate limited")

    log_room_event(
        path,
        f"骰子动画降级：{from_level} → {to_level}",
        level="warning",
        source="dice_animation",
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        请求ID=payload.get("request_id"),
        工具=str(payload.get("tool_name") or ""),
        原档位=from_level,
        新档位=to_level,
        原因=reason,
        错误=str(payload.get("error") or "")[:500],
        范围=scope,
        骰子=dice,
    )
    return success_response({"logged": True}, "Degradation reported")
