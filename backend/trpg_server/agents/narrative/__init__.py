"""世界树 / 图谱叙事层。

入口模块：
- `model`：`narrative.json` 数据模型与归一化；
- `visibility`：三态可见性、自动分类与输出闸门；
- `store`：读写与派生索引；
- `conditions`：条件 DSL 确定性求值。
"""
from trpg_server.agents.narrative.builder import (
    BuildResult,
    build_from_scenario,
    build_narrative,
    rebuild_all,
    rebuild_from_descriptor,
)
from trpg_server.agents.narrative.conditions import ConditionResult, evaluate_condition
from trpg_server.agents.narrative.conditions import condition_atoms
from trpg_server.agents.narrative.edges import build_structural_edges, merge_edges
from trpg_server.agents.narrative.extract import ExtractedNarrative, Section, detect_sections, extract_narrative
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.store import (
    NarrativeIndex,
    build_index,
    bundle_summary,
    find_scenario_storage,
    load_bundle,
    narrative_path,
    save_bundle,
)
from trpg_server.agents.narrative.layout import apply_layout
from trpg_server.agents.narrative.topology import TopologyDecision, detect_topology
from trpg_server.agents.narrative.validate import (
    ValidationIssue,
    ValidationReport,
    simulate_any_order,
    validate_bundle,
)
from trpg_server.agents.narrative.visibility import (
    assert_no_kp_only,
    classify_visibility,
    forbidden_names,
    is_visible_to_player,
    normalize_visibility,
)

__all__ = [
    "BuildResult",
    "ConditionResult",
    "ExtractedNarrative",
    "NarrativeBundle",
    "NarrativeEdge",
    "NarrativeIndex",
    "NarrativeNode",
    "Section",
    "TopologyDecision",
    "ValidationIssue",
    "ValidationReport",
    "apply_layout",
    "assert_no_kp_only",
    "build_from_scenario",
    "build_index",
    "build_narrative",
    "build_structural_edges",
    "bundle_summary",
    "classify_visibility",
    "condition_atoms",
    "detect_sections",
    "detect_topology",
    "evaluate_condition",
    "extract_narrative",
    "find_scenario_storage",
    "forbidden_names",
    "is_visible_to_player",
    "load_bundle",
    "merge_edges",
    "narrative_path",
    "normalize_visibility",
    "rebuild_all",
    "rebuild_from_descriptor",
    "save_bundle",
    "simulate_any_order",
    "validate_bundle",
]
