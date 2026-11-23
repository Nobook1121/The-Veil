"""场景转换核心（纯函数）：FVTT scene 文档 + 子集合 + 伴生 JSON → veil-map 载荷。

坐标系约定（「世界包参数直供」）
--------------------------------
输入与输出都用**背景图片像素坐标**（FVTT 语义）：

- ``grid.size → grid.size``；图内网格原点 ``grid.origin_x/origin_y =
  shiftX/shiftY + background.offsetX/offsetY``（v13 包 shift 缺省 0，
  对齐量在 background offset；伴生 JSON 用自身的 shiftX/shiftY）；
- 墙 ``c``、光 ``x/y``、棋子 ``x/y`` 原样保持图片像素，不做任何二次标定。

图片中心对齐（世界坐标换算）、格坐标取整、墙体/光源/棋子归一化与上限
校验由 veil-map 的导入服务（``map_service.import_scene``）统一完成——
这里不另写一套几何换算。

地图边界（``bounds_cells``）也在这一层按**底图尺寸**推导：veil-map 的
``bounds_cells`` 是正方形边长（格），世界坐标以原点为中心；沿用它的
``BOUNDS_DEFAULT``(50) 会把 2100×1500/150 = 14×10 的酒馆也撑成 50 格
（7500 单位），底图只占中心一小块、标尺画到 39 列。

跳过策略：六边形网格（grid.type≠1）、网格像素出窗、缺背景图、背景图超
40MB、非英尺网格——返回带稳定 code 的跳过记录，进报告警告，不猜测。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

GRID_TYPE_SQUARE = 1
GRID_MIN = 16
# 与 veil-map entry.py 的 MIN_GRID/MAX_GRID、importer.GRID_MAX 同步。真实世界包
# 会用更大的格（heroes-to-yuanshan「宅邸（二层）」= 282px），旧窗口 256 会让
# 这类场景被整场跳过；512 覆盖现有样例，同时仍能挡住「把图片尺寸当格大小」的
# 明显误标。放宽后下游无需再改：格坐标 = 像素 ÷ grid、迷雾按「格」生成。
GRID_MAX = 512
# 与 veil-map 的上限对齐（超限截断并写警告，不静默丢）。
MAX_WALLS = 200
MAX_LIGHTS = 100
MAX_TOKENS = 200
# 背景图单张上限（沿用 veil-map media.MAX_IMAGE_BYTES）。
MAX_BACKGROUND_BYTES = 40 * 1024 * 1024
FEET_PER_CELL = 5
# veil-map 的 bounds_cells 语义：**正方形边长（格）**，世界坐标以原点为中心，
# 可玩格 = [-half, bounds_cells - half)。一张图只能取「横/竖格数的较大值」，
# 不能分别存宽高；与 veil-map 的 MIN_BOUNDS/MAX_BOUNDS 同口径（改这里要同步）。
BOUNDS_MIN = 5
BOUNDS_MAX = 200

# disposition(-1 敌 / 0 中立 / 1 友) → 阵营色环（敌红 / 中立金 / 友青，
# 取 veil-map TOKEN_COLORS 现有色）。
DISPOSITION_COLORS = {-1: "#e06666", 0: "#c8a35a", 1: "#93c47d"}

_SCENE_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class SceneSkip(Exception):
    """场景不可转换；``code`` 稳定，进报告警告。"""

    def __init__(self, code: str, message: str, *, title: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.title = title


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _clamp_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _sub_id(doc_id: str) -> str:
    """``<sceneId>.<subId>`` → ``<subId>``；没有点号时取整段。"""
    _, _, sub = str(doc_id or "").partition(".")
    return sub or str(doc_id or "")


def safe_scene_id(scene_id: Any) -> str:
    """FVTT 场景 id → veil-map scene_id（``^[A-Za-z0-9_-]{1,64}$``）。"""
    text = _SCENE_ID_SAFE.sub("", str(scene_id or ""))[:58]
    if text:
        return f"fvtt-{text}"
    digest = hashlib.sha1(str(scene_id or "").encode("utf-8")).hexdigest()[:12]
    return f"fvtt-{digest}"


# -- 网格与背景 -----------------------------------------------------------


def scene_grid(scene: dict[str, Any], companion: dict[str, Any] | None = None) -> dict[str, Any]:
    """归一网格：LevelDB 场景文档 ``grid{size,type,distance,units}``，
    伴生 JSON ``grid``（int）+ ``gridDistance/gridUnits``。"""
    if companion is not None:
        size = companion.get("grid", companion.get("gridSize"))
        return {
            "size": _number(size, 0.0),
            "type": GRID_TYPE_SQUARE,
            "distance": _number(companion.get("gridDistance"), FEET_PER_CELL) or FEET_PER_CELL,
            "units": str(companion.get("gridUnits") or "ft"),
        }
    grid = scene.get("grid") if isinstance(scene.get("grid"), dict) else {}
    return {
        "size": _number(grid.get("size"), 0.0),
        "type": _clamp_int(grid.get("type"), GRID_TYPE_SQUARE, 0, 99),
        "distance": _number(grid.get("distance"), FEET_PER_CELL) or FEET_PER_CELL,
        "units": str(grid.get("units") or "ft"),
    }


def scene_grid_origin(scene: dict[str, Any], companion: dict[str, Any] | None = None) -> tuple[float, float]:
    """图内网格原点：v13 = background.offsetX/offsetY（+ 顶层 shiftX/shiftY 若有）；
    伴生 JSON = shiftX/shiftY。"""
    if companion is not None:
        return _number(companion.get("shiftX")), _number(companion.get("shiftY"))
    background = scene.get("background") if isinstance(scene.get("background"), dict) else {}
    shift_x = _number(scene.get("shiftX"))
    shift_y = _number(scene.get("shiftY"))
    return shift_x + _number(background.get("offsetX")), shift_y + _number(background.get("offsetY"))


def scene_bounds_cells(width: Any, height: Any, grid_size: Any) -> int:
    """底图像素尺寸 + 格大小 → veil-map ``bounds_cells``（正方形边长，格）。

    取横/竖格数的**较大值**并向上取整：宁可多出一格，也不让底图边缘落在
    可玩区之外。尺寸或格大小非法时回落 :data:`BOUNDS_MIN`（veil-map 的下限
    ``MIN_BOUNDS``），超过上限截到 :data:`BOUNDS_MAX`（``MAX_BOUNDS``）。
    """
    grid = int(round(_number(grid_size, 0.0)))
    width_px = _number(width, 0.0)
    height_px = _number(height, 0.0)
    if grid <= 0 or width_px <= 0 or height_px <= 0:
        return BOUNDS_MIN
    cells = max(math.ceil(width_px / grid), math.ceil(height_px / grid))
    return max(BOUNDS_MIN, min(BOUNDS_MAX, cells))


def verify_wall_extent(
    walls: list[dict[str, Any]],
    *,
    width: Any,
    height: Any,
    grid_size: Any,
    tolerance_cells: float = 1.0,
) -> list[str]:
    """墙折线是否落在背景图范围之内（图片像素口径）→ 可读警告。

    墙 ``c`` 与背景图必须同一个像素坐标系；若某个包把「格」当像素、或整体
    错位/缩放，症状就是墙线飘到地形之外。这里按「1 格容差」核对一次（源包
    贴边的墙常在 0–40px 之外一点，属正常），异常进警告而不是静默导入。
    """
    if not walls:
        return []
    grid = int(round(_number(grid_size, 0.0)))
    width_px = _number(width, 0.0)
    height_px = _number(height, 0.0)
    if grid <= 0 or width_px <= 0 or height_px <= 0:
        return []
    margin = grid * max(0.0, float(tolerance_cells))
    outside = 0
    for wall in walls:
        for point in wall.get("points") or []:
            x = _number(point.get("x"), math.nan)
            y = _number(point.get("y"), math.nan)
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            if (
                x < -margin
                or x > width_px + margin
                or y < -margin
                or y > height_px + margin
            ):
                outside += 1
                break
    if not outside:
        return []
    return [
        f"{outside}/{len(walls)} 段墙体落在背景图（{int(width_px)}×{int(height_px)}）"
        f"之外超过 {tolerance_cells:g} 格容差；墙与底图可能不是同一套像素坐标，"
        "已按原样导入，请核对。"
    ]


def scene_background_src(scene: dict[str, Any]) -> str:
    background = scene.get("background") if isinstance(scene.get("background"), dict) else {}
    src = background.get("src")
    if isinstance(src, str) and src.strip():
        return src.strip()
    # 伴生 JSON 的 img 兜底（两个样例包里均为 null，但字段属于该格式）。
    return str(scene.get("img") or "").strip()


# -- 子集合转换 -----------------------------------------------------------


def _blocks(value: Any) -> bool:
    """v10+ 墙体标志：0 = 不阻挡，非 0（1=NORMAL、2/20=LIMITED）按阻挡处理；
    缺省视为阻挡（保守）。"""
    if value is None:
        return True
    return _clamp_int(value, 1, 0, 99) != 0


def convert_walls(wall_docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """墙 ``c: [x1,y1,x2,y2]``（图片像素）→ veil-map 墙段载荷（仍为图片像素）。

    门：``door`` 非 0 → kind=door，``ds==2`` 开门；挡视线但不挡移动 → window；
    其余为 wall。LIMITED（2/20）按完全阻挡处理（见模块 docstring 的取舍）。
    """
    walls: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, doc in enumerate(wall_docs):
        coords = doc.get("c")
        if not isinstance(coords, (list, tuple)) or len(coords) != 4:
            continue
        points = [
            {"x": int(round(_number(coords[0]))), "y": int(round(_number(coords[1])))},
            {"x": int(round(_number(coords[2]))), "y": int(round(_number(coords[3])))},
        ]
        if points[0] == points[1]:
            continue
        blocks_sight = _blocks(doc.get("sight", doc.get("sense")))
        blocks_movement = _blocks(doc.get("move"))
        door = _clamp_int(doc.get("door"), 0, 0, 99)
        if door:
            kind = "door"
            state = "open" if _clamp_int(doc.get("ds"), 0, 0, 99) == 2 else "closed"
        elif not blocks_sight and blocks_movement:
            kind = "window"
            state = "closed"
        else:
            kind = "wall"
            state = "closed"
        walls.append(
            {
                "id": (_SCENE_ID_SAFE.sub("", _sub_id(doc.get("_id") or "")) or f"wall-{index + 1}")[:64],
                "kind": kind,
                "state": state,
                "points": points,
                "blocks_sight": blocks_sight,
                "blocks_movement": blocks_movement,
                "blocks_light": _blocks(doc.get("light")),
            }
        )
    if len(walls) > MAX_WALLS:
        warnings.append(f"墙体 {len(walls)} 段超过 {MAX_WALLS} 上限，已截断。")
        walls = walls[:MAX_WALLS]
    return walls, warnings


def convert_lights(
    light_docs: list[dict[str, Any]], distance: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """光 ``x/y``（图片像素）+ ``config{dim,bright,color,alpha,angle}`` → veil-map 光源。

    半径格化：``round(dim / grid.distance)``（dim/bright 的单位与 distance 一致，
    dnd5e 为英尺）；单位不是英尺时进警告。``hidden`` 光导入为关闭态（计数对账）。
    """
    lights: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, doc in enumerate(light_docs):
        x, y = _number(doc.get("x"), math.nan), _number(doc.get("y"), math.nan)
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
        dim = _number(config.get("dim"))
        bright = _number(config.get("bright"))
        range_cells = max(1, int(round(max(dim, bright) / distance)))
        bright_cells = min(range_cells, int(round(bright / distance)) if bright > 0 else 0)
        lights.append(
            {
                "id": (_SCENE_ID_SAFE.sub("", _sub_id(doc.get("_id") or "")) or f"light-{index + 1}")[:64],
                "x": int(round(x)),
                "y": int(round(y)),
                "range_cells": range_cells,
                "bright_cells": bright_cells,
                "color": str(config.get("color") or "#ffaa00"),
                "intensity": max(0.0, min(10.0, _number(config.get("alpha"), 1.0))),
                "angle": max(0, min(360, int(round(_number(config.get("angle"), 360.0))))),
                "enabled": not bool(doc.get("hidden")),
            }
        )
    if len(lights) > MAX_LIGHTS:
        warnings.append(f"光源 {len(lights)} 个超过 {MAX_LIGHTS} 上限，已截断。")
        lights = lights[:MAX_LIGHTS]
    return lights, warnings


def convert_tokens(token_docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """棋子 ``x/y``（图片像素）+ ``width/height``（格）+ disposition → veil-map 棋子。

    格坐标由 veil-map 服务按图片中心换算；这里只直供像素位置与阵营。
    disposition → ``color``（阵营色环）；``kind`` 一律 npc（玩家棋子由房间
    成员入座逻辑管理，不与导入棋子混绑）。``hidden`` 棋子照常导入（计数对账）。
    """
    tokens: list[dict[str, Any]] = []
    hidden = 0
    for index, doc in enumerate(token_docs):
        x, y = _number(doc.get("x"), math.nan), _number(doc.get("y"), math.nan)
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        disposition = _clamp_int(doc.get("disposition"), 0, -1, 1)
        hidden += 1 if doc.get("hidden") else 0
        tokens.append(
            {
                "id": (_SCENE_ID_SAFE.sub("", _sub_id(doc.get("_id") or "")) or f"token-{index + 1}")[:64],
                "name": str(doc.get("name") or "棋子")[:40],
                "x": int(round(x)),
                "y": int(round(y)),
                "w": _clamp_int(doc.get("width"), 1, 1, 4),
                "h": _clamp_int(doc.get("height"), 1, 1, 4),
                "kind": "npc",
                "color": DISPOSITION_COLORS.get(disposition, DISPOSITION_COLORS[0]),
                "disposition": disposition,
            }
        )
    warnings: list[str] = []
    if hidden:
        warnings.append(
            f"{hidden} 个棋子在源包中为 GM 隐藏态；当前按普通棋子导入，可按需移除。"
        )
    if len(tokens) > MAX_TOKENS:
        warnings.append(f"棋子 {len(tokens)} 个超过 {MAX_TOKENS} 上限，已截断。")
        tokens = tokens[:MAX_TOKENS]
    return tokens, warnings


def convert_ambient(scene: dict[str, Any], companion: dict[str, Any] | None = None) -> dict[str, Any]:
    """环境光：v13 ``environment{darknessLevel, globalLight}``；伴生 JSON
    ``darkness`` + ``globalLight``（bool）。全局照明 → veil-map ambient.enabled。"""
    if companion is not None:
        darkness = _number(companion.get("darkness"))
        global_enabled = bool(companion.get("globalLight"))
        alpha = 0.5
    else:
        environment = scene.get("environment") if isinstance(scene.get("environment"), dict) else {}
        darkness = _number(environment.get("darknessLevel"))
        global_light = (
            environment.get("globalLight") if isinstance(environment.get("globalLight"), dict) else {}
        )
        global_enabled = bool(global_light.get("enabled"))
        alpha = _number(global_light.get("alpha"), 0.5)
    return {
        "enabled": global_enabled,
        "color": "#ffffff",
        "intensity": max(0.0, min(10.0, alpha if alpha > 0 else 1.0)),
        "darkness": max(0.0, min(1.0, darkness)),
        "baked": False,
    }


# -- 伴生 JSON（sixianyuchanmian 形态） -------------------------------------


def companion_walls(companion: dict[str, Any]) -> list[dict[str, Any]]:
    """v8 伴生 JSON 墙 → v13 形态（``sense`` 1=透明/2=受限 → ``sight`` 反相）。"""
    walls: list[dict[str, Any]] = []
    for index, item in enumerate(companion.get("walls") or []):
        if not isinstance(item, dict):
            continue
        sense = _clamp_int(item.get("sense"), 1, 0, 99)
        walls.append(
            {
                "_id": f"companion.{index}",
                "c": item.get("c"),
                "move": 1 if _clamp_int(item.get("move"), 1, 0, 99) else 0,
                "sight": 0 if sense == 1 else 1,
                "light": 1 if _clamp_int(item.get("move"), 1, 0, 99) else 0,
                "door": _clamp_int(item.get("door"), 0, 0, 99),
                "ds": 0,
            }
        )
    return walls


def companion_lights(companion: dict[str, Any]) -> list[dict[str, Any]]:
    """v8 伴生 JSON 光 → v13 形态（``tintColor/tintAlpha`` → ``config.color/alpha``）。"""
    lights: list[dict[str, Any]] = []
    for index, item in enumerate(companion.get("lights") or []):
        if not isinstance(item, dict):
            continue
        lights.append(
            {
                "_id": f"companion.{index}",
                "x": item.get("x"),
                "y": item.get("y"),
                "hidden": False,
                "config": {
                    "dim": item.get("dim"),
                    "bright": item.get("bright"),
                    "color": item.get("tintColor"),
                    "alpha": item.get("tintAlpha"),
                    "angle": 360,
                },
            }
        )
    return lights


# -- 单场景 / 全包 --------------------------------------------------------


def convert_scene(
    scene: dict[str, Any],
    *,
    scene_id: str,
    walls: list[dict[str, Any]] | None = None,
    lights: list[dict[str, Any]] | None = None,
    tokens: list[dict[str, Any]] | None = None,
    companion: dict[str, Any] | None = None,
    background: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一个 FVTT 场景 → veil-map 导入载荷（图片像素坐标 + 直供网格原点）。

    ``background`` 是调用方解析好的 ``{"path", "name"}``（zip 内资产）；
    ``None`` 表示包内找不到背景图。抛 :class:`SceneSkip` 表示不可转换。
    """
    title = str(scene.get("name") or scene_id).strip()[:80]
    grid = scene_grid(scene, companion)
    grid_size = grid["size"]
    if grid["type"] != GRID_TYPE_SQUARE:
        raise SceneSkip(
            "fvtt.grid_hex",
            f"场景「{title}」是六边形网格（grid.type={grid['type']}），本期不支持，已跳过。",
            title=title,
        )
    if not GRID_MIN <= grid_size <= GRID_MAX:
        raise SceneSkip(
            "fvtt.grid_range",
            f"场景「{title}」的网格像素（{grid_size:g}）不在 {GRID_MIN}–{GRID_MAX} 范围内，已跳过。",
            title=title,
        )
    if background is None or not background.get("path"):
        raise SceneSkip(
            "fvtt.background_missing",
            f"场景「{title}」的背景图在包内找不到，已跳过。",
            title=title,
        )
    if background.get("bytes", 0) > MAX_BACKGROUND_BYTES:
        raise SceneSkip(
            "fvtt.background_too_large",
            f"场景「{title}」的背景图超过 40 MB 上限，已跳过。",
            title=title,
        )
    if grid["units"] not in {"ft", "feet", "foot"}:
        raise SceneSkip(
            "fvtt.grid_units",
            f"场景「{title}」的网格单位是「{grid['units']}」，本期只支持英尺，已跳过。",
            title=title,
        )

    wall_docs = walls if walls is not None else companion_walls(companion or {})
    light_docs = lights if lights is not None else companion_lights(companion or {})
    out_walls, wall_warnings = convert_walls(wall_docs)
    out_lights, light_warnings = convert_lights(light_docs, grid["distance"])
    out_tokens, token_warnings = convert_tokens(tokens or [])
    origin_x, origin_y = scene_grid_origin(scene, companion)
    warnings = wall_warnings + light_warnings + token_warnings

    # 地图边界按底图尺寸推导；背景尺寸由调用方解析资产时带上（取不到时留
    # ``None``，由 veil-map 的 import_scene 用真实图片尺寸兜底推导，口径一致）。
    width = _number(background.get("width"))
    height = _number(background.get("height"))
    bounds_cells: int | None = None
    if width > 0 and height > 0:
        bounds_cells = scene_bounds_cells(width, height, grid_size)
        raw_cells = max(math.ceil(width / grid_size), math.ceil(height / grid_size))
        if raw_cells > BOUNDS_MAX:
            warnings.append(
                f"底图需要 {raw_cells} 格边界，超过上限 {BOUNDS_MAX} 格，已按上限导入。"
            )
        warnings.extend(
            verify_wall_extent(
                out_walls, width=width, height=height, grid_size=grid_size
            )
        )

    return {
        "scene_id": safe_scene_id(scene_id),
        "source_scene_id": str(scene_id),
        "title": title or "未命名场景",
        "active": bool(scene.get("active")),
        "grid": {
            "size": int(round(grid_size)),
            "origin_x": origin_x,
            "origin_y": origin_y,
        },
        "background": dict(background),
        "bounds_cells": bounds_cells,
        "walls": out_walls,
        "lights": out_lights,
        "tokens": out_tokens,
        "ambient": convert_ambient(scene, companion),
        "warnings": warnings,
        "source_counts": {
            "walls": len(wall_docs),
            "lights": len(light_docs),
            "tokens": len(tokens or []),
        },
    }


