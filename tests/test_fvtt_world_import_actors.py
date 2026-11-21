"""fvtt-world-import 第三刀：actors → dnd5e 角色卡 + 剧本 NPC statblock 端点。

覆盖（两包真实 zip 对账 + 门控与配额）：

- ``POST /api/plugins/fvtt-world-import/rooms/<id>/actors/run``：
  heroes 的 10 个 character 落库（属性 / 法术位 / AC / HP 逐锚点核对，
  走真实 ``character_service.apply_save`` + dnd5e 信封打包 + 头像落盘）、
  12 个 NPC + 1 个 vehicle；sixian 的 14 个 NPC 模块并入房间绑定剧本
  （statblock 数值锚点 + 升版 + 重钉 + 幂等重导）；
- inspect 报告透传 actors 计数与文件夹分组；
- 门控：配额写盘前整体拒绝（USER 超额 403 且零落盘）、房管权限、
  非 dnd5e 房间、无绑定剧本、包里没有 actors 集合。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
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


fvtt = _load_module(FVTT_ENTRY, "fvtt_world_import_entry_actors_test")


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


@pytest.fixture()
def plugins():
    """装载 dnd5e / dnd5e-srd / veil-map 插件（信封打包 + 法术位表 + 怪物包）。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


# -- 装置 ------------------------------------------------------------------


