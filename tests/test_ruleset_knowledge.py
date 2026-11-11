from trpg_server.agents.ruleset_adapters import COC7Adapter
from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore


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
