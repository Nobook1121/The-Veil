"""dnd5e-vtt：角色速览（quick_stats）——四盒 + HP + 资源池。

覆盖：

- 玩家角色条目带 ``quick_stats``：AC / 命中 / 法术豁免 DC / 被动感知四个
  锚点与 dnd5e 插件派生值一致（不重算公式，直接对派生结果）；
- 命中加值两条路：卡上有可用武器走武器攻击加值（来源 ``weapon``），
  纯施法者退回 ``spellAttackBonus``（来源 ``spell``），都没有给 null；
- HP 三源优先级：房间 ``character_state`` 覆盖卡面，并夹紧到上限；
- 资源池：生命骰剩余（优先）+ 法术位合计，``reset`` = 长休；没有就不给；
- 玩家侧不下发 NPC 速览（``npc`` 键整块不存在）；DM 侧 NPC 条目的速览
  来自 statblock 模块，模块数值不全时如实不给该键；
- 无卡成员：``quick_stats`` 全 null + ``resources`` 空数组，端点不报错；
- 端点权限回归（未登录 / 非成员 / 缺参 / 未知房间 / 规则集门闸）。
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
from trpg_server.ruleset import get_plugin, reset_builtin_registry

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


dnd5e_vtt = _load_module(VTT_ENTRY, "dnd5e_vtt_entry_quick_stats_test")


@pytest.fixture()
def plugins():
    """装载真实插件根：dnd5e 规则集与数据包目录随之可用。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture()
def dnd5e():
    return get_plugin("dnd5e")


# ---------------------------------------------------------------- 装置（真实卡）

# 艾莉丝：5 级法师，带匕首（武器路径）+ 已用掉 1 颗生命骰 / 1 个 1 环法术位。
ALICE_CARD: dict[str, Any] = {
    "id": "card-alice",
    "name": "艾莉丝",
    "avatar": "/data/characters/c1/avatar.png",
    "rulesetId": "dnd5e",
    "level": 5,
    "sheet": {
        "level": 5,
        "class": "wizard",
        "race": "高等精灵",
        "abilities": {"str": 8, "dex": 14, "con": 12, "int": 17, "wis": 12, "cha": 10},
        "proficiencies": {"saves": ["int", "wis"], "skills": ["perception", "arcana"], "weapons": ["dagger"]},
        "hp": {"current": 24, "max": 32, "temp": 0},
        "armorClass": 15,
        "speed": 30,
        "hitDice": "5d6",
        "hit_dice": {"die": 6, "max": 5, "used": 1},
        "equipment": [{"id": "dagger", "name": "Dagger", "equipped": True}],
        "spellcasting": {"ability": "int", "slots": {"1": {"max": 4, "used": 1}, "2": {"max": 3, "used": 0}}},
    },
}

