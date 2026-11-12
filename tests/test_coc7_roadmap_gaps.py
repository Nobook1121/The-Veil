"""路线图缺口补齐回归：训练、成长标记落卡、schema 命名空间、B2/B14、薄工具。

覆盖 task-11（CoC7 批次补齐）新增的实现，旧的批次测试文件保持不动。
"""

from __future__ import annotations

import json

import pytest

from trpg_server import room_interlude, room_sanity
from trpg_server.agents.tools import coc7, combat
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import growth, schema, training
from trpg_server.ruleset.coc7.options import resolve_options


# ---------------------------------------------------------------------------
# 通用夹具
# ---------------------------------------------------------------------------
class FakeContext:
    def __init__(self, room_dir, house_rules=None):
        self.room_dir = room_dir
        self.room_id = "roadmap-gaps"
        self.config_dir = room_dir / "config"
        self.scenarios_dir = room_dir / "scenarios"
        self.characters_dir = room_dir / "characters"
        self._house_rules = house_rules or {}

    def room_info(self):
        return {"id": self.room_id, "members": [], "house_rules": self._house_rules}


def _queue_rng(values):
    queue = list(values)

    def rng(sides):
        value = queue.pop(0)
        assert 1 <= value <= sides, (value, sides)
        return value

    return rng


def _spec(name, **overrides):
    spec = {
        "name": name,
        "is_npc": True,
        "side": "enemy",
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 20,
        "max_hp": 20,
        "luck": 50,
        "skills": {"Fighting (Brawl)": 50, "射击(手枪)": 50},
        "weapons": [
            {"name": "Knife", "skill": "Fighting (Brawl)", "damage": "1D4+2", "attacks": "1"},
        ],
    }
    spec.update(overrides)
    return spec


def _gunner(name, **overrides):
    return _spec(
        name,
        weapons=[
            {
                "name": "手枪",
                "skill": "射击(手枪)",
                "damage": "1D10",
                "range": "15 yards",
                "attacks": "1",
                "ammo": "6",
            }
        ],
        **overrides,
    )


def _write_room(tmp_path, *, house_rules=None, skills=None, room_id="r-1"):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    card = {
        "name": "调查员",
        "rulesetId": "coc7",
        "attributes": {
            "STR": 50,
            "DEX": 50,
            "CON": 50,
            "APP": 50,
            "POW": 60,
            "SIZ": 50,
            "EDU": 60,
            "INT": 60,
        },
        "skills": skills or [{"name": "侦查", "value": 50}],
        "currentSan": 50,
        "maxSan": 60,
        "luck": 50,
    }
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "house_rules": dict(house_rules or {}),
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "room_role": "admin",
                "status": "active",
                "is_active": True,
                "character_card": card,
                "character_state": {
                    "current_san": 50,
                    "max_san": 60,
                    "current_hp": 10,
                    "max_hp": 10,
                    "growth_marks": [],
                },
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


# ---------------------------------------------------------------------------
# 批次 2：训练
# ---------------------------------------------------------------------------
def test_training_default_off():
    assert resolve_options(None).growth_training is False


def test_training_math_and_cap():
    assert training.normalize_weeks(0) == 1
    assert training.normalize_weeks(99) == 12
    assert training.weeks_to_days(2) == 14
    assert training.training_checks(4) == 1
    assert training.training_checks(8) == 2
    result = training.train_skill(50, weeks=4, rng=_queue_rng([51, 7]))
    assert result["checks"][0]["improved"] is True
    assert result["after"] == 57
    # 12 周 = 3 次检定；99 已经是上限，不再增长。
    capped = training.train_skill(99, weeks=12, rng=_queue_rng([100, 10, 100, 10, 100, 10]))
    assert capped["after"] == 99
    assert all(check["after"] <= 99 for check in capped["checks"])


