"""CoC7 幕间疗养 / 心理治疗的纯规则数学（规则书 p.129、p.167）。

只放数字与判定换算，不读写角色卡、不依赖房间时钟；编排在
:mod:`trpg_server.room_interlude`，时间来源是 :mod:`trpg_server.room_clock`。
"""

from __future__ import annotations

from typing import Any

SUCCESS_LEVELS = {"regular", "hard", "extreme", "critical", "成功"}
FUMBLE_LEVELS = {"fumble", "大失败"}

MONTH_DAYS = 30
SUCCESS_SAN_DIE = 3
FUMBLE_SAN_DIE = 6
INDEFINITE_MONTHS_DIE = 6


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: Any, low: int, high: int) -> int:
    return max(low, min(high, _int(value, low)))


def monthly_san_delta(level: Any, *, d3: Any = 1, d6: Any = 1) -> int:
    """一次月度精神分析检定的 SAN 变化。

    成功（regular 及以上）+1D3；大失败 −1D6；失败与不 Determination 均为 0。
    """
    key = str(level or "").strip().lower()
    if key in FUMBLE_LEVELS:
        return -_clamp(d6, 1, FUMBLE_SAN_DIE)
    if key in SUCCESS_LEVELS:
        return _clamp(d3, 1, SUCCESS_SAN_DIE)
    return 0


def indefinite_months(d6: Any = 1) -> int:
    """不定性疯狂持续时间：1D6 个月。"""
    return _clamp(d6, 1, INDEFINITE_MONTHS_DIE)


def months_from_days(days: Any) -> int:
    return max(0, _int(days)) // MONTH_DAYS


def recuperation_days(months: Any) -> int:
    return max(0, _int(months)) * MONTH_DAYS
