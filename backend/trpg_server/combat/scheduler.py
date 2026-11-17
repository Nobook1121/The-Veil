"""规则书无关的回合调度器。

只处理：轮/回合推进、行动余额、跳过无法行动者、轮末钩子。
不处理：先攻怎么算、伤害怎么扣、濒死怎么判——那些是 ruleset 插件的事。

E0 的 ``TurnModel`` 是可选增强：插件声明多槽行动经济（动作/附赠/反应/移动）后，
调度器在战斗者上维护 ``budget`` 字典，并在回合开始时重置 ``refresh="turn"`` 的槽位。
未声明 ``TurnModel`` 时只维护 CoC7 既有的 ``actions_remaining``，行为不变。
"""

from __future__ import annotations

from typing import Any

from .contracts import CombatRuleset

try:  # 避免规则集壳不可用时让战斗模块导入失败
    from trpg_server.ruleset.session import TurnModel, empty_budget
except Exception:  # pragma: no cover - 仅在规则集包损坏时触发
    TurnModel = Any  # type: ignore[assignment,misc]
    empty_budget = None  # type: ignore[assignment]


def spend_budget(combatant: dict[str, Any], slot: str, amount: int = 1) -> bool:
    """扣除一个行动槽；槽位不存在或额度不足时返回 ``False``。

    模块级函数供战斗工具以外的调用方（例如地图插件扣除移动力）复用，
    保证行动经济的判定只有一份实现。
    """
    budget = combatant.get("budget")
    if not isinstance(budget, dict):
        return False
    key = str(slot or "").strip().lower()
    cost = max(0, int(amount))
    if key not in budget:
        return False
    current = max(0, int(budget.get(key) or 0))
    if current < cost:
        return False
    budget[key] = current - cost
    return True


