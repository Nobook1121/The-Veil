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
from trpg_server.agents.tools import workflow as workflow_tools
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


@pytest.fixture(autouse=True)
def _reset_area_hooks():
    yield
    from trpg_server.agents.tools import areas as area_tools

    area_tools.install_hooks()
    area_store.register_position_provider(lambda *_args, **_kwargs: None)


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
        # 该文件模拟 KP 驱动整场战斗：NPC 反应/豁免由 KP 代为决定。
        self.agent_id = "kp"
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


def _arm_shield(context: _Context, *, name: str = "Goblin") -> None:
    combat = combat_tools._load_combat(context)
    target = combat["combatants"][name]
    target["spells_known"] = ["shield"]
    target["spell_slots"] = {"1": {"max": 1, "used": 0}}
    target.setdefault("budget", {})["reaction"] = 1
    combat_tools._save_combat(context, combat)


def test_shield_window_turns_hit_into_miss(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_shield(context)
    # Mira 徒手攻击加值 0：18 对 AC 15 命中，护盾 +5 后变未命中。
    awaiting = combat_tools.attack_combatant(
        {
            "attacker": "Mira",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-shield",
        },
        context,
    )
    assert awaiting.get("status") == "awaiting_reaction", awaiting
    flow_id = awaiting["workflow"]["id"]
    assert awaiting["awaiting"] == ["Goblin"]
    pending = reaction_tools.list_reactions({"candidate": "Goblin"}, context)["pending"]
    assert pending and pending[0]["flow_id"] == flow_id

    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Goblin",
            "decision": {"decision": "use"},
            "idempotency_key": "shield-1",
        },
        context,
    )
    continuation = resolved["continuation"]
    assert continuation["shield_used"] is True
    assert continuation["success"] is False
    assert continuation["damage"] is None
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] == hp_before  # 未命中，无伤害
    assert combat["combatants"]["Goblin"]["spell_slots"]["1"]["used"] == 1
    assert combat["combatants"]["Goblin"]["budget"]["reaction"] == 0
    assert combat["combatants"]["Mira"]["budget"]["action"] == 0
    assert WorkflowEngine(tmp_path).get(flow_id)["status"] == "completed"

    # 同一幂等键重复接受：不二次扣位、不二次结算。
    again = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Goblin",
            "decision": {"decision": "use"},
            "idempotency_key": "shield-1",
        },
        context,
    )
    assert again["duplicate"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["spell_slots"]["1"]["used"] == 1
    assert combat["combatants"]["Goblin"]["budget"]["reaction"] == 0


def test_shield_declined_keeps_original_hit(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_shield(context)
    awaiting = combat_tools.attack_combatant(
        {
            "attacker": "Mira",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-shield-decline",
        },
        context,
    )
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": awaiting["workflow"]["id"],
            "candidate": "Goblin",
            "decision": {"decision": "none"},
            "idempotency_key": "shield-decline",
        },
        context,
    )
    continuation = resolved["continuation"]
    assert continuation["shield_used"] is False
    assert continuation["success"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Goblin"]["spell_slots"]["1"]["used"] == 0
    assert combat["combatants"]["Goblin"]["budget"]["reaction"] == 1
    assert WorkflowEngine(tmp_path).get(awaiting["workflow"]["id"])["status"] == "completed"


def test_shield_timeout_uses_default_and_applies_damage(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_shield(context)
    awaiting = combat_tools.attack_combatant(
        {
            "attacker": "Mira",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-shield-timeout",
        },
        context,
    )
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    engine = WorkflowEngine(tmp_path)
    expired = engine.expire(awaiting["workflow"]["id"], is_manager=True)
    assert expired["expired"] is True
    flow = engine.get(awaiting["workflow"]["id"], expire=False)
    assert flow["status"] == "completed"
    assert flow["timed_out"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Goblin"]["spell_slots"]["1"]["used"] == 0
    assert combat["combatants"]["Goblin"]["budget"]["reaction"] == 1


def test_shield_not_offered_on_miss(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_shield(context)
    resolved = combat_tools.attack_combatant(
        {
            "attacker": "Mira",
            "target": "Goblin",
            "attack_roll": 5,
            "request_id": "atk-shield-miss",
        },
        context,
    )
    assert resolved.get("success") is False
    assert resolved.get("status") != "awaiting_reaction"
    assert WorkflowEngine(tmp_path).list(status="waiting") == []


def test_shield_reaction_auto_resolves_without_waiting(tmp_path, plugins):
    """生产攻击路径必须透传 reaction 策略：auto 时不停窗口，直接结算。"""
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_shield(context)
    WorkflowEngine(tmp_path).set_policy({"reaction": {"kp": "auto"}})
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = combat_tools.attack_combatant(
        {
            "attacker": "Mira",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-shield-auto",
        },
        context,
    )
    assert resolved.get("status") == "resolved", resolved
    assert resolved.get("success") is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Goblin"]["spell_slots"]["1"]["used"] == 0
    assert combat["combatants"]["Goblin"]["budget"]["reaction"] == 1
    assert WorkflowEngine(tmp_path).list(status="waiting") == []


def test_counter_spell_reaction_auto_resolves_without_waiting(tmp_path, plugins):
    """生产施法路径必须透传 reaction 策略：auto 时反制候选不阻断施法。"""
    context = _combat_context(tmp_path, [_wizard_card()])
    _arm_counter_spell(context)
    WorkflowEngine(tmp_path).set_policy({"reaction": {"kp": "auto"}})
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 1,
            "request_id": "cast-auto-reaction",
        },
        context,
    )
    assert resolved.get("status") == "resolved", resolved
    assert resolved.get("countered") is not True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 0
    assert WorkflowEngine(tmp_path).list(status="waiting") == []


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
    expired = engine.expire(awaiting["workflow"]["id"], is_manager=True)
    assert expired["expired"] is True
    flow = engine.get(awaiting["workflow"]["id"], expire=False)
    # 超时按“不反制”继续结算，流程应完成而不是永久挂起。
    assert flow["status"] == "completed"
    assert flow["timed_out"] is True
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 0


# ---------------------------------------------------------------------------
# 豁免 ask：流程真的暂停，提交/超时后继续
# ---------------------------------------------------------------------------
def _set_save_policy(context: _Context, policy: str = "ask") -> None:
    WorkflowEngine(context.room_dir).set_policy({"save": {"kp": policy}})


def test_save_ask_pauses_and_player_decision_resolves(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["sacred-flame"])])
    _set_save_policy(context)
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "sacred-flame",
            "targets": ["Goblin"],
            "slot_level": 0,
            "request_id": "save-ask-1",
        },
        context,
    )
    assert awaiting.get("status") == "awaiting_save", awaiting
    flow_id = awaiting["workflow"]["id"]
    assert awaiting["awaiting"] == ["Goblin"]
    flow = WorkflowEngine(tmp_path).get(flow_id)
    assert flow["status"] == "waiting"
    step = next(item for item in flow["steps"] if item["id"] == "save:Goblin")
    assert step["policy"] == "ask"
    assert step["status"] == "waiting"
    assert step["default"] == {"decision": "fail", "reason": "save_timeout"}
    # 停下时还没有产生任何伤害。
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_before

    decided = workflow_tools.decide_workflow(
        {
            "flow_id": flow_id,
            "step_id": "save:Goblin",
            "decision": {"decision": "success", "total": 20},
            "idempotency_key": "save-1",
        },
        context,
    )
    assert decided["duplicate"] is False
    assert decided["workflow"]["status"] == "completed"
    flow = WorkflowEngine(tmp_path).get(flow_id)
    save_result = next(item["result"] for item in flow["steps"] if item["id"] == "save:Goblin")
    assert save_result["decision"] == "success"
    resolved = next(item["result"] for item in flow["steps"] if item["id"] == "resolve")
    assert resolved["saves"][0]["success"] is True
    assert resolved["saves"][0].get("decided") is True
    assert resolved["damage"] == []  # 豁免成功，圣火无伤害
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_before

    again = workflow_tools.decide_workflow(
        {
            "flow_id": flow_id,
            "step_id": "save:Goblin",
            "decision": {"decision": "success"},
            "idempotency_key": "save-1",
        },
        context,
    )
    assert again["duplicate"] is True
    events = workflow_tools.get_workflow({"flow_id": flow_id}, context)["events"]
    assert any(event["type"] == "step.decided" for event in events)
    assert any(event["type"] == "flow.completed" for event in events)


