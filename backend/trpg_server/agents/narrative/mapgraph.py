"""地图图谱层：大节点（map）/ 兴趣点（poi）/ 跨图过场（portal）。

本模块只做「叙事图语义」的确定性投影，不读地图分片、不认识格子/像素：

- `AreaBrief`：一张图的背景、环境、出口与已发现兴趣点索引；
- `PoiCard`：单个兴趣点的标题、入口摘要、在场资产与遭遇块；
- 区域解析：任意叙事位置 → 所属大节点（修复 POI id 查不到底图的问题）。

旧剧本没有 `node_role` / `map` / `poi` 字段时，全部函数返回空结果，
调用方据此保持原行为（`narrative.no_poi_layer`）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.model import (
    NODE_ROLE_MAP,
    NODE_ROLE_POI,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)

AREA_BRIEF_MAX_CHARS = 600
POI_BODY_MAX_CHARS = 1200
POI_TRIGGER_MODES = ("enter", "adjacent", "inspect", "manual")
POI_STATUS_UNDISCOVERED = "undiscovered"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


# ---------------------------------------------------------------------------
# 区域与兴趣点索引
# ---------------------------------------------------------------------------


def map_nodes(bundle: NarrativeBundle) -> list[NarrativeNode]:
    return [node for node in bundle.nodes if node.is_map]


def poi_nodes(bundle: NarrativeBundle, area_id: str | None = None) -> list[NarrativeNode]:
    key = _text(area_id)
    result = [node for node in bundle.nodes if node.is_poi]
    if key:
        result = [node for node in result if poi_area_id(node) == key]
    return result


def poi_area_id(node: NarrativeNode | None) -> str:
    if node is None:
        return ""
    block = node.poi_block
    return _text(block.get("area_id")) or _text(node.parent)


def area_of(bundle: NarrativeBundle, location_id: Any) -> NarrativeNode | None:
    """任意叙事位置 → 所属大节点；位置本身是 map 时返回它自己。"""
    node = bundle.node(_text(location_id))
    seen: set[str] = set()
    while node is not None and node.id not in seen:
        seen.add(node.id)
        if node.is_map:
            return node
        block = node.poi_block
        next_id = _text(block.get("area_id")) or _text(node.parent)
        node = bundle.node(next_id)
    return None


def area_id_of(bundle: NarrativeBundle, location_id: Any) -> str:
    area = area_of(bundle, location_id)
    return area.id if area is not None else ""


def has_poi_layer(bundle: NarrativeBundle) -> bool:
    return any(node.is_map for node in bundle.nodes) and any(
        node.is_poi for node in bundle.nodes
    )


def scene_id_of(area: NarrativeNode) -> str:
    """`scene_id` 强制等于 `area_id`（本轮不做 scene_variant）。"""
    block = area.map_block
    return _text(block.get("scene_id")) or area.id


def entry_pois(bundle: NarrativeBundle, area: NarrativeNode) -> list[str]:
    block = area.map_block
    declared = _str_list(block.get("entry_pois"))
    if declared:
        return declared
    return [edge.to_id for edge in bundle.edges if edge.type == "leads_to" and edge.from_id == area.id]


def poi_code(node: NarrativeNode) -> str:
    return _text(node.poi_block.get("code"))


def poi_label(node: NarrativeNode) -> str:
    """去掉 DM 编号前缀的玩家可见标题。"""
    title = node.title or node.id
    code = poi_code(node)
    if code and title.startswith(f"{code}."):
        return title[len(code) + 1:].strip() or title
    return title


def poi_status(state: Mapping[str, Any], node: NarrativeNode) -> str:
    discovered = state.get("poi_discovered")
    triggered = state.get("poi_triggered")
    if isinstance(triggered, Mapping) and node.id in triggered:
        return "completed"
    if isinstance(discovered, Mapping) and node.id in discovered:
        return "visited"
    status = (state.get("node_status") or {}) if isinstance(state.get("node_status"), Mapping) else {}
    value = str(status.get(node.id) or "")
    if value in {"visited", "active", "completed"}:
        return value
    return POI_STATUS_UNDISCOVERED


def poi_discovered(state: Mapping[str, Any], node: NarrativeNode) -> bool:
    return poi_status(state, node) != POI_STATUS_UNDISCOVERED


# ---------------------------------------------------------------------------
# 几何：只读叙事图里的格坐标，不碰底图像素
# ---------------------------------------------------------------------------


def poi_rect(node: NarrativeNode) -> dict[str, int] | None:
    geometry = node.poi_block.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    rect = geometry.get("rect_cells")
    if not isinstance(rect, Mapping):
        return None
    width = _as_int(rect.get("w"), _as_int(rect.get("width"), 0))
    height = _as_int(rect.get("h"), _as_int(rect.get("height"), 0))
    if width <= 0 or height <= 0:
        return None
    return {
        "x": _as_int(rect.get("x"), 0),
        "y": _as_int(rect.get("y"), 0),
        "w": width,
        "h": height,
    }


def poi_center(node: NarrativeNode) -> tuple[float, float] | None:
    rect = poi_rect(node)
    if rect is not None:
        return (rect["x"] + rect["w"] / 2.0, rect["y"] + rect["h"] / 2.0)
    geometry = node.poi_block.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    center = geometry.get("center_cells")
    if isinstance(center, Mapping):
        return (float(_as_int(center.get("x"), 0)), float(_as_int(center.get("y"), 0)))
    return None


def poi_trigger_mode(node: NarrativeNode) -> str:
    trigger = node.poi_block.get("trigger")
    mode = _text(trigger.get("mode")) if isinstance(trigger, Mapping) else ""
    return mode if mode in POI_TRIGGER_MODES else "manual"


def poi_contains_cell(node: NarrativeNode, cell: tuple[int, int]) -> bool:
    rect = poi_rect(node)
    if rect is None:
        return False
    x, y = int(cell[0]), int(cell[1])
    return (
        rect["x"] <= x < rect["x"] + rect["w"]
        and rect["y"] <= y < rect["y"] + rect["h"]
    )


def poi_distance_cells(node: NarrativeNode, cell: tuple[int, int]) -> int | None:
    rect = poi_rect(node)
    x, y = int(cell[0]), int(cell[1])
    if rect is not None:
        dx = max(rect["x"] - x, 0, x - (rect["x"] + rect["w"] - 1))
        dy = max(rect["y"] - y, 0, y - (rect["y"] + rect["h"] - 1))
        return max(dx, dy)
    center = poi_center(node)
    if center is None:
        return None
    return int(max(abs(center[0] - x), abs(center[1] - y)))


def pois_within(
    bundle: NarrativeBundle, area_id: str, cell: tuple[int, int], radius: int = 1
) -> list[NarrativeNode]:
    result: list[NarrativeNode] = []
    for poi in poi_nodes(bundle, area_id):
        distance = poi_distance_cells(poi, cell)
        if distance is not None and distance <= max(0, radius):
            result.append(poi)
    return result


# ---------------------------------------------------------------------------
# 资产：NPC / 线索 / 物品
# ---------------------------------------------------------------------------


def _linked_nodes(
    bundle: NarrativeBundle, poi_id: str, edge_type: str
) -> list[NarrativeNode]:
    result: list[NarrativeNode] = []
    for edge in bundle.edges:
        if edge.type != edge_type or edge.from_id != poi_id:
            continue
        node = bundle.node(edge.to_id)
        if node is not None:
            result.append(node)
    return result


def poi_assets(bundle: NarrativeBundle, node: NarrativeNode) -> dict[str, list[str]]:
    npcs = [item.id for item in _linked_nodes(bundle, node.id, "inhabits")]
    clues = [item.id for item in _linked_nodes(bundle, node.id, "reveals")]
    return {"npcs": npcs, "clues": clues}


# ---------------------------------------------------------------------------
# AreaBrief
# ---------------------------------------------------------------------------


def _environment(area: NarrativeNode) -> dict[str, Any]:
    block = area.map_block
    raw = block.get("environment")
    environment = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "lighting": _text(environment.get("lighting")),
        "temperature": _text(environment.get("temperature")),
        "smell": _text(environment.get("smell")),
        "sound": _text(environment.get("sound")),
        "terrain": _str_list(environment.get("terrain")),
        "weather": _text(environment.get("weather")),
    }


def area_exits(bundle: NarrativeBundle, area: NarrativeNode) -> list[dict[str, Any]]:
    """出口派生视图：`portal` 边优先，其次 map.exits 声明。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edge in bundle.edges:
        if edge.type != "portal":
            continue
        other: str | None = None
        if edge.from_id == area.id:
            other = edge.to_id
        elif edge.to_id == area.id and edge.bidirectional:
            other = edge.from_id
        if other:
            target = bundle.node(other)
            target_area = area_of(bundle, other)
            key = target_area.id if target_area is not None else other
            if key in seen:
                continue
            seen.add(key)
            transition = edge.transition
            result.append(
                {
                    "to_area": target_area.id if target_area is not None else other,
                    "label": _text(transition.get("label")) or edge.label or (target.title if target else other),
                    "direction": _text(transition.get("direction"))
                    or ("two_way" if edge.bidirectional else "one_way"),
                    "blocked": False,
                }
            )
    block = area.map_block
    for item in block.get("exits") or []:
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("to_area"))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "to_area": key,
                "label": _text(item.get("label")),
                "direction": _text(item.get("direction")) or "one_way",
                "blocked": False,
            }
        )
    return result


