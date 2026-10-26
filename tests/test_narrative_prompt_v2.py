"""状态摘要：确定性、可见性过滤、出口封锁与长度预算（§6.4.4）。"""
from __future__ import annotations

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.prompt_v2 import render_state_summary


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        nodes=[
            NarrativeNode(id="floor", kind="area", title="一层"),
            NarrativeNode(id="hall", kind="location", title="门厅", parent="floor"),
            NarrativeNode(id="cellar", kind="location", title="地下室", parent="floor"),
            NarrativeNode(id="attic", kind="location", title="阁楼", parent="floor"),
            NarrativeNode(id="clue_1", kind="clue", title="血迹", parent="hall"),
            NarrativeNode(id="lamp", kind="item", title="提灯", parent="hall"),
            NarrativeNode(id="npc_butler", kind="npc", title="管家", parent="hall"),
            NarrativeNode(id="clock_flood", kind="clock", title="血池", parent="hall"),
        ],
        edges=[
            NarrativeEdge(
                id="edge_hall_cellar", from_id="hall", to_id="cellar", type="adjacent",
                hard=True, condition={"clock": "clock_flood", "gte": 10},
            ),
            NarrativeEdge(
                id="edge_hall_attic", from_id="hall", to_id="attic", type="portal",
                visibility="hidden",
            ),
        ],
    )


def _state() -> dict:
    return {
        "location": "hall",
        "clock": {"turn": 7, "in_game_time": "23:47"},
        "visit_log": ["hall"],
        "node_status": {"hall": "active"},
        "pc_state": {
            "pc_li": {
                "name": "李默", "location": "hall", "position": "书架前",
                "inventory": ["lamp"],
                "conditions": {"hp": 11, "hp_max": 13, "san": 54, "san_max": 60},
                "status": ["轻伤"],
            },
            "pc_wang": {"name": "王珂", "location": "cellar"},
        },
        "party_locations": {"pc_li": "hall", "pc_wang": "cellar"},
        "party_split": True,
        "revealed_clues": ["clue_1"],
        "pc_known_clues": {"pc_li": ["clue_1"]},
        "npc_state": {"npc_butler": {"location": "hall", "attitude": -1}},
        "clocks": {"clock_flood": {"value": 3, "threshold": 25}},
    }


def test_empty_state_renders_nothing() -> None:
    assert render_state_summary({}) == ""
    assert render_state_summary(None) == ""


def test_summary_contains_expected_layers() -> None:
    text = render_state_summary(_state(), _bundle())

    assert text.startswith("【当前状态】第 7 轮 / 游戏内 23:47")
    assert "位置：门厅（hall） / 一层" in text
    assert "队伍：李默在门厅；王珂在地下室（已分队）" in text
    assert "角色：李默（HP 11/13，SAN 54/60，轻伤）" in text
    assert "背包：提灯" in text
    assert "已知线索：血迹（仅李默）" in text
    assert "在场：管家" in text
    assert "出口：地下室（封锁：血池）" in text
    assert "阁楼" not in text
    assert "全局：血池 3/25" in text
    assert len(text) <= 300


def test_revealed_hidden_exit_appears() -> None:
    state = _state()
    state["revealed"] = [{"id": "edge_hall_attic", "kind": "edge", "turn": 3}]

    text = render_state_summary(state, _bundle())

    assert "阁楼（可达）" in text


def test_summary_respects_max_chars() -> None:
    text = render_state_summary(_state(), _bundle(), max_chars=80)

    assert len(text) <= 80
    assert text.endswith("…")


def test_unrevealed_clues_are_not_rendered() -> None:
    state = _state()
    state["revealed_clues"] = []
    state["pc_known_clues"] = {}

    text = render_state_summary(state, _bundle())

    assert "已知线索" not in text
    assert "血迹" not in text
