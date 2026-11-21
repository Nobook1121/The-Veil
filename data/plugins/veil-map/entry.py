"""地图插件（veil-map）的服务端接口。

核心只提供画布宿主与事件通道；棋子坐标、网格吸附、多格占位、地图基础
编辑与战斗回合联动都在这里落地。状态写在房间自己的插件命名空间：

    <ROOMS_DIR>/<room_id>/plugins/veil-map/map.json

战斗行动经济直接复用 ``trpg_server.combat.scheduler.spend_budget``，不另写
一套移动力判定；越权与预算不足都返回可读中文消息，由前端提示。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, send_from_directory, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.combat.scheduler import spend_budget
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_clock import room_lock
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
# 房间状态统一用 ``room_clock.room_lock(room_dir)``：它同时覆盖地图、战斗预算
# 与 info.json 的读改写，并与房间时钟/法术模块共用同一把跨进程锁。锁定顺序
# 固定为「角色卡锁 → 房间锁」；持有房间锁时不再获取角色卡锁。
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
MAX_FOG_OBSERVERS = 256
_FOG_SCOPES = {"observer", "party"}
_OBSERVER_KEY = re.compile(r"^(?:user|token):[A-Za-z0-9._:@-]{1,96}$")


def _default_fog() -> dict[str, Any]:
    """静态迷雾：探索记录 + 观察者当前可见。

    探索记录有两种范围（``scope``）：

    - ``observer``（默认）：每名观察者分别记录自己探索过的区域，玩家之间
      存在信息差；没有绑定棋子的玩家退回队伍视角。
    - ``party``：队伍共享一份探索记录，所有玩家看到同一片已探索区域。

    ``explored`` 保存共享基线（旧数据迁移来源，也是 ``party`` 范围的累计
    记录）；``explored_by`` 按观察者保存**增量**：只存基线之外新探索到的
    格，用线性下标的稀疏文本表示，不写一人一份整图掩码。

    动态光源投影做的是静态投影（光源位置、明暗半径、墙体遮挡），昼夜循环
    和天气没有实现。迷雾默认开启，房主可在面板里关掉；关掉后服务端退回
    整图推送（没有任何需要隐藏的信息）。
    """
    return {
        "enabled": True,
        "mode": "static",
        "scope": "observer",
        "vision_cells": DEFAULT_VISION_CELLS,
        "explored": "",
        "explored_count": 0,
        "explored_by": {},
        "bounds_cells": BOUNDS_DEFAULT,
        "grid_size": DEFAULT_GRID,
    }


def _cells_text(cells: Any, bounds_cells: int) -> str:
    """格集合 → 稀疏线性下标文本（升序、连续段写成 ``起-止``）。

    只记录实际探索到的格，连续段压成一个区间：常见视野圆盘每行一段，
    比逐个坐标或整图掩码都小。越界格丢弃。
    """
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    indices: set[int] = set()
    for item in cells or []:
        try:
            cell_x, cell_y = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (-half <= cell_x < bounds - half and -half <= cell_y < bounds - half):
            continue
        indices.add((cell_y + half) * bounds + (cell_x + half))
    ordered = sorted(indices)
    if not ordered:
        return ""
    parts: list[str] = []
    start = prev = ordered[0]
    for index in ordered[1:]:
        if index == prev + 1:
            prev = index
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = index
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _parse_cells_text(text: Any, bounds_cells: int) -> set[tuple[int, int]]:
    """稀疏线性下标文本 → 格集合；支持 ``起-止`` 区间，非法条目跳过。"""
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    total = bounds * bounds
    cells: set[tuple[int, int]] = set()
    for chunk in str(text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            left, _sep, right = chunk.partition("-")
            try:
                start, end = int(left), int(right)
            except (TypeError, ValueError):
                continue
            if start > end:
                start, end = end, start
            for index in range(max(0, start), min(total - 1, end) + 1):
                cells.add((index % bounds - half, index // bounds - half))
            continue
        try:
            index = int(chunk)
        except (TypeError, ValueError):
            continue
        if 0 <= index < total:
            cells.add((index % bounds - half, index // bounds - half))
    return cells


def _fog_records(fog: dict[str, Any], bounds_cells: int) -> dict[str, set[tuple[int, int]]]:
    """解码所有观察者增量记录；空记录不返回。"""
    records: dict[str, set[tuple[int, int]]] = {}
    raw = fog.get("explored_by")
    for key, value in (raw if isinstance(raw, dict) else {}).items():
        payload = value if isinstance(value, dict) else {}
        cells = _parse_cells_text(payload.get("cells"), bounds_cells)
        if cells:
            records[str(key)] = cells
    return records


def _observer_key(token: dict[str, Any]) -> str:
    """观察者键：优先绑定玩家，未绑定棋子时退回棋子 id。"""
    owner = str(token.get("owner_user_id") or "").strip()
    if owner:
        return f"user:{owner}"
    token_id = str(token.get("id") or "").strip()
    return f"token:{token_id}" if token_id else ""


def _normalize_fog(raw: Any, bounds_cells: int, grid_size: int) -> dict[str, Any]:
    """迷雾掩码只在同一网格/边界下有效；标定变了就清空探索记录。

    旧数据只有共享 ``explored`` 掩码：归一化后 ``scope`` 取默认 ``observer``，
    共享掩码作为基线保留，所有观察者在它之上继续记录各自增量——迁移后每个
    人看到的仍是原来那份共享探索记录。
    """
    fog = _default_fog()
    fog["bounds_cells"] = int(bounds_cells)
    fog["grid_size"] = int(grid_size)
    if not isinstance(raw, dict):
        return fog
    fog["enabled"] = bool(raw.get("enabled", fog["enabled"]))
    fog["scope"] = str(raw.get("scope") or "").strip().lower()
    if fog["scope"] not in _FOG_SCOPES:
        fog["scope"] = "observer"
    fog["vision_cells"] = _clamp(
        _as_int(raw.get("vision_cells"), fog["vision_cells"]), 1, MAX_VISION_CELLS
    )
    text = str(raw.get("explored") or "")
    same_space = (
        _as_int(raw.get("bounds_cells"), 0) == int(bounds_cells)
        and _as_int(raw.get("grid_size"), 0) == int(grid_size)
    )
    if not same_space:
        return fog
    if text:
        fog["explored"] = text
        fog["explored_count"] = max(0, _as_int(raw.get("explored_count"), 0))
    records = raw.get("explored_by")
    if isinstance(records, dict):
        for key, value in list(records.items())[:MAX_FOG_OBSERVERS]:
            key = str(key or "").strip()
            if not _OBSERVER_KEY.match(key):
                continue
            payload = value if isinstance(value, dict) else {}
            cells = _parse_cells_text(payload.get("cells"), bounds_cells)
            if cells:
                fog["explored_by"][key] = {"cells": _cells_text(cells, bounds_cells)}
    return fog


def _default_map() -> dict[str, Any]:
    return {
        "version": MAP_VERSION,
        "revision": 0,
        "scene_id": "default",
        "title": "主场景",
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


def _normalize_token_combat(raw: Any) -> dict[str, Any] | None:
    """棋子上的战斗快照（HP / 先攻 / 当前行动者）；非战斗棋子为 ``None``。"""
    if not isinstance(raw, dict):
        return None
    combat_id = str(raw.get("combat_id") or "").strip()[:64]
    combatant_id = str(raw.get("combatant_id") or "").strip()[:96]
    if not combat_id and not combatant_id:
        return None
    name = str(raw.get("combatant_name") or raw.get("name") or "").strip()[:MAX_NAME]
    return {
        "combat_id": combat_id,
        "combatant_id": combatant_id,
        "combatant_name": name,
        "hp": max(0, _as_int(raw.get("hp"), 0)),
        "max_hp": max(0, _as_int(raw.get("max_hp"), 0)),
        "temp_hp": max(0, _as_int(raw.get("temp_hp"), 0)),
        "initiative": _as_int(raw.get("initiative"), 0),
        "status": str(raw.get("status") or "")[:24],
        "side": str(raw.get("side") or "")[:24],
        "round": max(1, _as_int(raw.get("round"), 1)),
        "current": bool(raw.get("current")),
        "active": bool(raw.get("active", True)),
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
        # 战斗绑定：``combatant`` 存绑定串（NPC 为 ``npc:<combatant_id>``），
        # ``combatant_name`` 保留可读名字，``combat`` 是战斗状态快照。
        "combatant_name": str(raw.get("combatant_name") or "")[:MAX_NAME],
        "combat": _normalize_token_combat(raw.get("combat")),
        # 角色卡头像（仅本地资源路径）；空字符串时前端保持旧的色块+首字表现。
        "avatar": _token_avatar(raw.get("avatar")),
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


def _normalize_scene_binding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    url = str(raw.get("url") or "").strip()
    location_id = str(raw.get("location_id") or "").strip()[:128]
    if not location_id or not url.startswith("/"):
        return {}
    return {
        "location_id": location_id,
        "title": str(raw.get("title") or "")[:80],
        "url": url[:2048],
        "asset_id": str(raw.get("asset_id") or "")[:64],
        "calibrated": bool(raw.get("calibrated")),
        "fingerprint": str(raw.get("fingerprint") or "")[:300],
    }


def _normalize_map(raw: Any) -> dict[str, Any]:
    state = _default_map()
    if isinstance(raw, dict):
        state.update(raw)
    state["version"] = MAP_VERSION
    state["revision"] = max(0, _as_int(state.get("revision"), 0))
    state["scene_id"] = str(state.get("scene_id") or "default")[:64]
    state["title"] = str(state.get("title") or state["scene_id"])[:80]
    state["background"] = str(state.get("background") or "")[:2048]
    state["background_meta"] = _normalize_background_meta(state.get("background_meta"))
    # 模组自动绑定到当前场景的记录；用于判断进入新场景时是否需要换图。
    state["scene_binding"] = _normalize_scene_binding(state.get("scene_binding"))
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


def _load_room_scenario(info: dict[str, Any]) -> tuple[dict[str, Any] | None, Path | None]:
    """读取房间绑定的剧本及其目录；没有剧本或读取失败时返回 (None, None)。"""
    scenario_id = (info or {}).get("scenario_id")
    if scenario_id in (None, "", 0, "0"):
        return None, None
    try:
        from flask import current_app

        from trpg_server.scenario_store import load_scenario_by_id

        scenarios_dir = current_app.config.get("SCENARIOS_DIR")
        if not scenarios_dir:
            return None, None
        path, scenario = load_scenario_by_id(
            Path(scenarios_dir),
            scenario_id,
            (info or {}).get("scenario_version"),
        )
    except Exception:
        return None, None
    if not isinstance(scenario, dict):
        return None, None
    return scenario, (Path(path).parent if path else None)


def _room_scenario(info: dict[str, Any]) -> dict[str, Any] | None:
    scenario, _directory = _load_room_scenario(info)
    return scenario


def _narrative_bundle(info: dict[str, Any]):
    """房间剧本的叙事图；没有图谱或读取失败时返回 None（地图照常可用）。"""
    _scenario, directory = _load_room_scenario(info)
    if directory is None:
        return None
    try:
        from trpg_server.agents.narrative.store import load_bundle

        return load_bundle(directory)
    except Exception:  # noqa: BLE001 - 图谱不可用不阻断地图
        logger.warning("veil_map_narrative_bundle_failed", exc_info=True)
        return None


def _resolve_scene_area(info: dict[str, Any], scenario: dict[str, Any] | None, location: str) -> str:
    """叙事位置 → 所属大节点；没有图谱标注时退回位置本身。

    地图轮询（``/map``）会高频调用，因此按 ``narrative.json`` 的 mtime 缓存
    位置→区域映射，避免每次请求都解析整张图谱。
    """
    if not location:
        return ""
    _scenario, directory = _load_room_scenario(info)
    if directory is None:
        return location
    mapping = _area_lookup(directory)
    if not mapping:
        return location
    return mapping.get(location) or location


_AREA_CACHE: dict[tuple[str, float], dict[str, str]] = {}
_AREA_CACHE_LIMIT = 8


def _area_lookup(directory: Path) -> dict[str, str]:
    narrative_path = Path(directory) / "narrative.json"
    try:
        mtime = narrative_path.stat().st_mtime
    except OSError:
        return {}
    key = (str(directory), mtime)
    cached = _AREA_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from trpg_server.agents.narrative.mapgraph import area_id_of
        from trpg_server.agents.narrative.store import load_bundle

        bundle = load_bundle(Path(directory))
        mapping = {
            node.id: area_id_of(bundle, node.id) or node.id for node in bundle.nodes
        }
    except Exception:  # noqa: BLE001 - 图谱不可用时退回位置本身
        logger.warning("veil_map_area_lookup_failed", exc_info=True)
        return {}
    if len(_AREA_CACHE) >= _AREA_CACHE_LIMIT:
        _AREA_CACHE.pop(next(iter(_AREA_CACHE)))
    _AREA_CACHE[key] = mapping
    return mapping


def _resolve_room_scene(
    scenario: dict[str, Any] | None, room_dir, info: dict[str, Any]
) -> str:
    """房间当前场景：叙事运行态优先，其次 active_scene_id，最后剧本起点。"""
    narrative: dict[str, Any] = {}
    try:
        from trpg_server.agents.narrative.state_store import load_narrative_state

        narrative = load_narrative_state(room_dir) or {}
    except Exception:
        narrative = {}
    location = str(narrative.get("location") or narrative.get("cursor") or "").strip()
    if location:
        return location
    active = str((info or {}).get("active_scene_id") or "").strip()
    if active and isinstance(scenario, dict):
        for module in scenario.get("modules") or []:
            if not isinstance(module, dict):
                continue
            if active in {str(module.get("id") or ""), str(module.get("scene_id") or "")}:
                return str(module.get("id") or active)
    if active:
        return active
    start = scenario.get("start") if isinstance(scenario, dict) and isinstance(scenario.get("start"), dict) else {}
    return str(start.get("location_id") or "")


def _map_marker_visibility(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "kp" if text in {"kp", "kp_only", "dm", "dm_only"} else "public"


def _normalize_map_marker_types(raw: Any) -> list[dict[str, Any]]:
    """剧本编辑器写入的标记类型清单：类型 → 颜色 / 说明 / 可见性。"""
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()[:40]
        if not label:
            continue
        color = str(item.get("color") or "").strip()
        items.append(
            {
                "id": str(item.get("id") or f"type-{index + 1}")[:40],
                "label": label,
                "color": color if _HEX_COLOR.match(color) else TOKEN_COLORS[0],
                "description": str(item.get("description") or item.get("note") or "").strip()[:400],
                "visibility": _map_marker_visibility(item.get("visibility")),
            }
        )
    return items[:40]


def _normalize_map_markers(raw: Any) -> list[dict[str, Any]]:
    """标记矩形按底图原始像素保存；房间侧再换算到世界坐标。"""
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        x = _fraction(item.get("x"))
        y = _fraction(item.get("y"))
        width = _fraction(item.get("w"))
        height = _fraction(item.get("h"))
        if width <= 0 or height <= 0:
            continue
        items.append(
            {
                "id": str(item.get("id") or f"marker-{index + 1}")[:60],
                "type": str(item.get("type") or "")[:40],
                "label": str(item.get("label") or item.get("name") or "").strip()[:80],
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "visibility": _map_marker_visibility(item.get("visibility")),
            }
        )
    return items[:500]


def _scenario_map_config(scenario: dict[str, Any] | None, location: str) -> dict[str, Any]:
    if not isinstance(scenario, dict) or not location:
        return {}
    config = scenario.get("map_config")
    if not isinstance(config, dict):
        return {}
    scene = config.get(location)
    return scene if isinstance(scene, dict) else {}


def _scenario_asset_url(scenario: dict[str, Any], asset_id: str) -> str:
    if not asset_id:
        return ""
    for asset in scenario.get("assets") or []:
        if not isinstance(asset, dict) or str(asset.get("id") or "") != asset_id:
            continue
        url = str(asset.get("url") or "").strip()
        if url.startswith("/") and not url.startswith("//"):
            return url
    return ""


def _declared_scene_map(
    scenario: dict[str, Any] | None, location: str, area_id: str = ""
) -> dict[str, Any] | None:
    """模组为该场景声明的玩家可见地图。

    优先使用剧本地图编辑器写入的 ``map_config``（含人工标定的每格像素与
    偏移，不再依赖图片尺寸自动推测）；没有标定配置时退回 enter 触发器里的
    玩家可见图片。两种来源都只取非 kp_only 的底图。

    ``area_id`` 是叙事图里的大节点：地图按大节点声明，兴趣点位置仍要能
    找到所属地图的底图（修复 POI id 查不到地图的问题）。
    """
    target = area_id or location
    if not scenario or not target:
        return None
    title = ""
    for module in scenario.get("modules") or []:
        if not isinstance(module, dict):
            continue
        if target in {str(module.get("id") or ""), str(module.get("scene_id") or "")}:
            title = str(module.get("title") or "")
            break

    config = _scenario_map_config(scenario, target)
    if config:
        asset_id = str(config.get("background_asset_id") or "").strip()
        url = str(config.get("background_url") or "").strip() or _scenario_asset_url(
            scenario, asset_id
        )
        if url.startswith("/") and not url.startswith("//"):
            width = max(0, _as_int(config.get("width"), 0))
            height = max(0, _as_int(config.get("height"), 0))
            cell_px = round(_fraction(config.get("cell_px")), 4)
            offset_x = round(_fraction(config.get("offset_x")), 4)
            offset_y = round(_fraction(config.get("offset_y")), 4)
            calibrated = cell_px > 0 and width > 0 and height > 0
            return {
                "location_id": target,
                "title": title or str(config.get("title") or ""),
                "url": url,
                "asset_id": asset_id,
                "width": width,
                "height": height,
                "grid_size": 0,
                "grid_offset": None,
                "cell_px": cell_px,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "calibrated": calibrated,
                "fingerprint": (
                    f"{url}|{width}x{height}|{cell_px}|{offset_x}|{offset_y}"
                    if calibrated
                    else ""
                ),
                "marker_types": _normalize_map_marker_types(config.get("marker_types")),
                "markers": _normalize_map_markers(config.get("markers")),
            }

    assets = {
        str(asset.get("id")): asset
        for asset in scenario.get("assets") or []
        if isinstance(asset, dict) and asset.get("id")
    }
    candidates: list[tuple[int, dict[str, Any]]] = []
    for trigger in scenario.get("triggers") or []:
        if not isinstance(trigger, dict) or trigger.get("enabled") is False:
            continue
        binding = trigger.get("binding") if isinstance(trigger.get("binding"), dict) else {}
        if str(binding.get("kind") or "") != "enter":
            continue
        if str(binding.get("location_id") or "") != target:
            continue
        group_rank = 0 if str(trigger.get("group") or "") == "地图" else 1
        for order, attachment in enumerate(trigger.get("attachments") or []):
            if not isinstance(attachment, dict):
                continue
            asset = assets.get(str(attachment.get("asset_id") or ""))
            if not asset or str(asset.get("kind") or "") != "image":
                continue
            visibility = str(attachment.get("visibility") or asset.get("visibility") or "public")
            if visibility == "kp_only":
                continue
            url = str(asset.get("url") or "").strip()
            if not url.startswith("/") or url.startswith("//"):
                continue
            candidates.append(
                (
                    group_rank * 1000 + order,
                    {
                        "location_id": target,
                        "title": title or str(asset.get("name") or ""),
                        "url": url,
                        "asset_id": str(asset.get("id") or ""),
                        "width": max(0, _as_int(asset.get("width"), 0)),
                        "height": max(0, _as_int(asset.get("height"), 0)),
                        "grid_size": _as_int(
                            asset.get("grid_size") or asset.get("gridSize"), 0
                        ),
                        "grid_offset": asset.get("grid_offset")
                        if isinstance(asset.get("grid_offset"), dict)
                        else asset.get("gridOffset"),
                    },
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _apply_scene_binding(state: dict[str, Any], binding: dict[str, Any]) -> None:
    width = _as_int(binding.get("width"), 0)
    height = _as_int(binding.get("height"), 0)
    cell_px = _fraction(binding.get("cell_px"))
    if cell_px > 0:
        # 人工标定：图内每格像素直接作为画布网格；图片按原始尺寸以世界原点
        # 居中绘制，所以图内偏移减去图片半宽/半高就是画布格线偏移。
        grid = _clamp(int(round(cell_px)), MIN_GRID, MAX_GRID)
        raw_offset = {
            "x": int(round(_fraction(binding.get("offset_x")) - width / 2)) if width > 0 else 0,
            "y": int(round(_fraction(binding.get("offset_y")) - height / 2)) if height > 0 else 0,
        }
    else:
        declared_grid = _as_int(binding.get("grid_size"), 0)
        grid = _clamp(
            declared_grid if declared_grid > 0 else state.get("grid_size"), MIN_GRID, MAX_GRID
        )
        offset = binding.get("grid_offset")
        if isinstance(offset, dict):
            raw_offset = {"x": _as_int(offset.get("x"), 0), "y": _as_int(offset.get("y"), 0)}
        else:
            # 背景图以世界原点为中心绘制：把图片左上角对齐到格线，图内格子就是整格。
            raw_offset = {
                "x": int(round((-width / 2) % grid)) if width > 0 else 0,
                "y": int(round((-height / 2) % grid)) if height > 0 else 0,
            }
    state["grid_size"] = grid
    # 格线按间隔重复，偏移只需保留一个周期内的同余值；这样大图坐标不会
    # 被钳到边界而整体错位。
    state["grid_offset"] = {
        "x": _as_int(raw_offset.get("x"), 0) % grid,
        "y": _as_int(raw_offset.get("y"), 0) % grid,
    }
    state["background"] = str(binding["url"])
    state["background_meta"] = _normalize_background_meta(
        {
            "kind": "scenario",
            "format": "scenario",
            "name": str(binding.get("title") or "场景地图"),
            "url": binding["url"],
            "width": width,
            "height": height,
        }
    )
    state["scene_binding"] = {
        "location_id": binding["location_id"],
        "title": str(binding.get("title") or "")[:80],
        "url": binding["url"],
        "asset_id": str(binding.get("asset_id") or "")[:64],
        "calibrated": bool(binding.get("calibrated")),
        "fingerprint": str(binding.get("fingerprint") or "")[:300],
    }


def _scene_binding_markers(
    binding: dict[str, Any] | None, manager: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按观察者过滤剧本地图标记：DM 版标记与含义只发给房主/房管。

    标记在剧本里以底图原始像素保存；房间画布把底图按原始大小居中绘制，
    所以这里统一换算成世界坐标，前端只负责画。
    """
    if not binding:
        return [], []
    types = {
        str(item.get("id") or ""): item
        for item in binding.get("marker_types") or []
        if isinstance(item, dict)
    }
    width = _as_int(binding.get("width"), 0)
    height = _as_int(binding.get("height"), 0)
    visible_types: list[dict[str, Any]] = []
    for item in types.values():
        if not manager and item.get("visibility") == "kp":
            continue
        visible_types.append(dict(item))
    markers: list[dict[str, Any]] = []
    for marker in binding.get("markers") or []:
        if not isinstance(marker, dict):
            continue
        kind = types.get(str(marker.get("type") or "")) or {}
        visibility = str(marker.get("visibility") or kind.get("visibility") or "public")
        if not manager and visibility == "kp":
            continue
        # 类型本身是 DM 专属时，即使某条标记被设为玩家可见，也不下发类型
        # 名称与说明，避免把 DM 的含义泄露给玩家。
        kind_hidden = not manager and str(kind.get("visibility") or "") == "kp"
        markers.append(
            {
                "id": str(marker.get("id") or ""),
                "type": str(marker.get("type") or ""),
                "type_label": "" if kind_hidden else str(kind.get("label") or ""),
                "description": "" if kind_hidden else str(kind.get("description") or ""),
                "label": str(marker.get("label") or ""),
                "color": str(kind.get("color") or TOKEN_COLORS[0]),
                "visibility": visibility,
                "x": round(_fraction(marker.get("x")) - width / 2, 2),
                "y": round(_fraction(marker.get("y")) - height / 2, 2),
                "w": round(_fraction(marker.get("w")), 2),
                "h": round(_fraction(marker.get("h")), 2),
            }
        )
    return visible_types, markers


