"""批次 1 纯规则计算的单元测试：社交难度、组合技能、团体幸运、经济、熟人、年龄。"""

from __future__ import annotations

import pytest

from trpg_server.ruleset.coc7.combined import resolve_combined_check
from trpg_server.ruleset.coc7.contacts import (
    assist_bonus,
    contact_check,
    contact_difficulty,
    contact_outcome,
)
from trpg_server.ruleset.coc7.creation import (
    aging_effects,
    allocate_reduction,
    creation_age_effects,
    resolve_edu_enhancement,
)
from trpg_server.ruleset.coc7.economy import (
    apply_credit_event,
    apply_expense,
    credit_profile,
    refresh_economy,
)
from trpg_server.ruleset.coc7.group import group_luck_check, pick_lowest_luck
from trpg_server.ruleset.coc7.options import Coc7RuleOptions, resolve_options
from trpg_server.ruleset.coc7.social import defense_skill, social_difficulty


def _rng(values):
    queue = iter(values)

    def roll(sides: int) -> int:
        value = next(queue)
        assert 1 <= value <= sides
        return value

    return roll


def _card(**skills) -> dict:
    return {
        "name": "调查员",
        "attributes": {"STR": 50, "DEX": 50, "LUCK": 0},
        "skills": [{"name": name, "value": value} for name, value in skills.items()],
    }


def test_options_default_off_and_parse_house_rules():
    options = Coc7RuleOptions()
    assert not any(
        (
            options.social_difficulty,
            options.combined_checks,
            options.group_luck,
            options.economy,
            options.contacts,
            options.age_adjustments,
        )
    )
    resolved = resolve_options(
        {"house_rules": {"coc7_social_difficulty": "on", "coc7_economy": True}}
    )
    assert resolved.social_difficulty is True
    assert resolved.economy is True
    assert resolved.contacts is False
    assert resolve_options({}).combined_checks is False


def test_social_difficulty_uses_target_skill_and_attitude():
    weak = _card(心理学=45)
    assert social_difficulty(weak)["difficulty"] == "regular"

    trained = _card(心理学=50)
    assert social_difficulty(trained)["difficulty"] == "hard"

    expert = _card(心理学=90)
    assert social_difficulty(expert)["difficulty"] == "extreme"

    assert social_difficulty(_card(心理学=45), attitude="hostile")["difficulty"] == "extreme"
    assert social_difficulty(_card(心理学=45), attitude="hostile", leverage=True)["difficulty"] == "hard"
    assert social_difficulty(_card(心理学=90), leverage=True)["difficulty"] == "hard"

    supportive = social_difficulty(_card(心理学=90), attitude="supportive")
    assert supportive["auto_success"] is True
    assert supportive["difficulty"] == "auto"


def test_defense_skill_takes_highest_of_social_and_psychology():
    card = _card(取悦=30, 说服=70, 心理学=55)
    assert defense_skill(card) == {"skill": "说服", "value": 70}


def test_combined_check_rolls_once_and_compares_every_skill():
    card = _card(**{"机械维修": 40, "电气维修": 60})

    both = resolve_combined_check(card, ["机械维修", "电气维修"], roll=7, mode="all")
    assert both["success"] is True
    assert [row["success"] for row in both["skills"]] == [True, True]
    assert both["skills"][0]["level"] == "extreme"

    partial = resolve_combined_check(card, ["机械维修", "电气维修"], roll=45, mode="all")
    assert partial["success"] is False
    assert [row["success"] for row in partial["skills"]] == [False, True]

    any_mode = resolve_combined_check(card, ["机械维修", "电气维修"], roll=45, mode="any")
    assert any_mode["success"] is True
    assert any_mode["mode"] == "any"

    hard = resolve_combined_check(
        card, ["机械维修", "电气维修"], roll=25, mode="all", difficulty="hard"
    )
    assert [row["threshold"] for row in hard["skills"]] == [20, 30]
    assert hard["success"] is False


def test_combined_check_rejects_unknown_skill():
    with pytest.raises(ValueError):
        resolve_combined_check(_card(侦察=50), ["潜行"], roll=10)


def test_group_luck_uses_lowest_active_member():
    members = [
        {"username": "高幸运", "character_card": {"attributes": {"LUC": 80}}},
        {"username": "已离场", "is_active": False, "character_card": {"attributes": {"LUC": 10}}},
        {"username": "低幸运", "character_card": {"attributes": {"LUC": 35}}},
    ]
    chosen = pick_lowest_luck(members)
    assert chosen["username"] == "低幸运"

    result = group_luck_check(members, roll=20)
    assert result["player_name"] == "低幸运"
    assert result["luck"] == 35
    assert result["success"] is True

    with pytest.raises(ValueError):
        group_luck_check([], roll=20)


def test_credit_profile_matches_book_tables():
    standard = credit_profile(41, "1920s")
    assert standard["key"] == "standard"
    assert standard["cash"] == 82
    assert standard["assets"] == 2050
    assert standard["spending"] == 10

    modern = credit_profile(41, "modern")
    assert modern["cash"] == 1640
    assert modern["assets"] == 41000
    assert modern["spending"] == 200

    assert credit_profile(0, "1920s")["cash"] == 0.5
    assert credit_profile(99, "1920s") == {
        "era": "1920s",
        "key": "super_rich",
        "label": "富豪",
        "credit": 99,
        "cash": 50000.0,
        "assets": 5_000_000.0,
        "spending": 5000.0,
    }


