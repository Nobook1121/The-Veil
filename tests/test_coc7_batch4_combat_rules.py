"""批次 4 战斗细节的纯规则与战斗工具回归测试。"""

from __future__ import annotations

from trpg_server.agents.tools import combat
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import combat_rules
from trpg_server.ruleset.coc7.options import resolve_options


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "combat-batch4"
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
        "side": "enemy",
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 10,
        "max_hp": 10,
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


# ---------------------------------------------------------------------------
# 纯规则
# ---------------------------------------------------------------------------
def test_new_switches_default_off():
    options = resolve_options(None)
    assert options.combat_aim_tracking is False
    assert options.combat_cover_dodge is False
    assert options.combat_friendly_fire is False
    assert options.combat_knockout is False
    assert options.combat_hit_locations is False


def test_aim_lifecycle_rules():
    assert combat_rules.aim_bonus(False) == 0
    assert combat_rules.aim_bonus(True) == 1
    assert combat_rules.can_declare_aim({"name": "A"})[0] is True
    assert combat_rules.can_declare_aim({"name": "A", "aiming": True})[0] is False
    assert combat_rules.can_declare_aim({"name": "A", "dead": True})[0] is False
    assert combat_rules.declare_aim(3) == {"aiming": True, "aim_declared_round": 3}
    assert combat_rules.aim_consumed_patch() == {"aiming": False, "aim_declared_round": 0}


def test_cover_dodge_rules():
    assert combat_rules.cover_penalty(True) == 1
    assert combat_rules.cover_penalty(False) == 0
    assert combat_rules.attacker_loses_next_attack("regular") is True
    assert combat_rules.attacker_loses_next_attack("extreme") is True
    assert combat_rules.attacker_loses_next_attack("failure") is False


def test_friendly_fire_victim_picks_lowest_luck_ally():
    combatants = {
        "Attacker": {"name": "Attacker", "side": "player", "luck": 60},
        "Lucky": {"name": "Lucky", "side": "player", "luck": 80},
        "Unlucky": {"name": "Unlucky", "side": "player", "luck": 10},
        "Downed": {"name": "Downed", "side": "player", "luck": 5, "unconscious": True},
        "Target": {"name": "Target", "side": "enemy", "luck": 1},
    }
    victim = combat_rules.friendly_fire_victim(
        combatants, attacker="Attacker", target="Target", side="player"
    )
    assert victim == "Unlucky"


def test_hit_location_table_covers_all_ranges():
    assert combat_rules.hit_location(1)["location"] == "右腿"
    assert combat_rules.hit_location(5)["location"] == "左腿"
    assert combat_rules.hit_location(8)["location"] == "腹部"
    assert combat_rules.hit_location(13)["location"] == "胸部"
    assert combat_rules.hit_location(16)["location"] == "右臂"
    assert combat_rules.hit_location(20)["location"] == "左臂"
    assert combat_rules.hit_location(2)["limb"] is True
    assert combat_rules.hit_location(13)["limb"] is False
    assert "肢体" in combat_rules.major_wound_effect("右臂")


def test_blunt_weapon_detection():
    assert combat_rules.is_blunt_weapon({"name": "棍棒"}) is True
    assert combat_rules.is_blunt_weapon({"name": "拳套", "skill": "Brawl"}) is True
    assert combat_rules.is_blunt_weapon({"name": "Knife"}) is False
    assert combat_rules.knockout_result(7) == {"damage": 1, "unconscious": True, "rounds": 7}


# ---------------------------------------------------------------------------
# 战斗工具接线
# ---------------------------------------------------------------------------
def test_aim_tracking_requires_declaration(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_aim_tracking": True})
    combat.start_combat(
        {"participants": [_gunner("Attacker", side="player"), _spec("Target")]}, context
    )

    declared = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "aim_action": True},
        context,
    )
    assert declared["aiming"] is True
    assert "瞄准" in declared["summary"]
    state = combat._load_combat(context)
    assert state["combatants"]["Attacker"]["aiming"] is True

    # 回到攻击者行动，射击应享受瞄准奖励骰，并在射击后清除瞄准。
    for _ in range(4):
        if combat.combat_state({}, context)["current"] == "Attacker":
            break
        combat.next_turn({}, context)
    result = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "aim": True, "attack_roll": 1},
        context,
    )
    assert result["shots"][0]["bonus_dice"] >= 1
    state = combat._load_combat(context)
    assert state["combatants"]["Attacker"]["aiming"] is False