def _write_room(rooms: Path, room_id: str, *, ruleset_id: str = "dnd5e", owner_id: int = 1, scenario_id: int | None = 101) -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "世界包角色导入测试",
        "room_code": room_id.upper(),
        "creator_id": str(owner_id),
        "ruleset_id": ruleset_id,
        "members": [
            {
                "user_id": str(owner_id),
                "username": "alice" if owner_id == 1 else "bob",
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
        info["scenario_version"] = "1.0.0"
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_scenario(scenarios: Path, scenario_id: int = 101) -> None:
    sdir = scenarios / f"scenario-{scenario_id}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(
        json.dumps(
            {
                "id": scenario_id,
                "title": "测试剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "scenario_version": "1.0.0",
                "modules": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_app(tmp_path: Path, *, ruleset_id: str = "dnd5e", max_cards: int = 20, scenario: bool = True, owner_id: int = 1):
    """dnd5e 房间 + 绑定剧本 + 配额配置的测试应用工厂。"""
    rooms = tmp_path / "rooms"
    _write_room(rooms, "room1", ruleset_id=ruleset_id, owner_id=owner_id, scenario_id=101 if scenario else None)
    scenarios = tmp_path / "scenarios"
    if scenario:
        _write_scenario(scenarios, 101)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text(
        f"[character_rules]\nmax_cards_per_user = {max_cards}\n", encoding="utf-8"
    )
    app = Flask(__name__)
    app.secret_key = "test"
    # ROOMS_DIR 必须是 Path：characters 路由的房间快照同步会对它调用 .exists()。
    app.config.update(
        ROOMS_DIR=rooms,
        CONFIG_DIR=str(config),
        SCENARIOS_DIR=scenarios,
        CHARACTERS_DIR=tmp_path / "characters",
    )
    app.register_blueprint(fvtt.bp)
    return app.test_client(), tmp_path / "characters", rooms, scenarios


def _login(client, user_id: int, username: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=username, role="USER")


def _upload(client, zip_path: Path, *, user_id: int = 1, username: str = "alice"):
    _login(client, user_id, username)
    with open(zip_path, "rb") as handle:
        return client.post(
            "/api/plugins/fvtt-world-import/rooms/room1/actors/run",
            data={"file": (handle, zip_path.name)},
            content_type="multipart/form-data",
        )


def _stored_cards(characters_dir: Path) -> dict[str, dict[str, Any]]:
    """角色卡存储目录 → {name: card}；目录缺席时为空。"""
    cards: dict[str, dict[str, Any]] = {}
    if not characters_dir.exists():
        return cards
    for cdir in sorted(characters_dir.iterdir()):
        path = cdir / "character.json"
        if not path.exists():
            continue
        card = json.loads(path.read_text(encoding="utf-8"))
        cards[str(card.get("name"))] = card
    return cards


def _response_character(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in data["characters"] if row["name"] == name)


def _scenario_modules(scenarios: Path) -> list[dict[str, Any]]:
    payload = json.loads(
        (scenarios / "scenario-101" / "scenario.json").read_text(encoding="utf-8")
    )
    return list(payload.get("modules") or [])


def _room_info(rooms: Path) -> dict[str, Any]:
    return json.loads((rooms / "room1" / "info.json").read_text(encoding="utf-8"))


def _tiny_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "world.json",
            json.dumps({"id": "demo", "title": "演示世界", "system": "dnd5e", "coreVersion": "13.351"}),
        )
    return buffer.getvalue()


# -- inspect 报告：actors 计数透传 -----------------------------------------


def test_inspect_reports_actor_counts(tmp_path, plugins):
    client, _chars, _rooms, _scenarios = _make_app(tmp_path)
    _login(client, 1, "alice")
    for zip_path, characters, npcs in ((HEROES_ZIP, 10, 12), (SIXIAN_ZIP, 0, 14)):
        with open(_require_zip(zip_path), "rb") as handle:
            rv = client.post(
                "/api/plugins/fvtt-world-import/inspect",
                data={"file": (handle, zip_path.name)},
                content_type="multipart/form-data",
            )
        assert rv.status_code == 200
        data = rv.get_json()["data"]
        assert data["characters"] == characters
        assert data["npcs"] == npcs
    # heroes：10 角色 + 12 NPC + 1 载具，文件夹分组如实上报。
    with open(_require_zip(HEROES_ZIP), "rb") as handle:
        data = client.post(
            "/api/plugins/fvtt-world-import/inspect",
            data={"file": (handle, "heroes.zip")},
            content_type="multipart/form-data",
        ).get_json()["data"]
    assert data["vehicles"] == 1
    assert any(group["character"] or group["npc"] for group in data["actor_folders"])


# -- heroes：character 落库 ------------------------------------------------


def test_heroes_run_imports_character_cards(tmp_path, plugins):
    client, characters_dir, _rooms, _scenarios = _make_app(tmp_path)
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["totals"]["characters"] == 10 and data["totals"]["npcs"] == 12 and data["totals"]["vehicles"] == 1

    stored = _stored_cards(characters_dir)
    assert len(stored) == 10
    assert all(card["playerId"] == "1" and card["rulesetId"] == "dnd5e" for card in stored.values())

    # 卓尔法师：int16/wis8、1 环 2 法术位、无甲 AC12 + 默认速度警告。
    drow = stored["预设角色：卓尔法师"]
    assert drow["sheet"]["abilities"]["int"] == 16 and drow["sheet"]["abilities"]["wis"] == 8
    assert drow["sheet"]["spellcasting"]["slots"] == {"1": {"max": 2, "used": 0}}
    assert drow["armorClass"] == 12
    drow_warnings = _response_character(data, "预设角色：卓尔法师")["warnings"]
    assert any("无甲" in text for text in drow_warnings)
    assert any("默认 30 尺" in text for text in drow_warnings)

    # 哈普（源包无 HP 字段）按体质推导 10；圣武士链甲+盾 18；野蛮人 flat 15。
    assert stored["哈普·希尔·软"]["maxHp"] == 10
    assert stored["预设角色：人类圣武士"]["armorClass"] == 18
    assert stored["预设角色：龙裔野蛮人"]["armorClass"] == 15

    # 头像：机核小队的 img 指向包内资产 → data URI 落盘为 avatar.* 文件。
    avatar_files = list(characters_dir.glob("*/avatar.*"))
    assert avatar_files and all(path.stat().st_size > 0 for path in avatar_files)


# -- heroes：NPC 模块 + vehicle + 剧本重钉 --------------------------------


def test_heroes_run_imports_npc_modules_and_repins_scenario(tmp_path, plugins):
    client, _characters_dir, rooms, scenarios = _make_app(tmp_path)
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]

    assert len(data["npcs"]) == 12
    # 蜘蛛群：啃咬多段伤害只留首段，on-hit 副段降级为附加标签。
    spider = next(row for row in data["npcs"] if "蜘蛛群" in row["title"])
    assert any("多段伤害" in text for text in spider["warnings"])
    # 马车：vehicle 无战斗数值，只报告不落库。
    wagon = data["vehicles"][0]
    assert wagon["name"] == "马车" and wagon["kind"] == "vehicle"

    modules = _scenario_modules(scenarios)
    assert sum(1 for m in modules if m.get("module_type") == "npc") == 12
    assert all(m.get("visibility") == "kp" for m in modules if m.get("module_type") == "npc")
    assert (scenarios / "scenario-101" / "versions" / "1.0.1.json").exists()
    assert _room_info(rooms)["scenario_version"] == "1.0.1"


# -- sixian：NPC statblock 锚点 + 幂等重导 --------------------------------


def test_sixian_run_imports_npc_modules(tmp_path, plugins):
    client, _characters_dir, rooms, scenarios = _make_app(tmp_path)
    rv = _upload(client, _require_zip(SIXIAN_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["totals"] == {"characters": 0, "npcs": 14, "vehicles": 0, "warnings": 2}

    modules = {m.get("title"): m for m in _scenario_modules(scenarios)}
    assert len(modules) == 14
    veteran = modules["[瓦尔哈拉酒馆] 105老兵"]["statblock"]
    assert (veteran["armorClass"], veteran["maxHp"], veteran["cr"]) == (17, 45, "3")
    necro = modules["[地下墓穴] 女死灵术士"]["statblock"]
    assert necro["spellcasting"]["spellSaveDC"] == 14 and necro["spellcasting"]["spellAttackBonus"] == 6
    assert necro["spell_slots"]["1"] == {"max": 4, "used": 0}
    assert _room_info(rooms)["scenario_version"] == "1.0.1"

    # 幂等重导：同 fvtt_actor_id 模块先移除再追加，数量不翻倍，版本再升一版。
    rv2 = _upload(client, _require_zip(SIXIAN_ZIP))
    assert rv2.status_code == 200
    assert len(_scenario_modules(scenarios)) == 14
    assert _room_info(rooms)["scenario_version"] == "1.0.2"


# -- 门控：配额 / 权限 / 规则集 / 剧本 / 包结构 ---------------------------


def test_quota_precheck_rejects_whole_batch(tmp_path, plugins):
    # bob 是房主但只是 USER：10 张角色卡会把默认配额（3）击穿 → 写盘前整体拒绝。
    client, characters_dir, _rooms, scenarios = _make_app(tmp_path, max_cards=3, owner_id=2)
    rv = _upload(client, _require_zip(HEROES_ZIP), user_id=2, username="bob")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.character_quota"
    assert not characters_dir.exists()
    assert _scenario_modules(scenarios) == []


def test_run_requires_room_manager(tmp_path, plugins):
    client, _characters_dir, _rooms, _scenarios = _make_app(tmp_path)
    # bob 是普通成员：只有房主或房管可以导入。
    rv = _upload(client, _require_zip(HEROES_ZIP), user_id=2, username="bob")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.manager_only"


def test_run_rejects_non_dnd5e_room(tmp_path, plugins):
    client, _characters_dir, _rooms, _scenarios = _make_app(tmp_path, ruleset_id="coc7")
    _login(client, 1, "alice")
    rv = client.post(
        "/api/plugins/fvtt-world-import/rooms/room1/actors/run",
        data={"file": (io.BytesIO(_tiny_zip()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.ruleset_unsupported"


def test_run_rejects_room_without_scenario(tmp_path, plugins):
    # 包里有 NPC 而房间没有绑定剧本：整体拒绝，不落半份模块。
    client, _characters_dir, _rooms, scenarios = _make_app(tmp_path, scenario=False)
    rv = _upload(client, _require_zip(SIXIAN_ZIP))
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.scenario_missing"
    assert not (scenarios / "scenario-101").exists()


def test_run_rejects_package_without_actors(tmp_path, plugins):
    client, _characters_dir, _rooms, _scenarios = _make_app(tmp_path)
    _login(client, 1, "alice")
    rv = client.post(
        "/api/plugins/fvtt-world-import/rooms/room1/actors/run",
        data={"file": (io.BytesIO(_tiny_zip()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.actors_missing"
