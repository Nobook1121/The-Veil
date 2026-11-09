"""年龄调整与 EDU 增强（规则书 p.36-37、p.83-84）。

两条流程不能混用：

- **建卡**（p.37 快速参考）：按年龄段直接给一次总修正；
- **老化**（p.84 5.15）：跨过 20/40/50/60/70/80 岁里程碑时累加效果。

EDU 增强检定：掷 1D100，结果大于当前 EDU 则 +1D10，EDU 不超过 99。
达到 20/40/50/60 岁各获得一次；70 岁与 80 岁不再获得；魔法造成的突然老化
不获得教育。

本模块只计算，不写角色卡；建卡页与幕间流程各自决定何时调用。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Mapping

# 建卡年龄段 → 总修正（p.37）。
AGE_BANDS: tuple[dict[str, Any], ...] = (
    {"label": "15-19", "min": 15, "max": 19, "str_siz": -5, "str_con_dex": 0, "app": 0, "mov": 0, "edu_points": -5, "edu_checks": 0, "luck_rolls": 2},
    {"label": "20-39", "min": 20, "max": 39, "str_siz": 0, "str_con_dex": 0, "app": 0, "mov": 0, "edu_points": 0, "edu_checks": 1, "luck_rolls": 1},
    {"label": "40-49", "min": 40, "max": 49, "str_siz": 0, "str_con_dex": -5, "app": -5, "mov": -1, "edu_points": 0, "edu_checks": 2, "luck_rolls": 1},
    {"label": "50-59", "min": 50, "max": 59, "str_siz": 0, "str_con_dex": -10, "app": -10, "mov": -2, "edu_points": 0, "edu_checks": 3, "luck_rolls": 1},
    {"label": "60-69", "min": 60, "max": 69, "str_siz": 0, "str_con_dex": -20, "app": -15, "mov": -3, "edu_points": 0, "edu_checks": 4, "luck_rolls": 1},
    {"label": "70-79", "min": 70, "max": 79, "str_siz": 0, "str_con_dex": -40, "app": -20, "mov": -4, "edu_points": 0, "edu_checks": 4, "luck_rolls": 1},
    {"label": "80-89", "min": 80, "max": 89, "str_siz": 0, "str_con_dex": -80, "app": -25, "mov": -5, "edu_points": 0, "edu_checks": 4, "luck_rolls": 1},
)

# 老化里程碑（p.84）；达到 20 岁时 STR/SIZ 合计 +5。
AGING_MILESTONES: dict[int, dict[str, int]] = {
    20: {"str_siz": 5, "str_con_dex": 0, "app": 0, "mov": 0, "edu_checks": 1},
    40: {"str_siz": 0, "str_con_dex": -5, "app": -5, "mov": -1, "edu_checks": 1},
    50: {"str_siz": 0, "str_con_dex": -5, "app": -5, "mov": -1, "edu_checks": 1},
    60: {"str_siz": 0, "str_con_dex": -10, "app": -5, "mov": -1, "edu_checks": 1},
    70: {"str_siz": 0, "str_con_dex": -20, "app": -5, "mov": -1, "edu_checks": 0},
    80: {"str_siz": 0, "str_con_dex": -40, "app": -5, "mov": -1, "edu_checks": 0},
}
# 80 岁之后每十年：STR/CON/DEX 合计 −80，MOV −1，不再获得 EDU。
DECADE_BEYOND_80 = {"str_siz": 0, "str_con_dex": -80, "app": 0, "mov": -1, "edu_checks": 0}


def _clamp_age(age: Any) -> int:
    return max(15, min(99, int(age or 25)))


def creation_age_effects(age: Any) -> dict[str, Any]:
    """建卡时的年龄修正；返回需要玩家手动分配的总点数。"""
    value = _clamp_age(age)
    band = next((item for item in AGE_BANDS if item["min"] <= value <= item["max"]), AGE_BANDS[-1])
    return {
        "age": value,
        "band": band["label"],
        "str_siz_total": int(band["str_siz"]),
        "str_con_dex_total": int(band["str_con_dex"]),
        "app": int(band["app"]),
        "mov": int(band["mov"]),
        "edu_points": int(band["edu_points"]),
        "edu_checks": int(band["edu_checks"]),
        "luck_rolls": int(band["luck_rolls"]),
        "reduction_total": abs(int(band["str_con_dex"])),
    }


def allocate_pool(
    total: Any,
    allocation: Mapping[str, Any] | None,
    allowed: tuple[str, ...],
) -> dict[str, int]:
    """把一组合计扣减分配到 ``allowed`` 中的属性；必须恰好用完。"""
    expected = max(0, int(total or 0))
    result: dict[str, int] = {key: 0 for key in allowed}
    for key, value in (allocation or {}).items():
        upper = str(key).strip().upper()
        if upper not in result:
            raise ValueError(f"allocation only supports {', '.join(allowed)}")
        result[upper] = max(0, int(value or 0))
    if sum(result.values()) != expected:
        raise ValueError(f"allocation must add up to {expected}")
    return result


def allocate_reduction(total: Any, allocation: Mapping[str, Any] | None) -> dict[str, int]:
    """把 STR/CON/DEX 的合计扣减分配到具体属性；必须恰好用完。"""
    return allocate_pool(total, allocation, ("STR", "CON", "DEX"))


def allocate_str_siz_reduction(total: Any, allocation: Mapping[str, Any] | None) -> dict[str, int]:
    """15-19 岁建卡的 STR/SIZ 合计扣减分配；必须恰好用完。"""
    return allocate_pool(total, allocation, ("STR", "SIZ"))


def apply_creation_age_adjustment(
    age: Any,
    *,
    str_siz_allocation: Mapping[str, Any] | None = None,
    str_con_dex_allocation: Mapping[str, Any] | None = None,
    edu_gain: Any = 0,
) -> dict[str, Any]:
    """把建卡年龄修正整理成属性增量。

    ``mov`` 不放进增量：移动力由年龄与 STR/DEX 派生，调用方重算派生值即可，
    避免与既有派生公式重复扣减。EDU 增量 = 年龄段固定修正 + 逐次增强检定累计。
    """
    effects = creation_age_effects(age)
    str_siz = allocate_str_siz_reduction(abs(effects["str_siz_total"]), str_siz_allocation)
    str_con_dex = allocate_reduction(effects["reduction_total"], str_con_dex_allocation)
    gain = max(0, int(edu_gain or 0))
    deltas = {
        "STR": -(str_siz["STR"] + str_con_dex["STR"]),
        "SIZ": -str_siz["SIZ"],
        "CON": -str_con_dex["CON"],
        "DEX": -str_con_dex["DEX"],
        "APP": effects["app"],
        "EDU": effects["edu_points"] + gain,
    }
    return {
        "age": effects["age"],
        "band": effects["band"],
        "effects": effects,
        "deltas": deltas,
        "str_siz_allocation": str_siz,
        "str_con_dex_allocation": str_con_dex,
        "edu_gain": gain,
    }


def resolve_edu_enhancement(
    edu: Any,
    *,
    attempts: Any,
    rng: Callable[[int], int] = random.randint,
) -> dict[str, Any]:
    """执行若干次 EDU 增强检定；EDU 不超过 99。"""
    current = max(0, min(99, int(edu or 0)))
    start = current
    records: list[dict[str, int]] = []
    for _ in range(max(0, int(attempts or 0))):
        if current >= 99:
            break
        roll = int(rng(100))
        gain = 0
        if roll > current:
            gain = min(99 - current, max(0, int(rng(10))))
        records.append({"roll": roll, "gain": gain})
        current += gain
    return {"from": start, "to": current, "attempts": records, "checks": len(records)}


def aging_effects(
    from_age: Any,
    to_age: Any,
    *,
    grant_edu: bool = True,
) -> dict[str, Any]:
    """跨里程碑的老化效果；``grant_edu=False`` 用于魔法造成的突然老化。"""
    start = _clamp_age(from_age)
    end = _clamp_age(to_age)
    if end <= start:
        return {
            "from": start,
            "to": end,
            "milestones": [],
            "str_siz_total": 0,
            "str_con_dex_total": 0,
            "app": 0,
            "mov": 0,
            "edu_checks": 0,
            "reduction_total": 0,
        }

    crossed: list[int] = [age for age in AGING_MILESTONES if start < age <= end]
    decade = 90
    while decade <= end:
        if decade > start:
            crossed.append(decade)
        decade += 10

    totals = {"str_siz": 0, "str_con_dex": 0, "app": 0, "mov": 0, "edu_checks": 0}
    milestones: list[dict[str, Any]] = []
    for age in sorted(crossed):
        effect = AGING_MILESTONES.get(age, DECADE_BEYOND_80)
        for key in totals:
            totals[key] += int(effect[key])
        milestones.append({"age": age, **effect})

    if not grant_edu:
        totals["edu_checks"] = 0
    return {
        "from": start,
        "to": end,
        "milestones": milestones,
        "str_siz_total": totals["str_siz"],
        "str_con_dex_total": totals["str_con_dex"],
        "app": totals["app"],
        "mov": totals["mov"],
        "edu_checks": totals["edu_checks"],
        "reduction_total": abs(totals["str_con_dex"]),
    }
