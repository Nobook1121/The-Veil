"""E5/E6 核心：通用效果容器、持续时间 tick 与资源阈值接线。"""

from __future__ import annotations

from trpg_server.ruleset.effects import (
    add_effect,
    apply_resource_delta,
    clear_effects,
    effect_summary,
    effects_of,
    remove_effect,
    tick_holder,
)
from trpg_server.ruleset.resources import ResourceDelta


class _Schema:
    def resource_slots(self):
        return [
            {
                "kind": "hp",
                "state": "current_hp",
                "mirror": "currentHp",
                "cap_state": "max_hp",
                "cap_min": 0,
                "thresholds": [{"at": 0, "when": "below", "event": "zero_hp"}],
            }
        ]


class _Effects:
    def __init__(self) -> None:
        self.ticks = []
        self.expired = []
        self.thresholds = []

    def tick(self, holder, effect, *, phase, timing=None, combat=None, rng=None):
        self.ticks.append((effect.get("id"), phase, timing))
        if effect.get("detail", {}).get("stop"):
            return {"remove": True, "notes": ["手动结束"]}
        return {}

    def on_expired(self, holder, effect):
        self.expired.append(effect.get("id"))
        return [f"{effect.get('id')} 结束"]

    def on_threshold(self, holder, event, *, combat=None, rng=None):
        self.thresholds.append(event["event"])
        return [f"阈值 {event['event']}"]


class _Plugin:
    def __init__(self) -> None:
        self.schema = _Schema()
        self.effects = _Effects()


def test_add_effect_normalizes_and_merges_same_source():
    holder: dict = {}
    add_effect(holder, {"id": "a", "kind": "condition", "source": "spell:x", "stacks": 1})
    add_effect(holder, {"id": "b", "kind": "condition", "source": "spell:x", "stacks": 2})
    effects = effects_of(holder)
    assert len(effects) == 1
    assert effects[0]["stacks"] == 3
    assert effect_summary(holder)[0]["kind"] == "condition"


def test_remove_effect_by_kind_and_scope():
    holder: dict = {}
    add_effect(holder, {"id": "a", "kind": "condition", "scope": "character"})
    add_effect(holder, {"id": "b", "kind": "buff", "scope": "party"})
    removed = remove_effect(holder, kind="condition")
    assert [item["id"] for item in removed] == ["a"]
    assert [item["id"] for item in effects_of(holder)] == ["b"]
    assert remove_effect(holder, kind="missing") == []


def test_tick_holder_respects_timing_and_expires_with_hook():
    plugin = _Plugin()
    holder: dict = {
        "name": "Aria",
        "effects": [
            {
                "id": "e1",
                "kind": "condition",
                "duration": {"unit": "turn", "remaining": 2, "timing": "turn_end"},
            }
        ],
    }
    notes, expired = tick_holder(holder, phase="turn", timing="turn_start", plugin=plugin)
    assert notes == [] and expired == []
    assert holder["effects"][0]["duration"]["remaining"] == 2
    assert plugin.effects.ticks == [("e1", "turn", "turn_start")]

    notes, expired = tick_holder(holder, phase="turn", timing="turn_end", plugin=plugin)
    assert notes == [] and expired == []
    assert holder["effects"][0]["duration"]["remaining"] == 1
    assert plugin.effects.ticks[-1] == ("e1", "turn", "turn_end")

    notes, expired = tick_holder(holder, phase="turn", timing="turn_end", plugin=plugin)
    assert expired and expired[0]["id"] == "e1"
    assert notes == ["e1 结束"]
    assert plugin.effects.expired == ["e1"]
    assert holder["effects"] == []


def test_tick_holder_plugin_can_remove_effect():
    plugin = _Plugin()
    holder: dict = {
        "effects": [
            {
                "id": "e2",
                "kind": "condition",
                "duration": {"unit": "turn", "remaining": 3, "timing": "turn_end"},
                "detail": {"stop": True},
            }
        ]
    }
    notes, expired = tick_holder(holder, phase="turn", timing="turn_end", plugin=plugin)
    assert expired and expired[0]["id"] == "e2"
    assert "手动结束" in notes
    assert holder["effects"] == []


def test_apply_resource_delta_uses_slot_state_and_emits_threshold():
    plugin = _Plugin()
    holder = {"name": "Aria", "current_hp": 5, "max_hp": 20}
    change, notes = apply_resource_delta(
        holder, plugin, "hp", ResourceDelta(kind="hp", delta=-10, reason="damage")
    )
    assert change is not None
    assert holder["current_hp"] == 0
    assert change.after == 0
    assert [event.event for event in change.events] == ["zero_hp"]
    assert notes == ["阈值 zero_hp"]
    assert plugin.effects.thresholds == ["zero_hp"]


def test_apply_resource_delta_reads_combatant_hp_key():
    plugin = _Plugin()
    holder = {"name": "Goblin", "hp": 7, "max_hp": 7}
    change, _notes = apply_resource_delta(holder, plugin, "hp", ResourceDelta(kind="hp", delta=-2))
    assert change is not None
    assert holder["hp"] == 5
    assert "current_hp" not in holder


def test_tick_holder_applies_effect_resource_through_thresholds():
    plugin = _Plugin()
    holder = {
        "name": "Aria",
        "hp": 4,
        "max_hp": 20,
        "effects": [
            {
                "id": "dot",
                "kind": "damage_over_time",
                "duration": {"unit": "turn", "remaining": 2, "timing": "turn_end"},
                "detail": {"resource": {"kind": "hp", "delta": -6}},
            }
        ],
    }

    class _TickEffects(_Effects):
        def tick(self, holder, effect, *, phase, timing=None, combat=None, rng=None):
            return {"resource": {"kind": "hp", "delta": -6}}

    plugin.effects = _TickEffects()
    notes, expired = tick_holder(holder, phase="turn", timing="turn_end", plugin=plugin)
    assert expired == []
    assert holder["hp"] == 0
    assert "阈值 zero_hp" in notes
    assert plugin.effects.thresholds == ["zero_hp"]


def test_clear_effects_keeps_rest_and_irreversible():
    holder: dict = {
        "effects": [
            {"id": "a", "kind": "condition", "persistence": "removable"},
            {"id": "b", "kind": "exhaustion", "persistence": "rest"},
            {"id": "c", "kind": "curse", "persistence": "irreversible"},
        ]
    }
    clear_effects(holder, plugin=_Plugin())
    assert [item["id"] for item in holder["effects"]] == ["b", "c"]
