"""CoC7 战斗规则修正与规则书插件的回归测试。"""

from __future__ import annotations

import random

import pytest

from trpg_server.agents.tools import combat
from trpg_server.combat.registry import get_ruleset, list_rulesets
from trpg_server.combat.rulesets.coc7 import Coc7Ruleset
from trpg_server.combat.scheduler import TurnScheduler
from trpg_server.combat.settings import CombatSettings


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "combat-test"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._house_rules = house_rules or {}

    def room_info(self):
        return {"id": self.room_id, "members": [], "house_rules": self._house_rules}


def _spec(name, **overrides):
    spec = {
        "name": name,
        "is_npc": True,
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 10,
        "max_hp": 10,
        "skills": {"Fighting (Brawl)": 50},
        "weapons": [
            {"name": "Knife", "skill": "Fighting (Brawl)", "damage": "1D4+2", "attacks": "1"},
        ],
    }
    spec.update(overrides)
    return spec


def test_settings_defaults_are_core_rules():
    settings = CombatSettings.from_house_rules(None)
    assert settings.surprise_model == "bonus_die"
    assert settings.dex_extra_action_threshold is None
    assert settings.ready_weapon_cost == "free"
    assert settings.cast_interrupt == "keeper"


def test_house_rules_override_combat_settings():
    settings = CombatSettings.from_house_rules(
        {
            "combat_surprise_model": "auto_success",
            "combat_ready_weapon_cost": "action",
            "combat_cast_interrupt": "any_damage",
            "combat_major_wound_penalty": "cannot_act",
            "combat_dex_extra_action_threshold": 70,
        }
    )
    assert settings.surprise_model == "auto_success"
    assert settings.ready_weapon_cost == "action"
    assert settings.cast_interrupt == "any_damage"
    assert settings.major_wound_penalty == "cannot_act"
    assert settings.dex_extra_action_threshold == 70


def test_dex_above_50_does_not_grant_extra_actions_by_default():
    ruleset = Coc7Ruleset()
    combatant = {"dex": 90, "actions_base": 1}
    assert ruleset.actions_per_round(combatant, CombatSettings()) == 1


def test_dex_extra_action_is_opt_in_house_rule():
    ruleset = Coc7Ruleset()
    combatant = {"dex": 90, "actions_base": 1}
    settings = CombatSettings(dex_extra_action_threshold=70)
    assert ruleset.actions_per_round(combatant, settings) == 2


def test_initiative_tie_uses_fighting_skill():
    ruleset = Coc7Ruleset()
    slow_hand = {"dex": 60, "fighting": 30}
    quick_hand = {"dex": 60, "fighting": 60}
    assert ruleset.initiative_tiebreak(quick_hand) > ruleset.initiative_tiebreak(slow_hand)


def test_melee_tie_policies_follow_rulebook():
    ruleset = Coc7Ruleset()
    assert ruleset.resolve_melee("regular", "regular", "dodge") == "miss"
    assert ruleset.resolve_melee("regular", "regular", "counter") == "hit"
    assert ruleset.resolve_melee("regular", "hard", "counter") == "counter_hit"
    assert ruleset.resolve_melee("regular", "hard", "dodge") == "miss"
    assert ruleset.resolve_melee("failure", "failure", "none") == "miss"


def test_damage_equal_to_max_hp_is_dying_not_dead():
    ruleset = Coc7Ruleset()
    target = {"hp": 10, "max_hp": 10, "con": 50, "status": "active"}
    outcome = ruleset.resolve_damage(
        target,
        10,
        armor=0,
        ignore_armor=False,
        roll_d100=lambda: 50,
        thresholds=(1, 96),
        settings=CombatSettings(),
    )
    assert outcome.hp_after == 0
    assert outcome.major_wound is True
    assert outcome.dying is True
    assert outcome.dead is False


def test_damage_above_max_hp_is_instant_death():
    ruleset = Coc7Ruleset()
    target = {"hp": 10, "max_hp": 10, "con": 50, "status": "active"}
    outcome = ruleset.resolve_damage(
        target,
        11,
        armor=0,
        ignore_armor=False,
        roll_d100=lambda: 50,
        thresholds=(1, 96),
        settings=CombatSettings(),
    )
    assert outcome.dead is True
    assert target["dead"] is True


