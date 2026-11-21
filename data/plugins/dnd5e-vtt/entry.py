"""D&D 5e 战桌插件（dnd5e-vtt）服务端：面板数据端点 + 观察者裁剪。

M2 第一刀只做骨架：

- ``GET /api/plugins/dnd5e-vtt/panels?room=<room_id>``：按观察者（DM/玩家）
  裁剪的面板清单。**场景面板与角色面板的 NPC/怪物分组只对 DM 下发**，
  玩家的响应里整块不出现（不是前端隐藏）；前端只消费、不做本地权限判断。
- 战桌 UI 由 ``ui/dnd5e-vtt.js`` 通过 E0 的 ``room.rail.right`` 槽挂载；
  布局沿用 dnd5e 规则集已声明的 ``dnd5e-canvas``，本插件不重复声明
  workspace，也不动 dnd5e / veil-map 的任何文件。
- veil-map（地图）是**可选**依赖：不 import 它的 Python 模块、不读它的
  存储文件；地图相关面板（场景列表）给占位空态与接线点，不造假数据。

数据全部取自现有核心存储：战斗快照与事件流来自房间状态（``state.json``），
玩家角色来自成员卡，NPC 目录来自剧本 NPC 模块（世界包导入的产物）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, request, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import resolve_rule_id

bp = Blueprint("dnd5e_vtt", __name__, url_prefix="/api/plugins/dnd5e-vtt")
logger = logging.getLogger(__name__)

SUPPORTED_RULESETS = {"dnd5e"}
OWNER_ROLES = {"owner", "admin"}
MAX_JOURNAL_ENTRIES = 30
MAX_NPCS = 60
MAX_PLAYERS = 60
# 玩家侧日志只放行公开事件类型；KP 手记（room console）等不入玩家流。
PLAYER_SAFE_EVENT_KINDS = {"player_decision"}

PANEL_CHAT = {
    "id": "chat",
    "title": "聊天",
    "hint": "房间聊天流：发言、掷骰卡与检定回执都在这里。",
}
PANEL_ITEMS = {
    "id": "items",
    "title": "物品",
    "items": [],
    "hint": "物品与战利品目录将在角色卡装备与剧本数据接入后列出。",
}


def _login_required():
    if "user_id" not in session:
        return error_response("请先登录。", 401, "auth.required")
    return None


def _active_member(info: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for item in info.get("members") or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_active", True) is False or item.get("status") == "removed":
            continue
        if str(item.get("user_id")) == user_id:
            return item
    return None


def _is_manager(info: dict[str, Any]) -> bool:
    """观察者角色判定：房主/房管/站点管理员视为 DM；权限口径与核心一致。"""
    user_id = str(session.get("user_id") or "")
    member = _active_member(info, user_id)
    role = str((member or {}).get("room_role") or "").strip().lower()
    if member is not None and str(member.get("user_id")) == str(info.get("creator_id")):
        role = "owner"
    return bool(is_elevated() or role in OWNER_ROLES)


def _ruleset_id(info: dict[str, Any]) -> str:
    """规则集解析：优先核心解析器；解析失败时退回房间显式值（学 veil-map）。"""
    try:
        return str(resolve_rule_id(info, scenarios_dir=current_app.config.get("SCENARIOS_DIR")) or "").strip().lower()
    except Exception:  # noqa: BLE001 - 未启用规则集等场景退回显式字段再判断
        return str((info or {}).get("ruleset_id") or "").strip().lower()


def _combat_view(state: dict[str, Any]) -> dict[str, Any]:
    """战斗面板数据骨架：先攻名单来自房间战斗状态，未开战给空态。

    完整战斗交互（掷先攻/回合步进/操作按钮）仍由现有 ``/api/rooms/<id>/combat``
    与战斗轮面板承担；本端点只给目录级快照，前端据此渲染先攻列表。
    """
    combat = state.get("combat") if isinstance(state.get("combat"), dict) else None
    if not combat or not combat.get("active"):
        return {"active": False, "round": None, "turn": None, "order": [], "current": None}
    order = [str(name or "").strip() for name in combat.get("order") or [] if str(name or "").strip()]
    try:
        turn = int(combat.get("turn"))
    except (TypeError, ValueError):
        turn = 0
    current = order[turn] if 0 <= turn < len(order) else None
    return {
        "active": True,
        "round": combat.get("round") or 1,
        "turn": turn if 0 <= turn < len(order) else None,
        "order": order,
        "current": current,
    }


def _card_text(card: Any, *keys: str, limit: int = 120) -> str:
    if not isinstance(card, dict):
        return ""
    for key in keys:
        value = card.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:limit]
    return ""


def _player_roster(info: dict[str, Any]) -> list[dict[str, Any]]:
    """玩家角色目录：成员卡是唯一数据源；未建卡的成员如实标记。"""
    roster: list[dict[str, Any]] = []
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        name = _card_text(card, "name") or _card_text(member, "username")
        if not name:
            continue
        roster.append(
            {
                "user_id": str(member.get("user_id") or ""),
                "username": _card_text(member, "username"),
                "name": name,
                "avatar": _card_text(card, "avatar", limit=400),
                "level": _card_text(card, "level", limit=4),
                "class": _card_text(card, "class", "character_class"),
                "race": _card_text(card, "race"),
                "online": member.get("is_online") is True,
                "has_card": bool(_card_text(card, "name")),
            }
        )
        if len(roster) >= MAX_PLAYERS:
            break
    return roster


def _npc_roster(info: dict[str, Any]) -> dict[str, Any]:
    """NPC/怪物分组（仅 DM 下发）：来自房间绑定剧本的 NPC 模块。

    世界包导入（fvtt-world-import actors/run）的产物就是 ``module_type=npc``
    的剧本模块；没有绑定剧本或剧本为空时如实给空态，不造假数据。
    """
    scenario_id = str(info.get("scenario_id") or "").strip()
    if not scenario_id:
        return {"items": [], "hint": "房间未绑定剧本；导入世界包或绑定剧本后，NPC 与怪物会列在这里。"}
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR") or "")
    scenario: dict[str, Any] | None = None
    try:
        from trpg_server.scenario_store import load_scenario_by_id

        _descriptor, scenario = load_scenario_by_id(
            scenarios_dir,
            scenario_id,
            scenario_version=info.get("scenario_version"),
        )
    except Exception:  # noqa: BLE001 - 剧本读取失败按空目录降级，不阻断面板
        logger.debug("dnd5e-vtt: 读取剧本 %s 失败", scenario_id, exc_info=True)
        scenario = None
    if not isinstance(scenario, dict):
        return {"items": [], "hint": "剧本不可读，NPC 目录暂为空。"}
    items: list[dict[str, Any]] = []
    for module in scenario.get("modules") or []:
        if not isinstance(module, dict):
            continue
        module_type = str(module.get("module_type") or module.get("type") or "").strip().lower()
        if module_type != "npc":
            continue
        title = _card_text(module, "title", "name")
        if not title:
            continue
        items.append(
            {
                "id": str(module.get("id") or ""),
                "title": title,
                "summary": _card_text(module, "summary", limit=200),
            }
        )
        if len(items) >= MAX_NPCS:
            break
    return {
        "items": items,
        "hint": "" if items else "剧本暂无 NPC 模块；导入 FVTT 世界包后，怪物与 NPC 会列在这里。",
    }


def _journal_entries(state: dict[str, Any], *, manager: bool) -> list[dict[str, Any]]:
    """日志事件流：房间事件账本的尾部；玩家侧按公开类型过滤（过滤版日志）。"""
    entries: list[dict[str, Any]] = []
    for item in state.get("event_log") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "event")
        if not manager and kind not in PLAYER_SAFE_EVENT_KINDS:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        entries.append(
            {
                "id": str(item.get("id") or ""),
                "kind": kind,
                "content": content[:400],
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return entries[-MAX_JOURNAL_ENTRIES:]


def _panels_payload(room_dir: Path | None, info: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    state = load_room_state(Path(room_dir)) if room_dir else {}
    room_id = str(info.get("id") or "")
    panels: list[dict[str, Any]] = [
        dict(PANEL_CHAT),
        {
            "id": "combat",
            "title": "战斗",
            "state": _combat_view(state),
            # 接线点：完整战斗状态与写操作沿用核心接口，前端按需拉取。
            "source": f"/api/rooms/{room_id}/combat",
        },
    ]
    if manager:
        # 场景面板只对 DM 下发；玩家响应里整个 tab 不出现（服务端裁剪）。
        panels.append(
            {
                "id": "scenes",
                "title": "场景",
                "items": [],
                # 接线点：场景列表来自 veil-map 场景存储（M3 接入），本刀占位。
                "hint": "场景列表将在地图插件数据接入后列出，用于切换场景与把玩家拉入场景。",
            }
        )
    characters: dict[str, Any] = {
        "id": "characters",
        "title": "角色",
        "players": _player_roster(info),
    }
    if manager:
        # NPC/怪物分组仅 DM 视图；玩家响应里没有 npc 键。
        characters["npc"] = _npc_roster(info)
    panels.append(characters)
    panels.append(dict(PANEL_ITEMS))
    panels.append(
        {
            "id": "journal",
            "title": "日志",
            "entries": _journal_entries(state, manager=manager),
        }
    )
    return {
        "room_id": room_id,
        "ruleset_id": "dnd5e",
        "observer": "dm" if manager else "player",
        "panels": panels,
    }


@bp.route("/panels", methods=["GET"])
def panels():
    denied = _login_required()
    if denied:
        return denied
    room_id = str(request.args.get("room") or "").strip()
    if not room_id:
        return error_response("缺少 room 参数。", 400, "dnd5e_vtt.room_required")
    room_dir, info = find_room(room_id)
    if not info:
        return error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return error_response("你不在这个房间里。", 403, "room.access_denied")
    if _ruleset_id(info) not in SUPPORTED_RULESETS:
        return error_response("当前房间的规则集未启用战桌。", 400, "dnd5e_vtt.ruleset_disabled")
    manager = _is_manager(info)
    return success_response(_panels_payload(room_dir, info, manager=manager), "D&D 5e 战桌面板")
