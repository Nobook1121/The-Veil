"""dnd5e 法术结构化字段、升环解析与目标限制。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
SPELLS_PATH = PLUGIN_ROOT / "dnd5e-srd" / "packs" / "spells.json"


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


def _schema():
    return importlib.import_module("dnd5e_plugin.spell_schema")


def _spells() -> dict[str, dict]:
    rows = json.loads(SPELLS_PATH.read_text(encoding="utf-8"))
    return {str(row["id"]): row for row in rows}


def _caster(level: int = 3, known=None) -> dict:
    card = _plugin().schema.pack(
        {
            "id": f"wizard-{level}",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "proficiencies": {"saves": ["int", "wis"], "skills": ["arcana"]},
            "hp": {"current": 24, "max": 24},
            "spellcasting": {"known": list(known or [])},
            "equipment": [],
        }
    )
    combatant = {"name": "Mira", "is_player": True, "side": "player", "level": level}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=None)
    return combatant


def _monster(name: str, monster_id: str) -> dict:
    combatant = {"name": name, "is_player": False, "side": "enemy", "monster_id": monster_id}
    _plugin().combat.prepare_combatant(combatant, card=None, settings=None)
    return combatant


def _fail_save(sides: int) -> int:
    return 1 if sides == 20 else sides


def test_describe_structured_fields_and_legacy_fallback(plugins):
    schema = _schema()
    spells = _spells()

    fireball = schema.describe(spells["fireball"])
    assert fireball["activation"]["type"] == "action"
    assert fireball["range"] == {"type": "ranged", "feet": 150}
    assert fireball["duration"]["unit"] == "instant"
    assert fireball["target"]["scope"] == "area"
    assert fireball["area"]["size_ft"] == 20
    assert fireball["resolution"] == {
        "kind": "save",
        "ability": "dex",
        "half_on_success": True,
        "attack_type": "",
    }

    shield = schema.describe(spells["shield"])
    assert shield["activation"]["type"] == "reaction"
    assert shield["duration"] == {
        "value": 1,
        "unit": "round",
        "concentration": False,
        "anchor": "turn_start",
    }
    assert shield["target"]["scope"] == "self"

    hold = schema.describe(spells["hold-person"])
    assert hold["target"]["type"] == "humanoid"
    assert hold["repeat_save"] == {"ability": "wis", "timing": "turn_end"}

    # 旧数据没有新字段时，从 casting_time / range / area / scaling 推断。
    legacy = {
        key: value
        for key, value in spells["fireball"].items()
        if key
        not in {
            "activation",
            "range_spec",
            "duration_spec",
            "target_spec",
            "area_spec",
            "resolution",
            "upcast",
        }
    }
    inferred = schema.describe(legacy)
    assert inferred["activation"]["type"] == "action"
    assert inferred["range"]["feet"] == 150
    assert inferred["target"]["scope"] == "area"
    assert schema.upcast_rules(legacy) == [{"kind": "damage_dice", "per_level": "1d6"}]


def test_upcast_rules_cover_damage_projectiles_targets_and_cantrip(plugins):
    schema = _schema()
    spells = _spells()

    fireball = schema.apply_upcast(spells["fireball"], slot_level=5)
    assert fireball["damage"][0]["dice"] == "10d6"

    missiles = schema.apply_upcast(spells["magic-missile"], slot_level=3)
    assert missiles["projectiles"] == 5

    hold = schema.apply_upcast(spells["hold-person"], slot_level=4)
    assert hold["target_count"] == 3

    flame = schema.apply_upcast(spells["sacred-flame"], caster_level=11)
    assert flame["damage"][0]["dice"] == "3d8"
    low_flame = schema.apply_upcast(spells["sacred-flame"], caster_level=4)
    assert low_flame["damage"][0]["dice"] == "1d8"

    cure = schema.apply_upcast(spells["cure-wounds"], slot_level=3)
    assert cure["healing"]["dice"] == "3d8"


def test_validate_targets_enforces_type_visibility_voluntary_and_range(plugins):
    schema = _schema()
    spells = _spells()
    hold = spells["hold-person"]
    cure = spells["cure-wounds"]
    sacred = spells["sacred-flame"]
    caster = {"name": "Mira", "cell_x": 0, "cell_y": 0}

    wolf = {"name": "Wolf", "creature_type": "beast"}
    errors = schema.validate_targets(hold, caster, [wolf])
    assert errors and "humanoid" in errors[0]

    goblin = {"name": "Goblin", "creature_type": "humanoid", "cell_x": 12, "cell_y": 0}
    assert schema.validate_targets(hold, caster, [goblin]) == []

    barrel = {"name": "Barrel", "is_object": True}
    errors = schema.validate_targets(hold, caster, [barrel])
    assert any("物件" in item for item in errors)

    skeleton = {"name": "Skeleton", "creature_type": "undead"}
    errors = schema.validate_targets(cure, caster, [skeleton])
    assert any("undead" in item for item in errors)

    unwilling = {"name": "Ally", "creature_type": "humanoid", "voluntary": False}
    errors = schema.validate_targets(cure, caster, [unwilling])
    assert any("自愿" in item for item in errors)

    far = {"name": "Goblin", "creature_type": "humanoid", "cell_x": 13, "cell_y": 0}
    errors = schema.validate_targets(hold, caster, [far])
    assert any("射程" in item for item in errors)

    unseen = {"name": "Goblin", "creature_type": "humanoid", "visible": False}
    errors = schema.validate_targets(sacred, caster, [unseen])
    assert any("不可见" in item for item in errors)


def test_cast_rejects_out_of_range_without_consuming_slot(plugins):
    caster = _caster(level=3, known=["hold-person"])
    caster["cell_x"], caster["cell_y"] = 0, 0
    before = caster["spell_slots"]["2"]["used"]

    far = _monster("Far Goblin", "goblin")
    far["cell_x"], far["cell_y"] = 13, 0
    result = _plugin().spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[far],
        slot_level=2,
        rng=_fail_save,
    )
    assert "射程" in result["error"]
    assert caster["spell_slots"]["2"]["used"] == before

    near = _monster("Near Goblin", "goblin")
    near["cell_x"], near["cell_y"] = 12, 0
    result = _plugin().spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[near],
        slot_level=2,
        rng=_fail_save,
    )
    assert "error" not in result
    assert caster["spell_slots"]["2"]["used"] == before + 1
    assert result["effects"][0]["target"] == "Near Goblin"


def test_cast_rejects_non_humanoid_without_side_effects(plugins):
    caster = _caster(level=3, known=["hold-person"])
    wolf = _monster("Wolf", "wolf")
    before = caster["spell_slots"]["2"]["used"]
    result = _plugin().spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=[wolf],
        slot_level=2,
        rng=_fail_save,
    )
    assert "humanoid" in result["error"]
    assert caster["spell_slots"]["2"]["used"] == before
    assert wolf.get("effects") in (None, [])


def test_cast_upcast_increases_target_count(plugins):
    caster = _caster(level=5, known=["hold-person"])
    goblins = [_monster("Goblin A", "goblin"), _monster("Goblin B", "goblin")]
    result = _plugin().spells.cast(
        caster,
        None,
        spell="hold-person",
        targets=goblins,
        slot_level=3,
        rng=_fail_save,
    )
    assert "error" not in result
    assert len(result["effects"]) == 2
    assert all(effect.get("kind") == "condition" for effect in result["effects"])
    assert result["upcast"]["target_count"] == 2
