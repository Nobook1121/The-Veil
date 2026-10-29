"""图谱一键修复：只做结构性、确定性的修复，不臆造剧情内容。

修复范围对应 ``validate.py`` 的通用包检查：

- 重复节点 id、端点不存在或重复的边；
- 悬空或成环的 ``parent``；
- 开场起点指向不存在 / 非地点的节点，以及没有任何入口地点；
- 没有任何移动边的孤立地点、从起点不可达的公开地点；
- 不可达的结局节点（补一条 ``leads_to``）。

可见性复核、触发器绑定、结局条件（``gate.unsatisfiable``）等涉及剧情判断的
问题不自动修改，保留在 ``remaining`` 里交给作者或站点 Agent 处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.model import (
    VISIBILITY_PUBLIC,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
    normalize_start,
    normalize_visibility,
    slugify_id,
)
from trpg_server.agents.narrative.validate import MOVEMENT_EDGE_TYPES, validate_bundle

_START_KINDS = frozenset({"location", "ending"})


@dataclass
class RepairAction:
    code: str
    action: str
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "action": self.action, "target": self.target}


@dataclass
class RepairReport:
    applied: list[RepairAction] = field(default_factory=list)
    remaining: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.get("level") == "error" for issue in self.remaining)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": [action.to_dict() for action in self.applied],
            "applied_count": len(self.applied),
            "remaining_count": len(self.remaining),
            "remaining": self.remaining,
        }


def _position(node: NarrativeNode) -> tuple[float, float] | None:
    layout = node.layout if isinstance(node.layout, Mapping) else {}
    try:
        return float(layout.get("x")), float(layout.get("y"))
    except (TypeError, ValueError):
        return None


def _distance(left: NarrativeNode, right: NarrativeNode) -> float:
    left_pos = _position(left)
    right_pos = _position(right)
    if left_pos is None or right_pos is None:
        return float("inf")
    return (left_pos[0] - right_pos[0]) ** 2 + (left_pos[1] - right_pos[1]) ** 2


def _nearest(source: NarrativeNode, candidates: Iterable[NarrativeNode]) -> NarrativeNode | None:
    best: NarrativeNode | None = None
    best_distance = float("inf")
    for candidate in candidates:
        distance = _distance(source, candidate)
        if distance < best_distance:
            best = candidate
            best_distance = distance
    if best is not None:
        return best
    return next(iter(candidates), None)


def _reachable(start: str | None, bundle: NarrativeBundle) -> set[str]:
    """与校验 ``_player_reachable`` 一致：只走 public 移动边，两个方向都算通。"""
    if not start:
        return set()
    public_nodes = {
        node.id for node in bundle.nodes
        if normalize_visibility(node.visibility) == VISIBILITY_PUBLIC
    }
    if start not in public_nodes:
        return set()
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in public_nodes}
    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if normalize_visibility(edge.visibility) != VISIBILITY_PUBLIC:
            continue
        if edge.from_id not in public_nodes or edge.to_id not in public_nodes:
            continue
        adjacency[edge.from_id].add(edge.to_id)
        adjacency[edge.to_id].add(edge.from_id)
    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for target in adjacency.get(current, ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _cycle_node(node_map: Mapping[str, NarrativeNode]) -> str | None:
    """返回一个处于 parent 环里的最小 id，没有环返回 None。"""
    for node_id in sorted(node_map):
        path: list[str] = [node_id]
        current = node_map[node_id]
        while current.parent and current.parent in node_map:
            if current.parent in path:
                return min(path[path.index(current.parent):])
            path.append(current.parent)
            current = node_map[current.parent]
    return None


def _add_edge(
    bundle: NarrativeBundle,
    used_ids: set[str],
    from_id: str,
    to_id: str,
    edge_type: str,
    *,
    bidirectional: bool,
) -> NarrativeEdge:
    base = slugify_id(f"edge_{from_id}__{to_id}__{edge_type}", prefix="edge")
    edge_id = base
    suffix = 1
    while edge_id in used_ids:
        suffix += 1
        edge_id = f"{base}_{suffix}"
    used_ids.add(edge_id)
    edge = NarrativeEdge(
        id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        bidirectional=bidirectional,
        source="repair",
    )
    bundle.edges.append(edge)
    return edge


def repair_bundle(bundle: NarrativeBundle) -> RepairReport:
    """就地修复图谱结构问题；返回修复动作与修复后仍存在的问题。"""
    report = RepairReport()

    # 1. 重复节点 id：保留第一份。
    seen: set[str] = set()
    kept: list[NarrativeNode] = []
    for node in bundle.nodes:
        if node.id in seen:
            report.applied.append(RepairAction("node.duplicate_id", "删除重复节点", node.id))
            continue
        seen.add(node.id)
        kept.append(node)
    bundle.nodes = kept
    node_map = {node.id: node for node in bundle.nodes}

    # 2. 端点缺失 / 重复的边。
    edges: list[NarrativeEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for edge in bundle.edges:
        if edge.from_id not in node_map or edge.to_id not in node_map:
            report.applied.append(RepairAction("edge.missing_endpoint", "删除端点不存在的边", edge.id))
            continue
        key = (edge.from_id, edge.to_id, edge.type)
        if key in edge_keys:
            report.applied.append(RepairAction("edge.duplicate", "删除重复边", edge.id))
            continue
        edge_keys.add(key)
        edges.append(edge)
    bundle.edges = edges

    # 3. 悬空 parent 与 parent 环。
    for node in bundle.nodes:
        if node.parent and (node.parent not in node_map or node.parent == node.id):
            report.applied.append(RepairAction("tree.parent_missing", "清空无效 parent", node.id))
            node.parent = None
    while True:
        cycle_id = _cycle_node(node_map)
        if cycle_id is None:
            break
        node_map[cycle_id].parent = None
        report.applied.append(RepairAction("tree.parent_cycle", "断开 parent 环", cycle_id))

    locations = [node for node in bundle.nodes if node.kind == "location"]
    start = normalize_start(bundle.start if isinstance(bundle.start, Mapping) else {})
    start_id = str(start.get("location_id") or "").strip() or None
    start_node = node_map.get(start_id) if start_id else None
    start_changed = False

    # 4. 开场起点无效：重置为入口地点。
    if start_id and (start_node is None or start_node.kind not in _START_KINDS):
        code = "start.unknown_location" if start_node is None else "start.not_location"
        report.applied.append(RepairAction(code, "重置开场起点", start_id))
        start_id = None
        start_node = None
        start_changed = True

    def _entry_location() -> NarrativeNode | None:
        incoming: dict[str, set[str]] = {node.id: set() for node in locations}
        for edge in bundle.edges:
            if edge.type not in MOVEMENT_EDGE_TYPES:
                continue
            if edge.to_id in incoming and edge.from_id != edge.to_id:
                incoming[edge.to_id].add(edge.from_id)
        for node in locations:
            if not incoming.get(node.id):
                return node
        return locations[0] if locations else None

    if start_id is None:
        chosen = _entry_location()
        if chosen is not None:
            start_id = chosen.id
            start_node = chosen
            start["location_id"] = chosen.id
            start_changed = True
            report.applied.append(RepairAction("graph.missing_entry", "设置开场起点", chosen.id))
    if start_changed:
        bundle.start = start

    # 5. 孤立地点：接到度数最高的地点。
    incident = {
        value
        for edge in bundle.edges
        if edge.type in MOVEMENT_EDGE_TYPES
        for value in (edge.from_id, edge.to_id)
    }
    degree: dict[str, int] = {}
    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        degree[edge.from_id] = degree.get(edge.from_id, 0) + 1
        degree[edge.to_id] = degree.get(edge.to_id, 0) + 1
    used_edge_ids = {edge.id for edge in bundle.edges}
    for node in locations:
        if node.id in incident:
            continue
        others = [item for item in locations if item.id != node.id]
        hub = max(others, key=lambda item: (degree.get(item.id, 0), item.id), default=None)
        if hub is None:
            continue
        _add_edge(bundle, used_edge_ids, node.id, hub.id, "adjacent", bidirectional=True)
        incident.update({node.id, hub.id})
        degree[hub.id] = degree.get(hub.id, 0) + 1
        report.applied.append(RepairAction("location.isolated", "接入相邻地点", node.id))

    # 6. 从起点不可达的公开地点：接到最近的可达地点。
    reachable = _reachable(start_id, bundle)
    for node in locations:
        if node.id in reachable:
            continue
        if normalize_visibility(node.visibility) != VISIBILITY_PUBLIC:
            continue
        anchors = [item for item in locations if item.id in reachable and item.id != node.id]
        anchor = _nearest(node, anchors)
        if anchor is None:
            continue
        _add_edge(bundle, used_edge_ids, anchor.id, node.id, "adjacent", bidirectional=True)
        reachable = _reachable(start_id, bundle)
        report.applied.append(RepairAction("location.unreachable", "接回可达子图", node.id))

    # 7. 不可达的结局：从最近的可达地点补一条 leads_to。
    reachable = _reachable(start_id, bundle)
    for ending in bundle.nodes_of("ending"):
        if ending.id in reachable:
            continue
        anchors = [item for item in locations if item.id in reachable]
        anchor = _nearest(ending, anchors)
        if anchor is None:
            continue
        _add_edge(bundle, used_edge_ids, anchor.id, ending.id, "leads_to", bidirectional=False)
        reachable = _reachable(start_id, bundle)
        report.applied.append(RepairAction("graph.unreachable_ending", "补充通往结局的边", ending.id))

    # 8. 没有任何入口地点（每个地点都有入边）：把一条入边改成从起点单向出去。
    #    只改方向、不删边，玩家可达性（按双向计算）不受影响。
    if locations:
        incoming = {node.id: [] for node in locations}
        for edge in bundle.edges:
            if edge.type not in MOVEMENT_EDGE_TYPES or edge.from_id == edge.to_id:
                continue
            if edge.to_id in incoming:
                incoming[edge.to_id].append(edge)
        if all(incoming[node.id] for node in locations):
            anchor = start_node if start_node is not None and start_node.kind == "location" else locations[0]
            candidates = sorted(incoming[anchor.id], key=lambda edge: (edge.from_id, edge.id))
            if candidates:
                edge = candidates[0]
                edge.from_id, edge.to_id = anchor.id, edge.from_id
                edge.bidirectional = False
                report.applied.append(RepairAction("graph.missing_entry", "把入边改为起点单向出口", anchor.id))

    # 9. 修复后再跑一次校验，剩下的交给作者。
    after = validate_bundle(bundle)
    report.remaining = [issue.to_dict() for issue in after.issues]
    return report
