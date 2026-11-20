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
# 默认徒手武器条目（中性）：规则集未声明 ``unarmed_weapon()`` 时使用。
# 只保证攻击链路可用（可解析的小伤害 + 近战），不带任何规则集的专精键、
# 贯穿或卡壳语义。
DEFAULT_UNARMED_WEAPON: dict[str, Any] = {
    "name": "徒手",
    "skill": "徒手",
    "skillKey": "",
    "specialtyKey": "",
    "damage": "1D3",
    "range": "接触",
    "impale": False,
    "attacks": "1",
    "ammo": "N/A",
    "malfunction": "N/A",
}


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
    """伤害结算结果。字段是“事实”，不是规则书术语。

    ``major_wound`` / ``dying`` / ``unconscious`` / ``dead`` 是 CoC7 的伤势
    状态集，对其他伤害模型并非天然存在：规则集没有对应概念时置 ``None``
    （含义是「不适用」，由 :func:`trpg_server.combat.registry` 之外的唯一
    消费方 ``agents/tools/combat._apply_damage`` 按 ``False`` 呈现），不强制
    其他伤害模型塞假布尔值。
    """

    damage: int = 0
    armor: int = 0
    effective: int = 0
    hp_before: int = 0
    hp_after: int = 0
    major_wound: bool | None = None
    dying: bool | None = None
    unconscious: bool | None = None
    dead: bool | None = None
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

    可选扩展（插件不实现即保持旧行为）：

    - ``attack_mode``：``"opposed"``（缺省，旧百分制对抗）或 ``"d20"``。
      核心只按这个声明决定是否把整次攻击交给 ``resolve_attack_action``。
    - ``prepare_combatant``：开战/加入时由插件把角色卡或数据包条目映射成
      自己的战斗者字段；核心不解释映射结果。
    - ``resolve_attack_action``：``attack_mode="d20"`` 时整次攻击（命中、伤害、
      暴击、抗性）都由插件结算，核心只负责日志、保存与行动预算。
    - ``start_turn`` / ``end_turn``：回合开始/结束的规则钩子（如死亡豁免）。
    - ``prepare_combatant_from_module``：参与者来自剧本 NPC/怪物模块时，由规则集
      读取模块原文并映射成自己的战斗者；核心只转交模块，不解释字段。
    - ``on_combat_saved``：战斗状态每次落盘后的可选外设同步钩子（如地图棋子），
      失败必须由插件自行降级，不能影响战斗成立。
    - ``unarmed_weapon()``：未持械攻击/反击时使用的徒手武器目录条目；未声明时
      核心退中性徒手（无专精键、无贯穿/卡壳语义）。
    - ``extreme_damage(...)``：极限成功的伤害合成（满骰面 / 贯穿追加）；核心只
      转交武器解析结果，未声明时退「满骰面、不贯穿」。
    - ``weapon_malfunction(roll, threshold)``：射击卡壳判定；未声明时恒不卡壳。
    - ``auto_fire_composition(...)``：全自动的命中数 / 贯穿数配比；未声明时全中
      且不贯穿。
    - ``initiative_score(level, dex)``：可选先攻检定的数值合成；未声明时核心按
      「等级排序 ×1000 + DEX」合成。
    - ``initial_sanity(power)``：无卡参与者未显式给出理智时的起始值约定；战斗
      侧理智字段的存在性由插件 schema 的 ``san`` 资源槽位声明，未声明该槽位的
      规则集在战斗中整体跳过理智读写。
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