def test_save_ask_timeout_uses_default_fail(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["sacred-flame"])])
    _set_save_policy(context)
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "sacred-flame",
            "targets": ["Goblin"],
            "slot_level": 0,
            "request_id": "save-timeout-1",
        },
        context,
    )
    engine = WorkflowEngine(tmp_path)
    expired = engine.expire(awaiting["workflow"]["id"], is_manager=True)
    assert expired["expired"] is True
    flow = engine.get(awaiting["workflow"]["id"], expire=False)
    assert flow["status"] == "completed"
    assert flow["timed_out"] is True
    step = next(item for item in flow["steps"] if item["id"] == "save:Goblin")
    assert step["result"]["decision"] == "fail"
    assert step["result"]["timeout"] is True
    # 默认失败：伤害照常结算。
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] < hp_before


def test_reaction_then_ask_save_keeps_one_queue(tmp_path, plugins):
    context = _combat_context(tmp_path, [_wizard_card(known=["sacred-flame"])])
    _arm_counter_spell(context)
    _set_save_policy(context)
    awaiting = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "sacred-flame",
            "targets": ["Goblin"],
            "slot_level": 0,
            "request_id": "save-after-counter",
        },
        context,
    )
    assert awaiting.get("status") == "awaiting_reaction", awaiting
    flow_id = awaiting["workflow"]["id"]
    declined = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Rival",
            "decision": {"decision": "none"},
            "idempotency_key": "counter-decline-1",
        },
        context,
    )
    assert declined["continuation"]["status"] == "awaiting_save"
    flow = WorkflowEngine(tmp_path).get(flow_id)
    assert flow["status"] == "waiting"
    save_step = next(item for item in flow["steps"] if item["id"] == "save:Goblin")
    assert save_step["status"] == "waiting"

    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    workflow_tools.decide_workflow(
        {
            "flow_id": flow_id,
            "step_id": "save:Goblin",
            "decision": {"decision": "success"},
            "idempotency_key": "save-after-counter-1",
        },
        context,
    )
    flow = WorkflowEngine(tmp_path).get(flow_id, expire=False)
    assert flow["status"] == "completed"
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_before


