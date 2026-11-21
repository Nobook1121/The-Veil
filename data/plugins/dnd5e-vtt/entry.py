"""D&D 5e 战桌插件（dnd5e-vtt）服务端：面板数据、测量模板与棋子操作卡。

M2 骨架 + M3 第一段（地图交互）：

- ``GET /api/plugins/dnd5e-vtt/panels?room=<room_id>``：按观察者（DM/玩家）
  裁剪的面板清单。**场景面板与角色面板的 NPC/怪物分组只对 DM 下发**，
  玩家的响应里整块不出现（不是前端隐藏）；前端只消费、不做本地权限判断。
- 测量模板（圆/锥/线/方）：房间级登记 + 广播。
  ``GET  /api/plugins/dnd5e-vtt/rooms/<id>/templates`` 全员可读（玩家端只读
  展示）；``POST`` 仅 DM（玩家 403），``request_id`` 幂等 + ``revision``
  乐观并发，接口风格学 veil-map（四态回执）。存
  ``<room>/plugins/dnd5e-vtt/templates.json``；写入成功后向房间广播
  ``dnd5e_vtt_templates``，所有客户端重绘；清空（clear）同样走该端点且仅 DM。
- 棋子操作卡数据：``GET /api/plugins/dnd5e-vtt/rooms/<id>/token-actions``。
  DM 拉任意棋子的动作清单（攻击/法术/特性/能力/物品，来自房间战斗状态、
  剧本 NPC 模块 statblock 与成员角色卡）；**玩家只允许查自己绑定的棋子且
  响应不含 actions 键**（服务端不下发 → 前端 DOM 不渲染，拍板决策）。
- 战桌 UI 由 ``ui/dnd5e-vtt.js`` 通过 E0 的 ``room.rail.right`` 槽挂载，
  并在同一挂载里自绘画布左上工具组（选择/测距尺/模板四型/清空）与棋子
  操作卡浮层；布局沿用 dnd5e 规则集已声明的 ``dnd5e-canvas``。
- veil-map（地图）是**可选**依赖：不 import 它的 Python 模块、不读它的
  存储文件。空间几何全部走它的公开 HTTP 端点（``/spatial/range`` 取格、
  ``/spatial/line`` 测距判墙），由前端直接调用；veil-map 缺席或调用失败时
  前端按本地几何近似并明说（锥 53° 与方形没有对应原语，本来就是本地近似）。

数据全部取自现有核心存储：战斗快照与事件流来自房间状态（``state.json``），
玩家角色来自成员卡，NPC 目录与动作清单来自剧本 NPC 模块（世界包导入的
产物，``statblock`` 含结构化 attacks / spellcasting / senses）。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_clock import room_lock
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

# ------------------------------------------------------------ 测量模板常量
TEMPLATES_VERSION = 1
TEMPLATE_KINDS = ("circle", "cone", "line", "rect")
TEMPLATE_ACTIONS = ("place", "move", "delete", "clear")
MAX_TEMPLATES = 60
MIN_TEMPLATE_CELLS = 1
MAX_TEMPLATE_CELLS = 100  # 单边最大格数（500 尺）
# FVTT 锥形模板默认张角约 53.13°（对角网格）；veil-map 的锥原语固定 90°，
# 几何不匹配，受影响格由前端本地近似并明说（见 ui/dnd5e-vtt.js 注释）。
DEFAULT_CONE_ANGLE = 53.13
MIN_CONE_ANGLE = 5.0
MAX_CONE_ANGLE = 360.0
TEMPLATE_COLORS = {
    "circle": "#e06666",
    "cone": "#f6b26b",
    "line": "#6fa8dc",
    "rect": "#93c47d",
}
MAX_TEMPLATE_LABEL = 60
TEMPLATE_RECENT_LIMIT = 64
_TEMPLATE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TEMPLATE_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_CARD_TEXT = 120

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


# ============================================================
# 测量模板（圆/锥/线/方）：房间级登记、DM 写入门控、request_id 幂等
# 与 revision 乐观并发；接口风格与回执结构学 veil-map 的写协议。
# ============================================================


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _templates_path(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "dnd5e-vtt" / "templates.json"


def _default_templates() -> dict[str, Any]:
    return {
        "version": TEMPLATES_VERSION,
        "revision": 0,
        "templates": [],
        "recent_requests": [],
        "updated_at": "",
    }


def _load_templates(room_dir) -> dict[str, Any]:
    store = read_json(_templates_path(room_dir), default=None)
    if not isinstance(store, dict):
        return _default_templates()
    store.setdefault("version", TEMPLATES_VERSION)
    store.setdefault("revision", 0)
    if not isinstance(store.get("templates"), list):
        store["templates"] = []
    store.setdefault("recent_requests", [])
    return store


def _normalize_direction(raw: Any) -> tuple[float, float] | None:
    """方向向量归一化（数据校验，不是空间计算；取格判定仍走 veil-map）。"""
    if isinstance(raw, dict):
        dx, dy = raw.get("dx"), raw.get("dy")
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        dx, dy = raw[0], raw[1]
    else:
        return None
    try:
        fx, fy = float(dx), float(dy)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(fx) and math.isfinite(fy)) or (fx == 0 and fy == 0):
        return None
    length = math.hypot(fx, fy)
    return round(fx / length, 6), round(fy / length, 6)


def _normalize_template(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """模板字段规范化；返回 ``(template, error)``，出错时 template 为 ``None``。"""
    if not isinstance(raw, dict):
        return None, "template 必须是 JSON 对象。"
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in TEMPLATE_KINDS:
        return None, "模板类型只能是 circle / cone / line / rect。"
    try:
        cell_x = int(round(float(raw.get("cell_x"))))
        cell_y = int(round(float(raw.get("cell_y"))))
    except (TypeError, ValueError):
        return None, "模板原点需要整数 cell_x / cell_y。"
    if abs(cell_x) > 1_000_000 or abs(cell_y) > 1_000_000:
        return None, "模板原点超出可表示范围。"
    template: dict[str, Any] = {"kind": kind, "cell_x": cell_x, "cell_y": cell_y}
    if kind == "rect":
        try:
            width = int(round(float(raw.get("w_cells"))))
            height = int(round(float(raw.get("h_cells"))))
        except (TypeError, ValueError):
            return None, "方形模板需要整数 w_cells / h_cells。"
        if not (MIN_TEMPLATE_CELLS <= width <= MAX_TEMPLATE_CELLS):
            return None, "方形宽度必须在 1–100 格之间。"
        if not (MIN_TEMPLATE_CELLS <= height <= MAX_TEMPLATE_CELLS):
            return None, "方形高度必须在 1–100 格之间。"
        template["w_cells"] = width
        template["h_cells"] = height
    else:
        try:
            size = int(round(float(raw.get("size_cells"))))
        except (TypeError, ValueError):
            return None, "模板尺寸需要整数 size_cells。"
        if not (MIN_TEMPLATE_CELLS <= size <= MAX_TEMPLATE_CELLS):
            return None, "模板尺寸必须在 1–100 格之间（每格 5 尺）。"
        template["size_cells"] = size
    if kind in {"cone", "line"}:
        unit = _normalize_direction(raw.get("direction"))
        if unit is None:
            return None, "锥形/线形模板需要非零 direction {dx, dy}。"
        template["direction"] = {"dx": unit[0], "dy": unit[1]}
    if kind == "cone":
        try:
            angle = float(raw.get("angle") or DEFAULT_CONE_ANGLE)
        except (TypeError, ValueError):
            angle = DEFAULT_CONE_ANGLE
        if not math.isfinite(angle):
            angle = DEFAULT_CONE_ANGLE
        template["angle"] = round(min(MAX_CONE_ANGLE, max(MIN_CONE_ANGLE, angle)), 2)
    color = str(raw.get("color") or "").strip()
    if not _TEMPLATE_HEX_COLOR.match(color):
        color = TEMPLATE_COLORS[kind]
    template["color"] = color
    label = str(raw.get("label") or "").strip()
    if label:
        template["label"] = label[:MAX_TEMPLATE_LABEL]
    return template, ""


def _templates_payload(store: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    return {
        "templates": store.get("templates") or [],
        "revision": int(store.get("revision") or 0),
        "can_manage": manager,
        "observer": "dm" if manager else "player",
    }


def _template_receipt(
    status: str,
    message: str,
    *,
    code: str = "",
    request_id: str = "",
    revision: int = 0,
    duplicate: bool = False,
    data: dict[str, Any] | None = None,
    http_status: int = 200,
):
    """四态回执（success / conflict / rejected / invalid），学 veil-map。"""
    payload: dict[str, Any] = {
        "success": status == "success",
        "status": status,
        "code": code,
        "message": message,
        "request_id": str(request_id or ""),
        "revision": int(revision),
        "duplicate": bool(duplicate),
    }
    if data is not None:
        payload["data"] = data
    return jsonify(payload), http_status


def _broadcast_templates(room_id: str, store: dict[str, Any]) -> None:
    """模板广播到房间：模板是 DM 放置的公共信息，全员可见、无观察者分叉。"""
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit(
            "dnd5e_vtt_templates",
            {
                "room_id": str(room_id),
                "templates": store.get("templates") or [],
                "revision": int(store.get("revision") or 0),
            },
            room=str(room_id),
        )
    except Exception:  # noqa: BLE001 - 广播失败不能影响写入结果
        logger.warning("dnd5e_vtt_templates_broadcast_failed room=%s", room_id, exc_info=True)


def _templates_room_or_error(room_id: str):
    """模板与棋子卡接口统一入口：登录、房间、成员与规则集门闸（与 /panels 同口径）。"""
    denied = _login_required()
    if denied:
        return None, None, denied
    room_dir, info = find_room(room_id)
    if not info:
        return room_dir, None, error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return room_dir, info, error_response("你不在这个房间里。", 403, "room.access_denied")
    if _ruleset_id(info) not in SUPPORTED_RULESETS:
        return room_dir, info, error_response(
            "当前房间的规则集未启用战桌。", 400, "dnd5e_vtt.ruleset_disabled"
        )
    return room_dir, info, None


@bp.route("/rooms/<room_id>/templates", methods=["GET"])
def get_templates(room_id: str):
    """模板读取：全员可读（玩家端只读展示，编辑工具不在玩家视图渲染）。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    with room_lock(room_dir):
        store = _load_templates(room_dir)
    manager = _is_manager(info)
    return success_response(_templates_payload(store, manager=manager), "测量模板清单")


