"""5.13 熟人（规则书 p.82-83）。

创建熟人：玩家选择最合适的技能或属性检定；地点会调整难度——远离家乡或
人烟稀少处提高一级；在家乡且找同专业人士降低一级。失败后孤注一掷应暗骰，
失败结果可能是「看似有帮助、实则敌对或不可靠」的熟人。

规则书没有给出固定的机械加值；``assist_bonus`` 是默认关闭的房规，用于把
「熟人提供帮助」量化为 1 颗奖励骰，由 KP 决定是否采用。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.coc7.options import Coc7RuleOptions

DIFFICULTY_LADDER = ("regular", "hard", "extreme")


def available_contacts(card: Mapping[str, Any] | None) -> int:
    """卡面 ``friends`` 条目数；只读，不判断关系质量。"""
    friends = card.get("friends") if isinstance(card, Mapping) else None
    if isinstance(friends, list):
        return sum(1 for item in friends if str(item or "").strip())
    if isinstance(friends, str):
        return 1 if friends.strip() else 0
    return 0


def contact_difficulty(
    *,
    base_difficulty: str = "regular",
    in_hometown: bool = False,
    remote: bool = False,
    same_profession: bool = False,
) -> dict[str, Any]:
    """按地点与关系调整创建熟人的难度。"""
    key = str(base_difficulty or "regular").strip().lower()
    if key not in DIFFICULTY_LADDER:
        raise ValueError("base_difficulty must be regular, hard, or extreme")
    index = DIFFICULTY_LADDER.index(key)
    # 远离家乡或人烟稀少：提高一级；在家乡且找同专业人士：降低一级。
    shift = 1 if remote else 0
    if in_hometown and same_profession:
        shift -= 1
    index = max(0, min(len(DIFFICULTY_LADDER) - 1, index + shift))
    return {
        "difficulty": DIFFICULTY_LADDER[index],
        "base_difficulty": key,
        "shift": shift,
        "dark_roll_on_push": True,
    }


def contact_check(
    card: Mapping[str, Any],
    name: str,
    *,
    in_hometown: bool = False,
    remote: bool = False,
    same_profession: bool = False,
    base_difficulty: str = "regular",
    checks: Any = None,
) -> dict[str, Any]:
    """给出创建熟人检定的目标值与难度；不掷骰。"""
    if checks is None:
        from trpg_server.ruleset.coc7.checks import Coc7Checks

        checks = Coc7Checks()
    value = checks.lookup_target(card, name)
    if value is None:
        raise ValueError(f"{name} was not found on character card")
    plan = contact_difficulty(
        base_difficulty=base_difficulty,
        in_hometown=in_hometown,
        remote=remote,
        same_profession=same_profession,
    )
    return {
        "name": name,
        "value": int(value),
        "threshold": int(checks.threshold(int(value), plan["difficulty"])),
        **plan,
    }


def contact_outcome(success: bool, *, pushed: bool = False) -> dict[str, Any]:
    """把检定结果映射为熟人状态提示；孤注一掷失败给出暗骰后果。"""
    if success:
        return {
            "state": "contact",
            "hint": "熟人成立；仍需说服、话术、取悦或恐吓才会按调查员意愿行动",
            "dark_roll": False,
        }
    if pushed:
        return {
            "state": "hostile_contact",
            "hint": "孤注一掷失败：可给出看似有帮助、实则会出卖或欺骗调查员的熟人",
            "dark_roll": True,
        }
    return {"state": "none", "hint": "未能找到熟人", "dark_roll": False}


def assist_bonus(card: Mapping[str, Any] | None, options: Coc7RuleOptions) -> int:
    """房规：启用熟人开关且卡上有熟人时，熟人协助给 1 颗奖励骰。"""
    if not options.contacts:
        return 0
    return 1 if available_contacts(card) > 0 else 0
