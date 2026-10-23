"""网站 Agent 的工具注册表。

``build_tool_registry`` 按当前身份过滤能力并包一层统一的鉴权、审计与两段式确认。
模型永远看不到未授权工具；即使伪造调用名也会在服务端被拒绝。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import ToolRegistry
from trpg_server.logging_config import redact_sensitive
from trpg_server.site_agent_auth import authorize_tool
from trpg_server.site_agent_store import append_audit, create_action
from trpg_server.site_agent_tools.base import (
    ToolArgumentError,
    ToolContext,
    ToolResult,
    ToolSpec,
    sanitize_arguments,
)
from trpg_server.site_agent_tools.characters import CHARACTER_TOOLS
from trpg_server.site_agent_tools.permissions import PERMISSION_TOOLS
from trpg_server.site_agent_tools.profile import PROFILE_TOOLS
from trpg_server.site_agent_tools.rooms import ROOM_TOOLS
from trpg_server.site_agent_tools.scenarios import SCENARIO_TOOLS
from trpg_server.site_agent_tools.settings import SITE_SETTING_TOOLS
from trpg_server.site_agent_tools.users import USER_TOOLS
from trpg_server.site_agent_tools.utilities import UTILITY_TOOLS

ALL_TOOL_SPECS: tuple[ToolSpec, ...] = tuple(
    CHARACTER_TOOLS
    + SCENARIO_TOOLS
    + PERMISSION_TOOLS
    + USER_TOOLS
    + UTILITY_TOOLS
    + ROOM_TOOLS
    + SITE_SETTING_TOOLS
    + PROFILE_TOOLS
)
SPECS_BY_NAME = {spec.name: spec for spec in ALL_TOOL_SPECS}

DENIAL_MESSAGES = {
    "session_expired": "登录状态已失效，请刷新页面重新登录",
    "agent_disabled": "网站 Agent 已被管理员关闭",
    "agent_forbidden": "你没有使用网站 Agent 的权限",
    "permission_denied": "缺少执行该操作所需的权限",
    "room_context_required": "请先进入房间再使用房间工具",
    "room_not_joined": "你当前不在该房间中",
}


def exposed_specs(
    actor: Any,
    config: Any,
    *,
    room_id: str | None = None,
) -> list[ToolSpec]:
    if not getattr(config, "enabled", False) or not actor.has("agent.use"):
        return []
    result: list[ToolSpec] = []
    for spec in ALL_TOOL_SPECS:
        if spec.required_permission and not actor.has(spec.required_permission):
            continue
        if spec.scope == "room" and not room_id:
            continue
        result.append(spec)
    return result


def _audit(actor: Any, context: ToolContext, **fields: Any) -> None:
    try:
        append_audit(
            {
                "event": "agent_tool",
                "user_id": actor.user_id,
                "username": actor.username,
                "role": actor.role,
                "conversation_id": context.conversation_id,
                "turn_id": context.turn_id,
                "ip": context.ip,
                **fields,
            }
        )
    except Exception:
        # 审计失败不能阻断主流程，但会在应用日志里留下堆栈。
        pass


def _emit_tool(context: ToolContext, spec: ToolSpec, status: str, *, action_id: str = "") -> None:
    payload = {
        "name": spec.name,
        "risk": spec.risk,
        "status": status,
        "conversation_id": context.conversation_id,
    }
    if action_id:
        payload["action_id"] = action_id
    context.emit("agent_tool", payload)


def _deny(actor: Any, context: ToolContext, spec: ToolSpec, args: dict[str, Any], reason: str) -> dict[str, Any]:
    message = DENIAL_MESSAGES.get(reason, reason)
    _audit(
        actor,
        context,
        tool=spec.name,
        risk=spec.risk,
        arguments=redact_sensitive(args),
        ok=False,
        error=reason,
    )
    _emit_tool(context, spec, "denied")
    return {"error": message, "error_code": reason}


def _make_wrapper(spec: ToolSpec, actor: Any, context: ToolContext, config: Any):
    def wrapper(arguments: dict[str, Any], _runtime_context: Any = None) -> dict[str, Any]:
        try:
            args = sanitize_arguments(spec, arguments)
        except ToolArgumentError as exc:
            _audit(actor, context, tool=spec.name, risk=spec.risk, ok=False, error=f"invalid_arguments:{exc}")
            _emit_tool(context, spec, "invalid")
            return {"error": f"参数不合法：{exc}", "error_code": "invalid_arguments"}

        decision = authorize_tool(actor, spec, args, config=config, room_id=context.room_id)
        if not decision.allowed:
            return _deny(actor, context, spec, args, decision.reason)

        if spec.risk == "read":
            if spec.handler is None:
                return {"error": "该工具未实现", "error_code": "not_implemented"}
            result = spec.handler(actor, args, context)
            _audit(
                actor,
                context,
                tool=spec.name,
                risk=spec.risk,
                arguments=redact_sensitive(args),
                ok=result.ok,
                error=result.error,
            )
            _emit_tool(context, spec, "ok" if result.ok else "failed")
            return result.to_dict()

        if spec.preview is None or spec.apply is None:
            return {"error": "该工具未实现", "error_code": "not_implemented"}
        preview = spec.preview(actor, args, context)
        if not preview.ok:
            _audit(
                actor,
                context,
                tool=spec.name,
                risk=spec.risk,
                arguments=redact_sensitive(args),
                ok=False,
                error=preview.error,
            )
            _emit_tool(context, spec, "failed")
            return preview.to_dict()

        data = preview.data if isinstance(preview.data, dict) else {}
        # 预览函数用 ``apply_args`` 提供真正要落盘的参数（如角色卡的 payload），
        # 确认时必须使用它而不是模型的原始入参；``action_args`` 为历史别名。
        action_args = data.pop("action_args", None)
        if not isinstance(action_args, dict):
            action_args = data.pop("apply_args", None)
        action = create_action(
            actor_id=actor.user_id,
            conversation_id=context.conversation_id,
            tool=spec.name,
            args=action_args if isinstance(action_args, dict) else args,
            summary=preview.summary or str(data.get("summary") or ""),
            diff=data,
            risk=spec.risk,
            ttl_seconds=getattr(config, "action_ttl_seconds", 300),
            room_id=context.room_id,
        )
        context.pending_actions.append(
            {
                "kind": "pending_action",
                "action_id": action["id"],
                "tool": spec.name,
                "risk": spec.risk,
                "summary": action["summary"],
                "expires_at": action["expires_at"],
                "diff": data,
            }
        )
        _audit(
            actor,
            context,
            tool=spec.name,
            risk=spec.risk,
            arguments=redact_sensitive(args),
            ok=True,
            pending_action_id=action["id"],
        )
        _emit_tool(context, spec, "pending", action_id=action["id"])
        return {
            "ok": True,
            "data": data,
            "summary": action["summary"],
            "pending_action": {
                "action_id": action["id"],
                "summary": action["summary"],
                "risk": spec.risk,
                "expires_at": action["expires_at"],
            },
            "note": "尚未执行；用户需在界面点击确认后才会落盘。",
        }

    return wrapper


def build_tool_registry(actor: Any, context: ToolContext, config: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in exposed_specs(actor, config, room_id=context.room_id):
        registry.register(
            spec.build_agent_tool(_make_wrapper(spec, actor, context, config))
        )
    return registry


def apply_pending_action(actor: Any, action: dict[str, Any], *, config: Any) -> ToolResult:
    """执行已确认的待确认操作；执行前重新鉴权并复用 apply 实现。"""
    spec = SPECS_BY_NAME.get(str(action.get("tool") or ""))
    if spec is None:
        return ToolResult.failure("待确认操作对应的工具不存在")
    if spec.risk not in {"write", "danger"} or spec.apply is None:
        return ToolResult.failure("该操作不是可执行的写操作")
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    room_id = str(action.get("room_id") or "") or None
    decision = authorize_tool(actor, spec, args, config=config, room_id=room_id)
    if not decision.allowed:
        return ToolResult.failure(DENIAL_MESSAGES.get(decision.reason, decision.reason))
    context = ToolContext(
        actor=actor,
        config=config,
        conversation_id=str(action.get("conversation_id") or ""),
        turn_id=f"apply-{action.get('id')}",
        ip="",
        room_id=room_id,
    )
    return spec.apply(actor, args, context)
