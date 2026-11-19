"""dnd5e.start_encounter：显式失败与复用核心 combat.start。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests.test_narrative_map_graph import _context

ENCOUNTER = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "plugins"
    / "dnd5e"
    / "dnd5e_plugin"
    / "encounter.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("dnd5e_encounter_test", ENCOUNTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


encounter = _load_plugin()


def test_tool_is_declared_for_dnd5e_only() -> None:
    tools = encounter.create_tools()
    assert [tool.name for tool in tools] == ["dnd5e.start_encounter"]
    assert tools[0].rulesets == ("dnd5e",)


def test_missing_encounter_fails_explicitly(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    result = encounter._start_encounter({"poi_id": "poi_entry"}, context)
    assert result["ok"] is False
    assert result["code"] == "dnd5e.no_encounter"


def test_missing_poi_fails_explicitly(tmp_path) -> None:
    context = _context(tmp_path, location="poi_entry")
    result = encounter._start_encounter({"poi_id": "nope"}, context)
    assert result["ok"] is False
    assert result["code"] == "narrative.poi_not_found"


def test_encounter_passes_monsters_to_core_combat(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start(payload, context):
        captured["payload"] = payload
        return {"ok": True, "active": True}

    monkeypatch.setattr(encounter, "start_combat", fake_start)
    context = _context(tmp_path, location="poi_entry")
    result = encounter._start_encounter({"poi_id": "poi_lair"}, context)
    assert result["ok"] is True
    assert result["auto_start"] is False
    payload = captured["payload"]
    assert payload["surprised"] == []
    monsters = [item for item in payload["participants"] if isinstance(item, dict)]
    assert [item["monster_id"] for item in monsters] == ["goblin", "goblin"]
    assert all(item["is_npc"] is True and item["side"] == "enemy" for item in monsters)

    state = json.loads((context.room_dir / "state.json").read_text(encoding="utf-8"))
    narrative = state.get("narrative") or {}
    assert "poi_lair" in (narrative.get("poi_discovered") or {})
    assert "poi_lair" in (narrative.get("poi_triggered") or {})
