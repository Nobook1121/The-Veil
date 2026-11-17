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

from flask import Blueprint, current_app, jsonify, request, send_from_directory, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.combat.scheduler import spend_budget
from trpg_server.json_store import read_json
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import resolve_rule_id

bp = Blueprint("veil_map", __name__, url_prefix="/api/plugins/veil-map")
logger = logging.getLogger(__name__)

MAP_VERSION = 3
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
MAX_LIGHTS = 100
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
media = _load_local_module("media")
importer = _load_local_module("importer")
scene_store = _load_local_module("scene_store")
visibility = _load_local_module("visibility")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _as_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _default_calibration() -> dict[str, int]:
    return {"cells_across": 0, "cells_down": 0, "offset_x": 0, "offset_y": 0}


def _default_ambient() -> dict[str, Any]:
    return {
        "enabled": False,
        "color": "#ffffff",
        "intensity": 1.0,
        "darkness": 0.0,
        "baked": False,
        # 昼夜与天气本轮不做，只留字段给后续场景环境使用。
        "time_of_day": "",
        "weather": "",
    }


DEFAULT_VISION_CELLS = 12
MAX_VISION_CELLS = MAX_BOUNDS * 2
MAX_ACTIVE_LIGHTS = 24


def _default_fog() -> dict[str, Any]:
    """静态迷雾：探索记录（格位掩码）+ 观察者当前可见。

    动态光源投影做的是静态投影（光源位置、明暗半径、墙体遮挡），昼夜循环
    和天气没有实现。迷雾默认开启，房主可在面板里关掉；关掉后服务端退回
    整图推送（没有任何需要隐藏的信息）。
    """
    return {
        "enabled": True,
        "mode": "static",
        "vision_cells": DEFAULT_VISION_CELLS,
        "explored": "",
        "explored_count": 0,
        "bounds_cells": BOUNDS_DEFAULT,
        "grid_size": DEFAULT_GRID,
    }


def _normalize_fog(raw: Any, bounds_cells: int, grid_size: int) -> dict[str, Any]:
    """迷雾掩码只在同一网格/边界下有效；标定变了就清空探索记录。"""
    fog = _default_fog()
    fog["bounds_cells"] = int(bounds_cells)
    fog["grid_size"] = int(grid_size)
    if not isinstance(raw, dict):
        return fog
    fog["enabled"] = bool(raw.get("enabled", fog["enabled"]))
    fog["vision_cells"] = _clamp(
        _as_int(raw.get("vision_cells"), fog["vision_cells"]), 1, MAX_VISION_CELLS
    )
    text = str(raw.get("explored") or "")
    same_space = (
        _as_int(raw.get("bounds_cells"), 0) == int(bounds_cells)
        and _as_int(raw.get("grid_size"), 0) == int(grid_size)
    )
    if same_space and text:
        fog["explored"] = text
        fog["explored_count"] = max(0, _as_int(raw.get("explored_count"), 0))
    return fog


def _default_map() -> dict[str, Any]:
    return {
        "version": MAP_VERSION,
        "revision": 0,
        "scene_id": "default",
        "background": "",
        "background_meta": {},
        "grid_size": DEFAULT_GRID,
        "grid_offset": {"x": 0, "y": 0},
        "calibration": _default_calibration(),
        "bounds_cells": BOUNDS_DEFAULT,
        "tokens": [],
        "walls": [],
        "lights": [],
        "ambient_light": _default_ambient(),
        "fog": _default_fog(),
        "recent_requests": [],
        "updated_at": "",
    }


