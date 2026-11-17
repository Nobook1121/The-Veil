"""持续区域工具（仅 dnd5e 房间）。

区域实体存放在场景级 ``scene_areas.json``；这里负责把规则集插件的判定结果
接到区域触发上，并保证同一目标同一回合只结算一次。CoC7 没有区域概念，
工具不会下发，handler 也会拒绝。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.combat import (
    _append_log,
    _as_int,
    _as_text,
    _combat_ruleset,
    _combat_settings,
    _combat_summary_state,
    _load_combat,
    _player_card,
    _resolve_combatant,
    _roller,
    _save_combat,
)
from trpg_server.combat.settings import resolve_settings
from trpg_server.workflows import areas as area_store
from trpg_server.workflows import concentration


def _is_dnd5e(context: Any, info: dict[str, Any] | None = None) -> bool:
    info = info if info is not None else (
        context.room_info() if hasattr(context, "room_info") else {}
    )
    return str(resolve_settings(info).ruleset_id or "").strip().lower() == "dnd5e"


def list_areas(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    scene_id = _as_text(arguments.get("scene_id")) or area_store.scene_id_of(context.room_dir)
    rows = area_store.list_areas(context.room_dir, scene_id=scene_id)
    return {"scene_id": scene_id, "areas": rows}


def create_area(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    spec = {
        "scene_id": _as_text(arguments.get("scene_id")),
        "name": _as_text(arguments.get("name")),
        "spell": _as_text(arguments.get("spell")),
        "source": _as_text(arguments.get("source")),
        "owner": _as_text(arguments.get("owner")),
        "shape": _as_text(arguments.get("shape")) or "sphere",
        "size_ft": arguments.get("size_ft"),
        "origin": arguments.get("origin"),
        "trigger": _as_text(arguments.get("trigger")) or "enter_or_end_turn",
        "save": arguments.get("save"),
        "damage": arguments.get("damage"),
        "effects": arguments.get("effect") or arguments.get("effects"),
        "duration": arguments.get("duration"),
        "rounds": arguments.get("rounds"),
        "concentration": bool(arguments.get("concentration")),
        "created_round": arguments.get("round"),
    }
    result = area_store.create_area(context.room_dir, spec)
    if result.get("error"):
        return result
    return {"area": result["area"], "revision": result["revision"]}


def trigger_area(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，区域触发需要战斗上下文"}
    target = _resolve_combatant(combat, arguments.get("target"))
    if target is None:
        return {"error": "缺少有效的 target"}
    area_id = _as_text(arguments.get("area_id") or arguments.get("id"))
    if not area_id:
        return {"error": "缺少 area_id"}
    round_number = _as_int(arguments.get("round"), _as_int(combat.get("round"), 0))
    ruleset = _combat_ruleset(combat, _combat_settings(context))
    spells = getattr(ruleset, "spells", None)
    if spells is None or not hasattr(spells, "trigger_area"):
        return {"error": "当前规则集没有区域结算能力"}
    target_name = _as_text(target.get("name"))

    def apply(area: dict[str, Any], name: str) -> dict[str, Any]:
        result = spells.trigger_area(
            area,
            target,
            _player_card(target, context),
            rng=_roller,
            combat=combat,
        )
        return result

    result = area_store.trigger(
        context.room_dir,
        area_id,
        target_name,
        round_number=round_number,
        apply=apply,
    )
    if result.get("error"):
        return result
    if result.get("duplicate"):
        return {
            "duplicate": True,
            "area": result.get("area"),
            "target": target_name,
            "round": round_number,
            "summary": f"{target_name} 本回合已经受到该区域影响，不再重复结算。",
        }
    notes = [f"{target_name} 触发持续区域“{result['area'].get('name')}”。"]
    _append_log(combat, notes[0], context)
    _save_combat(context, combat)
    concentration.sync(
        context.room_dir,
        concentration.holders_from_combat(combat),
    )
    return {
        **_combat_summary_state(combat),
        "duplicate": False,
        "area": result.get("area"),
        "target": target_name,
        "round": round_number,
        "result": result.get("result"),
        "summary": "；".join(notes),
    }


def remove_area(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    result = area_store.remove_area(
        context.room_dir,
        _as_text(arguments.get("area_id") or arguments.get("id")),
        reason=_as_text(arguments.get("reason")),
    )
    return result


def tick_areas(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    return area_store.tick_areas(context.room_dir, rounds=_as_int(arguments.get("rounds"), 1))


AREA_LIST_TOOL = AgentTool(
    name="area.list",
    description="列出当前场景的持续区域实体。",
    parameters={
        "type": "object",
        "properties": {"scene_id": {"type": "string", "description": "省略则用当前场景"}},
    },
    handler=list_areas,
)

AREA_CREATE_TOOL = AgentTool(
    name="area.create",
    description=(
        "在场景上创建一个持续区域实体：shape/size_ft、save、damage、duration.rounds、"
        "trigger。区域会按回合存在，同一目标每回合最多触发一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "spell": {"type": "string"},
            "source": {"type": "string"},
            "owner": {"type": "string"},
            "scene_id": {"type": "string"},
            "shape": {"type": "string"},
            "size_ft": {"type": "integer", "minimum": 0},
            "origin": {"type": "object"},
            "trigger": {"type": "string"},
            "save": {"type": "object"},
            "damage": {"type": "array"},
            "effect": {"type": "object"},
            "duration": {"type": "object"},
            "rounds": {"type": "integer", "minimum": 0},
            "concentration": {"type": "boolean"},
            "round": {"type": "integer"},
        },
        "required": ["name"],
    },
    handler=create_area,
)

AREA_TRIGGER_TOOL = AgentTool(
    name="area.trigger",
    description="让一个目标触发持续区域；同一区域同一目标同一回合只结算一次。",
    parameters={
        "type": "object",
        "properties": {
            "area_id": {"type": "string"},
            "target": {"type": "string"},
            "round": {"type": "integer"},
        },
        "required": ["area_id", "target"],
    },
    handler=trigger_area,
)

AREA_REMOVE_TOOL = AgentTool(
    name="area.remove",
    description="移除一个持续区域实体。",
    parameters={
        "type": "object",
        "properties": {
            "area_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["area_id"],
    },
    handler=remove_area,
)

AREA_TICK_TOOL = AgentTool(
    name="area.tick",
    description="推进持续区域的剩余回合，到期的区域会被移除。",
    parameters={
        "type": "object",
        "properties": {"rounds": {"type": "integer", "minimum": 1}},
    },
    handler=tick_areas,
)

AREA_TOOLS = [
    AREA_LIST_TOOL,
    AREA_CREATE_TOOL,
    AREA_TRIGGER_TOOL,
    AREA_REMOVE_TOOL,
    AREA_TICK_TOOL,
]
AREA_TOOL_NAMES = [tool.name for tool in AREA_TOOLS]


__all__ = ["AREA_TOOLS", "AREA_TOOL_NAMES", "create_area", "list_areas", "trigger_area"]
