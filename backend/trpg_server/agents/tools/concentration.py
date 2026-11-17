"""专注注册表工具（仅 dnd5e 房间）。

``concentration.list`` 刷新并列出房间级专注索引；``concentration.break``
走规则集插件的 ``end_concentration``，清理关联效果并同步注册表。
"""

from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.combat import (
    _append_log,
    _as_text,
    _combat_ruleset,
    _combat_settings,
    _combat_summary_state,
    _load_combat,
    _resolve_combatant,
    _save_combat,
)
from trpg_server.combat.settings import resolve_settings
from trpg_server.workflows import concentration


def _is_dnd5e(context: Any, info: dict[str, Any] | None = None) -> bool:
    info = info if info is not None else (
        context.room_info() if hasattr(context, "room_info") else {}
    )
    return str(resolve_settings(info).ruleset_id or "").strip().lower() == "dnd5e"


def list_concentration(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用专注注册表"}
    combat = _load_combat(context)
    if combat:
        state = concentration.sync(
            context.room_dir, concentration.holders_from_combat(combat)
        )
    else:
        info = context.room_info()
        state = concentration.sync(
            context.room_dir, concentration.holders_from_members(info.get("members"))
        )
    return {
        "active": [
            {"holder": name, **dict(entry)}
            for name, entry in (state.get("entries") or {}).items()
        ],
        "revision": state.get("revision"),
    }


def break_concentration(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_dnd5e(context):
        return {"error": "当前规则集未启用专注注册表"}
    holder = _as_text(arguments.get("holder") or arguments.get("target"))
    if not holder:
        return {"error": "缺少 holder"}
    reason = _as_text(arguments.get("reason")) or "手动打断"
    entry_before = concentration.get(context.room_dir, holder)
    combat = _load_combat(context)
    notes: list[str] = []
    if combat:
        target = _resolve_combatant(combat, holder)
        if target is None:
            return {"error": f"战斗中找不到“{holder}”"}
        ruleset = _combat_ruleset(combat, _combat_settings(context))
        spells = getattr(ruleset, "spells", None)
        if spells is None:
            return {"error": "当前规则集没有专注能力"}
        notes = spells.end_concentration(target, combat=combat, reason=reason)
        for text in notes:
            _append_log(combat, text, context)
        _save_combat(context, combat)
        concentration.sync(
            context.room_dir, concentration.holders_from_combat(combat)
        )
    else:
        from trpg_server import room_spells

        info = context.room_info()
        ruleset, _settings = room_spells.ruleset_for(info)
        spells = getattr(ruleset, "spells", None)
        if spells is None:
            return {"error": "当前规则集没有专注能力"}
        holders, members = room_spells._holder_map(info, ruleset, _settings)
        holder_obj = next(
            (item for name, item in holders.items() if name.casefold() == holder.casefold()),
            None,
        )
        if holder_obj is None:
            return {"error": f"房间里找不到“{holder}”"}
        notes = spells.end_concentration(holder_obj, combat={"combatants": holders}, reason=reason)
        room_spells._sync_all(members, holders, ruleset)
        from trpg_server.room_rules import save_room_info

        save_room_info(context.room_dir, info)
        concentration.sync(
            context.room_dir, concentration.holders_from_members(info.get("members"))
        )
    entry = concentration.remove(context.room_dir, holder, reason=reason)
    return {
        "broken": bool(entry_before or entry or notes),
        "entry": entry or entry_before,
        "notes": notes,
    }


CONCENTRATION_LIST_TOOL = AgentTool(
    name="concentration.list",
    description="列出当前房间所有正在专注的法术及其关联效果。",
    parameters={"type": "object", "properties": {}},
    handler=list_concentration,
)

CONCENTRATION_BREAK_TOOL = AgentTool(
    name="concentration.break",
    description=(
        "打断某个角色的专注：清理关联效果、同步注册表。用于失能、死亡、"
        "二次专注或 KP 裁定。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "holder": {"type": "string", "description": "专注者名称"},
            "reason": {"type": "string"},
        },
        "required": ["holder"],
    },
    handler=break_concentration,
)

CONCENTRATION_TOOLS = [CONCENTRATION_LIST_TOOL, CONCENTRATION_BREAK_TOOL]
CONCENTRATION_TOOL_NAMES = [tool.name for tool in CONCENTRATION_TOOLS]


__all__ = [
    "CONCENTRATION_BREAK_TOOL",
    "CONCENTRATION_LIST_TOOL",
    "CONCENTRATION_TOOLS",
    "CONCENTRATION_TOOL_NAMES",
    "break_concentration",
    "list_concentration",
]
