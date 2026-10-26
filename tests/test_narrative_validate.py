"""拓扑分档校验与任意顺序模拟的确定性测试（设计文档 §7.6）。"""
from __future__ import annotations

from trpg_server.agents.narrative.model import (
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle


def _node(
    node_id: str,
    kind: str = "location",
    *,
    parent: str | None = None,
    order: int = 0,
    scene_role: str | None = None,
    visibility: str = "public",
    completion: dict | None = None,
    objects: list[dict] | None = None,
) -> NarrativeNode:
    return NarrativeNode(
        id=node_id,
        kind=kind,
        title=node_id,
        parent=parent,
        order=order,
        scene_role="ending" if kind == "ending" else scene_role,
        visibility=visibility,
        completion=completion,
        objects=objects or [],
    )


def _edge(
    from_id: str,
    to_id: str,
    edge_type: str = "adjacent",
    *,
    hard: bool = False,
    bidirectional: bool | None = None,
    condition: dict | None = None,
    visibility: str = "public",
) -> NarrativeEdge:
    return NarrativeEdge(
        id="",
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        hard=hard,
        bidirectional=bidirectional,
        condition=condition,
        visibility=visibility,
    )


def _codes(report_or_issues) -> list[str]:
    issues = report_or_issues.issues if hasattr(report_or_issues, "issues") else report_or_issues
    return [issue.code for issue in issues]


def test_missing_entry_and_ending_are_errors() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[_node("a", order=0), _node("b", order=1)],
        edges=[
            _edge("a", "b", "next", bidirectional=False),
            _edge("b", "a", "next", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "graph.missing_entry" in _codes(report)
    assert "graph.missing_ending" in _codes(report)
    assert report.error_count >= 2


def test_valid_hub_is_ok() -> None:
    bundle = NarrativeBundle(
        topology="hub",
        scenario_id="hub_ok",
        nodes=[
            _node("floor", "area", order=0),
            _node("hall", parent="floor", order=1, scene_role="hub"),
            _node("room_a", parent="floor", order=2, scene_role="room"),
            _node("room_b", parent="floor", order=3, scene_role="room"),
            _node("clue_escape", "clue", parent="room_b", order=4),
            _node("escape", "ending", parent="floor", order=5),
        ],
        edges=[
            _edge("hall", "room_a", "adjacent", bidirectional=True),
            _edge("hall", "room_b", "adjacent", bidirectional=True),
            _edge(
                "hall",
                "escape",
                "leads_to",
                hard=True,
                bidirectional=False,
                condition={"clue": "clue_escape"},
            ),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is True, report.to_dict()
    assert report.error_count == 0
    assert "topology.hub.missing_hub" not in _codes(report)


def test_hidden_without_reveal_when_warns() -> None:
    bundle = NarrativeBundle(
        topology="linear",
        nodes=[
            _node("a", order=0),
            _node("b", order=1),
            _node("secret", "concept", order=2, visibility="hidden"),
            _node("end", "ending", order=3, completion={"clue": "none"}),
        ],
        edges=[
            _edge("a", "b", "next", bidirectional=False),
            _edge("b", "end", "next", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is True
    assert any(
        issue.code == "visibility.hidden_without_reveal" and issue.level == "warn"
        for issue in report.issues
    )


def test_hard_gate_with_missing_prerequisite_is_error() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[
            _node("a", order=0),
            _node("b", order=1),
            _node("end", "ending", order=2),
        ],
        edges=[
            _edge("a", "b", "portal", hard=True, bidirectional=False, condition={"clue": "clue_missing"}),
            _edge("b", "end", "next", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "gate.unsatisfiable" in _codes(report)


def test_self_locked_key_behind_door_is_reported_by_simulation() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        scenario_id="key_behind_door",
        nodes=[
            _node("room_a", order=0),
            _node("room_b", order=1),
            _node("clue_key", "clue", parent="room_b", order=2),
            _node("end", "ending", order=3),
        ],
        edges=[
            _edge(
                "room_a",
                "room_b",
                "portal",
                hard=True,
                bidirectional=False,
                condition={"clue": "clue_key"},
            ),
            _edge("room_b", "end", "next", bidirectional=False),
        ],
    )

    issues = simulate_any_order(bundle)
    codes = _codes(issues)

    assert "order_simulation.self_locked_edge" in codes
    assert all(issue.level == "error" for issue in issues if issue.code.startswith("order_simulation."))
    assert len([issue for issue in issues if issue.code == "order_simulation.self_locked_edge"]) == 1


def test_linear_branch_is_error() -> None:
    bundle = NarrativeBundle(
        topology="linear",
        nodes=[
            _node("a", order=0),
            _node("b", order=1),
            _node("c", order=2),
            _node("end", "ending", order=3),
        ],
        edges=[
            _edge("a", "b", "next", bidirectional=False),
            _edge("a", "c", "next", bidirectional=False),
            _edge("b", "end", "next", bidirectional=False),
            _edge("c", "end", "next", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "topology.linear.branch" in _codes(report)


def test_hybrid_split_components_is_error() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[
            _node("a", order=0),
            _node("b", order=1),
            _node("c", order=2),
            _node("d", order=3),
            _node("end", "ending", order=4),
        ],
        edges=[
            _edge("a", "b", "adjacent", bidirectional=True),
            _edge("c", "d", "adjacent", bidirectional=True),
            _edge("b", "end", "leads_to", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "topology.hybrid.split_components" in _codes(report)


def test_kp_only_node_in_public_exits_is_error() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[
            _node("hall", order=0),
            _node("secret", order=1, visibility="kp_only"),
            _node("end", "ending", order=2),
        ],
        edges=[
            _edge("hall", "secret", "adjacent", bidirectional=True),
            _edge("hall", "end", "leads_to", bidirectional=False),
        ],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "visibility.kp_only_in_public_exits" in _codes(report)


def test_report_truncates_and_appends_info() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[_node("a", kind="location", order=index) for index in range(10)],
        edges=[],
    )

    report = validate_bundle(bundle, max_issues=2)

    assert len(report.issues) == 3
    assert report.issues[-1].code == "validation.truncated"
    assert report.issues[-1].level == "info"


def test_object_interaction_referencing_missing_clue_is_error() -> None:
    bundle = NarrativeBundle(
        topology="hybrid",
        nodes=[
            _node(
                "room",
                order=0,
                objects=[
                    {
                        "id": "obj_desk",
                        "interactions": [
                            {"id": "int_search", "outcomes": {"success": {"clue": "clue_missing"}}},
                        ],
                    }
                ],
            ),
            _node("end", "ending", order=1),
        ],
        edges=[_edge("room", "end", "leads_to", bidirectional=False)],
    )

    report = validate_bundle(bundle)

    assert report.ok is False
    assert "object.missing_clue" in _codes(report)
