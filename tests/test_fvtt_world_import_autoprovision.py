"""fvtt-world-import 第六刀：剧本暂存（stash）+ 进房自动装配（auto-provision）。

覆盖（两包真实 zip 对账 + 端到端 + 幂等矩阵 + 门闸）：

- ``POST /scenario/import`` 的暂存产物：heroes → ``fvtt-world/stash.json``
  4 个可转换场景（含 282px/格的「宅邸（二层）」，旧 256 上限会整场跳过），
  active 场景带墙/光/棋子，
  背景图内容寻址落剧本 ``assets/``（stash 只引用相对路径）；12 个 NPC
  statblock（fvtt_actor_id 幂等键）；sixian → 6 场景 + 14 NPC；
- ``POST /rooms/<id>/auto-provision`` 端到端（真实 veil-map ``map_service``）：
  房间出现 4 个场景 + active 场景含棋子、NPC 模块幂等并入剧本并升版重钉
  （0.1.6 → 0.1.7）、装配标记落盘、socket 广播 ``room_map_changed``；
- 幂等矩阵：二调 ``already_provisioned`` 零写入、房间已有场景
  ``room_has_scenes``、无 stash 剧本 ``scenario_not_world``（可读文案）、
  无绑定剧本 ``scenario_missing``；
- 门闸：玩家身份 403 ``fvtt.manager_only``、非 dnd5e 房 403
  ``fvtt.ruleset_unsupported``。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
VEIL_ENTRY = REPO_ROOT / "data" / "plugins" / "veil-map" / "entry.py"
FVTT_ENTRY = PLUGIN_DIR / "entry.py"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


veil_map = _load_module(VEIL_ENTRY, "veil_map_entry_autoprovision_test")
fvtt = _load_module(FVTT_ENTRY, "fvtt_world_import_entry_autoprovision_test")


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


@pytest.fixture()
def plugins():
    """装载全部插件：dnd5e 规则集注册 + veil-map 真实 map_service 挂进运行时。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


# -- 装置 ------------------------------------------------------------------


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event, payload, room=None, **_kwargs):
        self.events.append({"event": event, "payload": payload, "room": room})


