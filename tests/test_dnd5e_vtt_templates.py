"""dnd5e-vtt 插件（M3 第一段）：测量模板端点 + 棋子操作卡数据。

覆盖：

- 模板写权限：DM（房主/房管）可 place/move/delete/clear；**玩家 POST 一律
  403（rejected）**，存储文件不产生；
- ``request_id`` 幂等：同一 request_id 重复提交返回成功且 ``duplicate=true``，
  模板数与 revision 不再变化；
- ``revision`` 乐观并发：过期 revision 返回 409 conflict 并附最新模板清单；
- 模板读取差异：玩家 GET 只读（``can_manage=false``）且能看到 DM 放的模板
  （玩家端只读展示）；DM GET 带 ``can_manage=true``；
- 清空门控：clear 仅 DM；玩家清空 403；
- 棋子操作卡：DM 从房间战斗状态拉攻击/法术清单（命中与伤害文本）；剧本
  NPC 模块 statblock 作为无战斗时的数据源；**玩家响应不含 ``actions`` 键**
  （服务端不下发，DOM 不渲染），且只能查自己绑定的棋子（他人 403）；
- 玩家 /panels 与模板读写的观察者差异一并断言（场景 tab 仍不下发）。
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


dnd5e_vtt = _load_module(VTT_ENTRY, "dnd5e_vtt_entry_templates_test")


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


def _write_room(rooms: Path, room_id: str = "room1") -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "模板测试房",
        "room_code": room_id.upper(),
        "creator_id": "1",
        "ruleset_id": "dnd5e",
        "scenario_id": "101",
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
                    "hp": {"current": 10, "max": 16},
                    "armorClass": 14,
                    "proficiencies": {"weapons": ["匕首"], "languages": ["通用语", "精灵语"]},
                    "features": ["奥术恢复"],
                    "equipment": [{"name": "法师长袍", "quantity": 1}],
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
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_scenario(scenarios: Path, scenario_id: int = 101) -> None:
    sdir = scenarios / f"scenario-{scenario_id}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(
        json.dumps(
            {
                "id": scenario_id,
                "title": "模板测试剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "modules": [
                    {
                        "id": "npc-goblin",
                        "module_type": "npc",
                        "title": "地精哨兵",
                        "summary": "CR 1/2，短矛与盾。",
                        "content": "AC 15 HP 6",
                        "statblock": {
                            "name": "地精哨兵",
                            "armorClass": 15,
                            "hp": {"current": 6, "max": 6},
                            "maxHp": 6,
                            "cr": "1/2",
                            "senses": {"darkvision": 60},
                            "attacks": [
                                {
                                    "name": "短矛",
                                    "kind": "melee",
                                    "attack_bonus": 4,
                                    "damage_dice": "1d6",
                                    "damage_bonus": 2,
                                    "damage_type": "穿刺",
                                    "reach_ft": 5,
                                }
                            ],
                            "spellcasting": {"spellSaveDC": 13},
                            "spells_known": ["火球术"],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_state(rooms: Path, room_id: str = "room1", *, with_combat: bool = False) -> None:
    state: dict[str, Any] = {}
    if with_combat:
        state["combat"] = {
            "active": True,
            "round": 1,
            "turn": 0,
            "order": ["地精哨兵", "艾莉丝"],
            "combatants": {
                "地精哨兵": {
                    "name": "地精哨兵",
                    "hp": 5,
                    "max_hp": 6,
                    "armorClass": 15,
                    "attacks": [
                        {
                            "name": "短矛",
                            "kind": "melee",
                            "attack_bonus": 4,
                            "damage_dice": "1d6",
                            "damage_bonus": 2,
                            "damage_type": "穿刺",
                        }
                    ],
                    "spells_known": ["火球术"],
                    "spellcasting": {"spellSaveDC": 13},
                }
            },
        }
    (rooms / room_id / "state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def _make_client(tmp_path: Path, *, with_combat: bool = False):
    rooms = tmp_path / "rooms"
    _write_room(rooms)
    _write_state(rooms, with_combat=with_combat)
    scenarios = tmp_path / "scenarios"
    _write_scenario(scenarios)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=rooms, CONFIG_DIR=str(config), SCENARIOS_DIR=scenarios)
    app.register_blueprint(dnd5e_vtt.bp)
    return app.test_client(), rooms


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=user_id, role="USER")


def _place(client, template: dict[str, Any], *, request_id: str = "req-1", revision: int = 0):
    return client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={"request_id": request_id, "revision": revision, "action": "place", "template": template},
    )


def _templates_file(rooms: Path, room_id: str = "room1") -> Path | None:
    path = rooms / room_id / "plugins" / "dnd5e-vtt" / "templates.json"
    return path if path.exists() else None


CIRCLE = {"kind": "circle", "cell_x": 3, "cell_y": 4, "size_cells": 4}
CONE = {
    "kind": "cone",
    "cell_x": 0,
    "cell_y": 0,
    "size_cells": 6,
    "direction": {"dx": 1, "dy": 0},
    "angle": 53.13,
}
LINE = {"kind": "line", "cell_x": 0, "cell_y": 0, "size_cells": 6, "direction": {"dx": 3, "dy": 4}}
RECT = {"kind": "rect", "cell_x": 2, "cell_y": 2, "w_cells": 3, "h_cells": 2}


# ---------------------------------------------------------------- 权限与幂等


def test_dm_places_all_four_template_kinds(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    revision = 0
    for index, template in enumerate([CIRCLE, CONE, LINE, RECT]):
        response = _place(client, template, request_id=f"req-{index}", revision=revision)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True
        assert body["status"] == "success"
        revision = body["revision"]
        assert revision == index + 1
    stored = json.loads(_templates_file(rooms).read_text(encoding="utf-8"))
    kinds = [item["kind"] for item in stored["templates"]]
    assert kinds == ["circle", "cone", "line", "rect"]
    # 归一化：方向单位化、锥默认角保留、颜色回退。
    cone = stored["templates"][1]
    assert cone["direction"] == {"dx": 1.0, "dy": 0.0}
    assert cone["angle"] == 53.13
    line = stored["templates"][2]
    assert line["direction"] == {"dx": 0.6, "dy": 0.8}
    assert stored["templates"][0]["color"] == "#e06666"
    # 幂等记录落盘，供重复请求短路。
    assert [item["request_id"] for item in stored["recent_requests"]] == [
        "req-0", "req-1", "req-2", "req-3",
    ]


def test_player_cannot_write_templates(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "2")  # bob：普通成员
    response = _place(client, CIRCLE)
    assert response.status_code == 403
    body = response.get_json()
    assert body["status"] == "rejected"
    assert body["code"] == "dnd5e_vtt.templates_dm_only"
    assert _templates_file(rooms) is None  # 玩家请求不产生存储


def test_request_id_is_idempotent(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    first = _place(client, CIRCLE, request_id="same-req", revision=0)
    assert first.status_code == 200
    revision = first.get_json()["revision"]
    second = _place(client, CIRCLE, request_id="same-req", revision=revision)
    assert second.status_code == 200
    body = second.get_json()
    assert body["duplicate"] is True
    assert body["code"] == "dnd5e_vtt.duplicate_request"
    assert body["revision"] == revision  # 无副作用：revision 不再推进
    stored = json.loads(_templates_file(rooms).read_text(encoding="utf-8"))
    assert len(stored["templates"]) == 1


def test_revision_conflict_returns_latest_templates(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    _place(client, CIRCLE, request_id="req-a", revision=0)
    stale = _place(client, LINE, request_id="req-b", revision=0)  # 过期 revision
    assert stale.status_code == 409
    body = stale.get_json()
    assert body["status"] == "conflict"
    assert body["code"] == "dnd5e_vtt.revision_conflict"
    kinds = [item["kind"] for item in body["data"]["templates"]]
    assert kinds == ["circle"]  # 冲突响应附最新清单，前端可直接刷新


def test_write_envelope_validation(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    # 缺 request_id / 缺 revision / 未知 action / 非法模板，全部 400 invalid。
    base = "/api/plugins/dnd5e-vtt/rooms/room1/templates"
    bad = client.post(base, json={"revision": 0, "action": "place", "template": CIRCLE})
    assert bad.status_code == 400 and bad.get_json()["status"] == "invalid"
    bad = client.post(base, json={"request_id": "r1", "action": "place", "template": CIRCLE})
    assert bad.status_code == 400 and bad.get_json()["status"] == "invalid"
    bad = client.post(
        base,
        json={"request_id": "r1", "revision": 0, "action": "rotate", "template": CIRCLE},
    )
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.invalid_action"
    bad = client.post(
        base,
        json={"request_id": "r1", "revision": 0, "action": "place", "template": {"kind": "hex"}},
    )
    assert bad.status_code == 400 and bad.get_json()["code"] == "dnd5e_vtt.invalid_template"
    bad = client.post(
        base,
        json={
            "request_id": "r1",
            "revision": 0,
            "action": "place",
            "template": {"kind": "cone", "cell_x": 0, "cell_y": 0, "size_cells": 6},
        },
    )
    assert bad.status_code == 400  # 锥缺方向 → invalid
    assert _templates_file(rooms) is None


# ---------------------------------------------------------------- 读差异与清空门控


def test_templates_read_split_between_dm_and_player(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    _place(client, CIRCLE, request_id="req-1", revision=0)
    # DM 读取：can_manage=true。
    dm_view = client.get("/api/plugins/dnd5e-vtt/rooms/room1/templates")
    assert dm_view.status_code == 200
    dm_data = dm_view.get_json()["data"]
    assert dm_data["can_manage"] is True
    assert dm_data["observer"] == "dm"
    assert [item["kind"] for item in dm_data["templates"]] == ["circle"]
    # 玩家读取：只读展示（can_manage=false），模板内容可见。
    _login(client, "2")
    player_view = client.get("/api/plugins/dnd5e-vtt/rooms/room1/templates")
    assert player_view.status_code == 200
    player_data = player_view.get_json()["data"]
    assert player_data["can_manage"] is False
    assert player_data["observer"] == "player"
    assert [item["kind"] for item in player_data["templates"]] == ["circle"]


def test_clear_is_dm_only(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    _login(client, "1")
    _place(client, CIRCLE, request_id="req-1", revision=0)
    # 玩家清空 → 403 rejected，模板原样保留。
    _login(client, "2")
    denied = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={"request_id": "req-clear-player", "revision": 1, "action": "clear"},
    )
    assert denied.status_code == 403
    assert denied.get_json()["status"] == "rejected"
    # DM 清空 → 成功，存储清空但 revision 继续推进。
    _login(client, "1")
    cleared = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={"request_id": "req-clear-dm", "revision": 1, "action": "clear"},
    )
    assert cleared.status_code == 200
    body = cleared.get_json()
    assert body["data"]["templates"] == []
    assert body["revision"] == 2
    stored = json.loads(_templates_file(rooms).read_text(encoding="utf-8"))
    assert stored["templates"] == []


def test_move_and_delete_templates(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    _place(client, CONE, request_id="req-1", revision=0)
    revision = 1
    moved = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={
            "request_id": "req-2",
            "revision": revision,
            "action": "move",
            "id": "tpl-x",
            "template": CONE,
        },
    )
    assert moved.status_code == 404  # 目标不存在 → invalid（学 veil-map 404 语义）
    assert moved.get_json()["code"] == "dnd5e_vtt.template_not_found"
    # 玩家请求也拿不到 move 权限（403 先于 404）。
    _login(client, "2")
    denied = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={"request_id": "req-2b", "revision": revision, "action": "move", "id": "missing"},
    )
    assert denied.status_code == 403
    _login(client, "1")
    # 用服务端分配的 id 做 move + delete（404 尝试不推进 revision，仍为 1）。
    template_id = client.get("/api/plugins/dnd5e-vtt/rooms/room1/templates").get_json()["data"][
        "templates"
    ][0]["id"]
    move_body = dict(CONE, cell_x=7, cell_y=8, direction={"dx": 0, "dy": -1})
    moved = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={
            "request_id": "req-3",
            "revision": revision,
            "action": "move",
            "id": template_id,
            "template": move_body,
        },
    )
    assert moved.status_code == 200
    moved_template = moved.get_json()["data"]["templates"][0]
    assert (moved_template["cell_x"], moved_template["cell_y"]) == (7, 8)
    assert moved_template["direction"] == {"dx": 0.0, "dy": -1.0}
    assert moved_template["id"] == template_id  # move 不换 id
    deleted = client.post(
        "/api/plugins/dnd5e-vtt/rooms/room1/templates",
        json={"request_id": "req-4", "revision": revision + 1, "action": "delete", "id": template_id},
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["data"]["templates"] == []


def test_template_gates_mirror_panels(tmp_path, plugins):
    """登录 / 成员 / 规则集三层门闸与 /panels 同口径。"""
    client, _rooms = _make_client(tmp_path)
    path = "/api/plugins/dnd5e-vtt/rooms/room1/templates"
    assert client.get(path).status_code == 401  # 未登录
    _login(client, "9")
    assert client.get(path).status_code == 403  # 非成员
    assert client.get("/api/plugins/dnd5e-vtt/rooms/nope/templates").status_code == 404


def test_player_panels_still_scoped_with_templates_enabled(tmp_path, plugins):
    """模板能力开启后，玩家 /panels 差异保持不变（场景 tab 不下发 + 无 npc 键）。"""
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    _place(client, CIRCLE, request_id="req-1", revision=0)
    _login(client, "2")
    panels = client.get("/api/plugins/dnd5e-vtt/panels?room=room1").get_json()["data"]
    assert panels["observer"] == "player"
    ids = [panel["id"] for panel in panels["panels"]]
    assert "scenes" not in ids
    assert "npc" not in panels["panels"][2]
    # 模板端点读得到（只读），写被拒：观察者差异贯穿两套接口。
    view = client.get("/api/plugins/dnd5e-vtt/rooms/room1/templates").get_json()["data"]
    assert view["can_manage"] is False
    assert [item["kind"] for item in view["templates"]] == ["circle"]
    assert _place(client, LINE, request_id="req-x", revision=1).status_code == 403


# ---------------------------------------------------------------- 棋子操作卡


def test_dm_token_actions_from_combat_state(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path, with_combat=True)
    _login(client, "1")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions"
        "?name=%E5%9C%B0%E7%B2%BE%E5%93%A8%E5%85%B5&combatant=npc%3Agoblin"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["matched"] is True
    assert data["card"]["name"] == "地精哨兵"
    assert data["card"]["hp"] == 5
    assert data["card"]["max_hp"] == 6
    assert data["card"]["ac"] == 15
    attacks = data["actions"]["attacks"]
    assert attacks[0]["name"] == "短矛"
    assert attacks[0]["hit"] == "+4"
    assert attacks[0]["damage"] == "1d6+2 穿刺"
    spells = data["actions"]["spells"]
    assert [item["name"] for item in spells] == ["火球术"]
    assert "DC 13" in spells[0]["detail"]


def test_dm_token_actions_from_npc_statblock_without_combat(tmp_path, plugins):
    """无战斗时回落到剧本 NPC 模块 statblock（世界包导入的结构化产物）。"""
    client, _rooms = _make_client(tmp_path, with_combat=False)
    _login(client, "1")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions?name=%E5%9C%B0%E7%B2%BE%E5%93%A8%E5%85%B5"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["matched"] is True
    assert data["card"]["kind"] == "npc"
    assert data["card"]["cr"] == "1/2"
    assert data["card"]["hp"] == 6
    attacks = data["actions"]["attacks"]
    assert attacks[0]["hit"] == "+4"
    assert attacks[0]["damage"] == "1d6+2 穿刺"
    assert attacks[0]["detail"] == "触及 5 尺"
    abilities = data["actions"]["abilities"]
    assert any(item["name"] == "darkvision" for item in abilities)
    features = data["actions"]["features"]
    assert features == []  # NPC 特性未结构化 → 空态由前端提示


def test_dm_token_actions_for_player_card(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path, with_combat=False)
    _login(client, "1")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions?name=%E8%89%BE%E8%8E%89%E4%B8%9D&owner=1"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["card"]["kind"] == "player"
    assert data["card"]["avatar"] == "/data/characters/c1/avatar.png"
    assert data["card"]["ac"] == 14
    assert [item["name"] for item in data["actions"]["features"]] == ["奥术恢复"]
    assert data["actions"]["items"][0]["name"] == "法师长袍"
    abilities = {item["name"]: item["detail"] for item in data["actions"]["abilities"]}
    assert abilities["武器熟练"] == "匕首"
    assert abilities["语言"] == "通用语、精灵语"


def test_player_token_actions_own_card_without_actions_key(tmp_path, plugins):
    """玩家查自己的棋子：卡可见，但响应不含 actions（服务端裁剪，DOM 不渲染）。"""
    client, _rooms = _make_client(tmp_path, with_combat=False)
    _login(client, "2")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions?name=%E9%B2%8D%E5%8B%83&owner=2"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["card"]["kind"] == "player"
    assert data["card"]["name"] == "鲍勃"
    assert "actions" not in data
    assert "attacks" not in response.get_data(as_text=True)  # 原始 JSON 也不携带


def test_player_token_actions_other_token_forbidden(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path, with_combat=False)
    _login(client, "2")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions?name=%E5%9C%B0%E7%B2%BE%E5%93%A8%E5%85%B5&owner=1"
    )
    assert response.status_code == 403
    assert "自己" in str(response.get_json()["message"])


def test_dm_token_actions_unmatched_token_is_empty_state(tmp_path, plugins):
    """无匹配数据源：matched=false + 空清单 + 可读提示（前端空态）。"""
    client, _rooms = _make_client(tmp_path, with_combat=False)
    _login(client, "1")
    response = client.get(
        "/api/plugins/dnd5e-vtt/rooms/room1/token-actions?name=%E6%9C%AA%E7%9F%A5%E6%A3%8B%E5%AD%90"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["matched"] is False
    assert data["card"]["kind"] == "token"
    assert data["card"]["name"] == "未知棋子"
    actions = data["actions"]
    assert actions["attacks"] == []
    assert "开战" in actions["hint"]


def test_token_actions_requires_identifier(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    response = client.get("/api/plugins/dnd5e-vtt/rooms/room1/token-actions")
    assert response.status_code == 400
