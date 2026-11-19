"""E1：SRD 内容数据包的加载、校验与最小集合覆盖。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.content_packs import ContentPackError, ContentPackLibrary
from trpg_server.plugins.manifest import ManifestError, load_manifest, parse_manifest
from trpg_server.plugins.registry import PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SRD_PACK_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e-srd"

PLUGIN_MANIFEST = {
    "schema_version": 1,
    "id": "demo-pack",
    "name": "Demo Content Pack",
    "version": "1.0.0",
    "api_version": 1,
    "capabilities": {"content_packs": {"dirs": ["packs"], "contract_version": 1}},
}

PACK_MANIFEST = {
    "pack_id": "demo-pack-1",
    "pack_version": "1.0.0",
    "contract_version": 1,
    "ruleset_id": "demo",
    "content_version": "1.0.0",
    "source": {
        "system": "Demo System",
        "document": "Demo SRD",
        "license": "CC BY 4.0",
        "attribution": "Demo attribution text.",
    },
    "files": [
        {"path": "conditions.json", "type": "condition"},
        {"path": "extras.json", "type": "condition"},
    ],
}


def _write_pack(root: Path, *, manifest: dict | None = None, files: dict[str, object] | None = None) -> Path:
    plugin_dir = root / "demo-pack"
    pack_dir = plugin_dir / "packs"
    pack_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(PLUGIN_MANIFEST), encoding="utf-8")
    (pack_dir / "pack.json").write_text(json.dumps(manifest or PACK_MANIFEST), encoding="utf-8")
    payload = files or {
        "conditions.json": [{"id": "prone", "name": "Prone", "effects": {"attack_rolls_disadvantage": True}}],
        "extras.json": [{"id": "grappled", "name": "Grappled"}],
    }
    for name, data in payload.items():
        (pack_dir / name).write_text(json.dumps(data), encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------- 真实 SRD 包


def test_real_srd_pack_loads_with_versions_and_license():
    manifest = load_manifest(SRD_PACK_DIR, expected_id="dnd5e-srd")
    assert manifest.has_code is False
    assert manifest.capability("content_packs") == {"dirs": ["packs"], "contract_version": 1}

    library = ContentPackLibrary.from_manifest(manifest)
    assert library.pack_ids == ("dnd5e-srd-5-1",)
    pack = library.packs[0]
    assert pack.ruleset_id == "dnd5e"
    assert pack.content_version == "1.0.0"
    assert pack.source.license == "CC BY 4.0"
    assert "SRD 5.1" in pack.source.document
    attribution = library.attribution("dnd5e")[0]
    assert "System Reference Document 5.1" in attribution
    assert "Creative Commons Attribution 4.0" in attribution
    # 许可原文不翻译。
    assert "CC BY 4.0" in pack.source.license


def test_real_srd_pack_minimal_coverage_counts():
    library = ContentPackLibrary.from_manifest(load_manifest(SRD_PACK_DIR, expected_id="dnd5e-srd"))
    expected = {
        "ability": 6,
        "skill": 18,
        "progression": 20,
        "equipment": 37,
        "condition": 15,
        "spell": 11,
        "monster": 7,
        "class": 2,
    }
    assert library.types("dnd5e") == tuple(expected)
    for type_name, count in expected.items():
        assert len(library.entries("dnd5e", type_name)) == count, type_name
    assert sum(expected.values()) == 116


def test_real_srd_pack_shapes_are_ready_for_engine_consumption():
    library = ContentPackLibrary.from_manifest(load_manifest(SRD_PACK_DIR, expected_id="dnd5e-srd"))

    fireball = library.get("dnd5e", "spell", "fireball")
    assert fireball is not None
    assert fireball["level"] == 3
    assert fireball["save"] == {"ability": "dex", "half_on_success": True}
    assert fireball["damage"] == [{"dice": "8d6", "type": "fire"}]
    assert fireball["scaling"]["per_level"] == "1d6"
    assert fireball["area"] == {"shape": "sphere", "radius_ft": 20}

    magic_missile = library.get("dnd5e", "spell", "magic-missile")
    assert magic_missile["attack"] is None
    assert magic_missile["damage"][0]["dice"] == "1d4+1"

    hold_person = library.get("dnd5e", "spell", "hold-person")
    assert hold_person["concentration"] is True
    assert hold_person["applies_condition"] == "paralyzed"

    goblin = library.get("dnd5e", "monster", "goblin")
    assert goblin["ac"][0]["value"] == 15
    assert goblin["hp"] == {"average": 7, "formula": "2d6"}
    assert goblin["abilities"]["dex"] == 14
    assert goblin["actions"][0]["attack_bonus"] == 4
    assert goblin["actions"][0]["damage"][0] == {"dice": "1d6+2", "type": "slashing"}

    bugbear = library.get("dnd5e", "monster", "bugbear")
    assert bugbear is not None
    assert bugbear["ac"] == [{"value": 16, "note": "hide armor, shield"}]
    assert bugbear["hp"] == {"average": 27, "formula": "5d8+5"}
    assert bugbear["cr"] == "1" and bugbear["xp"] == 200
    assert bugbear["actions"][0]["name"] == "Morningstar"
    assert bugbear["actions"][0]["damage"][0] == {"dice": "2d8+2", "type": "piercing"}

    prone = library.get("dnd5e", "condition", "prone")
    assert prone["effects"]["stand_cost_half_movement"] is True
    exhaustion = library.get("dnd5e", "condition", "exhaustion")
    assert [item["level"] for item in exhaustion["effects_by_level"]] == [1, 2, 3, 4, 5, 6]

    chain_mail = library.get("dnd5e", "equipment", "chain-mail")
    assert chain_mail["base_ac"] == 16
    assert chain_mail["strength_min"] == 13
    assert chain_mail["stealth_disadvantage"] is True

    wizard = library.get("dnd5e", "class", "wizard")
    slots = wizard["spellcasting"]["slot_table"]
    assert len(slots) == 20
    assert all(len(row) == 9 for row in slots)
    assert slots[0] == [2, 0, 0, 0, 0, 0, 0, 0, 0]
    assert slots[4] == [4, 3, 2, 0, 0, 0, 0, 0, 0]
    assert slots[19] == [4, 3, 3, 3, 3, 2, 2, 1, 1]

    progression = library.entries("dnd5e", "progression")
    assert progression[0]["xp"] == 0
    assert progression[-1] == {
        "id": "level-20",
        "name": "Level 20",
        "level": 20,
        "xp": 355000,
        "proficiency_bonus": 6,
        "source": "SRD 5.1",
    }


def test_real_srd_pack_search_and_locales():
    library = ContentPackLibrary.from_manifest(load_manifest(SRD_PACK_DIR, expected_id="dnd5e-srd"))
    assert [item["id"] for item in library.search("dnd5e", "spell", "fire")] == ["fireball"]
    assert library.search("dnd5e", "condition", "倒地")[0]["id"] == "prone"
    assert library.search("dnd5e", "spell", "not-a-spell") == []
    assert library.get("dnd5e", "spell", "missing") is None


# ---------------------------------------------------------------- 通用校验


def test_valid_pack_loads_lazily_and_searches(tmp_path):
    plugin_dir = _write_pack(tmp_path / "plugins")
    manifest = load_manifest(plugin_dir, expected_id="demo-pack")
    library = ContentPackLibrary.from_manifest(manifest)

    assert library.types("demo") == ("condition",)
    assert library.get("demo", "condition", "prone")["effects"]["attack_rolls_disadvantage"] is True
    assert [item["id"] for item in library.entries("demo", "condition")] == ["prone", "grappled"]
    assert library.search("demo", "condition", "grap")[0]["id"] == "grappled"
    assert library.sources("demo")[0]["source"]["license"] == "CC BY 4.0"


def test_pack_without_license_or_attribution_is_rejected(tmp_path):
    manifest = dict(PACK_MANIFEST)
    manifest["source"] = {"system": "Demo", "document": "Demo SRD", "license": "", "attribution": "x"}
    plugin_dir = _write_pack(tmp_path / "plugins", manifest=manifest)
    with pytest.raises(ContentPackError):
        ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))

    manifest["source"] = {"system": "Demo", "document": "Demo SRD", "license": "CC BY 4.0", "attribution": ""}
    plugin_dir = _write_pack(tmp_path / "plugins2", manifest=manifest)
    with pytest.raises(ContentPackError):
        ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))


def test_duplicate_ids_and_path_escape_are_rejected(tmp_path):
    duplicate = {
        "conditions.json": [{"id": "prone", "name": "Prone"}],
        "extras.json": [{"id": "prone", "name": "Prone Again"}],
    }
    plugin_dir = _write_pack(tmp_path / "plugins", files=duplicate)
    library = ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))
    with pytest.raises(ContentPackError):
        library.entries("demo", "condition")

    manifest = dict(PACK_MANIFEST)
    manifest["files"] = [{"path": "../outside.json", "type": "condition"}]
    plugin_dir = _write_pack(tmp_path / "plugins2", manifest=manifest)
    with pytest.raises(Exception):
        ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))


def test_deep_json_and_oversized_text_are_rejected(tmp_path):
    deep: object = "leaf"
    for _ in range(20):
        deep = {"nested": deep}
    plugin_dir = _write_pack(
        tmp_path / "plugins",
        files={"conditions.json": [{"id": "prone", "name": "Prone", "deep": deep}], "extras.json": []},
    )
    library = ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))
    with pytest.raises(ContentPackError):
        library.entries("demo", "condition")

    long_text = "x" * 40_001
    plugin_dir = _write_pack(
        tmp_path / "plugins2",
        files={"conditions.json": [{"id": "prone", "name": "Prone", "text": long_text}], "extras.json": []},
    )
    library = ContentPackLibrary.from_manifest(load_manifest(plugin_dir, expected_id="demo-pack"))
    with pytest.raises(ContentPackError):
        library.entries("demo", "condition")


def test_manifest_rejects_unsupported_pack_contract_version(tmp_path):
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                **PLUGIN_MANIFEST,
                "capabilities": {"content_packs": {"dirs": ["packs"], "contract_version": 2}},
            },
            tmp_path,
        )


def test_registry_activates_content_packs_and_exposes_them_by_ruleset(tmp_path):
    root = tmp_path / "plugins"
    _write_pack(root)
    (root / "enabled.json").write_text(
        json.dumps({"version": 1, "enabled": ["demo-pack"], "updated_at": ""}), encoding="utf-8"
    )
    registry = PluginRegistry(root).load()
    try:
        record = registry.get("demo-pack")
        assert record is not None and record.loaded
        assert "content_packs" in record.loaded_capabilities
        library = runtime.content_packs_for("demo")
        assert library.pack_ids == ("demo-pack-1",)
        assert library.get("demo", "condition", "prone")["name"] == "Prone"
        assert runtime.content_packs_for("other").packs == ()
    finally:
        runtime.clear_plugin_runtime()
