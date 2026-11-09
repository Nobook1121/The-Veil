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
    # 瞄准跟踪：须上一轮声明，下一轮射击获得奖励骰；受伤或移动后失效。
    combat_aim_tracking: bool = False
    # 掩体与闪避：目标借掩体闪避成功后，攻击者失去下一次攻击。
    combat_cover_dodge: bool = False
    # 射击入近战：额外惩罚骰，大失败时误伤同方幸运最低的同伴。
    combat_friendly_fire: bool = False
    # 击晕：钝器战技成功改为造成 1 点伤害并击晕。
    combat_knockout: bool = False
    # 部位命中：每次伤害掷 1D20 定位并给出部位重伤描述。
    combat_hit_locations: bool = False
    # 毒剂：开放通用毒剂接口（KP 给毒性/伤害/发作轮数，CON 抵抗后结算）。
    combat_poison: bool = False
    # 自然恢复：开放 rule heal natural 命令（显式给出休养天数）。
    healing_natural: bool = False
    # 自然恢复必须声明休养；开启后未带 rest 不结算。
    healing_require_rest: bool = False

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
            combat_aim_tracking=_flag(data, "coc7_combat_aim_tracking"),
            combat_cover_dodge=_flag(data, "coc7_combat_cover_dodge"),
            combat_friendly_fire=_flag(data, "coc7_combat_friendly_fire"),
            combat_knockout=_flag(data, "coc7_combat_knockout"),
            combat_hit_locations=_flag(data, "coc7_combat_hit_locations"),
            combat_poison=_flag(data, "coc7_combat_poison"),
            healing_natural=_flag(data, "coc7_healing_natural"),
            healing_require_rest=_flag(data, "coc7_healing_require_rest"),
        )


def resolve_options(room_info: Mapping[str, Any] | None = None) -> Coc7RuleOptions:
    """从房间信息读取开关；缺省或旧房间一律按全部关闭处理。"""
    info = room_info if isinstance(room_info, Mapping) else {}
    rules = info.get("house_rules")
    return Coc7RuleOptions.from_house_rules(rules if isinstance(rules, Mapping) else None)