# ---------------------------------------------------------------------------
# 高环反制：施法属性检定走判定壳
# ---------------------------------------------------------------------------
def _arm_fourth_slot(context: _Context) -> None:
    combat = combat_tools._load_combat(context)
    combat["combatants"]["Mira"].setdefault("spell_slots", {})["4"] = {"max": 1, "used": 0}
    combat_tools._save_combat(context, combat)


def _cast_at_fourth(context: _Context, request_id: str) -> dict:
    return spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 4,
            "request_id": request_id,
        },
        context,
    )


def test_high_level_counter_check_success_negates_spell(tmp_path, plugins, monkeypatch):
    context = _combat_context(tmp_path, [_wizard_card(known=["magic-missile"])])
    _arm_counter_spell(context)
    _arm_fourth_slot(context)
    monkeypatch.setattr(spell_tools, "_roller", lambda sides: 20)
    awaiting = _cast_at_fourth(context, "counter-high-success")
    assert awaiting.get("status") == "awaiting_reaction", awaiting
    flow_id = awaiting["workflow"]["id"]
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Rival",
            "decision": {"decision": "use"},
            "idempotency_key": "counter-high-ok",
        },
        context,
    )
    continuation = resolved["continuation"]
    assert continuation["countered"] is True
    check = continuation["counter_spell"]
    assert check["dc"] == 14 and check["spell_level"] == 4
    assert check["success"] is True and check["ability"]
    flow = WorkflowEngine(tmp_path).get(flow_id, expire=False)
    assert any(
        event["type"] == "counter.check" and event["payload"]["success"] is True
        for event in flow["events"]
    )
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] == hp_before  # 反制成功，无伤害
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 1
    assert combat["combatants"]["Rival"]["budget"]["reaction"] == 0
    assert combat["combatants"]["Mira"]["spell_slots"]["4"]["used"] == 1


