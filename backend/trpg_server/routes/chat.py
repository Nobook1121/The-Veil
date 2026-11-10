"""聊天路由入口：/api/chat、/api/messages 与剧本模消息入口。

回合流水线在 :mod:`trpg_server.agents.turn_pipeline`，本模块只负责请求解析、
把流水线结果/异常映射成响应，并保留对既有符号（缓存单例、请求构造器等）的 re-export。
"""
import json
import logging
import secrets
import time
from dataclasses import replace
from pathlib import Path

import requests
from flask import Blueprint, current_app, request, session
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.ai_platform_resolver import (
    CAPABILITY_CHAT,
    mark_platform_failure,
    mark_platform_success,
)
from trpg_server.agents.ai_request import merge_request_template
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.context import build_agent_context
from trpg_server.agents.profiles import resolve_agent_profile, with_graph_tools
from trpg_server.agents.runtime import run_agent_completion
from trpg_server.agents.structured_output import apply_state_updates, parse_kp_response, validate_state_updates, validate_structured_response
from trpg_server.agents.telemetry import build_provider_cache_key, calculate_cache_hit_rate, record_ai_usage
from trpg_server.agents.prompt_builder import build_prompt_layers
from trpg_server.agents.token_accounting import attribute_prompt_segments
from trpg_server.agents.cache import ExactResponseCache, ProviderPrefixCache, SemanticCache, build_exact_response_key, stable_state_for_cache
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.agents.knowledge_base import KnowledgeBaseService
from trpg_server.agents.latex import build_latex_prompt, clean_message_for_model, load_latex_settings
from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore, search_ruleset
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.combat import COMBAT_TOOL_NAMES
from trpg_server.agents.tools.room import get_room_snapshot
from trpg_server.agents.room_state import append_room_event, project_room_state
from trpg_server.agents.narrative.guards import apply_output_guard_report
from trpg_server.agents.narrative.logger import log_event
from trpg_server.agents.narrative.metrics import bump as bump_narrative_metrics
from trpg_server.agents.narrative.metrics import observe as observe_narrative_metrics
from trpg_server.agents.narrative.prompt_v2 import render_state_summary
from trpg_server.agents.narrative.prejudge import prepare_turn, render_contract, retrieval_candidates
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError, narrative_available
from trpg_server.agents.narrative.runtime_settings import log_bypass_once, resolve_narrative_settings
from trpg_server.immersive.service import on_narrative_turn
from trpg_server.voice.director import prepare_voice
from trpg_server.agents.narrative.state_store import narrative_from_room_state
from trpg_server.agents.narrative.store import NarrativeStoreError, load_bundle
from trpg_server.agents.narrative.trace import append_trace
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.room_log import log_room_event
from trpg_server import room_ai_changes, room_interlude, room_rules
from trpg_server.room_mode import (
    SCENARIO_BINDING_NONE,
    SCENARIO_BINDING_REQUIRED,
    ai_kp_enabled,
    normalize_room_mode,
)
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.role_config import load_roles, resolve_task_model
from trpg_server.world_override import (
    facts_from_state,
    format_facts_block,
    rate_limit_snapshot,
    record_override,
    resolve_policy,
)
from trpg_server.settings import (
    CONFIG_DIR,
    ROOMS_DIR,
    SCENARIOS_DIR,
    LOGS_DIR,
    KNOWLEDGE_BASES_DIR,
)
from trpg_server.agents.memory_consolidation import (
    archive_history_for_memory,
    build_memory_context,
    harvest_room_memory,
    remember_compact_facts,
)
from trpg_server.routes.chat_helpers import (
    _begin_graph_scenario,
    _build_agent_requester,
    _build_knowledge_usage,
    _can_start_scenario,
    _compact_history_with_ai,
    _deliver_structured_triggers,
    _emit_thinking_stage,
    _format_user_content,
    _get_ai_platform_dir,
    _get_config_dir,
    _get_kp_prompt_file,
    _get_role_config_file,
    _history_for_request,
    _history_needs_compaction,
    _is_compact_command,
    _json_for_log,
    _load_debug_kp_prompt,
    _load_enabled_platform,
    _load_enabled_platforms,
    _load_history,
    _load_kp_prompt,
    _mark_scenario_started,
    _maybe_remember_important_action,
    _message_response,
    _opening_fallback_text,
    _opening_request_text,
    _notify_platform_switch,
    _profile_for_request,
    _request_allows_check,
    _request_allows_manual_trigger,
    _request_allows_scene_transition,
    _retrieval_query,
    _room_session_id,
    _room_snapshot_system_message,
    _select_model,
    _speaker_for_user,
    _strip_compact_command,
    _sync_room_active_scene,
    _timestamp,
    _load_role_for_content,
)

bp = Blueprint("chat", __name__)
logger = logging.getLogger(__name__)
_PREFIX_CACHE = ProviderPrefixCache(default_ttl=3600)
_EXACT_CACHE = ExactResponseCache(default_ttl=300)
_SEMANTIC_CACHE = SemanticCache(default_ttl=120)


from trpg_server.agents.turn_pipeline import (  # noqa: F401 - 兼容旧导入
    FREEFORM_EXCLUDED_TOOLS,
    WORLD_OVERRIDE_BLOCKED_TOOLS,
    _build_platform_request_state,
    _freeform_system_prompt_suffix,
    _opening_fallback_payload,
    _post_ai_request,
    run_turn,
)

@bp.route("/api/chat", methods=["POST"])
def chat():
    message_data = request.get_json(silent=True)
    if not message_data:
        return error_response("Please provide message data", 400, "No data")
    return run_turn(
        message_data,
        prefix_cache=_PREFIX_CACHE,
        exact_cache=_EXACT_CACHE,
        semantic_cache=_SEMANTIC_CACHE,
    )


@bp.route("/api/messages", methods=["POST"])
def send_home_message():
    try:
        message_data = request.get_json(silent=True)
        if not message_data:
            return error_response("Please provide message data", 400, "No data")

        user_id = message_data.get("user_id", "unknown")
        message_content = message_data.get("content", "")
        if not message_content:
            return error_response(
                "Please provide message content",
                400,
                "No message content",
            )

        log_user_action(
            logger,
            user_action_text(session.get("username") or user_id, "发送了首页消息"),
            用户ID=session.get("user_id") or user_id,
            内容长度=len(message_content),
        )
        return _message_response(user_id, message_content, "Message sent successfully")
    except Exception as exc:
        logger.exception("Failed to send home message")
        return server_error("Failed to send message")


@bp.route("/api/scenarios/<int:script_id>/messages", methods=["POST"])
def send_message(script_id):
    try:
        message_data = request.get_json(silent=True)
        if not message_data:
            return error_response("Please provide message data", 400, "No data")

        user_id = message_data.get("user_id", "unknown")
        message_content = message_data.get("content", "")
        if not message_content:
            return error_response(
                "Please provide message content",
                400,
                "No message content",
            )

        log_user_action(
            logger,
            user_action_text(session.get("username") or user_id, "发送了剧本消息"),
            用户ID=session.get("user_id") or user_id,
            剧本ID=script_id,
            内容长度=len(message_content),
        )
        return _message_response(
            user_id,
            message_content,
            "Scenario message sent successfully",
            script_id=script_id,
        )
    except Exception as exc:
        logger.exception("Failed to send scenario message: %s", script_id)
        return server_error("Failed to send scenario message")
