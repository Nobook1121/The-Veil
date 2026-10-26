"""拓扑识别与结构边构建的确定性测试（设计文档 §3.4、§7.4）。"""
from __future__ import annotations

from trpg_server.agents.narrative.edges import (
    build_structural_edges,
    infer_edge_type,
    merge_edges,
)
from trpg_server.agents.narrative.model import NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.topology import detect_topology


def _node(
    node_id: str,
    kind: str = "location",
    *,
    parent: str | None = None,
    order: int = 0,
    title: str | None = None,
    body: str = "",
    completion: dict | None = None,
) -> NarrativeNode:
    return NarrativeNode(
        id=node_id,
        kind=kind,
        title=title or node_id,
        parent=parent,
        order=order,
        body=body,
        completion=completion,
    )


def _edge(
    from_id: str,
    to_id: str,
    edge_type: str = "adjacent",
    *,
    hard: bool = True,
    bidirectional: bool | None = None,
) -> NarrativeEdge:
    return NarrativeEdge(
        id="",
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        hard=hard,
        bidirectional=bidirectional,
    )


def test_pure_chain_is_linear() -> None:
    nodes = [
        _node("a", order=0),
        _node("b", order=1),
        _node("c", order=2),
        _node("ending", "ending", order=3),
    ]
    edges = [
        _edge("a", "b", "next"),
        _edge("b", "c", "next"),
        _edge("c", "ending", "next"),
    ]

    decision = detect_topology(nodes, edges)

    assert decision.topology == "linear"
    assert decision.source == "auto"
    assert 0.5 <= decision.confidence < 1.0
    assert any("out_degree<=1" in item for item in decision.evidence)


def test_hall_with_four_rooms_is_hub() -> None:
    nodes = [_node("hall", parent="floor", order=0)]
    nodes.extend(
        _node(f"room_{index}", parent="floor", order=index + 1)
        for index in range(4)
    )
    edges = [
        _edge("hall", f"room_{index}", "adjacent", bidirectional=True)
        for index in range(4)
    ]

    decision = detect_topology(nodes, edges)

    assert decision.topology == "hub"
    assert any(item.startswith("hub_ratio=") for item in decision.evidence)
    assert 0.5 <= decision.confidence < 1.0


def test_multi_entrance_cyclic_map_is_sandbox() -> None:
    nodes = [
        _node("east_gate", title="东入口", parent="map"),
        _node("west_gate", title="西入口", parent="map"),
        _node("room_b", parent="map"),
        _node("room_c", parent="map"),
        _node("room_e", parent="map"),
        _node("room_f", parent="map"),
        _node("room_g", parent="map"),
    ]
    edges = [
        _edge("east_gate", "room_b", bidirectional=False),
        _edge("west_gate", "room_e", bidirectional=False),
        _edge("room_b", "room_c", bidirectional=False),
        _edge("room_c", "room_b", bidirectional=False),
        _edge("room_e", "room_f", bidirectional=False),
        _edge("room_f", "room_e", bidirectional=False),
        _edge("room_b", "room_e", bidirectional=False),
        _edge("room_c", "room_g", bidirectional=False),
        _edge("room_g", "room_f", bidirectional=False),
    ]

    decision = detect_topology(nodes, edges)

    assert decision.topology == "sandbox"
    assert "entry_points=2" in decision.evidence
    assert any(item.startswith("cycle_rank=") for item in decision.evidence)


def test_order_alone_never_builds_next_edges() -> None:
    nodes = [
        _node("area", "area", order=0),
        _node("room_1", parent="area", order=1, body="空房间。"),
        _node("room_2", parent="area", order=2, body="安静的房间。"),
        _node("room_3", parent="area", order=3, body="灰尘落满地面。"),
    ]

    edges = build_structural_edges(nodes)

    assert [edge for edge in edges if edge.type == "next"] == []
    assert len(edges) == 3
    assert all(edge.type == "contains" and edge.hard for edge in edges)


def test_gate_phrase_builds_one_way_edge_without_fabricated_condition() -> None:
    nodes = [
        _node("archive", parent="area", title="档案室", body="通往B房间的门需要钥匙才能打开。"),
        _node("room_b", parent="area", title="B房间", body="里面很暗。"),
    ]

    edges = build_structural_edges(nodes)
    gate_edges = [
        edge for edge in edges
        if {edge.from_id, edge.to_id} == {"archive", "room_b"}
        and edge.type in {"portal", "leads_to"}
    ]

    assert infer_edge_type("需要钥匙才能打开")[0] == "portal"
    assert len(gate_edges) == 1
    gate = gate_edges[0]
    assert gate.type == "leads_to"
    assert gate.bidirectional is False
    assert gate.hard is False
    assert gate.condition is None
    assert gate.evidence is not None
    assert gate.evidence["warning"] == "condition_unstructured"
    assert [edge.type for edge in edges if {edge.from_id, edge.to_id} == {"archive", "room_b"}] == [
        "leads_to"
    ]


def test_explicit_topology_overrides_automatic_detection() -> None:
    nodes = [_node("hall", parent="floor")]
    nodes.extend(_node(f"room_{index}", parent="floor") for index in range(4))
    edges = [
        _edge("hall", f"room_{index}", bidirectional=True) for index in range(4)
    ]

    assert detect_topology(nodes, edges).topology == "hub"

    decision = detect_topology(nodes, edges, explicit="linear")

    assert decision.topology == "linear"
    assert decision.source == "author"
    assert decision.confidence == 1.0


def test_merge_edges_dedupes_and_promotes_hard() -> None:
    existing = [
        NarrativeEdge(
            id="",
            from_id="a",
            to_id="b",
            type="adjacent",
            hard=True,
            source="explicit",
        ),
        NarrativeEdge(
            id="",
            from_id="c",
            to_id="d",
            type="leads_to",
            hard=False,
            bidirectional=False,
            source="inferred",
        ),
    ]
    incoming = [
        NarrativeEdge(id="", from_id="a", to_id="b", type="adjacent", hard=False),
        NarrativeEdge(
            id="",
            from_id="c",
            to_id="d",
            type="leads_to",
            hard=True,
            bidirectional=False,
            source="explicit",
        ),
        NarrativeEdge(id="", from_id="a", to_id="b", type="adjacent", hard=False),
    ]

    merged = merge_edges(existing, incoming)
    by_key = {(edge.from_id, edge.to_id, edge.type): edge for edge in merged}

    assert len(merged) == 2
    assert by_key[("a", "b", "adjacent")].hard is True
    assert by_key[("c", "d", "leads_to")].hard is True
    assert existing[1].hard is False