def test_high_level_counter_check_failure_lets_spell_through(tmp_path, plugins, monkeypatch):
    context = _combat_context(tmp_path, [_wizard_card(known=["magic-missile"])])
    _arm_counter_spell(context)
    _arm_fourth_slot(context)
    monkeypatch.setattr(spell_tools, "_roller", lambda sides: 1)
    awaiting = _cast_at_fourth(context, "counter-high-fail")
    flow_id = awaiting["workflow"]["id"]
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    resolved = reaction_tools.resolve_reaction(
        {
            "flow_id": flow_id,
            "candidate": "Rival",
            "decision": {"decision": "use"},
            "idempotency_key": "counter-high-fail-1",
        },
        context,
    )
    continuation = resolved["continuation"]
    assert continuation["countered"] is False
    check = continuation["counter_spell"]
    assert check["dc"] == 14 and check["success"] is False
    flow = WorkflowEngine(tmp_path).get(flow_id, expire=False)
    assert any(
        event["type"] == "counter.check" and event["payload"]["success"] is False
        for event in flow["events"]
    )
    combat = combat_tools._load_combat(context)
    assert combat["combatants"]["Goblin"]["hp"] < hp_before  # 原法术照常生效
    # 反制法术已经施放：反应与 3 环位不退。
    assert combat["combatants"]["Rival"]["spell_slots"]["3"]["used"] == 1
    assert combat["combatants"]["Rival"]["budget"]["reaction"] == 0
    assert combat["combatants"]["Mira"]["spell_slots"]["4"]["used"] == 1


# ---------------------------------------------------------------------------
# 无棋子时的校验退化提示
# ---------------------------------------------------------------------------
def test_guard_notes_surface_in_flow_and_tool_result(tmp_path, plugins):
    from trpg_server.workflows import guards

    def note_guard(payload):
        if str(payload.get("kind")) in {"cast", "attack"}:
            return {"notes": ["本次未做视线与墙体校验（测试退化）"]}
        return []

    guards.register_action_guard(note_guard)
    try:
        context = _combat_context(tmp_path, [_wizard_card()])
        cast = spell_tools.cast_spell(
            {
                "caster": "Mira",
                "spell": "magic-missile",
                "targets": ["Goblin"],
                "slot_level": 1,
                "request_id": "cast-guard-notes",
            },
            context,
        )
        assert any("本次未做视线与墙体校验" in note for note in cast["guard_notes"])
        flow = WorkflowEngine(tmp_path).find_by_request_key("cast-guard-notes")
        resolve = next(step for step in flow["steps"] if step["id"] == "resolve")
        assert any("本次未做视线与墙体校验" in note for note in resolve["result"]["guard_notes"])
        events = workflow_tools.get_workflow({"flow_id": flow["id"]}, context)["events"]
        applied = [
            event for event in events
            if event["type"] == "side_effect.applied" and event["payload"].get("key") == "resolve"
        ]
        assert applied and any(
            "本次未做视线与墙体校验" in note
            for note in applied[0]["payload"]["result"]["guard_notes"]
        )

        combat = combat_tools._load_combat(context)
        combat["combatants"]["Mira"].setdefault("budget", {})["action"] = 1
        combat_tools._save_combat(context, combat)
        attack = combat_tools.attack_combatant(
            {
                "attacker": "Mira",
                "target": "Goblin",
                "attack_roll": 18,
                "request_id": "attack-guard-notes",
            },
            context,
        )
        assert any("本次未做视线与墙体校验" in note for note in attack["guard_notes"])
        attack_flow = WorkflowEngine(tmp_path).find_by_request_key("attack-guard-notes")
        attack_step = next(step for step in attack_flow["steps"] if step["id"] == "attack")
        assert any(
            "本次未做视线与墙体校验" in note
            for note in attack_step["result"]["guard_notes"]
        )
    finally:
        guards.clear_action_guards()


