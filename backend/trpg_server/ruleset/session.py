"""通用会话/回合壳（E0 会话契约）。

核心保留会话容器：参与者、顺序、回合指针、日志。先攻公式、行动经济、
战斗解析由插件声明；没有战斗的规则集把 ``combat`` 声明为 ``None``，
``TurnModel.mode`` 为 ``none``，房间不显示战斗面板。

本模块只定义契约与纯函数，不改现有 ``combat/scheduler.py`` 行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

TURN_MODEL_VERSION = 1
ACTION_BUDGET_VERSION = 1

VALID_MODES = frozenset({"none", "round", "initiative", "phase", "free"})
VALID_INITIATIVE = frozenset({"none", "dexterity", "roll", "fixed", "manual"})
VALID_ECONOMIES = frozenset({"slots", "free"})
VALID_REFRESH = frozenset({"turn", "round", "scene", "never"})


@dataclass(frozen=True)
class ActionBudgetSlot:
    """一个行动额度槽，如 ``action`` / ``bonus`` / ``reaction`` / ``movement``。"""

    key: str
    per_turn: int | None = None
    per_round: int | None = None
    refresh: str = "turn"
    label_i18n: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "per_turn": self.per_turn,
            "per_round": self.per_round,
            "refresh": self.refresh,
            "label_i18n": self.label_i18n,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class TurnModel:
    """插件声明的回合模型；核心只按声明推进指针与额度。"""

    mode: str = "none"
    initiative: str = "none"
    # 先攻公式只作展示/说明，核心不解析表达式；实际计算仍由插件完成。
    initiative_formula: str = ""
    economy: str = "slots"
    budget: tuple[ActionBudgetSlot, ...] = ()
    advance: str = "manual"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def has_turns(self) -> bool:
        return self.mode != "none"

    @property
    def budget_keys(self) -> tuple[str, ...]:
        return tuple(slot.key for slot in self.budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_model_version": TURN_MODEL_VERSION,
            "mode": self.mode,
            "initiative": self.initiative,
            "initiative_formula": self.initiative_formula,
            "economy": self.economy,
            "budget": [slot.to_dict() for slot in self.budget],
            "advance": self.advance,
            "detail": dict(self.detail),
        }


def _normalize_budget(raw: Any) -> tuple[ActionBudgetSlot, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    slots: list[ActionBudgetSlot] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not key:
            continue
        refresh = str(item.get("refresh") or "turn").strip().lower()
        slots.append(
            ActionBudgetSlot(
                key=key,
                per_turn=_optional_int(item.get("per_turn")),
                per_round=_optional_int(item.get("per_round")),
                refresh=refresh if refresh in VALID_REFRESH else "turn",
                label_i18n=str(item.get("label_i18n") or ""),
                detail=dict(item.get("detail") or {}),
            )
        )
    return tuple(slots)


def normalize_turn_model(raw: Any) -> TurnModel:
    """把插件声明（``TurnModel`` 或字典）归一；未知枚举回退中性值。"""
    if isinstance(raw, TurnModel):
        return raw
    if not isinstance(raw, Mapping):
        return TurnModel()
    mode = str(raw.get("mode") or "none").strip().lower()
    initiative = str(raw.get("initiative") or "none").strip().lower()
    economy = str(raw.get("economy") or "slots").strip().lower()
    return TurnModel(
        mode=mode if mode in VALID_MODES else "none",
        initiative=initiative if initiative in VALID_INITIATIVE else "none",
        initiative_formula=str(raw.get("initiative_formula") or ""),
        economy=economy if economy in VALID_ECONOMIES else "slots",
        budget=_normalize_budget(raw.get("budget")),
        advance=str(raw.get("advance") or "manual"),
        detail=dict(raw.get("detail") or {}),
    )


def turn_model_of(plugin: Any) -> TurnModel:
    """读取插件的回合模型；未声明时按是否具备战斗能力给中性默认。"""
    declared = getattr(plugin, "turn", None)
    if declared is not None:
        return normalize_turn_model(declared)
    if combat_available(plugin):
        return TurnModel(
            mode="round",
            economy="slots",
            budget=(ActionBudgetSlot(key="action", per_round=1),),
        )
    return TurnModel()


def combat_available(plugin: Any) -> bool:
    """``combat`` 是可选能力：未注册实现即视为该规则集没有战斗。"""
    return getattr(plugin, "combat", None) is not None


def empty_budget(model: TurnModel) -> dict[str, int]:
    """生成回合开始时的额度快照；没有回合模型时返回空字典。"""
    budget: dict[str, int] = {}
    for slot in model.budget:
        value = slot.per_turn if slot.per_turn is not None else slot.per_round
        budget[slot.key] = max(0, int(value or 0))
    return budget


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ACTION_BUDGET_VERSION",
    "TURN_MODEL_VERSION",
    "VALID_ECONOMIES",
    "VALID_INITIATIVE",
    "VALID_MODES",
    "ActionBudgetSlot",
    "TurnModel",
    "combat_available",
    "empty_budget",
    "normalize_turn_model",
    "turn_model_of",
]
