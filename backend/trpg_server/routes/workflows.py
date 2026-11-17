"""行动工作流 REST API（仅 dnd5e 房间）。

前端与外部调用可以用同一套接口查询、取消、超时收尾或提交待决决定；
真正的状态变更仍由服务端工作流执行，客户端不能直接改棋盘或战斗数据。
"""

from __future__ import annotations

from flask import Blueprint, request, session

from trpg_server.combat.settings import resolve_settings
from trpg_server.responses import error_response, success_response
from trpg_server.routes.rooms import (
    _can_access,
    _can_manage,
    _denied,
    _find_room,
    _require_login,
)
from trpg_server.workflows import access as flow_access
from trpg_server.workflows.engine import WorkflowEngine

bp = Blueprint("workflows", __name__)


def _engine_or_error(room_id: str):
    room_dir, info = _find_room(room_id)
    if not info:
        return None, None, None, error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return None, None, None, _denied(room_id)
    if str(resolve_settings(info).ruleset_id or "").strip().lower() != "dnd5e":
        return None, None, None, error_response(
            "该规则集未启用行动自动化工作流", 409, "workflow.unsupported_ruleset"
        )
    return room_dir, info, WorkflowEngine(room_dir, room_id=room_id), None


def _manage_flag(info: dict) -> bool:
    return bool(_can_manage(info))


def _summary(flow: dict, *, manager: bool = True, info: dict | None = None) -> dict:
    """按观察者裁剪步骤结果：普通成员只看自己角色的步骤。"""
    viewer_id = session.get("user_id")
    steps = []
    for step in flow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        visible = manager or flow_access.step_belongs_to(
            flow, step, user_id=viewer_id, info=info
        )
        steps.append(
            {
                "id": step.get("id"),
                "label": step.get("label"),
                "policy": step.get("policy"),
                "status": step.get("status"),
                "result": step.get("result") if visible else None,
            }
        )
    return {
        "id": flow.get("id"),
        "kind": flow.get("kind"),
        "status": flow.get("status"),
        "initiator": flow.get("initiator"),
        "initiator_type": flow.get("initiator_type"),
        "created_at": flow.get("created_at"),
        "expires_at": flow.get("expires_at"),
        "metadata": flow.get("metadata") or {},
        "steps": steps,
    }


@bp.route("/api/rooms/<room_id>/workflows", methods=["GET"])
def list_workflows(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, engine, denied = _engine_or_error(room_id)
    if denied:
        return denied
    manager = _manage_flag(info)
    rows = engine.list(status=request.args.get("status") or None, limit=50)
    rows = [
        flow
        for flow in rows
        if flow_access.flow_visible_to(
            flow, user_id=session.get("user_id"), manager=manager, info=info
        )
    ]
    return success_response(
        {
            "workflows": [
                _summary(flow, manager=manager, info=info) for flow in rows
            ]
        },
        "Workflows loaded",
    )


@bp.route("/api/rooms/<room_id>/workflows/<flow_id>", methods=["GET"])
def get_workflow(room_id, flow_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, engine, denied = _engine_or_error(room_id)
    if denied:
        return denied
    flow = engine.get(flow_id)
    if flow is None:
        return error_response("Flow not found", 404, "workflow.not_found")
    manager = _manage_flag(info)
    if not flow_access.flow_visible_to(
        flow, user_id=session.get("user_id"), manager=manager, info=info
    ):
        return error_response(
            "该流程与你的角色无关，只有 KP / 房管可以查看", 403, "workflow.forbidden"
        )
    return success_response(
        {
            "workflow": _summary(flow, manager=manager, info=info),
            # 事件日志包含其他角色的副作用明细，只对 KP / 房管开放。
            "events": (flow.get("events") or []) if manager else [],
        },
        "Workflow loaded",
    )


@bp.route("/api/rooms/<room_id>/workflows/<flow_id>/cancel", methods=["POST"])
def cancel_workflow(room_id, flow_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, engine, denied = _engine_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    result = engine.cancel(
        flow_id,
        actor=str(session.get("username") or ""),
        reason=str(payload.get("reason") or ""),
        actor_user_id=session.get("user_id"),
        is_manager=_manage_flag(info),
    )
    if result.get("error"):
        status = 403 if result.get("code") == flow_access.FORBIDDEN else 404
        return error_response(result["error"], status, result.get("code"))
    return success_response(result, "Workflow cancelled")


@bp.route("/api/rooms/<room_id>/workflows/<flow_id>/decide", methods=["POST"])
def decide_workflow(room_id, flow_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, engine, denied = _engine_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    step_id = str(payload.get("step_id") or "").strip()
    if not step_id:
        return error_response("step_id is required", 400, "workflow.missing_step")
    result = engine.submit(
        flow_id,
        step_id,
        decision=payload.get("decision") if payload.get("decision") is not None else {},
        idempotency_key=str(payload.get("idempotency_key") or ""),
        # actor 由服务端会话派生，不信任请求体里的身份字段。
        actor=str(session.get("username") or ""),
        actor_user_id=session.get("user_id"),
        is_manager=_manage_flag(info),
    )
    if result.get("error"):
        status = 403 if result.get("code") == flow_access.FORBIDDEN else 409
        return error_response(result["error"], status, result.get("code"))
    return success_response(result, "Decision accepted")


@bp.route("/api/rooms/<room_id>/workflows/expire", methods=["POST"])
def expire_workflows(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, engine, denied = _engine_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    if flow_id:
        result = engine.expire(
            flow_id,
            actor=str(session.get("username") or ""),
            actor_user_id=session.get("user_id"),
            is_manager=_manage_flag(info),
        )
        if result.get("error"):
            status = 403 if result.get("code") == flow_access.FORBIDDEN else 404
            return error_response(result["error"], status, result.get("code"))
        return success_response(result, "Workflow expired")
    expired = engine.expire_due()
    return success_response({"expired": expired, "count": len(expired)}, "Workflows expired")


__all__ = ["bp"]
