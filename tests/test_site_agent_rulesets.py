"""网站 Agent 的规则集分派：工具清单、提示词、导入规则集与建卡 schema。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry
from trpg_server.scenario_bundle import BUNDLE_FORMAT
from trpg_server.site_agent_auth import Actor
from trpg_server.site_agent_tools import exposed_specs
from trpg_server.site_agent_tools.base import ToolContext
from trpg_server.site_agent_tools.characters import get_schema
from trpg_server.site_agent_tools.ruleset import resolve_current_ruleset, site_agent_profile
from trpg_server.site_agent_tools.scenarios import (
    _check_import_script,
    creation_guide,
    preview_import_script,
)

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


def _actor() -> Actor:
    return Actor(
        user_id=1,
        username="alice",
        role="USER",
        permissions=frozenset(
            {"agent.use", "characters.manage_own", "characters.create", "scenarios.preview", "scenarios.create"}
        ),
        room_ids=frozenset({"room-1"}),
        session_current=True,
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(enabled=True, daily_token_budget=0, max_tool_rounds=6)


def _context(ruleset_id: str, *, room: bool = True) -> ToolContext:
    context = ToolContext(
        actor=_actor(),
        config=_config(),
        conversation_id="conversation-1",
        turn_id="turn-1",
        room_id="room-1" if room else None,
        room_info={"room_id": "room-1", "title": "测试房", "ruleset_id": ruleset_id} if room else {"ruleset_id": ruleset_id},
    )
    plugin = resolve_current_ruleset(context)
    context.ruleset_id = plugin.id
    context.ruleset_plugin = plugin
    return context


def _names(context: ToolContext) -> set[str]:
    return {
        spec.name
        for spec in exposed_specs(
            context.actor,
            context.config,
            room_id=context.room_id,
            ruleset_id=context.ruleset_id,
            context=context,
        )
    }


def test_dnd5e_room_gets_plugin_tools_and_character_schema(plugins):
    context = _context("dnd5e")
    names = _names(context)
    assert "dnd5e.roll_check" in names
    assert "content.search" in names
    assert "spell.cast" in names
    assert "combat.attack" in names
    assert "veil-map.move_token" in names
    assert "characters.schema" in names
    assert "scenarios.create_guide" in names

    schema = get_schema(context.actor, {}, context)
    assert schema.ok
    assert schema.data["ruleset_id"] == "dnd5e"
    assert "sections" in schema.data["schema"]


def test_coc7_room_does_not_get_dnd5e_tools(plugins):
    context = _context("coc7")
    names = _names(context)
    assert "dice.roll_coc_check" in names
    assert "san.roll_sanity_check" in names
    assert "combat.shoot" in names
    assert "dnd5e.roll_check" not in names
    assert "spell.cast" not in names
    assert "veil-map.move_token" not in names

    schema = get_schema(context.actor, {}, context)
    assert schema.ok
    assert schema.data["ruleset_id"] == "coc7"
    assert "attributes" not in schema.data["schema"]
    assert schema.data["schema"]["attribute_keys"]


def test_creation_prompt_is_plugin_owned(plugins):
    from trpg_server.site_agent_prompt import build_system_prompt

    dnd = _context("dnd5e")
    dnd_text = build_system_prompt(
        actor=dnd.actor,
        config=dnd.config,
        tools=exposed_specs(dnd.actor, dnd.config, room_id=dnd.room_id, ruleset_id=dnd.ruleset_id, context=dnd),
        room_context=dnd.room_info,
        ruleset_profile=site_agent_profile(dnd.ruleset_plugin),
    )
    assert "D&D 5e" in dnd_text
    assert "不要把 DND 式职业/等级带入" not in dnd_text
    assert "SAN" not in dnd_text or "不要套用" in dnd_text

    coc = _context("coc7")
    coc_text = build_system_prompt(
        actor=coc.actor,
        config=coc.config,
        tools=exposed_specs(coc.actor, coc.config, room_id=coc.room_id, ruleset_id=coc.ruleset_id, context=coc),
        room_context=coc.room_info,
        ruleset_profile=site_agent_profile(coc.ruleset_plugin),
    )
    assert "CoC 7e" in coc_text
    assert "调查员" in coc_text
    assert "D&D 5e" not in coc_text


def test_create_guide_reads_current_ruleset_and_rejects_other_set(plugins):
    context = _context("dnd5e", room=False)
    dnd = creation_guide(context.actor, {"section": "ruleset"}, context)
    assert dnd.ok
    assert dnd.data["ruleset_id"] == "dnd5e"
    assert "D&D 5e 剧本创作规范" in dnd.data["guide"]

    coc = creation_guide(context.actor, {"section": "coc7"}, context)
    assert not coc.ok
    assert "dnd5e" in coc.error.lower() or "D&D 5e" in coc.error


def test_character_save_inherits_current_ruleset(plugins, tmp_path):
    from flask import Flask

    from trpg_server.site_agent_tools.characters import preview_save

    app = Flask(__name__)
    app.config["CHARACTERS_DIR"] = tmp_path / "characters"
    with app.app_context():
        dnd = _context("dnd5e", room=False)
        preview = preview_save(dnd.actor, {"name": "5e 新卡", "fields": {"sheet": {"level": 1}}}, dnd)
        assert preview.ok, preview.error
        assert preview.data["ruleset_id"] == "dnd5e"
        assert preview.data["apply_args"]["payload"]["rulesetId"] == "dnd5e"

        explicit = preview_save(
            dnd.actor,
            {"name": "显式规则卡", "fields": {"rulesetId": "coc7"}},
            dnd,
        )
        assert explicit.ok, explicit.error
        assert explicit.data["apply_args"]["payload"]["rulesetId"] == "coc7"

        coc = _context("coc7", room=False)
        coc_preview = preview_save(coc.actor, {"name": "CoC 新卡"}, coc)
        assert coc_preview.ok, coc_preview.error
        assert coc_preview.data["apply_args"]["payload"]["rulesetId"] == "coc7"


def test_unknown_ruleset_is_rejected_explicitly(plugins):
    context = _context("dnd5e", room=False)
    schema = get_schema(context.actor, {"ruleset_id": "not-a-ruleset"}, context)
    assert not schema.ok
    guide = creation_guide(context.actor, {"ruleset_id": "not-a-ruleset", "section": "ruleset"}, context)
    assert not guide.ok
    assert _check_import_script(context.actor, {"content": "文本", "ruleset_id": "not-a-ruleset"})


def test_import_preview_sets_explicit_ruleset(plugins, tmp_path):
    from flask import Flask

    app = Flask(__name__)
    app.config["SCENARIOS_DIR"] = tmp_path / "scenarios"
    with app.app_context():
        _assert_import_preview_sets_explicit_ruleset()


def _assert_import_preview_sets_explicit_ruleset():
    context = _context("dnd5e", room=False)
    bundle = {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "scenario": {"title": "规则集测试", "ruleset_id": "coc7"},
        "modules": [
            {
                "id": "hall",
                "title": "大厅",
                "module_type": "scene",
                "content": "大厅很暗。",
                "summary": "大厅",
            }
        ],
        "narrative": {
            "topology": "linear",
            "start": {"location_id": "hall"},
            "nodes": [{"id": "hall", "kind": "location", "title": "大厅"}],
            "edges": [],
        },
    }
    result = preview_import_script(context.actor, {"bundle": bundle, "ruleset_id": "dnd5e"}, context)
    assert result.ok, result.error
    assert result.data["ruleset_id"] == "dnd5e"
    assert result.data["apply_args"]["payload"]["ruleset_id"] == "dnd5e"
    assert result.data["apply_args"]["ruleset_id"] == "dnd5e"
    assert any("coc7" in str(item) for item in result.data["warnings"])
