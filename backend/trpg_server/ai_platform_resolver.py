"""AI 平台「用途」与按顺序解析。

每个 AI 平台可以在配置里声明 ``capabilities``（用途多选）。运行时按管理员拖动的
``order`` 顺序遍历平台，只有同时满足以下条件的第一个平台才会被采用：

1. 平台已启用（``enabled``）；
2. 平台用途包含本次请求的用途；
3. 平台 API 可用（配置完整，且近期没有失败记录）。

「API 可用」不额外发探测请求：一次真实调用失败就会写入短期健康缓存，后续请求
直接跳过该平台、顺延到下一个；缓存过期后自动半开重试，调用成功即恢复。这样既
不会每个请求都打一次探测接口，也不会对明显不可用的平台反复重试。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.ai_platform_config import load_platform_config, ordered_platform_paths
from trpg_server.settings import AI_PLATFORM_SECRET_DIR

# 用途清单：只列真实调用点，embedding 等独立体系不放进平台用途。
CAPABILITY_CHAT = "chat"
CAPABILITY_ILLUSTRATION = "illustration"
CAPABILITY_MUSIC_DIRECTOR = "music_director"
CAPABILITY_SCENARIO = "scenario"
CAPABILITY_AGENT = "agent"
CAPABILITY_VOICE = "voice"

CAPABILITY_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "value": CAPABILITY_CHAT,
        "label": "对话 / KP",
        "description": "房间 AI 对话、角色卡生成与叙事续写",
    },
    {
        "value": CAPABILITY_ILLUSTRATION,
        "label": "配图判断",
        "description": "AI KP 判断是否配图并生成画面提示词；实际渲染仍走生图平台表",
    },
    {
        "value": CAPABILITY_MUSIC_DIRECTOR,
        "label": "音乐导演",
        "description": "实时配乐的后台 LLM 精修",
    },
    {
        "value": CAPABILITY_SCENARIO,
        "label": "剧本创建",
        "description": "剧本摘要、导入拆分与模块分析",
    },
    {
        "value": CAPABILITY_AGENT,
        "label": "站点 Agent",
        "description": "站点助手的对话与工具调用",
    },
    {
        "value": CAPABILITY_VOICE,
        "label": "语音配音",
        "description": "云端语音占位；当前本地配音不经过 AI 平台",
    },
)
CAPABILITY_VALUES: tuple[str, ...] = tuple(item["value"] for item in CAPABILITY_OPTIONS)

# 旧平台没有 capabilities 字段时的默认用途：历史上它们都参与过「第一个启用平台」
# 的文本补全调用，因此默认放行 chat / 配图判断 / 音乐导演 / 剧本 / Agent；语音是
# 本版新增的占位用途，从未由这些平台承担，不默认勾选。
DEFAULT_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_CHAT,
    CAPABILITY_ILLUSTRATION,
    CAPABILITY_MUSIC_DIRECTOR,
    CAPABILITY_SCENARIO,
    CAPABILITY_AGENT,
)

# 失败短期记忆：普通错误 60 秒，鉴权/地址类错误 5 分钟后再试。
TRANSIENT_FAILURE_TTL_SECONDS = 60.0
CONFIG_FAILURE_TTL_SECONDS = 300.0
AUTH_FAILURE_STATUSES = {401, 403, 404}

_HEALTH_LOCK = threading.Lock()
_FAILED_UNTIL: dict[tuple[str, str], float] = {}


class PlatformUpstreamError(RuntimeError):
    """携带 HTTP 状态码的上游错误，用于决定健康缓存时长。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def now() -> float:
    return time.monotonic()


def normalize_capabilities(raw: Any) -> list[str]:
    """把配置里的用途归一为有序、去重、只含已知值的列表。

    缺失字段（``None``）返回旧平台默认值；显式空列表表示该平台不参与任何用途。
    """
    if raw is None:
        return list(DEFAULT_CAPABILITIES)
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
    else:
        return list(DEFAULT_CAPABILITIES)
    cleaned: list[str] = []
    for value in values:
        if value in CAPABILITY_VALUES and value not in cleaned:
            cleaned.append(value)
    return cleaned


def platform_capabilities(config: dict[str, Any] | None) -> list[str]:
    if not isinstance(config, dict):
        return list(DEFAULT_CAPABILITIES)
    return normalize_capabilities(config.get("capabilities"))


def platform_supports(config: dict[str, Any] | None, purpose: str) -> bool:
    return purpose in platform_capabilities(config)


def failure_ttl(status_code: Any) -> float:
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return TRANSIENT_FAILURE_TTL_SECONDS
    if code in AUTH_FAILURE_STATUSES:
        return CONFIG_FAILURE_TTL_SECONDS
    return TRANSIENT_FAILURE_TTL_SECONDS