def _scene_binding_view(
    state: dict[str, Any],
    scenario: dict[str, Any] | None,
    location: str,
    manager: bool = False,
    area_id: str = "",
) -> dict[str, Any]:
    target = area_id or location
    binding = _declared_scene_map(scenario, location, area_id=area_id)
    stored = state.get("scene_binding") or {}
    fingerprint = str((binding or {}).get("fingerprint") or "")
    applied = (
        bool(binding)
        and str(stored.get("location_id") or "") == target
        and (not fingerprint or str(stored.get("fingerprint") or "") == fingerprint)
    )
    marker_types, markers = _scene_binding_markers(binding, manager)
    return {
        "managed": True,
        "location_id": target,
        "area_id": area_id or location,
        "title": (binding or {}).get("title") or "",
        "url": (binding or {}).get("url") or "",
        "applied": applied,
        "calibrated": bool((binding or {}).get("calibrated")),
        "fingerprint": fingerprint,
        "markers": markers,
        "marker_types": marker_types,
    }


def _placement_offsets() -> list[tuple[int, int]]:
    """入口附近的落点顺序：先入口格，再按环向外扩散。"""
    offsets = [(0, 0)]
    for radius in range(1, 5):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) == radius:
                    offsets.append((dx, dy))
    return offsets


def _entry_anchor_cell(bundle: Any, entry_poi: str, state: dict[str, Any]) -> tuple[int, int]:
    """入口兴趣点的中心格；没有几何信息时落在画布原点。"""
    bounds = _clamp(_as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS)
    half = bounds // 2
    cell_x, cell_y = 0, 0
    if bundle is not None and entry_poi:
        try:
            from trpg_server.agents.narrative.mapgraph import poi_center

            center = poi_center(bundle.node(entry_poi))
            if center is not None:
                cell_x, cell_y = int(round(center[0])), int(round(center[1]))
        except Exception:  # noqa: BLE001 - 几何缺失时用原点
            pass
    return (max(-half + 1, min(half - 1, cell_x)), max(-half + 1, min(half - 1, cell_y)))


