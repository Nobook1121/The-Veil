"""veil-map 插件：token 权限、网格吸附与战斗移动预算。"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from flask import Flask

from trpg_server.agents import player_agent, profiles
from trpg_server.plugins import runtime as plugin_runtime

ENTRY = Path(__file__).resolve().parent.parent / "data" / "plugins" / "veil-map" / "entry.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("veil_map_entry_test", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


veil_map = _load_plugin()


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event, payload, room=None, **_kwargs):
        self.events.append({"event": event, "payload": payload, "room": room})


def _write_room(tmp_path: Path, *, combat: dict | None = None) -> Path:
    rooms = tmp_path / "rooms"
    room = rooms / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "地图测试",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
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
            {
                "user_id": "u3",
                "username": "carol",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Carol"},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    if combat is not None:
        (room / "state.json").write_text(json.dumps({"combat": combat}, ensure_ascii=False), encoding="utf-8")
    return rooms


def _combat(order: list[str], turn: int = 0) -> dict:
    combatants = {}
    for index, name in enumerate(order):
        combatants[name] = {
            "user_id": f"u{index + 1}",
            "budget": {"action": 1, "bonus_action": 1, "reaction": 1, "movement": 30},
            "actions_remaining": 1,
        }
    return {
        "active": True,
        "round": 1,
        "turn": turn,
        "order": order,
        "combatants": combatants,
        "ruleset": "dnd5e",
        "log": [],
    }


@pytest.fixture()
def client(tmp_path):
    rooms = _write_room(tmp_path)
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.extensions["socketio"] = _FakeSocket()
    app.register_blueprint(veil_map.bp)
    return app.test_client(), app


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=user_id, role="USER")


def _map(client) -> dict:
    response = client.get("/api/plugins/veil-map/rooms/room1/map")
    assert response.status_code == 200
    return response.get_json()["data"]


def _write(client, method: str, path: str, body: dict | None = None):
    """带并发契约的写请求：每次先读当前 revision，再带唯一 request_id。"""
    data = _map(client)
    payload = dict(body or {})
    payload["request_id"] = uuid4().hex
    payload["revision"] = data["map"]["revision"]
    if method == "delete":
        separator = "&" if "?" in path else "?"
        return client.delete(
            path
            + separator
            + urlencode({"request_id": payload["request_id"], "revision": payload["revision"]})
        )
    return getattr(client, method)(path, json=payload)


def _token(data: dict, token_id: str) -> dict:
    return next(item for item in data["map"]["tokens"] if item["id"] == token_id)


def test_get_map_reconciles_member_tokens(client):
    manager, _app = client
    _login(manager, "u2")
    data = _map(manager)
    names = {token["name"] for token in data["map"]["tokens"]}
    assert {"Alice", "Bob", "Carol"} <= names
    assert data["can_manage"] is False
    assert data["map"]["grid_size"] == 64


def test_player_can_only_move_own_token(client):
    manager, app = client
    _login(manager, "u2")
    _map(manager)

    denied = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u1/move", {"x": 128, "y": 0})
    assert denied.status_code == 403
    assert denied.get_json()["status"] == "rejected"
    assert "自己绑定" in denied.get_json()["message"]

    moved = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u2/move", {"x": 192, "y": 0})
    assert moved.status_code == 200
    data = moved.get_json()["data"]
    assert _token(data, "member-u2")["x"] == 192
    events = [item for item in app.extensions["socketio"].events if item["event"] == "room_map_changed"]
    assert events
    # 迷雾开启时不再整房间广播：每个成员收到自己的裁剪载荷。
    assert all(str(item["room"]).startswith("user:") for item in events)
    assert "user:u2" in {str(item["room"]) for item in events}


def test_combat_move_uses_turn_and_movement_budget(tmp_path):
    rooms = _write_room(tmp_path, combat=_combat(["Alice", "Bob"]))
    # 只保留两名成员，避免第三枚棋子占住 Alice 的移动目标格。
    info_path = rooms / "room1" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["members"] = [member for member in info["members"] if member["user_id"] != "u3"]
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.extensions["socketio"] = _FakeSocket()
    app.register_blueprint(veil_map.bp)
    manager = app.test_client()
    _login(manager, "u1")
    _map(manager)

    # 非当前行动者：房主也不能替 Bob 移动。
    denied = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u2/move", {"x": 192, "y": 0})
    assert denied.status_code == 409
    assert denied.get_json()["status"] == "rejected"
    assert "轮到" in denied.get_json()["message"]

    # 当前行动者移动 2 格：30 → 20 ft。
    moved = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u1/move", {"x": 128, "y": 0})
    assert moved.status_code == 200
    combat = moved.get_json()["data"]["combat"]
    alice = next(item for item in combat["order"] if item["name"] == "Alice")
    assert alice["budget"]["movement"] == 20

    # 再移动 10 格 = 50 ft，超出剩余 20 ft，预算不变。
    denied = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u1/move", {"x": 768, "y": 0})
    assert denied.status_code == 409
    assert denied.get_json()["status"] == "rejected"
    assert "剩余 20 英尺" in denied.get_json()["message"]
    state = json.loads((rooms / "room1" / "state.json").read_text(encoding="utf-8"))
    assert state["combat"]["combatants"]["Alice"]["budget"]["movement"] == 20


def test_dm_edits_map_and_adds_multicell_token(client):
    manager, app = client
    _login(manager, "u1")

    updated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {
        "grid_size": 48,
        "background": "https://example.com/map.png",
    })
    assert updated.status_code == 200
    data = updated.get_json()["data"]
    assert data["map"]["grid_size"] == 48
    assert data["map"]["background"] == "https://example.com/map.png"

    bad = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {
        "background": "javascript:alert(1)",
    })
    assert bad.status_code == 400
    assert bad.get_json()["status"] == "invalid"

    added = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens", {
        "name": "Ogre", "x": 0, "y": 0, "w": 2, "h": 2,
    })
    assert added.status_code == 200
    token = added.get_json()["data"]["token"]
    assert token["w"] == 2 and token["h"] == 2
    assert token["kind"] == "npc"

    # 非战斗者 token 不受回合限制。
    moved = _write(manager, "post", f"/api/plugins/veil-map/rooms/room1/tokens/{token['id']}/move", {"x": 96, "y": 96})
    assert moved.status_code == 200
    moved_token = _token(moved.get_json()["data"], token["id"])
    assert moved_token["x"] == 96 and moved_token["y"] == 96


def test_player_cannot_edit_map_or_add_tokens(client):
    manager, _app = client
    _login(manager, "u2")
    _map(manager)

    assert _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 32}).status_code == 403
    assert _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens", {"name": "Imp"}).status_code == 403
    assert _write(manager, "delete", "/api/plugins/veil-map/rooms/room1/tokens/member-u1").status_code == 403


def test_duplicate_request_id_has_no_side_effect(client):
    manager, app = client
    _login(manager, "u1")
    current = _map(manager)
    payload = {
        "request_id": "add-echo-1",
        "revision": current["map"]["revision"],
        "name": "Echo",
        "x": 0,
        "y": 0,
    }
    path = "/api/plugins/veil-map/rooms/room1/tokens"
    first = manager.post(path, json=payload)
    second = manager.post(path, json=payload)

    assert first.status_code == 200
    assert first.get_json()["status"] == "success"
    assert second.status_code == 200
    assert second.get_json()["status"] == "success"
    assert second.get_json()["duplicate"] is True

    names = [token["name"] for token in _map(manager)["map"]["tokens"]]
    assert names.count("Echo") == 1
    events = [
        item
        for item in app.extensions["socketio"].events
        if item["event"] == "room_map_changed"
    ]
    assert events
    assert all(str(item["room"]).startswith("user:") for item in events)


def test_stale_revision_returns_conflict_and_latest_map(client):
    manager, _app = client
    _login(manager, "u1")
    current = _map(manager)
    stale_revision = current["map"]["revision"]
    path = "/api/plugins/veil-map/rooms/room1/tokens"

    first = manager.post(
        path,
        json={
            "request_id": "rev-first",
            "revision": stale_revision,
            "name": "Echo",
            "x": 0,
            "y": 0,
        },
    )
    assert first.status_code == 200
    latest_revision = first.get_json()["revision"]
    assert latest_revision > stale_revision

    conflict = manager.post(
        path,
        json={
            "request_id": "rev-second",
            "revision": stale_revision,
            "name": "Ghost",
            "x": 128,
            "y": 0,
        },
    )
    assert conflict.status_code == 409
    body = conflict.get_json()
    assert body["status"] == "conflict"
    assert body["code"] == "map.revision_conflict"
    assert body["data"]["map"]["revision"] == latest_revision
    names = [token["name"] for token in body["data"]["map"]["tokens"]]
    assert "Ghost" not in names
    assert names.count("Echo") == 1


def test_write_requires_request_envelope(client):
    manager, _app = client
    _login(manager, "u1")
    _map(manager)
    path = "/api/plugins/veil-map/rooms/room1/tokens"

    missing = manager.post(path, json={"name": "X"})
    assert missing.status_code == 400
    assert missing.get_json()["status"] == "invalid"
    assert missing.get_json()["code"] == "map.invalid_request_id"

    bad_revision = manager.post(
        path,
        json={"request_id": "env-1", "revision": "abc", "name": "X"},
    )
    assert bad_revision.status_code == 400
    assert bad_revision.get_json()["code"] == "map.invalid_revision"


def test_coc7_room_has_no_map_endpoint(tmp_path):
    rooms = _write_room(tmp_path)
    info_path = rooms / "room1" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["ruleset_id"] = "coc7"
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.register_blueprint(veil_map.bp)
    client = app.test_client()
    _login(client, "u1")

    response = client.get("/api/plugins/veil-map/rooms/room1/map")
    assert response.status_code == 400
    assert response.get_json()["error"] == "map.ruleset_disabled"


def _agent_context(room_dir: Path, *, user_id: str, agent_id: str) -> SimpleNamespace:
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        room_id="room1",
        room_dir=room_dir,
        scenarios_dir=None,
        user_id=user_id,
        agent_id=agent_id,
        room_info=lambda: info,
    )


def _agent_app() -> Flask:
    app = Flask(__name__)
    app.extensions["socketio"] = _FakeSocket()
    return app


def _read_map_tokens(room_dir: Path) -> list[dict]:
    state, _source = veil_map.scene_store.load(room_dir)
    return state.get("tokens") or []


@pytest.fixture()
def registered_plugin_tools():
    plugin_runtime.register_agent_tools("veil-map", veil_map.create_tools())
    yield
    plugin_runtime.unregister_plugin("veil-map")


def test_agent_tool_is_scoped_to_dnd5e(registered_plugin_tools, monkeypatch):
    tools = veil_map.create_tools()
    assert [tool.name for tool in tools] == ["veil-map.move_token"]
    assert tools[0].rulesets == ("dnd5e",)

    assert "veil-map.move_token" in profiles.plugin_tool_names("dnd5e")
    assert "veil-map.move_token" not in profiles.plugin_tool_names("coc7")

    # 裸测试环境没有加载 dnd5e 规则集插件，先把其它扩展工具清空，
    # 只验证 KP 工具链会并入插件工具。
    for name in (
        "ruleset_tool_names",
        "combat_budget_tool_names",
        "spell_tool_names",
        "effect_tool_names",
    ):
        monkeypatch.setattr(profiles, name, lambda *_args, **_kwargs: [])
    assert "veil-map.move_token" in profiles.build_kp_tool_names("dnd5e")
    assert "veil-map.move_token" not in profiles.build_kp_tool_names("coc7")

    assert "veil-map.move_token" in player_agent._build_profile("dnd5e", {}).tool_names
    assert "veil-map.move_token" not in player_agent._build_profile("coc7", {}).tool_names


def test_agent_kp_moves_player_token_in_exploration(tmp_path):
    rooms = _write_room(tmp_path)
    room_dir = rooms / "room1"
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        result = tool.handler(
            {"token": "Bob", "cell_x": 2, "cell_y": 3, "reason": "退到石柱后"},
            context,
        )

    assert result["ok"] is True
    assert result["to"] == {"cell_x": 2, "cell_y": 3}
    assert result["cost_ft"] == 0
    bob = next(token for token in _read_map_tokens(room_dir) if token["name"] == "Bob")
    assert (bob["cell_x"], bob["cell_y"]) == (2, 3)
    assert (bob["offset_x"], bob["offset_y"]) == (0.0, 0.0)
    assert "x" not in bob and "y" not in bob

    events = [item for item in app.extensions["socketio"].events if item["event"] == "room_map_changed"]
    move = events[-1]["payload"]["move"]
    assert move["source"] == "ai"
    assert move["actor"] == "KP"
    assert move["token_name"] == "Bob"
    assert move["reason"] == "退到石柱后"
    assert move["combat_active"] is False


def test_agent_player_can_only_move_own_token(tmp_path):
    rooms = _write_room(tmp_path)
    room_dir = rooms / "room1"
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u2", agent_id="player")

    with app.app_context():
        denied = tool.handler({"token": "Carol", "cell_x": 1, "cell_y": 1}, context)
        allowed = tool.handler({"token": "Bob", "cell_x": 1, "cell_y": 1}, context)

    assert denied["ok"] is False
    assert "只能移动自己绑定的棋子" in denied["error"]
    assert allowed["ok"] is True


def test_agent_move_uses_combat_turn_and_budget(tmp_path):
    rooms = _write_room(tmp_path, combat=_combat(["Alice", "Bob"]))
    room_dir = rooms / "room1"
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        not_current = tool.handler({"token": "Bob", "cell_x": 1, "cell_y": 1}, context)
        moved = tool.handler({"token": "Alice", "cell_x": 3, "cell_y": 0}, context)
        exhausted = tool.handler({"token": "Alice", "cell_x": 7, "cell_y": 0}, context)

    assert not_current["ok"] is False
    assert "轮到" in not_current["error"]
    assert moved["ok"] is True
    assert moved["cost_ft"] == 15
    assert exhausted["ok"] is False
    assert "剩余 15 英尺" in exhausted["error"]

    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert state["combat"]["combatants"]["Alice"]["budget"]["movement"] == 15
    alice = next(token for token in _read_map_tokens(room_dir) if token["name"] == "Alice")
    assert (alice["cell_x"], alice["cell_y"]) == (3, 0)


def test_agent_move_rejects_bounds_and_occupied_cell(tmp_path):
    rooms = _write_room(tmp_path)
    room_dir = rooms / "room1"
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        outside = tool.handler({"token": "Bob", "cell_x": 100, "cell_y": 0}, context)
        occupied = tool.handler({"token": "Bob", "cell_x": 2, "cell_y": 0}, context)

    assert outside["ok"] is False
    assert "超出地图边界" in outside["error"]
    assert occupied["ok"] is False
    assert "Carol" in occupied["error"]


def test_agent_move_rejects_non_combatant_during_combat(tmp_path):
    rooms = _write_room(tmp_path, combat=_combat(["Alice", "Bob"]))
    room_dir = rooms / "room1"
    map_dir = room_dir / "plugins" / "veil-map"
    map_dir.mkdir(parents=True)
    (map_dir / "map.json").write_text(
        json.dumps(
            {
                "grid_size": 64,
                "bounds_cells": 50,
                "tokens": [
                    {"id": "npc-ogre", "name": "Ogre", "x": 320, "y": 320, "w": 1, "h": 1, "kind": "npc"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        result = tool.handler({"token": "Ogre", "cell_x": 6, "cell_y": 6}, context)

    assert result["ok"] is False
    assert "没有加入先攻顺序" in result["error"]
    ogre = next(token for token in _read_map_tokens(room_dir) if token["name"] == "Ogre")
    assert (ogre["x"], ogre["y"]) == (320, 320)


def test_agent_move_rejects_coc7_room(tmp_path):
    rooms = _write_room(tmp_path)
    room_dir = rooms / "room1"
    info_path = room_dir / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["ruleset_id"] = "coc7"
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        result = tool.handler({"token": "Bob", "cell_x": 1, "cell_y": 1}, context)

    assert result["ok"] is False
    assert "规则集不支持" in result["error"]


def test_spatial_distance_ranges_and_shapes():
    spatial = veil_map.spatial

    assert spatial.feet_to_cells(30) == 6
    assert spatial.feet_to_cells(29) == 5
    assert spatial.cells_to_feet(6) == 30
    assert spatial.cell_distance((0, 0), (3, 4)) == 4
    assert spatial.cell_distance((0, 0), (3, 4), "euclidean") == 5

    circle = spatial.range_cells((0, 0), shape="circle", size_cells=2)
    assert len(circle) == 25
    assert (2, 2) in circle

    cone = spatial.range_cells((0, 0), shape="cone", size_cells=2, direction="e")
    assert len(cone) == 8
    assert (2, 0) in cone and (2, 1) in cone
    assert (-1, 0) not in cone and (0, 1) not in cone

    line = spatial.range_cells((0, 0), shape="line", size_cells=3, direction="e")
    assert set(line) == {(1, 0), (2, 0), (3, 0)}


def test_wall_payload_blocks_sight_and_movement(client):
    manager, _app = client
    _login(manager, "u1")
    walls = [
        {
            "id": "wall-a",
            "kind": "wall",
            "points": [{"x": 0, "y": -128}, {"x": 0, "y": 128}],
        },
        {
            "id": "door-b",
            "kind": "door",
            "state": "open",
            "points": [{"x": 128, "y": -128}, {"x": 128, "y": 128}],
        },
    ]
    saved = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": walls})
    assert saved.status_code == 200
    assert saved.get_json()["status"] == "success"
    assert [wall["id"] for wall in saved.get_json()["data"]["map"]["walls"]] == ["wall-a", "door-b"]

    blocked = manager.post(
        "/api/plugins/veil-map/rooms/room1/spatial/line",
        json={
            "from": {"cell_x": -2, "cell_y": 0},
            "to": {"cell_x": 2, "cell_y": 0},
            "mode": "sight",
        },
    )
    assert blocked.status_code == 200
    data = blocked.get_json()["data"]
    assert data["blocked"] is True
    assert data["blocked_by"] == ["wall-a"]
    assert data["distance_cells"] == 4
    assert data["distance_ft"] == 20

    # 开着的门不阻挡视线。
    doorway = manager.post(
        "/api/plugins/veil-map/rooms/room1/spatial/line",
        json={
            "from": {"cell_x": 2, "cell_y": -2},
            "to": {"cell_x": 2, "cell_y": 2},
            "mode": "sight",
        },
    )
    assert doorway.get_json()["data"]["blocked"] is False


def test_walls_survive_map_settings_and_require_manager(client):
    manager, _app = client
    _login(manager, "u1")
    walls = [
        {
            "id": "wall-a",
            "kind": "wall",
            "points": [[-64, -64], [64, -64]],
        }
    ]
    assert _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": walls}).status_code == 200

    updated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {
        "background": "https://example.com/next-map.png",
    })
    assert updated.status_code == 200
    assert [wall["id"] for wall in updated.get_json()["data"]["map"]["walls"]] == ["wall-a"]

    invalid = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": "nope"})
    assert invalid.status_code == 400
    assert invalid.get_json()["status"] == "invalid"

    _login(manager, "u2")
    denied = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": walls})
    assert denied.status_code == 403
    assert denied.get_json()["status"] == "rejected"


def test_spatial_range_endpoint_returns_cells(client):
    manager, _app = client
    _login(manager, "u2")
    _map(manager)

    response = manager.post(
        "/api/plugins/veil-map/rooms/room1/spatial/range",
        json={
            "origin": {"cell_x": 0, "cell_y": 0},
            "shape": "circle",
            "range_ft": 10,
        },
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["size_cells"] == 2
    assert data["size_ft"] == 10
    assert data["count"] == 25
    assert {"cell_x": 0, "cell_y": 0, "x": 0, "y": 0} in data["cells"]

    cone = manager.post(
        "/api/plugins/veil-map/rooms/room1/spatial/range",
        json={
            "origin": {"cell_x": 0, "cell_y": 0},
            "shape": "cone",
            "size_cells": 2,
            "direction": "e",
        },
    )
    assert cone.status_code == 200
    assert cone.get_json()["data"]["count"] == 8

    bad = manager.post(
        "/api/plugins/veil-map/rooms/room1/spatial/range",
        json={"origin": {"cell_x": 0, "cell_y": 0}, "shape": "line", "size_cells": 2},
    )
    assert bad.status_code == 400


def test_spatial_window_blocks_movement_not_sight():
    spatial = veil_map.spatial
    walls = [
        {
            "id": "window-1",
            "kind": "window",
            "points": [{"x": -64, "y": 0}, {"x": 64, "y": 0}],
        },
        {
            "id": "door-1",
            "kind": "door",
            "state": "open",
            "points": [{"x": 0, "y": -64}, {"x": 0, "y": 64}],
        },
    ]
    assert spatial.segment_blockers(walls, (0, -64), (0, 64), mode="sight") == []
    assert spatial.segment_blockers(walls, (0, -64), (0, 64), mode="movement") == ["window-1"]


def test_move_blocked_by_closed_wall_keeps_position_and_budget(tmp_path):
    rooms = _write_room(tmp_path, combat=_combat(["Alice", "Bob"]))
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.extensions["socketio"] = _FakeSocket()
    app.register_blueprint(veil_map.bp)
    manager = app.test_client()
    _login(manager, "u1")
    _map(manager)

    wall = {
        "id": "wall-block",
        "kind": "wall",
        "points": [{"x": 64, "y": -128}, {"x": 64, "y": 128}],
    }
    saved = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": [wall]})
    assert saved.status_code == 200

    blocked = _write(
        manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u1/move",
        {"cell_x": 4, "cell_y": 0},
    )
    body = blocked.get_json()
    assert blocked.status_code == 403
    assert body["status"] == "rejected"
    assert body["code"] == "map.wall_blocked"
    assert "墙体" in body["message"]

    data = _map(manager)
    alice = _token(data, "member-u1")
    assert (alice["cell_x"], alice["cell_y"]) == (0, 0)
    alice_combat = next(item for item in data["combat"]["order"] if item["name"] == "Alice")
    assert alice_combat["budget"]["movement"] == 30

    door = dict(wall, kind="door", state="open")
    assert _write(
        manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": [door]}
    ).status_code == 200
    moved = _write(
        manager, "post", "/api/plugins/veil-map/rooms/room1/tokens/member-u1/move",
        {"cell_x": 4, "cell_y": 0},
    )
    assert moved.status_code == 200
    assert (_token(moved.get_json()["data"], "member-u1")["cell_x"]) == 4


def test_agent_move_wall_check_uses_corners_doors_and_windows(tmp_path):
    rooms = _write_room(tmp_path)
    room_dir = rooms / "room1"
    map_dir = room_dir / "plugins" / "veil-map"
    map_dir.mkdir(parents=True)
    map_path = map_dir / "map.json"
    base_state = {
        "grid_size": 64,
        "bounds_cells": 50,
        "tokens": [
            {"id": "npc-ogre", "name": "Ogre", "cell_x": 6, "cell_y": 3, "w": 2, "h": 2, "kind": "npc"}
        ],
    }
    # 墙体只覆盖棋子顶部一行：锚点与中心路径都不会穿过，只有 footprint 角会。
    wall = {
        "id": "door-1",
        "kind": "door",
        "state": "closed",
        "points": [{"x": 512, "y": 192}, {"x": 512, "y": 240}],
    }
    map_path.write_text(
        json.dumps({**base_state, "walls": [wall]}, ensure_ascii=False), encoding="utf-8"
    )
    app = _agent_app()
    tool = veil_map.create_tools()[0]
    context = _agent_context(room_dir, user_id="u1", agent_id="kp")

    with app.app_context():
        blocked_door = tool.handler({"token": "Ogre", "cell_x": 7, "cell_y": 3}, context)

        map_path.write_text(
            json.dumps({**base_state, "walls": [dict(wall, kind="window")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        blocked_window = tool.handler({"token": "Ogre", "cell_x": 7, "cell_y": 3}, context)

        map_path.write_text(
            json.dumps({**base_state, "walls": [dict(wall, state="open")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        allowed = tool.handler({"token": "Ogre", "cell_x": 7, "cell_y": 3}, context)

    assert blocked_door["ok"] is False
    assert blocked_door["code"] == "map.wall_blocked"
    assert "门" in blocked_door["error"]
    assert blocked_window["ok"] is False
    assert "窗户" in blocked_window["error"]
    assert allowed["ok"] is True
    ogre = next(token for token in _read_map_tokens(room_dir) if token["name"] == "Ogre")
    assert (ogre["cell_x"], ogre["cell_y"]) == (7, 3)


def _png(width: int, height: int) -> bytes:
    """构造只含合法 PNG 文件头的字节串；尺寸解析只需要 IHDR。"""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = (
        struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )
    return signature + ihdr + b"\x00\x00\x00\x00"


def _import_map(client, content: bytes, filename: str, request_id: str):
    data = _map(client)
    return client.post(
        "/api/plugins/veil-map/rooms/room1/map/import",
        data={
            "request_id": request_id,
            "revision": str(data["map"]["revision"]),
            "file": (io.BytesIO(content), filename),
        },
        content_type="multipart/form-data",
    )


def test_import_map_stores_asset_under_room_and_is_idempotent(client):
    manager, _app = client
    _login(manager, "u1")
    before = _map(manager)
    token_positions = {
        token["id"]: (token["x"], token["y"]) for token in before["map"]["tokens"]
    }

    imported = _import_map(manager, _png(640, 320), "dungeon.png", "import-1")
    assert imported.status_code == 200
    body = imported.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["background"]["width"] == 640
    assert data["background"]["height"] == 320
    assert data["map"]["background"].startswith(
        "/api/plugins/veil-map/rooms/room1/assets/maps/"
    )
    assert data["map"]["background_meta"]["name"] == "dungeon.png"

    asset_dir = Path(_app.config["ROOMS_DIR"]) / "room1" / "plugins" / "veil-map" / "assets" / "maps"
    files = list(asset_dir.iterdir())
    assert len(files) == 1

    # 重复提交同一个 request_id：返回 duplicate，不会多存一份。
    replay = _import_map(manager, _png(640, 320), "dungeon.png", "import-1")
    assert replay.status_code == 200
    assert replay.get_json()["duplicate"] is True
    assert len(list(asset_dir.iterdir())) == 1

    served = manager.get(data["map"]["background"])
    assert served.status_code == 200
    assert served.data == _png(640, 320)

    # 地图元数据不写进 info.json，棋子坐标保持不变。
    info_text = (Path(_app.config["ROOMS_DIR"]) / "room1" / "info.json").read_text(encoding="utf-8")
    assert "base64" not in info_text
    after = _map(manager)["map"]
    assert {
        token["id"]: (token["x"], token["y"]) for token in after["tokens"]
    } == token_positions


def test_import_map_readable_errors(client, monkeypatch):
    manager, _app = client
    _login(manager, "u1")

    unsupported = _import_map(manager, b"whatever", "map.txt", "bad-1")
    assert unsupported.status_code == 400
    assert unsupported.get_json()["code"] == "map.image_unsupported"

    broken = _import_map(manager, b"not a png", "map.png", "bad-2")
    assert broken.status_code == 400
    assert broken.get_json()["code"] == "map.image_invalid"

    tiny = _import_map(manager, _png(32, 32), "tiny.png", "bad-3")
    assert tiny.status_code == 400
    assert tiny.get_json()["code"] == "map.image_tiny"

    monkeypatch.setattr(veil_map.media, "MAX_IMAGE_BYTES", 100)
    huge = _import_map(manager, _png(640, 320) + b"x" * 200, "huge.png", "bad-4")
    assert huge.status_code == 413
    assert huge.get_json()["code"] == "map.image_too_large"


def test_import_requires_manager(client, tmp_path):
    _manager, app = client
    player = app.test_client()
    _login(player, "u2")
    response = _import_map(player, _png(640, 320), "dungeon.png", "player-1")
    assert response.status_code == 403
    assert response.get_json()["status"] == "rejected"
    asset_dir = Path(app.config["ROOMS_DIR"]) / "room1" / "plugins" / "veil-map" / "assets" / "maps"
    assert not asset_dir.exists() or not list(asset_dir.iterdir())


def test_calibration_saves_and_keeps_token_cells(client):
    manager, _app = client
    _login(manager, "u1")
    before = _map(manager)
    token_cells = {
        token["id"]: (token["cell_x"], token["cell_y"], token["offset_x"], token["offset_y"])
        for token in before["map"]["tokens"]
    }
    imported = _import_map(manager, _png(2048, 1536), "big.png", "cal-import")
    assert imported.status_code == 200

    calibrated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map/calibration", {
        "cells_across": 32,
        "offset_x": 12,
        "offset_y": 4,
    })
    assert calibrated.status_code == 200
    body = calibrated.get_json()
    assert body["status"] == "success"
    state = body["data"]["map"]
    assert state["grid_size"] == 64
    assert state["calibration"]["cells_across"] == 32
    assert state["calibration"]["offset_x"] == 12
    assert state["grid_offset"]["x"] == 12
    # 标定只改网格锚点；棋子仍留在原来的格里。
    assert {
        token["id"]: (token["cell_x"], token["cell_y"], token["offset_x"], token["offset_y"])
        for token in state["tokens"]
    } == token_cells

    # 重新调整：改为 64 格 → 每格 32px，旧标定可覆盖。
    recalibrated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map/calibration", {
        "cells_across": 64,
        "offset_x": 0,
        "offset_y": 0,
    })
    assert recalibrated.status_code == 200
    assert recalibrated.get_json()["data"]["map"]["grid_size"] == 32


def test_calibration_rejects_invalid_or_missing_background(client):
    manager, _app = client
    _login(manager, "u1")
    missing = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map/calibration", {
        "cells_across": 20,
    })
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "map.no_background_image"

    assert _import_map(manager, _png(640, 320), "small.png", "cal-bad-import").status_code == 200
    too_few = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map/calibration", {
        "cells_across": 200,
    })
    assert too_few.status_code == 400
    assert too_few.get_json()["code"] == "map.grid_too_small"


def _uvtt_payload(
    *,
    width: int = 640,
    height: int = 320,
    grid: int = 64,
    map_size: tuple[int, int] | None = None,
    walls: list[dict] | None = None,
    doors: list[dict] | None = None,
    lights: list[dict] | None = None,
    environment: dict | None = None,
) -> dict:
    declared = map_size or (width, height)
    return {
        "format": 0.3,
        "resolution": {
            "map_origin": {"x": 0, "y": 0},
            "map_size": {"x": declared[0], "y": declared[1]},
            "pixels_per_grid": grid,
        },
        "image": base64.b64encode(_png(width, height)).decode("ascii"),
        "walls": walls or [],
        "doors": doors or [],
        "lights": lights or [],
        "environment": environment or {},
    }


def test_import_uvtt_brings_walls_doors_lights_and_ambient(client):
    manager, _app = client
    _login(manager, "u1")
    payload = _uvtt_payload(
        walls=[
            {"points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}], "wall_type": "wall"},
            {"points": [{"x": 0, "y": 1}, {"x": 0, "y": 4}], "wall_type": "invisible"},
        ],
        doors=[
            {"points": [{"x": 2, "y": 2}, {"x": 3, "y": 2}], "door_type": "door", "open": True},
            {"points": [{"x": 4, "y": 2}, {"x": 5, "y": 2}], "door_type": "window", "open": False},
        ],
        lights=[
            {
                "position": {"x": 5, "y": 2.5},
                "range": 6,
                "color": "ffaa00",
                "intensity": 0.8,
                "shadows": True,
            }
        ],
        environment={"baked_lighting": False, "ambient_light": {"color": [0.1, 0.2, 0.3, 0.5]}},
    )

    response = _import_map(manager, json.dumps(payload).encode(), "dungeon.json", "uvtt-1")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["import"]["format"] == "uvtt"
    assert data["import"]["walls"] == 4
    assert data["import"]["doors"] == 2
    assert data["import"]["lights"] == 1
    assert data["import"]["needs_review"] is False

    state = data["map"]
    assert state["grid_size"] == 64
    assert [wall["kind"] for wall in state["walls"]] == ["wall", "wall", "door", "window"]
    assert state["walls"][2]["state"] == "open"
    assert state["walls"][0]["points"] == [{"x": -320, "y": -160}, {"x": 320, "y": -160}]
    light = state["lights"][0]
    assert (light["x"], light["y"]) == (0, 0)
    assert (light["range_cells"], light["range_ft"]) == (6, 30)
    assert light["color"] == "#ffaa00"
    assert light["enabled"] is True
    assert state["ambient_light"]["enabled"] is True
    assert state["ambient_light"]["color"] == "#1a334d"
    assert state["ambient_light"]["intensity"] == 0.5
    assert state["background_meta"]["format"] == "uvtt"


def test_import_uvtt_grid_mismatch_asks_for_review(client):
    manager, _app = client
    _login(manager, "u1")
    payload = _uvtt_payload(width=650, height=320, grid=64, map_size=(640, 300))

    response = _import_map(manager, json.dumps(payload).encode(), "odd.uvtt", "uvtt-review")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["import"]["needs_review"] is True
    assert data["calibration_hint"]["needs_review"] is True
    warnings = " ".join(data["import"]["warnings"])
    assert "宽度" in warnings and "整数倍" in warnings
    assert data["map"]["grid_size"] == 64
    assert data["map"]["background_meta"]["import"]["needs_review"] is True


def test_import_foundry_scene_with_embedded_image(client):
    manager, _app = client
    _login(manager, "u1")
    scene = {
        "name": "Crypt",
        "width": 640,
        "height": 320,
        "background": {"src": "data:image/png;base64," + base64.b64encode(_png(640, 320)).decode()},
        "grid": {"size": 64, "distance": 5, "units": "ft", "offsetX": 0, "offsetY": 0},
        "walls": [
            {"c": [0, 0, 640, 0], "sight": 1, "move": 1, "light": 1, "door": 0},
            {"c": [64, 64, 128, 64], "sight": 1, "move": 1, "light": 1, "door": 1, "ds": 2},
        ],
        "lights": [
            {
                "x": 320,
                "y": 160,
                "config": {"dim": 30, "bright": 10, "color": "#ffaa00", "alpha": 0.8, "shadows": True},
            }
        ],
        "environment": {"darkness": 0.4, "globalLight": {"enabled": True, "color": "#ffffff", "alpha": 0.3}},
    }

    response = _import_map(manager, json.dumps(scene).encode(), "scene.json", "foundry-1")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["import"]["format"] == "foundry"
    assert data["import"]["walls"] == 2
    assert data["import"]["doors"] == 1
    assert data["import"]["lights"] == 1
    state = data["map"]
    assert state["walls"][1]["kind"] == "door"
    assert state["walls"][1]["state"] == "open"
    assert state["lights"][0]["range_cells"] == 6
    assert state["lights"][0]["bright_cells"] == 2
    assert state["ambient_light"]["enabled"] is True
    assert state["ambient_light"]["darkness"] == 0.4


def test_import_foundry_external_image_is_rejected_with_scope(client):
    manager, _app = client
    _login(manager, "u1")
    scene = {
        "width": 640,
        "height": 320,
        "background": {"src": "maps/crypt.png"},
        "grid": {"size": 64, "distance": 5, "units": "ft"},
        "walls": [],
    }
    response = _import_map(manager, json.dumps(scene).encode(), "scene.json", "foundry-external")
    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "map.import_image_missing"
    assert "Universal VTT" in body["message"]


def test_import_detects_content_not_extension_and_rejects_unknown_json(client):
    manager, app = client
    _login(manager, "u1")

    renamed = _import_map(manager, _png(640, 320), "map.txt", "content-1")
    assert renamed.status_code == 200
    assert renamed.get_json()["data"]["import"]["format"] == "image"

    unknown = _import_map(manager, json.dumps({"hello": "world"}).encode(), "map.json", "content-2")
    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "map.import_unknown_json"

    asset_dir = Path(app.config["ROOMS_DIR"]) / "room1" / "plugins" / "veil-map" / "assets" / "maps"
    assert len(list(asset_dir.iterdir())) == 1


def test_failed_uvtt_import_leaves_no_partial_scene(client):
    manager, app = client
    _login(manager, "u1")
    before = _map(manager)
    asset_dir = Path(app.config["ROOMS_DIR"]) / "room1" / "plugins" / "veil-map" / "assets" / "maps"
    before_assets = len(list(asset_dir.iterdir())) if asset_dir.exists() else 0
    before_walls = before["map"]["walls"]

    broken = _uvtt_payload()
    broken["image"] = "not-base64!!"
    response = _import_map(manager, json.dumps(broken).encode(), "broken.uvtt", "broken-1")
    assert response.status_code == 400
    assert response.get_json()["code"] == "map.import_image_invalid"

    after = _map(manager)
    assert after["map"]["revision"] == before["map"]["revision"]
    assert after["map"]["walls"] == before_walls
    after_assets = len(list(asset_dir.iterdir())) if asset_dir.exists() else 0
    assert after_assets == before_assets


def _scene_dir(room_dir: Path) -> Path:
    return room_dir / "plugins" / "veil-map" / "scenes" / "default"


def _generation_dir(room_dir: Path) -> Path:
    active = json.loads((_scene_dir(room_dir) / "active.json").read_text(encoding="utf-8"))
    return _scene_dir(room_dir) / active["generation"]


def test_scene_shards_share_same_revision(client):
    manager, app = client
    _login(manager, "u1")
    room_dir = Path(app.config["ROOMS_DIR"]) / "room1"

    updated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 48})
    assert updated.status_code == 200
    revision = updated.get_json()["revision"]
    generation = _generation_dir(room_dir)
    scene = json.loads((generation / "scene.json").read_text(encoding="utf-8"))
    assert scene["revision"] == revision
    assert scene["grid_size"] == 48
    for filename in ("tokens.json", "walls.json", "lights.json", "journal.json"):
        payload = json.loads((generation / filename).read_text(encoding="utf-8"))
        assert payload["revision"] == revision
    assert (room_dir / "plugins" / "veil-map" / "scenes" / "active.json").exists()
    assert not (room_dir / "plugins" / "veil-map" / "map.json").exists()


def test_legacy_map_json_is_read_then_migrated_on_first_write(client):
    _manager, app = client
    room_dir = Path(app.config["ROOMS_DIR"]) / "room1"
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    info["members"] = [member for member in info["members"] if member["user_id"] == "u1"]
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    legacy = {
        "version": 1,
        "revision": 7,
        "scene_id": "default",
        "grid_size": 48,
        "bounds_cells": 50,
        "tokens": [
            {
                "id": "member-u1",
                "name": "Alice",
                "x": 96,
                "y": 96,
                "w": 1,
                "h": 1,
                "kind": "pc",
                "owner_user_id": "u1",
                "combatant": "Alice",
            }
        ],
        "walls": [{"id": "wall-a", "kind": "wall", "points": [{"x": 0, "y": 0}, {"x": 64, "y": 0}]}],
    }
    map_dir = room_dir / "plugins" / "veil-map"
    map_dir.mkdir(parents=True)
    (map_dir / "map.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    manager = app.test_client()
    _login(manager, "u1")
    opened = _map(manager)
    assert opened["map"]["revision"] == 7
    assert opened["map"]["grid_size"] == 48
    assert opened["map"]["walls"][0]["id"] == "wall-a"
    assert not _scene_dir(room_dir).exists()

    updated = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 32})
    assert updated.status_code == 200
    assert updated.get_json()["revision"] == 8
    assert _scene_dir(room_dir).exists()
    assert (map_dir / "map.json").exists()

    reopened = _map(manager)
    assert reopened["map"]["grid_size"] == 32
    assert reopened["map"]["revision"] == 8


def test_scene_loader_falls_back_to_previous_complete_generation(client):
    manager, app = client
    _login(manager, "u1")
    room_dir = Path(app.config["ROOMS_DIR"]) / "room1"

    first = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 48})
    assert first.status_code == 200
    second = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 56})
    assert second.status_code == 200
    latest = _generation_dir(room_dir)
    (latest / "tokens.json").unlink()

    state = veil_map._load_map(room_dir)
    assert state["revision"] == first.get_json()["revision"]
    assert state["grid_size"] == 48


def test_legacy_pixel_tokens_migrate_to_cells_without_visual_shift(client):
    _manager, app = client
    room_dir = Path(app.config["ROOMS_DIR"]) / "room1"
    info_path = room_dir / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["members"] = [member for member in info["members"] if member["user_id"] == "u1"]
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    legacy = {
        "version": 1,
        "revision": 3,
        "scene_id": "default",
        "grid_size": 64,
        "bounds_cells": 50,
        "tokens": [
            {
                "id": "member-u1",
                "name": "Alice",
                "x": 100,
                "y": 164,
                "w": 1,
                "h": 1,
                "kind": "pc",
                "owner_user_id": "u1",
                "combatant": "Alice",
            }
        ],
    }
    map_dir = room_dir / "plugins" / "veil-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "map.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    manager = app.test_client()
    _login(manager, "u1")
    token = _map(manager)["map"]["tokens"][0]
    assert (token["cell_x"], token["cell_y"]) == (1, 2)
    assert round(token["offset_x"], 4) == 0.5625
    assert round(token["offset_y"], 4) == 0.5625
    assert (token["x"], token["y"]) == (100, 164)

    saved = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"bounds_cells": 40})
    assert saved.status_code == 200
    stored = _read_map_tokens(room_dir)[0]
    assert (stored["cell_x"], stored["cell_y"]) == (1, 2)
    assert "x" not in stored and "y" not in stored

    # 改网格尺寸后棋子仍停在原来的格，不再相对网格漂移。
    resized = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"grid_size": 48})
    assert resized.status_code == 200
    resized_token = resized.get_json()["data"]["map"]["tokens"][0]
    assert (resized_token["cell_x"], resized_token["cell_y"]) == (1, 2)
    assert (resized_token["x"], resized_token["y"]) == (75, 123)


# ---------------------------------------------------------------------------
# 视线与迷雾
# ---------------------------------------------------------------------------


def test_visibility_blocked_by_wall_and_cached_by_version():
    module = veil_map.visibility
    module.reset_cache()
    walls = [
        {
            "id": "wall-fog",
            "kind": "wall",
            "points": [{"x": 320, "y": -1600}, {"x": 320, "y": 1600}],
        }
    ]
    version = module.state_version(walls)
    origin = (96, 32)  # 格 (1, 0) 的中心

    opened = module.visibility(
        origin,
        walls=[],
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        radius_cells=10,
        wall_version=module.state_version([]),
    )
    assert (10, 0) in opened["cells"]

    blocked = module.visibility(
        origin,
        walls=walls,
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        radius_cells=10,
        wall_version=version,
    )
    assert (0, 0) in blocked["cells"] and (1, 0) in blocked["cells"]
    assert (6, 0) not in blocked["cells"]
    assert (10, 0) not in blocked["cells"]
    assert len(blocked["polygon"]) >= 4

    again = module.visibility(
        origin,
        walls=walls,
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        radius_cells=10,
        wall_version=version,
    )
    assert again["cached"] is True

    # 墙体版本变化立即失效缓存，并反映新的几何。
    changed = module.visibility(
        origin,
        walls=[],
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        radius_cells=10,
        wall_version="walls-v2",
    )
    assert changed["cached"] is False
    assert (10, 0) in changed["cells"]


def test_fog_mask_roundtrip():
    module = veil_map.visibility
    cells = {(0, 0), (1, 0), (-5, 3), (9, -9)}
    encoded = module.encode_mask(cells, 20)
    assert module.decode_mask(encoded, 20) == cells
    assert module.decode_mask("", 20) == set()


def _fog_api_room(client):
    manager, app = client
    _login(manager, "u1")
    _map(manager)
    added = _write(manager, "post", "/api/plugins/veil-map/rooms/room1/tokens", {
        "name": "Ogre",
        "cell_x": 10,
        "cell_y": 0,
    })
    assert added.status_code == 200
    walls = [
        {
            "id": "wall-fog",
            "kind": "wall",
            "points": [{"x": 320, "y": -1600}, {"x": 320, "y": 1600}],
        }
    ]
    saved = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/walls", {"walls": walls})
    assert saved.status_code == 200
    return app, Path(app.config["ROOMS_DIR"]) / "room1"


def test_fog_filters_player_payload_and_keeps_kp_full(client):
    manager, _unused = client
    app, room_dir = _fog_api_room(client)
    visitor = app.test_client()
    _login(visitor, "u2")

    player = _map(visitor)
    names = {token["name"] for token in player["map"]["tokens"]}
    assert "Ogre" not in names
    assert {"Alice", "Bob", "Carol"} <= names
    assert {"wall-fog"} <= {wall["id"] for wall in player["map"]["walls"]}
    assert player["fog"]["enabled"] is True
    assert player["fog"]["viewer"] == "player"
    visible = veil_map.visibility.decode_mask(player["fog"]["visible"], 50)
    assert (1, 0) in visible
    assert (10, 0) not in visible
    assert veil_map.visibility.decode_mask(player["fog"]["explored"], 50)

    kp = _map(manager)
    assert "Ogre" in {token["name"] for token in kp["map"]["tokens"]}
    assert kp["fog"]["viewer"] == "kp"


def test_fog_broadcast_is_cropped_per_observer(client):
    manager, _unused = client
    app, room_dir = _fog_api_room(client)
    token_id = next(
        token["id"] for token in _map(manager)["map"]["tokens"] if token["name"] == "Ogre"
    )
    app.extensions["socketio"].events.clear()

    context = _agent_context(room_dir, user_id="u1", agent_id="kp")
    with app.app_context():
        result = veil_map._move_token_agent(
            room_dir,
            context.room_info(),
            room_id="room1",
            user_id="u1",
            agent_id="kp",
            token_ref=token_id,
            cell_x=11,
            cell_y=0,
        )
    assert result["ok"] is True, result

    events = {
        str(item["room"]): item["payload"]
        for item in app.extensions["socketio"].events
        if item["event"] == "room_map_changed"
    }
    assert "room1" not in events
    assert "move" in events["user:u1"]
    assert "move" not in events["user:u2"]
    assert "Ogre" not in {token["name"] for token in events["user:u2"]["map"]["tokens"]}
    assert "Ogre" in {token["name"] for token in events["user:u1"]["map"]["tokens"]}


def test_fog_explored_mask_is_persisted_with_scene_revision(client):
    _manager, app = client
    _app, room_dir = _fog_api_room(client)
    generation = _generation_dir(room_dir)
    fog_payload = json.loads((generation / "fog.json").read_text(encoding="utf-8"))
    scene = json.loads((generation / "scene.json").read_text(encoding="utf-8"))
    assert fog_payload["revision"] == scene["revision"]
    assert fog_payload["fog"]["enabled"] is True
    explored = veil_map.visibility.decode_mask(fog_payload["fog"]["explored"], 50)
    assert (1, 0) in explored or (0, 0) in explored

    reloaded = veil_map._load_map(room_dir)
    assert reloaded["fog"]["explored"]
    assert reloaded["fog"]["bounds_cells"] == 50


def test_fog_can_be_disabled_and_reenabled(client):
    manager, _unused = client
    app, _room_dir = _fog_api_room(client)
    off = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"fog_enabled": False})
    assert off.status_code == 200
    assert off.get_json()["data"]["map"]["fog"]["enabled"] is False

    visitor = app.test_client()
    _login(visitor, "u2")
    player = _map(visitor)
    assert player["fog"]["enabled"] is False
    assert "Ogre" in {token["name"] for token in player["map"]["tokens"]}

    on = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/map", {"fog_enabled": True})
    assert on.status_code == 200
    assert on.get_json()["data"]["map"]["fog"]["enabled"] is True


def test_coc7_room_has_no_fog_entry(tmp_path):
    rooms = _write_room(tmp_path)
    info_path = rooms / "room1" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["ruleset_id"] = "coc7"
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.register_blueprint(veil_map.bp)
    client = app.test_client()
    _login(client, "u1")

    body = client.get("/api/plugins/veil-map/rooms/room1/map").get_json()
    assert body["error"] == "map.ruleset_disabled"
    assert "fog" not in body

    lights = client.put(
        "/api/plugins/veil-map/rooms/room1/lights",
        json={"lights": [], "request_id": "coc7-lights", "revision": 1},
    ).get_json()
    assert lights["error"] == "map.ruleset_disabled"


# ---------------------------------------------------------------------------
# 动态光源投影
# ---------------------------------------------------------------------------


def test_lighting_splits_bright_dim_and_wall_shadow():
    module = veil_map.visibility
    module.reset_cache()
    walls = [
        {
            "id": "wall-light",
            "kind": "wall",
            "blocks_sight": True,
            "blocks_light": True,
            "points": [{"x": 320, "y": -1600}, {"x": 320, "y": 1600}],
        }
    ]
    version = module.state_version(walls)
    first = module.lighting(
        (96, 32),
        walls=walls,
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        bright_cells=4,
        dim_cells=8,
        wall_version=version,
        light_version="lights-v1",
        room_key="room-light",
    )
    assert (1, 0) in first["bright"]
    assert (4, 0) in first["bright"]
    assert (5, 0) not in first["bright"] and (5, 0) not in first["dim"]
    assert (-4, 0) in first["dim"]
    assert (-6, 0) in first["dim"]
    assert (-9, 0) not in first["dim"]
    assert first["cached"] is False

    again = module.lighting(
        (96, 32),
        walls=walls,
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        bright_cells=4,
        dim_cells=8,
        wall_version=version,
        light_version="lights-v1",
        room_key="room-light",
    )
    assert again["cached"] is True

    # 光源版本变化（开关、移动、半径调整）必须让投影缓存失效。
    changed = module.lighting(
        (96, 32),
        walls=walls,
        grid_size=64,
        offset=(0, 0),
        bounds_cells=50,
        bright_cells=4,
        dim_cells=8,
        wall_version=version,
        light_version="lights-v2",
        room_key="room-light",
    )
    assert changed["cached"] is False


def test_active_light_sources_include_token_carry_and_priority_cap():
    lights = [
        {
            "id": f"light-{index}",
            "x": 0,
            "y": 0,
            "bright_cells": 0,
            "range_cells": index,
            "enabled": True,
        }
        for index in range(1, 31)
    ]
    state = {
        "grid_size": 64,
        "grid_offset": {"x": 0, "y": 0},
        "tokens": [],
        "lights": lights,
    }
    sources = veil_map._active_light_sources(state)
    assert len(sources) == veil_map.MAX_ACTIVE_LIGHTS
    assert sources[0]["dim_cells"] == 30
    assert sources[-1]["dim_cells"] == 7

    carried = {
        "grid_size": 64,
        "grid_offset": {"x": 0, "y": 0},
        "lights": [],
        "tokens": [
            {
                "id": "token-light",
                "kind": "pc",
                "cell_x": 0,
                "cell_y": 0,
                "w": 2,
                "h": 2,
                "light": {"bright_cells": 4, "dim_cells": 8, "enabled": True},
            }
        ],
    }
    carried_sources = veil_map._active_light_sources(carried)
    assert len(carried_sources) == 1
    assert carried_sources[0]["token_id"] == "token-light"
    assert carried_sources[0]["follows_token"] is True
    assert (carried_sources[0]["x"], carried_sources[0]["y"]) == (64, 64)


def test_scene_light_toggle_changes_player_lighting(client):
    manager, _app = client
    _login(manager, "u1")
    _map(manager)
    light = {
        "id": "torch-1",
        "x": 96,
        "y": 32,
        "bright_cells": 3,
        "range_cells": 6,
        "color": "#ffcc66",
    }
    saved = _write(manager, "put", "/api/plugins/veil-map/rooms/room1/lights", {"lights": [light]})
    assert saved.status_code == 200

    visitor = _app.test_client()
    _login(visitor, "u2")
    player = _map(visitor)
    assert player["fog"]["lighting"] == "sources"
    assert player["fog"]["light_sources"] == 1
    bright = veil_map.visibility.decode_mask(player["fog"]["bright"], 50)
    dim = veil_map.visibility.decode_mask(player["fog"]["dim"], 50)
    assert (1, 0) in bright
    assert (5, 0) in dim
    assert (8, 0) not in bright and (8, 0) not in dim

    off = _write(
        manager,
        "put",
        "/api/plugins/veil-map/rooms/room1/lights",
        {"lights": [dict(light, enabled=False)]},
    )
    assert off.status_code == 200
    dark = _map(visitor)
    assert dark["fog"]["lighting"] == "none"
    assert veil_map.visibility.decode_mask(dark["fog"]["bright"], 50) == veil_map.visibility.decode_mask(
        dark["fog"]["visible"], 50
    )


def test_token_light_permissions_and_removal(client):
    manager, app = client
    _login(manager, "u1")
    state = _map(manager)
    tokens = {token["name"]: token["id"] for token in state["map"]["tokens"]}
    bob_id = tokens["Bob"]
    alice_id = tokens["Alice"]

    visitor = app.test_client()
    _login(visitor, "u2")
    added = _write(
        visitor,
        "put",
        f"/api/plugins/veil-map/rooms/room1/tokens/{bob_id}/light",
        {"light": {"bright_cells": 4, "dim_cells": 8, "color": "#ffaa00"}},
    )
    assert added.status_code == 200
    body = added.get_json()["data"]
    assert body["token"]["light"]["enabled"] is True
    assert body["fog"]["lighting"] == "sources"

    denied = _write(
        visitor,
        "put",
        f"/api/plugins/veil-map/rooms/room1/tokens/{alice_id}/light",
        {"light": {"bright_cells": 4, "dim_cells": 8}},
    )
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "map.not_owner"

    removed = _write(
        visitor,
        "put",
        f"/api/plugins/veil-map/rooms/room1/tokens/{bob_id}/light",
        {"remove": True},
    )
    assert removed.status_code == 200
    assert removed.get_json()["data"]["token"]["light"] is None

    manager_added = _write(
        manager,
        "put",
        f"/api/plugins/veil-map/rooms/room1/tokens/{alice_id}/light",
        {"light": {"bright_cells": 2, "dim_cells": 4}},
    )
    assert manager_added.status_code == 200
    assert manager_added.get_json()["data"]["token"]["light"]["enabled"] is True
