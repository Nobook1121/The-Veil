"""AI 平台 / 本地缓存 / 用量计数运维（控制台专用）。

平台配置读写复用 :mod:`trpg_server.ai_platform_config` 与
:mod:`trpg_server.ai_capabilities`；本地缓存实例定义在 ``routes/chat.py``，此处惰性
导入，避免 console 与 routes 之间形成模块级循环依赖。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from trpg_server.agents.ai_request import PROBE_MAX_TOKENS
from trpg_server.agents.cache_probe import select_platform_model
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.ai_capabilities import chat_completions_endpoint, probe_responses_api
from trpg_server.ai_platform_config import (
    load_platform_config,
    load_public_platform_config,
    ordered_platform_paths,
    save_platform_config,
)
from trpg_server.settings import AI_PLATFORM_SECRET_DIR, CONFIG_DIR, LOGS_DIR
from trpg_server.agents.telemetry import USAGE_FILENAME

logger = logging.getLogger(__name__)


def _resolve_dirs(platform_dir: Any, secret_dir: Any) -> tuple[Path, Path]:
    resolved_platform = Path(platform_dir) if platform_dir else Path(CONFIG_DIR / "aiplatform")
    resolved_secret = Path(secret_dir) if secret_dir else Path(AI_PLATFORM_SECRET_DIR)
    return resolved_platform, resolved_secret


def list_platforms(platform_dir: Any = None, secret_dir: Any = None) -> list[dict[str, Any]]:
    """列出全部平台：ID / 启用 / 顺序 / 模型（不回传密钥）。"""

    resolved_platform, resolved_secret = _resolve_dirs(platform_dir, secret_dir)
    items: list[dict[str, Any]] = []
    for path in ordered_platform_paths(resolved_platform):
        if path.name == "default-request.json":
            continue
        try:
            config = load_public_platform_config(path, resolved_secret / path.name)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to load platform config: %s", path.name)
            continue
        models = config.get("models") if isinstance(config.get("models"), list) else []
        items.append(
            {
                "id": path.stem,
                "enabled": bool(config.get("enabled", False)),
                "order": config.get("order"),
                "models": [str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id")],
            }
        )
    return items


def set_platform_enabled(platform: str, enabled: bool, platform_dir: Any = None, secret_dir: Any = None) -> dict[str, Any]:
    """启用 / 停用某平台（写回配置文件）。"""

    resolved_platform, resolved_secret = _resolve_dirs(platform_dir, secret_dir)
    config_path = resolved_platform / f"{platform}.json"
    if not config_path.exists():
        raise LookupError(f"platform not found: {platform}")
    secret_path = resolved_secret / f"{platform}.json"
    config = load_platform_config(config_path, secret_path)
    config["enabled"] = bool(enabled)
    save_platform_config(config_path, secret_path, config)
    return {"id": platform, "enabled": bool(enabled)}


def test_platform(platform: str, platform_dir: Any = None, secret_dir: Any = None, post: Any = None) -> dict[str, Any]:
    """对指定平台做一次最小对话请求，验证连通性。"""

    resolved_platform, resolved_secret = _resolve_dirs(platform_dir, secret_dir)
    config_path = resolved_platform / f"{platform}.json"
    if not config_path.exists():
        raise LookupError(f"platform not found: {platform}")
    config = load_platform_config(config_path, resolved_secret / f"{platform}.json")
    section = config.get("config") if isinstance(config.get("config"), dict) else {}
    api_key = section.get("api_key") or ("lm-studio" if platform == "lmstudio" else None)
    endpoint = chat_completions_endpoint(section.get("base_url"))
    if not endpoint:
        raise ValueError("base_url is not set")
    model = select_platform_model(config)

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key or ''}"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
    }
    sender = post or requests.post
    runtime_config = load_ai_runtime_config(CONFIG_DIR)
    try:
        response = post_with_retry(
            endpoint,
            headers=headers,
            payload=payload,
            timeout=30,
            max_retries=runtime_config.ai_request_max_retries,
            post=sender,
            label="platform_test",
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常转为可读结果
        logger.warning("AI 平台连通性测试失败 platform=%s: %s", platform, exc)
        return {"ok": False, "status": None, "detail": str(exc), "endpoint": endpoint, "model": model}
    status = getattr(response, "status_code", None)
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        data = None
    if status != 200:
        detail = "request failed"
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            detail = str(data["error"].get("message") or detail)
        return {"ok": False, "status": status, "detail": detail, "endpoint": endpoint, "model": model}
    return {"ok": True, "status": status, "detail": "ok", "endpoint": endpoint, "model": model}


def detect_responses(platform: str, platform_dir: Any = None, secret_dir: Any = None) -> dict[str, Any]:
    """探测平台是否支持 Responses API，并把结论写回配置。"""

    resolved_platform, resolved_secret = _resolve_dirs(platform_dir, secret_dir)
    config_path = resolved_platform / f"{platform}.json"
    if not config_path.exists():
        raise LookupError(f"platform not found: {platform}")
    secret_path = resolved_secret / f"{platform}.json"
    config = load_platform_config(config_path, secret_path)
    section = config.get("config") if isinstance(config.get("config"), dict) else {}
    api_key = section.get("api_key") or ("lm-studio" if platform == "lmstudio" else None)
    base_url = section.get("base_url")
    if not base_url:
        raise ValueError("base_url is not set")
    model = select_platform_model(config)

    result = probe_responses_api(base_url, api_key, model)
    supported = bool(result.get("supported"))
    section.pop("api_key", None)
    section["responses_api_supported"] = supported
    section["responses_api_detail"] = str(result.get("detail") or "")
    if not supported:
        section["use_previous_response_id"] = False
    config["config"] = section
    save_platform_config(config_path, secret_path, config)
    return {"id": platform, "supported": supported, "detail": str(result.get("detail") or "")}


def _caches() -> dict[str, Any]:
    from trpg_server.routes.chat import _EXACT_CACHE, _PREFIX_CACHE, _SEMANTIC_CACHE

    return {"prefix": _PREFIX_CACHE, "exact": _EXACT_CACHE, "semantic": _SEMANTIC_CACHE}


def cache_stats() -> dict[str, Any]:
    """本地缓存（前缀 / 精确 / 语义）当前存活条目与命中数。"""

    try:
        caches = _caches()
    except Exception:  # noqa: BLE001 - 未加载 chat 模块时视为不可用
        logger.debug("AI caches unavailable", exc_info=True)
        return {}
    return {name: cache.snapshot() for name, cache in caches.items()}


def clear_caches() -> dict[str, int]:
    """清空本地缓存，返回各层被清除的条目数。"""

    try:
        caches = _caches()
    except Exception:  # noqa: BLE001
        logger.debug("AI caches unavailable", exc_info=True)
        return {}
    return {name: int(cache.clear()) for name, cache in caches.items()}


def reset_usage(log_dir: Any = None, day: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    """删除使用日志中某一天（默认今天）的记录，用于重置用量计数。

    ``agent_id`` 非空时只删除该 agent 的记录（对应 ``ai usage reset <agent>``）；
    为空时删除当天全部记录（对应 ``ai usage reset all``）。
    """

    from datetime import date

    target_day = day or date.today().isoformat()
    target_agent = str(agent_id) if agent_id else None
    path = Path(log_dir or LOGS_DIR) / USAGE_FILENAME
    if not path.exists():
        return {"day": target_day, "agent_id": target_agent, "removed": 0, "kept": 0}
    removed = 0
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        item_day = str(item.get("day") or str(item.get("timestamp", ""))[:10])
        item_agent = str(item.get("agent_id") or "")
        if item_day == target_day and (target_agent is None or item_agent == target_agent):
            removed += 1
        else:
            kept.append(line)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return {"day": target_day, "agent_id": target_agent, "removed": removed, "kept": len(kept)}