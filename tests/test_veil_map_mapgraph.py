"""veil-map 图谱联动：按区域建/切场景、POI 事件与试探移动。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.runtime import NarrativeRuntime
from trpg_server.agents.narrative.state_store import save_narrative_state
from trpg_server.agents.narrative.store import save_bundle

ENTRY = Path(__file__).resolve().parent.parent / "data" / "plugins" / "veil-map" / "entry.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("veil_map_mg_entry_test", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


veil_map = _load_plugin()


def _scenario_dir(tmp_path: Path) -> Path:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True)
    scenario = {
        "id": "s1",
        "title": "测试剧本",
        "start": {"location_id": "site_trail"},
        "modules": [
            {"id": "area_map", "title": "矿坑"},
            {"id": "poi_entry", "title": "1. 入口"},
            {"id": "poi_lair", "title": "2. 巢穴"},
        ],
        "assets": [
            {
                "id": "asset_map_player",
                "kind": "image",
                "name": "矿坑（玩家版）",
                "url": "/assets/scenarios/s1/map.webp",
                "width": 1600,
                "height": 1200,
                "visibility": "public",
            }
        ],
        "triggers": [
            {
                "id": "trg_map_area",
                "binding": {"kind": "enter", "location_id": "area_map"},
                "attachments": [
                    {
                        "asset_id": "asset_map_player",
                        "kind": "image",
                        "visibility": "player_visible",
                        "order": 0,
                    }
                ],
                "group": "地图",
                "enabled": True,
            }
        ],
    }
    (scenario_dir / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False), encoding="utf-8"
    )
    nodes = [
        NarrativeNode(id="site_trail", kind="location", title="小径", extra={"node_role": "site"}),
        NarrativeNode(
            id="area_map",
            kind="location",
            title="矿坑",
            extra={
                "node_role": "map",
                "map": {
                    "scene_id": "area_map",
                    "entry_pois": ["poi_entry"],
                    "environment": {"lighting": "昏暗"},
                },
            },
        ),
        NarrativeNode(
            id="poi_entry",
            kind="location",
            title="1. 入口",
            parent="area_map",
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
            extra={
                "node_role": "poi",
                "poi": {
                    "area_id": "area_map",
                    "code": "2",
                    "trigger": {"mode": "enter", "radius_cells": 0},
                    "geometry": {"rect_cells": {"x": 8, "y": 8, "w": 4, "h": 4}},
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
            extra={"transition": {"to_area": "area_map", "entry_poi": "poi_entry"}},
        )
    ]
    save_bundle(scenario_dir, NarrativeBundle(scenario_id="s1", nodes=nodes, edges=edges))
    return scenarios


def _room(tmp_path: Path, *, location: str = "poi_entry") -> tuple[Path, dict]:
    rooms = tmp_path / "rooms"
    room = rooms / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "地图测试",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "scenario_id": "s1",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Alice"},
            },
            {
                "user_id": "u2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Bob"},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    save_narrative_state(room, {"location": location, "clock": {"turn": 1}})
    return room, info


def _app(tmp_path: Path, rooms: Path, scenarios: Path) -> Flask:
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=str(rooms),
        CONFIG_DIR=str(tmp_path / "config"),
        SCENARIOS_DIR=str(scenarios),
    )
    app.extensions["socketio"] = SimpleNamespace(events=[], emit=lambda *a, **k: None)
    app.register_blueprint(veil_map.bp)
    return app


def _context(room: Path, info: dict, *, user_id: str = "u1", agent_id: str = "kp"):
    return SimpleNamespace(
        room_id="room1",
        room_dir=room,
        scenarios_dir=None,
        user_id=user_id,
        agent_id=agent_id,
        room_info=lambda: info,
    )


def test_scene_store_deactivate_preserves_tokens(tmp_path) -> None:
    room, _info = _room(tmp_path, location="poi_entry")
    store = veil_map.scene_store
    store.save(
        room,
        {
            "scene_id": "area_map",
            "title": "矿坑",
            "revision": 1,
            "tokens": [{"id": "pc1", "kind": "pc", "x": 2, "y": 2}],
            "walls": [],
            "lights": [],
            "fog": {},
            "recent_requests": [],
        },
    )
    assert store.activate(room, "area_map") is True
    assert store.active_scene_id(room) == "area_map"

    store.deactivate(room)

    assert store.active_scene_id(room) == ""
    state, source = store.load(room)
    assert state == {}
    assert source == "closed"
    assert store.activate(room, "area_map") is True
    state, source = store.load(room)
    assert source == "scene"
    assert state["tokens"][0]["id"] == "pc1"


def test_multi_map_round_trip_closes_and_restores_tokens(tmp_path) -> None:
    scenarios = Path(__file__).resolve().parent.parent / "data" / "scenarios"
    room = tmp_path / "rooms" / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "LMoP 多图",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "scenario_id": "1795010280000",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Alice"},
            }
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    save_narrative_state(room, {"location": "p1_hideout_1_cave_mouth"})
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    veil_map._register_action_guard()
    store = veil_map.scene_store

    with app.app_context():
        context = SimpleNamespace(
            room_id="room1",
            room_dir=room,
            scenarios_dir=scenarios,
            user_id="u1",
            agent_id="kp",
            room_info=lambda: info,
        )
        runtime = NarrativeRuntime.load(context)

        runtime.travel_to("p1_trail")
        assert store.active_scene_id(room) == ""
        _state, source = store.load(room)
        assert source == "closed"

        result = runtime.travel_to("p1_hideout")
        assert result["ok"] is True
        assert store.active_scene_id(room) == "p1_hideout"
        state, source = store.load(room)
        assert source == "scene"
        assert any(str(token.get("kind")) == "pc" for token in state.get("tokens") or [])


def test_area_resolution_and_declared_map_use_area(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        assert veil_map._resolve_scene_area(info, veil_map._room_scenario(info), "poi_lair") == "area_map"
        binding = veil_map._declared_scene_map(veil_map._room_scenario(info), "poi_lair", area_id="area_map")
        assert binding is not None
        assert binding["location_id"] == "area_map"
        assert binding["url"].endswith("map.webp")


def test_activate_area_scene_creates_and_switches(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        first = veil_map._activate_area_scene(room, info, "area_map")
        assert first["ok"] is True
        assert first["created"] is True
        assert first["scene_id"] == "area_map"
        assert len(first["placements"]) == 2
        state, _source = veil_map.scene_store.load(room)
        assert state["scene_id"] == "area_map"
        assert state["background"].endswith("map.webp")
        assert state["scene_binding"]["location_id"] == "area_map"
        assert {token["name"] for token in state["tokens"]} == {"Alice", "Bob"}
        # 入口区域中心附近落子，而不是仍在旧图原点。
        assert all(-5 <= token["cell_x"] <= 10 for token in state["tokens"])

        second = veil_map._activate_area_scene(room, info, "area_map")
        assert second["ok"] is True
        assert second["created"] is False
        assert second["changed"] is False

        missing = veil_map._activate_area_scene(room, info, "area_unknown")
        assert missing["ok"] is False
        assert missing["code"] == "map.scene_unavailable"


def test_sync_scene_route_uses_area_and_creates_scene(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, _info = _room(tmp_path, location="poi_lair")
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id="u1", username="u1", role="USER")

    response = client.post("/api/plugins/veil-map/rooms/room1/map/sync-scene")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["changed"] is True
    assert data["created"] is True
    assert data["area_id"] == "area_map"
    assert data["scene_id"] == "area_map"

    again = client.post("/api/plugins/veil-map/rooms/room1/map/sync-scene")
    assert again.status_code == 200
    assert again.get_json()["data"]["changed"] is False


def test_preview_move_rejects_bounds_and_reports_poi(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        veil_map._activate_area_scene(room, info, "area_map")
        context = _context(room, info)
        preview = veil_map._preview_move_tool(
            {"token": "Bob", "cell_x": 9, "cell_y": 9}, context
        )
        assert preview["ok"] is True
        assert preview["allowed"] is True
        assert preview["distance_ft"] > 0
        assert {item["poi_id"] for item in preview["poi_on_path"]} >= {"poi_lair"}

        outside = veil_map._preview_move_tool(
            {"token": "Bob", "cell_x": 60, "cell_y": 60}, context
        )
        assert outside["allowed"] is False
        assert outside["code"] == "map.out_of_bounds"


def test_move_returns_poi_events(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        veil_map._activate_area_scene(room, info, "area_map")
        context = _context(room, info)
        result = veil_map._move_token_tool(
            {"token": "Bob", "cell_x": 9, "cell_y": 9}, context
        )
        assert result["ok"] is True
        assert result["area_id"] == "area_map"
        poi_ids = {item["poi_id"] for item in result["poi_events"]}
        assert "poi_lair" in poi_ids
        event = next(item for item in result["poi_events"] if item["poi_id"] == "poi_lair")
        assert event["first_time"] is True

        # 同一次移动重复提交：幂等返回，不产生第二次副作用。
        again = veil_map._move_token_tool(
            {"token": "Bob", "cell_x": 10, "cell_y": 10, "request_id": "m-1"}, context
        )
        assert again["ok"] is True
        duplicate = veil_map._move_token_tool(
            {"token": "Bob", "cell_x": 11, "cell_y": 11, "request_id": "m-1"}, context
        )
        assert duplicate["ok"] is True
        assert duplicate.get("duplicate") is True
        tokens = veil_map.scene_store.load(room)[0]["tokens"]
        bob = next(token for token in tokens if token["name"] == "Bob")
        assert (bob["cell_x"], bob["cell_y"]) == (10, 10)


def test_preview_move_rejects_wall(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        veil_map._activate_area_scene(room, info, "area_map")
        state, _source = veil_map.scene_store.load(room)
        state["walls"] = [
            {
                "id": "wall-east",
                "kind": "wall",
                "points": [{"x": 256, "y": 128}, {"x": 256, "y": 384}],
            }
        ]
        veil_map._save_map(room, state)

        context = _context(room, info)
        preview = veil_map._preview_move_tool(
            {"token": "Bob", "cell_x": 4, "cell_y": 3}, context
        )
        assert preview["allowed"] is False
        assert preview["code"] == "map.wall_blocked"

        move = veil_map._move_token_tool({"token": "Bob", "cell_x": 4, "cell_y": 3}, context)
        assert move["ok"] is False
        bob = next(
            token
            for token in veil_map.scene_store.load(room)[0]["tokens"]
            if token["name"] == "Bob"
        )
        assert (bob["cell_x"], bob["cell_y"]) == (3, 3)


def test_get_position_and_inspect_area(tmp_path) -> None:
    scenarios = _scenario_dir(tmp_path)
    room, info = _room(tmp_path)
    app = _app(tmp_path, tmp_path / "rooms", scenarios)
    with app.app_context():
        veil_map._activate_area_scene(room, info, "area_map")
        context = _context(room, info)
        position = veil_map._get_position_tool({"token": "Bob"}, context)
        assert position["ok"] is True
        assert position["area_id"] == "area_map"
        assert position["position"]["kind"] in {"poi", "open"}
        assert position["party"]

        area = veil_map._inspect_area_tool({"token": "Bob", "radius_cells": 8}, context)
        assert area["ok"] is True
        assert area["area_id"] == "area_map"
        assert area["environment"]["lighting"] == "昏暗"
        assert {item["poi_id"] for item in area["nearby_pois"]} >= {"poi_lair"}

        # 找不到棋子时显式失败，而不是返回空位置。
        missing = veil_map._get_position_tool({"token": "nobody"}, context)
        assert missing["ok"] is False


LMOP_ID = "1795010280000"  # scenario.json 的 id，目录名为 scenario-lmop-5e-001
LMOP_DIR = "scenario-lmop-5e-001"
REAL_SCENARIOS = Path(__file__).resolve().parent.parent / "data" / "scenarios"


@pytest.mark.skipif(not (REAL_SCENARIOS / LMOP_DIR).exists(), reason="LMoP 剧本不在工作区")
def test_lmop_p1_hideout_slice_end_to_end(tmp_path) -> None:
    """M1 切片：travel_to 切图 → 区域简报 → 边界/墙体拒绝 → 兴趣点卡片。"""
    from trpg_server.agents.narrative.runtime import NarrativeRuntime
    from trpg_server.agents.narrative.state_store import load_narrative_state
    from trpg_server.agents.tools.narrative import get_area, get_poi, travel_to
    from trpg_server.plugins import runtime as plugin_runtime

    room, info = _room(tmp_path, location="p1_trail")
    info["scenario_id"] = LMOP_ID
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = _app(tmp_path, tmp_path / "rooms", REAL_SCENARIOS)
    context = _context(room, info)
    context.scenarios_dir = REAL_SCENARIOS

    plugin_runtime.register_service("veil-map", "map_service", veil_map._MAP_SERVICE)
    try:
        with app.app_context():
            runtime = NarrativeRuntime.load(context)
            assert runtime.area_id == ""
            travel = travel_to({"to_area": "p1_hideout"}, context)
            assert travel["ok"] is True
            assert travel["entry_poi"] == "p1_hideout_1_cave_mouth"
            assert travel["scene_switched"] is True
            assert travel["area_brief"]["area_id"] == "p1_hideout"
            assert len(travel["area_brief"]["poi_index"]) == 8
            assert travel["area_brief"]["hidden_poi_count"] >= 1
            assert load_narrative_state(room)["location"] == "p1_hideout_1_cave_mouth"

            state = veil_map.scene_store.load(room)[0]
            assert state["scene_id"] == "p1_hideout"
            assert "cragmaw-hideout-player.webp" in state["background"]
            assert len(state["tokens"]) == 2

            # 地图房间内叙事移动被拒绝，必须走地图工具。
            blocked = veil_map._move_token_tool(
                {"token": "Bob", "cell_x": 60, "cell_y": 60}, context
            )
            assert blocked["ok"] is False
            assert blocked["error"]

            # 走进 2 号瞭望台：工具回传兴趣点事件与遭遇就绪标记。
            moved = veil_map._move_token_tool(
                {"token": "Bob", "cell_x": 9, "cell_y": 17}, context
            )
            assert moved["ok"] is True
            assert moved["area_id"] == "p1_hideout"
            events = {item["poi_id"]: item for item in moved["poi_events"]}
            assert "p1_hideout_2_lookout" in events
            assert events["p1_hideout_2_lookout"]["encounter_ready"] is True
            assert moved["encounter_ready"] is True

            card = get_poi({"poi": "p1_hideout_2_lookout"}, context)
            assert card["ok"] is True
            assert card["encounter"]["auto_start"] is False
            assert card["encounter"]["count"] == 2

            area = get_area({}, context)
            assert area["ok"] is True
            assert area["area_id"] == "p1_hideout"
            assert "地图" in area["area_brief_text"] or area["title"]
    finally:
        plugin_runtime.unregister_plugin("veil-map")
