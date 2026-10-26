import json

from trpg_server.agents.embedding_provider import HashedTokenEmbedding
from trpg_server.agents.knowledge_base import (
    KnowledgeBaseService,
    build_knowledge_chunks,
    list_knowledge_sections,
    load_knowledge_index,
    persist_knowledge_index,
    update_knowledge_section,
)
from trpg_server.agents.vector_store import EmbeddedVectorStore
from trpg_server.scenario_import_pipeline import derive_keywords, derive_tier, is_constant_section
from trpg_server.scenario_store import save_scenario_record


def test_build_chunks_contains_versioned_metadata_and_visibility():
    chunks = build_knowledge_chunks(
        {
            "id": "case-1",
            "scenario_version": "2",
            "modules": [
                {
                    "id": "scene-1",
                    "module_type": "scene",
                    "title": "Library",
                    "summary": "A locked library",
                    "content": "The brass key opens the library.",
                    "visibility": "public",
                    "spoiler_level": 1,
                }
            ],
        }
    )
    assert chunks[0].scenario_id == "case-1"
    assert chunks[0].scenario_version == "2"
    assert chunks[0].scene_id == "scene-1"
    assert chunks[0].card_type == "scene"
    assert chunks[0].text == "The brass key opens the library."


def test_search_filters_scenario_version_scene_and_spoiler_before_scoring(tmp_path):
    scenarios = tmp_path / "scenarios"
    rooms = tmp_path / "rooms"
    scenarios.mkdir()
    room = rooms / "room-1"
    room.mkdir(parents=True)
    (room / "info.json").write_text(
        '{"id":"room-1","scenario_id":"case-1","scenario_version":"2","active_scene_id":"scene-1","spoiler_level":1}',
        encoding="utf-8",
    )
    service = KnowledgeBaseService(
        rooms_dir=rooms,
        scenarios={
            "case-1": {
                "id": "case-1",
                "scenario_version": "2",
                "modules": [
                    {"id": "scene-1", "module_type": "scene", "content": "brass key library", "spoiler_level": 1},
                    {"id": "scene-2", "module_type": "scene", "content": "brass key garden", "spoiler_level": 0},
                    {"id": "scene-1-old", "scene_id": "scene-1", "module_type": "scene", "content": "brass key old", "scenario_version": "1", "spoiler_level": 0},
                    {"id": "scene-1-secret", "module_type": "scene", "content": "brass key secret", "spoiler_level": 2},
                ],
            }
        },
    )

    result = service.search("room-1", "brass key", top_k=10)

    assert [item["text"] for item in result] == ["brass key library"]


def test_search_fuses_vector_results_after_hard_filters(tmp_path):
    scenarios = tmp_path / "scenarios"
    rooms = tmp_path / "rooms"
    scenarios.mkdir()
    room = rooms / "room-1"
    room.mkdir(parents=True)
    (room / "info.json").write_text(
        '{"id":"room-1","scenario_id":"case-1","scenario_version":"1.0.0","active_scene_id":"scene-1","spoiler_level":1}',
        encoding="utf-8",
    )

    class FakeEmbedding:
        configured = True
        dimensions = 2

        def embed(self, texts):
            assert texts == ["brass key"]
            return [[1.0, 0.0]]

    class FakeVectorStore:
        def search(self, collection, vector, limit=5):
            assert collection == "scenario_case-1_1.0.0"
            assert vector == [1.0, 0.0]
            return [
                {"id": "secret", "score": 0.99, "payload": {"module_id": "scene-secret"}},
                {"id": "scene-1", "score": 0.8, "payload": {"module_id": "scene-1"}},
            ]

    service = KnowledgeBaseService(
        rooms_dir=rooms,
        scenarios={
            "case-1": {
                "id": "case-1",
                "scenario_version": "1.0.0",
                "modules": [
                    {"id": "scene-1", "module_type": "scene", "content": "brass key library", "spoiler_level": 0},
                    {"id": "scene-secret", "module_type": "scene", "content": "brass key secret", "spoiler_level": 2},
                ],
            }
        },
        vector_store=FakeVectorStore(),
        embedding_provider=FakeEmbedding(),
    )

    result = service.search("room-1", "brass key", top_k=5)

    assert [item["chunk_id"] for item in result] == ["scene-1"]
    assert result[0]["score_components"]["vector"] == 0.8


