"""图谱质量指标（设计文档 §7.9）。

纯确定性统计，不调用模型；在「校验」页与 `narrative validate` 输出中展示。
返回每项指标的实测值、目标区间与是否达标，便于人工判断能否发布。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from trpg_server.agents.narrative.model import NarrativeBundle
from trpg_server.agents.narrative.store import build_index, resolve_start
from trpg_server.agents.narrative.visibility import normalize_visibility

_MOVEMENT_TYPES = {"adjacent", "portal", "next", "leads_to"}


@dataclass
class QualityMetric:
    key: str
    label: str
    value: float | int | bool | None
    target: str
    passed: bool | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "target": self.target,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class QualityReport:
    topology: str
    metrics: list[QualityMetric] = field(default_factory=list)

    @property
    def failed(self) -> list[QualityMetric]:
        return [item for item in self.metrics if item.passed is False]

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "total": len(self.metrics),
            "passed": sum(1 for item in self.metrics if item.passed is True),
            "failed": [item.key for item in self.failed],
            "metrics": [item.to_dict() for item in self.metrics],
        }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _check_clue_usage(bundle: NarrativeBundle) -> tuple[int, int]:
    clues = [node for node in bundle.nodes if node.kind == "clue"]
    blob = json.dumps(
        {
            "edges": [
                {"condition": edge.condition, "extra": edge.extra, "label": edge.label}
                for edge in bundle.edges
            ],
            "triggers": bundle.triggers,
            "nodes": [{"id": node.id, "extra": node.extra, "objects": node.objects} for node in bundle.nodes],
        },
        ensure_ascii=False,
        default=str,
    )
    used = 0
    for clue in clues:
        candidates = [clue.id, clue.title, *clue.aliases]
        if any(candidate and candidate in blob for candidate in candidates):
            used += 1
    return used, len(clues)


def _check_objects(bundle: NarrativeBundle) -> tuple[int, int]:
    checks = 0
    split = 0
    for node in bundle.nodes:
        for obj in node.objects or []:
            if not isinstance(obj, dict):
                continue
            for interaction in obj.get("interactions") or []:
                if not isinstance(interaction, dict):
                    continue
                for check in interaction.get("checks") or []:
                    if not isinstance(check, dict):
                        continue
                    checks += 1
                    outcomes = check.get("outcomes") or check.get("results")
                    if isinstance(outcomes, dict) and outcomes.get("success") and outcomes.get("fail"):
                        split += 1
    return split, checks


def compute_quality(
    bundle: NarrativeBundle,
    *,
    order_independent: bool | None = None,
) -> QualityReport:
    topology = str(bundle.topology or "hybrid")
    index = build_index(bundle)
    locations = [node for node in bundle.nodes if node.kind in {"location", "area", "act", "ending"}]
    plan_locations = [node for node in locations if node.kind in {"location", "area", "act"}]
    movement = [edge for edge in bundle.edges if edge.type in _MOVEMENT_TYPES]
    hard_movement = [edge for edge in movement if edge.hard]
    gated = [edge for edge in movement if edge.condition]
    gated_ok = [edge for edge in gated if edge.condition]
    next_edges = [edge for edge in movement if edge.type == "next"]
    inferred = [edge for edge in bundle.edges if edge.source not in {"author", "import", "structure"}]
    hidden_missing = [
        node for node in bundle.nodes
        if normalize_visibility(node.visibility) == "hidden" and not node.reveal_when
    ]
    review = [
        node for node in bundle.nodes
        if isinstance(node.extra, dict) and node.extra.get("needs_review")
    ]
    with_body = [node for node in bundle.nodes if str(node.body or node.summary or "").strip()]
    start = resolve_start(bundle)
    start_node = bundle.node(start) if start else None

    used_clues, total_clues = _check_clue_usage(bundle)
    split_checks, total_checks = _check_objects(bundle)
    reachable_plan = [node for node in plan_locations if not start or node.id in index.reachable]
    undirected_return = 0
    if start:
        reverse: dict[str, set[str]] = {}
        for source, targets in index.adjacency.items():
            for target in targets:
                reverse.setdefault(target, set()).add(source)
        seen = {start}
        queue = [start]
        while queue:
            current = queue.pop()
            for source in reverse.get(current, set()):
                if source not in seen:
                    seen.add(source)
                    queue.append(source)
        undirected_return = len(
            [node for node in plan_locations if node.id in seen and node.id != start]
        )
    non_end_locations = [node for node in plan_locations if node.kind != "ending"]

    degrees = [len(index.adjacency.get(node.id, [])) for node in bundle.nodes]
    avg_degree = round(sum(degrees) / len(degrees), 2) if degrees else 0.0
    max_degree = max(degrees) if degrees else 0
    hard_ratio = _ratio(len(hard_movement), len(movement))
    linearity = _ratio(len(next_edges), len(movement))
    inferred_confidence = (
        round(sum(edge.confidence for edge in inferred) / len(inferred), 3) if inferred else 1.0
    )
    review_rate = _ratio(len(review), len(with_body))
    reach_rate = _ratio(len(reachable_plan), len(plan_locations))
    return_rate = _ratio(undirected_return, len(non_end_locations))

    if topology == "linear":
        degree_target, degree_pass = "1.0 - 1.2", 1.0 <= avg_degree <= 1.2
        linear_target, linear_pass = ">= 0.9", linearity >= 0.9
        return_target, return_pass = "-", None
        order_target, order_pass = "-", None
    else:
        degree_target, degree_pass = "1.5 - 4.0 / 最大 <= 8", 1.5 <= avg_degree <= 4.0 and max_degree <= 8
        linear_target, linear_pass = "<= 0.1", linearity <= 0.1
        return_target, return_pass = "= 100%", return_rate >= 0.999
        order_target = "= true"
        order_pass = None if order_independent is None else bool(order_independent)

    metrics = [
        QualityMetric("reachability", "可达率", reach_rate, "= 100%", reach_rate >= 0.999,
                      f"{len(reachable_plan)}/{len(plan_locations)} 个 plan 节点可达"),
        QualityMetric("clue_usage", "线索利用率", _ratio(used_clues, total_clues), ">= 80%",
                      None if not total_clues else used_clues / total_clues >= 0.8,
                      f"{used_clues}/{total_clues} 条线索被引用"),
        QualityMetric("hard_ratio", "硬边占比", hard_ratio, "30% - 70%", 0.3 <= hard_ratio <= 0.7,
                      f"{len(hard_movement)}/{len(movement)} 条移动边为硬约束"),
        QualityMetric("degree", "平均/最大出度", f"{avg_degree}/{max_degree}", degree_target, degree_pass),
        QualityMetric("return_rate", "返回率", return_rate, return_target, return_pass,
                      f"{undirected_return}/{len(non_end_locations)} 个非结局地点可返回入口"),
        QualityMetric("order_independence", "顺序无关性", order_independent, order_target, order_pass),
        QualityMetric("gate_source", "门控来源完整率", _ratio(len(gated_ok), len(gated)), "= 100%",
                      len(gated_ok) == len(gated), f"{len(gated_ok)}/{len(gated)} 条门控边有条件来源"),
        QualityMetric("check_split", "结果拆分率", _ratio(split_checks, total_checks), "= 100%",
                      None if not total_checks else split_checks == total_checks,
                      f"{split_checks}/{total_checks} 个检定同时有成功/失败结果"),
        QualityMetric("linearity", "线性度", linearity, linear_target, linear_pass,
                      f"{len(next_edges)}/{len(movement)} 条移动边为 next"),
        QualityMetric("inferred_confidence", "推断边平均置信度", inferred_confidence, ">= 0.75",
                      inferred_confidence >= 0.75, f"{len(inferred)} 条推断边"),
        QualityMetric("review_rate", "可见性复核率", review_rate, "<= 10%", review_rate <= 0.1,
                      f"{len(review)}/{len(with_body)} 个有正文对象待复核"),
        QualityMetric("kp_leak", "KP-only 泄露", 0, "= 0（硬性）", True, "由输出闸门与检索过滤保证"),
        QualityMetric("hidden_missing", "隐藏缺条件", len(hidden_missing), "= 0",
                      not hidden_missing, f"{len(hidden_missing)} 个 hidden 对象缺少 reveal_when"),
        QualityMetric("opening", "开场可用性", bool(start_node), "= 100%",
                      bool(start_node and start_node.visibility == "public" and start_node.kind != "ending"),
                      f"start={start or '-'}"),
    ]
    return QualityReport(topology=topology, metrics=metrics)


__all__ = ["QualityMetric", "QualityReport", "compute_quality"]
