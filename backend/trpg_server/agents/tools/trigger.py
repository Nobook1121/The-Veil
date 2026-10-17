from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.trigger_system import find_trigger_definition, validate_trigger
from trpg_server.scenario_store import build_trigger_message, find_trigger_by_id, load_scenario_by_id


def _condition_requires_check(condition: str) -> bool:
    text = condition.casefold()
    return any(
        marker in text
        for marker in ("检定", "鉴定", "判定", "技能", "属性", "灵感", "check", "roll", "skill", "attribute")
    )


def _reveal_library_trigger(
    scenario: dict[str, Any], trigger_id: Any, context: Any, arguments: dict[str, Any]
) -> dict[str, Any]:
    """解析并揭示「触发器资源库」中的触发器（scenario.trigger_cards / 模块附件）。

    场景栏位触发器由 find_trigger_by_id 处理，资源库触发器没有内联 content，
    内容来自 attachment.resourceRef。这里复用 validate_trigger（条件、剧本版本、
    可见性、剧透等级、是否已触发等）做权威校验，避免 KP 直接泄露未解锁内容。
    """
    definition = find_trigger_definition(scenario, str(trigger_id))
    if not definition:
        return {"error": f"trigger {trigger_id} was not found", "trigger_id": trigger_id}

    room_state = context.room_state() if callable(getattr(context, "room_state", None)) else {}
    room_state = room_state if isinstance(room_state, dict) else {}
    room_info = context.room_info() if callable(getattr(context, "room_info", None)) else {}
    room_info = room_info if isinstance(room_info, dict) else {}

    validation = validate_trigger(
        str(trigger_id),
        {**room_state, "scenario_version": room_info.get("scenario_version")},
        scenario,
        audience="kp",
    )
    if not validation.get("ok"):
        return {
            "error": f"trigger {trigger_id} was not approved: {validation.get('reason')}",
            "trigger_id": trigger_id,
            "reason": validation.get("reason"),
        }

    approved = validation.get("trigger") or {}
    attachments = [item for item in approved.get("attachments", []) if isinstance(item, dict)]
    contents: list[str] = []
    for attachment in attachments:
        resource = attachment.get("resourceRef") or {}
        if resource.get("content"):
            contents.append(str(resource["content"]))
        elif resource.get("url"):
            contents.append(f"[{resource.get('alt')}]({resource.get('url')})")
    if not contents:
        return {"error": f"trigger {trigger_id} has no revealable content", "trigger_id": trigger_id}

    message = {
        "type": "trigger",
        "sender_name": str(approved.get("text") or f"触发器{trigger_id}"),
        "avatar": "",
        "content": "\n\n".join(contents),
        "metadata": {
            "trigger_id": str(trigger_id),
            "content_mode": "attachment",
            "scene_id": approved.get("sceneId"),
        },
    }
    return {
        "triggered": True,
        "trigger_id": str(trigger_id),
        "condition": attachments[0].get("condition") if attachments else None,
        "direct_message": message,
        "message": message,
    }


