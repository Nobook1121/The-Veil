"""工作流查询与决策工具（仅 dnd5e 房间下发）。

对应第二段自动化底座：跨请求查看、取消、超时收尾与提交待决决定。
CoC7 没有自动化工作流，工具在 handler 里再校验一次规则集，避免被直接调用。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.combat.settings import resolve_settings
from trpg_server.workflows.engine import WorkflowEngine


def _engine(context: Any, info: dict[str, Any] | None = None) -> tuple[WorkflowEngine | None, dict[str, Any] | None]:
    room_dir = getattr(context, "room_dir", None)
    if not room_dir:
        return None, {"error": "缺少房间上下文"}
    if info is None:
        info = context.room_info() if hasattr(context, "room_info") else {}
    ruleset_id = str(resolve_settings(info).ruleset_id or "").strip().lower()
    if ruleset_id != "dnd5e":
        return None, {"error": "当前规则集未启用行动自动化工作流"}
    return WorkflowEngine(room_dir, room_id=str(getattr(context, "room_id", "") or "")), None


def _summary(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": flow.get("id"),
        "kind": flow.get("kind"),
        "status": flow.get("status"),
        "initiator": flow.get("initiator"),
        "initiator_type": flow.get("initiator_type"),
        "created_at": flow.get("created_at"),
        "expires_at": flow.get("expires_at"),
        "steps": [
            {
                "id": step.get("id"),
                "label": step.get("label"),
                "policy": step.get("policy"),
                "status": step.get("status"),
                "result": step.get("result") if step.get("status") == "waiting" else None,
            }
            for step in flow.get("steps") or []
            if isinstance(step, dict)
        ],
        "metadata": flow.get("metadata") or {},
    }


def list_workflows(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    engine, error = _engine(context)
    if error:
        return error
    rows = engine.list(
        status=str(arguments.get("status") or "").strip() or None,
        limit=int(arguments.get("limit") or 20),
    )
    return {"workflows": [_summary(flow) for flow in rows]}


def get_workflow(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    engine, error = _engine(context)
    if error:
        return error
    flow_id = str(arguments.get("flow_id") or "").strip()
    flow = engine.get(flow_id)
    if flow is None:
        return {"error": "工作流不存在", "code": "workflow.not_found"}
    return {"workflow": _summary(flow), "events": flow.get("events") or []}


def cancel_workflow(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    engine, error = _engine(context)
    if error:
        return error
    result = engine.cancel(
        str(arguments.get("flow_id") or ""),
        actor=str(getattr(context, "user_id", "") or ""),
        reason=str(arguments.get("reason") or ""),
    )
    if result.get("error"):
        return result
    return {"cancelled": result.get("cancelled"), "workflow": _summary(result["flow"])}


def decide_workflow(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    engine, error = _engine(context)
    if error:
        return error
    decision = arguments.get("decision")
    if decision is None:
        decision = {}
    result = engine.submit(
        str(arguments.get("flow_id") or ""),
        str(arguments.get("step_id") or ""),
        decision=decision,
        idempotency_key=str(arguments.get("idempotency_key") or ""),
        actor=str(getattr(context, "user_id", "") or ""),
    )
    if result.get("error"):
        return result
    return {
        "duplicate": bool(result.get("duplicate")),
        "workflow": _summary(result["flow"]),
        "result": result.get("result"),
    }


def expire_workflows(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    engine, error = _engine(context)
    if error:
        return error
    flow_id = str(arguments.get("flow_id") or "").strip()
    if flow_id:
        result = engine.expire(flow_id)
        if result.get("error"):
            return result
        return {"expired": result.get("expired"), "workflow": _summary(result["flow"])}
    expired = engine.expire_due()
    return {"expired": expired, "count": len(expired)}


def workflow_policy(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """读取或合并房间级策略：``{step: {kp/player/ai: auto|ask|manual}}``。"""
    engine, error = _engine(context)
    if error:
        return error
    overrides = arguments.get("policy")
    if isinstance(overrides, dict):
        effective = engine.set_policy(overrides)
        return {"policy": effective, "updated": True}
    return {"policy": engine.policy(), "updated": False}


WORKFLOW_LIST_TOOL = AgentTool(
    name="workflow.list",
    description="列出当前房间的行动工作流（施法/攻击），可按状态过滤。",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "running / waiting / completed / cancelled / expired / failed"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
    handler=list_workflows,
)

WORKFLOW_GET_TOOL = AgentTool(
    name="workflow.get",
    description="查看单个行动工作流的步骤、状态与事件日志。",
    parameters={
        "type": "object",
        "properties": {"flow_id": {"type": "string"}},
        "required": ["flow_id"],
    },
    handler=get_workflow,
)

WORKFLOW_CANCEL_TOOL = AgentTool(
    name="workflow.cancel",
    description="取消一个未结束的行动工作流，并释放该角色的行动锁。",
    parameters={
        "type": "object",
        "properties": {
            "flow_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["flow_id"],
    },
    handler=cancel_workflow,
)

WORKFLOW_DECIDE_TOOL = AgentTool(
    name="workflow.decide",
    description=(
        "提交待决步骤的决定（如是否使用反应、豁免结果）。同一 idempotency_key "
        "重复提交只生效一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "flow_id": {"type": "string"},
            "step_id": {"type": "string"},
            "decision": {"type": "object", "description": "决定内容，如 {decision: none}"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["flow_id", "step_id"],
    },
    handler=decide_workflow,
)

WORKFLOW_EXPIRE_TOOL = AgentTool(
    name="workflow.expire",
    description="按 TTL 收尾超时工作流；不传 flow_id 时处理全部到期流程。",
    parameters={
        "type": "object",
        "properties": {"flow_id": {"type": "string"}},
    },
    handler=expire_workflows,
)

WORKFLOW_POLICY_TOOL = AgentTool(
    name="workflow.policy",
    description=(
        "读取或设置自动化策略。策略按步骤 × 执行者（kp/player/ai）配置 "
        "auto / ask / manual，例如 {\"save\": {\"player\": \"ask\"}}。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "policy": {
                "type": "object",
                "description": "要合并的策略覆盖；省略则只读取当前策略",
            }
        },
    },
    handler=workflow_policy,
)

WORKFLOW_TOOLS = [
    WORKFLOW_LIST_TOOL,
    WORKFLOW_GET_TOOL,
    WORKFLOW_CANCEL_TOOL,
    WORKFLOW_DECIDE_TOOL,
    WORKFLOW_EXPIRE_TOOL,
    WORKFLOW_POLICY_TOOL,
]
WORKFLOW_TOOL_NAMES = [tool.name for tool in WORKFLOW_TOOLS]
WORKFLOW_RULESETS = ("dnd5e",)


__all__ = [
    "WORKFLOW_CANCEL_TOOL",
    "WORKFLOW_DECIDE_TOOL",
    "WORKFLOW_EXPIRE_TOOL",
    "WORKFLOW_GET_TOOL",
    "WORKFLOW_LIST_TOOL",
    "WORKFLOW_POLICY_TOOL",
    "WORKFLOW_RULESETS",
    "WORKFLOW_TOOL_NAMES",
    "WORKFLOW_TOOLS",
]
