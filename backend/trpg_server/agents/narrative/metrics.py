"""房间级轻量指标（设计文档 §10.6）。

计数器写入房间目录的 `metrics.json`；平均值用「累计值 + 样本数」计算，读取时
换算成 `avg_*` 字段。写失败只记日志，不影响主请求。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Mapping

from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

METRICS_FILENAME = "metrics.json"

COUNTERS = (
    "turn_count",
    "guard_denied",
    "guard_violation",
    "spoiler_denied",
    "jev_model_calls",
    "jev_timeouts",
    "budget_degraded",
    "retrieval_hits",
    "cache_hits",
    "free_moves",
    "interactions_resolved",
    "triggers_fired",
    "triggers_denied",
    "trigger_dedupe",
    "media_missing",
    "visibility_needs_review",
    "opening_fallbacks",
)

AVERAGES = (
    "scope_ms",
    "context_tokens",
    "latency_ms",
)

_lock = threading.Lock()


def metrics_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / METRICS_FILENAME


def load_metrics(room_dir: Path | str) -> dict[str, Any]:
    try:
        payload = read_json(metrics_path(room_dir), default={})
    except ValueError:
        logger.warning("narrative.metrics.corrupt path=%s", metrics_path(room_dir))
        payload = {}
    metrics = dict(payload) if isinstance(payload, dict) else {}
    for name in COUNTERS:
        metrics.setdefault(name, 0)
    for name in AVERAGES:
        total = metrics.get(f"_{name}_total")
        samples = metrics.get(f"_{name}_samples")
        if isinstance(total, (int, float)) and isinstance(samples, (int, float)) and samples > 0:
            metrics[f"avg_{name}"] = round(float(total) / float(samples), 2)
    return metrics


def bump(room_dir: Path | str, **deltas: int) -> dict[str, Any]:
    """累加整数计数器；未知键忽略，返回最新指标。"""
    clean = {key: int(value) for key, value in deltas.items() if key in COUNTERS and isinstance(value, (int, float))}
    if not clean:
        return load_metrics(room_dir)
    with _lock:
        metrics = load_metrics(room_dir)
        for key, value in clean.items():
            metrics[key] = int(metrics.get(key) or 0) + value
        _write(room_dir, metrics)
    return metrics


def observe(room_dir: Path | str, samples: Mapping[str, float]) -> None:
    """记录平均值样本（毫秒 / token 数）。"""
    clean = {
        key: float(value)
        for key, value in samples.items()
        if key in AVERAGES and isinstance(value, (int, float)) and value >= 0
    }
    if not clean:
        return
    with _lock:
        metrics = load_metrics(room_dir)
        for key, value in clean.items():
            metrics[f"_{key}_total"] = round(float(metrics.get(f"_{key}_total") or 0) + value, 3)
            metrics[f"_{key}_samples"] = int(metrics.get(f"_{key}_samples") or 0) + 1
        _write(room_dir, metrics)


def _write(room_dir: Path | str, metrics: dict[str, Any]) -> None:
    path = metrics_path(room_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, metrics)
    except Exception:  # noqa: BLE001 - 指标失败不能影响房间主流程
        logger.warning("narrative.metrics.write_failed path=%s", path)


__all__ = ["AVERAGES", "COUNTERS", "METRICS_FILENAME", "bump", "load_metrics", "metrics_path", "observe"]
