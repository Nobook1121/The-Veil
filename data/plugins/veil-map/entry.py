"""地图插件（veil-map）的服务端接口。

核心只提供画布宿主与事件通道；棋子坐标、网格吸附、多格占位、地图基础
编辑与战斗回合联动都在这里落地。状态写在房间自己的插件命名空间：

    <ROOMS_DIR>/<room_id>/plugins/veil-map/map.json

战斗行动经济直接复用 ``trpg_server.combat.scheduler.spend_budget``，不另写
一套移动力判定；越权与预算不足都返回可读中文消息，由前端提示。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, request, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.combat.scheduler import spend_budget
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated

bp = Blueprint("veil_map", __name__, url_prefix="/api/plugins/veil-map")
logger = logging.getLogger(__name__)

MAP_VERSION = 1
DEFAULT_GRID = 64
MIN_GRID = 16
MAX_GRID = 256
MAX_TOKENS = 200
MAX_NAME = 40
FEET_PER_CELL = 5
TOKEN_COLORS = ("#c8a35a", "#6fa8dc", "#93c47d", "#e06666", "#8e7cc3", "#76a5af", "#f6b26b", "#a64d79")
_MAP_LOCK = threading.RLock()
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_OWNER_ROLES = {"owner", "admin"}


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
        "scene_id": "default",
        "background": "",
        "grid_size": DEFAULT_GRID,
        "tokens": [],
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


def _normalize_map(raw: Any) -> dict[str, Any]:
    state = _default_map()
    if isinstance(raw, dict):
        state.update(raw)
    state["version"] = MAP_VERSION
    state["scene_id"] = str(state.get("scene_id") or "default")[:64]
    state["background"] = str(state.get("background") or "")[:2048]
    state["grid_size"] = _clamp(_as_int(state.get("grid_size"), DEFAULT_GRID), MIN_GRID, MAX_GRID)
    tokens: list[dict[str, Any]] = []
    for item in state.get("tokens") or []:
        token = _normalize_token(item)
        if token is not None:
            tokens.append(token)
    state["tokens"] = tokens[-MAX_TOKENS:]
    state["updated_at"] = str(state.get("updated_at") or "")
    return state


def _load_map(room_dir) -> dict[str, Any]:
    return _normalize_map(read_json(_map_path(room_dir), default={}))


def _save_map(room_dir, state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_map(state)
    normalized["updated_at"] = _now()
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


def _broadcast(room_id: str, state: dict[str, Any]) -> None:
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit("room_map_changed", {"room_id": str(room_id), "map": state}, room=str(room_id))
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


def _snap(value: Any, grid: int) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    cell = int(round(number / grid))
    cell = _clamp(cell, -1000, 1000)
    return cell * grid


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
) -> tuple[bool, Any]:
    """校验并扣除移动力；返回 ``(True, None)`` 或 ``(False, error_response)``。"""
    combat = load_room_state(room_dir).get("combat")
    if not isinstance(combat, dict) or not combat.get("active"):
        return True, None
    name, combatant = _combatant_for_token(combat, token)
    if not name or combatant is None:
        return True, None
    order = [str(item) for item in combat.get("order") or []]
    turn = _as_int(combat.get("turn"), 0)
    current = order[turn] if 0 <= turn < len(order) else ""
    if current and name != current:
        return False, error_response(
            f"现在轮到「{current}」行动，不能移动「{token.get('name')}」。",
            409,
            "map.not_your_turn",
        )
    grid = int(map_state.get("grid_size") or DEFAULT_GRID)
    from_col = int(round(_as_int(token.get("x"), 0) / grid))
    from_row = int(round(_as_int(token.get("y"), 0) / grid))
    to_col = int(round(target_x / grid))
    to_row = int(round(target_y / grid))
    cells = max(abs(to_col - from_col), abs(to_row - from_row))
    cost = cells * FEET_PER_CELL
    if cost <= 0:
        return True, None
    budget = combatant.get("budget")
    if not isinstance(budget, dict) or "movement" not in budget:
        return False, error_response(
            "战斗行动预算尚未初始化，请重新进入战斗后再移动。",
            409,
            "map.budget_missing",
        )
    remaining = _as_int(budget.get("movement"), 0)
    if cost > remaining:
        return False, error_response(
            f"移动距离超出本回合剩余移动力（剩余 {remaining} 英尺，需要 {cost} 英尺）。",
            409,
            "map.movement_exhausted",
        )
    budget_before = remaining
    if not spend_budget(combatant, "movement", cost):
        return False, error_response(
            f"移动距离超出本回合剩余移动力（剩余 {remaining} 英尺，需要 {cost} 英尺）。",
            409,
            "map.movement_exhausted",
        )
    try:
        from trpg_server.agents.tools.combat import _save_combat

        _save_combat(SimpleNamespace(room_dir=room_dir, room_id=room_id), combat)
    except Exception:  # noqa: BLE001 - 预算回滚后按服务端错误返回
        budget["movement"] = budget_before
        logger.warning("veil_map_combat_save_failed room=%s", room_id, exc_info=True)
        return False, error_response("战斗状态保存失败，请稍后重试。", 500, "map.combat_save_failed")
    return True, None


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


@bp.route("/rooms/<room_id>/map", methods=["GET"])
def get_map(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    return success_response(_map_payload(room_id, room_dir, info), "Map loaded")


@bp.route("/rooms/<room_id>/map", methods=["PUT"])
def update_map(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    if not manager:
        return error_response("只有房主或房管可以编辑地图。", 403, "map.manager_only")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if "grid_size" in payload:
            raw_grid = payload.get("grid_size")
            grid = _as_int(raw_grid, state["grid_size"])
            if grid < MIN_GRID or grid > MAX_GRID:
                return error_response(
                    f"网格尺寸必须在 {MIN_GRID}–{MAX_GRID} 之间。",
                    400,
                    "map.invalid_grid",
                )
            state["grid_size"] = grid
        if "background" in payload:
            background = _safe_background(payload.get("background"))
            if background is None:
                return error_response(
                    "背景图地址必须是站内路径、http(s) 链接或 data:image 数据。",
                    400,
                    "map.invalid_background",
                )
            state["background"] = background
        state = _save_map(room_dir, state)
    _broadcast(room_id, state)
    return success_response({"map": state, "can_manage": True}, "Map updated")


@bp.route("/rooms/<room_id>/tokens", methods=["POST"])
def add_token(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    if not manager:
        return error_response("只有房主或房管可以添加棋子。", 403, "map.manager_only")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    name = str(payload.get("name") or "").strip()[:MAX_NAME]
    if not name:
        return error_response("棋子名称不能为空。", 400, "map.invalid_name")
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    if owner_user_id:
        members = {str(member.get("user_id")) for member in _active_members(info)}
        if owner_user_id not in members:
            return error_response("绑定成员不在该房间中。", 400, "map.invalid_owner")
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if len(state["tokens"]) >= MAX_TOKENS:
            return error_response("棋子数量已达上限。", 400, "map.too_many_tokens")
        grid = int(state["grid_size"])
        x = _snap(payload.get("x", 0), grid)
        y = _snap(payload.get("y", 0), grid)
        if x is None or y is None:
            return error_response("棋子坐标不合法。", 400, "map.invalid_coordinates")
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
        state = _save_map(room_dir, state)
    _broadcast(room_id, state)
    return success_response({"map": state, "token": token}, "Token added")


@bp.route("/rooms/<room_id>/tokens/<token_id>", methods=["DELETE"])
def delete_token(room_id: str, token_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    if not manager:
        return error_response("只有房主或房管可以删除棋子。", 403, "map.manager_only")
    with _MAP_LOCK:
        state = _load_map(room_dir)
        tokens = [token for token in state["tokens"] if str(token.get("id")) != str(token_id)]
        if len(tokens) == len(state["tokens"]):
            return error_response("棋子不存在。", 404, "map.token_not_found")
        state["tokens"] = tokens
        state = _save_map(room_dir, state)
    _broadcast(room_id, state)
    return success_response({"map": state}, "Token removed")


@bp.route("/rooms/<room_id>/tokens/<token_id>/move", methods=["POST"])
def move_token(room_id: str, token_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        token = next((item for item in state["tokens"] if str(item.get("id")) == str(token_id)), None)
        if token is None:
            return error_response("棋子不存在。", 404, "map.token_not_found")
        owner_user_id = str(token.get("owner_user_id") or "")
        if not manager and owner_user_id != str(session.get("user_id") or ""):
            return error_response("你只能移动自己绑定的棋子。", 403, "map.not_owner")
        grid = int(state.get("grid_size") or DEFAULT_GRID)
        x = _snap(payload.get("x"), grid)
        y = _snap(payload.get("y"), grid)
        if x is None or y is None:
            return error_response("棋子坐标不合法。", 400, "map.invalid_coordinates")
        ok, error = _apply_combat_cost(room_id, room_dir, state, token, x, y)
        if not ok:
            return error
        token["x"] = x
        token["y"] = y
        state = _save_map(room_dir, state)
    _broadcast(room_id, state)
    return success_response(
        {"map": state, "token": token, "combat": _combat_payload(room_dir)},
        "Token moved",
    )


__all__ = ["bp"]