def area_brief(
    bundle: NarrativeBundle,
    state: Mapping[str, Any],
    area_id: str,
    *,
    include_dm: bool = False,
) -> dict[str, Any]:
    area = bundle.node(area_id)
    if area is None or not area.is_map:
        return {}
    block = area.map_block
    pois = poi_nodes(bundle, area.id)
    discovered = [node for node in pois if poi_discovered(state, node)]
    discovered_payload = [
        {
            "poi_id": node.id,
            "label": poi_label(node),
            "status": poi_status(state, node),
        }
        for node in discovered
    ]
    payload: dict[str, Any] = {
        "area_id": area.id,
        "scene_id": scene_id_of(area),
        "title": area.title or area.id,
        "brief": _text(block.get("brief")) or area.summary,
        "environment": _environment(area),
        "exits": area_exits(bundle, area),
        "discovered_pois": discovered_payload,
        "hidden_poi_count": max(0, len(pois) - len(discovered)),
        "entry_pois": _str_list(block.get("entry_pois")),
        "has_poi_layer": has_poi_layer(bundle),
    }
    if include_dm:
        payload["dm_brief"] = _text(block.get("dm_brief"))
    return payload


def render_area_brief(
    bundle: NarrativeBundle,
    state: Mapping[str, Any],
    area_id: str,
    *,
    max_chars: int = AREA_BRIEF_MAX_CHARS,
) -> str:
    brief = area_brief(bundle, state, area_id)
    if not brief:
        return ""
    environment = brief.get("environment") or {}
    parts: list[str] = []
    if environment.get("lighting"):
        parts.append(environment["lighting"])
    if environment.get("smell"):
        parts.append(environment["smell"])
    if environment.get("sound"):
        parts.append(environment["sound"])
    terrain = environment.get("terrain") or []
    if terrain:
        parts.append("、".join(terrain))
    lines = [f"【当前地图】{brief['title']}（{brief['area_id']}）"]
    if brief.get("brief"):
        lines.append(str(brief["brief"]))
    if parts:
        lines.append("环境：" + "；".join(parts) + "。")
    exits = brief.get("exits") or []
    if exits:
        lines.append(
            "出口：" + "、".join(
                f"{item.get('label') or item.get('to_area')}（{'双向' if item.get('direction') == 'two_way' else '单向'}）"
                for item in exits[:4]
            )
        )
    discovered = brief.get("discovered_pois") or []
    if discovered:
        lines.append("已发现：" + "、".join(item["label"] for item in discovered[:8]))
    hidden = _as_int(brief.get("hidden_poi_count"), 0)
    if hidden:
        lines.append(f"未发现：其余 {hidden} 处（不要提及其编号或内容）。")
    text = "\n".join(lines)
    limit = max(80, _as_int(max_chars, AREA_BRIEF_MAX_CHARS))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# PoiCard