def reveal_scenario_trigger(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_info = context.room_info()
    scenario_id = room_info.get("scenario_id")
    if scenario_id in (None, ""):
        return {"error": "current room has no bound scenario"}

    trigger_id = arguments.get("trigger_id") or arguments.get("id")
    if trigger_id in (None, ""):
        return {"error": "trigger_id is required"}

    scenarios_dir = context.scenarios_dir
    if not scenarios_dir:
        return {"error": "scenarios directory is not configured"}

    _, scenario = load_scenario_by_id(scenarios_dir, scenario_id, scenario_version=room_info.get("scenario_version"))
    if not scenario:
        return {"error": f"scenario {scenario_id} was not found"}

    trigger = find_trigger_by_id(scenario, trigger_id)
    if not trigger:
        # 场景栏位触发器未命中时回退到「触发器资源库」（trigger_cards / 模块附件）。
        return _reveal_library_trigger(scenario, trigger_id, context, arguments)

    condition = str(trigger.get("condition") or "").strip()
    condition_met = arguments.get("condition_met")
    tool_state = getattr(context, "tool_state", {}) or {}
    is_explicit_trigger_request = bool(tool_state.get("allow_unconditional_trigger"))
    if not condition and not is_explicit_trigger_request:
        return {
            "error": (
                "trigger has no explicit condition; do not reveal it automatically. "
                "A room manager must use the manual trigger command."
            ),
            "trigger_id": trigger_id,
        }
    if condition:
        if not isinstance(condition_met, bool):
            return {
                "error": "condition_met must be provided for conditional triggers",
                "trigger_id": trigger_id,
                "condition": condition,
            }
        if _condition_requires_check(condition):
            check_result = tool_state.get("last_check")
            if not isinstance(check_result, dict) or "success" not in check_result:
                return {
                    "error": "conditional trigger requires a preceding dice/check tool result",
                    "trigger_id": trigger_id,
                    "condition": condition,
                }
            check_name = str(arguments.get("check_name") or "").strip()
            if check_name and str(check_result.get("name") or check_result.get("skill") or "").casefold() != check_name.casefold():
                return {
                    "error": "the preceding check does not match check_name",
                    "trigger_id": trigger_id,
                    "condition": condition,
                }
            if bool(check_result.get("success")) != condition_met:
                return {
                    "error": "condition_met does not match the preceding check result",
                    "trigger_id": trigger_id,
                    "condition": condition,
                    "check_success": bool(check_result.get("success")),
                }
        if not condition_met:
            return {
                "triggered": False,
                "trigger_id": trigger_id,
                "condition": condition,
                "message": "trigger condition was not met",
            }

    message = build_trigger_message(scenario, trigger_id)
    if not message:
        return {"error": f"trigger {trigger_id} was not found"}

    return {
        "triggered": True,
        "condition": condition,
        "direct_message": message,
        "message": message,
    }


def reveal_trigger_after_check(trigger_id: Any, success: bool, context: Any) -> dict[str, Any]:
    """在检定完成的同一轮内按检定结果揭示触发器。

    检定工具可选的 ``trigger_id`` 参数会走到这里：复用 :func:`reveal_scenario_trigger`
    的全部条件/版本/可见性/剧透校验，并强制以工具自身算出的 ``success`` 作为
    ``condition_met``，模型无法改写判定。检定失败时不显示内容，与 kp.md 一致。
    """
    if not success:
        return {
            "triggered": False,
            "trigger_id": str(trigger_id),
            "message": "check failed; trigger content withheld",
        }
    tool_state = getattr(context, "tool_state", None)
    if isinstance(tool_state, dict):
        # reveal_scenario_trigger 会校验「前一次检定」的存在与结果；同轮合并时先写入，
        # 使语义与原先「先检定、再单独调用触发器工具」完全等价。
        tool_state["last_check"] = {"success": True}
    return reveal_scenario_trigger({"trigger_id": trigger_id, "condition_met": True}, context)


REVEAL_SCENARIO_TRIGGER_TOOL = AgentTool(
    name="trigger.reveal_scenario_trigger",
    description=(
        "Reveal a stored trigger by id after its condition has been evaluated. Supports both scene-slot triggers "
        "and triggers from the scenario trigger library (trigger_cards). "
        "For check-based conditions, a preceding check.roll_room_check or dice.roll_coc_check result is required. "
        "Do not invent trigger content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "trigger_id": {
                "type": ["integer", "string"],
                "description": "场景栏位触发器为数字 id；触发器资源库卡片为字符串 id（如 trg_...）。",
            },
            "condition_met": {
                "type": "boolean",
                "description": "Whether the trigger condition is satisfied after evaluating the current action and any check result.",
            },
            "check_name": {
                "type": "string",
                "description": "The skill or attribute name used by the preceding check, when applicable.",
            },
        },
        "required": ["trigger_id"],
    },
    handler=reveal_scenario_trigger,
)
