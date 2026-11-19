"""M10：剧本 NPC 到 5e 战斗者的桥接。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


@pytest.fixture()
def plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _module(**extra):
    module = {
        "id": "npc_klarg",
        "title": "NPC：克拉格",
        "module_type": "npc",
        "card_type": "npc",
        "monster_id": "bugbear",
    }
    module.update(extra)
    return module


def test_module_npc_uses_pack_baseline(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    spec = combatant_from_module(_module())
    assert spec["name"] == "克拉格"
    assert spec["monster_id"] == "bugbear"
    assert spec["abilities"]["str"] == 15
    assert spec["abilities"]["dex"] == 14
    assert spec["armorClass"] == 16
    assert spec["max_hp"] == 27
    assert spec["hp"] == 27
    assert spec["cr"] == "1"
    assert spec["speed_ft"] == 30
    assert [attack["name"] for attack in spec["attacks"]] == ["Morningstar", "Javelin"]
    assert spec["source"] == "dnd5e-srd"


def test_module_overrides_pack_values(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    spec = combatant_from_module(
        _module(
            abilities={"str": 18},
            armorClass=17,
            maxHp=35,
            attacks=[
                {
                    "name": "Greatclub",
                    "kind": "melee",
                    "attack_bonus": 6,
                    "damage_dice": "2d8+4",
                    "damage_type": "bludgeoning",
                }
            ],
        )
    )
    assert spec["abilities"]["str"] == 18
    assert spec["abilities"]["dex"] == 14
    assert spec["armorClass"] == 17
    assert spec["max_hp"] == 35
    assert spec["hp"] == 35
    assert len(spec["attacks"]) == 1
    assert spec["attacks"][0]["name"] == "Greatclub"
    assert spec["attacks"][0]["attack_bonus"] == 6


def test_missing_monster_fails_explicitly(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    with pytest.raises(ValueError, match="没有怪物"):
        combatant_from_module(_module(monster_id="ancient-red-dragon"))


def test_module_without_any_statblock_fails_explicitly(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    with pytest.raises(ValueError):
        combatant_from_module({"id": "npc_unknown", "title": "NPC：未知"})


def test_ruleset_prepares_combatant_from_module(plugins) -> None:
    rules = builtin_registry().resolve("dnd5e").combat
    combatant = {"name": "NPC：克拉格", "side": "enemy"}
    rules.prepare_combatant_from_module(combatant, _module(), settings=None)

    assert combatant["name"] == "NPC：克拉格"
    assert combatant["is_npc"] is True
    assert combatant["combatant_id"].startswith("npc-")
    assert combatant["abilities"]["dex"] == 14
    assert combatant["armorClass"] == 16
    assert combatant["max_hp"] == 27
    assert combatant["hp"] == 27
    assert combatant["monster_id"] == "bugbear"
    assert combatant["statblock_source"] == "dnd5e-srd"


def test_core_dispatches_module_combatant_to_ruleset(tmp_path, plugins) -> None:
    from types import SimpleNamespace

    from trpg_server.agents.tools import combat

    scenarios = tmp_path / "scenarios" / "s1"
    scenarios.mkdir(parents=True)
    (scenarios / "scenario.json").write_text(
        json.dumps({"id": "s1", "modules": [_module()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    context = SimpleNamespace(
        room_dir=tmp_path / "room",
        room_id="m10-room",
        config_dir=tmp_path / "config",
        scenarios_dir=tmp_path / "scenarios",
        characters_dir=tmp_path / "characters",
        room_info=lambda: {
            "id": "m10-room",
            "ruleset_id": "dnd5e",
            "scenario_id": "s1",
            "house_rules": {},
            "members": [],
        },
    )
    (tmp_path / "room").mkdir()

    started = combat.start_combat(
        {
            "participants": [
                {
                    "module_id": "npc_klarg",
                    "name": "克拉格",
                    "is_npc": True,
                    "side": "enemy",
                }
            ]
        },
        context,
    )
    assert "error" not in started
    state = combat._load_combat(context)
    klarg = state["combatants"]["克拉格"]
    assert klarg["is_npc"] is True
    assert klarg["abilities"]["str"] == 15
    assert klarg["armorClass"] == 16
    assert klarg["max_hp"] == 27
    assert klarg["attacks"][0]["name"] == "Morningstar"
    assert klarg["combatant_id"].startswith("npc-")

