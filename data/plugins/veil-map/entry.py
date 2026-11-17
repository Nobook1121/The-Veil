"""地图插件（veil-map）的服务端接口。

核心只提供画布宿主与事件通道；棋子坐标、网格吸附、多格占位、地图基础
编辑与战斗回合联动都在这里落地。状态写在房间自己的插件命名空间：

    <ROOMS_DIR>/<room_id>/plugins/veil-map/map.json

战斗行动经济直接复用 ``trpg_server.combat.scheduler.spend_budget``，不另写
一套移动力判定；越权与预算不足都返回可读中文消息，由前端提示。
"""

from __future__ import annotations

import importlib.util
import logging
import math
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.combat.scheduler import spend_budget
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import resolve_rule_id

bp = Blueprint("veil_map", __name__, url_prefix="/api/plugins/veil-map")
logger = logging.getLogger(__name__)

MAP_VERSION = 2
DEFAULT_GRID = 64
MIN_GRID = 16
MAX_GRID = 256
MAX_TOKENS = 200
MAX_NAME = 40
MAX_REASON = 120
FEET_PER_CELL = 5
BOUNDS_DEFAULT = 50
MIN_BOUNDS = 5
MAX_BOUNDS = 200
DM_AGENT_IDS = {"kp"}
SUPPORTED_RULESETS = {"dnd5e"}
TOKEN_COLORS = ("#c8a35a", "#6fa8dc", "#93c47d", "#e06666", "#8e7cc3", "#76a5af", "#f6b26b", "#a64d79")
MAX_WALLS = 200
MAX_WALL_POINTS = 64
WALL_KINDS = {"wall", "door", "window", "terrain"}
RECENT_REQUEST_LIMIT = 64
_MAP_LOCK = threading.RLock()
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_OWNER_ROLES = {"owner", "admin"}

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_local_module(name: str):
    """加载同目录模块。

    插件运行时会把插件目录临时加入 ``sys.path``，但测试会直接用 importlib 加载
    entry.py；这里按文件路径加载并用带前缀的模块名，避免和核心包重名。
    """
    module_name = f"veil_map_{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 veil-map 模块：{name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


spatial = _load_local_module("spatial")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _map_path(room_dir):
    return room_dir / "plugins" / "veil-map" / "map.json"


def _default_map() -> dict[str, Any]:
    return {
        "version": MAP_VERSION,
        "revision": 0,
        "scene_id": "default",
        "background": "",
        "grid_size": DEFAULT_GRID,
        "grid_offset": {"x": 0, "y": 0},
        "bounds_cells": BOUNDS_DEFAULT,
        "tokens": [],
        "walls": [],
        "recent_requests": [],
        "updated_at": "",
    }