def test_search_fallback_advances_with_room_cursor_instead_of_repeating(tmp_path):
    scenarios = tmp_path / "scenarios"
    rooms = tmp_path / "rooms"
    scenarios.mkdir()
    room = rooms / "room-1"
    room.mkdir(parents=True)
    (room / "info.json").write_text(
        '{"id":"room-1","scenario_id":"case-1","scenario_version":"1","spoiler_level":0}',
        encoding="utf-8",
    )
    service = KnowledgeBaseService(
        rooms_dir=rooms,
        scenarios={
            "case-1": {
                "id": "case-1",
                "scenario_version": "1",
                "modules": [
                    {"id": f"chunk-{index}", "module_type": "lore", "content": f"alpha {index}"}
                    for index in range(1, 7)
                ],
            }
        },
    )

    # 无任何词法/向量命中的泛化输入会走兜底路径；此时应优先返回尚未消费过的块，
    # 使连续检索随剧情推进而变化，而不是每次都重复开头几段。
    first = service.search("room-1", "zzzzzz", top_k=3)
    second = service.search("room-1", "zzzzzz", top_k=3)

    assert [item["chunk_id"] for item in first] == ["chunk-1", "chunk-2", "chunk-3"]
    assert [item["chunk_id"] for item in second] == ["chunk-4", "chunk-5", "chunk-6"]
    assert (room / "knowledge_cursor.json").exists()


def test_saving_scenario_persists_versioned_knowledge_index(tmp_path):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    descriptor = save_scenario_record(scenarios, {"id": 9, "scenario_version": "3", "modules": [{"id": "scene", "module_type": "scene", "content": "lighthouse"}]})
    chunks = load_knowledge_index(descriptor, "3")
    assert [chunk.text for chunk in chunks] == ["lighthouse"]


def test_persisted_json_index_is_idempotently_written_to_embedded_store(tmp_path):
    descriptor = tmp_path / "scenarios" / "scenario-9" / "scenario.json"
    store = EmbeddedVectorStore(tmp_path / "vectors")
    provider = HashedTokenEmbedding(32)
    version_one = {
        "id": "9",
        "scenario_version": "1",
        "modules": [{"id": "scene", "module_type": "scene", "content": "old lighthouse"}],
    }
    version_two = {
        "id": "9",
        "scenario_version": "2",
        "modules": [{"id": "scene", "module_type": "scene", "content": "new lighthouse"}],
    }

    persist_knowledge_index(descriptor, version_one, vector_store=store, embedding_provider=provider)
    persist_knowledge_index(descriptor, version_one, vector_store=store, embedding_provider=provider)
    persist_knowledge_index(descriptor, version_two, vector_store=store, embedding_provider=provider)

    assert store.count({"scenario_id": "9", "scenario_version": "1"}) == 1
    assert store.count({"scenario_id": "9", "scenario_version": "2"}) == 1
    assert store.count({"scenario_id": "9"}) == 2


def _sectioned_service(tmp_path, modules):
    rooms = tmp_path / "rooms"
    room = rooms / "room-1"
    room.mkdir(parents=True)
    (room / "info.json").write_text(
        '{"id":"room-1","scenario_id":"case-1","scenario_version":"1","spoiler_level":0}',
        encoding="utf-8",
    )
    service = KnowledgeBaseService(
        rooms_dir=rooms,
        scenarios={"case-1": {"id": "case-1", "scenario_version": "1", "modules": modules}},
    )
    return service, room


def test_search_backfills_section_context_from_sibling_chunks(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "第一部分：铜钥匙", "metadata": {"section": "图书馆", "title": "图书馆"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "第二部分：书架暗门", "metadata": {"section": "图书馆", "title": "图书馆"}},
            {"id": "chunk-0003", "module_type": "lore", "content": "花园里的玫瑰", "metadata": {"section": "花园", "title": "花园"}},
        ],
    )

    result = service.search("room-1", "书架暗门", top_k=5)

    # 命中子块所属的整章只返回一条结果，且把同章节的相邻子块合并回填。
    assert len(result) == 1
    assert result[0]["chunk_ids"] == ["chunk-0001", "chunk-0002"]
    assert "铜钥匙" in result[0]["text"]
    assert "书架暗门" in result[0]["text"]


