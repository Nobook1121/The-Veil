"""规则书插件边界、版本门控与旧卡迁移的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.ruleset import (
    RULESET_API_VERSION,
    RulesetRegistry,
    RulesetSelectionError,
    builtin_registry,
    default_ruleset_id,
)
from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs
from trpg_server.ruleset.migrations import migrate_character_cards


class _FakePlugin:
    def __init__(self, ruleset_id: str, api_version: int = RULESET_API_VERSION) -> None:
        self.id = ruleset_id
        self.display_name = ruleset_id
        self.api_version = api_version
        self.ruleset_version = "1.0"
        self.schema_version = 1
        self.supported_locales = ("zh-CN",)
        self.capabilities = frozenset()
        self.schema = None
        self.catalogs = None
        self.checks = None
        self.combat = None
        self.tools = None


def test_builtin_registry_only_exposes_coc7():
    registry = builtin_registry()
    assert registry.ids() == ["coc7"]
    plugin = registry.resolve(None)
    assert plugin.id == default_ruleset_id()
    assert plugin.api_version == RULESET_API_VERSION
    assert "character_schema" in plugin.capabilities


def test_unknown_ruleset_falls_back_and_room_scenario_resolution():
    registry = builtin_registry()
    assert registry.resolve("does-not-exist").id == "coc7"
    assert registry.resolve_for_scenario({"ruleset_id": "coc7"}).id == "coc7"
    assert registry.resolve_for_room({"scenario": {"ruleset_id": "coc7"}}).id == "coc7"
    assert registry.resolve_for_room({"ruleset_id": "coc7"}).id == "coc7"
    assert registry.resolve_for_room({}).id == "coc7"


def test_incompatible_plugin_is_disabled_with_reason():
    registry = RulesetRegistry([_FakePlugin("legacy", api_version=RULESET_API_VERSION + 1)])
    assert registry.ids() == []
    items = registry.describe()
    assert len(items) == 1
    assert items[0]["enabled"] is False
    assert str(RULESET_API_VERSION + 1) in items[0]["reason"]


def test_registry_runs_without_any_builtin_plugin():
    """核心只依赖注册表：一个最小占位插件即可成为默认规则集。"""
    plugin = _FakePlugin("placeholder")
    registry = RulesetRegistry([plugin])
    assert registry.default_id() == "placeholder"
    assert registry.resolve(None) is plugin
    assert registry.resolve("unknown") is plugin
    assert registry.assert_selectable(None) is plugin


def test_empty_registry_reports_unavailable_instead_of_crashing_import():
    registry = RulesetRegistry([])
    assert registry.default_id() is None
    with pytest.raises(RuntimeError):
        registry.resolve(None)
    with pytest.raises(RulesetSelectionError):
        registry.assert_selectable(None)


def test_explicit_default_plugin_wins():
    first, second = _FakePlugin("first"), _FakePlugin("second")
    registry = RulesetRegistry([first, second], default_id="second")
    assert registry.default_id() == "second"
    assert registry.resolve(None) is second


def test_plugin_modules_are_registry_driven_not_hardcoded(monkeypatch):
    from trpg_server.ruleset import bootstrap

    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    empty = bootstrap.build_registry()
    assert empty.ids() == []

    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "trpg_server.ruleset.coc7")
    loaded = bootstrap.build_registry()
    assert loaded.ids() == ["coc7"]


def test_schema_pack_keeps_legacy_mirror_and_adds_sheet():
    plugin = builtin_registry().resolve("coc7")
    stored = {
        "id": "c1",
        "name": "调查员",
        "attributes": {"str": 50},
        "skillGroups": {"explore": []},
        "unknown_field": 1,
    }
    packed = plugin.schema.pack(stored)
    assert packed["rulesetId"] == "coc7"
    assert packed["rulesetSchemaVersion"] == 1
    assert packed["sheet"]["attributes"] == {"str": 50}
    # 旧字段保留为镜像，读取路径不受影响
    assert packed["attributes"] == {"str": 50}
    assert packed["unknown_field"] == 1
    # 打包不影响原对象
    assert "rulesetId" not in stored


def test_catalogs_prefer_plugin_directory(tmp_path: Path):
    catalogs_dir = tmp_path / "catalogs"
    (catalogs_dir / "weapons" / "builtin").mkdir(parents=True)
    (catalogs_dir / "occupations" / "builtin").mkdir(parents=True)
    (catalogs_dir / "skills.json").write_text(
        json.dumps({"version": 2, "skills": [{"key": "stealth"}]}), encoding="utf-8"
    )
    (catalogs_dir / "occupations" / "builtin" / "detective.json").write_text(
        json.dumps({"id": "detective"}), encoding="utf-8"
    )
    catalogs = Coc7Catalogs(catalogs_dir)
    assert catalogs.skills()["version"] == 2
    assert catalogs.skills()["skills"][0]["key"] == "stealth"
    assert catalogs.occupations() == [{"id": "detective"}]
    assert catalogs.items() == []


def test_migration_is_dry_run_then_idempotent(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir()
    legacy = characters_dir / "investigator-1.json"
    legacy.write_text(
        json.dumps({"id": "investigator-1", "name": "旧卡", "attributes": {"str": 60}}, ensure_ascii=False),
        encoding="utf-8",
    )
    gallery_dir = tmp_path / "gallery"
    gallery_dir.mkdir()
    (gallery_dir / "pub1.json").write_text(
        json.dumps({"id": "gallery-1", "name": "广场卡", "attributes": {"str": 40}}), encoding="utf-8"
    )

    preview = migrate_character_cards(characters_dir, gallery_dir=gallery_dir, dry_run=True)
    assert preview["scanned"] == 2
    assert preview["counts"] == {"would_migrate": 2}
    assert "rulesetId" not in json.loads(legacy.read_text(encoding="utf-8"))

    applied = migrate_character_cards(characters_dir, gallery_dir=gallery_dir, dry_run=False)
    assert applied["counts"] == {"migrated": 2}
    stored = json.loads(legacy.read_text(encoding="utf-8"))
    assert stored["rulesetId"] == "coc7"
    assert stored["sheet"]["attributes"] == {"str": 60}

    repeat = migrate_character_cards(characters_dir, gallery_dir=gallery_dir, dry_run=False)
    assert repeat["counts"] == {"skipped": 2}


def test_check_bridge_matches_core_threshold():
    plugin = builtin_registry().resolve("coc7")
    assert plugin.checks.threshold(50, "regular") == 50
    assert plugin.checks.threshold(50, "hard") == 25
    assert plugin.checks.success_level(5, 50) == "extreme"
    assert plugin.checks.success_level(99, 50) in {"failure", "fumble"}


def test_kp_tools_are_assembled_from_ruleset_plugin():
    from trpg_server.agents.profiles import (
        DEFAULT_KP_TOOLS,
        build_kp_tool_names,
        ruleset_tool_names,
    )

    plugin = builtin_registry().resolve("coc7")
    assert ruleset_tool_names("coc7") == list(plugin.tools.tool_names())
    assert build_kp_tool_names("coc7") == DEFAULT_KP_TOOLS
    assert "check.roll_room_check" in DEFAULT_KP_TOOLS
    # 未知规则集回退默认规则集，工具清单保持一致。
    assert build_kp_tool_names("does-not-exist") == DEFAULT_KP_TOOLS


def test_descriptor_transcribes_coc7_schema():
    from trpg_server.ruleset.descriptors import SCHEMA_DESCRIPTOR_VERSION, descriptor_of

    plugin = builtin_registry().resolve("coc7")
    descriptor = descriptor_of(plugin)
    assert descriptor["descriptor_version"] == SCHEMA_DESCRIPTOR_VERSION
    assert descriptor["ruleset_id"] == "coc7"
    assert descriptor["schema_version"] == plugin.schema_version
    assert "attributes" in descriptor["sheet_fields"]
    assert "str" in descriptor["attribute_keys"]
    assert "sanity" in descriptor["resource_keys"]
    assert "maxSan" in descriptor["derived_card_keys"]
    assert {slot["kind"] for slot in descriptor["resource_slots"]} == {"hp", "san", "mp"}


def test_descriptor_without_schema_is_empty_not_error():
    from trpg_server.ruleset.descriptors import descriptor_of

    descriptor = descriptor_of(_FakePlugin("bare"))
    assert descriptor["ruleset_id"] == "bare"
    assert descriptor["envelope_fields"] == []
    assert descriptor["sheet_fields"] == []
    assert descriptor["resource_slots"] == []


def test_rulesets_endpoint_exposes_schema_descriptor():
    registry = builtin_registry()
    items = {item["id"]: item for item in registry.describe()}
    assert items["coc7"]["schema"]["ruleset_id"] == "coc7"
    assert items["coc7"]["schema"]["attribute_keys"]


def test_combat_registry_forwards_to_ruleset_plugin():
    from trpg_server.combat import registry as combat_registry

    plugin = builtin_registry().resolve("coc7")
    assert combat_registry.get_ruleset("coc7") is plugin.combat
    # dnd5e 仍是开发中占位：注册表不注册插件，但战斗兼容实现保留，避免既有测试/流程回退。
    assert builtin_registry().get("dnd5e") is None
    assert combat_registry.get_ruleset("dnd5e").ruleset_id == "dnd5e"
    assert {item["id"] for item in combat_registry.list_rulesets()} >= {"coc7", "dnd5e"}


def test_check_service_dispatches_to_ruleset_plugin(monkeypatch):
    from trpg_server import check_service
    from trpg_server.ruleset import CheckResolution

    plugin = builtin_registry().resolve("coc7")
    seen: dict[str, object] = {}

    def fake_roll(card, request, rng):
        seen["name"] = request.name
        seen["difficulty"] = request.difficulty
        return CheckResolution(
            roll=12,
            target=50,
            level="regular",
            success=True,
            detail={"base_target": 50, "candidates": [], "display_name": "力量"},
        )

    monkeypatch.setattr(plugin.checks, "roll", fake_roll)
    member = {"username": "tester", "character_card": {"name": "调查员", "attributes": {"STR": 50}}}
    result = check_service.run_room_check(
        member,
        name="力量",
        difficulty="hard",
        ruleset_id="coc7",
        rng=lambda sides: 1,
    )
    assert seen == {"name": "力量", "difficulty": "hard"}
    assert result["roll"] == 12
    assert result["success"] is True
    assert result["player_name"] == "tester"


def test_sanity_capability_missing_reports_unsupported(monkeypatch):
    import trpg_server.ruleset.registry as registry_module
    from trpg_server import room_rules
    from trpg_server.agents.tools import dice

    registry = RulesetRegistry(plugins=[_FakePlugin("bare")])
    monkeypatch.setattr(registry_module, "_BUILTIN_REGISTRY", registry)

    member = {
        "user_id": "u1",
        "username": "tester",
        "character_card": {"name": "调查员", "rulesetId": "bare", "attributes": {"POW": 50}},
        "character_state": {"current_san": 40, "max_san": 50},
    }
    with pytest.raises(room_rules.RuleOperationError) as excinfo:
        room_rules.apply_san_change(None, {}, member, delta=-1)
    assert excinfo.value.code == "sanity_unsupported"

    class _Context:
        room_dir = None

        def room_info(self):
            return {"members": [member]}

    result = dice.roll_sanity_check({"player_name": "tester", "san_change": "1/1d6"}, _Context())
    assert "不支持" in result.get("error", "")


class _AdminActor:
    user_id = 7
    role = "ADMIN"

    def has(self, *_args, **_kwargs) -> bool:
        return True


def test_apply_save_stamps_ruleset_envelope(tmp_path: Path):
    from trpg_server import character_service
    from trpg_server.app_factory import create_app

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
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
    with app.app_context():
        saved = character_service.apply_save(
            _AdminActor(),
            "investigator-1001",
            {"name": "测试调查员", "attributes": {"str": 50}},
        )

    assert saved["sheet"]["attributes"]["str"] == 50
    path = tmp_path / "characters" / "investigator-1001" / "character.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["rulesetId"] == "coc7"
    assert stored["rulesetSchemaVersion"] == 1
    assert stored["sheet"]["attributes"]["str"] == 50
    # 旧字段镜像保留，历史读取路径不受影响
    assert stored["attributes"]["str"] == 50
