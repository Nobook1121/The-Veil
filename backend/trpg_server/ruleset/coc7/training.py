"""CoC7 可选「幕间训练」规则的纯计算。

边界：
- 本模块只做周数换算与成长检定，不读写房间 / 角色卡；记录落盘、权限与
  留痕由 ``room_interlude`` / ``room_rules`` 负责。
- 训练是可选项（``coc7_growth_training`` 房规开关，默认关闭）：幕间由 KP
  批准投入的周数，每 ``WEEKS_PER_GAIN`` 周获得一次成长检定机会。
- 成长检定与幕间成长同一公式（见 :mod:`trpg_server.ruleset.coc7.growth`）：
  1D100 > 当前技能值（或 >95）则 +1D10，受 ``SKILL_MAX`` 上限约束。
- 时间单位默认「周」（对应计划里的 ``training.time_unit=week``）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trpg_server.ruleset.coc7.growth import growth_check
from trpg_server.ruleset.coc7.rules import SKILL_MAX

# 训练计时的默认单位（周）；数据里只记周数，避免房间时钟误差。
TIME_UNIT = "week"
TIME_UNITS = ("day", "week", "month")
TIME_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}

# 每多少周获得一次成长检定机会。
WEEKS_PER_GAIN = 4
# 单次训练请求最多批准的周数（防止一次投入把技能刷满）。
MAX_TRAINING_WEEKS = 12
# 角色卡 extensions.training.records 的保留条数。
MAX_RECORDS = 12


def normalize_weeks(weeks: Any) -> int:
    """把请求周数夹到 1..MAX_TRAINING_WEEKS；非法值按 1 处理。"""
    try:
        value = int(weeks)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_TRAINING_WEEKS, value))


def weeks_to_days(weeks: Any, unit: str = TIME_UNIT) -> int:
    """按训练单位换算天数；未知单位按周处理。"""
    amount = normalize_weeks(weeks)
    return amount * TIME_UNIT_DAYS.get(str(unit or TIME_UNIT), TIME_UNIT_DAYS[TIME_UNIT])


def training_checks(weeks: Any) -> int:
    """本次训练能获得几次成长检定机会。"""
    return normalize_weeks(weeks) // WEEKS_PER_GAIN


def train_skill(
    value: Any,
    *,
    weeks: Any,
    rng: Callable[[int], int],
) -> dict[str, Any]:
    """结算一次训练：按周数掷成长检定，返回前后值、每次检定与总增益。

    ``rng`` 与其它规则模块一致：``rng(100)`` 掷 d100，``rng(10)`` 掷 d10。
    """
    before = max(0, min(SKILL_MAX, int(value or 0)))
    weeks_used = normalize_weeks(weeks)
    checks: list[dict[str, Any]] = []
    current = before
    for _ in range(training_checks(weeks_used)):
        outcome = growth_check(current, roll=rng(100), gain_roll=rng(10))
        current = outcome["after"]
        checks.append(
            {
                "roll": outcome["roll"],
                "improved": outcome["improved"],
                "gain": outcome["gain"],
                "after": outcome["after"],
            }
        )
    return {
        "before": before,
        "weeks": weeks_used,
        "checks": checks,
        "improved": current > before,
        "gain": current - before,
        "after": current,
        "cap": SKILL_MAX,
    }


__all__ = [
    "MAX_RECORDS",
    "MAX_TRAINING_WEEKS",
    "TIME_UNITS",
    "TIME_UNIT",
    "WEEKS_PER_GAIN",
    "normalize_weeks",
    "train_skill",
    "training_checks",
    "weeks_to_days",
]
