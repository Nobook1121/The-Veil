"""非战斗施法：房间成员结算、仪式、治疗、专注与显式时间推进。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server import room_spells
from trpg_server.agents.tools import effects as effect_tools
from trpg_server.agents.tools import spells as spell_tools
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


def _wizard_card(level: int = 5, known=None) -> dict:
    return _plugin().schema.pack(
        {
            "id": f"wizard-{level}",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 24, "max": 24},
            "spellcasting": {
                "known": list(known or ["cure-wounds", "detect-magic", "hold-person", "shield"])
            },
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


def _room(tmp_path: Path) -> tuple[Path, dict]:
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    info = {
        "id": "noncombat-room",
        "ruleset_id": "dnd5e",
        "house_rules": {},
        "members": [
            {
                "user_id": 1,
                "username": "Mira",
                "is_active": True,
                "character_card": _wizard_card(),
                "character_state": {"current_hp": 24, "max_hp": 24},
            },
            {
                "user_id": 2,
                "username": "Borin",
                "is_active": True,
                "character_card": _fighter_card(),
                "character_state": {"current_hp": 10, "max_hp": 30},
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _member(info: dict, username: str) -> dict:
    return next(item for item in info["members"] if item["username"] == username)


class _Context:
    def __init__(self, room_dir: Path, info: dict):
        self.room_dir = room_dir
        self.room_id = info["id"]
        self.config_dir = room_dir / "config"
        self.user_id = 1
        self._info = info

    def room_info(self) -> dict:
        return self._info


def test_unknown_caster_errors_instead_of_falling_back(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    result = room_spells.cast(
        room_dir,
        info,
        caster="Nobody",
        spell="cure-wounds",
        targets=["Borin"],
        slot_level=1,
        rng=lambda sides: sides,
    )
    assert "找不到施法者" in result.get("error", "")
    mira = _member(info, "Mira")
    slots = mira["character_card"]["sheet"].get("spellcasting", {}).get("slots") or {}
    assert all(int(row.get("used", 0)) == 0 for row in slots.values())

    listing = room_spells.list_spells(info, caster="Nobody")
    assert "找不到施法者" in listing.get("error", "")


# ---------------------------------------------------------------------------
# 治疗与法术位
# ---------------------------------------------------------------------------
def test_noncombat_healing_writes_back_hp_and_slots(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    result = room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="cure-wounds",
        targets=["Borin"],
        slot_level=1,
        rng=lambda sides: sides,
    )
    assert "error" not in result, result
    assert result["noncombat"] is True
    assert result["healing"][0]["healed"] == 11
    borin = _member(info, "Borin")
    assert borin["character_state"]["current_hp"] == 21
    assert borin["character_card"]["sheet"]["hp"]["current"] == 21
    mira = _member(info, "Mira")
    assert mira["character_card"]["sheet"]["spellcasting"]["slots"]["1"]["used"] == 1


# ---------------------------------------------------------------------------
# 仪式与专注
# ---------------------------------------------------------------------------
def test_noncombat_ritual_does_not_consume_slot(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    result = room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="detect-magic",
        ritual=True,
        rng=lambda sides: sides,
    )
    assert "error" not in result, result
    assert result["ritual"] is True
    assert result["slot_state"] is None
    mira = _member(info, "Mira")
    slots = mira["character_card"]["sheet"].get("spellcasting", {}).get("slots") or {}
    assert all(int(row.get("used", 0)) == 0 for row in slots.values())
    assert mira["character_state"]["concentration"]["spell"] == "detect-magic"
    effects = mira["character_state"]["effects"]
    assert effects and effects[0]["detail"]["effects"]["sense_magic_60ft"] is True


def test_noncombat_concentration_break_removes_target_effect(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    cast = room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="hold-person",
        targets=["Borin"],
        slot_level=2,
        rng=lambda sides: 1,
    )
    assert "error" not in cast, cast
    borin = _member(info, "Borin")
    assert [item["detail"]["condition"] for item in borin["character_state"]["effects"]] == [
        "paralyzed"
    ]
    mira = _member(info, "Mira")
    assert mira["character_state"]["concentration"]["effects"][0]["target"] == "Borin"

    status = room_spells.concentration(room_dir, info, member="Mira", action="status")
    assert status["concentration"]["spell"] == "hold-person"

    broken = room_spells.concentration(room_dir, info, member="Mira", action="break")
    assert broken["broken"] is True
    assert "concentration" not in mira["character_state"]
    assert borin["character_state"]["effects"] == []


def test_noncombat_concentration_damage_triggers_check(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    cast = room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="detect-magic",
        ritual=True,
        rng=lambda sides: sides,
    )
    assert "error" not in cast, cast
    mira = _member(info, "Mira")

    held = room_spells.concentration(
        room_dir, info, member="Mira", damage=10, rng=lambda sides: 20
    )
    assert held["broken"] is False
    assert mira["character_state"]["concentration"]["spell"] == "detect-magic"

    broken = room_spells.concentration(
        room_dir, info, member="Mira", damage=30, rng=lambda sides: 1
    )
    assert broken["broken"] is True
    assert "concentration" not in mira["character_state"]
    assert mira["character_state"]["effects"] == []


# ---------------------------------------------------------------------------
# 持续时间与显式推进
# ---------------------------------------------------------------------------
def test_noncombat_timed_effect_expires_on_advance(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    cast = room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="shield",
        slot_level=1,
        rng=lambda sides: sides,
    )
    assert "error" not in cast, cast
    mira = _member(info, "Mira")
    effect = mira["character_state"]["effects"][0]
    assert effect["duration"] == {"unit": "minute", "remaining": 1, "timing": "time_advance"}

    advanced = room_spells.advance_minutes(room_dir, info, minutes=1)
    assert advanced["minutes"] == 1
    assert any("结束" in note for note in advanced["notes"])
    assert mira["character_state"]["effects"] == []


def test_scene_advance_before_duration_keeps_effect(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    room_spells.cast(
        room_dir,
        info,
        caster="Mira",
        spell="detect-magic",
        ritual=True,
        rng=lambda sides: sides,
    )
    advanced = room_spells.advance_minutes(room_dir, info, minutes=30)
    assert advanced["notes"] == []
    mira = _member(info, "Mira")
    assert mira["character_state"]["effects"]


# ---------------------------------------------------------------------------
# 工具路径（无战斗）
# ---------------------------------------------------------------------------
def test_tools_use_noncombat_path_without_combat(tmp_path, plugins):
    room_dir, info = _room(tmp_path)
    context = _Context(room_dir, info)

    listing = spell_tools.list_spells({"caster": "Mira"}, context)
    assert listing["noncombat"] is True
    assert listing["spells"]["spells"]

    cast = spell_tools.cast_spell(
        {"caster": "Mira", "spell": "cure-wounds", "targets": ["Borin"], "slot_level": 1},
        context,
    )
    assert "error" not in cast, cast
    assert cast["noncombat"] is True
    assert _member(info, "Borin")["character_state"]["current_hp"] > 10

    advanced = effect_tools.advance_scene({"minutes": 1}, context)
    assert advanced["minutes"] == 1 and advanced["noncombat"] is True

    status = spell_tools.concentration({"member": "Mira", "action": "status"}, context)
    assert status["member"] == "Mira"


def test_tool_gating_includes_scene_advance_only_for_effects_rulesets(plugins):
    from trpg_server.agents.profiles import build_kp_tool_names

    assert "scene.advance" in build_kp_tool_names("dnd5e")
    assert "scene.advance" not in build_kp_tool_names("coc7")
