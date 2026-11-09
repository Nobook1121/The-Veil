import json

from trpg_server.agents.memory_consolidation import (
    archive_history_for_memory,
    build_memory_context,
    harvest_room_memory,
    load_memory_records,
    memory_recall,
    remember_compact_facts,
)


def _state_with_events():
    return {
        "clock": {"turn": 12},
        "event_log": [
            {
                "id": "evt_1",
                "kind": "player_decision",
                "content": "玩家答应把怀表还给老板娘玛莎",
                "created_at": "2026-11-09 10:00:00",
            },
            {
                "id": "evt_2",
                "kind": "state_update",
                "content": "AI validated state update",
                "metadata": {"clues": ["染血的船票"]},
                "created_at": "2026-11-09 10:05:00",
            },
        ],
    }


def test_harvest_and_dedupe(tmp_path):
    state = _state_with_events()
    assert harvest_room_memory(tmp_path, state) == 2
    assert harvest_room_memory(tmp_path, state) == 0

    records = load_memory_records(tmp_path)
    texts = [item["text"] for item in records]
    assert any("玛莎" in text for text in texts)
    assert any("染血的船票" in text for text in texts)
    assert all(item["source"].get("type") for item in records)


def test_archive_history_and_recall(tmp_path):
    history = [
        {"role": "user", "content": "我把银钥匙交给守夜人，答应替他查清灯塔的事"},
        {"role": "assistant", "content": "守夜人收下钥匙，再三叮嘱你不要声张。"},
    ]
    assert archive_history_for_memory(tmp_path, history) == 2
    assert archive_history_for_memory(tmp_path, history) == 0

    context = build_memory_context(tmp_path, "守夜人 灯塔 钥匙", budget_chars=600, max_items=4)
    assert "长期记忆" in context
    assert "守夜人" in context
    assert "对话归档" in context
    assert "不得编造" in context

    unrelated = build_memory_context(tmp_path, "今天天气如何", budget_chars=600, max_items=4)
    assert unrelated == ""


def test_compact_facts_are_traceable(tmp_path):
    entries = [
        {
            "role": "system",
            "compact": True,
            "content": "【历史压缩摘要】\nnarrative...\n关键事实：\n- [承诺] 答应玛莎在月落前送回怀表\n- [决策] 选择从西侧码头进入",
        }
    ]
    assert remember_compact_facts(tmp_path, entries) == 2
    records = load_memory_records(tmp_path)
    assert {item["kind"] for item in records} == {"promise", "decision"}
    assert all(item["source"]["type"] == "compact_summary" for item in records)

    recalls = memory_recall(tmp_path, "玛莎 怀表")
    assert recalls and "玛莎" in recalls[0]["text"]


def test_inject_budget_limits_output(tmp_path):
    for index in range(10):
        archive_history_for_memory(
            tmp_path,
            [{"role": "user", "content": f"第{index}条线索：在码头发现了刻着旧神符号的木箱"}],
        )
    context = build_memory_context(tmp_path, "码头 木箱 旧神符号", budget_chars=240, max_items=10)
    assert context
    # 标题之外只保留能放进预算的少量条目，不会把 10 条全部注入。
    assert 1 <= context.count("\n- ") <= 3


def test_agent_memory_file_is_harvested(tmp_path):
    (tmp_path / "agent_memory.json").write_text(
        json.dumps(
            [
                {
                    "id": "m1",
                    "kind": "fact",
                    "content": "玩家从神父那里取得了地窖钥匙",
                    "importance": 4,
                    "created_at": "2026-11-09 09:00:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert harvest_room_memory(tmp_path, {}) == 1
    context = build_memory_context(tmp_path, "神父 地窖钥匙", budget_chars=400, max_items=3)
    assert "神父" in context
    assert "AI 记忆" in context


def test_prompt_layers_include_memory_segment():
    from trpg_server.agents.prompt_builder import build_prompt_layers

    with_memory = build_prompt_layers(
        global_rules="rules",
        scenario=None,
        scene=None,
        room_state={},
        history=[],
        user_input="hi",
        memory_context="【长期记忆】玩家早期答应过玛莎还怀表",
    )
    assert "memory" in with_memory.segments
    assert any("玛莎" in message["content"] for message in with_memory.messages)

    without_memory = build_prompt_layers(
        global_rules="rules",
        scenario=None,
        scene=None,
        room_state={},
        history=[],
        user_input="hi",
    )
    assert "memory" not in without_memory.segments