def test_major_wound_threshold_is_half_max_hp():
    ruleset = Coc7Ruleset()
    target = {"hp": 10, "max_hp": 10, "con": 99, "status": "active"}
    outcome = ruleset.resolve_damage(
        target,
        5,
        armor=0,
        ignore_armor=False,
        roll_d100=lambda: 10,
        thresholds=(1, 96),
        settings=CombatSettings(),
    )
    assert outcome.major_wound is True
    assert outcome.dying is False

    fresh = {"hp": 10, "max_hp": 10, "con": 99, "status": "active"}
    below = ruleset.resolve_damage(
        fresh,
        4,
        armor=0,
        ignore_armor=False,
        roll_d100=lambda: 10,
        thresholds=(1, 96),
        settings=CombatSettings(),
    )
    assert below.major_wound is False


def test_dying_character_checks_con_at_round_end():
    ruleset = Coc7Ruleset()
    target = {"hp": 0, "max_hp": 10, "con": 50, "status": "dying", "dying": True}
    dead = ruleset.on_round_end(
        target, roll_d100=lambda: 99, thresholds=(1, 96), settings=CombatSettings()
    )
    assert dead.dead is True

    survivor = {"hp": 0, "max_hp": 10, "con": 50, "status": "dying", "dying": True}
    alive = ruleset.on_round_end(
        survivor, roll_d100=lambda: 10, thresholds=(1, 96), settings=CombatSettings()
    )
    assert alive.dead is False


def test_scheduler_uses_action_budget_and_skips_incapacitated():
    ruleset = Coc7Ruleset()
    state = {
        "round": 1,
        "turn": 0,
        "order": ["A", "B"],
        "combatants": {
            "A": {"dex": 60, "actions_base": 2, "status": "active"},
            "B": {"dex": 40, "actions_base": 1, "status": "active"},
        },
    }
    scheduler = TurnScheduler(ruleset, CombatSettings())
    scheduler.begin_round(state)
    assert state["combatants"]["A"]["actions_remaining"] == 2

    scheduler.advance(state)
    assert state["turn"] == 0
    assert state["combatants"]["A"]["actions_remaining"] == 1

    scheduler.advance(state)
    assert state["turn"] == 1

    state["combatants"]["B"]["status"] = "unconscious"
    scheduler.advance(state)
    assert state["turn"] == 0
    assert state["round"] == 2


def test_surprise_is_attack_modifier_not_lost_turn(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {
            "participants": [_spec("Attacker", dex=60), _spec("Target", dex=40)],
            "surprised": ["Target"],
        },
        context,
    )
    state = combat.combat_state({}, context)
    assert state["current"] == "Attacker"

    combat.next_turn({}, context)
    state = combat.combat_state({}, context)
    # 被突袭者仍然正常获得行动，不再被跳过。
    assert state["current"] == "Target"
    assert state["round"] == 1


def test_surprise_auto_success_house_rule(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"combat_surprise_model": "auto_success"})
    combat.start_combat(
        {
            "participants": [_spec("Attacker", dex=60), _spec("Target", dex=40)],
            "surprised": ["Target"],
        },
        context,
    )
    monkeypatch.setattr(combat, "_roller", lambda sides: 1)
    result = combat.attack_combatant(
        {
            "actor": "Attacker",
            "target": "Target",
            "weapon": "Knife",
            "defense": "dodge",
        },
        context,
    )
    assert "error" not in result
    target = combat._load_combat(context)["combatants"]["Target"]
    assert target["hp"] < target["max_hp"]
    assert target["reactions_this_round"] == 0


def test_monster_actions_per_round_from_stat_block(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {
            "participants": [
                _spec("Ghoul", dex=60, actions_per_round=3),
                _spec("Investigator", dex=40),
            ]
        },
        context,
    )
    state = combat.combat_state({}, context)
    ghoul = next(item for item in state["order"] if item["name"] == "Ghoul")
    assert ghoul["actions_per_round"] == 3


def test_dnd5e_plugin_proves_core_abstraction():
    ruleset = get_ruleset("dnd5e")
    assert {item["id"] for item in list_rulesets()} == {"coc7", "dnd5e"}

    rng = random.Random(7)
    fighter = {"dex": 16, "hp": 20, "max_hp": 20, "speed": 30}
    initiative = ruleset.roll_initiative(fighter, rng, CombatSettings())
    assert 4 <= initiative <= 23
    assert ruleset.action_budget(fighter, CombatSettings()) == {
        "action": 1,
        "bonus_action": 1,
        "reaction": 1,
        "movement": 30,
    }
    assert ruleset.resolve_attack(15, 14) == "hit"
    assert ruleset.resolve_attack(10, 14) == "miss"


