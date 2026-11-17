"""veil-map 视线与可见性计算。

本模块只做几何：给定观察者世界坐标、挡视线的墙体折线、视野半径与地图边界，
用角度扫描（angular sweep）求出可见多边形，再栅格化为可见格集合。权限裁剪、
迷雾记录与推送都留在 entry.py，本模块不读写房间文件。

坐标与 ``spatial.py`` 一致：世界像素、图片中心为原点；格子 ``(cx, cy)`` 的
锚点是 ``grid_offset + (cx, cy) * grid_size``。观察者取棋子 footprint 的中心。

性能
----
墙体先按格分桶（每条线段登记到经过的格），求交时只取视野圆盘附近的桶；
结果按 ``(观察者, 墙体版本, 光源版本, 视野, 网格, 边界)`` 缓存。墙体/光源没变
时同一观察者反复查询直接命中缓存，不逐帧重算。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Sequence

_EPSILON = 1e-9
_ANGLE_EPSILON = 1e-4
_CACHE_LIMIT = 512
_BUCKET_LIMIT = 64
_ARC_STEPS = 72

_CACHE: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_BUCKETS: "OrderedDict[tuple, tuple[dict[tuple[int, int], list[int]], list[tuple[tuple[float, float], tuple[float, float]]]]]" = OrderedDict()


# ---------------------------------------------------------------------------
# 版本与缓存
# ---------------------------------------------------------------------------


def state_version(items: Any) -> str:
    """墙体/光源的内容版本：内容不变则版本不变。"""
    text = json.dumps(
        items or [],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def reset_cache() -> None:
    _CACHE.clear()
    _BUCKETS.clear()


def cache_size() -> int:
    return len(_CACHE)


def _remember(cache: "OrderedDict", key: Any, value: Any, limit: int) -> Any:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


# ---------------------------------------------------------------------------
# 格位掩码（迷雾落盘与推送共用）
# ---------------------------------------------------------------------------


def mask_bytes(bounds_cells: int) -> int:
    return (max(1, int(bounds_cells)) ** 2 + 7) // 8


def cell_index(cell_x: int, cell_y: int, bounds_cells: int) -> int | None:
    """格坐标 → 位图下标；越界返回 ``None``。"""
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    col = int(cell_x) + half
    row = int(cell_y) + half
    if col < 0 or row < 0 or col >= bounds or row >= bounds:
        return None
    return row * bounds + col


def encode_mask(cells: Iterable[Sequence[int]], bounds_cells: int) -> str:
    """把格集合编码为 base64 位图；跨场景（边界不同）时不复用。"""
    bounds = max(1, int(bounds_cells))
    data = bytearray(mask_bytes(bounds))
    for item in cells or []:
        try:
            cell_x, cell_y = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        index = cell_index(cell_x, cell_y, bounds)
        if index is None:
            continue
        data[index >> 3] |= 1 << (index & 7)
    return base64.b64encode(bytes(data)).decode("ascii")


def decode_mask(text: Any, bounds_cells: int) -> set[tuple[int, int]]:
    """base64 位图 → 格集合；长度不符时按较短内容尽力解析。"""
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    try:
        data = base64.b64decode(str(text or ""), validate=False)
    except (ValueError, TypeError):
        return set()
    if not data:
        return set()
    cells: set[tuple[int, int]] = set()
    total = min(len(data) * 8, bounds * bounds)
    for index in range(total):
        if not data[index >> 3] & (1 << (index & 7)):
            continue
        cells.add((index % bounds - half, index // bounds - half))
    return cells


# ---------------------------------------------------------------------------
# 几何基础
# ---------------------------------------------------------------------------


def bounds_rect(
    bounds_cells: int, grid_size: int, offset: Sequence[int] = (0, 0)
) -> tuple[float, float, float, float]:
    """地图边界的世界像素矩形 ``(x0, y0, x1, y1)``。"""
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    return (
        float(ox - half * grid),
        float(oy - half * grid),
        float(ox + (bounds - half) * grid),
        float(oy + (bounds - half) * grid),
    )


def cell_center(
    cell_x: int, cell_y: int, grid_size: int, offset: Sequence[int] = (0, 0)
) -> tuple[float, float]:
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    return (ox + (int(cell_x) + 0.5) * grid, oy + (int(cell_y) + 0.5) * grid)


def footprint_center(
    cell_x: int,
    cell_y: int,
    width_cells: int,
    height_cells: int,
    grid_size: int,
    offset: Sequence[int] = (0, 0),
) -> tuple[float, float]:
    """棋子 footprint 中心；1 格棋子即格子中心。"""
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    return (
        ox + (int(cell_x) + max(1, int(width_cells)) / 2.0) * grid,
        oy + (int(cell_y) + max(1, int(height_cells)) / 2.0) * grid,
    )


def point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]]) -> bool:
    """射线法；多边形顶点按角度排序，原点位于多边形内部。"""
    count = len(polygon)
    if count < 3:
        return False
    inside = False
    jx, jy = float(polygon[-1][0]), float(polygon[-1][1])
    for index in range(count):
        ix, iy = float(polygon[index][0]), float(polygon[index][1])
        if (iy > y) != (jy > y):
            cross_x = (jx - ix) * (y - iy) / ((jy - iy) or _EPSILON) + ix
            if x < cross_x:
                inside = not inside
        jx, jy = ix, iy
    return inside


def _blocks_occluder(wall: Mapping[str, Any], mode: str = "sight") -> bool:
    """墙体是否阻挡指定通道；门开着一律放行，窗户默认只挡移动。"""
    if str(wall.get("state") or "").strip().lower() == "open":
        return False
    key = {"sight": "blocks_sight", "light": "blocks_light"}.get(
        str(mode or "sight").lower(), "blocks_sight"
    )
    is_window = str(wall.get("kind") or "wall").strip().lower() == "window"
    default = not is_window
    return bool(wall.get(key, default))


def _blocks_sight(wall: Mapping[str, Any]) -> bool:
    return _blocks_occluder(wall, "sight")


def wall_segments(
    walls: Iterable[Mapping[str, Any]], mode: str = "sight"
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """阻挡指定通道的墙体 → 世界像素线段列表（视线与光照共用）。"""
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for wall in walls or []:
        if not isinstance(wall, Mapping) or not _blocks_occluder(wall, mode):
            continue
        points: list[tuple[float, float]] = []
        for item in wall.get("points") or []:
            try:
                if isinstance(item, Mapping):
                    points.append((float(item.get("x")), float(item.get("y"))))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue
        for index in range(len(points) - 1):
            segments.append((points[index], points[index + 1]))
    return segments


def _bucket_index(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    grid_size: int,
    offset: Sequence[int],
) -> tuple[dict[tuple[int, int], list[int]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    buckets: dict[tuple[int, int], list[int]] = {}
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    for index, (start, end) in enumerate(segments):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, int(math.ceil(length / max(1.0, grid * 0.5))))
        seen: set[tuple[int, int]] = set()
        for step in range(steps + 1):
            ratio = step / steps
            x = start[0] + (end[0] - start[0]) * ratio
            y = start[1] + (end[1] - start[1]) * ratio
            cell = (int(math.floor((x - ox) / grid)), int(math.floor((y - oy) / grid)))
            if cell in seen:
                continue
            seen.add(cell)
            buckets.setdefault(cell, []).append(index)
    return buckets, segments


def _buckets_for(
    walls: Iterable[Mapping[str, Any]],
    grid_size: int,
    offset: Sequence[int],
    wall_version: str,
    mode: str = "sight",
) -> tuple[dict[tuple[int, int], list[int]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    key = (wall_version, int(grid_size), int(offset[0]), int(offset[1]), str(mode or "sight"))
    cached = _BUCKETS.get(key)
    if cached is not None:
        _BUCKETS.move_to_end(key)
        return cached
    segments = wall_segments(walls, mode)
    value = _bucket_index(segments, grid_size, offset)
    return _remember(_BUCKETS, key, value, _BUCKET_LIMIT)


# ---------------------------------------------------------------------------
# 角度扫描
# ---------------------------------------------------------------------------


def _ray_box_exit(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    box: tuple[float, float, float, float],
) -> float:
    x0, y0, x1, y1 = box
    tx = math.inf
    ty = math.inf
    if dx > _EPSILON:
        tx = (x1 - ox) / dx
    elif dx < -_EPSILON:
        tx = (x0 - ox) / dx
    if dy > _EPSILON:
        ty = (y1 - oy) / dy
    elif dy < -_EPSILON:
        ty = (y0 - oy) / dy
    exit_t = min(tx, ty)
    if not math.isfinite(exit_t) or exit_t <= 0:
        return 0.0
    return exit_t


def _ray_segment(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> float | None:
    ex = end[0] - start[0]
    ey = end[1] - start[1]
    denominator = dx * ey - dy * ex
    if abs(denominator) < 1e-12:
        return None
    ax = start[0] - ox
    ay = start[1] - oy
    t = (ax * ey - ay * ex) / denominator
    if t < -_EPSILON:
        return None
    u = (ax * dy - ay * dx) / denominator
    if u < -1e-6 or u > 1 + 1e-6:
        return None
    return max(0.0, t)


def _candidate_segments(
    buckets: dict[tuple[int, int], list[int]],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    origin: tuple[float, float],
    origin_cell: tuple[int, int],
    radius_cells: int,
    grid_size: int,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """只取视野圆盘附近的线段：桶是稀疏的，按桶格过滤即可。"""
    grid = max(1, int(grid_size))
    reach = int(radius_cells) + 2 if radius_cells > 0 else 0
    indices: set[int] = set()
    for cell, items in buckets.items():
        if reach:
            if max(abs(cell[0] - origin_cell[0]), abs(cell[1] - origin_cell[1])) > reach:
                continue
        indices.update(items)
    # 观察者与视野边缘之间的墙也必须算；半径外的桶已按格距离排除，
    # 但原点所在桶可能登记了远处线段，交给射线求交时的 t 上界过滤。
    return [segments[index] for index in indices]


def _sweep(
    origin: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    box: tuple[float, float, float, float],
    max_distance: float,
) -> list[tuple[float, float]]:
    ox, oy = origin
    angles: set[float] = set()
    for start, end in segments:
        for point in (start, end):
            distance = math.hypot(point[0] - ox, point[1] - oy)
            if max_distance > 0 and distance > max_distance + 1e-6:
                continue
            angle = math.atan2(point[1] - oy, point[0] - ox)
            angles.add(round(angle - _ANGLE_EPSILON, 9))
            angles.add(round(angle, 9))
            angles.add(round(angle + _ANGLE_EPSILON, 9))
    for step in range(_ARC_STEPS):
        angle = -math.pi + (2 * math.pi * step / _ARC_STEPS)
        angles.add(round(angle, 9))
    # 补齐 ±π 两侧，闭合边落在同一条射线上，不会横切地图。
    angles.add(round(-math.pi, 9))
    angles.add(round(math.pi, 9))

    points: list[tuple[float, float]] = []
    for angle in sorted(angles):
        dx, dy = math.cos(angle), math.sin(angle)
        limit = _ray_box_exit(ox, oy, dx, dy, box)
        if max_distance > 0:
            limit = min(limit, max_distance)
        if limit <= 0:
            continue
        best = limit
        for start, end in segments:
            hit = _ray_segment(ox, oy, dx, dy, start, end)
            if hit is not None and hit < best:
                best = hit
        points.append((ox + dx * best, oy + dy * best))
    # 去掉角度过近的重复点，保持多边形简单。
    cleaned: list[tuple[float, float]] = []
    for point in points:
        if cleaned and math.hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) < 0.05:
            continue
        cleaned.append(point)
    return cleaned


def _rasterize(
    polygon: Sequence[Sequence[float]],
    origin_cell: tuple[int, int],
    radius_cells: int,
    bounds_cells: int,
    grid_size: int,
    offset: Sequence[int],
) -> set[tuple[int, int]]:
    bounds = max(1, int(bounds_cells))
    half = bounds // 2
    grid = max(1, int(grid_size))
    reach = int(radius_cells) if radius_cells > 0 else bounds
    cells: set[tuple[int, int]] = set()
    min_x = max(-half, origin_cell[0] - reach)
    max_x = min(bounds - half - 1, origin_cell[0] + reach)
    min_y = max(-half, origin_cell[1] - reach)
    max_y = min(bounds - half - 1, origin_cell[1] + reach)
    ox, oy = origin_cell
    for cell_y in range(min_y, max_y + 1):
        for cell_x in range(min_x, max_x + 1):
            if radius_cells > 0 and max(abs(cell_x - ox), abs(cell_y - oy)) > radius_cells:
                continue
            x, y = cell_center(cell_x, cell_y, grid, offset)
            if point_in_polygon(x, y, polygon):
                cells.add((cell_x, cell_y))
    cells.add((origin_cell[0], origin_cell[1]))
    return cells


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def visibility(
    origin: Sequence[float],
    *,
    walls: Iterable[Mapping[str, Any]],
    grid_size: int,
    offset: Sequence[int] = (0, 0),
    bounds_cells: int,
    radius_cells: int = 0,
    wall_version: str = "",
    light_version: str = "",
    room_key: str = "",
    facing: Any = None,
    mode: str = "sight",
) -> dict[str, Any]:
    """计算观察者可见多边形与可见格集合（带缓存）。

    ``radius_cells`` 为 0 表示只受地图边界限制，否则只看见半径内的格子。
    ``mode="light"`` 时按 ``blocks_light`` 过滤墙体，供光源投影复用；视线与
    光照分开计算，调用方再取交集。
    """
    ox, oy = float(origin[0]), float(origin[1])
    grid = max(1, int(grid_size))
    bounds = max(1, int(bounds_cells))
    radius = max(0, int(radius_cells))
    mode = str(mode or "sight").lower()
    origin_cell = (
        int(math.floor((ox - int(offset[0])) / grid)),
        int(math.floor((oy - int(offset[1])) / grid)),
    )
    wall_version = str(wall_version or state_version(list(walls or [])))
    key = (
        str(room_key or ""),
        round(ox, 2),
        round(oy, 2),
        radius,
        grid,
        int(offset[0]),
        int(offset[1]),
        bounds,
        wall_version,
        str(light_version or ""),
        str(facing or ""),
        mode,
    )
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        result = dict(cached)
        result["cached"] = True
        return result

    box = bounds_rect(bounds, grid, offset)
    buckets, segments = _buckets_for(walls, grid, offset, wall_version, mode)
    candidates = _candidate_segments(buckets, segments, (ox, oy), origin_cell, radius, grid)
    max_distance = radius * grid if radius > 0 else 0.0
    polygon = _sweep((ox, oy), candidates, box, max_distance)
    cells = _rasterize(polygon, origin_cell, radius, bounds, grid, offset)
    result = {
        "polygon": polygon,
        "cells": cells,
        "origin": (ox, oy),
        "origin_cell": origin_cell,
        "radius_cells": radius,
        "cached": False,
    }
    _remember(_CACHE, key, result, _CACHE_LIMIT)
    return result


def lighting(
    origin: Sequence[float],
    *,
    walls: Iterable[Mapping[str, Any]],
    grid_size: int,
    offset: Sequence[int] = (0, 0),
    bounds_cells: int,
    bright_cells: int,
    dim_cells: int,
    wall_version: str = "",
    light_version: str = "",
    room_key: str = "",
) -> dict[str, Any]:
    """单个光源的明区 / 暗区格集合。

    ``dim_cells`` 是外圈半径，``bright_cells`` 是内圈明亮半径；墙体按
    ``blocks_light`` 遮挡。明暗两组分开返回，前端据此叠两层遮罩。
    """
    bright_radius = max(0, int(bright_cells))
    dim_radius = max(bright_radius, int(dim_cells))
    if dim_radius <= 0:
        return {"bright": set(), "dim": set(), "polygon": [], "cached": False}
    result = visibility(
        origin,
        walls=walls,
        grid_size=grid_size,
        offset=offset,
        bounds_cells=bounds_cells,
        radius_cells=dim_radius,
        wall_version=wall_version,
        light_version=light_version,
        room_key=room_key,
        mode="light",
    )
    ox, oy = result["origin_cell"]
    bright: set[tuple[int, int]] = set()
    dim: set[tuple[int, int]] = set()
    for cell in result["cells"]:
        if max(abs(cell[0] - ox), abs(cell[1] - oy)) <= bright_radius:
            bright.add(cell)
        else:
            dim.add(cell)
    return {
        "bright": bright,
        "dim": dim,
        "polygon": result["polygon"],
        "origin_cell": result["origin_cell"],
        "cached": result.get("cached", False),
    }
