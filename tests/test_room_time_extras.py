"""房间时间模型补完：文件锁、time correct、战斗外毒剂队列、SAN 到期钩子。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from trpg_server import room_clock, room_poison, room_sanity
from trpg_server.agents.tools import combat
from trpg_server.room_console_commands import execute_room_command
from trpg_server.room_log import read_room_log
from trpg_server.room_rules import load_room_info, save_room_info


def _info(house_rules=None):
    return {
        "id": "extra-room",
        "name": "补完房",
        "house_rules": house_rules or {},
        "members": [
            {
                "user_id": "p1-id",
                "username": "哈维",
                "is_active": True,
                "character_card": {
                    "name": "哈维",
                    "rulesetId": "coc7",
                    "attributes": {"CON": 50, "POW": 50, "DEX": 60, "STR": 50, "SIZ": 50},
                    "skills": [],
                },
                "character_state": {
                    "current_hp": 10,
                    "max_hp": 10,
                    "current_mp": 10,
                    "max_mp": 10,
                    "current_san": 50,
                    "max_san": 99,
                },
            }
        ],
    }


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "extra-room"
        self.config_dir = tmp_path / "config"
        self._house_rules = house_rules or {}

    def room_info(self):
        info = load_room_info(self.room_dir) or {}
        rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
        rules.update(self._house_rules)
        info["house_rules"] = rules
        return info


# ---------------------------------------------------------------------------
# 文件锁
# ---------------------------------------------------------------------------
def test_file_lock_is_released_after_advance(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    room_clock.advance_clock(tmp_path, days=1, actor="房主")
    assert not (tmp_path / room_clock.LOCK_FILENAME).exists()


def test_stale_file_lock_is_reclaimed(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    lock = tmp_path / room_clock.LOCK_FILENAME
    lock.write_text("dead-process", encoding="utf-8")
    stale = os.path.getmtime(lock) - room_clock.LOCK_STALE_SECONDS - 10
    os.utime(lock, (stale, stale))

    clock = room_clock.advance_clock(tmp_path, days=1, actor="房主")
    assert clock["elapsed_seconds"] == 86400
    assert not lock.exists()


def test_fresh_file_lock_raises_clock_busy(tmp_path, monkeypatch):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    lock = tmp_path / room_clock.LOCK_FILENAME
    lock.write_text("busy-process", encoding="utf-8")
    monkeypatch.setattr(room_clock, "LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(room_clock, "LOCK_POLL_SECONDS", 0.01)

    with pytest.raises(room_clock.ClockError) as caught:
        room_clock.advance_clock(tmp_path, days=1, actor="房主")
    assert caught.value.code == "clock_busy"
    assert lock.exists()


# ---------------------------------------------------------------------------
# time correct
# ---------------------------------------------------------------------------
def test_correct_allows_rewind_and_records_audit(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, days=2, actor="房主")
    before = room_clock.parse_moment(clock["current"])
    target = before - timedelta(days=1)

    updated = room_clock.correct_clock(
        tmp_path,
        room_clock.format_moment(target),
        actor="房主",
        actor_id="owner-id",
        reason="上一幕时间记错，回拨一天",
    )
    assert room_clock.parse_moment(updated["current"]) == target
    assert updated["elapsed_seconds"] == 86400
    record = updated["corrections"][-1]
    assert record["before"] == clock["current"]
    assert record["after"] == room_clock.format_moment(target)
    assert record["delta_seconds"] == -86400
    assert record["actor"] == "房主" and record["actor_id"] == "owner-id"
    assert "回拨" in record["reason"]

    entries = read_room_log(tmp_path)["entries"]
    assert any("游戏时间校正" in entry.get("message", "") for entry in entries)


def test_correct_requires_reason(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    room_clock.advance_clock(tmp_path, days=1, actor="房主")
    with pytest.raises(room_clock.ClockError) as caught:
        room_clock.correct_clock(tmp_path, room_clock.format_moment(room_clock.server_now()))
    assert caught.value.code == "reason_required"


def test_correct_console_is_owner_only_and_needs_yes(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, days=3, actor="房主")
    moment = room_clock.format_moment(room_clock.parse_moment(clock["current"]) - timedelta(days=1))

    denied = execute_room_command(
        f"time correct {moment} reason 修正", room_id="extra-room", room_dir=tmp_path, actor_rank=2
    )
    assert denied.ok is False and "房主" in denied.message

    preview = execute_room_command(
        f"time correct {moment} reason 修正", room_id="extra-room", room_dir=tmp_path, actor_rank=3
    )
    assert preview.ok and "预演" in preview.message
    assert room_clock.read_clock(load_room_info(tmp_path))["current"] == clock["current"]

    applied = execute_room_command(
        f"time correct {moment} reason 修正 yes",
        room_id="extra-room",
        room_dir=tmp_path,
        actor_rank=3,
        actor="房主",
    )
    assert applied.ok, applied.message
    assert room_clock.read_clock(load_room_info(tmp_path))["current"] == moment


def test_correct_console_confirms_once(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, days=1, actor="房主")
    moment = room_clock.format_moment(room_clock.parse_moment(clock["current"]) - timedelta(hours=1))
    prompts: list[str] = []

    def confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    applied = execute_room_command(
        f"time correct {moment} reason 测试",
        room_id="extra-room",
        room_dir=tmp_path,
        actor_rank=3,
        confirm=confirm,
    )
    assert applied.ok, applied.message
    assert len(prompts) == 1
    assert room_clock.read_clock(load_room_info(tmp_path))["current"] == moment


def test_correct_console_rejects_dash_prefix(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, days=1, actor="房主")

    result = execute_room_command(
        f"time advance 1d --yes", room_id="extra-room", room_dir=tmp_path, actor_rank=3
    )
    assert result.ok is False and "--" in result.message
    assert room_clock.read_clock(load_room_info(tmp_path))["current"] == clock["current"]

    correct = execute_room_command(
        f"time correct {clock['current']} reason 测试 --yes",
        room_id="extra-room",
        room_dir=tmp_path,
        actor_rank=3,
    )
    assert correct.ok is False and "--" in correct.message


# ---------------------------------------------------------------------------
# 战斗外毒剂队列
# ---------------------------------------------------------------------------
def test_narrative_poison_registers_and_settles_on_time(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True, "coc7_combat_poison": True}))
    room_clock.advance_clock(tmp_path, hours=1, actor="房主")
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})

    added = combat.poison_queue(
        {"action": "add", "target": "哈维", "damage": "1D3", "onset_hours": 2}, context
    )
    assert added.get("pending") is True
    assert len(room_poison.load_queue(tmp_path)) == 1

    early = combat.settle_poisons_after_time(tmp_path, room_id="extra-room")
    assert early["settled"] == 0

    room_clock.advance_clock(tmp_path, hours=3, actor="房主")
    settled = combat.settle_poisons_after_time(tmp_path, room_id="extra-room")
    assert settled["settled"] == 1
    assert any("毒剂发作" in text for text in settled["messages"])
    assert room_poison.load_queue(tmp_path) == []
    assert combat.poison_queue({"action": "list"}, context)["summary"] == "战斗外毒剂队列为空。"


def test_narrative_poison_round_based_settles_on_combat_turn(tmp_path):
    save_room_info(tmp_path, _info({"coc7_combat_poison": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat(
        {"participants": ["哈维", {"name": "Target", "is_npc": True, "side": "enemy", "dex": 50, "hp": 20}]},
        context,
    )
    added = combat.poison_queue(
        {"action": "add", "target": "哈维", "damage": "1D6+3", "onset_rounds": 1}, context
    )
    assert added["entry"]["due_round"] == 2

    combat.next_turn({}, context)
    combat.next_turn({}, context)
    assert room_poison.load_queue(tmp_path) == []
    # 战斗内结算只写战斗快照（persist=False），伤害应体现在 combatant 面板。
    # 1D6+3 即使抵抗减半再扣护甲也至少 1 点，因此结果必定小于初始 10。
    panel = combat.combat_state({}, context)
    hp_in_panel = next(item["hp"] for item in panel["order"] if item["name"] == "哈维")
    assert 1 <= hp_in_panel <= 9


def test_narrative_poison_round_mode_requires_combat(tmp_path):
    save_room_info(tmp_path, _info({"coc7_combat_poison": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    result = combat.poison_queue(
        {"action": "add", "target": "哈维", "damage": "1D3", "onset_rounds": 2}, context
    )
    assert "error" in result and "战斗" in result["error"]


def test_narrative_poison_in_combat_does_not_roll_back(tmp_path):
    """战斗内结算战斗外毒剂：伤害落在 combatant 上，后续动作不会回滚。"""
    save_room_info(tmp_path, _info({"coc7_combat_poison": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat(
        {
            "participants": [
                "哈维",
                {"name": "Target", "is_npc": True, "side": "enemy", "dex": 40, "hp": 20},
            ]
        },
        context,
    )
    added = combat.poison_queue(
        {"action": "add", "target": "哈维", "damage": "1D6+3", "onset_rounds": 1}, context
    )
    assert added["entry"]["due_round"] == 2

    combat.next_turn({}, context)
    combat.next_turn({}, context)
    panel = combat.combat_state({}, context)
    hp_in_panel = next(item["hp"] for item in panel["order"] if item["name"] == "哈维")
    assert hp_in_panel < 10
    assert room_poison.load_queue(tmp_path) == []

    # 再走一次战斗动作：旧实现会从旧 combatant 回写，把毒剂伤害顶回 10。
    combat.damage_combatant({"target": "哈维", "amount": 1, "ignore_armor": True}, context)
    panel = combat.combat_state({}, context)
    hp_after = next(item["hp"] for item in panel["order"] if item["name"] == "哈维")
    assert hp_after == hp_in_panel - 1
    member = load_room_info(tmp_path)["members"][0]
    assert member["character_state"]["current_hp"] == hp_after


def test_narrative_poison_single_failure_does_not_double_charge(tmp_path, monkeypatch):
    """单条结算抛错时丢弃该条，其余条目只扣一次血，重试不再扣。"""
    info = _info({"coc7_game_clock": True, "coc7_combat_poison": True})
    info["members"].append(
        {
            "user_id": "p2-id",
            "username": "露娜",
            "is_active": True,
            "character_card": {
                "name": "露娜",
                "rulesetId": "coc7",
                "attributes": {"CON": 50, "POW": 50, "DEX": 50, "STR": 50, "SIZ": 50},
                "skills": [],
            },
            "character_state": {"current_hp": 10, "max_hp": 10, "current_mp": 10, "max_mp": 10},
        }
    )
    save_room_info(tmp_path, info)
    clock = room_clock.advance_clock(tmp_path, hours=2, actor="房主")
    past = room_clock.format_moment(room_clock.parse_moment(clock["current"]) - timedelta(hours=1))
    room_poison.register(tmp_path, target="哈维", damage="1D6+3", due_at=past)
    room_poison.register(tmp_path, target="露娜", damage="5", due_at=past)

    original = combat._resolve_poison

    def flaky(combat_obj, target, **kwargs):
        if target.get("name") == "露娜":
            raise RuntimeError("boom")
        return original(combat_obj, target, **kwargs)

    monkeypatch.setattr(combat, "_resolve_poison", flaky)
    result = combat.settle_poisons_after_time(tmp_path, room_id="extra-room")
    assert any("结算失败，已丢弃" in text for text in result["messages"])
    assert room_poison.load_queue(tmp_path) == []

    hp_once = load_room_info(tmp_path)["members"][0]["character_state"]["current_hp"]
    assert hp_once < 10
    assert load_room_info(tmp_path)["members"][1]["character_state"]["current_hp"] == 10

    # 重试：队列已清空，不会再扣一次。
    monkeypatch.setattr(combat, "_resolve_poison", original)
    again = combat.settle_poisons_after_time(tmp_path, room_id="extra-room")
    assert again["settled"] == 0
    assert load_room_info(tmp_path)["members"][0]["character_state"]["current_hp"] == hp_once


def test_narrative_poison_is_claimed_before_damage(tmp_path, monkeypatch):
    """到期项在结算伤害前就已从队列移出，另一个结算方不会重复认领。"""
    save_room_info(tmp_path, _info({"coc7_game_clock": True, "coc7_combat_poison": True}))
    clock = room_clock.advance_clock(tmp_path, hours=2, actor="房主")
    past = room_clock.format_moment(room_clock.parse_moment(clock["current"]) - timedelta(hours=1))
    entry = room_poison.register(tmp_path, target="哈维", damage="5", due_at=past)

    seen: list[list[str]] = []
    original = combat._resolve_poison

    def spy(combat_obj, target, **kwargs):
        seen.append([item["id"] for item in room_poison.load_queue(tmp_path)])
        return original(combat_obj, target, **kwargs)

    monkeypatch.setattr(combat, "_resolve_poison", spy)
    combat.settle_poisons_after_time(tmp_path, room_id="extra-room")
    assert seen == [[]]
    assert entry["id"] not in [item["id"] for item in room_poison.load_queue(tmp_path)]


def test_end_combat_clears_round_poison_entries(tmp_path):
    """战斗结束清掉按回合的毒剂；新战斗开始也不捡旧数据。"""
    save_room_info(tmp_path, _info({"coc7_combat_poison": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat(
        {
            "participants": [
                "哈维",
                {"name": "Target", "is_npc": True, "side": "enemy", "dex": 40, "hp": 20},
            ]
        },
        context,
    )
    added = combat.poison_queue(
        {"action": "add", "target": "哈维", "damage": "1D3", "onset_rounds": 3}, context
    )
    assert added["entry"]["due_round"] == 4
    assert added["entry"]["combat_id"]

    ended = combat.end_combat({}, context)
    assert ended["active"] is False
    assert room_poison.load_queue(tmp_path) == []

    room_poison.register(tmp_path, target="哈维", damage="1D3", due_round=2, combat_id="old")
    assert len(room_poison.load_queue(tmp_path)) == 1
    combat.start_combat({"participants": ["哈维"]}, context)
    assert room_poison.load_queue(tmp_path) == []


def test_poison_queue_rejects_mixed_onset(tmp_path):
    save_room_info(tmp_path, _info({"coc7_game_clock": True, "coc7_combat_poison": True}))
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    result = combat.poison_queue(
        {"action": "add", "target": "哈维", "onset_rounds": 1, "onset_hours": 2}, context
    )
    assert "error" in result and "二选一" in result["error"]


# ---------------------------------------------------------------------------
# SAN 到期钩子
# ---------------------------------------------------------------------------
def test_time_advance_calls_expire_bouts_hook(tmp_path, monkeypatch):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    calls: list[dict] = []

    def fake_expire(*, info=None, now=None):
        calls.append({"info": info, "now": now})
        return {"messages": ["不定性疯狂到期：哈维"]}

    monkeypatch.setattr(room_sanity, "expire_bouts", fake_expire, raising=False)
    result = execute_room_command(
        "time advance 1d yes", room_id="extra-room", room_dir=tmp_path, actor_rank=3
    )
    assert result.ok, result.message
    assert "不定性疯狂到期" in result.message
    assert calls and calls[0]["now"] is not None


def test_time_advance_without_hook_still_works(tmp_path, monkeypatch):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    monkeypatch.delattr(room_sanity, "expire_bouts", raising=False)
    result = execute_room_command(
        "time advance 1d yes", room_id="extra-room", room_dir=tmp_path, actor_rank=3
    )
    assert result.ok, result.message
    assert room_clock.read_clock(load_room_info(tmp_path))["elapsed_seconds"] == 86400


def test_correct_does_not_settle(tmp_path, monkeypatch):
    save_room_info(tmp_path, _info({"coc7_game_clock": True}))
    clock = room_clock.advance_clock(tmp_path, days=2, actor="房主")
    moment = room_clock.format_moment(room_clock.parse_moment(clock["current"]) - timedelta(days=1))
    calls: list[str] = []
    monkeypatch.setattr(
        room_sanity, "expire_bouts", lambda **_: calls.append("called"), raising=False
    )
    result = execute_room_command(
        f"time correct {moment} reason 回拨 yes",
        room_id="extra-room",
        room_dir=tmp_path,
        actor_rank=3,
    )
    assert result.ok, result.message
    assert calls == []
