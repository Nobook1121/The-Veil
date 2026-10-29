from __future__ import annotations

from trpg_server.agents.narrative.model import (
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)
from trpg_server.agents.narrative.repair import repair_bundle
from trpg_server.agents.narrative.validate import validate_bundle


def _node(
    node_id: str,
    kind: str = "location",
    *,
    parent: str | None = None,
    visibility: str = "public",
    layout: dict | None = None,
) -> NarrativeNode:
    return NarrativeNode(
        id=node_id,
        kind=kind,
        title=node_id,
        parent=parent,
        visibility=visibility,
        layout=layout or {},
    )


def _edge(
    from_id: str,
    to_id: str,
    edge_type: str = "adjacent",
    *,
    edge_id: str = "",
    bidirectional: bool = True,
) -> NarrativeEdge:
    return NarrativeEdge(
        id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        bidirectional=bidirectional,
    )


def _codes(report) -> list[str]:
    return [action.code for action in report.applied]


def test_repair_drops_duplicate_nodes_and_dangling_edges() -> None:
    bundle = NarrativeBundle(
        nodes=[_node("room"), _node("room"), _node("hall")],
        edges=[
            _edge("room", "hall", edge_id="ok"),
            _edge("room", "ghost", edge_id="dangling"),
            _edge("room", "hall", edge_id="duplicate"),
        ],
    )
    report = repair_bundle(bundle)

    assert [node.id for node in bundle.nodes] == ["room", "hall"]
    assert {edge.id for edge in bundle.edges} == {"ok"}
    assert "node.duplicate_id" in _codes(report)
    assert "edge.missing_endpoint" in _codes(report)
    assert "edge.duplicate" in _codes(report)


def test_repair_breaks_parent_cycle_and_missing_parent() -> None:
    bundle = NarrativeBundle(
        nodes=[_node("a", parent="b"), _node("b", parent="a"), _node("c", parent="nope")],
        edges=[],
    )
    report = repair_bundle(bundle)

    parents = {node.id: node.parent for node in bundle.nodes}
    assert parents["c"] is None
    assert parents["a"] is None or parents["b"] is None
    assert "tree.parent_missing" in _codes(report)
    assert "tree.parent_cycle" in _codes(report)


def test_repair_sets_start_to_entry_location_when_missing() -> None:
    bundle = NarrativeBundle(nodes=[_node("porch"), _node("room")], edges=[])
    report = repair_bundle(bundle)

    assert bundle.start["location_id"] in {"porch", "room"}
    assert "graph.missing_entry" in _codes(report)


def test_repair_replaces_start_pointing_at_missing_node() -> None:
    bundle = NarrativeBundle(nodes=[_node("room")], edges=[], start={"location_id": "gone"})
    report = repair_bundle(bundle)

    assert bundle.start["location_id"] == "room"
    assert "start.unknown_location" in _codes(report)


def test_repair_connects_isolated_and_unreachable_locations() -> None:
    bundle = NarrativeBundle(
        nodes=[
            _node("start", layout={"x": 0, "y": 0}),
            _node("hall", layout={"x": 100, "y": 0}),
            _node("attic", layout={"x": 500, "y": 0}),
            _node("cellar", layout={"x": 600, "y": 0}),
        ],
        edges=[_edge("start", "hall"), _edge("attic", "cellar")],
        start={"location_id": "start"},
    )
    report = repair_bundle(bundle)

    codes = _codes(report)
    assert "location.unreachable" in codes
    assert "location.isolated" not in codes

    reachable_issue = [issue for issue in validate_bundle(bundle).issues if issue.code == "location.unreachable"]
    assert reachable_issue == []


def test_repair_connects_isolated_location() -> None:
    bundle = NarrativeBundle(
        nodes=[
            _node("start", layout={"x": 0, "y": 0}),
            _node("hall", layout={"x": 100, "y": 0}),
            _node("attic", layout={"x": 500, "y": 0}),
        ],
        edges=[_edge("start", "hall")],
        start={"location_id": "start"},
    )
    report = repair_bundle(bundle)

    assert "location.isolated" in _codes(report)
    assert any(edge.from_id == "attic" or edge.to_id == "attic" for edge in bundle.edges)


def test_repair_connects_unreachable_ending() -> None:
    bundle = NarrativeBundle(
        nodes=[
            _node("start", layout={"x": 0, "y": 0}),
            _node("hall", layout={"x": 100, "y": 0}),
            _node("finale", kind="ending", layout={"x": 200, "y": 0}),
        ],
        edges=[_edge("start", "hall")],
        start={"location_id": "start"},
    )
    report = repair_bundle(bundle)

    assert "graph.unreachable_ending" in _codes(report)
    assert any(edge.to_id == "finale" for edge in bundle.edges)


def test_repair_keeps_visibility_decisions_for_author() -> None:
    review = _node("hidden", visibility="hidden")
    review.extra["needs_review"] = True
    bundle = NarrativeBundle(
        nodes=[_node("start"), review],
        edges=[_edge("start", "hidden")],
        start={"location_id": "start"},
    )
    report = repair_bundle(bundle)

    assert review.visibility == "hidden"
    assert any(issue["code"] == "visibility.needs_review" for issue in report.remaining)


def test_repair_creates_entry_when_every_location_has_incoming_edge() -> None:
    bundle = NarrativeBundle(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("a", "b", edge_id="ab"), _edge("b", "a", edge_id="ba")],
        start={"location_id": "a"},
    )
    report = repair_bundle(bundle)

    assert "graph.missing_entry" in _codes(report)
    assert any(
        issue.code == "graph.missing_entry" for issue in validate_bundle(bundle).issues
    ) is False


def test_repair_is_idempotent() -> None:
    bundle = NarrativeBundle(
        nodes=[_node("start"), _node("hall"), _node("attic")],
        edges=[_edge("start", "hall"), _edge("hall", "ghost")],
        start={"location_id": "start"},
    )
    repair_bundle(bundle)
    second = repair_bundle(bundle)

    assert second.applied == []
