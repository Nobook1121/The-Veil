"""CoC7 技能成长与幕间成长检定的纯规则计算。

边界：
- 本模块只做数学与筛选，不读写房间 / 角色卡；落盘、权限与留痕由 ``room_interlude``
  与 ``room_rules`` 负责。
- 成长标记在剧情中的技能检定成功时产生；幕间统一掷成长检定，一次结算后写回。
- 克苏鲁神话与信用评级不可成长，也不参与 90%+ 技能的幕间 SAN 恢复。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from trpg_server.ruleset.coc7.rules import SKILL_MAX, MYTHOS_SKILL_NAMES, skill_rows, skill_value

# 信用评级在角色卡里既可能是技能名也可能的技能键。
CREDIT_RATING_NAMES = {"信用评级", "creditrating", "credit rating", "credit_rating"}
# 技能达到该值时，幕间额外恢复 2D6 SAN（每个技能一次）。
HIGH_SKILL_SAN_THRESHOLD = 90


def _normalize(name: Any) -> str:
    return "".join(str(name or "").split()).casefold()


def is_growth_eligible(skill_name: Any) -> bool:
    """该技能是否可以成长 / 参与 90%+ 恢复。"""
    normalized = _normalize(skill_name)
    if not normalized:
        return False
    if normalized in {_normalize(name) for name in MYTHOS_SKILL_NAMES}:
        return False
    if normalized in {_normalize(name) for name in CREDIT_RATING_NAMES}:
        return False
    return True


def mark_decision(
    skill_name: Any,
    value: Any,
    *,
    success: bool,
    bonus_dice: int = 0,
    luck_spent: bool = False,
    opposed_failed: bool = False,
) -> dict[str, Any]:
    """返回一次技能检定是否打勾以及原因（供留痕与调试）。

    ``eligible=True`` 时 ``reason="check_success"``；否则 reason 说明被哪条
    排除项挡下，便于写进 ``extensions.growth.marks`` 的留痕字段。
    """
    if not success:
        return {"eligible": False, "reason": "not_success"}
    if int(bonus_dice or 0) > 0:
        return {"eligible": False, "reason": "bonus_dice"}
    if luck_spent:
        return {"eligible": False, "reason": "luck_spent"}
    if opposed_failed:
        return {"eligible": False, "reason": "opposed_failed"}
    if not is_growth_eligible(skill_name):
        return {"eligible": False, "reason": "not_growth_eligible"}
    try:
        number = int(value)
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "invalid_value"}
    if not (0 <= number <= SKILL_MAX):
        return {"eligible": False, "reason": "out_of_range"}
    return {"eligible": True, "reason": "check_success"}


def should_mark(
    skill_name: Any,
    value: Any,
    *,
    success: bool,
    bonus_dice: int = 0,
    luck_spent: bool = False,
    opposed_failed: bool = False,
) -> bool:
    """一次技能检定后是否打成长标记。

    口径：只有成功、未使用奖励骰、未花费幸运、对抗未失败的技能检定才打勾。
    属性检定、神话与信用评级不打勾。
    """
    return mark_decision(
        skill_name,
        value,
        success=success,
        bonus_dice=bonus_dice,
        luck_spent=luck_spent,
        opposed_failed=opposed_failed,
    )["eligible"]


# 角色卡扩展里成长标记的命名空间与键（批次 2；旧数据仍在成员快照里）。
EXTENSION_NAMESPACE = "growth"
EXTENSION_MARKS_KEY = "marks"
EXTENSION_HISTORY_LIMIT = 20


def marks_from_card(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """读 ``extensions.growth.marks``（新位置）；结构不对时返回空列表。"""
    extensions = card.get("extensions") if isinstance(card.get("extensions"), Mapping) else {}
    block = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    marks = block.get(EXTENSION_MARKS_KEY) if isinstance(block, Mapping) else None
    if not isinstance(marks, list):
        return []
    return [row for row in marks if isinstance(row, Mapping)]


def read_marks(card: Mapping[str, Any], fallback: Any = None) -> list[dict[str, Any]]:
    """优先读角色卡扩展里的标记；旧房间快照里的标记作为兼容回退。"""
    marks = marks_from_card(card)
    if marks:
        return marks
    if isinstance(fallback, list):
        return [row for row in fallback if isinstance(row, Mapping)]
    return []


def growth_check(value: Any, *, roll: int, gain_roll: int) -> dict[str, Any]:
    """一次成长检定：1D100 > 技能值（或 >95）时技能 +1D10。

    ``roll`` 与 ``gain_roll`` 由调用方提供，便于测试与复用同一 RNG。
    """
    before = max(0, min(SKILL_MAX, int(value)))
    d100 = max(1, min(100, int(roll)))
    improved = d100 > before or d100 > 95
    gain = max(1, min(10, int(gain_roll))) if improved else 0
    after = min(SKILL_MAX, before + gain)
    return {
        "before": before,
        "roll": d100,
        "improved": improved,
        "gain": after - before,
        "after": after,
    }


def resolve_member_growth(
    card: Mapping[str, Any],
    marks: Iterable[Mapping[str, Any]],
    rng: Callable[[int], int],
) -> list[dict[str, Any]]:
    """按成员的成长标记逐项掷成长检定（同一技能只掷一次）。"""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mark in marks or []:
        if not isinstance(mark, Mapping):
            continue
        name = str(mark.get("skill") or mark.get("name") or "").strip()
        key = _normalize(name)
        if not key or key in seen:
            continue
        seen.add(key)
        if not is_growth_eligible(name):
            continue
        value = skill_value(card, [name])
        if value is None:
            continue
        outcome = growth_check(value, roll=rng(100), gain_roll=rng(10))
        results.append(
            {
                "skill": name,
                "value": value,
                **outcome,
                "mark_id": mark.get("id"),
            }
        )
    return results


def high_skill_san_awards(card: Mapping[str, Any], rng: Callable[[int], int]) -> list[dict[str, Any]]:
    """技能达到 90%+ 的幕间 SAN 恢复：每个合格技能 2D6。"""
    awards: list[dict[str, Any]] = []
    for row in skill_rows(card):
        name = str(row.get("name") or "").strip()
        value = skill_value(card, [name]) if name else None
        if value is None or value < HIGH_SKILL_SAN_THRESHOLD:
            continue
        if not is_growth_eligible(name):
            continue
        san = sum(max(1, min(6, int(rng(6)))) for _ in range(2))
        awards.append({"skill": name, "value": value, "san": san})
    return awards


__all__ = [
    "EXTENSION_MARKS_KEY",
    "EXTENSION_NAMESPACE",
    "HIGH_SKILL_SAN_THRESHOLD",
    "growth_check",
    "high_skill_san_awards",
    "is_growth_eligible",
    "mark_decision",
    "marks_from_card",
    "read_marks",
    "resolve_member_growth",
    "should_mark",
]
