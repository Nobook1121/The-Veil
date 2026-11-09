"""战斗规则书插件的公共契约。

这里的类型刻意保持“弱语义”：只描述一次结算的输入输出，不出现 HP、SAN、DB 等
CoC7 专有字段。规则书插件可以把这些概念映射到自己的资源模型上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

RollD100 = Callable[[], int]
RollDice = Callable[[int, int], int]

# 成功等级的默认排序：核心只做「按顺序比较」，等级名与顺序由插件的
# ``level_rank()`` 覆盖。保留默认值是为了让只声明元数据的占位插件也能跑通。
DEFAULT_LEVEL_RANK: dict[str, int] = {
    "fumble": -1,
    "failure": 0,
    "regular": 1,
    "hard": 2,
    "extreme": 3,
    "critical": 4,
}
# 默认「已失去行动能力」的状态集合，插件可用 ``skipped_statuses()`` 覆盖。
DEFAULT_SKIPPED_STATUSES = frozenset({"unconscious", "dying", "dead"})


def level_rank_of(level: str, rank: Mapping[str, int] | None = None) -> int:
    table = rank if rank is not None else DEFAULT_LEVEL_RANK
    return int(table.get(str(level or ""), 0))


def is_success_level(level: str, rank: Mapping[str, int] | None = None) -> bool:
    return level_rank_of(level, rank) > 0


@dataclass
class CheckOutcome:
    """一次判定的可序列化结果，供核心写日志/回放。"""

    roll: int
    target: int
    level: str
    difficulty: str = "regular"
    note: str = ""


@dataclass
class DamageOutcome:
    """伤害结算结果。字段是“事实”，不是规则书术语。"""

    damage: int = 0
    armor: int = 0
    effective: int = 0
    hp_before: int = 0
    hp_after: int = 0
    major_wound: bool = False
    dying: bool = False
    unconscious: bool = False
    dead: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoundEndOutcome:
    """轮末结算结果（濒死检定、持续伤害、状态恢复等）。"""

    dead: bool = False
    note: str = ""
    needs_check: bool = False
    check: CheckOutcome | None = None


@runtime_checkable
class CombatRuleset(Protocol):
    """规则书插件必须实现的钩子。

    只把“核心必须知道”的东西放进协议；规则书专有的动作解析、法术效果、
    武器表仍然留在插件自己的模块里，由工具层调用。
    """

    ruleset_id: str
    display_name: str

    def roll_initiative(self, combatant: Mapping[str, Any], rng: Any, settings: Any) -> int:
        """返回本轮先攻值；越大越先行动。"""

    def initiative_tiebreak(self, combatant: Mapping[str, Any]) -> int:
        """先攻同值时的次级排序键；越大越先行动。"""

    def actions_per_round(self, combatant: Mapping[str, Any], settings: Any) -> int:
        """该角色每轮可以执行几次显著行动。"""

    def reaction_limit(self, combatant: Mapping[str, Any], settings: Any) -> int:
        """寡不敌众规则中，目标在承受奖励骰前可反应的次数。"""

    def action_budget(self, combatant: Mapping[str, Any], settings: Any) -> dict[str, int]:
        """行动经济槽位。CoC7 只有 action；D&D 5e 有 action/bonus/reaction/movement。"""

    def is_incapacitated(self, combatant: Mapping[str, Any]) -> bool:
        """是否跳过该角色的回合（死亡、昏迷、濒死等）。"""

    def resolve_melee(
        self,
        attack_level: str,
        defense_level: str,
        defense_kind: str,
    ) -> str:
        """近战对抗结果：hit / counter_hit / miss。"""

    def resolve_damage(
        self,
        combatant: dict[str, Any],
        amount: int,
        *,
        armor: int,
        ignore_armor: bool,
        roll_d100: RollD100,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> DamageOutcome:
        """把一次伤害应用到角色状态上（原地修改 combatant）。"""

    def on_round_end(
        self,
        combatant: dict[str, Any],
        *,
        roll_d100: RollD100,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> RoundEndOutcome:
        """轮末结算；默认无操作。"""

    def spell_timing(self, cast_time: str, dex: int) -> dict[str, int]:
        """返回法术的生效时序：round_offset / initiative_bonus。"""

    def spend_resource(
        self,
        combatant: dict[str, Any],
        resource: str,
        amount: int,
        *,
        roll_d100: RollD100 | None = None,
    ) -> dict[str, int]:
        """扣除一项资源；返回 spent / overflow（溢出部分由核心决定怎么转成伤害）。"""