def convert_world_scenes(
    index: Any,
    *,
    resolve_background: Any = None,
    companion_for: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """整包转换：``SceneIndex`` → (可导入载荷, 跳过记录, 全局警告)。

    ``resolve_background(scene_doc) -> {"path","name"} | None`` 由调用方
    注入（zip 资产解析）；``companion_for(scene_doc)`` 返回伴生 JSON（没有
    传 None）。子集合缺位时用伴生 JSON 兜底——两个样例包实测伴生 JSON 与
    LevelDB 逐场景完全一致（墙折线 100% 重合），LevelDB 为主、伴生兜底。
    """
    converted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    order = sorted(
        index.documents.items(),
        key=lambda item: (not bool(item[1].get("active")), str(item[1].get("name") or item[0])),
    )
    for scene_id, doc in order:
        title = str(doc.get("name") or scene_id)
        try:
            walls = index.walls.get(scene_id)
            lights = index.lights.get(scene_id)
            tokens = index.tokens.get(scene_id) or []
            companion = companion_for(doc) if companion_for else None
            # 伴生 JSON 兜底：仅当 LevelDB 子集合缺位时启用（两个样例包实测
            # 伴生 JSON 与 LevelDB 逐场景完全一致，LevelDB 为主源）。
            used_companion = False
            if companion is not None and (walls is None or lights is None):
                used_companion = True
                if walls is None:
                    walls = companion_walls(companion)
                if lights is None:
                    lights = companion_lights(companion)
            background = resolve_background(doc) if resolve_background else None
            payload = convert_scene(
                doc,
                scene_id=scene_id,
                walls=walls or [],
                lights=lights or [],
                tokens=tokens,
                companion=companion if used_companion else None,
                background=background,
            )
        except SceneSkip as exc:
            skipped.append({"title": exc.title or title, "code": exc.code, "message": exc.message})
            warnings.append(exc.message)
            continue
        converted.append(payload)
        warnings.extend(f"场景「{payload['title']}」：{text}" for text in payload["warnings"])
    if index.orphans:
        warnings.append(
            f"{len(index.orphans)} 个棋子/墙/光源条目引用了不存在的场景 id，未导入。"
        )
    return converted, skipped, warnings


__all__ = [
    "BOUNDS_MAX",
    "BOUNDS_MIN",
    "DISPOSITION_COLORS",
    "GRID_MAX",
    "GRID_MIN",
    "MAX_LIGHTS",
    "MAX_TOKENS",
    "MAX_WALLS",
    "SceneSkip",
    "companion_lights",
    "companion_walls",
    "convert_ambient",
    "convert_lights",
    "convert_scene",
    "convert_tokens",
    "convert_walls",
    "convert_world_scenes",
    "safe_scene_id",
    "scene_background_src",
    "scene_bounds_cells",
    "scene_grid",
    "scene_grid_origin",
    "verify_wall_extent",
]