def test_aim_tracking_disabled_keeps_legacy_bonus(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat(
        {"participants": [_gunner("Attacker", side="player"), _spec("Target")]}, context
    )
    result = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "aim": True, "attack_roll": 1},
        context,
    )
    assert result["shots"][0]["bonus_dice"] == 1
    assert combat._load_combat(context)["combatants"]["Attacker"].get("aiming") is None


def test_damage_clears_aiming(tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_aim_tracking": True})
    combat.start_combat(
        {"participants": [_gunner("Attacker", side="player"), _spec("Target")]}, context
    )
    combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "aim_action": True},
        context,
    )
    state = combat._load_combat(context)
    state["combatants"]["Attacker"]["aiming"] = True
    combat._save_combat(context, state)
    combat.damage_combatant({"target": "Attacker", "amount": 1}, context)
    assert combat._load_combat(context)["combatants"]["Attacker"]["aiming"] is False


def test_cover_dodge_makes_attacker_lose_next_attack(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_cover_dodge": True})
    combat.start_combat(
        {"participants": [_gunner("Attacker", side="player"), _spec("Target", dex=80)]}, context
    )
    monkeypatch.setattr(combat, "_percentile_roll", lambda *args, **kwargs: (1, []))
    result = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "cover": True}, context
    )
    assert "闪避成功" in result["summary"]
    assert combat._load_combat(context)["combatants"]["Attacker"]["lost_next_attack"] is True

    blocked = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪"}, context
    )
    assert "失去下一次攻击" in blocked["summary"]
    assert combat._load_combat(context)["combatants"]["Attacker"]["lost_next_attack"] is False


def test_friendly_fire_on_fumble(tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_friendly_fire": True})
    combat.start_combat(
        {
            "participants": [
                _gunner("Attacker", side="player", luck=60),
                _spec("Ally", side="player", luck=10, hp=10, max_hp=10),
                _spec("Target", luck=90),
            ]
        },
        context,
    )
    result = combat.shoot_combatant(
        {
            "actor": "Attacker",
            "target": "Target",
            "weapon": "手枪",
            "in_melee": True,
            "attack_roll": 100,
        },
        context,
    )
    shot = result["shots"][0]
    assert shot["friendly_fire"]["victim"] == "Ally"
    assert shot["friendly_fire"]["effective"] >= 1


def test_hit_locations_on_shooting(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_hit_locations": True})
    combat.start_combat(
        {"participants": [_gunner("Attacker", side="player"), _spec("Target", hp=30, max_hp=30)]},
        context,
    )
    monkeypatch.setattr(combat, "_roller", lambda sides: 20 if sides == 20 else 1)
    result = combat.shoot_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "手枪", "attack_roll": 1},
        context,
    )
    location = result["shots"][0]["hit_location"]
    assert location["location"] == "左臂"
    assert "命中左臂" in result["summary"]


