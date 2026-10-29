"""房间 AI 用量与花费统计。

每个房间在自身目录下维护 ``usage.json``，按平台/模型单价累计 token 与费用。
单价来自 AI 平台配置中的模型字段（每百万 token）：

- ``input_price`` / ``output_price``：普通输入、输出单价；
- ``cache_hit_price``：缓存命中输入单价，留空时按 ``input_price`` 计；
- ``peak_hours`` / ``peak_multiplier`` / ``offpeak_multiplier``：峰谷计价，
  ``peak_hours`` 形如 ``HH:MM-HH:MM``（支持跨午夜），留空则不区分峰谷；
  命中峰时时段时全部费用乘以峰时倍率，其余时间乘以谷时倍率，默认 1。

货币来自平台配置的 ``config.currency``（默认 CNY）。未配置单价的模型只统计 token。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic

USAGE_FILENAME = "usage.json"
DEFAULT_CURRENCY = "CNY"
PRICE_UNIT = 1_000_000
_PEAK_WINDOW_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~—–至]\s*(\d{1,2}):(\d{2})")

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


def _peak_window(peak_hours: Any) -> tuple[int, int] | None:
    """解析 ``HH:MM-HH:MM`` 为分钟区间；留空或格式错误返回 None（不分峰谷）。"""
    if not isinstance(peak_hours, str):
        return None
    match = _PEAK_WINDOW_RE.search(peak_hours.strip())
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = (int(item) for item in match.groups())
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def _peak_multiplier(pricing: dict[str, Any], now: time.struct_time | None = None) -> float:
    """返回当前时刻适用的价格倍率；未配置峰谷时段时恒为 1。"""
    window = _peak_window(pricing.get("peak_hours"))
    if window is None:
        return 1.0
    start, end = window
    current = now or time.localtime()
    minutes = current.tm_hour * 60 + current.tm_min
    in_peak = start <= minutes < end if start <= end else (minutes >= start or minutes < end)
    multiplier = pricing.get("peak_multiplier" if in_peak else "offpeak_multiplier")
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        return 1.0
    return max(0.0, float(multiplier))


def _platform_pricing(config_dir: Path | None, provider: str, model: str) -> dict[str, Any]:
    """返回模型的计价配置；未配置单价时价格字段为 None。"""
    pricing: dict[str, Any] = {
        "input_price": None,
        "output_price": None,
        "cache_hit_price": None,
        "peak_hours": "",
        "peak_multiplier": 1.0,
        "offpeak_multiplier": 1.0,
        "currency": DEFAULT_CURRENCY,
    }
    if not config_dir or not provider:
        return pricing
    path = Path(config_dir) / "aiplatform" / f"{provider}.json"
    if not path.exists():
        return pricing
    config = read_json(path, default={})
    if not isinstance(config, dict):
        return pricing
    raw_section = config.get("config")
    config_section: dict[str, Any] = raw_section if isinstance(raw_section, dict) else {}
    pricing["currency"] = (
        str(config_section.get("currency") or config.get("currency") or DEFAULT_CURRENCY).strip()
        or DEFAULT_CURRENCY
    )
    for item in config.get("models") or []:
        if not isinstance(item, dict) or str(item.get("id") or "") != str(model or ""):
            continue
        for field in ("input_price", "output_price", "cache_hit_price"):
            if item.get(field) is not None:
                pricing[field] = _as_price(item.get(field))
        pricing["peak_hours"] = str(item.get("peak_hours") or "").strip()
        for field in ("peak_multiplier", "offpeak_multiplier"):
            if item.get(field) is not None:
                pricing[field] = _as_price(item.get(field))
        break
    return pricing


def _request_cost(
    pricing: dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    now: time.struct_time | None = None,
) -> float:
    """按单价计算一次请求费用；缓存命中部分使用缓存价，留空则沿用输入价。"""
    input_price = pricing.get("input_price")
    output_price = pricing.get("output_price")
    if input_price is None and output_price is None:
        return 0.0
    cached = min(max(0, cached_tokens), max(0, prompt_tokens))
    billable_prompt = max(0, prompt_tokens - cached)
    cache_price = pricing.get("cache_hit_price")
    if cache_price is None:
        cache_price = input_price or 0.0
    cost = (
        (billable_prompt / PRICE_UNIT) * (input_price or 0.0)
        + (cached / PRICE_UNIT) * cache_price
        + (completion_tokens / PRICE_UNIT) * (output_price or 0.0)
    )
    return cost * _peak_multiplier(pricing, now)


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
    pricing = _platform_pricing(config_dir, provider, model)
    currency = pricing["currency"]
    cost = _request_cost(pricing, prompt_tokens, completion_tokens, cached_tokens)

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