class TurnScheduler:
    def __init__(
        self,
        ruleset: CombatRuleset,
        settings: Any,
        *,
        roll_d100=None,
        roll_die=None,
        thresholds: tuple[int, int] = (1, 100),
        turn_model: Any | None = None,
    ):
        self.ruleset = ruleset
        self.settings = settings
        self.roll_d100 = roll_d100
        self.roll_die = roll_die
        self.thresholds = thresholds
        self.turn_model = turn_model

    # -- 行动预算 ---------------------------------------------------------
    def _declared_budget(self, combatant: dict[str, Any]) -> dict[str, int]:
        """插件 ``action_budget`` 给出的具体额度（如按速度变化的移动力）。"""
        hook = getattr(self.ruleset, "action_budget", None)
        if not callable(hook):
            return {}
        try:
            declared = hook(combatant, self.settings)
        except Exception:  # noqa: BLE001 - 预算声明失败时退回 TurnModel 默认值
            return {}
        if not isinstance(declared, dict):
            return {}
        return {str(key): max(0, int(value or 0)) for key, value in declared.items()}

    def initialize_combatant(self, combatant: dict[str, Any]) -> None:
        """按插件声明的 TurnModel 生成一次完整预算快照。"""
        model = self.turn_model
        if model is None or empty_budget is None or not getattr(model, "has_turns", False):
            return
        budget = empty_budget(model)
        for key, value in self._declared_budget(combatant).items():
            if key in budget:
                budget[key] = value
        combatant["budget"] = budget

    def reset_turn_budget(self, combatant: dict[str, Any]) -> None:
        """重置 ``refresh="turn"`` 的槽位；每回合开始时调用。"""
        model = self.turn_model
        budget = combatant.get("budget")
        if model is None or not isinstance(budget, dict):
            return
        declared = self._declared_budget(combatant)
        for slot in getattr(model, "budget", ()) or ():
            if getattr(slot, "refresh", "turn") != "turn":
                continue
            value = slot.per_turn if slot.per_turn is not None else slot.per_round
            budget[str(slot.key)] = declared.get(str(slot.key), max(0, int(value or 0)))

    def budget_of(self, combatant: dict[str, Any]) -> dict[str, int]:
        budget = combatant.get("budget")
        return budget if isinstance(budget, dict) else {}

    def _tick_effects(
        self,
        combatant: dict[str, Any],
        combat: dict[str, Any],
        phase: str,
        timing: str,
    ) -> list[str]:
        """推进效果持续时间并调用插件 tick；规则集未声明效果能力时跳过。"""
        if getattr(self.ruleset, "effects", None) is None:
            return []
        try:
            from trpg_server.ruleset.effects import tick_holder

            notes, _expired = tick_holder(
                combatant,
                phase=phase,
                timing=timing,
                plugin=self.ruleset,
                combat=combat,
                rng=self.roll_die,
            )
            return notes
        except Exception:  # noqa: BLE001 - 效果 tick 不能击穿回合推进
            return []

    def spend_budget(self, combatant: dict[str, Any], slot: str, amount: int = 1) -> bool:
        """扣除一个行动槽；槽位不存在或额度不足时返回 ``False``。"""
        return spend_budget(combatant, slot, amount)

    # -- 轮与回合 ---------------------------------------------------------
    def begin_round(self, combat: dict[str, Any]) -> list[str]:
        """轮开始：重置旧字段与预算；返回插件产生的事件文本。"""
        events: list[str] = []
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
            self.initialize_combatant(combatant)
            events.extend(self._tick_effects(combatant, combat, "round", "round_end"))
        return events

    def start_turn(self, combat: dict[str, Any]) -> list[str]:
        """当前行动者的回合开始：重置每回合槽位并调用插件钩子。"""
        actor = self._current_actor(combat)
        if not isinstance(actor, dict):
            return []
        self.reset_turn_budget(actor)
        events: list[str] = self._tick_effects(actor, combat, "turn", "turn_start")
        hook = getattr(self.ruleset, "start_turn", None)
        if callable(hook):
            try:
                notes = hook(
                    actor,
                    combat=combat,
                    settings=self.settings,
                    rng=self.roll_die,
                )
            except Exception:  # noqa: BLE001 - 单个插件的回合钩子不能击穿主循环
                notes = None
            if isinstance(notes, str):
                events.append(notes)
            elif isinstance(notes, (list, tuple)):
                events.extend(str(item) for item in notes if str(item or "").strip())
        return events

    def end_turn(self, combat: dict[str, Any]) -> list[str]:
        """当前行动者的回合结束；插件未实现时仍会推进效果 tick。"""
        actor = self._current_actor(combat)
        if not isinstance(actor, dict):
            return []
        events: list[str] = self._tick_effects(actor, combat, "turn", "turn_end")
        hook = getattr(self.ruleset, "end_turn", None)
        if not callable(hook):
            return events
        try:
            notes = hook(actor, combat=combat, settings=self.settings, rng=self.roll_die)
        except Exception:  # noqa: BLE001
            return events
        if isinstance(notes, str):
            events.append(notes)
        elif isinstance(notes, (list, tuple)):
            events.extend(str(item) for item in notes if str(item or "").strip())
        return events

    def advance(self, combat: dict[str, Any], *, consume_all: bool = False) -> list[str]:
        """推进到下一个可行动者；返回轮末/回合开始产生的事件文本。"""

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

        events.extend(self.end_turn(combat))

        index = turn
        for _ in range(len(order)):
            index += 1
            if index >= len(order):
                index = 0
                events.extend(self._end_round(combat))
                combat["round"] = int(combat.get("round") or 1) + 1
                events.extend(self.begin_round(combat))
            candidate = order[index]
            party = combat.get("combatants", {}).get(candidate)
            if isinstance(party, dict) and not self.ruleset.is_incapacitated(party):
                combat["turn"] = index
                events.extend(self.start_turn(combat))
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

    def _current_actor(self, combat: dict[str, Any]) -> dict[str, Any] | None:
        order = combat.get("order") or []
        turn = int(combat.get("turn") or 0)
        if not order or not 0 <= turn < len(order):
            return None
        actor = (combat.get("combatants") or {}).get(order[turn])
        return actor if isinstance(actor, dict) else None
