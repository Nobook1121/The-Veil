"""建图流水线（设计文档 §7.2）。

阶段 A-G 的确定性部分：
A 解析文档 → B 章节切分 → C 定位树 → D 边生成 → E 拓扑判定
→ F 可见性裁决 → G 校验与布局 → 写 `narrative.json`。

LLM 补边（阶段 H）不在本模块内，保持零模型调用。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.narrative.edges import build_structural_edges, merge_edges
from trpg_server.agents.narrative.extract import ExtractedNarrative, extract_narrative
from trpg_server.agents.narrative.layout import apply_layout
from trpg_server.agents.narrative.logger import log_event, log_warning
from trpg_server.agents.narrative.model import (
    TOPOLOGIES,
    NarrativeBundle,
    content_fingerprint,
    utc_now_iso,
)
from trpg_server.agents.narrative.store import (
    NarrativeIndex,
    build_index,
    save_bundle,
    scenario_dir_for_descriptor,
    scenario_id_from_dir,
)
from trpg_server.agents.narrative.topology import TopologyDecision, detect_topology
from trpg_server.agents.narrative.validate import ValidationReport, validate_bundle
from trpg_server.json_store import read_json
from trpg_server.scenario_documents import ParsedDocument

BUILDER_VERSION = "narrative.builder.v1"


@dataclass
class BuildResult:
    bundle: NarrativeBundle
    report: ValidationReport
    index: NarrativeIndex
    warnings: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None


def _scenario_modules(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    modules = scenario.get("modules") if isinstance(scenario, Mapping) else None
    if not isinstance(modules, list):
        return []
    return [dict(module) for module in modules if isinstance(module, Mapping)]


def _document_text(document: ParsedDocument | str | None) -> str:
    if isinstance(document, ParsedDocument):
        return str(document.markdown or "")
    return str(document or "")


def _source_hash(text: str, modules: list[dict[str, Any]]) -> str:
    payload = text if text.strip() else json.dumps(modules, ensure_ascii=False, sort_keys=True)
    return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _explicit_topology(scenario: Mapping[str, Any]) -> str | None:
    value = str(scenario.get("topology") or "").strip().lower()
    if value in TOPOLOGIES and str(scenario.get("topology_source") or "").strip().lower() == "author":
        return value
    return None


def build_narrative(
    document: ParsedDocument | str | None = None,
    *,
    scenario: Mapping[str, Any] | None = None,
    scenario_id: str = "",
    scenario_version: str = "",
    modules: list[dict[str, Any]] | None = None,
    scenario_dir: Path | str | None = None,
    save: bool = False,
) -> BuildResult:
    scenario = scenario or {}
    sid = str(scenario_id or scenario.get("id") or "").strip()
    version = str(scenario_version or scenario.get("version") or "1.0.0").strip()
    module_list = modules if modules is not None else _scenario_modules(scenario)
    text = _document_text(document)

    extracted: ExtractedNarrative = extract_narrative(
        document if document is not None else "",
        scenario_id=sid,
        modules=module_list,
    )
    bundle = NarrativeBundle(scenario_id=sid, scenario_version=version, nodes=list(extracted.nodes))

    text_by_node = {node.id: node.body for node in bundle.nodes}
    incoming = build_structural_edges(bundle.nodes, sections=extracted.sections, text_by_node=text_by_node)
    bundle.edges = merge_edges(bundle.edges, incoming)

    decision: TopologyDecision = detect_topology(bundle, explicit=_explicit_topology(scenario))
    bundle.topology = decision.topology
    bundle.topology_confidence = round(float(decision.confidence), 2)
    bundle.topology_source = decision.source
    bundle.meta = {
        "start": str(scenario.get("start") or scenario.get("start_location") or "").strip() or None,
        "generator": BUILDER_VERSION,
        "topology_evidence": list(decision.evidence),
    }
    bundle.source_document_hash = _source_hash(text, module_list)

    positioned = apply_layout(bundle)
    index = build_index(bundle)
    if not bundle.meta.get("start"):
        bundle.meta["start"] = index.start
    report = validate_bundle(bundle, index=index)

    warnings = list(extracted.warnings)
    for issue in report.issues:
        if issue.level in {"warn", "error"}:
            warnings.append(
                {
                    "code": issue.code,
                    "level": issue.level,
                    "message": issue.message,
                    "node_id": issue.node_id,
                    "edge_id": issue.edge_id,
                }
            )
    bundle.warnings = warnings[:200]
    bundle.build_hash = content_fingerprint(bundle)
    bundle.generated_at = utc_now_iso()

    path: Path | None = None
    if scenario_dir is not None and save:
        path = save_bundle(scenario_dir, bundle, update_hash=False)

    log_event(
        "build.completed",
        scenario_id=sid,
        nodes=len(bundle.nodes),
        edges=len(bundle.edges),
        topology=bundle.topology,
        topology_confidence=bundle.topology_confidence,
        errors=report.error_count,
        warnings=report.warning_count,
        positioned=positioned,
        saved=bool(path),
    )
    if report.error_count:
        log_warning("validate.errors", scenario_id=sid, errors=report.error_count)
    return BuildResult(bundle=bundle, report=report, index=index, warnings=list(bundle.warnings), path=path)


def build_from_scenario(
    scenario: Mapping[str, Any],
    *,
    document: ParsedDocument | str | None = None,
    scenario_dir: Path | str | None = None,
    save: bool = False,
) -> BuildResult:
    text = document
    if text is None:
        text = (
            scenario.get("document_text")
            or scenario.get("raw_text")
            or scenario.get("script_text")
            or scenario.get("source_text")
            or ""
        )
    return build_narrative(
        text,
        scenario=scenario,
        scenario_id=str(scenario.get("id") or ""),
        scenario_version=str(scenario.get("version") or ""),
        scenario_dir=scenario_dir,
        save=save,
    )


def rebuild_from_descriptor(descriptor_path: Path | str, *, save: bool = True) -> BuildResult:
    descriptor = Path(descriptor_path)
    scenario = read_json(descriptor, default={})
    if not isinstance(scenario, Mapping):
        raise ValueError(f"scenario.json 不是对象: {descriptor}")
    scenario_dir = scenario_dir_for_descriptor(descriptor)
    result = build_from_scenario(scenario, scenario_dir=scenario_dir, save=save)
    if not result.bundle.scenario_id:
        result.bundle.scenario_id = scenario_id_from_dir(scenario_dir)
    return result


def rebuild_all(scenarios_dir: Path | str) -> list[tuple[Path, BuildResult | None, str | None]]:
    """批量重建；返回 (descriptor_path, result, error)。"""
    root = Path(scenarios_dir)
    results: list[tuple[Path, BuildResult | None, str | None]] = []
    for descriptor in sorted(root.glob("*/scenario.json")):
        try:
            results.append((descriptor, rebuild_from_descriptor(descriptor), None))
        except Exception as exc:  # noqa: BLE001 - 单剧本失败不应中断批量
            log_warning("build.failed", descriptor=str(descriptor), error=type(exc).__name__)
            results.append((descriptor, None, str(exc)))
    return results


__all__ = [
    "BUILDER_VERSION",
    "BuildResult",
    "build_from_scenario",
    "build_narrative",
    "rebuild_all",
    "rebuild_from_descriptor",
]
