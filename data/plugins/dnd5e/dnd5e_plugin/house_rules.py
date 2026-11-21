"""D&D 5e 房规声明。

核心只负责把这份 schema 合并进房间房规、按 ``type`` 校验并原样下发给前端；
字段名、取值范围、默认值与中英文文案都属于插件，核心不出现任何 5e 字段。
"""

from __future__ import annotations

from typing import Any

_SCOPE = "ruleset"


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh-CN": zh, "en-US": en}


def _option_labels(**items: tuple[str, str]) -> dict[str, dict[str, str]]:
    return {value: _text(zh, en) for value, (zh, en) in items.items()}


def house_rules() -> dict[str, dict[str, Any]]:
    combat = _text("战斗与先攻", "Combat & Initiative")
    rest = _text("休息与恢复", "Rest & Recovery")
    optional = _text("可选规则", "Optional Rules")
    return {
        "dnd5e_critical_rule": {
            "type": "enum",
            "default": "standard",
            "values": ["standard", "max_dice", "confirm", "none"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("暴击规则", "Critical Hit Rule"),
            "description_i18n": _text(
                "决定自然 20 的伤害结算方式：标准双倍骰、取满骰、二次确认或关闭暴击。",
                "How natural 20 damage is resolved: double dice, maximum dice, confirmation roll, or no criticals.",
            ),
            "option_labels": _option_labels(
                standard=("标准：伤害骰翻倍", "Standard: double damage dice"),
                max_dice=("取满：伤害骰取最大值", "Maximum: maximize damage dice"),
                confirm=("确认：再掷一次攻击确认", "Confirm: roll a second attack to confirm"),
                none=("关闭暴击", "No critical hits"),
            ),
        },
        "dnd5e_initiative_mode": {
            "type": "enum",
            "default": "roll",
            "values": ["roll", "fixed", "advantage"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("先攻方式", "Initiative Mode"),
            "description_i18n": _text(
                "按 d20 + 敏捷调整值掷先攻、使用固定值（10 + 敏捷调整值），或让先攻带优势。",
                "Roll d20 + DEX modifier, use a fixed value (10 + DEX modifier), or roll initiative with advantage.",
            ),
            "option_labels": _option_labels(
                roll=("掷 d20 先攻", "Roll d20 initiative"),
                fixed=("固定值（10 + 敏捷）", "Fixed (10 + DEX)"),
                advantage=("先攻带优势", "Initiative with advantage"),
            ),
        },
        "dnd5e_rest_variant": {
            "type": "enum",
            "default": "standard",
            "values": ["standard", "gritty", "heroic"],
            "scope": _SCOPE,
            "group": "rest",
            "group_label_i18n": rest,
            "label_i18n": _text("休息规则", "Rest Variant"),
            "description_i18n": _text(
                "标准休息、坚韧现实（短休 8 小时 / 长休 7 天）或英雄式休息（短休 5 分钟 / 长休 1 小时）。",
                "Standard rests, gritty realism (8-hour short rest / 7-day long rest), or heroic rests (5-minute short rest / 1-hour long rest).",
            ),
            "option_labels": _option_labels(
                standard=("标准休息", "Standard rests"),
                gritty=("坚韧现实", "Gritty realism"),
                heroic=("英雄式休息", "Heroic rests"),
            ),
        },
        "dnd5e_surprise_rule": {
            "type": "enum",
            "default": "standard",
            "values": ["standard", "initiative_disadvantage", "none"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("突袭规则", "Surprise Rule"),
            "description_i18n": _text(
                "标准规则下被突袭者首轮不能行动或移动；也可改为先攻劣势，或忽略突袭。",
                "Under standard rules a surprised creature cannot act or move on the first round; alternatively use initiative disadvantage or ignore surprise.",
            ),
            "option_labels": _option_labels(
                standard=("标准：首轮不能行动", "Standard: no action on first round"),
                initiative_disadvantage=("先攻劣势", "Initiative disadvantage"),
                none=("忽略突袭", "Ignore surprise"),
            ),
        },
        "dnd5e_flanking": {
            "type": "boolean",
            "default": False,
            "scope": _SCOPE,
            "group": "optional",
            "group_label_i18n": optional,
            "label_i18n": _text("夹击给优势", "Flanking Grants Advantage"),
            "description_i18n": _text(
                "开启后，两名及以上近战角色夹击同一目标时，近战攻击获得优势。",
                "When enabled, melee attacks have advantage when two or more allies flank the same target.",
            ),
        },
        "dnd5e_cover_rule": {
            "type": "enum",
            "default": "standard",
            "values": ["standard", "none"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("掩体规则", "Cover Rule"),
            "description_i18n": _text(
                "标准掩体给半掩体 +2、四分之三掩体 +5 AC；关闭后忽略掩体加值。",
                "Standard cover grants +2 AC for half cover and +5 for three-quarters cover; disable to ignore cover bonuses.",
            ),
            "option_labels": _option_labels(
                standard=("标准掩体", "Standard cover"),
                none=("忽略掩体", "Ignore cover"),
            ),
        },
        "dnd5e_variant_encumbrance": {
            "type": "boolean",
            "default": False,
            "scope": _SCOPE,
            "group": "optional",
            "group_label_i18n": optional,
            "label_i18n": _text("变体负重", "Variant Encumbrance"),
            "description_i18n": _text(
                "开启后按力量计算负重阈值，超重会降低速度并带来检定劣势。",
                "When enabled, calculate encumbrance thresholds from Strength; being encumbered reduces speed and imposes disadvantage.",
            ),
        },
        "dnd5e_spell_points": {
            "type": "boolean",
            "default": False,
            "scope": _SCOPE,
            "group": "optional",
            "group_label_i18n": optional,
            "label_i18n": _text("法术点制", "Spell Points"),
            "description_i18n": _text(
                "开启后用法术点替代法术位，按 SRD 变体规则换算。",
                "When enabled, use spell points instead of spell slots using the SRD variant conversion.",
            ),
        },
        "dnd5e_positioning": {
            "type": "enum",
            "default": "theater_of_mind",
            "values": ["theater_of_mind", "grid"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("距离与位置口径", "Distance & Positioning"),
            "description_i18n": _text(
                "剧场式：距离用近战/近/远档位叙述，不依赖棋盘格（现状默认）。"
                "方格：距离以格计（1 格 = 5 尺），移动与射程按棋盘位置折算，"
                "适合带地图的战桌房间。该口径同时决定 AI 主持的距离描述方式。",
                "Theater of mind: narrated distance tiers without a grid (current default). "
                "Grid: distances in cells (5 ft each), movement and range follow board "
                "positions — for map-enabled tables. Also drives how the AI DM narrates distance.",
            ),
            "option_labels": _option_labels(
                theater_of_mind=("剧场式（近战/近/远档位）", "Theater of mind (melee/near/far tiers)"),
                grid=("方格（1 格 = 5 尺）", "Grid (1 cell = 5 ft)"),
            ),
        },
        "dnd5e_diagonal_rule": {
            "type": "enum",
            "default": "5-5-5",
            "values": ["5-5-5", "5-10-5"],
            "scope": _SCOPE,
            "group": "combat",
            "group_label_i18n": combat,
            "label_i18n": _text("对角线规则（预留）", "Diagonal Rule (reserved)"),
            "description_i18n": _text(
                "方格战斗的对角线规则（DMG 可选规则）：5-5-5 每步对角计 5 尺，"
                "5-10-5 交替计 5/10 尺。注意：引擎当前测距固定按 5-5-5（Chebyshev）"
                "结算，本键为预留档位、尚未生效。",
                "Diagonal movement on a grid (DMG optional rule): 5-5-5 counts every "
                "diagonal as 5 ft; 5-10-5 alternates 5/10 ft. Note: the engine currently "
                "measures distances as 5-5-5 (Chebyshev) — this key is reserved and not "
                "yet in effect.",
            ),
            "option_labels": _option_labels(
                **{
                    "5-5-5": ("5-5-5（每步对角 5 尺）", "5-5-5 (every diagonal 5 ft)"),
                    "5-10-5": ("5-10-5（交替 5/10 尺）", "5-10-5 (alternating 5/10 ft)"),
                }
            ),
        },
        "dnd5e_milestone_leveling": {
            "type": "boolean",
            "default": False,
            "scope": _SCOPE,
            "group": "optional",
            "group_label_i18n": optional,
            "label_i18n": _text("里程碑升级（预留）", "Milestone Leveling (reserved)"),
            "description_i18n": _text(
                "里程碑升级（DMG 可选规则）：由 KP 按剧情节点直接授级，跳过 XP 结算。"
                "注意：当前升级管线只支持 XP，本键为预留开关、尚未生效。",
                "Milestone leveling (DMG optional rule): the DM grants levels at story "
                "beats instead of XP. Note: the progression pipeline currently only "
                "supports XP — this key is reserved and not yet in effect.",
            ),
        },
    }


__all__ = ["house_rules"]
