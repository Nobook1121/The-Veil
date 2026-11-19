"""E6：D&D 5e 条件、力竭、持续时间、回合 tick 与专注连带移除。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.combat.scheduler import TurnScheduler
from trpg_server.combat.settings import resolve_settings
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.effects import add_effect, tick_holder
from trpg_server.ruleset.session import turn_model_of

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _plugin():
    return builtin_registry().resolve("dnd5e")


def _effects_api():
    """插件模块在测试 fixture 加载插件后才可导入。"""
    from dnd5e_plugin import effects

    return effects


def _goblin(name: str = "Goblin") -> dict:
    combatant = {"name": name, "is_player": False, "side": "enemy", "monster_id": "goblin"}
    _plugin().combat.prepare_combatant(combatant, card=None, settings=None)
    return combatant


def _wizard(level: int = 5, known=None) -> tuple[dict, dict]:
    card = _plugin().schema.pack(
        {
            "id": f"wizard-{level}",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 24, "max": 24},
            "spellcasting": {"known": list(known or ["hold-person"])},
            "equipment": [],
        }
    )
    combatant = {"name": "Mira", "is_player": True, "side": "player", "level": level}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=None)
    return combatant, card


# ---------------------------------------------------------------------------
# 条件与力竭
# ---------------------------------------------------------------------------
def test_apply_and_remove_condition(plugins):
    effects = _plugin().effects
    goblin = _goblin()
    result = effects.apply(goblin, {"condition": "poisoned", "source": "test"})
    assert result["effect"]["kind"] == "condition"
    assert result["effect"]["detail"]["condition"] == "poisoned"
    assert _effects_api().flags_of(goblin)["attack_rolls_disadvantage"] is True

    removed = effects.remove(goblin, "poisoned")
    assert [item["id"] for item in removed["removed"]] == [result["effect"]["id"]]
    assert goblin["effects"] == []
    assert _effects_api().flags_of(goblin) == {}


def test_exhaustion_levels_are_cumulative(plugins):
    effects = _plugin().effects
    goblin = _goblin()
    effects.apply(goblin, {"condition": "exhaustion", "levels": 1})
    assert _effects_api().exhaustion_level(goblin) == 1
    assert _effects_api().flags_of(goblin).get("ability_checks_disadvantage") is True
    assert _effects_api().effective_speed(goblin, 30) == 30

    effects.apply(goblin, {"condition": "exhaustion", "levels": 4})
    assert _effects_api().exhaustion_level(goblin) == 5
    assert _effects_api().flags_of(goblin).get("speed_halved") is True
    assert _effects_api().flags_of(goblin).get("speed_zero") is True
    assert _effects_api().effective_speed(goblin, 30) == 0


def test_exhaustion_six_kills(plugins):
    effects = _plugin().effects
    goblin = _goblin()
    effects.apply(goblin, {"condition": "exhaustion", "levels": 5})
    assert not goblin.get("dead")
    result = effects.apply(goblin, {"condition": "exhaustion", "levels": 1})
    assert goblin["dead"] is True
    assert any("死亡" in note for note in result["notes"])


# ---------------------------------------------------------------------------
# 持续时间与回合 tick
# ---------------------------------------------------------------------------
def test_turn_timed_condition_expires(plugins):
    goblin = _goblin()
    effect = _effects_api().build_condition(
        "poisoned",
        source="test",
        duration={"unit": "turn", "remaining": 1, "timing": "turn_end"},
    )
    add_effect(goblin, effect)
    notes, expired = tick_holder(goblin, phase="turn", timing="turn_start", plugin=_plugin())
    assert expired == [] and goblin["effects"]
    notes, expired = tick_holder(goblin, phase="turn", timing="turn_end", plugin=_plugin())
    assert expired and expired[0]["detail"]["condition"] == "poisoned"
    assert any("中毒结束" in note for note in notes)
    assert goblin["effects"] == []


def test_hold_person_save_ends_on_success(plugins):
    goblin = _goblin()
    effect = _effects_api().build_condition(
        "paralyzed",
        source="mira:hold-person",
        duration={"unit": "permanent"},
        dc=14,
        save_ends={"ability": "wis", "dc": 14, "timing": "turn_end"},
    )
    add_effect(goblin, effect)

    notes, expired = tick_holder(
        goblin, phase="turn", timing="turn_end", plugin=_plugin(), rng=lambda sides: 20
    )
    assert expired and expired[0]["detail"]["condition"] == "paralyzed"
    assert any("结束" in note for note in notes)
    assert goblin["effects"] == []


def test_hold_person_persists_on_failed_save(plugins):
    goblin = _goblin()
    add_effect(
        goblin,
        _effects_api().build_condition(
            "paralyzed",
            source="mira:hold-person",
            duration={"unit": "permanent"},
            dc=14,
            save_ends={"ability": "wis", "dc": 14, "timing": "turn_end"},
        ),
    )
    notes, expired = tick_holder(
        goblin, phase="turn", timing="turn_end", plugin=_plugin(), rng=lambda sides: 1
    )
    assert expired == []
    assert goblin["effects"]
    assert any("持续" in note for note in notes)


def test_scheduler_end_turn_ticks_effects(plugins):
    plugin = _plugin()
    settings = resolve_settings({"id": "room", "ruleset_id": "dnd5e", "house_rules": {}})
    scheduler = TurnScheduler(
        plugin.combat,
        settings,
        roll_d100=lambda: 1,
        roll_die=lambda sides: 1,
        thresholds=(1, 96),
        turn_model=turn_model_of(plugin),
    )
    goblin = _goblin()
    goblin["budget"] = {"action": 1, "bonus_action": 1, "reaction": 1, "movement": 30}
    combat = {
        "combatants": {goblin["name"]: goblin},
        "order": [goblin["name"]],
        "turn": 0,
        "round": 1,
    }
    add_effect(
        goblin,
        _effects_api().build_condition(
            "poisoned",
            source="test",
            duration={"unit": "turn", "remaining": 1, "timing": "turn_end"},
        ),
    )
    events = scheduler.end_turn(combat)
    assert any("中毒结束" in event for event in events)
    assert goblin["effects"] == []


def test_real_hp_slot_feeds_core_threshold_hook(plugins):
    from trpg_server.ruleset.effects import apply_resource_delta
    from trpg_server.ruleset.resources import ResourceDelta

    goblin = _goblin()
    change, notes = apply_resource_delta(
        goblin, _plugin(), "hp", ResourceDelta(kind="hp", delta=-99, reason="test")
    )
    assert change is not None
    assert goblin["hp"] == 0
    assert [event.event for event in change.events] == ["zero_hp"]
    assert notes == ["Goblin 的生命值降到 0。"]


def test_tool_gating_by_capability(plugins):
    from trpg_server.agents.profiles import build_kp_tool_names

    dnd5e_tools = set(build_kp_tool_names("dnd5e"))
    assert {"spell.list", "spell.cast", "effect.list", "effect.apply", "effect.remove"} <= dnd5e_tools
    coc7_tools = set(build_kp_tool_names("coc7"))
    assert "spell.cast" not in coc7_tools
    assert "effect.apply" not in coc7_tools


def test_kp_tool_policy_trims_tools_per_ruleset(plugins):
    from trpg_server.agents.profiles import build_kp_tool_names

    removed = {
        "combat.shoot",
        "combat.treat",
        "character.set_attribute",
        "character.set_skill",
        "character.add_item",
        "room.request_interlude",
    }
    dnd5e_tools = set(build_kp_tool_names("dnd5e"))
    assert {"combat.heal", "combat.join"} <= dnd5e_tools
    assert not (removed & dnd5e_tools)

    # coc7 没有声明裁剪，工具清单保持原样。
    coc7_tools = set(build_kp_tool_names("coc7"))
    assert removed <= coc7_tools
    assert "combat.heal" not in coc7_tools
    assert "combat.join" not in coc7_tools


# ---------------------------------------------------------------------------
# 条件影响攻击
# ---------------------------------------------------------------------------
def test_poisoned_attacker_has_disadvantage(plugins):
    attacker = _goblin("Goblin A")
    target = _goblin("Goblin B")
    _plugin().effects.apply(attacker, {"condition": "poisoned"})
    mods = _effects_api().attack_modifiers(attacker, target)
    assert mods["disadvantage"] is True
    assert mods["advantage"] is False


def test_paralyzed_target_grants_advantage_and_auto_crit(plugins):
    attacker = _goblin("Goblin A")
    target = _goblin("Goblin B")
    _plugin().effects.apply(target, {"condition": "paralyzed"})
    mods = _effects_api().attack_modifiers(attacker, target, ranged=False)
    assert mods["advantage"] is True
    assert mods["auto_crit"] is True


def test_condition_attack_integration_uses_advantage(plugins):
    attacker, _card = _wizard()
    target = _goblin()
    _plugin().effects.apply(target, {"condition": "paralyzed"})
    attacker["attacks"] = [
        {
            "id": "staff",
            "name": "法杖",
            "kind": "melee",
            "attack_bonus": 5,
            "damage_bonus": 0,
            "damage_dice": "1d6",
            "damage_type": "bludgeoning",
            "properties": [],
        }
    ]
    result = _plugin().combat.resolve_attack_action(
        attacker,
        target,
        {"attack_roll": 15},
        rng=lambda sides: 10,
        combat={"combatants": {target["name"]: target}},
    )
    assert result["natural"] == 15
    assert result["critical"] is True


# ---------------------------------------------------------------------------
# 专注打断由受伤钩子触发
# ---------------------------------------------------------------------------
def test_on_damage_taken_breaks_concentration(plugins):
    caster, _card = _wizard()
    target = _goblin()
    combat = {"combatants": {target["name"]: target}, "round": 1}
    _plugin().spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[target],
        slot_level=2,
        rng=lambda sides: 1,
        combat=combat,
    )
    assert target["effects"]
    notes = _plugin().combat.on_damage_taken(
        caster,
        SimpleNamespace(effective=22),
        rng=lambda sides: 1,
        combat=combat,
    )
    assert any("专注被打断" in note for note in notes)
    assert "concentration" not in caster
    assert target["effects"] == []
