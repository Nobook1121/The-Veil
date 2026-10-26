"""房间 AI 用量与花费统计。

每个房间在自身目录下维护 ``usage.json``，按平台/模型单价累计 token 与费用。
单价来自 AI 平台配置中的模型字段 ``input_price`` / ``output_price``（每百万 token），
货币来自平台配置的 ``config.currency``（默认 CNY）。未配置单价的模型只统计 token。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic

USAGE_FILENAME = "usage.json"
DEFAULT_CURRENCY = "CNY"
PRICE_UNIT = 1_000_000

_LOCK = threading.RLock()


def usage_file(room_dir: Path) -> Path:
    return Path(room_dir) / USAGE_FILENAME


def empty_usage(room_id: str = "") -> dict[str, Any]:
    return {
        "room_id": room_id,
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "costs": {},
        "models": {},
        "updated_at": "",
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_price(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _normalize(raw: Any, room_id: str = "") -> dict[str, Any]:
    data = empty_usage(room_id)
    if not isinstance(raw, dict):
        return data
    for key in ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        data[key] = _as_int(raw.get(key))
    for key, value in (raw.get("costs") or {}).items():
        amount = _as_price(value)
        if amount:
            data["costs"][str(key)] = round(amount, 6)
    models = raw.get("models")
    if isinstance(models, dict):
        data["models"] = {
            str(name): {
                "requests": _as_int(item.get("requests")),
                "prompt_tokens": _as_int(item.get("prompt_tokens")),
                "completion_tokens": _as_int(item.get("completion_tokens")),
                "total_tokens": _as_int(item.get("total_tokens")),
                "costs": {
                    str(currency): round(_as_price(amount), 6)
                    for currency, amount in (item.get("costs") or {}).items()
                    if _as_price(amount)
                },
            }
            for name, item in models.items()
            if isinstance(item, dict)
        }
    data["updated_at"] = str(raw.get("updated_at") or "")
    return data


def load_room_usage(room_dir: Path, room_id: str = "") -> dict[str, Any]:
    """读取房间累计用量；文件不存在或损坏时返回零值。"""
    if not room_dir:
        return empty_usage(room_id)
    try:
        raw = read_json(usage_file(room_dir), default={})
    except Exception:
        raw = {}
    return _normalize(raw, room_id or str(Path(room_dir).name))


def _platform_pricing(config_dir: Path | None, provider: str, model: str) -> tuple[float, float, str]:
    """返回（输入单价, 输出单价, 货币）。未配置时单价为 0。"""
    if not config_dir or not provider:
        return 0.0, 0.0, DEFAULT_CURRENCY
    path = Path(config_dir) / "aiplatform" / f"{provider}.json"
    if not path.exists():
        return 0.0, 0.0, DEFAULT_CURRENCY
    config = read_json(path, default={})
    if not isinstance(config, dict):
        return 0.0, 0.0, DEFAULT_CURRENCY
    raw_section = config.get("config")
    config_section: dict[str, Any] = raw_section if isinstance(raw_section, dict) else {}
    currency = str(config_section.get("currency") or config.get("currency") or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY
    for item in config.get("models") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == str(model or ""):
            return _as_price(item.get("input_price")), _as_price(item.get("output_price")), currency
    return 0.0, 0.0, currency


def record_room_usage(room_dir: Path, usage: dict[str, Any], *, config_dir: Path | None = None) -> dict[str, Any]:
    """把一次 AI 请求的用量累加到房间 ``usage.json``。

    调用方应在 Flask 应用上下文中传入 ``config_dir``；统计失败不应影响主请求。
    """
    if not room_dir or not isinstance(usage, dict):
        return empty_usage()

    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    total_tokens = _as_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
    cached_tokens = _as_int(usage.get("cached_tokens"))
    if not (prompt_tokens or completion_tokens or total_tokens or cached_tokens):
        return load_room_usage(room_dir)

    provider = str(usage.get("provider") or "").strip()
    model = str(usage.get("model") or "").strip()
    input_price, output_price, currency = _platform_pricing(config_dir, provider, model)
    cost = (prompt_tokens / PRICE_UNIT) * input_price + (completion_tokens / PRICE_UNIT) * output_price

    model_key = f"{provider}/{model}" if provider or model else "unknown"
    with _LOCK:
        data = load_room_usage(room_dir)
        data["requests"] += 1
        data["prompt_tokens"] += prompt_tokens
        data["completion_tokens"] += completion_tokens
        data["total_tokens"] += total_tokens
        data["cached_tokens"] += cached_tokens
        if cost:
            data["costs"][currency] = round(float(data["costs"].get(currency, 0.0)) + cost, 6)
        model_entry = data["models"].setdefault(
            model_key,
            {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "costs": {}},
        )
        model_entry["requests"] += 1
        model_entry["prompt_tokens"] += prompt_tokens
        model_entry["completion_tokens"] += completion_tokens
        model_entry["total_tokens"] += total_tokens
        if cost:
            model_entry["costs"][currency] = round(float(model_entry["costs"].get(currency, 0.0)) + cost, 6)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json_atomic(usage_file(room_dir), data)
        return data
