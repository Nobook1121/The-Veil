"""CoC7 房间级房规开关。

全部默认关闭：关闭时调用方保持现有行为，不改变任何检定、消费或建卡结果。
键名与 ``routes/rooms.py`` 的 ``HOUSE_RULE_DEFINITIONS`` 一一对应，统一使用
``coc7_`` 前缀，避免与战斗房规（``combat_*``）混在一起。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _flag(rules: Mapping[str, Any], key: str) -> bool:
    value = rules.get(key, False)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return bool(value)


@dataclass(frozen=True)
class Coc7RuleOptions:
    """只读开关集合；每一项都对应一个可单独验收的规则面。"""

    # 社交技能：按对方社交/心理学技能与态度建议难度等级。
    social_difficulty: bool = False
    # 组合技能：一次掷骰对多个技能比较（全部成功 / 任一成功）。
    combined_checks: bool = False
    # 团体幸运：由在场幸运最低者代掷。
    group_luck: bool = False
    # 信用评级开支：消费水平、现金/资产扣减、幕间 CR 变动。
    economy: bool = False
    # 熟人：创建熟人与熟人协助的规则化入口。
    contacts: bool = False
    # 年龄调整：建卡年龄修正与老化/EDU 增强检定。
    age_adjustments: bool = False

    @classmethod
    def from_house_rules(cls, rules: Mapping[str, Any] | None = None) -> "Coc7RuleOptions":
        data = rules if isinstance(rules, Mapping) else {}
        return cls(
            social_difficulty=_flag(data, "coc7_social_difficulty"),
            combined_checks=_flag(data, "coc7_combined_checks"),
            group_luck=_flag(data, "coc7_group_luck"),
            economy=_flag(data, "coc7_economy"),
            contacts=_flag(data, "coc7_contacts"),
            age_adjustments=_flag(data, "coc7_age_adjustments"),
        )


def resolve_options(room_info: Mapping[str, Any] | None = None) -> Coc7RuleOptions:
    """从房间信息读取开关；缺省或旧房间一律按全部关闭处理。"""
    info = room_info if isinstance(room_info, Mapping) else {}
    rules = info.get("house_rules")
    return Coc7RuleOptions.from_house_rules(rules if isinstance(rules, Mapping) else None)
