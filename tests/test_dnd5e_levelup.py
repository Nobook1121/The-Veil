"""dnd5e 升级入口：HP 掷骰/平均值、职业特性、子职、属性提升与卡面手动升级端点。

覆盖四面：
- 纯规划器：4→5 级（熟练 +3、HP 增加、特性表命中）、子职级落地与「过了子职级不再选」、
  属性提升上限 20 校验（含失败后计划不被占用）；
- 非施法职业：stub 数据包确认无法术槽位；
- 与 ``schema.derive`` 交叉一致：升级后的熟练加值 / maxHp / 法术豁免 DC 同源；
- HTTP：``POST /api/plugins/dnd5e/characters/<id>/level-up`` 两段式契约（计划 → 落库），
  失败不写盘、XP 不动、未登录 401。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

from trpg_server import character_service
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

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


# ---------------------------------------------------------------- 夹具


def _wizard_sheet(level: int = 1) -> dict:
    slots = {"1": {"max": 2 if level == 1 else 3, "used": 0}}
    return {
        "level": level,
        "class": "wizard",
        "xp": 0,
        "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
        "proficiencies": {"saves": ["int", "wis"], "skills": ["arcana"]},
        "hp": {"current": 7, "max": 7},
        "hit_dice": {"die": 6, "max": level, "used": 0},
        "features": [],
        "spellcasting": {"known": [], "prepared": [], "slots": slots},
    }


def _cleric_sheet(level: int = 4) -> dict:
    return {
        "level": level,
        "class": "cleric",
        "xp": 3000,
        "abilities": {"str": 14, "dex": 12, "con": 14, "int": 10, "wis": 16, "cha": 8},
        "proficiencies": {"saves": ["wis", "cha"], "skills": ["religion"]},
        "hp": {"current": 20, "max": 28},
        "hit_dice": {"die": 8, "max": level, "used": 2},
        "features": [],
        "spellcasting": {
            "ability": "wis",
            "known": [],
            "prepared": ["cure-wounds"],
            "slots": {"1": {"max": 4, "used": 1}, "2": {"max": 3, "used": 0}},
        },
    }


def _kinds(plan: dict) -> set[str]:
    return {str(slot.get("kind")) for slot in plan.get("slots") or []}


def _slot(plan: dict, kind: str) -> dict:
    return next(slot for slot in plan["slots"] if slot["kind"] == kind)


# ---------------------------------------------------------------- 规划器


def test_cleric_4_to_5_hp_proficiency_and_feature_table(plugins):
    """4→5 级：熟练 +2→+3、HP 按 d8+CON 增加、职业表命中「摧毁不死生物」。"""
    plan = progression.create_plan(
        {"sheet": _cleric_sheet(4)},
        5,
        owner_key="character:c1",
        class_advancement=True,
    )
    assert plan["advancement"] is True
    assert {"hp", "proficiency_bonus"} <= _kinds(plan)
    # 手动升级入口：XP 冻结在升级前的值
    assert plan["xp"] == {"before": 3000, "after": 3000, "manual": True}

    hit_points = plan["hit_points"]
    assert hit_points["die"] == 8
    assert 1 <= hit_points["roll"] <= 8
    assert hit_points["average"] == 5  # d8：8/2+1
    assert hit_points["con_modifier"] == 2
    assert hit_points["roll_total"] == hit_points["roll"] + 2
    assert hit_points["average_total"] == 7

    hp_slot = _slot(plan, "hp")
    assert hp_slot["required"] is True
    assert [item["value"] for item in hp_slot["candidates"]] == ["roll", "average"]
    assert hp_slot["detail"]["before_max"] == 28

    granted = [item["label"] for item in plan["features"]]
    assert granted == ["摧毁不死生物（CR 1/2）"]

    result = progression.apply_plan(
        plan["plan_id"],
        [
            {"slot_id": "level-5:hp", "values": ["roll"]},
            {"slot_id": "level-5:spell_prepared", "values": ["cure-wounds"]},
        ],
        {"sheet": _cleric_sheet(4)},
    )
    sheet = result["sheet"]
    assert sheet["level"] == 5
    assert sheet["hp"]["max"] == 28 + hit_points["roll_total"]
    assert sheet["hp"]["current"] == 20 + hit_points["roll_total"]
    assert sheet["hit_dice"] == {"die": 8, "max": 5, "used": 2}
    assert "摧毁不死生物（CR 1/2）" in sheet["features"]
    assert sheet["xp"] == 3000
    assert sheet["spellcasting"]["slots"]["3"] == {"max": 2, "used": 0}

    changes = {item["field"]: item for item in result["changes"]}
    assert changes["proficiency_bonus"]["before"] == 2
    assert changes["proficiency_bonus"]["after"] == 3
    assert changes["hp.max"]["after"] == sheet["hp"]["max"]
    assert changes["hit_dice.max"] == {"field": "hit_dice.max", "before": 4, "after": 5}
    assert "features" in changes


def test_wizard_subclass_at_level_2_and_never_again(plugins):
    """子职级（法师 2 级）必须选；3 级法师升 4 级不再出现子职槽位。"""
    plan = progression.create_plan(
        {"sheet": _wizard_sheet(1)},
        2,
        owner_key="character:w1",
        class_advancement=True,
    )
    subclass_slot = _slot(plan, "subclass")
    assert subclass_slot["required"] is True
    assert [item["value"] for item in subclass_slot["candidates"]] == ["evocation"]
    assert subclass_slot["detail"]["subclass_level"] == 2
    assert plan["subclass"]["level_due"] is True

    result = progression.apply_plan(
        plan["plan_id"],
        [
            {"slot_id": "level-2:subclass", "values": ["evocation"]},
            {"slot_id": "level-2:hp", "values": ["average"]},
            {"slot_id": "level-2:spell_known", "values": ["shield", "detect-magic"]},
        ],
        {"sheet": _wizard_sheet(1)},
    )
    sheet = result["sheet"]
    assert sheet["subclass"] == "塑能学派"
    assert sheet["hp"]["max"] == 7 + plan["hit_points"]["average_total"]
    assert sheet["spellcasting"]["known"] == ["shield", "detect-magic"]
    assert {item["field"] for item in result["changes"]} >= {
        "level",
        "subclass",
        "hp.max",
        "hit_dice.max",
    }

    # 边界：3 级法师没有子职 → 升 4 级不给子职槽位，只提示该在 2 级选
    later = progression.create_plan(
        {"sheet": _wizard_sheet(3)},
        4,
        owner_key="character:w2",
        class_advancement=True,
    )
    assert "subclass" not in _kinds(later)
    assert later["subclass"]["level"] == 2
    assert later["subclass"]["level_due"] is False
    assert any("子职" in item for item in later["warnings"])


def test_ability_score_improvement_cap_20(plugins):
    """属性已达 20 时 +2 被拒；改选 +1/+1 仍然通过（失败不占用计划）。"""
    sheet = _wizard_sheet(3)
    sheet["abilities"]["int"] = 20
    plan = progression.create_plan(
        {"sheet": sheet}, 4, owner_key="character:w3", class_advancement=True
    )
    assert "asi" in _kinds(plan)
    asi_slot = _slot(plan, "asi")
    assert asi_slot["required"] is True
    assert asi_slot["max_picks"] == 2

    with pytest.raises(progression.ProgressionError) as capped:
        progression.apply_plan(
            plan["plan_id"],
            [
                {"slot_id": "level-4:hp", "values": ["average"]},
                {"slot_id": "level-4:asi", "values": ["int"]},
                {"slot_id": "level-4:spell_known", "values": ["shield", "misty-step"]},
            ],
            {"sheet": sheet},
        )
    assert capped.value.code == "progression.ability_cap"
    assert "20" in capped.value.message
    assert progression._PLANS[plan["plan_id"]].get("claimed") is None

    result = progression.apply_plan(
        plan["plan_id"],
        [
            {"slot_id": "level-4:hp", "values": ["average"]},
            {"slot_id": "level-4:asi", "values": ["dex", "wis"]},
            {"slot_id": "level-4:spell_known", "values": ["shield", "misty-step"]},
        ],
        {"sheet": sheet},
    )
    abilities = result["sheet"]["abilities"]
    assert abilities["dex"] == 15 and abilities["wis"] == 11
    assert abilities["int"] == 20  # 未动的属性保持原值


class _StubLibrary:
    """最小数据包替身：只提供 class / progression 两类条目。"""

    def __init__(self, classes: list[dict], levels: list[dict]) -> None:
        self._classes = classes
        self._levels = levels

    def entries(self, ruleset_id: str, type_name: str) -> list[dict]:
        if type_name == "class":
            return self._classes
        if type_name == "progression":
            return self._levels
        return []


def _progression_rows() -> list[dict]:
    return [
        {"id": f"level-{level}", "level": level, "xp": 0, "proficiency_bonus": 2 + (level - 1) // 4}
        for level in range(1, 21)
    ]


def test_non_caster_class_has_no_spell_step(monkeypatch):
    """非施法职业（无 spellcasting）不产生法术位 / 法术名额槽位。"""
    fighter = {
        "id": "fighter",
        "name": "Fighter",
        "hit_die": 10,
        "locales": {"zh-CN": {"name": "战士"}},
        "features": [
            {"id": "fighter-second-wind", "level": 1, "name": "Second Wind",
             "locales": {"zh-CN": {"name": "回气"}}},
            {"id": "fighter-action-surge", "level": 2, "name": "Action Surge",
             "locales": {"zh-CN": {"name": "行动如潮"}}},
        ],
    }
    stub = _StubLibrary([fighter], _progression_rows())
    monkeypatch.setattr(runtime, "content_packs_for", lambda ruleset_id: stub)

    sheet = {
        "level": 1,
        "class": "fighter",
        "abilities": {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 12, "cha": 8},
        "hp": {"current": 12, "max": 12},
        "features": ["回气"],
    }
    plan = progression.create_plan(
        {"sheet": sheet}, 2, owner_key="character:f1", class_advancement=True
    )
    # 2 级战士：只有 HP（熟练仍是 +2，没有属性提升，没有法术）
    assert _kinds(plan) == {"hp"}
    assert plan["hit_points"]["die"] == 10
    assert plan["hit_points"]["average"] == 6
    assert [item["label"] for item in plan["features"]] == ["行动如潮"]
    assert plan["subclass"]["level"] == 0

    result = progression.apply_plan(
        plan["plan_id"], [{"slot_id": "level-2:hp", "values": ["average"]}], {"sheet": sheet}
    )
    assert result["sheet"]["level"] == 2
    assert result["sheet"]["features"] == ["回气", "行动如潮"]
    assert "spellcasting" not in result["sheet"]


def test_upgrade_math_matches_schema_derive(plugins):
    """progression 的升级结果与插件 schema.derive 的派生值同口径。"""
    sheet = _cleric_sheet(4)
    plan = progression.create_plan(
        {"sheet": sheet}, 5, owner_key="character:c2", class_advancement=True
    )
    result = progression.apply_plan(
        plan["plan_id"],
        [{"slot_id": "level-5:hp", "values": ["average"]}],
        {"sheet": sheet},
    )
    upgraded = result["sheet"]
    derived = builtin_registry().resolve("dnd5e").schema.derive(upgraded)

    assert derived["proficiencyBonus"] == 3
    assert derived["maxHp"] == upgraded["hp"]["max"] == 28 + plan["hit_points"]["average_total"]
    # 感知 16（+3）+ 熟练 +3 → 法术豁免 DC 14，攻击 +6
    assert derived["spellSaveDC"] == 14
    assert derived["spellAttackBonus"] == 6
    assert result["after"]["hp"]["max"] == derived["maxHp"]
    assert result["after"]["spellcasting"]["slots"]["3"]["max"] == 2


# ---------------------------------------------------------------- HTTP


class _FakeUserManager:
    def is_session_current(self, user_id, token) -> bool:
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


class _AdminActor:
    user_id = 1
    role = "ADMIN"

    def has(self, *_args, **_kwargs) -> bool:
        return True


def _app_config(tmp_path: Path) -> dict:
    return {
        "TESTING": True,
        "SECRET_KEY": "test",
        "ROOMS_DIR": tmp_path / "rooms",
        "CONFIG_DIR": tmp_path / "config",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "USERS_DIR": tmp_path / "users",
        "WEAPONS_DIR": tmp_path / "weapons",
        "OCCUPATIONS_DIR": tmp_path / "occupations",
        "USER_MANAGER": _FakeUserManager(),
    }


@pytest.fixture()
def client(tmp_path, plugins):
    app = Flask(__name__)
    app.config.update(_app_config(tmp_path))
    app.register_blueprint(dnd5e_bp)
    with app.app_context():
        character_service.apply_save(
            _AdminActor(),
            "dnd-card-1",
            {"name": "艾拉", "rulesetId": "dnd5e", "sheet": _wizard_sheet(1)},
        )
    return app.test_client(), app


def _login(client, user_id: int = 1) -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role="ADMIN", session_token="token")


def _stored(app) -> dict:
    path = Path(app.config["CHARACTERS_DIR"]) / "dnd-card-1" / "character.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_http_level_up_plans_then_applies(client):
    manager, app = client
    _login(manager)
    planned = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up", json={"target_level": 2}
    )
    assert planned.status_code == 200
    plan = planned.get_json()["data"]
    assert plan["advancement"] is True
    assert plan["character_id"] == "dnd-card-1"
    assert plan["current_level"] == 1 and plan["target_level"] == 2
    assert {"hp", "subclass"} <= _kinds(plan)

    before = _stored(app)
    applied = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up",
        json={
            "plan_id": plan["plan_id"],
            "choices": [
                {"slot_id": "level-2:hp", "values": ["average"]},
                {"slot_id": "level-2:subclass", "values": ["evocation"]},
                {"slot_id": "level-2:spell_known", "values": ["shield", "detect-magic"]},
            ],
        },
    )
    assert applied.status_code == 200
    body = applied.get_json()["data"]
    assert body["before"]["level"] == 1
    assert body["after"]["level"] == 2
    assert body["after"]["subclass"] == "塑能学派"
    assert body["after"]["xp"] == before["sheet"]["xp"] == 0

    stored = _stored(app)
    assert stored["sheet"]["level"] == 2
    assert stored["sheet"]["subclass"] == "塑能学派"
    assert stored["sheet"]["xp"] == 0  # 手动升级不动 XP
    assert stored["maxHp"] == body["sheet"]["hp"]["max"]
    assert stored["proficiencyBonus"] == 2
    assert stored["sheet"]["hit_dice"]["max"] == 2


def test_http_level_up_rejects_invalid_choice_without_writing(client):
    manager, app = client
    _login(manager)
    plan = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up", json={"target_level": 2}
    ).get_json()["data"]
    before = _stored(app)

    invalid = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up",
        json={
            "plan_id": plan["plan_id"],
            "choices": [
                {"slot_id": "level-2:hp", "values": ["average"]},
                {"slot_id": "level-2:subclass", "values": ["evocation"]},
                {"slot_id": "level-2:spell_known", "values": ["shield"]},  # 需要 2 个
            ],
        },
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "progression.invalid_option"
    assert _stored(app) == before

    jump = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up", json={"target_level": 5}
    )
    assert jump.status_code == 400
    assert jump.get_json()["error"] == "progression.level_jump"


def test_http_level_up_requires_login(client):
    manager, _app = client
    denied = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up", json={"target_level": 2}
    )
    assert denied.status_code == 401
    assert denied.get_json()["error"] == "not_logged_in"


def test_http_level_up_rejects_foreign_character(client):
    manager, _app = client

    class _OtherUserManager(_FakeUserManager):
        def get_user_by_id(self, user_id):
            return {"id": user_id, "username": "other", "role": "USER"}

    _login(manager, user_id=2)
    manager.application.config["USER_MANAGER"] = _OtherUserManager()
    denied = manager.post(
        "/api/plugins/dnd5e/characters/dnd-card-1/level-up", json={"target_level": 2}
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "permission_denied"
