"""图谱模式下的触发器工具（§5.8.3）。

`trigger.list_available` 只返回 id 与 label，防止模型提前读出触发器正文；
`trigger.request` 只放行有状态证据（检定/地点/线索/NPC）或 manual 绑定的触发器。
"""
from __future__ import annotations

from typing import Any

from trpg_server.agents.narrative.runtime import NarrativeRuntime
from trpg_server.agents.tools.base import AgentTool


def _runtime(context: Any) -> NarrativeRuntime:
    return NarrativeRuntime.load(context)


def list_available(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _runtime(context).list_available_triggers()


def request_trigger(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    trigger_id = arguments.get("trigger_id") or arguments.get("trigger")
    if not trigger_id:
        return {"error": "trigger_id is required"}
    condition_met = arguments.get("condition_met")
    if condition_met is not None:
        condition_met = bool(condition_met)
    return _runtime(context).request_trigger(
        trigger_id,
        condition_met=condition_met,
        check_id=arguments.get("check_id"),
        pc=arguments.get("pc"),
        action_text=str(arguments.get("action_text") or ""),
    )


LIST_AVAILABLE_TOOL = AgentTool(
    name="trigger.list_available",
    description=(
        "列出当前地点或 manual 可触发的触发器，只返回 trigger_id 与 label，不返回正文、附件或条件。"
        "不得绕过本工具自行编造触发器内容。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=list_available,
)

REQUEST_TRIGGER_TOOL = AgentTool(
    name="trigger.request",
    description=(
        "请求触发一个触发器。引擎会校验 binding 证据、condition、once、cooldown 与可见性；"
        "manual 绑定必须提供与要求一致的 action_text，检定类必须提供 check_id。"
        "被拒绝时不得改写为已触发。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "触发器 id，来自 trigger.list_available。"},
            "condition_met": {
                "type": "boolean",
                "description": "模型判断的触发条件是否成立；引擎仍会独立复核。",
            },
            "check_id": {"type": "string", "description": "检定类触发器所需的 check_id。"},
            "pc": {"type": "string", "description": "可选：触发者 PC id，用于 once_scope=pc。"},
            "action_text": {"type": "string", "description": "manual 触发器对应的玩家动作文本。"},
        },
        "required": ["trigger_id"],
    },
    handler=request_trigger,
)

TRIGGER_RUNTIME_TOOLS = [LIST_AVAILABLE_TOOL, REQUEST_TRIGGER_TOOL]
TRIGGER_RUNTIME_TOOL_NAMES = [tool.name for tool in TRIGGER_RUNTIME_TOOLS]

__all__ = [
    "LIST_AVAILABLE_TOOL",
    "REQUEST_TRIGGER_TOOL",
    "TRIGGER_RUNTIME_TOOL_NAMES",
    "TRIGGER_RUNTIME_TOOLS",
]
