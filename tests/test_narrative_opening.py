from __future__ import annotations

from pathlib import Path

from trpg_server.agents.narrative.builder import build_narrative
from trpg_server.agents.narrative.model import (
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
    normalize_start,
)
from trpg_server.agents.narrative.opening import (
    OpeningError,
    begin_scenario,
    build_opening_pack,
    plan_scenario,
    render_opening_prompt,
)
from trpg_server.agents.narrative.state_store import (
    load_keyframes,
    load_narrative_state,
)
from trpg_server.agents.narrative.store import build_index
from trpg_server.agents.narrative.validate import validate_bundle
from trpg_server.agents.room_state import load_room_state, save_room_state


def _bundle(
    *,
    mode: str = "hybrid",
    intro: list[str] | None = None,
    attachments: list[str] | None = None,
) -> NarrativeBundle:
    return NarrativeBundle(
        scenario_id="s1",
        scenario_version="2",
        topology="hub",
        nodes=[
            NarrativeNode(
                id="room_hall",
                kind="location",
                title="门厅",
                summary="潮湿的门厅",
                objects=[{"id": "obj_door", "name": "木门", "summary": "一扇锁着的木门。"}],
                kp_notes="门后藏着管家。",
            ),
            NarrativeNode(id="room_cellar", kind="location", title="地下室"),
            NarrativeNode(id="clue_secret", kind="clue", title="密室真相", summary="秘密", visibility="hidden"),
            NarrativeNode(id="clock_1", kind="clock", title="仪式推进"),
        ],
        edges=[
            NarrativeEdge(
                id="edge_hall_cellar",
                from_id="room_hall",
                to_id="room_cellar",
                type="adjacent",
                label="下楼",
            )
        ],
        start={
            "location_id": "room_hall",
            "opening_mode": mode,
            "public_intro": intro if intro is not None else ["你们站在门厅里。"],
            "opening_attachments": attachments or [],
        },
        clocks=[{"id": "clock_1", "threshold": 4}],
        triggers=[
            {
                "id": "trg_opening",
                "label": "开场钟声",
                "binding": {"kind": "enter", "location_id": "room_hall", "first_time": True},
                "content": "门厅的钟声响起。",
            },
            {
                "id": "trg_reenter",
                "label": "再次进入",
                "binding": {"kind": "enter", "location_id": "room_hall", "first_time": False},
                "content": "你再次回到门厅。",
            },
        ],
        assets=[
            {
                "id": "asset_cover",
                "kind": "image",
                "url": "http://example.test/hall.png",
                "mime": "image/png",
                "alt": "门厅照片",
                "visibility": "public",
            }
        ],
    )


def test_normalize_start_supports_legacy_string_and_modes() -> None:
    assert normalize_start("hall")["location_id"] == "hall"
    assert normalize_start({"fixed_opening": True})["opening_mode"] == "fixed"
    assert normalize_start({})["opening_mode"] == "hybrid"
    assert normalize_start({"public_intro": "第一段\n\n第二段"})["public_intro"] == ["第一段", "第二段"]


def test_bundle_start_round_trips() -> None:
    bundle = _bundle()
    restored = NarrativeBundle.from_dict(bundle.to_dict())
    assert restored.start["location_id"] == "room_hall"
    assert restored.start["opening_mode"] == "hybrid"


def test_builder_records_start_and_opening_module() -> None:
    modules = [
        {"id": "m_hall", "module_type": "scene", "title": "门厅", "content": "门厅描述"},
        {"id": "m_open", "module_type": "opening", "title": "开场", "content": "你们来到门厅。"},
    ]
    result = build_narrative(
        "",
        scenario={
            "id": "s1",
            "modules": modules,
            "start": {
                "location_id": "location_门厅",
                "opening_mode": "model",
                "public_intro": ["作者写好的开场。"],
            },
        },
        modules=modules,
        save=False,
    )
    assert result.bundle.start["opening_mode"] == "model"
    assert result.bundle.start["public_intro"] == ["作者写好的开场。"]
    assert result.bundle.start["opening_module_id"] == "m_open"
    assert result.bundle.meta["start"] == result.bundle.nodes[0].id