@bp.route("/rooms/<room_id>/templates", methods=["POST"])
def write_templates(room_id: str):
    """模板写入口：place / move / delete / clear，仅 DM；request_id 幂等。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    manager = _is_manager(info)
    if not manager:
        return _template_receipt(
            "rejected",
            "测量模板只能由 DM（房主/房管）放置与修改。",
            code="dnd5e_vtt.templates_dm_only",
            http_status=403,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _template_receipt(
            "invalid", "请求体必须是 JSON 对象。", code="dnd5e_vtt.invalid_payload"
        )
    request_id = str(payload.get("request_id") or "").strip()
    if not _TEMPLATE_REQUEST_ID.match(request_id):
        return _template_receipt(
            "invalid",
            "写请求缺少合法的 request_id（1–64 位字母、数字或 . _ : -）。",
            code="dnd5e_vtt.invalid_request_id",
            http_status=400,
        )
    raw_revision = payload.get("revision")
    if isinstance(raw_revision, bool):
        raw_revision = None
    try:
        expected = int(str(raw_revision).strip())
    except (TypeError, ValueError):
        expected = -1
    if expected < 0:
        return _template_receipt(
            "invalid",
            "写请求缺少非负整数 revision。",
            code="dnd5e_vtt.invalid_revision",
            request_id=request_id,
            http_status=400,
        )
    action = str(payload.get("action") or "").strip().lower()
    if action not in TEMPLATE_ACTIONS:
        return _template_receipt(
            "invalid",
            "action 只能是 place / move / delete / clear。",
            code="dnd5e_vtt.invalid_action",
            request_id=request_id,
            http_status=400,
        )
    with room_lock(room_dir):
        store = _load_templates(room_dir)
        revision = int(store.get("revision") or 0)
        if expected != revision:
            return _template_receipt(
                "conflict",
                f"模板已被其他端更新（当前版本 {revision}），请刷新后再试。",
                code="dnd5e_vtt.revision_conflict",
                request_id=request_id,
                revision=revision,
                http_status=409,
                data=_templates_payload(store, manager=manager),
            )
        for record in store.get("recent_requests") or []:
            if str(record.get("request_id")) == request_id:
                # 幂等：同一 request_id 直接回成功，不再产生副作用。
                return _template_receipt(
                    "success",
                    "该操作已生效，重复提交已忽略。",
                    code="dnd5e_vtt.duplicate_request",
                    request_id=request_id,
                    revision=revision,
                    duplicate=True,
                    data=_templates_payload(store, manager=manager),
                )
        templates: list[dict[str, Any]] = [
            item for item in store.get("templates") or [] if isinstance(item, dict)
        ]
        if action == "place":
            template, error = _normalize_template(payload.get("template"))
            if error:
                return _template_receipt(
                    "invalid",
                    error,
                    code="dnd5e_vtt.invalid_template",
                    request_id=request_id,
                    revision=revision,
                    http_status=400,
                )
            if len(templates) >= MAX_TEMPLATES:
                return _template_receipt(
                    "rejected",
                    f"模板数量已达上限（{MAX_TEMPLATES}），请先清理。",
                    code="dnd5e_vtt.too_many_templates",
                    request_id=request_id,
                    revision=revision,
                    http_status=409,
                )
            template_id = str(payload.get("id") or "").strip()[:64]
            if template_id and any(str(item.get("id")) == template_id for item in templates):
                return _template_receipt(
                    "invalid",
                    "模板 id 已存在。",
                    code="dnd5e_vtt.duplicate_template_id",
                    request_id=request_id,
                    revision=revision,
                    http_status=409,
                )
            template["id"] = template_id or f"tpl-{uuid4().hex[:12]}"
            template["placed_by"] = str(session.get("user_id") or "")
            template["created_at"] = _utc_now()
            templates.append(template)
        elif action == "move":
            template_id = str(payload.get("id") or "").strip()
            target = next(
                (item for item in templates if str(item.get("id")) == template_id), None
            )
            if target is None:
                return _template_receipt(
                    "invalid",
                    "模板不存在。",
                    code="dnd5e_vtt.template_not_found",
                    request_id=request_id,
                    revision=revision,
                    http_status=404,
                )
            incoming, error = _normalize_template(payload.get("template") or target)
            if error:
                return _template_receipt(
                    "invalid",
                    error,
                    code="dnd5e_vtt.invalid_template",
                    request_id=request_id,
                    revision=revision,
                    http_status=400,
                )
            moved = dict(target)
            moved.update(incoming)
            moved["id"] = str(target.get("id"))
            moved["updated_at"] = _utc_now()
            templates[templates.index(target)] = moved
        else:  # delete / clear
            if action == "clear":
                templates = []
            else:
                template_id = str(payload.get("id") or "").strip()
                if not any(str(item.get("id")) == template_id for item in templates):
                    return _template_receipt(
                        "invalid",
                        "模板不存在。",
                        code="dnd5e_vtt.template_not_found",
                        request_id=request_id,
                        revision=revision,
                        http_status=404,
                    )
                templates = [
                    item for item in templates if str(item.get("id")) != template_id
                ]
        store["templates"] = templates
        store["revision"] = revision + 1
        store["updated_at"] = _utc_now()
        records = [
            item for item in store.get("recent_requests") or [] if isinstance(item, dict)
        ]
        records.append(
            {
                "request_id": request_id,
                "action": action,
                "revision": store["revision"],
                "at": store["updated_at"],
            }
        )
        store["recent_requests"] = records[-TEMPLATE_RECENT_LIMIT:]
        write_json_atomic(_templates_path(room_dir), store)
    _broadcast_templates(room_id, store)
    return _template_receipt(
        "success",
        "模板已更新。",
        code=f"dnd5e_vtt.templates_{action}",
        request_id=request_id,
        revision=store["revision"],
        data=_templates_payload(store, manager=manager),
    )


# ============================================================
# 棋子操作卡：DM 拉任意棋子的动作清单；玩家只读自己的卡且无 actions 键。
# ============================================================


def _trim_card_text(value: Any, limit: int = MAX_CARD_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _combatant_candidates(params: dict[str, Any]) -> list[str]:
    """棋子 → 战斗者/NPC 模块的候选名（保序去重；``npc:`` 前缀剥掉后并保留）。"""
    raw = [params.get("combatant_name"), params.get("combatant"), params.get("name")]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        text = _trim_card_text(item, 96)
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
        if text.lower().startswith("npc:"):
            stripped = text[4:].strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                ordered.append(stripped)
    return ordered


def _find_combatant(state: dict[str, Any], candidates: list[str]) -> dict[str, Any] | None:
    combat = state.get("combat") if isinstance(state.get("combat"), dict) else None
    if not combat or not combat.get("active"):
        return None
    combatants = combat.get("combatants")
    if not isinstance(combatants, dict):
        return None
    for candidate in candidates:
        row = combatants.get(candidate)
        if isinstance(row, dict):
            return row
        lowered = str(candidate).lower()
        for name, item in combatants.items():
            if str(name).lower() == lowered and isinstance(item, dict):
                return item
    return None


def _find_npc_module(info: dict[str, Any], candidates: list[str]) -> dict[str, Any] | None:
    """按候选名匹配剧本 NPC 模块（世界包导入产物，含结构化 statblock）。"""
    scenario_id = str(info.get("scenario_id") or "").strip()
    if not scenario_id or not candidates:
        return None
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR") or "")
    try:
        from trpg_server.scenario_store import load_scenario_by_id

        _descriptor, scenario = load_scenario_by_id(
            scenarios_dir,
            scenario_id,
            scenario_version=info.get("scenario_version"),
        )
    except Exception:  # noqa: BLE001 - 剧本读取失败按无模块降级
        logger.debug("dnd5e-vtt: token-actions 读取剧本 %s 失败", scenario_id, exc_info=True)
        return None
    if not isinstance(scenario, dict):
        return None
    lowered = [str(item).lower() for item in candidates]
    for module in scenario.get("modules") or []:
        if not isinstance(module, dict):
            continue
        if str(module.get("module_type") or module.get("type") or "").strip().lower() != "npc":
            continue
        title = str(module.get("title") or module.get("name") or "").strip().lower()
        if title and title in lowered:
            return module
    return None


def _find_member(info: dict[str, Any], owner_user_id: Any) -> dict[str, Any] | None:
    owner = str(owner_user_id or "").strip()
    if not owner:
        return None
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if str(member.get("user_id")) == owner:
            return member
    return None


def _attack_rows(source: dict[str, Any]) -> list[dict[str, Any]]:
    """攻击行：命中 + 伤害文本（本刀只展示，发起结算归后续刀接 combat 工作流）。"""
    rows: list[dict[str, Any]] = []
    for row in source.get("attacks") or []:
        if not isinstance(row, dict):
            continue
        name = _trim_card_text(row.get("name"), 60)
        if not name:
            continue
        try:
            bonus = int(round(float(row.get("attack_bonus") or 0)))
        except (TypeError, ValueError):
            bonus = 0
        try:
            damage_bonus = int(round(float(row.get("damage_bonus") or 0)))
        except (TypeError, ValueError):
            damage_bonus = 0
        dice = _trim_card_text(row.get("damage_dice"), 32)
        damage_text = dice
        if dice and damage_bonus:
            damage_text = f"{dice}{damage_bonus:+d}"
        damage_type = _trim_card_text(row.get("damage_type"), 24)
        if damage_text and damage_type:
            damage_text += f" {damage_type}"
        detail = ""
        ranged = row.get("range")
        reach = row.get("reach_ft")
        if isinstance(ranged, dict):
            detail = f"射程 {ranged.get('normal_ft') or 0}/{ranged.get('long_ft') or 0} 尺"
        elif reach:
            detail = f"触及 {reach} 尺"
        versatile = _trim_card_text(row.get("versatile_dice"), 32)
        if versatile:
            detail = (detail + " · " if detail else "") + f"双手 {versatile}"
        rows.append(
            {
                "name": name,
                "kind": _trim_card_text(row.get("kind"), 12) or "melee",
                "hit": f"{bonus:+d}" if bonus else "",
                "damage": damage_text,
                "detail": detail,
            }
        )
    return rows


def _spell_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    names: list[str] = []
    for key in ("spells_known", "spells_prepared"):
        for item in source.get(key) or []:
            text = _trim_card_text(item, 80)
            if text and text not in names:
                names.append(text)
    spellcasting = source.get("spellcasting")
    detail = ""
    if isinstance(spellcasting, dict):
        parts = []
        save_dc = spellcasting.get("spellSaveDC") or spellcasting.get("save_dc")
        if save_dc:
            parts.append(f"豁免 DC {save_dc}")
        attack_bonus = spellcasting.get("spellAttackBonus") or spellcasting.get("attack_bonus")
        if attack_bonus:
            parts.append(f"法术命中 {attack_bonus}")
        detail = " · ".join(parts)
    return [{"name": name, "detail": detail} for name in names]


def _ability_rows(source: dict[str, Any], *, npc: bool) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if npc:
        senses = source.get("senses") if isinstance(source.get("senses"), dict) else {}
        for key, value in senses.items():
            name = _trim_card_text(key, 40)
            if name:
                rows.append({"name": name, "detail": _trim_card_text(value, 60)})
        legendary = source.get("legendary_actions_max")
        if legendary:
            rows.append({"name": "传奇动作", "detail": f"每轮最多 {legendary} 次"})
        resistances = source.get("legendary_resistances_max")
        if resistances:
            rows.append({"name": "传奇抗性", "detail": f"每日 {resistances} 次"})
        return rows
    proficiencies = (
        source.get("proficiencies") if isinstance(source.get("proficiencies"), dict) else {}
    )
    labels = {
        "weapons": "武器熟练",
        "armor": "护甲熟练",
        "tools": "工具熟练",
        "languages": "语言",
    }
    for key, label in labels.items():
        items = [
            str(item).strip() for item in proficiencies.get(key) or [] if str(item or "").strip()
        ]
        if items:
            rows.append({"name": label, "detail": "、".join(items)[:MAX_CARD_TEXT]})
    return rows


def _item_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in source.get("equipment") or []:
        if isinstance(row, dict):
            name = _trim_card_text(row.get("name"), 60)
            if not name:
                continue
            quantity = row.get("quantity")
            detail = f"数量 {quantity}" if quantity not in (None, "") else ""
            rows.append({"name": name, "detail": _trim_card_text(detail, 24)})
        else:
            name = _trim_card_text(row, 60)
            if name:
                rows.append({"name": name, "detail": ""})
    return rows


def _feature_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in source.get("features") or []:
        name = _trim_card_text(item, 80)
        if name:
            rows.append({"name": name, "detail": ""})
    return rows


def _card_hp(source: dict[str, Any]) -> tuple[int, int]:
    """HP 双源：资源字段 ``hp{current,max}`` 优先，退回扁平键。"""
    hp_field = source.get("hp")
    if isinstance(hp_field, dict):
        try:
            current = int(round(float(hp_field.get("current") or hp_field.get("current_hp") or 0)))
        except (TypeError, ValueError):
            current = 0
        try:
            maximum = int(
                round(
                    float(
                        hp_field.get("max")
                        or hp_field.get("max_hp")
                        or source.get("maxHp")
                        or 0
                    )
                )
            )
        except (TypeError, ValueError):
            maximum = 0
        if maximum > 0:
            return max(0, min(maximum, current)), maximum
    try:
        current = int(round(float(source.get("hp") or 0)))
    except (TypeError, ValueError):
        current = 0
    try:
        maximum = int(round(float(source.get("max_hp") or source.get("maxHp") or 0)))
    except (TypeError, ValueError):
        maximum = 0
    return max(0, current), max(0, maximum)


def _token_actions_payload(
    room_dir,
    info: dict[str, Any],
    *,
    manager: bool,
    params: dict[str, Any],
) -> dict[str, Any]:
    """棋子操作卡数据装配。

    匹配顺序：房间战斗状态（含已推导的攻击加值/伤害）→ 成员角色卡 →
    剧本 NPC 模块 statblock。DM 得到完整动作清单；**玩家响应不含
    actions 键**（服务端裁剪，DOM 不渲染，拍板决策）。
    """
    state = load_room_state(Path(room_dir)) if room_dir else {}
    candidates = _combatant_candidates(params)
    combatant = _find_combatant(state, candidates)
    member = _find_member(info, params.get("owner"))
    module = _find_npc_module(info, candidates)
    member_card = member.get("character_card") if isinstance(member, dict) else None
    member_card = member_card if isinstance(member_card, dict) else {}
    statblock = module.get("statblock") if isinstance(module, dict) else None
    statblock = statblock if isinstance(statblock, dict) else {}
    kind = "token"
    card_source: dict[str, Any] = {}
    if combatant:
        card_source = combatant
        kind = "npc" if str(params.get("combatant") or "").lower().startswith("npc:") else "combatant"
    if member is not None:
        card_source = member_card or card_source
        kind = "player"
    elif module is not None and not combatant:
        # 战斗状态优先于剧本 statblock：前者有实时 HP，后者只有满血模板。
        card_source = statblock or card_source
        if kind == "token":
            kind = "npc"
    hp, max_hp = _card_hp(card_source)
    try:
        ac = int(round(float(card_source.get("armorClass") or card_source.get("ac") or 0)))
    except (TypeError, ValueError):
        ac = 0
    name = (
        _trim_card_text(params.get("name"), 60)
        or _trim_card_text(member_card.get("name"), 60)
        or _trim_card_text((module or {}).get("title"), 60)
        or _trim_card_text(card_source.get("name"), 60)
        or "棋子"
    )
    card: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "hp": hp,
        "max_hp": max_hp,
        "ac": ac,
    }
    if kind == "player":
        card["avatar"] = _trim_card_text(member_card.get("avatar"), 400)
        card["level"] = _trim_card_text(member_card.get("level"), 4)
        card["class"] = _trim_card_text(member_card.get("class"), 40)
        card["race"] = _trim_card_text(member_card.get("race"), 40)
    elif kind == "npc":
        card["avatar"] = ""
        card["cr"] = _trim_card_text(card_source.get("cr"), 12)
    data: dict[str, Any] = {
        "card": card,
        "matched": bool(combatant or member is not None or module is not None),
    }
    if not manager:
        return data
    # DM 动作清单：战斗状态优先，NPC statblock 次之，成员角色卡最后。
    actions_source = combatant or card_source or {}
    actions: dict[str, Any] = {
        "attacks": _attack_rows(actions_source),
        "spells": _spell_rows(actions_source),
        "features": _feature_rows(actions_source if kind == "player" else {}),
        "abilities": _ability_rows(actions_source, npc=kind != "player"),
        "items": _item_rows(actions_source),
    }
    if not (combatant or member is not None or module is not None):
        actions["hint"] = "未找到该棋子对应的战斗者、剧本 NPC 或角色卡；开战后动作清单会从战斗状态生成。"
    data["actions"] = actions
    return data


@bp.route("/rooms/<room_id>/token-actions", methods=["GET"])
def token_actions(room_id: str):
    """棋子操作卡数据：DM 全量；玩家仅自己棋子且不含操作按钮。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    params = {
        "name": request.args.get("name") or "",
        "owner": request.args.get("owner") or "",
        "combatant": request.args.get("combatant") or "",
        "combatant_name": request.args.get("combatant_name") or "",
    }
    if not any(str(value).strip() for value in params.values()):
        return error_response(
            "缺少棋子标识（name / owner / combatant / combatant_name 至少一项）。",
            400,
            "dnd5e_vtt.token_required",
        )
    manager = _is_manager(info)
    viewer_id = str(session.get("user_id") or "")
    if not manager and str(params["owner"]).strip() != viewer_id:
        return error_response(
            "玩家只能查看自己角色的棋子卡。",
            403,
            "dnd5e_vtt.token_actions_forbidden",
        )
    payload = _token_actions_payload(room_dir, info, manager=manager, params=params)
    return success_response(payload, "棋子操作卡数据")
