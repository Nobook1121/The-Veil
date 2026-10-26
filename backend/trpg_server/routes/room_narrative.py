"""房间侧图谱运行态 API（设计文档 §8.5）。

提供当前地点 / 队伍 / 状态查询、房间可触发列表，以及房主/管理员的手动触发。
手动触发会写入运行态账本（`narrative/state.json`），并记录审计日志。
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, session

from trpg_server.agents.context import build_agent_context
from trpg_server.agents.narrative.logger import log_event
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, success_response
from trpg_server.routes.rooms import (
    _can_access,
    _can_manage,
    _denied,
    _find_room,
    _get_rooms_dir,
    _get_scenarios_dir,
    _require_login,
)

bp = Blueprint("room_narrative", __name__)
logger = logging.getLogger(__name__)


def _open_runtime(room_id: str) -> tuple[NarrativeRuntime | None, Any]:
    context = build_agent_context(
        room_id,
        _get_rooms_dir(),
        _get_scenarios_dir(),
        user_id=session.get("user_id"),
        config_dir=current_app.config.get("CONFIG_DIR"),
    )
    try:
        return NarrativeRuntime.load(context), None
    except NarrativeRuntimeError as exc:
        return None, error_response(str(exc), 404, "Narrative unavailable")


def _location_payload(runtime: NarrativeRuntime, node_id: Any) -> dict[str, Any]:
    node = runtime.node(node_id)
    if node is None:
        return {"id": str(node_id or ""), "title": str(node_id or ""), "kind": "", "visible": False, "visited": False}
    return {
        "id": node.id,
        "title": node.title,
        "kind": node.kind,
        "visible": runtime.node_visible(node.id),
        "visited": runtime.is_visited(node.id),
    }


def _party_payload(runtime: NarrativeRuntime, info: dict[str, Any]) -> dict[str, Any]:
    narrative = runtime.narrative if isinstance(runtime.narrative, dict) else {}
    party_locations = narrative.get("party_locations") if isinstance(narrative.get("party_locations"), dict) else {}
    members = []
    for member in info.get("members", []) if isinstance(info.get("members"), list) else []:
        if not isinstance(member, dict):
            continue
        members.append(
            {
                "user_id": member.get("user_id"),
                "username": member.get("username"),
                "room_role": member.get("room_role"),
                "active": member.get("is_active", True),
            }
        )
    return {
        "split": bool(narrative.get("party_split")),
        "locations": [
            {
                "pc_id": str(pc_id),
                "location_id": str(location_id),
                "location_title": runtime.title(location_id),
            }
            for pc_id, location_id in party_locations.items()
        ],
        "members": members,
    }


def _status_payload(runtime: NarrativeRuntime) -> dict[str, Any]:
    narrative = runtime.narrative if isinstance(runtime.narrative, dict) else {}
    clock = narrative.get("clock") if isinstance(narrative.get("clock"), dict) else {}
    return {
        "mode": narrative.get("mode"),
        "topology": runtime.bundle.topology,
        "scenario_version": narrative.get("scenario_version") or runtime.bundle.scenario_version,
        "turn": clock.get("turn", 0),
        "clocks": narrative.get("clocks") or {},
        "flags": narrative.get("flags") or {},
        "node_status": narrative.get("node_status") or {},
        "trigger_fires": len(narrative.get("trigger_fires") or {}),
        "revealed_clues": narrative.get("revealed_clues") or [],
        "visit_count": len(narrative.get("visit_log") or []),
    }


@bp.route("/api/rooms/<room_id>/narrative", methods=["GET"])
def get_room_narrative(room_id: str):
    if (error := _require_login()) is not None:
        return error
    room_dir, info = _find_room(room_id)
    if room_dir is None:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    runtime, failure = _open_runtime(room_id)
    if failure is not None:
        return failure
    return success_response(
        {
            "room_id": room_id,
            "scenario_id": info.get("scenario_id"),
            "location": _location_payload(runtime, runtime.location_id),
            "party": _party_payload(runtime, info),
            "status": _status_payload(runtime),
            "permissions": {"can_manage": _can_manage(info)},
        },
        "Room narrative loaded",
    )


@bp.route("/api/rooms/<room_id>/triggers", methods=["GET"])
def list_room_triggers(room_id: str):
    if (error := _require_login()) is not None:
        return error
    room_dir, info = _find_room(room_id)
    if room_dir is None:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    runtime, failure = _open_runtime(room_id)
    if failure is not None:
        return failure
    payload = runtime.list_available_triggers()
    payload["permissions"] = {"can_fire": _can_manage(info)}
    return success_response(payload, "Room triggers loaded")


@bp.route("/api/rooms/<room_id>/triggers/<trigger_id>/fire", methods=["POST"])
def fire_room_trigger(room_id: str, trigger_id: str):
    if (error := _require_login()) is not None:
        return error
    room_dir, info = _find_room(room_id)
    if room_dir is None:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    runtime, failure = _open_runtime(room_id)
    if failure is not None:
        return failure
    result = runtime.force_trigger(
        trigger_id,
        audience="player",
        actor_id=str(session.get("user_id") or ""),
    )
    if not result.get("triggered"):
        reason = str(result.get("reason") or "trigger_not_found")
        status = 404 if reason == "trigger_not_found" else 409
        return error_response("Trigger not fired", status, reason)
    log_event(
        "room.trigger.fire",
        room_id=room_id,
        trigger_id=trigger_id,
        user_id=session.get("user_id"),
        event_id=result.get("event_id"),
    )
    log_user_action(
        logger,
        user_action_text(session.get("username"), "手动触发了房间触发器"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        触发器ID=trigger_id,
    )
    return success_response(result, "Trigger fired")


__all__ = ["bp"]
