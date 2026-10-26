"""触发器运行时：自动触发、一次性、可见性、分组与工具入口。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.publisher import publish_hits
from trpg_server.agents.narrative.resolver import TriggerHit
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError
from trpg_server.agents.narrative.state_store import load_narrative_state, save_narrative_state
from trpg_server.agents.tools.narrative_triggers import list_available, request_trigger


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        scenario_id="s1",
        nodes=[
            NarrativeNode(
                id="room_hall", kind="location", title="门厅",
                summary="门厅",
                objects=[{
                    "id": "obj_note", "name": "便签", "summary": "一张便签。", "visible": True,
                    "visibility": "public",
                    "interactions": [{
                        "id": "int_note", "skill": "侦查", "difficulty": "regular",
                        "repeatable": True,
                        "outcomes": {
                            "success": {"text": "便签上写着暗号。", "effects": {"clocks": {"clock_1": 2}}},
                        },
                    }],
                }],
            ),
            NarrativeNode(id="room_cellar", kind="location", title="地下室"),
            NarrativeNode(id="clue_1", kind="clue", title="暗号线索", summary="暗号", body="暗号正文"),
            NarrativeNode(id="npc_1", kind="npc", title="管家", parent="room_hall"),
            NarrativeNode(id="clock_1", kind="clock", title="仪式推进"),
        ],
        edges=[NarrativeEdge(id="edge_hall_cellar", from_id="room_hall", to_id="room_cellar", type="adjacent")],
        triggers=[
            {
                "id": "trg_note", "label": "便签",
                "binding": {"kind": "interaction", "object_id": "obj_note", "interaction_id": "int_note", "result": "success"},
                "attachments": [{"id": "att_note", "kind": "richtext", "body": "便签上写着暗号。", "order": 1}],
                "group": "study", "priority": 10,
            },
            {
                "id": "trg_note_extra", "label": "第二页",
                "binding": {"kind": "interaction", "object_id": "obj_note", "interaction_id": "int_note", "result": "success"},
                "attachments": [{"id": "att_extra", "kind": "richtext", "body": "第二页还有字。", "order": 0}],
                "group": "study", "priority": 5,
            },
            {
                "id": "trg_kp", "label": "KP 备忘",
                "binding": {"kind": "interaction", "object_id": "obj_note", "interaction_id": "int_note", "result": "success"},
                "content": "KP only 内容不可给玩家", "visibility": "kp_only",
            },
            {
                "id": "trg_clock", "label": "仪式推进",
                "binding": {"kind": "clock", "clock_id": "clock_1", "gte": 2},
                "content": "远处传来钟声。",
            },
            {
                "id": "trg_enter_first", "label": "第一次进入",
                "binding": {"kind": "enter", "location_id": "room_cellar", "first_time": True},
                "attachments": [{"id": "att_enter", "kind": "image", "asset_id": "asset_img", "order": 0}],
            },
            {
                "id": "trg_enter_again", "label": "再次进入",
                "binding": {"kind": "enter", "location_id": "room_cellar", "first_time": False},
                "content": "你再次回到地下室。",
            },
            {
                "id": "trg_clue", "label": "暗号线索",
                "binding": {"kind": "clue", "clue_id": "clue_1"},
                "content": "暗号指向东侧。",
            },
            {
                "id": "trg_npc", "label": "管家",
                "binding": {"kind": "npc", "npc_id": "npc_1", "event": "attitude"},
                "content": "管家的态度变了。",
            },
            {
                "id": "trg_manual", "label": "敲门",
                "binding": {"kind": "manual", "keyword": "敲门"},
                "content": "门后传来回应。",
            },
            {
                "id": "trg_manual_review", "label": "待复核",
                "binding": {"kind": "manual", "note": "旧自然语言条件"},
                "condition": {"note": "旧自然语言条件"},
                "needs_review": True,
                "content": "待复核内容。",
            },
        ],
        assets=[{
            "id": "asset_img", "kind": "image", "url": "http://example.test/cellar.png",
            "mime": "image/png", "alt": "地下室照片",
        }],
    )


def _context(tmp_path: Path) -> SimpleNamespace:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text('{"id": "s1", "modules": []}', encoding="utf-8")
    save_bundle(scenario_dir, _bundle())
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    save_narrative_state(room_dir, {"location": "room_hall", "clock": {"turn": 1}})
    return SimpleNamespace(
        room_dir=room_dir,
        scenarios_dir=scenarios,
        room_id="room-1",
        room_info=lambda: {"scenario_id": "s1"},
    )


def _runtime(context: SimpleNamespace) -> NarrativeRuntime:
    return NarrativeRuntime.load(context)


def test_interaction_fires_grouped_trigger_and_clock(tmp_path) -> None:
    runtime = _runtime(_context(tmp_path))
    result = runtime.resolve_interaction("obj_note", "int_note", "success")
    assert result["resolved"] is True
    assert set(result["triggers_fired"]) == {"trg_note", "trg_note_extra", "trg_clock"}
    messages = result["direct_messages"]
    assert len(messages) == 2
    grouped = messages[0]
    assert grouped["metadata"]["trigger_group"] == "study"
    assert {item["id"] for item in grouped["metadata"]["attachments"]} == {"att_note", "att_extra"}
    assert "便签" in grouped["content"]
    assert all("KP only" not in message["content"] for message in messages)
    assert any(item["trigger_id"] == "trg_kp" and item["reason"] == "visibility_denied" for item in result["trigger_denied"])

    state = load_narrative_state(runtime.room_dir)
    assert state["trigger_fires"]["trg_note"]["count"] == 1
    assert state["trigger_fires"]["trg_clock"]["count"] == 1
    assert "trg_kp" not in state["trigger_fires"]


def test_once_prevents_second_interaction_trigger(tmp_path) -> None:
    runtime = _runtime(_context(tmp_path))
    runtime.resolve_interaction("obj_note", "int_note", "success")
    second = runtime.resolve_interaction("obj_note", "int_note", "success")
    assert second["triggers_fired"] == []
    assert second["direct_messages"] == []
    assert any(item["reason"] == "once" for item in second["trigger_denied"])


def test_move_enter_first_time_and_reenter(tmp_path) -> None:
    runtime = _runtime(_context(tmp_path))
    first = runtime.move("room_cellar")
    assert first["visited_before"] is False
    assert "trg_enter_first" in first["triggers_fired"]
    assert "trg_enter_again" not in first["triggers_fired"]
    assert first["direct_messages"][0]["metadata"]["attachments"][0]["kind"] == "image"

    runtime.move("room_hall")
    third = runtime.move("room_cellar")
    assert third["visited_before"] is True
    assert "trg_enter_again" in third["triggers_fired"]


def test_reveal_clue_and_update_npc_fire_triggers(tmp_path) -> None:
    context = _context(tmp_path)
    runtime = _runtime(context)
    clue = runtime.reveal_clue("clue_1")
    assert "trg_clue" in clue["triggers_fired"]
    assert clue["direct_messages"][0]["content"] == "暗号指向东侧。"

    npc = runtime.update_npc_state("npc_1", {"value": 3})
    assert "trg_npc" in npc["triggers_fired"]
    assert npc["direct_messages"][0]["content"] == "管家的态度变了。"


def test_check_id_is_validated_before_interaction(tmp_path) -> None:
    runtime = _runtime(_context(tmp_path))
    runtime.record({"kind": "check", "check": {"check_id": "chk_1", "skill": "侦查", "result": "success"}})
    ok = runtime.resolve_interaction("obj_note", "int_note", "success", check_id="chk_1")
    assert ok["resolved"] is True
    with pytest.raises(NarrativeRuntimeError):
        runtime.resolve_interaction("obj_note", "int_note", "fail", check_id="chk_1")


def test_request_manual_trigger_and_review_guard(tmp_path) -> None:
    context = _context(tmp_path)
    runtime = _runtime(context)
    result = runtime.request_trigger("trg_manual", action_text="我想敲门")
    assert result["triggered"] is True
    assert result["direct_messages"][0]["content"] == "门后传来回应。"

    denied = runtime.request_trigger("trg_manual_review")
    assert denied["triggered"] is False
    assert denied["denied"][0]["reason"] == "condition_not_met"

    missing = runtime.request_trigger("trg_missing")
    assert missing["triggered"] is False
    assert missing["denied"][0]["reason"] == "trigger_not_found"


def test_tool_handlers_list_and_request(tmp_path) -> None:
    context = _context(tmp_path)
    available = list_available({}, context)
    ids = {item["trigger_id"] for item in available["triggers"]}
    assert "trg_manual" in ids
    assert all("content" not in item for item in available["triggers"])

    result = request_trigger({"trigger_id": "trg_manual", "action_text": "敲门"}, context)
    assert result["triggered"] is True

    error = request_trigger({}, context)
    assert error["error"]


def test_publish_hits_collapses_after_three_messages() -> None:
    hits = [
        TriggerHit(
            spec={
                "id": f"trg_{index}", "label": f"资料 {index}", "content": f"内容 {index}",
                "group": "", "priority": 0, "visibility": "player_visible", "spoiler_level": 0,
            },
            attachments=[],
        )
        for index in range(5)
    ]
    messages = publish_hits(hits, source="manual")
    assert len(messages) == 4
    assert messages[-1]["content"] == "还有 2 份资料。"
    assert messages[-1]["metadata"]["collapsed_count"] == 2


def test_trigger_events_are_logged(caplog) -> None:
    """§10.2：求值、触发与被拒都要有结构化事件名。"""
    import logging

    from trpg_server.agents.narrative.resolver import TriggerResolver

    resolver = TriggerResolver(_bundle(), {"clock": {"turn": 0}})
    with caplog.at_level(logging.INFO, logger="trpg_server.narrative"):
        hits, denied = resolver.evaluate(
            "interaction",
            {"object_id": "obj_note", "interaction_id": "int_note", "result": "success"},
            audience="player",
        )
    assert hits
    assert any(item["reason"] == "visibility_denied" for item in denied)
    text = caplog.text
    assert "narrative.trigger.evaluate" in text
    assert "narrative.trigger.fire" in text
    assert "narrative.trigger.denied" in text
    assert "visibility_denied" in text

