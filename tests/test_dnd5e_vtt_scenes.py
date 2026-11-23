"""dnd5e-vtt：场景面板（真实 veil-map 场景目录）与 DM 场景切换端点。

覆盖：

- DM 响应里的 scenes tab 是真实数据：``scene_id`` / ``title`` / ``active`` /
  ``tokens`` / ``grid_size`` / ``thumb``；缩略图就是 veil-map 的背景资产地址
  （服务端不做图片处理，前端 object-fit 裁）；
- 玩家响应里 scenes tab 整块不出现（服务端裁剪，不是前端隐藏）；
- ``POST /rooms/<id>/scenes/<scene_id>/activate``：正常切换会落盘 + 广播
  ``room_map_changed``（观察者裁剪复用 veil-map）+ ``dnd5e_vtt_scene_switch``；
  非 DM 403、未知场景 404、veil-map 缺席 503，错误码稳定；
- veil-map 缺席时场景面板降级为空清单 + 可读提示，不报错。

场景数据用 **真实的 veil-map 服务对象**（``_MAP_SERVICE`` + ``scene_store``），
不用假服务：本刀要验证的正是两个插件之间的真实契约。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
VTT_ENTRY = PLUGIN_ROOT / "dnd5e-vtt" / "entry.py"
MAP_ENTRY = PLUGIN_ROOT / "veil-map" / "entry.py"

CAVE_BG = "/api/plugins/veil-map/rooms/room1/assets/maps/9f2c1a.jpg"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dnd5e_vtt = _load_module(VTT_ENTRY, "dnd5e_vtt_entry_scenes_test")
veil_map = _load_module(MAP_ENTRY, "veil_map_entry_scenes_test")


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event, payload, room=None, **_kwargs):
        self.events.append({"event": event, "payload": payload, "room": room})

    def names(self) -> list[str]:
        return [item["event"] for item in self.events]


def _scene_state(scene_id: str, title: str, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "scene_id": scene_id,
        "title": title,
        "revision": 1,
        "background": "",
        "grid_size": 64,
        "grid_offset": {"x": 0, "y": 0},
        "bounds_cells": 40,
        "tokens": [],
        "walls": [],
        "lights": [],
        "fog": {},
    }
    state.update(overrides)
    return state


def _write_room(rooms: Path, room_id: str = "room1", *, ruleset_id: str = "dnd5e") -> Path:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "场景面板测试房",
        "room_code": room_id.upper(),
        "creator_id": "1",
        "ruleset_id": ruleset_id,
        "members": [
            {"user_id": "1", "username": "alice", "room_role": "owner", "status": "active", "is_active": True},
            {"user_id": "2", "username": "bob", "room_role": "member", "status": "active", "is_active": True},
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room


def _seed_scenes(room: Path) -> None:
    """用 veil-map 的真实存储写两个场景：洞穴（当前）+ 大厅（带棋子）。"""
    veil_map.scene_store.save(
        room,
        _scene_state(
            "cave",
            "银矿洞口",
            background=CAVE_BG,
            tokens=[{"id": "t1", "name": "哥布林", "cell_x": 1, "cell_y": 1, "w": 1, "h": 1}],
        ),
    )
    veil_map.scene_store.save(
        room,
        _scene_state(
            "hall",
            "废弃大厅",
            revision=2,
            background="",
            grid_size=96,
            tokens=[
                {"id": "t2", "name": "骷髅", "cell_x": 2, "cell_y": 2, "w": 1, "h": 1},
                {"id": "t3", "name": "僵尸", "cell_x": 3, "cell_y": 2, "w": 1, "h": 1},
            ],
        ),
        activate=False,
    )


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """房间 + 真实 veil-map 服务 + 假 socketio。"""
    rooms = tmp_path / "rooms"
    room = _write_room(rooms)
    _seed_scenes(room)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=rooms,
        CONFIG_DIR=str(config),
        SCENARIOS_DIR=tmp_path / "scenarios",
    )
    socket = _FakeSocket()
    app.extensions["socketio"] = socket
    app.register_blueprint(dnd5e_vtt.bp)
    app.register_blueprint(veil_map.bp)
    monkeypatch.setattr(dnd5e_vtt, "_map_service", lambda: veil_map._MAP_SERVICE)
    return app.test_client(), socket, room


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=user_id, role="USER")


def _panels(client, room_id: str = "room1"):
    return client.get(f"/api/plugins/dnd5e-vtt/panels?room={room_id}")


def _scenes_panel(data: dict[str, Any]) -> dict[str, Any]:
    for panel in data["panels"]:
        if panel.get("id") == "scenes":
            return panel
    raise AssertionError("场景面板缺失")


def _activate(client, scene_id: str, room_id: str = "room1"):
    return client.post(
        f"/api/plugins/dnd5e-vtt/rooms/{room_id}/scenes/{scene_id}/activate",
        json={},
    )


# ---------------------------------------------------------------- 面板数据


def test_dm_scene_panel_lists_real_scenes_with_thumbnails(harness):
    client, _socket, room = harness
    _login(client, "1")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["observer"] == "dm"
    scenes = _scenes_panel(data)
    items = scenes["items"]
    assert [item["scene_id"] for item in items] == ["cave", "hall"]

    cave = next(item for item in items if item["scene_id"] == "cave")
    assert cave["title"] == "银矿洞口"
    assert cave["active"] is True
    assert cave["tokens"] == 1
    assert cave["grid_size"] == 64
    # 缩略图 = 背景资产地址本身（服务端不生成图片），前端 object-fit: cover 裁。
    assert cave["thumb"] == CAVE_BG
    assert cave["thumb"].startswith("/api/plugins/veil-map/rooms/room1/assets/maps/")

    hall = next(item for item in items if item["scene_id"] == "hall")
    assert hall["active"] is False
    assert hall["tokens"] == 2
    assert hall["grid_size"] == 96
    # 场景没有背景资产时如实给空串，不编造缩略图。
    assert hall["thumb"] == ""
    assert scenes["hint"] == ""
    # 切换入口由服务端下发，前端照着拼地址。
    assert scenes["activate_path"].endswith("/scenes/{scene_id}/activate")
    # 真实存储没被改动：仍在 cave。
    assert veil_map.scene_store.active_scene_id(room) == "cave"


def test_player_never_receives_scene_panel(harness):
    client, _socket, _room = harness
    _login(client, "2")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["observer"] == "player"
    assert all(panel.get("id") != "scenes" for panel in data["panels"])
    text = response.get_data(as_text=True)
    assert "银矿洞口" not in text
    assert "废弃大厅" not in text
    assert CAVE_BG not in text
    # 玩家也打不了切换端点。
    assert _activate(client, "hall").status_code == 403


def test_scene_panel_degrades_without_map_service(harness, monkeypatch):
    client, _socket, _room = harness
    monkeypatch.setattr(dnd5e_vtt, "_map_service", lambda: None)
    _login(client, "1")
    scenes = _scenes_panel(_panels(client).get_json()["data"])
    assert scenes["items"] == []
    assert "veil-map" in scenes["hint"]


# ---------------------------------------------------------------- 切换场景


def test_dm_activate_switches_scene_and_broadcasts(harness):
    client, socket, room = harness
    _login(client, "1")
    response = _activate(client, "hall")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ok"] is True
    assert data["scene_id"] == "hall"
    assert data["title"] == "废弃大厅"
    assert data["changed"] is True

    # 真实落盘：房间当前场景已切到 hall。
    assert veil_map.scene_store.active_scene_id(room) == "hall"

    # 地图广播走 veil-map 的观察者裁剪（复用，不重写）：迷雾开启时逐成员推到
    # user:<id>，关闭时整房间频道；两条路都只推刚激活的场景。
    map_events = [item for item in socket.events if item["event"] == "room_map_changed"]
    assert map_events
    assert all(item["payload"]["map"]["scene_id"] == "hall" for item in map_events)
    assert all(item["room"] in {"room1", "user:1", "user:2"} for item in map_events)

    # 场景切换事件给前端盖遮罩（与 AI 工具同一事件、by 区分发起方）。
    switch_events = [item for item in socket.events if item["event"] == "dnd5e_vtt_scene_switch"]
    assert len(switch_events) == 1
    payload = switch_events[0]["payload"]
    assert payload == {"room_id": "room1", "scene_id": "hall", "title": "废弃大厅", "by": "dm"}

    # 面板随后如实反映新当前场景。
    items = _scenes_panel(_panels(client).get_json()["data"])["items"]
    assert next(item for item in items if item["scene_id"] == "hall")["active"] is True
    assert next(item for item in items if item["scene_id"] == "cave")["active"] is False


def test_activate_current_scene_is_idempotent_and_quiet(harness):
    client, socket, _room = harness
    _login(client, "1")
    response = _activate(client, "cave")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["scene_id"] == "cave"
    assert data["changed"] is False
    # 已是当前场景：不重播地图广播，也不弹遮罩。
    assert socket.events == []


def test_activate_requires_dm(harness):
    client, _socket, room = harness
    # 未登录 → 401
    assert _activate(client, "hall").status_code == 401
    # 房间成员（非房主/房管）→ 403，错误码稳定
    _login(client, "2")
    denied = _activate(client, "hall")
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "dnd5e_vtt.dm_only"
    # 非成员 → 403（房间门闸）
    _login(client, "9")
    assert _activate(client, "hall").status_code == 403
    # 场景没有被动过。
    assert veil_map.scene_store.active_scene_id(room) == "cave"


def test_activate_unknown_scene_is_404(harness):
    client, _socket, room = harness
    _login(client, "1")
    response = _activate(client, "no-such-scene")
    assert response.status_code == 404
    assert response.get_json()["error"] == "dnd5e_vtt.scene_missing"
    assert veil_map.scene_store.active_scene_id(room) == "cave"


def test_activate_without_map_service_is_503(harness, monkeypatch):
    client, _socket, _room = harness
    _login(client, "1")
    monkeypatch.setattr(dnd5e_vtt, "_map_service", lambda: None)
    response = _activate(client, "hall")
    assert response.status_code == 503
    assert response.get_json()["error"] == "dnd5e_vtt.scene_unavailable"


def test_activate_unknown_room_is_404(harness):
    client, _socket, _room = harness
    _login(client, "1")
    assert _activate(client, "hall", room_id="nope").status_code == 404
