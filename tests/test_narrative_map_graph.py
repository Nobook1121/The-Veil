"""地图图谱层：区域解析、AreaBrief/PoiCard、跨图与地图房间移动约束。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.agents.narrative import mapgraph
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.prompt_v2 import render_state_summary
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError
from trpg_server.agents.narrative.state_store import load_narrative_state, save_narrative_state
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.agents.tools.narrative import get_area, get_poi, move_to_location, travel_to
from trpg_server.plugins import runtime as plugin_runtime


def _bundle() -> NarrativeBundle:
    map_block = {
        "scene_id": "area_map",
        "entry_pois": ["poi_entry"],
        "environment": {"lighting": "昏暗", "smell": "潮湿"},
        "brief": "一座废弃矿坑。",
        "exits": [{"to_area": "site_trail", "label": "退回小径", "direction": "two_way"}],
    }
    nodes = [
        NarrativeNode(
            id="site_trail",
            kind="location",
            title="林间小径",
            extra={"node_role": "site"},
        ),
        NarrativeNode(
            id="area_map",
            kind="location",
            title="矿坑",
            extra={"node_role": "map", "map": map_block},
        ),
        NarrativeNode(
            id="poi_entry",
            kind="location",
            title="1. 入口",
            parent="area_map",
            summary="潮湿的入口。",
            extra={
                "node_role": "poi",
                "poi": {
                    "area_id": "area_map",
                    "code": "1",
                    "trigger": {"mode": "enter", "radius_cells": 0},
                    "geometry": {"rect_cells": {"x": 2, "y": 2, "w": 3, "h": 3}},
                },
            },
        ),
        NarrativeNode(
            id="poi_lair",
            kind="location",
            title="2. 巢穴",
            parent="area_map",
            summary="巢穴深处有动静。",
            body_ref={"module_id": "p1_lair"},
            extra={
                "node_role": "poi",
                "poi": {
                    "area_id": "area_map",
                    "code": "2",
                    "trigger": {"mode": "enter", "radius_cells": 0},
                    "geometry": {"rect_cells": {"x": 8, "y": 8, "w": 4, "h": 4}},
                    "encounter": {
                        "id": "enc_lair",
                        "monster_ids": ["goblin"],
                        "count": 2,
                        "auto_start": False,
                    },
                },
            },
        ),
    ]
    edges = [
        NarrativeEdge(
            id="edge_trail_map",
            from_id="site_trail",
            to_id="area_map",
            type="portal",
            bidirectional=True,
            label="洞口",
            extra={
                "transition": {
                    "to_area": "area_map",
                    "entry_poi": "poi_entry",
                    "text": "树根后露出洞口。",
                }
            },
        ),
        NarrativeEdge(id="edge_map_entry", from_id="area_map", to_id="poi_entry", type="leads_to"),
        NarrativeEdge(id="edge_entry_lair", from_id="poi_entry", to_id="poi_lair", type="next"),
        NarrativeEdge(
            id="edge_entry_trail",
            from_id="poi_entry",
            to_id="site_trail",
            type="portal",
            label="退出",
        ),
    ]
    return NarrativeBundle(scenario_id="s1", nodes=nodes, edges=edges)


def _context(tmp_path: Path, *, location: str = "site_trail") -> SimpleNamespace:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text(
        '{"id": "s1", "modules": [{"id": "p1_lair", "content": "地精在巢穴里分赃。"}]}',
        encoding="utf-8",
    )
    save_bundle(scenario_dir, _bundle())
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    save_narrative_state(room_dir, {"location": location, "clock": {"turn": 3}})
    return SimpleNamespace(
        room_dir=room_dir,
        scenarios_dir=scenarios,
        room_id="r1",
        room_info=lambda: {"scenario_id": "s1"},
    )


def test_area_resolution_from_poi_and_legacy_nodes(tmp_path) -> None:
    context = _context(tmp_path, location="poi_lair")
    runtime = NarrativeRuntime.load(context)
    assert runtime.area_id == "area_map"
    assert runtime.in_map_room() is True
    assert mapgraph.resolve_area_for_location(runtime.bundle, "poi_lair") == "area_map"
    assert mapgraph.resolve_area_for_location(runtime.bundle, "site_trail") == ""


def test_area_brief_and_poi_card(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    runtime = NarrativeRuntime.load(context)
    brief = runtime.get_area()
    assert brief["area_id"] == "area_map"
    assert brief["scene_id"] == "area_map"
    assert brief["environment"]["lighting"] == "昏暗"
    assert {item["poi_id"] for item in brief["poi_index"]} == {"poi_entry", "poi_lair"}
    by_id = {item["poi_id"]: item for item in brief["poi_index"]}
    # 当前所在位置视为已发现，未去过的巢穴仍隐藏。
    assert by_id["poi_entry"]["discovered"] is True
    assert by_id["poi_lair"]["discovered"] is False
    assert brief["hidden_poi_count"] == 1

    card = runtime.get_poi("poi_lair")
    assert card["poi_id"] == "poi_lair"
    assert card["encounter"]["auto_start"] is False
    assert card["encounter_ready"] is True
    assert card["body_excerpt"] == "地精在巢穴里分赃。"


def test_hidden_poi_rejected_for_player_view(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    result = get_poi({"poi": "poi_lair", "viewer": "player"}, context)
    assert result["ok"] is False
    assert result["code"] == "narrative.poi_hidden"


def test_move_to_location_disabled_inside_map(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    blocked = move_to_location({"location": "poi_lair"}, context)
    assert blocked["ok"] is False
    assert blocked["code"] == "narrative.use_map_movement"

    # 从无地图地点进入地图，只能走 travel_to。
    context2 = _context(tmp_path / "b", location="site_trail")
    blocked2 = move_to_location({"location": "area_map"}, context2)
    assert blocked2["ok"] is False
    assert blocked2["code"] == "narrative.use_travel_to"


def test_travel_to_switches_location_and_calls_map_service(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    class FakeService:
        def activate_for_area(self, room_dir, info, area_id, entry_poi=""):
            calls.append((area_id, entry_poi))
            return {
                "ok": True,
                "scene_id": area_id,
                "created": True,
                "placements": [{"token_id": "member-u1", "cell_x": 3, "cell_y": 3}],
            }

    plugin_runtime.register_service("veil-map", "map_service", FakeService())
    try:
        context = _context(tmp_path, location="site_trail")
        first = travel_to({"to_area": "area_map", "request_id": "req-1"}, context)
        assert first["ok"] is True
        assert first["entry_poi"] == "poi_entry"
        assert first["scene_switched"] is True
        assert first["transition_text"] == "树根后露出洞口。"
        assert calls == [("area_map", "poi_entry")]
        assert load_narrative_state(context.room_dir)["location"] == "poi_entry"

        repeat = travel_to({"to_area": "area_map", "request_id": "req-1"}, context)
        assert repeat["ok"] is True
        assert repeat["entry_poi"] == "poi_entry"
        assert calls == [("area_map", "poi_entry")]
    finally:
        plugin_runtime.unregister_plugin("veil-map")


def test_travel_to_without_service_still_moves_narrative(tmp_path) -> None:
    context = _context(tmp_path, location="site_trail")
    result = travel_to({"to_area": "area_map"}, context)
    assert result["ok"] is True
    assert result["scene_switched"] is False
    assert load_narrative_state(context.room_dir)["location"] == "poi_entry"


def test_travel_to_rejects_unknown_route(tmp_path) -> None:
    context = _context(tmp_path, location="poi_lair")
    runtime = NarrativeRuntime.load(context)
    runtime.bundle.nodes.append(
        NarrativeNode(id="site_far", kind="location", title="远方", extra={"node_role": "site"})
    )
    with pytest.raises(NarrativeRuntimeError) as info:
        runtime.travel_to("site_far")
    assert info.value.code == "narrative.no_route"


def test_state_summary_only_extends_when_area_given(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    runtime = NarrativeRuntime.load(context)
    base = render_state_summary(runtime.narrative, runtime.bundle)
    assert "地图：" not in base

    extended = render_state_summary(
        runtime.narrative, runtime.bundle, area="area_map", poi="poi_entry"
    )
    assert "地图：矿坑（area_map）" in extended
    assert "位置：1. 入口" in extended


def test_state_summary_lists_triggered_and_hides_undiscovered_nearby(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    runtime = NarrativeRuntime.load(context)
    state = dict(runtime.narrative)
    state["poi_triggered"] = {"poi_entry": {"count": 1}}

    summary = render_state_summary(
        state, runtime.bundle, area="area_map", poi="poi_entry", nearby=("poi_lair",)
    )
    assert "已触发：入口" in summary
    # 未发现的巢穴不能以名称进入状态摘要。
    assert "巢穴" not in summary

    state["poi_discovered"] = {"poi_lair": {"at": "poi_entry"}}
    extended = render_state_summary(
        state, runtime.bundle, area="area_map", poi="poi_entry", nearby=("poi_lair",)
    )
    assert "附近：巢穴" in extended
    # 展示层去掉 DM 编号。
    assert "2. 巢穴" not in extended


def test_get_area_tool_returns_code_on_missing_area(tmp_path) -> None:
    context = _context(tmp_path, location="site_trail")
    result = get_area({}, context)
    assert result["ok"] is False
    assert result["code"] == "narrative.area_not_found"


def test_legacy_bundle_without_roles_round_trips(tmp_path) -> None:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "legacy"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text('{"id": "legacy", "modules": []}', encoding="utf-8")
    save_bundle(
        scenario_dir,
        NarrativeBundle(
            scenario_id="legacy",
            nodes=[NarrativeNode(id="room_a", kind="location", title="A")],
            edges=[],
        ),
    )
    room = tmp_path / "room"
    room.mkdir()
    save_narrative_state(room, {"location": "room_a"})
    context = SimpleNamespace(
        room_dir=room,
        scenarios_dir=scenarios,
        room_id="legacy-room",
        room_info=lambda: {"scenario_id": "legacy"},
    )
    runtime = NarrativeRuntime.load(context)
    assert runtime.area_id == ""
    assert runtime.in_map_room() is False
    assert runtime.node("room_a").node_role == "site"
    assert mapgraph.map_nodes(runtime.bundle) == []
    # 旧剧本没有地图层，移动行为保持原样。
    moved = move_to_location({"location": "room_a"}, context)
    assert moved.get("already_here") is True
