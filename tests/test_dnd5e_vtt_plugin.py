"""dnd5e-vtt 插件（M2 第一刀）：装载、面板数据端点与观察者裁剪。

覆盖：

- 插件装载：真实插件根加载成功（依赖 dnd5e），``room.rail.right`` 槽与
  前端资产声明齐全；单独的临时插件根里**只有 dnd5e 没有 veil-map** 时同样
  装载成功——veil-map 是可选依赖，缺失不报错；
- ``GET /api/plugins/dnd5e-vtt/panels?room=…``：
  - DM（房主/房管）看到六面板（FVTT 顺序）且角色面板带 NPC/怪物分组；
  - 玩家只有五个面板——**场景 tab 整块不下发**，角色面板没有 ``npc`` 键，
    日志为公开事件过滤版；
  - 战斗面板先攻快照来自房间战斗状态（state.json），未开战是空态；
  - 登录 / 房间成员 / dnd5e 规则集三层门闸；
- 面板端点在**未挂载 veil-map 蓝图**的裸 Flask 应用上照常工作（降级不报错）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
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


dnd5e_vtt = _load_module(VTT_ENTRY, "dnd5e_vtt_entry_test")


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


def _write_room(rooms: Path, room_id: str, *, ruleset_id: str = "dnd5e", scenario_id: int | None = 101) -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "战桌测试房",
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
                "character_card": {
                    "name": "艾莉丝",
                    "avatar": "/data/characters/c1/avatar.png",
                    "level": 3,
                    "class": "法师",
                    "race": "高等精灵",
                },
            },
            {
                "user_id": "2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "鲍勃", "level": 2, "class": "战士"},
            },
        ],
    }
    if scenario_id is not None:
        info["scenario_id"] = str(scenario_id)
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_scenario(scenarios: Path, scenario_id: int = 101) -> None:
    sdir = scenarios / f"scenario-{scenario_id}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(
        json.dumps(
            {
                "id": scenario_id,
                "title": "战桌测试剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "modules": [
                    {
                        "id": "npc-goblin",
                        "module_type": "npc",
                        "title": "地精哨兵",
                        "summary": "CR 1/2，短矛与盾。",
                        "content": "AC 15 HP 6",
                    },
                    {
                        "id": "mod-scene-1",
                        "module_type": "scene",
                        "title": "村口",
                        "content": "普通的场景模块，不应进 NPC 分组。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_client(tmp_path: Path, *, ruleset_id: str = "dnd5e", scenario: bool = True):
    rooms = tmp_path / "rooms"
    _write_room(rooms, "room1", ruleset_id=ruleset_id, scenario_id=101 if scenario else None)
    scenarios = tmp_path / "scenarios"
    if scenario:
        _write_scenario(scenarios, 101)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=rooms, CONFIG_DIR=str(config), SCENARIOS_DIR=scenarios)
    # 只注册本插件蓝图：veil-map 缺席本身就是本刀的降级验收条件。
    app.register_blueprint(dnd5e_vtt.bp)
    return app.test_client(), rooms


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=user_id, role="USER")


def _room_state(rooms: Path) -> dict[str, Any]:
    return json.loads((rooms / "room1" / "state.json").read_text(encoding="utf-8"))


def _panels(client, room_id: str = "room1"):
    response = client.get(f"/api/plugins/dnd5e-vtt/panels?room={room_id}")
    return response


# ---------------------------------------------------------------- 插件装载


def test_plugin_loads_and_declares_rail_slot(plugins):
    record = plugins.get("dnd5e-vtt")
    assert record is not None
    assert record.state == "loaded", getattr(record, "reason", "")
    capabilities = record.manifest.capabilities or {}
    slots = ((capabilities.get("ui") or {}).get("slots")) or []
    rail = [slot for slot in slots if slot.get("slot") == "room.rail.right"]
    assert len(rail) == 1
    assert rail[0]["mount"] == "mountRail"
    assert rail[0]["rulesets"] == ["dnd5e"]
    assets = {asset["path"] for asset in capabilities.get("frontend_assets") or []}
    assert {"ui/dnd5e-vtt.js", "ui/dnd5e-vtt.css"} <= assets


def test_plugin_loads_without_veil_map(tmp_path):
    """veil-map 可选：只有 dnd5e（纯清单桩）时同样装载成功。"""
    root = tmp_path / "plugins"
    shutil.copytree(VTT_DIR, root / "dnd5e-vtt")
    (root / "dnd5e").mkdir()
    (root / "dnd5e" / "plugin.json").write_text(
        json.dumps(
            {"schema_version": 1, "id": "dnd5e", "name": "dnd5e", "version": "0.1.0", "api_version": 1},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "enabled.json").write_text(
        json.dumps({"version": 1, "enabled": ["dnd5e", "dnd5e-vtt"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = PluginRegistry(root).load()
    record = registry.get("dnd5e-vtt")
    assert record is not None and record.state == "loaded", getattr(record, "reason", "")


# ---------------------------------------------------------------- 面板端点：DM


def test_dm_sees_six_panels_with_scenes_and_npc(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["observer"] == "dm"
    # FVTT 顺序：聊天 → 战斗 → 场景 → 角色 → 物品 → 日志。
    assert [panel["id"] for panel in data["panels"]] == [
        "chat", "combat", "scenes", "characters", "items", "journal",
    ]
    scenes = data["panels"][2]
    assert scenes["items"] == []  # 场景列表占位，不造假数据
    characters = data["panels"][3]
    assert [player["name"] for player in characters["players"]] == ["艾莉丝", "鲍勃"]
    assert characters["players"][0]["class"] == "法师"
    npc = characters["npc"]
    assert [item["title"] for item in npc["items"]] == ["地精哨兵"]


def test_combat_panel_reflects_room_state(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    (rooms / "room1" / "state.json").write_text(
        json.dumps(
            {
                "combat": {
                    "active": True,
                    "round": 2,
                    "turn": 1,
                    "order": ["地精哨兵", "鲍勃"],
                    "combatants": {},
                },
                "event_log": [
                    {"id": "e1", "kind": "player_decision", "content": "我推开石门。", "created_at": "2026-11-21 10:00:00"},
                    {"id": "e2", "kind": "kp_note", "content": "KP 私注：门后有埋伏。", "created_at": "2026-11-21 10:01:00"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    combat = data["panels"][1]
    assert combat["state"]["active"] is True
    assert combat["state"]["round"] == 2
    assert combat["state"]["order"] == ["地精哨兵", "鲍勃"]
    assert combat["state"]["current"] == "鲍勃"
    assert combat["source"] == "/api/rooms/room1/combat"
    journal = data["panels"][5]
    assert [entry["kind"] for entry in journal["entries"]] == ["player_decision", "kp_note"]


# ---------------------------------------------------------------- 面板端点：玩家裁剪


def test_player_never_receives_scenes_or_npc(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "2")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["observer"] == "player"
    ids = [panel["id"] for panel in data["panels"]]
    # 场景 tab 整块不下发（服务端裁剪，非前端隐藏）。
    assert ids == ["chat", "combat", "characters", "items", "journal"]
    characters = data["panels"][2]
    assert "npc" not in characters  # NPC/怪物分组不下发
    assert [player["name"] for player in characters["players"]] == ["艾莉丝", "鲍勃"]
    # 原始 JSON 里也不允许出现 NPC 标题（防止藏在别处）。
    assert "地精哨兵" not in response.get_data(as_text=True)


def test_player_journal_is_filtered(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    (rooms / "room1" / "state.json").write_text(
        json.dumps(
            {
                "event_log": [
                    {"id": "e1", "kind": "player_decision", "content": "我推开石门。", "created_at": "2026-11-21 10:00:00"},
                    {"id": "e2", "kind": "kp_note", "content": "KP 私注：门后有埋伏。", "created_at": "2026-11-21 10:01:00"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _login(client, "2")
    data = _panels(client).get_json()["data"]
    journal = data["panels"][4]
    assert [entry["kind"] for entry in journal["entries"]] == ["player_decision"]
    assert "KP 私注" not in json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------- 门闸与降级


def test_panels_require_login_membership_and_ruleset(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path, ruleset_id="dnd5e")
    # 未登录 → 401
    assert _panels(client).status_code == 401
    # 非房间成员 → 403
    _login(client, "9")
    assert _panels(client).status_code == 403
    # 参数缺失 → 400；未知房间 → 404
    assert client.get("/api/plugins/dnd5e-vtt/panels").status_code == 400
    assert client.get("/api/plugins/dnd5e-vtt/panels?room=nope").status_code == 404
    # coc7 房间 → 规则集门闸拒绝
    coc_client, _coc_rooms = _make_client(tmp_path, ruleset_id="coc7", scenario=False)
    _login(coc_client, "1")
    denied = _panels(coc_client)
    assert denied.status_code == 400
    assert "规则集" in str(denied.get_json())


def test_room_admin_counts_as_dm(tmp_path, plugins):
    """房管（room_role=admin）与房主同权：能看场景面板与 NPC 分组。"""
    client, rooms = _make_client(tmp_path)
    info = json.loads((rooms / "room1" / "info.json").read_text(encoding="utf-8"))
    info["members"].append(
        {"user_id": "7", "username": "carol", "room_role": "admin", "status": "active", "is_active": True}
    )
    (rooms / "room1" / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    _login(client, "7")
    data = _panels(client).get_json()["data"]
    assert data["observer"] == "dm"
    assert "scenes" in [panel["id"] for panel in data["panels"]]
    assert "npc" in data["panels"][3]


def test_endpoint_works_without_veil_map_blueprint(tmp_path, plugins):
    """裸 Flask 只注册 dnd5e-vtt 蓝图：无 veil-map 时面板端点照常返回。"""
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["observer"] == "dm"
    assert [panel["id"] for panel in data["panels"]][0] == "chat"


def test_panels_survive_room_without_scenario_or_state(tmp_path, plugins):
    """无绑定剧本、无 state.json：全部面板空态返回，不抛错、不造假数据。"""
    client, _rooms = _make_client(tmp_path, scenario=False)
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    combat = data["panels"][1]
    assert combat["state"] == {"active": False, "round": None, "turn": None, "order": [], "current": None}
    characters = data["panels"][3]
    assert characters["npc"]["items"] == []
    journal = data["panels"][5]
    assert journal["entries"] == []
