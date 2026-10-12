import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AIRuntimeConfig:
    stream_output: bool = False
    debug_mode: bool = False
    show_ai_hints: bool = True
    small_model_tasks: dict[str, str] = field(default_factory=lambda: {
        "intent_classification": "",
        "state_update": "",
        "summarization": "",
        "schema_repair": "",
    })
    # Prompt-compaction knobs. These bound how much *redundant* context is sent
    # to the model without capping tool-call rounds or the response max_tokens.
    snapshot_manifest_summary_cap: int = 140
    max_snapshot_scene_manifest: int = 30
    snapshot_include_entity_manifest: bool = False
    max_tool_result_chars: int = 4000
    # 同一轮 KP 发言里，用相同参数重复调用同一工具时是否短路复用上次结果。
    # 避免重复掷骰/重复触发造成状态错乱，也省掉重复执行的往返与 token。
    dedupe_tool_calls: bool = True
    # 本地响应缓存：命中即跳过整次上游请求，是最直接的省 token 手段。
    # TTL 越大命中越多，但过期内容被复用的窗口也越长；两者都可从设置页调整。
    semantic_cache_enabled: bool = True
    semantic_cache_ttl: int = 120
    exact_cache_ttl: int = 300
    # KP 单次发言允许的最大工具调用轮数，以及每次 AI 请求的等待时间（秒）。
    # 超过后不会静默丢回复，而是给出明确的收尾叙事与提示。
    max_tool_rounds: int = 8
    ai_request_timeout: int = 300
    # 连接被网络层中断（防火墙/NAT 空闲超时等）时，单次 AI 请求最多重试几次。
    ai_request_max_retries: int = 5
    # 是否把每次请求的提示词按分段估算 token 并记入用量日志，供成本归因与仪表盘展示。
    record_prompt_segments: bool = True
    # 骰娘大成功 / 大失败的全局默认阈值（管理员设置页可改，房规可覆盖）。
    dice_critical_threshold: int = 1
    dice_fumble_threshold: int = 96
    # 知识库检索与切块。``top_k_*`` 现在表示「返回的章节数」：命中后会把同章节的
    # 相邻小块回填成完整章节，因此数值比按块召回时可以更小。
    # 直接导入的剧本只有知识块这一条剧情来源，因此召回数可单独放大。
    retrieval_top_k_default: int = 5
    retrieval_top_k_direct: int = 6
    # 子块用于精确召回，父块上限用于命中后回填章节上下文。
    chunk_child_max_chars: int = 800
    chunk_parent_max_chars: int = 2400
    # 章节粘滞/冷却的默认时长：关键词激活的条目在 sticky_rounds 轮内强制注入，
    # 随后进入 cooldown_rounds 轮硬冷却（条目可用自身字段覆盖）。
    sticky_rounds: int = 1
    cooldown_rounds: int = 3
    # 世界书（lorebook）通道：关键词直命中、递归扫描，以及可选的注入 token 预算。
    # token 预算 0 表示不限制（默认），仅在为超大剧本显式开启时按排序丢弃低优先级条目。
    keyword_channel_enabled: bool = True
    recursive_scanning: bool = True
    max_recursion_depth: int = 3
    lorebook_token_budget: int = 0


