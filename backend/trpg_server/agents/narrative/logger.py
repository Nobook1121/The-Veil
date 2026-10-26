"""叙事层的结构化日志（设计文档 §7.9）。

统一走 `trpg_server` logger，事件名以 `narrative.` 开头；不记录正文、
不记录 KP-only 内容，避免日志成为剧透渠道。
"""
from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger("trpg_server.narrative")

_SENSITIVE_KEYS = {
    "kp_notes",
    "body",
    "content",
    "text",
    "prompt",
    "message",
    "authorization",
    "token",
    "api_key",
    "secret",
    "password",
}


def _safe_value(key: str, value: Any) -> Any:
    if key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, str) and len(value) > 120:
        return value[:117] + "..."
    return value


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    payload = {key: _safe_value(key, value) for key, value in fields.items()}
    name = event if event.startswith("narrative.") else f"narrative.{event}"
    if payload:
        _logger.log(level, "%s %s", name, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _logger.log(level, "%s", name)


def log_warning(event: str, **fields: Any) -> None:
    log_event(event, level=logging.WARNING, **fields)