def test_knockout_maneuver_house_rule(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_knockout": True})
    combat.start_combat(
        {
            "participants": [
                _spec(
                    "Attacker",
                    side="player",
                    weapons=[
                        {
                            "name": "棍棒",
                            "skill": "Fighting (Brawl)",
                            "damage": "1D6",
                            "attacks": "1",
                        }
                    ],
                ),
                _spec("Target"),
            ]
        },
        context,
    )
    monkeypatch.setattr(
        combat,
        "_opposed_rolls",
        lambda *args, **kwargs: {
            "attack_roll": 50,
            "attack_candidates": [],
            "attack_level": "regular",
            "defense_roll": 100,
            "defense_candidates": [],
            "defense_level": "fumble",
        },
    )
    result = combat.attack_combatant(
        {"actor": "Attacker", "target": "Target", "weapon": "棍棒", "maneuver": "knockout"},
        context,
    )
    state = combat._load_combat(context)
    target = state["combatants"]["Target"]
    assert target["unconscious"] is True
    assert target["hp"] == 9
    assert 1 <= target["knockout_rounds"] <= 10
    assert "击晕" in result["summary"]


# ---------------------------------------------------------------------------
# 毒剂
# ---------------------------------------------------------------------------
def test_poison_rules_halve_damage_on_resistance():
    assert combat_rules.poison_check_target(60) == 60
    assert combat_rules.poison_penalty_dice(5) == 2
    assert combat_rules.poison_penalty_dice(-1) == 0
    assert combat_rules.poison_resisted("regular") is True
    assert combat_rules.poison_resisted("failure") is False
    assert combat_rules.poison_damage(9, resisted=True) == 4
    assert combat_rules.poison_damage(9, resisted=False) == 9


def test_poison_requires_house_rule(tmp_path):
    context = FakeContext(tmp_path)
    combat.start_combat({"participants": [_spec("Target")]}, context)
    result = combat.poison_combatant({"target": "Target", "damage": "1D6"}, context)
    assert "毒剂房规未开启" in result["error"]


def test_poison_resistance_halves_damage(monkeypatch, tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat({"participants": [_spec("Target", hp=20, max_hp=20)]}, context)
    monkeypatch.setattr(combat, "_percentile_roll", lambda *args, **kwargs: (1, []))
    monkeypatch.setattr(combat, "_roller", lambda sides: 6 if sides == 10 else 1)
    result = combat.poison_combatant({"target": "Target", "damage": "1D10+4"}, context)
    assert result["check"]["level"] == "critical"
    assert result["poison_damage"]["total"] == 10
    assert result["poison_damage"]["applied"] == 5
    assert result["hp_after"] == 15


def test_poison_onset_is_registered_not_scheduled(tmp_path):
    context = FakeContext(tmp_path, {"coc7_combat_poison": True})
    combat.start_combat({"participants": [_spec("Target", hp=20, max_hp=20)]}, context)
    result = combat.poison_combatant(
        {"target": "Target", "damage": "1D6", "onset_rounds": 3}, context
    )
    assert result["pending"] is True
    assert result["onset_rounds"] == 3
    assert combat._load_combat(context)["combatants"]["Target"]["hp"] == 20


# ---------------------------------------------------------------------------
# 自然恢复
# ---------------------------------------------------------------------------
def test_natural_healing_rules():
    assert combat_rules.natural_healing(10) == 10
    assert combat_rules.natural_healing(10, major_wound=True) == 1
    assert combat_rules.natural_healing(6, major_wound=True) == 0
    assert combat_rules.natural_healing(0) == 0
    assert combat_rules.natural_healing(-3) == 0


def _heal_room(house_rules):
    return {
        "room_id": "r1",
        "house_rules": house_rules,
        "members": [
            {
                "user_id": "p1-id",
                "username": "玩家甲",
                "character_card": {
                    "name": "调查员",
                    "maxHp": 10,
                    "currentHp": 3,
                    "status": {},
                },
                "character_state": {"current_hp": 3, "max_hp": 10},
            }
        ],
    }


def test_natural_healing_command_applies_days_without_game_date(tmp_path):
    save_room_info(tmp_path, _heal_room({"coc7_healing_natural": True}))
    ok, text = handle_room_rule_command(
        ["heal", "natural", "玩家甲", "3"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is True
    assert "3 → 6" in text
    saved = load_room_info(tmp_path)
    assert saved["members"][0]["character_card"]["currentHp"] == 6
    assert saved["members"][0]["character_state"]["current_hp"] == 6


def test_natural_healing_requires_rest_when_enabled(tmp_path):
    save_room_info(
        tmp_path,
        _heal_room({"coc7_healing_natural": True, "coc7_healing_require_rest": True}),
    )
    ok, _ = handle_room_rule_command(
        ["heal", "natural", "玩家甲", "2"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is False
    ok, text = handle_room_rule_command(
        ["heal", "natural", "玩家甲", "2", "rest"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is True, text
    assert "含休养" in text


def test_natural_healing_requires_switch(tmp_path):
    save_room_info(tmp_path, _heal_room({}))
    ok, _ = handle_room_rule_command(
        ["heal", "natural", "玩家甲", "2"],
        room_id="r1",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )
    assert ok is False
