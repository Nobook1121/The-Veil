"""D&D 5e 遭遇开战工具（`dnd5e.start_encounter`）。

只有 5e 房间会加载本模块；怪物与遭遇内容来自叙事图的兴趣点卡片，实际战斗
建立复用核心 `combat.start`，回合、先攻与怪物数值仍由规则集处理。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server.agents.room_state import load_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.combat import start_combat

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _scenario_module(context: Any, module_id: str) -> dict[str, Any] | None:
    """按 id 读取当前房间剧本里的 NPC 模块卡。"""
    scenarios_dir = getattr(context, "scenarios_dir", None)
    if not scenarios_dir or not module_id:
        return None
    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else {}
    scenario_id = info.get("scenario_id") if isinstance(info, dict) else None
    if scenario_id in (None, ""):
        return None
    from trpg_server.scenario_store import load_scenario_by_id

    try:
        _, scenario = load_scenario_by_id(
            Path(scenarios_dir), scenario_id, (info or {}).get("scenario_version")
        )
    except (OSError, ValueError):
        return None
    modules = scenario.get("modules") if isinstance(scenario, dict) else None
    for module in modules if isinstance(modules, list) else []:
        if isinstance(module, dict) and str(module.get("id")) == module_id:
            return module
    return None


def _members(info: Any) -> list[str]:
    names: list[str] = []
    for member in (info or {}).get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        name = _text(card.get("name") or member.get("character_name") or member.get("username"))
        if name and name not in names:
            names.append(name)
    return names


def _start_encounter(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir = getattr(context, "room_dir", None)
    if not room_dir:
        return {"ok": False, "error": "开战需要房间上下文。", "code": "dnd5e.no_room"}
    poi_id = _text(arguments.get("poi_id") or arguments.get("poi"))
    if not poi_id:
        return {"ok": False, "error": "请提供遭遇所在的兴趣点 id。", "code": "dnd5e.poi_required"}
    combat = load_room_state(room_dir).get("combat")
    if isinstance(combat, dict) and combat.get("active"):
        return {"ok": False, "error": "战斗已经开始。", "code": "combat.already_active"}

    from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError

    try:
        runtime = NarrativeRuntime.load(context)
        card = runtime.get_poi(poi_id, viewer="kp")
    except NarrativeRuntimeError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code or "dnd5e.poi_missing"}
    encounter = card.get("encounter")
    if not isinstance(encounter, dict):
        return {
            "ok": False,
            "error": "该兴趣点没有配置遭遇。",
            "code": "dnd5e.no_encounter",
        }
    monster_ids = [str(item) for item in encounter.get("monster_ids") or [] if str(item)]
    npc_ids = [str(item).strip() for item in encounter.get("npc_ids") or [] if str(item).strip()]
    if not monster_ids and not npc_ids:
        return {
            "ok": False,
            "error": "遭遇缺少 monster_ids 或 npc_ids，无法建立战斗。",
            "code": "dnd5e.monster_missing",
        }
    try:
        count = max(1, int(encounter.get("count") or len(monster_ids)))
    except (TypeError, ValueError):
        count = len(monster_ids)

    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else room_info
    participants: list[Any] = _members(info if isinstance(info, dict) else {})
    for npc_id in npc_ids:
        module = _scenario_module(context, npc_id)
        if module is None:
            return {
                "ok": False,
                "error": f"剧本中找不到 NPC 模块「{npc_id}」，无法建立战斗。",
                "code": "dnd5e.npc_missing",
            }
        from dnd5e_plugin.statblock import combatant_from_module

        try:
            spec = combatant_from_module(module)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "dnd5e.monster_missing",
            }
        participants.append(
            {
                "module_id": npc_id,
                "name": spec["name"],
                "is_npc": True,
                "side": spec.get("side") or "enemy",
            }
        )
    for index in range(count if monster_ids else 0):
        monster_id = monster_ids[index % len(monster_ids)]
        participants.append(
            {
                "name": monster_id,
                "monster_id": monster_id,
                "is_npc": True,
                "side": "enemy",
                "count": 1,
            }
        )
    payload: dict[str, Any] = {
        "participants": participants,
        "surprised": list(encounter.get("surprise") or []),
    }
    result = start_combat(payload, context)
    if isinstance(result, dict) and result.get("error"):
        return {
            "ok": False,
            "error": str(result["error"]),
            "code": "dnd5e.encounter_failed",
        }
    try:
        runtime.record(
            {
                "kind": "poi",
                "poi_id": poi_id,
                "triggered": True,
                "source": "encounter",
            },
            summary=f"在兴趣点 {poi_id} 开战",
        )
    except Exception:  # noqa: BLE001 - 战斗已经建立，标记失败不回滚
        logger.warning("dnd5e_encounter_poi_mark_failed", exc_info=True)
    # 战斗已经成立；地图棋子同步只做状态汇报，失败/无地图不改变战斗结果。
    try:
        from dnd5e_plugin.map_link import sync_combat_tokens
    except ImportError:  # 独立加载单个插件文件（如单元测试）时没有包上下文
        sync_combat_tokens = None

    current = load_room_state(room_dir).get("combat")
    map_status: dict[str, Any] = {"ok": True, "status": "skipped", "reason": "no_combat"}
    if sync_combat_tokens is not None and isinstance(current, dict):
        map_status = sync_combat_tokens(context, current)
        if map_status.get("status") == "skipped":
            logger.info(
                "dnd5e_encounter_map_skipped poi=%s reason=%s",
                poi_id,
                map_status.get("reason"),
            )
    return {
        "ok": True,
        "poi_id": poi_id,
        "monsters": [item.get("monster_id") for item in participants if isinstance(item, dict)],
        "npcs": npc_ids,
        "auto_start": bool(encounter.get("auto_start")),
        "combat": result,
        "map": map_status,
    }


START_ENCOUNTER_TOOL = AgentTool(
    name="dnd5e.start_encounter",
    description=(
        "按兴趣点配置的遭遇块建立战斗：自动带入房间成员，按 monster_ids 生成怪物，"
        "按 npc_ids 把剧本 NPC 转成 5e 战斗者并同步到地图棋子；"
        "先攻、突袭与数值由 D&D 5e 规则集结算。战斗中不要重复调用；"
        "若没有遭遇配置、NPC 或怪物数据缺失，工具会显式报错，不要编造数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "poi_id": {"type": "string", "description": "遭遇所在的兴趣点 id。"},
        },
        "required": ["poi_id"],
    },
    handler=_start_encounter,
    rulesets=("dnd5e",),
)


def create_tools() -> list[AgentTool]:
    return [START_ENCOUNTER_TOOL]


__all__ = ["START_ENCOUNTER_TOOL", "create_tools"]
