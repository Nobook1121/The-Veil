"""房间级轨迹文件（设计文档 §10.5）。

每回合向 `narrative_trace.jsonl` 追加一行，只保留最近 2000 条；不存正文，
只存 id、计数与耗时，供房间控制台 `narrative trace` 排障。整体删除不影响
永久记忆（账簿与关键帧）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TRACE_FILENAME = "narrative_trace.jsonl"
TRACE_LIMIT = 2000


def trace_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / TRACE_FILENAME


def append_trace(room_dir: Path | str, payload: Mapping[str, Any], *, limit: int = TRACE_LIMIT) -> None:
    """追加一行轨迹；写失败只记日志，绝不影响主请求。"""
    path = trace_path(room_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
        _trim(path, limit)
    except OSError:
        logger.warning("narrative.trace.write_failed path=%s", path)


def _trim(path: Path, limit: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= limit:
        return
    try:
        path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("narrative.trace.trim_failed path=%s", path)


def read_trace(
    room_dir: Path | str,
    *,
    turn: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """读取轨迹；按回合过滤后返回最后 `limit` 条。"""
    path = trace_path(room_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if turn is not None and int(item.get("turn") or 0) != int(turn):
                continue
            rows.append(item)
    except OSError:
        return []
    if limit > 0:
        rows = rows[-limit:]
    return rows


__all__ = ["TRACE_FILENAME", "TRACE_LIMIT", "append_trace", "read_trace", "trace_path"]