def test_veil_map_guard_marks_missing_tokens(tmp_path, plugins):
    import sys

    module = sys.modules.get("trpg_plugin_veil_map_entry")
    if module is None:
        pytest.skip("veil-map 插件未加载")
    result = module._action_line_guard(
        {
            "room_dir": tmp_path,
            "kind": "cast",
            "actor": "Mira",
            "targets": ["Goblin"],
            "range_ft": 60,
        }
    )
    assert result.get("errors", []) == []
    assert any("本次未做视线与墙体校验" in note for note in result.get("notes", []))


def test_coc7_has_no_reaction_or_area_tools(plugins):
    from trpg_server.agents.profiles import workflow_tool_names

    names = workflow_tool_names("coc7")
    assert names == []
    assert "area.trigger" not in names
    assert "reaction.resolve" not in names
    assert "concentration.break" not in names


# ---------------------------------------------------------------------------
# 区域自动触发：移动进入 / 回合开始 / 去重 / 审计
# ---------------------------------------------------------------------------
def _area_context(tmp_path):
    from trpg_server.agents.tools import areas as area_tools

    area_tools.install_hooks()
    context = _combat_context(tmp_path)
    area_store.register_position_provider(
        lambda _room_dir, _name: {"cell_x": 1, "cell_y": 1, "grid_ft": 5}
    )
    return context


def _make_area(tmp_path, *, rounds=2, trigger="enter_or_start_turn"):
    created = area_store.create_area(
        tmp_path,
        {
            "name": "Spike Growth",
            "shape": "sphere",
            "size_ft": 20,
            "origin": {"cell_x": 1, "cell_y": 1},
            "grid_ft": 5,
            "trigger": trigger,
            "duration": {"unit": "round", "rounds": rounds},
            "damage": [{"dice": "1d6", "type": "piercing"}],
        },
    )
    return created["area"]


def test_area_move_enter_triggers_and_audit_flow(tmp_path, plugins):
    context = _area_context(tmp_path)
    area = _make_area(tmp_path)
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]

    results = area_store.notify_move(
        tmp_path, "Goblin", (5, 5), (1, 1), round_number=1
    )
    assert len(results) == 1 and results[0].get("workflow")
    flow = WorkflowEngine(tmp_path).get(results[0]["workflow"]["id"])
    assert flow["kind"] == "area"
    assert flow["status"] == "completed"
    assert any(event["type"] == "step.completed" for event in flow["events"])
    assert any(event["type"] == "side_effect.applied" for event in flow["events"])
    hp_after = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    assert hp_after < hp_before

    # 同一回合重复进入：自动钩子与手动触发共用去重状态。
    again = area_store.notify_move(tmp_path, "Goblin", (5, 5), (1, 1), round_number=1)
    assert again
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_after
    manual = area_store.trigger(
        tmp_path, area["id"], "Goblin", round_number=1, apply=lambda _a, _t: {"amount": 99}
    )
    assert manual["duplicate"] is True

    # 在区域内移动不触发，离开也不触发。
    assert area_store.notify_move(tmp_path, "Goblin", (1, 1), (2, 1), round_number=1) == []
    assert area_store.notify_move(tmp_path, "Goblin", (2, 1), (6, 6), round_number=1) == []


def test_area_turn_start_triggers_once_per_round(tmp_path, plugins):
    context = _area_context(tmp_path)
    _make_area(tmp_path, rounds=3, trigger="start_turn")
    hp_before = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]

    first = area_store.notify_turn_start(tmp_path, "Goblin", 1, context=context)
    assert first and first[0].get("workflow")
    hp_after = combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"]
    assert hp_after < hp_before
    second = area_store.notify_turn_start(tmp_path, "Goblin", 1, context=context)
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_after
    assert second  # 命中同一条审计流程，不会再次结算

    third = area_store.notify_turn_start(tmp_path, "Goblin", 2, context=context)
    assert third
    assert combat_tools._load_combat(context)["combatants"]["Goblin"]["hp"] <= hp_after


def test_area_ticks_with_combat_round(tmp_path, plugins):
    context = _combat_context(tmp_path)
    area_store.register_position_provider(lambda _room_dir, _name: None)
    _make_area(tmp_path, rounds=1)
    for _ in range(3):
        combat_tools.next_turn({}, context)
    assert area_store.list_areas(tmp_path) == []