# 鲍勃：纯战卡，没有任何装备条目与施法档案 → 命中只能给 null。
BOB_CARD: dict[str, Any] = {
    "id": "card-bob",
    "name": "鲍勃",
    "rulesetId": "dnd5e",
    "level": 2,
    "sheet": {
        "level": 2,
        "class": "fighter",
        "abilities": {"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8},
        "hp": {"current": 20, "max": 20},
        "armorClass": 16,
        "speed": 30,
        "equipment": [],
    },
}

# 黛娜：牧师卡，只有施法没有武器 → 命中退回法术攻击加值。
DANA_CARD: dict[str, Any] = {
    "id": "card-dana",
    "name": "黛娜",
    "rulesetId": "dnd5e",
    "level": 3,
    "sheet": {
        "level": 3,
        "class": "cleric",
        "abilities": {"str": 12, "dex": 10, "con": 14, "int": 10, "wis": 16, "cha": 12},
        "proficiencies": {"skills": ["perception"]},
        "hp": {"current": 21, "max": 21},
        "armorClass": 13,
        "speed": 30,
        "equipment": [],
        "spellcasting": {"ability": "wis"},
    },
}


def _write_room(rooms: Path, room_id: str, *, ruleset_id: str = "dnd5e", scenario_id: int | None = 101) -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "速览测试房",
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
                "character_card": json.loads(json.dumps(ALICE_CARD)),
                # 房间侧当前 HP 才是非战斗时的真值（卡面只提供上限）。
                "character_state": {"current_hp": 19, "max_hp": 32},
            },
            {
                "user_id": "2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": json.loads(json.dumps(BOB_CARD)),
            },
            {
                "user_id": "3",
                "username": "dana",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": json.loads(json.dumps(DANA_CARD)),
            },
            {
                # 未建卡成员：面板如实标记，速览全 null + 空资源池。
                "user_id": "4",
                "username": "carol",
                "room_role": "member",
                "status": "active",
                "is_active": True,
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
                "title": "速览测试剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "modules": [
                    {
                        "id": "npc-bugbear",
                        "module_type": "npc",
                        "title": "熊地精哨兵",
                        "summary": "CR 1，狼牙棒。",
                        "monster_id": "bugbear",
                        "statblock": {"senses": {"passive_perception": 11}},
                    },
                    {
                        # 世界包导入写给 NPC 的分速表与语言（statblock.speed /
                        # statblock.languages）：速览要按玩家侧同口径补出来。
                        "id": "npc-wisp",
                        "module_type": "npc",
                        "title": "洞窟鬼火",
                        "summary": "会飞的鬼火，说通用语与地精语。",
                        "monster_id": "goblin",
                        "statblock": {
                            "speed": {"walk": 30, "fly": 60},
                            "languages": ["Common", "Goblin"],
                        },
                    },
                    {
                        # 模块没有任何可用数值：速览如实缺席（不塞占位值）。
                        "id": "npc-shell",
                        "module_type": "npc",
                        "title": "空壳 NPC",
                        "summary": "只有叙事文本。",
                        "content": "AC 15 HP 6",
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
    app.register_blueprint(dnd5e_vtt.bp)
    return app.test_client(), rooms


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=user_id, role="USER")


def _panels(client, room_id: str = "room1"):
    return client.get(f"/api/plugins/dnd5e-vtt/panels?room={room_id}")


def _characters(data: dict[str, Any]) -> dict[str, Any]:
    for panel in data["panels"]:
        if panel.get("id") == "characters":
            return panel
    raise AssertionError("角色面板缺失")


def _player(data: dict[str, Any], name: str) -> dict[str, Any]:
    for row in _characters(data)["players"]:
        if row["name"] == name:
            return row
    raise AssertionError(f"玩家目录里没有 {name}")


def _npc(data: dict[str, Any], title: str) -> dict[str, Any]:
    for row in _characters(data)["npc"]["items"]:
        if row["title"] == title:
            return row
    raise AssertionError(f"NPC 目录里没有 {title}")


# ---------------------------------------------------------------- 四盒锚点


def test_player_quick_stats_match_plugin_derived_values(tmp_path, plugins, dnd5e):
    """AC / 法术 DC / 被动感知三锚点必须与 dnd5e 插件的派生值完全一致。"""
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    alice = _player(data, "艾莉丝")
    quick = alice["quick_stats"]

    derived = dnd5e.schema.derive(ALICE_CARD)
    assert quick["ac"] == derived["armorClass"] == 15
    assert quick["spell_save_dc"] == derived["spellSaveDC"] == 14
    assert quick["passive_perception"] == derived["passivePerception"] == 14
    # 命中 = 卡上主要武器（匕首，敏捷 +2 + 熟练 +3）的攻击加值。
    assert quick["attack_bonus"] == 5
    assert quick["attack_bonus_source"] == "weapon"
    # 卡面没有落盘派生键时也能读出同一套值（走 plugin.schema.derive 补齐）。
    assert "armorClass" not in ALICE_CARD


def test_quick_stats_hp_prefers_room_character_state(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    quick = _player(_panels(client).get_json()["data"], "艾莉丝")["quick_stats"]
    # character_state.current_hp=19 覆盖卡面 24；上限取 32。
    assert quick["hp"] == {"value": 19, "max": 32, "temp": 0}


def test_hp_is_clamped_and_unknown_when_card_has_no_hp(tmp_path, plugins):
    client, rooms = _make_client(tmp_path)
    room = rooms / "room1"
    info = json.loads((room / "info.json").read_text(encoding="utf-8"))
    # 鲍勃的当前值被改成超过上限：面板必须夹紧，不能显示 99/20。
    info["members"][1]["character_state"] = {"current_hp": 99, "max_hp": 20}
    # 黛娜的卡去掉 HP 字段：速览保留四盒，HP 给 0/0（未知），不编造。
    info["members"][2]["character_card"]["sheet"].pop("hp")
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    assert _player(data, "鲍勃")["quick_stats"]["hp"] == {"value": 20, "max": 20, "temp": 0}
    assert _player(data, "黛娜")["quick_stats"]["hp"] == {"value": 0, "max": 0, "temp": 0}


# ---------------------------------------------------------------- 命中加值两条路


def test_attack_bonus_falls_back_to_spell_attack_for_casters(tmp_path, plugins, dnd5e):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    dana = _player(data, "黛娜")["quick_stats"]
    derived = dnd5e.schema.derive(DANA_CARD)
    # 无武器条目 → 命中退回法术攻击加值，来源标记 spell。
    assert dana["attack_bonus"] == derived["spellAttackBonus"] == 5
    assert dana["attack_bonus_source"] == "spell"
    assert dana["spell_save_dc"] == derived["spellSaveDC"] == 13


def test_attack_bonus_is_null_when_nothing_is_available(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    bob = _player(_panels(client).get_json()["data"], "鲍勃")["quick_stats"]
    # 没有武器、没有施法档案 → 不编造命中加值。
    assert bob["attack_bonus"] is None
    assert bob["attack_bonus_source"] == ""


# ---------------------------------------------------------------- 资源池


def test_resources_hit_dice_and_spell_slots(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    quick = _player(_panels(client).get_json()["data"], "艾莉丝")["quick_stats"]
    resources = quick["resources"]
    assert [item["id"] for item in resources] == ["hit_dice", "spell_slots"]
    hit_dice = resources[0]
    # 卡面 hit_dice 显式值：max 5 / used 1 → 剩余 4，骰型 d6，长休刷新。
    assert hit_dice == {
        "id": "hit_dice",
        "label": "生命骰",
        "value": 4,
        "max": 5,
        "die": 6,
        "reset": "long_rest",
    }
    slots = resources[1]
    # 5 级法师 4/3/2 = 9 个法术位，已用 1 → 8。
    assert slots == {
        "id": "spell_slots",
        "label": "法术位",
        "value": 8,
        "max": 9,
        "reset": "long_rest",
    }


def test_resources_only_hit_dice_for_non_caster(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    # 战士：只有生命骰（按等级默认满池），没有任何法术位。
    bob = _player(data, "鲍勃")["quick_stats"]
    assert [item["id"] for item in bob["resources"]] == ["hit_dice"]
    assert bob["resources"][0]["max"] == 2
    assert bob["resources"][0]["value"] == 2


def test_member_without_card_has_null_stats_and_empty_resources(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "2")
    data = _panels(client).get_json()["data"]
    carol = _player(data, "carol")
    assert carol["has_card"] is False
    quick = carol["quick_stats"]
    assert quick == {
        "ac": None,
        "attack_bonus": None,
        "attack_bonus_source": "",
        "spell_save_dc": None,
        "passive_perception": None,
        "hp": {"value": 0, "max": 0, "temp": 0},
        "resources": [],
    }


# ---------------------------------------------------------------- 观察者裁剪


def test_player_view_never_receives_npc_quick_stats(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "2")
    response = _panels(client)
    assert response.status_code == 200
    data = response.get_json()["data"]
    characters = _characters(data)
    assert data["observer"] == "player"
    # NPC 分组整块不下发 → 玩家侧只有玩家角色的速览。
    assert "npc" not in characters
    assert all("quick_stats" in row for row in characters["players"])
    text = response.get_data(as_text=True)
    assert "熊地精哨兵" not in text
    assert "空壳 NPC" not in text


def test_dm_view_has_npc_quick_stats_from_statblock(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    data = _panels(client).get_json()["data"]
    assert data["observer"] == "dm"
    bear = _npc(data, "熊地精哨兵")["quick_stats"]
    # 数据包基线（bugbear）+ 模块 senses 覆盖。
    assert bear["ac"] == 16
    assert bear["hp"]["max"] > 0 and bear["hp"]["value"] == bear["hp"]["max"]
    assert bear["attack_bonus"] == 4
    assert bear["attack_bonus_source"] == "weapon"
    assert bear["passive_perception"] == 11
    assert bear["resources"] == []
    # 只有 walk（数据包基线 30 尺）→ 分速不显示；模块没写语言 → null。
    assert bear["speeds"] is None
    assert bear["languages"] is None
    # 模块数值不全的 NPC：如实不给 quick_stats 键（不塞占位数字）。
    assert "quick_stats" not in _npc(data, "空壳 NPC")


def test_npc_quick_stats_carry_speeds_and_languages(tmp_path, plugins):
    """NPC 速览补分速与语言：与玩家侧同口径（walk 之外有值才给分速表）。"""
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    wisp = _npc(_panels(client).get_json()["data"], "洞窟鬼火")["quick_stats"]
    # 模块写的 statblock.speed 分速表原样下发（世界包导入就是这个形状）。
    assert wisp["speeds"] == {"walk": 30, "fly": 60}
    assert wisp["languages"] == ["Common", "Goblin"]
    # 其余速览不受影响（数据包基线 + 模块覆盖同一条 statblock 通路）。
    assert wisp["ac"] == 15
    assert wisp["attack_bonus"] == 4


def test_characters_panel_documents_quick_stats_fields(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    _login(client, "1")
    characters = _characters(_panels(client).get_json()["data"])
    fields = characters["quick_stats_fields"]
    assert set(fields) == {
        "ac",
        "attack_bonus",
        "spell_save_dc",
        "passive_perception",
        "hp",
        "resources",
        "speeds",
        "languages",
    }
    assert "命中加值" in fields["attack_bonus"]
    assert "分速" in fields["speeds"]
    assert "语言" in fields["languages"]


# ---------------------------------------------------------------- 端点权限回归


def test_panels_permissions_regression(tmp_path, plugins):
    client, _rooms = _make_client(tmp_path)
    # 未登录 → 401
    assert _panels(client).status_code == 401
    # 非房间成员 → 403
    _login(client, "9")
    assert _panels(client).status_code == 403
    # 缺参 → 400；未知房间 → 404
    assert client.get("/api/plugins/dnd5e-vtt/panels").status_code == 400
    assert client.get("/api/plugins/dnd5e-vtt/panels?room=nope").status_code == 404
    # 非 dnd5e 房间 → 规则集门闸
    coc_client, _coc_rooms = _make_client(tmp_path, ruleset_id="coc7", scenario=False)
    _login(coc_client, "1")
    denied = _panels(coc_client)
    assert denied.status_code == 400
    assert "规则集" in str(denied.get_json())


def test_quick_stats_survive_missing_plugin_modules(tmp_path, plugins, monkeypatch):
    """插件模块拿不到时降级：四盒照旧由派生值给出，资源池为空数组。"""
    client, _rooms = _make_client(tmp_path)
    monkeypatch.setattr(dnd5e_vtt, "_dnd5e_modules", lambda: None)
    _login(client, "1")
    response = _panels(client)
    assert response.status_code == 200
    quick = _player(response.get_json()["data"], "艾莉丝")["quick_stats"]
    assert quick["ac"] == 15
    assert quick["resources"] == []