def _fraction(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _carry_fraction(cell: int, fraction: float) -> tuple[int, float]:
    """把格内偏移折进 ``[0, 1)``，超出部分进位到格坐标。"""
    carry = math.floor(fraction)
    return int(cell) + int(carry), fraction - carry


def _normalize_token_light(raw: Any) -> dict[str, Any] | None:
    """棋子自带光源（火把、法术光）；与场景光源走同一套明暗半径描述。"""
    if not isinstance(raw, dict) or raw.get("remove") is True:
        return None
    bright = _clamp(_as_int(raw.get("bright_cells"), 4), 0, MAX_VISION_CELLS)
    raw_dim = raw.get("dim_cells", raw.get("range_cells"))
    dim = _clamp(_as_int(raw_dim, max(bright, 8)), 0, MAX_VISION_CELLS)
    dim = max(dim, bright)
    if dim <= 0:
        return None
    color = str(raw.get("color") or "#ffaa00").strip().lower()
    if not _HEX_COLOR.match(color):
        color = "#ffaa00"
    try:
        intensity = float(raw.get("intensity", 1.0))
    except (TypeError, ValueError):
        intensity = 1.0
    if not math.isfinite(intensity):
        intensity = 1.0
    return {
        "bright_cells": bright,
        "dim_cells": dim,
        "range_ft": spatial.cells_to_feet(dim, FEET_PER_CELL),
        "color": color,
        "intensity": max(0.0, min(10.0, intensity)),
        "enabled": bool(raw.get("enabled", True)),
        "shadows": bool(raw.get("shadows", True)),
    }


def _normalize_token(raw: Any, grid: int, offset_x: int, offset_y: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    token_id = str(raw.get("id") or "").strip()
    if not token_id:
        return None
    name = str(raw.get("name") or "").strip()[:MAX_NAME]
    fraction_x = 0.0
    fraction_y = 0.0
    raw_cell_x, raw_cell_y = raw.get("cell_x"), raw.get("cell_y")
    if raw_cell_x is not None and raw_cell_y is not None:
        try:
            cell_x = int(round(float(raw_cell_x)))
            cell_y = int(round(float(raw_cell_y)))
        except (TypeError, ValueError):
            return None
        fraction_x = _fraction(raw.get("offset_x"))
        fraction_y = _fraction(raw.get("offset_y"))
    else:
        # 旧数据是图片像素坐标：换算成“格 + 格内偏移”，世界位置保持不变。
        x = _coordinate(raw.get("x"))
        y = _coordinate(raw.get("y"))
        if x is None:
            x = 0
        if y is None:
            y = 0
        exact_x = (x - offset_x) / grid
        exact_y = (y - offset_y) / grid
        cell_x = int(math.floor(exact_x + 1e-9))
        cell_y = int(math.floor(exact_y + 1e-9))
        fraction_x = exact_x - cell_x
        fraction_y = exact_y - cell_y
    cell_x, fraction_x = _carry_fraction(cell_x, fraction_x)
    cell_y, fraction_y = _carry_fraction(cell_y, fraction_y)
    cell_x = _clamp(cell_x, -1_000_000, 1_000_000)
    cell_y = _clamp(cell_y, -1_000_000, 1_000_000)
    fraction_x = round(fraction_x, 6)
    fraction_y = round(fraction_y, 6)
    return {
        "id": token_id[:64],
        "name": name or "棋子",
        "cell_x": cell_x,
        "cell_y": cell_y,
        "offset_x": fraction_x,
        "offset_y": fraction_y,
        # 派生世界坐标只用于渲染与旧接口兼容；落盘时会被去掉。
        "x": int(round(offset_x + (cell_x + fraction_x) * grid)),
        "y": int(round(offset_y + (cell_y + fraction_y) * grid)),
        "w": _clamp(_as_int(raw.get("w"), 1), 1, 4),
        "h": _clamp(_as_int(raw.get("h"), 1), 1, 4),
        "color": str(raw.get("color") or TOKEN_COLORS[0]),
        "owner_user_id": str(raw.get("owner_user_id") or ""),
        "kind": str(raw.get("kind") or "npc"),
        "combatant": str(raw.get("combatant") or ""),
        # 0 表示跟随场景迷雾的默认视野；>0 时为该棋子的视野半径（格）。
        "vision_cells": _clamp(_as_int(raw.get("vision_cells"), 0), 0, MAX_VISION_CELLS),
        "light": _normalize_token_light(raw.get("light")),
    }


def _normalize_background_meta(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    url = str(raw.get("url") or "").strip()
    if not url.startswith("/"):
        return {}
    meta = {
        "kind": str(raw.get("kind") or "uploaded")[:24],
        "format": str(raw.get("format") or raw.get("kind") or "uploaded")[:24],
        "name": str(raw.get("name") or "地图图片")[:120],
        "url": url[:2048],
        "width": max(0, _as_int(raw.get("width"), 0)),
        "height": max(0, _as_int(raw.get("height"), 0)),
        "bytes": max(0, _as_int(raw.get("bytes"), 0)),
        "uploaded_at": str(raw.get("uploaded_at") or "")[:32],
    }
    details = raw.get("import")
    if isinstance(details, dict):
        warnings = [
            str(item).strip()[:200]
            for item in (details.get("warnings") or [])
            if str(item).strip()
        ][:8]
        meta["import"] = {
            "format": str(details.get("format") or meta["format"])[:24],
            "walls": max(0, _as_int(details.get("walls"), 0)),
            "doors": max(0, _as_int(details.get("doors"), 0)),
            "lights": max(0, _as_int(details.get("lights"), 0)),
            "needs_review": bool(details.get("needs_review")),
            "warnings": warnings,
        }
    return meta


def _normalize_calibration(raw: Any) -> dict[str, int]:
    calibration = _default_calibration()
    if isinstance(raw, dict):
        calibration["cells_across"] = max(0, _as_int(raw.get("cells_across"), 0))
        calibration["cells_down"] = max(0, _as_int(raw.get("cells_down"), 0))
        calibration["offset_x"] = max(0, _as_int(raw.get("offset_x"), 0))
        calibration["offset_y"] = max(0, _as_int(raw.get("offset_y"), 0))
    return calibration


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
        # 窗户挡人不挡视线：门/窗/墙默认都阻挡移动，门可通过 state 放行。
        "blocks_movement": bool(raw.get("blocks_movement", True)),
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


def _normalize_light(raw: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    x, y = _coordinate(raw.get("x")), _coordinate(raw.get("y"))
    if x is None or y is None:
        return None
    range_cells = _as_int(raw.get("range_cells"), 0)
    if range_cells <= 0:
        range_cells = spatial.feet_to_cells(raw.get("range_ft"), FEET_PER_CELL)
    range_cells = _clamp(range_cells, 1, MAX_BOUNDS * 2)
    bright_cells = _clamp(_as_int(raw.get("bright_cells"), 0), 0, range_cells)
    color = str(raw.get("color") or "#ffaa00").strip().lower()
    if not _HEX_COLOR.match(color):
        color = "#ffaa00"
    try:
        intensity = float(raw.get("intensity", 1.0))
    except (TypeError, ValueError):
        intensity = 1.0
    if not math.isfinite(intensity):
        intensity = 1.0
    light_id = str(raw.get("id") or "").strip()[:64] or f"light-{index + 1}"
    return {
        "id": light_id,
        "x": x,
        "y": y,
        "range_cells": range_cells,
        "range_ft": spatial.cells_to_feet(range_cells, FEET_PER_CELL),
        "bright_cells": bright_cells,
        "color": color,
        "intensity": max(0.0, min(10.0, intensity)),
        "shadows": bool(raw.get("shadows", True)),
        "angle": _clamp(_as_int(raw.get("angle"), 360), 0, 360),
        # 场景光源可被 KP 开关；UVTT 导入的光源默认点亮、可直接使用。
        "enabled": bool(raw.get("enabled", True)),
    }


def _normalize_lights(raw: Any) -> list[dict[str, Any]]:
    lights: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        light = _normalize_light(item, index)
        if light is None:
            continue
        if light["id"] in seen:
            light["id"] = f"{light['id']}-{index + 1}"
        seen.add(light["id"])
        lights.append(light)
        if len(lights) >= MAX_LIGHTS:
            break
    return lights


def _normalize_ambient(raw: Any) -> dict[str, Any]:
    ambient = _default_ambient()
    if not isinstance(raw, dict):
        return ambient
    color = str(raw.get("color") or "#ffffff").strip().lower()
    ambient["color"] = color if _HEX_COLOR.match(color) else "#ffffff"
    try:
        intensity = float(raw.get("intensity", 1.0))
    except (TypeError, ValueError):
        intensity = 1.0
    if not math.isfinite(intensity):
        intensity = 1.0
    ambient["intensity"] = max(0.0, min(10.0, intensity))
    try:
        darkness = float(raw.get("darkness", 0.0))
    except (TypeError, ValueError):
        darkness = 0.0
    if not math.isfinite(darkness):
        darkness = 0.0
    ambient["darkness"] = max(0.0, min(1.0, darkness))
    ambient["enabled"] = bool(raw.get("enabled"))
    ambient["baked"] = bool(raw.get("baked"))
    # 预留字段：本轮不参与光照计算，只在场景里原样保存。
    ambient["time_of_day"] = str(raw.get("time_of_day") or "")[:32]
    ambient["weather"] = str(raw.get("weather") or "")[:32]
    return ambient


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
    state["background_meta"] = _normalize_background_meta(state.get("background_meta"))
    state["grid_size"] = _clamp(_as_int(state.get("grid_size"), DEFAULT_GRID), MIN_GRID, MAX_GRID)
    offset = state.get("grid_offset") if isinstance(state.get("grid_offset"), dict) else {}
    grid = int(state["grid_size"])
    state["grid_offset"] = {
        "x": _clamp(_as_int(offset.get("x"), 0), -grid, grid),
        "y": _clamp(_as_int(offset.get("y"), 0), -grid, grid),
    }
    state["calibration"] = _normalize_calibration(state.get("calibration"))
    state["bounds_cells"] = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    tokens: list[dict[str, Any]] = []
    for item in state.get("tokens") or []:
        token = _normalize_token(item, grid, state["grid_offset"]["x"], state["grid_offset"]["y"])
        if token is not None:
            tokens.append(token)
    state["tokens"] = tokens[-MAX_TOKENS:]
    state["walls"] = _normalize_walls(state.get("walls"))
    state["lights"] = _normalize_lights(state.get("lights"))
    state["ambient_light"] = _normalize_ambient(state.get("ambient_light"))
    state["fog"] = _normalize_fog(state.get("fog"), state["bounds_cells"], state["grid_size"])
    state["recent_requests"] = _normalize_recent_requests(state.get("recent_requests"))
    state["updated_at"] = str(state.get("updated_at") or "")
    return state


def _load_map(room_dir) -> dict[str, Any]:
    state, _source = scene_store.load(room_dir)
    return _normalize_map(state)


def _persistable(state: dict[str, Any]) -> dict[str, Any]:
    """棋子以格坐标落盘；``x/y`` 是派生字段，不写进分片。"""
    persisted = dict(state)
    persisted["tokens"] = [
        {key: value for key, value in token.items() if key not in {"x", "y"}}
        for token in state.get("tokens") or []
        if isinstance(token, dict)
    ]
    return persisted


def _save_map(
    room_dir,
    state: dict[str, Any],
    request_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写入场景并推进 revision；成功的写请求会留下幂等记录。

    新布局把 scene / tokens / walls / lights / journal 拆成独立分片，revision
    写在每个分片里并由 scene_store 统一提交，旧 ``map.json`` 只在没有分片时读取。
    """
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
    # 探索记录跟着场景一起落盘：写操作发生时，队伍视野覆盖的格子并入掩码。
    _update_explored(room_dir, normalized)
    scene_store.save(room_dir, _persistable(normalized))
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
    """按活跃成员补齐 PC 棋子；已有棋子保留格坐标与尺寸。"""
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
            "cell_x": index % 6,
            "cell_y": index // 6,
            "offset_x": 0.0,
            "offset_y": 0.0,
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
    map_view: dict[str, Any] | None = None,
    fog_view: dict[str, Any] | None = None,
):
    revision = int(state.get("revision") or 0)
    body = {"map": map_view or state, "can_manage": manager}
    if fog_view is not None:
        body["fog"] = fog_view
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
    state: dict[str, Any],
    manager: bool,
    record: dict[str, Any],
    *,
    map_view: dict[str, Any] | None = None,
    fog_view: dict[str, Any] | None = None,
) -> Any:
    """重复提交直接返回成功，不再产生副作用。"""
    token = None
    token_id = str(record.get("token_id") or "")
    if token_id:
        token = next(
            (item for item in state.get("tokens") or [] if str(item.get("id")) == token_id),
            None,
        )
    data: dict[str, Any] = {"map": map_view or state, "can_manage": manager}
    if fog_view is not None:
        data["fog"] = fog_view
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


def _move_for_observer(
    move: dict[str, Any] | None,
    state: dict[str, Any],
    user_id: Any,
    manager: bool,
    visible: set[tuple[int, int]],
) -> dict[str, Any] | None:
    """移动提示按观察者裁剪：看不到的棋子动了，就不该收到提示。"""
    if not isinstance(move, dict) or not move:
        return None
    if manager or str(move.get("actor_user_id") or "") == str(user_id or ""):
        return move
    grid, offset_x, offset_y = _grid_of(state)
    token_id = str(move.get("token_id") or "")
    for token in state.get("tokens") or []:
        if str(token.get("id")) != token_id:
            continue
        if any(cell in visible for cell in _token_cells(token, grid, offset_x, offset_y)):
            return move
        return None
    return None


def _broadcast(room_id: str, state: dict[str, Any], move: dict[str, Any] | None = None) -> None:
    """按观察者裁剪后推送。

    迷雾关闭时没有需要隐藏的信息，沿用整房间广播；迷雾开启时逐个成员推到
    各自的 ``user:<id>`` 频道：KP 收完整地图与全队可见范围，玩家只收自己
    当前能看到的棋子、墙体、光源与视野掩码。
    """
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    room_dir, info = find_room(room_id)
    fog = state.get("fog") or {}
    if not info or not fog.get("enabled"):
        payload: dict[str, Any] = {"room_id": str(room_id), "map": state}
        if isinstance(move, dict) and move:
            payload["move"] = move
        try:
            socketio.emit("room_map_changed", payload, room=str(room_id))
        except Exception:  # noqa: BLE001 - 广播失败不能影响写入结果
            logger.warning("veil_map_broadcast_failed room=%s", room_id, exc_info=True)
        return

    delivered = 0
    seen: set[str] = set()
    for member in _active_members(info):
        user_id = str(member.get("user_id") or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        manager = _user_is_manager(info, user_id)
        view, fog_view, visible = _observer_scope(state, room_dir, user_id, manager)
        payload = {
            "room_id": str(room_id),
            "map": view,
            "fog": fog_view,
            "can_manage": manager,
        }
        filtered_move = _move_for_observer(move, state, user_id, manager, visible)
        if filtered_move:
            payload["move"] = filtered_move
        try:
            socketio.emit("room_map_changed", payload, room=f"user:{user_id}")
            delivered += 1
        except Exception:  # noqa: BLE001 - 单个成员失败不影响其他人
            logger.warning(
                "veil_map_broadcast_failed room=%s user=%s", room_id, user_id, exc_info=True
            )
    if delivered == 0:
        # 没有成员（或成员都没有 user_id）时退回房间广播，避免地图完全静默。
        payload = {"room_id": str(room_id), "map": state}
        if isinstance(move, dict) and move:
            payload["move"] = move
        try:
            socketio.emit("room_map_changed", payload, room=str(room_id))
        except Exception:  # noqa: BLE001
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
    """棋子所在格；优先格坐标，旧数据才回退到像素坐标换算。"""
    raw_x, raw_y = token.get("cell_x"), token.get("cell_y")
    if raw_x is not None and raw_y is not None:
        return _as_int(raw_x, 0), _as_int(raw_y, 0)
    x = _as_int(token.get("x"), 0)
    y = _as_int(token.get("y"), 0)
    return (
        int(math.floor((x - offset_x) / grid + 1e-9)),
        int(math.floor((y - offset_y) / grid + 1e-9)),
    )


# ---------------------------------------------------------------------------
# 视线、迷雾与按观察者裁剪
# ---------------------------------------------------------------------------

MAX_VISION_SOURCES = 8


def _vision_versions(state: dict[str, Any]) -> tuple[str, str]:
    """墙体 / 光源内容版本；没有任何变化时可见性缓存直接命中。"""
    return (
        visibility.state_version(state.get("walls") or []),
        visibility.state_version(state.get("lights") or []),
    )


def _scene_room_key(room_dir) -> str:
    try:
        return str(Path(room_dir).resolve())
    except OSError:
        return str(room_dir or "")


def _active_light_sources(state: dict[str, Any]) -> list[dict[str, Any]]:
    """场景光源 + 棋子自带光源，统一成同一种描述。

    场景光源是固定位置；棋子光源跟随棋子 footprint 中心。超过上限时按覆盖
    半径与亮度排序，只保留首要的若干个，避免一次请求做太多投影。
    """
    grid, offset_x, offset_y = _grid_of(state)
    sources: list[dict[str, Any]] = []
    for light in state.get("lights") or []:
        if not light.get("enabled", True):
            continue
        bright = _clamp(_as_int(light.get("bright_cells"), 0), 0, MAX_VISION_CELLS)
        dim = _clamp(
            _as_int(light.get("range_cells"), bright), bright, MAX_VISION_CELLS
        )
        if dim <= 0:
            continue
        sources.append(
            {
                "id": str(light.get("id") or ""),
                "x": _as_int(light.get("x"), 0),
                "y": _as_int(light.get("y"), 0),
                "bright_cells": bright,
                "dim_cells": dim,
                "color": str(light.get("color") or "#ffaa00"),
                "intensity": max(0.0, min(10.0, _fraction(light.get("intensity")))),
                "token_id": "",
                "follows_token": False,
            }
        )
    for token in state.get("tokens") or []:
        light = token.get("light")
        if not isinstance(light, dict) or not light.get("enabled"):
            continue
        bright = _clamp(_as_int(light.get("bright_cells"), 0), 0, MAX_VISION_CELLS)
        dim = _clamp(
            _as_int(light.get("dim_cells"), bright), bright, MAX_VISION_CELLS
        )
        if dim <= 0:
            continue
        cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
        width, height = _footprint(token)
        x, y = visibility.footprint_center(
            cell_x, cell_y, width, height, grid, (offset_x, offset_y)
        )
        sources.append(
            {
                "id": f"{token.get('id')}:light",
                "x": int(round(x)),
                "y": int(round(y)),
                "bright_cells": bright,
                "dim_cells": dim,
                "color": str(light.get("color") or "#ffaa00"),
                "intensity": max(
                    0.0, min(10.0, _fraction(light.get("intensity")))
                ),
                "token_id": str(token.get("id") or ""),
                "follows_token": True,
            }
        )
    sources.sort(
        key=lambda item: (
            item["dim_cells"] * 2 + item["bright_cells"],
            item["intensity"],
        ),
        reverse=True,
    )
    return sources[:MAX_ACTIVE_LIGHTS]


def _scene_lighting(
    state: dict[str, Any], room_dir
) -> tuple[set[tuple[int, int]], set[tuple[int, int]], list[dict[str, Any]]]:
    """场景光照：多个光源分别投影后取并集，明区与暗区分开返回。"""
    sources = _active_light_sources(state)
    if not sources:
        return set(), set(), []
    grid, offset_x, offset_y = _grid_of(state)
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    wall_version = visibility.state_version(state.get("walls") or [])
    # 光源与棋子位置都进入 light_version：任一光源变化会让投影缓存整体失效。
    light_version = visibility.state_version(sources)
    room_key = _scene_room_key(room_dir)
    bright: set[tuple[int, int]] = set()
    dim: set[tuple[int, int]] = set()
    for source in sources:
        result = visibility.lighting(
            (source["x"], source["y"]),
            walls=state.get("walls") or [],
            grid_size=grid,
            offset=(offset_x, offset_y),
            bounds_cells=bounds,
            bright_cells=source["bright_cells"],
            dim_cells=source["dim_cells"],
            wall_version=wall_version,
            light_version=light_version,
            room_key=room_key,
        )
        bright |= result["bright"]
        dim |= result["dim"]
    dim -= bright
    return bright, dim, sources


def _pc_tokens(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        token
        for token in state.get("tokens") or []
        if str(token.get("kind") or "") == "pc"
    ]


def _vision_tokens(
    state: dict[str, Any], user_id: Any, manager: bool
) -> list[dict[str, Any]]:
    """观察者来源：KP 用全队，玩家用自己绑定的棋子；没有绑定棋子时退回队伍视野。"""
    pcs = _pc_tokens(state)
    if manager:
        return pcs
    owned = [
        token
        for token in pcs
        if str(token.get("owner_user_id") or "") == str(user_id or "")
    ]
    return owned or pcs


def _vision_radius(token: dict[str, Any], fog: dict[str, Any]) -> int:
    radius = _as_int(token.get("vision_cells"), 0)
    if radius <= 0:
        radius = _as_int(fog.get("vision_cells"), DEFAULT_VISION_CELLS)
    return _clamp(radius, 1, MAX_VISION_CELLS)


def _vision_for(
    state: dict[str, Any], room_dir, tokens: list[dict[str, Any]]
) -> tuple[set[tuple[int, int]], list[list[tuple[float, float]]], list[str]]:
    """一组观察者棋子的可见格并集、可见多边形与来源 id。"""
    fog = state.get("fog") or {}
    if not fog.get("enabled"):
        return set(), [], []
    grid, offset_x, offset_y = _grid_of(state)
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    wall_version, light_version = _vision_versions(state)
    room_key = _scene_room_key(room_dir)
    cells: set[tuple[int, int]] = set()
    polygons: list[list[tuple[float, float]]] = []
    observers: list[str] = []
    for token in tokens[:MAX_VISION_SOURCES]:
        cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
        width, height = _footprint(token)
        origin = visibility.footprint_center(
            cell_x, cell_y, width, height, grid, (offset_x, offset_y)
        )
        result = visibility.visibility(
            origin,
            walls=state.get("walls") or [],
            grid_size=grid,
            offset=(offset_x, offset_y),
            bounds_cells=bounds,
            radius_cells=_vision_radius(token, fog),
            wall_version=wall_version,
            light_version=light_version,
            room_key=room_key,
        )
        cells |= result["cells"]
        polygons.append(result["polygon"])
        observers.append(str(token.get("id") or ""))
    return cells, polygons, observers


def _token_cells(
    token: dict[str, Any], grid: int, offset_x: int, offset_y: int
) -> list[tuple[int, int]]:
    cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
    width, height = _footprint(token)
    return [
        (cell_x + dx, cell_y + dy)
        for dx in range(width)
        for dy in range(height)
    ]


def _polyline_touches(
    points: Any, cells: set[tuple[int, int]], grid: int, offset_x: int, offset_y: int
) -> bool:
    """折线是否经过给定格集合（按半格采样，避免整段逐点求交）。"""
    if not cells or not isinstance(points, list) or len(points) < 2:
        return False
    step = max(1.0, grid * 0.5)
    for index in range(len(points) - 1):
        start, end = points[index], points[index + 1]
        try:
            ax, ay = float(start.get("x")), float(start.get("y"))
            bx, by = float(end.get("x")), float(end.get("y"))
        except (AttributeError, TypeError, ValueError):
            continue
        length = math.hypot(bx - ax, by - ay)
        count = max(1, int(math.ceil(length / step)))
        for sample in range(count + 1):
            ratio = sample / count
            x = ax + (bx - ax) * ratio
            y = ay + (by - ay) * ratio
            cell = (
                int(math.floor((x - offset_x) / grid + 1e-9)),
                int(math.floor((y - offset_y) / grid + 1e-9)),
            )
            if cell in cells:
                return True
    return False


def _fog_public(fog: dict[str, Any]) -> dict[str, Any]:
    """给客户端的迷雾元信息；格位掩码放在载荷顶层的 fog 字段里。"""
    return {
        "enabled": bool(fog.get("enabled")),
        "mode": str(fog.get("mode") or "static"),
        "vision_cells": _as_int(fog.get("vision_cells"), DEFAULT_VISION_CELLS),
        "bounds_cells": _as_int(fog.get("bounds_cells"), BOUNDS_DEFAULT),
        "grid_size": _as_int(fog.get("grid_size"), DEFAULT_GRID),
        "explored_count": _as_int(fog.get("explored_count"), 0),
    }


def _filter_map_for_observer(
    state: dict[str, Any],
    user_id: Any,
    visible: set[tuple[int, int]],
    known: set[tuple[int, int]],
) -> dict[str, Any]:
    """玩家只拿到自己该看到的棋子 / 墙体 / 光源子集。"""
    grid, offset_x, offset_y = _grid_of(state)
    view = dict(state)
    tokens: list[dict[str, Any]] = []
    for token in state.get("tokens") or []:
        owner = str(token.get("owner_user_id") or "")
        if owner and owner == str(user_id or ""):
            tokens.append(token)
            continue
        if any(cell in visible for cell in _token_cells(token, grid, offset_x, offset_y)):
            tokens.append(token)
    view["tokens"] = tokens
    view["walls"] = [
        wall
        for wall in state.get("walls") or []
        if _polyline_touches(wall.get("points"), known, grid, offset_x, offset_y)
    ]
    lights: list[dict[str, Any]] = []
    for light in state.get("lights") or []:
        cell = (
            int(math.floor((_as_int(light.get("x"), 0) - offset_x) / grid + 1e-9)),
            int(math.floor((_as_int(light.get("y"), 0) - offset_y) / grid + 1e-9)),
        )
        if cell in known:
            lights.append(light)
    view["lights"] = lights
    view["recent_requests"] = []
    view["fog"] = _fog_public(state.get("fog") or {})
    return view


def _observer_scope(
    state: dict[str, Any], room_dir, user_id: Any, manager: bool
) -> tuple[dict[str, Any], dict[str, Any], set[tuple[int, int]]]:
    """返回 ``(裁剪后的地图, 迷雾载荷, 当前可见格)``；KP 拿完整地图。"""
    fog = state.get("fog") or {}
    if not fog.get("enabled"):
        return state, {"enabled": False}, set()
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    grid, offset_x, offset_y = _grid_of(state)
    party_cells, _party_polygons, _party_observers = _vision_for(
        state, room_dir, _pc_tokens(state)
    )
    visible, polygons, observers = _vision_for(
        state, room_dir, _vision_tokens(state, user_id, manager)
    )
    explored = visibility.decode_mask(fog.get("explored"), bounds) | party_cells
    ambient = state.get("ambient_light") or {}
    bright_all, dim_all, light_sources = _scene_lighting(state, room_dir)
    if ambient.get("enabled"):
        # 环境光（UVTT 的全局照明）下，视野内都算明亮。
        bright = set(visible)
        dim: set[tuple[int, int]] = set()
        lighting_mode = "ambient"
    elif not light_sources:
        # 没有任何光源时退化为纯视线可见，避免整张图漆黑一片。
        bright = set(visible)
        dim = set()
        lighting_mode = "none"
    else:
        bright = visible & bright_all
        dim = (visible & dim_all) - bright
        lighting_mode = "sources"
    if manager:
        view = state
    else:
        known = visible | explored
        view = _filter_map_for_observer(state, user_id, visible, known)
    payload = {
        "enabled": True,
        "mode": str(fog.get("mode") or "static"),
        "viewer": "kp" if manager else "player",
        "bounds_cells": bounds,
        "half": bounds // 2,
        "grid_size": grid,
        "grid_offset": {"x": offset_x, "y": offset_y},
        "vision_cells": _as_int(fog.get("vision_cells"), DEFAULT_VISION_CELLS),
        "lighting": lighting_mode,
        "light_sources": len(light_sources),
        "bright": visibility.encode_mask(bright, bounds),
        "dim": visibility.encode_mask(dim, bounds),
        "explored": visibility.encode_mask(explored, bounds),
        "visible": visibility.encode_mask(visible, bounds),
        "explored_count": len(explored),
        "visible_count": len(visible),
        "observers": observers,
        "polygons": [
            [[round(point[0], 1), round(point[1], 1)] for point in polygon]
            for polygon in polygons[:4]
        ],
    }
    return view, payload, visible


def _update_explored(room_dir, state: dict[str, Any]) -> None:
    """把队伍当前可见格并入探索记录（只增不减）。"""
    fog = state.get("fog") or {}
    if not fog.get("enabled"):
        return
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    cells, _polygons, _observers = _vision_for(state, room_dir, _pc_tokens(state))
    if not cells:
        return
    explored = visibility.decode_mask(fog.get("explored"), bounds) | cells
    encoded = visibility.encode_mask(explored, bounds)
    if encoded != fog.get("explored"):
        fog["explored"] = encoded
        fog["explored_count"] = len(explored)


def _user_is_manager(info: dict[str, Any], user_id: Any) -> bool:
    target = str(user_id or "")
    if not target:
        return False
    if target == str(info.get("creator_id") or ""):
        return True
    for member in _active_members(info):
        if str(member.get("user_id") or "") != target:
            continue
        return str(member.get("room_role") or "").strip().lower() in _OWNER_ROLES
    return False


def _payload_cell(
    payload: Any, grid: int, offset_x: int, offset_y: int
) -> tuple[int, int, float, float] | None:
    """解析目标位置：优先 ``cell_x/cell_y``，兼容旧的像素 ``x/y``。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("cell_x") is not None or payload.get("cell_y") is not None:
        try:
            cell_x = int(round(float(payload.get("cell_x"))))
            cell_y = int(round(float(payload.get("cell_y"))))
        except (TypeError, ValueError):
            return None
        fraction_x = _fraction(payload.get("offset_x"))
        fraction_y = _fraction(payload.get("offset_y"))
        cell_x, fraction_x = _carry_fraction(cell_x, fraction_x)
        cell_y, fraction_y = _carry_fraction(cell_y, fraction_y)
        return cell_x, cell_y, round(fraction_x, 6), round(fraction_y, 6)
    x = _snap(payload.get("x"), grid, offset_x)
    y = _snap(payload.get("y"), grid, offset_y)
    if x is None or y is None:
        return None
    exact_x = (x - offset_x) / grid
    exact_y = (y - offset_y) / grid
    cell_x = int(math.floor(exact_x + 1e-9))
    cell_y = int(math.floor(exact_y + 1e-9))
    return cell_x, cell_y, round(exact_x - cell_x, 6), round(exact_y - cell_y, 6)


def _set_token_cell(
    token: dict[str, Any],
    cell_x: int,
    cell_y: int,
    fraction_x: float,
    fraction_y: float,
    grid: int,
    offset_x: int,
    offset_y: int,
) -> None:
    cell_x, fraction_x = _carry_fraction(cell_x, fraction_x)
    cell_y, fraction_y = _carry_fraction(cell_y, fraction_y)
    token["cell_x"] = int(cell_x)
    token["cell_y"] = int(cell_y)
    token["offset_x"] = round(fraction_x, 6)
    token["offset_y"] = round(fraction_y, 6)
    token["x"] = int(round(offset_x + (cell_x + token["offset_x"]) * grid))
    token["y"] = int(round(offset_y + (cell_y + token["offset_y"]) * grid))


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


_MOVE_INSET_PX = 1.0


def _footprint_points(
    state: dict[str, Any], cell_x: int, cell_y: int, width: int, height: int
) -> list[tuple[float, float]]:
    """棋子 footprint 四角（世界像素）；向内收 1px，避免贴墙格把边线当成穿越。"""
    grid, offset_x, offset_y = _grid_of(state)
    left = offset_x + int(cell_x) * grid + _MOVE_INSET_PX
    top = offset_y + int(cell_y) * grid + _MOVE_INSET_PX
    right = offset_x + (int(cell_x) + width) * grid - _MOVE_INSET_PX
    bottom = offset_y + (int(cell_y) + height) * grid - _MOVE_INSET_PX
    right = max(left, right)
    bottom = max(top, bottom)
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def _movement_wall_blockers(
    state: dict[str, Any],
    token: dict[str, Any],
    from_cell: tuple[int, int],
    to_cell: tuple[int, int],
) -> list[str]:
    """返回移动路径穿过的墙体 id；门 open 时放行，窗户按 blocks_movement 判定。"""
    walls = state.get("walls")
    if not isinstance(walls, list) or not walls:
        return []
    if (int(from_cell[0]), int(from_cell[1])) == (int(to_cell[0]), int(to_cell[1])):
        return []
    width, height = _footprint(token)
    starts = _footprint_points(state, from_cell[0], from_cell[1], width, height)
    ends = _footprint_points(state, to_cell[0], to_cell[1], width, height)
    paths = list(zip(starts, ends))
    if width > 1 or height > 1:
        # 多格棋子再检查四条边的中点，覆盖四角轨迹之间的中段。
        for index in range(4):
            start_a, start_b = starts[index], starts[(index + 1) % 4]
            end_a, end_b = ends[index], ends[(index + 1) % 4]
            paths.append(
                (
                    ((start_a[0] + start_b[0]) / 2.0, (start_a[1] + start_b[1]) / 2.0),
                    ((end_a[0] + end_b[0]) / 2.0, (end_a[1] + end_b[1]) / 2.0),
                )
            )
    blockers: list[str] = []
    for start, end in paths:
        if abs(end[0] - start[0]) < 1e-6 and abs(end[1] - start[1]) < 1e-6:
            continue
        for wall_id in spatial.segment_blockers(walls, start, end, mode="movement"):
            if wall_id not in blockers:
                blockers.append(wall_id)
    return blockers


def _movement_block_message(state: dict[str, Any], blockers: list[str]) -> str:
    blocked_ids = {str(item) for item in blockers}
    kinds = {
        str(wall.get("kind") or "wall").strip().lower()
        for wall in state.get("walls") or []
        if isinstance(wall, dict) and str(wall.get("id") or "") in blocked_ids
    }
    if "door" in kinds and "window" not in kinds:
        return "移动路径被未打开的门挡住，请先开门或绕行。"
    if "window" in kinds and "door" not in kinds:
        return "移动路径被窗户挡住，无法穿过。"
    return "移动路径被墙体挡住，请绕行。"


def _move_target_rejection(
    state: dict[str, Any], token: dict[str, Any], cell_x: int, cell_y: int
) -> tuple[str, str] | None:
    """移动入口共用的目标格校验：边界、占位与墙体穿越；被挡住时不改坐标与预算。"""
    if not _within_bounds(state, token, cell_x, cell_y):
        bounds = _clamp(
            _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
        )
        return "map.out_of_bounds", f"目标位置超出地图边界（当前地图 {bounds} 格）。"
    occupied = _occupancy_conflict(state, token, cell_x, cell_y)
    if occupied:
        return "map.cell_occupied", f"目标格已被「{occupied}」占用。"
    grid, offset_x, offset_y = _grid_of(state)
    from_cell = _cell_of(token, grid, offset_x, offset_y)
    blockers = _movement_wall_blockers(state, token, from_cell, (cell_x, cell_y))
    if blockers:
        return "map.wall_blocked", _movement_block_message(state, blockers)
    return None


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
    target_cell_x: int,
    target_cell_y: int,
) -> tuple[bool, str, str]:
    """校验并扣除移动力；距离按格坐标计算。"""
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
    cells = max(
        abs(int(target_cell_x) - from_col),
        abs(int(target_cell_y) - from_row),
    )
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
    user_id = str(session.get("user_id") or "")
    view, fog_payload, _visible = _observer_scope(state, room_dir, user_id, manager)
    return {
        "map": view,
        "fog": fog_payload,
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

        reset_fog = False
        if "grid_size" in payload:
            grid = _as_int(payload.get("grid_size"), state["grid_size"])
            if grid < MIN_GRID or grid > MAX_GRID:
                return _invalid_request(
                    f"网格尺寸必须在 {MIN_GRID}–{MAX_GRID} 之间。",
                    "map.invalid_grid",
                    request_id=request_id,
                    revision=state["revision"],
                )
            if grid != state["grid_size"]:
                reset_fog = True
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
            if bounds != state["bounds_cells"]:
                reset_fog = True
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
            if background != state.get("background"):
                state["background_meta"] = {}
                state["calibration"] = _default_calibration()
                state["grid_offset"] = {"x": 0, "y": 0}
                reset_fog = True
            state["background"] = background
        if "fog_enabled" in payload:
            # 关掉迷雾时保留已探索掩码，重新打开可以接着用。
            state["fog"]["enabled"] = _as_bool(payload.get("fog_enabled"), state["fog"]["enabled"])
        if "fog_vision_cells" in payload:
            vision = _as_int(payload.get("fog_vision_cells"), state["fog"]["vision_cells"])
            if vision < 1 or vision > MAX_VISION_CELLS:
                return _invalid_request(
                    f"视野半径必须在 1–{MAX_VISION_CELLS} 格之间。",
                    "map.invalid_vision",
                    request_id=request_id,
                    revision=state["revision"],
                )
            state["fog"]["vision_cells"] = vision
        if reset_fog:
            # 网格 / 边界 / 背景变了，旧格位掩码失去意义，从当前位置重新探索。
            state["fog"]["explored"] = ""
            state["fog"]["explored_count"] = 0
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "map.update"},
        )
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "地图设置已保存。",
        request_id=request_id,
        revision=state["revision"],
        code="map.updated",
        data={"map": state, "fog": fog_payload, "can_manage": True},
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
        target = _payload_cell(
            {
                "cell_x": payload.get("cell_x"),
                "cell_y": payload.get("cell_y"),
                "offset_x": payload.get("offset_x"),
                "offset_y": payload.get("offset_y"),
                "x": payload.get("x", 0),
                "y": payload.get("y", 0),
            },
            grid,
            offset_x,
            offset_y,
        )
        if target is None:
            return _invalid_request(
                "棋子坐标不合法。",
                "map.invalid_coordinates",
                request_id=request_id,
                revision=state["revision"],
            )
        cell_x, cell_y, fraction_x, fraction_y = target
        color = str(payload.get("color") or "").strip()
        if not _HEX_COLOR.match(color):
            color = TOKEN_COLORS[len(state["tokens"]) % len(TOKEN_COLORS)]
        token = {
            "id": f"token-{uuid4().hex[:10]}",
            "name": name,
            "cell_x": cell_x,
            "cell_y": cell_y,
            "offset_x": fraction_x,
            "offset_y": fraction_y,
            "w": _clamp(_as_int(payload.get("w"), 1), 1, 4),
            "h": _clamp(_as_int(payload.get("h"), 1), 1, 4),
            "color": color,
            "owner_user_id": owner_user_id,
            "kind": "pc" if owner_user_id else "npc",
            "combatant": name,
        }
        _set_token_cell(token, cell_x, cell_y, fraction_x, fraction_y, grid, offset_x, offset_y)
        state["tokens"].append(token)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.add", "token_id": token["id"]},
        )
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已添加。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_added",
        data={"map": state, "fog": fog_payload, "token": token, "can_manage": True},
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
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已删除。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_deleted",
        data={"map": state, "fog": fog_payload, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/tokens/<token_id>/move", methods=["POST"])
def move_token(room_id: str, token_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    viewer_id = str(session.get("user_id") or "")
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
        if not manager and owner_user_id != viewer_id:
            return _rejected_request(
                "你只能移动自己绑定的棋子。",
                "map.not_owner",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        view, fog_view, _visible = _observer_scope(state, room_dir, viewer_id, manager)
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(
                state, manager, record, map_view=view, fog_view=fog_view
            )
        if expected != state["revision"]:
            return _conflict_request(
                state, manager, request_id, map_view=view, fog_view=fog_view
            )

        grid, offset_x, offset_y = _grid_of(state)
        target = _payload_cell(payload, grid, offset_x, offset_y)
        if target is None:
            return _invalid_request(
                "棋子坐标不合法。",
                "map.invalid_coordinates",
                request_id=request_id,
                revision=state["revision"],
            )
        cell_x, cell_y, fraction_x, fraction_y = target
        rejection = _move_target_rejection(state, token, cell_x, cell_y)
        if rejection is not None:
            code, message = rejection
            return _rejected_request(
                message,
                code,
                request_id=request_id,
                revision=state["revision"],
                data={"map": view, "fog": fog_view, "can_manage": manager},
            )
        ok, code, message = _apply_combat_cost(room_id, room_dir, state, token, cell_x, cell_y)
        if not ok:
            return _rejected_request(
                message,
                code,
                request_id=request_id,
                revision=state["revision"],
                http_status=409,
                data={"map": view, "fog": fog_view, "can_manage": manager},
            )
        _set_token_cell(token, cell_x, cell_y, fraction_x, fraction_y, grid, offset_x, offset_y)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.move", "token_id": token["id"]},
        )
        view, fog_view, _visible = _observer_scope(state, room_dir, viewer_id, manager)
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子已移动。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_moved",
        data={
            "map": view,
            "fog": fog_view,
            "token": token,
            "combat": _combat_payload(room_dir),
            "can_manage": manager,
        },
    )


def _map_assets_dir(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "veil-map" / "assets" / "maps"


def _wrap_grid_offset(value: float, grid: int) -> int:
    """把世界像素偏移折回 ``[-grid/2, grid/2)``，避免存大数。"""
    grid = max(1, int(grid))
    wrapped = (int(round(value)) + grid // 2) % grid
    return wrapped - grid // 2


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
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "墙体已保存。",
        request_id=request_id,
        revision=state["revision"],
        code="map.walls_updated",
        data={"map": state, "fog": fog_payload, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/lights", methods=["PUT"])
def update_lights(room_id: str):
    """KP 保存场景光源（含开关）。棋子自带光源走 token 级接口。"""
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以编辑场景光源。",
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
        raw_lights = payload.get("lights")
        if not isinstance(raw_lights, list):
            return _invalid_request(
                "lights 必须是光源数组。",
                "map.invalid_lights",
                request_id=request_id,
                revision=state["revision"],
            )
        if len(raw_lights) > MAX_LIGHTS:
            return _invalid_request(
                f"光源数量不能超过 {MAX_LIGHTS} 个。",
                "map.too_many_lights",
                request_id=request_id,
                revision=state["revision"],
            )
        state["lights"] = _normalize_lights(raw_lights)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "map.lights"},
        )
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "光源已保存。",
        request_id=request_id,
        revision=state["revision"],
        code="map.lights_updated",
        data={"map": state, "fog": fog_payload, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/tokens/<token_id>/light", methods=["PUT"])
def update_token_light(room_id: str, token_id: str):
    """给棋子加 / 去掉自带光源；房主可操作任意棋子，玩家只能动自己的。"""
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    viewer_id = str(session.get("user_id") or "")
    with _MAP_LOCK:
        state = _load_map(room_dir)
        token = next(
            (item for item in state["tokens"] if str(item.get("id")) == str(token_id)),
            None,
        )
        if token is None:
            return _invalid_request(
                "棋子不存在。",
                "map.token_not_found",
                revision=state["revision"],
                http_status=404,
            )
        if not manager and str(token.get("owner_user_id") or "") != viewer_id:
            return _rejected_request(
                "你只能操作自己绑定的棋子上的光源。",
                "map.not_owner",
                revision=state["revision"],
            )
        request_id, expected, error = _read_envelope(payload, state)
        if error is not None:
            return error
        view, fog_view, _visible = _observer_scope(
            state, room_dir, viewer_id, manager
        )
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(
                state, manager, record, map_view=view, fog_view=fog_view
            )
        if expected != state["revision"]:
            return _conflict_request(
                state, manager, request_id, map_view=view, fog_view=fog_view
            )
        raw_light = payload.get("light")
        if payload.get("remove") is True or raw_light is None:
            token["light"] = None
        else:
            light = _normalize_token_light(raw_light)
            if light is None:
                return _invalid_request(
                    "光源参数不合法：需要明亮半径或昏暗半径。",
                    "map.invalid_light",
                    request_id=request_id,
                    revision=state["revision"],
                )
            token["light"] = light
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.light", "token_id": token_id},
        )
        view, fog_view, _visible = _observer_scope(
            state, room_dir, viewer_id, manager
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        "棋子光源已更新。",
        request_id=request_id,
        revision=state["revision"],
        code="map.token_light_updated",
        data={
            "map": view,
            "fog": fog_view,
            "token": token,
            "can_manage": manager,
        },
    )


@bp.route("/rooms/<room_id>/map/import", methods=["POST"])
def import_map(room_id: str):
    """导入外部地图：裸图片、Universal VTT 或 Foundry 场景 JSON。

    整个文件先解析成内存里的导入包，所有校验通过后才写图片与地图状态；任何
    一步失败都不会留下半个场景。结构化格式会替换墙体、门与光源，棋子坐标不动。
    """
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    if not manager:
        return _rejected_request("只有房主或房管可以导入地图。", "map.manager_only")

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _invalid_request("请选择要导入的地图文件。", "map.import_missing")
    raw = uploaded.read(importer.MAX_IMPORT_BYTES + 1)
    try:
        bundle = importer.read_import(raw, uploaded.filename, media)
    except importer.ImportError as exc:
        return _invalid_request(exc.message, exc.code, http_status=exc.status)
    except media.ImageError as exc:
        return _invalid_request(exc.message, exc.code, http_status=413 if "too_large" in exc.code else 400)

    image = bundle["image"]
    structured = bundle["format"] != importer.FORMAT_IMAGE
    warnings = list(bundle.get("warnings") or [])
    needs_review = bool(bundle.get("needs_review"))
    walls = bundle.get("walls")
    lights = bundle.get("lights")
    ambient = bundle.get("ambient_light")
    doors = sum(1 for wall in walls or [] if str(wall.get("kind")) in {"door", "window"})

    form = dict(request.form)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        request_id, expected, error = _read_envelope(form, state)
        if error is not None:
            return error
        record = _duplicate_record(state, request_id)
        if record is not None:
            return _duplicate_receipt(state, manager, record)
        if expected != state["revision"]:
            return _conflict_request(state, manager, request_id)

        if structured:
            grid = int(bundle["grid"]["size"])
            origin_x = float(bundle["grid"].get("origin_x") or 0.0)
            origin_y = float(bundle["grid"].get("origin_y") or 0.0)
            grid_offset = {
                "x": _wrap_grid_offset(origin_x - image["width"] / 2.0, grid),
                "y": _wrap_grid_offset(origin_y - image["height"] / 2.0, grid),
            }
            offset_x = int(round(origin_x)) % grid
            offset_y = int(round(origin_y)) % grid
        else:
            # 裸图片不改变当前网格；格线先回到图片中心对齐，等用户确认标定。
            grid = int(state["grid_size"])
            grid_offset = {"x": 0, "y": 0}
            offset_x = 0
            offset_y = 0
        cells_across = max(1, int(round(image["width"] / max(1, grid))))
        cells_down = max(1, int(round(image["height"] / max(1, grid))))

        stored_name = f"{uuid4().hex}.{image['suffix']}"
        asset_path = _map_assets_dir(room_dir) / stored_name
        display_name = Path(str(uploaded.filename)).name[:120] or f"地图 {importer.FORMAT_LABELS[bundle['format']]}"
        try:
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(image["bytes"])

            state["background"] = (
                f"/api/plugins/veil-map/rooms/{room_id}/assets/maps/{stored_name}"
            )
            state["background_meta"] = {
                "kind": bundle["format"],
                "format": bundle["format"],
                "name": display_name,
                "url": state["background"],
                "width": image["width"],
                "height": image["height"],
                "bytes": image["size"],
                "uploaded_at": _now(),
                "import": {
                    "format": bundle["format"],
                    "walls": len(walls or []),
                    "doors": doors,
                    "lights": len(lights or []),
                    "needs_review": needs_review,
                    "warnings": warnings,
                },
            }
            state["grid_size"] = grid
            state["grid_offset"] = grid_offset
            state["calibration"] = {
                "cells_across": cells_across,
                "cells_down": cells_down,
                "offset_x": offset_x,
                "offset_y": offset_y,
            }
            if walls is not None:
                state["walls"] = _normalize_walls(walls)
            if lights is not None:
                state["lights"] = _normalize_lights(lights)
            if ambient is not None:
                state["ambient_light"] = _normalize_ambient(ambient)
            state = _save_map(
                room_dir,
                state,
                {"request_id": request_id, "action": "map.import"},
            )
        except Exception:  # noqa: BLE001 - 导入失败必须回滚已落盘的图片
            asset_path.unlink(missing_ok=True)
            logger.exception("veil_map_import_failed room=%s", room_id)
            return _receipt(
                "invalid",
                "地图导入失败，原场景未被修改，请稍后重试。",
                request_id=request_id,
                revision=state["revision"],
                code="map.import_failed",
                http_status=500,
            )
        # 视线的几何计算放在落盘之后：即使计算失败，也不会误删已提交的地图。
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    summary = {
        "format": bundle["format"],
        "label": bundle["label"],
        "walls": len(walls or []),
        "doors": doors,
        "lights": len(lights or []),
        "warnings": warnings,
        "needs_review": needs_review,
    }
    if structured:
        message = (
            f"已导入{bundle['label']}：墙体 {summary['walls']} 段、门/窗 {doors} 个、"
            f"光源 {summary['lights']} 个。"
        )
        if needs_review or warnings:
            message += " 有需要复核的网格提示，请检查标定。"
        else:
            message += " 请确认网格标定。"
    else:
        message = "地图已导入，请确认网格标定。"
    return _receipt(
        "success",
        message,
        request_id=request_id,
        revision=state["revision"],
        code="map.imported",
        data={
            "map": state,
            "fog": fog_payload,
            "can_manage": True,
            "import": summary,
            "background": {
                "url": state["background"],
                "width": image["width"],
                "height": image["height"],
                "name": state["background_meta"]["name"],
                "grid_size": grid,
                "cells_across": cells_across,
                "cells_down": cells_down,
            },
            "calibration_hint": {
                "cells_across": cells_across,
                "cells_down": cells_down,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "needs_review": needs_review,
                "warnings": warnings,
            },
        },
    )


@bp.route("/rooms/<room_id>/map/calibration", methods=["PUT"])
def calibrate_map(room_id: str):
    """保存图片像素 → 格数 → 格线偏移的标定结果。"""
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with _MAP_LOCK:
        state = _load_map(room_dir)
        if not manager:
            return _rejected_request(
                "只有房主或房管可以标定地图网格。",
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

        background = state.get("background_meta") or {}
        width = _as_int(background.get("width"), 0)
        height = _as_int(background.get("height"), 0)
        if width <= 0 or height <= 0:
            return _invalid_request(
                "当前地图不是导入的图片，无法按像素标定；请先导入地图图片。",
                "map.no_background_image",
                request_id=request_id,
                revision=state["revision"],
            )
        cells_across = _as_int(payload.get("cells_across"), 0)
        if cells_across < 1 or cells_across > 400:
            return _invalid_request(
                "横向格数必须在 1–400 之间。",
                "map.invalid_cells_across",
                request_id=request_id,
                revision=state["revision"],
            )
        grid = int(round(width / cells_across))
        if grid < MIN_GRID:
            return _invalid_request(
                f"按 {cells_across} 格切分后每格只有 {grid}px，低于最小值 {MIN_GRID}px；请减少格数。",
                "map.grid_too_small",
                request_id=request_id,
                revision=state["revision"],
            )
        if grid > MAX_GRID:
            return _invalid_request(
                f"按 {cells_across} 格切分后每格 {grid}px，超过最大值 {MAX_GRID}px；请增加格数。",
                "map.grid_too_large",
                request_id=request_id,
                revision=state["revision"],
            )
        cells_down = _as_int(payload.get("cells_down"), max(1, int(round(height / grid))))
        if cells_down < 1 or cells_down > 400:
            return _invalid_request(
                "纵向格数必须在 1–400 之间。",
                "map.invalid_cells_down",
                request_id=request_id,
                revision=state["revision"],
            )
        offset_x = _as_int(payload.get("offset_x"), 0)
        offset_y = _as_int(payload.get("offset_y"), 0)
        if not 0 <= offset_x < grid or not 0 <= offset_y < grid:
            return _invalid_request(
                f"格线偏移必须在 0–{grid - 1}px 之间。",
                "map.invalid_grid_offset",
                request_id=request_id,
                revision=state["revision"],
            )
        state["grid_size"] = grid
        state["grid_offset"] = {
            "x": _wrap_grid_offset(offset_x - width / 2.0, grid),
            "y": _wrap_grid_offset(offset_y - height / 2.0, grid),
        }
        state["calibration"] = {
            "cells_across": cells_across,
            "cells_down": cells_down,
            "offset_x": offset_x,
            "offset_y": offset_y,
        }
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "map.calibration"},
        )
        _view, fog_payload, _visible = _observer_scope(
            state, room_dir, str(session.get("user_id") or ""), True
        )
    _broadcast(room_id, state)
    return _receipt(
        "success",
        f"网格标定已保存：{cells_across} × {cells_down} 格，每格 {grid}px。",
        request_id=request_id,
        revision=state["revision"],
        code="map.calibrated",
        data={"map": state, "fog": fog_payload, "can_manage": True},
    )


@bp.route("/rooms/<room_id>/assets/maps/<path:filename>")
def serve_map_asset(room_id: str, filename: str):
    room_dir, _info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    response = send_from_directory(_map_assets_dir(room_dir), filename)
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


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
        try:
            to_cell_x = int(cell_x)
            to_cell_y = int(cell_y)
        except (TypeError, ValueError):
            return {"ok": False, "error": "目标格坐标不合法。"}
        from_cell_x, from_cell_y = _cell_of(token, grid, offset_x, offset_y)

        rejection = _move_target_rejection(state, token, to_cell_x, to_cell_y)
        if rejection is not None:
            code, message = rejection
            return {"ok": False, "error": message, "code": code}

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
            room_id, room_dir, state, token, to_cell_x, to_cell_y
        )
        if not ok:
            return {"ok": False, "error": message or "移动被战斗规则拒绝。", "code": code}

        distance = max(abs(to_cell_x - from_cell_x), abs(to_cell_y - from_cell_y))
        cost_ft = distance * FEET_PER_CELL if combat_active else 0
        _set_token_cell(token, to_cell_x, to_cell_y, 0.0, 0.0, grid, offset_x, offset_y)
        state = _save_map(room_dir, state)

    move: dict[str, Any] = {
        "source": "ai",
        "actor": _agent_actor_label(info, agent_id, user_id),
        "actor_user_id": str(user_id or ""),
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


def _action_line_guard(payload: dict[str, Any]) -> list[str]:
    """给服务端行动工作流提供墙体/视线校验。

    只看同时有地图棋子的施法者与目标；找不到棋子时不做臆测，交给规则集
    自己的射程校验。门开着按 ``wall_blocks`` 的既有语义放行。
    """
    room_dir = payload.get("room_dir")
    walls = []
    tokens = []
    grid, offset_x, offset_y = DEFAULT_GRID, 0, 0
    try:
        state, _source = scene_store.load(room_dir)
        walls = state.get("walls") or []
        tokens = [token for token in state.get("tokens") or [] if isinstance(token, dict)]
        grid, offset_x, offset_y = _grid_of(state)
    except Exception:  # noqa: BLE001 - 没有地图时不影响规则结算
        return []
    if not walls or not tokens:
        return []

    def find_token(name: Any) -> dict[str, Any] | None:
        wanted = str(name or "").strip().casefold()
        if not wanted:
            return None
        for token in tokens:
            for key in ("name", "combatant", "label"):
                if str(token.get(key) or "").strip().casefold() == wanted:
                    return token
        return None

    def center(token: dict[str, Any]) -> tuple[float, float] | None:
        try:
            cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
            width, height = _footprint(token)
            return visibility.footprint_center(
                cell_x, cell_y, width, height, grid, (offset_x, offset_y)
            )
        except Exception:  # noqa: BLE001
            return None

    actor_token = find_token(payload.get("actor"))
    if actor_token is None:
        return []
    actor_point = center(actor_token)
    if actor_point is None:
        return []
    actor_name = str(actor_token.get("name") or payload.get("actor") or "施法者")
    errors: list[str] = []
    for target_name in payload.get("targets") or []:
        target_token = find_token(target_name)
        if target_token is None or target_token is actor_token:
            continue
        target_point = center(target_token)
        if target_point is None:
            continue
        blockers = spatial.segment_blockers(
            walls, actor_point, target_point, mode="sight"
        )
        if blockers:
            errors.append(f"{actor_name} 与 {target_name} 之间被墙体或关闭的门阻挡")
    return errors


_ACTION_GUARD_REGISTERED = False


def _register_action_guard() -> None:
    global _ACTION_GUARD_REGISTERED
    if _ACTION_GUARD_REGISTERED:
        return
    try:
        from trpg_server.workflows.guards import register_action_guard
    except Exception:  # noqa: BLE001 - 旧核心没有工作流时静默跳过
        return
    register_action_guard(_action_line_guard)
    _ACTION_GUARD_REGISTERED = True


def create_tools() -> list[AgentTool]:
    _register_action_guard()
    return [MOVE_TOKEN_TOOL]


__all__ = ["bp", "create_tools", "MOVE_TOKEN_TOOL"]