def test_area_ticks_with_noncombat_time(tmp_path, plugins):
    context = _Context(tmp_path)
    room_rules.save_room_info(tmp_path, context.room_info())
    area_store.register_position_provider(lambda _room_dir, _name: None)
    _make_area(tmp_path, rounds=1)
    result = effects_tools.advance_scene({"minutes": 10}, context)
    assert "error" not in result, result
    assert area_store.list_areas(tmp_path) == []
    assert result["areas"]["expired"]


def _raw_area(tmp_path, **spec):
    payload = {
        "name": "Area",
        "shape": "sphere",
        "size_ft": 10,
        "origin": {"cell_x": 0, "cell_y": 0},
        "grid_ft": 5,
        "duration": {"unit": "round", "rounds": 2},
    }
    payload.update(spec)
    return area_store.create_area(tmp_path, payload)["area"]


def test_area_trigger_claims_once_under_concurrency(tmp_path, plugins):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    area = _raw_area(tmp_path)
    calls: list[str] = []
    barrier = threading.Barrier(4)

    def apply(_area, target):
        calls.append(target)
        time.sleep(0.05)
        return {"ok": True}

    def run(_index):
        barrier.wait()
        return area_store.trigger(
            tmp_path, area["id"], "Goblin", round_number=1, apply=apply
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))

    assert calls == ["Goblin"]
    assert sum(1 for item in results if not item.get("duplicate")) == 1
    assert sum(1 for item in results if item.get("duplicate")) == 3


def test_area_out_of_combat_dedupes_and_clears_on_exit(tmp_path, plugins):
    area = _raw_area(tmp_path)
    calls: list[int] = []

    def apply(_area, _target):
        calls.append(1)
        return {"ok": True}

    first = area_store.trigger(tmp_path, area["id"], "Goblin", round_number=0, apply=apply)
    assert not first["duplicate"]
    second = area_store.trigger(tmp_path, area["id"], "Goblin", round_number=0, apply=apply)
    assert second["duplicate"] is True
    assert len(calls) == 1

    assert area_store.clear_trigger(tmp_path, area["id"], "Goblin") is True
    third = area_store.trigger(tmp_path, area["id"], "Goblin", round_number=0, apply=apply)
    assert not third["duplicate"]
    assert len(calls) == 2


def test_area_trigger_apply_failure_is_audited_not_swallowed(tmp_path, plugins):
    area = _raw_area(tmp_path)

    def apply(_area, _target):
        raise RuntimeError("damage pipeline exploded")

    result = area_store.trigger(tmp_path, area["id"], "Goblin", round_number=1, apply=apply)
    assert result["result"]["code"] == "area.apply_failed"
    stored = area_store.list_areas(tmp_path)[0]
    assert stored["last_trigger_error"]


