import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
    dice_player_roll: bool = False
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


def _toml_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(raw: str, default: bool) -> bool:
    """与历史行为保持一致：只有字面量 ``true``（忽略大小写）才算真。"""
    return raw.lower() == "true"


def _as_text(raw: str, default: str) -> str:
    return raw.strip()


def _clamped_int(*, minimum: int, maximum: int | None = None) -> Callable[[str, int], int]:
    """生成整数转换器：非法值回退默认值，再收敛到 ``[minimum, maximum]``。"""

    def convert(raw: str, default: int) -> int:
        value = _toml_int(raw, default)
        if value < minimum:
            return minimum
        if maximum is not None and value > maximum:
            return maximum
        return value

    return convert


def _enum(field_name: str, allowed: frozenset[str]) -> Callable[[str, str], str]:
    """生成枚举转换器：非法值告警并回退默认值。"""

    def convert(raw: str, default: str) -> str:
        candidate = raw.strip().lower()
        if candidate in allowed:
            return candidate
        logger.warning("Invalid %s %r in general.toml; falling back to %s", field_name, raw, default)
        return default

    return convert


@dataclass(frozen=True)
class _FieldSpec:
    """一条配置项接线：``general.toml`` 的分节/键 → ``AIRuntimeConfig`` 字段 + 转换方式。"""

    name: str
    section: str
    keys: tuple[str, ...]
    convert: Callable[[str, Any], Any]


# 权威字段清单。新增配置项只需在这里加一行，解析循环与对象装配都不再需要改动。
_FIELD_SPECS: tuple[_FieldSpec, ...] = (
    # [ai]
    _FieldSpec("stream_output", "ai", ("stream_output",), _as_bool),
    _FieldSpec("debug_mode", "ai", ("debug_mode",), _as_bool),
    _FieldSpec("show_ai_hints", "ai", ("show_ai_hints", "show_kp_hints", "reply_hints"), _as_bool),
    _FieldSpec("max_tool_rounds", "ai", ("max_tool_rounds",), _clamped_int(minimum=1, maximum=30)),
    _FieldSpec("ai_request_timeout", "ai", ("ai_request_timeout",), _clamped_int(minimum=30, maximum=1800)),
    _FieldSpec("ai_request_max_retries", "ai", ("ai_request_max_retries",), _clamped_int(minimum=0, maximum=20)),
    _FieldSpec("constrain_max_tokens", "ai", ("constrain_max_tokens",), _as_bool),
    _FieldSpec("max_tokens", "ai", ("max_tokens",), _clamped_int(minimum=1, maximum=200000)),
    _FieldSpec("record_prompt_segments", "ai", ("record_prompt_segments",), _as_bool),
    _FieldSpec("cloud_embedding_enabled", "ai", ("cloud_embedding_enabled",), _as_bool),
    _FieldSpec("local_embedding_repo", "ai", ("local_embedding_repo",), _as_text),
    _FieldSpec("dice_critical_threshold", "ai", ("dice_critical_threshold",), _clamped_int(minimum=0, maximum=100)),
    _FieldSpec("dice_fumble_threshold", "ai", ("dice_fumble_threshold",), _clamped_int(minimum=0, maximum=100)),
    _FieldSpec(
        "dice_animation_level",
        "ai",
        ("dice_animation_level",),
        _enum("dice_animation_level", frozenset({"text", "simple", "3d"})),
    ),
    _FieldSpec("dice_physics_results", "ai", ("dice_physics_results",), _as_bool),
    _FieldSpec("dice_player_roll", "ai", ("dice_player_roll",), _as_bool),
    _FieldSpec(
        "dice_player_roll_timeout", "ai", ("dice_player_roll_timeout",), _clamped_int(minimum=30, maximum=300)
    ),
    _FieldSpec(
        "dice_reroll_mode",
        "ai",
        ("dice_reroll_mode",),
        _enum("dice_reroll_mode", frozenset({"off", "limited", "unlimited"})),
    ),
    _FieldSpec("dice_reroll_limit", "ai", ("dice_reroll_limit",), _clamped_int(minimum=1, maximum=20)),
    # [ai.prompt]
    _FieldSpec(
        "snapshot_manifest_summary_cap", "ai.prompt", ("snapshot_manifest_summary_cap",), _clamped_int(minimum=0)
    ),
    _FieldSpec(
        "max_snapshot_scene_manifest", "ai.prompt", ("max_snapshot_scene_manifest",), _clamped_int(minimum=0)
    ),
    _FieldSpec("snapshot_include_entity_manifest", "ai.prompt", ("snapshot_include_entity_manifest",), _as_bool),
    _FieldSpec("max_tool_result_chars", "ai.prompt", ("max_tool_result_chars",), _clamped_int(minimum=500)),
    _FieldSpec("dedupe_tool_calls", "ai.prompt", ("dedupe_tool_calls",), _as_bool),
    # [ai.cache]
    _FieldSpec("semantic_cache_enabled", "ai.cache", ("semantic_enabled",), _as_bool),
    _FieldSpec("semantic_cache_ttl", "ai.cache", ("semantic_ttl_seconds",), _clamped_int(minimum=30, maximum=86400)),
    _FieldSpec("exact_cache_ttl", "ai.cache", ("exact_ttl_seconds",), _clamped_int(minimum=30, maximum=86400)),
    # [ai.knowledge]
    _FieldSpec("retrieval_top_k_default", "ai.knowledge", ("top_k_default",), _clamped_int(minimum=1, maximum=32)),
    _FieldSpec("retrieval_top_k_direct", "ai.knowledge", ("top_k_direct",), _clamped_int(minimum=1, maximum=32)),
    _FieldSpec(
        "chunk_child_max_chars", "ai.knowledge", ("chunk_child_max_chars",), _clamped_int(minimum=200, maximum=8000)
    ),
    _FieldSpec(
        "chunk_parent_max_chars",
        "ai.knowledge",
        ("chunk_parent_max_chars",),
        _clamped_int(minimum=200, maximum=16000),
    ),
    _FieldSpec("sticky_rounds", "ai.knowledge", ("sticky_rounds",), _clamped_int(minimum=0, maximum=10)),
    _FieldSpec("cooldown_rounds", "ai.knowledge", ("cooldown_rounds",), _clamped_int(minimum=0, maximum=20)),
    _FieldSpec("keyword_channel_enabled", "ai.knowledge", ("keyword_channel_enabled",), _as_bool),
    _FieldSpec("recursive_scanning", "ai.knowledge", ("recursive_scanning",), _as_bool),
    _FieldSpec("max_recursion_depth", "ai.knowledge", ("max_recursion_depth",), _clamped_int(minimum=0, maximum=10)),
    _FieldSpec(
        "lorebook_token_budget", "ai.knowledge", ("lorebook_token_budget",), _clamped_int(minimum=0, maximum=200000)
    ),
)

