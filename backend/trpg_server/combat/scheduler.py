"""规则书无关的回合调度器。

只处理：轮/回合推进、行动余额、跳过无法行动者、轮末钩子。
不处理：先攻怎么算、伤害怎么扣、濒死怎么判——那些是 ruleset 插件的事。
"""

from __future__ import annotations

from typing import Any

from .contracts import CombatRuleset


class TurnScheduler:
    def __init__(
        self,
        ruleset: CombatRuleset,
        settings: Any,
        *,
        roll_d100=None,
        thresholds: tuple[int, int] = (1, 100),
    ):
        self.ruleset = ruleset
        self.settings = settings
        self.roll_d100 = roll_d100
        self.thresholds = thresholds

    def begin_round(self, combat: dict[str, Any]) -> None:
        for combatant in combat.get("combatants", {}).values():
            combatant["actions_remaining"] = self.ruleset.actions_per_round(
                combatant, self.settings
            )
            combatant["actions_per_round"] = combatant["actions_remaining"]
            combatant["reactions_this_round"] = 0
            combatant["used_dodge"] = False
            combatant["used_counter"] = False
            combatant["disengaged"] = False
            combatant["damage_taken_this_round"] = 0

    def advance(self, combat: dict[str, Any], *, consume_all: bool = False) -> list[str]:
        """推进到下一个可行动者；返回轮末产生的事件文本。"""

        order = combat.get("order") or []
        if not order:
            return []

        events: list[str] = []
        turn = int(combat.get("turn") or 0)
        current = order[turn] if 0 <= turn < len(order) else order[0]
        actor = combat.get("combatants", {}).get(current)
        if isinstance(actor, dict):
            if consume_all:
                actor["actions_remaining"] = 0
            else:
                actor["actions_remaining"] = max(
                    0, int(actor.get("actions_remaining", 1)) - 1
                )
            if int(actor.get("actions_remaining", 0)) > 0:
                combat["turn"] = turn
                return events

        index = turn
        for _ in range(len(order)):
            index += 1
            if index >= len(order):
                index = 0
                events.extend(self._end_round(combat))
                combat["round"] = int(combat.get("round") or 1) + 1
                self.begin_round(combat)
            candidate = order[index]
            party = combat.get("combatants", {}).get(candidate)
            if isinstance(party, dict) and not self.ruleset.is_incapacitated(party):
                combat["turn"] = index
                return events

        # 全场都无法行动：停在当前位，交给调用方结束战斗。
        combat["turn"] = turn
        return events

    def _end_round(self, combat: dict[str, Any]) -> list[str]:
        events: list[str] = []
        for name, combatant in combat.get("combatants", {}).items():
            if not isinstance(combatant, dict):
                continue
            if combatant.get("dead"):
                continue
            outcome = self.ruleset.on_round_end(
                combatant,
                roll_d100=self.roll_d100,
                thresholds=self.thresholds,
                settings=self.settings,
            )
            if outcome.dead:
                events.append(f"{name} 濒死检定失败，死亡。")
            elif outcome.note:
                events.append(f"{name}：{outcome.note}")
        return events


def has_actions(combatant: dict[str, Any]) -> bool:
    return int(combatant.get("actions_remaining", 0)) > 0