def _place_party_tokens(
    state: dict[str, Any],
    info: dict[str, Any],
    anchor: tuple[int, int],
    bundle: Any = None,
) -> list[dict[str, Any]]:
    """按活跃成员在新场景入口附近放 PC 棋子；墙与旧图棋子不跟过来。"""
    tokens = state.setdefault("tokens", [])
    grid, offset_x, offset_y = _grid_of(state)
    offsets = _placement_offsets()
    placed: list[dict[str, Any]] = []
    for index, member in enumerate(_active_members(info)):
        user_id = str(member.get("user_id") or "").strip()
        if not user_id:
            continue
        name = _member_name(member)
        avatar = _member_avatar(member)
        token = _find_token(state, f"member-{user_id}")
        if token is None:
            token = {
                "id": f"member-{user_id}",
                "name": name,
                "cell_x": 0,
                "cell_y": 0,
                "offset_x": 0.0,
                "offset_y": 0.0,
                "w": 1,
                "h": 1,
                "color": TOKEN_COLORS[index % len(TOKEN_COLORS)],
                "owner_user_id": user_id,
                "kind": "pc",
                "combatant": name,
                "avatar": avatar,
            }
            tokens.append(token)
        else:
            token["name"] = name
            token["owner_user_id"] = user_id
            token["kind"] = "pc"
            token["combatant"] = name
            token["avatar"] = avatar
        target = None
        for dx, dy in offsets:
            cell_x, cell_y = anchor[0] + dx, anchor[1] + dy
            if not _within_bounds(state, token, cell_x, cell_y):
                continue
            if _occupancy_conflict(state, token, cell_x, cell_y) is not None:
                continue
            target = (cell_x, cell_y)
            break
        if target is None:
            continue
        _set_token_cell(token, target[0], target[1], 0.0, 0.0, grid, offset_x, offset_y)
        placed.append(
            {
                "token_id": str(token.get("id") or ""),
                "name": str(token.get("name") or ""),
                "cell_x": target[0],
                "cell_y": target[1],
            }
        )
    return placed


