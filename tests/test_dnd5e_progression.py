"""dnd5e 升级规划：plan 只读、apply 一次性落库。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from flask import Flask

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
if str(PLUGIN_ROOT / "dnd5e") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "dnd5e"))

from dnd5e_plugin import progression  # noqa: E402  (需先准备插件运行时)
from dnd5e_plugin.http import bp as dnd5e_bp  # noqa: E402


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture()
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _wizard_sheet(level: int = 3) -> dict:
    return {
        "level": level,
        "class": "wizard",
        "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
        "proficiencies": {"saves": ["int", "wis"], "skills": ["arcana", "investigation"]},
        "features": [],
        "spellcasting": {
            "known": ["magic-missile"],
            "prepared": [],
            "slots": {"1": {"max": 4, "used": 1}, "2": {"max": 2, "used": 0}},
        },
    }


def _room(tmp_path: Path, *, ruleset: str = "dnd5e", card: dict | None = None) -> Path:
    rooms = tmp_path / "rooms"
    room = rooms / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "升级测试",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": ruleset,
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": card
                or {"name": "艾拉", "level": 3, "class": "wizard", "sheet": _wizard_sheet()},
            },
            {
                "user_id": "u2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "鲍勃", "sheet": _wizard_sheet(2)},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return rooms


@pytest.fixture()
def client(tmp_path, plugins):
    rooms = _room(tmp_path)
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=str(rooms),
        CONFIG_DIR=str(tmp_path / "config"),
        SCENARIOS_DIR=str(tmp_path / "scenarios"),
        CHARACTERS_DIR=str(tmp_path / "characters"),
        TESTING=True,
    )
    app.register_blueprint(dnd5e_bp)
    return app.test_client(), app


def _login(client, user_id: str = "u1") -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=user_id, role="USER")


def _read_info(app) -> dict:
    return json.loads(
        (Path(app.config["ROOMS_DIR"]) / "room1" / "info.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------- 纯规划器


def test_plan_wizard_level_3_to_4(plugins):
    plan = progression.create_plan(
        {"sheet": _wizard_sheet(3)}, 4, owner_key="room1:u1", target_user_id="u1"
    )
    kinds = {slot["kind"] for slot in plan["slots"]}
    assert kinds == {"asi", "spell_slots", "spell_known"}
    assert plan["current_level"] == 3 and plan["target_level"] == 4
    asi = next(slot for slot in plan["slots"] if slot["kind"] == "asi")
    assert asi["required"] is True and asi["blocking"] is True
    known = next(slot for slot in plan["slots"] if slot["kind"] == "spell_known")
    assert known["min_picks"] == known["max_picks"] == 2
    assert all(item["value"] != "magic-missile" for item in known["candidates"])
    assert plan["warnings"]


def test_apply_requires_single_level_and_same_class(plugins):
    with pytest.raises(progression.ProgressionError) as jump:
        progression.create_plan(
            {"sheet": _wizard_sheet(3)}, 5, owner_key="room1:u1"
        )
    assert jump.value.code == "progression.level_jump"

    with pytest.raises(progression.ProgressionError) as cross:
        progression.create_plan(
            {"sheet": _wizard_sheet(3)}, 4, owner_key="room1:u1", class_id="cleric"
        )
    assert cross.value.code == "progression.class_mismatch"


def test_apply_builds_new_sheet_and_rejects_partial_choices(plugins):
    plan = progression.create_plan({"sheet": _wizard_sheet(3)}, 4, owner_key="room1:u1")
    with pytest.raises(progression.ProgressionError) as missing:
        progression.apply_plan(plan["plan_id"], [], {"sheet": _wizard_sheet(3)})
    assert missing.value.code == "progression.missing_choice"

    result = progression.apply_plan(
        plan["plan_id"],
        [
            {"slot_id": "level-4:asi", "values": ["int"]},
            {"slot_id": "level-4:spell_known", "values": ["shield", "misty-step"]},
        ],
        {"sheet": _wizard_sheet(3)},
    )
    sheet = result["sheet"]
    assert sheet["level"] == 4
    assert sheet["abilities"]["int"] == 18
    assert sheet["spellcasting"]["known"] == ["magic-missile", "shield", "misty-step"]
    assert sheet["spellcasting"]["slots"]["2"]["max"] == 3
    assert sheet["spellcasting"]["slots"]["1"]["used"] == 1
    assert {change["field"] for change in result["changes"]} >= {
        "level",
        "abilities.int",
        "spellcasting.known",
        "spellcasting.slots",
        "xp",
    }

    # 计划成功后由落库方 commit，之后不能重放。
    progression.commit_plan(plan["plan_id"])
    with pytest.raises(progression.ProgressionError) as replay:
        progression.apply_plan(plan["plan_id"], [], {"sheet": _wizard_sheet(3)})
    assert replay.value.code == "progression.plan_not_found"


def test_cleric_prepared_slot_is_optional(plugins):
    sheet = {
        "level": 4,
        "class": "cleric",
        "abilities": {"str": 10, "dex": 10, "con": 12, "int": 10, "wis": 16, "cha": 10},
        "features": [],
        "spellcasting": {"known": [], "prepared": ["cure-wounds"], "slots": {"1": {"max": 4, "used": 0}, "2": {"max": 3, "used": 0}}},
    }
    plan = progression.create_plan({"sheet": sheet}, 5, owner_key="room1:u1")
    prepared = next(slot for slot in plan["slots"] if slot["kind"] == "spell_prepared")
    assert prepared["required"] is False
    assert prepared["default"] == ["cure-wounds"]
    result = progression.apply_plan(plan["plan_id"], [], {"sheet": sheet})
    assert result["sheet"]["level"] == 5
    assert result["sheet"]["spellcasting"]["prepared"] == ["cure-wounds"]


# ---------------------------------------------------------------- HTTP


def test_http_plan_and_apply_writes_room_card(client):
    manager, app = client
    _login(manager, "u1")

    planned = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan", json={"target_level": 4}
    )
    assert planned.status_code == 200
    plan = planned.get_json()["data"]
    assert plan["class"] == "wizard"
    plan_id = plan["plan_id"]

    applied = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/apply",
        json={
            "plan_id": plan_id,
            "choices": [
                {"slot_id": "level-4:asi", "values": ["int"]},
                {"slot_id": "level-4:spell_known", "values": ["shield", "misty-step"]},
            ],
        },
    )
    assert applied.status_code == 200
    body = applied.get_json()["data"]
    assert body["before"]["level"] == 3
    assert body["after"]["level"] == 4
    assert any(change["field"] == "level" for change in body["changes"])

    info = _read_info(app)
    card = info["members"][0]["character_card"]
    assert card["level"] == 4
    assert card["sheet"]["abilities"]["int"] == 18
    assert card["sheet"]["spellcasting"]["known"] == ["magic-missile", "shield", "misty-step"]


def test_http_apply_rejects_invalid_choice_without_writing(client):
    manager, app = client
    _login(manager, "u1")
    plan = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan", json={"target_level": 4}
    ).get_json()["data"]
    before = _read_info(app)["members"][0]["character_card"]

    invalid = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": [
                {"slot_id": "level-4:asi", "values": ["luck"]},
                {"slot_id": "level-4:spell_known", "values": ["shield", "misty-step"]},
            ],
        },
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "progression.invalid_option"
    assert _read_info(app)["members"][0]["character_card"] == before


def test_http_plan_expires_and_detects_changed_card(client):
    manager, app = client
    _login(manager, "u1")
    plan = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan", json={"target_level": 4}
    ).get_json()["data"]

    progression._PLANS[plan["plan_id"]]["expires_epoch"] = time.time() - 1
    expired = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/apply",
        json={"plan_id": plan["plan_id"], "choices": []},
    )
    assert expired.status_code == 410
    assert expired.get_json()["error"] == "progression.plan_expired"

    fresh = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan", json={"target_level": 4}
    ).get_json()["data"]
    info = _read_info(app)
    info["members"][0]["character_card"]["sheet"]["abilities"]["dex"] = 16
    (Path(app.config["ROOMS_DIR"]) / "room1" / "info.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    stale = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/apply",
        json={"plan_id": fresh["plan_id"], "choices": []},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "progression.plan_stale"


def test_http_plan_rejects_level_jump_and_non_manager_target(client):
    manager, _app = client
    _login(manager, "u2")
    jump = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan", json={"target_level": 5}
    )
    assert jump.status_code == 400
    assert jump.get_json()["error"] == "progression.level_jump"

    foreign = manager.post(
        "/api/plugins/dnd5e/rooms/room1/progression/plan",
        json={"target_level": 3, "user_id": "u1"},
    )
    assert foreign.status_code == 403