def test_search_bm25_prefers_rare_term_over_common_term(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "魔法 魔法 魔法 魔法", "metadata": {"section": "甲", "title": "甲"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "魔法 禁忌咒语", "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    result = service.search("room-1", "禁忌咒语 魔法", top_k=5)

    # 稀有词（禁忌咒语）的 IDF 更高，含稀有词的章节应排在只堆砌高频词的前面。
    assert result[0]["chunk_ids"] == ["chunk-0002"]


def test_search_records_round_history_for_sticky_cooldown(tmp_path):
    service, room = _sectioned_service(
        tmp_path,
        [
            {"id": f"chunk-{index}", "module_type": "lore", "content": f"alpha {index}"}
            for index in range(1, 5)
        ],
    )

    service.search("room-1", "zzzzzz", top_k=2)

    data = json.loads((room / "knowledge_cursor.json").read_text(encoding="utf-8"))
    assert data["scenario"] == "case-1@1"
    assert len(data["rounds"]) == 1
    assert len(data["rounds"][0]) == 2


def test_keyword_channel_activates_without_lexical_overlap(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "花园里长满玫瑰。", "metadata": {"section": "花园", "title": "花园"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "张三推开了暗门。", "keywords": ["铜钥匙"], "metadata": {"section": "图书馆", "title": "图书馆"}},
        ],
    )

    # 查询与正文没有任何词法/向量重叠，只有触发词命中：应直接激活该条，
    # 且不再退回兜底（兜底会按文档顺序返回 chunk-0001）。
    result = service.search("room-1", "我掏出铜钥匙", top_k=5)

    assert [item["chunk_id"] for item in result] == ["chunk-0002"]


def test_secondary_keywords_gate_activation(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {
                "id": "chunk-0001",
                "module_type": "lore",
                "content": "AAA",
                "keywords": ["铜钥匙"],
                "secondary_keywords": ["夜晚"],
                "secondary_logic": "and_all",
                "metadata": {"section": "甲", "title": "甲"},
            },
            {"id": "chunk-0002", "module_type": "lore", "content": "BBBB", "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    # 副键门槛（and_all 需同时出现“夜晚”）未满足 → 关键词通道不激活，退回兜底。
    blocked = service.search("room-1", "铜钥匙", top_k=5)
    assert [item["chunk_id"] for item in blocked] == ["chunk-0001", "chunk-0002"]

    # 主键 + 副键同时命中 → 关键词通道直接激活，只返回该条。
    activated = service.search("room-1", "铜钥匙 夜晚", top_k=5)
    assert [item["chunk_id"] for item in activated] == ["chunk-0001"]


def test_sticky_then_cooldown_hard_gate(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {
                "id": "chunk-0001",
                "module_type": "lore",
                "content": "铜钥匙与暗门",
                "keywords": ["铜钥匙"],
                "sticky_rounds": 1,
                "cooldown_rounds": 1,
                "metadata": {"section": "甲", "title": "甲"},
            },
            {"id": "chunk-0002", "module_type": "lore", "content": "花园里的玫瑰", "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    first = service.search("room-1", "铜钥匙", top_k=5)
    second = service.search("room-1", "铜钥匙", top_k=5)  # 粘滞期内强制注入
    third = service.search("room-1", "铜钥匙", top_k=5)  # 进入冷却，被硬门控挡下

    assert [item["chunk_id"] for item in first] == ["chunk-0001"]
    assert [item["chunk_id"] for item in second] == ["chunk-0001"]
    # 冷却中的章节被剔除，但空结果会兜底返回冷却之外的章节（导入剧本不能因检索为空而「忘记」剧情）。
    assert [item["chunk_id"] for item in third] == ["chunk-0002"]


def test_constant_entry_is_always_injected(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "alpha beta", "metadata": {"section": "甲", "title": "甲"}},
            {
                "id": "chunk-0002",
                "module_type": "lore",
                "content": "本世界由三块大陆构成。",
                "is_constant": True,
                "metadata": {"section": "世界观", "title": "世界观"},
            },
        ],
    )

    result = service.search("room-1", "zzzzzz", top_k=2)

    assert "chunk-0002" in [item["chunk_id"] for item in result]


def test_probability_zero_drops_entry(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {
                "id": "chunk-0001",
                "module_type": "lore",
                "content": "铜钥匙",
                "keywords": ["铜钥匙"],
                "probability": 0,
                "metadata": {"section": "甲", "title": "甲"},
            }
        ],
    )

    assert service.search("room-1", "铜钥匙", top_k=5) == []


def test_group_competition_keeps_highest_weight(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "红药水", "keywords": ["药水"], "group": "potion", "group_weight": 10, "metadata": {"section": "甲", "title": "甲"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "蓝药水", "keywords": ["药水"], "group": "potion", "group_weight": 5, "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    result = service.search("room-1", "药水", top_k=5)

    assert [item["chunk_id"] for item in result] == ["chunk-0001"]


def test_recursive_scanning_activates_referenced_entry(tmp_path):
    modules = [
        {
            "id": "chunk-0001",
            "module_type": "lore",
            "content": "铜钥匙由老陈保管。",
            "keywords": ["铜钥匙"],
            "trigger_chunks": True,
            "metadata": {"section": "甲", "title": "甲"},
        },
        {"id": "chunk-0002", "module_type": "lore", "content": "老陈是图书馆的管家。", "keywords": ["老陈"], "metadata": {"section": "乙", "title": "乙"}},
    ]
    service, _room = _sectioned_service(tmp_path / "on", modules)
    with_recursion = service.search("room-1", "铜钥匙", top_k=5)
    assert [item["chunk_id"] for item in with_recursion] == ["chunk-0001", "chunk-0002"]

    service_off, _room_off = _sectioned_service(tmp_path / "off", modules)
    without = service_off.search("room-1", "铜钥匙", top_k=5, recursive_scanning=False)
    assert [item["chunk_id"] for item in without] == ["chunk-0001"]


def test_token_budget_drops_low_priority_but_never_truncates(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "铜" * 150, "keywords": ["钥匙"], "priority": 200, "metadata": {"section": "甲", "title": "甲"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "银" * 150, "keywords": ["钥匙"], "priority": 10, "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    unbounded = service.search("room-1", "钥匙", top_k=5)
    assert len(unbounded) == 2
    assert all(len(item["text"]) >= 150 for item in unbounded)  # 正文从未被截断

    budgeted = service.search("room-1", "钥匙", top_k=5, token_budget=200)
    assert [item["chunk_id"] for item in budgeted] == ["chunk-0001"]
    assert len(budgeted[0]["text"]) >= 150


def test_archived_tier_is_excluded(tmp_path):
    service, _room = _sectioned_service(
        tmp_path,
        [
            {"id": "chunk-0001", "module_type": "lore", "content": "铜钥匙", "keywords": ["铜钥匙"], "tier": "archived", "metadata": {"section": "甲", "title": "甲"}},
            {"id": "chunk-0002", "module_type": "lore", "content": "玫瑰", "metadata": {"section": "乙", "title": "乙"}},
        ],
    )

    result = service.search("room-1", "铜钥匙", top_k=5)

    assert [item["chunk_id"] for item in result] == ["chunk-0002"]


def test_lorebook_fields_survive_knowledge_index_roundtrip(tmp_path):
    descriptor = save_scenario_record(
        tmp_path / "scenarios",
        {
            "id": 42,
            "scenario_version": "1",
            "modules": [
                {
                    "id": "scene",
                    "module_type": "scene",
                    "content": "灯塔",
                    "keywords": ["灯塔"],
                    "secondary_keywords": ["夜晚"],
                    "secondary_logic": "not_any",
                    "is_constant": True,
                    "tier": "core",
                    "probability": 40,
                    "group": "potion",
                    "group_weight": 7,
                    "priority": 3,
                    "trigger_chunks": True,
                    "sticky_rounds": 2,
                    "cooldown_rounds": 4,
                }
            ],
        },
    )
    chunk = load_knowledge_index(descriptor, "1")[0]

    assert chunk.keywords == ["灯塔"]
    assert chunk.secondary_keywords == ["夜晚"]
    assert chunk.secondary_logic == "not_any"
    assert chunk.is_constant is True
    assert chunk.tier == "core"
    assert chunk.probability == 40
    assert chunk.group == "potion"
    assert chunk.group_weight == 7
    assert chunk.priority == 3
    assert chunk.trigger_chunks is True
    assert chunk.sticky_rounds == 2
    assert chunk.cooldown_rounds == 4


def test_derive_keywords_prefers_quoted_repeated_and_latin_terms():
    text = "「铜钥匙」可以打开暗门。老陈说铜钥匙很关键。老陈把地图交给了你。Melville 是船长。"

    keywords = derive_keywords(text, "第一章 图书馆")

    assert "铜钥匙" in keywords
    assert "老陈" in keywords
    assert "Melville" in keywords
    assert "第一章" not in keywords
    assert len(keywords) <= 8


def test_derive_keywords_rejects_bracket_prose_and_numeric_noise():
    """括号整句、数字/页码/骰式、目录点线、n-gram 碎片都不应成为触发词。"""
    text = (
        "（详见下文）这句话是整句注释。徐福服下了仙药，徐福又提到仙药。"
        "冯季阳在兵马俑坑里发现了冯季阳的笔记。调查员应当进行检定，SC 0/1。"
        "孩子与绣花鞋......................."
    )

    keywords = derive_keywords(text, "本模组根据coc7版规则创作")

    assert "详见下文" not in keywords
    assert "冯季阳" in keywords
    assert "冯季" not in keywords and "季阳" not in keywords
    assert "调查员" not in keywords
    assert not any(any(ch.isdigit() for ch in item) for item in keywords)
    assert not any("....." in item for item in keywords)
    assert len(keywords) <= 8
    # 跨词边界的 n-gram（内含规则书泛词）不是专名。
    assert "如果调查" not in derive_keywords("如果调查了地面，如果调查了墙壁。", "标题")


def test_constant_and_tier_derivation():
    assert is_constant_section("世界观设定")
    assert not is_constant_section("第一章 图书馆")
    assert derive_tier("真相终局") == "core"
    assert derive_tier("第一章 图书馆") == "background"


def test_list_and_update_knowledge_section_lorebook_fields():
    chunks = build_knowledge_chunks(
        {
            "id": "case-1",
            "scenario_version": "1",
            "modules": [
                {"id": "c1", "module_type": "lore", "content": "甲之一", "metadata": {"section": "甲", "title": "甲章"}},
                {"id": "c2", "module_type": "lore", "content": "甲之二", "metadata": {"section": "甲", "title": "甲章"}},
                {"id": "c3", "module_type": "lore", "content": "乙", "metadata": {"section": "乙", "title": "乙章"}},
            ],
        }
    )

    sections = list_knowledge_sections(chunks)
    assert [item["section_key"] for item in sections] == ["section:甲", "section:乙"]
    assert sections[0]["chunk_ids"] == ["c1", "c2"]
    assert sections[0]["title"] == "甲章"

    updated, count = update_knowledge_section(
        chunks,
        "section:甲",
        {
            "keywords": "铜钥匙, 铜钥匙，暗门，真相",
            "secondary_keywords": ["夜晚"],
            "secondary_logic": "and_all",
            "visibility": "hidden",
            "keywords_visibility": "kp_only",
            "is_constant": True,
            "tier": "CORE",
            "probability": 999,
            "group": " 钥匙 ",
            "group_weight": -3,
            "sticky_rounds": "2",
            "trigger_chunks": 1,
        },
    )

    assert count == 2
    by_id = {chunk.chunk_id: chunk for chunk in updated}
    assert by_id["c1"].keywords == ["铜钥匙", "暗门", "真相"]  # 逗号/顿号混合分隔并去重
    assert by_id["c1"].visibility == "hidden"
    assert by_id["c1"].keywords_visibility == "kp_only"
    assert by_id["c2"].is_constant is True
    assert by_id["c1"].secondary_logic == "and_all"
    assert by_id["c1"].tier == "core"  # 大小写归一
    assert by_id["c1"].probability == 100  # 越界收敛
    assert by_id["c1"].group == "钥匙"
    assert by_id["c1"].group_weight == 0
    assert by_id["c1"].sticky_rounds == 2
    assert by_id["c1"].trigger_chunks is True
    # 未命中的章节不受影响
    assert by_id["c3"].keywords is None
    sections_after = list_knowledge_sections(updated)
    assert sections_after[0]["visibility"] == "hidden"
    assert sections_after[0]["keywords_visibility"] == "kp_only"
    assert sections_after[0]["keyword_spoiler"] is True


def test_update_knowledge_section_ignores_unknown_section():
    chunks = build_knowledge_chunks({"id": "case-1", "modules": [{"id": "c1", "module_type": "lore", "content": "甲"}]})

    updated, count = update_knowledge_section(chunks, "section:不存在", {"keywords": ["x"]})

    assert count == 0
    assert updated[0].keywords is None
