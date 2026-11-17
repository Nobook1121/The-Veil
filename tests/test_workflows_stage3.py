"""第三段：持续区域、专注注册表与反应中断队列。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server import room_rules
from trpg_server.agents.tools import combat as combat_tools
from trpg_server.agents.tools import concentration as concentration_tools
from trpg_server.agents.tools import effects as effects_tools
from trpg_server.agents.tools import reactions as reaction_tools
from trpg_server.agents.tools import spells as spell_tools
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.workflows import areas as area_store
from trpg_server.workflows import concentration
from trpg_server.workflows.engine import WorkflowEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


@pytest.fixture
def plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    PluginRegistry(PLUGIN_ROOT).load()
    yield
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
            "spellcasting": {"known": list(known or ["magic-missile"])},
            "equipment": [],
        }
    )


class _Context:
    def __init__(self, room_dir: Path, card: dict | None = None):
        self.room_dir = room_dir
        self.room_id = "stage3-room"
        self.config_dir = room_dir / "config"
        self.scenarios_dir = room_dir / "scenarios"
        self.user_id = 1
        self.agent_id = ""
        self._card = card or _wizard_card()

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "members": [
                {
                    "user_id": 1,
                    "username": "Mira",
                    "is_active": True,
                    "character_card": self._card,
                    "character_state": {},
                }
            ],
        }


def _combat_context(tmp_path, cards: list[dict] | None = None) -> _Context:
    context = _Context(tmp_path)
    if cards:
        context._card = cards[0]
    room_rules.save_room_info(tmp_path, context.room_info())
    started = combat_tools.start_combat(
        {
            "participants": [
                "Mira",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
                {"name": "Rival", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started, started
    combat = combat_tools._load_combat(context)
    if combat["order"][combat["turn"]] != "Mira":
        combat_tools.next_turn({}, context)
    return context


# ---------------------------------------------------------------------------
# 持续区域
# ---------------------------------------------------------------------------
def test_area_trigger_dedupes_per_target_per_round(tmp_path):
    created = area_store.create_area(
        tmp_path,
        {
            "name": "Spike Growth",
            "shape": "sphere",
            "size_ft": 20,
            "duration": {"unit": "round", "rounds": 3},
            "damage": [{"dice": "1d4", "type": "piercing"}],
        },
    )
    area_id = created["area"]["id"]
    first = area_store.trigger(
        tmp_path,
        area_id,
        "Goblin",
        round_number=1,
        apply=lambda area, target: {"amount": 4},
    )
    assert first["duplicate"] is False
    assert first["result"] == {"amount": 4}
    again = area_store.trigger(
        tmp_path,
        area_id,
        "Goblin",
        round_number=1,
        apply=lambda area, target: {"amount": 99},
    )
    assert again["duplicate"] is True
    next_round = area_store.trigger(
        tmp_path,
        area_id,
        "Goblin",
        round_number=2,
        apply=lambda area, target: {"amount": 5},
    )
    assert next_round["duplicate"] is False
    assert next_round["result"] == {"amount": 5}


def test_area_duration_ticks_and_expires(tmp_path):
    created = area_store.create_area(
        tmp_path,
        {"name": "Fog", "shape": "sphere", "size_ft": 10, "duration": {"unit": "round", "rounds": 2}},
    )
    area_id = created["area"]["id"]
    assert area_store.tick_areas(tmp_path, rounds=1)["expired"] == []
    assert area_store.list_areas(tmp_path)[0]["duration"]["remaining"] == 1
    expired = area_store.tick_areas(tmp_path, rounds=1)
    assert expired["expired"][0]["id"] == area_id
    assert area_store.list_areas(tmp_path) == []


def test_cast_persistent_spell_creates_scene_area(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["moonbeam"])])
    result = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "moonbeam",
            "targets": ["Goblin"],
            "slot_level": 2,
            "request_id": "moonbeam-1",
        },
        context,
    )
    assert "error" not in result, result
    area = result.get("area")
    assert area and area["shape"] == "cylinder"
    assert area["duration"]["unit"] == "round"
    assert area["duration"]["rounds"] > 0
    assert area_store.list_areas(tmp_path)[0]["id"] == area["id"]


def test_area_trigger_applies_damage_once_per_round(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["moonbeam"])])
    result = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "moonbeam",
            "targets": ["Goblin"],
            "slot_level": 2,
            "request_id": "moonbeam-2",
        },
        context,
    )
    area_id = result["area"]["id"]
    before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    first = reaction_safe_area_trigger(context, area_id, "Goblin", 1)
    assert first["duplicate"] is False
    after_first = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    assert after_first <= before
    second = reaction_safe_area_trigger(context, area_id, "Goblin", 1)
    assert second["duplicate"] is True
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == after_first


def reaction_safe_area_trigger(context, area_id, target, round_number):
    from trpg_server.agents.tools import areas as area_tools

    return area_tools.trigger_area(
        {"area_id": area_id, "target": target, "round": round_number}, context
    )


# ---------------------------------------------------------------------------
# 专注注册表
# ---------------------------------------------------------------------------
def test_concentration_registry_and_incapacitation_break(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["hold-person"])])
    cast = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "hold-person",
            "targets": ["Goblin"],
            "slot_level": 2,
            "request_id": "hold-1",
        },
        context,
    )
    assert "error" not in cast, cast
    active = concentration_tools.list_concentration({}, context)["active"]
    assert [row["holder"] for row in active] == ["Mira"]
    assert active[0]["spell"]

    stunned = effects_tools.apply_effect({"target": "Mira", "condition": "stunned"}, context)
    assert "error" not in stunned, stunned
    combat = combat_tools._load_combat(context)
    assert not combat["combatants"]["Mira"].get("concentration")
    assert concentration_tools.list_concentration({}, context)["active"] == []


def test_concentration_break_tool_cleans_linked_effect(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["hold-person"])])
    spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "hold-person",
            "targets": ["Goblin"],
            "slot_level": 2,
            "request_id": "hold-2",
        },
        context,
    )
    combat = combat_tools._load_combat(context)
    if not combat["combatants"]["Goblin"].get("effects"):
        # 豁免成功时没有附加效果；手动补一个由专注维持的效果用于验证清理。
        effects_tools.apply_effect(
            {
                "target": "Goblin",
                "effect": {
                    "id": "hold-person-test",
                    "kind": "spell_effect",
                    "scope": "character",
                    "source": "hold-person",
                    "duration": {"unit": "round", "remaining": 10},
                    "persistence": "removable",
                },
            },
            context,
        )
        combat = combat_tools._load_combat(context)
        combat["combatants"]["Mira"]["concentration"] = {
            "spell": "hold-person",
            "name": "Hold Person",
            "level": 2,
            "dc": 14,
            "round": 1,
            "effects": [{"target": "Goblin", "id": "hold-person-test"}],
        }
        combat_tools._save_combat(context, combat)
        concentration.sync(
            context.room_dir, concentration.holders_from_combat(combat)
        )
    broken = concentration_tools.break_concentration(
        {"holder": "Mira", "reason": "KP 裁定"}, context
    )
    assert broken["broken"] is True
    assert concentration_tools.list_concentration({}, context)["active"] == []


# ---------------------------------------------------------------------------
# 反应中断队列
# ---------------------------------------------------------------------------
def _arm_counter_spell(context: _Context) -> None:
    combat = combat_tools._load_combat(context)
    rival = combat["combatants"]["Rival"]
    rival["spells_known"] = ["counter-spell"]
    rival["spell_slots"] = {"3": {"max": 1, "used": 0}}
    rival.setdefault("budget", {})["reaction"] = 1
    combat_tools._save_combat(context, combat)


def test_counterspell_window_pauses_and_consumes_slot_without_effect(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_counter_spell(context)
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 1,
            "request_id": "cast-counter",
        },
        context,
    )
    assert awaiting.get("status") == "awaiting_reaction", awaiting
    flow_id = awaiting["workflow"]["id"]
    assert "Rival" in awaiting["awaiting"]
    pending = reaction_tools.list_reactions({"candidate": "Rival"}, context)["pending"]
    assert pending and pending[0]["flow_id"] == flow_id

    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Rival",
            "decision": {"decision": "use"},
            "idempotency_key": "counter-1",
        },
        context,
    )
    assert resolved["countered"] is True
    assert resolved["countered_by"] == "Rival"
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] == hp_before  # 无效果
    assert combat["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1  # 法术位不退
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 1
    assert combat["combatants"]["Rival"]["budget"]["reaction"] == 0
    assert WorkflowEngine(tmp_path).get(flow_id)["status"] == "completed"

    # 同一幂等键重复提交不二次扣位。
    again = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Rival",
            "decision": {"decision": "use"},
            "idempotency_key": "counter-1",
        },
        context,
    )
    assert again["duplicate"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1


def test_reaction_declined_continues_cast(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_counter_spell(context)
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 1,
            "request_id": "cast-decline",
        },
        context,
    )
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": awaiting["workflow"]["id"],
            "candidate": "Rival",
            "decision": {"decision": "none"},
            "idempotency_key": "decline-1",
        },
        context,
    )
    assert resolved["countered"] is False
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1
    assert WorkflowEngine(tmp_path).get(awaiting["workflow"]["id"])["status"] == "completed"


def test_reaction_timeout_uses_default_and_continues(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_counter_spell(context)
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 1,
            "request_id": "cast-timeout",
        },
        context,
    )
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    engine = WorkflowEngine(tmp_path)
    expired = engine.expire(awaiting["workflow"]["id"])
    assert expired["expired"] is True
    flow = engine.get(awaiting["workflow"]["id"], expire=False)
    # 超时按“不反制”继续结算，流程应完成而不是永久挂起。
    assert flow["status"] == "completed"
    assert flow["timed_out"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 0


def test_coc7_has_no_reaction_or_area_tools(plugins):
    from trpg_server.agents.profiles import workflow_tool_names

    names = workflow_tool_names("coc7")
    assert names == []
    assert "area.trigger" not in names
    assert "reaction.resolve" not in names
    assert "concentration.break" not in names
