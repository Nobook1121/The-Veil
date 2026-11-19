"""D&D 5e 遭遇开战工具（`dnd5e.start_encounter`）。

只有 5e 房间会加载本模块；怪物与遭遇内容来自叙事图的兴趣点卡片，实际战斗
建立复用核心 `combat.start`，回合、先攻与怪物数值仍由规则集处理。
"""
from __future__ import annotations

import logging
from typing import Any

from trpg_server.agents.room_state import load_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.combat import start_combat

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


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
    if not monster_ids:
        return {
            "ok": False,
            "error": "遭遇缺少 monster_ids，无法建立战斗。",
            "code": "dnd5e.monster_missing",
        }
    try:
        count = max(1, int(encounter.get("count") or len(monster_ids)))
    except (TypeError, ValueError):
        count = len(monster_ids)

    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else room_info
    participants: list[Any] = _members(info if isinstance(info, dict) else {})
    for index in range(count):
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
    return {
        "ok": True,
        "poi_id": poi_id,
        "monsters": [item.get("monster_id") for item in participants if isinstance(item, dict)],
        "auto_start": bool(encounter.get("auto_start")),
        "combat": result,
    }


START_ENCOUNTER_TOOL = AgentTool(
    name="dnd5e.start_encounter",
    description=(
        "按兴趣点配置的遭遇块建立战斗：自动带入房间成员并按 monster_ids 生成怪物，"
        "先攻、突袭与数值由 D&D 5e 规则集结算。战斗中不要重复调用；"
        "若没有遭遇配置或怪物数据缺失，工具会显式报错，不要编造怪物数据。"
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
