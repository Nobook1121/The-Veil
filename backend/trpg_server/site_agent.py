"""网站 Agent 的编排层：单轮对话、工具执行、写操作确认与审计。

这里不直接暴露 Flask 路由；``routes/site_agent.py`` 负责 HTTP 与会话，本模块只接收
已构建的 :class:`Actor`，方便测试与复用。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import current_app

from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.narrative.logger import log_event
from trpg_server.agents.profiles import AgentProfile
from trpg_server.agents.runtime import run_agent_completion
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.json_store import read_json
from trpg_server.site_agent_auth import Actor, AgentAuthError, authorize_tool, build_actor
from trpg_server.site_agent_config import SiteAgentConfig, load_site_agent_config
from trpg_server.site_agent_prompt import build_system_prompt, detect_injection
from trpg_server.site_agent_store import (
    acquire_turn_lock,
    append_audit,
    append_message,
    create_conversation,
    list_conversations,
    load_action,
    load_conversation,
    record_usage,
    release_turn_lock,
    save_conversation,
    site_usage_window,
    turns_last_hour,
    update_action,
    user_today_usage,
)
from trpg_server.site_agent_tools import (
    apply_pending_action,
    build_tool_registry,
    exposed_specs,
)
from trpg_server.site_agent_tools.base import ToolContext
from trpg_server.settings import CONFIG_DIR, ROOMS_DIR

logger = logging.getLogger(__name__)


class SiteAgentError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _config() -> SiteAgentConfig:
    return load_site_agent_config(current_app.config.get("CONFIG_DIR", CONFIG_DIR))


def _emit(user_id: int, event: str, payload: dict[str, Any]) -> None:
    emitter = current_app.config.get("SITE_AGENT_EVENT_EMITTER")
    if callable(emitter):
        try:
            emitter(int(user_id), event, payload)
        except Exception:
            logger.debug("Site agent emitter failed", exc_info=True)
        return
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit("agent_event", {"event": event, **payload}, room=f"user:{user_id}")
    except Exception:
        logger.debug("Unable to emit site agent event", exc_info=True)


def _room_context(actor: Actor, room_id: str | None) -> dict[str, Any] | None:
    """只有当前确实在该房间（socket 成员）时才注入房间上下文。"""
    if not room_id or room_id not in actor.room_ids:
        return None
    rooms_dir = Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR))
    info = read_json(rooms_dir / str(room_id) / "info.json", default={})
    if not isinstance(info, dict) or not info:
        return None
    return {
        "room_id": str(room_id),
        "title": info.get("name"),
        "scenario_id": info.get("scenario_id"),
        "scenario_title": info.get("scenario_title"),
    }


def _platform_candidates(provider_id: Any = None) -> list[dict[str, Any]]:
    from trpg_server.routes.chat_helpers import _load_enabled_platforms

    candidates: list[dict[str, Any]] = []
    for platform_id, platform_config in _load_enabled_platforms(provider_id):
        section = platform_config.get("config") if isinstance(platform_config, dict) else {}
        section = section if isinstance(section, dict) else {}
        api_key = section.get("api_key")
        base_url = chat_completions_endpoint(str(section.get("base_url") or ""))
        if not base_url:
            continue
        if not api_key and platform_id == "lmstudio":
            api_key = "lm-studio"
        elif not api_key:
            continue
        raw_models = platform_config.get("models") if isinstance(platform_config, dict) else None
        models: list[Any] = raw_models if isinstance(raw_models, list) else []
        model = next((item for item in models if item.get("enabled", True)), models[0] if models else None)
        candidates.append(
            {
                "platform": platform_id,
                "base_url": base_url,
                "headers": {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                "model": (model or {}).get("id", "local-model") if isinstance(model, dict) else "local-model",
            }
        )
    return candidates


def _build_requester(candidates: list[dict[str, Any]], runtime_config: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    from trpg_server.agents.http_retry import post_with_retry

    def requester(payload: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        for candidate in candidates:
            body = {**payload, "model": candidate["model"]}
            try:
                response = post_with_retry(
                    candidate["base_url"],
                    headers=candidate["headers"],
                    payload=body,
                    timeout=runtime_config.ai_request_timeout,
                    max_retries=runtime_config.ai_request_max_retries,
                    label="site_agent",
                )
                if not response.ok:
                    detail = str(getattr(response, "text", ""))[:500]
                    raise RuntimeError(f"HTTP {response.status_code}: {detail}")
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("AI 平台返回的数据格式无效")
                return data
            except Exception as exc:
                errors.append(f"{candidate['platform']}: {exc}")
                logger.warning("Site agent platform %s failed: %s", candidate["platform"], exc)
        raise RuntimeError("所有已启用 AI 平台均请求失败：" + "；".join(errors[-2:]))

    return requester


def _history(conversation: dict[str, Any], max_messages: int) -> list[dict[str, str]]:
    raw_messages = conversation.get("messages")
    messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
    result: list[dict[str, str]] = []
    for message in messages[-max_messages:]:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if content:
            result.append({"role": role, "content": content})
    return result


def _check_usage(actor: Actor, config: SiteAgentConfig) -> None:
    if turns_last_hour(actor.user_id) >= config.max_turns_per_hour:
        raise SiteAgentError("rate_limited", "过去一小时内对话次数已达上限，请稍后再试", 429)
    if config.daily_token_budget > 0:
        usage = user_today_usage(actor.user_id)
        if int(usage["total_tokens"]) >= config.daily_token_budget:
            raise SiteAgentError("budget_exhausted", "今日 Agent token 额度已用完", 429)


def _get_conversation(actor: Actor, conversation_id: str | None, context_data: dict[str, Any] | None) -> dict[str, Any]:
    if conversation_id:
        conversation = load_conversation(actor.user_id, str(conversation_id))
        if not conversation:
            raise SiteAgentError("conversation_not_found", "会话不存在或已被删除", 404)
        return conversation
    return create_conversation(
        actor.user_id,
        context=context_data,
        max_conversations=_config().max_conversations_per_user,
    )


def run_turn(
    actor: Actor,
    *,
    content: str,
    conversation_id: str | None = None,
    room_id: str | None = None,
    scenario_context: dict[str, Any] | None = None,
    ip: str = "",
) -> dict[str, Any]:
    config = _config()
    if not config.enabled:
        raise SiteAgentError("agent_disabled", "网站 Agent 已被管理员关闭", 403)
    if not actor.has("agent.use"):
        raise SiteAgentError("agent_forbidden", "你没有使用网站 Agent 的权限", 403)
    if not actor.session_current:
        raise SiteAgentError("session_expired", "登录状态已失效，请刷新页面重新登录", 401)
    text = str(content or "").strip()
    if not text:
        raise SiteAgentError("empty_message", "消息不能为空", 400)
    if len(text) > config.max_message_chars:
        raise SiteAgentError("message_too_long", f"消息不能超过 {config.max_message_chars} 个字符", 400)
    _check_usage(actor, config)

    if not acquire_turn_lock(actor.user_id):
        raise SiteAgentError("turn_in_progress", "上一轮对话仍在处理，请稍候", 429)
    try:
        return _run_turn_locked(
            actor,
            config=config,
            text=text,
            conversation_id=conversation_id,
            room_id=room_id,
            scenario_context=scenario_context,
            ip=ip,
        )
    finally:
        release_turn_lock(actor.user_id)


def _run_turn_locked(
    actor: Actor,
    *,
    config: SiteAgentConfig,
    text: str,
    conversation_id: str | None,
    room_id: str | None,
    ip: str,
    scenario_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    room = _room_context(actor, room_id)
    context_data: dict[str, Any] = {}
    if room:
        context_data["room_id"] = room.get("room_id")
    if scenario_context:
        context_data.update({key: value for key, value in scenario_context.items() if value not in (None, "")})
    conversation = _get_conversation(actor, conversation_id, context_data or None)
    user_message = append_message(conversation, role="user", content=text)
    save_conversation(conversation, max_messages=config.max_messages_per_conversation)

    injection_hits = detect_injection(text)
    if injection_hits:
        append_audit(
            {
                "event": "prompt_injection_suspected",
                "user_id": actor.user_id,
                "username": actor.username,
                "conversation_id": conversation["id"],
                "patterns": injection_hits,
            }
        )

    turn_id = f"t-{user_message['id']}"
    tool_context = ToolContext(
        actor=actor,
        config=config,
        conversation_id=conversation["id"],
        turn_id=turn_id,
        room_id=room.get("room_id") if room else None,
        room_info=room,
        scenario_context=scenario_context,
        ip=ip,
        event_callback=lambda event, payload: _emit(actor.user_id, event, payload),
    )
    specs = exposed_specs(actor, config, room_id=tool_context.room_id)
    registry = build_tool_registry(actor, tool_context, config)
    profile = AgentProfile(
        id="site_agent",
        name="网站 Agent",
        prompt="",
        provider=None,
        tool_names=[spec.name for spec in specs],
    )

    candidates = _platform_candidates()
    if not candidates:
        raise SiteAgentError("no_platform", "没有已启用的 AI 平台，请联系管理员配置", 503)
    runtime_config = load_ai_runtime_config(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
    requester = _build_requester(candidates, runtime_config)

    system_prompt = build_system_prompt(
        actor=actor,
        config=config,
        tools=[spec for spec in specs],
        room_context=room,
        scenario_context=scenario_context,
    )
    messages = [{"role": "system", "content": system_prompt}, *_history(conversation, config.max_history_messages)]

    _emit(actor.user_id, "agent_stage", {"stage": "thinking", "conversation_id": conversation["id"]})
    result = run_agent_completion(
        requester,
        {"messages": messages, "stream": False},
        profile,
        registry,
        tool_context,
        max_tool_rounds=config.max_tool_rounds,
        max_tool_result_chars=config.max_tool_result_chars,
    )

    prompt_tokens = int(result.prompt_token_count or 0)
    completion_tokens = int(result.completion_token_count or 0)
    total_tokens = int(result.token_count or (prompt_tokens + completion_tokens))
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    record_usage(
        actor.user_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        counted_turn=True,
    )

    content_text = str(result.content or "").strip()
    if not content_text:
        content_text = result.notice or "Agent 未能生成回复，请稍后重试。"
    attachments = list(tool_context.pending_actions)
    assistant_message = append_message(
        conversation,
        role="assistant",
        content=content_text,
        attachments=attachments or None,
        usage=usage,
    )
    if result.notice and result.notice not in content_text:
        assistant_message["notice"] = result.notice
    save_conversation(conversation, max_messages=config.max_messages_per_conversation)

    append_audit(
        {
            "event": "agent_turn",
            "user_id": actor.user_id,
            "username": actor.username,
            "role": actor.role,
            "conversation_id": conversation["id"],
            "turn_id": turn_id,
            "ip": ip,
            "tokens": usage,
            "tool_calls": len(tool_context.pending_actions),
        }
    )
    payload = {"conversation": conversation, "message": assistant_message}
    _emit(actor.user_id, "agent_message", {"conversation_id": conversation["id"], "message": assistant_message})
    return payload


def conversations(actor: Actor) -> list[dict[str, Any]]:
    return list_conversations(actor.user_id, retention_days=_config().conversation_retention_days)


def conversation_detail(actor: Actor, conversation_id: str) -> dict[str, Any]:
    conversation = load_conversation(actor.user_id, conversation_id)
    if not conversation:
        raise SiteAgentError("conversation_not_found", "会话不存在或已被删除", 404)
    return conversation


def delete_conversation_for(actor: Actor, conversation_id: str) -> bool:
    from trpg_server.site_agent_store import delete_conversation

    return delete_conversation(actor.user_id, conversation_id)


def confirm_action(actor: Actor, action_id: str, *, apply: bool) -> dict[str, Any]:
    """确认或拒绝一条待确认操作。"""
    config = _config()
    if not config.enabled or not actor.has("agent.use") or not actor.session_current:
        raise SiteAgentError("agent_forbidden", "当前会话无权执行该操作", 403)
    action = load_action(action_id)
    if not action or str(action.get("actor_id")) != str(actor.user_id):
        raise SiteAgentError("action_not_found", "待确认操作不存在", 404)
    if action.get("status") != "pending":
        raise SiteAgentError("action_not_pending", "该操作已处理或已过期", 409)
    if not apply:
        update_action(action, status="rejected")
        append_audit(
            {
                "event": "agent_action_rejected",
                "user_id": actor.user_id,
                "action_id": action_id,
                "tool": action.get("tool"),
            }
        )
        _sync_conversation_attachment(actor, action, "rejected")
        return {"status": "rejected", "action_id": action_id}

    raw_args = action.get("args")
    action_args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    room_id = str(action.get("room_id") or "") or None
    spec = next(
        (item for item in exposed_specs(actor, config, room_id=room_id) if item.name == action.get("tool")),
        None,
    )
    if spec is None:
        from trpg_server.site_agent_tools import SPECS_BY_NAME

        spec = SPECS_BY_NAME.get(str(action.get("tool")))
    if spec is not None:
        decision = authorize_tool(actor, spec, action_args, config=config, room_id=room_id)
        if not decision.allowed:
            raise SiteAgentError("permission_denied", "权限已变化，无法执行该操作", 403)
    result = apply_pending_action(actor, action, config=config)
    if not result.ok:
        append_audit(
            {
                "event": "agent_action_failed",
                "user_id": actor.user_id,
                "action_id": action_id,
                "tool": action.get("tool"),
                "error": result.error,
            }
        )
        raise SiteAgentError("action_failed", result.error or "执行失败", 400)
    update_action(action, status="applied", applied_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    append_audit(
        {
            "event": "agent_action_applied",
            "user_id": actor.user_id,
            "action_id": action_id,
            "tool": action.get("tool"),
            "summary": action.get("summary"),
        }
    )
    if str(action.get("tool")) == "scenarios.save":
        try:
            raw_args = action.get("args")
            arg_payload: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            raw_fields = arg_payload.get("fields")
            fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
            log_event(
                "agent.scenario.patch",
                scenario_id=str(arg_payload.get("scenario_id") or ""),
                preview_id=action_id,
                ops=[str(key) for key in list(fields)[:20]],
                user_id=str(actor.user_id),
                result="applied",
            )
        except Exception:  # noqa: BLE001 - 事件日志失败不影响执行结果
            logger.exception("Failed to log agent.scenario.patch for action %s", action_id)
    _sync_conversation_attachment(actor, action, "applied")
    return {"status": "applied", "action_id": action_id, "result": result.to_dict()}


def _sync_conversation_attachment(actor: Actor, action: dict[str, Any], status: str) -> None:
    conversation = load_conversation(actor.user_id, str(action.get("conversation_id") or ""))
    if not conversation:
        return
    changed = False
    for message in conversation.get("messages", []):
        for attachment in message.get("attachments") or []:
            if isinstance(attachment, dict) and attachment.get("action_id") == action.get("id"):
                attachment["status"] = status
                changed = True
    if changed:
        save_conversation(conversation, max_messages=_config().max_messages_per_conversation)


def usage_summary(hours: int = 24) -> dict[str, Any]:
    return site_usage_window(hours)


def current_actor() -> Actor:
    try:
        return build_actor()
    except AgentAuthError as exc:
        raise SiteAgentError(exc.code, exc.message, exc.status) from exc