def _toml_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _unquote_toml_name(raw: str) -> str:
    """去掉段名 / 键名外层引号（如 `["ai.small_models"]` 中的引号）。

    手写解析器按行切分，若不还原引号，点号段名（``ai.small_models``）会永远匹配不上。
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            unquoted = json.loads(text)
        except ValueError:
            return text
        return unquoted if isinstance(unquoted, str) else text
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return text


def ai_debug_enabled(config_dir: str | Path | None = None) -> bool:
    """是否开启 AI 调试模式。

    完整 AI 请求/响应会包含整段提示词与模型输出，只有显式开启调试模式时才写日志，
    避免正常运行（尤其是大剧本导入）把日志刷满。
    """
    if config_dir is None:
        from trpg_server.settings import CONFIG_DIR

        config_dir = CONFIG_DIR
    return load_ai_runtime_config(Path(config_dir)).debug_mode


def load_ai_runtime_config(config_dir: Path) -> AIRuntimeConfig:
    general_file = Path(config_dir) / "general.toml"
    if not general_file.exists():
        return AIRuntimeConfig()

    current_section = ""
    stream_output = False
    debug_mode = False
    show_ai_hints = True
    small_model_tasks = AIRuntimeConfig().small_model_tasks.copy()
    snapshot_manifest_summary_cap = AIRuntimeConfig.snapshot_manifest_summary_cap
    max_snapshot_scene_manifest = AIRuntimeConfig.max_snapshot_scene_manifest
    snapshot_include_entity_manifest = AIRuntimeConfig.snapshot_include_entity_manifest
    max_tool_result_chars = AIRuntimeConfig.max_tool_result_chars
    dedupe_tool_calls = AIRuntimeConfig.dedupe_tool_calls
    semantic_cache_enabled = AIRuntimeConfig.semantic_cache_enabled
    semantic_cache_ttl = AIRuntimeConfig.semantic_cache_ttl
    exact_cache_ttl = AIRuntimeConfig.exact_cache_ttl
    max_tool_rounds = AIRuntimeConfig.max_tool_rounds
    ai_request_timeout = AIRuntimeConfig.ai_request_timeout
    ai_request_max_retries = AIRuntimeConfig.ai_request_max_retries
    record_prompt_segments = AIRuntimeConfig.record_prompt_segments
    dice_critical_threshold = AIRuntimeConfig.dice_critical_threshold
    dice_fumble_threshold = AIRuntimeConfig.dice_fumble_threshold
    retrieval_top_k_default = AIRuntimeConfig.retrieval_top_k_default
    retrieval_top_k_direct = AIRuntimeConfig.retrieval_top_k_direct
    chunk_child_max_chars = AIRuntimeConfig.chunk_child_max_chars
    chunk_parent_max_chars = AIRuntimeConfig.chunk_parent_max_chars
    sticky_rounds = AIRuntimeConfig.sticky_rounds
    cooldown_rounds = AIRuntimeConfig.cooldown_rounds
    keyword_channel_enabled = AIRuntimeConfig.keyword_channel_enabled
    recursive_scanning = AIRuntimeConfig.recursive_scanning
    max_recursion_depth = AIRuntimeConfig.max_recursion_depth
    lorebook_token_budget = AIRuntimeConfig.lorebook_token_budget
    for raw_line in general_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = _unquote_toml_name(line[1:-1])
            continue
        if current_section in {"ai", "ai.small_models", "ai.prompt", "ai.knowledge", "ai.cache"} and "=" in line:
            key, value = [part.strip() for part in line.split("=", 1)]
            key = _unquote_toml_name(key)
            quoted_value = bool(value.startswith(('"', "'")))
            if quoted_value and value.endswith(('"', "'")):
                value = value[1:-1]
            if current_section == "ai.small_models":
                small_model_tasks[key] = value
            elif current_section == "ai.prompt":
                if key == "snapshot_manifest_summary_cap":
                    snapshot_manifest_summary_cap = max(0, _toml_int(value, AIRuntimeConfig.snapshot_manifest_summary_cap))
                elif key == "max_snapshot_scene_manifest":
                    max_snapshot_scene_manifest = max(0, _toml_int(value, AIRuntimeConfig.max_snapshot_scene_manifest))
                elif key == "snapshot_include_entity_manifest":
                    snapshot_include_entity_manifest = value.lower() == "true"
                elif key == "max_tool_result_chars":
                    max_tool_result_chars = max(500, _toml_int(value, AIRuntimeConfig.max_tool_result_chars))
                elif key == "dedupe_tool_calls":
                    dedupe_tool_calls = value.lower() == "true"
            elif current_section == "ai.cache":
                if key == "semantic_enabled":
                    semantic_cache_enabled = value.lower() == "true"
                elif key == "semantic_ttl_seconds":
                    semantic_cache_ttl = max(30, min(86400, _toml_int(value, AIRuntimeConfig.semantic_cache_ttl)))
                elif key == "exact_ttl_seconds":
                    exact_cache_ttl = max(30, min(86400, _toml_int(value, AIRuntimeConfig.exact_cache_ttl)))
            elif current_section == "ai.knowledge":
                if key == "top_k_default":
                    retrieval_top_k_default = max(1, min(32, _toml_int(value, AIRuntimeConfig.retrieval_top_k_default)))
                elif key == "top_k_direct":
                    retrieval_top_k_direct = max(1, min(32, _toml_int(value, AIRuntimeConfig.retrieval_top_k_direct)))
                elif key == "chunk_child_max_chars":
                    chunk_child_max_chars = max(200, min(8000, _toml_int(value, AIRuntimeConfig.chunk_child_max_chars)))
                elif key == "chunk_parent_max_chars":
                    chunk_parent_max_chars = max(200, min(16000, _toml_int(value, AIRuntimeConfig.chunk_parent_max_chars)))
                elif key == "sticky_rounds":
                    sticky_rounds = max(0, min(10, _toml_int(value, AIRuntimeConfig.sticky_rounds)))
                elif key == "cooldown_rounds":
                    cooldown_rounds = max(0, min(20, _toml_int(value, AIRuntimeConfig.cooldown_rounds)))
                elif key == "keyword_channel_enabled":
                    keyword_channel_enabled = value.lower() == "true"
                elif key == "recursive_scanning":
                    recursive_scanning = value.lower() == "true"
                elif key == "max_recursion_depth":
                    max_recursion_depth = max(0, min(10, _toml_int(value, AIRuntimeConfig.max_recursion_depth)))
                elif key == "lorebook_token_budget":
                    lorebook_token_budget = max(0, min(200000, _toml_int(value, AIRuntimeConfig.lorebook_token_budget)))
            elif key == "stream_output":
                stream_output = value.lower() == "true"
            elif key == "debug_mode":
                debug_mode = value.lower() == "true"
            elif key in {"show_ai_hints", "show_kp_hints", "reply_hints"}:
                show_ai_hints = value.lower() == "true"
            elif key == "max_tool_rounds":
                max_tool_rounds = max(1, min(30, _toml_int(value, AIRuntimeConfig.max_tool_rounds)))
            elif key == "ai_request_timeout":
                ai_request_timeout = max(30, min(1800, _toml_int(value, AIRuntimeConfig.ai_request_timeout)))
            elif key == "ai_request_max_retries":
                ai_request_max_retries = max(0, min(20, _toml_int(value, AIRuntimeConfig.ai_request_max_retries)))
            elif key == "record_prompt_segments":
                record_prompt_segments = value.lower() == "true"
            elif key == "dice_critical_threshold":
                dice_critical_threshold = max(0, min(100, _toml_int(value, AIRuntimeConfig.dice_critical_threshold)))
            elif key == "dice_fumble_threshold":
                dice_fumble_threshold = max(0, min(100, _toml_int(value, AIRuntimeConfig.dice_fumble_threshold)))
    return AIRuntimeConfig(
        stream_output=stream_output,
        debug_mode=debug_mode,
        show_ai_hints=show_ai_hints,
        small_model_tasks=small_model_tasks,
        snapshot_manifest_summary_cap=snapshot_manifest_summary_cap,
        max_snapshot_scene_manifest=max_snapshot_scene_manifest,
        snapshot_include_entity_manifest=snapshot_include_entity_manifest,
        max_tool_result_chars=max_tool_result_chars,
        dedupe_tool_calls=dedupe_tool_calls,
        semantic_cache_enabled=semantic_cache_enabled,
        semantic_cache_ttl=semantic_cache_ttl,
        exact_cache_ttl=exact_cache_ttl,
        max_tool_rounds=max_tool_rounds,
        ai_request_timeout=ai_request_timeout,
        ai_request_max_retries=ai_request_max_retries,
        record_prompt_segments=record_prompt_segments,
        dice_critical_threshold=dice_critical_threshold,
        dice_fumble_threshold=dice_fumble_threshold,
        retrieval_top_k_default=retrieval_top_k_default,
        retrieval_top_k_direct=retrieval_top_k_direct,
        chunk_child_max_chars=chunk_child_max_chars,
        chunk_parent_max_chars=chunk_parent_max_chars,
        sticky_rounds=sticky_rounds,
        cooldown_rounds=cooldown_rounds,
        keyword_channel_enabled=keyword_channel_enabled,
        recursive_scanning=recursive_scanning,
        max_recursion_depth=max_recursion_depth,
        lorebook_token_budget=lorebook_token_budget,
    )