# ---------------------------------------------------------------------------


def normalize_encounter(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    monster_ids = _str_list(raw.get("monster_ids"))
    if not monster_ids:
        # 没有怪物数据时视为“无遭遇”，避免给出假的 encounter_ready。
        return None
    count = max(1, _as_int(raw.get("count"), len(monster_ids)))
    return {
        "id": _text(raw.get("id")) or "",
        "monster_ids": monster_ids,
        "count": count,
        "trigger": _text(raw.get("trigger")) or "on_enter",
        # 默认不自动开战：由剧本显式配置或 KP 调工具。
        "auto_start": bool(raw.get("auto_start", False)),
        "surprise": _str_list(raw.get("surprise")),
        "placement": dict(raw.get("placement")) if isinstance(raw.get("placement"), Mapping) else {},
    }


def poi_card(
    bundle: NarrativeBundle,
    state: Mapping[str, Any],
    poi_id: str,
    *,
    module_content: str = "",
) -> dict[str, Any]:
    node = bundle.node(poi_id)
    if node is None or not node.is_poi:
        return {}
    block = node.poi_block
    assets = poi_assets(bundle, node)
    status = poi_status(state, node)
    encounter = normalize_encounter(block.get("encounter"))
    body = module_content or node.body or node.summary
    excerpt = body[:POI_BODY_MAX_CHARS]
    return {
        "poi_id": node.id,
        "area_id": poi_area_id(node),
        "code": poi_code(node),
        "title": node.title or node.id,
        "label": poi_label(node),
        "status": status,
        "discovered": status != POI_STATUS_UNDISCOVERED,
        "entry_brief": _text(block.get("entry_brief")) or node.summary,
        "card": {
            "read_aloud": _text(block.get("read_aloud")) or node.summary,
            "features": _str_list(block.get("features")),
            "npcs": [bundle.node(item).title if bundle.node(item) else item for item in assets["npcs"]],
            "monsters": (
                [{"monster_id": item, "count": encounter["count"]} for item in encounter["monster_ids"]]
                if encounter
                else []
            ),
            "clues": [bundle.node(item).title if bundle.node(item) else item for item in assets["clues"]],
            "items": _str_list(block.get("items")),
        },
        "encounter": encounter,
        "encounter_ready": bool(encounter),
        "repeatable": bool(block.get("repeatable", node.repeatable)),
        "content_ref": dict(node.body_ref),
        "body_excerpt": excerpt,
        "body_truncated": len(body) > POI_BODY_MAX_CHARS,
        "dm_notes": _text(block.get("dm_notes")),
    }


def poi_event(
    bundle: NarrativeBundle,
    state: Mapping[str, Any],
    poi_id: str,
    *,
    trigger: str = "enter",
) -> dict[str, Any]:
    """`move_token` 返回给模型的最小事件：不含正文，只给入口摘要。"""
    node = bundle.node(poi_id)
    if node is None or not node.is_poi:
        return {}
    was_discovered = poi_discovered(state, node)
    block = node.poi_block
    return {
        "poi_id": node.id,
        "code": poi_code(node),
        "label": poi_label(node),
        "trigger": trigger,
        "first_time": not was_discovered,
        "entry_brief": _text(block.get("entry_brief")) or node.summary,
        "content_ref": dict(node.body_ref),
        "encounter_ready": normalize_encounter(block.get("encounter")) is not None,
    }


# ---------------------------------------------------------------------------
# 跨图 portal
# ---------------------------------------------------------------------------


def portal_edges_from(
    bundle: NarrativeBundle, location_id: str
) -> list[NarrativeEdge]:
    location = _text(location_id)
    result: list[NarrativeEdge] = []
    for edge in bundle.edges:
        if edge.type != "portal":
            continue
        if edge.from_id == location or (edge.to_id == location and edge.bidirectional):
            result.append(edge)
    return result


def portal_target(
    bundle: NarrativeBundle, edge: NarrativeEdge, location_id: str
) -> str:
    """从当前坐标出发，这条 portal 指向的另一端。"""
    if edge.from_id == _text(location_id):
        return edge.to_id
    return edge.from_id


def transition_entry_poi(edge: NarrativeEdge) -> str:
    transition = edge.transition
    entry = _text(transition.get("entry_poi"))
    if entry:
        return entry
    # 反向走时 entry_poi 描述的是正向目标，不能直接复用。
    return ""


def resolve_area_for_location(bundle: NarrativeBundle | None, location_id: Any) -> str:
    """给地图插件用的区域解析：POI → 所属 map；map → 自己；其它 → 空串。"""
    if bundle is None:
        return ""
    return area_id_of(bundle, location_id)


def iter_poi_ids(nodes: Iterable[NarrativeNode]) -> list[str]:
    return [node.id for node in nodes if node.is_poi]


__all__ = [
    "AREA_BRIEF_MAX_CHARS",
    "POI_BODY_MAX_CHARS",
    "NODE_ROLE_MAP",
    "NODE_ROLE_POI",
    "area_brief",
    "area_exits",
    "area_id_of",
    "area_of",
    "entry_pois",
    "has_poi_layer",
    "iter_poi_ids",
    "map_nodes",
    "normalize_encounter",
    "poi_area_id",
    "poi_assets",
    "poi_card",
    "poi_center",
    "poi_code",
    "poi_contains_cell",
    "poi_discovered",
    "poi_distance_cells",
    "poi_event",
    "poi_label",
    "poi_nodes",
    "poi_rect",
    "poi_status",
    "poi_trigger_mode",
    "pois_within",
    "portal_edges_from",
    "portal_target",
    "render_area_brief",
    "resolve_area_for_location",
    "scene_id_of",
]
