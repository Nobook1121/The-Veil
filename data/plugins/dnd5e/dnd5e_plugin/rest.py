"""D&D 5e 休息、生命骰与调谐。

核心把「短休/长休/调谐」当作规则集中立操作，只负责资源与状态的通用恢复
流程；休息时长、生命骰、恢复比例与同调上限等具体数值属于插件，由
``dnd5e_rest_variant`` 等房规决定。
"""

from __future__ import annotations

from typing import Any, Mapping

from dnd5e_plugin.combat import (
    _as_int,
    _equipment_items,
    _pack_entry,
    _roll_die,
    _text,
    house_rule,
)
from dnd5e_plugin.rules import ability_modifier, ability_score, sheet_of
from trpg_server.ruleset.effects import add_effect, effects_of, remove_effect

# 各休息变体的时长（分钟）；用于叙事时间推进、记录与提示词。
REST_PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "short_rest_minutes": 60,
        "long_rest_minutes": 480,
        "label": "标准休息",
    },
    "gritty": {
        "short_rest_minutes": 480,
        "long_rest_minutes": 10080,
        "label": "坚韧现实",
    },
    "heroic": {
        "short_rest_minutes": 5,
        "long_rest_minutes": 60,
        "label": "英雄式休息",
    },
}

ATTUNEMENT_LIMIT = 3


def _rest_variant(settings: Any) -> str:
    return str(house_rule(settings, "dnd5e_rest_variant", "standard") or "").strip().lower()


def rest_profile(settings: Any) -> dict[str, Any]:
    """按房间房规返回休息档案；未知取值退回标准休息。"""
    return dict(REST_PROFILES.get(_rest_variant(settings)) or REST_PROFILES["standard"])


def _class_hit_die(sheet: Mapping[str, Any]) -> int:
    declared = _text(sheet.get("class") or sheet.get("classId") or sheet.get("class_id")).casefold()
    if declared:
        from dnd5e_plugin.combat import _pack_entries, _display_name

        for entry in _pack_entries("class"):
            names = {
                _text(entry.get("id")).casefold(),
                _text(entry.get("name")).casefold(),
                _display_name(entry).casefold(),
            }
            if declared in names:
                return max(4, _as_int(entry.get("hit_die"), 8))
    return 8


def hit_dice_state(holder: Mapping[str, Any], sheet: Mapping[str, Any]) -> dict[str, int]:
    """读取生命骰池；卡面显式值优先，否则按职业生命骰与等级生成。"""
    raw = holder.get("hit_dice") or sheet.get("hit_dice")
    level = max(1, _as_int(sheet.get("level"), 1))
    die = _class_hit_die(sheet)
    if isinstance(raw, Mapping):
        die = max(4, _as_int(raw.get("die"), die))
        maximum = max(0, _as_int(raw.get("max"), level))
        used = max(0, min(maximum, _as_int(raw.get("used"), 0)))
        return {"die": die, "max": maximum, "used": used}
    return {"die": die, "max": level, "used": 0}


def _exhaustion_effect(holder: dict[str, Any]) -> dict[str, Any] | None:
    for effect in effects_of(holder, kind="exhaustion"):
        return dict(effect)
    return None


def _decrease_exhaustion(holder: dict[str, Any]) -> int:
    effect = _exhaustion_effect(holder)
    if effect is None:
        return 0
    level = max(0, _as_int(effect.get("stacks"), 1) - 1)
    remove_effect(holder, effect_id=str(effect.get("id") or ""))
    if level <= 0:
        return 0
    from dnd5e_plugin.effects import build_condition

    rebuilt = build_condition("exhaustion", levels=level)
    if rebuilt:
        rebuilt["id"] = str(effect.get("id") or "exhaustion")
        add_effect(holder, rebuilt)
    return level


