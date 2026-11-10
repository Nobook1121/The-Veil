"""配置 schema：代码默认值（L1 兜底）与权威字段清单。

``config/templates/general.toml``（L2）的每个键都必须在这里有对应规格；
新增配置项只需在 ``SPECS`` 加一行并同步模板。``AIRuntimeConfig`` 是 AI 段的
类型化视图，字段默认值即代码兜底值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trpg_server.config.fields import (
    FieldSpec,
    as_bool,
    as_enum,
    as_list,
    as_str_list,
    as_text,
    clamped_int,
)

# network.port 的代码默认值（L1）。``settings.DEFAULT_PORT`` 由此派生，避免两处漂移。
DEFAULT_NETWORK_PORT = 8086


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
        "narrative_fill": "",
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
    # 所有平台的「最大输出 token」默认值。开启约束时，未在请求模板里自行声明的请求
    # 一律按此值下发 max_tokens；模板里显式写了 max_tokens 的则以模板为准。
    # 默认取一个足够宽裕的值，避免用压缩输出预算的方式省 token（那样容易截断回复或挤空
    # 推理正文）；探针/连通性测试始终使用极小值，不受此影响。
    constrain_max_tokens: bool = True
    max_tokens: int = 16384
    # 是否把每次请求的提示词按分段估算 token 并记入用量日志，供成本归因与仪表盘展示。
    record_prompt_segments: bool = True
    # 骰娘大成功 / 大失败的全局默认阈值（管理员设置页可改，房规可覆盖）。
    dice_critical_threshold: int = 1
    dice_fumble_threshold: int = 96
    # 骰子动画全局默认档位：text（纯文本）/ simple（简易动画）/ 3d（3D 物理骰子）。
    # 房间可在房规内覆盖；两者相互独立，玩家投掷不依赖动画档位。
    dice_animation_level: str = "3d"
    # 骰子结果来源：开=读取骰子落定后朝上的面；关=沿用服务端随机数生成器。
    # 关闭后旧生成器同时负责纯文本档位与所有兜底场景。
    dice_physics_results: bool = True
    # 是否允许 AI 检定等待玩家点击投掷（全局默认，房规可覆盖）。
    dice_player_roll: bool = True
    # 等待玩家投掷的最长秒数，超时后由服务端自动投掷。
    dice_player_roll_timeout: int = 120
    # 玩家投掷的重投模式：off（关闭）/ limited（指定次数）/ unlimited（无限）。
    # 仅影响玩家投掷流程；自动投掷恒为关闭。
    dice_reroll_mode: str = "off"
    # ``limited`` 模式下的重投次数上限（1-20）。
    dice_reroll_limit: int = 3
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
    # 云端 embedding 接口（OpenAI 兼容）默认关闭。只有管理员在设置页显式开启后，
    # 才会在本地模型不可用时改用 AI 平台配置里的 embedding 接口；关闭时一律回落到
    # 本地模型（可用的话）或确定性的哈希向量，避免静默产生云端调用费用。
    cloud_embedding_enabled: bool = False
    # 首次部署引导「程序自动下载」使用的模型仓库（HuggingFace ``owner/name``）。
    # 默认值只是兜底，实际取值以 general.toml 的 [ai] 段为准；引导页会把该值显示给
    # 用户，因此前端文案里不要重复写死仓库名。
    local_embedding_repo: str = "BAAI/bge-small-zh-v1.5"
    # 长期记忆巩固：从房间事件日志/AI 记忆/关键节点/对话归档中按相关性挑选少量回顾，
    # 每轮注入受字符预算约束，不注入全量历史。预算为 0 表示关闭注入。
    memory_enabled: bool = True
    memory_inject_budget_chars: int = 800
    memory_inject_max_items: int = 6


_AI_DEFAULTS = AIRuntimeConfig()


def _ai(section: str, name: str, keys: tuple[str, ...], convert: Any) -> FieldSpec:
    return FieldSpec(name, section, keys, convert, getattr(_AI_DEFAULTS, name))


# AI 段权威清单（自 agents/config.py 迁入）。新增 AI 配置项只需加一行。
AI_SPECS: tuple[FieldSpec, ...] = (
    # [ai]
    _ai("ai", "stream_output", ("stream_output",), as_bool),
    _ai("ai", "debug_mode", ("debug_mode",), as_bool),
    _ai("ai", "show_ai_hints", ("show_ai_hints", "show_kp_hints", "reply_hints"), as_bool),
    _ai("ai", "max_tool_rounds", ("max_tool_rounds",), clamped_int(minimum=1, maximum=30)),
    _ai("ai", "ai_request_timeout", ("ai_request_timeout",), clamped_int(minimum=30, maximum=1800)),
    _ai("ai", "ai_request_max_retries", ("ai_request_max_retries",), clamped_int(minimum=0, maximum=20)),
    _ai("ai", "constrain_max_tokens", ("constrain_max_tokens",), as_bool),
    _ai("ai", "max_tokens", ("max_tokens",), clamped_int(minimum=1, maximum=200000)),
    _ai("ai", "record_prompt_segments", ("record_prompt_segments",), as_bool),
    _ai("ai", "cloud_embedding_enabled", ("cloud_embedding_enabled",), as_bool),
    _ai("ai", "local_embedding_repo", ("local_embedding_repo",), as_text),
    _ai("ai", "dice_critical_threshold", ("dice_critical_threshold",), clamped_int(minimum=0, maximum=100)),
    _ai("ai", "dice_fumble_threshold", ("dice_fumble_threshold",), clamped_int(minimum=0, maximum=100)),
    _ai("ai", "dice_animation_level", ("dice_animation_level",),
        as_enum("dice_animation_level", frozenset({"text", "simple", "3d"}))),
    _ai("ai", "dice_physics_results", ("dice_physics_results",), as_bool),
    _ai("ai", "dice_player_roll", ("dice_player_roll",), as_bool),
    _ai("ai", "dice_player_roll_timeout", ("dice_player_roll_timeout",), clamped_int(minimum=30, maximum=300)),
    _ai("ai", "dice_reroll_mode", ("dice_reroll_mode",),
        as_enum("dice_reroll_mode", frozenset({"off", "limited", "unlimited"}))),
    _ai("ai", "dice_reroll_limit", ("dice_reroll_limit",), clamped_int(minimum=1, maximum=20)),
    # [ai.prompt]
    _ai("ai.prompt", "snapshot_manifest_summary_cap", ("snapshot_manifest_summary_cap",), clamped_int(minimum=0)),
    _ai("ai.prompt", "max_snapshot_scene_manifest", ("max_snapshot_scene_manifest",), clamped_int(minimum=0)),
    _ai("ai.prompt", "snapshot_include_entity_manifest", ("snapshot_include_entity_manifest",), as_bool),
    _ai("ai.prompt", "max_tool_result_chars", ("max_tool_result_chars",), clamped_int(minimum=500)),
    _ai("ai.prompt", "dedupe_tool_calls", ("dedupe_tool_calls",), as_bool),
    # [ai.cache]
    _ai("ai.cache", "semantic_cache_enabled", ("semantic_enabled",), as_bool),
    _ai("ai.cache", "semantic_cache_ttl", ("semantic_ttl_seconds",), clamped_int(minimum=30, maximum=86400)),
    _ai("ai.cache", "exact_cache_ttl", ("exact_ttl_seconds",), clamped_int(minimum=30, maximum=86400)),
    # [ai.knowledge]
    _ai("ai.knowledge", "retrieval_top_k_default", ("top_k_default",), clamped_int(minimum=1, maximum=32)),
    _ai("ai.knowledge", "retrieval_top_k_direct", ("top_k_direct",), clamped_int(minimum=1, maximum=32)),
    _ai("ai.knowledge", "chunk_child_max_chars", ("chunk_child_max_chars",), clamped_int(minimum=200, maximum=8000)),
    _ai("ai.knowledge", "chunk_parent_max_chars", ("chunk_parent_max_chars",), clamped_int(minimum=200, maximum=16000)),
    _ai("ai.knowledge", "sticky_rounds", ("sticky_rounds",), clamped_int(minimum=0, maximum=10)),
    _ai("ai.knowledge", "cooldown_rounds", ("cooldown_rounds",), clamped_int(minimum=0, maximum=20)),
    _ai("ai.knowledge", "keyword_channel_enabled", ("keyword_channel_enabled",), as_bool),
    _ai("ai.knowledge", "recursive_scanning", ("recursive_scanning",), as_bool),
    _ai("ai.knowledge", "max_recursion_depth", ("max_recursion_depth",), clamped_int(minimum=0, maximum=10)),
    _ai("ai.knowledge", "lorebook_token_budget", ("lorebook_token_budget",), clamped_int(minimum=0, maximum=200000)),
    # [ai.memory]
    _ai("ai.memory", "memory_enabled", ("enabled",), as_bool),
    _ai("ai.memory", "memory_inject_budget_chars", ("inject_budget_chars",), clamped_int(minimum=0, maximum=4000)),
    _ai("ai.memory", "memory_inject_max_items", ("inject_max_items",), clamped_int(minimum=1, maximum=20)),
)

# 小模型任务映射：[ai.small_models] 的键是动态任务名，值为模型 id（字符串）。
SMALL_MODELS_SECTION = "ai.small_models"
SMALL_MODEL_TASK_KEYS = tuple(AIRuntimeConfig().small_model_tasks.keys())

# 非 AI 段权威清单。默认值与各读取模块（room_log/site_agent_config 等）保持一致；
# name 与文件键同名（非 AI 段无历史别名映射）。
GENERAL_SPECS: tuple[FieldSpec, ...] = (
    # [appearance]
    FieldSpec("theme", "appearance", ("theme",), as_text, "light"),
    # 管理员可用的主题白名单（空 = 全部可用）
    FieldSpec("enabled_themes", "appearance", ("enabled_themes",), as_str_list, []),
    # 站点默认的主题参数与锁定项（JSON 字符串，见 frontend/src/app/themes/contract.ts）
    FieldSpec("theme_overrides", "appearance", ("theme_overrides",), as_text, "{}"),
    # [language]
    FieldSpec("language", "language", ("language",), as_text, "zh-CN"),
    # [notification]
    FieldSpec("enable_sound", "notification", ("enable_sound",), as_bool, True),
    FieldSpec("enable_desktop_notification", "notification", ("enable_desktop_notification",), as_bool, False),
    # [autosave]
    FieldSpec("enabled", "autosave", ("enabled",), as_bool, True),
    FieldSpec("interval", "autosave", ("interval",), clamped_int(minimum=30, maximum=86400), 300),
    FieldSpec("max_nodes", "autosave", ("max_nodes",), clamped_int(minimum=1, maximum=50), 3),
    # [room_console]
    FieldSpec("enabled", "room_console", ("enabled",), as_bool, True),
    FieldSpec("mirror_to_server_console", "room_console", ("mirror_to_server_console",), as_bool, False),
    FieldSpec("max_entries", "room_console", ("max_entries",), clamped_int(minimum=50, maximum=5000), 500),
    FieldSpec("view_role", "room_console", ("view_role",), as_text, "admin"),
    FieldSpec("execute_role", "room_console", ("execute_role",), as_text, "admin"),
    # [rooms]
    # 未激活房间自动回收期限（天）；0 表示不回收。只清理未归档、无活跃成员且
    # 最后活动时间超过阈值的房间，不触碰角色卡与用户数据。
    FieldSpec("cleanup_inactive_days", "rooms", ("cleanup_inactive_days",), clamped_int(minimum=0, maximum=3650), 7),
    # [world_override]
    # 世界意志频率限制（站点默认）。房间只能在此基础上收紧，不能放宽。
    # 0 表示不限制；房间级配置见房规 world_override_*。
    FieldSpec("cooldown_seconds", "world_override", ("cooldown_seconds",), clamped_int(minimum=0, maximum=3600), 20),
    FieldSpec("hourly_limit", "world_override", ("hourly_limit",), clamped_int(minimum=0, maximum=10000), 30),
    # [chat]
    FieldSpec("show_timestamp", "chat", ("show_timestamp",), as_bool, True),
    FieldSpec("message_font_size", "chat", ("message_font_size",), clamped_int(minimum=10, maximum=24), 14),
    FieldSpec("max_history_messages", "chat", ("max_history_messages",), clamped_int(minimum=50, maximum=10000), 1000),
    FieldSpec("latex_enabled", "chat", ("latex_enabled",), as_bool, True),
    FieldSpec("latex_kp_enabled", "chat", ("latex_kp_enabled",), as_bool, True),
    FieldSpec("latex_player_enabled", "chat", ("latex_player_enabled",), as_bool, True),
    FieldSpec(
        "latex_intensity",
        "chat",
        ("latex_intensity",),
        as_enum("latex_intensity", frozenset({"light", "standard", "rich"})),
        "standard",
    ),
    FieldSpec(
        "latex_max_formulas_per_message",
        "chat",
        ("latex_max_formulas_per_message",),
        clamped_int(minimum=1, maximum=50),
        12,
    ),
    FieldSpec(
        "latex_max_formula_chars",
        "chat",
        ("latex_max_formula_chars",),
        clamped_int(minimum=50, maximum=2000),
        300,
    ),
    FieldSpec(
        "latex_max_clean_message_chars",
        "chat",
        ("latex_max_clean_message_chars",),
        clamped_int(minimum=200, maximum=10000),
        2000,
    ),
    # [character_rules]
    FieldSpec("attribute_ratio_percent", "character_rules", ("attribute_ratio_percent",), clamped_int(minimum=0, maximum=100), 20),
    FieldSpec("max_cards_per_user", "character_rules", ("max_cards_per_user",), clamped_int(minimum=1, maximum=50), 3),
    FieldSpec("weapon_slot_count", "character_rules", ("weapon_slot_count",), clamped_int(minimum=1, maximum=10), 5),
    FieldSpec("attribute_roll_str", "character_rules", ("attribute_roll_str",), as_text, "3d6x5"),
    FieldSpec("attribute_roll_dex", "character_rules", ("attribute_roll_dex",), as_text, "3d6x5"),
    FieldSpec("attribute_roll_siz", "character_rules", ("attribute_roll_siz",), as_text, "(2d6+6)x5"),
    FieldSpec("attribute_roll_app", "character_rules", ("attribute_roll_app",), as_text, "3d6x5"),
    FieldSpec("attribute_roll_con", "character_rules", ("attribute_roll_con",), as_text, "3d6x5"),
    FieldSpec("attribute_roll_int", "character_rules", ("attribute_roll_int",), as_text, "(2d6+6)x5"),
    FieldSpec("attribute_roll_pow", "character_rules", ("attribute_roll_pow",), as_text, "3d6x5"),
    FieldSpec("attribute_roll_edu", "character_rules", ("attribute_roll_edu",), as_text, "(2d6+6)x5"),
    FieldSpec("attribute_roll_luc", "character_rules", ("attribute_roll_luc",), as_text, "3d6x5"),
    # 技能基础值覆盖（JSON 字符串，前端角色卡编辑器写回）；默认不进模板。
    FieldSpec("skill_bases", "character_rules", ("skill_bases",), as_text, ""),
    # [scenario]
    FieldSpec("trigger_max_file_size", "scenario", ("trigger_max_file_size",), clamped_int(minimum=1024, maximum=536870912), 5242880),
    FieldSpec("draft_autosave_interval", "scenario", ("draft_autosave_interval",), clamped_int(minimum=15, maximum=86400), 300),
    # [window]
    FieldSpec("remember_window_size", "window", ("remember_window_size",), as_bool, True),
    FieldSpec("window_width", "window", ("window_width",), clamped_int(minimum=400, maximum=10000), 1200),
    FieldSpec("window_height", "window", ("window_height",), clamped_int(minimum=300, maximum=10000), 800),
    # [update]
    FieldSpec("check_update_on_startup", "update", ("check_update_on_startup",), as_bool, True),
    # auto_update 的语义是「自动下载」，不做自动安装/替换（见 update_service）
    FieldSpec("auto_update", "update", ("auto_update",), as_bool, False),
    FieldSpec("repo", "update", ("repo",), as_text, "Nobook1121/The-Veil"),
    FieldSpec("channel", "update", ("channel",), as_text, "stable"),
    FieldSpec("request_timeout_seconds", "update", ("request_timeout_seconds",), clamped_int(minimum=3, maximum=15), 8),
    # [site_agent]（与 site_agent_config.SiteAgentConfig 默认值/边界保持一致）
    FieldSpec("enabled", "site_agent", ("enabled",), as_bool, True),
    FieldSpec("max_tool_rounds", "site_agent", ("max_tool_rounds",), clamped_int(minimum=1, maximum=20), 6),
    FieldSpec("request_timeout", "site_agent", ("request_timeout",), clamped_int(minimum=30, maximum=1800), 120),
    FieldSpec("max_history_messages", "site_agent", ("max_history_messages",), clamped_int(minimum=4, maximum=200), 40),
    FieldSpec("max_tool_result_chars", "site_agent", ("max_tool_result_chars",), clamped_int(minimum=200, maximum=20000), 4000),
    FieldSpec("max_message_chars", "site_agent", ("max_message_chars",), clamped_int(minimum=100, maximum=20000), 4000),
    FieldSpec("max_turns_per_hour", "site_agent", ("max_turns_per_hour",), clamped_int(minimum=1, maximum=1000), 60),
    FieldSpec("daily_token_budget", "site_agent", ("daily_token_budget",), clamped_int(minimum=0, maximum=100000000), 200000),
    FieldSpec("conversation_retention_days", "site_agent", ("conversation_retention_days",), clamped_int(minimum=1, maximum=3650), 30),
    FieldSpec("action_ttl_seconds", "site_agent", ("action_ttl_seconds",), clamped_int(minimum=30, maximum=3600), 300),
    FieldSpec("max_conversations_per_user", "site_agent", ("max_conversations_per_user",), clamped_int(minimum=1, maximum=200), 30),
    FieldSpec("max_messages_per_conversation", "site_agent", ("max_messages_per_conversation",), clamped_int(minimum=10, maximum=2000), 200),
    # [immersive]（实时配乐与实时配图；房间可覆盖开关，站点级预算仅管理员可改）
    FieldSpec("enabled", "immersive", ("enabled",), as_bool, True),
    FieldSpec("music_enabled", "immersive", ("music_enabled",), as_bool, True),
    FieldSpec("music_auto_switch", "immersive", ("music_auto_switch",), as_bool, True),
    FieldSpec(
        "music_default_profile",
        "immersive",
        ("music_default_profile",),
        as_enum(
            "music_default_profile",
            frozenset({"daily", "suspense", "unease", "horror", "urgency", "sorrow", "awe", "aftermath"}),
        ),
        "daily",
    ),
    FieldSpec(
        "music_default_intensity",
        "immersive",
        ("music_default_intensity",),
        as_enum("music_default_intensity", frozenset({"low", "medium", "high"})),
        "medium",
    ),
    FieldSpec("music_volume", "immersive", ("music_volume",), clamped_int(minimum=0, maximum=100), 35),
    FieldSpec("music_crossfade_ms", "immersive", ("music_crossfade_ms",), clamped_int(minimum=500, maximum=10000), 3000),
    FieldSpec("music_hysteresis_turns", "immersive", ("music_hysteresis_turns",), clamped_int(minimum=0, maximum=10), 2),
    FieldSpec("music_seed", "immersive", ("music_seed",), as_text, ""),
    FieldSpec("music_duck_bgm", "immersive", ("music_duck_bgm",), as_bool, True),
    FieldSpec("illustration_enabled", "immersive", ("illustration_enabled",), as_bool, False),
    FieldSpec(
        "illustration_provider",
        "immersive",
        ("illustration_provider",),
        as_enum("illustration_provider", frozenset({"procedural", "platform", "openai_compatible"})),
        "procedural",
    ),
    # 默认生图平台 ID（见 immersive.image_platforms）；provider=platform 时唯一真相。
    FieldSpec("illustration_platform_id", "immersive", ("illustration_platform_id",), as_text, ""),
    FieldSpec("illustration_auto", "immersive", ("illustration_auto",), as_bool, True),
    # 旧单条配置：仅用于一次性迁移到生图平台表，运行期不再读取。
    FieldSpec("illustration_base_url", "immersive", ("illustration_base_url",), as_text, ""),
    FieldSpec("illustration_model", "immersive", ("illustration_model",), as_text, "dall-e-3"),
    FieldSpec("illustration_size", "immersive", ("illustration_size",), as_text, "1024x1024"),
    # 旧生图接口密钥：仅用于一次性迁移；新流程按平台条目分开存储。
    FieldSpec("api_key", "immersive", ("api_key",), as_text, ""),
    FieldSpec("illustration_timeout", "immersive", ("illustration_timeout",), clamped_int(minimum=20, maximum=600), 120),
    FieldSpec("illustration_concurrency", "immersive", ("illustration_concurrency",), clamped_int(minimum=1, maximum=4), 2),
    FieldSpec("illustration_daily_budget", "immersive", ("illustration_daily_budget",), clamped_int(minimum=0, maximum=100000), 0),
    FieldSpec("illustration_room_hourly_cap", "immersive", ("illustration_room_hourly_cap",), clamped_int(minimum=0, maximum=500), 12),
    FieldSpec("illustration_min_turns", "immersive", ("illustration_min_turns",), clamped_int(minimum=1, maximum=50), 4),
    # 配图触发档位：off / important（仅重要场景转换）/ turn_interval（每 N 回合）/ frequent。
    FieldSpec(
        "illustration_trigger",
        "immersive",
        ("illustration_trigger",),
        as_enum("illustration_trigger", frozenset({"off", "important", "turn_interval", "frequent"})),
        "important",
    ),
    # 允许个人覆盖的档位（逗号分隔）；off 始终允许，作为个人收紧项。
    FieldSpec(
        "illustration_trigger_allowed",
        "immersive",
        ("illustration_trigger_allowed",),
        as_text,
        "important,turn_interval,frequent",
    ),
    # AI KP 判断：平台/模型统一取「模型设置」中启用的 AI 平台，这里只保留超时与预算。
    FieldSpec("illustration_kp_timeout", "immersive", ("illustration_kp_timeout",), clamped_int(minimum=10, maximum=300), 45),
    FieldSpec("illustration_kp_daily_budget", "immersive", ("illustration_kp_daily_budget",), clamped_int(minimum=0, maximum=10000), 200),
    FieldSpec(
        "illustration_kp_max_context_chars",
        "immersive",
        ("illustration_kp_max_context_chars",),
        clamped_int(minimum=200, maximum=8000),
        1600,
    ),
    # NPC 头像：尺寸与场景图区分，每回合最多新增一张，同一 NPC 在房间内复用。
    FieldSpec("illustration_npc_enabled", "immersive", ("illustration_npc_enabled",), as_bool, False),
    FieldSpec("illustration_npc_size", "immersive", ("illustration_npc_size",), as_text, "768x768"),
    FieldSpec("illustration_npc_max_per_turn", "immersive", ("illustration_npc_max_per_turn",), clamped_int(minimum=0, maximum=4), 1),
    # 房间级缓存：落盘在房间目录内，可随归档保留；默认 40 条 / 64MB / 单图 8MB，超限 LRU。
    FieldSpec("illustration_room_cache_max_entries", "immersive", ("illustration_room_cache_max_entries",), clamped_int(minimum=0, maximum=200), 40),
    FieldSpec("illustration_room_cache_quota_mb", "immersive", ("illustration_room_cache_quota_mb",), clamped_int(minimum=0, maximum=1024), 64),
    FieldSpec("illustration_room_cache_max_image_mb", "immersive", ("illustration_room_cache_max_image_mb",), clamped_int(minimum=1, maximum=64), 8),
    FieldSpec("illustration_room_cache_overflow", "immersive", ("illustration_room_cache_overflow",), as_enum("illustration_room_cache_overflow", frozenset({"lru", "reject"})), "lru"),
    # 音乐来源：realtime（仅实时配乐）/ bgm（仅 BGM）/ hybrid（实时配乐主层 + BGM 底噪）。
    FieldSpec(
        "music_source",
        "immersive",
        ("music_source",),
        as_enum("music_source", frozenset({"realtime", "bgm", "hybrid"})),
        "hybrid",
    ),
    # 允许个人选择的音乐来源档位（逗号分隔）；个人只能在站点允许集合内覆盖。
    FieldSpec("music_source_allowed", "immersive", ("music_source_allowed",), as_text, "realtime,bgm,hybrid"),
    # 关闭后个人设置中的音乐来源不可覆盖，只能跟随站点默认。
    FieldSpec("music_source_allow_personal", "immersive", ("music_source_allow_personal",), as_bool, True),
    # 音乐引擎：站点默认，房间只能降级（external -> procedural -> off）。
    FieldSpec(
        "music_engine",
        "immersive",
        ("music_engine",),
        as_enum("music_engine", frozenset({"procedural", "external", "off"})),
        "procedural",
    ),
    # 管理员允许的引擎档位（逗号分隔）；房间只能取其中更低的一档。
    FieldSpec("music_engine_allowed", "immersive", ("music_engine_allowed",), as_text, "procedural,external"),
    # 音频块允许清单（逗号分隔）：ambient,procedural,music_model,external,off。
    # 缺失时由旧 music_engine / music_engine_allowed 自动推导，升级不会突然静音。
    FieldSpec("audio_blocks_allowed", "immersive", ("audio_blocks_allowed",), as_text, "ambient,procedural,external,off"),
    # 外链曲库：Jamendo 主源 + Openverse 次源。
    FieldSpec("music_external_providers", "immersive", ("music_external_providers",), as_text, "jamendo,openverse"),
    FieldSpec("music_external_license_allow", "immersive", ("music_external_license_allow",), as_text, "cc0,by,by-sa"),
    FieldSpec("music_external_instrumental_only", "immersive", ("music_external_instrumental_only",), as_bool, True),
    FieldSpec("music_external_timeout", "immersive", ("music_external_timeout",), clamped_int(minimum=2, maximum=30), 6),
    FieldSpec("music_external_result_limit", "immersive", ("music_external_result_limit",), clamped_int(minimum=3, maximum=20), 10),
    FieldSpec("music_external_search_ttl", "immersive", ("music_external_search_ttl",), clamped_int(minimum=0, maximum=604800), 86400),
    FieldSpec("music_external_url_ttl", "immersive", ("music_external_url_ttl",), clamped_int(minimum=60, maximum=86400), 600),
    FieldSpec("music_external_daily_budget", "immersive", ("music_external_daily_budget",), clamped_int(minimum=0, maximum=100000), 200),
    FieldSpec("music_external_room_hourly_cap", "immersive", ("music_external_room_hourly_cap",), clamped_int(minimum=0, maximum=10000), 30),
    FieldSpec("music_external_cache_audio", "immersive", ("music_external_cache_audio",), as_bool, False),
    FieldSpec("music_external_cache_quota_mb", "immersive", ("music_external_cache_quota_mb",), clamped_int(minimum=0, maximum=102400), 256),
    FieldSpec("music_external_cache_max_entries", "immersive", ("music_external_cache_max_entries",), clamped_int(minimum=0, maximum=1000), 60),
    FieldSpec("music_external_cache_max_track_mb", "immersive", ("music_external_cache_max_track_mb",), clamped_int(minimum=1, maximum=64), 12),
    FieldSpec(
        "music_external_cache_overflow",
        "immersive",
        ("music_external_cache_overflow",),
        as_enum("music_external_cache_overflow", frozenset({"lru", "reject"})),
        "lru",
    ),
    FieldSpec("music_external_scenario_record", "immersive", ("music_external_scenario_record",), as_bool, False),
    FieldSpec("music_external_scenario_prefer", "immersive", ("music_external_scenario_prefer",), as_bool, True),
    FieldSpec("music_external_circuit_failures", "immersive", ("music_external_circuit_failures",), clamped_int(minimum=1, maximum=20), 3),
    FieldSpec("music_external_circuit_cooldown_s", "immersive", ("music_external_circuit_cooldown_s",), clamped_int(minimum=30, maximum=7200), 600),
    FieldSpec("music_external_jamendo_client_id", "immersive", ("music_external_jamendo_client_id",), as_text, ""),
    FieldSpec("music_external_allowed_hosts", "immersive", ("music_external_allowed_hosts",), as_text, ""),
    FieldSpec("music_external_phrase_bars", "immersive", ("music_external_phrase_bars",), clamped_int(minimum=1, maximum=16), 8),
    FieldSpec("music_external_fade_ms", "immersive", ("music_external_fade_ms",), clamped_int(minimum=1500, maximum=12000), 4200),
    # 剧本配图缓存：默认只复用、不写入作者剧本目录，避免无声修改与磁盘膨胀。
    FieldSpec("illustration_scenario_record", "immersive", ("illustration_scenario_record",), as_bool, False),
    FieldSpec("illustration_scenario_prefer", "immersive", ("illustration_scenario_prefer",), as_bool, True),
    FieldSpec("illustration_scenario_quota_mb", "immersive", ("illustration_scenario_quota_mb",), clamped_int(minimum=0, maximum=102400), 256),
    FieldSpec("illustration_scenario_max_image_mb", "immersive", ("illustration_scenario_max_image_mb",), clamped_int(minimum=1, maximum=64), 8),
    FieldSpec(
        "illustration_scenario_overflow",
        "immersive",
        ("illustration_scenario_overflow",),
        as_enum("illustration_scenario_overflow", frozenset({"lru", "reject"})),
        "lru",
    ),
    # [immersive] LLM 音乐导演：规则分析器先行，LLM 只在后台精修乐谱，失败一律回退。
    FieldSpec("music_director_enabled", "immersive", ("music_director_enabled",), as_bool, False),
    # 导演使用的平台/模型统一取「模型设置」中启用的 AI 平台，不再单独配置。
    # 触发频率：每 N 个叙事回合尝试一次（阶段/情绪事件会额外提前触发）。
    FieldSpec("music_director_interval_turns", "immersive", ("music_director_interval_turns",), clamped_int(minimum=1, maximum=20), 2),
    # 站点级每日导演调用预算（次，0 表示不限制；房间只能收紧）。
    FieldSpec("music_director_daily_budget", "immersive", ("music_director_daily_budget",), clamped_int(minimum=0, maximum=100000), 0),
    FieldSpec("music_director_timeout", "immersive", ("music_director_timeout",), clamped_int(minimum=20, maximum=300), 45),
    # 乐谱缓存 TTL（秒，0 表示不复用缓存）。
    FieldSpec("music_director_cache_ttl", "immersive", ("music_director_cache_ttl",), clamped_int(minimum=0, maximum=86400), 3600),
    FieldSpec("music_director_min_confidence", "immersive", ("music_director_min_confidence",), clamped_int(minimum=0, maximum=100), 55),
    FieldSpec(
        "music_director_fallback",
        "immersive",
        ("music_director_fallback",),
        as_enum("music_director_fallback", frozenset({"rule", "hold"})),
        "rule",
    ),
    FieldSpec("music_director_max_context_chars", "immersive", ("music_director_max_context_chars",), clamped_int(minimum=200, maximum=4000), 1200),
    # [immersive] AI 对话语音配音：默认关闭，系统音色（浏览器 Web Speech）默认引擎。
    FieldSpec("voice_enabled", "immersive", ("voice_enabled",), as_bool, False),
    FieldSpec(
        "voice_engine",
        "immersive",
        ("voice_engine",),
        as_enum("voice_engine", frozenset({"system", "local", "cloud"})),
        "system",
    ),
    # 允许的引擎白名单（逗号分隔）；本阶段只有 system 真正可用。
    FieldSpec("voice_engine_allowed", "immersive", ("voice_engine_allowed",), as_text, "system"),
    FieldSpec("voice_autoplay", "immersive", ("voice_autoplay",), as_bool, False),
    FieldSpec("voice_volume", "immersive", ("voice_volume",), clamped_int(minimum=0, maximum=100), 70),
    FieldSpec("voice_rate", "immersive", ("voice_rate",), clamped_int(minimum=50, maximum=200), 100),
    FieldSpec("voice_max_turn_chars", "immersive", ("voice_max_turn_chars",), clamped_int(minimum=40, maximum=4000), 800),
    FieldSpec("voice_max_segment_chars", "immersive", ("voice_max_segment_chars",), clamped_int(minimum=10, maximum=500), 40),
    FieldSpec("voice_max_segments", "immersive", ("voice_max_segments",), clamped_int(minimum=1, maximum=200), 24),
    FieldSpec(
        "voice_interrupt_policy",
        "immersive",
        ("voice_interrupt_policy",),
        as_enum("voice_interrupt_policy", frozenset({"interrupt", "queue"})),
        "interrupt",
    ),
    FieldSpec("voice_ducking", "immersive", ("voice_ducking",), as_bool, True),
    FieldSpec("voice_ducking_level", "immersive", ("voice_ducking_level",), clamped_int(minimum=0, maximum=90), 35),
    FieldSpec("voice_room_hourly_cap", "immersive", ("voice_room_hourly_cap",), clamped_int(minimum=0, maximum=10000), 120),
    # 云端引擎占位：本阶段不发起请求；如需凭据，统一使用「模型设置」里的 AI 平台，
    # 不再在语音配置中单独保存 API Key（旧配置残留字段读取时直接忽略）。
    FieldSpec("voice_cloud_base_url", "immersive", ("voice_cloud_base_url",), as_text, ""),
    FieldSpec("voice_cloud_model", "immersive", ("voice_cloud_model",), as_text, ""),
    # 本地多音色引擎（sherpa-onnx + Kokoro v1.1-zh）；默认关闭，需管理员显式开启并安装权重。
    FieldSpec("voice_local_enabled", "immersive", ("voice_local_enabled",), as_bool, False),
    # 留空表示由主进程托管本地子服务；填写后连接管理员自托管的回环服务。
    FieldSpec("voice_local_base_url", "immersive", ("voice_local_base_url",), as_text, ""),
    FieldSpec("voice_local_model", "immersive", ("voice_local_model",), as_text, "kokoro-int8-multi-lang-v1_1"),
    # 语音并发固定可配；与实时配图错峰，默认 1。
    FieldSpec("voice_local_concurrency", "immersive", ("voice_local_concurrency",), clamped_int(minimum=1, maximum=8), 1),
    FieldSpec("voice_local_timeout", "immersive", ("voice_local_timeout",), clamped_int(minimum=5, maximum=180), 30),
    # 站点默认的人设→说话人绑定（JSON 文本，例如 {"narrator": 24, "npc_male_elder": 102}）。
    FieldSpec("voice_local_speakers", "immersive", ("voice_local_speakers",), as_text, ""),
    FieldSpec("voice_cache_enabled", "immersive", ("voice_cache_enabled",), as_bool, True),
    FieldSpec("voice_cache_quota_mb", "immersive", ("voice_cache_quota_mb",), clamped_int(minimum=0, maximum=1024), 64),
    FieldSpec("voice_cache_max_entries", "immersive", ("voice_cache_max_entries",), clamped_int(minimum=0, maximum=10000), 300),
    FieldSpec(
        "voice_cache_overflow",
        "immersive",
        ("voice_cache_overflow",),
        as_enum("voice_cache_overflow", frozenset({"lru", "reject"})),
        "lru",
    ),
    FieldSpec("voice_allow_personal", "immersive", ("voice_allow_personal",), as_bool, True),
    # [site]（整合自原 site.json；setup.py 读写）
    FieldSpec("name", "site", ("name",), as_text, ""),
    FieldSpec("domain", "site", ("domain",), as_text, ""),
    FieldSpec("setup_dismissed", "site", ("setup_dismissed",), as_bool, False),
    # [network]（整合自原 network.json；server.py/routes/network.py 读写，port 需重启）
    FieldSpec("port", "network", ("port",), clamped_int(minimum=1, maximum=65535), DEFAULT_NETWORK_PORT),
    FieldSpec("discovery_enabled", "network", ("discovery_enabled",), as_bool, True),
    FieldSpec("share_base_url", "network", ("share_base_url",), as_text, ""),
    # [network.access_control]（原 network.json 的嵌套 access_control 表）
    FieldSpec("enabled", "network.access_control", ("enabled",), as_bool, False),
    FieldSpec("allowed_ips", "network.access_control", ("allowed_ips",), as_str_list, []),
    # [network.penetration]（整合自原 penetration.json；启停需重启穿透进程）
    FieldSpec("enabled", "network.penetration", ("enabled",), as_bool, False),
    FieldSpec("type", "network.penetration", ("type",), as_text, "ngrok"),
    FieldSpec("port_mappings", "network.penetration", ("port_mappings",), as_list, []),
    # [network.penetration.settings]（穿透令牌等；模板中一律留空，不落密钥）
    FieldSpec("auth_token", "network.penetration.settings", ("auth_token",), as_text, ""),
    FieldSpec("region", "network.penetration.settings", ("region",), as_text, "us"),
    FieldSpec("subdomain", "network.penetration.settings", ("subdomain",), as_text, ""),
)

SPECS: tuple[FieldSpec, ...] = AI_SPECS + GENERAL_SPECS
SPECS_BY_SECTION_KEY: dict[tuple[str, str], FieldSpec] = {
    (spec.section, key): spec for spec in SPECS for key in spec.keys
}
AI_SECTIONS = frozenset({"ai", SMALL_MODELS_SECTION, "ai.prompt", "ai.knowledge", "ai.cache"})
