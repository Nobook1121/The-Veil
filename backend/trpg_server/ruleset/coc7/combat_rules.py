"""CoC7 战斗细节（批次 4）的纯规则计算。

核心只负责战斗状态存取与调度；这里的函数不读写房间文件、不掷骰，
只把「房规开关 + 输入状态」映射成修正值、状态补丁或结果，方便单测。

对应规则书：

- 瞄准：需在行动前声明，下一轮攻击获得 1 奖励骰；受伤或移动后失效（p.96）。
- 掩体：目标有掩体时攻击 1 惩罚骰；成功闪避后攻击者失去下一次攻击（p.90、p.96）。
- 射击入近战：额外 1 惩罚骰；大失败时命中同方最倒霉的同伴（p.96 房规化）。
- 击晕：钝器战技成功只造成 1 点伤害并击晕目标（p.106）。
- 部位命中：1D20 定位，用于描述伤害落点与重伤后果（p.108 可选规则）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# 瞄准
AIM_BONUS = 1
# 掩体
COVER_PENALTY = 1
# 射击入近战
FRIENDLY_FIRE_PENALTY = 1
# 击晕
KNOCKOUT_DAMAGE = 1

# 成功等级中视为「闪避成功」的集合。
SUCCESS_LEVELS = frozenset({"regular", "hard", "extreme", "critical"})

# 1D20 部位表：上限 → 部位（p.108）。
HIT_LOCATION_TABLE: tuple[tuple[int, str], ...] = (
    (3, "右腿"),
    (6, "左腿"),
    (10, "腹部"),
    (15, "胸部"),
    (17, "右臂"),
    (20, "左臂"),
)
# 四肢部位：重伤后该肢体失效。
LIMB_LOCATIONS = frozenset({"右腿", "左腿", "右臂", "左臂"})


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ---------------------------------------------------------------------------
# 瞄准
# ---------------------------------------------------------------------------
def aim_bonus(aiming: Any) -> int:
    """处于瞄准状态时，下一次远程攻击获得 1 奖励骰。"""
    return AIM_BONUS if _as_bool(aiming) else 0


def can_declare_aim(combatant: Mapping[str, Any]) -> tuple[bool, str]:
    """判断能否在本轮声明瞄准；不能时返回原因。"""
    if combatant.get("dead"):
        return False, "角色已死亡，无法瞄准"
    if combatant.get("unconscious"):
        return False, "角色已昏迷，无法瞄准"
    if combatant.get("dying"):
        return False, "角色濒死，无法瞄准"
    if _as_bool(combatant.get("aiming")):
        return False, "角色已经在瞄准"
    return True, ""


def declare_aim(round_no: Any) -> dict[str, Any]:
    """声明瞄准的状态补丁；瞄准在下一轮攻击时生效。"""
    try:
        round_value = max(1, int(round_no))
    except (TypeError, ValueError):
        round_value = 1
    return {"aiming": True, "aim_declared_round": round_value}


def aim_consumed_patch() -> dict[str, Any]:
    """攻击结算后清除瞄准状态。"""
    return {"aiming": False, "aim_declared_round": 0}


def clear_aim(combatant: dict[str, Any], reason: str = "") -> bool:
    """受伤或移动时清除瞄准；返回此前是否处于瞄准状态（原地修改）。"""
    was_aiming = _as_bool(combatant.get("aiming"))
    combatant["aiming"] = False
    combatant["aim_declared_round"] = 0
    if was_aiming and reason:
        combatant["aim_lost_reason"] = reason
    return was_aiming


# ---------------------------------------------------------------------------
# 掩体与闪避
# ---------------------------------------------------------------------------
def cover_penalty(has_cover: Any) -> int:
    """目标处于掩体后时，攻击承受 1 惩罚骰。"""
    return COVER_PENALTY if _as_bool(has_cover) else 0


def dodge_success(level: Any) -> bool:
    """闪避检定达到常规成功或更好即视为闪避成功。"""
    return str(level or "").strip().lower() in SUCCESS_LEVELS


def attacker_loses_next_attack(level: Any) -> bool:
    """房规：目标借掩体闪避成功，攻击者失去下一次攻击。"""
    return dodge_success(level)


# ---------------------------------------------------------------------------
# 射击入近战
# ---------------------------------------------------------------------------
def friendly_fire_penalty(in_melee: Any) -> int:
    """向近战中的目标射击，额外 1 惩罚骰。"""
    return FRIENDLY_FIRE_PENALTY if _as_bool(in_melee) else 0


def friendly_fire_victim(
    combatants: Mapping[str, Mapping[str, Any]],
    *,
    attacker: str,
    target: str,
    side: Any = None,
    exclude: Iterable[str] = (),
) -> str | None:
    """大失败时选择被误伤的同伴：同方、存活、非攻击者/目标，幸运最低者优先。"""
    attacker_key = str(attacker or "").casefold()
    target_key = str(target or "").casefold()
    blocked = {attacker_key, target_key}
    blocked.update(str(name).casefold() for name in exclude if str(name).strip())

    def _luck(name: str, item: Mapping[str, Any]) -> int:
        try:
            return int(item.get("luck") or 0)
        except (TypeError, ValueError):
            return 0

    candidates: list[tuple[int, str]] = []
    for name, item in combatants.items():
        key = str(name).casefold()
        if key in blocked:
            continue
        if side is not None and str(item.get("side") or "") != str(side):
            continue
        if item.get("dead") or item.get("unconscious") or item.get("dying"):
            continue
        candidates.append((_luck(name, item), str(item.get("name") or name)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


# ---------------------------------------------------------------------------
# 击晕
# ---------------------------------------------------------------------------
def knockout_result(rounds: Any) -> dict[str, Any]:
    """钝器战技成功：造成 1 点伤害并使目标昏迷指定轮数。"""
    try:
        duration = max(1, int(rounds))
    except (TypeError, ValueError):
        duration = 1
    return {"damage": KNOCKOUT_DAMAGE, "unconscious": True, "rounds": duration}


def is_blunt_weapon(weapon: Mapping[str, Any] | None) -> bool:
    """判断武器是否适合击晕（钝器或徒手）。"""
    data = weapon if isinstance(weapon, Mapping) else {}
    text = " ".join(
        str(data.get(key) or "")
        for key in ("name", "nameEn", "skill", "skillKey", "category", "type")
    ).casefold()
    blunt_tokens = ("钝", "棍", "棒", "锤", "杖", "拳", "徒手", "blunt", "club", "hammer", "mace", "brawl", "fist")
    return any(token in text for token in blunt_tokens)


# ---------------------------------------------------------------------------
# 部位命中
# ---------------------------------------------------------------------------
def hit_location(roll: Any) -> dict[str, Any]:
    """按 1D20 返回命中部位；超出范围时夹到 1-20。"""
    try:
        value = min(20, max(1, int(roll)))
    except (TypeError, ValueError):
        value = 1
    location = HIT_LOCATION_TABLE[-1][1]
    for upper, name in HIT_LOCATION_TABLE:
        if value <= upper:
            location = name
            break
    return {"roll": value, "location": location, "limb": location in LIMB_LOCATIONS}


def major_wound_effect(location: Any) -> str:
    """部位重伤的附加后果描述（不额外改变数值）。"""
    name = str(location or "").strip()
    if name in {"右腿", "左腿"}:
        return "腿部重伤，倒地，移动受限"
    if name in {"右臂", "左臂"}:
        return "手臂重伤，该肢体无法使用"
    if name == "胸部":
        return "胸部重伤，濒死风险更高"
    if name == "腹部":
        return "腹部重伤，持续失血"
    return ""


# ---------------------------------------------------------------------------
# 毒剂
# ---------------------------------------------------------------------------
def poison_check_target(constitution: Any) -> int:
    """毒剂抗性按体质检定；目标值即 CON。"""
    try:
        return max(1, int(constitution))
    except (TypeError, ValueError):
        return 1


def poison_penalty_dice(potency: Any) -> int:
    """毒性换算为体质检定的惩罚骰：0-2，超过按 2 处理。"""
    try:
        value = int(potency)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(2, value))


def poison_resisted(level: Any) -> bool:
    """体质检定达到常规成功或更好即为抵抗成功。"""
    return dodge_success(level)


def poison_damage(total: Any, *, resisted: Any) -> int:
    """抵抗成功伤害减半（向下取整）；失败承受完整伤害。"""
    try:
        value = max(0, int(total))
    except (TypeError, ValueError):
        return 0
    if _as_bool(resisted):
        return value // 2
    return value


# ---------------------------------------------------------------------------
# 自然恢复
# ---------------------------------------------------------------------------
def natural_healing(days: Any, *, major_wound: Any = False) -> int:
    """按休养天数结算自然恢复：常规每日 1 点，重伤每周 1 点（p.62）。"""
    try:
        total_days = max(0, int(days))
    except (TypeError, ValueError):
        return 0
    if total_days <= 0:
        return 0
    if _as_bool(major_wound):
        return total_days // 7
    return total_days
