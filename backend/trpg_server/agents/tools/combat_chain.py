"""战斗数值链（combat log chain）。

一次攻击/豁免结算在聊天里应当是一张「单卡闭环」：攻击式 → 命中 vs AC/DC →
伤害 → HP 变动。规则集返回的数字其实都在，但分散在多个字段里，前端只能把中文
摘要当纯文本显示。本模块把这些数字整理成结构化 ``combat_log``，作为工具消息
metadata 的**追加**字段下发::

    metadata = {"dice_roll": {...}, "combat_log": {...}}

``dice_roll`` 完全不动（骰桌动画链路依赖它）；旧消息没有 ``combat_log`` 时前端
按老样式渲染，不会崩。本模块只做纯数据整理：不读房间状态、不掷骰、不写日志，
因此可以在任何消息组装点安全调用。
"""
from __future__ import annotations

from typing import Any, Mapping

CHAIN_VERSION = 1

# 命中判定口径：dnd5e 用 success + critical 字段；coc7 用 success + success_level。
_CRITICAL_LEVELS = {"critical", "critical_success", "extreme"}

_HIT = "hit"
_MISS = "miss"
_CRITICAL = "critical"
_SAVE_SUCCEEDED = "save_succeeded"
_SAVE_FAILED = "save_failed"