def test_dnd5e_death_saves_are_plugin_owned():
    ruleset = get_ruleset("dnd5e")
    target = {"hp": 20, "max_hp": 20, "status": "active"}
    outcome = ruleset.resolve_damage(
        target,
        25,
        armor=0,
        ignore_armor=False,
        roll_d100=None,
        thresholds=(1, 96),
        settings=CombatSettings(),
    )
    assert outcome.dying is True
    assert target["death_saves"] == {"successes": 0, "failures": 0}

    # 三次失败后死亡（5e 用 d20，10 以下失败）。
    for _ in range(3):
        result = ruleset.on_round_end(
            target, roll_d100=lambda: 5, thresholds=(1, 96), settings=CombatSettings()
        )
    assert result.dead is True
    assert target["status"] == "dead"


def test_delay_moves_actor_to_end_of_round(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {"participants": [_spec("Fast", dex=60), _spec("Slow", dex=40)]}, context
    )

    result = combat.delay_combatant({}, context)

    assert "error" not in result
    state = combat.combat_state({}, context)
    assert [item["name"] for item in state["order"]] == ["Slow", "Fast"]
    assert state["current"] == "Slow"


def test_reload_refills_ammo_and_consumes_action(tmp_path):
    rifle = {
        "name": "Rifle",
        "skill": "Firearms",
        "damage": "1D10",
        "range": "100码",
        "attacks": "1",
        "ammo": "5",
        "malfunction": "98",
    }
    context = FakeContext(tmp_path)
    combat.start_combat(
        {
            "participants": [
                _spec("Sniper", dex=60, weapons=[rifle]),
                _spec("Target", dex=40),
            ]
        },
        context,
    )

    result = combat.reload_combatant({"actor": "Sniper", "weapon": "Rifle"}, context)

    assert "error" not in result
    sniper = combat._load_combat(context)["combatants"]["Sniper"]
    assert sniper["ammo"]["Rifle"] == 5
    # 装填消耗一次显著行动，回合推进到下一位。
    assert combat.combat_state({}, context)["current"] == "Target"


def test_cast_spell_spends_mp_and_san_and_schedules_resolution(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {
            "participants": [
                _spec("Caster", dex=60, mp=10, san=50),
                _spec("Target", dex=40),
            ]
        },
        context,
    )

    result = combat.cast_spell(
        {
            "caster": "Caster",
            "spell": "Dread Curse",
            "cast_time": "two_rounds",
            "mp_cost": 4,
            "san_cost": 2,
        },
        context,
    )

    assert "error" not in result
    caster = combat._load_combat(context)["combatants"]["Caster"]
    assert caster["mp"] == 6
    assert caster["san"] == 48
    assert result["spell"]["resolve_round"] == 2
    assert combat.combat_state({}, context)["current"] == "Target"


def test_cast_spell_mp_overflow_becomes_hp_damage(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {
            "participants": [
                _spec("Caster", dex=60, mp=1, san=50, hp=10, max_hp=10),
                _spec("Target", dex=40),
            ]
        },
        context,
    )

    result = combat.cast_spell(
        {"caster": "Caster", "spell": "Curse", "mp_cost": 5, "san_cost": 0}, context
    )

    assert "error" not in result
    caster = combat._load_combat(context)["combatants"]["Caster"]
    assert caster["mp"] == 0
    assert caster["hp"] == 6
    assert result["mp_overflow_damage"]["effective"] == 4


def test_ready_weapon_house_rule_can_consume_action(tmp_path):
    context = FakeContext(tmp_path, {"combat_ready_weapon_cost": "action"})
    combat.start_combat(
        {"participants": [_spec("Fast", dex=60), _spec("Slow", dex=40)]}, context
    )

    result = combat.ready_combatant({"actor": "Fast"}, context)

    assert "error" not in result
    fast = combat._load_combat(context)["combatants"]["Fast"]
    assert fast["ready_weapon"] is True
    assert combat.combat_state({}, context)["current"] == "Slow"


def test_major_wound_cannot_act_house_rule_blocks_utility_actions(tmp_path):
    context = FakeContext(tmp_path, {"combat_major_wound_penalty": "cannot_act"})
    combat.start_combat(
        {
            "participants": [
                _spec("Hurt", dex=60, major_wound=True),
                _spec("Other", dex=40),
            ]
        },
        context,
    )

    assert "error" in combat.delay_combatant({}, context)
    assert "error" in combat.disengage_combatant({"actor": "Hurt"}, context)
    assert "error" in combat.ready_combatant({"actor": "Hurt"}, context)
    assert "error" in combat.cast_spell(
        {"caster": "Hurt", "spell": "Anything", "mp_cost": 0, "san_cost": 0}, context
    )
