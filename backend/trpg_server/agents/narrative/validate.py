"""拓扑分档校验与任意顺序模拟（设计文档 §7.6）。

校验分三层：

1. 通用包：入口/结局、可达性、门控来源、树结构、可见性、孤立地点等；
2. 拓扑包：`linear / hub / sandbox / hybrid` 各自的结构约束；
3. 任意顺序模拟：在确定性状态 `(visited, clues, flags, clocks, location)` 上
   枚举房间顺序，检测自锁门、孤立线索与不可达结局。

全部为确定性分析：不调用模型、不访问网络、不把 `kp_notes` 写进报告。
"""
from __future__ import annotations

import hashlib
import itertools
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from trpg_server.agents.narrative.conditions import evaluate_condition
from trpg_server.agents.narrative.model import (
    VISIBILITY_HIDDEN,
    VISIBILITY_KP_ONLY,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)
from trpg_server.agents.narrative.store import (
    NarrativeIndex,
    build_index,
    resolve_start,
)
from trpg_server.agents.narrative.visibility import normalize_visibility
from trpg_server.agents.narrative.triggers import (
    ATTACHMENT_KINDS,
    BINDING_KINDS,
    ONCE_SCOPES,
    TRIGGER_VISIBILITIES,
    normalize_trigger_spec,
)

MOVEMENT_EDGE_TYPES = frozenset({"adjacent", "portal", "next", "leads_to"})
_LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}
_LEAF_KEYS = {
    "clue", "flag", "item", "visited", "visited_object",
    "check", "clock", "attitude", "type",
}
_CLUE_KEYS = ("clue", "clue_id", "reveals", "reveal", "reveal_clue", "add_clue", "grants")
_FLAG_KEYS = ("flag", "flag_id", "set_flag", "grants_flag")
_ITEM_KEYS = ("item", "item_id", "add_item", "grants_item")
_CLOCK_WRAPPER_KEYS = ("clock_add", "clock_set")
_NESTED_KEYS = ("outcomes", "effects", "interactions", "success", "fail", "critical", "fumble", "partial")


@dataclass
class ValidationIssue:
    code: str
    level: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    rule: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "rule": self.rule,
            "suggestion": self.suggestion,
        }


@dataclass
class ValidationReport:
    topology: str
    issues: list[ValidationIssue]
    error_count: int
    warning_count: int

    @property
    def ok(self) -> bool:
        return self.error_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "ok": self.ok,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": sum(1 for issue in self.issues if issue.level == "info"),
            "issues": [issue.to_dict() for issue in self.issues],
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _id_list(value: Any) -> list[str]:
    """把 str / list / {id: ...} 归一化成 id 列表。"""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float)):
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_id_list(item))
        return result
    if isinstance(value, Mapping):
        for key in ("id", "clue", "clue_id", "flag", "flag_id", "item", "item_id", "clock", "name", "key"):
            if key in value:
                return _id_list(value.get(key))
    return []


def _truthy_condition(condition: Any) -> bool:
    return condition not in (None, "", {}, [])


def _movement_edges(bundle: NarrativeBundle, node_ids: set[str]) -> list[NarrativeEdge]:
    return [
        edge for edge in bundle.edges
        if edge.type in MOVEMENT_EDGE_TYPES
        and edge.from_id in node_ids
        and edge.to_id in node_ids
    ]


def _node_map(bundle: NarrativeBundle) -> dict[str, NarrativeNode]:
    result: dict[str, NarrativeNode] = {}
    for node in bundle.nodes:
        result.setdefault(node.id, node)
    return result


def _condition_atoms_required(condition: Any) -> Iterator[Mapping[str, Any]]:
    """展开必须满足的叶子；`not` 分支不要求获得任何东西。"""
    if not isinstance(condition, Mapping):
        return
    for key in ("all", "any"):
        for item in _as_list(condition.get(key)):
            yield from _condition_atoms_required(item)
    if _LEAF_KEYS & set(condition.keys()) or "error" in condition:
        yield dict(condition)


def _state(
    visited: set[str],
    clues: set[str],
    flags: Mapping[str, Any],
    items: set[str],
    clocks: Mapping[str, float],
    skills: set[str],
) -> dict[str, Any]:
    return {
        "revealed_clues": sorted(clues),
        "flags": dict(flags),
        "items": sorted(items),
        "visit_log": sorted(visited),
        "clocks": {clock_id: {"value": value} for clock_id, value in clocks.items()},
        "check_results": {skill: {"result": "success"} for skill in skills},
        "checks": [{"skill": skill, "result": "success"} for skill in skills],
    }


# ---------------------------------------------------------------------------
# 资源发现：线索/物品/flag/时钟由哪里提供
# ---------------------------------------------------------------------------


@dataclass
class _Resources:
    clues: dict[str, set[str]] = field(default_factory=dict)
    flags: dict[str, set[str]] = field(default_factory=dict)
    items: dict[str, set[str]] = field(default_factory=dict)
    clock_deltas: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    clue_providers: dict[str, set[str]] = field(default_factory=dict)
    flag_providers: dict[str, set[str]] = field(default_factory=dict)
    item_providers: dict[str, set[str]] = field(default_factory=dict)
    clock_providers: dict[str, set[str]] = field(default_factory=dict)
    clues_defined: set[str] = field(default_factory=set)
    flags_defined: set[str] = field(default_factory=set)
    items_defined: set[str] = field(default_factory=set)
    clocks_defined: set[str] = field(default_factory=set)
    interaction_ids: set[str] = field(default_factory=set)
    object_ids: set[str] = field(default_factory=set)


def _asset_ids(bundle: NarrativeBundle, kinds: set[str]) -> set[str]:
    result: set[str] = set()
    for item in bundle.assets:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if kinds and kind not in kinds:
            continue
        for value in _id_list(item.get("id") or item.get("name")):
            result.add(value)
    return result