def test_expense_keeps_cash_within_standard_and_uses_assets_above():
    within = apply_expense(cash=82, assets=1665, amount=8, cr=41)
    assert within["within_standard"] is True
    assert within["cash"] == 82

    over = apply_expense(cash=82, assets=1665, amount=40, cr=41)
    assert over["within_standard"] is False
    assert over["cash"] == 42
    assert over["assets"] == 1665
    assert over["spent_from_cash"] == 40

    short = apply_expense(cash=42, assets=1665, amount=120, cr=41)
    assert short["cash"] == 0
    assert short["assets"] == 1587
    assert short["uncovered"] == 0

    broke = apply_expense(cash=10, assets=20, amount=120, cr=41)
    assert broke["uncovered"] == 90


def test_credit_events_follow_interlude_table():
    assert apply_credit_event(41, "promotion", rng=_rng([4]))["to"] == 45
    assert apply_credit_event(41, "steady", rng=_rng([]))["to"] == 41
    assert apply_credit_event(3, "belt_tightening", rng=_rng([7]))["to"] == 0
    assert apply_credit_event(10, "bankruptcy", rng=_rng([100]))["to"] == 0

    inherited = apply_credit_event(
        41, "inherited", rng=_rng([4, 3, 9]), target_assets=30000
    )
    assert inherited["to"] == 57
    assert inherited["profile"]["key"] == "comfortable"

    supported = apply_credit_event(
        10, "lost_income", rng=_rng([8, 7, 6]), has_social_security=True
    )
    assert supported["to"] == 5
    assert supported["floor"] == 5


def test_refresh_economy_only_refreshes_cash_when_assets_known():
    refreshed = refresh_economy(41, current_assets=50)
    assert refreshed["cash"] == 82
    assert refreshed["assets"] == 50


def test_contact_difficulty_location_and_relation():
    assert contact_difficulty()["difficulty"] == "regular"
    assert contact_difficulty(remote=True)["difficulty"] == "hard"
    assert contact_difficulty(remote=True, in_hometown=True, same_profession=True)["difficulty"] == "regular"
    assert contact_difficulty(base_difficulty="extreme", remote=True)["difficulty"] == "extreme"
    assert contact_difficulty()["dark_roll_on_push"] is True


def test_contact_check_and_outcome():
    card = _card(母语=84)
    plan = contact_check(card, "母语", remote=True)
    assert plan["difficulty"] == "hard"
    assert plan["threshold"] == 42

    assert contact_outcome(True)["state"] == "contact"
    failed = contact_outcome(False, pushed=True)
    assert failed["state"] == "hostile_contact"
    assert failed["dark_roll"] is True

    options = Coc7RuleOptions(contacts=True)
    assert assist_bonus({"friends": ["报社编辑"]}, options) == 1
    assert assist_bonus({"friends": []}, options) == 0
    assert assist_bonus({"friends": ["报社编辑"]}, Coc7RuleOptions()) == 0


def test_creation_age_bands():
    young = creation_age_effects(17)
    assert young["str_siz_total"] == -5
    assert young["edu_points"] == -5
    assert young["luck_rolls"] == 2

    adult = creation_age_effects(25)
    assert adult["edu_checks"] == 1
    assert adult["str_con_dex_total"] == 0

    middle = creation_age_effects(41)
    assert middle["band"] == "40-49"
    assert middle["str_con_dex_total"] == -5
    assert middle["app"] == -5
    assert middle["mov"] == -1
    assert middle["edu_checks"] == 2
    assert middle["reduction_total"] == 5

    senior = creation_age_effects(82)
    assert senior["str_con_dex_total"] == -80
    assert senior["app"] == -25
    assert senior["mov"] == -5


def test_allocation_must_use_exact_total():
    assert allocate_reduction(5, {"STR": 3, "CON": 2}) == {"STR": 3, "CON": 2, "DEX": 0}
    with pytest.raises(ValueError):
        allocate_reduction(5, {"STR": 3})
    with pytest.raises(ValueError):
        allocate_reduction(5, {"APP": 5})


def test_edu_enhancement_rolls_against_current_value_and_caps_at_99():
    result = resolve_edu_enhancement(40, attempts=2, rng=_rng([50, 5, 30, 3]))
    assert result["to"] == 45
    assert result["attempts"] == [{"roll": 50, "gain": 5}, {"roll": 30, "gain": 0}]

    capped = resolve_edu_enhancement(98, attempts=2, rng=_rng([99, 10]))
    assert capped["to"] == 99
    assert capped["checks"] == 1


def test_aging_crosses_milestones_cumulatively_and_skips_edu_after_60():
    middle = aging_effects(39, 52)
    assert [item["age"] for item in middle["milestones"]] == [40, 50]
    assert middle["str_con_dex_total"] == -10
    assert middle["app"] == -10
    assert middle["mov"] == -2
    assert middle["edu_checks"] == 2
    assert middle["reduction_total"] == 10

    old = aging_effects(79, 81)
    assert old["str_con_dex_total"] == -40
    assert old["app"] == -5
    assert old["edu_checks"] == 0

    magical = aging_effects(85, 95, grant_edu=False)
    assert magical["str_con_dex_total"] == -80
    assert magical["mov"] == -1
    assert magical["edu_checks"] == 0


def test_house_rule_definitions_expose_off_by_default():
    from trpg_server.routes.rooms import HOUSE_RULE_DEFINITIONS

    keys = (
        "coc7_social_difficulty",
        "coc7_combined_checks",
        "coc7_group_luck",
        "coc7_economy",
        "coc7_contacts",
        "coc7_age_adjustments",
    )
    for key in keys:
        assert HOUSE_RULE_DEFINITIONS[key]["default"] is False
        assert HOUSE_RULE_DEFINITIONS[key]["type"] == "boolean"