def test_train_command_writes_extension_records(tmp_path):
    room_dir, info = _write_room(tmp_path, house_rules={"coc7_growth_training": True})
    assert room_interlude.start_interlude(room_dir, info, rng=_queue_rng([]))["state"]["active"] is True

    ok, text = handle_room_rule_command(
        ["train", "tester", "侦查", "4"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is True, text
    assert "周" in text
    saved = load_room_info(room_dir)
    records = saved["members"][0]["character_card"]["extensions"]["training"]["records"]
    assert len(records) == 1
    assert records[0]["skill"] == "侦查"
    assert records[0]["weeks"] == 4


def test_train_command_requires_switch_and_interlude(tmp_path):
    room_dir, _ = _write_room(tmp_path)
    ok, _ = handle_room_rule_command(
        ["train", "tester", "侦查", "4"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is False

    room_dir, info = _write_room(tmp_path / "on", house_rules={"coc7_growth_training": True})
    ok, text = handle_room_rule_command(
        ["train", "tester", "侦查", "4"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is False
    assert "幕间" in text


# ---------------------------------------------------------------------------
# 批次 2：成长标记写 extensions.growth.marks
# ---------------------------------------------------------------------------
def test_growth_mark_written_to_card_extension(tmp_path):
    room_dir, info = _write_room(tmp_path)
    member = info["members"][0]
    record = room_interlude.record_growth_mark(
        room_dir, info, member, skill="侦查", value=50
    )
    assert record is not None
    saved = load_room_info(room_dir)
    card = saved["members"][0]["character_card"]
    marks = card["extensions"]["growth"]["marks"]
    assert any(row.get("skill") == "侦查" for row in marks)
    # 旧字段仍然保留，兼容旧读。
    assert saved["members"][0]["character_state"]["growth_marks"]


def test_read_marks_prefers_extension_and_falls_back():
    new_card = {"extensions": {"growth": {"marks": [{"skill": "侦查"}]}}}
    assert growth.read_marks(new_card, [{"skill": "聆听"}]) == [{"skill": "侦查"}]
    legacy_card = {"extensions": {"growth": {}}}
    assert growth.read_marks(legacy_card, [{"skill": "聆听"}]) == [{"skill": "聆听"}]
    assert growth.read_marks({}, [{"skill": "聆听"}]) == [{"skill": "聆听"}]


def test_settle_clears_extension_marks_and_keeps_history(tmp_path):
    room_dir, info = _write_room(tmp_path)
    info["members"][0]["character_card"].setdefault("extensions", {})["growth"] = {
        "marks": [{"id": "m-1", "skill": "侦查", "value": 50, "eligible": True, "reason": "check_success"}]
    }
    info["members"][0]["character_state"]["growth_marks"] = [{"id": "m-1", "skill": "侦查", "value": 50}]
    save_room_info(room_dir, info)

    outcome = room_interlude.start_interlude(room_dir, info, rng=_queue_rng([60, 5]))
    assert outcome["settlement"]["growth_done"] is True
    card = info["members"][0]["character_card"]
    block = card["extensions"]["growth"]
    assert block["marks"] == []
    assert block["history"]
    assert block["history"][0]["skill"] == "侦查"


def test_experience_check_requires_interlude_and_mark(tmp_path):
    room_dir, info = _write_room(tmp_path)
    member = info["members"][0]
    with pytest.raises(room_interlude.InterludeError):
        room_interlude.experience_check(room_dir, info, member, skill="侦查")

    assert room_interlude.start_interlude(room_dir, info, rng=_queue_rng([]))["state"]["active"]
    with pytest.raises(room_interlude.InterludeError):
        room_interlude.experience_check(room_dir, info, member, skill="侦查")

    mark = {"id": "m-2", "skill": "侦查", "value": 50, "eligible": True, "reason": "check_success"}
    member["character_card"].setdefault("extensions", {})["growth"] = {"marks": [mark]}
    member["character_state"]["growth_marks"] = [dict(mark)]
    save_room_info(room_dir, info)
    result = room_interlude.experience_check(
        room_dir, info, member, skill="侦查", rng=_queue_rng([60, 5])
    )
    assert result["record"]["skill"] == "侦查"
    assert result["record"]["after"] == 55


# ---------------------------------------------------------------------------
# 批次 0：extensions 命名空间
# ---------------------------------------------------------------------------
def test_extension_namespaces_and_normalize():
    namespaces = set(schema.EXTENSION_SCHEMA_VERSIONS)
    assert namespaces == {
        "growth", "mental", "social", "economy", "contacts",
        "training", "age", "chase", "magic",
    }
    assert schema.EXTENSION_SCHEMA_VERSIONS["mental"] >= 2
    inst = schema.Coc7CharacterSchema()
    assert set(inst.default_extensions()) == namespaces
    raw = {"growth": {"marks": []}, "unknown": {"x": 1}}
    normalized = inst.normalize_extensions(raw)
    assert normalized["growth"]["schemaVersion"] == 1
    assert normalized["growth"]["marks"] == []
    assert normalized["unknown"] == {"x": 1}
    # 不改入参
    assert raw == {"growth": {"marks": []}, "unknown": {"x": 1}}


# ---------------------------------------------------------------------------
# B2：奖励骰/惩罚骰先抵消
# ---------------------------------------------------------------------------
def _melee_with_dice(monkeypatch, tmp_path, bonus, penalty):
    captured = {}

    def fake_opposed(*args, **kwargs):
        captured.update(kwargs)
        return {
            "attack_roll": 50,
            "attack_candidates": [],
            "attack_level": "regular",
            "defense_roll": 100,
            "defense_candidates": [],
            "defense_level": "fumble",
        }

    monkeypatch.setattr(combat, "_opposed_rolls", fake_opposed)
    context = FakeContext(tmp_path)
    combat.start_combat({"participants": [_spec("A", side="player"), _spec("B")]}, context)
    result = combat.attack_combatant(
        {
            "actor": "A",
            "target": "B",
            "weapon": "Knife",
            "bonus_dice": bonus,
            "penalty_dice": penalty,
        },
        context,
    )
    assert "error" not in result
    return captured


@pytest.mark.parametrize(
    "bonus,penalty,expected",
    [
        (1, 1, (0, 0)),
        (2, 1, (1, 0)),
        (2, 2, (0, 0)),
        (0, 3, (0, 2)),
        (3, 0, (2, 0)),
    ],
)
def test_melee_normalizes_bonus_and_penalty(monkeypatch, tmp_path, bonus, penalty, expected):
    captured = _melee_with_dice(monkeypatch, tmp_path, bonus, penalty)
    assert (captured["bonus"], captured["penalty"]) == expected


def _shoot_with_dice(monkeypatch, tmp_path, attacker_build=None, target_build=None, **dice):
    captured = []

    def fake_percentile(roller, bonus, penalty, *args, **kwargs):
        captured.append((bonus, penalty))
        return 1, []

    monkeypatch.setattr(combat, "_percentile_roll", fake_percentile)
    context = FakeContext(tmp_path)
    attacker = _gunner("A", side="player")
    target = _spec("B", hp=30, max_hp=30)
    if attacker_build is not None:
        attacker["build"] = attacker_build
    if target_build is not None:
        target["build"] = target_build
    combat.start_combat({"participants": [attacker, target]}, context)
    payload = {"actor": "A", "target": "B", "weapon": "手枪", "attack_roll": 1}
    payload.update(dice)
    result = combat.shoot_combatant(payload, context)
    assert "error" not in result
    return captured, result


def test_shoot_normalizes_bonus_and_penalty(monkeypatch, tmp_path):
    captured, result = _shoot_with_dice(
        monkeypatch, tmp_path, bonus_dice=2, penalty_dice=1
    )
    shot = result["shots"][0]
    assert (shot["bonus_dice"], shot["penalty_dice"]) == (1, 0)
    assert captured[0] == (1, 0)


# ---------------------------------------------------------------------------
# B14：体格差（目标 − 攻击者）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attacker_build,target_build,expected",
    [
        (0, -2, (2, 0)),
        (0, -1, (1, 0)),
        (0, 0, (0, 0)),
        (0, 1, (0, 1)),
        (0, 2, (0, 2)),
    ],
)
def test_shoot_build_difference(monkeypatch, tmp_path, attacker_build, target_build, expected):
    captured, _ = _shoot_with_dice(
        monkeypatch, tmp_path, attacker_build=attacker_build, target_build=target_build
    )
    assert captured[0] == expected


# ---------------------------------------------------------------------------
# 薄工具注册与战斗新工具
# ---------------------------------------------------------------------------
def test_thin_tools_registered():
    coc7_names = {tool.name for tool in coc7.COC7_EXTRA_TOOLS}
    assert {
        "check.combined",
        "check.group",
        "character.contacts",
        "character.spend",
        "character.experience_check",
        "character.train",
        "san.treat",
    } <= coc7_names
    combat_names = {tool.name for tool in combat.COMBAT_TOOLS}
    assert {"combat.knockout", "combat.hit_location"} <= combat_names


def test_knockout_and_hit_location_tools(tmp_path, monkeypatch):
    context = FakeContext(tmp_path)
    combat.start_combat({"participants": [_spec("A", side="player"), _spec("B")]}, context)
    assert "未开启" in combat.knockout_combatant({"target": "B"}, context)["error"]
    assert "未开启" in combat.hit_location_check({}, context)["error"]

    room_dir = tmp_path / "switched"
    room_dir.mkdir()
    context = FakeContext(
        room_dir, {"coc7_combat_knockout": True, "coc7_combat_hit_locations": True}
    )
    combat.start_combat({"participants": [_spec("A", side="player"), _spec("B")]}, context)
    monkeypatch.setattr(combat, "_roller", lambda sides: 7 if sides == 10 else 20)
    knocked = combat.knockout_combatant({"target": "B"}, context)
    assert knocked["knockout"]["unconscious"] is True
    target = next(row for row in knocked["order"] if row["name"] == "B")
    assert target["stunned"] is True
    assert target["knockout_rounds"] == 7
    location = combat.hit_location_check({"roll": 20, "major_wound": True}, context)
    assert location["hit_location"]["location"] == "左臂"
    assert "肢体" in location["major_wound_effect"]


# ---------------------------------------------------------------------------
# 批次 3：san.treat / 治疗记录
# ---------------------------------------------------------------------------
def test_san_treat_success_records_treatment(tmp_path):
    room_dir, info = _write_room(tmp_path, house_rules={"coc7_sanity_state": True})
    member = info["members"][0]
    result = room_sanity.treatment_session(
        room_dir, info, member, level="regular", months=2, rng=_queue_rng([2, 6, 3, 6])
    )
    assert result["treatment"]["delta"] == 5
    assert "SAN +5" in result["summary"]
    card = member["character_card"]
    assert card["currentSan"] == 55
    records = card["extensions"]["mental"]["treatment"]
    assert len(records) == 1
    assert records[0]["months"] == 2


def test_san_treat_fumble_loses_san(tmp_path):
    room_dir, info = _write_room(tmp_path, house_rules={"coc7_sanity_state": True})
    member = info["members"][0]
    result = room_sanity.treatment_session(
        room_dir, info, member, level="fumble", months=1, rng=_queue_rng([2, 5])
    )
    assert result["treatment"]["delta"] == -5
    assert member["character_card"]["currentSan"] == 45


def test_san_treat_command(tmp_path):
    room_dir, _ = _write_room(tmp_path, house_rules={"coc7_sanity_state": True})
    ok, text = handle_room_rule_command(
        ["san", "treat", "tester", "regular", "1"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is True, text
    assert "心理治疗" in text


# ---------------------------------------------------------------------------
# 批次 1：计划 §5 的命令写法 rule check --skills / --group
# ---------------------------------------------------------------------------
def test_rule_check_skills_alias_runs_combined(tmp_path):
    room_dir, _ = _write_room(
        tmp_path,
        house_rules={"coc7_combined_checks": True},
        skills=[{"name": "侦查", "value": 50}, {"name": "聆听", "value": 50}],
    )
    ok, text = handle_room_rule_command(
        ["check", "tester", "--skills=侦查,聆听", "--mode=all"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is True, text
    assert "组合" in text


def test_rule_check_group_alias_runs_group_luck(tmp_path):
    room_dir, _ = _write_room(tmp_path, house_rules={"coc7_group_luck": True})
    ok, text = handle_room_rule_command(
        ["check", "--group=luck"],
        room_id="r-1",
        room_dir=room_dir,
        actor="gamekeeper",
        actor_id="kp-id",
        actor_rank=3,
    )
    assert ok is True, text
    assert "幸运" in text
