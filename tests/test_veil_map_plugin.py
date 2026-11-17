"""veil-map 插件：token 权限、网格吸附与战斗移动预算。"""

from __future__ import annotations

import importlib.util
import json
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
    assert events and events[-1]["room"] == "room1"


def test_combat_move_uses_turn_and_movement_budget(tmp_path):
    rooms = _write_room(tmp_path, combat=_combat(["Alice", "Bob"]))
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
    assert len(events) == 1


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
    path = room_dir / "plugins" / "veil-map" / "map.json"
    return json.loads(path.read_text(encoding="utf-8"))["tokens"]


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
    assert (bob["x"], bob["y"]) == (128, 192)

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
    assert (alice["x"], alice["y"]) == (192, 0)


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
