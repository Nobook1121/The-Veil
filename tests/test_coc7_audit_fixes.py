"""审计阻断项修复的回归测试（CoC7 规则数学 / 战斗资源 / 统一写回）。"""

import json
import random
from pathlib import Path

from trpg_server import character_service, room_rules
from trpg_server.agents.tools import combat, dice
from trpg_server.app_factory import create_app
from trpg_server.ruleset import get_plugin
from trpg_server.ruleset.coc7 import rules as coc7_rules


def _attributes(**overrides):
    values = {"STR": 60, "DEX": 60, "CON": 60, "APP": 50, "POW": 50, "SIZ": 70, "EDU": 60, "INT": 60, "LUC": 50}
    values.update(overrides)
    return values


def _card(card_id="card-1", name="Alice", san=40, **extra):
    card = {
        "id": card_id,
        "name": name,
        "rulesetId": "coc7",
        "attributes": _attributes(),
        "maxSan": san,
        "initialSan": san,
        "currentSan": san,
        "skills": [{"name": "侦查", "value": 60}],
        "equipment": [],
        "weapons": [],
        "status": {},
        "armor": 0,
    }
    card.update(extra)
    return card


def _member(card=None, state=None, user_id=1, username="tester"):
    return {
        "user_id": user_id,
        "username": username,
        "role": "USER",
        "room_role": "member",
        "status": "active",
        "is_active": True,
        "character_card": card or _card(),
        "character_state": state or {},
    }


def _room_dir(tmp_path, info):
    room_dir = Path(tmp_path) / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _context(room_dir, info):
    return room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=1, username="tester")


# ---------------------------------------------------------------- SAN 上限


def test_san_cap_and_initial_value_come_from_pow_and_mythos():
    sheet = {"attributes": _attributes(POW=55), "skills": [{"name": "克苏鲁神话", "value": 20}]}

    derived = get_plugin("coc7").schema.derive(sheet)

    assert derived["initialSan"] == 55
    assert derived["maxSan"] == 79  # 99 - 20
    assert derived["currentSan"] == 55  # 新卡从 POW 起步，而不是直接拉到上限


def test_san_recovery_is_clamped_to_cap(tmp_path):
    card = _card(san=95)
    card["skills"].append({"name": "克苏鲁神话", "value": 5})
    member = _member(card, {"max_san": 94, "current_san": 94})
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)

    result = room_rules.apply_san_change(room_dir, info, member, delta=10, actor="tester", actor_id=1)

    assert result["san_after"] == 94  # 上限 99 - 5
    assert member["character_state"]["current_san"] == 94
    assert member["character_card"]["currentSan"] == 94


def test_mythos_increase_presses_existing_san_down(tmp_path):
    card = _card(san=90)
    card["skills"].append({"name": "克苏鲁神话", "value": 0})
    member = _member(card, {"max_san": 99, "current_san": 90, "current_hp": 10, "current_mp": 10})
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)

    room_rules.apply_skill_change(
        room_dir, info, member, name="克苏鲁神话", value=30, actor="tester", actor_id=1, reason="阅读魔典"
    )

    assert member["character_state"]["max_san"] == 69
    assert member["character_state"]["current_san"] == 69
    assert member["character_card"]["currentSan"] == 69
    types = [change["type"] for change in member["character_state"]["pending_changes"]]
    assert "skill" in types and "san" in types


def test_skill_value_is_capped_at_99(tmp_path):
    member = _member()
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)

    result = room_rules.apply_skill_change(room_dir, info, member, name="侦查", value=130, actor="tester", actor_id=1)

    assert result["after"] == 99
    assert member["character_card"]["skills"][0]["value"] == 99


# ---------------------------------------------------------------- SAN 100


def test_san_roll_of_100_uses_maximum_loss():
    card = _card(san=40)
    member = _member(card, {"max_san": 99, "current_san": 40})
    info = {"members": [member]}
    context = _context(None, info)

    result = dice.roll_sanity_check(
        {"player_name": "tester", "san_change": "1d4/1d10"}, context, faces=[0, 0]
    )

    assert result["roll"] == 100
    assert result["success"] is False
    assert result["change"] == -10  # 取 1d10 的最大值
    assert result["current_san"] == 30


# ---------------------------------------------------------------- 奖励 / 惩罚骰


