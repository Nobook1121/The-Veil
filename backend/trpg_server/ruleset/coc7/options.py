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


def _minutes(rules: Mapping[str, Any], key: str, default: int = 60, maximum: int = 1440) -> int:
    """读分钟数；留空沿用默认值，0 表示关闭该工程兜底。"""
    value = rules.get(key, default)
    if value is None or value == "":
        return default
    try:
        return max(0, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


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
    # 幕间训练（批次 2 可选规则）：KP 批准投入时间换成长检定机会；默认关闭。
    growth_training: bool = False
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
    # 房间游戏时钟：按服务器时间记录房间内经过的时间，供恢复 / 典籍 / 毒剂 /
    # 幕间疗养读取。关闭时所有依赖时间的规则仍要求显式天数 / 周数。
    game_clock: bool = False
    # 追逐：开放地点 / 行动点 / 险境 / 障碍 / 冲突的追逐会话。
    chase_enabled: bool = False
    # 法术与典籍：开放施法消耗、首次施法检定与典籍阅读结算（不含内容表）。
    magic_enabled: bool = False
    # 法术 / 典籍内容表：按需查询规则书第十二章法术与第十一章典籍（不含结算）。
    magic_catalog_enabled: bool = False
    # 理智结构化：恐惧症 / 躁狂症 / 发作 / 习惯恐惧 / 现实免疫写入卡面扩展。
    # 这是批次 3 的总开关；关闭时其余理智开关一律不生效，行为与旧版本一致。
    sanity_state: bool = False
    # 目击神话：首次因神话陷入疯狂时克苏鲁神话 +5，之后每次 +1。
    sanity_mythos: bool = False
    # 习惯恐惧：按来源累计理智损失，上限为该存在单次遭遇最大损失；幕间 −1。
    sanity_habituation: bool = False
    # 现实认知：理智检定（目标值为当前 SAN）；成功获得对幻觉的抗性直到下次失去理智值。
    sanity_reality: bool = False
    # 发作计时：即时症状 1D10 战斗轮 / 总结症状 1D10 小时，写入可查询的 bout。
    sanity_bout: bool = False
    # 即时症状收尾安全阀（分钟）。不是规则书数值，只用于防止 KP 忘记收尾后永久免疫；
    # 0 = 关闭兜底（完全按原逻辑，需要显式收尾），留空按默认 60。
    sanity_bout_safety_minutes: int = 60
    # 恐惧 / 躁狂 100 条表：疯狂表现按需查表，暴露来源时检定吃惩罚骰。
    sanity_traits: bool = False

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
            growth_training=_flag(data, "coc7_growth_training"),
            combat_aim_tracking=_flag(data, "coc7_combat_aim_tracking"),
            combat_cover_dodge=_flag(data, "coc7_combat_cover_dodge"),
            combat_friendly_fire=_flag(data, "coc7_combat_friendly_fire"),
            combat_knockout=_flag(data, "coc7_combat_knockout"),
            combat_hit_locations=_flag(data, "coc7_combat_hit_locations"),
            combat_poison=_flag(data, "coc7_combat_poison"),
            healing_natural=_flag(data, "coc7_healing_natural"),
            healing_require_rest=_flag(data, "coc7_healing_require_rest"),
            game_clock=_flag(data, "coc7_game_clock"),
            chase_enabled=_flag(data, "coc7_chase_enabled"),
            magic_enabled=_flag(data, "coc7_magic_enabled"),
            magic_catalog_enabled=_flag(data, "coc7_magic_catalog_enabled"),
            sanity_state=_flag(data, "coc7_sanity_state"),
            sanity_mythos=_flag(data, "coc7_sanity_mythos"),
            sanity_habituation=_flag(data, "coc7_sanity_habituation"),
            sanity_reality=_flag(data, "coc7_sanity_reality"),
            sanity_bout=_flag(data, "coc7_sanity_bout"),
            sanity_bout_safety_minutes=_minutes(data, "coc7_sanity_bout_safety_minutes"),
            sanity_traits=_flag(data, "coc7_sanity_traits"),
        )


def resolve_options(room_info: Mapping[str, Any] | None = None) -> Coc7RuleOptions:
    """从房间信息读取开关；缺省或旧房间一律按全部关闭处理。"""
    info = room_info if isinstance(room_info, Mapping) else {}
    rules = info.get("house_rules")
    return Coc7RuleOptions.from_house_rules(rules if isinstance(rules, Mapping) else None)
