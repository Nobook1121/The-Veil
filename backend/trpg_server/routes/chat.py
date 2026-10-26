"""聊天路由：/api/chat、/api/messages 与剧本消息入口。

纯辅助逻辑（历史压缩、提示词组装、剧本检索、结构化响应落库等）已拆分到
:mod:`trpg_server.routes.chat_helpers`，本文件只保留路由、请求编排与响应收尾，
因此原先 1500+ 行的超大文件被拆成「路由编排」与「辅助函数」两块。
"""
import json
import logging
import time
from pathlib import Path

import requests
from flask import Blueprint, current_app, request, session
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.agents.ai_request import merge_request_template
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.context import build_agent_context
from trpg_server.agents.profiles import resolve_agent_profile
from trpg_server.agents.runtime import run_agent_completion
from trpg_server.agents.structured_output import apply_state_updates, parse_kp_response, validate_state_updates, validate_structured_response
from trpg_server.agents.telemetry import build_provider_cache_key, calculate_cache_hit_rate, record_ai_usage
from trpg_server.agents.prompt_builder import build_prompt_layers
from trpg_server.agents.token_accounting import attribute_prompt_segments
from trpg_server.agents.cache import ExactResponseCache, ProviderPrefixCache, SemanticCache, build_exact_response_key, stable_state_for_cache
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.agents.knowledge_base import KnowledgeBaseService
from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore, search_ruleset
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.room import get_room_snapshot
from trpg_server.agents.room_state import append_room_event, project_room_state
from trpg_server.agents.narrative.prompt_v2 import render_state_summary
from trpg_server.agents.narrative.state_store import narrative_from_room_state
from trpg_server.agents.narrative.store import NarrativeStoreError, load_bundle
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.room_log import log_room_event
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.role_config import load_roles, resolve_task_model
from trpg_server.settings import (
    CONFIG_DIR,
    ROOMS_DIR,
    SCENARIOS_DIR,
    LOGS_DIR,
    KNOWLEDGE_BASES_DIR,
)
from trpg_server.routes.chat_helpers import (
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


def _post_ai_request(base_url, headers, timeout=300, debug=False, max_retries=5, on_retry=None):
    """构造发送 chat/completions 请求的可调用对象。

    ``debug`` 为真时才会把完整的请求/响应体写进日志——这些内容包含整段提示词与
    模型输出，正常运行下会把日志刷满，因此默认只在调试模式开启。

    ``max_retries`` 控制「连接被网络层中断」时的重试上限，``on_retry`` 会在每次重试
    前被调用（用于在聊天框里提示重试进度）。重试与预算逻辑集中在
    :func:`trpg_server.agents.http_retry.post_with_retry`，与 Responses 路径共用。
    """

    def response_detail(response):
        try:
            detail = response.json()
            return detail.get("error", detail) if isinstance(detail, dict) else detail
        except (ValueError, requests.exceptions.JSONDecodeError):
            return str(getattr(response, "text", ""))[:500]

    def provider_requires_json(status_code, detail):
        text = str(detail).lower()
        return (
            status_code == 400
            and "response_format" in text
            and "json_object" in text
            and "must contain" in text
            and "json" in text
        )

    def post(payload):
        return post_with_retry(
            base_url,
            headers=headers,
            payload=payload,
            timeout=timeout,
            max_retries=max_retries,
            on_retry=on_retry,
            label="kp_chat",
        )

    def requester(payload):
        if debug:
            logger.info("AI API request payload: %s", _json_for_log(payload))
        response = post(payload)
        if not response.ok:
            detail = response_detail(response)
            if provider_requires_json(response.status_code, detail):
                retry_payload = {**payload}
                retry_messages = [dict(message) for message in payload.get("messages", [])]
                if not any("json" in str(message.get("content", "")).lower() for message in retry_messages):
                    retry_messages.append(
                        {
                            "role": "system",
                            "content": "Return the final answer as valid JSON. The response must be a JSON object.",
                        }
                    )
                retry_payload["messages"] = retry_messages
                logger.warning("AI API retry after provider JSON response_format validation failure")
                if debug:
                    logger.info("AI API retry request payload: %s", _json_for_log(retry_payload))
                response = post(retry_payload)
                if not response.ok:
                    detail = response_detail(response)
            if not response.ok:
                raise RuntimeError(f"AI 平台请求失败（HTTP {response.status_code}）：{detail}")
        response_data = response.json()
        if not isinstance(response_data, dict):
            raise RuntimeError("AI 平台返回的数据格式无效：响应必须是对象")
        if debug:
            logger.info("AI API response payload: %s", _json_for_log(response_data))
        return response_data

    return requester


def _build_platform_request_state(platform_id, platform_config, runtime_config, on_retry=None):
    """为某个已启用平台构造一次请求所需的全部状态；配置不完整时返回 ``None``。

    房间内 AI 对话在首个平台失败后，会用它重新构造下一个候选平台的
    ``base_url``/``headers``/``model``/``requester``，实现顺序加载与自动切换。
    """
    section = platform_config.get("config") if isinstance(platform_config, dict) else None
    section = section if isinstance(section, dict) else {}
    api_key = section.get("api_key")
    base_url = chat_completions_endpoint(section.get("base_url"))
    if not base_url:
        return None
    if not api_key and platform_id == "lmstudio":
        api_key = "lm-studio"
    elif not api_key:
        return None

    model = _select_model(platform_config)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    requester = _post_ai_request(
        base_url,
        headers,
        runtime_config.ai_request_timeout,
        debug=runtime_config.debug_mode,
        max_retries=runtime_config.ai_request_max_retries,
        on_retry=on_retry,
    )
    agent_requester = _build_agent_requester(
        platform_config,
        base_url,
        headers,
        model,
        runtime_config.ai_request_timeout,
        requester,
        max_retries=runtime_config.ai_request_max_retries,
    )
    return {
        "platform": platform_id,
        "base_url": base_url,
        "headers": headers,
        "model": model,
        "requester": requester,
        "agent_requester": agent_requester,
    }


@bp.route("/api/chat", methods=["POST"])
def chat():
    try:
        message_data = request.get_json(silent=True)
        if not message_data:
            return error_response("Please provide message data", 400, "No data")

        user_id = message_data.get("user_id", "unknown")
        content = message_data.get("content", "")
        requested_role_id = str(message_data.get("role_id") or "").strip()
        if requested_role_id:
            roles = load_roles(_get_role_config_file(), _get_kp_prompt_file(), _get_ai_platform_dir())
            role_config = next((role for role in roles if str(role.get("id")) == requested_role_id), None) or _load_role_for_content(content)
        else:
            role_config = _load_role_for_content(content)
        agent_profile = resolve_agent_profile(
            role_config,
            _get_kp_prompt_file(),
            prefer_prompt_file=str(role_config.get("id") or "kp") == "kp",
        )
        selected_platform, platform_config = _load_enabled_platform(agent_profile.provider)
        if not platform_config:
            return error_response(
                "No enabled AI platform",
                400,
                "No enabled platform",
            )

        api_key = platform_config.get("config", {}).get("api_key")
        # 用户可能只填了主机名或 /v1 前缀，这里在发请求前补齐端点；不回写配置。
        base_url = chat_completions_endpoint(platform_config.get("config", {}).get("base_url"))
        if not base_url:
            return error_response(
                "AI platform config is incomplete",
                400,
                "Incomplete platform config",
            )

        if not api_key and selected_platform == "lmstudio":
            api_key = "lm-studio"
        elif not api_key:
            return error_response(
                "AI platform config is incomplete",
                400,
                "Incomplete platform config",
            )

        room_id = message_data.get("room_id")
        if room_id:
            _room_archived_check = read_json(
                Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR)) / str(room_id) / "info.json",
                default={},
            )
            if isinstance(_room_archived_check, dict) and _room_archived_check.get("archived"):
                log_access_denied(
                    logger,
                    user_action_text(session.get("username"), "访问房间被拒绝"),
                    用户ID=session.get("user_id"),
                    房间ID=room_id,
                    原因="房间已归档",
                )
                return error_response("Room is archived", 403, "归档房间已关闭 AI，无法继续调用；仍可使用骰娘与其他房间工具")
        scenario_start = message_data.get("scenario_start") is True
        if scenario_start and agent_profile.id != "kp":
            return error_response("Scenario start must use the KP agent", 400, "KP agent required")
        ai_request_id = str(message_data.get("ai_request_id") or "").strip()
        agent_context = build_agent_context(
            room_id=room_id,
            rooms_dir=current_app.config.get("ROOMS_DIR", ROOMS_DIR),
            scenarios_dir=current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR),
            user_id=user_id,
            agent_id=agent_profile.id,
            request_content=content,
            config_dir=current_app.config.get("CONFIG_DIR", CONFIG_DIR),
        )
        if scenario_start:
            if not room_id or not agent_context.room_dir:
                return error_response("A room is required to start the scenario", 400, "Room is required")
            room_info = agent_context.room_info()
            session_user_id = session.get("user_id")
            if session_user_id is None or not _can_start_scenario(room_info, session_user_id, session.get("role")):
                return error_response("Permission denied", 403, "Only room managers can start the scenario")
            if room_info.get("scenario_started_at"):
                return error_response("Scenario has already started", 409, "Scenario already started")
            content = (
                "@KP [系统开场任务] 请读取当前房间快照和起始场景所需的剧本模块，"
                "直接为所有玩家进行简洁、有代入感的开场导入。不要声称玩家说了‘开始’，"
                "不要替玩家决定行动，不要在结尾列出选项。"
            )
            agent_context = build_agent_context(
                room_id=room_id,
                rooms_dir=current_app.config.get("ROOMS_DIR", ROOMS_DIR),
                scenarios_dir=current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR),
                user_id=session_user_id,
                agent_id=agent_profile.id,
                request_content=content,
                config_dir=current_app.config.get("CONFIG_DIR", CONFIG_DIR),
            )
        agent_context.tool_state.update(
            {
                "allow_checks": _request_allows_check(content),
                "allow_unconditional_trigger": _request_allows_manual_trigger(content),
                "allow_scene_transition": _request_allows_scene_transition(content),
            }
        )
        # 骰子动画档位与玩家投掷：房间规则优先，管理员默认兜底（工具自身也会兜底）。
        from trpg_server.agents.dice_settings import resolve_dice_settings

        dice_settings = resolve_dice_settings(
            current_app.config.get("CONFIG_DIR", CONFIG_DIR),
            (agent_context.room_info() or {}).get("house_rules"),
        )
        agent_context.tool_state.update(
            {
                "dice_animation_level": dice_settings["level"],
                "dice_physics_results": dice_settings["physics_results"],
                "player_dice_roll": dice_settings["player_roll"],
                "player_dice_roll_timeout": dice_settings["timeout"],
                "dice_reroll_mode": dice_settings["reroll_mode"],
                "dice_reroll_limit": dice_settings["reroll_limit"],
                "dice_rerolls": dice_settings["rerolls"],
                "user_name": session.get("username") or str(session_user_id),
                # 玩家投掷模式下这几个工具允许同参数重复调用（每次都要重新等待玩家）。
                "no_dedupe_tools": [
                    "check.roll_room_check",
                    "dice.roll_coc_check",
                    "san.roll_sanity_check",
                    "sanity.roll_sanity_check",
                    "dice.roll",
                    "dice.roll_dice",
                ],
            }
        )
        ruleset_metrics = {"latency_ms": 0.0}
        if room_id:
            ruleset_store = RulesetKnowledgeStore(
                current_app.config.get("KNOWLEDGE_BASES_DIR", KNOWLEDGE_BASES_DIR),
                rooms_dir=current_app.config.get("ROOMS_DIR", ROOMS_DIR),
            )

            def ruleset_search(query, top_k):
                started = time.perf_counter()
                _emit_thinking_stage(room_id, ai_request_id, "ruleset_search", "正在查询规则书")
                try:
                    return search_ruleset(room_id, query, store=ruleset_store, top_k=top_k)
                finally:
                    ruleset_metrics["latency_ms"] += round((time.perf_counter() - started) * 1000, 2)

            agent_context.tool_state["ruleset_search"] = ruleset_search
        if room_id and ai_request_id:
            agent_context.tool_state["thinking_stage_callback"] = lambda stage, label: _emit_thinking_stage(
                room_id, ai_request_id, stage, label
            )
        runtime_config = load_ai_runtime_config(_get_config_dir())
        # 本地响应缓存的 TTL 可在设置页调整：命中即整次跳过上游请求，是最直接的省 token
        # 手段。这里把配置回写到模块级单例，保证新写入的条目使用新的过期时间。
        _SEMANTIC_CACHE.default_ttl = runtime_config.semantic_cache_ttl
        _EXACT_CACHE.default_ttl = runtime_config.exact_cache_ttl
        room_snapshot_message = None
        room_snapshot = None
        # Action suggestions are gated by the global hint switch (authoritative)
        # AND the per-room house rule, which defaults to off.
        house_rules = agent_context.room_info().get("house_rules") if room_id else {}
        house_rules = house_rules if isinstance(house_rules, dict) else {}
        effective_suggestions = bool(runtime_config.show_ai_hints) and bool(
            house_rules.get("action_suggestions_enabled")
        )
        if room_id:
            agent_context.tool_state["allow_action_suggestions"] = effective_suggestions
        request_profile = _profile_for_request(
            agent_profile,
            agent_context.tool_state,
            enable_suggestions=effective_suggestions,
        )
        if room_id:
            room_snapshot = get_room_snapshot({}, agent_context)
            room_snapshot_message = _room_snapshot_system_message(room_snapshot, None, runtime_config)
        model = _select_model(platform_config)
        small_model = resolve_task_model(runtime_config, platform_config, "summarization", model)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        def notify_ai_retry(retry_number, max_retries):
            """连接被中断后重试时，在聊天框的思考气泡里提示重试进度。"""
            _emit_thinking_stage(
                room_id,
                ai_request_id,
                "retrying",
                f"连接中断，正在重试（第 {retry_number}/{max_retries} 次）",
            )

        requester = _post_ai_request(
            base_url,
            headers,
            runtime_config.ai_request_timeout,
            debug=runtime_config.debug_mode,
            max_retries=runtime_config.ai_request_max_retries,
            on_retry=notify_ai_retry,
        )
        agent_requester = _build_agent_requester(
            platform_config,
            base_url,
            headers,
            model,
            runtime_config.ai_request_timeout,
            requester,
            max_retries=runtime_config.ai_request_max_retries,
        )
        history_file, history = _load_history(
            user_id,
            room_id=room_id,
            agent_id=agent_profile.id,
        )

        if _is_compact_command(content):
            if history:
                history = _compact_history_with_ai(requester, small_model, history)
                write_json_atomic(history_file, history)
                response_text = "历史记录已压缩。"
            else:
                response_text = "暂无可压缩的历史记录。"
            return success_response(message=None, content=response_text, token_count=None)

        if _history_needs_compaction(history):
            try:
                history = _compact_history_with_ai(requester, small_model, history)
                write_json_atomic(history_file, history)
            except Exception:
                logger.exception("Failed to compact chat history automatically")

        speaker = None if scenario_start else (_speaker_for_user(agent_context.room_info(), user_id) if room_id else None)
        user_content = _format_user_content(content, speaker)
        system_prompt = (
            _load_debug_kp_prompt()
            if runtime_config.debug_mode
            else agent_profile.prompt or _load_kp_prompt()
        )
        # 常驻行为约束：写入系统提示的静态层（global_rules），随每次请求重新注入，
        # 因此即便历史被压缩或截断，该设置也不会在多轮对话中丢失。
        # 仅当「全局提示开关开启且房间房规显式启用行动建议」时才允许给出提示；
        # 其余情况一律禁止主动提示、无故要求检定，以及“你要做什么：1…2…”式选项列表。
        if not effective_suggestions:
            system_prompt += (
                "\n【常驻约束·优先级仅次于系统规则】除非玩家在本轮明确请求提示、可行行动选项或要求进行检定，"
                "否则：不得主动提示、不得给出“你要做什么：1.… 2.…”这类行动列表或下一步建议；"
                "不得主动要求玩家进行检定（例如“请你过一个侦查检定”）；不得在回复结尾追加引导。"
                "只依据玩家已声明的行动进行叙事。"
            )
        # 兜底硬门控：房间已绑定剧本但尚未开始（房主/管理员未点击“开启剧本”）时，
        # 禁止 KP 自行开场/导入/推进剧本，也不能调用场景与触发器工具；最多提醒房主开场。
        # 正式开场（scenario_start=true）流程不受影响。
        if agent_profile.id == "kp" and room_id and not scenario_start:
            _start_gate_info = agent_context.room_info()
            if _start_gate_info.get("scenario_id") not in (None, "") and not _start_gate_info.get("scenario_started_at"):
                system_prompt += (
                    "\n【常驻约束】本房间剧本尚未开始。禁止自行开场、导入或推进剧本内容；"
                    "禁止调用场景工具（如 room.activate_scenario_scene）与触发器工具（trigger.reveal_scenario_trigger）；"
                    "最多用一句话提醒房主或管理员点击“开启剧本”。"
                )
        direct_import = False
        if room_id:
            system_prompt = (
                f"{system_prompt}\n"
                "最终回复必须是有效的 JSON 对象（JSON object），不要输出 JSON 之外的内容。"
                "所有工具调用必须使用接口原生的 tool_calls 字段，禁止把工具调用写进正文 JSON（例如 "
                "{\"tool_calls\": [...]}），也不要只描述“将要调用”某个工具而不真正调用。"
                "房间快照中的 scene_manifest/global_manifest 是唯一索引；摘要不是事实。若 sequential=true，优先按 order 顺序加载模块。"
                "仅在用户明确需要时按 ID 加载模块原文；转场先调用 room.activate_scenario_scene，再读模块。"
                "不得创造剧本未写出的地点、设施、NPC、道具或触发器内容；未提供就明确说明。"
                "剧本、房间快照、角色卡和工具返回值是唯一事实来源；不得把猜测写成既定事实。"
                "不要因问候、摘要或关键词自动检定/揭示/记忆；工具完成后立即简短叙事回复。"
                "本轮已注入房间快照、动态状态和剧本知识库检索结果，不要重复调用 room.get_room_snapshot；"
                "检索结果足够回答时不要再调用 room.get_scenario_context 或 room.get_scenario_module。"
                "明确检定时直接调用对应检定工具；得到结果后直接生成最终叙事，除非已知触发条件要求继续调用触发器。"
                "检定只能使用 COC7 标准属性（力量/敏捷/意志/教育等）或角色卡上已有的技能名，禁止编造技能目录中不存在的技能。"
                "禁止使用相同参数重复调用同一工具；互不依赖的工具应在同一轮并行调用。"
                "只有明确到达结局或满足剧本结束条件时，才在 state_updates 中设置 completed 或 ending_reached 为 true。"
            )
            scenario_summary = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else None
            scenario_summary = scenario_summary if isinstance(scenario_summary, dict) else {}
            direct_import = str(scenario_summary.get("import_mode") or "") == "direct" or bool(
                scenario_summary.get("found") and not scenario_summary.get("module_count")
            )
            if direct_import:
                system_prompt += (
                    "\n【直接导入剧本】本剧本以知识库检索块为唯一剧情来源，没有场景模块与 scene_manifest。"
                    "请勿调用 room.get_scenario_module 或 room.activate_scenario_scene，也不要因缺少模块而声称剧本缺失。"
                    "叙事必须严格依据下方「导入剧本目录」与每轮自动注入的 Knowledge retrieval 片段；"
                    "未被检索覆盖的细节应先说明需要检索，不得编造或自行推进剧情。"
                )

        prompt_history = _history_for_request(history)
        scene_static = None
        if isinstance(room_snapshot, dict):
            scenario_data = room_snapshot.get("scenario")
            if isinstance(scenario_data, dict):
                active_scene = scenario_data.get("active_scene_id")
                manifest = scenario_data.get("scene_manifest") if isinstance(scenario_data.get("scene_manifest"), list) else []
                scene_static = next((item for item in manifest if isinstance(item, dict) and str(item.get("id")) == str(active_scene)), None)
        scenario_results = []
        if room_id:
            _emit_thinking_stage(room_id, ai_request_id, "vector_search", "正在调用向量库")
            try:
                knowledge_service = KnowledgeBaseService(
                    rooms_dir=current_app.config.get("ROOMS_DIR", ROOMS_DIR),
                    scenarios_dir=current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR),
                    vector_store=current_app.extensions.get("vector_store"),
                    embedding_provider=current_app.extensions.get("embedding_provider"),
                )
                # 导入剧本只有知识块这一条剧情来源，多取几条降低"遗忘"概率；
                # 检索 query 带上最近几轮对话，避免泛化输入每次都落到同一批兜底块。
                retrieval_query = _retrieval_query(history, content)
                retrieval_top_k = (
                    runtime_config.retrieval_top_k_direct
                    if direct_import
                    else runtime_config.retrieval_top_k_default
                )
                scenario_results = knowledge_service.search(
                    room_id,
                    retrieval_query,
                    top_k=retrieval_top_k,
                    sticky_rounds=runtime_config.sticky_rounds,
                    cooldown_rounds=runtime_config.cooldown_rounds,
                    parent_max_chars=runtime_config.chunk_parent_max_chars,
                    keyword_channel=runtime_config.keyword_channel_enabled,
                    recursive_scanning=runtime_config.recursive_scanning,
                    max_recursion_depth=runtime_config.max_recursion_depth,
                    token_budget=runtime_config.lorebook_token_budget,
                )
                scenario_results = [
                    item for item in scenario_results
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                if direct_import:
                    catalog = knowledge_service.catalog(room_id)
                    if catalog:
                        # 目录是稳定前缀（可命中 provider 前缀缓存），但仍用紧凑行格式
                        # 而非 JSON，避免每行长键名把静态提示撑大、占用输入 token。
                        catalog_lines = "\n".join(
                            f"{item.get('chunk_id')}：{item.get('title')}" for item in catalog
                        )
                        system_prompt += (
                            "\n【导入剧本目录】以下是本剧本的全部章节索引（chunk_id：标题），请据此把握剧情走向；"
                            "需要某章细节时以每轮自动注入的 Knowledge retrieval 为准。\n"
                            + catalog_lines
                        )
            except (OSError, ValueError):
                logger.exception("Scenario retrieval failed")
        rules_version = str((agent_context.room_info().get("rulesets") or {}).get("coc7") or "1") if room_id else "1"
        projected_state = project_room_state(agent_context.room_state(), room_snapshot) if room_id else {}
        # 图谱运行态摘要：确定性渲染，不依赖检索命中；没有位置且没有回合时不注入。
        narrative_summary = ""
        if room_id:
            narrative = narrative_from_room_state(agent_context.room_state())
            narrative_bundle = None
            snapshot_room = (room_snapshot or {}).get("room") if isinstance(room_snapshot, dict) else {}
            scenario_id = snapshot_room.get("scenario_id") if isinstance(snapshot_room, dict) else None
            if scenario_id is not None and agent_context.scenarios_dir:
                try:
                    descriptor, _ = load_scenario_by_id(
                        agent_context.scenarios_dir,
                        scenario_id,
                        snapshot_room.get("scenario_version"),
                    )
                    if descriptor:
                        narrative_bundle = load_bundle(descriptor.parent)
                except (OSError, ValueError, NarrativeStoreError):
                    # 图谱文件损坏不应阻断对话；摘要退化为使用节点 id。
                    logger.warning("Narrative bundle load failed for room %s", room_id, exc_info=True)
            narrative_summary = render_state_summary(narrative, narrative_bundle)
        if effective_suggestions and isinstance(projected_state, dict):
            # Only AI-relevant active house rules reach the prompt, injected into
            # the dynamic room-state layer so the static prefix stays cacheable.
            projected_state["house_rules"] = {
                "ai_next_step_hints": "用 room.suggest_actions 返回；不要在正文列出选项"
            }
        prompt_layers = build_prompt_layers(
            global_rules=system_prompt,
            scenario=(room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else None,
            scene=scene_static,
            room_state=projected_state,
            history=prompt_history,
            user_input=user_content,
            rules_version=rules_version,
            retrieval_results=scenario_results,
            ruleset_results=[],
            retrieval_full=direct_import,
            room_snapshot_message=room_snapshot_message,
            narrative_summary=narrative_summary,
        )
        _emit_thinking_stage(room_id, ai_request_id, "vector_search_done", "向量库检索完成")
        # 提示词分段估算：把本次输入 token 拆到静态前缀/知识库/历史/本轮输入等分段，
        # 随用量一起落库。关了开关就跳过估算，避免不必要的额外开销。
        prompt_segments = (
            attribute_prompt_segments(prompt_layers.messages, prompt_layers.segments)
            if runtime_config.record_prompt_segments
            else {}
        )
        prefix_cache_hit = _PREFIX_CACHE.lookup(prompt_layers.cache_key)
        if prompt_segments:
            logger.debug(
                "prompt segments room_id=%s agent=%s segments=%s",
                room_id,
                agent_profile.id,
                prompt_segments,
            )
        scenario_info_for_cache = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
        scenario_info_for_cache = scenario_info_for_cache if isinstance(scenario_info_for_cache, dict) else {}
        state_for_cache = agent_context.room_state() if room_id else {}
        exact_cache_key = build_exact_response_key(
            scenario_info_for_cache.get("id") or "unknown",
            scenario_info_for_cache.get("scenario_version") or scenario_info_for_cache.get("version") or "1",
            scenario_info_for_cache.get("active_scene_id") or "unknown",
            stable_state_for_cache(state_for_cache),
            content,
        )
        semantic_state = {
            "room_id": str(room_id or "home"),
            "scenario_id": scenario_info_for_cache.get("id"),
            "scenario_version": scenario_info_for_cache.get("scenario_version") or scenario_info_for_cache.get("version") or "1",
            "active_scene_id": scenario_info_for_cache.get("active_scene_id"),
            # 指纹剔除 event_log 的随机 id / 时间戳等每轮都变的字段，否则本地缓存几乎永不命中。
            "state": stable_state_for_cache(state_for_cache),
        }
        # 只有「无副作用」的输入才允许走本地缓存：检定/手动触发/转场会推进剧情，
        # 复用旧回复会让 KP 忽略本次裁定与状态变化。这类输入一律直连上游。
        side_effect_requested = (
            _request_allows_check(content)
            or _request_allows_manual_trigger(content)
            or _request_allows_scene_transition(content)
        )
        cache_lookup_allowed = not scenario_start and not side_effect_requested
        cached_result = None
        if cache_lookup_allowed:
            if not room_id:
                cached_result = _EXACT_CACHE.get(exact_cache_key)
            elif runtime_config.semantic_cache_enabled:
                cached_result = _SEMANTIC_CACHE.get(content, semantic_state)
        if isinstance(cached_result, dict) and cached_result.get("content"):
            _emit_thinking_stage(room_id, ai_request_id, "finalizing", "正在整理回复")
            cached_knowledge_usage = cached_result.get("knowledge_usage") or {
                "used": bool(scenario_results),
                "total_chunks": len(scenario_results),
                "ruleset": {"called": False, "calls": 0, "chunks": 0, "sources": 0, "ruleset_ids": [], "knowledge_versions": [], "topics": [], "citations": []},
                "scenario": {"chunks": len(scenario_results)},
            }
            cached_knowledge_usage = {**cached_knowledge_usage, "cached": True}
            record_ai_usage(
                current_app.config.get("LOGS_DIR", LOGS_DIR),
                {
                    "agent_id": agent_profile.id,
                    "provider": selected_platform,
                    "model": model,
                    "room_id": str(room_id) if room_id else None,
                    "scenario_id": scenario_info_for_cache.get("id") or None,
                    "scenario_version": scenario_info_for_cache.get("scenario_version") or scenario_info_for_cache.get("version") or "1",
                    "scene_id": scenario_info_for_cache.get("active_scene_id") or None,
                    "cache_key": prompt_layers.cache_key,
                    "cache_layer": "semantic" if room_id else "exact",
                    "prefix_cache_hit": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                    # 本地缓存命中：没有请求上游，按 100% 命中记账。
                    "cache_hit_rate": 100.0,
                    "ruleset_ids": cached_knowledge_usage.get("ruleset", {}).get("ruleset_ids", []),
                    "knowledge_versions": cached_knowledge_usage.get("ruleset", {}).get("knowledge_versions", []),
                    "retrieval_topics": cached_knowledge_usage.get("ruleset", {}).get("topics", []),
                    "retrieval_chunk_count": cached_knowledge_usage.get("ruleset", {}).get("chunks", 0),
                    "retrieval_latency_ms": 0,
                    "retrieval_citations": cached_knowledge_usage.get("ruleset", {}).get("citations", []),
                    # 本地缓存命中的分段只作参考：没有调用上游，token 记为 0，分段占比
                    # 用于说明「如果没有缓存会花在哪里」。
                    "prompt_segments": prompt_segments,
                },
            )
            return success_response(
                message=None,
                content=cached_result["content"],
                token_count=cached_result.get("token_count"),
                prompt_tokens=0,
                completion_tokens=0,
                cached_tokens=0,
                cache_hit_rate=100.0,
                cache_key=prompt_layers.cache_key,
                prefix_cache_hit=True,
                knowledge_usage=cached_knowledge_usage,
                structured_output=cached_result.get("structured_output"),
            )
        request_data = {
            "messages": prompt_layers.messages,
            "model": model,
            "temperature": 0.5,
            "top_p": 0.85,
        }
        if room_id:
            request_data["metadata"] = {
                **request_data.get("metadata", {}),
                "session_id": _room_session_id(room_id, agent_profile.id),
                "room_id": str(room_id),
                "agent_id": agent_profile.id,
                "cache_key": prompt_layers.cache_key,
            }
            # 不要在此设置 response_format=json_object：它和原生工具调用（tools +
            # tool_choice）互斥。强制 JSON 输出会让部分厂商把本该放进 tool_calls 字段
            # 的调用塞进正文 JSON（{"reply": ..., "tool_calls": [...]}），
            # trpg_server.agents.runtime._tool_calls 只认原生字段，于是工具被静默丢弃、
            # 原始 JSON 反而被当成 KP 回复展示。
        if runtime_config.stream_output:
            request_data["stream"] = False

        log_room_event(
            message=user_action_text(session.get("username") or user_id, "开始 AI 对话"),
            actor=session.get("username") or user_id,
            room_id=room_id,
            rooms_dir=current_app.config.get("ROOMS_DIR"),
            logger_=logger,
            用户ID=session.get("user_id") or user_id,
            角色=role_config.get("id"),
            平台=selected_platform,
            模型=request_data["model"],
            内容长度=len(content),
        )
        # 房间内 AI 对话：按管理员拖动排序后的顺序加载启用平台；首个平台无法加载
        # （报错、余额不足等）时自动切换到下一个启用平台，同一时刻只使用一个平台。
        # 切换原因推送给在线管理员。非房间场景（模块摘要、剧本导入等）仍只用首选平台。
        attempt_platforms = [(selected_platform, platform_config)]
        if room_id:
            for _candidate_id, _candidate_config in _load_enabled_platforms(agent_profile.provider):
                if _candidate_id != selected_platform:
                    attempt_platforms.append((_candidate_id, _candidate_config))

        result = None
        last_error = None
        elapsed_ms = 0.0
        for attempt_index, (attempt_platform, attempt_config) in enumerate(attempt_platforms):
            next_platform = (
                attempt_platforms[attempt_index + 1][0]
                if attempt_index + 1 < len(attempt_platforms)
                else None
            )
            if attempt_index == 0:
                attempt_state = {"model": model, "agent_requester": agent_requester}
            else:
                attempt_state = _build_platform_request_state(
                    attempt_platform, attempt_config, runtime_config, on_retry=notify_ai_retry
                )
            if not attempt_state:
                last_error = f"AI 平台「{attempt_platform}」配置不完整"
                if next_platform:
                    _notify_platform_switch(attempt_platform, next_platform, "配置不完整")
                continue

            request_data["model"] = attempt_state["model"]
            # 请求模板（default-request.json + aimodel/<平台>/<模型>.json）作为默认参数并入；
            # 本层的 messages/model/temperature/top_p 等运行时字段优先。输出长度只由全局
            # 「是否约束最大 token」开关决定，未开启时不下发 max_tokens（把上限交给平台）。
            attempt_payload = merge_request_template(
                request_data,
                attempt_platform,
                attempt_state["model"],
                runtime_config=runtime_config,
                config_dir=_get_config_dir(),
            )
            started_at = time.perf_counter()
            attempt_result = run_agent_completion(
                requester=attempt_state["agent_requester"],
                base_payload=attempt_payload,
                profile=request_profile,
                registry=default_tool_registry(),
                context=agent_context,
                max_tool_rounds=runtime_config.max_tool_rounds,
                max_tool_result_chars=runtime_config.max_tool_result_chars,
                dedupe_tool_calls=runtime_config.dedupe_tool_calls,
                excluded_tools=(
                    {"room.get_scenario_module", "room.activate_scenario_scene"} if direct_import else None
                ),
            )
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

            if attempt_result.error:
                last_error = attempt_result.error
                logger.warning(
                    "AI agent request failed platform=%s room_id=%s user_id=%s error=%s",
                    attempt_platform,
                    room_id,
                    session.get("user_id"),
                    attempt_result.error,
                )
                if next_platform:
                    _notify_platform_switch(attempt_platform, next_platform, attempt_result.error)
                    continue
                break

            result = attempt_result
            selected_platform = attempt_platform
            model = attempt_state["model"]
            break

        if result is None:
            _emit_thinking_stage(room_id, ai_request_id, "error", "AI 请求失败")
            return error_response(last_error or "AI agent request failed", 500, "AI agent request failed")

        if result.error:
            logger.warning(
                "AI agent request failed room_id=%s user_id=%s error=%s",
                room_id,
                session.get("user_id"),
                result.error,
            )
            _emit_thinking_stage(room_id, ai_request_id, "error", "AI 请求失败")
            # 把具体原因（超时/连接失败等）作为 message 返回，前端会直接展示在聊天框内。
            return error_response(result.error or "AI agent request failed", 500, "AI agent request failed")
        if result.rounds_exhausted:
            logger.warning(
                "AI agent reached max tool rounds room_id=%s user_id=%s rounds=%s",
                room_id,
                session.get("user_id"),
                runtime_config.max_tool_rounds,
            )

        knowledge_usage = _build_knowledge_usage(result, scenario_results)

        _emit_thinking_stage(room_id, ai_request_id, "finalizing", "正在整理回复")

        prompt_tokens = result.prompt_token_count or 0
        completion_tokens = result.completion_token_count or 0
        total_tokens = result.token_count or (prompt_tokens + completion_tokens)
        cached_tokens = result.cached_token_count or 0
        cache_hit_rate = calculate_cache_hit_rate(prompt_tokens, cached_tokens)
        scenario_info = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
        if not isinstance(scenario_info, dict):
            scenario_info = {}
        cache_key = build_provider_cache_key(
            scenario_info.get("id") or "unknown",
            scenario_info.get("version") or "1",
            scenario_info.get("active_scene_id") or "unknown",
            scenario_info.get("scene_version") or "1",
            "1",
        )
        usage_record = record_ai_usage(
            current_app.config.get("LOGS_DIR", LOGS_DIR),
            {
                "agent_id": agent_profile.id,
                "provider": selected_platform,
                "model": request_data["model"],
                "room_id": str(room_id) if room_id else None,
                "scene_id": scenario_info.get("active_scene_id") if isinstance(scenario_info, dict) else None,
                "scenario_id": scenario_info.get("id") if isinstance(scenario_info, dict) else None,
                "scenario_version": (scenario_info.get("scenario_version") or scenario_info.get("version")) if isinstance(scenario_info, dict) else None,
                "cache_key": cache_key,
                "prefix_cache_hit": prefix_cache_hit,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cached_tokens": cached_tokens,
                "elapsed_ms": elapsed_ms,
                "ruleset_ids": knowledge_usage["ruleset"]["ruleset_ids"],
                "knowledge_versions": knowledge_usage["ruleset"]["knowledge_versions"],
                "retrieval_topics": knowledge_usage["ruleset"]["topics"],
                "retrieval_chunk_count": knowledge_usage["ruleset"]["chunks"],
                "retrieval_latency_ms": ruleset_metrics["latency_ms"],
                "retrieval_citations": knowledge_usage["ruleset"]["citations"],
                "scenario_retrieval_chunk_count": knowledge_usage["scenario"]["chunks"],
                "agent_request_rounds": int(agent_context.tool_state.get("agent_request_rounds") or 0),
                "agent_tool_calls": list(agent_context.tool_state.get("tool_call_trace") or []),
                "prompt_segments": prompt_segments,
            },
        )

        direct_message = None
        if isinstance(result.response_data, dict):
            direct_message = result.response_data.get("direct_message")
        direct_messages = result.direct_messages or []
        if direct_message and not direct_messages:
            direct_messages = [direct_message]

        # Action options staged by the KP via room.suggest_actions. These are
        # rendered as buttons in the chat UI (owner-interactive, read-only for
        # other room members) and never persisted as room messages.
        suggestions = []
        staged_suggestions = agent_context.tool_state.get("suggested_actions")
        if isinstance(staged_suggestions, list):
            suggestions = [str(item) for item in staged_suggestions if str(item).strip()]

        structured = parse_kp_response(result.content)
        if structured:
            structured = validate_structured_response(structured, (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {})
        validated_updates = {}
        delivered_structured_triggers = []
        ending_reached = False
        if structured:
            ai_response = structured.narration
            if room_id and structured.state_updates:
                scenario_data = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
                validated_updates = validate_state_updates(
                    structured.state_updates,
                    agent_context.room_state(),
                    scenario_data,
                )
                if validated_updates:
                    apply_state_updates(agent_context.room_dir, validated_updates)
                    append_room_event(
                        agent_context.room_dir,
                        {"kind": "state_update", "content": "AI validated state update", "metadata": validated_updates},
                    )
            if structured.next_scene and room_id and "active_scene_id" not in validated_updates:
                scenario_data = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
                next_updates = validate_state_updates(
                    {"active_scene_id": structured.next_scene},
                    agent_context.room_state(),
                    scenario_data,
                )
                if next_updates:
                    apply_state_updates(agent_context.room_dir, next_updates)
                    validated_updates.update(next_updates)
            if room_id and validated_updates.get("active_scene_id"):
                _sync_room_active_scene(
                    agent_context.room_dir,
                    validated_updates["active_scene_id"],
                    (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else None,
                )
            # Reaching an ending is the only automatic completion signal.  It
            # enables the manager-facing archive action without deleting a
            # still-running room.
            if room_id and agent_context.room_dir:
                ending_id = validated_updates.get("active_scene_id") or structured.next_scene
                scenario_for_completion = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
                manifest = scenario_for_completion.get("scene_manifest", []) if isinstance(scenario_for_completion, dict) else []
                if validated_updates.get("completed") or validated_updates.get("ending_reached") or (ending_id and any(isinstance(item, dict) and str(item.get("id") or item.get("module_id")) == str(ending_id) and str(item.get("type") or "").lower() == "ending" for item in manifest)):
                    ending_reached = True
                    room_info_path = agent_context.room_dir / "info.json"
                    room_info = read_json(room_info_path, default={})
                    if isinstance(room_info, dict) and not room_info.get("completed_at"):
                        room_info["completed_at"] = _timestamp()
                        room_info["completed_by"] = "ai"
                        write_json_atomic(room_info_path, room_info)
            if structured.triggered_files and agent_context.room_dir:
                scenario_data = (room_snapshot or {}).get("scenario") if isinstance(room_snapshot, dict) else {}
                room_info = (room_snapshot or {}).get("room") if isinstance(room_snapshot, dict) and isinstance((room_snapshot or {}).get("room"), dict) else {}
                delivered_structured_triggers = _deliver_structured_triggers(agent_context.room_dir, room_info, scenario_data, structured.triggered_files)
                if delivered_structured_triggers:
                    direct_messages.extend(delivered_structured_triggers)
        else:
            ai_response, _ = _strip_compact_command(result.content)
        _, compact_requested_by_ai = _strip_compact_command(result.content)
        token_count = result.token_count
        if not ai_response and compact_requested_by_ai:
            ai_response = "历史记录已压缩。"
        # A few providers terminate after a successful tool call without a
        # second assistant message. Keep the tool/direct message visible and
        # return a normal chat response instead of surfacing a false failure.
        if not ai_response and (result.direct_messages or result.tool_messages):
            ai_response = "已完成检定/场景处理，详情见上方记录。"
        if not ai_response and suggestions:
            ai_response = "请从下方选择你的行动。"
        # 非致命异常（达到最大工具轮数、请求中断）也要在聊天框内可见，
        # 而不是让玩家面对一条没有任何说明的回复。
        if result.notice:
            ai_response = f"{ai_response}\n\n{result.notice}" if ai_response else result.notice
        if not ai_response:
            return error_response(
                "AI platform did not return a response",
                400,
                "No response",
            )
        log_room_event(
            message=user_action_text(session.get("username") or user_id, "收到 AI 回复"),
            actor=session.get("username") or user_id,
            room_id=room_id,
            rooms_dir=current_app.config.get("ROOMS_DIR"),
            logger_=logger,
            用户ID=session.get("user_id") or user_id,
            平台=selected_platform,
            模型=request_data["model"],
            回复长度=len(ai_response),
            Token数=token_count,
            输入Token=prompt_tokens,
            输出Token=completion_tokens,
            缓存Token=cached_tokens,
            缓存命中率=cache_hit_rate,
            耗时毫秒=elapsed_ms,
        )

        user_history_item = {"role": "system" if scenario_start else "user", "content": content}
        if speaker:
            user_history_item["speaker"] = speaker
        history.extend([user_history_item, {"role": "assistant", "content": ai_response}])
        if not result.tool_messages and not direct_messages and not validated_updates and structured is None and not suggestions:
            # 有副作用的输入（检定/触发/转场）即使本次没有工具产出，也不写缓存：
            # 下一次相同输入很可能是新的裁定，复用旧叙事会削弱剧本遵守度。
            cache_value = {"content": ai_response, "token_count": token_count, "structured_output": None, "knowledge_usage": knowledge_usage}
            if room_id:
                if runtime_config.semantic_cache_enabled and not side_effect_requested:
                    _SEMANTIC_CACHE.set(content, semantic_state, cache_value)
            elif not side_effect_requested:
                _EXACT_CACHE.set(exact_cache_key, cache_value)
        _maybe_remember_important_action(content, agent_context)
        if compact_requested_by_ai:
            try:
                history = _compact_history_with_ai(requester, small_model, history)
                write_json_atomic(history_file, history)
            except Exception:
                logger.exception("Failed to compact chat history after AI request")
                write_json_atomic(history_file, history[-20:])
        else:
            write_json_atomic(history_file, history[-20:])

        scenario_started_at = None
        if scenario_start and agent_context.room_dir:
            scenario_started_at = _mark_scenario_started(agent_context.room_dir, session.get("user_id"))
            logger.info("scenario_started room_id=%s user_id=%s", room_id, session.get("user_id"))

        return success_response(
            message=None,
            content=ai_response,
            token_count=token_count,
            direct_message=direct_messages[0] if direct_messages else None,
            direct_messages=direct_messages,
            tool_messages=result.tool_messages or [],
            suggestions=suggestions,
            suggestions_owner=user_id if suggestions else None,
            prompt_tokens=usage_record["prompt_tokens"],
            completion_tokens=usage_record["completion_tokens"],
            cached_tokens=usage_record["cached_tokens"],
            cache_hit_rate=usage_record["cache_hit_rate"],
            elapsed_ms=usage_record["elapsed_ms"],
            cache_key=cache_key,
            prefix_cache_hit=prefix_cache_hit,
            knowledge_usage=knowledge_usage,
            scenario_started_at=scenario_started_at,
            ending_reached=ending_reached,
            structured_output=(
                {
                    "options": structured.options,
                    "state_updates": validated_updates,
                    "next_scene": structured.next_scene,
                    "npc_actions": structured.npc_actions,
                    "triggered_files": structured.triggered_files,
                }
                if structured
                else None
            ),
        )
    except requests.exceptions.Timeout:
        return error_response("AI platform request timeout", 504, "Request timeout")
    except requests.exceptions.ConnectionError:
        return error_response("Cannot connect to AI platform", 503, "Connection error")
    except json.JSONDecodeError as exc:
        logger.exception("Failed to parse AI platform response")
        return server_error("Failed to parse AI platform response")
    except Exception as exc:
        logger.exception("Chat request failed")
        return server_error("Chat request failed")


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
