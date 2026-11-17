"""外部地图导入器：内容探测、格式解析与统一中文错误。

支持范围（不做“看起来兼容”的静默降级）
--------------------------------------
1. 裸图片：PNG / JPG / WEBP / BMP。
2. Universal VTT（``.uvtt`` / ``.dd2vtt``，Dungeondraft 等导出）：JSON 内嵌
   base64 图片，并带以“格”为单位的墙折线、门、光源（半径 / 颜色 / 强度）、
   环境光、分辨率与每格像素数。导入后会一并带出墙体、门与光源。
3. Foundry VTT 场景 JSON（Export Scene Data）：仅当场景把背景图内嵌为
   ``data:image/...`` base64 时支持。``background.src`` 指向外部文件路径的
   场景不会去读取服务器或网络文件，会返回明确错误。可导入墙体、门、光源与
   网格；Foundry 的棋子、地形、音效与宏不在支持范围内。

所有格式都按内容判断（JSON 看结构、图片看 magic bytes），扩展名只用于展示。
墙线与光源最终转换为“图片中心为原点”的世界像素坐标，和 ``spatial.py`` 的
坐标约定一致；网格锚点单独返回，由调用方换算成 ``grid_offset``。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from typing import Any

MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_IMPORT_WALLS = 200
MAX_IMPORT_LIGHTS = 100
FEET_PER_CELL = 5
GRID_MIN = 16
GRID_MAX = 256

FORMAT_IMAGE = "image"
FORMAT_UVTT = "uvtt"
FORMAT_FOUNDRY = "foundry"

FORMAT_LABELS = {
    FORMAT_IMAGE: "地图图片",
    FORMAT_UVTT: "Universal VTT",
    FORMAT_FOUNDRY: "Foundry 场景 JSON",
}

SUPPORTED_SCOPE = (
    "支持 PNG / JPG / WEBP / BMP 图片、Universal VTT（.uvtt / .dd2vtt）"
    "以及内嵌背景图的 Foundry VTT 场景 JSON；Foundry 场景只读取内嵌 data:image，"
    "不读取外部文件路径，也不导入棋子与音效。"
)


class ImportError(Exception):
    """导入失败；``code`` 为稳定错误码，``message`` 面向用户。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def _positive_int(value: Any) -> int:
    number = _number(value, 0.0)
    return int(round(number)) if number > 0 else 0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _color(value: Any, fallback: str = "#ffffff") -> str:
    """把 ``#rrggbb`` / ``rrggbb`` / 0xRRGGBB 统一成 ``#rrggbb``。"""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number = int(value)
        if 0 <= number <= 0xFFFFFF:
            return f"#{number:06x}"
        return fallback
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        text = "".join(ch * 2 for ch in text)
    if len(text) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return f"#{text.lower()}"
    return fallback


def _rgba_color(value: Any) -> tuple[str, float]:
    """UVTT 环境光是 ``[r, g, b, a]``（0–1 浮点或 0–255 整数）。"""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return "#ffffff", 1.0
    channels = [_number(item, 0.0) for item in value[:4]]
    if any(channel > 1.0 for channel in channels):
        channels = [channel / 255.0 for channel in channels]
    red, green, blue = (
        int(_clamp(channel, 0.0, 1.0) * 255 + 0.5) for channel in channels[:3]
    )
    alpha = _clamp(channels[3], 0.0, 1.0) if len(channels) >= 4 else 1.0
    return f"#{red:02x}{green:02x}{blue:02x}", alpha


