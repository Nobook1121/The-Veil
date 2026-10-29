from __future__ import annotations

from pathlib import Path

from trpg_server.scenario_bundle import BUNDLE_FORMAT
from trpg_server.site_agent_tools import scenarios as tools
from trpg_server.site_agent_tools.base import ToolContext


def _context() -> ToolContext:
    return ToolContext(actor=None, config=None, conversation_id="c", turn_id="t")


def _bundle() -> dict:
    return {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "scenario": {"title": "Agent Sample", "author": "Alice", "playerCount": 1},
        "modules": [
            {"id": "hall", "title": "大厅", "module_type": "scene", "content": "大厅里有一盏灯。"},
            {"id": "finale", "title": "结局", "module_type": "ending", "content": "玩家离开。"},
        ],
        "narrative": {
            "topology": "linear",
            "start": {"location_id": "hall"},
            "nodes": [
                {"id": "hall", "kind": "location", "title": "大厅"},
                {"id": "finale", "kind": "ending", "title": "结局"},
                {"id": "lamp", "kind": "item", "title": "灯"},
            ],
            "edges": [
                {"from": "hall", "to": "finale", "type": "leads_to"},
                {"from": "hall", "to": "lamp", "type": "contains"},
                {"from": "hall", "to": "missing", "type": "adjacent"},
            ],
        },
        "exclusions": [{"prefix": "作者吐槽", "line_start": 3, "category": "author_note", "reason": "与剧情无关"}],
    }


def test_preview_import_script_returns_bundle_stats(monkeypatch):
    """网站 Agent 传 bundle 时不再调用 AI，只做归一化并返回待确认预览。"""
    monkeypatch.setattr(tools.scenario_service, "preview_create", lambda actor, payload: {"summary": "待创建"})
    result = tools.preview_import_script(None, {"bundle": _bundle()}, _context())

    assert result.ok, result.error
    data = result.data
    assert data["modules"] == 2
    assert data["nodes"] >= 3
    assert data["exclusions"][0]["prefix"] == "作者吐槽"
    payload = data["apply_args"]["payload"]
    assert payload["import_mode"] == "ai"
    assert payload["import_exclusions"][0]["reason"] == "与剧情无关"
    assert data["apply_args"]["bundle"]["format"] == BUNDLE_FORMAT


def test_apply_import_script_creates_scenario_and_narrative(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.scenario_service,
        "apply_create",
        lambda actor, payload: {"id": 7, "title": "Agent Sample", "scenario_version": "1.0.0"},
    )
    monkeypatch.setattr(
        tools.scenario_service,
        "descriptor_path_for_actor",
        lambda actor, scenario_id: tmp_path / "scenario.json",
    )
    result = tools.apply_import_script(
        None,
        {"payload": {"title": "Agent Sample"}, "bundle": _bundle()},
        _context(),
    )

    assert result.ok, result.error
    assert result.data["id"] == 7
    assert (tmp_path / "narrative.json").exists()
