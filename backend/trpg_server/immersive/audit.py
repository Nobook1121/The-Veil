"""沉浸系统的持久化审计日志（JSONL）。

管理员对生图平台、密钥、预算、音乐导演的每一次写操作都要可追溯：谁改的、
改了什么（脱敏后）、结果如何。日志追加写入 ``data/runtime/immersive/audit.jsonl``，
与房间目录无关，便于按时间检索；密钥类字段只记录「已配置/未配置」，绝不回显。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from trpg_server.logging_config import redact_sensitive
from trpg_server.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

AUDIT_PATH = RUNTIME_DIR / "immersive" / "audit.jsonl"


def write_path() -> Path:
    return AUDIT_PATH


def record(
    action: str,
    *,
    actor: dict[str, Any] | str | None = None,
    target: str = "",
    result: str = "ok",
    detail: dict[str, Any] | None = None,
    error: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    """追加一条审计记录并返回写入的内容；失败绝不影响主流程。"""
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": str(action or "")[:120],
        "actor": _actor_text(actor),
        "target": str(target or "")[:160],
        "result": str(result or "ok")[:40],
        "detail": redact_sensitive(detail or {}),
        "error": str(error or "")[:300],
    }
    try:
        target_path = path or AUDIT_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Failed to persist immersive audit log", exc_info=True)
    logger.info(
        "immersive_audit action=%s target=%s result=%s actor=%s error=%s",
        entry["action"], entry["target"], entry["result"], entry["actor"], entry["error"] or "-",
    )
    return entry


def read_recent(limit: int = 20, *, path: Path | None = None) -> list[dict[str, Any]]:
    """读取最近若干条审计记录（倒序），用于控制台排查。"""
    target = path or AUDIT_PATH
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)) :]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            items.append(value)
    return list(reversed(items))


def _actor_text(actor: dict[str, Any] | str | None) -> str:
    if isinstance(actor, dict):
        username = str(actor.get("username") or "").strip()
        user_id = actor.get("user_id")
        role = str(actor.get("role") or "").strip()
        parts = [part for part in (username, f"id={user_id}" if user_id is not None else "", role) if part]
        return " ".join(parts)[:80] or "未知操作者"
    return str(actor or "未知操作者")[:80]
