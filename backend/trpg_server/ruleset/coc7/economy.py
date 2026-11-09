"""5.12 信用评级、生活标准与开支（规则书 p.36-37、p.81-82）。

纯计算：按信用评级查现金 / 其他资产 / 消费水平，结算一次消费，以及幕间
信用评级的七种变动。不读写角色卡；调用方负责把现金与资产写回现有字段。

金额口径照抄书内表Ⅱ（1920s 与现代两套）。表中「消费水平」是每天可免费花费
的上限；超出部分必须从现金扣除，现金不足再动用其他资产。
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable, Mapping

ERA_1920S = "1920s"
ERA_MODERN = "modern"
ERAS = (ERA_1920S, ERA_MODERN)

_ERA_ALIASES = {
    "1920": ERA_1920S,
    "1920s": ERA_1920S,
    "classic": ERA_1920S,
    "modern": ERA_MODERN,
    "present": ERA_MODERN,
    "现代": ERA_MODERN,
}

# cash = credit * cash_factor + cash_flat；assets 同理。super rich 为定额。
_TIERS: dict[str, list[dict[str, Any]]] = {
    ERA_1920S: [
        {"key": "destitute", "label": "身无分文", "min": 0, "max": 0, "cash_factor": 0, "cash_flat": 0.5, "assets_factor": 0, "assets_flat": 0, "spending": 0.5},
        {"key": "poor", "label": "贫穷", "min": 1, "max": 9, "cash_factor": 1, "cash_flat": 0, "assets_factor": 10, "assets_flat": 0, "spending": 2},
        {"key": "standard", "label": "标准", "min": 10, "max": 49, "cash_factor": 2, "cash_flat": 0, "assets_factor": 50, "assets_flat": 0, "spending": 10},
        {"key": "comfortable", "label": "小康", "min": 50, "max": 89, "cash_factor": 5, "cash_flat": 0, "assets_factor": 500, "assets_flat": 0, "spending": 50},
        {"key": "wealthy", "label": "富裕", "min": 90, "max": 98, "cash_factor": 20, "cash_flat": 0, "assets_factor": 2000, "assets_flat": 0, "spending": 250},
        {"key": "super_rich", "label": "富豪", "min": 99, "max": 99, "cash_factor": 0, "cash_flat": 50000, "assets_factor": 0, "assets_flat": 5_000_000, "spending": 5000},
    ],
    ERA_MODERN: [
        {"key": "destitute", "label": "身无分文", "min": 0, "max": 0, "cash_factor": 0, "cash_flat": 10, "assets_factor": 0, "assets_flat": 0, "spending": 10},
        {"key": "poor", "label": "贫穷", "min": 1, "max": 9, "cash_factor": 20, "cash_flat": 0, "assets_factor": 200, "assets_flat": 0, "spending": 40},
        {"key": "standard", "label": "标准", "min": 10, "max": 49, "cash_factor": 40, "cash_flat": 0, "assets_factor": 1000, "assets_flat": 0, "spending": 200},
        {"key": "comfortable", "label": "小康", "min": 50, "max": 89, "cash_factor": 100, "cash_flat": 0, "assets_factor": 10000, "assets_flat": 0, "spending": 1000},
        {"key": "wealthy", "label": "富裕", "min": 90, "max": 98, "cash_factor": 400, "cash_flat": 0, "assets_factor": 40000, "assets_flat": 0, "spending": 5000},
        {"key": "super_rich", "label": "富豪", "min": 99, "max": 99, "cash_factor": 0, "cash_flat": 1_000_000, "assets_factor": 0, "assets_flat": 100_000_000, "spending": 100000},
    ],
}

CREDIT_EVENTS = {
    "inherited": "发家了：按新资产档次逐次 +1D10，直到信用评级进入该档",
    "promotion": "局势好转：信用评级 +1D6",
    "steady": "生活如常：信用评级不变",
    "belt_tightening": "勒紧腰带：信用评级 −1D10",
    "sell_assets": "变卖家产：信用评级 −1D10",
    "lost_income": "厄运缠身：每次幕间信用评级 −2D10，有社会保障时下限 1D10−1",
    "bankruptcy": "失败破产：信用评级 −1D100（最低 0）",
}


def _era_key(era: Any) -> str:
    text = str(era or ERA_1920S).strip().lower()
    if text not in _ERA_ALIASES:
        raise ValueError("era must be 1920s or modern")
    return _ERA_ALIASES[text]


def _tiers(era: Any) -> list[dict[str, Any]]:
    return _TIERS[_era_key(era)]


def _tier_for_credit(cr: int, era: Any) -> dict[str, Any]:
    tiers = _tiers(era)
    for tier in tiers:
        if tier["min"] <= cr <= tier["max"]:
            return tier
    return tiers[-1]


def _tier_rank(tier: Mapping[str, Any], era: Any) -> int:
    return next(index for index, item in enumerate(_tiers(era)) if item["key"] == tier["key"])


def tier_for_assets(assets: Any, era: str = ERA_1920S) -> dict[str, Any]:
    """按「其他资产」金额找到对应生活水平档次。"""
    value = max(0.0, float(assets or 0))
    tiers = _tiers(era)
    for tier in tiers[:-1]:
        ceiling = tier["assets_factor"] * tier["max"] + tier["assets_flat"]
        if value <= ceiling:
            return tier
    return tiers[-1]


def credit_profile(cr: Any, era: str = ERA_1920S) -> dict[str, Any]:
    """返回某信用评级下的现金、其他资产与每日消费水平。"""
    credit = max(0, min(99, int(cr or 0)))
    tier = _tier_for_credit(credit, era)
    cash = tier["cash_factor"] * credit + tier["cash_flat"]
    assets = tier["assets_factor"] * credit + tier["assets_flat"]
    return {
        "era": _era_key(era),
        "key": tier["key"],
        "label": tier["label"],
        "credit": credit,
        "cash": round(float(cash), 2),
        "assets": round(float(assets), 2),
        "spending": round(float(tier["spending"]), 2),
    }


def apply_expense(
    *,
    cash: Any,
    assets: Any,
    amount: Any,
    cr: Any,
    era: str = ERA_1920S,
) -> dict[str, Any]:
    """结算一次消费。

    不超过消费水平时不动现金；超过时全额扣现金，现金不足再扣其他资产。
    资产也不足时返回 ``uncovered``（差额），由 KP 决定借款或变卖。
    """
    profile = credit_profile(cr, era)
    spend = max(0.0, float(amount or 0))
    cash_before = max(0.0, float(cash or 0))
    assets_before = max(0.0, float(assets or 0))
    if spend <= profile["spending"]:
        return {
            "within_standard": True,
            "cash": round(cash_before, 2),
            "assets": round(assets_before, 2),
            "uncovered": 0.0,
            "spent_from_cash": 0.0,
            "spent_from_assets": 0.0,
        }
    cash_after = cash_before - spend
    spent_from_assets = 0.0
    uncovered = 0.0
    if cash_after < 0:
        shortfall = -cash_after
        spent_from_assets = min(assets_before, shortfall)
        uncovered = max(0.0, shortfall - spent_from_assets)
        cash_after = 0.0
    assets_after = assets_before - spent_from_assets
    return {
        "within_standard": False,
        "cash": round(cash_after, 2),
        "assets": round(assets_after, 2),
        "uncovered": round(uncovered, 2),
        "spent_from_cash": round(cash_before - cash_after, 2),
        "spent_from_assets": round(spent_from_assets, 2),
    }


_DICE_RE = re.compile(r"^(\d+)d(\d+)([+-]\d+)?$", re.IGNORECASE)


def roll_dice(expression: str, rng: Callable[[int], int] = random.randint) -> int:
    """掷 ``1D10`` / ``2D10`` / ``1D100`` 这类表达式；不支持嵌套。"""
    match = _DICE_RE.match(str(expression or "").strip())
    if not match:
        raise ValueError(f"unsupported dice expression: {expression}")
    count, sides = int(match.group(1)), int(match.group(2))
    modifier = int(match.group(3) or 0)
    return sum(int(rng(sides)) for _ in range(count)) + modifier


def apply_credit_event(
    cr: Any,
    event: str,
    *,
    rng: Callable[[int], int] = random.randint,
    era: str = ERA_1920S,
    target_assets: Any = None,
    has_social_security: bool = False,
) -> dict[str, Any]:
    """幕间信用评级变动。

    ``inherited`` 需要 ``target_assets``（继承后的资产额），按新档次逐次 +1D10。
    其余事件直接按书内骰值调整；结果夹在 0-99。
    """
    event_key = str(event or "").strip().lower()
    if event_key not in CREDIT_EVENTS:
        raise ValueError(f"unknown credit event: {event}")
    before = max(0, min(99, int(cr or 0)))
    rolls: list[int] = []
    floor = 0

    if event_key == "inherited":
        if target_assets is None:
            raise ValueError("target_assets is required for inherited")
        target_tier = tier_for_assets(target_assets, era)
        target_rank = _tier_rank(target_tier, era)
        current = before
        for _ in range(30):
            if _tier_rank(_tier_for_credit(current, era), era) >= target_rank or current >= 99:
                break
            step = roll_dice("1D10", rng)
            rolls.append(step)
            current = min(99, current + step)
    elif event_key == "promotion":
        step = roll_dice("1D6", rng)
        rolls.append(step)
        current = min(99, before + step)
    elif event_key == "steady":
        current = before
    elif event_key in {"belt_tightening", "sell_assets"}:
        step = roll_dice("1D10", rng)
        rolls.append(step)
        current = max(0, before - step)
    elif event_key == "lost_income":
        step = roll_dice("2D10", rng)
        rolls.append(step)
        current = max(0, before - step)
        if has_social_security:
            floor = max(0, roll_dice("1D10", rng) - 1)
            current = max(current, floor)
    else:  # bankruptcy
        step = roll_dice("1D100", rng)
        rolls.append(step)
        current = max(0, before - step)

    return {
        "event": event_key,
        "from": before,
        "to": int(current),
        "rolls": rolls,
        "floor": int(floor),
        "profile": credit_profile(current, era),
    }


def refresh_economy(
    cr: Any,
    *,
    era: str = ERA_1920S,
    current_assets: Any = None,
) -> dict[str, Any]:
    """幕间现金刷新：现金补到当前 CR 的余额；未给资产时按 CR 重算。"""
    profile = credit_profile(cr, era)
    assets = profile["assets"] if current_assets is None else max(0.0, float(current_assets))
    return {
        "cash": profile["cash"],
        "assets": round(assets, 2),
        "spending": profile["spending"],
        "profile": profile,
    }
