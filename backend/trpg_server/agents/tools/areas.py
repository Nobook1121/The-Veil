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
        "grid_ft": arguments.get("grid_ft"),
        "targets": arguments.get("targets"),
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


def _apply_area_trigger(
    context: Any,
    area_id: str,
    target: Any,
    round_number: int,
) -> dict[str, Any]:
    """真正结算一次区域触发；去重状态由 :mod:`workflows.areas` 维护。"""
    combat = _load_combat(context)
    if not combat:
        return {"error": "当前没有进行中的战斗，区域触发需要战斗上下文"}
    target_obj = _resolve_combatant(combat, target)
    if target_obj is None:
        return {"error": "缺少有效的 target"}
    area_id = _as_text(area_id)
    if not area_id:
        return {"error": "缺少 area_id"}
    ruleset = _combat_ruleset(combat, _combat_settings(context))
    spells = getattr(ruleset, "spells", None)
    if spells is None or not hasattr(spells, "trigger_area"):
        return {"error": "当前规则集没有区域结算能力"}
    target_name = _as_text(target_obj.get("name"))

    def apply(area: dict[str, Any], name: str) -> dict[str, Any]:
        return spells.trigger_area(
            area,
            target_obj,
            _player_card(target_obj, context),
            rng=_roller,
            combat=combat,
        )

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


def _run_area_trigger(
    context: Any,
    area_id: str,
    target: str,
    round_number: int,
    *,
    actor: str,
    hook: str,
    request_key: str = "",
) -> dict[str, Any]:
    """把一次区域判定包成工作流，事件日志可用 ``workflow.get`` 复盘。"""
    from trpg_server.workflows.actions import flow_result, run_flow_action
    from trpg_server.workflows.model import make_step

    outcome = run_flow_action(
        context.room_dir,
        kind="area",
        room_id=str(getattr(context, "room_id", "") or ""),
        initiator=actor or target,
        initiator_type="kp",
        steps=[make_step("trigger", "区域判定")],
        handlers={
            "trigger": lambda _flow, _step: _apply_area_trigger(
                context, area_id, target, round_number
            )
        },
        request_key=str(request_key or ""),
        metadata={"area_id": area_id, "target": target, "hook": hook, "round": round_number},
        lock_character=False,
    )
    if outcome.get("error"):
        return {"error": outcome["error"], "code": outcome.get("code")}
    flow = outcome.get("flow") or {}
    result = flow_result(flow, "trigger")
    if not isinstance(result, dict):
        return {"error": "区域结算返回了无法识别的结果"}
    if result.get("error"):
        return result
    return {
        **result,
        "workflow": {
            "id": flow.get("id"),
            "status": flow.get("status"),
            "duplicate": bool(outcome.get("duplicate")),
        },
    }


class _AutoContext:
    """自动钩子没有请求对象时，用房间信息构造最小上下文。"""

    def __init__(self, room_dir: Any, info: dict[str, Any]):
        from pathlib import Path

        self.room_dir = room_dir
        self.room_id = str(info.get("id") or "")
        self.config_dir = Path(room_dir) / "config"
        self.scenarios_dir = Path(room_dir) / "scenarios"
        self.user_id = 0
        self.agent_id = ""
        self._info = info

    def room_info(self) -> dict[str, Any]:
        return self._info


def _auto_trigger(
    room_dir: Any,
    area: dict[str, Any],
    actor: str,
    round_number: int,
    hook: str,
    context: Any = None,
) -> dict[str, Any]:
    """移动/回合开始钩子的公共入口；非 dnd5e 或无战斗时静默跳过。"""
    from uuid import uuid4

    if context is None:
        from trpg_server.room_rules import load_room_info

        context = _AutoContext(room_dir, load_room_info(room_dir))
    if not _is_dnd5e(context):
        return {}
    if not _load_combat(context):
        return {}
    round_number = _as_int(round_number, 0)
    if round_number:
        key = f"area:{area.get('id')}:{actor}:{round_number}:{hook}"
    else:
        # 非战斗没有回合号；用事件 id 避免永久复用同一条审计流程。
        key = f"area:{area.get('id')}:{actor}:time:{uuid4().hex[:10]}"
    return _run_area_trigger(
        context,
        _as_text(area.get("id")),
        actor,
        round_number,
        actor=actor,
        hook=hook,
        request_key=key,
    )


def install_hooks() -> None:
    """安装移动/回合开始的自动触发回调（模块导入时自动调用）。"""
    area_store.register_trigger_handler(_auto_trigger)


install_hooks()


def trigger_area(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用持续区域"}
    area_id = _as_text(arguments.get("area_id") or arguments.get("id"))
    target = _as_text(arguments.get("target"))
    if not area_id:
        return {"error": "缺少 area_id"}
    if not target:
        return {"error": "缺少有效的 target"}
    combat = _load_combat(context)
    round_number = _as_int(arguments.get("round"), _as_int((combat or {}).get("round"), 0))
    return _run_area_trigger(
        context,
        area_id,
        target,
        round_number,
        actor=target,
        hook="manual",
        request_key=_as_text(arguments.get("request_id")),
    )


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
            "request_id": {"type": "string", "description": "幂等键；重复提交只结算一次"},
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
