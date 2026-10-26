"""记忆账本：追加/幂等/检索、事实与摘要、KP-only 闸门。"""
from __future__ import annotations

import pytest

from trpg_server.agents.narrative.ledger import (
    LedgerVisibilityError,
    append_fact,
    append_ledger_event,
    ledger_size,
    load_facts,
    load_summaries,
    read_ledger_events,
    save_summary,
    set_fact_status,
)
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        nodes=[
            NarrativeNode(id="room_a", kind="location", title="门厅"),
            NarrativeNode(id="secret", kind="clue", title="黑幕真相", visibility="kp_only"),
        ],
    )


def test_ledger_append_is_idempotent_and_searchable(tmp_path) -> None:
    first = append_ledger_event(tmp_path, {"id": "evt_1", "turn": 1, "kind": "action", "summary": "调查员推开门"})
    second = append_ledger_event(tmp_path, {"id": "evt_1", "turn": 1, "kind": "action", "summary": "重复"})
    append_ledger_event(tmp_path, {"id": "evt_2", "turn": 2, "kind": "discovery", "summary": "发现血迹", "key": True})
    append_ledger_event(tmp_path, {"id": "evt_3", "turn": 2, "kind": "move", "summary": "进入书房"})

    assert first["id"] == second["id"]
    assert first["summary"] == "调查员推开门"
    assert ledger_size(tmp_path) > 0

    events = read_ledger_events(tmp_path)
    assert [item["id"] for item in events] == ["evt_1", "evt_2", "evt_3"]
    assert [item["_seq"] for item in events] == [1, 2, 3]
    assert [item["id"] for item in read_ledger_events(tmp_path, key_only=True)] == ["evt_2"]
    assert [item["id"] for item in read_ledger_events(tmp_path, search="书房")] == ["evt_3"]
    assert [item["id"] for item in read_ledger_events(tmp_path, after_seq=1)] == ["evt_2", "evt_3"]
    assert [item["id"] for item in read_ledger_events(tmp_path, limit=2)] == ["evt_2", "evt_3"]


def test_ledger_rotates_into_segments_and_keeps_global_order(tmp_path, monkeypatch) -> None:
    # §6.4.5：账本过大时按段切分，重放跨段保持顺序与全局序号。
    import trpg_server.agents.narrative.ledger as ledger_module

    append_ledger_event(tmp_path, {"id": "evt_1", "turn": 1, "kind": "action", "summary": "一"})
    monkeypatch.setattr(ledger_module, "LEDGER_SEGMENT_BYTES", 1)
    append_ledger_event(tmp_path, {"id": "evt_2", "turn": 2, "kind": "action", "summary": "二"})
    append_ledger_event(tmp_path, {"id": "evt_3", "turn": 3, "kind": "action", "summary": "三"})

    assert (tmp_path / "story_ledger.0001.jsonl").exists()
    assert (tmp_path / "ledger_manifest.json").exists()
    events = read_ledger_events(tmp_path)
    assert [item["id"] for item in events] == ["evt_1", "evt_2", "evt_3"]
    assert [item["_seq"] for item in events] == [1, 2, 3]
    assert ledger_size(tmp_path) > 0


def test_ledger_rejects_kp_only_names(tmp_path) -> None:
    with pytest.raises(LedgerVisibilityError):
        append_ledger_event(
            tmp_path,
            {"id": "evt_bad", "turn": 1, "kind": "discovery", "summary": "黑幕真相就在地下室"},
            bundle=_bundle(),
        )
    assert ledger_size(tmp_path) == 0


def test_summary_rejects_kp_only_names(tmp_path) -> None:
    with pytest.raises(LedgerVisibilityError):
        save_summary(tmp_path, "location", "room_a", "这里藏着黑幕真相", bundle=_bundle())
    assert load_summaries(tmp_path)["location"] == {}


def test_summary_roundtrip(tmp_path) -> None:
    save_summary(tmp_path, "location", "room_a", "门厅里有一盏摇晃的灯。", turn=3)
    save_summary(tmp_path, "scenario", "main", "调查员们刚进入宅邸。", turn=3)

    summaries = load_summaries(tmp_path)
    assert summaries["location"]["room_a"]["text"] == "门厅里有一盏摇晃的灯。"
    assert summaries["location"]["room_a"]["turn"] == 3
    assert summaries["scenario"]["main"]["text"] == "调查员们刚进入宅邸。"


def test_facts_default_provisional_and_status_transition(tmp_path) -> None:
    fact = append_fact(tmp_path, {"subject": "李默", "predicate": "看到", "object": "血迹", "turn": 2})
    assert fact["status"] == "provisional"
    assert fact["visibility"] == "kp_only"
    assert load_facts(tmp_path)[0]["id"] == fact["id"]

    updated = set_fact_status(tmp_path, fact["id"], "canon")
    assert updated["status"] == "canon"
    assert load_facts(tmp_path)[0]["status"] == "canon"


def test_append_fact_is_idempotent_for_same_canon_triple(tmp_path) -> None:
    first = append_fact(
        tmp_path,
        {"id": "fact_1", "subject": "李默", "predicate": "看到", "object": "血迹", "status": "canon"},
    )
    second = append_fact(
        tmp_path,
        {"subject": "李默", "predicate": "看到", "object": "血迹", "status": "canon"},
    )
    assert second["id"] == first["id"]
    assert len(load_facts(tmp_path)) == 1
