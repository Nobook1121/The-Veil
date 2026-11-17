"""E5：D&D 5e 法术位、已知/准备、升环、法术攻击/豁免与专注。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.combat.settings import resolve_settings
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

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


def _wizard_card(level: int = 5, known=None, prepared=None) -> dict:
    spellcasting: dict = {}
    if known is not None:
        spellcasting["known"] = list(known)
    if prepared is not None:
        spellcasting["prepared"] = list(prepared)
    return _plugin().schema.pack(
        {
            "id": f"wizard-{level}",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "proficiencies": {"saves": ["int", "wis"], "skills": ["arcana"]},
            "hp": {"current": 24, "max": 24},
            "spellcasting": spellcasting,
            "equipment": [],
        }
    )


def _fighter_card() -> dict:
    return _plugin().schema.pack(
        {
            "id": "fighter-1",
            "name": "Borin",
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 12, "con": 14, "int": 8, "wis": 10, "cha": 10},
            "hp": {"current": 30, "max": 30},
            "equipment": [],
        }
    )


def _caster(level: int = 5, known=None, prepared=None) -> dict:
    card = _wizard_card(level, known=known, prepared=prepared)
    combatant = {"name": "Mira", "is_player": True, "side": "player", "level": level}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=None)
    return combatant


def _goblin(name: str = "Goblin") -> dict:
    combatant = {"name": name, "is_player": False, "side": "enemy", "monster_id": "goblin"}
    _plugin().combat.prepare_combatant(combatant, card=None, settings=None)
    return combatant


def _fail_save_max_damage(sides: int) -> int:
    """豁免 d20 取 1（失败），其余骰取最大，便于断言伤害骰数。"""
    return 1 if sides == 20 else sides


class _FakeContext:
    def __init__(self, tmp_path: Path, card: dict | None = None):
        self.room_dir = tmp_path
        self.room_id = "dnd5e-spells"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._card = card or {}

    def room_info(self) -> dict:
        members = []
        if self._card:
            members.append(
                {
                    "user_id": 1,
                    "username": "Mira",
                    "character_card": self._card,
                    "character_state": {},
                }
            )
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "house_rules": {},
            "members": members,
        }


# ---------------------------------------------------------------------------
# 法术位与已知/准备
# ---------------------------------------------------------------------------
def test_wizard_slots_come_from_class_table(plugins):
    caster = _caster(level=3)
    assert caster["spellcasting"]["ability"] == "int"
    assert caster["spellcasting"]["spellSaveDC"] == 13
    assert caster["spellcasting"]["spellAttackBonus"] == 5
    assert caster["spell_slots"] == {
        "1": {"max": 4, "used": 0},
        "2": {"max": 2, "used": 0},
    }


def test_non_caster_has_no_spell_slots(plugins):
    card = _fighter_card()
    combatant = {"name": "Borin", "is_player": True, "side": "player"}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=None)
    assert "spellcasting" not in combatant
    assert not combatant.get("spell_slots")


def test_prepared_list_overrides_known(plugins):
    caster = _caster(level=3, known=["fireball"], prepared=["magic-missile"])
    spells = _plugin().spells
    listing = spells.list_spells(caster, _wizard_card(3, known=["fireball"], prepared=["magic-missile"]))
    ids = {row["id"] for row in listing["spells"]}
    assert "magic-missile" in ids
    assert "fireball" not in ids
    result = spells.cast(
        caster, None, spell="fireball", targets=[_goblin()], slot_level=3, rng=lambda sides: 1
    )
    assert "没有准备" in result["error"]


def test_uncastable_spell_is_rejected(plugins):
    caster = _caster(level=3, known=["magic-missile"])
    result = _plugin().spells.cast(
        caster, None, spell="hold-person", targets=[_goblin()], slot_level=2, rng=lambda sides: 1
    )
    assert "没有准备" in result["error"]


# ---------------------------------------------------------------------------
# 升环与伤害
# ---------------------------------------------------------------------------
def test_fireball_upcast_adds_damage_dice(plugins):
    caster = _caster(level=7, known=["fireball"])
    spells = _plugin().spells
    target_1 = _goblin("Goblin A")
    result_1 = spells.cast(
        caster,
        None,
        spell="fireball",
        targets=[target_1],
        slot_level=3,
        rng=_fail_save_max_damage,
    )
    assert result_1["damage"][0]["dice"] == "8d6"
    assert result_1["damage"][0]["effective"] == 48
    assert target_1["hp"] == 0
    assert result_1["slot_state"] == {"level": 3, "remaining": 2}

    target_2 = _goblin("Goblin B")
    result_2 = spells.cast(
        caster,
        None,
        spell="fireball",
        targets=[target_2],
        slot_level=4,
        rng=_fail_save_max_damage,
    )
    assert result_2["damage"][0]["dice"] == "9d6"
    assert result_2["damage"][0]["effective"] == 54


def test_magic_missile_upcast_adds_projectiles(plugins):
    caster = _caster(level=3, known=["magic-missile"])
    spells = _plugin().spells
    first = _goblin("Goblin A")
    result = spells.cast(
        caster, None, spell="magic-missile", targets=[first], slot_level=1, rng=lambda sides: sides
    )
    assert result["attack"] is None
    assert result["damage"][0]["effective"] == 15

    second = _goblin("Goblin B")
    result = spells.cast(
        caster, None, spell="magic-missile", targets=[second], slot_level=2, rng=lambda sides: sides
    )
    assert result["damage"][0]["effective"] == 20


def test_spell_slot_is_consumed(plugins):
    caster = _caster(level=3, known=["magic-missile"])
    _plugin().spells.cast(
        caster, None, spell="magic-missile", targets=[_goblin()], slot_level=1, rng=lambda sides: 1
    )
    assert caster["spell_slots"]["1"]["used"] == 1


def test_missing_slot_returns_error(plugins):
    caster = _caster(level=3, known=["fireball"])
    result = _plugin().spells.cast(
        caster, None, spell="fireball", targets=[_goblin()], slot_level=3, rng=lambda sides: 1
    )
    assert "没有 3 环法术位" in result["error"]


def test_spell_points_replace_slots_when_enabled(plugins):
    settings = resolve_settings(
        {"ruleset_id": "dnd5e", "house_rules": {"dnd5e_spell_points": True}}
    )
    caster = _caster(level=3, known=["magic-missile"])
    spells = _plugin().spells
    first = spells.cast(
        caster,
        None,
        spell="magic-missile",
        targets=[_goblin()],
        slot_level=1,
        rng=lambda sides: 1,
        settings=settings,
    )
    assert "error" not in first
    assert first["slot_state"]["spell_points"] == {
        "max": 14,
        "used": 2,
        "remaining": 12,
        "cost": 2,
    }
    # 法术位本身不被消耗。
    assert caster["spell_slots"]["1"]["used"] == 0
    for _ in range(6):
        cast = spells.cast(
            caster,
            None,
            spell="magic-missile",
            targets=[_goblin()],
            slot_level=1,
            rng=lambda sides: 1,
            settings=settings,
        )
        assert "error" not in cast
    assert caster["spell_points"] == {"max": 14, "used": 14}
    blocked = spells.cast(
        caster,
        None,
        spell="magic-missile",
        targets=[_goblin()],
        slot_level=1,
        rng=lambda sides: 1,
        settings=settings,
    )
    assert "法术点不足" in blocked["error"]


# ---------------------------------------------------------------------------
# 豁免与攻击
# ---------------------------------------------------------------------------
def test_save_spell_halves_on_success(plugins):
    caster = _caster(level=7, known=["fireball"])
    target = _goblin()
    result = _plugin().spells.cast(
        caster, None, spell="fireball", targets=[target], slot_level=3, rng=lambda sides: 20
    )
    assert result["saves"][0]["success"] is True
    assert result["saves"][0]["ability"] == "dex"
    assert result["damage"][0]["effective"] == 24


def test_save_spell_negates_when_not_half(plugins):
    caster = _caster(level=5, known=["sacred-flame"])
    target = _goblin()
    result = _plugin().spells.cast(
        caster, None, spell="sacred-flame", targets=[target], slot_level=0, rng=lambda sides: 20
    )
    assert result["saves"][0]["success"] is True
    assert result["damage"] == []
    assert target["hp"] == target["max_hp"]


def test_cantrip_scales_with_character_level(plugins):
    caster = _caster(level=5, known=["sacred-flame"])
    result = _plugin().spells.cast(
        caster,
        None,
        spell="sacred-flame",
        targets=[_goblin()],
        slot_level=0,
        rng=_fail_save_max_damage,
    )
    assert result["saves"][0]["success"] is False
    assert result["damage"][0]["dice"] == "2d8"
    assert result["damage"][0]["effective"] == 16


def test_spell_and_effect_tools_end_to_end(tmp_path, plugins):
    from trpg_server.agents.tools import combat as combat_tools
    from trpg_server.agents.tools import effects as effect_tools
    from trpg_server.agents.tools import spells as spell_tools

    card = _wizard_card(5, known=["magic-missile"])
    context = _FakeContext(tmp_path, card)
    started = combat_tools.start_combat(
        {
            "participants": [
                "Mira",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started
    combat = combat_tools._load_combat(context)
    if combat["order"][combat["turn"]] != "Mira":
        combat_tools.next_turn({}, context)

    result = spell_tools.cast_spell(
        {"caster": "Mira", "spell": "magic-missile", "targets": ["Goblin"], "slot_level": 1},
        context,
    )
    assert "error" not in result, result
    assert result["damage"]
    state = combat_tools._load_combat(context)
    assert state["combatants"]["Mira"]["budget"]["action"] == 0
    assert state["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1

    applied = effect_tools.apply_effect({"target": "Goblin", "condition": "poisoned"}, context)
    assert "中毒" in applied["summary"]
    state = combat_tools._load_combat(context)
    assert state["combatants"]["Goblin"]["effects"]

    combat_tools.end_combat({}, context)
    state = combat_tools._load_combat(context)
    assert not state


def test_spell_attack_goes_through_check_shell(plugins, monkeypatch):
    import dnd5e_plugin.spells as spells_module

    entry = {
        "id": "test-bolt",
        "name": "Test Bolt",
        "level": 0,
        "school": "evocation",
        "casting_time": "1 action",
        "range": "60 feet",
        "duration": "instantaneous",
        "concentration": False,
        "classes": ["wizard"],
        "save": None,
        "attack": {"type": "ranged"},
        "damage": [{"dice": "1d10", "type": "fire"}],
        "locales": {"zh-CN": {"name": "测试箭"}},
    }
    original = spells_module._pack_entry

    def fake_entry(type_name, identifier):
        if type_name == "spell" and str(identifier).casefold() in {"test-bolt", "测试箭"}:
            return dict(entry)
        return original(type_name, identifier)

    monkeypatch.setattr(spells_module, "_pack_entry", fake_entry)
    caster = _caster(level=5, known=["test-bolt"])
    target = _goblin()
    result = _plugin().spells.cast(
        caster, None, spell="test-bolt", targets=[target], slot_level=0, rng=lambda sides: 20
    )
    assert result["attack"]["kind"] == "attack"
    assert result["attack"]["degree"] == "critical_success"
    assert result["damage"][0]["effective"] == 20


# ---------------------------------------------------------------------------
# 专注
# ---------------------------------------------------------------------------
def test_second_concentration_ends_first(plugins):
    caster = _caster(level=5, known=["hold-person"])
    spells = _plugin().spells
    first = _goblin("Goblin A")
    second = _goblin("Goblin B")
    combat = {"combatants": {first["name"]: first, second["name"]: second}, "round": 1}

    spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[first],
        slot_level=2,
        rng=lambda sides: 1,
        combat=combat,
    )
    assert [item["detail"]["condition"] for item in first["effects"]] == ["paralyzed"]
    first_effect_id = first["effects"][0]["id"]
    assert caster["concentration"]["effects"] == [{"target": "Goblin A", "id": first_effect_id}]

    result = spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[second],
        slot_level=2,
        rng=lambda sides: 1,
        combat=combat,
    )
    assert first["effects"] == []
    assert [item["detail"]["condition"] for item in second["effects"]] == ["paralyzed"]
    assert caster["concentration"]["effects"][0]["target"] == "Goblin B"
    assert any("专注结束" in note for note in result["notes"])


def test_concentration_break_removes_linked_effect(plugins):
    caster = _caster(level=5, known=["hold-person"])
    spells = _plugin().spells
    target = _goblin()
    combat = {"combatants": {target["name"]: target}, "round": 1}
    spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[target],
        slot_level=2,
        rng=lambda sides: 1,
        combat=combat,
    )
    assert target["effects"]

    notes = spells.concentration_check(caster, 22, combat=combat, rng=lambda sides: 1)
    assert "concentration" not in caster
    assert target["effects"] == []
    assert any("专注被打断" in note for note in notes)


def test_concentration_survives_successful_save(plugins):
    caster = _caster(level=5, known=["hold-person"])
    spells = _plugin().spells
    target = _goblin()
    combat = {"combatants": {target["name"]: target}, "round": 1}
    spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[target],
        slot_level=2,
        rng=lambda sides: 1,
        combat=combat,
    )
    notes = spells.concentration_check(caster, 10, combat=combat, rng=lambda sides: 20)
    assert "concentration" in caster
    assert target["effects"]
    assert any("专注检定成功" in note for note in notes)
