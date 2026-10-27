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
    ensure_built,
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
from trpg_server.agents.narrative.ledger import (
    LedgerVisibilityError,
    append_fact,
    append_ledger_event,
    load_facts,
    read_ledger_events,
    save_summary,
)
from trpg_server.agents.narrative.layout import apply_layout
from trpg_server.agents.narrative.opening import (
    OpeningError,
    OpeningPlan,
    begin_scenario,
    build_opening_pack,
    opening_message,
    plan_scenario,
    render_opening_prompt,
    resolve_start_config,
)
from trpg_server.agents.narrative.prompt_v2 import render_state_summary
from trpg_server.agents.narrative.publisher import MAX_MESSAGES_PER_TURN, publish_hits
from trpg_server.agents.narrative.resolver import TriggerHit, TriggerResolver
from trpg_server.agents.narrative.state_store import (
    apply_event,
    default_narrative_state,
    load_narrative_state,
    normalize_narrative_state,
    replay_narrative,
    reset_narrative_state,
    save_narrative_state,
    write_keyframe,
)
from trpg_server.agents.narrative.topology import TopologyDecision, detect_topology
from trpg_server.agents.narrative.triggers import (
    adapt_triggers,
    normalize_asset,
    normalize_trigger_spec,
)
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
    "LedgerVisibilityError",
    "MAX_MESSAGES_PER_TURN",
    "NarrativeBundle",
    "OpeningError",
    "OpeningPlan",
    "NarrativeEdge",
    "NarrativeIndex",
    "NarrativeNode",
    "Section",
    "TopologyDecision",
    "TriggerHit",
    "TriggerResolver",
    "ValidationIssue",
    "ValidationReport",
    "adapt_triggers",
    "append_fact",
    "append_ledger_event",
    "apply_event",
    "apply_layout",
    "assert_no_kp_only",
    "begin_scenario",
    "build_from_scenario",
    "build_index",
    "build_narrative",
    "build_opening_pack",
    "build_structural_edges",
    "bundle_summary",
    "classify_visibility",
    "condition_atoms",
    "default_narrative_state",
    "detect_sections",
    "detect_topology",
    "evaluate_condition",
    "ensure_built",
    "extract_narrative",
    "find_scenario_storage",
    "forbidden_names",
    "is_visible_to_player",
    "load_bundle",
    "load_facts",
    "load_narrative_state",
    "merge_edges",
    "narrative_path",
    "normalize_narrative_state",
    "normalize_trigger_spec",
    "normalize_asset",
    "normalize_visibility",
    "opening_message",
    "plan_scenario",
    "publish_hits",
    "read_ledger_events",
    "rebuild_all",
    "rebuild_from_descriptor",
    "render_state_summary",
    "render_opening_prompt",
    "replay_narrative",
    "reset_narrative_state",
    "resolve_start_config",
    "save_bundle",
    "save_narrative_state",
    "save_summary",
    "simulate_any_order",
    "validate_bundle",
    "write_keyframe",
]
