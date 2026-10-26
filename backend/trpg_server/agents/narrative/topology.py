"""拓扑形态判定（设计文档 §3.4、§7.4、§7.4.1）。

只统计定位树节点（`act/area/location/ending`）与移动边
（`adjacent/portal/next/leads_to`），不调用模型、不读正文。

判定顺序：
1. 作者显式指定（`explicit` 或 `topology_source="author"`）直接生效；
2. `linear`：唯一入口 + 单链（允许旁支资产，不允许 location 分叉）；
3. `hub`：一个 location 连接 ≥60% 的其他 location，且以双向 adjacent 为主；
4. `sandbox`：≥4 个 location、多入口或没有支配性枢纽、且边密度高/多环；
5. 其余为 `hybrid`。

`order` 只用于展示，任何分支都不会把它当作线性顺序的证据。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re

from trpg_server.agents.narrative.model import (
    TOPOLOGIES,
    TREE_KINDS,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)

MOVEMENT_EDGE_TYPES = frozenset({"adjacent", "portal", "next", "leads_to"})

_LOCATION = "location"
_ENDING = "ending"
_ENTRY_TITLE_RE = re.compile(r"入口|大门|门口|起点|起始点|登陆点|登船口|出发点")


@dataclass
class TopologyDecision:
    topology: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    source: str = "auto"

    def to_dict(self) -> dict[str, object]:
        return {
            "topology": self.topology,
            "topology_confidence": self.confidence,
            "topology_evidence": list(self.evidence),
            "topology_source": self.source,
        }


def _normalize_explicit(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in TOPOLOGIES else None


def _confidence(value: float) -> float:
    """自动判定永远不给 1.0；只有作者显式覆盖才是 1.0。"""
    return round(min(0.95, max(0.5, value)), 2)


def _movement_edges(edges: list[NarrativeEdge], tree_ids: set[str]) -> list[NarrativeEdge]:
    result: list[NarrativeEdge] = []
    for edge in edges or []:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if edge.from_id not in tree_ids or edge.to_id not in tree_ids:
            continue
        if edge.from_id == edge.to_id:
            continue
        result.append(edge)
    return result


def _component_count(node_ids: set[str], undirected: dict[str, set[str]]) -> int:
    seen: set[str] = set()
    count = 0
    for start in node_ids:
        if start in seen:
            continue
        count += 1
        seen.add(start)
        stack = [start]
        while stack:
            current = stack.pop()
            for target in undirected.get(current, ()):
                if target in node_ids and target not in seen:
                    seen.add(target)
                    stack.append(target)
    return count


def _entry_ids(
    locations: list[NarrativeNode],
    incoming: dict[str, set[str]],
    start_id: str | None,
) -> list[str]:
    """入口 = 没有被任何移动边指向的 location；标题显式标记的入口也计入。"""
    entries = [node.id for node in locations if not incoming.get(node.id)]
    seen = set(entries)
    for node in locations:
        if node.id in seen:
            continue
        if _ENTRY_TITLE_RE.search(f"{node.title} {node.summary}"):
            entries.append(node.id)
            seen.add(node.id)
    if start_id and start_id in {node.id for node in locations} and start_id not in seen:
        entries.append(start_id)
    return entries


def _reach(start: str, out: dict[str, set[str]]) -> tuple[set[str], dict[str, str | None]]:
    seen = {start}
    parent: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in out.get(current, ()):
            if target in seen:
                continue
            seen.add(target)
            parent[target] = current
            queue.append(target)
    return seen, parent


def _collect(
    nodes: list[NarrativeNode],
    edges: list[NarrativeEdge],
    *,
    start_id: str | None = None,
) -> dict[str, object]:
    tree_ids = {node.id for node in nodes if node.kind in TREE_KINDS}
    locations = [node for node in nodes if node.kind == _LOCATION]
    location_ids = {node.id for node in locations}
    movement = _movement_edges(edges, tree_ids)

    out: dict[str, set[str]] = {node.id: set() for node in nodes}
    incoming: dict[str, set[str]] = {node.id: set() for node in nodes}
    undirected: dict[str, set[str]] = {node.id: set() for node in nodes}
    for edge in movement:
        out[edge.from_id].add(edge.to_id)
        incoming[edge.to_id].add(edge.from_id)
        undirected[edge.from_id].add(edge.to_id)
        undirected[edge.to_id].add(edge.from_id)
        if edge.bidirectional:
            out[edge.to_id].add(edge.from_id)

    location_pairs: set[tuple[str, str]] = set()
    for edge in movement:
        if edge.from_id in location_ids and edge.to_id in location_ids:
            pair = (edge.from_id, edge.to_id)
            location_pairs.add(pair if pair[0] < pair[1] else (pair[1], pair[0]))

    degree = {node_id: 0 for node_id in location_ids}
    for first, second in location_pairs:
        degree[first] += 1
        degree[second] += 1

    components = _component_count(location_ids, undirected)
    entries = _entry_ids(locations, incoming, start_id)

    next_location_edges = [
        edge for edge in movement
        if edge.type == "next" and edge.from_id in location_ids and edge.to_id in location_ids
    ]

    return {
        "locations": locations,
        "location_ids": location_ids,
        "movement": movement,
        "out": out,
        "incoming": incoming,
        "undirected": undirected,
        "location_pairs": location_pairs,
        "degree": degree,
        "components": components,
        "entries": entries,
        "next_location_edges": next_location_edges,
    }


def _linear_evaluation(metrics: dict[str, object]) -> tuple[bool, int, dict[str, str | None]]:
    locations: list[NarrativeNode] = metrics["locations"]  # type: ignore[assignment]
    location_ids: set[str] = metrics["location_ids"]  # type: ignore[assignment]
    entries: list[str] = metrics["entries"]  # type: ignore[assignment]
    pairs: set[tuple[str, str]] = metrics["location_pairs"]  # type: ignore[assignment]
    degree: dict[str, int] = metrics["degree"]  # type: ignore[assignment]
    out: dict[str, set[str]] = metrics["out"]  # type: ignore[assignment]
    count = len(locations)

    reachable, parent = _reach(entries[0], out) if len(entries) == 1 else (set(), {})
    ok_count = 0
    if len(entries) == 1:
        for node in locations:
            previous = parent.get(node.id)
            targets = set(out.get(node.id, ()))
            if previous:
                targets.discard(previous)
            if len(targets) <= 1:
                ok_count += 1

    chain_ok = (
        count >= 1
        and len(entries) == 1
        and metrics["components"] == 1
        and max(degree.values(), default=0) <= 2
        and len(pairs) == max(0, count - 1)
        and location_ids <= reachable
        and ok_count == count
    )
    return chain_ok, ok_count, parent


def _hub_evaluation(metrics: dict[str, object]) -> tuple[str | None, float, float, int, float]:
    locations: list[NarrativeNode] = metrics["locations"]  # type: ignore[assignment]
    location_ids: set[str] = metrics["location_ids"]  # type: ignore[assignment]
    undirected: dict[str, set[str]] = metrics["undirected"]  # type: ignore[assignment]
    movement: list[NarrativeEdge] = metrics["movement"]  # type: ignore[assignment]
    count = len(locations)
    if count < 3:
        return None, 0.0, 0.0, 0, 0.0

    hub_id: str | None = None
    hub_ratio = 0.0
    hub_neighbors = 0
    for node in locations:
        neighbors = set(undirected.get(node.id, ())) & location_ids
        neighbors.discard(node.id)
        ratio = len(neighbors) / max(1, count - 1)
        if ratio > hub_ratio:
            hub_id = node.id
            hub_ratio = ratio
            hub_neighbors = len(neighbors)

    incident = [
        edge for edge in movement
        if edge.from_id == hub_id or edge.to_id == hub_id
    ]
    adjacent_share = (
        sum(1 for edge in incident if edge.type == "adjacent") / max(1, len(incident))
        if incident else 0.0
    )
    next_count = len(metrics["next_location_edges"])  # type: ignore[arg-type]
    return hub_id, hub_ratio, adjacent_share, hub_neighbors, float(next_count)


def detect_topology(
    nodes: list[NarrativeNode] | NarrativeBundle,
    edges: list[NarrativeEdge] | None = None,
    *,
    explicit: str | None = None,
) -> TopologyDecision:
    """识别剧本拓扑；`explicit` 与作者覆盖优先于任何结构统计。"""
    bundle: NarrativeBundle | None = nodes if isinstance(nodes, NarrativeBundle) else None
    if bundle is not None:
        node_list = list(bundle.nodes)
        edge_list = list(bundle.edges if edges is None else edges)
    else:
        node_list = list(nodes or [])
        edge_list = list(edges or [])

    explicit_value = _normalize_explicit(explicit)
    if explicit_value is None and bundle is not None and bundle.topology_source == "author":
        explicit_value = _normalize_explicit(bundle.topology)
    if explicit_value:
        return TopologyDecision(
            topology=explicit_value,
            confidence=1.0,
            evidence=[f"author_override:{explicit_value}"],
            source="author",
        )

    start_id = None
    if bundle is not None and isinstance(bundle.meta, dict):
        candidate = bundle.meta.get("start") or bundle.meta.get("start_location")
        if isinstance(candidate, str) and candidate:
            start_id = candidate

    metrics = _collect(node_list, edge_list, start_id=start_id)
    locations: list[NarrativeNode] = metrics["locations"]  # type: ignore[assignment]
    movement: list[NarrativeEdge] = metrics["movement"]  # type: ignore[assignment]
    entries: list[str] = metrics["entries"]  # type: ignore[assignment]
    pairs: set[tuple[str, str]] = metrics["location_pairs"]  # type: ignore[assignment]
    count = len(locations)

    evidence = [
        f"locations={count}",
        f"movement_edges={len(movement)}",
        f"entry_points={len(entries)}",
    ]
    if count == 0:
        return TopologyDecision(
            topology="hybrid",
            confidence=0.5,
            evidence=evidence + ["no_location_nodes"],
            source="auto",
        )

    chain_ok, ok_count, _ = _linear_evaluation(metrics)
    if chain_ok:
        confidence = 0.7 + 0.05 * min(count, 4)
        return TopologyDecision(
            topology="linear",
            confidence=_confidence(confidence),
            evidence=evidence + [
                "out_degree<=1: {}/{}".format(ok_count, count),
                "chain_edges={}".format(len(pairs)),
            ],
            source="auto",
        )

    hub_id, hub_ratio, adjacent_share, hub_neighbors, next_count = _hub_evaluation(metrics)
    if (
        hub_id is not None
        and hub_ratio >= 0.6
        and adjacent_share >= 0.5
        and next_count == 0
    ):
        ratio_progress = (hub_ratio - 0.6) / 0.4 if hub_ratio < 1.0 else 1.0
        confidence = 0.62 + 0.28 * max(0.0, ratio_progress) + (0.03 if adjacent_share >= 1.0 else 0.0)
        return TopologyDecision(
            topology="hub",
            confidence=_confidence(confidence),
            evidence=evidence + [
                "hub_ratio={:.2f}".format(hub_ratio),
                "hub={}".format(hub_id),
                "hub_neighbors={}".format(hub_neighbors),
                "adjacent_share={:.2f}".format(adjacent_share),
                "next_edges=0",
            ],
            source="auto",
        )

    components = int(metrics["components"])  # type: ignore[arg-type]
    edge_count = len(pairs)
    density = edge_count / max(1, count * (count - 1) // 2)
    cycle_rank = edge_count - count + components
    hub_dominant = hub_ratio >= 0.6
    high_density = cycle_rank >= 1 or density >= 0.4

    if count >= 4 and (len(entries) >= 2 or not hub_dominant) and high_density:
        confidence = 0.58 + 0.08 * min(len(entries), 3) + (0.06 if cycle_rank >= 2 else 0.0)
        if density >= 0.5:
            confidence += 0.05
        return TopologyDecision(
            topology="sandbox",
            confidence=_confidence(confidence),
            evidence=evidence + [
                "hub_ratio={:.2f}".format(hub_ratio),
                "cycle_rank={}".format(cycle_rank),
                "density={:.2f}".format(density),
            ],
            source="auto",
        )

    confidence = 0.52 + (0.08 if next_count else 0.0) + (0.05 if len(entries) > 1 else 0.0)
    if any(edge.type == "adjacent" for edge in movement):
        confidence += 0.05
    return TopologyDecision(
        topology="hybrid",
        confidence=_confidence(confidence),
        evidence=evidence + [
            "hub_ratio={:.2f}".format(hub_ratio),
            "next_edges={}".format(next_count),
            "cycle_rank={}".format(cycle_rank),
        ],
        source="auto",
    )
