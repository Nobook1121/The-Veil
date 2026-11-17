from trpg_server.agents.ruleset_adapters import COC7Adapter, Dnd5eAdapter
from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore, search_ruleset


def test_coc7_chunk_keeps_formula_table_exception_and_example_together():
    chunks = COC7Adapter().chunk("""# 检定
目标值 = 技能值 - 难度等级
| 难度 | 修正 |
| 困难 | -20 |
例外：对抗检定使用双方结果。
示例：玩家掷骰并比较结果。
""", {"source_id": "core", "knowledge_version": "1"})
    assert len(chunks) == 1
    assert "目标值" in chunks[0].text and "例外" in chunks[0].text and "示例" in chunks[0].text
    assert chunks[0].topic == "check"


def test_ruleset_store_versions_and_room_binding_are_isolated(tmp_path):
    store = RulesetKnowledgeStore(tmp_path / "kb", rooms_dir=tmp_path / "rooms")
    store.upload_source("coc7", "rules.md", "# Sanity\n理智损失 1d6".encode("utf-8"), "zh-CN")
    version = store.reindex("coc7")["knowledge_version"]
    assert store.search("coc7", "sanity")[0]["ruleset_id"] == "coc7"
    store.bind_room("room-1", "coc7", version)
    assert (tmp_path / "rooms" / "room-1" / "info.json").exists()
    assert store.search("dnd", "damage") == []


# ---------------------------------------------------------------------------
# G3：5e 规则检索 adapter
# ---------------------------------------------------------------------------
def _load_plugins():
    from pathlib import Path

    from trpg_server.plugins import runtime
    from trpg_server.plugins.registry import PluginRegistry
    from trpg_server.ruleset import reset_builtin_registry

    root = Path(__file__).resolve().parent.parent / "data" / "plugins"
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(root).load()


def _unload_plugins():
    from trpg_server.plugins import runtime
    from trpg_server.ruleset import reset_builtin_registry

    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def test_dnd5e_adapter_returns_unified_results_with_source_and_license():
    _load_plugins()
    try:
        adapter = Dnd5eAdapter()
        results = adapter.builtin_search("fireball", limit=3)
        assert results, "SRD 内容包里应能检索到 fireball"
        first = results[0]
        for key in (
            "ruleset_id",
            "knowledge_version",
            "chunk_id",
            "topic",
            "title",
            "citation",
            "text",
            "source",
            "license",
            "license_url",
        ):
            assert key in first
        assert first["ruleset_id"] == "dnd5e"
        assert "8d6" in first["text"]
        assert first["source"]["attribution"] and first["license"]
    finally:
        _unload_plugins()


def test_search_ruleset_merges_dnd5e_and_keeps_coc7_output_unchanged(tmp_path):
    _load_plugins()
    try:
        store = RulesetKnowledgeStore(tmp_path / "kb", rooms_dir=tmp_path / "rooms")
        store.upload_source("coc7", "rules.md", "# Sanity\n理智损失 1d6".encode("utf-8"), "zh-CN")
        store.reindex("coc7")
        coc_direct = store.search("coc7", "sanity", top_k=3)
        coc_merged = search_ruleset("room-1", "sanity", store=store, ruleset_id="coc7", top_k=3)
        assert coc_merged == coc_direct

        dnd = search_ruleset("room-2", "fireball", store=store, ruleset_id="dnd5e", top_k=3)
        assert dnd and dnd[0]["ruleset_id"] == "dnd5e"
        assert dnd[0]["license"] and dnd[0]["source"]["document"]
    finally:
        _unload_plugins()


def test_knowledge_tool_forwards_source_and_license():
    from trpg_server.agents.tools.knowledge import search_ruleset_knowledge

    class _Context:
        tool_state = {
            "ruleset_search": lambda query, top_k: [
                {
                    "ruleset_id": "dnd5e",
                    "knowledge_version": "srd-5.1",
                    "chunk_id": "srd:spell:fireball",
                    "topic": "spell",
                    "title": "Fireball",
                    "citation": "SRD 5.1 / Fireball",
                    "text": "8d6 fire damage",
                    "source": {"document": "SRD 5.1", "attribution": "Wizards of the Coast"},
                    "license": "CC-BY-4.0",
                    "license_url": "https://example.test/license",
                }
            ]
        }

    result = search_ruleset_knowledge({"query": "fireball"}, _Context())
    reference = result["references"][0]
    assert reference["license"] == "CC-BY-4.0"
    assert reference["source"]["document"] == "SRD 5.1"
    assert reference["title"] == "Fireball"