def _int(value: Any) -> int | None:
    """宽松取整：缺省、布尔与不可解析值一律返回 ``None``（不猜 0）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ints(values: Any) -> list[int]:
    result: list[int] = []
    for item in values or []:
        number = _int(item)
        if number is not None:
            result.append(number)
    return result


# ---------------------------------------------------------------------------
# 伤害段
# ---------------------------------------------------------------------------
def damage_notes(damage: Mapping[str, Any]) -> list[str]:
    """把伤害结果里的附加状态翻成卡面上的短标签（顺序稳定）。"""
    notes: list[str] = []
    absorbed = _int(damage.get("temp_hp_absorbed")) or 0
    if absorbed > 0:
        notes.append(f"临时 HP 吸收 {absorbed}")
    if damage.get("vulnerability"):
        notes.append("易伤翻倍")
    elif damage.get("resistance"):
        notes.append("抗性减半")
    elif damage.get("immunity"):
        notes.append("免疫")
    if damage.get("major_wound"):
        notes.append("重伤")
    if damage.get("dead"):
        notes.append("目标死亡")
    elif damage.get("dying"):
        notes.append("目标陷入濒死")
    elif damage.get("unconscious"):
        notes.append("目标昏迷")
    note = _text(damage.get("note"))
    if note:
        notes.append(note)
    for item in damage.get("concentration") or []:
        text = _text(item)
        if text:
            notes.append(text)
    for item in damage.get("notes") or []:
        text = _text(item)
        if text and text not in notes:
            notes.append(text)
    return notes


def _damage_fields(damage: Any) -> dict[str, Any]:
    """伤害结果 → 卡面字段；没有可用伤害数据时返回空字典。"""
    if not isinstance(damage, Mapping):
        return {}
    raw = _int(damage.get("damage"))
    if raw is None:
        raw = _int(damage.get("amount"))
    effective = _int(damage.get("effective"))
    if effective is None:
        effective = raw
    roll_text = _text(damage.get("damage_detail") or damage.get("dice"))
    fields: dict[str, Any] = {
        "damage_roll": roll_text or None,
        "damage": effective,
        "damage_raw": raw,
        "damage_type": _text(damage.get("damage_type") or damage.get("type")) or None,
        "hp_before": _int(damage.get("hp_before")),
        "hp_after": _int(damage.get("hp_after")),
        "notes": damage_notes(damage),
    }
    if (
        fields["damage"] is None
        and fields["hp_before"] is None
        and fields["hp_after"] is None
        and not fields["damage_roll"]
    ):
        return {}
    return fields


def _empty_chain(kind: str) -> dict[str, Any]:
    """预先铺满字段，让渲染层不必逐键判空。"""
    return {
        "version": CHAIN_VERSION,
        "kind": kind,
        "attacker": "",
        "target": "",
        "weapon": "",
        "spell": "",
        "expression": "",
        "roll": None,
        "rolls": [],
        "modifier": None,
        "total": None,
        "compare": {"label": "", "value": None},
        "hit": False,
        "critical": False,
        "outcome": _MISS,
        "damage_roll": None,
        "damage": None,
        "damage_raw": None,
        "damage_type": None,
        "hp_before": None,
        "hp_after": None,
        "hp_delta": None,
        "notes": [],
    }


def _finish(chain: dict[str, Any], damage: Any) -> dict[str, Any]:
    fields = _damage_fields(damage)
    if fields:
        chain.update(fields)
    before, after = chain.get("hp_before"), chain.get("hp_after")
    if before is not None and after is not None:
        chain["hp_delta"] = after - before
    return chain


# ---------------------------------------------------------------------------
# 攻击段
# ---------------------------------------------------------------------------
def attack_chain(result: Any) -> dict[str, Any] | None:
    """一次攻击结算（d20 或 d100 口径）→ 数值链；无法识别时返回 ``None``。

    dnd5e 传 ``target_ac``/``attack_bonus``，coc7 传 ``skill_value``；两种口径
    共用同一张卡，``compare`` 里写明比的是 AC 还是技能值。
    """
    if not isinstance(result, Mapping) or result.get("error"):
        return None
    percentile = _text(result.get("attack_mode")) == "percentile" or (
        "target_ac" not in result and "skill_value" in result
    )
    roll = _int(result.get("natural"))
    if roll is None:
        roll = _int(result.get("roll"))
    if roll is None:
        return None
    rolls = _ints(result.get("rolls")) or [roll]
    hit = bool(result.get("success"))
    level = _text(result.get("success_level")).casefold()
    critical = bool(result.get("critical")) or level in _CRITICAL_LEVELS
    chain = _empty_chain("attack")
    chain["attacker"] = _text(result.get("attacker"))
    chain["target"] = _text(result.get("target"))
    chain["weapon"] = _text(result.get("weapon"))
    chain["roll"] = roll
    chain["rolls"] = rolls
    chain["hit"] = hit
    chain["critical"] = bool(critical and hit)
    if percentile:
        chain["ruleset"] = "coc7"
        chain["expression"] = "1d100"
        chain["total"] = roll
        chain["compare"] = {"label": "技能值", "value": _int(result.get("skill_value"))}
        chain["skill_value"] = _int(result.get("skill_value"))
        defense = _text(result.get("defense"))
        if defense and defense != "none":
            chain["defense"] = {
                "kind": defense,
                "roll": _int(result.get("defense_roll")),
                "level": _text(result.get("defense_level")),
            }
    else:
        chain["ruleset"] = "dnd5e"
        chain["expression"] = _text(result.get("expression")) or "1d20"
        total = _int(result.get("total"))
        modifier = _int(result.get("attack_bonus"))
        if modifier is None and total is not None:
            modifier = total - roll
        if total is None and modifier is not None:
            total = roll + modifier
        chain["modifier"] = modifier
        chain["total"] = total
        ac = _int(result.get("target_ac"))
        chain["compare"] = {"label": "AC", "value": ac}
        chain["attack_total"] = total
        chain["target_ac"] = ac
        if result.get("advantage") or result.get("disadvantage"):
            chain["advantage"] = bool(result.get("advantage"))
            chain["disadvantage"] = bool(result.get("disadvantage"))
    chain["outcome"] = _CRITICAL if chain["critical"] else (_HIT if hit else _MISS)
    return _finish(chain, result.get("damage"))


def _outcome_fields(source: Any) -> dict[str, Any]:
    """判定壳（CheckRequest outcome）→ 骰面字段；兼容 ``to_dict`` 的两种形态。"""
    if not isinstance(source, Mapping):
        return {}
    detail = source.get("detail") if isinstance(source.get("detail"), Mapping) else {}
    dice = _ints(source.get("dice"))
    roll = _int(detail.get("kept"))
    if roll is None:
        roll = dice[0] if dice else None
    total = _int(source.get("total"))
    if roll is None and total is None:
        return {}
    modifier = total - roll if (total is not None and roll is not None) else None
    return {
        "roll": roll,
        "rolls": dice or ([roll] if roll is not None else []),
        "total": total,
        "modifier": modifier,
        "success": source.get("success"),
        "degree": _text(source.get("degree")).casefold(),
        "target": _int(source.get("target")),
    }


def spell_attack_chain(
    attack: Any,
    *,
    caster: Any = "",
    target: Any = "",
    spell: Any = "",
    damage: Any = None,
) -> dict[str, Any] | None:
    """法术攻击（如火焰箭）：攻击式 → vs AC → 命中/未命中 → 伤害 → HP。"""
    fields = _outcome_fields(attack)
    if not fields:
        return None
    chain = _empty_chain("attack")
    chain["ruleset"] = "dnd5e"
    chain["attacker"] = _text(caster)
    chain["target"] = _text(target)
    chain["spell"] = _text(spell)
    chain["weapon"] = _text(spell)
    chain["expression"] = "1d20"
    chain["roll"] = fields["roll"]
    chain["rolls"] = fields["rolls"]
    chain["modifier"] = fields["modifier"]
    chain["total"] = fields["total"]
    ac = fields["target"]
    chain["compare"] = {"label": "AC", "value": ac}
    chain["attack_total"] = fields["total"]
    chain["target_ac"] = ac
    hit = bool(fields["success"])
    critical = fields["degree"] in _CRITICAL_LEVELS
    chain["hit"] = hit
    chain["critical"] = bool(critical and hit)
    chain["outcome"] = _CRITICAL if chain["critical"] else (_HIT if hit else _MISS)
    return _finish(chain, damage)


def save_chain(
    save: Any,
    *,
    caster: Any = "",
    spell: Any = "",
    damage: Any = None,
) -> dict[str, Any] | None:
    """豁免类（如圣火）：豁免式 → vs DC → 成功/失败 → 伤害（含half）→ HP。"""
    if not isinstance(save, Mapping):
        return None
    fields = _outcome_fields(save)
    dc = _int(save.get("dc"))
    if dc is None:
        dc = fields.get("target")
    success = save.get("success")
    if not fields and dc is None and not isinstance(success, bool):
        return None
    succeeded = bool(success)
    chain = _empty_chain("save")
    chain["ruleset"] = "dnd5e"
    chain["attacker"] = _text(caster)
    chain["target"] = _text(save.get("target"))
    chain["spell"] = _text(spell)
    chain["expression"] = "1d20"
    chain["roll"] = fields.get("roll")
    chain["rolls"] = fields.get("rolls") or []
    chain["modifier"] = fields.get("modifier")
    chain["total"] = fields.get("total")
    chain["compare"] = {"label": "DC", "value": dc}
    chain["save_total"] = fields.get("total")
    chain["dc"] = dc
    chain["save_ability"] = _text(save.get("ability"))
    chain["success"] = succeeded
    chain["hit"] = not succeeded
    if save.get("auto_fail"):
        chain["auto_fail"] = True
    chain["outcome"] = _SAVE_SUCCEEDED if succeeded else _SAVE_FAILED
    return _finish(chain, damage)


__all__ = [
    "CHAIN_VERSION",
    "attack_chain",
    "damage_notes",
    "save_chain",
    "spell_attack_chain",
]
