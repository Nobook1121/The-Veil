"""战斗房规配置。

规则书留白（拔枪耗时、施法打断、施法时能否闪避、重伤是否影响行动、盾牌与格挡）
统一放在这里，由房间房规覆盖；默认值取“规则书没有规定时最保守的解释”，
并在字段注释里标明它不是核心规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SURPRISE_MODELS = ("bonus_die", "auto_success")
READY_WEAPON_COSTS = ("free", "action", "round")
CAST_INTERRUPTS = ("keeper", "any_damage", "never")
MAJOR_WOUND_PENALTIES = ("none", "cannot_act")
SHIELD_ACTIONS = ("disabled", "counter_alias")
COMBAT_RULESETS = ("coc7",)


@dataclass(frozen=True)
class CombatSettings:
    """一套战斗的可配置规则。

    默认值全部是 CoC7 核心规则；下列字段属于规则书未规定、由本项配置决定的
    解释点，不是核心规则：

    - ``ready_weapon_cost``：拔枪/准备武器耗时。规则书未规定（报告 §9）。
    - ``cast_interrupt``：施法被打断的判定方式。规则书只说“KP 裁定”（p206）。
    - ``cast_defense_allowed``：施法当轮能否闪避/反击。规则书未规定（报告 §9）。
    - ``major_wound_penalty``：重伤是否限制行动。规则书未规定（报告 §9）。
    - ``shield_action``：盾牌是否提供独立格挡。核心无盾牌条目（p371-374）。
    - ``dex_extra_action_threshold``：DEX 阈值给额外行动，属于房规，
      不是核心规则（p86）；默认关闭。
    """

    ruleset_id: str = "coc7"
    surprise_model: str = "bonus_die"
    ready_weapon_cost: str = "free"
    cast_interrupt: str = "keeper"
    cast_defense_allowed: bool = False
    major_wound_penalty: str = "none"
    shield_action: str = "disabled"
    dex_extra_action_threshold: int | None = None
    dying_check: bool = True

    @classmethod
    def from_house_rules(cls, rules: Mapping[str, Any] | None) -> "CombatSettings":
        rules = rules or {}

        def _choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
            value = str(rules.get(key, default) or default).strip().lower()
            return value if value in allowed else default

        def _flag(key: str, default: bool) -> bool:
            value = rules.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        threshold = rules.get("combat_dex_extra_action_threshold")
        try:
            threshold = int(threshold) if threshold is not None else None
        except (TypeError, ValueError):
            threshold = None
        if threshold is not None and not 1 <= threshold <= 100:
            threshold = None

        return cls(
            ruleset_id=_choice("combat_ruleset", "coc7", COMBAT_RULESETS),
            surprise_model=_choice("combat_surprise_model", "bonus_die", SURPRISE_MODELS),
            ready_weapon_cost=_choice("combat_ready_weapon_cost", "free", READY_WEAPON_COSTS),
            cast_interrupt=_choice("combat_cast_interrupt", "keeper", CAST_INTERRUPTS),
            cast_defense_allowed=_flag("combat_cast_defense_allowed", False),
            major_wound_penalty=_choice(
                "combat_major_wound_penalty", "none", MAJOR_WOUND_PENALTIES
            ),
            shield_action=_choice("combat_shield_action", "disabled", SHIELD_ACTIONS),
            dex_extra_action_threshold=threshold,
            dying_check=_flag("combat_dying_check", True),
        )


def resolve_settings(room_info: Mapping[str, Any] | None) -> CombatSettings:
    """从房间信息里解析战斗配置；缺失时返回默认值。"""

    if not isinstance(room_info, Mapping):
        return CombatSettings()
    rules = room_info.get("house_rules")
    return CombatSettings.from_house_rules(rules if isinstance(rules, Mapping) else None)
