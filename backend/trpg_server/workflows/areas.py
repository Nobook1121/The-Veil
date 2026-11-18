"""场景级持续区域。

持续区域不是棋子上的临时效果，而是挂在场景上的实体：创建一次、多回合存在，
每个目标每回合最多触发一次。区域本身只描述几何、持续时间、豁免与伤害事实；
真正的判定与伤害仍由规则集插件执行。

存储放在房间目录的 ``scene_areas.json``，按 ``scene_id`` 分组并整体带 revision。
地图插件存在时调用方可以传入当前场景 id；没有地图时使用 ``default``。
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.workflows.store import room_write_lock

logger = logging.getLogger(__name__)

AREAS_FILENAME = "scene_areas.json"
AREAS_SCHEMA = 1
SHAPES = {"sphere", "cube", "cylinder", "cone", "line", "square", "circle"}
TRIGGERS = {"enter", "start_turn", "end_turn", "enter_or_end_turn", "enter_or_start_turn"}
FEET_PER_CELL = 5
# 5e 锥形区域的张角：末端宽度等于长度，半角约 26.57°。
CONE_HALF_SLOPE = 0.5
_DIRECTION_NAMES = {
    "n": (0.0, -1.0),
    "north": (0.0, -1.0),
    "ne": (1.0, -1.0),
    "northeast": (1.0, -1.0),
    "e": (1.0, 0.0),
    "east": (1.0, 0.0),
    "se": (1.0, 1.0),
    "southeast": (1.0, 1.0),
    "s": (0.0, 1.0),
    "south": (0.0, 1.0),
    "sw": (-1.0, 1.0),
    "southwest": (-1.0, 1.0),
    "w": (-1.0, 0.0),
    "west": (-1.0, 0.0),
    "nw": (-1.0, -1.0),
    "northwest": (-1.0, -1.0),
}


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


# 地图插件注册位置查询；没有地图时自动触发直接跳过，不臆测格子。
_POSITION_PROVIDER: Callable[[Any, str], Mapping[str, Any] | None] | None = None
# 地图插件注册当前场景查询；核心不直接读插件私有文件。
_SCENE_ID_PROVIDER: Callable[[Any], str] | None = None
# 行动侧注册真正执行豁免/伤害的回调；区域核心只负责几何、去重与审计。
_TRIGGER_HANDLER: Callable[..., Any] | None = None


def register_position_provider(provider: Callable[[Any, str], Mapping[str, Any] | None]) -> None:
    global _POSITION_PROVIDER
    _POSITION_PROVIDER = provider


def clear_position_provider() -> None:
    global _POSITION_PROVIDER
    _POSITION_PROVIDER = None


def register_scene_id_provider(provider: Callable[[Any], str]) -> None:
    global _SCENE_ID_PROVIDER
    _SCENE_ID_PROVIDER = provider


def clear_scene_id_provider() -> None:
    global _SCENE_ID_PROVIDER
    _SCENE_ID_PROVIDER = None


def register_trigger_handler(handler: Callable[..., Any]) -> None:
    global _TRIGGER_HANDLER
    _TRIGGER_HANDLER = handler


def clear_hooks() -> None:
    global _TRIGGER_HANDLER
    clear_position_provider()
    clear_scene_id_provider()
    _TRIGGER_HANDLER = None


def lookup_position(room_dir: Any, name: str) -> dict[str, Any] | None:
    provider = _POSITION_PROVIDER
    if provider is None:
        return None
    try:
        found = provider(room_dir, str(name or ""))
    except Exception:  # noqa: BLE001 - 地图数据异常时退回无位置
        logger.exception("区域位置查询失败 room=%s name=%s", room_dir, name)
        return None
    return dict(found) if isinstance(found, Mapping) else None


def _direction_vector(area: Mapping[str, Any]) -> tuple[float, float] | None:
    """解析区域朝向，返回单位向量；无法确定时返回 ``None``。

    支持 ``direction`` / ``angle`` 的多种写法：角度（0° 指向 +x，顺时针）、
    罗盘方位字符串、``{"dx": 1, "dy": 0}`` 形式的向量，以及 ``to`` / ``end``
    目标格。地图的 y 轴向下，所以 north 是 ``(0, -1)``。
    """
    raw = area.get("direction", area.get("angle"))
    angle: float | None = None
    vector: tuple[float, float] | None = None
    if isinstance(raw, Mapping):
        for left, right in (("dx", "dy"), ("x", "y")):
            if raw.get(left) is not None and raw.get(right) is not None:
                vector = (_float(raw.get(left)), _float(raw.get(right)))
                break
        if vector is None:
            angle = _float(raw.get("degrees", raw.get("angle")), None)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        vector = (_float(raw[0]), _float(raw[1]))
    elif isinstance(raw, str):
        text = raw.strip().casefold()
        if text in _DIRECTION_NAMES:
            vector = _DIRECTION_NAMES[text]
        else:
            angle = _float(text, None)
    elif raw is not None:
        angle = _float(raw, None)
    if vector is None and angle is not None:
        radians = math.radians(angle)
        vector = (math.cos(radians), math.sin(radians))
    if vector is None:
        origin = area.get("origin") if isinstance(area.get("origin"), Mapping) else None
        end = area.get("to") or area.get("end")
        if origin is not None and isinstance(end, Mapping):
            try:
                vector = (
                    _float(end.get("cell_x", end.get("col"))) - _float(origin.get("cell_x", origin.get("col"))),
                    _float(end.get("cell_y", end.get("row"))) - _float(origin.get("cell_y", origin.get("row"))),
                )
            except (TypeError, ValueError):
                vector = None
    if vector is None:
        return None
    dx, dy = vector
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    return (dx / length, dy / length)


def _float(value: Any, fallback: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _cell_size(position: Mapping[str, Any] | None) -> tuple[int, int]:
    """棋子的格子占位；兼容 ``w/h`` 与 ``width/height``。"""
    position = position or {}
    width = position.get("w", position.get("width_cells", position.get("width", 1)))
    height = position.get("h", position.get("height_cells", position.get("height", 1)))
    return (min(4, max(1, _as_int(width, 1))), min(4, max(1, _as_int(height, 1))))


def cells_of(cell: tuple[Any, Any] | None, size: tuple[int, int] | None = None) -> list[tuple[int, int]]:
    """把锚点格展开成 footprint 覆盖的所有格（锚点为左上角）。"""
    if cell is None:
        return []
    try:
        x = int(cell[0])
        y = int(cell[1])
    except (TypeError, ValueError):
        return []
    width, height = size or (1, 1)
    width = min(4, max(1, int(width)))
    height = min(4, max(1, int(height)))
    return [(x + dx, y + dy) for dx in range(width) for dy in range(height)]


def contains(area: Mapping[str, Any], cell_x: Any, cell_y: Any) -> bool:
    """区域是否覆盖某格；无原点时退回显式 targets 列表。"""
    origin = area.get("origin") if isinstance(area.get("origin"), Mapping) else None
    if origin is None:
        names = {
            _text(name).casefold()
            for name in (area.get("targets") or [])
            if _text(name)
        }
        return bool(names) and _text(area.get("_query_target")).casefold() in names
    try:
        ox = int(origin.get("cell_x", origin.get("col")))
        oy = int(origin.get("cell_y", origin.get("row")))
        x = int(cell_x)
        y = int(cell_y)
    except (TypeError, ValueError):
        return False
    grid_ft = max(1, _as_int(area.get("grid_ft"), FEET_PER_CELL))
    size_ft = max(grid_ft, _as_int(area.get("size_ft"), grid_ft))
    shape = _text(area.get("shape")).lower()
    vx, vy = float(x - ox), float(y - oy)
    if shape in {"sphere", "circle", "cylinder"}:
        # 圆柱在俯视网格上的投影就是圆，二者共用同一判定。
        radius = max(1, math.ceil(size_ft / grid_ft))
        return vx ** 2 + vy ** 2 <= radius ** 2
    if shape in {"cube", "square"}:
        half = max(1, math.ceil(size_ft / grid_ft / 2))
        return abs(vx) <= half and abs(vy) <= half
    direction = _direction_vector(area)
    if shape == "cone":
        if direction is None:
            # 没有朝向时不臆测锥形方向，退化为同尺寸圆形（仍按区域处理）。
            radius = max(1, math.ceil(size_ft / grid_ft))
            return vx ** 2 + vy ** 2 <= radius ** 2
        length = max(1.0, size_ft / grid_ft)
        forward = vx * direction[0] + vy * direction[1]
        lateral = abs(vx * direction[1] - vy * direction[0])
        if forward < -0.5 or forward > length + 0.5:
            return False
        return lateral <= max(0.5, forward * CONE_HALF_SLOPE)
    if shape == "line":
        dx, dy = direction or (1.0, 0.0)
        length = max(1.0, size_ft / grid_ft)
        width_ft = max(grid_ft, _as_int(area.get("width_ft"), grid_ft))
        half_width = max(0.5, width_ft / grid_ft / 2)
        forward = vx * dx + vy * dy
        lateral = abs(vx * dy - vy * dx)
        return -0.5 <= forward <= length + 0.5 and lateral <= half_width
    radius = max(1, math.ceil(size_ft / grid_ft))
    return vx ** 2 + vy ** 2 <= radius ** 2


def _area_covers(area: Mapping[str, Any], target: str, cell_x: Any, cell_y: Any) -> bool:
    if cell_x is None or cell_y is None:
        return False
    probe = {**dict(area), "_query_target": target}
    return contains(probe, cell_x, cell_y)


def _area_covers_cells(area: Mapping[str, Any], target: str, cells: list[tuple[int, int]]) -> bool:
    return any(_area_covers(area, target, cell[0], cell[1]) for cell in cells)


def _trigger_allowed(area: Mapping[str, Any], hook: str) -> bool:
    trigger = _text(area.get("trigger")).lower()
    if hook == "move_enter":
        return trigger in {"enter", "enter_or_end_turn", "enter_or_start_turn"} or not trigger
    if hook == "turn_start":
        return trigger in {"start_turn", "enter_or_start_turn"} or not trigger
    return True


def _failed_result(area: Mapping[str, Any], actor: str, exc: Exception) -> dict[str, Any]:
    """单个区域钩子失败时返回可审计的降级结果，而不是静默跳过。"""
    logger.exception(
        "区域触发失败 area=%s actor=%s", _text(area.get("id")), _text(actor)
    )
    return {
        "error": "区域触发失败，已跳过本次结算",
        "code": "area.trigger_failed",
        "area": _text(area.get("id")),
        "target": _text(actor),
        "degraded": True,
        "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
    }


def _notify(
    room_dir: Any,
    *,
    actor: str,
    cells: list[tuple[int, int]] | None,
    hook: str,
    round_number: int,
    context: Any = None,
) -> list[dict[str, Any]]:
    handler = _TRIGGER_HANDLER
    if handler is None or not _text(actor) or not cells:
        return []
    results: list[dict[str, Any]] = []
    for area in list_areas(room_dir, scene_id=scene_id_of(room_dir)):
        if not _trigger_allowed(area, hook):
            continue
        if not _area_covers_cells(area, actor, cells):
            continue
        try:
            result = handler(
                room_dir,
                area,
                _text(actor),
                int(round_number or 0),
                hook,
                context,
            )
        except Exception as exc:  # noqa: BLE001 - 单个区域失败不能阻断移动/回合
            results.append(_failed_result(area, actor, exc))
            continue
        if isinstance(result, Mapping) and result:
            results.append(dict(result))
    return results


def notify_move(
    room_dir: Any,
    actor: str,
    from_cell: tuple[Any, Any] | None,
    to_cell: tuple[Any, Any] | None,
    round_number: int = 0,
    *,
    hook: str = "move_enter",
    size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """棋子移动后触发“进入”区域；离开或原地移动不触发。

    ``size`` 是棋子的格子占位（宽, 高），锚点为左上角。多格棋子只要 footprint
    的任意一格进入区域就触发，任意一格仍在区域内就不算重新进入。
    """
    if to_cell is None:
        return []
    new_cells = cells_of(to_cell, size)
    old_cells = cells_of(from_cell, size)
    results: list[dict[str, Any]] = []
    handler = _TRIGGER_HANDLER
    for area in list_areas(room_dir, scene_id=scene_id_of(room_dir)):
        if not _trigger_allowed(area, hook):
            continue
        inside_new = _area_covers_cells(area, actor, new_cells)
        inside_old = _area_covers_cells(area, actor, old_cells)
        if not inside_new:
            # 战斗外没有回合号，离开区域时清掉去重标记，允许再次进入时重新触发。
            if inside_old and not _as_int(round_number, 0):
                clear_trigger(room_dir, _text(area.get("id")), actor)
            continue
        if inside_old or handler is None:
            continue
        try:
            result = handler(room_dir, area, _text(actor), int(round_number or 0), hook, None)
        except Exception as exc:  # noqa: BLE001
            results.append(_failed_result(area, actor, exc))
            continue
        if isinstance(result, Mapping) and result:
            results.append(dict(result))
    return results


def notify_turn_start(
    room_dir: Any,
    actor: str,
    round_number: int,
    *,
    context: Any = None,
) -> list[dict[str, Any]]:
    """回合开始时对区域内目标判定一次。"""
    position = lookup_position(room_dir, actor)
    cells = None
    if isinstance(position, Mapping):
        cells = cells_of(
            (position.get("cell_x"), position.get("cell_y")),
            _cell_size(position),
        )
    return _notify(
        room_dir,
        actor=actor,
        cells=cells,
        hook="turn_start",
        round_number=round_number,
        context=context,
    )



def store_path(room_dir: Any) -> Path:
    return Path(room_dir) / AREAS_FILENAME


def _empty() -> dict[str, Any]:
    return {"schema": AREAS_SCHEMA, "revision": 0, "areas": []}


def load(room_dir: Any) -> dict[str, Any]:
    raw = read_json(store_path(room_dir), default={})
    if not isinstance(raw, Mapping):
        return _empty()
    areas = [
        dict(item)
        for item in (raw.get("areas") if isinstance(raw.get("areas"), list) else [])
        if isinstance(item, Mapping) and _text(item.get("id"))
    ]
    return {
        "schema": AREAS_SCHEMA,
        "revision": _as_int(raw.get("revision"), 0),
        "areas": areas,
    }


def save(room_dir: Any, state: Mapping[str, Any]) -> None:
    write_json_atomic(
        store_path(room_dir),
        {
            "schema": AREAS_SCHEMA,
            "revision": _as_int(state.get("revision"), 0),
            "areas": list(state.get("areas") or []),
        },
    )


def scene_id_of(room_dir: Any) -> str:
    provider = _SCENE_ID_PROVIDER
    if provider is not None:
        try:
            scene = _text(provider(room_dir))
            if scene:
                return scene
        except Exception:  # noqa: BLE001 - 场景提供方异常时退回兼容读取
            logger.exception("场景 id 查询失败 room=%s", room_dir)
    # 兼容没有加载地图插件的房间与旧测试；生产路径由插件注册的 provider 提供。
    root = Path(room_dir) / "plugins" / "veil-map" / "scenes"
    pointer = read_json(root / "active.json", default={})
    if isinstance(pointer, Mapping):
        scene = _text(pointer.get("scene_id"))
        if scene:
            return scene
    return "default"


def list_areas(room_dir: Any, *, scene_id: str = "", active_only: bool = True) -> list[dict[str, Any]]:
    state = load(room_dir)
    wanted = _text(scene_id)
    rows = [
        dict(area)
        for area in state["areas"]
        if not wanted or _text(area.get("scene_id")) == wanted
    ]
    if active_only:
        rows = [area for area in rows if not area.get("ended_at")]
    return rows


def create_area(room_dir: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """创建区域实体；几何与持续时间只做基本归一化，不做规则解释。"""
    shape = _text(spec.get("shape") or "sphere").lower()
    if shape not in SHAPES:
        return {"error": f"不支持的区域形状“{shape}”", "code": "area.bad_shape"}
    size = max(0, _as_int(spec.get("size_ft"), 0))
    rounds = max(0, _as_int(spec.get("rounds"), 0))
    duration = spec.get("duration") if isinstance(spec.get("duration"), Mapping) else {}
    if rounds <= 0:
        rounds = _as_int(duration.get("rounds"), 0)
    origin = spec.get("origin") if isinstance(spec.get("origin"), Mapping) else None
    area = {
        "id": f"area-{uuid4().hex[:12]}",
        "scene_id": _text(spec.get("scene_id")) or scene_id_of(room_dir),
        "name": _text(spec.get("name") or spec.get("spell") or "持续区域"),
        "source": _text(spec.get("source")),
        "spell": _text(spec.get("spell")),
        "owner": _text(spec.get("owner")),
        "shape": shape,
        "size_ft": size,
        "width_ft": _as_int(spec.get("width_ft"), 0),
        "origin": dict(origin) if origin else None,
        "grid_ft": max(1, _as_int(spec.get("grid_ft"), FEET_PER_CELL)),
        "targets": [str(name) for name in (spec.get("targets") or []) if str(name or "").strip()],
        "trigger": _text(spec.get("trigger") or "enter_or_end_turn"),
        "save": dict(spec.get("save")) if isinstance(spec.get("save"), Mapping) else None,
        "damage": [dict(row) for row in (spec.get("damage") or []) if isinstance(row, Mapping)],
        "effects": dict(spec.get("effects")) if isinstance(spec.get("effects"), Mapping) else {},
        "duration": {
            "unit": _text(duration.get("unit") or "round"),
            "rounds": rounds,
            "remaining": rounds if rounds else None,
        },
        "concentration": bool(spec.get("concentration")),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_round": _as_int(spec.get("created_round"), 0),
        "triggered": {},
    }
    with room_write_lock(room_dir):
        state = load(room_dir)
        state["areas"].append(area)
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"area": area, "revision": state["revision"]}


def remove_area(room_dir: Any, area_id: str, *, reason: str = "") -> dict[str, Any]:
    area_id = _text(area_id)
    with room_write_lock(room_dir):
        state = load(room_dir)
        kept = [area for area in state["areas"] if _text(area.get("id")) != area_id]
        removed = [area for area in state["areas"] if _text(area.get("id")) == area_id]
        if not removed:
            return {"error": "区域不存在", "code": "area.not_found"}
        if reason:
            for area in removed:
                area["ended_reason"] = reason
        state["areas"] = kept
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"removed": removed[0], "revision": state["revision"]}


def tick_areas(room_dir: Any, *, rounds: int = 1) -> dict[str, Any]:
    """推进持续区域；剩余回合归零时移除并标记结束原因。"""
    step = max(1, int(rounds or 1))
    expired: list[dict[str, Any]] = []
    with room_write_lock(room_dir):
        state = load(room_dir)
        kept: list[dict[str, Any]] = []
        for area in state["areas"]:
            duration = area.get("duration") if isinstance(area.get("duration"), Mapping) else {}
            remaining = duration.get("remaining")
            if remaining is None:
                kept.append(area)
                continue
            remaining = max(0, _as_int(remaining, 0) - step)
            duration = dict(duration)
            duration["remaining"] = remaining
            area["duration"] = duration
            if remaining <= 0:
                area["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                area["ended_reason"] = "duration"
                expired.append(dict(area))
            else:
                kept.append(area)
        state["areas"] = kept
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"expired": expired, "revision": state["revision"]}


def area_spec_from_cast(
    result: Mapping[str, Any],
    *,
    caster: str = "",
    dc: int = 0,
    scene_id: str = "",
    origin: Mapping[str, Any] | None = None,
    grid_ft: int = FEET_PER_CELL,
    targets: list[str] | None = None,
) -> dict[str, Any] | None:
    """从 ``spells.cast`` 结果提取持续区域描述；瞬时法术返回 ``None``。"""
    descriptor = result.get("descriptor") if isinstance(result.get("descriptor"), Mapping) else {}
    area = descriptor.get("area") if isinstance(descriptor.get("area"), Mapping) else {}
    duration = descriptor.get("duration") if isinstance(descriptor.get("duration"), Mapping) else {}
    upcast = result.get("upcast") if isinstance(result.get("upcast"), Mapping) else {}
    damage = [dict(row) for row in upcast.get("damage") or [] if isinstance(row, Mapping)]
    unit = _text(duration.get("unit")).lower()
    if not area or unit in {"", "instant"} or not damage:
        return None
    value = max(1, _as_int(duration.get("value"), 1))
    per_unit = {"round": 1, "minute": 10, "hour": 600, "day": 14400}.get(unit, 10)
    resolution = descriptor.get("resolution") if isinstance(descriptor.get("resolution"), Mapping) else {}
    save = None
    if _text(resolution.get("kind")).lower() == "save":
        save = {
            "ability": _text(resolution.get("ability")) or "dex",
            "dc": max(1, int(dc or 10)),
            "half_on_success": bool(resolution.get("half_on_success")),
        }
    spell_info = result.get("spell") if isinstance(result.get("spell"), Mapping) else {}
    concentration_info = (
        result.get("concentration") if isinstance(result.get("concentration"), Mapping) else {}
    )
    spell_name = _text(spell_info.get("name"))
    return {
        "scene_id": _text(scene_id),
        "name": spell_name or "持续区域",
        "spell": _text(spell_info.get("id")),
        "source": f"{caster}:{spell_name}" if caster else spell_name,
        "owner": _text(caster),
        "shape": _text(area.get("shape")) or "sphere",
        "size_ft": area.get("size_ft") or area.get("radius_ft") or 0,
        "width_ft": area.get("width_ft") or area.get("length_ft") or 0,
        "origin": dict(origin) if origin else None,
        "grid_ft": max(1, int(grid_ft or FEET_PER_CELL)),
        "targets": list(targets or []),
        "trigger": "enter_or_start_turn",
        "save": save,
        "damage": damage,
        "duration": {"unit": "round", "rounds": value * per_unit},
        "concentration": bool(duration.get("concentration")),
        "created_round": _as_int(concentration_info.get("round"), 0),
    }


def trigger(
    room_dir: Any,
    area_id: str,
    target: str,
    *,
    round_number: int,
    apply: Callable[[dict[str, Any], str], Any] | None = None,
    dedupe_key: str = "",
) -> dict[str, Any]:
    """触发一次区域；同一目标同一回合只结算一次。

    去重标记在调用 ``apply`` 之前就在锁内落盘，因此两个并发请求只有一个能拿到
    结算权； ``apply`` 在锁外执行，避免长时间占用房间写锁。战斗外 ``round`` 为 0
    时同样去重，离开区域后由 :func:`clear_trigger` 清除标记，允许再次进入触发。
    """
    area_id = _text(area_id)
    target = _text(target)
    round_number = _as_int(round_number, 0)
    if not target:
        return {"error": "缺少 target", "code": "area.missing_target"}
    key = f"key:{_text(dedupe_key)}" if _text(dedupe_key) else target
    with room_write_lock(room_dir):
        state = load(room_dir)
        area = next((item for item in state["areas"] if _text(item.get("id")) == area_id), None)
        if area is None:
            ended = [
                item for item in load(room_dir)["areas"]
                if _text(item.get("id")) == area_id
            ]
            return {"error": "区域不存在或已结束", "code": "area.not_found", "ended": ended}
        triggered = area.get("triggered") if isinstance(area.get("triggered"), Mapping) else {}
        if _as_int(triggered.get(key), -1) == round_number:
            return {"duplicate": True, "area": dict(area)}
        triggered = dict(triggered)
        triggered[key] = round_number
        area["triggered"] = triggered
        # 先落盘认领，再执行结算：并发调用只会有一个看到“未触发”。
        for item in state["areas"]:
            if _text(item.get("id")) == area_id:
                item["triggered"] = dict(triggered)
                item["last_claimed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    try:
        result = apply(area, target) if callable(apply) else {"area": dict(area), "target": target}
    except Exception as exc:  # noqa: BLE001 - 结算失败不能留下半写状态，但要可审计
        logger.exception("区域结算失败 area=%s target=%s", area_id, target)
        result = {
            "error": "区域结算失败",
            "code": "area.apply_failed",
            "area": area_id,
            "target": target,
            "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    with room_write_lock(room_dir):
        state = load(room_dir)
        for item in state["areas"]:
            if _text(item.get("id")) == area_id:
                item["last_triggered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(result, Mapping) and result.get("error"):
                    item["last_trigger_error"] = _text(result.get("error"))
                break
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"duplicate": False, "area": dict(area), "result": result}


def clear_trigger(room_dir: Any, area_id: str, target: str, *, round_number: int | None = None) -> bool:
    """清除去重标记；``round_number`` 给定时只清除该回合的标记。"""
    area_id = _text(area_id)
    target = _text(target)
    if not area_id or not target:
        return False
    cleared = False
    with room_write_lock(room_dir):
        state = load(room_dir)
        for item in state["areas"]:
            if _text(item.get("id")) != area_id:
                continue
            triggered = item.get("triggered")
            if not isinstance(triggered, Mapping) or target not in triggered:
                break
            if round_number is not None and _as_int(triggered.get(target), -1) != _as_int(round_number, 0):
                break
            updated = dict(triggered)
            updated.pop(target, None)
            item["triggered"] = updated
            cleared = True
            break
        if cleared:
            state["revision"] = _as_int(state.get("revision"), 0) + 1
            save(room_dir, state)
    return cleared


__all__ = [
    "AREAS_FILENAME",
    "SHAPES",
    "TRIGGERS",
    "cells_of",
    "clear_hooks",
    "clear_position_provider",
    "clear_scene_id_provider",
    "clear_trigger",
    "contains",
    "create_area",
    "list_areas",
    "load",
    "lookup_position",
    "notify_move",
    "notify_turn_start",
    "register_position_provider",
    "register_scene_id_provider",
    "register_trigger_handler",
    "remove_area",
    "scene_id_of",
    "tick_areas",
    "trigger",
]