def _decode_inline_image(value: Any) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise ImportError("map.import_image_missing", "文件里没有内嵌地图图片。")
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    compact = "".join(text.split())
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ImportError(
            "map.import_image_invalid",
            "内嵌地图图片不是合法的 base64 数据，文件可能已损坏或只导出了一半。",
        )
    if not data:
        raise ImportError("map.import_image_invalid", "内嵌地图图片是空数据。")
    return data


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError(
            "map.import_invalid_json",
            "文件看起来是 JSON，但不是 UTF-8 编码，请重新导出后再试。",
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError(
            "map.import_invalid_json",
            f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列），文件可能不完整。",
        )
    if not isinstance(data, dict):
        raise ImportError("map.import_unknown_json", f"JSON 顶层必须是对象。{SUPPORTED_SCOPE}")
    return data


def _looks_like_json(raw: bytes) -> bool:
    return raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"{"


def _looks_like_uvtt(data: dict[str, Any]) -> bool:
    return isinstance(data.get("resolution"), dict) and isinstance(data.get("image"), str)


def _looks_like_foundry(data: dict[str, Any]) -> bool:
    return isinstance(data.get("walls"), list) and isinstance(data.get("grid"), dict)


def _image_meta(raw: bytes, media: Any, filename: Any = "") -> dict[str, Any]:
    try:
        meta = media.inspect_image(raw, filename)
    except media.ImageError as exc:
        status = 413 if "too_large" in exc.code else 400
        raise ImportError(exc.code, exc.message, status) from exc
    return {
        "bytes": raw,
        "suffix": str(meta["suffix"]),
        "width": int(meta["width"]),
        "height": int(meta["height"]),
        "size": int(meta["size"]),
    }


def read_import(raw: bytes, filename: Any, media: Any) -> dict[str, Any]:
    """解析上传文件为导入包；失败抛 :class:`ImportError`。

    返回结构::

        {
          "format": "image" | "uvtt" | "foundry",
          "label": "Universal VTT",
          "image": {bytes, suffix, width, height, size},
          "walls": [...] | None,          # None 表示保持现有墙体
          "lights": [...] | None,         # None 表示保持现有光源
          "ambient_light": {...} | None,
          "grid": {"size": 0, "origin_x": 0.0, "origin_y": 0.0},
          "warnings": [...],
          "needs_review": bool,
        }
    """
    if not raw:
        raise ImportError("map.import_empty", "上传的文件是空的，请重新选择。")
    if len(raw) > MAX_IMPORT_BYTES:
        limit = MAX_IMPORT_BYTES // (1024 * 1024)
        raise ImportError(
            "map.import_too_large",
            f"地图文件不能超过 {limit} MB（UVTT 含 base64 图片时会更大），请压缩图片后重新导出。",
            413,
        )
    if not _looks_like_json(raw):
        meta = _image_meta(raw, media, filename)
        return {
            "format": FORMAT_IMAGE,
            "label": FORMAT_LABELS[FORMAT_IMAGE],
            "image": meta,
            "walls": None,
            "lights": None,
            "ambient_light": None,
            "grid": {"size": 0, "origin_x": 0.0, "origin_y": 0.0},
            "warnings": [],
            "needs_review": False,
        }

    data = _parse_json(raw)
    if _looks_like_uvtt(data):
        return _read_uvtt(data, media)
    if _looks_like_foundry(data):
        return _read_foundry(data, media)
    raise ImportError(
        "map.import_unknown_json",
        f"无法识别的 JSON 地图格式。{SUPPORTED_SCOPE}",
    )


def _uvtt_points(
    raw_points: Any, origin_x: float, origin_y: float, grid: int, center_x: float, center_y: float
) -> list[dict[str, int]]:
    points: list[dict[str, int]] = []
    for item in raw_points if isinstance(raw_points, list) else []:
        if isinstance(item, dict):
            gx, gy = _number(item.get("x"), math.nan), _number(item.get("y"), math.nan)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            gx, gy = _number(item[0], math.nan), _number(item[1], math.nan)
        else:
            continue
        if not (math.isfinite(gx) and math.isfinite(gy)):
            continue
        points.append(
            {
                "x": int(round(origin_x + gx * grid - center_x)),
                "y": int(round(origin_y + gy * grid - center_y)),
            }
        )
    return points


def _wall_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"window", "glass"}:
        return "window"
    if text in {"door", "secret"}:
        return "door"
    if text == "terrain":
        return "terrain"
    return "wall"