def test_bonus_and_penalty_cancel_to_net_and_cap_at_two():
    assert dice._normalize_bonus_penalty(1, 1) == (0, 0)
    assert dice._normalize_bonus_penalty(3, 1) == (2, 0)
    assert dice._normalize_bonus_penalty(0, 4) == (0, 2)
    assert dice._normalize_bonus_penalty(2, 0) == (2, 0)


def test_bonus_tens_are_sampled_independently_with_duplicates_allowed():
    def roller(sides):
        return 5  # 个位 5；十位每次都掷出 5 -> (5-1)=4

    roll, candidates = dice._percentile_roll(roller, bonus=1)

    assert candidates == [45, 45]
    assert roll == 45


def test_bonus_and_penalty_change_roll_distribution_statistically():
    plain = random.Random(11)
    bonus = random.Random(11)
    penalty = random.Random(11)
    plain_mean = sum(dice._percentile_roll(lambda s: plain.randint(1, s))[0] for _ in range(4000)) / 4000
    bonus_mean = sum(dice._percentile_roll(lambda s: bonus.randint(1, s), 1, 0)[0] for _ in range(4000)) / 4000
    penalty_mean = sum(dice._percentile_roll(lambda s: penalty.randint(1, s), 0, 1)[0] for _ in range(4000)) / 4000

    assert bonus_mean < plain_mean - 8
    assert penalty_mean > plain_mean + 8


def test_coc_check_accepts_both_bonus_and_penalty():
    result = dice.roll_coc_check({"target": 50, "bonus_dice": 2, "penalty_dice": 1}, lambda sides: 1)

    assert "error" not in result
    assert result["bonus_dice"] == 1
    assert result["penalty_dice"] == 0


# ---------------------------------------------------------------- 不定性疯狂


def test_single_day_cumulative_loss_crosses_threshold_and_rolls_over(tmp_path):
    member = _member(_card(san=40), {"max_san": 99, "current_san": 40})
    info = {"members": [member], "game_date": "1925-03-01"}
    room_dir = _room_dir(tmp_path, info)

    room_rules.apply_san_change(room_dir, info, member, delta=-4, actor="tester", actor_id=1)
    assert member["character_state"]["san_daily"]["loss"] == 4
    assert member["character_state"].get("mental_status", {}).get("indefiniteInsanity") is not True

    room_rules.apply_san_change(room_dir, info, member, delta=-4, actor="tester", actor_id=1)
    daily = member["character_state"]["san_daily"]
    assert daily["loss"] == 8 and daily["threshold_met"] is True
    assert member["character_state"]["mental_status"]["indefiniteInsanity"] is True

    info["game_date"] = "1925-03-02"
    room_rules.apply_san_change(room_dir, info, member, delta=-4, actor="tester", actor_id=1)
    rolled = member["character_state"]["san_daily"]
    assert rolled["date"] == "1925-03-02"
    assert rolled["loss"] == 4  # 跨日重新累计
    assert rolled["threshold_met"] is False


def test_record_daily_loss_is_a_pure_rolling_helper():
    state = {}
    first = coc7_rules.record_daily_loss(state, day="1925-01-01", before=50, loss=10)
    second = coc7_rules.record_daily_loss(state, day="1925-01-01", before=40, loss=1)
    next_day = coc7_rules.record_daily_loss(state, day="1925-01-02", before=39, loss=1)

    assert first["threshold_met"] is True  # 10 / 50 = 20%
    assert second["loss"] == 11
    assert next_day["loss"] == 1 and next_day["base"] == 39


# ---------------------------------------------------------------- 派生值重算


def test_attribute_change_recalculates_derived_stats(tmp_path):
    card = _card(san=40)
    card["maxHp"] = 13
    card["currentHp"] = 13
    card["maxMp"] = 10
    card["currentMp"] = 10
    member = _member(card, {"max_hp": 13, "current_hp": 13, "max_san": 99, "current_san": 40, "max_mp": 10, "current_mp": 10})
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)

    result = room_rules.apply_attribute_change(room_dir, info, member, key="CON", value=80, actor="tester", actor_id=1)

    assert result["derived"]["max_hp"] == 15  # (80 + 70) // 10
    assert member["character_state"]["max_hp"] == 15
    assert member["character_card"]["maxHp"] == 15
    types = [change["type"] for change in member["character_state"]["pending_changes"]]
    assert "attr" in types


