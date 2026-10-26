"""图谱运行工具与 profile/registry 接入。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.state_store import load_narrative_state, save_narrative_state
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.agents.profiles import (
    GRAPH_TOOL_NAMES,
    DEFAULT_KP_TOOLS,
    expand_graph_tool_names,
    resolve_agent_profile,
    with_graph_tools,
)
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.narrative import (
    NARRATIVE_TOOL_NAMES,
    get_current_location,
    lookup_location,
    move_to_location,
    resolve_interaction,
    write_fact,
)
from trpg_server.agents.tools.narrative_triggers import TRIGGER_RUNTIME_TOOL_NAMES


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        scenario_id="s1",
        nodes=[
            NarrativeNode(
                id="room_hall", kind="location", title="门厅",
                summary="门厅里有一盏提灯。",
                objects=[{
                    "id": "obj_lamp", "name": "提灯", "summary": "黄铜提灯。", "visible": True,
                    "visibility": "public",
                    "interactions": [{
                        "id": "int_lamp", "skill": "侦查", "repeatable": True,
                        "outcomes": {"success": {"text": "灯罩上有血迹。"}},
                    }],
                }],
            ),
            NarrativeNode(id="room_cellar", kind="location", title="地下室"),
        ],
        edges=[NarrativeEdge(id="edge_hall_cellar", from_id="room_hall", to_id="room_cellar", type="adjacent")],
    )


def _context(tmp_path: Path) -> SimpleNamespace:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text('{"id": "s1", "modules": []}', encoding="utf-8")
    save_bundle(scenario_dir, _bundle())
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    save_narrative_state(room_dir, {"location": "room_hall", "clock": {"turn": 1}})
    return SimpleNamespace(
        room_dir=room_dir,
        scenarios_dir=scenarios,
        room_id="room-1",
        room_info=lambda: {"scenario_id": "s1"},
    )


def test_registry_contains_narrative_tools() -> None:
    registry = default_tool_registry()
    for name in [*NARRATIVE_TOOL_NAMES, *TRIGGER_RUNTIME_TOOL_NAMES]:
        assert registry.get(name) is not None


def test_graph_profile_names_stay_in_sync() -> None:
    assert GRAPH_TOOL_NAMES == [*NARRATIVE_TOOL_NAMES, *TRIGGER_RUNTIME_TOOL_NAMES]
    expanded = expand_graph_tool_names(DEFAULT_KP_TOOLS, graph_enabled=True)
    assert "room.get_scenario_module" not in expanded
    assert "room.activate_scenario_scene" not in expanded
    assert "trigger.reveal_scenario_trigger" not in expanded
    assert all(name in expanded for name in GRAPH_TOOL_NAMES)

    unchanged = expand_graph_tool_names(DEFAULT_KP_TOOLS, graph_enabled=False)
    assert unchanged == DEFAULT_KP_TOOLS


def test_with_graph_tools_returns_original_profile_when_disabled(tmp_path) -> None:
    profile = resolve_agent_profile({"id": "kp", "tools": ["room.get_room_snapshot"]}, tmp_path / "kp.md")
    assert with_graph_tools(profile, graph_enabled=False) is profile

    expanded = with_graph_tools(profile, graph_enabled=True)
    assert "room.get_room_snapshot" in expanded.tool_names
    assert "room.get_scenario_module" not in expanded.tool_names
    assert all(name in expanded.tool_names for name in GRAPH_TOOL_NAMES)


def test_get_current_location_and_lookup(tmp_path) -> None:
    context = _context(tmp_path)
    card = get_current_location({}, context)
    assert card["title"] == "门厅"
    assert card["objects"][0]["name"] == "提灯"
    assert card["exits"][0]["target_id"] == "room_cellar"

    lookup = lookup_location({"query": "地下"}, context)
    assert lookup["matches"][0]["title"] == "地下室"


def test_move_tool_writes_state(tmp_path) -> None:
    context = _context(tmp_path)
    result = move_to_location({"location": "地下室", "actor": "pc_li"}, context)
    assert result["moved"] is True

    narrative = load_narrative_state(context.room_dir)
    assert narrative["location"] == "room_cellar"
    assert narrative["party_locations"] == {"pc_li": "room_cellar"}


def test_interaction_tool_returns_outcome(tmp_path) -> None:
    context = _context(tmp_path)
    result = resolve_interaction({"object": "提灯", "result": "success", "total": 30}, context)
    assert result["resolved"] is True
    assert result["outcome_text"] == "灯罩上有血迹。"


def test_write_fact_tool_and_canon_conflict(tmp_path) -> None:
    context = _context(tmp_path)
    first = write_fact({"subject": "李默", "predicate": "看到", "object": "血迹"}, context)
    assert first["written"] is True
    assert first["fact"]["status"] == "provisional"

    canon = write_fact(
        {"subject": "李默", "predicate": "看到", "object": "血迹", "status": "canon"},
        context,
    )
    assert canon["written"] is True

    conflict = write_fact(
        {"subject": "李默", "predicate": "看到", "object": "空无一物", "status": "canon"},
        context,
    )
    assert conflict["error"].startswith("canon fact conflict")


def test_narrative_tools_require_graph_room(tmp_path) -> None:
    context = SimpleNamespace(room_dir=tmp_path / "room", scenarios_dir=tmp_path, room_id="r", room_info=lambda: {})
    with pytest.raises(Exception):
        get_current_location({}, context)