def _normalize_token(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    token_id = str(raw.get("id") or "").strip()
    if not token_id:
        return None
    name = str(raw.get("name") or "").strip()[:MAX_NAME]
    return {
        "id": token_id[:64],
        "name": name or "棋子",
        "x": _as_int(raw.get("x"), 0),
        "y": _as_int(raw.get("y"), 0),
        "w": _clamp(_as_int(raw.get("w"), 1), 1, 4),
        "h": _clamp(_as_int(raw.get("h"), 1), 1, 4),
        "color": str(raw.get("color") or TOKEN_COLORS[0]),
        "owner_user_id": str(raw.get("owner_user_id") or ""),
        "kind": str(raw.get("kind") or "npc"),
        "combatant": str(raw.get("combatant") or ""),
    }


def _coordinate(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > 1_000_000:
        return None
    return int(round(number))


def _normalize_wall(raw: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    points: list[dict[str, int]] = []
    for item in raw.get("points") or []:
        if isinstance(item, dict):
            x, y = _coordinate(item.get("x")), _coordinate(item.get("y"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = _coordinate(item[0]), _coordinate(item[1])
        else:
            continue
        if x is None or y is None:
            continue
        points.append({"x": x, "y": y})
        if len(points) >= MAX_WALL_POINTS:
            break
    if len(points) < 2:
        return None
    kind = str(raw.get("kind") or "wall").strip().lower()
    if kind not in WALL_KINDS:
        kind = "wall"
    state = str(raw.get("state") or "closed").strip().lower()
    if state not in {"closed", "open"}:
        state = "closed"
    wall_id = str(raw.get("id") or "").strip()[:64] or f"wall-{index + 1}"
    try:
        opacity = float(raw.get("opacity", 1.0))
    except (TypeError, ValueError):
        opacity = 1.0
    if not math.isfinite(opacity):
        opacity = 1.0
    try:
        height = float(raw.get("height", 1.0))
    except (TypeError, ValueError):
        height = 1.0
    if not math.isfinite(height):
        height = 1.0
    return {
        "id": wall_id,
        "kind": kind,
        "points": points,
        "blocks_sight": bool(raw.get("blocks_sight", kind != "window")),
        "blocks_movement": bool(raw.get("blocks_movement", kind != "window")),
        "blocks_light": bool(raw.get("blocks_light", kind != "window")),
        "opacity": max(0.0, min(1.0, opacity)),
        "height": max(0.0, min(100.0, height)),
        "state": state,
    }


def _normalize_walls(raw: Any) -> list[dict[str, Any]]:
    walls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        wall = _normalize_wall(item, index)
        if wall is None:
            continue
        if wall["id"] in seen:
            wall["id"] = f"{wall['id']}-{index + 1}"
        seen.add(wall["id"])
        walls.append(wall)
        if len(walls) >= MAX_WALLS:
            break
    return walls


def _normalize_recent_requests(raw: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("request_id") or "").strip()
        if not _REQUEST_ID.match(request_id):
            continue
        records.append(
            {
                "request_id": request_id,
                "action": str(item.get("action") or "")[:32],
                "revision": max(0, _as_int(item.get("revision"), 0)),
                "token_id": str(item.get("token_id") or "")[:64],
                "at": str(item.get("at") or "")[:32],
            }
        )
    return records[-RECENT_REQUEST_LIMIT:]


def _normalize_map(raw: Any) -> dict[str, Any]:
    state = _default_map()
    if isinstance(raw, dict):
        state.update(raw)
    state["version"] = MAP_VERSION
    state["revision"] = max(0, _as_int(state.get("revision"), 0))
    state["scene_id"] = str(state.get("scene_id") or "default")[:64]
    state["background"] = str(state.get("background") or "")[:2048]
    state["grid_size"] = _clamp(_as_int(state.get("grid_size"), DEFAULT_GRID), MIN_GRID, MAX_GRID)
    offset = state.get("grid_offset") if isinstance(state.get("grid_offset"), dict) else {}
    grid = int(state["grid_size"])
    state["grid_offset"] = {
        "x": _clamp(_as_int(offset.get("x"), 0), -grid, grid),
        "y": _clamp(_as_int(offset.get("y"), 0), -grid, grid),
    }
    state["bounds_cells"] = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    tokens: list[dict[str, Any]] = []
    for item in state.get("tokens") or []:
        token = _normalize_token(item)
        if token is not None:
            tokens.append(token)
    state["tokens"] = tokens[-MAX_TOKENS:]
    state["walls"] = _normalize_walls(state.get("walls"))
    state["recent_requests"] = _normalize_recent_requests(state.get("recent_requests"))
    state["updated_at"] = str(state.get("updated_at") or "")
    return state


def _load_map(room_dir) -> dict[str, Any]:
    return _normalize_map(read_json(_map_path(room_dir), default={}))


def _save_map(
    room_dir,
    state: dict[str, Any],
    request_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写入地图并推进 revision；成功的写请求会留下幂等记录。"""
    state["revision"] = max(0, _as_int(state.get("revision"), 0)) + 1
    normalized = _normalize_map(state)
    normalized["updated_at"] = _now()
    if isinstance(request_record, dict) and request_record.get("request_id"):
        records = list(normalized.get("recent_requests") or [])
        record = {
            "request_id": str(request_record.get("request_id"))[:64],
            "action": str(request_record.get("action") or "")[:32],
            "revision": normalized["revision"],
            "token_id": str(request_record.get("token_id") or "")[:64],
            "at": normalized["updated_at"],
        }
        records.append(record)
        normalized["recent_requests"] = records[-RECENT_REQUEST_LIMIT:]
    path = _map_path(room_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, normalized)
    return normalized


def _active_members(info: dict[str, Any]) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for member in (info or {}).get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        members.append(member)
    return members


def _member_name(member: dict[str, Any]) -> str:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    name = str(card.get("name") or member.get("character_name") or member.get("username") or "").strip()
    return name[:MAX_NAME] or "玩家"


def _reconcile_tokens(state: dict[str, Any], info: dict[str, Any]) -> bool:
    """按活跃成员补齐 PC 棋子；已有棋子保留坐标与尺寸。"""
    grid = int(state.get("grid_size") or DEFAULT_GRID)
    tokens = state.setdefault("tokens", [])
    by_id = {str(token.get("id")): token for token in tokens}
    by_owner = {
        str(token.get("owner_user_id")): token
        for token in tokens
        if str(token.get("kind") or "") == "pc" and token.get("owner_user_id")
    }
    changed = False
    for index, member in enumerate(_active_members(info)):
        user_id = str(member.get("user_id") or "").strip()
        if not user_id:
            continue
        name = _member_name(member)
        token = by_id.get(f"member-{user_id}") or by_owner.get(user_id)
        if token is not None:
            if token.get("name") != name or token.get("owner_user_id") != user_id:
                token["name"] = name
                token["owner_user_id"] = user_id
                token["kind"] = "pc"
                token["combatant"] = name
                changed = True
            continue
        token = {
            "id": f"member-{user_id}",
            "name": name,
            "x": (index % 6) * grid,
            "y": (index // 6) * grid,
            "w": 1,
            "h": 1,
            "color": TOKEN_COLORS[index % len(TOKEN_COLORS)],
            "owner_user_id": user_id,
            "kind": "pc",
            "combatant": name,
        }
        tokens.append(token)
        by_id[token["id"]] = token
        by_owner[user_id] = token
        changed = True
    if len(tokens) > MAX_TOKENS:
        state["tokens"] = tokens[-MAX_TOKENS:]
        changed = True
    return changed


def _room_context(info: dict[str, Any]) -> tuple[str, bool]:
    user_id = str(session.get("user_id") or "")
    member = None
    for item in _active_members(info):
        if str(item.get("user_id")) == user_id:
            member = item
            break
    role = str((member or {}).get("room_role") or "").strip().lower()
    if member is not None and str(member.get("user_id")) == str(info.get("creator_id")):
        role = "owner"
    return role, bool(is_elevated() or role in _OWNER_ROLES)


def _room_or_error(room_id: str):
    room_dir, info = find_room(room_id)
    if not info:
        return None, None, error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return None, None, error_response("你不在这个房间里。", 403, "room.access_denied")
    return room_dir, info, None


def _ruleset_enabled(info: dict[str, Any]) -> bool:
    return _agent_ruleset_id(info) in SUPPORTED_RULESETS


def _map_room_or_error(room_id: str):
    """地图接口统一入口：房间访问 + 规则集门闸。

    Coc7 等未声明 canvas workspace 的规则集不会挂载地图插件；服务端再挡一层，
    避免旧链接或脚本在 coc7 房间创建空舞台与地图数据。
    """
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return room_dir, info, denied
    if not _ruleset_enabled(info):
        return room_dir, info, error_response(
            "当前房间的规则集未启用地图。", 400, "map.ruleset_disabled"
        )
    return room_dir, info, None


def _receipt(
    status: str,
    message: str,
    *,
    request_id: str = "",
    revision: int = 0,
    code: str = "",
    http_status: int = 200,
    data: dict[str, Any] | None = None,
    duplicate: bool = False,
):
    """统一写操作回执：success / conflict / rejected / invalid。"""
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


def _invalid_request(
    message: str,
    code: str,
    *,
    request_id: str = "",
    revision: int = 0,
    data: dict[str, Any] | None = None,
    http_status: int = 400,
):
    return _receipt(
        "invalid",
        message,
        request_id=request_id,
        revision=revision,
        code=code,
        http_status=http_status,
        data=data,
    )


def _rejected_request(
    message: str,
    code: str,
    *,
    request_id: str = "",
    revision: int = 0,
    http_status: int = 403,
    data: dict[str, Any] | None = None,
):
    return _receipt(
        "rejected",
        message,
        request_id=request_id,
        revision=revision,
        code=code,
        http_status=http_status,
        data=data,
    )


def _conflict_request(
    state: dict[str, Any],
    manager: bool,
    request_id: str,
    *,
    message: str = "",
    data: dict[str, Any] | None = None,
):
    revision = int(state.get("revision") or 0)
    body = {"map": state, "can_manage": manager}
    if isinstance(data, dict):
        body.update(data)
    return _receipt(
        "conflict",
        message or f"地图已被其他成员更新（当前版本 {revision}），请刷新后再试。",
        request_id=request_id,
        revision=revision,
        code="map.revision_conflict",
        http_status=409,
        data=body,
    )


def _read_envelope(
    payload: Any, state: dict[str, Any]
) -> tuple[str, int, Any]:
    """校验 request_id / revision；返回 ``(request_id, revision, error)``。"""
    if not isinstance(payload, dict):
        return "", 0, _invalid_request(
            "请求体必须是 JSON 对象。",
            "map.invalid_payload",
            revision=state.get("revision", 0),
        )
    request_id = str(payload.get("request_id") or "").strip()
    if not _REQUEST_ID.match(request_id):
        return "", 0, _invalid_request(
            "写请求缺少合法的 request_id（1–64 位字母、数字或 . _ : -）。",
            "map.invalid_request_id",
            revision=state.get("revision", 0),
        )
    raw_revision = payload.get("revision")
    if isinstance(raw_revision, bool):
        raw_revision = None
    try:
        expected = int(str(raw_revision).strip())
    except (TypeError, ValueError):
        expected = None
    if expected is None or expected < 0:
        return request_id, 0, _invalid_request(
            "写请求缺少非负整数 revision。",
            "map.invalid_revision",
            request_id=request_id,
            revision=state.get("revision", 0),
        )
    return request_id, expected, None


def _duplicate_record(state: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    for record in state.get("recent_requests") or []:
        if str(record.get("request_id")) == str(request_id):
            return record
    return None


def _duplicate_receipt(
    state: dict[str, Any], manager: bool, record: dict[str, Any]
) -> Any:
    """重复提交直接返回成功，不再产生副作用。"""
    token = None
    token_id = str(record.get("token_id") or "")
    if token_id:
        token = next(
            (item for item in state.get("tokens") or [] if str(item.get("id")) == token_id),
            None,
        )
    data: dict[str, Any] = {"map": state, "can_manage": manager}
    if token is not None:
        data["token"] = token
    return _receipt(
        "success",
        "该操作已生效，重复提交已忽略。",
        request_id=str(record.get("request_id") or ""),
        revision=int(state.get("revision") or 0),
        code="map.duplicate_request",
        data=data,
        duplicate=True,
    )


def _broadcast(room_id: str, state: dict[str, Any], move: dict[str, Any] | None = None) -> None:
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    payload: dict[str, Any] = {"room_id": str(room_id), "map": state}
    if isinstance(move, dict) and move:
        payload["move"] = move
    try:
        socketio.emit("room_map_changed", payload, room=str(room_id))
    except Exception:  # noqa: BLE001 - 广播失败不能影响写入结果
        logger.warning("veil_map_broadcast_failed room=%s", room_id, exc_info=True)


def _combat_payload(room_dir) -> dict[str, Any]:
    combat = load_room_state(room_dir).get("combat")
    if not isinstance(combat, dict) or not combat.get("active"):
        return {"active": False}
    order = [str(name) for name in combat.get("order") or []]
    turn = _as_int(combat.get("turn"), 0)
    current = order[turn] if 0 <= turn < len(order) else ""
    participants = []
    for name in order:
        combatant = (combat.get("combatants") or {}).get(name) or {}
        participants.append(
            {
                "name": name,
                "user_id": combatant.get("user_id"),
                "budget": dict(combatant.get("budget") or {}),
            }
        )
    return {
        "active": True,
        "round": combat.get("round"),
        "turn": turn,
        "current": current,
        "order": participants,
    }


def _combatant_for_token(combat: dict[str, Any], token: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    combatants = combat.get("combatants") or {}
    candidates = {
        str(token.get("combatant") or "").strip().casefold(),
        str(token.get("name") or "").strip().casefold(),
    }
    owner = str(token.get("owner_user_id") or "")
    for name, combatant in combatants.items():
        if not isinstance(combatant, dict):
            continue
        if str(name).strip().casefold() in candidates:
            return str(name), combatant
        if owner and str(combatant.get("user_id") or "") == owner:
            return str(name), combatant
    return "", None


def _grid_of(state: dict[str, Any]) -> tuple[int, int, int]:
    grid = int(state.get("grid_size") or DEFAULT_GRID)
    offset = state.get("grid_offset") if isinstance(state.get("grid_offset"), dict) else {}
    return grid, _as_int(offset.get("x"), 0), _as_int(offset.get("y"), 0)


def _snap(value: Any, grid: int, offset: int = 0) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    cell = int(round((number - offset) / grid))
    cell = _clamp(cell, -1000, 1000)
    return offset + cell * grid


def _cell_of(
    token: dict[str, Any], grid: int, offset_x: int = 0, offset_y: int = 0
) -> tuple[int, int]:
    return (
        int(round((_as_int(token.get("x"), 0) - offset_x) / grid)),
        int(round((_as_int(token.get("y"), 0) - offset_y) / grid)),
    )


def _footprint(token: dict[str, Any]) -> tuple[int, int]:
    return (
        _clamp(_as_int(token.get("w"), 1), 1, 4),
        _clamp(_as_int(token.get("h"), 1), 1, 4),
    )


def _within_bounds(state: dict[str, Any], token: dict[str, Any], cell_x: int, cell_y: int) -> bool:
    """地图边界以世界原点为中心；``bounds_cells`` 是整张地图的格数。"""
    bounds = _clamp(_as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS)
    half = bounds // 2
    width, height = _footprint(token)
    return (
        -half <= cell_x
        and cell_x + width <= bounds - half
        and -half <= cell_y
        and cell_y + height <= bounds - half
    )


def _occupancy_conflict(
    state: dict[str, Any], token: dict[str, Any], cell_x: int, cell_y: int
) -> str:
    """返回目标 footprint 上已占用的棋子名称；无冲突时返回空串。"""
    grid, offset_x, offset_y = _grid_of(state)
    width, height = _footprint(token)
    for other in state.get("tokens") or []:
        if other is token or str(other.get("id")) == str(token.get("id")):
            continue
        other_x, other_y = _cell_of(other, grid, offset_x, offset_y)
        other_w, other_h = _footprint(other)
        if (
            cell_x < other_x + other_w
            and other_x < cell_x + width
            and cell_y < other_y + other_h
            and other_y < cell_y + height
        ):
            return str(other.get("name") or "其他棋子")
    return ""


def _find_token(state: dict[str, Any], reference: Any) -> dict[str, Any] | None:
    needle = str(reference or "").strip().casefold()
    if not needle:
        return None
    tokens = state.get("tokens") or []
    for token in tokens:
        if str(token.get("id") or "").casefold() == needle:
            return token
    for token in tokens:
        names = {
            str(token.get("name") or "").strip().casefold(),
            str(token.get("combatant") or "").strip().casefold(),
        }
        if needle in names:
            return token
    return None


def _member_role(info: dict[str, Any], user_id: Any) -> str:
    target = str(user_id or "")
    if target and target == str(info.get("creator_id") or ""):
        return "owner"
    for member in _active_members(info):
        if str(member.get("user_id") or "") == target:
            return str(member.get("room_role") or "").strip().lower()
    return ""


def _member_name_by_id(info: dict[str, Any], user_id: Any) -> str:
    for member in _active_members(info):
        if str(member.get("user_id") or "") != str(user_id or ""):
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        return str(card.get("name") or member.get("username") or "").strip()[:MAX_NAME]
    return ""


def _agent_actor_label(info: dict[str, Any], agent_id: Any, user_id: Any) -> str:
    if str(agent_id or "").strip().lower() in DM_AGENT_IDS:
        return "KP"
    name = _member_name_by_id(info, user_id)
    return f"AI 玩家（{name}）" if name else "AI 玩家"


def _agent_can_move(
    info: dict[str, Any], token: dict[str, Any], user_id: Any, agent_id: Any
) -> tuple[bool, str]:
    """KP 视为 DM 可移动任意棋子；其余按房间角色或棋子归属判定。"""
    if str(agent_id or "").strip().lower() in DM_AGENT_IDS:
        return True, ""
    if _member_role(info, user_id) in _OWNER_ROLES:
        return True, ""
    owner = str(token.get("owner_user_id") or "")
    if owner and owner == str(user_id or ""):
        return True, ""
    controller = _member_name_by_id(info, owner) or "其他成员"
    return (
        False,
        f"你只能移动自己绑定的棋子（「{token.get('name') or '该棋子'}」由 {controller} 控制）。",
    )


def _agent_ruleset_id(info: dict[str, Any], scenarios_dir: Any = None) -> str:
    try:
        return str(resolve_rule_id(info, scenarios_dir=scenarios_dir) or "").strip().lower()
    except Exception:  # noqa: BLE001 - 规则集解析失败时退回房间显式值
        return str((info or {}).get("ruleset_id") or "").strip().lower()


def _safe_background(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 2048:
        return None
    if text.startswith(("/", "http://", "https://", "data:image/")):
        return text
    return None


def _apply_combat_cost(
    room_id: str,
    room_dir,
    map_state: dict[str, Any],
    token: dict[str, Any],
    target_x: int,
    target_y: int,
) -> tuple[bool, str, str]:
    """校验并扣除移动力；返回 ``(True, "", "")`` 或 ``(False, code, message)``。"""
    combat = load_room_state(room_dir).get("combat")
    if not isinstance(combat, dict) or not combat.get("active"):
        return True, "", ""
    name, combatant = _combatant_for_token(combat, token)
    if not name or combatant is None:
        return True, "", ""
    order = [str(item) for item in combat.get("order") or []]
    turn = _as_int(combat.get("turn"), 0)
    current = order[turn] if 0 <= turn < len(order) else ""
    if current and name != current:
        return (
            False,
            "map.not_your_turn",
            f"现在轮到「{current}」行动，不能移动「{token.get('name')}」。",
        )
    grid, offset_x, offset_y = _grid_of(map_state)
    from_col, from_row = _cell_of(token, grid, offset_x, offset_y)
    to_col = int(round((target_x - offset_x) / grid))
    to_row = int(round((target_y - offset_y) / grid))
    cells = max(abs(to_col - from_col), abs(to_row - from_row))
    cost = cells * FEET_PER_CELL
    if cost <= 0:
        return True, "", ""
    budget = combatant.get("budget")
    if not isinstance(budget, dict) or "movement" not in budget:
        return (
            False,
            "map.budget_missing",
            "战斗行动预算尚未初始化，请重新进入战斗后再移动。",
        )
    remaining = _as_int(budget.get("movement"), 0)
    if cost > remaining:
        return (
            False,
            "map.movement_exhausted",
            f"移动距离超出本回合剩余移动力（剩余 {remaining} 英尺，需要 {cost} 英尺）。",
        )
    budget_before = remaining
    if not spend_budget(combatant, "movement", cost):
        return (
            False,
            "map.movement_exhausted",
            f"移动距离超出本回合剩余移动力（剩余 {remaining} 英尺，需要 {cost} 英尺）。",
        )
    try:
        from trpg_server.agents.tools.combat import _save_combat

        _save_combat(SimpleNamespace(room_dir=room_dir, room_id=room_id), combat)
    except Exception:  # noqa: BLE001 - 预算回滚后按服务端错误返回
        budget["movement"] = budget_before
        logger.warning("veil_map_combat_save_failed room=%s", room_id, exc_info=True)
        return False, "map.combat_save_failed", "战斗状态保存失败，请稍后重试。"
    return True, "", ""


def _map_payload(room_id: str, room_dir, info: dict[str, Any]) -> dict[str, Any]:
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if _reconcile_tokens(state, info):
            state = _save_map(room_dir, state)
    _role, manager = _room_context(info)
    return {
        "map": state,
        "combat": _combat_payload(room_dir),
        "can_manage": manager,
        "room_id": str(room_id),
    }


def _write_payload() -> dict[str, Any] | None:
    """写请求体：优先 JSON，其次表单/查询参数（DELETE 没有请求体）。"""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    merged = dict(request.args)
    merged.update(request.form)
    return merged or None


@bp.route("/rooms/<room_id>/map", methods=["GET"])
def get_map(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    return success_response(_map_payload(room_id, room_dir, info), "Map loaded")


@bp.route("/rooms/<room_id>/map", methods=["PUT"])
def update_map(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以编辑地图。",
                "map.manager_only",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)

        if "grid_size" in payload:
            grid = _as_int(payload.get("grid_size"), state["grid_size"])
            if grid < MIN_GRID or grid > MAX_GRID:
                return _invalid_request(
                    f"网格尺寸必须在 {MIN_GRID}–{MAX_GRID} 之间。",
                    "map.invalid_grid",
                    request_id=request_id,
                    revision=state["revision"],
                )
            state["grid_size"] = grid
        if "bounds_cells" in payload:
            bounds = _as_int(payload.get("bounds_cells"), state["bounds_cells"])
            if bounds < MIN_BOUNDS or bounds > MAX_BOUNDS:
                return _invalid_request(
                    f"地图边界必须在 {MIN_BOUNDS}–{MAX_BOUNDS} 格之间。",
                    "map.invalid_bounds",
                    request_id=request_id,
                    revision=state["revision"],
                )
            state["bounds_cells"] = bounds
        if "background" in payload:
            background = _safe_background(payload.get("background"))
            if background is None:
                return _invalid_request(
                    "背景图地址必须是站内路径、http(s) 链接或 data:image 数据。",
                    "map.invalid_background",
                    request_id=request_id,
                    revision=state["revision"],
                )
            state["background"] = background
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "map.update"},
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "地图设置已保存。",
        request_id=request_id,
        revision=state["revision"],
        code="map.updated",
        data={"map": state, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/tokens", methods=["POST"])
def add_token(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以添加棋子。",
                "map.manager_only",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)

        name = str(payload.get("name") or "").strip()[:MAX_NAME]
        if not name:
            return _invalid_request(
                "棋子名称不能为空。",
                "map.invalid_name",
                request_id=request_id,
                revision=state["revision"],
            )
        owner_user_id = str(payload.get("owner_user_id") or "").strip()
        if owner_user_id:
            members = {str(member.get("user_id")) for member in _active_members(info)}
            if owner_user_id not in members:
                return _invalid_request(
                    "绑定成员不在该房间中。",
                    "map.invalid_owner",
                    request_id=request_id,
                    revision=state["revision"],
                )
        if len(state["tokens"]) >= MAX_TOKENS:
            return _invalid_request(
                "棋子数量已达上限。",
                "map.too_many_tokens",
                request_id=request_id,
                revision=state["revision"],
            )
        grid, offset_x, offset_y = _grid_of(state)
        x = _snap(payload.get("x", 0), grid, offset_x)
        y = _snap(payload.get("y", 0), grid, offset_y)
        if x is None or y is None:
            return _invalid_request(
                "棋子坐标不合法。",
                "map.invalid_coordinates",
                request_id=request_id,
                revision=state["revision"],
            )
        color = str(payload.get("color") or "").strip()
        if not _HEX_COLOR.match(color):
            color = TOKEN_COLORS[len(state["tokens"]) % len(TOKEN_COLORS)]
        token = {
            "id": f"token-{uuid4().hex[:10]}",
            "name": name,
            "x": x,
            "y": y,
            "w": _clamp(_as_int(payload.get("w"), 1), 1, 4),
            "h": _clamp(_as_int(payload.get("h"), 1), 1, 4),
            "color": color,
            "owner_user_id": owner_user_id,
            "kind": "pc" if owner_user_id else "npc",
            "combatant": name,
        }
        state["tokens"].append(token)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.add", "token_id": token["id"]},
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已添加。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_added",
        data={"map": state, "token": token, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/tokens/<token_id>", methods=["DELETE"])
def delete_token(room_id: str, token_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以删除棋子。",
                "map.manager_only",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)
        tokens = [token for token in state["tokens"] if str(token.get("id")) != str(token_id)]
        if len(tokens) == len(state["tokens"]):
            return _invalid_request(
                "棋子不存在。",
                "map.token_not_found",
                request_id=request_id,
                revision=state["revision"],
                http_status=404,
            )
        state["tokens"] = tokens
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.delete", "token_id": token_id},
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已删除。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_deleted",
        data={"map": state, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/tokens/<token_id>/move", methods=["POST"])
def move_token(room_id: str, token_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        token = next((item for item in state["tokens"] if str(item.get("id")) == str(token_id)), None)
        if token is None:
            return _invalid_request(
                "棋子不存在。",
                "map.token_not_found",
                revision=state["revision"],
                http_status=404,
            )
        owner_user_id = str(token.get("owner_user_id") or "")
        if not manager and owner_user_id != str(session.get("user_id") or ""):
            return _rejected_request(
                "你只能移动自己绑定的棋子。",
                "map.not_owner",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)

        grid, offset_x, offset_y = _grid_of(state)
        x = _snap(payload.get("x"), grid, offset_x)
        y = _snap(payload.get("y"), grid, offset_y)
        if x is None or y is None:
            return _invalid_request(
                "棋子坐标不合法。",
                "map.invalid_coordinates",
                request_id=request_id,
                revision=state["revision"],
            )
        ok, code, message = _apply_combat_cost(room_id, room_dir, state, token, x, y)
        if not ok:
            return _rejected_request(
                message,
                code,
                request_id=request_id,
                revision=state["revision"],
                http_status=409,
                data={"map": state, "can_manage": manager},
            )
        token["x"] = x
        token["y"] = y
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.move", "token_id": token["id"]},
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已移动。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_moved",
        data={"map": state, "token": token, "combat": _combat_payload(room_dir), "can_manage": manager},
    )


def _point_from_payload(raw: Any, state: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """解析 ``{cell_x, cell_y}`` 或 ``{x, y}``，返回 ``(world_x, world_y, cell_x, cell_y)``。"""
    if not isinstance(raw, dict):
        return None
    grid, offset_x, offset_y = _grid_of(state)
    if "cell_x" in raw or "cell_y" in raw:
        try:
            cell_x = int(raw.get("cell_x"))
            cell_y = int(raw.get("cell_y"))
        except (TypeError, ValueError):
            return None
        return (
            offset_x + cell_x * grid,
            offset_y + cell_y * grid,
            cell_x,
            cell_y,
        )
    x, y = _coordinate(raw.get("x")), _coordinate(raw.get("y"))
    if x is None or y is None:
        return None
    cell_x = int(round((x - offset_x) / grid))
    cell_y = int(round((y - offset_y) / grid))
    return x, y, cell_x, cell_y


@bp.route("/rooms/<room_id>/walls", methods=["PUT"])
def update_walls(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以编辑墙体。",
                "map.manager_only",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)
        raw_walls = payload.get("walls")
        if not isinstance(raw_walls, list):
            return _invalid_request(
                "walls 必须是墙体数组。",
                "map.invalid_walls",
                request_id=request_id,
                revision=state["revision"],
            )
        if len(raw_walls) > MAX_WALLS:
            return _invalid_request(
                f"墙体数量不能超过 {MAX_WALLS} 段。",
                "map.too_many_walls",
                request_id=request_id,
                revision=state["revision"],
            )
        state["walls"] = _normalize_walls(raw_walls)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "map.walls"},
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "墙体已保存。",
        request_id=request_id,
        revision=state["revision"],
        code="map.walls_updated",
        data={"map": state, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/spatial/range", methods=["POST"])
def spatial_range(room_id: str):
    room_dir, _info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    with _MAP_LOCK:
        state = _load_map(room_dir)
    origin = _point_from_payload(payload.get("origin"), state)
    if origin is None:
        return error_response("origin 需要 {cell_x, cell_y} 或 {x, y}。", 400, "map.invalid_origin")
    shape = str(payload.get("shape") or "circle").strip().lower()
    if shape not in {"circle", "cone", "line"}:
        return error_response("shape 只能是 circle / cone / line。", 400, "map.invalid_shape")
    metric = str(payload.get("metric") or "chebyshev").strip().lower()
    if metric not in spatial.METRICS:
        return error_response("metric 只能是 chebyshev 或 euclidean。", 400, "map.invalid_metric")
    if "size_cells" in payload:
        size = _as_int(payload.get("size_cells"), -1)
    elif "range_ft" in payload:
        size = spatial.feet_to_cells(payload.get("range_ft"), FEET_PER_CELL)
    else:
        return error_response("需要 size_cells 或 range_ft。", 400, "map.missing_range")
    if size < 0 or size > MAX_BOUNDS * 2:
        return error_response("范围大小不合法。", 400, "map.invalid_range")
    try:
        cells = spatial.range_cells(
            (origin[2], origin[3]),
            shape=shape,
            size_cells=size,
            direction=payload.get("direction"),
            metric=metric,
        )
    except ValueError as exc:
        return error_response(str(exc), 400, "map.invalid_direction")
    grid, offset_x, offset_y = _grid_of(state)
    return success_response(
        {
            "shape": shape,
            "metric": metric,
            "origin": {"cell_x": origin[2], "cell_y": origin[3], "x": origin[0], "y": origin[1]},
            "size_cells": size,
            "size_ft": spatial.cells_to_feet(size, FEET_PER_CELL),
            "cells": [
                {
                    "cell_x": cell_x,
                    "cell_y": cell_y,
                    "x": offset_x + cell_x * grid,
                    "y": offset_y + cell_y * grid,
                }
                for cell_x, cell_y in cells
            ],
            "count": len(cells),
        },
        "Range resolved",
    )


@bp.route("/rooms/<room_id>/spatial/line", methods=["POST"])
def spatial_line(room_id: str):
    """视线或移动阻挡：起点到终点是否被墙体挡住。"""
    room_dir, _info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    with _MAP_LOCK:
        state = _load_map(room_dir)
    mode = str(payload.get("mode") or "sight").strip().lower()
    if mode not in {"sight", "movement"}:
        return error_response("mode 只能是 sight 或 movement。", 400, "map.invalid_mode")
    start = _point_from_payload(payload.get("from"), state)
    end = _point_from_payload(payload.get("to"), state)
    if start is None or end is None:
        return error_response(
            "from / to 需要 {cell_x, cell_y} 或 {x, y}。",
            400,
            "map.invalid_points",
        )
    blockers = spatial.segment_blockers(
        state.get("walls") or [],
        (start[0], start[1]),
        (end[0], end[1]),
        mode=mode,
    )
    distance = spatial.cell_distance((start[2], start[3]), (end[2], end[3]))
    return success_response(
        {
            "mode": mode,
            "blocked": bool(blockers),
            "blocked_by": blockers,
            "distance_cells": distance,
            "distance_ft": spatial.cells_to_feet(distance, FEET_PER_CELL),
            "from": {"cell_x": start[2], "cell_y": start[3], "x": start[0], "y": start[1]},
            "to": {"cell_x": end[2], "cell_y": end[3], "x": end[0], "y": end[1]},
        },
        "Line resolved",
    )


def _move_token_agent(
    room_dir,
    info: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
    agent_id: str,
    token_ref: str,
    cell_x: int,
    cell_y: int,
    reason: str = "",
    scenarios_dir: Any = None,
) -> dict[str, Any]:
    """KP/AI 移动棋子：权限、边界、占位与战斗预算都走同一套插件规则。"""
    if not str(room_id or "").strip() or room_dir is None:
        return {"ok": False, "error": "缺少房间上下文，无法移动棋子。"}
    if _agent_ruleset_id(info, scenarios_dir) != "dnd5e":
        return {"ok": False, "error": "当前房间的规则集不支持地图棋子移动。"}
    with _MAP_LOCK:
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
        token = _find_token(state, token_ref)
        if token is None:
            return {"ok": False, "error": f"没有找到棋子「{token_ref}」。"}
        allowed, denied = _agent_can_move(info, token, user_id, agent_id)
        if not allowed:
            return {"ok": False, "error": denied}

        grid, offset_x, offset_y = _grid_of(state)
        target_x = _snap(cell_x * grid + offset_x, grid, offset_x)
        target_y = _snap(cell_y * grid + offset_y, grid, offset_y)
        if target_x is None or target_y is None:
            return {"ok": False, "error": "目标格坐标不合法。"}
        to_cell_x = int(round((target_x - offset_x) / grid))
        to_cell_y = int(round((target_y - offset_y) / grid))
        from_cell_x, from_cell_y = _cell_of(token, grid, offset_x, offset_y)

        if not _within_bounds(state, token, to_cell_x, to_cell_y):
            bounds = _clamp(
                _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
            )
            return {"ok": False, "error": f"目标位置超出地图边界（当前地图 {bounds} 格）。"}
        occupied = _occupancy_conflict(state, token, to_cell_x, to_cell_y)
        if occupied:
            return {"ok": False, "error": f"目标格已被「{occupied}」占用，无法移动。"}

        combat = load_room_state(room_dir).get("combat")
        combat_active = isinstance(combat, dict) and bool(combat.get("active"))
        if combat_active:
            combat_name, combatant = _combatant_for_token(combat, token)
            if not combat_name or combatant is None:
                return {
                    "ok": False,
                    "error": (
                        f"战斗进行中，「{token.get('name') or '该棋子'}」没有加入先攻顺序，"
                        "无法移动。"
                    ),
                }

        # 战斗内复用现有行动经济：当前行动者、movement 预算、扣减与回滚。
        ok, code, message = _apply_combat_cost(
            room_id, room_dir, state, token, target_x, target_y
        )
        if not ok:
            return {"ok": False, "error": message or "移动被战斗规则拒绝。", "code": code}

        distance = max(abs(to_cell_x - from_cell_x), abs(to_cell_y - from_cell_y))
        cost_ft = distance * FEET_PER_CELL if combat_active else 0
        token["x"] = target_x
        token["y"] = target_y
        state = _save_map(room_dir, state)

    move: dict[str, Any] = {
        "source": "ai",
        "actor": _agent_actor_label(info, agent_id, user_id),
        "token_id": str(token.get("id") or ""),
        "token_name": str(token.get("name") or ""),
        "from": {"cell_x": from_cell_x, "cell_y": from_cell_y},
        "to": {"cell_x": to_cell_x, "cell_y": to_cell_y},
        "cost_ft": cost_ft,
        "combat_active": combat_active,
    }
    if reason:
        move["reason"] = reason
    _broadcast(room_id, state, move)
    return {
        "ok": True,
        "message": f"{move['actor']} 将「{move['token_name']}」移动到 ({to_cell_x}, {to_cell_y})。",
        "token": token,
        "from": move["from"],
        "to": move["to"],
        "cost_ft": cost_ft,
        "combat_active": combat_active,
    }


def _move_token_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        room_dir = getattr(context, "room_dir", None)
        room_id = str(getattr(context, "room_id", "") or "")
        info_fn = getattr(context, "room_info", None)
        info = info_fn() if callable(info_fn) else {}
        if not isinstance(info, dict) or not info:
            info = read_json(room_dir / "info.json", default={}) if room_dir else {}
        token_ref = str(arguments.get("token") or "").strip()
        if not token_ref:
            return {"ok": False, "error": "请提供要移动的棋子 id 或名称。"}
        try:
            cell_x = int(arguments.get("cell_x"))
            cell_y = int(arguments.get("cell_y"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "目标格坐标必须是整数（cell_x / cell_y）。"}
        return _move_token_agent(
            room_dir,
            info,
            room_id=room_id,
            user_id=str(getattr(context, "user_id", "") or ""),
            agent_id=str(getattr(context, "agent_id", "") or ""),
            token_ref=token_ref,
            cell_x=cell_x,
            cell_y=cell_y,
            reason=str(arguments.get("reason") or "").strip()[:MAX_REASON],
            scenarios_dir=getattr(context, "scenarios_dir", None),
        )
    except Exception:  # noqa: BLE001 - 工具异常只影响本次调用
        logger.warning("veil_map_agent_move_failed", exc_info=True)
        return {"ok": False, "error": "地图移动失败，请稍后重试。"}


MOVE_TOKEN_TOOL = AgentTool(
    name="veil-map.move_token",
    description=(
        "移动地图上的一枚棋子。战斗中必须由当前行动者发起，并按移动距离消耗 movement "
        "预算；探索阶段不消耗行动经济，但仍受地图边界与占位限制。KP 可以移动任意棋子，"
        "AI 玩家只能移动自己绑定的棋子。cell_x / cell_y 是格子坐标（可为负），服务端会"
        "按网格吸附；reason 用一句短语说明移动意图。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "棋子 id 或名称"},
            "cell_x": {"type": "integer", "description": "目标格列坐标（可为负）"},
            "cell_y": {"type": "integer", "description": "目标格行坐标（可为负）"},
            "reason": {"type": "string", "description": "给玩家的简短移动说明（可选）"},
        },
        "required": ["token", "cell_x", "cell_y"],
    },
    handler=_move_token_tool,
    rulesets=("dnd5e",),
)


def create_tools() -> list[AgentTool]:
    return [MOVE_TOKEN_TOOL]


__all__ = ["bp", "create_tools", "MOVE_TOKEN_TOOL"]