def is_platform_available(platform_id: str, purpose: str, *, at: float | None = None) -> bool:
    key = (str(platform_id), str(purpose))
    expires = _FAILED_UNTIL.get(key)
    if expires is None:
        return True
    return (now() if at is None else at) >= expires


def mark_platform_failure(platform_id: str, purpose: str, *, status_code: Any = None) -> float:
    """记录一次失败，返回该记录的有效秒数。"""
    ttl = failure_ttl(status_code)
    with _HEALTH_LOCK:
        _FAILED_UNTIL[(str(platform_id), str(purpose))] = now() + ttl
    return ttl


def mark_platform_success(platform_id: str, purpose: str) -> None:
    with _HEALTH_LOCK:
        _FAILED_UNTIL.pop((str(platform_id), str(purpose)), None)


def reset_health(platform_id: str | None = None) -> None:
    with _HEALTH_LOCK:
        if platform_id is None:
            _FAILED_UNTIL.clear()
            return
        for key in list(_FAILED_UNTIL):
            if key[0] == str(platform_id):
                _FAILED_UNTIL.pop(key, None)


def health_snapshot() -> list[dict[str, Any]]:
    current = now()
    with _HEALTH_LOCK:
        return [
            {
                "platform": platform_id,
                "purpose": purpose,
                "remaining_seconds": round(max(0.0, expires - current), 1),
            }
            for (platform_id, purpose), expires in _FAILED_UNTIL.items()
            if expires > current
        ]


def _resolve_root(config_dir: Any) -> Path:
    if config_dir:
        return Path(config_dir)
    from trpg_server.config.paths import resolve_config_dir

    return Path(resolve_config_dir(None))


def _select_model(config: dict[str, Any], model_id: str = "") -> str:
    section = config.get("config") if isinstance(config.get("config"), dict) else {}
    selected = str(model_id or section.get("default_model") or "").strip()
    if selected:
        return selected
    models = config.get("models") if isinstance(config.get("models"), list) else []
    for item in models:
        if isinstance(item, dict) and item.get("enabled", True):
            candidate = str(item.get("id") or "").strip()
            if candidate:
                return candidate
    return "local-model"


def build_platform_target(
    platform_id: str,
    config: dict[str, Any],
    *,
    model_id: str = "",
) -> dict[str, Any] | None:
    """把平台配置转成请求目标；配置不完整时返回 ``None``（视为 API 不可用）。"""
    if not isinstance(config, dict):
        return None
    section = config.get("config") if isinstance(config.get("config"), dict) else {}
    base_url = str(section.get("base_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        return None
    headers = section.get("headers") if isinstance(section.get("headers"), dict) else {}
    return {
        "platform": str(platform_id),
        "model": _select_model(config, model_id),
        "endpoint": chat_completions_endpoint(base_url),
        "headers": {str(key): str(value) for key, value in headers.items()},
        "api_key": str(section.get("api_key") or ""),
        "timeout": int(section.get("timeout") or 60),
        "capabilities": platform_capabilities(config),
    }


def resolve_platform_targets(
    config_dir: Any,
    purpose: str,
    *,
    platform_id: str = "",
    model_id: str = "",
    secret_dir_override: Any = None,
    include_unavailable: bool = False,
) -> list[dict[str, Any]]:
    """按 ``order`` 返回所有候选平台目标。

    过滤顺序：启用 → 用途包含 → 配置完整 → 近期无失败记录。``include_unavailable``
    仅用于后台状态展示，正常请求不要打开。
    """
    platform_dir = _resolve_root(config_dir) / "aiplatform"
    secret_dir = Path(secret_dir_override) if secret_dir_override else AI_PLATFORM_SECRET_DIR
    targets: list[dict[str, Any]] = []
    for path in ordered_platform_paths(platform_dir):
        if platform_id and path.stem != platform_id:
            continue
        try:
            config = load_platform_config(path, secret_dir / path.name)
        except (OSError, ValueError):
            continue
        if not config.get("enabled", False):
            continue
        if not platform_supports(config, purpose):
            continue
        target = build_platform_target(path.stem, config, model_id=model_id)
        if target is None:
            continue
        if not include_unavailable and not is_platform_available(path.stem, purpose):
            continue
        targets.append(target)
    return targets


def resolve_platform_target(config_dir: Any, purpose: str, **kwargs: Any) -> dict[str, Any] | None:
    targets = resolve_platform_targets(config_dir, purpose, **kwargs)
    return targets[0] if targets else None
