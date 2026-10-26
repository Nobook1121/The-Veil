"""图谱运行时：地点卡、出口硬条件、移动、交互、隐藏物件与线索。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError, narrative_available
from trpg_server.agents.narrative.state_store import (
    load_keyframes,
    load_narrative_state,
    save_narrative_state,
)
from trpg_server.agents.narrative.store import build_index, save_bundle


def _bundle(scenario_id: str = "s1") -> NarrativeBundle:
    return NarrativeBundle(
        scenario_id=scenario_id,
        nodes=[
            NarrativeNode(id="area_ground", kind="area", title="一层"),
            NarrativeNode(
                id="room_hall", kind="location", title="门厅", parent="area_ground",
                summary="门厅里有一盏摇晃的提灯。", body="门厅正文：墙纸发霉。",
                objects=[
                    {
                        "id": "obj_lamp", "name": "提灯", "summary": "一盏黄铜提灯。",
                        "visible": True, "visibility": "public",
                        "interactions": [{
                            "id": "int_lamp_investigate", "skill": "侦查", "difficulty": "regular",
                            "repeatable": False,
                            "outcomes": {
                                "success": {
                                    "text": "灯罩内侧有新鲜血迹。",
                                    "effects": {"clues": ["clue_blood"], "flags": {"lamp_checked": True}},
                                },
                                "fail": {"text": "你只看到一层油垢。"},
                            },
                        }],
                    },
                    {
                        "id": "obj_secret", "name": "暗格", "summary": "墙板后似乎有空间。",
                        "visible": True, "visibility": "hidden",
                        "reveal_when": {"flag": "secret_opened"},
                        "interactions": [],
                    },
                ],
            ),
            NarrativeNode(id="room_cellar", kind="location", title="地下室", parent="area_ground"),
            NarrativeNode(
                id="ending_escape", kind="ending", title="逃出宅邸",
                completion={"clock": "clock_flood", "gte": 10},
            ),
            NarrativeNode(
                id="clue_blood", kind="clue", title="灯罩血迹",
                summary="灯罩内侧的血迹还没干。", body="血迹属于失踪的管家。",
            ),
            NarrativeNode(
                id="clue_letter", kind="clue", title="旧信",
                summary="一封封口的旧信。", body="信里提到了地下室。",
                reveal_when={"flag": "letter_found"},
            ),
            NarrativeNode(id="clue_truth", kind="clue", title="黑幕真相", visibility="kp_only"),
            NarrativeNode(id="npc_butler", kind="npc", title="管家"),
            NarrativeNode(id="clock_flood", kind="clock", title="血池"),
        ],
        edges=[
            NarrativeEdge(
                id="edge_hall_cellar", from_id="room_hall", to_id="room_cellar",
                type="adjacent", hard=True, condition={"clock": "clock_flood", "gte": 10},
            ),
            NarrativeEdge(
                id="edge_cellar_ending", from_id="room_cellar", to_id="ending_escape",
                type="leads_to", bidirectional=False,
            ),
        ],
    )


def _runtime(tmp_path: Path, *, clock: int = 0) -> NarrativeRuntime:
    bundle = _bundle()
    save_narrative_state(tmp_path, {
        "location": "room_hall",
        "clock": {"turn": 3, "in_game_time": "23:47"},
        "node_status": {"room_hall": "active"},
        "clocks": {"clock_flood": {"value": clock, "threshold": 25}},
    })
    return NarrativeRuntime(
        bundle=bundle,
        narrative=load_narrative_state(tmp_path),
        index=build_index(bundle),
        room_id="room-1",
        room_dir=tmp_path,
    )


def test_keyframes_written_on_move_clock_reach_and_turn_interval(tmp_path) -> None:
    # §6.4.2：移动 / 时钟阈值 / 每 10 回合自动写关键帧。
    runtime = _runtime(tmp_path)

    runtime.record({"kind": "move", "to": "room_cellar"}, summary="移动到地下室")
    assert load_keyframes(tmp_path)[-1]["reason"] == "move"

    runtime.record(
        {"kind": "clock", "clock_id": "clock_flood", "value": 10, "threshold": 10, "turn": 11},
        summary="血池上涨",
    )
    assert load_keyframes(tmp_path)[-1]["reason"] == "clock_reach"

    runtime.record({"kind": "flag", "name": "lamp_checked", "value": True, "turn": 20}, summary="检查提灯")
    frames = load_keyframes(tmp_path)
    assert frames[-1]["reason"] == "interval"
    assert frames[-1]["turn"] == 20


def test_location_card_hides_unrevealed_objects_and_marks_blocked_exit(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    card = runtime.location_card()

    assert card["location_id"] == "room_hall"
    assert card["title"] == "门厅"
    assert [obj["id"] for obj in card["objects"]] == ["obj_lamp"]
    assert card["objects"][0]["interactions"][0]["skill"] == "侦查"
    assert card["objects"][0]["interactions"][0]["repeatable"] is False
    exit_entry = next(item for item in card["exits"] if item["target_id"] == "room_cellar")
    assert exit_entry["blocked"] is True
    assert exit_entry["missing"] == [{"clock": "clock_flood", "gte": 10}]
    assert card["exits"][0]["target_id"] == "room_cellar"


def test_inspect_secret_object_requires_flag_then_reveals_once(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    with pytest.raises(NarrativeRuntimeError):
        runtime.inspect_object("暗格")

    runtime.record({"kind": "flag", "name": "secret_opened", "value": True}, summary="打开暗格")
    first = runtime.inspect_object("暗格")
    assert first["revealed_now"] is True
    assert first["name"] == "暗格"

    second = runtime.inspect_object("暗格")
    assert second["revealed_now"] is False


def test_move_rejected_until_hard_condition_met(tmp_path) -> None:
    runtime = _runtime(tmp_path, clock=0)
    with pytest.raises(NarrativeRuntimeError) as exc:
        runtime.move("地下室")
    assert "缺少" in str(exc.value)

    runtime.record({"kind": "clock", "clock_id": "clock_flood", "value": 10})
    result = runtime.move("地下室")
    assert result["moved"] is True
    assert load_narrative_state(tmp_path)["location"] == "room_cellar"


def test_ending_requires_completion_condition(tmp_path) -> None:
    runtime = _runtime(tmp_path, clock=5)
    runtime.narrative["location"] = "room_cellar"
    save_narrative_state(tmp_path, runtime.narrative)

    with pytest.raises(NarrativeRuntimeError) as exc:
        runtime.move("逃出宅邸")
    assert "结局条件未满足" in str(exc.value)

    runtime.record({"kind": "clock", "clock_id": "clock_flood", "value": 10})
    result = runtime.move("逃出宅邸")
    assert result["moved"] is True


def test_interaction_applies_outcome_and_is_one_shot(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    result = runtime.resolve_interaction(
        "提灯", "int_lamp_investigate", "success", skill="侦查", total=42, roll=12, pc="pc_li",
    )
    assert result["outcome_text"] == "灯罩内侧有新鲜血迹。"
    assert result["resolved"] is True

    narrative = load_narrative_state(tmp_path)
    assert narrative["interaction_results"]["int_lamp_investigate"] == "success"
    assert narrative["revealed_clues"] == ["clue_blood"]
    assert narrative["flags"]["lamp_checked"] is True
    assert narrative["checks"][-1]["skill"] == "侦查"
    assert narrative["checks"][-1]["total"] == 42

    again = runtime.resolve_interaction("提灯", "int_lamp_investigate", "success")
    assert again["already_resolved"] is True
    assert again["resolved"] is False


def test_reveal_clue_respects_conditions_and_kp_only(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    with pytest.raises(NarrativeRuntimeError):
        runtime.reveal_clue("旧信")
    with pytest.raises(NarrativeRuntimeError):
        runtime.reveal_clue("黑幕真相")

    runtime.record({"kind": "flag", "name": "letter_found", "value": True})
    result = runtime.reveal_clue("旧信", pc="pc_li")
    assert result["revealed"] is True
    assert result["body"] == "信里提到了地下室。"
    assert load_narrative_state(tmp_path)["pc_known_clues"] == {"pc_li": ["clue_letter"]}


def test_update_npc_state_only_touches_npc(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    result = runtime.update_npc_state("管家", {"delta": -2, "location": "room_cellar", "visible": False})

    assert result["state"]["attitude"] == -2
    narrative = load_narrative_state(tmp_path)
    assert narrative["npc_state"]["npc_butler"]["location"] == "room_cellar"
    assert narrative["npc_state"]["npc_butler"]["visible"] is False
    assert narrative["pc_state"] == {}


def test_lookup_location_hides_unrevealed_body(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    result = runtime.lookup_location("地下")

    assert result["matches"][0]["location_id"] == "room_cellar"
    assert "body" not in result["matches"][0]
    assert result["matches"][0]["visited"] is False


def test_narrative_available_detects_bundle(tmp_path) -> None:
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text('{"id": "s1", "modules": []}', encoding="utf-8")
    room_dir = tmp_path / "room"
    room_dir.mkdir()

    def context():
        return SimpleNamespace(
            room_dir=room_dir,
            scenarios_dir=scenarios,
            room_id="room-1",
            room_info=lambda: {"scenario_id": "s1"},
        )

    assert narrative_available(context()) is False
    save_bundle(scenario_dir, _bundle())
    assert narrative_available(context()) is True
    runtime = NarrativeRuntime.load(context())
    assert runtime.bundle.scenario_id == "s1"
