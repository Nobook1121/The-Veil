"""5.10 社交技能：难度等级（规则书 p.79）。

难度由目标对象的「社交技能或心理学」中较高者决定：
- 目标技能 < 50：常规；
- 目标技能 >= 50：困难；
- 目标技能 >= 90：极难。

态度只调整难度等级：倾向支持直接同意；没有强烈感受不调整；强烈反对提高
1-2 级；玩家有切实筹码时降低 1 级。最低为常规，最高为极难。

本模块只计算，不掷骰、不读写角色卡；调用方按结果发起普通检定即可。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.coc7.rules import skill_value

DIFFICULTY_LADDER = ("regular", "hard", "extreme")

# 取悦 / 话术 / 恐吓 / 说服，以及心理学；名称与技能目录、英文键兼容。
DEFENSE_SKILLS: dict[str, tuple[str, ...]] = {
    "取悦": ("取悦", "charm"),
    "话术": ("话术", "fasttalk", "fast_talk"),
    "恐吓": ("恐吓", "intimidate"),
    "说服": ("说服", "persuade"),
    "心理学": ("心理学", "psychology"),
}

ATTITUDE_SHIFTS = {
    "neutral": 0,
    "opposed": 1,
    "hostile": 2,
}


def defense_skill(card: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """返回目标卡上最高的社交/心理学技能；没有可用技能时返回 ``None``。"""
    best: dict[str, Any] | None = None
    for label, aliases in DEFENSE_SKILLS.items():
        value = skill_value(card, aliases)
        if value is None:
            continue
        if best is None or value > int(best["value"]):
            best = {"skill": label, "value": int(value)}
    return best


def social_difficulty(
    target_card: Mapping[str, Any] | None = None,
    *,
    target_skill: int | None = None,
    attitude: str = "neutral",
    leverage: bool = False,
) -> dict[str, Any]:
    """给出一次社交检定的建议难度。

    ``target_card`` 用于自动取对方最高社交/心理学技能；``target_skill`` 可直接
    指定数值（NPC 未建卡时由 KP 估计）。``attitude`` 取
    ``supportive / neutral / opposed / hostile``。``leverage`` 表示玩家有切实
    论点、威胁等筹码。
    """
    attitude_key = str(attitude or "neutral").strip().lower()
    if attitude_key not in {"supportive", "neutral", "opposed", "hostile"}:
        raise ValueError("attitude must be supportive, neutral, opposed, or hostile")

    defense: dict[str, Any] | None = None
    if target_skill is None and isinstance(target_card, Mapping):
        defense = defense_skill(target_card)
        if defense is not None:
            target_skill = int(defense["value"])
    if target_skill is None:
        target_skill = 0

    base_index = 2 if target_skill >= 90 else 1 if target_skill >= 50 else 0
    base_difficulty = DIFFICULTY_LADDER[base_index]

    if attitude_key == "supportive":
        return {
            "auto_success": True,
            "difficulty": "auto",
            "base_difficulty": base_difficulty,
            "shift": 0,
            "target_skill": target_skill,
            "defense_skill": defense,
            "reason": "NPC 倾向支持玩家目标，直接同意，不检定",
        }

    shift = ATTITUDE_SHIFTS[attitude_key] - (1 if leverage else 0)
    index = max(0, min(len(DIFFICULTY_LADDER) - 1, base_index + shift))
    return {
        "auto_success": False,
        "difficulty": DIFFICULTY_LADDER[index],
        "base_difficulty": base_difficulty,
        "shift": shift,
        "target_skill": target_skill,
        "defense_skill": defense,
        "reason": "目标技能决定基础难度，态度与筹码各调整一至两级",
    }
