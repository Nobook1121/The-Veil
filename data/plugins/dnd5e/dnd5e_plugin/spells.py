"""D&D 5e（SRD 5.1）法术：法术位、已知/准备、升环、攻击/豁免与专注。

所有法术事实都来自 ``dnd5e-srd`` 的 ``spell`` / ``class`` 条目：环阶、施法时间、
豁免属性、伤害/治疗骰、升环方式、专注与施加的条件。伤害与治疗复用 E4 的
``_apply_damage`` 管线；攻击与豁免通过 ``Dnd5eChecks.roll_outcome`` 走 E0 判定壳。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping
from uuid import uuid4

from dnd5e_plugin import spell_schema
from dnd5e_plugin.combat import (
    _as_int,
    _display_name,
    _pack_entries,
    _pack_entry,
    _parse_damage,
    _roll_terms,
    _text,
)
from dnd5e_plugin.effects import (
    Dnd5eEffects,
    attack_modifiers,
    build_condition,
    card_of,
    save_modifiers,
)
from dnd5e_plugin.rules import ability_modifier, ability_score, proficiency_bonus, sheet_of
from trpg_server.ruleset.contracts import CheckRequest
from trpg_server.ruleset.effects import add_effect, remove_effect

CASTING_SLOT_BY_TIME = {
    "bonus action": "bonus_action",
    "1 bonus action": "bonus_action",
    "reaction": "reaction",
    "1 reaction": "reaction",
    "action": "action",
    "1 action": "action",
}


def _spell_label(entry: Mapping[str, Any]) -> str:
    locales = entry.get("locales")
    if isinstance(locales, Mapping):
        for key in ("zh-CN", "zh", "en-US", "en"):
            value = locales.get(key)
            if isinstance(value, Mapping) and _text(value.get("name")):
                return _text(value.get("name"))
    return _text(entry.get("name")) or _text(entry.get("id"))


def _class_entry(sheet: Mapping[str, Any]) -> dict[str, Any] | None:
    declared = sheet.get("class") or sheet.get("classId") or sheet.get("class_id")
    raw_spellcasting = sheet.get("spellcasting")
    if not declared and isinstance(raw_spellcasting, Mapping):
        declared = raw_spellcasting.get("class")
    key = _text(declared).casefold()
    if not key:
        return None
    for entry in _pack_entries("class"):
        names = {
            _text(entry.get("id")).casefold(),
            _text(entry.get("name")).casefold(),
            _display_name(entry).casefold(),
        }
        if key in names:
            return entry
    return None


def _spellcasting_raw(sheet: Mapping[str, Any]) -> dict[str, Any]:
    raw = sheet.get("spellcasting")
    return dict(raw) if isinstance(raw, Mapping) else {}


def spellcasting_profile(sheet: Mapping[str, Any]) -> dict[str, Any] | None:
    """返回 ``{ability, spellSaveDC, spellAttackBonus, class}``；非施法者返回 ``None``。"""
    raw = _spellcasting_raw(sheet)
    entry = _class_entry(sheet)
    class_spellcasting = entry.get("spellcasting") if entry else None
    class_spellcasting = class_spellcasting if isinstance(class_spellcasting, Mapping) else {}
    ability = _text(raw.get("ability") or class_spellcasting.get("ability")).casefold()
    if not ability:
        return None
    bonus = proficiency_bonus(sheet.get("level", 1)) + ability_modifier(ability_score(sheet, ability))
    return {
        "ability": ability,
        "spellSaveDC": 8 + bonus,
        "spellAttackBonus": bonus,
        "class": _text(entry.get("id")) if entry else _text(raw.get("class")),
    }


def _slot_table_row(sheet: Mapping[str, Any]) -> list[int]:
    entry = _class_entry(sheet)
    spellcasting = entry.get("spellcasting") if entry else None
    table = spellcasting.get("slot_table") if isinstance(spellcasting, Mapping) else None
    if not isinstance(table, list) or not table:
        return []
    level = max(1, min(20, _as_int(sheet.get("level"), 1)))
    row = table[level - 1] if level - 1 < len(table) else table[-1]
    if not isinstance(row, list):
        return []
    return [_as_int(item, 0) for item in row]


def spell_slots_from_sheet(sheet: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """按职业表生成 ``{"1": {"max", "used"}}``；卡面显式 ``slots`` 优先。"""
    raw = _spellcasting_raw(sheet)
    saved = raw.get("slots")
    saved = saved if isinstance(saved, Mapping) else {}
    table = _slot_table_row(sheet)
    slots: dict[str, dict[str, int]] = {}
    for index in range(9):
        key = str(index + 1)
        declared = saved.get(key) if isinstance(saved.get(key), Mapping) else {}
        maximum = _as_int(declared.get("max"), table[index] if index < len(table) else 0)
        if maximum <= 0:
            continue
        used = max(0, min(maximum, _as_int(declared.get("used"), 0)))
        slots[key] = {"max": maximum, "used": used}
    return slots


def _name_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [_text(item) for item in value if _text(item)]


def _spell_lists(combatant: Mapping[str, Any], card: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    sheet = sheet_of(card) if isinstance(card, Mapping) else {}
    raw = _spellcasting_raw(sheet)
    known = _name_list(
        combatant.get("spells_known")
        or raw.get("known")
        or raw.get("spells_known")
        or sheet.get("spellsKnown")
    )
    prepared = _name_list(
        combatant.get("spells_prepared")
        or raw.get("prepared")
        or raw.get("spells_prepared")
    )
    return known, prepared


def _matches(entries: list[str], entry: Mapping[str, Any]) -> bool:
    names = {
        _text(entry.get("id")).casefold(),
        _text(entry.get("name")).casefold(),
        _spell_label(entry).casefold(),
    }
    return bool({item.casefold() for item in entries} & {item for item in names if item})


def prepare_spellcasting(combatant: dict[str, Any], sheet: Mapping[str, Any]) -> None:
    """开战时把施法档案、法术位与已知/准备列表挂到战斗者上。"""
    profile = spellcasting_profile(sheet)
    if profile is None:
        return
    combatant["spellcasting"] = profile
    if not isinstance(combatant.get("spell_slots"), dict) or not combatant["spell_slots"]:
        combatant["spell_slots"] = spell_slots_from_sheet(sheet)
    raw = _spellcasting_raw(sheet)
    combatant["spells_known"] = _name_list(
        raw.get("known") or raw.get("spells_known") or sheet.get("spellsKnown")
    )
    combatant["spells_prepared"] = _name_list(raw.get("prepared") or raw.get("spells_prepared"))


def persist_spellcasting(card: dict[str, Any], combatant: Mapping[str, Any]) -> None:
    """把战斗中的法术位写回卡面 ``sheet.spellcasting.slots``。"""
    slots = combatant.get("spell_slots")
    if not isinstance(slots, Mapping):
        return
    sheet = card.get("sheet") if isinstance(card.get("sheet"), Mapping) else {}
    sheet = dict(sheet)
    profile = sheet.get("spellcasting")
    profile = dict(profile) if isinstance(profile, Mapping) else {}
    profile["slots"] = {str(key): dict(value) for key, value in slots.items() if isinstance(value, Mapping)}
    sheet["spellcasting"] = profile
    card["sheet"] = sheet


class Dnd5eSpells:
    """5e 法术结算；核心通过 ``spell.cast`` / ``spell.list`` 工具间接调用。"""

    ruleset_id = "dnd5e"

    def __init__(self, *, combat: Any = None, checks: Any = None, effects: Any = None) -> None:
        self.combat = combat
        self._checks = checks
        self.effects = effects or Dnd5eEffects()

    @property
    def checks(self) -> Any:
        if self._checks is None:
            from dnd5e_plugin.checks import Dnd5eChecks

            self._checks = Dnd5eChecks()
        return self._checks

    # -- 列表与可用性 -----------------------------------------------------
    def list_spells(self, combatant: Mapping[str, Any], card: Mapping[str, Any] | None = None) -> dict[str, Any]:
        sheet = sheet_of(card) if isinstance(card, Mapping) else {}
        profile = combatant.get("spellcasting") if isinstance(combatant.get("spellcasting"), Mapping) else spellcasting_profile(sheet)
        slots = combatant.get("spell_slots") if isinstance(combatant.get("spell_slots"), Mapping) else spell_slots_from_sheet(sheet)
        known, prepared = _spell_lists(combatant, card)
        class_entry = _class_entry(sheet)
        class_id = _text(class_entry.get("id")) if class_entry else ""
        available: list[dict[str, Any]] = []
        fallback = not known and not prepared
        for entry in _pack_entries("spell"):
            if not self._is_available(entry, known, prepared, class_id, fallback):
                continue
            level = _as_int(entry.get("level"), 0)
            slot_row = slots.get(str(level)) if level else None
            remaining = (
                max(0, _as_int(slot_row.get("max"), 0) - _as_int(slot_row.get("used"), 0))
                if isinstance(slot_row, Mapping)
                else 0
            )
            available.append(
                {
                    "id": _text(entry.get("id")),
                    "name": _spell_label(entry),
                    "level": level,
                    "school": _text(entry.get("school")),
                    "casting_time": _text(entry.get("casting_time")),
                    "range": _text(entry.get("range")),
                    "concentration": bool(entry.get("concentration")),
                    "ritual": bool(entry.get("ritual")),
                    "prepared": not prepared or _matches(prepared, entry),
                    "known": not known or _matches(known, entry),
                    "castable": level == 0 or remaining > 0,
                }
            )
        return {
            "class": class_id,
            "ability": _text((profile or {}).get("ability")),
            "spell_save_dc": _as_int((profile or {}).get("spellSaveDC"), 0) or None,
            "spell_attack_bonus": _as_int((profile or {}).get("spellAttackBonus"), 0) or None,
            "slots": {
                str(level): {
                    "max": _as_int(row.get("max"), 0),
                    "used": _as_int(row.get("used"), 0),
                    "remaining": max(0, _as_int(row.get("max"), 0) - _as_int(row.get("used"), 0)),
                }
                for level, row in slots.items()
                if isinstance(row, Mapping)
            },
            "fallback": fallback,
            "spells": available,
        }

    def _is_available(
        self,
        entry: Mapping[str, Any],
        known: list[str],
        prepared: list[str],
        class_id: str,
        fallback: bool,
    ) -> bool:
        if prepared:
            return _matches(prepared, entry)
        if known:
            return _matches(known, entry)
        if fallback:
            classes = {_text(item).casefold() for item in entry.get("classes") or []}
            return not classes or not class_id or class_id.casefold() in classes
        return False

    def required_slot(self, spell: Any) -> str:
        entry = spell if isinstance(spell, Mapping) else _pack_entry("spell", spell)
        casting_time = _text((entry or {}).get("casting_time")).casefold()
        if "bonus action" in casting_time:
            return "bonus_action"
        if "reaction" in casting_time:
            return "reaction"
        return "action"

    def target_mode(self, spell: Any) -> str:
        """返回 ``self`` / ``target`` / ``none``，供非战斗施法选择目标。"""
        entry = spell if isinstance(spell, Mapping) else _pack_entry("spell", spell)
        if not isinstance(entry, Mapping):
            return "target"
        area = entry.get("area")
        if area:
            return "target"
        if entry.get("attack") or entry.get("save") or entry.get("damage"):
            return "target"
        if _text(entry.get("range")).casefold() in {"self", "自身"}:
            return "self"
        return "none"

    def save_ability(self, spell: Any) -> str:
        """返回法术豁免属性（如 ``dex``）；不需要豁免返回空串。

        核心据此决定是否把豁免拆成待决步骤，从而在策略为 ask 时暂停流程。
        """
        entry = spell if isinstance(spell, Mapping) else _pack_entry("spell", spell)
        save = (entry or {}).get("save") if isinstance(entry, Mapping) else None
        return _text(save.get("ability")) if isinstance(save, Mapping) else ""

    def casting_ability(
        self,
        caster: Mapping[str, Any],
        card: Mapping[str, Any] | None = None,
    ) -> str:
        """返回施法者的施法属性；没有显式配置时按职业与卡面推算。"""
        profile = caster.get("spellcasting") if isinstance(caster.get("spellcasting"), Mapping) else None
        ability = _text((profile or {}).get("ability")).casefold()
        if ability:
            return ability
        computed = spellcasting_profile(sheet_of(card) if isinstance(card, Mapping) else {})
        return _text((computed or {}).get("ability")).casefold()

    # -- 施法 -------------------------------------------------------------
    def cast(
        self,
        caster: dict[str, Any],
        card: Mapping[str, Any] | None = None,
        *,
        spell: Any = "",
        targets: list[dict[str, Any]] | None = None,
        slot_level: int | None = None,
        target_cards: Mapping[str, Mapping[str, Any]] | None = None,
        rng: Callable[[int], int] | None = None,
        combat: Mapping[str, Any] | None = None,
        settings: Any = None,
        noncombat: bool = False,
        ritual: bool = False,
        save_results: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = _pack_entry("spell", spell)
        if entry is None:
            return {"error": f"数据包 dnd5e-srd 中没有法术“{spell}”"}
        spell_id = _text(entry.get("id"))
        label = _spell_label(entry)
        level = max(0, _as_int(entry.get("level"), 0))
        known, prepared = _spell_lists(caster, card)
        class_entry = _class_entry(sheet_of(card) if isinstance(card, Mapping) else {})
        class_id = _text(class_entry.get("id")) if class_entry else ""
        fallback = not known and not prepared
        if not self._is_available(entry, known, prepared, class_id, fallback):
            return {"error": f"{caster.get('name')} 没有准备或学会法术“{label}”"}
        if ritual and not entry.get("ritual"):
            return {"error": f"“{label}”不是仪式法术"}

        use_level = level if level == 0 else max(level, _as_int(slot_level, level))
        if level > 0 and use_level > 9:
            return {"error": "法术位环阶不能超过 9"}
        caster_level = max(1, _as_int(caster.get("level"), 1))
        target_list = [target for target in (targets or []) if isinstance(target, dict)]
        # 目标与射程校验必须在扣法术位、打断旧专注之前完成。
        target_errors = spell_schema.validate_targets(
            entry,
            caster,
            target_list,
            slot_level=use_level,
            caster_level=caster_level,
        )
        if target_errors:
            return {"error": "；".join(target_errors)}
        slot_state: dict[str, int] | None = None
        if level > 0 and not ritual:
            slots = caster.get("spell_slots") if isinstance(caster.get("spell_slots"), Mapping) else {}
            row = slots.get(str(use_level)) if isinstance(slots.get(str(use_level)), Mapping) else None
            if row is None:
                return {"error": f"{caster.get('name')} 没有 {use_level} 环法术位"}
            remaining = _as_int(row.get("max"), 0) - _as_int(row.get("used"), 0)
            if remaining <= 0:
                return {"error": f"{caster.get('name')} 的 {use_level} 环法术位已用完"}
            row["used"] = _as_int(row.get("used"), 0) + 1
            slot_state = {"level": use_level, "remaining": remaining - 1}

        profile = caster.get("spellcasting") if isinstance(caster.get("spellcasting"), Mapping) else None
        if profile is None:
            profile = spellcasting_profile(sheet_of(card) if isinstance(card, Mapping) else {}) or {}
        dc = _as_int(profile.get("spellSaveDC"), 8 + proficiency_bonus(caster.get("level", 1)))
        attack_bonus = _as_int(profile.get("spellAttackBonus"), proficiency_bonus(caster.get("level", 1)))
        upcast = spell_schema.apply_upcast(
            entry, slot_level=use_level, caster_level=caster_level
        )
        notes: list[str] = []
        if entry.get("concentration") and isinstance(caster.get("concentration"), Mapping):
            notes.extend(self.end_concentration(caster, combat=combat, reason="施放新的专注法术"))

        results: dict[str, Any] = {
            "spell": {
                "id": spell_id,
                "name": label,
                "level": level,
                "slot_level": use_level,
                "concentration": bool(entry.get("concentration")),
                "ritual": bool(entry.get("ritual")),
            },
            "descriptor": spell_schema.describe(entry),
            "upcast": upcast,
            "slot": self.required_slot(entry),
            "slot_state": slot_state,
            "ritual": bool(ritual),
            "noncombat": bool(noncombat),
            "attack": None,
            "saves": [],
            "damage": [],
            "healing": [],
            "effects": [],
            "concentration": None,
            "notes": notes,
        }
        if not target_list and spell_schema.range_spec(entry)["type"] != "self":
            results["summary"] = f"{caster.get('name')} 施放{label}，但没有指定目标。"
            return results

        save = entry.get("save") if isinstance(entry.get("save"), Mapping) else None
        attack = entry.get("attack") if isinstance(entry.get("attack"), Mapping) else None
        concentration_refs: list[dict[str, Any]] = []
        for target in target_list:
            target_name = _text(target.get("name"))
            target_card = (target_cards or {}).get(target_name)
            hit = True
            critical = False
            save_outcome = None
            save_success: bool | None = None
            if save:
                ability = _text(save.get("ability")) or "dex"
                decided = (save_results or {}).get(target_name)
                if isinstance(decided, bool):
                    decided = {"success": decided}
                if isinstance(decided, Mapping) and isinstance(decided.get("success"), bool):
                    # 策略为 ask 时，豁免已由玩家/KP 在等待步骤里决定；这里只消费结果。
                    save_success = bool(decided.get("success"))
                    hit = not save_success
                    results["saves"].append(
                        {
                            "target": target_name,
                            "ability": ability,
                            "success": save_success,
                            "auto_fail": False,
                            "total": decided.get("total"),
                            "dc": dc,
                            "summary": _text(decided.get("summary"))
                            or f"{label}豁免：{'成功' if save_success else '失败'}（已决）。",
                            "dice": [
                                int(item)
                                for item in (decided.get("dice") or [])
                                if isinstance(item, (int, float))
                            ],
                            "detail": dict(decided.get("detail") or {}),
                            "presentation": dict(decided.get("presentation") or {}),
                            "degree": _text(decided.get("degree"))
                            or ("success" if save_success else "failure"),
                            "decided": True,
                        }
                    )
                else:
                    auto, disadvantage = save_modifiers(target, ability)
                    save_outcome = self.checks.roll_outcome(
                        target_card if isinstance(target_card, Mapping) else card_of(target),
                        CheckRequest(
                            kind="save",
                            name=f"{ability}豁免",
                            target=dc,
                            extra={
                                "ability": ability,
                                "auto_fail": auto,
                                "disadvantage": disadvantage,
                                "label": f"{label}豁免",
                            },
                        ),
                        rng,
                    )
                    save_success = bool(save_outcome.success)
                    hit = not save_success
                    results["saves"].append(
                        {
                            "target": target_name,
                            "ability": ability,
                            "success": save_success,
                            "auto_fail": auto,
                            "total": save_outcome.total,
                            "dc": save_outcome.target,
                            "summary": save_outcome.presentation.get("summary", ""),
                            # 供骰桌直接消费的 d20 明细；核心只负责转交元数据。
                            "dice": list(save_outcome.dice),
                            "detail": dict(save_outcome.detail),
                            "presentation": dict(save_outcome.presentation),
                            "degree": save_outcome.degree,
                        }
                    )
            elif attack is not None or (
                entry.get("damage") and not save and not entry.get("projectiles")
            ):
                condition_mods = attack_modifiers(
                    caster,
                    target,
                    ranged=_text((attack or {}).get("type")).casefold() != "melee",
                )
                natural_attack = self.checks.roll_outcome(
                    card if isinstance(card, Mapping) else card_of(caster),
                    CheckRequest(
                        kind="attack",
                        name=f"{label}攻击",
                        target=_as_int(target.get("armorClass") or target.get("ac"), 10),
                        modifier=attack_bonus,
                        extra={
                            "label": f"{label}攻击",
                            "advantage": condition_mods["advantage"],
                            "disadvantage": condition_mods["disadvantage"],
                        },
                    ),
                    rng,
                )
                hit = bool(natural_attack.success)
                critical = natural_attack.degree == "critical_success"
                results["attack"] = natural_attack.to_dict()
            if not hit and not (save and save.get("half_on_success")):
                continue
            for row in upcast["damage"]:
                dice = _text(row.get("dice"))
                damage_type = _text(row.get("type")) or "force"
                terms, fixed = _parse_damage(dice)
                amount, _detail = _roll_terms(terms, fixed, rng, critical=critical)
                amount *= max(1, _as_int(upcast.get("projectiles"), 1))
                if save and save_success:
                    amount = amount // 2 if save.get("half_on_success") else 0
                if amount <= 0:
                    continue
                if self.combat is None:
                    results["damage"].append(
                        {"target": target_name, "amount": amount, "type": damage_type, "requested": amount}
                    )
                    continue
                applied = self.combat._apply_damage(
                    target,
                    amount,
                    damage_type,
                    critical=critical,
                    combat=combat,
                    rng=rng,
                )
                applied["target"] = target_name
                applied["dice"] = dice
                results["damage"].append(applied)
            healing = upcast.get("healing")
            if isinstance(healing, Mapping) and healing:
                terms, fixed = _parse_damage(healing.get("dice"))
                healed, _detail = _roll_terms(terms, fixed, rng)
                if healing.get("add_spellcasting_modifier"):
                    healed += ability_modifier(ability_score(sheet_of(card) if isinstance(card, Mapping) else {}, _text(profile.get("ability")) or "wis"))
                applied = self._apply_healing(target, max(0, healed))
                applied["target"] = target_name
                results["healing"].append(applied)
            condition = _text(entry.get("applies_condition"))
            if condition:
                effect = self._condition_effect(
                    entry,
                    condition,
                    source=f"{caster.get('name')}:{spell_id}",
                    dc=dc,
                    noncombat=noncombat,
                )
                if effect:
                    effect["id"] = f"spell:{spell_id}:{uuid4().hex[:8]}"
                    stored = add_effect(target, effect)
                    results["effects"].append({**stored, "target": target_name})
                    if entry.get("concentration"):
                        concentration_refs.append({"target": target_name, "id": stored.get("id")})
            spell_effects = entry.get("effects")
            if isinstance(spell_effects, Mapping) and spell_effects:
                if entry.get("concentration") or not _is_instantaneous(entry):
                    effect = {
                        "id": f"spell:{spell_id}:{uuid4().hex[:8]}",
                        "kind": "spell_effect",
                        "scope": "character",
                        "source": f"{caster.get('name')}:{spell_id}",
                        "duration": {"unit": "permanent"}
                        if entry.get("concentration")
                        else _duration_of(entry, noncombat=noncombat),
                        "persistence": "removable",
                        "detail": {"label": label, "effects": dict(spell_effects)},
                    }
                    stored = add_effect(target, effect)
                    results["effects"].append({**stored, "target": target_name})
                    if entry.get("concentration"):
                        concentration_refs.append({"target": target_name, "id": stored.get("id")})
            if save and not hit and save_outcome is not None and save_outcome.success:
                notes.append(f"{target_name} 豁免成功（{save_outcome.presentation.get('summary', '')}）")

        if entry.get("concentration"):
            record = {
                "spell": spell_id,
                "name": label,
                "level": use_level,
                "round": _as_int((combat or {}).get("round"), 0),
                "dc": dc,
                "effects": concentration_refs,
            }
            caster["concentration"] = record
            results["concentration"] = record
            notes.append(f"{caster.get('name')} 开始专注{label}。")
        if ritual and noncombat:
            notes.append("仪式施法额外耗时 10 分钟；需要计时时用 scene.advance 推进时间。")
        results["summary"] = self._summary(caster, label, use_level, results)
        return results

    # -- 持续区域 ---------------------------------------------------------
    def trigger_area(
        self,
        area: Mapping[str, Any],
        target: dict[str, Any],
        target_card: Mapping[str, Any] | None = None,
        *,
        rng: Callable[[int], int] | None = None,
        combat: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """结算一次持续区域触发：豁免 → 伤害/减半 → 施加效果。

        “每目标每回合一次”的去重由核心区域实体负责，这里只做单次结算。
        """
        name = _text(target.get("name"))
        results: dict[str, Any] = {"target": name, "save": None, "damage": [], "effects": []}
        save = area.get("save") if isinstance(area.get("save"), Mapping) else None
        success = False
        if save:
            ability = _text(save.get("ability")) or "dex"
            auto, disadvantage = save_modifiers(target, ability)
            dc = max(1, _as_int(save.get("dc"), _as_int(area.get("dc"), 10)))
            outcome = self.checks.roll_outcome(
                target_card if isinstance(target_card, Mapping) else card_of(target),
                CheckRequest(
                    kind="save",
                    name=f"{ability}豁免",
                    target=dc,
                    extra={
                        "ability": ability,
                        "auto_fail": auto,
                        "disadvantage": disadvantage,
                        "label": f"{_text(area.get('name')) or '区域'}豁免",
                    },
                ),
                rng,
            )
            success = bool(outcome.success)
            results["save"] = {
                "ability": ability,
                "success": success,
                "total": outcome.total,
                "dc": outcome.target,
                "summary": outcome.presentation.get("summary", ""),
                "dice": list(outcome.dice),
                "detail": dict(outcome.detail),
                "presentation": dict(outcome.presentation),
                "degree": outcome.degree,
            }
        if success and not (save and save.get("half_on_success")):
            return results
        for row in area.get("damage") or []:
            if not isinstance(row, Mapping):
                continue
            dice = _text(row.get("dice"))
            damage_type = _text(row.get("type")) or "force"
            terms, fixed = _parse_damage(dice)
            amount, _detail = _roll_terms(terms, fixed, rng)
            if success:
                amount //= 2
            if amount <= 0:
                continue
            if self.combat is None:
                results["damage"].append(
                    {"target": name, "amount": amount, "type": damage_type, "requested": amount}
                )
                continue
            applied = self.combat._apply_damage(
                target, amount, damage_type, combat=combat, rng=rng
            )
            applied["target"] = name
            applied["dice"] = dice
            results["damage"].append(applied)
        effects = area.get("effects")
        if isinstance(effects, Mapping) and effects:
            effect = {
                "id": f"area:{_text(area.get('id'))}:{uuid4().hex[:8]}",
                "kind": "spell_effect",
                "scope": "character",
                "source": _text(area.get("source")) or _text(area.get("name")),
                "duration": {"unit": "round", "remaining": 1},
                "persistence": "removable",
                "detail": {"label": _text(area.get("name")), "effects": dict(effects)},
            }
            stored = add_effect(target, effect)
            results["effects"].append({**stored, "target": name})
        return results

    # -- 专注 -------------------------------------------------------------
    def end_concentration(
        self,
        caster: dict[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> list[str]:
        record = caster.get("concentration")
        if not isinstance(record, Mapping):
            caster.pop("concentration", None)
            return []
        combatants = combat.get("combatants") if isinstance(combat, Mapping) else None
        combatants = combatants if isinstance(combatants, Mapping) else {}
        for ref in record.get("effects") or []:
            if not isinstance(ref, Mapping):
                continue
            target = combatants.get(_text(ref.get("target")))
            if isinstance(target, dict):
                remove_effect(target, effect_id=_text(ref.get("id")))
        caster.pop("concentration", None)
        suffix = f"（{reason}）" if reason else ""
        return [f"{caster.get('name')} 的专注结束{suffix}：{record.get('name') or record.get('spell')}。"]

    def concentration_check(
        self,
        combatant: dict[str, Any],
        damage: int,
        *,
        combat: Mapping[str, Any] | None = None,
        rng: Callable[[int], int] | None = None,
    ) -> list[str]:
        """受伤后的专注检定：DC = max(10, 伤害 // 2)，走 E0 豁免壳。"""
        record = combatant.get("concentration")
        if not isinstance(record, Mapping):
            return []
        if combatant.get("dead") or combatant.get("unconscious"):
            return self.end_concentration(combatant, combat=combat, reason="失去意识")
        if damage <= 0:
            return []
        dc = max(10, int(damage) // 2)
        outcome = self.checks.roll_outcome(
            card_of(combatant),
            CheckRequest(
                kind="save",
                name="体质豁免",
                target=dc,
                extra={"ability": "con", "label": "专注检定"},
            ),
            rng,
        )
        summary = str(outcome.presentation.get("summary") or "")
        if outcome.success:
            return [f"{combatant.get('name')} 专注检定成功（{summary}）"]
        notes = self.end_concentration(combatant, combat=combat, reason="专注检定失败")
        return [f"{combatant.get('name')} 专注被打断（{summary}）", *notes]

    # -- 内部工具 ---------------------------------------------------------
    def _condition_effect(
        self,
        entry: Mapping[str, Any],
        condition: str,
        *,
        source: str,
        dc: int,
        noncombat: bool = False,
    ) -> dict[str, Any] | None:
        duration = {"unit": "permanent"}
        save_ends = None
        if not entry.get("concentration"):
            duration = _duration_of(entry, noncombat=noncombat)
        save = entry.get("save") if isinstance(entry.get("save"), Mapping) else None
        if save and entry.get("concentration"):
            save_ends = {
                "ability": _text(save.get("ability")) or "wis",
                "dc": dc,
                "timing": "turn_end",
            }
        return build_condition(condition, source=source, duration=duration, dc=dc, save_ends=save_ends)

    @staticmethod
    def _apply_healing(target: dict[str, Any], amount: int) -> dict[str, Any]:
        before = max(0, _as_int(target.get("hp"), 0))
        max_hp = max(1, _as_int(target.get("max_hp"), 1))
        after = min(max_hp, before + max(0, amount))
        target["hp"] = after
        if after > 0:
            if target.get("dying"):
                target["dying"] = False
                target["stable"] = True
            if _text(target.get("status")).casefold() in {"dying", "unconscious", "stable"}:
                target["status"] = "active"
        return {"hp_before": before, "hp_after": after, "healed": after - before}

    def _summary(
        self,
        caster: Mapping[str, Any],
        label: str,
        slot_level: int,
        results: Mapping[str, Any],
    ) -> str:
        parts = [f"{caster.get('name')} 施放{label}"]
        if results.get("ritual"):
            parts.append("（仪式，不消耗法术位）")
        else:
            parts.append(f"（{slot_level} 环）")
        if results.get("damage"):
            for item in results["damage"]:
                parts.append(
                    f"{item.get('target')} 受到 {item.get('effective', item.get('amount', 0))} 点"
                    f"{item.get('damage_type') or item.get('type') or ''}伤害"
                    f"（HP {item.get('hp_before')}→{item.get('hp_after')}）"
                )
        if results.get("healing"):
            for item in results["healing"]:
                parts.append(f"{item.get('target')} 恢复 {item.get('healed', 0)} HP")
        if results.get("effects"):
            for item in results["effects"]:
                detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
                parts.append(f"{item.get('target')} 获得{detail.get('label') or '效果'}")
        if results.get("concentration"):
            parts.append("专注中")
        return "；".join(parts) + "。"


def _duration_of(entry: Mapping[str, Any], *, noncombat: bool = False) -> dict[str, Any]:
    return spell_schema.effect_duration(
        spell_schema.duration_spec(entry), noncombat=noncombat
    )


def _is_instantaneous(entry: Mapping[str, Any]) -> bool:
    return spell_schema.duration_spec(entry)["unit"] == "instant"


__all__ = [
    "Dnd5eSpells",
    "persist_spellcasting",
    "prepare_spellcasting",
    "spell_slots_from_sheet",
    "spellcasting_profile",
]