def _clue_refs(value: Any, depth: int = 0) -> list[str]:
    """收集交互/结果/效果中引用到的线索 id。"""
    if depth > 5:
        return []
    result: list[str] = []
    if isinstance(value, Mapping):
        for key in _CLUE_KEYS:
            if key in value:
                result.extend(_id_list(value.get(key)))
        effect_name = str(value.get("effect") or "").strip().lower()
        if effect_name in {"add_clue", "reveal_clue"}:
            result.extend(_id_list(value.get("id") or value.get("clue")))
        for key in _NESTED_KEYS:
            child = value.get(key)
            if isinstance(child, (Mapping, list, tuple)):
                result.extend(_clue_refs(child, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(_clue_refs(item, depth + 1))
    deduped: list[str] = []
    for clue in result:
        if clue not in deduped:
            deduped.append(clue)
    return deduped


def _collect_resources(bundle: NarrativeBundle) -> _Resources:
    node_map = _node_map(bundle)
    location_ids = {node.id for node in bundle.nodes if node.kind == "location"}
    resources = _Resources()
    for location_id in location_ids:
        resources.clues[location_id] = set()
        resources.flags[location_id] = set()
        resources.items[location_id] = set()
        resources.clock_deltas[location_id] = []
    resources.clocks_defined = set(_asset_ids(bundle, {"clock"}))
    resources.flags_defined = set(_asset_ids(bundle, {"flag"}))
    resources.items_defined = set(_asset_ids(bundle, {"item"}))
    resources.clues_defined = set(_asset_ids(bundle, {"clue"}))

    for item in bundle.clocks:
        if isinstance(item, Mapping):
            resources.clocks_defined.update(_id_list(item.get("id") or item.get("name")))
    for item in bundle.flags:
        if isinstance(item, Mapping):
            resources.flags_defined.update(_id_list(item.get("id") or item.get("name") or item.get("key")))
    for node in bundle.nodes:
        if node.kind == "clue":
            resources.clues_defined.add(node.id)
        elif node.kind == "item":
            resources.items_defined.add(node.id)
        elif node.kind == "flag":
            resources.flags_defined.add(node.id)
        elif node.kind == "clock":
            resources.clocks_defined.add(node.id)

    def add_clue(location_id: str, clue_id: str) -> None:
        # 只有真实存在的线索节点/资产才算「可获得」，避免把无效引用当成来源。
        if clue_id not in resources.clues_defined:
            return
        resources.clues.setdefault(location_id, set()).add(clue_id)
        resources.clue_providers.setdefault(clue_id, set()).add(location_id)

    def add_flag(location_id: str, flag_id: str) -> None:
        resources.flags_defined.add(flag_id)
        resources.flags.setdefault(location_id, set()).add(flag_id)
        resources.flag_providers.setdefault(flag_id, set()).add(location_id)

    def add_item(location_id: str, item_id: str) -> None:
        resources.items_defined.add(item_id)
        resources.items.setdefault(location_id, set()).add(item_id)
        resources.item_providers.setdefault(item_id, set()).add(location_id)

    def add_clock(location_id: str, clock_id: str, delta: float) -> None:
        resources.clocks_defined.add(clock_id)
        resources.clock_deltas.setdefault(location_id, []).append((clock_id, delta))
        resources.clock_providers.setdefault(clock_id, set()).add(location_id)

    def walk(value: Any, location_id: str, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, location_id, depth + 1)
            return
        if not isinstance(value, Mapping):
            return
        for key in _CLUE_KEYS:
            for clue_id in _id_list(value.get(key)):
                add_clue(location_id, clue_id)
        for key in _FLAG_KEYS:
            for flag_id in _id_list(value.get(key)):
                add_flag(location_id, flag_id)
        for key in _ITEM_KEYS:
            for item_id in _id_list(value.get(key)):
                add_item(location_id, item_id)
        for key in _CLOCK_WRAPPER_KEYS:
            if key not in value:
                continue
            raw = value.get(key)
            if isinstance(raw, Mapping):
                clock_ids = _id_list(raw.get("id") or raw.get("clock"))
                delta = raw.get("value", raw.get("delta", 0))
            else:
                clock_ids = _id_list(raw)
                delta = 0
            for clock_id in clock_ids:
                try:
                    numeric = float(delta)
                except (TypeError, ValueError):
                    numeric = 0.0
                add_clock(location_id, clock_id, numeric)
        effect_name = str(value.get("effect") or "").strip().lower()
        if effect_name in {"add_clue", "reveal_clue"}:
            for clue_id in _id_list(value.get("id") or value.get("clue")):
                add_clue(location_id, clue_id)
        elif effect_name == "set_flag":
            for flag_id in _id_list(value.get("id") or value.get("flag")):
                add_flag(location_id, flag_id)
        elif effect_name == "add_item":
            for item_id in _id_list(value.get("id") or value.get("item")):
                add_item(location_id, item_id)
        elif effect_name in {"clock_add", "clock_set"}:
            clock_ids = _id_list(value.get("id") or value.get("clock"))
            try:
                numeric = float(value.get("value", value.get("delta", 0)) or 0)
            except (TypeError, ValueError):
                numeric = 0.0
            for clock_id in clock_ids:
                add_clock(location_id, clock_id, numeric)
        for key in _NESTED_KEYS:
            child = value.get(key)
            if isinstance(child, (Mapping, list, tuple)):
                walk(child, location_id, depth + 1)

    for node in bundle.nodes:
        for obj in node.objects:
            if isinstance(obj, Mapping):
                resources.object_ids.update(_id_list(obj.get("id")))
                for interaction in _as_list(obj.get("interactions")):
                    if isinstance(interaction, Mapping):
                        resources.interaction_ids.update(_id_list(interaction.get("id")))
        if node.kind == "location":
            walk([node.on_enter, node.on_reenter], node.id)

    for node in bundle.nodes:
        if node.kind == "clue" and node.parent in location_ids:
            add_clue(node.parent, node.id)
        elif node.kind == "item" and node.parent in location_ids:
            add_item(node.parent, node.id)
        if node.kind != "location":
            continue
        for obj in node.objects:
            walk(obj, node.id)

    for edge in bundle.edges:
        if edge.type != "reveals":
            continue
        target = node_map.get(edge.to_id)
        if target is None or target.kind != "clue":
            continue
        source = node_map.get(edge.from_id)
        location_id: str | None = None
        if source is not None and source.kind == "location":
            location_id = source.id
        elif source is not None and source.parent in location_ids:
            location_id = source.parent
        if location_id:
            add_clue(location_id, target.id)

    return resources


# ---------------------------------------------------------------------------
# 通用包
# ---------------------------------------------------------------------------


def _check_duplicate_node_ids(
    bundle: NarrativeBundle, issues: list[ValidationIssue],
) -> set[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for node in bundle.nodes:
        if node.id in seen:
            duplicated.add(node.id)
            issues.append(ValidationIssue(
                code="node.duplicate_id",
                level="error",
                message=f"节点 id {node.id} 重复出现，图谱会丢失其中一份。",
                node_id=node.id,
                rule="§7.6 通用包#3",
                suggestion="合并重复节点或改用唯一 id。",
            ))
        else:
            seen.add(node.id)
    return duplicated


def _check_edges(bundle: NarrativeBundle, node_ids: set[str], issues: list[ValidationIssue]) -> None:
    keys: set[tuple[str, str, str]] = set()
    for edge in bundle.edges:
        missing = [value for value in (edge.from_id, edge.to_id) if value not in node_ids]
        if missing:
            issues.append(ValidationIssue(
                code="edge.missing_endpoint",
                level="error",
                message=f"边 {edge.id} 的端点不存在：{', '.join(missing)}。",
                edge_id=edge.id or None,
                rule="§7.6 通用包#3",
                suggestion="修正 from/to，或先补建对应节点。",
            ))
        key = (edge.from_id, edge.to_id, edge.type)
        if key in keys:
            issues.append(ValidationIssue(
                code="edge.duplicate",
                level="warn",
                message=f"边 {edge.id} 与已有边重复（{edge.from_id} → {edge.to_id}, {edge.type}）。",
                edge_id=edge.id or None,
                rule="§7.6 通用包#10",
                suggestion="保留一条，或合并条件后删除重复边。",
            ))
        else:
            keys.add(key)


def _check_parents(
    node_map: Mapping[str, NarrativeNode], issues: list[ValidationIssue],
) -> None:
    for node in node_map.values():
        if node.parent and node.parent not in node_map:
            issues.append(ValidationIssue(
                code="tree.parent_missing",
                level="error",
                message=f"节点 {node.id} 的 parent {node.parent} 不存在。",
                node_id=node.id,
                rule="§7.6 通用包#4",
                suggestion="补建父节点，或清空 parent。",
            ))
    seen_cycles: set[frozenset[str]] = set()
    for node in node_map.values():
        if not node.parent or node.parent not in node_map:
            continue
        path = [node.id]
        current = node
        while current.parent and current.parent in node_map:
            parent = node_map[current.parent]
            if parent.id in path:
                cycle = path[path.index(parent.id):]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    issues.append(ValidationIssue(
                        code="tree.parent_cycle",
                        level="error",
                        message=f"parent 链成环：{' → '.join(cycle + [parent.id])}。",
                        node_id=cycle[0],
                        rule="§7.6 通用包#4",
                        suggestion="断开其中一条 parent 关系。",
                    ))
                break
            path.append(parent.id)
            current = parent


def _check_start(bundle: NarrativeBundle, node_map: dict[str, NarrativeNode], issues: list[ValidationIssue]) -> None:
    """§5.9 开场配置：起始地点与开场附件必须真实存在。"""
    start = bundle.start if isinstance(bundle.start, Mapping) else {}
    location_id = str(start.get("location_id") or "").strip()
    if location_id:
        node = node_map.get(location_id)
        if node is None:
            issues.append(ValidationIssue(
                code="start.unknown_location",
                level="error",
                message=f"开场起始地点 {location_id} 不存在。",
                node_id=location_id,
                rule="§5.9",
                suggestion="修正 start.location_id，或在剧本中补建该地点节点。",
            ))
        elif node.kind not in {"location", "ending"}:
            issues.append(ValidationIssue(
                code="start.not_location",
                level="warn",
                message=f"开场起始节点 {location_id} 的类型是 {node.kind}，不是地点。",
                node_id=location_id,
                rule="§5.9",
                suggestion="起始节点应指向 location 或 ending 节点。",
            ))
    asset_ids = {
        str(asset.get("id") or "").strip()
        for asset in bundle.assets
        if isinstance(asset, Mapping) and str(asset.get("id") or "").strip()
    }
    for asset_id in start.get("opening_attachments") or []:
        text = str(asset_id or "").strip()
        if text and text not in asset_ids:
            issues.append(ValidationIssue(
                code="start.missing_asset",
                level="warn",
                message=f"开场附件 {text} 不在 assets 中。",
                rule="§5.9",
                suggestion="补充素材，或修正 opening_attachments。",
            ))


def _check_visibility(bundle: NarrativeBundle, issues: list[ValidationIssue]) -> None:
    for node in bundle.nodes:
        if normalize_visibility(node.visibility) == VISIBILITY_HIDDEN and not _truthy_condition(node.reveal_when):
            issues.append(ValidationIssue(
                code="visibility.hidden_without_reveal",
                level="warn",
                message=f"节点 {node.id} 标记为 hidden，但没有 reveal_when。",
                node_id=node.id,
                rule="§7.6 通用包#5",
                suggestion="补充 reveal_when，或改为 public / kp_only。",
            ))
    for edge in bundle.edges:
        if normalize_visibility(edge.visibility) == VISIBILITY_HIDDEN and not _truthy_condition(edge.reveal_when):
            issues.append(ValidationIssue(
                code="visibility.hidden_without_reveal",
                level="warn",
                message=f"边 {edge.id} 标记为 hidden，但没有 reveal_when。",
                edge_id=edge.id or None,
                rule="§7.6 通用包#5",
                suggestion="补充 reveal_when，或改为 public / kp_only。",
            ))


def _check_public_exits(
    bundle: NarrativeBundle,
    index: NarrativeIndex,
    node_map: Mapping[str, NarrativeNode],
    issues: list[ValidationIssue],
) -> None:
    public_edges: dict[tuple[str, str], NarrativeEdge] = {}
    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if normalize_visibility(edge.visibility) != "public":
            continue
        public_edges.setdefault((edge.from_id, edge.to_id), edge)
        if edge.bidirectional:
            public_edges.setdefault((edge.to_id, edge.from_id), edge)

    reported: set[tuple[str, str]] = set()
    for source, targets in index.exits.items():
        source_node = node_map.get(source)
        if source_node is not None and normalize_visibility(source_node.visibility) == VISIBILITY_KP_ONLY:
            key = (source, source)
            if key not in reported:
                reported.add(key)
                issues.append(ValidationIssue(
                    code="visibility.kp_only_in_public_exits",
                    level="error",
                    message=f"kp_only 节点 {source} 出现在 public 出口列表中。",
                    node_id=source,
                    rule="§7.6 通用包#6",
                    suggestion="把该节点改为 public/hidden，或不要为它生成 public 出口。",
                ))
        for target in targets:
            target_node = node_map.get(target)
            if target_node is None or normalize_visibility(target_node.visibility) != VISIBILITY_KP_ONLY:
                continue
            key = (source, target)
            if key in reported:
                continue
            reported.add(key)
            edge = public_edges.get((source, target)) or public_edges.get((target, source))
            issues.append(ValidationIssue(
                code="visibility.kp_only_in_public_exits",
                level="error",
                message=f"kp_only 节点 {target} 进入了 {source} 的 public 出口列表，玩家会看到不该存在的地点。",
                node_id=target,
                edge_id=edge.id if edge is not None else None,
                rule="§7.6 通用包#6",
                suggestion="把该出口改为 hidden/kp_only，或把目标地点改为 public。",
            ))

    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if normalize_visibility(edge.visibility) != VISIBILITY_KP_ONLY:
            continue
        leaked = edge.to_id in index.exits.get(edge.from_id, [])
        if edge.bidirectional:
            leaked = leaked or edge.from_id in index.exits.get(edge.to_id, [])
        if not leaked:
            continue
        key = (edge.from_id, edge.to_id)
        if key in reported:
            continue
        reported.add(key)
        issues.append(ValidationIssue(
            code="visibility.kp_only_in_public_exits",
            level="error",
            message=f"kp_only 边 {edge.id} 进入了 public 出口列表。",
            edge_id=edge.id or None,
            rule="§7.6 通用包#6",
            suggestion="把该边改为 public/hidden，或从玩家侧出口中移除。",
        ))


def _check_endings(bundle: NarrativeBundle, node_map: Mapping[str, NarrativeNode], issues: list[ValidationIssue]) -> None:
    inbound: dict[str, list[NarrativeEdge]] = {}
    for edge in bundle.edges:
        inbound.setdefault(edge.to_id, []).append(edge)
    for ending in bundle.nodes_of("ending"):
        completion = ending.completion
        own_condition = ending.extra.get("condition") if isinstance(ending.extra, Mapping) else None
        has_condition = bool(_truthy_condition(own_condition) or _truthy_condition(completion))
        if not has_condition:
            for edge in inbound.get(ending.id, []):
                if _truthy_condition(edge.condition):
                    has_condition = True
                    break
        if not has_condition:
            issues.append(ValidationIssue(
                code="ending.missing_completion",
                level="warn",
                message=f"结局 {ending.id} 既没有 completion，也没有带条件的入边。",
                node_id=ending.id,
                rule="§7.6 通用包#7",
                suggestion="补充结局条件（completion 或入边 condition），避免直接、无条件进入结局。",
            ))


def _check_object_clues(
    bundle: NarrativeBundle,
    node_map: Mapping[str, NarrativeNode],
    resources: _Resources,
    issues: list[ValidationIssue],
) -> None:
    for node in bundle.nodes:
        for obj in node.objects:
            if not isinstance(obj, Mapping):
                continue
            object_id = next(iter(_id_list(obj.get("id") or obj.get("name"))), "object")
            for interaction in _as_list(obj.get("interactions")):
                if not isinstance(interaction, Mapping):
                    continue
                interaction_id = next(iter(_id_list(interaction.get("id"))), "interaction")
                for clue_id in _clue_refs(interaction):
                    target = node_map.get(clue_id)
                    if target is not None and target.kind == "clue":
                        continue
                    if clue_id in resources.clues_defined:
                        continue
                    issues.append(ValidationIssue(
                        code="object.missing_clue",
                        level="error",
                        message=(
                            f"节点 {node.id} 的对象 {object_id} 的交互 {interaction_id} "
                            f"引用了不存在的线索 {clue_id}。"
                        ),
                        node_id=node.id,
                        rule="§7.6 通用包#8",
                        suggestion="补建线索节点，或修正 interactions 里的 clue 引用。",
                    ))


def _entry_and_start(
    bundle: NarrativeBundle,
    movement: list[NarrativeEdge],
    location_ids: set[str],
    index: NarrativeIndex,
) -> tuple[list[str], str | None]:
    incoming: dict[str, set[str]] = {location_id: set() for location_id in location_ids}
    for edge in movement:
        if edge.to_id in incoming and edge.from_id != edge.to_id:
            incoming[edge.to_id].add(edge.from_id)
    entries = [node.id for node in bundle.nodes if node.kind == "location" and not incoming.get(node.id)]
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    declared = bundle.start.get("location_id") if isinstance(getattr(bundle, "start", None), Mapping) else None
    meta_start = declared or meta.get("start") or meta.get("start_location")
    meta_start = str(meta_start) if isinstance(meta_start, str) and meta_start in location_ids else None
    if meta_start and meta_start not in entries:
        entries.append(meta_start)
    start = meta_start or (entries[0] if entries else None) or index.start
    return entries, start


def _undirected_reach(start: str | None, bundle: NarrativeBundle, movement: list[NarrativeEdge]) -> set[str]:
    if not start:
        return set()
    adjacency: dict[str, set[str]] = {node.id: set() for node in bundle.nodes}
    for edge in movement:
        adjacency.setdefault(edge.from_id, set()).add(edge.to_id)
        adjacency.setdefault(edge.to_id, set()).add(edge.from_id)
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in adjacency.get(current, ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _check_locations(
    bundle: NarrativeBundle,
    index: NarrativeIndex,
    movement: list[NarrativeEdge],
    entries: list[str],
    issues: list[ValidationIssue],
) -> None:
    incident = {edge.from_id for edge in movement} | {edge.to_id for edge in movement}
    start = index.start
    for node in bundle.nodes_of("location"):
        if node.id not in incident and node.id != start:
            issues.append(ValidationIssue(
                code="location.isolated",
                level="warn",
                message=f"地点 {node.id} 没有任何移动边，是孤立地点。",
                node_id=node.id,
                rule="§7.6 通用包#9",
                suggestion="把它接到同 area 的 hub 或相邻地点；确实可选时标记 optional。",
            ))
        if start and node.id not in index.reachable:
            issues.append(ValidationIssue(
                code="location.unreachable",
                level="error",
                message=f"地点 {node.id} 从起点 {start} 完全不可达。",
                node_id=node.id,
                rule="§7.6 通用包#9",
                suggestion="补充双向 adjacent，或把它并入可达的移动子图。",
            ))
    if not entries:
        issues.append(ValidationIssue(
            code="graph.missing_entry",
            level="error",
            message="剧本没有任何入口地点：所有地点都有入边，且未设置 narrative.start。",
            rule="§7.6 通用包#1",
            suggestion="设置 meta.start，或断开一个地点的入边作为入口。",
        ))


def _check_ending_reachability(
    bundle: NarrativeBundle,
    start: str | None,
    movement: list[NarrativeEdge],
    issues: list[ValidationIssue],
) -> None:
    endings = bundle.nodes_of("ending")
    if not endings:
        issues.append(ValidationIssue(
            code="graph.missing_ending",
            level="error",
            message="剧本没有任何结局节点（kind=ending）。",
            rule="§7.6 通用包#1",
            suggestion="至少补一个结局，并为它提供可满足的进入条件。",
        ))
        return
    reachable = _undirected_reach(start, bundle, movement)
    for ending in endings:
        if start and ending.id not in reachable:
            issues.append(ValidationIssue(
                code="graph.unreachable_ending",
                level="error",
                message=f"结局 {ending.id} 在把 adjacent 视为双向的可达图中不可达。",
                node_id=ending.id,
                rule="§7.6 通用包#1",
                suggestion="补一条从可达地点通往该结局的边，或检查结局条件是否过严。",
            ))


def _atom_satisfiable(
    atom: Mapping[str, Any],
    *,
    node_map: Mapping[str, NarrativeNode],
    resources: _Resources,
    reachable: set[str],
) -> bool:
    if "error" in atom:
        return False
    if "clue" in atom:
        clue_id = str(atom.get("clue") or "").strip()
        node = node_map.get(clue_id)
        known = (node is not None and node.kind == "clue") or clue_id in resources.clues_defined
        if not known:
            return False
        providers = resources.clue_providers.get(clue_id, set())
        return bool(providers & reachable)
    if "item" in atom:
        item_id = str(atom.get("item") or "").strip()
        if item_id not in resources.items_defined:
            return False
        providers = resources.item_providers.get(item_id, set())
        return not providers or bool(providers & reachable)
    if "flag" in atom:
        flag_id = str(atom.get("flag") or "").strip()
        if flag_id in resources.flags_defined:
            return True
        providers = resources.flag_providers.get(flag_id, set())
        return bool(providers & reachable)
    if "clock" in atom:
        clock_id = str(atom.get("clock") or "").strip()
        if clock_id in resources.clocks_defined:
            return True
        providers = resources.clock_providers.get(clock_id, set())
        return bool(providers & reachable)
    if "check" in atom:
        spec = atom.get("check")
        return isinstance(spec, Mapping) and bool(str(spec.get("skill") or "").strip())
    if "visited" in atom:
        target = str(atom.get("visited") or "").strip()
        return target in reachable
    if "visited_object" in atom:
        target = str(atom.get("visited_object") or "").strip()
        return (not target) or target in resources.object_ids
    atom_type = str(atom.get("type") or "").strip().lower()
    if atom_type == "location":
        target = str(atom.get("id") or "").strip()
        return target in reachable
    if atom_type == "interaction_result":
        target = str(atom.get("interaction_id") or "").strip()
        return not target or target in resources.interaction_ids
    if atom_type == "skill":
        return bool(str(atom.get("name") or "").strip())
    if "attitude" in atom:
        return isinstance(atom.get("attitude"), Mapping)
    return False


def _unsatisfiable_atoms(
    condition: Any,
    *,
    node_map: Mapping[str, NarrativeNode],
    resources: _Resources,
    reachable: set[str],
) -> list[Mapping[str, Any]]:
    if not isinstance(condition, Mapping):
        return [{"error": "invalid_condition"}]
    if not condition:
        return []
    problems: list[Mapping[str, Any]] = []
    if "any" in condition:
        children = _as_list(condition.get("any"))
        if children:
            child_results = [
                _unsatisfiable_atoms(child, node_map=node_map, resources=resources, reachable=reachable)
                for child in children
            ]
            if any(not result for result in child_results):
                return []
            return list(min(child_results, key=len))
    for child in _as_list(condition.get("all")):
        problems.extend(
            _unsatisfiable_atoms(child, node_map=node_map, resources=resources, reachable=reachable)
        )
    if "not" in condition:
        return problems
    if _LEAF_KEYS & set(condition.keys()) or "error" in condition:
        if not _atom_satisfiable(condition, node_map=node_map, resources=resources, reachable=reachable):
            problems.append(dict(condition))
        return problems
    if "any" not in condition and "all" not in condition and condition:
        problems.append({"error": "unparsable_condition"})
    return problems


def _atom_label(atom: Mapping[str, Any]) -> str:
    for key in ("clue", "item", "flag", "clock", "visited", "visited_object"):
        if key in atom:
            return f"{key}={atom.get(key)}"
    if "check" in atom:
        spec = atom.get("check")
        return f"检定={dict(spec).get('skill')}" if isinstance(spec, Mapping) else "检定"
    if "error" in atom:
        return "无法解析的条件"
    if "attitude" in atom:
        return "NPC 态度"
    return "条件"


def _check_hard_gates(
    bundle: NarrativeBundle,
    node_map: Mapping[str, NarrativeNode],
    resources: _Resources,
    reachable: set[str],
    issues: list[ValidationIssue],
) -> None:
    for edge in bundle.edges:
        if not edge.hard or not _truthy_condition(edge.condition):
            continue
        problems = _unsatisfiable_atoms(
            edge.condition, node_map=node_map, resources=resources, reachable=reachable,
        )
        if not problems:
            continue
        labels = sorted({_atom_label(atom) for atom in problems})
        issues.append(ValidationIssue(
            code="gate.unsatisfiable",
            level="error",
            message=(
                f"硬门控边 {edge.id} 的解锁来源不存在或不可达：{'、'.join(labels)}。"
            ),
            edge_id=edge.id or None,
            rule="§7.6 通用包#2",
            suggestion="补充可获得的线索/物品/flag/检定/时钟来源，或把该边改为 soft。",
        ))


def _find_next_cycles(bundle: NarrativeBundle, node_ids: set[str]) -> list[list[str]]:
    graph: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in bundle.edges:
        if edge.type == "next" and edge.from_id in node_ids and edge.to_id in node_ids:
            graph[edge.from_id].append(edge.to_id)
    color: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node_id: str) -> None:
        color[node_id] = 1
        stack.append(node_id)
        for target in graph.get(node_id, []):
            if color.get(target, 0) == 0:
                dfs(target)
            elif color.get(target) == 1:
                index = stack.index(target)
                cycles.append(stack[index:])
        stack.pop()
        color[node_id] = 2

    for node_id in sorted(node_ids):
        if color.get(node_id, 0) == 0:
            dfs(node_id)
    deduped: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    for cycle in cycles:
        key = frozenset(cycle)
        if key not in seen:
            seen.add(key)
            deduped.append(cycle)
    return deduped


def _check_next_chains(
    bundle: NarrativeBundle,
    node_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    for cycle in _find_next_cycles(bundle, node_ids):
        if bundle.topology == "linear":
            issues.append(ValidationIssue(
                code="topology.linear.next_cycle",
                level="error",
                message=f"linear 主链的 next 链成环：{' → '.join(cycle + [cycle[0]])}。",
                node_id=cycle[0],
                rule="L2",
                suggestion="断开环路，或显式标记 loop 并改用非 linear 拓扑。",
            ))
        else:
            issues.append(ValidationIssue(
                code="graph.next_cycle",
                level="warn",
                message=f"next 链成环：{' → '.join(cycle + [cycle[0]])}。",
                node_id=cycle[0],
                rule="§7.6 通用包#10",
                suggestion="确认是否真的需要闭环；否则断开一条 next 边。",
            ))


# ---------------------------------------------------------------------------
# 拓扑包
# ---------------------------------------------------------------------------


def _directed_adjacency(movement: list[NarrativeEdge], node_ids: set[str]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in movement:
        adjacency.setdefault(edge.from_id, set()).add(edge.to_id)
        if edge.bidirectional:
            adjacency.setdefault(edge.to_id, set()).add(edge.from_id)
    return adjacency


def _bfs_parents(start: str, adjacency: Mapping[str, set[str]]) -> dict[str, str | None]:
    parent: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in adjacency.get(current, ()):
            if target not in parent:
                parent[target] = current
                queue.append(target)
    return parent


def _check_linear(
    bundle: NarrativeBundle,
    entries: list[str],
    endings: list[NarrativeNode],
    movement: list[NarrativeEdge],
    node_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    if len(entries) != 1:
        issues.append(ValidationIssue(
            code="topology.linear.entry_count",
            level="error",
            message=f"linear 拓扑要求恰好 1 个入口，实际 {len(entries)} 个：{', '.join(entries) or '无'}。",
            rule="L1",
            suggestion="保留一个入口，其余入口改为普通地点或删除入边。",
        ))
    if len(endings) != 1:
        issues.append(ValidationIssue(
            code="topology.linear.ending_count",
            level="error",
            message=f"linear 拓扑要求恰好 1 个结局，实际 {len(endings)} 个：{', '.join(node.id for node in endings) or '无'}。",
            rule="L1",
            suggestion="合并结局，或改用 hybrid/hub 拓扑。",
        ))
    if len(entries) != 1:
        return
    adjacency = _directed_adjacency(movement, node_ids)
    parents = _bfs_parents(entries[0], adjacency)
    for node in bundle.nodes_of("location"):
        targets = set(adjacency.get(node.id, set()))
        previous = parents.get(node.id)
        if previous:
            targets.discard(previous)
        targets.discard(node.id)
        if len(targets) > 1:
            issues.append(ValidationIssue(
                code="topology.linear.branch",
                level="error",
                message=(
                    f"linear 拓扑下地点 {node.id} 有 {len(targets)} 个后继：{'、'.join(sorted(targets))}。"
                ),
                node_id=node.id,
                rule="L1",
                suggestion="只保留一个后继，把支线改为可选 adjacent 或移到 hub/hybrid。",
            ))
        elif not targets:
            issues.append(ValidationIssue(
                code="topology.linear.chain_gap",
                level="error",
                message=f"linear 主链在地点 {node.id} 断开：没有任何后继。",
                node_id=node.id,
                rule="L1",
                suggestion="补一条通往下一地点或结局的 next 边。",
            ))


def _check_hub(
    bundle: NarrativeBundle,
    index: NarrativeIndex,
    movement: list[NarrativeEdge],
    location_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    if len(location_ids) < 2:
        issues.append(ValidationIssue(
            code="topology.hub.missing_hub",
            level="error",
            message="hub 拓扑至少需要 2 个地点才能形成枢纽与房间。",
            rule="H1",
            suggestion="补充房间地点，或把拓扑改为 linear。",
        ))
        return
    undirected: dict[str, set[str]] = {location_id: set() for location_id in location_ids}
    for edge in movement:
        if edge.from_id in location_ids and edge.to_id in location_ids and edge.from_id != edge.to_id:
            undirected[edge.from_id].add(edge.to_id)
            undirected[edge.to_id].add(edge.from_id)
    explicit = [node for node in bundle.nodes_of("location") if node.scene_role == "hub"]
    if explicit:
        hub = max(explicit, key=lambda node: len(undirected.get(node.id, set())))
    else:
        hub = max(
            bundle.nodes_of("location"),
            key=lambda node: (len(undirected.get(node.id, set())), -node.order),
        )
        coverage = len(undirected.get(hub.id, set())) / max(1, len(location_ids) - 1)
        if coverage < 0.5:
            issues.append(ValidationIssue(
                code="topology.hub.missing_hub",
                level="error",
                message=(
                    f"hub 拓扑下找不到连接 ≥50% 房间的枢纽；"
                    f"当前最大连接数是 {len(undirected.get(hub.id, set()))}/{len(location_ids) - 1}。"
                ),
                node_id=hub.id,
                rule="H1",
                suggestion="补建中央 hub 与各房间的双向 adjacent，或改用 sandbox 拓扑。",
            ))
            return
    if index.start and hub.id not in index.reachable:
        issues.append(ValidationIssue(
            code="topology.hub.unreachable_hub",
            level="error",
            message=f"hub {hub.id} 从起点 {index.start} 不可达。",
            node_id=hub.id,
            rule="H2",
            suggestion="从入口补一条通往 hub 的 adjacent，并把起点放在 hub。",
        ))
    for edge in bundle.edges:
        if edge.type != "next":
            continue
        if edge.from_id in location_ids and edge.to_id in location_ids:
            if edge.from_id != hub.id and edge.to_id != hub.id:
                issues.append(ValidationIssue(
                    code="topology.hub.next_between_rooms",
                    level="error",
                    message=(
                        f"hub 拓扑不允许房间之间用 next 串联：{edge.from_id} → {edge.to_id}。"
                    ),
                    edge_id=edge.id or None,
                    rule="H2",
                    suggestion="删除房间之间的 next，改用经 hub 的双向 adjacent。",
                ))


def _check_sandbox(
    bundle: NarrativeBundle,
    location_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    for edge in bundle.edges:
        if edge.type not in {"next", "leads_to"}:
            continue
        if not edge.hard or _truthy_condition(edge.condition):
            continue
        if edge.from_id not in location_ids or edge.to_id not in location_ids:
            continue
        issues.append(ValidationIssue(
            code="topology.sandbox.implicit_chain",
            level="error",
            message=(
                f"sandbox 拓扑出现无条件的硬链：{edge.from_id} → {edge.to_id}（{edge.type}）。"
            ),
            edge_id=edge.id or None,
            rule="H2",
            suggestion="删除隐式链路，或为它补显式 condition 并改为 portal。",
        ))


def _check_hybrid(
    bundle: NarrativeBundle,
    movement: list[NarrativeEdge],
    location_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    undirected: dict[str, set[str]] = {location_id: set() for location_id in location_ids}
    for edge in movement:
        if edge.from_id in location_ids and edge.to_id in location_ids and edge.from_id != edge.to_id:
            undirected.setdefault(edge.from_id, set()).add(edge.to_id)
            undirected.setdefault(edge.to_id, set()).add(edge.from_id)
    seen: set[str] = set()
    components: list[list[str]] = []
    for node in bundle.nodes_of("location"):
        if node.id in seen:
            continue
        component: list[str] = []
        queue = [node.id]
        seen.add(node.id)
        while queue:
            current = queue.pop()
            component.append(current)
            for target in undirected.get(current, ()):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        components.append(component)
    if len(components) > 1:
        preview = "；".join("{" + ", ".join(sorted(component)) + "}" for component in components[:3])
        issues.append(ValidationIssue(
            code="topology.hybrid.split_components",
            level="error",
            message=f"hybrid 移动子图分裂成 {len(components)} 个互不可达组件：{preview}。",
            node_id=components[1][0],
            rule="hybrid",
            suggestion="用双向 adjacent 把各组件接到主链或 hub，保证任意可达组件互通。",
        ))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _truncate(issues: list[ValidationIssue], max_issues: int) -> list[ValidationIssue]:
    limit = max(0, int(max_issues))
    if len(issues) <= limit:
        return issues
    dropped = len(issues) - limit
    return issues[:limit] + [ValidationIssue(
        code="validation.truncated",
        level="info",
        message=f"问题过多，已截断 {dropped} 条；请先修复高优先级问题后重新校验。",
        rule="§7.6",
    )]


def validate_bundle(
    bundle: NarrativeBundle,
    *,
    index: NarrativeIndex | None = None,
    max_issues: int = 200,
) -> ValidationReport:
    """按 §7.6 校验剧本包，返回确定性报告。"""
    index = index or build_index(bundle)
    node_map = _node_map(bundle)
    node_ids = set(node_map)
    issues: list[ValidationIssue] = []

    _check_duplicate_node_ids(bundle, issues)
    _check_edges(bundle, node_ids, issues)
    _check_parents(node_map, issues)
    _check_start(bundle, node_map, issues)
    _check_visibility(bundle, issues)
    _check_public_exits(bundle, index, node_map, issues)
    _check_endings(bundle, node_map, issues)

    movement = _movement_edges(bundle, node_ids)
    location_ids = {node.id for node in bundle.nodes if node.kind == "location"}
    entries, start = _entry_and_start(bundle, movement, location_ids, index)

    resources = _collect_resources(bundle)
    reachable = set(index.reachable)
    _check_object_clues(bundle, node_map, resources, issues)
    _check_locations(bundle, index, movement, entries, issues)
    _check_ending_reachability(bundle, start, movement, issues)
    _check_hard_gates(bundle, node_map, resources, reachable, issues)
    _check_next_chains(bundle, node_ids, issues)
    _check_triggers(bundle, node_map, resources, reachable, issues)

    endings = bundle.nodes_of("ending")
    if bundle.topology == "linear":
        _check_linear(bundle, entries, endings, movement, node_ids, issues)
    elif bundle.topology == "hub":
        _check_hub(bundle, index, movement, location_ids, issues)
    elif bundle.topology == "sandbox":
        _check_sandbox(bundle, location_ids, issues)
    else:
        _check_hybrid(bundle, movement, location_ids, issues)

    issues.sort(key=lambda issue: _LEVEL_ORDER.get(issue.level, 3))
    # 计数在校验报告被截断前完成，`ok` 不会因截断而失真。
    error_count = sum(1 for issue in issues if issue.level == "error")
    warning_count = sum(1 for issue in issues if issue.level == "warn")
    issues = _truncate(issues, max_issues)
    return ValidationReport(
        topology=bundle.topology,
        issues=issues,
        error_count=error_count,
        warning_count=warning_count,
    )


def _check_triggers(
    bundle: NarrativeBundle,
    node_map: Mapping[str, NarrativeNode],
    resources: "_Resources",
    reachable: set[str],
    issues: list[ValidationIssue],
) -> None:
    if not bundle.triggers:
        return
    assets = {
        str(item.get("id")): item
        for item in bundle.assets
        if isinstance(item, Mapping) and item.get("id")
    }
    object_owners: dict[str, str] = {}
    for node in bundle.nodes:
        for obj in node.objects or []:
            if isinstance(obj, Mapping) and str(obj.get("id") or "").strip():
                object_owners.setdefault(str(obj["id"]), node.id)

    seen: set[str] = set()
    for index, raw in enumerate(bundle.triggers):
        if not isinstance(raw, Mapping):
            issues.append(ValidationIssue(
                code="trigger.invalid", level="error",
                message=f"第 {index + 1} 个触发器不是对象",
                rule="trigger",
                suggestion="触发器必须是对象并包含 binding。",
            ))
            continue
        spec = normalize_trigger_spec(raw, assets_by_id=assets, index=index)
        if spec is None:
            issues.append(ValidationIssue(
                code="trigger.invalid_binding", level="error",
                message=f"触发器 {raw.get('id') or index} 的 binding 无效",
                rule="trigger",
                suggestion="binding.kind 必须是 interaction/enter/clock/event/clue/npc/manual。",
            ))
            continue
        trigger_id = spec["id"]
        if trigger_id in seen:
            issues.append(ValidationIssue(
                code="trigger.duplicate_id", level="error",
                message=f"触发器 id 重复：{trigger_id}",
                rule="trigger",
                suggestion="为每个触发器分配唯一 id。",
            ))
        seen.add(trigger_id)
        binding = spec["binding"]
        kind = str(binding.get("kind") or "")
        if kind not in BINDING_KINDS:
            issues.append(ValidationIssue(
                code="trigger.invalid_binding", level="error",
                message=f"触发器 {trigger_id} 的 binding.kind 无效：{kind}",
                rule="trigger",
            ))
            continue
        location_id = str(binding.get("location_id") or "")
        if kind in {"enter", "interaction"} and location_id:
            if location_id not in node_map:
                issues.append(ValidationIssue(
                    code="trigger.unknown_location", level="error",
                    message=f"触发器 {trigger_id} 引用的地点不存在：{location_id}",
                    rule="trigger",
                    suggestion="修正 binding.location_id，或重建世界树。",
                ))
            elif location_id not in reachable:
                issues.append(ValidationIssue(
                    code="trigger.unreachable_location", level="warn",
                    message=f"触发器 {trigger_id} 绑定在不可达地点：{location_id}",
                    rule="trigger",
                ))
        object_id = str(binding.get("object_id") or "")
        if kind == "interaction" and object_id:
            if object_id not in object_owners:
                issues.append(ValidationIssue(
                    code="trigger.unknown_object", level="error",
                    message=f"触发器 {trigger_id} 引用的物件不存在：{object_id}",
                    rule="trigger",
                ))
            elif location_id and object_owners.get(object_id) != location_id:
                issues.append(ValidationIssue(
                    code="trigger.object_location_mismatch", level="warn",
                    message=f"触发器 {trigger_id} 的物件 {object_id} 不在 {location_id}",
                    rule="trigger",
                ))
        if kind == "clue" and binding.get("clue_id") and binding["clue_id"] not in resources.clues_defined:
            issues.append(ValidationIssue(
                code="trigger.unknown_clue", level="error",
                message=f"触发器 {trigger_id} 引用的线索不存在：{binding['clue_id']}",
                rule="trigger",
            ))
        if kind == "npc" and binding.get("npc_id") and binding["npc_id"] not in node_map:
            issues.append(ValidationIssue(
                code="trigger.unknown_npc", level="error",
                message=f"触发器 {trigger_id} 引用的 NPC 不存在：{binding['npc_id']}",
                rule="trigger",
            ))
        if kind == "clock" and binding.get("clock_id") and binding["clock_id"] not in resources.clocks_defined:
            issues.append(ValidationIssue(
                code="trigger.unknown_clock", level="warn",
                message=f"触发器 {trigger_id} 引用的时钟不在剧本包中：{binding['clock_id']}",
                rule="trigger",
            ))
        if kind == "event" and binding.get("event_id") and binding["event_id"] not in resources.flags_defined:
            if binding["event_id"] not in node_map:
                issues.append(ValidationIssue(
                    code="trigger.unknown_event", level="warn",
                    message=f"触发器 {trigger_id} 引用的事件无法在包内确认：{binding['event_id']}",
                    rule="trigger",
                ))
        if kind == "manual" and not spec.get("condition") and not binding.get("keyword"):
            issues.append(ValidationIssue(
                code="trigger.manual_without_condition", level="warn",
                message=f"manual 触发器 {trigger_id} 没有条件或关键词，可能被模型随意请求",
                rule="trigger",
                suggestion="补充 condition 或 keyword。",
            ))
        if spec.get("needs_review"):
            issues.append(ValidationIssue(
                code="trigger.review_needed", level="warn",
                message=f"触发器 {trigger_id} 的旧条件无法结构化，发布前需人工确认",
                rule="trigger",
            ))
        if spec["visibility"] not in TRIGGER_VISIBILITIES:
            issues.append(ValidationIssue(
                code="trigger.invalid_visibility", level="error",
                message=f"触发器 {trigger_id} 的 visibility 无效：{spec['visibility']}",
                rule="trigger",
            ))
        if spec["once_scope"] not in ONCE_SCOPES:
            issues.append(ValidationIssue(
                code="trigger.invalid_once_scope", level="error",
                message=f"触发器 {trigger_id} 的 once_scope 无效：{spec['once_scope']}",
                rule="trigger",
            ))
        seen_attachments: set[str] = set()
        for attachment in spec["attachments"]:
            attachment_id = attachment["id"]
            if attachment_id in seen_attachments:
                issues.append(ValidationIssue(
                    code="trigger.duplicate_attachment", level="warn",
                    message=f"触发器 {trigger_id} 的附件 id 重复：{attachment_id}",
                    rule="trigger",
                ))
            seen_attachments.add(attachment_id)
            if attachment["kind"] not in ATTACHMENT_KINDS:
                issues.append(ValidationIssue(
                    code="trigger.invalid_attachment_kind", level="error",
                    message=f"触发器 {trigger_id} 的附件类型无效：{attachment['kind']}",
                    rule="trigger",
                ))
            asset_id = str(attachment.get("asset_id") or "")
            if not asset_id and not attachment.get("url") and attachment.get("body") is None:
                issues.append(ValidationIssue(
                    code="trigger.missing_asset", level="error",
                    message=f"触发器 {trigger_id} 的附件 {attachment_id} 没有素材或 URL",
                    rule="trigger",
                ))
            elif asset_id and asset_id not in assets and not attachment.get("url"):
                issues.append(ValidationIssue(
                    code="trigger.missing_asset", level="error",
                    message=f"触发器 {trigger_id} 引用的素材不存在：{asset_id}",
                    rule="trigger",
                ))


# ---------------------------------------------------------------------------
# 任意顺序模拟（H6）
# ---------------------------------------------------------------------------


_SIM_ISSUE_LIMIT = 20


@dataclass
class _EvaluatedEdge:
    target: str
    edge: NarrativeEdge


def _movement_adjacency(bundle: NarrativeBundle, node_ids: set[str]) -> dict[str, list[_EvaluatedEdge]]:
    adjacency: dict[str, list[_EvaluatedEdge]] = {node_id: [] for node_id in node_ids}
    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if edge.from_id not in node_ids or edge.to_id not in node_ids:
            continue
        adjacency[edge.from_id].append(_EvaluatedEdge(edge.to_id, edge))
        if edge.bidirectional:
            adjacency[edge.to_id].append(_EvaluatedEdge(edge.from_id, edge))
    return adjacency


def _edge_open(edge: NarrativeEdge, state: Mapping[str, Any]) -> bool:
    if edge.hard and _truthy_condition(edge.condition):
        return evaluate_condition(edge.condition, state).ok
    return True


def _travel_path(
    start: str,
    target: str,
    adjacency: Mapping[str, list[_EvaluatedEdge]],
    state: Mapping[str, Any],
    ending_ids: set[str],
) -> list[str] | None:
    if start == target:
        return [start]
    parent: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for link in adjacency.get(current, ()):
            if link.target in ending_ids or link.target in parent:
                continue
            if not _edge_open(link.edge, state):
                continue
            parent[link.target] = current
            if link.target == target:
                path = [target]
                cursor: str | None = current
                while cursor is not None:
                    path.append(cursor)
                    cursor = parent.get(cursor)
                path.reverse()
                return path
            queue.append(link.target)
    return None


def _ordered_permutations(location_ids: list[str], scenario_id: str) -> Iterator[tuple[str, ...]]:
    if len(location_ids) <= 7:
        yield from itertools.permutations(location_ids)
        return
    seed_material = "|".join([scenario_id, *location_ids])
    seed = int(hashlib.sha1(seed_material.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    base = list(location_ids)
    yield tuple(base)
    for _ in range(200):
        rng.shuffle(base)
        yield tuple(base)


def _condition_self_lock_atoms(
    condition: Any,
    target: str,
    clue_providers: Mapping[str, set[str]],
    flag_providers: Mapping[str, set[str]],
    item_providers: Mapping[str, set[str]],
) -> list[Mapping[str, Any]]:
    if not isinstance(condition, Mapping) or not condition:
        return []
    if "any" in condition:
        children = _as_list(condition.get("any"))
        if not children:
            return []
        results = [
            _condition_self_lock_atoms(child, target, clue_providers, flag_providers, item_providers)
            for child in children
        ]
        if any(not result for result in results):
            return []
        return list(min(results, key=len))
    problems: list[Mapping[str, Any]] = []
    for child in _as_list(condition.get("all")):
        problems.extend(
            _condition_self_lock_atoms(child, target, clue_providers, flag_providers, item_providers)
        )
    if "not" in condition:
        return problems
    for key, providers in (
        ("clue", clue_providers),
        ("flag", flag_providers),
        ("item", item_providers),
    ):
        if key not in condition:
            continue
        value = str(condition.get(key) or "").strip()
        locations = providers.get(value, set())
        if locations and locations <= {target}:
            problems.append(dict(condition))
        break
    return problems


def simulate_any_order(
    bundle: NarrativeBundle,
    *,
    max_steps: int = 5000,
) -> list[ValidationIssue]:
    """对 ≤7 个地点全排列、>7 个地点固定种子采样，检测顺序导致的死锁。"""
    node_map = _node_map(bundle)
    node_ids = set(node_map)
    locations = bundle.nodes_of("location")
    if not locations:
        return []
    endings = bundle.nodes_of("ending")
    location_ids = [node.id for node in locations]
    start = resolve_start(bundle)
    if not start:
        return []

    resources = _collect_resources(bundle)
    clue_providers: dict[str, set[str]] = {}
    for location_id, clues in resources.clues.items():
        for clue_id in clues:
            clue_providers.setdefault(clue_id, set()).add(location_id)
    flag_providers: dict[str, set[str]] = {}
    for location_id, flags in resources.flags.items():
        for flag_id in flags:
            flag_providers.setdefault(flag_id, set()).add(location_id)
    item_providers: dict[str, set[str]] = {}
    for location_id, items in resources.items.items():
        for item_id in items:
            item_providers.setdefault(item_id, set()).add(location_id)

    issues: list[ValidationIssue] = []
    seen_issues: set[tuple[str, str | None, str | None, str]] = set()

    def add(issue: ValidationIssue) -> None:
        if len(issues) >= _SIM_ISSUE_LIMIT:
            return
        key = (issue.code, issue.node_id, issue.edge_id, issue.message)
        if key in seen_issues:
            return
        seen_issues.add(key)
        issues.append(issue)

    # 静态部分一：硬边自锁（前置只能由目标地点自身提供）。
    for edge in bundle.edges:
        if not edge.hard or not _truthy_condition(edge.condition):
            continue
        problems = _condition_self_lock_atoms(
            edge.condition, edge.to_id, clue_providers, flag_providers, item_providers,
        )
        if not problems:
            continue
        labels = "、".join(sorted({_atom_label(atom) for atom in problems}))
        add(ValidationIssue(
            code="order_simulation.self_locked_edge",
            level="error",
            message=(
                f"硬边 {edge.id} 的解锁前置只能由目标地点 {edge.to_id} 自身提供，"
                f"形成自锁：{labels}。"
            ),
            edge_id=edge.id or None,
            rule="H6",
            suggestion="把钥匙/线索放到目标地点之外的房间，或放宽该门控条件。",
        ))

    # 静态部分二：条件引用的线索没有任何地点可获得。
    orphan_clues: set[str] = set()
    for edge in bundle.edges:
        if not _truthy_condition(edge.condition):
            continue
        for atom in _condition_atoms_required(edge.condition):
            if "clue" not in atom:
                continue
            clue_id = str(atom.get("clue") or "").strip()
            if not clue_id:
                continue
            if clue_id in orphan_clues:
                continue
            if clue_providers.get(clue_id):
                continue
            orphan_clues.add(clue_id)
            add(ValidationIssue(
                code="order_simulation.orphan_clue",
                level="error",
                message=f"线索 {clue_id} 只能在不存在的节点获得：没有任何地点或交互会提供它。",
                rule="H6",
                suggestion="补一个可获得该线索的房间/交互，或从门控条件中移除它。",
            ))

    if not endings:
        return issues

    adjacency = _movement_adjacency(bundle, node_ids)
    ending_ids = {ending.id for ending in endings}
    ending_inbound: dict[str, list[NarrativeEdge]] = {}
    for edge in bundle.edges:
        if edge.type in MOVEMENT_EDGE_TYPES and edge.to_id in ending_ids:
            ending_inbound.setdefault(edge.to_id, []).append(edge)

    check_skills: set[str] = set()
    for edge in bundle.edges:
        for atom in _condition_atoms_required(edge.condition):
            spec = atom.get("check")
            if isinstance(spec, Mapping) and str(spec.get("skill") or "").strip():
                check_skills.add(str(spec.get("skill")))
    reached_endings: set[str] = set()
    initial_clocks: dict[str, float] = {}
    for item in bundle.clocks:
        if not isinstance(item, Mapping):
            continue
        clock_id = next(iter(_id_list(item.get("id") or item.get("name"))), "")
        if not clock_id:
            continue
        raw_value = item.get("value", item.get("current", 0))
        try:
            initial_clocks[clock_id] = float(raw_value or 0)
        except (TypeError, ValueError):
            initial_clocks[clock_id] = 0.0

    for permutation in _ordered_permutations(location_ids, bundle.scenario_id):
        budget = max(0, int(max_steps))
        visited: set[str] = set()
        clues: set[str] = set()
        flags: dict[str, Any] = {}
        items: set[str] = set()
        clocks = dict(initial_clocks)
        current = start

        def current_state() -> dict[str, Any]:
            return _state(visited, clues, flags, items, clocks, check_skills)

        def apply_location(location_id: str) -> None:
            if location_id in visited:
                return
            visited.add(location_id)
            clues.update(resources.clues.get(location_id, set()))
            for flag_id in resources.flags.get(location_id, set()):
                flags[flag_id] = True
            items.update(resources.items.get(location_id, set()))
            for clock_id, delta in resources.clock_deltas.get(location_id, []):
                clocks[clock_id] = clocks.get(clock_id, 0.0) + delta

        def mark_endings() -> None:
            state = current_state()
            for ending in endings:
                if ending.id in reached_endings:
                    continue
                own = ending.extra.get("condition") if isinstance(ending.extra, Mapping) else None
                if own and not evaluate_condition(own, state).ok:
                    continue
                for edge in ending_inbound.get(ending.id, []):
                    if edge.from_id not in visited:
                        continue
                    if edge.hard and _truthy_condition(edge.condition):
                        if not evaluate_condition(edge.condition, state).ok:
                            continue
                    reached_endings.add(ending.id)
                    break

        apply_location(start)
        budget -= 1
        mark_endings()
        for target in permutation:
            if budget <= 0:
                break
            if target in visited:
                continue
            path = _travel_path(current, target, adjacency, current_state(), ending_ids)
            if not path:
                continue
            for location_id in path[1:]:
                if budget <= 0:
                    break
                apply_location(location_id)
                current = location_id
                budget -= 1
                mark_endings()

    for ending in endings:
        if ending.id in reached_endings:
            continue
        add(ValidationIssue(
            code="order_simulation.unreachable_ending",
            level="error",
            message=f"任意顺序模拟下结局 {ending.id} 始终不可达。",
            node_id=ending.id,
            rule="H6",
            suggestion="补充从可达房间进入结局的路径，或放宽结局条件。",
        ))

    return issues


__all__ = [
    "ValidationIssue",
    "ValidationReport",
    "simulate_any_order",
    "validate_bundle",
]
