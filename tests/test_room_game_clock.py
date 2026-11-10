"""房间游戏时钟与依赖时间的规则：时钟存取、恢复默认天数、毒剂到期、幕间月结。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trpg_server import room_clock, room_interlude, room_magic
from trpg_server.agents.tools import combat
from trpg_server.agents.tools.time import game_clock_tool
from trpg_server.room_console_commands import execute_room_command
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import treatment


def _info(house_rules=None, *, hp=5, max_hp=10, mp=5, pow_value=50, san=50):
    return {
        "id": "clock-room",
        "name": "时钟房",
        "house_rules": house_rules or {},
        "members": [
            {
                "user_id": "p1-id",
                "username": "哈维",
                "is_active": True,
                "character_card": {
                    "name": "哈维",
                    "rulesetId": "coc7",
                    "attributes": {"pow": pow_value, "con": 50, "dex": 60, "int": 60},
                    "skills": [{"name": "克苏鲁神话", "value": 0}],
                },
                "character_state": {
                    "current_hp": hp,
                    "max_hp": max_hp,
                    "current_mp": mp,
                    "max_mp": pow_value // 5,
                    "current_san": san,
                    "max_san": 99,
                },
            }
        ],
    }


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "clock-room"
        self.config_dir = tmp_path / "config"
        self._house_rules = house_rules or {}

    def room_info(self):
        info = load_room_info(self.room_dir) or {}
        rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
        rules.update(self._house_rules)
        info["house_rules"] = rules
        return info


# ---------------------------------------------------------------------------
# 时钟存取
# ---------------------------------------------------------------------------
def test_clock_disabled_reads_none_and_never_writes(tmp_path):
    save_room_info(tmp_path, _info())
    info = load_room_info(tmp_path)
    assert room_clock.is_enabled(info) is False
    assert room_clock.read_clock(info) is None
    assert "game_clock" not in info
    with pytest.raises(room_clock.ClockError):
        room_clock.advance_clock(tmp_path, days=1)
    assert "game_clock" not in load_room_info(tmp_path)


def test_clock_advance_initializes_lazily_and_accumulates(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    assert room_clock.read_clock(load_room_info(tmp_path)) is None

    clock = room_clock.advance_clock(tmp_path, days=2, hours=3, actor="房主")
    assert clock["elapsed_seconds"] == 2 * 86400 + 3 * 3600
    assert room_clock.parse_moment(clock["current"]) > room_clock.parse_moment(clock["started_at"])

    again = room_clock.advance_clock(tmp_path, hours=1, actor="房主")
    assert again["elapsed_seconds"] == 2 * 86400 + 4 * 3600
    assert again["started_at"] == clock["started_at"]


def test_clock_set_is_forward_only_and_detects_conflict(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, hours=1, actor="房主")
    current = room_clock.parse_moment(clock["current"])

    target = room_clock.format_moment(current + timedelta(days=3))
    updated = room_clock.set_clock(tmp_path, target, actor="房主")
    assert updated["current"] == target

    with pytest.raises(room_clock.ClockError) as backwards:
        room_clock.set_clock(tmp_path, room_clock.format_moment(current - timedelta(days=1)))
    assert backwards.value.code == "clock_backwards"

    with pytest.raises(room_clock.ClockError) as conflict:
        room_clock.advance_clock(
            tmp_path, days=1, expected_current="2000-01-01T00:00:00+08:00"
        )
    assert conflict.value.code == "clock_conflict"


def test_clock_console_preview_then_confirm(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    preview = execute_room_command(
        "time advance 3d", room_id="clock-room", room_dir=tmp_path, actor_rank=3
    )
    assert preview.ok and "预演" in preview.message
    assert room_clock.read_clock(load_room_info(tmp_path)) is None

    applied = execute_room_command(
        "time advance 3d yes", room_id="clock-room", room_dir=tmp_path, actor_rank=3
    )
    assert applied.ok
    clock = room_clock.read_clock(load_room_info(tmp_path))
    assert clock["elapsed_seconds"] == 3 * 86400

    denied = execute_room_command(
        "time advance 1h yes", room_id="clock-room", room_dir=tmp_path, actor_rank=1
    )
    assert denied.ok is False


def test_read_stored_clock_survives_house_rule_removal(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    room_clock.advance_clock(tmp_path, days=1)
    info = load_room_info(tmp_path)
    stored = room_clock.read_stored_clock(info)
    assert stored["elapsed_seconds"] == 86400
    info.pop("house_rules", None)
    assert room_clock.read_clock(info) is None
    assert room_clock.read_stored_clock(info) is not None


def test_ai_tool_is_read_only(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    context = FakeContext(tmp_path)
    assert game_clock_tool({}, context)["initialized"] is False
    room_clock.advance_clock(tmp_path, days=1)
    payload = game_clock_tool({}, context)
    assert payload["enabled"] is True and payload["initialized"] is True
    assert "当前游戏时间" in payload["summary"]


# ---------------------------------------------------------------------------
# 恢复 / 典籍默认读时钟
# ---------------------------------------------------------------------------
def test_natural_heal_uses_clock_days_when_omitted(tmp_path):
    save_room_info(
        tmp_path, _info({"coc7_game_clock": True, "coc7_healing_natural": True})
    )
    room_clock.advance_clock(tmp_path, days=3, actor="房主")
    ok, message = handle_room_rule_command(
        ["heal", "natural", "哈维"],
        room_id="clock-room",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok, message
    assert "游戏时钟 3 天" in message
    assert load_room_info(tmp_path)["members"][0]["character_state"]["current_hp"] == 8
    marker = load_room_info(tmp_path)["members"][0]["character_state"]["last_natural_heal"]
    assert marker

    # 显式天数仍然优先。
    ok, message = handle_room_rule_command(
        ["heal", "natural", "哈维", "2"],
        room_id="clock-room",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok and "2 天" in message


def test_heal_without_clock_requires_explicit_days(tmp_path):
    save_room_info(tmp_path, _info({"coc7_healing_natural": True}))
    ok, message = handle_room_rule_command(
        ["heal", "natural", "哈维"],
        room_id="clock-room",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is False and "天数" in message


def test_mp_recovery_reads_clock_days(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    room_clock.advance_clock(tmp_path, days=2, actor="房主")
    ok, message = handle_room_rule_command(
        ["heal", "mp", "哈维"],
        room_id="clock-room",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok, message
    assert load_room_info(tmp_path)["members"][0]["character_state"]["current_mp"] == 10


def test_grimoire_weeks_default_to_clock_delta(tmp_path):
    info = _info({"coc7_game_clock": True})
    now = room_clock.server_now()
    info["game_clock"] = {
        "started_at": room_clock.format_moment(now - timedelta(days=30)),
        "current": room_clock.format_moment(now),
        "elapsed_seconds": 30 * 86400,
    }
    info["members"][0]["character_card"]["extensions"] = {
        "magic": {"grimoires": {"《蠕虫之秘》": {"last_read_at": room_clock.format_moment(now - timedelta(days=22))}}}
    }
    save_room_info(tmp_path, info)
    info, member = load_room_info(tmp_path), load_room_info(tmp_path)["members"][0]
    payload = room_magic.read_grimoire(
        tmp_path, info, member, title="《蠕虫之秘》", mode="study",
        weeks=None, san_loss=1, cmi=1, cmf=2, rating=5,
    )
    assert payload["weeks"] == 3


# ---------------------------------------------------------------------------
# 毒剂延迟发作
# ---------------------------------------------------------------------------
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
        "skills": {},
        "weapons": [],
    }
    spec.update(overrides)
    return spec


def test_delayed_poison_settles_when_round_is_due(tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat({"participants": [_spec("Target")]}, context)
    result = combat.poison_combatant(
        {"target": "Target", "damage": "1D6", "onset_rounds": 2}, context
    )
    assert result["pending"] is True
    assert combat._load_combat(context)["pending_poisons"]

    combat.next_turn({}, context)
    assert combat._load_combat(context)["pending_poisons"]
    combat.next_turn({}, context)
    state = combat._load_combat(context)
    assert state["pending_poisons"] == []
    assert state["combatants"]["Target"]["hp"] <= 20


def test_delayed_poison_settles_on_time_advance(tmp_path):
    save_room_info(tmp_path, _info({"coc7_combat_poison": True, "coc7_game_clock": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat({"participants": [_spec("Target")]}, context)
    combat.poison_combatant(
        {"target": "Target", "damage": "1D6", "onset_rounds": 99}, context
    )
    settled = combat.settle_poisons_after_time(tmp_path, room_id="clock-room")
    assert settled["settled"] == 1
    assert combat._load_combat(context)["pending_poisons"] == []


# ---------------------------------------------------------------------------
# 幕间月结
# ---------------------------------------------------------------------------
def _interlude_info(**rules):
    info = _info(rules)
    info["interlude"] = {
        "active": True,
        "startedAt": "2026-11-01 10:00:00",
        "startedBy": "房主",
        "completedAt": None,
        "settlement": {},
        "reviews": [],
    }
    info["house_rules"].update(rules)
    now = room_clock.server_now()
    info["game_clock"] = {
        "started_at": room_clock.format_moment(now - timedelta(days=60)),
        "current": room_clock.format_moment(now),
        "elapsed_seconds": 60 * 86400,
    }
    return info


def test_monthly_care_settles_hp_and_mp(tmp_path):
    save_room_info(tmp_path, _interlude_info(coc7_game_clock=True))
    info = load_room_info(tmp_path)
    care = room_interlude.apply_monthly_care(tmp_path, info, months=1)
    assert care["months"] == 1 and care["days"] == 30
    member = load_room_info(tmp_path)["members"][0]
    assert member["character_state"]["current_hp"] == 10
    assert member["character_state"]["current_mp"] == 10
    assert load_room_info(tmp_path)["interlude"]["settlement"]["care"]["months"] == 1


def test_therapy_settles_san_month_by_month(tmp_path):
    save_room_info(tmp_path, _interlude_info(coc7_game_clock=True))
    info = load_room_info(tmp_path)
    record = room_interlude.apply_therapy(
        tmp_path,
        info,
        player="哈维",
        months=1,
        results=["regular"],
        rng=lambda _n: 2,
    )
    assert record["san_delta"] == 2
    assert load_room_info(tmp_path)["members"][0]["character_state"]["current_san"] == 52


def test_indefinite_duration_is_one_to_six_months():
    assert treatment.indefinite_months(1) == 1
    assert treatment.indefinite_months(9) == 6
    assert treatment.monthly_san_delta("fumble", d6=6) == -6
    assert treatment.monthly_san_delta("failure") == 0
