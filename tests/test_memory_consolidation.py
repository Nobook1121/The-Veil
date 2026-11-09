import json

import pytest

from trpg_server.agents import memory_consolidation
from trpg_server.agents.memory_consolidation import (
    archive_history_for_memory,
    build_memory_context,
    harvest_room_memory,
    load_memory_records,
    memory_cache_stats,
    memory_recall,
    remember_compact_facts,
    reset_memory_cache_stats,
)


def _write_ledger(path, records):
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record(index, text=None):
    return {
        "id": f"m{index}",
        "kind": "event",
        "text": text or f"第{index}条记录：码头仓库线索编号{index}",
        "importance": 2,
        "turn": index,
        "source": {"type": "event_log", "ref": f"evt{index}"},
        "created_at": "2026-11-09 10:00:00",
    }



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


def _simulate_turn(room_dir, turn):
    """模拟 chat.py 单轮的三处调用：收编 → 归档 → 构建注入块。"""
    harvest_room_memory(
        room_dir,
        {
            "clock": {"turn": turn},
            "event_log": [
                {
                    "id": f"evt_{turn}",
                    "kind": "player_decision",
                    "content": f"第{turn}轮玩家决定去码头仓库",
                }
            ],
        },
    )
    archive_history_for_memory(
        room_dir,
        [
            {"role": "user", "content": f"第{turn}轮：我去码头仓库看看"},
            {"role": "assistant", "content": f"第{turn}轮：仓库里堆满了旧木箱。"},
        ],
    )
    return build_memory_context(room_dir, "码头仓库线索")


def test_turn_reads_ledger_once_and_reuses_tokens(tmp_path):
    """长剧本量级：一次解析 + 一次性分词，之后每轮 0 解析、仅新增条目分词。"""
    _write_ledger(tmp_path / "memory_ledger.jsonl", [_record(i) for i in range(2500)])

    reset_memory_cache_stats()
    _simulate_turn(tmp_path, 1)
    cold = memory_cache_stats()
    # 冷启动只打开一次活动账本；打分只看最近 2000 条，因此分词量有上界。
    assert cold["segment_reads"] == 1
    assert cold["records_parsed"] == 2500
    assert cold["tokenizations"] <= memory_consolidation.MAX_LOAD_RECORDS

    reset_memory_cache_stats()
    _simulate_turn(tmp_path, 2)
    warm = memory_cache_stats()
    assert warm["segment_reads"] == 0      # 未重新解析文件
    assert warm["records_parsed"] == 0
    # 本轮新增 3 条（1 条决策 + 2 条对话），只有它们需要分词。
    assert warm["tokenizations"] <= 3


def test_dedupe_covers_records_beyond_recent_window(tmp_path):
    """早期记录（超出最近 2000 条窗口）仍参与去重，不会被重复追加。"""
    old_text = "第0条早期承诺：答应玛莎在月落前送回怀表"
    _write_ledger(
        tmp_path / "memory_ledger.jsonl",
        [_record(0, old_text), *[_record(i) for i in range(1, 2100)]],
    )
    added = harvest_room_memory(
        tmp_path,
        {
            "clock": {"turn": 2100},
            "event_log": [{"id": "dup", "kind": "player_decision", "content": old_text}],
        },
    )
    assert added == 0


def test_rotation_keeps_old_records_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_consolidation, "MEMORY_LEDGER_MAX_SEGMENT_RECORDS", 5)
    monkeypatch.setattr(memory_consolidation, "MEMORY_LEDGER_MAX_SEGMENT_BYTES", 1 << 30)

    for index in range(12):
        archive_history_for_memory(tmp_path, [{"role": "user", "content": f"第{index}条：码头线索"}], max_items=1)

    assert (tmp_path / "memory_ledger.1.jsonl").exists()
    records = load_memory_records(tmp_path)
    assert len(records) == 12
    assert any("第0条" in item["text"] for item in records)
    # 轮转后旧记录仍参与去重。
    assert archive_history_for_memory(tmp_path, [{"role": "user", "content": "第0条：码头线索"}], max_items=1) == 0


def test_legacy_ledger_without_optional_fields_is_readable(tmp_path):
    (tmp_path / "memory_ledger.jsonl").write_text(
        json.dumps({"kind": "event", "text": "旧格式记录：灯塔守夜人失踪"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    records = load_memory_records(tmp_path)
    assert len(records) == 1
    assert "灯塔" in records[0]["text"]
    assert "灯塔" in build_memory_context(tmp_path, "灯塔 守夜人")


def test_truncated_tail_line_is_not_skipped_permanently(tmp_path):
    ledger = tmp_path / "memory_ledger.jsonl"
    _write_ledger(ledger, [_record(0)])
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(1), ensure_ascii=False) + "\n")
        handle.write('{"id": "partial", "text": "写了一半')  # 模拟上游写到一半
    assert len(load_memory_records(tmp_path)) == 2
    # 补齐该行后再读，不会永久丢失。
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('的记录"}\n')
    assert len(load_memory_records(tmp_path)) == 3
