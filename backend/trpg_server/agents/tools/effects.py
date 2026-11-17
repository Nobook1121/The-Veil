"""通用效果工具（规则中立命名）。

``effect.list`` / ``effect.apply`` / ``effect.remove`` 操作核心的效果容器：
增删查、持续时间与作用域由核心维护，条件的含义、力竭分级与回合 tick 由规则集
插件决定。没有 ``effects`` 能力的规则集不会拿到这三个工具。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server import room_spells
from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.combat import (
    _append_log,
    _as_int,
    _as_text,
    _combat_ruleset,
    _combat_settings,
    _combat_summary_state,
    _load_combat,
    _resolve_combatant,
    _save_combat,
)
from trpg_server.ruleset.effects import effect_summary


def _require_effects(combat: dict[str, Any], context: Any) -> tuple[Any, dict[str, Any] | None]:
    ruleset = _combat_ruleset(combat, _combat_settings(context))
    effects = getattr(ruleset, "effects", None)
    if effects is None:
        return ruleset, {"error": "当前规则集没有状态与效果能力"}
    return effects, None


def list_effects(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """列出战斗中或非战斗角色身上的效果实例。"""
    wanted = _as_text(arguments.get("target"))
    combat = _load_combat(context)
    if not combat:
        info = context.room_info()
        ruleset, _settings = room_spells.ruleset_for(info)
        if not room_spells.effects_available(ruleset):
            return {"error": "当前规则集没有状态与效果能力"}
        rows: list[dict[str, Any]] = []
        for member in info.get("members") or []:
            if not isinstance(member, dict):
                continue
            card = member.get("character_card") if isinstance(member.get("character_card"), Mapping) else {}
            name = _as_text(card.get("name")) or _as_text(member.get("username"))
            if wanted and wanted.casefold() not in {name.casefold(), _as_text(member.get("username")).casefold()}:
                continue
            state = member.get("character_state") if isinstance(member.get("character_state"), Mapping) else {}
            rows.append(
                {
                    "name": name,
                    "concentration": state.get("concentration") or None,
                    "effects": effect_summary(state),
                }
            )
        return {"noncombat": True, "effects": rows}
    _effects, error = _require_effects(combat, context)
    if error:
        return error
    rows = []
    for name, combatant in (combat.get("combatants") or {}).items():
        if not isinstance(combatant, dict):
            continue
        if wanted and wanted.casefold() not in {
            str(name).casefold(),
            _as_text(combatant.get("name")).casefold(),
        }:
            continue
        rows.append({"name": name, "effects": effect_summary(combatant)})
    return {**_combat_summary_state(combat), "effects": rows}


def apply_effect(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """给目标附加条件、力竭等级或自定义效果。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    effects, error = _require_effects(combat, context)
    if error:
        return error
    target = _resolve_combatant(combat, arguments.get("target"))
    if target is None:
        return {"error": "缺少有效的 target"}
    payload: dict[str, Any] = {
        "condition": _as_text(arguments.get("condition") or arguments.get("condition_id")),
        "levels": arguments.get("levels"),
        "source": _as_text(arguments.get("source")),
        "duration": arguments.get("duration"),
        "dc": arguments.get("dc"),
        "save_ends": arguments.get("save_ends"),
        "effect": arguments.get("effect"),
    }
    result = effects.apply(target, payload)
    if result.get("error"):
        return result
    for text in result.get("notes") or []:
        _append_log(combat, str(text), context)
    _save_combat(context, combat)
    return {
        **_combat_summary_state(combat),
        "effect": result.get("effect"),
        "summary": "；".join(str(item) for item in result.get("notes") or []),
    }


def remove_effect_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """移除目标身上的效果：按效果 id、条件 id 或 kind。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，请先调用 combat.start"}
    effects, error = _require_effects(combat, context)
    if error:
        return error
    target = _resolve_combatant(combat, arguments.get("target"))
    if target is None:
        return {"error": "缺少有效的 target"}
    identifier = _as_text(
        arguments.get("id") or arguments.get("effect_id") or arguments.get("condition")
    )
    if not identifier:
        return {"error": "缺少要移除的效果 id 或条件"}
    result = effects.remove(target, identifier)
    for text in result.get("notes") or []:
        _append_log(combat, str(text), context)
    _save_combat(context, combat)
    return {
        **_combat_summary_state(combat),
        "removed": result.get("removed") or [],
        "summary": "；".join(str(item) for item in result.get("notes") or []),
    }


def advance_scene(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """非战斗场景显式推进时间，按分钟结算持续时间与到期效果。"""
    if _load_combat(context):
        return {"error": "战斗进行中请用回合推进；该工具只用于战斗外的场景时间"}
    minutes = _as_int(arguments.get("minutes"), 0) + 60 * _as_int(arguments.get("hours"), 0)
    if minutes <= 0:
        return {"error": "请给出 minutes 或 hours（正整数）"}
    result = room_spells.advance_minutes(context.room_dir, context.room_info(), minutes=minutes)
    if result.get("error"):
        return result
    return {**result, "noncombat": True}


EFFECT_LIST_TOOL = AgentTool(
    name="effect.list",
    description="列出战斗中每个角色身上的效果实例（条件、力竭、临时效果）。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "只看某个角色，省略则列出全部"},
        },
    },
    handler=list_effects,
)

EFFECT_APPLY_TOOL = AgentTool(
    name="effect.apply",
    description=(
        "给目标附加状态：condition 为规则集数据包中的条件 id，levels 供分级状态使用；"
        "也可直接传 effect 自定义效果。持续时间与结束时点由插件解释。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "condition": {"type": "string", "description": "数据包中的条件 id"},
            "levels": {"type": "integer", "minimum": 1, "description": "分级状态的等级增量"},
            "source": {"type": "string", "description": "来源法术/来源名"},
            "duration": {"type": "object", "description": "通用持续时间，如 {unit, remaining, timing}"},
            "dc": {"type": "integer", "description": "结束时的重复豁免 DC"},
            "save_ends": {"type": "object", "description": "结束时点与豁免属性"},
            "effect": {"type": "object", "description": "直接给一个通用效果实例"},
        },
        "required": ["target"],
    },
    handler=apply_effect,
)

EFFECT_REMOVE_TOOL = AgentTool(
    name="effect.remove",
    description="按效果 id、条件 id 或 kind 移除目标身上的效果。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "id": {"type": "string", "description": "效果 id"},
            "condition": {"type": "string", "description": "条件 id"},
        },
        "required": ["target"],
    },
    handler=remove_effect_tool,
)

SCENE_ADVANCE_TOOL = AgentTool(
    name="scene.advance",
    description=(
        "非战斗场景显式推进时间（分钟/小时），按规则集插件的持续时间结算效果到期。"
        "战斗进行中不可用；CoC 等没有效果能力的规则集不会看到该工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "minimum": 1},
            "hours": {"type": "integer", "minimum": 1},
        },
    },
    handler=advance_scene,
)

EFFECT_TOOLS = [EFFECT_LIST_TOOL, EFFECT_APPLY_TOOL, EFFECT_REMOVE_TOOL, SCENE_ADVANCE_TOOL]
EFFECT_TOOL_NAMES = [tool.name for tool in EFFECT_TOOLS]