_SPECS_BY_SECTION_KEY: dict[tuple[str, str], _FieldSpec] = {
    (spec.section, key): spec for spec in _FIELD_SPECS for key in spec.keys
}

_AI_SECTIONS = frozenset({"ai", "ai.small_models", "ai.prompt", "ai.knowledge", "ai.cache"})


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

    defaults = AIRuntimeConfig()
    overrides: dict[str, Any] = {}
    small_model_tasks = dict(defaults.small_model_tasks)
    current_section = ""

    for raw_line in general_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = _unquote_toml_name(line[1:-1])
            continue
        if current_section not in _AI_SECTIONS or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        key = _unquote_toml_name(key)
        if value.startswith(('"', "'")) and value.endswith(('"', "'")):
            value = value[1:-1]
        if current_section == "ai.small_models":
            small_model_tasks[key] = value
            continue
        spec = _SPECS_BY_SECTION_KEY.get((current_section, key))
        if spec is not None:
            overrides[spec.name] = spec.convert(value, getattr(defaults, spec.name))

    overrides["small_model_tasks"] = small_model_tasks
    return replace(defaults, **overrides)


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text.startswith(('"', "'")) and text.endswith(('"', "'")):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_ai_setting(key: str, value: Any, config_dir: Path | None = None) -> Path:
    """把单个 ``[ai]`` 键写回 ``general.toml``，保留其余注释与顺序（原子写入）。"""
    if config_dir is None:
        from trpg_server.settings import CONFIG_DIR

        config_dir = CONFIG_DIR
    path = Path(config_dir) / "general.toml"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = f"{key} = {_format_toml_value(value)}"
    start: int | None = None
    end = len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.split("#", 1)[0].strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        name = _unquote_toml_name(stripped[1:-1])
        if start is None:
            if name == "ai":
                start = index
        else:
            end = index
            break
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[ai]")
        lines.append(entry)
    else:
        replaced = False
        for index in range(start + 1, end):
            stripped = lines[index].split("#", 1)[0].strip()
            if "=" in stripped and _unquote_toml_name(stripped.split("=", 1)[0].strip()) == key:
                lines[index] = entry
                replaced = True
                break
        if not replaced:
            lines.insert(end, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return path
