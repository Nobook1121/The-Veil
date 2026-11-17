"""反应待决队列工具（仅 dnd5e 房间）。

``reaction.list`` 查看某个角色或全房间等待中的反应窗口；``reaction.resolve``
提交“使用/不使用”决定。决定提交后由流程续跑器继续施法或扣位。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.combat.settings import resolve_settings
from trpg_server.workflows import reactions
from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import FLOW_WAITING


def _is_dnd5e(context: Any) -> bool:
    info = context.room_info() if hasattr(context, "room_info") else {}
    return str(resolve_settings(info).ruleset_id or "").strip().lower() == "dnd5e"


def _reaction_steps(flow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in flow.get("steps") or []
        if isinstance(step, dict)
        and str(step.get("id") or "").startswith(reactions.REACTION_PREFIX)
        and str(step.get("status")) == "waiting"
    ]


def list_reactions(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用反应队列"}
    candidate = str(arguments.get("candidate") or arguments.get("actor") or "").strip()
    engine = WorkflowEngine(context.room_dir, room_id=str(getattr(context, "room_id", "") or ""))
    if candidate:
        rows = reactions.pending_for(context.room_dir, candidate)
    else:
        rows = [
            flow
            for flow in engine.list(status=FLOW_WAITING, limit=100)
            if _reaction_steps(flow)
        ]
    return {
        "pending": [
            {
                "flow_id": flow.get("id"),
                "kind": flow.get("kind"),
                "caster": flow.get("initiator"),
                "metadata": {
                    "spell": (flow.get("metadata") or {}).get("spell"),
                    "slot_level": (flow.get("metadata") or {}).get("slot_level"),
                    "targets": (flow.get("metadata") or {}).get("targets"),
                },
                "candidates": [
                    reactions.candidate_of(str(step.get("id")))
                    for step in _reaction_steps(flow)
                ],
            }
            for flow in rows
        ]
    }


def resolve_reaction(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用反应队列"}
    flow_id = str(arguments.get("flow_id") or "").strip()
    candidate = str(arguments.get("candidate") or arguments.get("actor") or "").strip()
    if not flow_id or not candidate:
        return {"error": "缺少 flow_id 或 candidate"}
    decision = arguments.get("decision")
    if decision is None:
        decision = {"decision": "none"}
    result = reactions.decide(
        context.room_dir,
        flow_id,
        candidate,
        decision,
        idempotency_key=str(arguments.get("idempotency_key") or ""),
        actor=candidate,
        actor_user_id=getattr(context, "user_id", ""),
        agent_id=getattr(context, "agent_id", ""),
        is_manager=str(getattr(context, "agent_id", "") or "").strip().lower() == "kp",
    )
    if result.get("error"):
        return result
    flow = result.get("flow") or {}
    continuation = flow.get("continuation_result") if isinstance(flow.get("continuation_result"), dict) else {}
    return {
        "duplicate": bool(result.get("duplicate")),
        "workflow": {
            "id": flow.get("id"),
            "status": flow.get("status"),
        },
        "countered": bool(continuation.get("countered")),
        "countered_by": continuation.get("countered_by"),
        "continuation": continuation,
    }


REACTION_LIST_TOOL = AgentTool(
    name="reaction.list",
    description="查看等待中的反应窗口（如反制法术），可按角色过滤。",
    parameters={
        "type": "object",
        "properties": {"candidate": {"type": "string", "description": "省略则列出全部"}},
    },
    handler=list_reactions,
)

REACTION_RESOLVE_TOOL = AgentTool(
    name="reaction.resolve",
    description=(
        "提交反应决定：decision={decision:'use'} 或 {decision:'none'}。"
        "同一幂等键只生效一次；决定后流程自动续跑。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "flow_id": {"type": "string"},
            "candidate": {"type": "string"},
            "decision": {"type": "object"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["flow_id", "candidate"],
    },
    handler=resolve_reaction,
)

REACTION_TOOLS = [REACTION_LIST_TOOL, REACTION_RESOLVE_TOOL]
REACTION_TOOL_NAMES = [tool.name for tool in REACTION_TOOLS]


__all__ = [
    "REACTION_LIST_TOOL",
    "REACTION_RESOLVE_TOOL",
    "REACTION_TOOLS",
    "REACTION_TOOL_NAMES",
    "list_reactions",
    "resolve_reaction",
]
