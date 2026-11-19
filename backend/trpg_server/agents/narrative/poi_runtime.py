"""兴趣点解析服务：veil-map 移动成功后由核心给出结构化结论。

边界：本模块只读叙事图与房间运行态，不读 `scene_store` 私有文件；格子位置
由 veil-map 传入。没有 POI 层（旧剧本 / CoC7）时返回空结果，调用方保持原行为。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.mapgraph import (
    area_of,
    has_poi_layer,
    poi_contains_cell,
    poi_distance_cells,
    poi_event,
    poi_nodes,
    poi_trigger_mode,
)
from trpg_server.agents.narrative.state_store import (
    apply_to_narrative,
    load_narrative_state,
    save_narrative_state,
)
from trpg_server.agents.narrative.store import (
    load_bundle,
    scenario_dir_for_descriptor,
)
from trpg_server.scenario_store import load_scenario_by_id

logger = logging.getLogger(__name__)


def _load_bundle_for_room(room_dir: Path | str, room_info: Mapping[str, Any], scenarios_dir: Any):
    scenario_id = str((room_info or {}).get("scenario_id") or "").strip()
    if not scenario_id or not scenarios_dir:
        return None
    try:
        descriptor, _scenario = load_scenario_by_id(
            Path(scenarios_dir), scenario_id, (room_info or {}).get("scenario_version")
        )
        if not descriptor:
            return None
        return load_bundle(scenario_dir_for_descriptor(descriptor))
    except Exception:  # noqa: BLE001 - 叙事图不可用时地图照常工作
        logger.debug("poi.bundle_unavailable room=%s", room_dir, exc_info=True)
        return None


def poi_layer_available(
    room_dir: Path | str, room_info: Mapping[str, Any], scenarios_dir: Any
) -> bool:
    bundle = _load_bundle_for_room(room_dir, room_info, scenarios_dir)
    return bool(bundle is not None and has_poi_layer(bundle))


def _trigger_radius(node, default: int = 1) -> int:
    block = node.poi_block
    trigger = block.get("trigger")
    if isinstance(trigger, Mapping):
        try:
            return max(0, int(trigger.get("radius_cells", default)))
        except (TypeError, ValueError):
            return default
    return default


def _walk_cells(
    path_cells: Iterable[Any], from_cell: tuple[int, int], to_cell: tuple[int, int]
) -> list[tuple[int, int]]:
    cells: list[tuple[int, int]] = []
    for item in path_cells or []:
        try:
            cells.append((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not cells:
        # 没有逐格路径时退化为起点与终点，足够覆盖「到达/离开」判定。
        cells = [from_cell, to_cell]
    return cells


def adjacent_reached(
    bundle, area_id: str, from_cell: tuple[int, int], to_cell: tuple[int, int]
) -> list[str]:
    """相邻触发用「目标格比起点更近」判定，避免走开时重复触发。"""
    result: list[str] = []
    for node in poi_nodes(bundle, area_id):
        if poi_trigger_mode(node) != "adjacent":
            continue
        radius = _trigger_radius(node)
        before = poi_distance_cells(node, from_cell)
        after = poi_distance_cells(node, to_cell)
        if after is None or before is None:
            continue
        if after <= radius and after < before:
            result.append(node.id)
    return result


def enter_reached(
    bundle, area_id: str, path_cells: Iterable[Any], to_cell: tuple[int, int]
) -> list[str]:
    """进入触发：路径上任意一格落在区域，或终点落在区域内。"""
    result: list[str] = []
    cells = list(path_cells or [])
    for node in poi_nodes(bundle, area_id):
        if poi_trigger_mode(node) != "enter":
            continue
        if any(poi_contains_cell(node, cell) for cell in cells) or poi_contains_cell(node, to_cell):
            result.append(node.id)
    return result


def pois_on_path(
    room_dir: Path | str,
    room_info: Mapping[str, Any],
    scenarios_dir: Any,
    *,
    cells: Iterable[Any],
    to_cell: tuple[int, int],
    from_cell: tuple[int, int],
) -> list[str]:
    bundle = _load_bundle_for_room(room_dir, room_info, scenarios_dir)
    if bundle is None:
        return []
    state = load_narrative_state(room_dir)
    area = area_of(bundle, (room_info or {}).get("active_scene_id") or state.get("location"))
    area_id = area.id if area is not None else ""
    if not area_id:
        return []
    found = enter_reached(bundle, area_id, _walk_cells(cells, from_cell, to_cell), to_cell)
    for poi_id in adjacent_reached(bundle, area_id, from_cell, to_cell):
        if poi_id not in found:
            found.append(poi_id)
    return found


def resolve_after_move(
    room_dir: Path | str,
    *,
    area_id: str,
    token: Mapping[str, Any],
    from_cell: tuple[int, int],
    to_cell: tuple[int, int],
    path_cells: Iterable[Any] = (),
    room_info: Mapping[str, Any] | None = None,
    scenarios_dir: Any = None,
) -> dict[str, Any]:
    """移动成功后的结构化回执；无 POI 层时返回空结果而不是报错。"""
    bundle = _load_bundle_for_room(room_dir, room_info or {}, scenarios_dir)
    empty = {
        "enter": [],
        "adjacent": [],
        "exit": [],
        "encounter_ready": False,
        "area_transition": None,
        "state_patch": {},
        "events": [],
    }
    if bundle is None or not has_poi_layer(bundle):
        return empty
    area = bundle.node(area_id)
    if area is None or not area.is_map:
        return empty
    state = load_narrative_state(room_dir)
    walk = _walk_cells(path_cells, from_cell, to_cell)
    entered = enter_reached(bundle, area.id, walk, to_cell)
    adjacent = [poi_id for poi_id in adjacent_reached(bundle, area.id, from_cell, to_cell) if poi_id not in entered]
    events: list[dict[str, Any]] = []
    encounter_ready = False
    for poi_id in [*entered, *adjacent]:
        trigger = "enter" if poi_id in entered else "adjacent"
        event = poi_event(bundle, state, poi_id, trigger=trigger)
        if not event:
            continue
        events.append(event)
        encounter_ready = encounter_ready or bool(event.get("encounter_ready"))
    if events:
        payloads = [
            {
                "kind": "poi",
                "poi_id": item["poi_id"],
                "triggered": bool(item.get("first_time")),
                "source": "map_move",
                "location_id": item["poi_id"],
                "turn": int(((state.get("clock") or {}).get("turn")) or 0),
                "token_id": str((token or {}).get("id") or ""),
                "cell": {"cell_x": int(to_cell[0]), "cell_y": int(to_cell[1])},
            }
            for item in events
        ]
        narrative = state
        for payload in payloads:
            narrative = apply_to_narrative(narrative, payload)
        save_narrative_state(room_dir, narrative)
    return {
        "enter": entered,
        "adjacent": adjacent,
        "exit": [],
        "encounter_ready": encounter_ready,
        "area_transition": None,
        "state_patch": {},
        "events": events,
    }


__all__ = [
    "adjacent_reached",
    "enter_reached",
    "poi_layer_available",
    "pois_on_path",
    "resolve_after_move",
]
