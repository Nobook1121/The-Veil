"""网站 Agent 的 HTTP 接口。"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, request

from trpg_server import site_agent
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, success_response
from trpg_server.site_agent import SiteAgentError
from trpg_server.site_agent_auth import AgentAuthError, build_actor
from trpg_server.site_agent_store import turns_last_hour, user_today_usage
from trpg_server.site_agent_tools import exposed_specs
from trpg_server.site_agent_config import load_site_agent_config
from trpg_server.settings import CONFIG_DIR

bp = Blueprint("site_agent", __name__)
logger = logging.getLogger(__name__)


def _actor():
    return build_actor()


def _error(exc: SiteAgentError):
    return error_response(exc.message, exc.status, exc.message)


@bp.route("/api/agent/status", methods=["GET"])
def agent_status():
    try:
        actor = _actor()
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    config = load_site_agent_config(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
    can_use = bool(config.enabled and actor.has("agent.use"))
    specs = exposed_specs(actor, config, room_id="probe") if can_use else []
    usage = user_today_usage(actor.user_id)
    return success_response(
        {
            "enabled": config.enabled,
            "can_use": can_use,
            "role": actor.role,
            "permissions": sorted(actor.permissions),
            "tools": [
                {"name": spec.name, "risk": spec.risk, "scope": spec.scope}
                for spec in specs
                if spec.scope != "room"
            ],
            "room_tools": [
                {"name": spec.name, "risk": spec.risk, "scope": spec.scope}
                for spec in specs
                if spec.scope == "room"
            ],
            "limits": {
                "max_tool_rounds": config.max_tool_rounds,
                "max_message_chars": config.max_message_chars,
                "max_turns_per_hour": config.max_turns_per_hour,
                "daily_token_budget": config.daily_token_budget,
            },
            "usage_today": usage,
            "turns_last_hour": turns_last_hour(actor.user_id),
        },
        "Agent status",
    )


@bp.route("/api/agent/conversations", methods=["GET"])
def list_agent_conversations():
    try:
        actor = _actor()
        return success_response(site_agent.conversations(actor), "Conversations")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    except SiteAgentError as exc:
        return _error(exc)


@bp.route("/api/agent/conversations", methods=["POST"])
def create_agent_conversation():
    try:
        actor = _actor()
        config = load_site_agent_config(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
        if not config.enabled or not actor.has("agent.use"):
            return error_response("Agent unavailable", 403, "Agent unavailable")
        from trpg_server.site_agent_store import create_conversation

        conversation = create_conversation(
            actor.user_id,
            max_conversations=config.max_conversations_per_user,
        )
        return success_response(conversation, "Conversation created", 201)
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)


@bp.route("/api/agent/conversations/<conversation_id>", methods=["GET"])
def get_agent_conversation(conversation_id):
    try:
        actor = _actor()
        return success_response(site_agent.conversation_detail(actor, conversation_id), "Conversation")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    except SiteAgentError as exc:
        return _error(exc)


@bp.route("/api/agent/conversations/<conversation_id>", methods=["DELETE"])
def delete_agent_conversation(conversation_id):
    try:
        actor = _actor()
        deleted = site_agent.delete_conversation_for(actor, conversation_id)
        return success_response({"deleted": deleted}, "Conversation deleted")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)


@bp.route("/api/agent/turn", methods=["POST"])
def agent_turn():
    try:
        actor = _actor()
        payload = request.get_json(silent=True) or {}
        result = site_agent.run_turn(
            actor,
            content=str(payload.get("content") or ""),
            conversation_id=payload.get("conversation_id"),
            room_id=payload.get("room_id"),
            ip=request.remote_addr or "",
        )
        return success_response(result, "Agent reply")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    except SiteAgentError as exc:
        return _error(exc)
    except Exception:
        logger.exception("Site agent turn failed")
        return error_response("Agent request failed", 500, "Agent request failed")


@bp.route("/api/agent/actions/<action_id>/confirm", methods=["POST"])
def confirm_agent_action(action_id):
    try:
        actor = _actor()
        result = site_agent.confirm_action(actor, action_id, apply=True)
        log_user_action(
            logger,
            user_action_text(actor.username, "通过网站 Agent 执行了写操作"),
            用户ID=actor.user_id,
            操作ID=action_id,
        )
        return success_response(result, "Action applied")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    except SiteAgentError as exc:
        return _error(exc)


@bp.route("/api/agent/actions/<action_id>/reject", methods=["POST"])
def reject_agent_action(action_id):
    try:
        actor = _actor()
        result = site_agent.confirm_action(actor, action_id, apply=False)
        return success_response(result, "Action rejected")
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    except SiteAgentError as exc:
        return _error(exc)


@bp.route("/api/agent/usage", methods=["GET"])
def agent_usage():
    """管理员按时间窗口查看全站 Agent token 消耗。"""
    try:
        actor = _actor()
    except AgentAuthError as exc:
        return error_response(exc.message, exc.status, exc.message)
    if not actor.has("settings.general"):
        return error_response("Permission denied", 403, "Permission denied")
    try:
        hours = int(request.args.get("window_hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    summary = site_agent.usage_summary(hours)
    log_user_action(
        logger,
        user_action_text(actor.username, "查看了网站 Agent 的 token 消耗"),
        时间窗口小时=hours,
        总token=summary.get("total_tokens"),
    )
    return success_response(summary, "Agent usage")
