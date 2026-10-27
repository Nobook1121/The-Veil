"""世界树生成向导（设计文档 §7.2、§8.5 生成页签）。

只做「预览」：在内存里跑确定性建图流水线，返回新 bundle 与相对当前草稿的
差异；绝不写 `narrative.json`。作者确认后再用 PUT /api/scenarios/<id>/narrative
以 `base_revision` 落盘，沿用既有冲突检测。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.agents.narrative.builder import build_narrative
from trpg_server.agents.narrative.model import TOPOLOGIES, NarrativeBundle, NarrativeEdge, utc_now_iso
from trpg_server.agents.narrative.store import build_index, bundle_summary, find_scenario_storage, load_bundle
from trpg_server.json_store import read_json

logger = logging.getLogger(__name__)

SOURCES = ("document", "modules", "premise")
BRANCH_LEVELS = ("low", "medium", "high")
MAX_ACTS = 8
MAX_SCENES_PER_ACT = 8
MAX_ENDINGS = 6
MAX_CLOCKS = 4

_SOURCE_TEXT_FIELDS = ("document_text", "raw_text", "script_text", "source_text")


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass
class GenerateOptions:
    source: str = "document"
    topology: str = "auto"
    acts: int = 2
    scenes_per_act: int = 3
    free_order: bool = True
    branch: str = "medium"
    endings: int = 2
    clocks: int = 1
    ai_fill: bool = False
    premise: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "GenerateOptions":
        data = payload if isinstance(payload, Mapping) else {}
        source = str(data.get("source") or "document").strip().lower()
        if source not in SOURCES:
            source = "document"
        topology = str(data.get("topology") or "auto").strip().lower()
        if topology not in TOPOLOGIES:
            topology = "auto"
        branch = str(data.get("branch") or "medium").strip().lower()
        if branch not in BRANCH_LEVELS:
            branch = "medium"
        return cls(
            source=source,
            topology=topology,
            acts=_clamp(data.get("acts"), 2, 1, MAX_ACTS),
            scenes_per_act=_clamp(data.get("scenes_per_act"), 3, 1, MAX_SCENES_PER_ACT),
            free_order=bool(data.get("free_order", True)),
            branch=branch,
            endings=_clamp(data.get("endings"), 2, 0, MAX_ENDINGS),
            clocks=_clamp(data.get("clocks"), 1, 0, MAX_CLOCKS),
            ai_fill=bool(data.get("ai_fill")),
            premise=str(data.get("premise") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "topology": self.topology,
            "acts": self.acts,
            "scenes_per_act": self.scenes_per_act,
            "free_order": self.free_order,
            "branch": self.branch,
            "endings": self.endings,
            "clocks": self.clocks,
            "ai_fill": self.ai_fill,
            "premise": self.premise,
        }


def _descriptor_text(descriptor: Mapping[str, Any]) -> str:
    for field in _SOURCE_TEXT_FIELDS:
        value = descriptor.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _modules_from_descriptor(descriptor: Mapping[str, Any]) -> list[dict[str, Any]]:
    modules = descriptor.get("modules")
    if not isinstance(modules, list):
        return []
    return [dict(module) for module in modules if isinstance(module, Mapping)]


def _modules_as_document(modules: list[dict[str, Any]]) -> str:
    """把模块拼成带 Markdown 标题的文档，供缺少原文的剧本复用标题识别。"""
    blocks: list[str] = []
    for module in modules:
        title = str(module.get("title") or module.get("name") or "").strip()
        content = str(module.get("content") or module.get("text") or module.get("summary") or "").strip()
        if not title or not content:
            continue
        blocks.append(f"## {title}\n\n{content}")
    return "\n\n".join(blocks)


def _premise_document(options: GenerateOptions) -> str:
    """把一句梗概展开成确定性的章节骨架，交给标题识别生成 act/area/location。"""
    premise = options.premise or "未命名剧本"
    lines = ["# 梗概", "", premise, ""]
    for act in range(1, options.acts + 1):
        lines += [f"# 第{act}幕", "", f"本幕围绕：{premise}", "", f"## 区域 {act}", ""]
        for scene in range(1, options.scenes_per_act + 1):
            lines += [f"### 地点 {act}-{scene}", "", "（待补充）", ""]
    if options.endings:
        lines += ["# 结局", ""]
        for index in range(1, options.endings + 1):
            lines += [f"## 结局 {index}", "", "（待补充）", ""]
    return "\n".join(lines)


def _build(document: str, descriptor: Mapping[str, Any], modules: list[dict[str, Any]], storage: Path) -> Any:
    return build_narrative(
        document,
        scenario=descriptor,
        scenario_id=str(descriptor.get("id") or ""),
        scenario_version=str(descriptor.get("version") or "1.0.0"),
        modules=modules or None,
        scenario_dir=storage,
        save=False,
    )


def _current_node_map(bundle: NarrativeBundle | None) -> dict[str, dict[str, Any]]:
    if bundle is None:
        return {}
    return {node.id: node.to_dict() for node in bundle.nodes}


def _current_edge_map(bundle: NarrativeBundle | None) -> dict[str, dict[str, Any]]:
    if bundle is None:
        return {}
    return {edge.id: edge.to_dict() for edge in bundle.edges}


def _diff_nodes(current: dict[str, dict[str, Any]], generated: dict[str, dict[str, Any]]) -> dict[str, Any]:
    added = sorted(set(generated) - set(current))
    removed = sorted(set(current) - set(generated))
    modified = sorted(key for key in set(current) & set(generated) if current[key] != generated[key])
    return {"added": added, "removed": removed, "modified": modified}


def _diff_edges(current: dict[str, dict[str, Any]], generated: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return _diff_nodes(current, generated)


def _edge_counts(bundle: NarrativeBundle) -> dict[str, int]:
    free = gated = soft = 0
    for edge in bundle.edges:
        if edge.condition is not None or edge.type == "requires":
            gated += 1
        elif edge.type in {"adjacent", "portal", "next", "leads_to"}:
            free += 1
        if not edge.hard:
            soft += 1
    return {"free": free, "gated": gated, "soft": soft}


def _add_branch_edges(bundle: NarrativeBundle, options: GenerateOptions) -> int:
    """按分支度在同一区域内补充相邻边；只作用于梗概骨架。"""
    if options.branch == "low":
        return 0
    by_parent: dict[str, list[str]] = {}
    parent_kind = {node.id: node.kind for node in bundle.nodes}
    for node in bundle.nodes:
        if node.kind != "location":
            continue
        parent = node.parent or ""
        if parent and parent_kind.get(parent) == "area":
            by_parent.setdefault(parent, []).append(node.id)
    existing = {(edge.from_id, edge.to_id, edge.type) for edge in bundle.edges}
    added = 0
    for location_ids in by_parent.values():
        pairs = []
        if options.branch == "medium" and len(location_ids) > 1:
            pairs = [(location_ids[0], location_ids[-1])]
        elif options.branch == "high":
            pairs = [
                (location_ids[first], location_ids[second])
                for first in range(len(location_ids))
                for second in range(first + 1, len(location_ids))
            ]
        for source, target in pairs:
            if (source, target, "adjacent") in existing or (target, source, "adjacent") in existing:
                continue
            bundle.edges.append(
                NarrativeEdge(
                    id=f"edge_{source}__{target}__adjacent",
                    from_id=source,
                    to_id=target,
                    type="adjacent",
                    source="generated",
                    bidirectional=True,
                )
            )
            existing.add((source, target, "adjacent"))
            added += 1
    return added


def _add_clocks(bundle: NarrativeBundle, options: GenerateOptions) -> int:
    if not options.clocks:
        return 0
    known = {
        str(item.get("id") or "")
        for item in bundle.clocks
        if isinstance(item, Mapping)
    }
    added = 0
    for index in range(1, options.clocks + 1):
        clock_id = f"clock_{index}"
        if clock_id in known:
            continue
        bundle.clocks.append(
            {"id": clock_id, "title": f"全局时钟 {index}", "max": 6, "value": 0, "source": "generated"}
        )
        added += 1
    return added


def _warning(code: str, message: str, level: str = "info") -> dict[str, Any]:
    return {"code": code, "level": level, "message": message}


def generate_preview(
    descriptor: Mapping[str, Any],
    storage: Path,
    options: GenerateOptions,
    *,
    current: NarrativeBundle | None = None,
    ai_filler: Callable[[NarrativeBundle, str, Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    modules = _modules_from_descriptor(descriptor)
    document = ""
    build_modules: list[dict[str, Any]] = []

    if options.source == "premise":
        document = _premise_document(options)
        warnings.append(
            _warning("generate.premise_skeleton", "梗概骨架只包含章节占位，请逐节点补全正文后再发布。", "warning")
        )
    elif options.source == "modules":
        build_modules = modules
        if not modules:
            warnings.append(_warning("generate.no_modules", "该剧本还没有模块，生成结果为空。", "warning"))
    else:
        document = _descriptor_text(descriptor)
        if not document:
            document = _modules_as_document(modules)
            if document:
                warnings.append(
                    _warning("generate.document_fallback", "未找到原始文档，已改用现有模块内容生成。", "warning")
                )
            else:
                warnings.append(_warning("generate.no_document", "该剧本没有可用的文档或模块内容。", "warning"))

    if options.ai_fill and ai_filler is None:
        warnings.append(
            _warning("generate.ai_fill_unavailable", "AI 语义补全尚未接入模型，本次仅使用确定性抽取。", "warning")
        )
    if options.source != "premise" and (options.acts or options.scenes_per_act or options.endings):
        warnings.append(
            _warning("generate.structure_options_ignored", "章节/区域/结局数量只影响「从梗概生成」。")
        )

    result = _build(document, descriptor, build_modules, storage)
    bundle: NarrativeBundle = result.bundle
    warnings.extend(result.warnings)

    if options.topology in TOPOLOGIES:
        bundle.topology = options.topology
        bundle.topology_source = "author"
    if options.source == "premise":
        branch_added = _add_branch_edges(bundle, options)
        clock_added = _add_clocks(bundle, options)
        if branch_added or clock_added:
            bundle.meta["generated_extras"] = {"branch_edges": branch_added, "clocks": clock_added}
    if options.ai_fill and ai_filler is not None:
        fill = ai_filler(bundle, document, storage)
        warnings.extend(fill.get("warnings") or [])
        bundle.meta["ai_fill"] = {
            "filled": fill.get("filled", 0),
            "calls": fill.get("calls", 0),
            "skipped": fill.get("skipped", 0),
        }
    bundle.meta["free_order"] = options.free_order
    bundle.meta["generator"] = "narrative.generate.v1"
    bundle.build_hash = ""
    bundle.generated_at = utc_now_iso()

    index = build_index(bundle)
    node_diff = _diff_nodes(_current_node_map(current), {node.id: node.to_dict() for node in bundle.nodes})
    edge_diff = _diff_edges(_current_edge_map(current), {edge.id: edge.to_dict() for edge in bundle.edges})
    edge_counts = _edge_counts(bundle)

    return {
        "options": options.to_dict(),
        "bundle": bundle.to_dict(),
        "summary": bundle_summary(bundle, index=index),
        "topology": {
            "topology": bundle.topology,
            "confidence": bundle.topology_confidence,
            "source": bundle.topology_source,
        },
        "diff": {
            "nodes": node_diff,
            "edges": edge_diff,
            "counts": {
                "nodes_added": len(node_diff["added"]),
                "nodes_removed": len(node_diff["removed"]),
                "nodes_modified": len(node_diff["modified"]),
                "edges_added": len(edge_diff["added"]),
                "edges_removed": len(edge_diff["removed"]),
                "edges_modified": len(edge_diff["modified"]),
                "free_edges": edge_counts["free"],
                "gated_edges": edge_counts["gated"],
                "soft_edges": edge_counts["soft"],
            },
        },
        "warnings": warnings,
        "base_revision": _bundle_revision(current),
        "report": {
            "ok": result.report.error_count == 0,
            "error_count": result.report.error_count,
            "warning_count": result.report.warning_count,
        },
    }


def _bundle_revision(bundle: NarrativeBundle | None) -> int:
    if bundle is None:
        return 0
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    try:
        return max(0, int(meta.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _update_active(store, job_id: str, **changes: Any) -> bool:
    """任务已被取消时不再写状态，避免 cancelled 被 done / failed 覆盖。"""
    job = store.get(job_id)
    if isinstance(job, Mapping) and job.get("status") == "cancelled":
        return False
    store.update(job_id, **changes)
    return True


def run_generate_job(app, job_id: str) -> None:
    """线程池入口：读取任务参数、生成预览并落 result.json。"""
    store = app.extensions["narrative_generate_store"]
    job = store.get(job_id)
    if not isinstance(job, Mapping):
        return
    if job.get("status") not in {"pending", "running"}:
        return
    if not _update_active(store, job_id, status="running", stage="loading", progress=10):
        return
    try:
        scenarios_dir = Path(app.config["SCENARIOS_DIR"])
        _, storage = find_scenario_storage(scenarios_dir, str(job.get("scenario_id")))
        if storage is None:
            raise ValueError("Scenario not found")
        descriptor = read_json(storage / "scenario.json", default={})
        if not isinstance(descriptor, Mapping):
            raise ValueError("scenario.json 不是对象")
        current = load_bundle(storage, use_cache=False)
        options = GenerateOptions.from_payload(job.get("options"))
        if not _update_active(store, job_id, stage="generating", progress=40):
            return
        ai_filler = None
        if options.ai_fill:
            from trpg_server.agents.narrative.generate_ai import make_ai_filler

            ai_filler = make_ai_filler(str(descriptor.get("title") or ""))
        preview = generate_preview(descriptor, storage, options, current=current, ai_filler=ai_filler)
        # 慢调用（尤其 AI 补全）返回后可能已经被取消，此时丢弃结果。
        if not _update_active(store, job_id):
            return
        preview["scenario_id"] = job.get("scenario_id")
        preview["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        store.save_result(job_id, preview)
        _update_active(
            store,
            job_id,
            status="done",
            stage="done",
            progress=100,
            result_summary={
                "nodes": len(preview["bundle"].get("nodes") or []),
                "edges": len(preview["bundle"].get("edges") or []),
                "topology": preview["topology"]["topology"],
                "diff": preview["diff"]["counts"],
                "warnings": len(preview["warnings"]),
            },
            warnings=preview["warnings"][:50],
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败要回传给前端
        logger.exception("Narrative generation failed: %s", job_id)
        _update_active(store, job_id, status="failed", stage="failed", progress=100, error=str(exc))


__all__ = ["GenerateOptions", "generate_preview", "run_generate_job"]
