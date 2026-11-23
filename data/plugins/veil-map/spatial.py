"""veil-map 空间判定（距离、范围、墙体遮挡与视线）。

坐标约定
--------
世界坐标以像素为单位，地图图片中心为原点。格子边长 ``grid_size``，格线偏移
``grid_offset``（世界像素）；格子 ``(cx, cy)`` 的棋子锚点坐标为::

    x = grid_offset.x + cx * grid_size
    y = grid_offset.y + cy * grid_size

和地图插件里棋子的 ``(x, y)`` 完全一致，因此空间查询结果可以直接用于移动。

阶段划分
--------
第一阶段（已实现）：格/尺换算、格距、圆形 / 锥形 / 线形范围取格。
第二阶段（已实现）：墙体数据结构、视线与移动阻挡判定。墙体是折线 + 属性，
``blocks_sight`` / ``blocks_movement`` / ``blocks_light`` / ``opacity`` 为后续
迷雾与光照预留，不需要再改结构。高频的移动判定走 :class:`WallIndex`（按格分桶，
只比较路径附近的墙段）；一次性查询仍可用 :func:`segment_blockers`。
第三阶段（暂不实现，仅设计）：格子图上的 A* 寻路。

第三阶段设计
------------
1. 节点是有界地图内的格子，边为 8 方向邻接；进入某格的成本由地形与是否有
   多格占位决定，默认 1 格 = 5 尺。
2. 节点可通行需同时满足：在 ``bounds_cells`` 内、没有其他棋子占用 footprint、
   与墙体不相交（复用 ``segment_blockers(..., mode="movement")``，门 open 时
   放行）。
3. 起点与终点先吸附到格；对多格棋子按 footprint 做角落检查，不能只检查锚点。
4. 返回 ``{"path": [(cx, cy), ...], "cost_ft": n, "complete": bool}``；无路可走
   时返回最近可达点并标记 ``complete=False``，由调用方给可读提示。
5. 平滑：对路径做视线检查（本模块第二阶段），能直连的拐点合并，减少锯齿。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

FEET_PER_CELL = 5
METRICS = ("chebyshev", "euclidean")
DIRECTIONS = {
    "n": (0.0, -1.0),
    "ne": (0.7071067811865476, -0.7071067811865476),
    "e": (1.0, 0.0),
    "se": (0.7071067811865476, 0.7071067811865476),
    "s": (0.0, 1.0),
    "sw": (-0.7071067811865476, 0.7071067811865476),
    "w": (-1.0, 0.0),
    "nw": (-0.7071067811865476, -0.7071067811865476),
}
_CONE_COS = math.sqrt(0.5)
_EPSILON = 1e-9


def feet_to_cells(feet: Any, feet_per_cell: int = FEET_PER_CELL) -> int:
    """英尺向下取整为格数；非正数返回 0。"""
    step = max(1, int(feet_per_cell))
    try:
        value = float(feet)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0
    return int(value // step)


def cells_to_feet(cells: Any, feet_per_cell: int = FEET_PER_CELL) -> int:
    try:
        value = max(0, int(cells))
    except (TypeError, ValueError):
        return 0
    return value * max(1, int(feet_per_cell))


def cell_distance(origin: Sequence[int], target: Sequence[int], metric: str = "chebyshev") -> int:
    """两格之间的格距；5e 默认 Chebyshev（斜向也算 1 格）。"""
    ox, oy = int(origin[0]), int(origin[1])
    tx, ty = int(target[0]), int(target[1])
    dx, dy = abs(tx - ox), abs(ty - oy)
    if str(metric or "").lower() == "euclidean":
        return int(math.floor(math.hypot(dx, dy)))
    return max(dx, dy)


def normalize_direction(direction: Any) -> tuple[float, float]:
    """把 ``"ne"`` 或 ``{"dx": 1, "dy": -1}`` 归一化为单位向量。"""
    if isinstance(direction, str):
        unit = DIRECTIONS.get(direction.strip().lower())
        if unit is None:
            raise ValueError("方向必须是 n/ne/e/se/s/sw/w/nw 之一。")
        return unit
    if isinstance(direction, Mapping):
        raw_x, raw_y = direction.get("dx"), direction.get("dy")
    elif isinstance(direction, (list, tuple)) and len(direction) >= 2:
        raw_x, raw_y = direction[0], direction[1]
    else:
        raise ValueError("方向必须是八方向名称或 {dx, dy} 向量。")
    try:
        dx, dy = float(raw_x), float(raw_y)
    except (TypeError, ValueError) as exc:
        raise ValueError("方向向量必须是数字。") from exc
    if not (math.isfinite(dx) and math.isfinite(dy)) or (dx == 0 and dy == 0):
        raise ValueError("方向向量不能为零。")
    length = math.hypot(dx, dy)
    return dx / length, dy / length


def range_cells(
    origin: Sequence[int],
    *,
    shape: str,
    size_cells: int,
    direction: Any = None,
    metric: str = "chebyshev",
) -> list[tuple[int, int]]:
    """按形状取格。

    - ``circle``：以 origin 为中心、半径 ``size_cells`` 的方形/圆形范围
      （5e 默认正方形网格用 Chebyshev，圆形模板可传 euclidean）。
    - ``cone``：从 origin 出发、张角 90° 的锥形，包含方向线两侧各 45°。
    - ``line``：从 origin 出发、宽 1 格的直线，长度为 ``size_cells``。

    圆形包含原点格；锥形与线形不包含原点格（施法者自身所在格）。
    """
    if metric not in METRICS:
        raise ValueError("metric 只能是 chebyshev 或 euclidean。")
    shape = str(shape or "circle").strip().lower()
    if shape not in {"circle", "cone", "line"}:
        raise ValueError("shape 只能是 circle / cone / line。")
    ox, oy = int(origin[0]), int(origin[1])
    size = max(0, int(size_cells))
    if shape == "circle":
        cells: list[tuple[int, int]] = []
        for dx in range(-size, size + 1):
            for dy in range(-size, size + 1):
                if metric == "euclidean":
                    if math.hypot(dx, dy) > size + _EPSILON:
                        continue
                elif max(abs(dx), abs(dy)) > size:
                    continue
                cells.append((ox + dx, oy + dy))
        return cells
    if size <= 0:
        return []
    unit = normalize_direction(direction)
    ux, uy = unit
    if shape == "cone":
        cells = []
        for dx in range(-size, size + 1):
            for dy in range(-size, size + 1):
                if dx == 0 and dy == 0:
                    continue
                distance = cell_distance((0, 0), (dx, dy), metric)
                if distance > size:
                    continue
                length = math.hypot(dx, dy)
                cosine = (dx * ux + dy * uy) / length
                if cosine >= _CONE_COS - _EPSILON:
                    cells.append((ox + dx, oy + dy))
        return cells
    # line：取中心到线段距离不超过半格、且投影落在线段内的格子。
    cells = []
    reach = size
    bound = size + 1
    for dx in range(-bound, bound + 1):
        for dy in range(-bound, bound + 1):
            if dx == 0 and dy == 0:
                continue
            projection = dx * ux + dy * uy
            if projection <= _EPSILON or projection > reach + _EPSILON:
                continue
            perpendicular = abs(dx * uy - dy * ux)
            if perpendicular >= 0.5 + _EPSILON:
                continue
            cells.append((ox + dx, oy + dy))
    return cells


def world_to_cell(
    x: Any, y: Any, grid_size: int, offset: Sequence[int] = (0, 0)
) -> tuple[int, int]:
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    return (
        int(round((float(x) - ox) / grid)),
        int(round((float(y) - oy) / grid)),
    )


def cell_to_world(
    cell_x: Any, cell_y: Any, grid_size: int, offset: Sequence[int] = (0, 0)
) -> tuple[int, int]:
    grid = max(1, int(grid_size))
    ox, oy = int(offset[0]), int(offset[1])
    return (ox + int(cell_x) * grid, oy + int(cell_y) * grid)


def _xy(point: Any) -> tuple[float, float]:
    """兼容 ``{x, y}`` 与 ``[x, y]`` 两种墙体点格式。"""
    if isinstance(point, Mapping):
        return float(point.get("x")), float(point.get("y"))
    return float(point[0]), float(point[1])


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> bool:
    return (
        min(ax, bx) - _EPSILON <= px <= max(ax, bx) + _EPSILON
        and min(ay, by) - _EPSILON <= py <= max(ay, by) + _EPSILON
    )


def segments_intersect(
    p1: Sequence[float],
    p2: Sequence[float],
    p3: Sequence[float],
    p4: Sequence[float],
) -> bool:
    """线段相交（含共线重叠）。"""
    ax, ay = _xy(p1)
    bx, by = _xy(p2)
    cx, cy = _xy(p3)
    dx, dy = _xy(p4)
    d1 = _cross(bx - ax, by - ay, cx - ax, cy - ay)
    d2 = _cross(bx - ax, by - ay, dx - ax, dy - ay)
    d3 = _cross(dx - cx, dy - cy, ax - cx, ay - cy)
    d4 = _cross(dx - cx, dy - cy, bx - cx, by - cy)
    if ((d1 > _EPSILON and d2 < -_EPSILON) or (d1 < -_EPSILON and d2 > _EPSILON)) and (
        (d3 > _EPSILON and d4 < -_EPSILON) or (d3 < -_EPSILON and d4 > _EPSILON)
    ):
        return True
    if abs(d1) <= _EPSILON and _on_segment(cx, cy, ax, ay, bx, by):
        return True
    if abs(d2) <= _EPSILON and _on_segment(dx, dy, ax, ay, bx, by):
        return True
    if abs(d3) <= _EPSILON and _on_segment(ax, ay, cx, cy, dx, dy):
        return True
    if abs(d4) <= _EPSILON and _on_segment(bx, by, cx, cy, dx, dy):
        return True
    return False


def wall_blocks(wall: Mapping[str, Any], mode: str = "sight") -> bool:
    """门开着时一律放行；其余按墙体属性判定。"""
    if str(wall.get("state") or "").strip().lower() == "open":
        return False
    key = {
        "sight": "blocks_sight",
        "movement": "blocks_movement",
        "light": "blocks_light",
    }.get(str(mode or "sight").lower())
    if key is None:
        raise ValueError("mode 只能是 sight / movement / light。")
    # 窗户默认只挡移动、不挡视线与光照；其它墙体默认全挡。显式属性优先。
    is_window = str(wall.get("kind") or "wall").strip().lower() == "window"
    default = (key == "blocks_movement") if is_window else True
    return bool(wall.get(key, default))


def segment_blockers(
    walls: Iterable[Mapping[str, Any]],
    start: Sequence[float],
    end: Sequence[float],
    *,
    mode: str = "sight",
) -> list[str]:
    """返回与线段相交且会阻挡该模式的墙体 id 列表（去重保序）。

    每次调用都会扫一遍全量墙，适合一次性查询（``/spatial/line``、行动守卫）。
    高频的移动判定用 :class:`WallIndex`，只比较路径附近的桶。
    """
    blockers: list[str] = []
    seen: set[str] = set()
    for wall in walls or []:
        if not isinstance(wall, Mapping) or not wall_blocks(wall, mode):
            continue
        wall_id = str(wall.get("id") or "").strip()
        points = wall.get("points") or []
        if len(points) < 2:
            continue
        for index in range(len(points) - 1):
            if segments_intersect(start, end, points[index], points[index + 1]):
                if wall_id and wall_id not in seen:
                    seen.add(wall_id)
                    blockers.append(wall_id)
                break
    return blockers


def _point_in_rect(
    point: Any, left: float, top: float, right: float, bottom: float
) -> bool:
    x, y = _xy(point)
    return (
        left - _EPSILON <= x <= right + _EPSILON
        and top - _EPSILON <= y <= bottom + _EPSILON
    )


_BUCKET_MIN_PX = 32.0

WallSegment = tuple[str, str, tuple[float, float], tuple[float, float]]


class WallIndex:
    """按格分桶的墙段索引，供移动判定复用。

    ``mode`` 在建索引时过滤一遍墙：``movement`` 模式下开着的门直接不进索引，
    窗户按 ``blocks_movement`` 判定（见 :func:`wall_blocks`）。之后每条线段登记
    到它经过的格桶，查询只比较路径/矩形附近的桶，不遍历全量墙。

    几何一律复用本模块的 ``segments_intersect``；这里只负责"少算"，不另写判定。
    """

    __slots__ = ("mode", "grid", "offset", "segments", "buckets")

    def __init__(
        self,
        walls: Iterable[Mapping[str, Any]],
        grid_size: int = 64,
        offset: Sequence[int] = (0, 0),
        mode: str = "movement",
    ) -> None:
        self.mode = str(mode or "movement").strip().lower()
        try:
            grid = float(grid_size)
        except (TypeError, ValueError):
            grid = 64.0
        self.grid = max(_BUCKET_MIN_PX, grid if math.isfinite(grid) and grid > 0 else 64.0)
        try:
            self.offset = (int(offset[0]), int(offset[1]))
        except (TypeError, ValueError, IndexError):
            self.offset = (0, 0)
        self.segments: list[WallSegment] = []
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for wall in walls or []:
            if not isinstance(wall, Mapping) or not wall_blocks(wall, self.mode):
                continue
            wall_id = str(wall.get("id") or "").strip()
            kind = str(wall.get("kind") or "wall").strip().lower()
            points: list[tuple[float, float]] = []
            for item in wall.get("points") or []:
                try:
                    points.append(_xy(item))
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
            for index in range(len(points) - 1):
                start, end = points[index], points[index + 1]
                slot = len(self.segments)
                self.segments.append((wall_id, kind, start, end))
                for cell in self._cells_along(start, end):
                    self.buckets.setdefault(cell, []).append(slot)

    def __len__(self) -> int:
        return len(self.segments)

    def __bool__(self) -> bool:
        return bool(self.segments)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (
            int(math.floor((x - self.offset[0]) / self.grid)),
            int(math.floor((y - self.offset[1]) / self.grid)),
        )

    def _cells_along(
        self, start: Sequence[float], end: Sequence[float]
    ) -> list[tuple[int, int]]:
        ax, ay = _xy(start)
        bx, by = _xy(end)
        length = math.hypot(bx - ax, by - ay)
        # 步长不超过半桶：线段经过的每个桶都会被登记，也不会漏掉查询桶。
        steps = max(1, int(math.ceil(length / max(1.0, self.grid * 0.5))))
        cells: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for step in range(steps + 1):
            ratio = step / steps
            cell = self._cell(ax + (bx - ax) * ratio, ay + (by - ay) * ratio)
            if cell in seen:
                continue
            seen.add(cell)
            cells.append(cell)
        return cells

    def _near_cells(
        self, cells: Iterable[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """把桶扩一圈，消除线段正好压在桶边界上的漏检。"""
        out: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for cell_x, cell_y in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cell = (cell_x + dx, cell_y + dy)
                    if cell in seen:
                        continue
                    seen.add(cell)
                    out.append(cell)
        return out

    def _candidates(self, cells: Iterable[tuple[int, int]]) -> list[int]:
        slots: list[int] = []
        seen: set[int] = set()
        for cell in cells:
            for slot in self.buckets.get(cell) or ():
                if slot in seen:
                    continue
                seen.add(slot)
                slots.append(slot)
        slots.sort()
        return slots

    def hits(
        self, start: Sequence[float], end: Sequence[float]
    ) -> list[WallSegment]:
        """与线段相交的墙段（``(墙 id, kind, 起点, 终点)``，去重保序）。"""
        out: list[WallSegment] = []
        cells = self._near_cells(self._cells_along(start, end))
        for slot in self._candidates(cells):
            wall_id, kind, p0, p1 = self.segments[slot]
            if segments_intersect(start, end, p0, p1):
                out.append(self.segments[slot])
        return out

    def blockers(self, start: Sequence[float], end: Sequence[float]) -> list[str]:
        """与线段相交的墙体 id（去重保序）。"""
        ids: list[str] = []
        for wall_id, _kind, _p0, _p1 in self.hits(start, end):
            if wall_id and wall_id not in ids:
                ids.append(wall_id)
        return ids

    def rect_hits(
        self, left: float, top: float, right: float, bottom: float
    ) -> list[WallSegment]:
        """与矩形相交、或整段落在矩形内部的墙段。

        用于"落点压墙"与"起点已经在墙里"两种判定：既查矩形四条边与线段求交，
        也查线段端点是否落在矩形内（短墙段可能整段藏在矩形里，不穿过任何边）。
        """
        corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
        cells = self._near_cells(self._cells_along((left, top), (right, bottom)))
        out: list[WallSegment] = []
        seen: set[int] = set()
        for slot in self._candidates(cells):
            if slot in seen:
                continue
            _wall_id, _kind, p0, p1 = self.segments[slot]
            inside = _point_in_rect(p0, left, top, right, bottom) or _point_in_rect(
                p1, left, top, right, bottom
            )
            if not inside:
                for index in range(4):
                    if segments_intersect(
                        corners[index], corners[(index + 1) % 4], p0, p1
                    ):
                        inside = True
                        break
            if inside:
                seen.add(slot)
                out.append(self.segments[slot])
        return out
