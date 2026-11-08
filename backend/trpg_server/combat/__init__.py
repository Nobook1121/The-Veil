"""规则书可插拔的战斗核心。

核心只负责事件流与结算时序：谁先行动、还剩几个行动、轮末触发什么。
具体怎么掷骰、怎么比较成功等级、伤害与死亡怎么判定，全部由 ruleset 插件提供。
"""

from .registry import get_ruleset, list_rulesets
from .settings import CombatSettings

__all__ = ["CombatSettings", "get_ruleset", "list_rulesets"]
