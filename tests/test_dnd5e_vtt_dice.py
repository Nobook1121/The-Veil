"""dnd5e-vtt 插件（M4）：骰桌动画设置端点。

覆盖：

- 写权限：DM（房主/房管）可改 enabled / max_dice；**玩家 POST 一律 403
  （rejected）**，存储文件不产生、socket 无广播；
- ``max_dice`` clamp 1–20：超限与非法输入折回边界或 400 invalid；
- 默认值：未写过设置时 GET 返回 ``{enabled: true, max_dice: 20}``，
  读操作不落盘；
- 广播：DM 写入成功后向房间 emit ``dnd5e_vtt_dice_settings``
  （payload {room_id, enabled, max_dice}），玩家读端即时应用；
- 观察者差异：玩家 GET 可读（``can_manage=false``）、DM ``can_manage=true``；
- 门闸与 /panels 同口径（登录 / 成员 / 规则集）；
- 设置读写完全不依赖 veil-map：装置只注册 dnd5e-vtt 蓝图，
  存储路径 ``<room>/plugins/dnd5e-vtt/dice-settings.json``。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
VTT_DIR = PLUGIN_ROOT / "dnd5e-vtt"
VTT_ENTRY = VTT_DIR / "entry.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dnd5e_vtt = _load_module(VTT_ENTRY, "dnd5e_vtt_entry_dice_test")


class _FakeSocket:
    """socketio 替身：只记录 emit 的事件名与载荷（学 veil-map 插件测试装置）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, payload: Any, room: str | None = None, **_kwargs) -> None:
        self.events.append({"event": event, "payload": payload, "room": room})