def test_derive_recomputes_mov_build_and_damage_bonus():
    derived = get_plugin("coc7").schema.derive({"attributes": _attributes(STR=60, SIZ=70, DEX=60)})

    assert derived["mov"] == 7  # STR/DEX 都低于 SIZ
    assert derived["build"] == 1  # STR+SIZ = 130
    assert derived["damageBonus"] == "+1D4"


# ---------------------------------------------------------------- 战斗资源


def test_combatant_reads_armor_san_and_mp_from_room_snapshot():
    card = _card(san=31, armor=3)
    member = _member(
        card,
        {"max_hp": 10, "current_hp": 10, "max_san": 99, "current_san": 31, "max_mp": 10, "current_mp": 4},
    )

    combatant = combat._combatant_from_member(member)

    assert combatant["armor"] == 3
    assert combatant["san"] == 31
    assert combatant["mp"] == 4
    assert combatant["max_mp"] == 10


def test_damage_is_reduced_by_armor_and_queues_hp_writeback(tmp_path):
    card = _card(san=31, armor=3)
    card["maxHp"] = 10
    card["currentHp"] = 10
    member = _member(
        card,
        {"max_hp": 10, "current_hp": 10, "max_san": 99, "current_san": 31, "max_mp": 10, "current_mp": 4},
    )
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)
    context = _context(room_dir, info)
    combatant = combat._combatant_from_member(member)

    result = combat._apply_damage(context, combatant, 8)

    assert result["armor"] == 3
    assert result["effective"] == 5
    assert combatant["hp"] == 5
    state = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))["members"][0]["character_state"]
    assert state["current_hp"] == 5
    kinds = [change["type"] for change in state["pending_changes"]]
    assert "hp" in kinds


def test_mp_is_not_reset_between_combats(tmp_path):
    card = _card(san=31, armor=3)
    member = _member(
        card,
        {"max_hp": 10, "current_hp": 10, "max_san": 99, "current_san": 31, "max_mp": 10, "current_mp": 4},
    )
    info = {"members": [member]}
    room_dir = _room_dir(tmp_path, info)
    context = _context(room_dir, info)

    combatant = combat._combatant_from_member(member)
    combatant["mp"] = 2
    combat._persist_player_state(context, combatant)

    rebuilt = combat._combatant_from_member(member)
    assert rebuilt["mp"] == 2


# ---------------------------------------------------------------- 统一写回


def _stored_card(tmp_path):
    card_dir = Path(tmp_path) / "characters" / "card-2"
    card_dir.mkdir(parents=True)
    stored = {
        "id": "card-2",
        "name": "Bob",
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": {key: 50 for key in ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")},
        "deriveAttributes": {
            "sanity": {"start": 50, "current": 50, "max": 50},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 10, "max": 10},
        },
        "skillGroups": {},
        "equipment": [],
        "weapons": [],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
        "battleAttributes": {"armor": "0"},
    }
    (card_dir / "character.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return card_dir / "character.json"


def test_room_changes_write_hp_mp_and_armor_to_persistent_card(tmp_path):
    from trpg_server.site_agent_auth import Actor, permission_set_for_role

    card_path = _stored_card(tmp_path)
    app = create_app(
        {
            "TESTING": True,
            "CONFIG_DIR": Path(tmp_path) / "config",
            "ROOMS_DIR": Path(tmp_path) / "rooms",
            "CHARACTERS_DIR": Path(tmp_path) / "characters",
        }
    )
    with app.app_context():
        actor = Actor(
            user_id=1,
            username="tester",
            role="ADMIN",
            permissions=permission_set_for_role("ADMIN"),
            room_ids=frozenset(),
        )
        character_service.apply_room_changes(
            actor,
            "card-2",
            [
                {"type": "hp", "value": 3},
                {"type": "mp", "value": 2},
                {"type": "armor", "value": 4},
            ],
        )

    saved = json.loads(card_path.read_text(encoding="utf-8"))
    assert int(saved["deriveAttributes"]["hp"]["current"]) == 3
    assert int(saved["deriveAttributes"]["mp"]["current"]) == 2
    assert int(saved["battleAttributes"]["armor"]) == 4


def test_fumble_threshold_derived_rule_only_when_not_customized():
    assert dice._success_level(100, 60) == "fumble"
    assert dice._success_level(96, 60) == "failure"  # 所需值 ≥50：默认只有 100 大失败
    assert dice._success_level(96, 60, 1, 90) == "fumble"  # 房规显式阈值优先
    assert dice._success_level(96, 40) == "fumble"  # 低于 50 沿用默认 96