def _read_uvtt(data: dict[str, Any], media: Any) -> dict[str, Any]:
    resolution = data.get("resolution")
    if not isinstance(resolution, dict):
        raise ImportError("map.import_uvtt_invalid", "UVTT 缺少 resolution 字段，无法确定网格。")
    image = _image_meta(_decode_inline_image(data.get("image")), media)
    width, height = image["width"], image["height"]
    center_x, center_y = width / 2.0, height / 2.0

    grid = _positive_int(resolution.get("pixels_per_grid"))
    if grid < GRID_MIN or grid > GRID_MAX:
        raise ImportError(
            "map.import_uvtt_grid",
            (
                f"UVTT 的每格像素数（{resolution.get('pixels_per_grid')!r}）不在 "
                f"{GRID_MIN}–{GRID_MAX} 范围内，无法换算墙体坐标。请重新导出，"
                "或只把图片作为裸图片导入后手动标定。"
            ),
        )

    warnings: list[str] = []
    needs_review = False
    map_size = resolution.get("map_size") if isinstance(resolution.get("map_size"), dict) else {}
    declared_width = _positive_int(map_size.get("x"))
    declared_height = _positive_int(map_size.get("y"))
    if declared_width and abs(declared_width - width) > 1:
        warnings.append(
            f"格式记录的地图宽度是 {declared_width}px，图片实际宽度 {width}px，已按图片尺寸导入。"
        )
        needs_review = True
    if declared_height and abs(declared_height - height) > 1:
        warnings.append(
            f"格式记录的地图高度是 {declared_height}px，图片实际高度 {height}px，已按图片尺寸导入。"
        )
        needs_review = True
    if width % grid or height % grid:
        warnings.append(
            f"图片尺寸 {width}×{height}px 不是每格 {grid}px 的整数倍，边缘会不足一格，请复核网格标定。"
        )
        needs_review = True

    origin = resolution.get("map_origin") if isinstance(resolution.get("map_origin"), dict) else {}
    origin_x = _number(origin.get("x"), 0.0)
    origin_y = _number(origin.get("y"), 0.0)

    walls: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("walls") or []):
        if not isinstance(item, dict):
            continue
        points = _uvtt_points(item.get("points"), origin_x, origin_y, grid, center_x, center_y)
        if len(points) < 2:
            continue
        walls.append(
            {
                "id": f"uvtt-wall-{index + 1}",
                "kind": _wall_kind(item.get("wall_type")),
                "points": points,
            }
        )
    for index, item in enumerate(data.get("doors") or []):
        if not isinstance(item, dict):
            continue
        points = _uvtt_points(item.get("points"), origin_x, origin_y, grid, center_x, center_y)
        if len(points) < 2:
            continue
        kind = _wall_kind(item.get("door_type"))
        walls.append(
            {
                "id": f"uvtt-door-{index + 1}",
                "kind": kind if kind in {"door", "window"} else "door",
                "points": points,
                "state": "open" if bool(item.get("open")) else "closed",
            }
        )
    if len(walls) > MAX_IMPORT_WALLS:
        raise ImportError(
            "map.import_too_many_walls",
            f"地图包含 {len(walls)} 段墙体/门，超过上限 {MAX_IMPORT_WALLS} 段；请拆分地图或精简墙体。",
        )
    if walls and any(
        abs(point["x"]) > width * 2 or abs(point["y"]) > height * 2
        for wall in walls
        for point in wall["points"]
    ):
        warnings.append("部分墙体坐标远超图片范围，已保留原始位置，请在画布上复核。")
        needs_review = True

    lights: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("lights") or []):
        if not isinstance(item, dict):
            continue
        position = item.get("position") if isinstance(item.get("position"), dict) else {}
        gx, gy = _number(position.get("x"), math.nan), _number(position.get("y"), math.nan)
        if not (math.isfinite(gx) and math.isfinite(gy)):
            continue
        range_cells = max(1, _positive_int(item.get("range")))
        lights.append(
            {
                "id": f"uvtt-light-{index + 1}",
                "x": int(round(origin_x + gx * grid - center_x)),
                "y": int(round(origin_y + gy * grid - center_y)),
                "range_cells": range_cells,
                "range_ft": range_cells * FEET_PER_CELL,
                "color": _color(item.get("color")),
                "intensity": _clamp(_number(item.get("intensity"), 1.0), 0.0, 10.0),
                "shadows": bool(item.get("shadows", True)),
                "angle": 360,
            }
        )
    if len(lights) > MAX_IMPORT_LIGHTS:
        raise ImportError(
            "map.import_too_many_lights",
            f"地图包含 {len(lights)} 个光源，超过上限 {MAX_IMPORT_LIGHTS} 个。",
        )

    environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    ambient_raw = environment.get("ambient_light")
    color_value = ambient_raw.get("color") if isinstance(ambient_raw, dict) else ambient_raw
    ambient_color, ambient_alpha = _rgba_color(color_value)
    ambient_light = {
        "enabled": bool(ambient_raw),
        "color": ambient_color,
        "intensity": round(ambient_alpha, 3),
        "darkness": 0.0,
        "baked": bool(environment.get("baked_lighting")),
    }

    return {
        "format": FORMAT_UVTT,
        "label": FORMAT_LABELS[FORMAT_UVTT],
        "image": image,
        "walls": walls,
        "lights": lights,
        "ambient_light": ambient_light,
        "grid": {"size": grid, "origin_x": origin_x, "origin_y": origin_y},
        "warnings": warnings,
        "needs_review": needs_review,
    }