def _activate_area_scene(
    room_dir: Any,
    info: dict[str, Any],
    area_id: str,
    entry_poi: str = "",
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按大节点新建/切换场景：``scene_id == area_id``，切图不丢旧图棋子与墙。"""
    key = str(area_id or "").strip()
    if not key:
        return {"ok": False, "code": "map.scene_unavailable", "error": "缺少目标区域 id。"}
    scenario = scenario if scenario is not None else _room_scenario(info)
    scene_id = scene_store.safe_scene_id(key)
    exists = scene_store.has_scene(room_dir, scene_id)
    binding = _declared_scene_map(scenario, key, area_id=key)
    if not exists and not binding:
        return {
            "ok": False,
            "code": "map.scene_unavailable",
            "error": f"区域「{key}」没有声明地图。",
        }
    bundle = _narrative_bundle(info) if not exists else None
    if not entry_poi and bundle is not None:
        try:
            from trpg_server.agents.narrative.mapgraph import entry_pois

            entries = entry_pois(bundle, bundle.node(key))
            entry_poi = entries[0] if entries else ""
        except Exception:  # noqa: BLE001 - 入口缺失时退回原点
            entry_poi = ""
    with room_lock(room_dir):
        created = False
        binding_changed = False
        placements: list[dict[str, Any]] = []
        if not exists:
            state = _default_map()
            state["scene_id"] = scene_id
            state["title"] = str((binding or {}).get("title") or key)[:80]
            if bundle is not None:
                area_node = bundle.node(key)
                declared_bounds = _as_int(
                    (area_node.map_block if area_node is not None else {}).get("bounds_cells"),
                    0,
                )
                if declared_bounds:
                    state["bounds_cells"] = _clamp(
                        declared_bounds, MIN_BOUNDS, MAX_BOUNDS
                    )
            _apply_scene_binding(state, binding or {})
            placements = _place_party_tokens(
                state, info, _entry_anchor_cell(bundle, entry_poi, state), bundle
            )
            state = _save_map(room_dir, state)
            created = True
            previous = ""
        else:
            previous = scene_store.active_scene_id(room_dir)
            scene_store.activate(room_dir, scene_id)
            state, _source = scene_store.load(room_dir)
            state = _normalize_map(state)
            stored = state.get("scene_binding") or {}
            fingerprint = str((binding or {}).get("fingerprint") or "")
            stale = bool(binding) and (
                str(stored.get("location_id") or "") != key
                or (fingerprint and str(stored.get("fingerprint") or "") != fingerprint)
            )
            if stale:
                _apply_scene_binding(state, binding or {})
                state = _save_map(room_dir, state)
                binding_changed = True
        state["scene_id"] = scene_id
    return {
        "ok": True,
        "scene_id": scene_id,
        "area_id": key,
        "created": created,
        "changed": created or previous != scene_id or binding_changed,
        "placements": placements,
        "state": state,
    }


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
    *,
    activate: bool = True,
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
    scene_store.save(room_dir, _persistable(normalized), activate=activate)
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


def _member_avatar(member: dict[str, Any]) -> str:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    return _token_avatar(card.get("avatar"))


def _token_avatar(value: Any) -> str:
    """只接受站内资源路径；没有头像时返回空串（前端回退色块）。"""
    avatar = str(value or "").strip()
    if not avatar.startswith("/") or avatar.startswith("//"):
        return ""
    return avatar[:512]


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
        avatar = _member_avatar(member)
        token = by_id.get(f"member-{user_id}") or by_owner.get(user_id)
        if token is not None:
            if (
                token.get("name") != name
                or token.get("owner_user_id") != user_id
                or _token_avatar(token.get("avatar")) != avatar
            ):
                token["name"] = name
                token["owner_user_id"] = user_id
                token["kind"] = "pc"
                token["combatant"] = name
                token["avatar"] = avatar
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
            "avatar": avatar,
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


def _map_settings_path(room_dir) -> Path:
    """房间级地图设置（不随场景分片切换）。"""
    return Path(room_dir) / "plugins" / "veil-map" / "settings.json"


def _load_map_settings(room_dir) -> dict[str, Any]:
    raw = read_json(_map_settings_path(room_dir), default={})
    return dict(raw) if isinstance(raw, dict) else {}


def _map_permission(room_dir) -> bool:
    """房间级棋子移动权限；缺省允许玩家移动自己的棋子（保持旧行为）。"""
    return _as_bool(_load_map_settings(room_dir).get("player_move_enabled"), True)


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
        payload: dict[str, Any] = {
            "room_id": str(room_id),
            "map": state,
            "player_move_enabled": _map_permission(room_dir) if room_dir else True,
        }
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
            "player_move_enabled": _map_permission(room_dir),
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
        # 没有可推送目标时不再回退到整房间全量 state：房间频道里可能仍有
        # 已被移除但连接未断的客户端，全量下发会让迷雾失效。这里只记日志。
        logger.debug("veil_map_broadcast_no_recipients room=%s", room_id)


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
                "combatant_id": str(combatant.get("combatant_id") or ""),
                "side": str(combatant.get("side") or ""),
                "hp": max(0, _as_int(combatant.get("hp"), 0)),
                "max_hp": max(0, _as_int(combatant.get("max_hp"), 0)),
                "initiative": _as_int(combatant.get("initiative"), 0),
                "status": str(combatant.get("status") or ""),
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
        str(token.get("combatant_name") or "").strip().casefold(),
    }
    combatant_raw = str(token.get("combatant") or "").strip()
    if combatant_raw.lower().startswith("npc:"):
        candidates.add(combatant_raw[4:].casefold())
    block = token.get("combat") if isinstance(token.get("combat"), dict) else {}
    bound_id = str(block.get("combatant_id") or "").strip()
    owner = str(token.get("owner_user_id") or "")
    for name, combatant in combatants.items():
        if not isinstance(combatant, dict):
            continue
        if bound_id and _combatant_binding_id(combatant) == bound_id:
            return str(name), combatant
        if str(name).strip().casefold() in candidates:
            return str(name), combatant
        if owner and str(combatant.get("user_id") or "") == owner:
            return str(name), combatant
    return "", None


def _combatant_binding_id(combatant: dict[str, Any]) -> str:
    combatant_id = str(combatant.get("combatant_id") or "").strip()
    if combatant_id:
        return combatant_id[:96]
    return str(combatant.get("name") or "").strip()[:96]


def _combat_binding(combatant: dict[str, Any]) -> str:
    return f"npc:{_combatant_binding_id(combatant)}"


def _combat_token_id(binding: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", binding).strip("-").lower()[:40]
    if not safe:
        safe = hashlib.sha1(binding.encode("utf-8")).hexdigest()[:10]
    return f"combat-{safe}"


def _combat_token_summary(
    combat: dict[str, Any], name: str, combatant: dict[str, Any]
) -> dict[str, Any]:
    order = [str(item) for item in combat.get("order") or []]
    turn = _as_int(combat.get("turn"), 0)
    current = order[turn] if 0 <= turn < len(order) else ""
    return {
        "combat_id": str(combat.get("id") or "")[:64],
        "combatant_id": _combatant_binding_id(combatant),
        "combatant_name": str(combatant.get("display_name") or name)[:MAX_NAME],
        "hp": max(0, _as_int(combatant.get("hp"), 0)),
        "max_hp": max(0, _as_int(combatant.get("max_hp"), 0)),
        "temp_hp": max(0, _as_int(combatant.get("temp_hp"), 0)),
        "initiative": _as_int(combatant.get("initiative"), 0),
        "status": str(combatant.get("status") or "")[:24],
        "side": str(combatant.get("side") or "")[:24],
        "round": max(1, _as_int(combat.get("round"), 1)),
        "current": name == current,
        "active": True,
    }


def _find_combat_token(state: dict[str, Any], combatant_id: str) -> dict[str, Any] | None:
    binding = f"npc:{combatant_id}"
    for token in state.get("tokens") or []:
        block = token.get("combat") if isinstance(token.get("combat"), dict) else None
        if block and str(block.get("combatant_id") or "") == combatant_id:
            return token
    for token in state.get("tokens") or []:
        if str(token.get("combatant") or "") == binding:
            return token
    return None


def _find_player_token(state: dict[str, Any], combatant: dict[str, Any]) -> dict[str, Any] | None:
    user_id = str(combatant.get("user_id") or "")
    name = str(combatant.get("name") or "").strip().casefold()
    for token in state.get("tokens") or []:
        if str(token.get("kind") or "") != "pc":
            continue
        if user_id and str(token.get("owner_user_id") or "") == user_id:
            return token
        for candidate in (token.get("combatant"), token.get("name")):
            if name and str(candidate or "").strip().casefold() == name:
                return token
    return None


def _combat_position(combatant: dict[str, Any], state: dict[str, Any]) -> tuple[int, int] | None:
    position = combatant.get("position")
    if not isinstance(position, dict):
        return None
    scene_id = str(position.get("scene_id") or "")
    if scene_id and scene_id != str(state.get("scene_id") or ""):
        return None
    cell = position.get("cell") if isinstance(position.get("cell"), dict) else position
    x, y = cell.get("x"), cell.get("y")
    if x in (None, "") or y in (None, ""):
        return None
    return _clamp(_as_int(x, 0), -1000, 1000), _clamp(_as_int(y, 0), -1000, 1000)


def _combat_anchor(state: dict[str, Any]) -> tuple[int, int]:
    grid, offset_x, offset_y = _grid_of(state)
    cells = [
        _cell_of(token, grid, offset_x, offset_y)
        for token in state.get("tokens") or []
        if str(token.get("kind") or "") == "pc"
    ]
    if not cells:
        return 0, 0
    return (
        int(round(sum(cell[0] for cell in cells) / len(cells))),
        int(round(sum(cell[1] for cell in cells) / len(cells))),
    )


def _combat_token_color(combatant_id: str) -> str:
    digest = hashlib.sha1(combatant_id.encode("utf-8")).hexdigest()
    return TOKEN_COLORS[int(digest[:8], 16) % len(TOKEN_COLORS)]


def _new_combat_token(
    state: dict[str, Any], combatant: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any] | None:
    if len(state.get("tokens") or []) >= MAX_TOKENS:
        return None
    binding = _combat_binding(combatant)
    token_id = _combat_token_id(binding)
    existing_ids = {str(token.get("id") or "") for token in state.get("tokens") or []}
    if token_id in existing_ids:
        token_id = f"{token_id[:48]}-{hashlib.sha1(binding.encode('utf-8')).hexdigest()[:6]}"
    return {
        "id": token_id[:64],
        "name": summary["combatant_name"],
        "kind": "npc",
        "combatant": binding[:96],
        "combatant_name": summary["combatant_name"],
        "combat": dict(summary),
        "cell_x": 0,
        "cell_y": 0,
        "x": 0.0,
        "y": 0.0,
        "w": 1,
        "h": 1,
        "color": _combat_token_color(summary["combatant_id"]),
        "owner_user_id": "",
        "avatar": "",
        "vision_cells": 0,
    }


def _place_combat_token(
    state: dict[str, Any],
    token: dict[str, Any],
    anchor: tuple[int, int],
    preferred: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    grid, offset_x, offset_y = _grid_of(state)
    candidates: list[tuple[int, int]] = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.extend(
        (anchor[0] + dx, anchor[1] + dy) for dx, dy in _placement_offsets()
    )
    seen: set[tuple[int, int]] = set()
    for cell_x, cell_y in candidates:
        if (cell_x, cell_y) in seen:
            continue
        seen.add((cell_x, cell_y))
        if not _within_bounds(state, token, cell_x, cell_y):
            continue
        if _occupancy_conflict(state, token, cell_x, cell_y) is not None:
            continue
        _set_token_cell(token, cell_x, cell_y, 0.0, 0.0, grid, offset_x, offset_y)
        return cell_x, cell_y
    return None


def _move_bound_token(
    state: dict[str, Any], token: dict[str, Any], position: tuple[int, int] | None
) -> bool:
    """战斗状态里的位置变化回写到棋子；目标格被占/越界时保留原位置。"""
    if position is None:
        return False
    grid, offset_x, offset_y = _grid_of(state)
    current = _cell_of(token, grid, offset_x, offset_y)
    if current == position:
        return False
    cell_x, cell_y = position
    if not _within_bounds(state, token, cell_x, cell_y):
        return False
    if _occupancy_conflict(state, token, cell_x, cell_y) is not None:
        return False
    _set_token_cell(token, cell_x, cell_y, 0.0, 0.0, grid, offset_x, offset_y)
    return True


def _apply_combat_block(token: dict[str, Any], summary: dict[str, Any]) -> bool:
    desired = dict(summary)
    if token.get("combat") == desired and token.get("combatant_name") == desired["combatant_name"]:
        return False
    token["combat"] = desired
    token["combatant_name"] = desired["combatant_name"]
    return True


def _clear_combat_tokens(state: dict[str, Any], combat_id: str) -> int:
    removed = 0
    kept: list[dict[str, Any]] = []
    for token in state.get("tokens") or []:
        block = token.get("combat") if isinstance(token.get("combat"), dict) else None
        if not block or str(block.get("combat_id") or "") != combat_id:
            kept.append(token)
            continue
        if str(token.get("kind") or "") == "npc":
            removed += 1
            continue
        token.pop("combat", None)
        token["combatant_name"] = ""
        kept.append(token)
    state["tokens"] = kept
    return removed


def _sync_scene_combat_tokens(
    state: dict[str, Any],
    combat: dict[str, Any],
    active: bool,
    active_ids: set[str],
) -> tuple[bool, dict[str, Any]]:
    """同步当前场景；返回 ``(changed, stats)``。"""
    combat_id = str(combat.get("id") or "")
    changed = False
    stats: dict[str, Any] = {"placed": [], "updated": 0, "removed": 0, "warnings": []}
    if not active:
        removed = _clear_combat_tokens(state, combat_id)
        return removed > 0, {**stats, "removed": removed}
    combatants = combat.get("combatants") if isinstance(combat.get("combatants"), dict) else {}
    anchor = _combat_anchor(state)
    retired_status = {"fled", "retreated", "escaped", "撤退", "脱离"}
    for name in combat.get("order") or []:
        combatant = combatants.get(str(name))
        if not isinstance(combatant, dict):
            continue
        combatant_id = _combatant_binding_id(combatant)
        if not combatant_id:
            continue
        summary = _combat_token_summary(combat, str(name), combatant)
        if combatant.get("is_player"):
            token = _find_player_token(state, combatant)
            if token is not None and _apply_combat_block(token, summary):
                changed = True
                stats["updated"] += 1
            continue
        token = _find_combat_token(state, combatant_id)
        if str(combatant.get("status") or "").strip().casefold() in retired_status:
            if token is not None:
                state["tokens"].remove(token)
                changed = True
                stats["removed"] += 1
            continue
        if token is None:
            token = _new_combat_token(state, combatant, summary)
            if token is None:
                stats["warnings"].append(
                    {"code": "map.token_limit", "message": "地图棋子数量已达上限，未放置 NPC 棋子。"}
                )
                continue
            state.setdefault("tokens", []).append(token)
            cell = _place_combat_token(state, token, anchor, _combat_position(combatant, state))
            if cell is None:
                state["tokens"].remove(token)
                stats["warnings"].append(
                    {
                        "code": "map.placement_failed",
                        "message": f"地图上没有空位，未放置 {summary['combatant_name']} 的棋子。",
                    }
                )
                continue
            stats["placed"].append(
                {
                    "token_id": token["id"],
                    "combatant_id": combatant_id,
                    "name": summary["combatant_name"],
                    "cell_x": cell[0],
                    "cell_y": cell[1],
                }
            )
            changed = True
        elif _move_bound_token(state, token, _combat_position(combatant, state)):
            changed = True
        if _apply_combat_block(token, summary):
            changed = True
            stats["updated"] += 1
    # 战斗中从行动顺序里被移除的单位：删掉它在这场战斗里的棋子。
    for token in list(state.get("tokens") or []):
        block = token.get("combat") if isinstance(token.get("combat"), dict) else None
        if not block or str(block.get("combat_id") or "") != combat_id:
            continue
        if str(token.get("kind") or "") != "npc":
            if str(block.get("combatant_id") or "") in active_ids:
                continue
            # PC 棋子不因退出战斗而删除，只清掉已失效的战斗快照。
            token.pop("combat", None)
            token["combatant_name"] = ""
            changed = True
            stats["updated"] += 1
            continue
        if str(block.get("combatant_id") or "") in active_ids:
            continue
        state["tokens"].remove(token)
        changed = True
        stats["removed"] += 1
    return changed, stats


def _active_combat_ids(combat: dict[str, Any]) -> set[str]:
    combatants = combat.get("combatants") if isinstance(combat.get("combatants"), dict) else {}
    ids: set[str] = set()
    for combatant in combatants.values():
        if isinstance(combatant, dict):
            combatant_id = _combatant_binding_id(combatant)
            if combatant_id:
                ids.add(combatant_id)
    return ids


def _combat_scene_fingerprint(combat: dict[str, Any], state: dict[str, Any]) -> str:
    payload = {
        "combat_id": str(combat.get("id") or ""),
        "active": bool(combat.get("active")),
        "round": _as_int(combat.get("round"), 1),
        "turn": _as_int(combat.get("turn"), 0),
        "tokens": [
            {
                "id": token.get("id"),
                "cell_x": token.get("cell_x"),
                "cell_y": token.get("cell_y"),
                "combat": token.get("combat"),
            }
            for token in state.get("tokens") or []
            if isinstance(token.get("combat"), dict)
        ],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _sync_combat_tokens(room_dir, info: dict[str, Any], combat: dict[str, Any]) -> dict[str, Any]:
    """按战斗状态维护当前场景与其它场景里的绑定棋子。

    没有地图/场景关闭时直接降级（``status=skipped``），不创建任何地图文件；
    放置失败只记 warning，战斗本身不受影响。
    """
    combat_id = str(combat.get("id") or "")
    active = bool(combat.get("active"))
    result: dict[str, Any] = {
        "ok": True,
        "status": "skipped",
        "reason": "no_scene",
        "combat_id": combat_id,
        "changed": False,
        "placed": [],
        "updated": 0,
        "removed": 0,
        "warnings": [],
    }
    with room_lock(room_dir):
        raw_state, source = scene_store.load(room_dir)
        if source not in {"scene", "legacy"} or not isinstance(raw_state, dict) or not raw_state:
            logger.info(
                "veil_map_combat_sync_skipped room=%s combat=%s reason=no_scene",
                info.get("id") or info.get("room_id") or "",
                combat_id,
            )
            return result
        state = _normalize_map(raw_state)
        _reconcile_tokens(state, info)
        active_ids = _active_combat_ids(combat)
        changed, stats = _sync_scene_combat_tokens(state, combat, active, active_ids)
        if changed:
            fingerprint = _combat_scene_fingerprint(combat, state)
            request_id = f"combat:{combat_id[:32]}:{fingerprint[:24]}"
            if _duplicate_record(state, request_id) is None:
                state = _save_map(
                    room_dir,
                    state,
                    {
                        "request_id": request_id,
                        "action": "token.combat_sync",
                        "ok": True,
                        "revision": state.get("revision") or 0,
                    },
                )
            _broadcast_combat_sync(info, state)
        result.update(
            status="synced",
            reason="",
            scene_id=str(state.get("scene_id") or ""),
            changed=changed,
            placed=stats["placed"],
            updated=stats["updated"],
            removed=stats["removed"],
            warnings=stats["warnings"],
        )
        if not active:
            removed = _cleanup_combat_scenes(
                room_dir, str(state.get("scene_id") or ""), combat_id
            )
            result["removed"] += removed
            if removed:
                result["changed"] = True
        for warning in result["warnings"]:
            logger.warning(
                "veil_map_combat_token_warning room=%s combat=%s code=%s",
                info.get("id") or "",
                combat_id,
                warning.get("code"),
            )
        return result


def _cleanup_combat_scenes(room_dir, active_scene: str, combat_id: str) -> int:
    """战斗结束后清理其它场景里遗留的本场棋子，避免切场景留下孤儿。"""
    removed = 0
    for scene in scene_store.list_scenes(room_dir):
        scene_id = str(scene.get("scene_id") or "")
        if not scene_id or scene_id == active_scene:
            continue
        raw = scene_store.load_scene(room_dir, scene_id)
        if not isinstance(raw, dict) or not raw:
            continue
        state = _normalize_map(raw)
        count = _clear_combat_tokens(state, combat_id)
        if not count:
            continue
        _save_map(
            room_dir,
            state,
            {
                "request_id": f"combat-clean:{combat_id[:24]}:{scene_id[:24]}",
                "action": "token.combat_cleanup",
                "ok": True,
                "revision": state.get("revision") or 0,
            },
            activate=False,
        )
        removed += count
    return removed


def _broadcast_combat_sync(info: dict[str, Any], state: dict[str, Any]) -> None:
    room_id = str(info.get("id") or info.get("room_id") or "")
    if not room_id:
        return
    try:
        _broadcast(room_id, state)
    except Exception:  # noqa: BLE001 - 后台/测试环境没有 Flask 上下文时忽略广播
        logger.debug("veil_map_combat_sync_broadcast_skipped room=%s", room_id)


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
        "scope": str(fog.get("scope") or "observer"),
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
    """玩家只拿到自己该看到的棋子 / 墙体 / 光源子集。

    已知风险（1.0.7 安全审查 M-2 的处置口径）：``background`` /
    ``background_meta`` 不在这里裁剪，底图 URL 对房间成员保持可用，视为该房间
    的公开素材。迷雾只承诺隐藏棋子、墙体、光源与未探索区域，不承诺隐藏底图
    画面本身；若需要连底图一起保密，应改为按已探索区域生成裁剪图或遮罩合成图，
    而不是继续依赖视图字段裁剪。
    """
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
    """返回 ``(裁剪后的地图, 迷雾载荷, 当前可见格)``；KP 拿完整地图。

    探索记录与可见区域按同一观察者对齐：玩家只用自己绑定棋子的记录，没有
    绑定棋子时退回队伍视角（队伍当前可见 + 全队探索记录）；队伍共享模式下
    所有玩家使用同一份累计记录。
    """
    fog = state.get("fog") or {}
    if not fog.get("enabled"):
        return state, {"enabled": False}, set()
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    grid, offset_x, offset_y = _grid_of(state)
    pcs = _pc_tokens(state)
    owned = [
        token
        for token in pcs
        if str(token.get("owner_user_id") or "") == str(user_id or "")
    ]
    fallback = not manager and not owned
    visible_tokens = pcs if (manager or fallback) else owned
    visible, polygons, observers = _vision_for(state, room_dir, visible_tokens)
    scope = str(fog.get("scope") or "observer")
    if scope not in _FOG_SCOPES:
        scope = "observer"
    base = visibility.decode_mask(fog.get("explored"), bounds)
    records = _fog_records(fog, bounds)
    explored = set(base)
    if manager or fallback or scope == "party":
        # KP / 无棋子玩家 / 队伍共享：都看队伍整体探索；前两者另外并入所有
        # 观察者的增量记录（KP 本就该看到全部信息）。
        party_cells, _party_polygons, _party_observers = _vision_for(
            state, room_dir, pcs
        )
        explored |= party_cells
        if manager or fallback:
            for cells in records.values():
                explored |= cells
    else:
        explored |= visible
        for token in owned:
            explored |= records.get(_observer_key(token), set())
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
        "scope": scope,
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
    """把当前可见格并入探索记录（只增不减）。

    队伍共享模式沿用单份累计掩码；默认的按观察者模式把每个 PC 棋子自己的
    视野写进 ``explored_by`` 的稀疏增量，已有共享掩码作为基线不动。
    """
    fog = state.get("fog") or {}
    if not fog.get("enabled"):
        return
    bounds = _clamp(
        _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
    )
    if str(fog.get("scope") or "observer") == "party":
        cells, _polygons, _observers = _vision_for(state, room_dir, _pc_tokens(state))
        if not cells:
            return
        explored = visibility.decode_mask(fog.get("explored"), bounds) | cells
        encoded = visibility.encode_mask(explored, bounds)
        if encoded != fog.get("explored"):
            fog["explored"] = encoded
            fog["explored_count"] = len(explored)
        return
    records = _fog_records(fog, bounds)
    # 共享基线里的格所有人都已经能看到，不再重复写进各人的增量。
    base = visibility.decode_mask(fog.get("explored"), bounds)
    changed = False
    for token in _pc_tokens(state):
        key = _observer_key(token)
        if not key:
            continue
        cells, _polygons, _observers = _vision_for(state, room_dir, [token])
        if not cells:
            continue
        cells -= base
        current = records.get(key, set())
        merged = current | cells
        if merged != current:
            records[key] = merged
            changed = True
    if not changed:
        return
    fog["explored_by"] = {
        key: {"cells": _cells_text(cells, bounds)}
        for key, cells in sorted(records.items())
        if cells
    }


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
) -> dict[str, Any] | None:
    """返回目标 footprint 上已占用的棋子；无冲突时返回 ``None``。"""
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
            return other
    return None


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
    state: dict[str, Any],
    token: dict[str, Any],
    cell_x: int,
    cell_y: int,
    *,
    manager: bool = True,
) -> tuple[str, str] | None:
    """移动入口共用的目标格校验：边界、占位与墙体穿越；被挡住时不改坐标与预算。

    非管理者只拿中性文案：迷雾下逐个格试走不能用来枚举隐藏棋子的名字。
    """
    if not _within_bounds(state, token, cell_x, cell_y):
        bounds = _clamp(
            _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT), MIN_BOUNDS, MAX_BOUNDS
        )
        return "map.out_of_bounds", f"目标位置超出地图边界（当前地图 {bounds} 格）。"
    occupied = _occupancy_conflict(state, token, cell_x, cell_y)
    if occupied is not None:
        if manager:
            return "map.cell_occupied", f"目标格已被「{occupied.get('name') or '其他棋子'}」占用。"
        return "map.cell_occupied", "目标格已有其他棋子或障碍物，无法进入。"
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
    info: dict[str, Any],
    token: dict[str, Any],
    user_id: Any,
    agent_id: Any,
    room_dir=None,
) -> tuple[bool, str]:
    """KP 视为 DM 可移动任意棋子；其余按房间角色、房间开关或棋子归属判定。"""
    if str(agent_id or "").strip().lower() in DM_AGENT_IDS:
        return True, ""
    if _member_role(info, user_id) in _OWNER_ROLES:
        return True, ""
    if room_dir is not None and not _map_permission(room_dir):
        return False, "KP 已关闭「玩家可移动自己的棋子」，只有房主或房管可以移动棋子。"
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
        from trpg_server.agents.tools.combat import save_combat

        combatant["position"] = {
            "scene_id": str(scene_store.active_scene_id(room_dir) or map_state.get("scene_id") or ""),
            "cell": {"x": int(target_cell_x), "y": int(target_cell_y)},
        }
        save_combat(
            SimpleNamespace(
                room_dir=room_dir,
                room_id=room_id,
                # 地图是这次移动的写入方，棋子位置已经随地图落盘；
                # 跳过插件钩子，避免钩子回写地图造成同一把锁内的旧状态覆盖。
                skip_combat_map_sync=True,
            ),
            combat,
        )
    except Exception:  # noqa: BLE001 - 预算回滚后按服务端错误返回
        budget["movement"] = budget_before
        logger.warning("veil_map_combat_save_failed room=%s", room_id, exc_info=True)
        return False, "map.combat_save_failed", "战斗状态保存失败，请稍后重试。"
    return True, "", ""


def _map_payload(room_id: str, room_dir, info: dict[str, Any]) -> dict[str, Any]:
    with room_lock(room_dir):
        state = _load_map(room_dir)
        # GET 必须只读：补齐 PC 棋子只发生在内存视图里，不写盘也不推进 revision，
        # 否则一个成员打开地图会让其他客户端手里的乐观锁版本立即过期。
        _reconcile_tokens(state, info)
        state = _normalize_map(state)
        scenes = scene_store.list_scenes(room_dir)
        if not scenes:
            # 还没有任何场景分片时，视图仍然要能展示当前（未落盘的）默认场景。
            scenes = [
                {
                    "scene_id": "default",
                    "title": "default",
                    "revision": _as_int(state.get("revision"), 0),
                    "updated_at": str(state.get("updated_at") or ""),
                    "background": str(state.get("background") or ""),
                    "tokens": len(state.get("tokens") or []),
                    "active": True,
                }
            ]
    _role, manager = _room_context(info)
    user_id = str(session.get("user_id") or "")
    view, fog_payload, _visible = _observer_scope(state, room_dir, user_id, manager)
    scenario = _room_scenario(info)
    location = _resolve_room_scene(scenario, room_dir, info)
    area_id = _resolve_scene_area(info, scenario, location)
    return {
        "map": view,
        "fog": fog_payload,
        "combat": _combat_payload(room_dir),
        "can_manage": manager,
        "player_move_enabled": _map_permission(room_dir),
        "scene_binding": _scene_binding_view(state, scenario, location, manager, area_id),
        "area_id": area_id,
        "room_id": str(room_id),
        "scene_id": view.get("scene_id") or state.get("scene_id"),
        "scenes": scenes,
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
        scope_before = str(state["fog"].get("scope") or "observer")
        scope_requested = ""
        if "fog_enabled" in payload:
            # 关掉迷雾时保留已探索掩码，重新打开可以接着用。
            state["fog"]["enabled"] = _as_bool(payload.get("fog_enabled"), state["fog"]["enabled"])
        if "fog_scope" in payload:
            scope_requested = str(payload.get("fog_scope") or "").strip().lower()
            if scope_requested not in _FOG_SCOPES:
                return _invalid_request(
                    "探索记录范围只能是 observer（按观察者）或 party（队伍共享）。",
                    "map.invalid_fog_scope",
                    request_id=request_id,
                    revision=state["revision"],
                )
            state["fog"]["scope"] = scope_requested
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
            state["fog"]["explored_by"] = {}
        elif scope_requested == "party" and scope_before != "party":
            # 切回队伍共享：把各观察者的增量并进共享记录，避免信息差关闭时丢失
            # 已经探索过的区域。
            merged = visibility.decode_mask(
                state["fog"].get("explored"), state["bounds_cells"]
            )
            for cells in _fog_records(state["fog"], state["bounds_cells"]).values():
                merged |= cells
            state["fog"]["explored"] = visibility.encode_mask(
                merged, state["bounds_cells"]
            )
            state["fog"]["explored_count"] = len(merged)
            state["fog"]["explored_by"] = {}
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


@bp.route("/rooms/<room_id>/map/permissions", methods=["PUT"])
def update_map_permissions(room_id: str):
    """房间级棋子移动权限：默认允许玩家移动自己的棋子。"""
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    if not isinstance(payload, dict) or "player_move_enabled" not in payload:
        return _invalid_request(
            "请求体缺少 player_move_enabled。",
            "map.invalid_payload",
        )
    _role, manager = _room_context(info)
    if not manager:
        return _rejected_request(
            "只有房主或房管可以修改棋子移动权限。",
            "map.manager_only",
        )
    enabled = _as_bool(payload.get("player_move_enabled"), True)
    with room_lock(room_dir):
        settings = _load_map_settings(room_dir)
        settings["player_move_enabled"] = enabled
        write_json_atomic(_map_settings_path(room_dir), settings)
        state = _load_map(room_dir)
    # 让所有已打开地图的客户端立即更新可移动状态。
    _broadcast(room_id, state)
    return success_response(
        {"player_move_enabled": enabled, "room_id": str(room_id)},
        "棋子移动权限已更新。",
    )


@bp.route("/rooms/<room_id>/map/sync-scene", methods=["POST"])
def sync_scene_map(room_id: str):
    """把模组为当前区域声明的地图同步为 canvas 背景（进入/切换场景时幂等调用）。

    地图按大节点（area）声明：叙事位置是 POI 时也会落到所属地图；第一次
    进入某张图会新建独立场景并切换过去，棋盘不再沿用上一张图的棋子与墙。

    地图由模组决定，请求体不参与选择；玩家也可以触发同步，但无法指定图片。
    """
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    scenario = _room_scenario(info)
    location = _resolve_room_scene(scenario, room_dir, info)
    area_id = _resolve_scene_area(info, scenario, location)
    binding = _declared_scene_map(scenario, location, area_id=area_id)
    if not binding:
        with room_lock(room_dir):
            state = _load_map(room_dir)
        return success_response(
            {
                "changed": False,
                "map": state,
                "area_id": area_id,
                "scene_binding": _scene_binding_view(
                    state, scenario, location, manager, area_id
                ),
            },
            "当前场景没有声明地图。",
        )
    result = _activate_area_scene(room_dir, info, area_id, scenario=scenario)
    if not result.get("ok"):
        return _rejected_request(
            str(result.get("error") or "目标地图场景不可用。"),
            str(result.get("code") or "map.scene_unavailable"),
        )
    state = result["state"]
    if result.get("changed"):
        _broadcast(room_id, state)
    return success_response(
        {
            "changed": bool(result.get("changed")),
            "created": bool(result.get("created")),
            "map": state,
            "area_id": area_id,
            "scene_id": result.get("scene_id"),
            "placements": result.get("placements") or [],
            "scene_binding": _scene_binding_view(state, scenario, location, manager, area_id),
        },
        "已切换到场景地图。" if result.get("changed") else "场景地图已绑定。",
    )


@bp.route("/rooms/<room_id>/map/scenes", methods=["GET"])
def list_map_scenes(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    active = scene_store.active_scene_id(room_dir)
    return success_response(
        {"scenes": scene_store.list_scenes(room_dir), "active_scene_id": active},
        "Scenes loaded",
    )


@bp.route("/rooms/<room_id>/map/scenes", methods=["POST"])
def create_map_scene(room_id: str):
    """新建场景并切换过去；``copy=true`` 时复制当前场景的棋子/墙/光源。"""
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload() or {}
    _role, manager = _room_context(info)
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
        if not manager:
            return error_response("只有房主或房管可以新建场景。", 403, code="map.manager_only")
        scene_id = scene_store.safe_scene_id(payload.get("scene_id") or "")
        if not scene_id or scene_id == "default" or scene_store.has_scene(room_dir, scene_id):
            scene_id = uuid4().hex[:12]
        title = str(payload.get("title") or "").strip()[:80] or "新场景"
        if _as_bool(payload.get("copy"), False):
            created = dict(state)
            created["scene_id"] = scene_id
            created["title"] = title
            created["revision"] = 0
            created["recent_requests"] = []
            # 新场景从未探索过：迷雾要按场景隔离，不能继承旧场景的记录。
            created["fog"] = _default_fog()
        else:
            created = _default_map()
            created["scene_id"] = scene_id
            created["title"] = title
        saved = _save_map(
            room_dir,
            created,
            {"request_id": str(payload.get("request_id") or uuid4().hex), "action": "scene.create"},
        )
        scene_store.activate(room_dir, scene_id)
    _broadcast(room_id, saved)
    return success_response(
        {
            "map": saved,
            "scenes": scene_store.list_scenes(room_dir),
            "scene_id": scene_id,
            "can_manage": True,
        },
        "场景已创建并切换。",
    )


@bp.route("/rooms/<room_id>/map/scenes/<scene_id>/activate", methods=["POST"])
def activate_map_scene(room_id: str, scene_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    _role, manager = _room_context(info)
    with room_lock(room_dir):
        if not manager:
            return error_response("只有房主或房管可以切换场景。", 403, code="map.manager_only")
        if not scene_store.activate(room_dir, scene_id):
            return error_response("场景不存在或尚未完整保存。", 404, code="map.scene_not_found")
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
    _broadcast(room_id, state)
    return success_response(
        {
            "map": state,
            "scenes": scene_store.list_scenes(room_dir),
            "scene_id": state["scene_id"],
            "can_manage": True,
        },
        "已切换场景。",
    )


@bp.route("/rooms/<room_id>/tokens", methods=["POST"])
def add_token(room_id: str):
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = _write_payload()
    _role, manager = _room_context(info)
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
        token = next((item for item in state["tokens"] if str(item.get("id")) == str(token_id)), None)
        if token is None:
            return _invalid_request(
                "棋子不存在。",
                "map.token_not_found",
                revision=state["revision"],
                http_status=404,
            )
        owner_user_id = str(token.get("owner_user_id") or "")
        if not manager:
            # 房间级开关：KP 关闭后玩家的移动请求在服务端直接拒绝，不能只靠前端
            # 隐藏拖拽。
            if not _map_permission(room_dir):
                return _rejected_request(
                    "KP 已关闭「玩家可移动自己的棋子」，请联系房主或房管移动。",
                    "map.player_move_disabled",
                    revision=state["revision"],
                )
            if owner_user_id != viewer_id:
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
        rejection = _move_target_rejection(
            state, token, cell_x, cell_y, manager=manager
        )
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
        from_cell = _cell_of(token, grid, offset_x, offset_y)
        _set_token_cell(token, cell_x, cell_y, fraction_x, fraction_y, grid, offset_x, offset_y)
        state = _save_map(
            room_dir,
            state,
            {"request_id": request_id, "action": "token.move", "token_id": token["id"]},
        )
        view, fog_view, _visible = _observer_scope(state, room_dir, viewer_id, manager)
    _notify_area_move(room_dir, token, from_cell, (cell_x, cell_y))
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
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
    """房间成员可读取底图资源。

    已知风险（安全审查 M-2）：迷雾开启时底图仍按「该房间公开素材」处理，接口
    不做按视野裁剪；迷雾只隐藏棋子/墙体/光源。若后续要求底图保密，需要在此
    按观察者探索区域返回裁剪图或遮罩合成图。
    """
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
    with room_lock(room_dir):
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


def _segment_cells(
    start: tuple[int, int],
    end: tuple[int, int],
    grid: int,
    offset_x: int,
    offset_y: int,
) -> set[tuple[int, int]]:
    """线段经过的格集合（半格采样，与视线/移动校验口径一致）。"""
    step = max(1.0, grid * 0.5)
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(end[0]), float(end[1])
    length = math.hypot(bx - ax, by - ay)
    count = max(1, int(math.ceil(length / step)))
    cells: set[tuple[int, int]] = set()
    for sample in range(count + 1):
        ratio = sample / count
        x = ax + (bx - ax) * ratio
        y = ay + (by - ay) * ratio
        cells.add(
            (
                int(math.floor((x - offset_x) / grid + 1e-9)),
                int(math.floor((y - offset_y) / grid + 1e-9)),
            )
        )
    return cells


@bp.route("/rooms/<room_id>/spatial/line", methods=["POST"])
def spatial_line(room_id: str):
    """视线或移动阻挡：起点到终点是否被墙体挡住。

    迷雾开启时，非管理者只能查询自己当前看得见的范围：起点必须可见，
    整条线段也必须落在可见格内。范围外返回「无法判定」且不下发墙体 id，
    避免用逐点探测还原整张地图与暗门位置。KP / 房管不受此限制。
    """
    room_dir, info, denied = _map_room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "map.invalid_payload")
    with room_lock(room_dir):
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
    _role, manager = _room_context(info)
    fog = state.get("fog") or {}
    restricted = bool(fog.get("enabled")) and not manager
    distance = spatial.cell_distance((start[2], start[3]), (end[2], end[3]))
    from_point = {"cell_x": start[2], "cell_y": start[3], "x": start[0], "y": start[1]}
    to_point = {"cell_x": end[2], "cell_y": end[3], "x": end[0], "y": end[1]}
    if restricted:
        viewer_id = str(session.get("user_id") or "")
        _view, _fog_payload, visible = _observer_scope(
            state, room_dir, viewer_id, manager
        )
        if (start[2], start[3]) not in visible:
            return error_response(
                "起点不在你的视野内，无法探测这条线段。",
                403,
                "map.line_out_of_sight",
            )
        grid, offset_x, offset_y = _grid_of(state)
        crossed = _segment_cells(
            (start[0], start[1]), (end[0], end[1]), grid, offset_x, offset_y
        )
        if any(cell not in visible for cell in crossed):
            return success_response(
                {
                    "mode": mode,
                    "blocked": None,
                    "blocked_by": [],
                    "blocked_by_count": 0,
                    "resolvable": False,
                    "reason": "线段超出你的可见范围，无法判定",
                    "distance_cells": distance,
                    "distance_ft": spatial.cells_to_feet(distance, FEET_PER_CELL),
                    "from": from_point,
                    "to": to_point,
                },
                "Line unresolved",
            )
    blockers = spatial.segment_blockers(
        state.get("walls") or [],
        (start[0], start[1]),
        (end[0], end[1]),
        mode=mode,
    )
    data: dict[str, Any] = {
        "mode": mode,
        "blocked": bool(blockers),
        "blocked_by": [] if restricted else blockers,
        "resolvable": True,
        "distance_cells": distance,
        "distance_ft": spatial.cells_to_feet(distance, FEET_PER_CELL),
        "from": from_point,
        "to": to_point,
    }
    if restricted:
        # 玩家只拿是否存在阻挡，不下发墙体 id，避免被逐条枚举。
        data["blocked_by_count"] = len(blockers)
    return success_response(data, "Line resolved")


def _path_cells(
    from_cell: tuple[int, int], to_cell: tuple[int, int]
) -> list[tuple[int, int]]:
    """起终点之间的直线格序列（不含起点、含终点）。"""
    dx = int(to_cell[0]) - int(from_cell[0])
    dy = int(to_cell[1]) - int(from_cell[1])
    steps = max(abs(dx), abs(dy))
    if steps <= 0:
        return []
    return [
        (
            int(round(from_cell[0] + dx * index / steps)),
            int(round(from_cell[1] + dy * index / steps)),
        )
        for index in range(1, steps + 1)
    ]


_DIRECTIONS = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
    "northeast": (1, -1),
    "northwest": (-1, -1),
    "southeast": (1, 1),
    "southwest": (-1, 1),
}


def _direction_target(
    room_dir: Any, token_ref: str, direction: str, steps: int
) -> tuple[int, int] | None:
    delta = _DIRECTIONS.get(str(direction or "").strip().lower())
    if delta is None or steps <= 0:
        return None
    try:
        state = _load_map(room_dir)
    except Exception:  # noqa: BLE001
        return None
    token = _find_token(state, token_ref)
    if token is None:
        return None
    grid, offset_x, offset_y = _grid_of(state)
    cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
    return (cell_x + delta[0] * steps, cell_y + delta[1] * steps)


def _scenarios_dir(value: Any) -> Any:
    if value:
        return value
    try:
        from flask import current_app

        return current_app.config.get("SCENARIOS_DIR")
    except Exception:  # noqa: BLE001 - 无应用上下文
        return None


def _resolve_poi_after_move(
    room_dir: Any,
    info: dict[str, Any],
    area_id: str,
    token: dict[str, Any],
    from_cell: tuple[int, int],
    to_cell: tuple[int, int],
    scenarios_dir: Any,
) -> dict[str, Any]:
    """把格子移动折算成兴趣点事件；旧剧本没有 POI 层时返回空结果。"""
    if not area_id:
        return {}
    scenarios_dir = _scenarios_dir(scenarios_dir)
    if not scenarios_dir:
        return {}
    try:
        from trpg_server.agents.narrative.poi_runtime import resolve_after_move

        return resolve_after_move(
            room_dir,
            area_id=area_id,
            token=token,
            from_cell=from_cell,
            to_cell=to_cell,
            path_cells=_path_cells(from_cell, to_cell),
            room_info=info,
            scenarios_dir=scenarios_dir,
        )
    except Exception:  # noqa: BLE001 - 兴趣点解析失败不能回滚棋子移动
        logger.warning("veil_map_poi_resolve_failed", exc_info=True)
        return {}


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
    request_id: str = "",
) -> dict[str, Any]:
    """KP/AI 移动棋子：权限、边界、占位与战斗预算都走同一套插件规则。"""
    if not str(room_id or "").strip() or room_dir is None:
        return {"ok": False, "error": "缺少房间上下文，无法移动棋子。"}
    if _agent_ruleset_id(info, scenarios_dir) != "dnd5e":
        return {"ok": False, "error": "当前房间的规则集不支持地图棋子移动。"}
    with room_lock(room_dir):
        state = _load_map(room_dir)
        _reconcile_tokens(state, info)
        duplicate = _duplicate_record(state, request_id) if request_id else None
        if duplicate is not None:
            return {
                "ok": True,
                "duplicate": True,
                "request_id": request_id,
                "revision": state.get("revision"),
                "record": duplicate,
                "message": "同一移动请求已执行过，本次不重复扣预算。",
            }
        token = _find_token(state, token_ref)
        if token is None:
            return {"ok": False, "error": f"没有找到棋子「{token_ref}」。"}
        allowed, denied = _agent_can_move(info, token, user_id, agent_id, room_dir)
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
        state = _save_map(
            room_dir,
            state,
            (
                {
                    "request_id": request_id,
                    "action": "token.move",
                    "token_id": str(token.get("id") or ""),
                }
                if request_id
                else None
            ),
        )
        area_id = str(state.get("scene_id") or "")

    _notify_area_move(
        room_dir, token, (from_cell_x, from_cell_y), (to_cell_x, to_cell_y)
    )
    poi_result = _resolve_poi_after_move(
        room_dir,
        info,
        area_id,
        token,
        (from_cell_x, from_cell_y),
        (to_cell_x, to_cell_y),
        scenarios_dir,
    )
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
        "area_id": area_id,
        "poi_events": poi_result.get("events") or [],
        "encounter_ready": bool(poi_result.get("encounter_ready")),
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
        "area_id": area_id,
        "poi_events": move["poi_events"],
        "encounter_ready": move["encounter_ready"],
        "request_id": request_id or None,
    }


def _tool_context(context: Any) -> tuple[Any, str, dict[str, Any]]:
    room_dir = getattr(context, "room_dir", None)
    room_id = str(getattr(context, "room_id", "") or "")
    info_fn = getattr(context, "room_info", None)
    info = info_fn() if callable(info_fn) else {}
    if not isinstance(info, dict) or not info:
        info = read_json(room_dir / "info.json", default={}) if room_dir else {}
    return room_dir, room_id, info


def _target_from_arguments(
    arguments: dict[str, Any], room_dir: Any, token_ref: str
) -> tuple[int, int] | None:
    if arguments.get("cell_x") is not None or arguments.get("cell_y") is not None:
        try:
            return (int(arguments.get("cell_x")), int(arguments.get("cell_y")))
        except (TypeError, ValueError):
            return None
    direction = str(arguments.get("direction") or "").strip()
    if direction:
        try:
            steps = max(1, min(20, int(arguments.get("steps") or 1)))
        except (TypeError, ValueError):
            steps = 1
        return _direction_target(room_dir, token_ref, direction, steps)
    return None


def _move_token_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        room_dir, room_id, info = _tool_context(context)
        token_ref = str(arguments.get("token") or "").strip()
        if not token_ref:
            return {"ok": False, "error": "请提供要移动的棋子 id 或名称。"}
        target = _target_from_arguments(arguments, room_dir, token_ref)
        if target is None:
            return {
                "ok": False,
                "error": "请提供目标格坐标（cell_x / cell_y），或方向 direction + steps。",
                "code": "map.invalid_target",
            }
        return _move_token_agent(
            room_dir,
            info,
            room_id=room_id,
            user_id=str(getattr(context, "user_id", "") or ""),
            agent_id=str(getattr(context, "agent_id", "") or ""),
            token_ref=token_ref,
            cell_x=target[0],
            cell_y=target[1],
            reason=str(arguments.get("reason") or "").strip()[:MAX_REASON],
            scenarios_dir=getattr(context, "scenarios_dir", None),
            request_id=str(arguments.get("request_id") or "").strip()[:64],
        )
    except Exception:  # noqa: BLE001 - 工具异常只影响本次调用
        logger.warning("veil_map_agent_move_failed", exc_info=True)
        return {"ok": False, "error": "地图移动失败，请稍后重试。"}


def _get_position_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        room_dir, _room_id, info = _tool_context(context)
        state = _load_map(room_dir)
        user_id = str(getattr(context, "user_id", "") or "")
        token = _find_token(
            state,
            arguments.get("token")
            or (f"member-{user_id}" if user_id else "")
            or next(
                (
                    item.get("id")
                    for item in state.get("tokens") or []
                    if str(item.get("kind") or "") == "pc"
                ),
                "",
            ),
        )
        if token is None:
            return {"ok": False, "error": "地图上没有可用的棋子。", "code": "map.token_missing"}
        grid, offset_x, offset_y = _grid_of(state)
        cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
        area_id = scene_store.active_scene_id(room_dir) or str(state.get("scene_id") or "")
        poi_id, poi_label = "", ""
        bundle = _narrative_bundle(info)
        if bundle is not None:
            from trpg_server.agents.narrative.mapgraph import (
                poi_contains_cell,
                poi_label as _poi_label,
                poi_nodes,
            )

            for node in poi_nodes(bundle, area_id):
                if poi_contains_cell(node, (cell_x, cell_y)):
                    poi_id, poi_label = node.id, _poi_label(node)
                    break
        party = []
        for item in state.get("tokens") or []:
            if str(item.get("kind") or "") != "pc":
                continue
            item_x, item_y = _cell_of(item, grid, offset_x, offset_y)
            party.append(
                {
                    "token_id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "cell_x": item_x,
                    "cell_y": item_y,
                }
            )
        return {
            "ok": True,
            "area_id": area_id,
            "scene_id": area_id,
            "cell": {"x": cell_x, "y": cell_y},
            "position": {
                "kind": "poi" if poi_id else "open",
                "poi_id": poi_id,
                "label": poi_label,
            },
            "token": {
                "token_id": str(token.get("id") or ""),
                "name": str(token.get("name") or ""),
            },
            "party": party,
            "bounds_cells": _as_int(state.get("bounds_cells"), BOUNDS_DEFAULT),
            "grid_ft": FEET_PER_CELL,
        }
    except Exception:  # noqa: BLE001
        logger.warning("veil_map_get_position_failed", exc_info=True)
        return {"ok": False, "error": "读取地图位置失败，请稍后重试。"}


def _inspect_area_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        room_dir, _room_id, info = _tool_context(context)
        state = _load_map(room_dir)
        user_id = str(getattr(context, "user_id", "") or "")
        token = _find_token(state, arguments.get("token") or f"member-{user_id}")
        if token is None:
            return {"ok": False, "error": "地图上没有可用的棋子。", "code": "map.token_missing"}
        grid, offset_x, offset_y = _grid_of(state)
        cell = _cell_of(token, grid, offset_x, offset_y)
        area_id = scene_store.active_scene_id(room_dir) or str(state.get("scene_id") or "")
        bundle = _narrative_bundle(info)
        if bundle is None:
            return {"ok": False, "error": "当前房间没有叙事图。", "code": "map.no_bundle"}
        try:
            radius = max(0, min(12, int(arguments.get("radius_cells") or 3)))
        except (TypeError, ValueError):
            radius = 3
        from trpg_server.agents.narrative import mapgraph

        try:
            from trpg_server.agents.narrative.state_store import load_narrative_state

            narrative = load_narrative_state(room_dir)
        except Exception:  # noqa: BLE001
            narrative = {}
        brief = mapgraph.area_brief(bundle, narrative, area_id)
        if not brief:
            return {"ok": False, "error": "当前位置没有所属地图。", "code": "map.area_missing"}
        is_manager = str(getattr(context, "agent_id", "") or "").strip().lower() in DM_AGENT_IDS
        nearby = []
        for node in mapgraph.pois_within(bundle, area_id, cell, radius):
            distance = mapgraph.poi_distance_cells(node, cell)
            discovered = mapgraph.poi_discovered(narrative, node)
            if not is_manager and not discovered:
                continue
            nearby.append(
                {
                    "poi_id": node.id,
                    "label": mapgraph.poi_label(node),
                    "distance_cells": distance,
                    "discovered": discovered,
                }
            )
        nearby.sort(key=lambda item: item.get("distance_cells") or 0)
        return {
            "ok": True,
            "area_id": area_id,
            "cell": {"x": cell[0], "y": cell[1]},
            "environment": brief.get("environment") or {},
            "exits": brief.get("exits") or [],
            "nearby_pois": nearby,
            "warnings": [],
        }
    except Exception:  # noqa: BLE001
        logger.warning("veil_map_inspect_area_failed", exc_info=True)
        return {"ok": False, "error": "读取区域信息失败，请稍后重试。"}


def _preview_move_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        room_dir, _room_id, info = _tool_context(context)
        token_ref = str(arguments.get("token") or "").strip()
        if not token_ref:
            return {"ok": False, "error": "请提供要试探移动的棋子。"}
        target = _target_from_arguments(arguments, room_dir, token_ref)
        if target is None:
            return {"ok": False, "error": "请提供目标格或方向。", "code": "map.invalid_target"}
        state = _load_map(room_dir)
        token = _find_token(state, token_ref)
        if token is None:
            return {"ok": False, "error": f"没有找到棋子「{token_ref}」。"}
        grid, offset_x, offset_y = _grid_of(state)
        from_cell = _cell_of(token, grid, offset_x, offset_y)
        rejection = _move_target_rejection(state, token, target[0], target[1])
        distance = max(abs(target[0] - from_cell[0]), abs(target[1] - from_cell[1]))
        payload: dict[str, Any] = {
            "ok": True,
            "allowed": rejection is None,
            "from": {"cell_x": from_cell[0], "cell_y": from_cell[1]},
            "to": {"cell_x": target[0], "cell_y": target[1]},
            "distance_cells": distance,
            "distance_ft": spatial.cells_to_feet(distance, FEET_PER_CELL),
            "area_id": scene_store.active_scene_id(room_dir) or str(state.get("scene_id") or ""),
            "poi_on_path": [],
            "encounter_ready": False,
        }
        if rejection is not None:
            payload["code"], payload["error"] = rejection
        bundle = _narrative_bundle(info)
        if bundle is not None:
            try:
                from trpg_server.agents.narrative.poi_runtime import pois_on_path

                poi_ids = pois_on_path(
                    room_dir,
                    info,
                    _scenarios_dir(getattr(context, "scenarios_dir", None)),
                    cells=_path_cells(from_cell, target),
                    to_cell=target,
                    from_cell=from_cell,
                )
            except Exception:  # noqa: BLE001
                poi_ids = []
            payload["poi_on_path"] = [
                {"poi_id": node.id, "label": node.title or node.id}
                for node in (bundle.node(poi_id) for poi_id in poi_ids)
                if node is not None
            ]
        return payload
    except Exception:  # noqa: BLE001
        logger.warning("veil_map_preview_move_failed", exc_info=True)
        return {"ok": False, "error": "试探移动失败，请稍后重试。"}


MOVE_TOKEN_TOOL = AgentTool(
    name="veil-map.move_token",
    description=(
        "移动地图上的一枚棋子。战斗中必须由当前行动者发起，并按移动距离消耗 movement "
        "预算；探索阶段不消耗行动经济，但仍受地图边界、占位与墙体限制。KP 可以移动任意"
        "棋子，AI 玩家只能移动自己绑定的棋子。可用 cell_x / cell_y 指定目标格（可为负），"
        "或用 direction（north/south/east/west 及斜向）+ steps 相对移动；reason 用一句短语"
        "说明意图。移动成功会返回 poi_events（碰到兴趣点时的事件摘要），需要内容时再调用"
        "narrative.get_poi。request_id 相同则幂等返回，不重复扣预算。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "棋子 id 或名称"},
            "cell_x": {"type": "integer", "description": "目标格列坐标（可为负）"},
            "cell_y": {"type": "integer", "description": "目标格行坐标（可为负）"},
            "direction": {
                "type": "string",
                "enum": sorted(_DIRECTIONS),
                "description": "相对方向（与 steps 配合，可替代绝对坐标）",
            },
            "steps": {"type": "integer", "minimum": 1, "maximum": 20, "description": "按方向移动的格数"},
            "reason": {"type": "string", "description": "给玩家的简短移动说明（可选）"},
            "request_id": {"type": "string", "description": "可选：幂等键"},
        },
        "required": ["token"],
    },
    handler=_move_token_tool,
    rulesets=("dnd5e",),
)

GET_POSITION_TOOL = AgentTool(
    name="veil-map.get_position",
    description=(
        "读取当前棋子所在的格坐标与所属地图，以及队伍各棋子的位置。"
        "位置是否落在兴趣点里由服务端判定；不要根据地图图片自行推测坐标。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "可选：棋子 id 或名称，默认发起者自己的棋子。"},
        },
        "required": [],
    },
    handler=_get_position_tool,
    rulesets=("dnd5e",),
)

INSPECT_AREA_TOOL = AgentTool(
    name="veil-map.inspect_area",
    description=(
        "按当前棋子位置探测周围环境：光照/地形等区域特征、出口、半径内可达的兴趣点。"
        "只返回观察者已看到或 DM 可见的兴趣点；不要向玩家描述未发现区域。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "可选：以哪个棋子为观察者。"},
            "radius_cells": {"type": "integer", "minimum": 0, "maximum": 12, "description": "探测半径（格），默认 3。"},
        },
        "required": [],
    },
    handler=_inspect_area_tool,
    rulesets=("dnd5e",),
)

PREVIEW_MOVE_TOOL = AgentTool(
    name="veil-map.preview_move",
    description=(
        "试探性移动：只做边界、占位与墙体校验并返回路径上的兴趣点，不改变任何状态。"
        "返回 allowed=false 时必须换路线或放弃，不得假装已经移动。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "棋子 id 或名称"},
            "cell_x": {"type": "integer", "description": "目标格列坐标"},
            "cell_y": {"type": "integer", "description": "目标格行坐标"},
            "direction": {"type": "string", "enum": sorted(_DIRECTIONS)},
            "steps": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["token"],
    },
    handler=_preview_move_tool,
    rulesets=("dnd5e",),
)


def _action_line_guard(payload: dict[str, Any]) -> dict[str, Any]:
    """给服务端行动工作流提供墙体/视线校验。

    只看同时有地图棋子的施法者与目标；找不到棋子时不做臆测，交给规则集
    自己的射程校验。退化时在 ``notes`` 里明确标注，让 KP 知道本次是按纯
    射程判的。门开着按 ``wall_blocks`` 的既有语义放行。
    """
    skipped = "本次未做视线与墙体校验"
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
        return {"notes": [f"{skipped}（地图不可用）"]}
    actor_name = str(payload.get("actor") or "施法者")
    if not tokens:
        return {"notes": [f"{skipped}（地图上没有棋子）"]}

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
            logger.debug("veil_map_token_center_failed", exc_info=True)
            return None

    actor_token = find_token(payload.get("actor"))
    if actor_token is None:
        return {"notes": [f"{skipped}（{actor_name} 没有绑定棋子）"]}
    actor_point = center(actor_token)
    if actor_point is None:
        return {"notes": [f"{skipped}（{actor_name} 的棋子位置无效）"]}
    if not walls:
        # 没有墙体时既没有可阻挡的线，也没有需要校验的墙；不算退化。
        return {}
    actor_name = str(actor_token.get("name") or payload.get("actor") or "施法者")
    errors: list[str] = []
    notes: list[str] = []
    for target_name in payload.get("targets") or []:
        target_token = find_token(target_name)
        if target_token is None:
            notes.append(f"{skipped}（{target_name} 没有绑定棋子）")
            continue
        if target_token is actor_token:
            continue
        target_point = center(target_token)
        if target_point is None:
            notes.append(f"{skipped}（{target_name} 的棋子位置无效）")
            continue
        blockers = spatial.segment_blockers(
            walls, actor_point, target_point, mode="sight"
        )
        if blockers:
            errors.append(f"{actor_name} 与 {target_name} 之间被墙体或关闭的门阻挡")
    return {"errors": errors, "notes": notes}


def _area_position_provider(room_dir: Any, name: str) -> dict[str, Any] | None:
    """给核心区域引擎提供棋子格坐标；找不到棋子时返回 None。"""
    if room_dir is None:
        return None
    try:
        state = _load_map(room_dir)
    except Exception:  # noqa: BLE001
        logger.warning("veil_map_area_position_load_failed room=%s", room_dir, exc_info=True)
        return None
    token = _find_token(state, name)
    if token is None:
        return None
    grid, offset_x, offset_y = _grid_of(state)
    cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
    width, height = _footprint(token)
    return {
        "cell_x": cell_x,
        "cell_y": cell_y,
        "w": width,
        "h": height,
        "grid_ft": FEET_PER_CELL,
    }


def _area_scene_provider(room_dir: Any) -> str:
    """给核心区域引擎提供当前场景 id，避免核心直接读取插件私有文件。"""
    try:
        return scene_store.active_scene_id(room_dir) or ""
    except Exception:  # noqa: BLE001 - 场景损坏时退回核心默认场景
        logger.warning("veil_map_scene_provider_failed room=%s", room_dir, exc_info=True)
        return ""


def _notify_area_move(
    room_dir: Any,
    token: dict[str, Any],
    from_cell: tuple[int, int],
    to_cell: tuple[int, int],
) -> None:
    """移动成功后通知区域引擎；离开区域不触发。"""
    try:
        from trpg_server.workflows import areas as area_store

        combat = load_room_state(room_dir).get("combat")
        round_number = int((combat or {}).get("round") or 0) if isinstance(combat, dict) and combat.get("active") else 0
        actor = str(token.get("combatant") or token.get("name") or "")
        area_store.notify_move(
            room_dir,
            actor,
            from_cell,
            to_cell,
            round_number,
            size=_footprint(token),
        )
    except Exception:  # noqa: BLE001 - 区域触发失败不能回滚移动
        logger.warning("veil_map_area_move_trigger_failed", exc_info=True)


def _import_external_scene(
    room_dir: Path, info: dict[str, Any], scene: dict[str, Any], *, activate: bool = False
) -> dict[str, Any]:
    """把外部转换器产出的场景写成 veil-map 场景（当前供 fvtt-world-import 调用）。

    载荷字段（全部图片像素坐标，转换器不感知世界原点）::

        scene_id/title/active          # active 只作标记，激活由 activate 控制
        grid{size, origin_x, origin_y}  # 图内网格原点（shift/offset 直供）
        image{name, bytes}              # 背景图，走 media 校验后落房间资产区
        walls[]/lights[]/tokens[]       # 见 convert 端的稳定字段
        ambient{enabled, color, intensity, darkness, baked}
        warnings[]                      # 转换警告，进 background_meta.import

    校验与归一化全部复用本插件现有路径：media.inspect_image（尺寸/体积/魔数）、
    _normalize_walls/_lights/_ambient（上限与字段裁剪）、_save_map（分片代际
    + revision + 迷雾重算）。图片中心换算一次完成，转换器不做第二套几何。
    """
    payload_grid = scene.get("grid") if isinstance(scene.get("grid"), dict) else {}
    grid = _as_int(payload_grid.get("size"), 0)
    if grid < MIN_GRID or grid > MAX_GRID:
        return {
            "ok": False,
            "code": "map.import_grid",
            "error": f"网格尺寸（{grid}）不在 {MIN_GRID}–{MAX_GRID} 像素范围内。",
        }
    image = scene.get("image") if isinstance(scene.get("image"), dict) else {}
    raw = image.get("bytes") or b""
    display_name = str(image.get("name") or "外部地图")[:120]
    meta = media.inspect_image(raw, display_name)
    width, height = int(meta["width"]), int(meta["height"])
    scene_id = scene_store.safe_scene_id(scene.get("scene_id"))
    if not scene_id:
        return {"ok": False, "code": "map.import_scene_id", "error": "场景 id 不合法。"}

    origin_x = _fraction(payload_grid.get("origin_x"))
    origin_y = _fraction(payload_grid.get("origin_y"))
    center_x, center_y = width / 2.0, height / 2.0

    def _shift_x(value: Any) -> int:
        return int(round(_as_int(value, 0) - center_x))

    def _shift_y(value: Any) -> int:
        return int(round(_as_int(value, 0) - center_y))

    walls = [
        {
            "id": item.get("id"),
            "kind": item.get("kind"),
            "state": item.get("state"),
            "blocks_sight": item.get("blocks_sight"),
            "blocks_movement": item.get("blocks_movement"),
            "blocks_light": item.get("blocks_light"),
            "points": [
                {"x": _shift_x(point.get("x")), "y": _shift_y(point.get("y"))}
                for point in item.get("points") or []
                if isinstance(point, dict)
            ],
        }
        for item in scene.get("walls") or []
        if isinstance(item, dict)
    ]
    lights = [
        {
            "id": item.get("id"),
            "x": _shift_x(item.get("x")),
            "y": _shift_y(item.get("y")),
            "range_cells": item.get("range_cells"),
            "bright_cells": item.get("bright_cells"),
            "color": item.get("color"),
            "intensity": item.get("intensity"),
            "angle": item.get("angle"),
            "enabled": item.get("enabled"),
        }
        for item in scene.get("lights") or []
        if isinstance(item, dict)
    ]
    tokens = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "x": _shift_x(item.get("x")),
            "y": _shift_y(item.get("y")),
            "w": item.get("w"),
            "h": item.get("h"),
            "kind": item.get("kind") or "npc",
            "color": item.get("color"),
        }
        for item in scene.get("tokens") or []
        if isinstance(item, dict)
    ]
    scene_warnings = [str(text)[:200] for text in (scene.get("warnings") or []) if str(text).strip()]
    asset_dir = _map_assets_dir(room_dir)
    stored_name = f"{uuid4().hex}.{meta['suffix']}"
    asset_path = asset_dir / stored_name
    room_id = Path(room_dir).name

    with room_lock(room_dir):
        exists = scene_store.has_scene(room_dir, scene_id)
        if exists:
            loaded = scene_store.load_scene(room_dir, scene_id)
            state = _normalize_map(loaded or {}) if loaded else _default_map()
        else:
            state = _default_map()
        state["scene_id"] = scene_id
        state["title"] = str(scene.get("title") or scene_id)[:80]
        state["scene_binding"] = {}
        try:
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(raw)
            state["background"] = (
                f"/api/plugins/veil-map/rooms/{room_id}/assets/maps/{stored_name}"
            )
            state["background_meta"] = _normalize_background_meta(
                {
                    "kind": "external",
                    "format": str(scene.get("format") or "external")[:24],
                    "name": display_name,
                    "url": state["background"],
                    "width": width,
                    "height": height,
                    "bytes": int(meta["size"]),
                    "uploaded_at": _now(),
                    "import": {
                        "format": str(scene.get("format") or "external")[:24],
                        "walls": len(walls),
                        "doors": sum(
                            1
                            for wall in walls
                            if str(wall.get("kind")) in {"door", "window"}
                        ),
                        "lights": len(lights),
                        "needs_review": bool(scene_warnings),
                        "warnings": scene_warnings[:8],
                    },
                }
            )
            state["grid_size"] = grid
            state["grid_offset"] = {
                "x": _wrap_grid_offset(origin_x - center_x, grid),
                "y": _wrap_grid_offset(origin_y - center_y, grid),
            }
            cells_across = max(1, int(round(width / grid)))
            cells_down = max(1, int(round(height / grid)))
            state["calibration"] = {
                "cells_across": cells_across,
                "cells_down": cells_down,
                "offset_x": int(round(origin_x)) % grid,
                "offset_y": int(round(origin_y)) % grid,
            }
            state["bounds_cells"] = _clamp(
                max(BOUNDS_DEFAULT, cells_across, cells_down), MIN_BOUNDS, MAX_BOUNDS
            )
            state["walls"] = _normalize_walls(walls)
            state["lights"] = _normalize_lights(lights)
            state["ambient_light"] = _normalize_ambient(scene.get("ambient") or {})
            state["tokens"] = tokens
            saved = _save_map(room_dir, state, activate=activate)
        except Exception:  # noqa: BLE001 - 写失败必须回滚已落盘的背景资产
            asset_path.unlink(missing_ok=True)
            raise
    if activate:
        _broadcast(room_id, saved)
    return {
        "ok": True,
        "scene_id": scene_id,
        "title": str(saved.get("title") or ""),
        "created": not exists,
        "revision": int(saved.get("revision") or 0),
        "grid_size": grid,
        "grid_offset": saved.get("grid_offset"),
        "bounds_cells": saved.get("bounds_cells"),
        "walls": len(saved.get("walls") or []),
        "lights": len(saved.get("lights") or []),
        "tokens": len(saved.get("tokens") or []),
        "background": {
            "url": str(saved.get("background") or ""),
            "width": width,
            "height": height,
        },
    }


class _MapService:
    """给核心叙事层用的地图能力（避免核心 import 插件包）。"""

    def activate_for_area(
        self,
        room_dir: Any,
        info: dict[str, Any],
        area_id: str,
        entry_poi: str = "",
    ) -> dict[str, Any]:
        try:
            if not _ruleset_enabled(info or {}):
                return {
                    "ok": False,
                    "code": "map.ruleset_disabled",
                    "error": "当前房间的规则集未启用地图。",
                }
            return _activate_area_scene(Path(room_dir), info or {}, area_id, entry_poi)
        except Exception:  # noqa: BLE001
            logger.warning("veil_map_activate_area_failed", exc_info=True)
            return {
                "ok": False,
                "code": "map.scene_unavailable",
                "error": "切换地图场景失败。",
            }

    def deactivate_for_site(self, room_dir: Any, info: dict[str, Any]) -> dict[str, Any]:
        """离开地图进入无地图地点：关闭当前地图场景，棋子保留在原场景。"""
        try:
            if not _ruleset_enabled(info or {}):
                return {"ok": False, "code": "map.ruleset_disabled"}
            scene_store.deactivate(room_dir)
            return {"ok": True, "closed": True}
        except Exception:  # noqa: BLE001
            logger.warning("veil_map_deactivate_failed", exc_info=True)
            return {"ok": False, "code": "map.scene_unavailable"}

    def sync_combat_tokens(
        self, room_dir: Any, info: dict[str, Any], combat: Any
    ) -> dict[str, Any]:
        """供 dnd5e 插件在战斗落盘后同步棋子；无地图时返回降级状态。"""
        try:
            if not _ruleset_enabled(info or {}):
                return {"ok": True, "status": "skipped", "reason": "map_unavailable"}
            if not isinstance(combat, dict) or not combat.get("id"):
                return {"ok": True, "status": "skipped", "reason": "no_combat"}
            return _sync_combat_tokens(Path(room_dir), info or {}, combat)
        except Exception:  # noqa: BLE001 - 棋子同步失败不能影响战斗成立
            logger.warning("veil_map_combat_sync_failed", exc_info=True)
            return {"ok": False, "status": "error", "error": "地图棋子同步失败。"}

    def current_position(self, room_dir: Any) -> dict[str, Any] | None:
        try:
            state = _load_map(room_dir)
        except Exception:  # noqa: BLE001
            return None
        token = next(
            (
                item
                for item in state.get("tokens") or []
                if str(item.get("kind") or "") == "pc"
            ),
            None,
        )
        if token is None:
            return None
        grid, offset_x, offset_y = _grid_of(state)
        cell_x, cell_y = _cell_of(token, grid, offset_x, offset_y)
        return {
            "scene_id": scene_store.active_scene_id(room_dir) or str(state.get("scene_id") or ""),
            "cell": {"x": cell_x, "y": cell_y},
            "token_id": str(token.get("id") or ""),
            "name": str(token.get("name") or ""),
        }

    def import_scene(
        self, room_dir: Any, info: dict[str, Any], scene: dict[str, Any], *, activate: bool = False
    ) -> dict[str, Any]:
        """外部转换器（fvtt-world-import）批量写入一个场景。

        载荷用「背景图片像素坐标」：``grid.origin_x/origin_y`` 是图内网格
        原点，墙/光/棋子坐标也是图片像素。图片中心对齐、格坐标换算与全部
        归一化都在本插件内完成——转换器不需要了解 veil-map 的世界原点约定。
        """
        try:
            if not _ruleset_enabled(info or {}):
                return {"ok": False, "code": "map.ruleset_disabled", "error": "当前房间的规则集未启用地图。"}
            return _import_external_scene(
                Path(room_dir), info or {}, scene if isinstance(scene, dict) else {}, activate=activate
            )
        except media.ImageError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message}
        except Exception:  # noqa: BLE001
            logger.warning("veil_map_import_scene_failed", exc_info=True)
            return {"ok": False, "code": "map.import_failed", "error": "场景写入失败。"}


_MAP_SERVICE = _MapService()
_ACTION_GUARD_REGISTERED = False


def _register_action_guard() -> None:
    global _ACTION_GUARD_REGISTERED
    if _ACTION_GUARD_REGISTERED:
        return
    try:
        from trpg_server.plugins.runtime import register_service
        from trpg_server.workflows import areas as area_store
        from trpg_server.workflows.guards import register_action_guard
    except Exception:  # noqa: BLE001 - 旧核心没有工作流时静默跳过
        return
    register_action_guard(_action_line_guard, critical=True)
    area_store.register_position_provider(_area_position_provider)
    area_store.register_scene_id_provider(_area_scene_provider)
    register_service("veil-map", "map_service", _MAP_SERVICE)
    _ACTION_GUARD_REGISTERED = True


def create_tools() -> list[AgentTool]:
    _register_action_guard()
    return [MOVE_TOKEN_TOOL, GET_POSITION_TOOL, INSPECT_AREA_TOOL, PREVIEW_MOVE_TOOL]


__all__ = [
    "bp",
    "create_tools",
    "MOVE_TOKEN_TOOL",
    "GET_POSITION_TOOL",
    "INSPECT_AREA_TOOL",
    "PREVIEW_MOVE_TOOL",
]

