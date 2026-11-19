"""E3：dnd5e 规则集插件本体。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server import check_service, character_service
from trpg_server.app_factory import create_app
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.capabilities import card_descriptor_of, workspace_of
from trpg_server.ruleset.contracts import CheckRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


class _AdminActor:
    user_id = 7
    role = "ADMIN"

    def has(self, *_args, **_kwargs) -> bool:
        return True


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


def _card(plugin) -> dict:
    return plugin.schema.pack(
        {
            "id": "dnd-card-1",
            "name": "艾拉",
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics", "perception"]},
            "hp": {"current": 20, "max": 24},
        }
    )


def test_card_without_ruleset_inherits_room_ruleset(plugins):
    from trpg_server.room_ops.cards import _sanitize_character_card

    card = _sanitize_character_card(
        {"id": "card-x", "name": "无规则卡", "maxHp": 10, "maxSan": 0},
        room_info={"ruleset_id": "dnd5e"},
    )
    assert card["rulesetId"] == "dnd5e"

    explicit = _sanitize_character_card(
        {"id": "card-y", "name": "显式规则卡", "rulesetId": "coc7"},
        room_info={"ruleset_id": "dnd5e"},
    )
    assert explicit["rulesetId"] == "coc7"


def test_enabled_state_turns_on_ruleset_and_srd_pack():
    data = json.loads((PLUGIN_ROOT / "enabled.json").read_text(encoding="utf-8"))
    assert {"dnd5e", "dnd5e-srd"} <= set(data["enabled"])


def test_plugin_page_and_ruleset_registration(plugins):
    srd = plugins.get("dnd5e-srd")
    dnd = plugins.get("dnd5e")
    assert srd is not None and srd.loaded and "content_packs" in srd.loaded_capabilities
    assert dnd is not None and dnd.loaded and "rulesets" in dnd.loaded_capabilities
    assert dnd.reason == ""

    registry = builtin_registry()
    assert "dnd5e" in registry.ids()
    plugin = registry.resolve("dnd5e")
    assert plugin.id == "dnd5e"
    assert registry.resolve_explicit("dnd5e").status == "available"
    assert any(item["id"] == "dnd5e" and item["selectable"] for item in registry.describe())

    library = runtime.content_packs_for("dnd5e")
    assert library.pack_ids == ("dnd5e-srd-5-1",)
    assert library.get("dnd5e", "skill", "athletics")["name"] == "Athletics"


def test_workspace_declaration_is_canvas_with_agreed_sizes(plugins):
    workspace = workspace_of(builtin_registry().resolve("dnd5e"))
    assert workspace.primary == "canvas"
    assert workspace.primary_min_width == 720
    assert workspace.sidebar.width == 380
    assert (workspace.sidebar.min_width, workspace.sidebar.max_width) == (360, 420)
    assert workspace.topbar.height == 48
    assert workspace.topbar.slots == ("scene", "players", "turn", "status", "settings")
    assert workspace.left_rail.width == 48 and workspace.left_rail.visible
    assert workspace.panels == {"chat": "sidebar", "character": "drawer", "combat": "topbar"}


def test_card_descriptor_exposes_5e_fields(plugins):
    descriptor = card_descriptor_of(builtin_registry().resolve("dnd5e"))
    assert descriptor["descriptor_version"] == 2
    assert descriptor["ruleset_id"] == "dnd5e"
    assert {"abilities", "proficiencies", "hp", "armorClass", "level"} <= set(descriptor["sheet_fields"])
    assert {section["key"] for section in descriptor["sections"]} >= {"identity", "abilities", "combat"}
    fields = {item["key"] for item in descriptor["fields"]}
    assert {"level", "class", "hp", "armorClass", "str", "dex", "con"} <= fields


def test_card_save_roundtrip_keeps_sheet_for_dnd5e(plugins, tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "PLUGIN_DIR": str(PLUGIN_ROOT),
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOMS_DIR": tmp_path / "rooms",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "WEAPONS_DIR": tmp_path / "weapons",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
        }
    )
    payload = {
        "name": "艾拉",
        "rulesetId": "dnd5e",
        "sheet": {
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics"]},
            "hp": {"current": 20, "max": 24},
            "armorClass": 16,
        },
    }
    with app.app_context():
        saved = character_service.apply_save(_AdminActor(), "dnd-card-1001", payload)

    assert saved["rulesetId"] == "dnd5e"
    assert saved["sheet"]["abilities"]["str"] == 16
    assert saved["sheet"]["proficiencies"]["skills"] == ["athletics"]

    stored = json.loads(
        (tmp_path / "characters" / "dnd-card-1001" / "character.json").read_text(encoding="utf-8")
    )
    assert stored["rulesetId"] == "dnd5e"
    assert stored["rulesetSchemaVersion"] == 1
    assert stored["sheet"]["level"] == 3
    assert stored["sheet"]["hp"] == {"current": 20, "max": 24, "temp": 0}
    assert stored["maxHp"] == 24 and stored["currentHp"] == 20
    assert stored["armorClass"] == 16
    assert stored["proficiencyBonus"] == 2


def test_dnd5e_declares_prompt_fragments_for_noncombat_casting(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    fragments = plugin.tools.prompt_fragments()
    assert fragments
    assert any("scene.advance" in item for item in fragments)
    assert any("spell.concentration" in item for item in fragments)
    assert plugin.tools.tool_names() == ("dnd5e.roll_check", "dnd5e.start_encounter")


def test_coc7_prompt_fragments_stay_empty(plugins):
    plugin = builtin_registry().resolve("coc7")
    assert plugin.tools.prompt_fragments() == []


def test_describe_exposes_ruleset_dice_without_crossing_semantics(plugins):
    items = {row["id"]: row for row in builtin_registry().describe()}
    assert items["dnd5e"]["dice"]["type"] == "d20"
    assert items["dnd5e"]["dice"]["expression"] == "1d20"
    assert items["dnd5e"]["dice"]["critical"] == [20]
    assert items["dnd5e"]["dice"]["fumble"] == [1]
    assert items["coc7"]["dice"]["type"] == "d100"
    assert items["coc7"]["dice"]["expression"] == "1d100"


def test_describe_exposes_workspace_declarations(plugins):
    items = {row["id"]: row for row in builtin_registry().describe()}
    canvas = items["dnd5e"]["workspace"]
    assert canvas["layout_id"] == "dnd5e-canvas"
    assert canvas["primary"] == "canvas"
    assert canvas["primary_min_width"] == 720
    assert canvas["sidebar"]["width"] == 380
    assert (canvas["sidebar"]["min_width"], canvas["sidebar"]["max_width"]) == (360, 420)
    assert canvas["sidebar"]["collapsed_width"] == 48
    assert canvas["topbar"]["height"] == 48
    assert canvas["left_rail"] == {"width": 48, "visible": True}

    conversation = items["coc7"]["workspace"]
    assert conversation["primary"] == "conversation"
    assert conversation["sidebar"]["width"] == 260
    assert conversation["topbar"]["height"] == 0
    assert conversation["left_rail"]["visible"] is False


def test_dnd5e_pack_initializes_spell_slots_from_class_table(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    card = plugin.schema.pack(
        {
            "id": "wizard-5",
            "name": "Mira",
            "level": 5,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 24, "max": 24},
            "spellcasting": {"ability": "int", "slots": {}},
        }
    )
    slots = card["sheet"]["spellcasting"]["slots"]
    assert slots["1"] == {"max": 4, "used": 0}
    assert slots["3"] == {"max": 2, "used": 0}
    assert "4" not in slots


def test_d20_roll_check_shell_presentation_drives_dice_metadata(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    card = plugin.schema.pack(
        {
            "id": "wizard-5",
            "name": "Mira",
            "level": 5,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 24, "max": 24},
        }
    )
    outcome = plugin.checks.roll_outcome(
        card,
        CheckRequest(
            kind="save",
            name="敏捷豁免",
            target=15,
            extra={"advantage": True},
        ),
        rng=lambda sides: 20 if sides == 20 else 1,
    )
    assert outcome.presentation["mode"] == "d20"
    assert len(outcome.dice) == 2
    assert outcome.detail["kept"] == 20


def _run(plugin, card, name, dc=15, rng=None, **kwargs):
    from trpg_server import check_service as service

    member = {"username": "tester", "character_card": card}
    original = service.get_plugin
    service.get_plugin = lambda ruleset_id=None: plugin
    try:
        return service.run_room_check_outcome(
            member,
            name=name,
            dc=dc,
            ruleset_id="dnd5e",
            rng=rng or (lambda sides: 12),
            **kwargs,
        )
    finally:
        service.get_plugin = original


def test_ability_check_uses_d20_plus_modifier(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    result = _run(plugin, _card(plugin), "力量检定", dc=15)
    assert result["ruleset_id"] == "dnd5e"
    assert result["kind"] == "check"
    assert result["dice"] == [12]
    assert result["total"] == 15
    assert result["target"] == 15
    assert result["margin"] == 0
    assert result["success"] is True
    assert result["detail"]["ability_modifier"] == 3
    assert result["presentation"]["mode"] == "d20"


def test_save_and_skill_apply_proficiency(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    card = _card(plugin)

    save = _run(plugin, card, "体质豁免", dc=15)
    assert save["kind"] == "save"
    assert save["detail"]["proficient"] is True
    assert save["detail"]["proficiency_bonus"] == 2
    assert save["total"] == 15

    skill = _run(plugin, card, "察觉检定", dc=15)
    assert skill["kind"] == "skill"
    assert skill["detail"]["skill"] == "perception"
    assert skill["detail"]["ability_modifier"] == 1
    assert skill["total"] == 15

    unproficient = _run(plugin, card, "历史检定", dc=15)
    assert unproficient["detail"]["proficient"] is False
    assert unproficient["total"] == 12


def test_natural_one_and_twenty_are_plugin_defined(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    card = _card(plugin)

    critical = _run(plugin, card, "力量检定", dc=30, rng=lambda sides: 20)
    assert critical["degree"] == "critical_success"
    assert critical["success"] is True

    fumble = _run(plugin, card, "力量检定", dc=1, rng=lambda sides: 1)
    assert fumble["degree"] == "critical_failure"
    assert fumble["success"] is False


def test_advantage_keeps_the_higher_d20(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    rolls = iter([5, 18])
    result = _run(plugin, _card(plugin), "力量检定", dc=15, rng=lambda sides: next(rolls), advantage=True)
    assert result["dice"] == [5, 18]
    assert result["detail"]["natural"] == 18
    assert result["total"] == 21
    assert result["success"] is True


def test_unknown_check_name_is_rejected(plugins):
    plugin = builtin_registry().resolve("dnd5e")
    with pytest.raises(check_service.CheckServiceError):
        _run(plugin, _card(plugin), "不存在的能力")


def test_dnd5e_real_room_check_and_save_reach_log(plugins, tmp_path):
    """真实房间入口：5e 属性检定与豁免走 d20，并写入房间日志。"""
    from trpg_server import room_rules
    from trpg_server.room_log import read_room_log

    plugin = builtin_registry().resolve("dnd5e")
    card = _card(plugin)
    member = {"user_id": "u-1", "username": "alice", "character_card": card}
    info = {"id": "room-5e", "ruleset_id": "dnd5e", "members": [member]}

    attack_roll = room_rules.roll_check(
        tmp_path,
        info,
        member,
        name="力量检定",
        dc=15,
        rng=lambda sides: 12,
    )
    assert attack_roll["ruleset_id"] == "dnd5e"
    assert attack_roll["dice"] == [12]
    assert attack_roll["target"] == 15
    assert attack_roll["roll"] == attack_roll["total"]
    assert attack_roll["success"] is (attack_roll["total"] >= 15)
    assert "d20" in attack_roll["summary"]

    save_roll = room_rules.roll_check(
        tmp_path,
        info,
        member,
        name="敏捷豁免",
        dc=12,
        rng=lambda sides: 8,
    )
    assert save_roll["ruleset_id"] == "dnd5e"
    assert save_roll["dice"] == [8]
    assert save_roll["target"] == 12

    entries = read_room_log(tmp_path)["entries"]
    messages = [str(entry.get("message") or "") for entry in entries]
    assert any("力量检定" in message and "DC 15" in message for message in messages)
    assert any("敏捷豁免" in message and "DC 12" in message for message in messages)


def test_disabled_ruleset_fails_in_room_house_rules_and_combat(plugins, monkeypatch):
    from trpg_server import house_rules_store, room_rules
    from trpg_server.combat.settings import CombatSettings
    from trpg_server.ruleset import RulesetSelectionError
    from trpg_server.ruleset.registry import RulesetRegistry

    real = builtin_registry()
    coc7 = real.get("coc7")
    assert coc7 is not None
    disabled = RulesetRegistry(plugins=[coc7], default_id="coc7")
    monkeypatch.setattr("trpg_server.ruleset.registry._BUILTIN_REGISTRY", disabled)

    info = {"id": "room-5e", "ruleset_id": "dnd5e"}
    with pytest.raises(RulesetSelectionError) as exc:
        room_rules.resolve_rule_id(info)
    assert "dnd5e" in str(exc.value)
    assert "未启用" in str(exc.value)

    with pytest.raises(RulesetSelectionError):
        house_rules_store.ruleset_definitions("dnd5e")

    with pytest.raises(RulesetSelectionError) as combat_exc:
        CombatSettings.from_house_rules({}, room_ruleset_id="dnd5e")
    assert "dnd5e" in str(combat_exc.value)


def test_unknown_ruleset_message_is_readable(plugins):
    from trpg_server import room_rules
    from trpg_server.ruleset import RulesetSelectionError

    with pytest.raises(RulesetSelectionError) as exc:
        room_rules.resolve_rule_id({"ruleset_id": "dnd5e-x"})
    message = str(exc.value)
    assert "dnd5e-x" in message
    assert "未知的规则集" in message


def test_workflow_tools_are_declared_by_ruleset(plugins):
    from trpg_server.agents.profiles import workflow_tool_names

    assert workflow_tool_names("coc7") == []
    dnd5e_tools = workflow_tool_names("dnd5e")
    assert "workflow.policy" in dnd5e_tools
    assert "reaction.list" in dnd5e_tools


def test_role_prompt_is_assembled_per_ruleset(plugins):
    from trpg_server.ruleset.prompts import assemble_role_prompt

    base = "基础 KP 提示词"
    dnd_prompt = assemble_role_prompt(base, "dnd5e")
    assert dnd_prompt.startswith(base)
    assert "D&D 5e" in dnd_prompt
    assert "d20" in dnd_prompt and "DC" in dnd_prompt
    assert "dnd5e.roll_check" in dnd_prompt
    assert "《克苏鲁的呼唤》第七版规则" not in dnd_prompt

    coc_prompt = assemble_role_prompt(base, "coc7")
    assert coc_prompt.startswith(base)
    assert "克苏鲁的呼唤第七版" in coc_prompt
    assert "check.roll_room_check" in coc_prompt