def _write_room(
    rooms: Path,
    room_id: str,
    *,
    ruleset_id: str = "dnd5e",
    scenario_id: int | str | None = 101,
    scenario_version: str = "1.0.0",
) -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "自动装配测试房间",
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
                "character_card": {"name": "Alice"},
            },
            {
                "user_id": "2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Bob"},
            },
        ],
    }
    if scenario_id is not None:
        info["scenario_id"] = str(scenario_id)
        info["scenario_version"] = scenario_version
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_plain_scenario(scenarios: Path, scenario_id: int = 999) -> None:
    """手工剧本（非世界包导入，没有 stash）：跳过路径的对照组。"""
    sdir = scenarios / f"scenario-{scenario_id}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(
        json.dumps(
            {
                "id": scenario_id,
                "title": "手工剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "scenario_version": "1.0.0",
                "modules": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_app(tmp_path: Path, *, ruleset_id: str = "dnd5e"):
    rooms = tmp_path / "rooms"
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    socket = _FakeSocket()
    app.extensions["socketio"] = socket
    app.config.update(
        ROOMS_DIR=rooms,
        CONFIG_DIR=str(config),
        SCENARIOS_DIR=scenarios,
    )
    app.register_blueprint(fvtt.bp)
    return app.test_client(), rooms, scenarios, socket


def _login(client, user_id: int, username: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=username, role="USER")


def _import_scenario(client, zip_path: Path, *, user_id: int = 1, username: str = "alice") -> dict[str, Any]:
    _login(client, user_id, username)
    with open(zip_path, "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/scenario/import",
            data={"file": (handle, zip_path.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 200, rv.get_data(as_text=True)
    return rv.get_json()["data"]


def _provision(client, room_id: str = "room1", *, user_id: int = 1, username: str = "alice"):
    _login(client, user_id, username)
    return client.post(f"/api/plugins/fvtt-world-import/rooms/{room_id}/auto-provision")


def _stash(scenarios: Path, scenario_id: int) -> dict[str, Any]:
    payload = json.loads(
        (scenarios / f"scenario-{scenario_id}" / "fvtt-world" / "stash.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(payload, dict)
    return payload


def _record(scenarios: Path, scenario_id: int) -> dict[str, Any]:
    payload = json.loads(
        (scenarios / f"scenario-{scenario_id}" / "scenario.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _room_info(rooms: Path, room_id: str = "room1") -> dict[str, Any]:
    return json.loads((rooms / room_id / "info.json").read_text(encoding="utf-8"))


def _marker(rooms: Path, room_id: str = "room1") -> dict[str, Any]:
    payload = json.loads(
        (rooms / room_id / "plugins" / "fvtt-world-import" / "provision.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(payload, dict)
    return payload


def _png_bytes(size: int = 64) -> bytes:
    """最小灰度 PNG（无 Pillow 依赖），供预置场景跳过路径使用。"""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes([200]) * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


# -- scenario/import 暂存对账 ----------------------------------------------


def test_import_heroes_stashes_scenes_and_npcs(tmp_path, plugins):
    client, _rooms, scenarios, _socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(HEROES_ZIP))
    scenario_id = int(data["scenario_id"])
    storage = scenarios / f"scenario-{scenario_id}"

    # 响应带 stash 摘要；开团指引改为「进房自动装配」+ 手动兜底。
    assert data["stash"] == {
        "written": True,
        "scenes": 4,
        "skipped": 0,
        "npcs": 12,
        "assets_bytes": data["stash"]["assets_bytes"],
        "warnings": data["stash"]["warnings"],
    }
    assert data["stash"]["assets_bytes"] > 0
    assert data["open_room"]["auto_provision"] is True
    assert any("auto-provision" in step for step in data["open_room"]["room_steps"])
    assert any("/rooms/<room_id>/run" in step for step in data["open_room"]["room_steps"])

    stash = _stash(scenarios, scenario_id)
    assert stash["schema"] == 1
    assert stash["world"]["id"] == "heroes-to-yuanshan"
    assert stash["world"]["version"] == "0.1.6"
    # heroes 4 场景全部进 stash：282px/格的「宅邸（二层）」不再因网格窗口被跳过。
    assert len(stash["scenes"]) == 4
    assert stash["skipped"] == []
    assert any(row["grid"]["size"] == 282 for row in stash["scenes"])

    active_rows = [row for row in stash["scenes"] if row["active"]]
    assert len(active_rows) == 1
    active = active_rows[0]
    # heroes 的 active 场景是「雨中伏击」：10 个棋子、1 个光源、0 段墙
    # （墙都在孤儿院场景），断言与真实数据对齐而不是想当然。
    assert active["tokens"]  # active 场景带棋子摆位
    assert active["lights"]
    assert active["grid"]["size"] == 150
    # 墙断言换到真的有墙的场景（孤儿院 23 段墙）。
    walled = [row for row in stash["scenes"] if row["walls"]]
    assert walled and walled[0]["walls"]
    # 背景图内容寻址复制进剧本 assets/，stash 只引用相对路径不内嵌字节。
    total_bytes = 0
    for row in stash["scenes"]:
        background = row["background"]
        assert background["asset"].startswith("assets/")
        asset = storage / background["asset"]
        assert asset.is_file()
        assert asset.stat().st_size == background["bytes"] == int(background["bytes"])
        assert "bytes" not in background or not isinstance(background.get("data"), (bytes, str))
        assert row.get("image") is None
        total_bytes += asset.stat().st_size
    assert total_bytes == data["stash"]["assets_bytes"]

    # NPC statblock：12 个，fvtt_actor_id 幂等键 + statblock 数值锚点。
    assert len(stash["npcs"]) == 12
    npc_ids = {row["fvtt_actor_id"] for row in stash["npcs"]}
    assert len(npc_ids) == 12 and "" not in npc_ids
    knight = next(row for row in stash["npcs"] if "塞巴斯蒂安" in row["title"])
    assert knight["module"]["fvtt_actor_id"] == knight["fvtt_actor_id"]
    assert knight["module"]["visibility"] == "kp"
    assert knight["module"]["statblock"]["armorClass"] > 0


def test_import_sixian_stashes_six_scenes(tmp_path, plugins):
    client, _rooms, scenarios, _socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(SIXIAN_ZIP))
    scenario_id = int(data["scenario_id"])
    storage = scenarios / f"scenario-{scenario_id}"

    assert data["stash"]["scenes"] == 6 and data["stash"]["skipped"] == 0
    assert data["stash"]["npcs"] == 14
    stash = _stash(scenarios, scenario_id)
    assert len(stash["scenes"]) == 6
    assert len([row for row in stash["scenes"] if row["active"]]) == 1
    # sixian 场景无内嵌棋子（两包形态不同），墙体如实进 stash。
    assert all(not row["tokens"] for row in stash["scenes"])
    assert sum(len(row["walls"]) for row in stash["scenes"]) > 0
    for row in stash["scenes"]:
        assert (storage / row["background"]["asset"]).is_file()


def test_import_upgrade_refreshes_stash_in_place(tmp_path, plugins):
    """幂等升版同步刷新 stash：同 world id 重导不产生重复剧本或资产增长。"""
    client, _rooms, scenarios, _socket = _make_app(tmp_path)
    first = _import_scenario(client, _require_zip(HEROES_ZIP))
    scenario_id = int(first["scenario_id"])
    assets_before = {
        path.name for path in (scenarios / f"scenario-{scenario_id}" / "assets").iterdir()
    }

    second = _import_scenario(client, HEROES_ZIP)
    assert second["scenario_id"] == scenario_id and second["created"] is False
    stash = _stash(scenarios, scenario_id)
    assert len(stash["scenes"]) == 4 and len(stash["npcs"]) == 12
    # 内容寻址：同图重导资产集合零增长。
    assets_after = {
        path.name for path in (scenarios / f"scenario-{scenario_id}" / "assets").iterdir()
    }
    assert assets_after == assets_before


# -- auto-provision 端到端 ---------------------------------------------------


def test_auto_provision_hydrates_room_end_to_end(tmp_path, plugins):
    client, rooms, scenarios, socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(HEROES_ZIP))
    scenario_id = int(data["scenario_id"])
    _write_room(rooms, "room1", scenario_id=scenario_id, scenario_version="0.1.6")
    socket.events.clear()

    rv = _provision(client)
    assert rv.status_code == 200, rv.get_data(as_text=True)
    payload = rv.get_json()["data"]

    # 房间出现全部场景，FVTT active 场景被激活。
    assert payload["status"] == "provisioned"
    assert len(payload["scenes"]) == 4
    assert payload["scene_failures"] == []
    active_id = payload["active_scene_id"]
    assert active_id == next(row["scene_id"] for row in payload["scenes"] if row["active"])
    room_dir = rooms / "room1"
    assert veil_map.scene_store.active_scene_id(room_dir) == active_id
    active_state = veil_map.scene_store.load_scene(room_dir, active_id)
    assert active_state is not None
    active_summary = next(row for row in payload["scenes"] if row["active"])
    assert len(active_state["tokens"]) == active_summary["tokens"] > 0
    # active 场景（雨中伏击）本身 0 段墙——墙在孤儿院场景，只对账计数相等。
    assert len(active_state["walls"]) == active_summary["walls"]

    # stash 逐场景与 veil-map 落库对账（全部 4 个场景都在房间场景库，含 282px 大格）。
    on_disk = {
        path.name
        for path in (room_dir / "plugins" / "veil-map" / "scenes").iterdir()
        if path.is_dir()
    }
    assert {row["scene_id"] for row in payload["scenes"]} <= on_disk

    # NPC 模块幂等并入房间绑定剧本 + 升版重钉（0.1.6 → 0.1.7）。
    assert payload["npcs"] == {"merged": 12, "failed": False}
    assert payload["scenario"]["scenario_version"] == "0.1.7"
    record = _record(scenarios, scenario_id)
    npc_modules = [m for m in record["modules"] if m.get("fvtt_actor_id")]
    assert len(npc_modules) == 12
    assert _room_info(rooms)["scenario_version"] == "0.1.7"

    # 装配标记落盘（防重复）。
    marker = _marker(rooms)
    assert marker["schema"] == 1
    assert str(marker["scenario_id"]) == str(scenario_id)
    assert marker["scenes"] == 4 and marker["npcs"] == 12
    assert marker["npc_failed"] is False
    assert marker["active_scene_id"] == active_id

    # 广播让所有在场端自动出现地图（NPC 并入还会带剧本事件；迷雾开启时
    # veil-map 走 user:<id> 逐成员频道，两种频道都算合法投递）。
    map_events = [event for event in socket.events if event["event"] == "room_map_changed"]
    assert map_events
    assert all(
        event["room"] == "room1" or str(event["room"]).startswith("user:")
        for event in map_events
    )


def test_auto_provision_second_call_skips_with_zero_writes(tmp_path, plugins):
    client, rooms, scenarios, _socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(HEROES_ZIP))
    scenario_id = int(data["scenario_id"])
    _write_room(rooms, "room1", scenario_id=scenario_id, scenario_version="0.1.6")
    first = _provision(client)
    assert first.status_code == 200
    active_id = first.get_json()["data"]["active_scene_id"]
    revision_before = veil_map.scene_store.load_scene(rooms / "room1", active_id)["revision"]
    modules_before = len(_record(scenarios, scenario_id)["modules"])
    marker_before = _marker(rooms)

    second = _provision(client)
    assert second.status_code == 200
    payload = second.get_json()["data"]
    assert payload["status"] == "skipped"
    assert payload["reason"] == "already_provisioned"

    # 零写入：场景代际、NPC 模块、剧本版本与标记全部不动。
    assert veil_map.scene_store.load_scene(rooms / "room1", active_id)["revision"] == revision_before
    assert len(_record(scenarios, scenario_id)["modules"]) == modules_before
    assert _room_info(rooms)["scenario_version"] == "0.1.7"
    assert _marker(rooms) == marker_before


def test_auto_provision_skips_room_with_existing_scenes(tmp_path, plugins):
    """房间已有场景（如 DM 手动导过地图）→ 跳过，绝不抢现有地图。"""
    client, rooms, scenarios, _socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(HEROES_ZIP))
    scenario_id = int(data["scenario_id"])
    _write_room(rooms, "room1", scenario_id=scenario_id, scenario_version="0.1.6")

    service = plugin_runtime.get_service("veil-map", "map_service")
    info = _room_info(rooms)
    # veil-map 的 import_scene 需要 Flask 应用上下文（媒体校验走 current_app）。
    with client.application.app_context():
        outcome = service.import_scene(
            rooms / "room1",
            info,
            {
                "scene_id": "manual-scene",
                "title": "手动地图",
                "grid": {"size": 64, "origin_x": 0, "origin_y": 0},
                "image": {"bytes": _png_bytes(), "name": "manual.png"},
                "walls": [],
                "lights": [],
                "tokens": [],
                "ambient": {},
            },
            activate=True,
        )
    assert outcome["ok"]

    rv = _provision(client)
    assert rv.status_code == 200
    payload = rv.get_json()["data"]
    assert payload["reason"] == "room_has_scenes"
    # 零写入：没有装配标记，剧本 NPC 模块也没并入。
    assert not (rooms / "room1" / "plugins" / "fvtt-world-import").exists()
    assert not [m for m in _record(scenarios, scenario_id)["modules"] if m.get("fvtt_actor_id")]


def test_auto_provision_skips_plain_scenario_without_stash(tmp_path, plugins):
    """非世界包剧本 → 可读「非世界包剧本」跳过（不是报错）。"""
    client, rooms, scenarios, _socket = _make_app(tmp_path)
    _write_plain_scenario(scenarios, 999)
    _write_room(rooms, "room1", scenario_id=999, scenario_version="1.0.0")

    rv = _provision(client)
    assert rv.status_code == 200
    payload = rv.get_json()["data"]
    assert payload["status"] == "skipped"
    assert payload["reason"] == "scenario_not_world"
    assert "不是世界包导入的剧本" in payload["message"]
    assert not (rooms / "room1" / "plugins" / "fvtt-world-import").exists()


def test_auto_provision_skips_room_without_scenario_binding(tmp_path, plugins):
    client, rooms, _scenarios, _socket = _make_app(tmp_path)
    _write_room(rooms, "room1", scenario_id=None)

    rv = _provision(client)
    assert rv.status_code == 200
    payload = rv.get_json()["data"]
    assert payload["reason"] == "scenario_missing"


def test_auto_provision_denies_player_identity(tmp_path, plugins):
    client, rooms, _scenarios, _socket = _make_app(tmp_path)
    _write_room(rooms, "room1", scenario_id=999, scenario_version="1.0.0")

    rv = _provision(client, user_id=2, username="bob")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.manager_only"


def test_auto_provision_rejects_non_dnd5e_room(tmp_path, plugins):
    client, rooms, scenarios, _socket = _make_app(tmp_path)
    data = _import_scenario(client, _require_zip(HEROES_ZIP))
    _write_room(
        rooms, "room1", ruleset_id="coc7", scenario_id=int(data["scenario_id"])
    )

    rv = _provision(client)
    assert rv.status_code == 403
    body = rv.get_json()
    assert body["error"] == "fvtt.ruleset_unsupported"
    assert "只有 dnd5e 房间" in body["message"]
