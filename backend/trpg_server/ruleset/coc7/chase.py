"""CoC7 第七章「追逐」纯规则。

依据：守秘人规则书第七章（p.112-117，「追逐」第一至第四部分）。
本模块只做数学与判定口径，不读写房间状态、不掷骰、不落盘：

- 追逐用抽象地点记录位置，相邻地点距离 1，初始距离 2（p.112-113）。
- 行动点 = 1 + max(0, 自己 MOV - 全场最低 MOV)；移动 1 个地点消耗 1 点（p.113）。
- 险境：可花 1 点换 1 个奖励骰，单次最多 2 个；失败损失 1D3 行动点，
  并按 KP 选定的伤害等级结算伤害（p.114）。
- 障碍：通过前不能前进；耐久归零后不再是障碍（p.115-116）。
- 冲突：除火器外只能攻击同地点目标，攻击消耗 1 点行动点（p.116）。
- 载具：每点体格 1D10 伤害；撞毁障碍后按障碍原耐久一半受反冲伤害（p.115-116）。
"""

from __future__ import annotations

from typing import Any

INITIAL_DISTANCE = 2
AP_COST_PER_LOCATION = 1
MAX_HAZARD_BONUS_DICE = 2
ACCELERATE_MIN = 2
ACCELERATE_MAX = 5

# 表：范例障碍物耐久值（p.116）。
OBSTACLE_DURABILITY = {
    "indoor_door": 5,
    "thin_fence": 5,
    "back_door": 10,
    "outdoor_door": 15,
    "brick_wall": 25,
    "tree": 50,
    "bridge_pier": 100,
}

# 随机险境 / 障碍（p.117）：01-59 无，60+ 普通，85+ 困难，96+ 极难。
RANDOM_HAZARD_THRESHOLDS = ((96, "extreme"), (85, "hard"), (60, "regular"))


def action_points(mov: Any, lowest_mov: Any) -> int:
    """本轮行动点：所有人 1 点，MOV 每比全场最低高 1 点再加 1 点。"""
    try:
        own = int(mov)
        lowest = int(lowest_mov)
    except (TypeError, ValueError):
        return 1
    return 1 + max(0, own - lowest)


def lowest_mov(participants: list[dict[str, Any]]) -> int:
    values = []
    for item in participants if isinstance(participants, list) else []:
        try:
            values.append(int(item.get("mov")))
        except (TypeError, ValueError, AttributeError):
            continue
    return min(values) if values else 1


def movement_cost(locations: Any) -> int:
    """移动若干个地点所需的行动点（无障碍时 1 点 / 地点）。"""
    try:
        count = int(locations)
    except (TypeError, ValueError):
        return AP_COST_PER_LOCATION
    return max(1, count) * AP_COST_PER_LOCATION


def accelerate_locations(requested: Any = None) -> int:
    """载具「猛踩油门」可前进 2-5 个地点；省略参数时取 2（p.117）。"""
    try:
        count = int(requested)
    except (TypeError, ValueError):
        return ACCELERATE_MIN
    return max(ACCELERATE_MIN, min(ACCELERATE_MAX, count))


def hazard_bonus_dice(ap_spent: Any) -> int:
    """谨慎通过险境：1 点行动点换 1 个奖励骰，单次最多 2 个（p.114）。"""
    try:
        spent = int(ap_spent)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_HAZARD_BONUS_DICE, spent))


def hazard_failure_ap_loss(roll: Any) -> int:
    """险境 / 战技失败：损失 1D3 行动点，结果夹在 1-3（p.114、p.116）。"""
    try:
        value = int(roll)
    except (TypeError, ValueError):
        return 1
    return max(1, min(3, value))


def conflict_ap_loss(roll: Any) -> int:
    """成功战技使目标如同险境失败：同样损失 1D3 行动点（p.116）。"""
    return hazard_failure_ap_loss(roll)


def random_hazard_difficulty(roll: Any) -> str | None:
    """随机险境 / 障碍难度（p.117）；``None`` 表示本段没有阻碍。"""
    try:
        value = int(roll)
    except (TypeError, ValueError):
        return None
    for threshold, difficulty in RANDOM_HAZARD_THRESHOLDS:
        if value >= threshold:
            return difficulty
    return None


def obstacle_durability(kind: Any = None, override: Any = None) -> int:
    """障碍耐久：优先使用显式数值，其次按范例表，最后按薄围栏 5。"""
    try:
        if override not in (None, ""):
            return max(1, int(override))
    except (TypeError, ValueError):
        pass
    key = str(kind or "").strip().lower()
    if key.isdigit():
        return max(1, int(key))
    return OBSTACLE_DURABILITY.get(key, OBSTACLE_DURABILITY["thin_fence"])


def can_attack(same_location: bool, has_firearm: bool = False) -> bool:
    """除火器外，只能攻击同一地点的目标（p.116）。"""
    return bool(same_location or has_firearm)


def can_push() -> bool:
    """追逐中不能孤注一掷，但下一轮可以再试（p.113）。"""
    return False


def maneuver_penalty_dice(attacker_build: Any, target_build: Any) -> int | None:
    """载具战技的体格差惩罚骰；差距 ≥3 时不可能成功（p.116）。"""
    try:
        diff = int(target_build) - int(attacker_build)
    except (TypeError, ValueError):
        return 0
    if diff >= 3:
        return None
    return max(0, diff)


def vehicle_ram_damage(build: Any) -> str:
    """载具每点体格对障碍 / 目标造成 1D10 伤害（p.115-116）。"""
    try:
        value = max(0, int(build))
    except (TypeError, ValueError):
        value = 0
    return f"{value}D10"


def vehicle_recoil_damage(durability: Any) -> int:
    """撞毁障碍后的反冲：障碍受冲击前耐久的一半（向下取整，p.116）。"""
    try:
        return max(0, int(durability)) // 2
    except (TypeError, ValueError):
        return 0


def vehicle_build_loss(damage: Any) -> int:
    """载具每受整 10 点伤害损失 1 点体格，余数忽略（p.116）。"""
    try:
        return max(0, int(damage)) // 10
    except (TypeError, ValueError):
        return 0


def vehicle_ram_self_damage(damage: Any, target_build: Any = None) -> int:
    """载具撞击造成的自伤为伤害的一半；体格损失不超过目标体格（p.116）。"""
    try:
        half = max(0, int(damage)) // 2
    except (TypeError, ValueError):
        return 0
    if target_build is None:
        return half
    try:
        cap = max(0, int(target_build)) * 10
    except (TypeError, ValueError):
        return half
    return min(half, cap)
