"""语音配音的结构化事件日志（JSONL）。

事件族统一以 ``voice_`` 开头，字段固定，便于 grep 与后续做用量面板：
room_id / message_id / turn / speaker / speaker_source / profile_id / engine /
text_chars / segment_count / latency_ms / degraded / reason。

日志失败绝不影响主叙事；文件超过阈值时保留最近一半，避免无限增长。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from trpg_server.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

EVENTS_PATH = RUNTIME_DIR / "voice" / "events.jsonl"
MAX_BYTES = 5 * 1024 * 1024

EVENT_FIELDS = (
    "room_id", "message_id", "turn", "speaker", "speaker_source", "profile_id",
    "engine", "text_chars", "segment_count", "latency_ms", "degraded", "reason",
    "model", "voice_id", "chars", "rtf", "audio_ms", "cache_key",
)


def emit(event: str, **fields: Any) -> dict[str, Any]:
    name = str(event or "").strip()[:80]
    if not name.startswith("voice_"):
        name = "voice_" + name
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": name,
    }
    for key in EVENT_FIELDS:
        value = fields.get(key)
        if isinstance(value, float):
            value = round(value, 1)
        if value is not None:
            entry[key] = value
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with EVENTS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("voice event log write failed", exc_info=True)
    logger.info(
        "%s room=%s message=%s turn=%s speaker=%s profile=%s engine=%s chars=%s segments=%s "
        "latency_ms=%s degraded=%s reason=%s",
        entry["event"],
        entry.get("room_id") or "-",
        entry.get("message_id") or "-",
        entry.get("turn") if entry.get("turn") is not None else "-",
        entry.get("speaker") or "-",
        entry.get("profile_id") or "-",
        entry.get("engine") or "-",
        entry.get("text_chars") if entry.get("text_chars") is not None else "-",
        entry.get("segment_count") if entry.get("segment_count") is not None else "-",
        entry.get("latency_ms") if entry.get("latency_ms") is not None else "-",
        entry.get("degraded") if entry.get("degraded") is not None else "-",
        entry.get("reason") or "-",
    )
    return entry


def _rotate_if_needed() -> None:
    try:
        if not EVENTS_PATH.exists() or EVENTS_PATH.stat().st_size < MAX_BYTES:
            return
        lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
        EVENTS_PATH.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        logger.debug("voice event rotation failed", exc_info=True)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    try:
        lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    values: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            values.append(data)
    return list(reversed(values))


def path() -> Path:
    return EVENTS_PATH
