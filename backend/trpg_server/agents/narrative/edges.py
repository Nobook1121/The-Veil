"""确定性结构边构建（设计文档 §7.4、§7.4.1、§4.2）。

三条硬规则：

- `order` 只用于展示，**绝不**因为同父节点 order 相邻就生成 `next`；
- `adjacent` 来自正文里的明确移动语义，双向且 hard；
- 条件门控只有在能结构化为 §4.2.1 叶子时才生成 `portal`，否则退化为
  soft 的 `leads_to` 并留下 warning，不编造条件。

`contains` 只表达定位树层级，不参与走位。
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.model import (
    TREE_KINDS,
    VISIBILITY_HIDDEN,
    VISIBILITY_PUBLIC,
    NarrativeEdge,
    NarrativeNode,
    slugify_id,
)
from trpg_server.agents.narrative.visibility import normalize_visibility, visibility_rank

_ADJACENT_RE = re.compile(
    r"相邻|相连|通往|通向|走廊|楼梯|门口|大门|门|光幕|出口|入口|回到|通道|甲板|舷梯|电梯|台阶|走进|推开|穿过"
)
_GATE_RE = re.compile(
    r"需要|必须|只有|才能|钥匙|密码|令牌|邀请函|暗门|密道|单程|离开后不可返回|无法返回|门在身后关上|条件"
)
_ONE_WAY_RE = re.compile(r"单程|离开后不可返回|无法返回|门在身后关上|坠落|下车后|上车后|离去")
_NEXT_RE = re.compile(
    r"下一场景|下一幕|下一个地点|随后|紧接着|接着|之后|结束后|天亮后|列车开动|列车启动"
)
_ENDING_CONDITION_RE = re.compile(
    r"达成条件|结局条件|完成条件|触发条件|满足.{0,12}(?:时|则)|当.{0,24}(?:时|则)|若.{0,24}(?:则|就)"
)
_GENERIC_GATE_ITEMS = {"钥匙", "密码", "令牌", "邀请函", "钥匙卡", "门", "锁"}


def infer_edge_type(text: str) -> tuple[str, bool]:
    """从一段正文推断移动边类型。

    返回 `(type, hard)`；没有明确移动语义时返回 `("mentions", False)`。
    `portal` 只代表「正文提到条件门控」，是否真的带 `condition` 由
    `build_structural_edges` 决定。
    """
    value = str(text or "")
    if not value.strip():
        return "mentions", False
    if _ONE_WAY_RE.search(value):
        return "leads_to", True
    if _GATE_RE.search(value):
        return "portal", True
    if _NEXT_RE.search(value):
        return "next", True
    if _ADJACENT_RE.search(value):
        return "adjacent", True
    return "mentions", False


def _field(section: Any, *names: str, default: Any = None) -> Any:
    if isinstance(section, Mapping):
        for name in names:
            value = section.get(name)
            if value not in (None, ""):
                return value
    else:
        for name in names:
            value = getattr(section, name, None)
            if value not in (None, ""):
                return value
    return default


def _section_texts(
    sections: list | None,
    node_ids: set[str],
    node_id_by_title: dict[str, str],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for section in sections or []:
        node_id = _field(section, "node_id", "node", "location_id", "location")
        title = _field(section, "title", "name")
        text = _field(section, "text", "body", "content", default="")
        chapter_path = _field(section, "chapter_path")
        resolved: str | None = None
        if node_id is not None and str(node_id) in node_ids:
            resolved = str(node_id)
        elif title is not None and str(title) in node_id_by_title:
            resolved = node_id_by_title[str(title)]
        elif isinstance(chapter_path, (list, tuple)) and chapter_path:
            last = str(chapter_path[-1])
            resolved = node_id_by_title.get(last)
        if resolved and text:
            result.setdefault(resolved, []).append(str(text))
    return result


def _texts_by_node(
    nodes: list[NarrativeNode],
    text_by_node: dict[str, str] | None,
    sections: list | None,
) -> dict[str, str]:
    node_ids = {node.id for node in nodes}
    node_id_by_title: dict[str, str] = {}
    for node in nodes:
        if node.title and node.title not in node_id_by_title:
            node_id_by_title[node.title.strip()] = node.id
        node_id_by_title.setdefault(node.id, node.id)

    extra = _section_texts(sections, node_ids, node_id_by_title)
    texts: dict[str, str] = {}
    for node in nodes:
        parts: list[str] = []
        if text_by_node and node.id in text_by_node:
            parts.append(str(text_by_node[node.id]))
        parts.extend([node.body or "", node.summary or ""])
        parts.extend(extra.get(node.id, []))
        texts[node.id] = "\n".join(part for part in parts if part)
    return texts


def _target_name(text: str, target: NarrativeNode) -> str | None:
    names: list[str] = []
    if target.title:
        names.append(target.title.strip())
    names.extend(alias.strip() for alias in target.aliases if alias)
    if target.id:
        names.append(target.id)
    for name in names:
        if len(name) >= 2 and name in text:
            return name
    return None


def _mentioned_target(
    text: str,
    others: Iterable[NarrativeNode],
) -> tuple[NarrativeNode, str] | None:
    """在一段正文里找最早提到的目标节点；只取一个，避免凭空分叉。"""
    best: tuple[int, NarrativeNode, str] | None = None
    for node in others:
        name = _target_name(text, node)
        if name is None:
            continue
        position = text.find(name)
        if best is None or position < best[0]:
            best = (position, node, name)
    if best is None:
        return None
    return best[1], best[2]


def _structured_condition(text: str, nodes: list[NarrativeNode]) -> dict[str, Any] | None:
    """只有正文明确点名了一个具体实体时才结构化，否则宁可不生成条件。"""
    for node in nodes:
        if node.kind not in {"item", "clue", "flag"}:
            continue
        title = (node.title or "").strip()
        if len(title) < 2 or title in _GENERIC_GATE_ITEMS or title not in text:
            continue
        if node.kind == "item":
            return {"item": node.id}
        if node.kind == "clue":
            return {"clue": node.id}
        return {"flag": node.id}
    return None


def _make_edge(
    from_id: str,
    to_id: str,
    edge_type: str,
    *,
    hard: bool,
    confidence: float,
    source: str,
    condition: dict[str, Any] | None = None,
    bidirectional: bool | None = None,
    evidence: dict[str, Any] | None = None,
    visibility: str = VISIBILITY_PUBLIC,
) -> NarrativeEdge:
    return NarrativeEdge(
        id=slugify_id(f"edge_{from_id}__{to_id}__{edge_type}", prefix="edge"),
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        hard=hard,
        bidirectional=bidirectional,
        condition=condition,
        evidence=evidence,
        confidence=confidence,
        source=source,
        visibility=visibility,
    )


def build_structural_edges(
    nodes: list[NarrativeNode],
    *,
    sections: list | None = None,
    text_by_node: dict[str, str] | None = None,
) -> list[NarrativeEdge]:
    """从定位树与正文证据构建 contains / adjacent / portal / next / leads_to。"""
    node_list = list(nodes or [])
    node_map = {node.id: node for node in node_list}
    tree_nodes = [node for node in node_list if node.kind in TREE_KINDS]
    locations_by_parent: dict[str | None, list[NarrativeNode]] = {}
    for node in tree_nodes:
        if node.kind == "location":
            locations_by_parent.setdefault(node.parent, []).append(node)
    texts = _texts_by_node(node_list, text_by_node, sections)

    contains: list[NarrativeEdge] = []

    # 1) 容器边：只表达定位树层级，不参与走位。
    for node in tree_nodes:
        parent = node_map.get(node.parent or "")
        if parent is None or parent.kind not in TREE_KINDS:
            continue
        contains.append(
            _make_edge(
                parent.id,
                node.id,
                "contains",
                hard=True,
                confidence=1.0,
                source="inferred",
                bidirectional=True,
            )
        )

    reserved_pairs: set[frozenset[str]] = set()
    gate_edges: list[NarrativeEdge] = []
    next_edges: list[NarrativeEdge] = []
    adjacent_edges: list[NarrativeEdge] = []

    # 2) 条件门控 / 单向边；先建，避免同一对节点再被 adjacent 覆盖。
    for index, first in enumerate(tree_nodes):
        for second in tree_nodes[index + 1:]:
            pair = frozenset((first.id, second.id))
            first_text = texts.get(first.id, "")
            second_text = texts.get(second.id, "")

            source_node: NarrativeNode | None = None
            target_node: NarrativeNode | None = None
            target_name: str | None = None
            if _GATE_RE.search(first_text):
                target_name = _target_name(first_text, second)
                if target_name:
                    source_node, target_node = first, second
            if source_node is None and _GATE_RE.search(second_text):
                target_name = _target_name(second_text, first)
                if target_name:
                    source_node, target_node = second, first
            if source_node is None and len(tree_nodes) == 2:
                # 只有两个定位节点时，条件句的分段本身已足以指向唯一目标。
                if _GATE_RE.search(first_text):
                    source_node, target_node = first, second
                elif _GATE_RE.search(second_text):
                    source_node, target_node = second, first
                if source_node is not None and target_node is not None:
                    target_name = target_node.title or target_node.id
            if source_node is None or target_node is None:
                continue

            source_text = texts.get(source_node.id, "")
            condition = _structured_condition(source_text, node_list)
            one_way = bool(_ONE_WAY_RE.search(source_text))
            evidence: dict[str, Any] = {"matched": target_name}
            if one_way:
                edge_type = "leads_to"
                hard = True
                confidence = 0.9
                edge_source = "explicit"
                evidence["one_way"] = True
                if condition is None:
                    evidence["warning"] = "one_way_without_condition"
            elif condition is not None:
                edge_type = "portal"
                hard = True
                confidence = 0.95
                edge_source = "explicit"
            else:
                edge_type = "leads_to"
                hard = False
                confidence = 0.55
                edge_source = "inferred"
                evidence["warning"] = "condition_unstructured"

            gate_edges.append(
                _make_edge(
                    source_node.id,
                    target_node.id,
                    edge_type,
                    hard=hard,
                    confidence=confidence,
                    source=edge_source,
                    condition=condition,
                    bidirectional=False,
                    evidence=evidence,
                )
            )
            reserved_pairs.add(pair)

    # 3) 顺序边：只有正文显式写出顺序词并点名目标时才生成。
    for source_node in tree_nodes:
        text = texts.get(source_node.id, "")
        phrase = _NEXT_RE.search(text)
        if phrase is None:
            continue
        mentioned = _mentioned_target(
            text,
            (node for node in tree_nodes if node.id != source_node.id),
        )
        if mentioned is None and len(tree_nodes) == 2:
            # 只有两个定位节点时，显式顺序词指向唯一目标。
            other = next(node for node in tree_nodes if node.id != source_node.id)
            mentioned = (other, other.title or other.id)
        if mentioned is None:
            continue
        target_node, target_name = mentioned
        pair = frozenset((source_node.id, target_node.id))
        if pair in reserved_pairs:
            continue
        next_edges.append(
            _make_edge(
                source_node.id,
                target_node.id,
                "next",
                hard=True,
                confidence=0.92,
                source="explicit",
                bidirectional=False,
                evidence={"matched": target_name, "sequence_phrase": phrase.group(0)},
            )
        )
        reserved_pairs.add(pair)

    # 4) 双向 adjacent：同父级 + 正文移动语义 + 点名目标。
    for index, first in enumerate(tree_nodes):
        for second in tree_nodes[index + 1:]:
            if first.kind != "location" or second.kind != "location":
                continue
            if first.parent != second.parent:
                continue
            pair = frozenset((first.id, second.id))
            if pair in reserved_pairs:
                continue
            first_text = texts.get(first.id, "")
            second_text = texts.get(second.id, "")
            matched: str | None = None
            if _ADJACENT_RE.search(first_text):
                matched = _target_name(first_text, second)
            if matched is None and _ADJACENT_RE.search(second_text):
                matched = _target_name(second_text, first)
            siblings = locations_by_parent.get(first.parent, [])
            if (
                matched is None
                and len(siblings) == 2
                and (_ADJACENT_RE.search(first_text) or _ADJACENT_RE.search(second_text))
            ):
                # 同层只有两个房间时，移动语义已经足够确定唯一邻居。
                matched = second.title or second.id
            if matched is None:
                continue
            adjacent_edges.append(
                _make_edge(
                    first.id,
                    second.id,
                    "adjacent",
                    hard=True,
                    confidence=0.92,
                    source="explicit",
                    bidirectional=True,
                    evidence={"matched": matched},
                )
            )

    result = contains + adjacent_edges + gate_edges + next_edges

    # 5) 指向 ending 的移动边：正文没有明确达成条件时收紧为 hidden + soft。
    for edge in result:
        if edge.type == "contains":
            continue
        target = node_map.get(edge.to_id)
        if target is None or target.kind != "ending":
            continue
        if target.completion or _ENDING_CONDITION_RE.search(
            "\n".join(
                part for part in (texts.get(edge.from_id, ""), texts.get(edge.to_id, "")) if part
            )
        ):
            continue
        edge.visibility = VISIBILITY_HIDDEN
        edge.hard = False
        edge.confidence = min(edge.confidence, 0.6)
        evidence = dict(edge.evidence or {})
        evidence["warning"] = "ending_without_condition"
        edge.evidence = evidence

    return result


def _merge_evidence(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not current:
        return dict(incoming) if incoming else None
    if not incoming:
        return dict(current)
    merged = dict(current)
    for key, value in incoming.items():
        if key not in merged:
            merged[key] = value
    if "warning" in merged and current.get("warning") and incoming.get("warning") != current.get("warning"):
        warnings = merged["warning"]
        merged["warning"] = sorted({str(warnings), str(incoming["warning"])})
    return merged


def merge_edges(
    existing: list[NarrativeEdge],
    incoming: list[NarrativeEdge],
) -> list[NarrativeEdge]:
    """按 `(from,to,type)` 去重；已有 hard/explicit 优先，重复时提升 hard。"""
    merged: dict[tuple[str, str, str], NarrativeEdge] = {}
    order: list[tuple[str, str, str]] = []

    for edge in list(existing or []) + list(incoming or []):
        key = (edge.from_id, edge.to_id, edge.type)
        if key not in merged:
            merged[key] = replace(edge)
            order.append(key)
            continue

        keep = merged[key]
        if not keep.hard and edge.hard:
            keep.hard = True
            keep.condition = keep.condition or edge.condition
            keep.confidence = max(keep.confidence, edge.confidence, 0.9)
        if keep.source != "explicit" and edge.source == "explicit":
            keep.source = "explicit"
            keep.confidence = max(keep.confidence, edge.confidence)
        if not keep.label and edge.label:
            keep.label = edge.label
        keep.condition = keep.condition or edge.condition
        keep.visibility = (
            keep.visibility
            if visibility_rank(keep.visibility) >= visibility_rank(edge.visibility)
            else normalize_visibility(edge.visibility)
        )
        keep.evidence = _merge_evidence(keep.evidence, edge.evidence)
        if keep.hard:
            keep.confidence = max(keep.confidence, 0.9)

    return [merged[key] for key in order]
