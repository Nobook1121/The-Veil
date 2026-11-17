"""CoC7 房规 schema（由插件声明，核心只做容器与校验）。

这些开关属于 CoC7 规则集；核心的 ``CORE_HOUSE_RULE_DEFINITIONS`` 只保留
BGM、骰子、沉浸、叙事、世界意志等跨规则集设置。
"""

from __future__ import annotations

from typing import Any

COC7_HOUSE_RULE_DEFINITIONS: dict[str, dict[str, Any]] = {
    # 技能基础值覆盖表：键为技能键（带专精时为 `skillKey.specialtyKey`），值为 0-99 整数。
    # 留空表示沿用下一优先级（用户个性化 > 管理员设置 > 技能目录默认值）。
    "skill_bases": {"type": "skill_bases", "default": {}, "ai_prompt": False},
    # CoC7 章节级规则开关（默认全部关闭，关闭时任何既有行为都不变）。
    # 社交技能难度：按对方社交/心理学与态度建议难度，见 ruleset/coc7/social.py。
    "coc7_social_difficulty": {"type": "boolean", "default": False, "ai_prompt": False},
    # 组合技能：一次掷骰对多个技能比较（全部成功 / 任一成功）。
    "coc7_combined_checks": {"type": "boolean", "default": False, "ai_prompt": False},
    # 团体幸运：由在场幸运最低者代掷。
    "coc7_group_luck": {"type": "boolean", "default": False, "ai_prompt": False},
    # 信用评级开支：消费水平、现金/资产扣减与幕间 CR 变动。
    "coc7_economy": {"type": "boolean", "default": False, "ai_prompt": False},
    # 熟人：创建熟人与熟人协助（房规 1 奖励骰）。
    "coc7_contacts": {"type": "boolean", "default": False, "ai_prompt": False},
    # 年龄调整：建卡年龄修正与老化 / EDU 增强检定。
    "coc7_age_adjustments": {"type": "boolean", "default": False, "ai_prompt": False},
    # 幕间训练（批次 2 可选规则）：KP 批准投入周数换成长检定；默认关闭。
    "coc7_growth_training": {"type": "boolean", "default": False, "ai_prompt": False},
    # 战斗规则留白配置（报告 §9）。这些都不是 CoC7 核心规则，默认值取最保守解释。
    # 突袭：bonus_die = 未准备目标近战给攻击者 1 奖励骰；auto_success = 自动成功（除大失败）。
    "combat_surprise_model": {
        "type": "enum",
        "default": "bonus_die",
        "values": ["bonus_die", "auto_success"],
        "ai_prompt": False,
    },
    # 准备武器耗时：规则书未规定（p96 只说已准备武器 DEX+50）。
    "combat_ready_weapon_cost": {
        "type": "enum",
        "default": "free",
        "values": ["free", "action", "round"],
        "ai_prompt": False,
    },
    # 施法打断：keeper = KP 裁定（规则书原文 p206）；any_damage = 当轮受伤即打断；never = 不打断。
    "combat_cast_interrupt": {
        "type": "enum",
        "default": "keeper",
        "values": ["keeper", "any_damage", "never"],
        "ai_prompt": False,
    },
    "combat_cast_defense_allowed": {"type": "boolean", "default": False, "ai_prompt": False},
    # 重伤是否限制行动：规则书未规定，默认不额外惩罚。
    "combat_major_wound_penalty": {
        "type": "enum",
        "default": "none",
        "values": ["none", "cannot_act"],
        "ai_prompt": False,
    },
    # 盾牌：核心无盾牌条目（p371-374），默认禁用独立格挡；counter_alias 仅把盾牌视为反击描述。
    "combat_shield_action": {
        "type": "enum",
        "default": "disabled",
        "values": ["disabled", "counter_alias"],
        "ai_prompt": False,
    },
    # 房规：DEX 达到阈值给 1 次额外行动。默认关闭；核心规则没有此条（p86）。
    "combat_dex_extra_action_threshold": {
        "type": "nullable_integer",
        "default": None,
        "min": 1,
        "max": 100,
        "ai_prompt": False,
    },
    "combat_dying_check": {"type": "boolean", "default": True, "ai_prompt": False},
    # 批次 4 战斗细节（默认全部关闭；关闭时射击/近战流程与旧版本完全一致）。
    # 瞄准跟踪：须上一轮声明，下一轮射击获得 1 奖励骰；受伤或移动后失效。
    "coc7_combat_aim_tracking": {"type": "boolean", "default": False, "ai_prompt": False},
    # 掩体与闪避：目标借掩体闪避成功后，攻击者失去下一次攻击。
    "coc7_combat_cover_dodge": {"type": "boolean", "default": False, "ai_prompt": False},
    # 射击入近战：额外 1 惩罚骰；大失败命中同方幸运最低的同伴。
    "coc7_combat_friendly_fire": {"type": "boolean", "default": False, "ai_prompt": False},
    # 击晕：钝器战技成功改为造成 1 点伤害并击晕（1D10 轮）。
    "coc7_combat_knockout": {"type": "boolean", "default": False, "ai_prompt": False},
    # 部位命中：每次伤害掷 1D20 定位，用于部位重伤描述。
    "coc7_combat_hit_locations": {"type": "boolean", "default": False, "ai_prompt": False},
    # 毒剂：KP 给毒性/伤害/发作轮数，系统做 CON 抵抗与伤害结算。
    "coc7_combat_poison": {"type": "boolean", "default": False, "ai_prompt": False},
    # 自然恢复：开放 rule heal natural，必须显式给出休养天数。
    "coc7_healing_natural": {"type": "boolean", "default": False, "ai_prompt": False},
    # 自然恢复必须声明休养；关闭时不强制 rest。
    "coc7_healing_require_rest": {"type": "boolean", "default": False, "ai_prompt": False},
    # 房间游戏时钟：按服务器时间累计房间内经过的时间（默认关闭）。
    # 开启后 rule heal natural / spell grimoire / heal mp / 毒剂到期 / 幕间疗养
    # 可以用「当前时间 − 上次结算时间」代替显式天数，显式参数仍然优先。
    "coc7_game_clock": {"type": "boolean", "default": False, "ai_prompt": False},
    # 追逐：开放地点 / 行动点 / 险境 / 障碍 / 冲突的追逐会话（默认关闭）。
    "coc7_chase_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 法术与典籍：施法消耗 / 首次施法检定 / 典籍阅读结算（默认关闭，不含内容表）。
    "coc7_magic_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 法术 / 典籍内容表：按需查询规则书条目（默认关闭，不影响施法结算）。
    "coc7_magic_catalog_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 批次 3 理智结构化（总开关；关闭时理智链路与旧版本逐字一致）。
    "coc7_sanity_state": {"type": "boolean", "default": False, "ai_prompt": False},
    # 目击神话：首次因神话疯狂时克苏鲁神话 +5，之后每次 +1。
    "coc7_sanity_mythos": {"type": "boolean", "default": False, "ai_prompt": False},
    # 习惯恐惧：按来源累计理智损失，上限为该存在单次遭遇最大损失；幕间 −1。
    "coc7_sanity_habituation": {"type": "boolean", "default": False, "ai_prompt": False},
    # 现实认知：理智检定（目标值为当前 SAN）成功看穿幻象，获得对幻觉的抗性直到下次失去理智值。
    "coc7_sanity_reality": {"type": "boolean", "default": False, "ai_prompt": False},
    # 发作计时：即时症状 1D10 战斗轮 / 总结症状 1D10 小时，写入可查询的 bout。
    "coc7_sanity_bout": {"type": "boolean", "default": False, "ai_prompt": False},
    # 即时症状收尾安全阀（分钟）。不是规则书数值：规则书只规定即时症状持续 1D10 战斗轮，
    # 系统没有按战斗轮推进的时钟，故用挂钟分钟数兜底收尾，防止 KP 忘记收尾导致永久免疫。
    # 0 = 关闭兜底（完全按原逻辑，需要 KP / rule san bout end 显式收尾）；留空按默认 60。
    "coc7_sanity_bout_safety_minutes": {
        "type": "nullable_integer",
        "default": 60,
        "min": 0,
        "max": 1440,
        "ai_prompt": False,
    },
    # 恐惧 / 躁狂 100 条表：按需查表，暴露来源时检定吃惩罚骰。
    "coc7_sanity_traits": {"type": "boolean", "default": False, "ai_prompt": False},
}


def coc7_house_rule_definitions() -> dict[str, dict[str, Any]]:
    return {key: dict(spec) for key, spec in COC7_HOUSE_RULE_DEFINITIONS.items()}


__all__ = ["COC7_HOUSE_RULE_DEFINITIONS", "coc7_house_rule_definitions"]
