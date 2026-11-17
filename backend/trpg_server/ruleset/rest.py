"""规则集中立的休息与调谐契约。

核心只认识「短休 / 长休 / 调谐」这三种操作与它们的通用结果字段；具体
生命骰、恢复比例、同调上限与是否需要休息，都由规则集插件声明并实现。
没有声明 ``rest`` 能力的规则集（如 CoC7）不会出现这些入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass
class RestMemberResult:
    """一个成员的一次休息结果；字段是事实，不是规则书术语。"""

    name: str = ""
    kind: str = ""
    hp_before: int = 0
    hp_after: int = 0
    healed: int = 0
    hit_dice_spent: int = 0
    hit_dice_regained: int = 0
    slots_restored: bool = False
    exhaustion_level: int = 0
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "hp_before": self.hp_before,
            "hp_after": self.hp_after,
            "healed": self.healed,
            "hit_dice_spent": self.hit_dice_spent,
            "hit_dice_regained": self.hit_dice_regained,
            "slots_restored": self.slots_restored,
            "exhaustion_level": self.exhaustion_level,
            "notes": list(self.notes),
            "error": self.error,
        }


@runtime_checkable
class RestRules(Protocol):
    """规则集插件声明的休息/调谐能力。"""

    ruleset_id: str

    def profile(self, settings: Any) -> Mapping[str, Any]:
        """返回休息时长等通用档案。"""

    def short_rest(
        self,
        holder: dict[str, Any],
        sheet: Mapping[str, Any],
        *,
        hit_dice: int,
        rng: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """消耗生命骰恢复 HP；原地修改 ``holder``。"""

    def long_rest(
        self,
        holder: dict[str, Any],
        sheet: Mapping[str, Any],
        *,
        rng: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """长休恢复；原地修改 ``holder``。"""

    def attunement_limit(self, settings: Any) -> int:
        """同调上限。"""

    def attunement_requires_rest(self) -> bool:
        """建立同调是否必须在休息期间进行。"""

    def requires_attunement(self, sheet: Mapping[str, Any], item_id: str) -> bool:
        """该装备是否需要同调。"""


def rest_rules_of(plugin: Any) -> RestRules | None:
    capability = getattr(plugin, "rest", None)
    if capability is None:
        return None
    return capability  # type: ignore[return-value]


__all__ = ["RestMemberResult", "RestRules", "rest_rules_of"]