def _foundry_image(data: dict[str, Any], media: Any) -> dict[str, Any]:
    background = data.get("background") if isinstance(data.get("background"), dict) else {}
    src = background.get("src") or data.get("img") or ""
    if not isinstance(src, str) or not src.startswith("data:image/"):
        raise ImportError(
            "map.import_image_missing",
            (
                "这个 Foundry 场景 JSON 没有内嵌背景图（background.src 指向外部文件路径）。"
                "本服务不会读取服务器或网络文件；请改用内嵌图片的 Foundry 导出，"
                "或使用 Universal VTT，或先把图片作为裸图片导入。"
            ),
        )
    return _image_meta(_decode_inline_image(src), media)


def _foundry_flag(value: Any) -> bool:
    """Foundry 的 sight / move / light 用 0 表示不阻挡，非 0 表示阻挡。"""
    if value is None:
        return True
    return _positive_int(value) != 0


def _read_foundry(data: dict[str, Any], media: Any) -> dict[str, Any]:
    grid = data.get("grid")
    if not isinstance(grid, dict):
        raise ImportError("map.import_foundry_invalid", "Foundry 场景缺少 grid 字段，无法确定网格。")
    image = _foundry_image(data, media)
    width = _positive_int(data.get("width")) or image["width"]
    height = _positive_int(data.get("height")) or image["height"]

    warnings: list[str] = []
    needs_review = False
    if width != image["width"] or height != image["height"]:
        warnings.append(
            f"场景记录尺寸 {width}×{height}px 与背景图 {image['width']}×{image['height']}px 不一致，已按图片尺寸导入。"
        )
        needs_review = True

    grid_size = _positive_int(grid.get("size"))
    if grid_size < GRID_MIN or grid_size > GRID_MAX:
        raise ImportError(
            "map.import_foundry_grid",
            (
                f"Foundry 场景的网格像素（{grid.get('size')!r}）不在 {GRID_MIN}–{GRID_MAX} 范围内，"
                "无法换算墙体坐标；请重新导出，或只把图片作为裸图片导入。"
            ),
        )
    distance = _number(grid.get("distance"), 5.0)
    if distance <= 0:
        distance = 5.0
    units = str(grid.get("units") or "ft").strip().lower()
    if units not in {"ft", "feet", "foot", "英尺"}:
        warnings.append(
            f"场景网格单位是「{units}」，本系统按每格 {distance:g} 单位、5 英尺/格换算移动力，请复核。"
        )
        needs_review = True
    if width % grid_size or height % grid_size:
        warnings.append(
            f"图片尺寸 {width}×{height}px 不是每格 {grid_size}px 的整数倍，边缘会不足一格，请复核标定。"
        )
        needs_review = True

    center_x, center_y = image["width"] / 2.0, image["height"] / 2.0
    walls: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("walls") or []):
        if not isinstance(item, dict):
            continue
        coordinates = item.get("c")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 4:
            continue
        x1, y1, x2, y2 = (_number(value, math.nan) for value in coordinates)
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            continue
        door = _positive_int(item.get("door"))
        blocks_sight = _foundry_flag(item.get("sight"))
        blocks_movement = _foundry_flag(item.get("move"))
        blocks_light = _foundry_flag(item.get("light"))
        if door:
            kind = "door"
        elif not blocks_sight and blocks_movement:
            kind = "window"
        else:
            kind = "wall"
        state = "open" if door and _positive_int(item.get("ds")) == 2 else "closed"
        walls.append(
            {
                "id": f"foundry-wall-{index + 1}",
                "kind": kind,
                "state": state,
                "points": [
                    {"x": int(round(x1 - center_x)), "y": int(round(y1 - center_y))},
                    {"x": int(round(x2 - center_x)), "y": int(round(y2 - center_y))},
                ],
                "blocks_sight": blocks_sight,
                "blocks_movement": blocks_movement,
                "blocks_light": blocks_light,
            }
        )
    if len(walls) > MAX_IMPORT_WALLS:
        raise ImportError(
            "map.import_too_many_walls",
            f"场景包含 {len(walls)} 段墙体/门，超过上限 {MAX_IMPORT_WALLS} 段；请精简后再导入。",
        )

    lights: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("lights") or []):
        if not isinstance(item, dict) or item.get("hidden"):
            continue
        x, y = _number(item.get("x"), math.nan), _number(item.get("y"), math.nan)
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        dim = _number(config.get("dim", item.get("dim")), 0.0)
        bright = _number(config.get("bright", item.get("bright")), 0.0)
        range_cells = int(round(dim / distance)) if dim > 0 else 0
        bright_cells = int(round(bright / distance)) if bright > 0 else 0
        if range_cells <= 0 and bright_cells <= 0:
            continue
        range_cells = max(1, range_cells)
        lights.append(
            {
                "id": f"foundry-light-{index + 1}",
                "x": int(round(x - center_x)),
                "y": int(round(y - center_y)),
                "range_cells": range_cells,
                "range_ft": range_cells * FEET_PER_CELL,
                "bright_cells": min(bright_cells, range_cells),
                "color": _color(config.get("color")),
                "intensity": _clamp(_number(config.get("alpha", 1.0), 1.0), 0.0, 10.0),
                "shadows": bool(config.get("shadows", True)),
                "angle": int(_clamp(_number(config.get("angle"), 360.0), 0.0, 360.0)),
            }
        )
    if len(lights) > MAX_IMPORT_LIGHTS:
        raise ImportError(
            "map.import_too_many_lights",
            f"场景包含 {len(lights)} 个光源，超过上限 {MAX_IMPORT_LIGHTS} 个。",
        )

    environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    global_light = environment.get("globalLight") if isinstance(environment.get("globalLight"), dict) else {}
    ambient_light = {
        "enabled": bool(global_light.get("enabled")),
        "color": _color(global_light.get("color")),
        "intensity": round(_clamp(_number(global_light.get("alpha"), 1.0), 0.0, 10.0), 3),
        "darkness": round(_clamp(_number(environment.get("darkness"), 0.0), 0.0, 1.0), 3),
        "baked": False,
    }

    offset_x = _number(grid.get("offsetX"), _number(data.get("offsetX"), 0.0))
    offset_y = _number(grid.get("offsetY"), _number(data.get("offsetY"), 0.0))
    return {
        "format": FORMAT_FOUNDRY,
        "label": FORMAT_LABELS[FORMAT_FOUNDRY],
        "image": image,
        "walls": walls,
        "lights": lights,
        "ambient_light": ambient_light,
        "grid": {"size": grid_size, "origin_x": offset_x, "origin_y": offset_y},
        "warnings": warnings,
        "needs_review": needs_review,
    }
