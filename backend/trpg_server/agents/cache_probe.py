"""上游前缀缓存探针：判定当前 AI 平台是否支持可观测的前缀缓存命中。

背景：部分平台（如 OpenAI、DeepSeek）会对「完全相同的前缀」复用缓存，并在
``usage`` 里回报命中 token；但很多 OpenAI 兼容网关要么不实现、要么实现了却不回报
``cached_tokens``。本地日志因此无法区分「真的没命中」和「平台压根不报」，界面上的
0% 命中率也就无从判断。

本探针做一次最小 A/B 实验：把同一段足够长的 payload 连发两次，比较第二次的
``usage`` 是否出现缓存命中 token。它只依赖文件系统（读取平台配置）与传入的 HTTP
发送函数，不依赖 Flask 请求上下文，便于在控制台线程与单测中直接调用。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import requests

from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.ai_platform_config import load_platform_config, ordered_platform_paths

logger = logging.getLogger(__name__)

# 探针正文：重复填充以越过各平台的最小可缓存前缀阈值（多平台在 1k token 量级）。
# 用可读的中文句子而非随机串，避免个别平台的内容过滤直接拒答。
_PROBE_FILLER_UNIT = "帷幕 TRPG 前缀缓存探测：本段用于验证相同前缀是否被服务端复用。"
DEFAULT_PROBE_FILLER_CHARS = 2000


def extract_cache_usage(data: Any) -> dict[str, Any] | None:
    """从一次响应里提取 prompt / cached token 用量。

    同时兼容 OpenAI 风格（``prompt_tokens_details.cached_tokens``）与 DeepSeek 风格
    （顶层 ``prompt_cache_hit_tokens``）。字段缺失时 ``cached_tokens`` 返回 ``None``，
    表示「平台没有回报缓存信息」，与「回报了但为 0」是两回事。
    """

    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = usage.get("prompt_tokens")
    cached_tokens: Any = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached_tokens = details.get("cached_tokens")
    elif usage.get("prompt_cache_hit_tokens") is not None:
        cached_tokens = usage.get("prompt_cache_hit_tokens")

    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return {
        "prompt_tokens": _as_int(prompt_tokens),
        "cached_tokens": _as_int(cached_tokens),
    }


def _build_probe_payload(model: str, filler_chars: int) -> dict[str, Any]:
    repeats = max(1, filler_chars // len(_PROBE_FILLER_UNIT) + 1)
    filler = (_PROBE_FILLER_UNIT * repeats)[:filler_chars]
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": filler},
            {"role": "user", "content": "只回复：pong"},
        ],
        "max_tokens": 16,
        "temperature": 0,
    }


def _post_json(
    sender: Callable[..., Any],
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
) -> tuple[int | None, Any]:
    try:
        response = sender(endpoint, headers=headers, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读结果
        logger.info("Cache probe request failed for %s: %s", endpoint, exc)
        return None, {"error": {"message": str(exc)}}
    try:
        return response.status_code, response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应同样视为无法判定
        return getattr(response, "status_code", None), None


def _error_detail(data: Any, status: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
    return f"HTTP {status}" if status else "no response"


def probe_prefix_cache(
    base_url: str,
    api_key: str | None,
    model: str,
    *,
    timeout: int = 30,
    filler_chars: int = DEFAULT_PROBE_FILLER_CHARS,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """对同一段 payload 连发两次，判断第二次是否命中前缀缓存。

    返回字典含：``ok``（两次请求是否都成功）、``supported``（是否可观测到缓存命中，
    三次取值：``True`` 命中、``False`` 明确未命中、``None`` 无法判定）、``first`` /
    ``second`` 各自用量、``endpoint`` 与 ``detail``（人类可读结论）。
    """

    endpoint = chat_completions_endpoint(base_url)
    if not endpoint:
        return {"ok": False, "supported": None, "detail": "base_url is not set", "endpoint": "", "model": model}
    if not model:
        return {"ok": False, "supported": None, "detail": "an enabled model is required", "endpoint": endpoint, "model": model}

    sender = post or requests.post
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key or ''}"}
    payload = _build_probe_payload(model, filler_chars)

    status, data = _post_json(sender, endpoint, headers, payload, timeout)
    if status != 200:
        return {
            "ok": False,
            "supported": None,
            "detail": f"first request failed: {_error_detail(data, status)}",
            "endpoint": endpoint,
            "model": model,
            "first": {"status": status, **({} if (u := extract_cache_usage(data)) is None else u)},
        }
    first = extract_cache_usage(data) or {"prompt_tokens": None, "cached_tokens": None}

    status, data = _post_json(sender, endpoint, headers, payload, timeout)
    if status != 200:
        return {
            "ok": False,
            "supported": None,
            "detail": f"second request failed: {_error_detail(data, status)}",
            "endpoint": endpoint,
            "model": model,
            "first": {"status": 200, **first},
        }
    second = extract_cache_usage(data) or {"prompt_tokens": None, "cached_tokens": None}

    if second["cached_tokens"] is None:
        detail = "平台未在 usage 中回报 cached_tokens，无法判定（该平台的前缀缓存不可观测）"
        supported: bool | None = None
    elif second["cached_tokens"] > 0:
        detail = f"第二次请求命中缓存 {second['cached_tokens']} tokens，平台支持可观测的前缀缓存"
        supported = True
    else:
        detail = "第二次请求 cached_tokens 为 0，未命中（平台回报了该字段，说明当前前缀未被复用）"
        supported = False

    return {
        "ok": True,
        "supported": supported,
        "detail": detail,
        "endpoint": endpoint,
        "model": model,
        "first": {"status": 200, **first},
        "second": {"status": 200, **second},
    }


def load_enabled_platforms(
    platform_dir: Path | None = None,
    secret_dir: Path | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """按加载顺序返回「已启用」平台：``[(platform_id, config), ...]``。

    与 Web 路由的 ``_load_enabled_platform`` 语义一致，但只读文件、不依赖 Flask
    上下文，便于控制台命令与单测使用。
    """

    from trpg_server.settings import AI_PLATFORM_SECRET_DIR, CONFIG_DIR

    resolved_platform_dir = Path(platform_dir) if platform_dir else CONFIG_DIR / "aiplatform"
    resolved_secret_dir = Path(secret_dir) if secret_dir else AI_PLATFORM_SECRET_DIR
    platforms: list[tuple[str, dict[str, Any]]] = []
    for path in ordered_platform_paths(resolved_platform_dir):
        try:
            config = load_platform_config(path, resolved_secret_dir / path.name)
        except (OSError, ValueError):
            logger.exception("Failed to read AI platform config: %s", path.name)
            continue
        if config.get("enabled", False):
            platforms.append((path.stem, config))
    return platforms


def select_platform_model(platform_config: dict[str, Any] | None) -> str:
    """挑出平台配置里第一个启用的模型 id（缺省 ``local-model``）。"""

    models = platform_config.get("models") if isinstance(platform_config, dict) else None
    if not isinstance(models, list) or not models:
        return "local-model"
    model = next((item for item in models if isinstance(item, dict) and item.get("enabled", True)), models[0])
    return str(model.get("id") or "local-model") if isinstance(model, dict) else "local-model"


def probe_enabled_platform(
    platform_id: str | None = None,
    *,
    platform_dir: Path | None = None,
    secret_dir: Path | None = None,
    timeout: int = 30,
    filler_chars: int = DEFAULT_PROBE_FILLER_CHARS,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """解析当前启用平台（或指定 ``platform_id``）并运行前缀缓存探针。"""

    platforms = load_enabled_platforms(platform_dir, secret_dir)
    if platform_id:
        platforms = [(pid, cfg) for pid, cfg in platforms if pid == platform_id]
        if not platforms:
            return {"ok": False, "supported": None, "detail": f"platform not enabled or not found: {platform_id}", "platform": platform_id}
    if not platforms:
        return {"ok": False, "supported": None, "detail": "no enabled AI platform", "platform": None}

    resolved_id, config = platforms[0]
    section = config.get("config") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    base_url = section.get("base_url")
    api_key = section.get("api_key")
    if not api_key and resolved_id == "lmstudio":
        api_key = "lm-studio"
    model = select_platform_model(config)

    result = probe_prefix_cache(
        str(base_url or ""),
        api_key,
        model,
        timeout=timeout,
        filler_chars=filler_chars,
        post=post,
    )
    result["platform"] = resolved_id
    return result