"""CoC7 第九章「魔法」与第十一章「神话典籍」的纯规则。

依据：守秘人规则书
- p.147 魔法值：MP = POW/5；耗尽后 1:1 扣耐久；每小时回复 1 点，POW 每满 100 额外 +1，
  回复上限为 POW/5。
- p.148 首次施法：困难 POW 检定；失败什么也不发生，可支付消耗后孤注一掷。
- p.149 施法孤注一掷失败：法术仍生效，但额外支付 1D6 倍消耗（MP / SAN / POW）。
- p.147-148 学习法术：通常 2D6 周；重新学习 2D6 周 + 困难 INT 检定。
- p.145 泛读：语言检定成功 → 获得 CMI、自动损失 SAN、得知书中法术。
- p.146 精读：无检定，耗时以周计；克苏鲁神话 < 神话等级时获得 CMF，否则只获得 CMI；
  后续精读耗时逐次翻倍；同一时段只能读一本。
- p.146 神话等级：花 1D4 小时查阅，1D100 ≤ 神话等级即找到资料。

本模块不内置法术表与典籍表——那属于内容包；这里只提供按 KP 传入数值结算的通用数学。
"""

from __future__ import annotations

from typing import Any

MP_PER_POW = 5
LEARNING_WEEKS_MIN = 2
LEARNING_WEEKS_MAX = 12
PUSH_MULTIPLIER_MIN = 1
PUSH_MULTIPLIER_MAX = 6

SUCCESS_LEVELS = frozenset({"regular", "hard", "extreme", "critical"})
FAILURE_LEVELS = frozenset({"failure", "fumble"})

# 施法孤注一掷失败副作用（p.149）——只做文本提示，不自动施加。
WEAK_PUSH_SIDE_EFFECTS = (
    "视野模糊或暂时目盲",
    "凭空出现的尖叫、声响或其他噪音",
    "狂风等大气效应",
    "流血——从施法者、在场的其他人或环境中",
    "奇怪的幻象和幻觉",
    "周围的小动物爆炸",
    "硫磺的臭气",
    "意外召唤出了神话怪物",
)
STRONG_PUSH_SIDE_EFFECTS = (
    "大地震动，墙体撕裂破碎",
    "惊人的雷鸣和闪电",
    "鲜血从天而降",
    "施法者的手凋零烧毁",
    "施法者不自然地变老（+2D10 岁）",
    "强大或众多的神话生物出现，施法者首当其冲",
    "施法者和附近所有人都被吸到遥远的时空中",
    "意外请出了神话神祇",
)


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def max_mp(pow_value: Any) -> int:
    """魔法值上限为 POW 的五分之一（向下取整，p.147）。"""
    return max(0, _int(pow_value)) // MP_PER_POW


def mp_recovery(hours: Any, pow_value: Any = None) -> int:
    """每小时回复 1 点；POW 每满 100 额外 +1 点（p.147）。"""
    total = max(0, _int(hours))
    bonus = max(0, _int(pow_value)) // 100
    return total * (1 + bonus)


def mp_cost_split(cost: Any, current_mp: Any, current_hp: Any) -> dict[str, int]:
    """支付 MP：不足部分按 1:1 从耐久扣除（p.147）。"""
    need = max(0, _int(cost))
    mp = max(0, _int(current_mp))
    hp = max(0, _int(current_hp))
    spent_mp = min(need, mp)
    spent_hp = min(need - spent_mp, hp)
    return {
        "mp": spent_mp,
        "hp": spent_hp,
        "unpaid": max(0, need - spent_mp - spent_hp),
    }


def first_cast_target(pow_value: Any) -> int:
    """首次施法为困难 POW 检定（p.148）。"""
    return max(1, _int(pow_value)) // 2


def is_success(level: Any) -> bool:
    return str(level or "").strip().lower() in SUCCESS_LEVELS


def push_multiplier(roll: Any) -> int:
    """孤注一掷失败的额外消耗倍数：1D6，夹在 1-6（p.149）。"""
    value = _int(roll, 0)
    if value <= 0:
        return 1
    return max(PUSH_MULTIPLIER_MIN, min(PUSH_MULTIPLIER_MAX, value))


def pushed_cost(cost: Any, multiplier: Any = 1) -> int:
    """额外支付「本次消耗 × 1D6 倍」（p.149）。"""
    return max(0, _int(cost)) * push_multiplier(multiplier)


def learning_weeks(roll: Any) -> int:
    """学习法术耗时 2D6 周，夹在 2-12（p.148）。"""
    value = _int(roll, LEARNING_WEEKS_MIN)
    return max(LEARNING_WEEKS_MIN, min(LEARNING_WEEKS_MAX, value))


def relearn_weeks(roll: Any) -> int:
    """重新学习同一法术同样是 2D6 周 + 困难 INT 检定（p.148）。"""
    return learning_weeks(roll)


def grimoire_mythos_gain(*, mythos: Any, rating: Any, cmi: Any, cmf: Any) -> int:
    """精读收益：神话技能低于神话等级拿 CMF，否则只拿 CMI（p.146）。"""
    if _int(mythos) < _int(rating):
        return max(0, _int(cmf))
    return max(0, _int(cmi))


def grimoire_weeks(base_weeks: Any, prior_reads: Any = 0) -> int:
    """精读耗时：首次为基础周数，之后每次翻倍（p.146）。"""
    base = max(1, _int(base_weeks, 1))
    rounds = max(0, _int(prior_reads))
    return base * (2**rounds)


def grimoire_lookup_success(roll: Any, rating: Any) -> bool:
    """按神话等级查阅资料：1D100 ≤ 神话等级即成功（p.146）。"""
    value = _int(roll)
    target = max(0, _int(rating))
    return value >= 1 and value <= target


def belief_san_loss(mythos_value: Any) -> int:
    """成为相信者时额外损失等于当前克苏鲁神话技能的 SAN（p.150）。"""
    return max(0, _int(mythos_value))


def push_side_effect(roll: Any, *, strong: bool = False) -> str:
    """孤注一掷失败的副作用：掷 1D8，按强/弱法术给出一句提示（p.149）。"""
    table = STRONG_PUSH_SIDE_EFFECTS if strong else WEAK_PUSH_SIDE_EFFECTS
    value = _int(roll, 1)
    index = (value - 1) % len(table)
    return table[index]
