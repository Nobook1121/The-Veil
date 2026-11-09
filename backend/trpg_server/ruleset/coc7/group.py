"""5.6 团体幸运检定（规则书 p.76）。

守秘人要求团体幸运时，由在场调查员中幸运值最低的玩家掷幸运。不能由高幸运
角色代掷，否则概率会被抬高。

本模块只负责选人与判定，不负责广播与日志。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from trpg_server.ruleset.coc7.rules import attribute_value


def _active_members(members: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        member
        for member in members
        if member.get("is_active", True) is not False and member.get("status", "active") != "removed"
    ]


def _card(member: Mapping[str, Any]) -> Mapping[str, Any]:
    card = member.get("character_card")
    return card if isinstance(card, Mapping) else member


def member_luck(member: Mapping[str, Any]) -> int | None:
    """读取成员幸运值；卡上没有幸运时返回 ``None``。"""
    card = _card(member)
    attributes = card.get("attributes")
    if isinstance(attributes, Mapping):
        for key, value in attributes.items():
            if str(key).strip().upper() == "LUC" and value not in (None, ""):
                return max(0, min(99, int(value)))
    for key in ("luck", "lucky", "LUC"):
        value = card.get(key)
        if value not in (None, ""):
            try:
                return max(0, min(99, int(value)))
            except (TypeError, ValueError):
                continue
    return None


def pick_lowest_luck(members: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """返回在场成员中幸运最低者；并列时取先出现者。"""
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    for member in _active_members(members):
        luck = member_luck(member)
        if luck is not None:
            candidates.append((luck, member))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def group_luck_check(
    members: Iterable[Mapping[str, Any]],
    *,
    roll: int,
    checks: Any = None,
) -> dict[str, Any]:
    """由最低幸运者结算一次团体幸运检定。"""
    member = pick_lowest_luck(members)
    if member is None:
        raise ValueError("no active member with luck value")
    luck = int(member_luck(member) or 0)
    roll_value = max(1, min(100, int(roll)))

    if checks is None:
        from trpg_server.ruleset.coc7.checks import Coc7Checks

        checks = Coc7Checks()
    level = str(checks.success_level(roll_value, luck))
    success = roll_value <= luck and level != "fumble"

    card = _card(member)
    return {
        "player_name": str(
            member.get("username") or member.get("character_name") or card.get("name") or ""
        ),
        "character_name": card.get("name"),
        "luck": luck,
        "roll": roll_value,
        "level": level,
        "success": success,
    }
