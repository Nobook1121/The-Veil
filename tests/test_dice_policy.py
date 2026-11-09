"""骰子策略（RollPolicy）边界测试。

核心只负责「随机数原语 + 修正净额 + 候选集编排 + 广播」，具体候选与选择规则由
策略提供。这里用一个与百分骰完全不同的策略验证核心没有写死骰子结构。
"""

from __future__ import annotations

from trpg_server.dice_policy import (
    PercentileRollPolicy,
    RollPlan,
    normalize_modifier,
    roll_with_policy,
)


class _TakeHighestPolicy:
    """示例策略：掷两枚同面骰，有利取高、否则取低。"""

    id = "take-highest"

    def plan(self, bonus: int = 0, penalty: int = 0) -> RollPlan:
        return RollPlan(sides=(20, 20), modifier=normalize_modifier(bonus, penalty))

    def roll(self, roller, plan: RollPlan) -> list[int]:
        return [roller(20), roller(20)]

    def normalize_faces(self, faces, plan: RollPlan) -> list[int]:
        return [int(face) for face in faces]

    def candidates(self, faces, plan: RollPlan) -> list[int]:
        return [int(face) for face in faces]

    def select(self, candidates, plan: RollPlan) -> int:
        return max(candidates) if plan.modifier > 0 else min(candidates)


def test_core_rolls_whatever_the_policy_asks_for():
    values = iter([3, 17])
    roll, candidates = roll_with_policy(
        _TakeHighestPolicy(), lambda sides: next(values), bonus=1
    )
    assert roll == 17
    assert candidates == [3, 17]


def test_core_does_not_know_percentile_structure():
    """换成 20 面骰策略后，核心不需要知道「十位/个位」。"""
    values = iter([20, 4])
    roll, _ = roll_with_policy(_TakeHighestPolicy(), lambda sides: next(values), penalty=1)
    assert roll == 4


def test_modifier_is_normalized_in_core():
    assert normalize_modifier(3, 1) == 2
    assert normalize_modifier(0, 4) == -2
    assert normalize_modifier(1, 1) == 0


def test_default_policy_is_percentile_primitive():
    assert isinstance(PercentileRollPolicy(), PercentileRollPolicy)
    plan = PercentileRollPolicy().plan(1, 0)
    assert plan.sides == (10, 10, 10)
    assert plan.modifier == 1
