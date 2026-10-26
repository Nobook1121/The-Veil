"""房间叙事状态：迁移、事件应用、旧字段投影、关键帧与重放。"""
from __future__ import annotations

from trpg_server.agents.narrative.ledger import append_ledger_event, read_ledger_events
from trpg_server.agents.narrative.state_store import (
    apply_event,
    apply_to_narrative,
    default_narrative_state,
    latest_keyframe,
    load_narrative_state,
    narrative_from_room_state,
    replay_narrative,
    save_narrative_state,
    write_keyframe,
)
from trpg_server.agents.room_state import load_room_state, save_room_state


def test_legacy_state_migrates_into_narrative(tmp_path) -> None:
    save_room_state(tmp_path, {
        "active_scene_id": "room_b",
        "cursor": "room_a",
        "path": ["room_a", "room_b"],
        "clues": ["clue_blood"],
        "items": ["crowbar"],
        "npc_attitudes": {"npc_keeper": -2},
    })

    narrative = load_narrative_state(tmp_path)

    assert narrative["location"] == "room_b"
    assert narrative["visit_log"] == ["room_a", "room_b"]
    assert narrative["node_status"]["room_b"] == "active"
    assert narrative["revealed_clues"] == ["clue_blood"]
    assert narrative["pc_state"]["party"]["inventory"] == ["crowbar"]
    assert narrative["npc_state"]["npc_keeper"]["attitude"] == -2


def test_apply_event_updates_state_and_legacy_projection(tmp_path) -> None:
    save_narrative_state(tmp_path, default_narrative_state())

    apply_event(tmp_path, {"id": "e1", "kind": "move", "to": "room_a", "turn": 1, "actor": "pc_li"})
    apply_event(tmp_path, {"id": "e2", "kind": "move", "to": "room_b", "turn": 2, "actor": "pc_wang"})
    apply_event(tmp_path, {"id": "e3", "kind": "clue", "clue_id": "clue_1", "pc": "pc_li", "turn": 2})
    apply_event(tmp_path, {"id": "e4", "kind": "item", "item_id": "lamp", "pc": "pc_li", "action": "add"})
    apply_event(tmp_path, {"id": "e5", "kind": "attitude", "npc_id": "npc_1", "delta": -1})
    apply_event(tmp_path, {"id": "e6", "kind": "clock", "clock_id": "clock_flood", "delta": 3, "threshold": 10})

    narrative = load_narrative_state(tmp_path)
    assert narrative["location"] == "room_b"
    assert narrative["party_split"] is True
    assert narrative["party_locations"] == {"pc_li": "room_a", "pc_wang": "room_b"}
    assert narrative["revealed_clues"] == ["clue_1"]
    assert narrative["pc_known_clues"] == {"pc_li": ["clue_1"]}
    assert narrative["pc_state"]["pc_li"]["inventory"] == ["lamp"]
    assert narrative["npc_state"]["npc_1"]["attitude"] == -1
    assert narrative["clocks"]["clock_flood"] == {"value": 3, "threshold": 10}

    state = load_room_state(tmp_path)
    assert state["active_scene_id"] == "room_b"
    assert state["cursor"] == "room_b"
    assert state["path"] == ["room_a", "room_b"]
    assert state["clues"] == ["clue_1"]
    assert state["items"] == ["lamp"]
    assert state["npc_attitudes"] == {"npc_1": -1}


def test_apply_event_is_idempotent_by_event_id(tmp_path) -> None:
    apply_event(tmp_path, {"id": "clock_1", "kind": "clock", "clock_id": "clock_flood", "delta": 2})
    apply_event(tmp_path, {"id": "clock_1", "kind": "clock", "clock_id": "clock_flood", "delta": 2})

    narrative = load_narrative_state(tmp_path)
    assert narrative["clocks"]["clock_flood"]["value"] == 2
    assert narrative["applied_event_ids"] == ["clock_1"]


def test_replay_uses_keyframe_and_ledger(tmp_path) -> None:
    apply_event(tmp_path, {"id": "evt_move", "kind": "move", "to": "room_a", "turn": 1})
    append_ledger_event(tmp_path, {"id": "evt_move", "kind": "move", "turn": 1, "location_id": "room_a"})
    write_keyframe(tmp_path, turn=1, reason="test", ledger_seq=0)

    # 关键帧之后的新事件只写账本，不写 state，模拟崩溃后重放。
    append_ledger_event(tmp_path, {
        "id": "evt_clue", "kind": "discovery", "turn": 2,
        "location_id": "room_a", "summary": "发现血迹", "entities": ["clue_1"],
    })
    assert len(read_ledger_events(tmp_path, after_seq=0)) == 2

    replayed = replay_narrative(tmp_path)
    assert replayed["location"] == "room_a"
    assert replayed["clock"]["turn"] == 2
    assert latest_keyframe(tmp_path)["reason"] == "test"


def test_keyframe_checksum_and_retention(tmp_path) -> None:
    keyframe = write_keyframe(tmp_path, narrative_from_room_state({}), turn=0, reason="empty")
    assert keyframe["checksum"].startswith("sha1:")
    assert keyframe["keyframe_id"] == "kf_0001"

    for turn in range(1, 25):
        write_keyframe(tmp_path, narrative_from_room_state({"narrative": {"clock": {"turn": turn}}}), turn=turn)
    from trpg_server.agents.narrative.state_store import load_keyframes

    keyframes = load_keyframes(tmp_path)
    assert len(keyframes) == 20
    assert keyframes[0]["keyframe_id"] == "kf_0001"
    assert keyframes[-1]["turn"] == 24


def test_apply_to_narrative_does_not_touch_disk(tmp_path) -> None:
    original = default_narrative_state()
    updated = apply_to_narrative(original, {"id": "x", "kind": "flag", "name": "door_open", "value": True})
    assert updated["flags"]["door_open"] is True
    assert original["flags"] == {}
    assert not (tmp_path / "state.json").exists()