def test_begin_scenario_resets_state_and_fires_first_enter(tmp_path: Path) -> None:
    room_dir = tmp_path / "room1"
    save_room_state(
        room_dir,
        {
            "active_scene_id": "old_scene",
            "clues": [{"id": "old_clue"}],
            "items": [{"id": "old_item"}],
            "npc_attitudes": {"管家": "hostile"},
        },
    )
    plan = begin_scenario(room_dir, _bundle(), {}, scenario_version="2", room_id="room1")

    state = load_narrative_state(room_dir)
    assert state["location"] == "room_hall"
    assert state["visit_log"] == ["room_hall"]
    assert state["node_status"]["room_hall"] == "active"
    assert state["clocks"]["clock_1"]["value"] == 0
    assert state["scenario_version"] == "2"
    assert plan.triggers_fired == ["trg_opening"]

    legacy = load_room_state(room_dir)
    assert legacy["active_scene_id"] == "room_hall"
    assert legacy["clues"] == []
    assert legacy["items"] == []
    assert legacy["npc_attitudes"] == {}

    keyframes = load_keyframes(room_dir)
    assert keyframes and keyframes[-1]["reason"] == "start"

    messages = plan.messages()
    assert messages[0]["type"] == "kp"
    assert messages[0]["metadata"]["message_kind"] == "opening"
    assert "门厅的钟声响起。" in [item["content"] for item in messages[1:]]


def test_model_mode_keeps_intro_for_prompt_and_only_publishes_attachments(tmp_path: Path) -> None:
    plan = begin_scenario(
        tmp_path / "room1",
        _bundle(mode="model", attachments=["asset_cover"]),
        {},
    )
    messages = plan.messages()
    assert messages[0]["content"] == ""
    assert messages[0]["metadata"]["attachments"][0]["url"] == "http://example.test/hall.png"
    assert any(item.get("type") == "trigger" for item in messages[1:])
    prompt = plan.prompt_text()
    assert "你们站在门厅里。" in prompt
    assert "请据此撰写开场" in prompt


def test_fixed_mode_publishes_intro_and_prompt_forbids_rewrite(tmp_path: Path) -> None:
    plan = begin_scenario(tmp_path / "room1", _bundle(mode="fixed"), {})
    messages = plan.messages()
    assert messages[0]["content"] == "你们站在门厅里。"
    assert "不要逐字复述" in render_opening_prompt(plan.pack, published=True)


def test_opening_module_intro_extraction_filters_kp_lines(tmp_path: Path) -> None:
    scenario = {
        "modules": [
            {
                "id": "open",
                "module_type": "opening",
                "content": "你们来到门厅。\n\nKP：管家其实是凶手。\n实际上门后有血迹。",
            }
        ]
    }
    bundle = _bundle(intro=[])
    bundle.start["opening_module_id"] = "open"
    plan = begin_scenario(tmp_path / "room1", bundle, scenario)

    assert plan.pack["public_intro"] == ["你们来到门厅。"]
    assert plan.missing_intro is False
    assert "管家其实是凶手" in plan.pack["kp_truth"]
    assert "实际上门后有血迹" in plan.pack["kp_truth"]
    prompt = plan.prompt_text()
    assert "KP 专属真相" in prompt
    assert "密室真相" in prompt


def test_missing_intro_uses_minimal_opening(tmp_path: Path) -> None:
    plan = begin_scenario(tmp_path / "room1", _bundle(intro=[]), {})
    assert plan.missing_intro is True
    assert plan.pack["public_intro"] == ["你们抵达了门厅。"]


def test_plan_scenario_is_read_only(tmp_path: Path) -> None:
    room_dir = tmp_path / "room1"
    begin_scenario(room_dir, _bundle(), {})
    before = (room_dir / "state.json").read_text(encoding="utf-8")
    plan = plan_scenario(room_dir, _bundle(), {})
    after = (room_dir / "state.json").read_text(encoding="utf-8")
    assert plan.location_id == "room_hall"
    assert before == after


def test_start_without_location_raises(tmp_path: Path) -> None:
    bundle = NarrativeBundle(scenario_id="empty", start={"location_id": None})
    try:
        begin_scenario(tmp_path / "room1", bundle, {})
    except OpeningError:
        pass
    else:  # pragma: no cover - 明确失败信息
        raise AssertionError("缺少起始地点时应抛出 OpeningError")


def test_validate_reports_broken_start_config() -> None:
    bundle = NarrativeBundle(
        scenario_id="s1",
        nodes=[NarrativeNode(id="room_a", kind="location", title="A")],
        start={"location_id": "room_missing", "opening_attachments": ["asset_ghost"]},
    )
    report = validate_bundle(bundle, index=build_index(bundle))
    codes = {issue.code for issue in report.issues}
    assert "start.unknown_location" in codes
    assert "start.missing_asset" in codes