def test_area_handler_exception_returns_degraded_result(tmp_path, plugins):
    from trpg_server.agents.tools import areas as area_tools

    area_tools.install_hooks()
    _raw_area(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("handler exploded")

    area_store.register_trigger_handler(boom)
    results = area_store.notify_move(
        tmp_path, "Goblin", (9, 9), (0, 0), round_number=1
    )
    assert len(results) == 1
    assert results[0]["code"] == "area.trigger_failed"
    assert results[0]["degraded"] is True


def test_area_geometry_cone_line_and_cylinder(tmp_path, plugins):
    origin = {"cell_x": 0, "cell_y": 0}

    cone = {
        "shape": "cone",
        "origin": origin,
        "size_ft": 15,
        "grid_ft": 5,
        "direction": "e",
    }
    assert area_store.contains(cone, 1, 0)
    assert area_store.contains(cone, 3, 0)
    assert not area_store.contains(cone, 1, 1)
    assert not area_store.contains(cone, -1, 0)

    cone_north = {**cone, "direction": "north"}
    assert area_store.contains(cone_north, 0, -2)
    assert not area_store.contains(cone_north, 0, 2)

    # 没有朝向时锥形退化为同尺寸圆形，而不是把整块方形都算进去。
    cone_free = {
        "shape": "cone",
        "origin": origin,
        "size_ft": 15,
        "grid_ft": 5,
    }
    assert area_store.contains(cone_free, 0, 2)
    assert area_store.contains(cone_free, 0, 3)

    line = {
        "shape": "line",
        "origin": origin,
        "size_ft": 15,
        "width_ft": 5,
        "grid_ft": 5,
        "direction": "e",
    }
    assert area_store.contains(line, 1, 0)
    assert area_store.contains(line, 3, 0)
    assert not area_store.contains(line, 1, 1)
    assert not area_store.contains(line, -1, 0)

    cylinder = {
        "shape": "cylinder",
        "origin": origin,
        "size_ft": 10,
        "grid_ft": 5,
    }
    assert area_store.contains(cylinder, 0, 2)
    assert not area_store.contains(cylinder, 2, 2)


def test_area_create_preserves_direction(tmp_path, plugins):
    area = area_store.create_area(
        tmp_path,
        {
            "name": "North line",
            "shape": "line",
            "size_ft": 15,
            "width_ft": 5,
            "origin": {"cell_x": 0, "cell_y": 0},
            "grid_ft": 5,
            "direction": "north",
            "duration": {"unit": "round", "rounds": 2},
        },
    )["area"]
    assert area["direction"] == "north"
    assert area_store.contains(area, 0, -2)
    assert not area_store.contains(area, 0, 2)


def test_area_move_uses_full_token_footprint(tmp_path, plugins):
    from trpg_server.agents.tools import areas as area_tools

    area_tools.install_hooks()
    area = area_store.create_area(
        tmp_path,
        {
            "name": "Edge",
            "shape": "sphere",
            "size_ft": 5,
            "origin": {"cell_x": 5, "cell_y": 5},
            "grid_ft": 5,
            "trigger": "enter",
            "duration": {"unit": "round", "rounds": 2},
        },
    )["area"]
    seen: list[str] = []

    def record(_room_dir, row, actor, _round, _hook, _context):
        seen.append(str(row.get("id")))
        return {"area": row.get("id")}

    area_store.register_trigger_handler(record)
    # 3x1 棋子的锚点在 (3,5)，但最右一格 (5,5) 已进入区域。
    results = area_store.notify_move(
        tmp_path, "Giant", (0, 5), (3, 5), round_number=1, size=(3, 1)
    )
    assert seen == [area["id"]]
    assert results and results[0]["area"] == area["id"]

    # footprint 仍与区域重叠时继续移动不重复触发。
    area_store.notify_move(
        tmp_path, "Giant", (3, 5), (3, 6), round_number=1, size=(3, 1)
    )
    assert seen == [area["id"]]


def test_clear_plugin_runtime_clears_area_providers(tmp_path, plugins):
    area_store.register_position_provider(
        lambda _room_dir, _name: {"cell_x": 1, "cell_y": 1}
    )
    area_store.register_scene_id_provider(lambda _room_dir: "scene-x")
    assert area_store.scene_id_of(tmp_path) == "scene-x"

    runtime.clear_plugin_runtime()

    assert area_store.lookup_position(tmp_path, "Goblin") is None
    assert area_store.scene_id_of(tmp_path) == "default"


def test_area_scene_provider_drives_scene_isolation(tmp_path, plugins):
    area_store.register_scene_id_provider(lambda _room_dir: "scene-2")
    try:
        area = _raw_area(tmp_path)
        assert area["scene_id"] == "scene-2"
        assert area_store.list_areas(tmp_path, scene_id="scene-2")
        assert area_store.list_areas(tmp_path, scene_id="default") == []
    finally:
        area_store.clear_scene_id_provider()


def test_guard_exception_degrades_or_fails_closed():
    from pathlib import Path

    from trpg_server.workflows import guards

    def boom(_payload):
        raise RuntimeError("guard exploded")

    try:
        guards.register_action_guard(boom)
        errors, notes = guards.validate_action_report(
            Path("."), kind="attack", actor="Mira"
        )
        assert errors == []
        assert any("降级放行" in note for note in notes)

        guards.clear_action_guards()
        guards.register_action_guard(boom, critical=True)
        errors, notes = guards.validate_action_report(
            Path("."), kind="attack", actor="Mira"
        )
        assert errors and not notes
        assert "已阻止本次行动" in errors[0]
    finally:
        guards.clear_action_guards()