@pytest.fixture()
def plugins():
    """装载真实插件根：dnd5e 规则集随之并入 builtin registry，房间规则集解析可用。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


# ---------------------------------------------------------------- 装置


def _write_room(rooms: Path, room_id: str = "room1", *, ruleset_id: str = "dnd5e") -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "骰桌测试房",
        "room_code": room_id.upper(),
        "creator_id": "1",
        "ruleset_id": ruleset_id,
        "members": [
            {
                "user_id": "1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "艾莉丝"},
            },
            {
                "user_id": "2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "鲍勃"},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_state(rooms: Path, room_id: str = "room1") -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    (room / "state.json").write_text("{}", encoding="utf-8")


def _make_client(tmp_path: Path, *, extra_room_ruleset: str | None = None):
    """只注册 dnd5e-vtt 蓝图：证明骰桌设置读写不依赖 veil-map 等其他插件。"""
    rooms = tmp_path / "rooms"
    _write_room(rooms)
    _write_state(rooms)
    if extra_room_ruleset:
        _write_room(rooms, "room2", ruleset_id=extra_room_ruleset)
        _write_state(rooms, "room2")
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=rooms, CONFIG_DIR=str(config), SCENARIOS_DIR=scenarios)
    app.extensions["socketio"] = _FakeSocket()
    app.register_blueprint(dnd5e_vtt.bp)
    return app.test_client(), rooms, app.extensions["socketio"]


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=user_id, role="USER")


def _settings_path(rooms: Path, room_id: str = "room1") -> Path:
    return rooms / room_id / "plugins" / "dnd5e-vtt" / "dice-settings.json"


def _stored_settings(rooms: Path, room_id: str = "room1") -> dict[str, Any] | None:
    path = _settings_path(rooms, room_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


SETTINGS_URL = "/api/plugins/dnd5e-vtt/rooms/room1/dice-settings"


# ---------------------------------------------------------------- DM 写入与广播


def test_dm_updates_dice_settings_and_broadcasts(tmp_path, plugins):
    client, rooms, socket = _make_client(tmp_path)
    _login(client, "1")
    response = client.post(SETTINGS_URL, json={"enabled": False, "max_dice": 6})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["success"] is True
    assert body["status"] == "success"
    assert body["code"] == "dnd5e_vtt.dice_settings_updated"
    data = body["data"]
    assert data == {"enabled": False, "max_dice": 6, "can_manage": True, "observer": "dm"}
    # 存储落盘在 dnd5e-vtt 自己的插件目录（不依赖 veil-map 的存储）。
    stored = _stored_settings(rooms)
    assert stored is not None
    assert stored["enabled"] is False
    assert stored["max_dice"] == 6
    assert stored["version"] == 1
    assert stored["updated_at"]
    # 写入成功后向房间广播一次设置。
    events = [item for item in socket.events if item["event"] == "dnd5e_vtt_dice_settings"]
    assert len(events) == 1
    assert events[0]["room"] == "room1"
    assert events[0]["payload"] == {"room_id": "room1", "enabled": False, "max_dice": 6}


def test_dm_partial_updates_keep_other_field(tmp_path, plugins):
    client, rooms, socket = _make_client(tmp_path)
    _login(client, "1")
    first = client.post(SETTINGS_URL, json={"enabled": False})
    assert first.status_code == 200
    assert first.get_json()["data"] == {"enabled": False, "max_dice": 20, "can_manage": True, "observer": "dm"}
    # 只改 max_dice：enabled 保持上次的 False（合并写入而非整表覆盖）。
    second = client.post(SETTINGS_URL, json={"max_dice": 12})
    assert second.status_code == 200
    assert second.get_json()["data"]["enabled"] is False
    assert second.get_json()["data"]["max_dice"] == 12
    stored = _stored_settings(rooms)
    assert (stored["enabled"], stored["max_dice"]) == (False, 12)
    assert len([e for e in socket.events if e["event"] == "dnd5e_vtt_dice_settings"]) == 2


def test_player_cannot_write_dice_settings(tmp_path, plugins):
    client, rooms, socket = _make_client(tmp_path)
    _login(client, "2")  # bob：普通成员
    response = client.post(SETTINGS_URL, json={"enabled": False, "max_dice": 4})
    assert response.status_code == 403
    body = response.get_json()
    assert body["status"] == "rejected"
    assert body["success"] is False
    assert body["code"] == "dnd5e_vtt.dice_settings_dm_only"
    # 玩家请求不产生存储、不产生广播。
    assert _stored_settings(rooms) is None
    assert socket.events == []


def test_dm_only_gates_apply_to_get_and_post(tmp_path, plugins):
    """门闸与 /panels 同口径：登录 / 成员 / 规则集。"""
    client, _rooms, _socket = _make_client(tmp_path, extra_room_ruleset="nosuch-ruleset")
    assert client.get(SETTINGS_URL).status_code == 401  # 未登录
    _login(client, "9")
    assert client.get(SETTINGS_URL).status_code == 403  # 非成员
    assert client.post(SETTINGS_URL, json={"max_dice": 5}).status_code == 403
    assert client.get("/api/plugins/dnd5e-vtt/rooms/nope/dice-settings").status_code == 404
    # 规则集未启用战桌：成员访问也拒绝（与 /panels 同口径）。
    _login(client, "1")  # room2 的房主
    assert client.get("/api/plugins/dnd5e-vtt/rooms/room2/dice-settings").status_code == 400
    assert (
        client.post("/api/plugins/dnd5e-vtt/rooms/room2/dice-settings", json={"max_dice": 5}).status_code
        == 400
    )


# ---------------------------------------------------------------- clamp 与校验


def test_max_dice_clamped_to_1_20(tmp_path, plugins):
    client, _rooms, _socket = _make_client(tmp_path)
    _login(client, "1")
    for raw, expected in ((999, 20), (0, 1), (-7, 1), (13.6, 14), ("8", 8), (19.7, 20)):
        response = client.post(SETTINGS_URL, json={"max_dice": raw})
        assert response.status_code == 200, (raw, response.get_json())
        assert response.get_json()["data"]["max_dice"] == expected, raw


def test_invalid_dice_settings_payloads(tmp_path, plugins):
    client, rooms, _socket = _make_client(tmp_path)
    _login(client, "1")
    bad = client.post(SETTINGS_URL, data="not-json", content_type="application/json")
    assert bad.status_code == 400 and bad.get_json()["status"] == "invalid"
    bad = client.post(SETTINGS_URL, json={})
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.dice_settings_empty"
    bad = client.post(SETTINGS_URL, json={"enabled": "yes"})
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.invalid_enabled"
    bad = client.post(SETTINGS_URL, json={"max_dice": "abc"})
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.invalid_max_dice"
    bad = client.post(SETTINGS_URL, json={"max_dice": True})
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.invalid_max_dice"
    # 全部非法请求都不落盘。
    assert _stored_settings(rooms) is None


# ---------------------------------------------------------------- 读差异与默认值


def test_defaults_without_store_and_read_does_not_persist(tmp_path, plugins):
    client, rooms, _socket = _make_client(tmp_path)
    _login(client, "1")
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data == {"enabled": True, "max_dice": 20, "can_manage": True, "observer": "dm"}
    # GET 是只读：不产生存储文件。
    assert _stored_settings(rooms) is None


def test_player_get_is_read_only_view(tmp_path, plugins):
    client, _rooms, _socket = _make_client(tmp_path)
    _login(client, "1")
    client.post(SETTINGS_URL, json={"enabled": False, "max_dice": 8})
    _login(client, "2")  # bob：普通成员
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["enabled"] is False
    assert data["max_dice"] == 8
    assert data["can_manage"] is False
    assert data["observer"] == "player"
    assert "actions" not in data  # 骰桌设置响应不夹带其他面板数据


def test_corrupt_store_falls_back_to_defaults(tmp_path, plugins):
    """存储损坏或字段越界：读侧归一折回合法值，不 500。"""
    client, rooms, _socket = _make_client(tmp_path)
    _login(client, "1")
    path = _settings_path(rooms)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")  # 列表形态：非法存储
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "enabled": True,
        "max_dice": 20,
        "can_manage": True,
        "observer": "dm",
    }
    path.write_text(json.dumps({"enabled": "off", "max_dice": 500}), encoding="utf-8")
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["enabled"] is True  # 非布尔按缺省 true 归一
    assert data["max_dice"] == 20   # 越界 clamp 回上限


def test_settings_survive_dm_relogin_and_broadcast_payload_is_latest(tmp_path, plugins):
    client, rooms, socket = _make_client(tmp_path)
    _login(client, "1")
    client.post(SETTINGS_URL, json={"enabled": True, "max_dice": 3})
    client.post(SETTINGS_URL, json={"enabled": False})
    stored = _stored_settings(rooms)
    assert (stored["enabled"], stored["max_dice"]) == (False, 3)
    events = [item for item in socket.events if item["event"] == "dnd5e_vtt_dice_settings"]
    # 每次成功写一条广播，载荷是当时的最新值。
    assert [event["payload"] for event in events] == [
        {"room_id": "room1", "enabled": True, "max_dice": 3},
        {"room_id": "room1", "enabled": False, "max_dice": 3},
    ]
