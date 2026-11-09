"""批次 1 运行时接线测试：统一检定入口、房间命令门控与建卡年龄增量。"""

from __future__ import annotations

from trpg_server import check_service
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import save_room_info
from trpg_server.ruleset.coc7.checks import Coc7Checks


def _rng(value: int):
    def roll(sides: int) -> int:
        assert 1 <= value <= sides
        return value

    return roll


def _card() -> dict:
    return {
        "name": "调查员",
        "attributes": {
            "STR": 50,
            "CON": 50,
            "DEX": 50,
            "SIZ": 50,
            "APP": 50,
            "INT": 50,
            "POW": 50,
            "EDU": 60,
            "LUC": 40,
        },
        "skills": [
            {"name": "说服", "value": 60},
            {"name": "侦查", "value": 50},
            {"name": "母语", "value": 80},
            {"name": "信用评级", "value": 41},
        ],
        "assets": {"cash": 82, "spendingLevel": 0},
    }


def _member(username: str = "玩家甲") -> dict:
    return {"user_id": f"{username}-id", "username": username, "character_card": _card()}


def test_social_check_auto_success_skips_roll():
    result = check_service.run_room_social_check(
        _member(),
        player_name="玩家甲",
        name="说服",
        target_card={"name": "NPC", "skills": [{"name": "说服", "value": 90}]},
        attitude="supportive",
    )
    assert result["auto_success"] is True
    assert result["roll"] is None


def test_social_check_uses_plugin_difficulty_and_single_roll():
    result = check_service.run_room_social_check(
        _member(),
        player_name="玩家甲",
        name="说服",
        rng=_rng(1),
    )
    assert result["social"]["difficulty"] == "regular"
    assert result["roll"] == 1
    assert result["success"] is True


def test_combined_check_rolls_once_for_all_skills():
    result = check_service.run_room_combined_check(
        _member(),
        player_name="玩家甲",
        names=["说服", "侦查"],
        mode="all",
        difficulty="regular",
        rng=_rng(10),
    )
    assert result["roll"] == 10
    assert [row["success"] for row in result["skills"]] == [True, True]
    assert result["success"] is True


def test_group_luck_picks_lowest_luck_member():
    high = _member("甲")
    low = _member("乙")
    high["character_card"]["attributes"]["LUC"] = 80
    low["character_card"]["attributes"]["LUC"] = 30
    result = check_service.run_room_group_luck([high, low], rng=_rng(20))
    assert result["player_name"] == "乙"
    assert result["luck"] == 30
    assert result["success"] is True


def test_contact_check_and_outcome_are_merged():
    result = check_service.run_room_contact_check(
        _member(),
        player_name="玩家甲",
        name="母语",
        remote=True,
        rng=_rng(30),
    )
    assert result["contact"]["difficulty"] == "hard"
    assert result["contact"]["state"] == "contact"


def test_economy_is_read_only_and_does_not_touch_card():
    member = _member()
    result = check_service.run_room_economy(member, amount=40)
    assert result["credit"] == 41
    assert result["expense"]["within_standard"] is False
    assert result["expense"]["cash"] == 42
    assert member["character_card"]["assets"]["cash"] == 82


def test_rule_subcommands_are_absent_when_switch_is_off(tmp_path):
    info = {"room_id": "r1", "house_rules": {}, "members": [_member()]}
    save_room_info(tmp_path, info)

    ok, text = handle_room_rule_command(
        ["social", "玩家甲", "说服"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is False
    assert "未知的规则命令" in text


def test_rule_social_works_when_switch_is_on(tmp_path):
    info = {
        "room_id": "r1",
        "house_rules": {"coc7_social_difficulty": True},
        "members": [_member()],
    }
    save_room_info(tmp_path, info)

    ok, text = handle_room_rule_command(
        ["social", "玩家甲", "说服"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is True
    assert "社交检定" in text


def test_age_adjustment_allocates_two_pools_and_reports_deltas():
    checks = Coc7Checks()
    young = checks.apply_age_adjustment(
        17,
        str_siz_allocation={"STR": 2, "SIZ": 3},
        edu_gain=4,
    )
    assert young["deltas"]["STR"] == -2
    assert young["deltas"]["SIZ"] == -3
    assert young["deltas"]["EDU"] == -1  # 年龄段 -5，加上 4 点增强

    middle = checks.apply_age_adjustment(
        45,
        str_con_dex_allocation={"STR": 1, "CON": 2, "DEX": 2},
        edu_gain=0,
    )
    assert middle["deltas"] == {"STR": -1, "SIZ": 0, "CON": -2, "DEX": -2, "APP": -5, "EDU": 0}