class Dnd5eRest:
    """5e 的休息与调谐实现。"""

    ruleset_id = "dnd5e"

    # -- 档案 -------------------------------------------------------------
    def profile(self, settings: Any) -> dict[str, Any]:
        return rest_profile(settings)

    def attunement_limit(self, settings: Any) -> int:
        return ATTUNEMENT_LIMIT

    def attunement_requires_rest(self) -> bool:
        return True

    def requires_attunement(self, sheet: Mapping[str, Any], item_id: str) -> bool:
        key = _text(item_id).casefold()
        if not key:
            return False
        raw = sheet.get("equipment")
        if isinstance(raw, Mapping):
            raw = raw.get("items")
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping):
                identifier = _text(item.get("id") or item.get("item_id") or item.get("name"))
                if identifier.casefold() != key:
                    continue
                if item.get("requires_attunement") or item.get("attunement"):
                    return True
        entry = _pack_entry("equipment", item_id)
        return bool(entry and entry.get("requires_attunement"))

    # -- 休息 -------------------------------------------------------------
    def short_rest(
        self,
        holder: dict[str, Any],
        sheet: Mapping[str, Any],
        *,
        hit_dice: int,
        rng: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """消耗生命骰恢复 HP：每颗骰 dX + 体质调整值，至少 0。"""
        data = sheet_of(sheet) if isinstance(sheet, Mapping) else {}
        state = hit_dice_state(holder, data)
        available = max(0, state["max"] - state["used"])
        spend = max(0, min(int(hit_dice), available))
        con_mod = ability_modifier(ability_score(data, "con"))
        rolled = 0
        rolls: list[int] = []
        for _ in range(spend):
            value = max(0, _roll_die(rng, state["die"]) + con_mod)
            rolls.append(value)
            rolled += value
        before = max(0, _as_int(holder.get("hp"), 0))
        max_hp = max(1, _as_int(holder.get("max_hp"), 1))
        after = min(max_hp, before + rolled)
        state["used"] += spend
        holder["hit_dice"] = state
        holder["hp"] = after
        notes = [
            f"{holder.get('name')} 短休消耗 {spend} 颗 d{state['die']}（{rolls}），恢复 {after - before} HP。"
        ]
        if spend == 0:
            notes.append("没有可用的生命骰，短休未恢复 HP。")
        return {
            "hp_before": before,
            "hp_after": after,
            "healed": after - before,
            "hit_dice_spent": spend,
            "hit_dice_remaining": max(0, state["max"] - state["used"]),
            "notes": notes,
        }

    def long_rest(
        self,
        holder: dict[str, Any],
        sheet: Mapping[str, Any],
        *,
        rng: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """长休：回满 HP、恢复一半生命骰与全部法术位、力竭降 1 级、结束专注。"""
        data = sheet_of(sheet) if isinstance(sheet, Mapping) else {}
        before = max(0, _as_int(holder.get("hp"), 0))
        max_hp = max(1, _as_int(holder.get("max_hp"), 1))
        holder["hp"] = max_hp

        state = hit_dice_state(holder, data)
        regained = min(state["used"], max(1, state["max"] // 2)) if state["used"] else 0
        state["used"] = max(0, state["used"] - regained)
        holder["hit_dice"] = state

        slots = holder.get("spell_slots")
        if isinstance(slots, dict):
            for row in slots.values():
                if isinstance(row, dict):
                    row["used"] = 0
        holder.pop("spell_points", None)
        holder.pop("temp_hp", None)
        holder["temp_hp"] = 0

        exhaustion = _decrease_exhaustion(holder)
        notes = [f"{holder.get('name')} 长休：HP {before}→{max_hp}，恢复 {regained} 颗生命骰。"]
        if holder.pop("concentration", None) is not None:
            notes.append(f"{holder.get('name')} 的专注在长休中结束。")
        if exhaustion:
            notes.append(f"{holder.get('name')} 的力竭降至 {exhaustion} 级。")
        return {
            "hp_before": before,
            "hp_after": max_hp,
            "healed": max_hp - before,
            "hit_dice_regained": regained,
            "slots_restored": True,
            "exhaustion_level": exhaustion,
            "notes": notes,
        }


_REST = Dnd5eRest()


def rest_capability() -> Dnd5eRest:
    return _REST


__all__ = [
    "ATTUNEMENT_LIMIT",
    "Dnd5eRest",
    "REST_PROFILES",
    "hit_dice_state",
    "rest_capability",
    "rest_profile",
]
