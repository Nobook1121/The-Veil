"""D&D 5e（SRD 5.1）状态与效果。

条件、力竭分级、持续时间与结束时的重复豁免都从 ``dnd5e-srd`` 的
``conditions.json`` 取数；本模块只把规则事实整理成核心的通用效果实例，
存储、持续时间推进与阈值事件由 ``trpg_server.ruleset.effects`` 负责。

核心不认识任何 5e 条件名：攻击/豁免/移动需要的修正都以“标志位”形式暴露在
效果实例的 ``detail.effects`` 里，由战斗与法术模块按标志位读取。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.effects import add_effect, effects_of, remove_effect

EXHAUSTION_ID = "exhaustion"
EXHAUSTION_MAX = 6


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _pack_entry(identifier: Any) -> dict[str, Any] | None:
    from dnd5e_plugin.combat import _pack_entry as lookup

    return lookup("condition", identifier)


def condition_label(entry: Mapping[str, Any]) -> str:
    locales = entry.get("locales")
    if isinstance(locales, Mapping):
        for key in ("zh-CN", "zh", "en-US", "en"):
            value = locales.get(key)
            if isinstance(value, Mapping) and _text(value.get("name")):
                return _text(value.get("name"))
    return _text(entry.get("name")) or _text(entry.get("id"))


def condition_ids(holder: Mapping[str, Any] | None) -> set[str]:
    result: set[str] = set()
    for effect in effects_of(holder, kind="condition"):
        detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
        identifier = _text(detail.get("condition"))
        if identifier:
            result.add(identifier.casefold())
    return result


def exhaustion_level(holder: Mapping[str, Any] | None) -> int:
    for effect in effects_of(holder, kind="exhaustion"):
        return max(0, min(EXHAUSTION_MAX, _as_int(effect.get("stacks"), 1)))
    return 0


def _exhaustion_flags(level: int) -> dict[str, Any]:
    entry = _pack_entry(EXHAUSTION_ID)
    if not entry:
        return {}
    flags: dict[str, Any] = {}
    rows = entry.get("effects_by_level") if isinstance(entry.get("effects_by_level"), list) else []
    for row in sorted(
        (item for item in rows if isinstance(item, Mapping)),
        key=lambda item: _as_int(item.get("level"), 0),
    ):
        if _as_int(row.get("level"), 0) > max(1, min(EXHAUSTION_MAX, level)):
            continue
        for flag in row.get("effects") or []:
            name = _text(flag)
            if name:
                flags[name] = True
    return flags


def flags_of(holder: Mapping[str, Any] | None) -> dict[str, Any]:
    """合并所有条件与力竭等级带来的标志位；核心不解释这些名字。"""
    flags: dict[str, Any] = {}
    for effect in effects_of(holder):
        kind = _text(effect.get("kind")).casefold()
        detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
        if kind == "condition":
            effects = detail.get("effects")
            if isinstance(effects, Mapping):
                flags.update(effects)
        elif kind == "exhaustion":
            flags.update(_exhaustion_flags(_as_int(effect.get("stacks"), 1)))
            flags["exhaustion_level"] = _as_int(effect.get("stacks"), 1)
    return flags


def auto_fail_saves(holder: Mapping[str, Any] | None) -> set[str]:
    flags = flags_of(holder)
    raw = flags.get("auto_fail_saves")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {_text(item).casefold() for item in raw if _text(item)}


def save_modifiers(holder: Mapping[str, Any] | None, ability: str) -> tuple[bool, bool]:
    """返回 ``(自动失败, 劣势)``；只依据插件声明的标志位。"""
    key = _text(ability).casefold()
    flags = flags_of(holder)
    auto = key in auto_fail_saves(holder)
    disadvantage = bool(flags.get("saving_throws_disadvantage"))
    if key == "dex" and flags.get("dex_saves_disadvantage"):
        disadvantage = True
    return auto, disadvantage


def effective_speed(holder: Mapping[str, Any] | None, base: int) -> int:
    flags = flags_of(holder)
    if flags.get("speed_zero"):
        return 0
    speed = max(0, _as_int(base, 0))
    if flags.get("speed_halved"):
        speed //= 2
    return speed


def attack_modifiers(
    attacker: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    *,
    ranged: bool = False,
) -> dict[str, bool]:
    """按双方条件的标志位给出优势/劣势与近战自动暴击。"""
    source = flags_of(attacker)
    defender = flags_of(target)
    advantage = bool(source.get("attack_rolls_advantage")) or bool(
        defender.get("attacks_against_advantage")
    )
    disadvantage = bool(source.get("attack_rolls_disadvantage")) or bool(
        source.get("attack_rolls_disadvantage_while_source_visible")
    ) or bool(defender.get("attacks_against_disadvantage"))
    if defender.get("attacks_against_advantage_within_5ft"):
        if ranged:
            disadvantage = True
        else:
            advantage = True
    auto_crit = bool(defender.get("melee_hits_within_5ft_critical")) and not ranged
    return {
        "advantage": advantage,
        "disadvantage": disadvantage,
        "auto_crit": auto_crit,
    }


def build_condition(
    condition_id: Any,
    *,
    source: str = "",
    duration: Mapping[str, Any] | None = None,
    dc: int | None = None,
    save_ends: Mapping[str, Any] | None = None,
    levels: int = 1,
    label: str = "",
) -> dict[str, Any] | None:
    """把一条条件（或力竭等级）整理成通用效果实例。"""
    key = _text(condition_id).casefold()
    entry = _pack_entry(key)
    if not entry:
        return None
    if key == EXHAUSTION_ID:
        level = max(1, min(EXHAUSTION_MAX, _as_int(levels, 1)))
        return {
            "id": EXHAUSTION_ID,
            "kind": "exhaustion",
            "scope": "character",
            "source": source or EXHAUSTION_ID,
            "stacks": level,
            "value": level,
            "duration": {"unit": "permanent"},
            "persistence": "rest",
            "detail": {
                "condition": EXHAUSTION_ID,
                "effects": _exhaustion_flags(level),
                "label": condition_label(entry),
                "level": level,
            },
        }
    effects = entry.get("effects") if isinstance(entry.get("effects"), Mapping) else {}
    detail: dict[str, Any] = {
        "condition": key,
        "effects": dict(effects),
        "label": label or condition_label(entry),
    }
    if dc is not None:
        detail["dc"] = int(dc)
    if isinstance(save_ends, Mapping) and save_ends:
        detail["save_ends"] = dict(save_ends)
    return {
        "id": f"condition:{key}:{source or 'effect'}",
        "kind": "condition",
        "scope": "character",
        "source": source or key,
        "stacks": 1,
        "duration": dict(duration) if isinstance(duration, Mapping) else {"unit": "permanent"},
        "persistence": "removable",
        "modifiers": [{"key": "condition", "value": key}],
        "detail": detail,
    }


def card_of(combatant: Mapping[str, Any]) -> dict[str, Any]:
    """把战斗者上的数值整理成检定壳可读的最小卡面。"""
    return {
        "sheet": {
            "level": max(1, _as_int(combatant.get("level"), 1)),
            "abilities": dict(combatant.get("abilities") or {}),
            "proficiencies": dict(combatant.get("proficiencies") or {}),
        }
    }


class Dnd5eEffects:
    """插件效果能力：条件构建、增删、回合 tick 与阈值回调。"""

    ruleset_id = "dnd5e"

    # -- 工具/法术调用 ---------------------------------------------------
    def apply(
        self,
        combatant: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = payload if isinstance(payload, Mapping) else {}
        notes: list[str] = []
        condition = _text(data.get("condition"))
        effect: dict[str, Any] | None = None
        if condition:
            if condition.casefold() == EXHAUSTION_ID:
                current = exhaustion_level(combatant)
                level = min(EXHAUSTION_MAX, current + max(1, _as_int(data.get("levels"), 1)))
                effect = build_condition(
                    EXHAUSTION_ID,
                    source=_text(data.get("source")) or EXHAUSTION_ID,
                    levels=level,
                )
                if effect and level >= EXHAUSTION_MAX:
                    combatant.update(
                        {"dead": True, "dying": False, "unconscious": True, "status": "dead"}
                    )
                    notes.append(f"{combatant.get('name')} 力竭达到 6 级，死亡。")
            else:
                effect = build_condition(
                    condition,
                    source=_text(data.get("source")) or "",
                    duration=data.get("duration") if isinstance(data.get("duration"), Mapping) else None,
                    dc=_as_int(data.get("dc"), 0) or None,
                    save_ends=data.get("save_ends") if isinstance(data.get("save_ends"), Mapping) else None,
                )
            if effect is None:
                return {"error": f"数据包中没有条件“{condition}”"}
        raw = data.get("effect")
        if effect is None and isinstance(raw, Mapping):
            effect = dict(raw)
        if effect is None:
            return {"error": "condition 或 effect 至少给一个"}
        stored = add_effect(combatant, effect)
        notes.append(f"{combatant.get('name')} 获得效果：{_label_of(stored)}。")
        notes.extend(self.on_effects_changed(combatant, combat=combat, reason="失能"))
        return {"effect": stored, "notes": notes}

    def on_effects_changed(
        self,
        combatant: dict[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> list[str]:
        """效果变化后检查是否失能；失能会打断专注并清理关联效果。"""
        if not isinstance(combatant.get("concentration"), Mapping):
            return []
        flags = flags_of(combatant)
        incapacitated = bool(flags.get("incapacitated")) or flags.get("can_take_actions") is False
        dead = bool(combatant.get("dead")) or bool(combatant.get("unconscious"))
        if not (incapacitated or dead):
            return []
        from dnd5e_plugin.spells import Dnd5eSpells

        why = reason or ("死亡" if combatant.get("dead") else "失去意识")
        return Dnd5eSpells().end_concentration(combatant, combat=combat, reason=why)


    def remove(self, combatant: dict[str, Any], identifier: Mapping[str, Any] | str) -> dict[str, Any]:
        payload = identifier if isinstance(identifier, Mapping) else {}
        target_id = _text(payload.get("id") or payload.get("effect_id"))
        condition = _text(payload.get("condition"))
        if not isinstance(identifier, Mapping):
            condition = _text(identifier)
        removed: list[dict[str, Any]] = []
        if target_id:
            removed.extend(remove_effect(combatant, effect_id=target_id))
        if condition:
            for effect in effects_of(combatant):
                detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
                names = {
                    _text(detail.get("condition")).casefold(),
                    _text(effect.get("kind")).casefold(),
                }
                if condition.casefold() in names:
                    removed.extend(remove_effect(combatant, effect_id=str(effect.get("id") or "")))
        if not removed and target_id:
            removed.extend(remove_effect(combatant, kind=target_id))
        notes = [f"{combatant.get('name')} 失去效果：{_label_of(item)}。" for item in removed]
        return {"removed": removed, "notes": notes}

    # -- 核心 tick 钩子 --------------------------------------------------
    def tick(
        self,
        combatant: dict[str, Any],
        effect: Mapping[str, Any],
        *,
        phase: str,
        timing: str | None = None,
        combat: Mapping[str, Any] | None = None,
        rng: Any = None,
    ) -> dict[str, Any]:
        detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
        resource = detail.get("resource")
        if isinstance(resource, Mapping):
            at = _text(resource.get("timing")) or "turn_end"
            if not timing or timing == at:
                return {"resource": dict(resource)}
        save_ends = detail.get("save_ends")
        if not isinstance(save_ends, Mapping):
            return {}
        ends_at = _text(save_ends.get("timing")) or "turn_end"
        if timing and timing != ends_at:
            return {}
        from dnd5e_plugin.checks import Dnd5eChecks
        from trpg_server.ruleset.contracts import CheckRequest

        ability = _text(save_ends.get("ability")) or "wis"
        dc = max(1, _as_int(save_ends.get("dc"), _as_int(detail.get("dc"), 10)))
        auto, disadvantage = save_modifiers(combatant, ability)
        outcome = Dnd5eChecks().roll_outcome(
            card_of(combatant),
            CheckRequest(
                kind="save",
                name=f"{ability}豁免",
                target=dc,
                extra={"ability": ability, "auto_fail": auto, "disadvantage": disadvantage},
            ),
            rng,
        )
        label = _label_of(effect)
        summary = str(outcome.presentation.get("summary") or "")
        if outcome.success:
            return {
                "remove": True,
                "notes": [f"{combatant.get('name')} 的{label}结束（{summary}）"],
            }
        return {"notes": [f"{combatant.get('name')} 的{label}持续（{summary}）"]}

    def on_expired(self, combatant: dict[str, Any], effect: Mapping[str, Any]) -> list[str]:
        detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
        if detail.get("silent"):
            return []
        return [f"{combatant.get('name')} 的{_label_of(effect)}结束。"]

    def on_threshold(
        self,
        combatant: dict[str, Any],
        event: Mapping[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        rng: Any = None,
    ) -> list[str]:
        name = _text(event.get("event")).casefold()
        if name == "zero_hp":
            return [f"{combatant.get('name')} 的生命值降到 0。"]
        return []


def _label_of(effect: Mapping[str, Any]) -> str:
    detail = effect.get("detail") if isinstance(effect.get("detail"), Mapping) else {}
    label = _text(detail.get("label"))
    if label:
        return label
    condition = _text(detail.get("condition"))
    entry = _pack_entry(condition) if condition else None
    if entry:
        return condition_label(entry)
    return _text(effect.get("kind")) or "效果"


__all__ = [
    "Dnd5eEffects",
    "EXHAUSTION_ID",
    "attack_modifiers",
    "auto_fail_saves",
    "build_condition",
    "card_of",
    "condition_ids",
    "condition_label",
    "effective_speed",
    "exhaustion_level",
    "flags_of",
    "save_modifiers",
]
