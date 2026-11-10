"""骰子策略：核心只认识「骰面 → 候选集 → 选择器」这套抽象。

分层约定：

- **核心**：随机数原语（``roller(sides)``）、奖励/惩罚的净额合成、候选集编排、
  结果广播。核心不出现「十位 / 个位 / 优势 / 奖励」等规则词。
- **策略**：由 :class:`RollPolicy` 描述要掷什么、如何从骰面得到候选值、如何选一个。
  :class:`PercentileRollPolicy` 是核心自带的百分骰原语策略（默认实现，行为与历史
  版本一致）；插件可以注册自己的策略覆盖它。

奖励骰/惩罚骰是核心机制：核心只负责 ``normalize_modifier`` 把正负修正合成为一个
净额并交给策略，策略决定「净额如何变成候选集」与「取大还是取小」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

# 修正净额上限（奖励与惩罚先抵消，净额最多 2 级）。
MODIFIER_LIMIT = 2

Roller = Callable[[int], int]


def sample_face(roller: Roller, sides: int) -> int:
    """掷一枚骰子，并强制随机源返回 ``1..sides``。

    百分骰的十位用 ``(x - 1) % 10``、个位用 ``x % 10`` 推导数字：只有随机源
    以 1 起始时两者才落在同一口径上。若随机源返回 0 起始或越界的值，十位会
    整体偏移一位，因此这里显式约束并直接报错，而不是静默夹紧或纠正。

    值域约定只作用于随机源；物理骰面走 ``normalize_faces`` 的 ``% 10``，
    因为前端上报的是 0-9 的骰面数字。
    """
    raw = roller(sides)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"roller must return an integer in 1..{sides}, got {raw!r}") from exc
    if not 1 <= value <= sides:
        raise ValueError(f"roller must return a value in 1..{sides}, got {value}")
    return value


def normalize_modifier(bonus: int | None, penalty: int | None, limit: int = MODIFIER_LIMIT) -> int:
    """奖励与惩罚先抵消，返回有符号净额（正=有利，负=不利）。"""
    try:
        favorable = max(0, int(bonus or 0))
    except (TypeError, ValueError):
        favorable = 0
    try:
        unfavorable = max(0, int(penalty or 0))
    except (TypeError, ValueError):
        unfavorable = 0
    return max(-limit, min(limit, favorable - unfavorable))


@dataclass(frozen=True)
class RollPlan:
    """一次掷骰的抽象计划：要掷哪些骰子，以及修正净额。"""

    sides: tuple[int, ...]
    modifier: int = 0


class RollPolicy(Protocol):
    """规则集可替换的骰子策略。"""

    id: str

    def plan(self, bonus: int = 0, penalty: int = 0) -> RollPlan:
        """把奖励/惩罚换算成掷骰计划（骰子面数 + 净额）。"""

    def roll(self, roller: Roller, plan: RollPlan) -> list[int]:
        """按计划掷骰，返回原始骰面。"""

    def normalize_faces(self, faces: Sequence[int], plan: RollPlan) -> list[int]:
        """把外部（物理骰）骰面整理成策略内部约定的顺序。"""

    def candidates(self, faces: Sequence[int], plan: RollPlan) -> list[int]:
        """由骰面得到候选结果集。"""

    def select(self, candidates: Sequence[int], plan: RollPlan) -> int:
        """按净额方向从候选集里选一个结果。"""


@dataclass(frozen=True)
class PercentileRollPolicy:
    """百分骰原语：个位一枚 + 十位若干枚，十位独立采样、允许重复。

    净额为 0 时掷单颗 d100；净额非 0 时十位骰数量为 ``|净额| + 1``，
    有利取最小候选、不利取最大候选。这是核心自带的默认策略，行为与历史
    版本逐位一致。
    """

    id: str = "percentile"
    limit: int = MODIFIER_LIMIT

    def plan(self, bonus: int = 0, penalty: int = 0) -> RollPlan:
        net = normalize_modifier(bonus, penalty, self.limit)
        if net == 0:
            return RollPlan(sides=(100,), modifier=0)
        return RollPlan(sides=(10,) * (abs(net) + 2), modifier=net)

    def roll(self, roller: Roller, plan: RollPlan) -> list[int]:
        # 随机源统一经 sample_face 约束为 1..sides，十位 / 个位才共用同一口径。
        if plan.sides == (100,):
            return [sample_face(roller, 100)]
        # 先掷个位再掷十位，保持既有随机序列与测试夹具的调用顺序。
        units = sample_face(roller, 10) % 10
        tens = [(sample_face(roller, 10) - 1) % 10 for _ in range(len(plan.sides) - 1)]
        return [*tens, units]

    def normalize_faces(self, faces: Sequence[int], plan: RollPlan) -> list[int]:
        values = [int(value) for value in faces]
        if len(values) < 2:
            raise ValueError("percentile roll requires at least two d10 faces")
        units = values[1] % 10
        if plan.sides == (100,):
            tens = values[0] % 10
            return [100 if tens == 0 and units == 0 else tens * 10 + units]
        needed = abs(plan.modifier) + 1
        tens_faces = [values[0] % 10, *[value % 10 for value in values[2:]]]
        # 物理模式通常已提供足够骰面；不足时用首颗十位补齐（重复十位本身合法）。
        while len(tens_faces) < needed:
            tens_faces.append(tens_faces[0])
        return [*tens_faces[:needed], units]

    def candidates(self, faces: Sequence[int], plan: RollPlan) -> list[int]:
        values = [int(value) for value in faces]
        if plan.sides == (100,):
            return values[:1]
        digits = [value % 10 for value in values]
        if not digits:
            return []
        units = digits[-1]
        return [100 if tens == 0 and units == 0 else tens * 10 + units for tens in digits[:-1]]

    def select(self, candidates: Sequence[int], plan: RollPlan) -> int:
        if not candidates:
            raise ValueError("roll produced no candidates")
        return min(candidates) if plan.modifier > 0 else max(candidates)


# 核心默认策略：百分骰原语。插件可通过 ``roll_policy`` 覆盖。
DEFAULT_ROLL_POLICY: RollPolicy = PercentileRollPolicy()


def _broadcast_candidates(candidates: list[int], plan: RollPlan) -> list[int]:
    """无修正时没有「多候选」概念，广播仍返回空列表（与历史元数据一致）。"""
    return candidates if plan.modifier else []


def roll_with_policy(
    policy: RollPolicy, roller: Roller, bonus: int = 0, penalty: int = 0
) -> tuple[int, list[int]]:
    plan = policy.plan(bonus, penalty)
    faces = policy.roll(roller, plan)
    candidates = policy.candidates(faces, plan)
    return policy.select(candidates, plan), _broadcast_candidates(candidates, plan)


def faces_with_policy(
    policy: RollPolicy, faces: Sequence[int], bonus: int = 0, penalty: int = 0
) -> tuple[int, list[int]]:
    plan = policy.plan(bonus, penalty)
    normalized = policy.normalize_faces(faces, plan)
    candidates = policy.candidates(normalized, plan)
    return policy.select(candidates, plan), _broadcast_candidates(candidates, plan)
