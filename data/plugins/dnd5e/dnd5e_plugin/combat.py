"""D&D 5e（SRD 5.1）战斗结算。

本模块只放 5e 的规则数学：先攻、攻击 vs AC、伤害骰与加值、暴击、抗性/易伤/
免疫、临时 HP、死亡豁免与行动经济。核心只通过 ``CombatRuleset`` 的可选钩子
调用这里，不解释命中、AC 或伤害类型。

武器、护甲、怪物数据全部来自 ``dnd5e-srd`` 数据包；插件不另存一份数值。
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable, Mapping

from dnd5e_plugin.effects import (
    attack_modifiers,
    effective_speed,
)
from dnd5e_plugin.rules import (
    ability_modifier,
    ability_score,
    proficiency_bonus,
    sheet_of,
)
from trpg_server.combat.contracts import DamageOutcome, RoundEndOutcome
from trpg_server.ruleset.resources import ResourceDelta, apply_delta

DEFAULT_UNARMED = {
    "id": "unarmed",
    "name": "徒手",
    "kind": "melee",
    "damage_dice": "1",
    "damage_type": "bludgeoning",
    "properties": [],
    "attack_bonus": 0,
    "damage_bonus": 0,
}

_DICE_RE = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)
_FLAT_RE = re.compile(r"^\s*([+-]?\d+)\s*$")


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _roll_die(rng: Any, sides: int) -> int:
    """兼容 ``Callable[[sides], int]`` 与 ``random`` 模块两种随机源。"""
    sides = max(1, int(sides))
    if callable(rng):
        try:
            return max(1, min(sides, int(rng(sides))))
        except TypeError:
            pass
    randint = getattr(rng, "randint", None)
    if callable(randint):
        return max(1, min(sides, int(randint(1, sides))))
    return random.randint(1, sides)


def _normalized_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {_text(item).casefold() for item in value if _text(item)}


def _display_name(entry: Mapping[str, Any]) -> str:
    locales = entry.get("locales")
    if isinstance(locales, Mapping):
        for key in ("zh-CN", "zh", "en-US", "en"):
            value = locales.get(key)
            if isinstance(value, Mapping) and _text(value.get("name")):
                return _text(value.get("name"))
    return _text(entry.get("name"))


def _library() -> Any:
    from trpg_server.plugins import runtime

    return runtime.content_packs_for("dnd5e")


def _pack_entries(type_name: str) -> list[dict[str, Any]]:
    try:
        return list(_library().entries("dnd5e", type_name) or [])
    except Exception:  # noqa: BLE001 - 数据包未启用时返回空目录
        return []


def _pack_entry(type_name: str, identifier: Any) -> dict[str, Any] | None:
    key = _text(identifier).casefold()
    if not key:
        return None
    try:
        found = _library().get("dnd5e", type_name, key)
        if isinstance(found, dict):
            return found
    except Exception:  # noqa: BLE001
        pass
    for entry in _pack_entries(type_name):
        names = {
            _text(entry.get("id")).casefold(),
            _text(entry.get("name")).casefold(),
            _display_name(entry).casefold(),
        }
        if key in names:
            return entry
    return None


def _parse_damage(expression: Any) -> tuple[list[tuple[int, int]], int]:
    """把 ``1d8`` / ``2d6+3`` / ``1`` 解析成 ``([(count, sides)], fixed)``。"""
    text = _text(expression)
    if not text:
        return [(1, 4)], 0
    flat = _FLAT_RE.match(text)
    if flat:
        return [], max(0, int(flat.group(1)))
    match = _DICE_RE.match(text)
    if not match:
        return [(1, 4)], 0
    count = int(match.group(1) or 1)
    sides = int(match.group(2) or 4)
    fixed = int((match.group(3) or "0").replace(" ", ""))
    return [(max(0, count), max(1, sides))], fixed


def _roll_terms(
    terms: list[tuple[int, int]], fixed: int, rng: Any, *, critical: bool = False
) -> tuple[int, list[dict[str, Any]]]:
    total = max(0, int(fixed))
    detail: list[dict[str, Any]] = []
    for count, sides in terms:
        actual = max(0, int(count)) * (2 if critical else 1)
        rolls = [_roll_die(rng, sides) for _ in range(actual)]
        subtotal = sum(rolls)
        total += subtotal
        detail.append({"count": actual, "sides": sides, "rolls": rolls, "total": subtotal})
    return total, detail


def _equipment_items(sheet: Mapping[str, Any]) -> list[tuple[str, bool]]:
    raw = sheet.get("equipment")
    if isinstance(raw, Mapping):
        raw = raw.get("items")
    items: list[tuple[str, bool]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            items.append((item, True))
        elif isinstance(item, Mapping):
            identifier = _text(item.get("id") or item.get("item_id") or item.get("name"))
            equipped = item.get("equipped", item.get("worn", item.get("equip", True)))
            if identifier:
                items.append((identifier, bool(equipped)))
    return items


def _equipment_rows(sheet: Mapping[str, Any], category: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for identifier, equipped in _equipment_items(sheet):
        if not equipped:
            continue
        row = _pack_entry("equipment", identifier)
        if row is None:
            continue
        if category and _text(row.get("category")).casefold() != category:
            continue
        rows.append(row)
    return rows


def armor_class_from_equipment(sheet: Mapping[str, Any]) -> int:
    """按已装备的护甲/盾牌计算 AC；没有护甲条目时退回卡面显式 AC。"""
    dex_mod = ability_modifier(ability_score(sheet, "dex"))
    armors = _equipment_rows(sheet, "armor")
    body: dict[str, Any] | None = None
    shields = 0
    for row in armors:
        if _text(row.get("armor_category")).casefold() == "shield":
            shields += max(0, _as_int(row.get("ac_bonus"), 0))
            continue
        if body is None or _as_int(row.get("base_ac"), 0) > _as_int(body.get("base_ac"), 0):
            body = row
    if body is None:
        explicit = sheet.get("armorClass")
        if explicit not in (None, ""):
            return max(0, _as_int(explicit, 10) + shields)
        return max(0, 10 + dex_mod + shields)
    base = _as_int(body.get("base_ac"), 10)
    if bool(body.get("dex_bonus", True)):
        cap = body.get("max_dex_bonus")
        dex_part = dex_mod if cap is None else min(dex_mod, _as_int(cap, dex_mod))
        base += dex_part
    base += max(0, _as_int(body.get("ac_bonus"), 0))
    return max(0, base + shields)


def _proficient_with_weapon(sheet: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    profs = sheet.get("proficiencies")
    profs = profs if isinstance(profs, Mapping) else {}
    declared = _normalized_set(profs.get("weapons"))
    if not declared:
        # 卡面没有声明武器熟练时按“已熟练”处理，避免简化卡永远 +0。
        return True
    candidates = {
        _text(row.get("id")).casefold(),
        _text(row.get("name")).casefold(),
        _display_name(row).casefold(),
        _text(row.get("weapon_category")).casefold(),
        _text(row.get("category")).casefold(),
        "simple" if _text(row.get("weapon_category")).casefold() == "simple" else "",
        "martial" if _text(row.get("weapon_category")).casefold() == "martial" else "",
    }
    return bool(declared & {item for item in candidates if item})


def _weapon_attack(row: Mapping[str, Any], sheet: Mapping[str, Any]) -> dict[str, Any]:
    properties = [_text(item).casefold() for item in row.get("properties") or []]
    kind = _text(row.get("kind")).casefold() or ("ranged" if "ammunition" in properties else "melee")
    ability = "dex" if ("finesse" in properties or kind == "ranged") else "str"
    modifier = ability_modifier(ability_score(sheet, ability))
    level = sheet.get("level", 1)
    bonus = modifier + (proficiency_bonus(level) if _proficient_with_weapon(sheet, row) else 0)
    damage = row.get("damage") if isinstance(row.get("damage"), Mapping) else {}
    return {
        "id": _text(row.get("id")),
        "name": _display_name(row) or _text(row.get("name")),
        "kind": kind,
        "ability": ability,
        "attack_bonus": bonus,
        "damage_bonus": modifier,
        "damage_dice": _text(damage.get("dice") or "1d4"),
        "damage_type": _text(damage.get("type") or "bludgeoning"),
        "versatile_dice": _text(row.get("versatile_damage")),
        "properties": properties,
    }


def _player_attacks(sheet: Mapping[str, Any]) -> list[dict[str, Any]]:
    attacks = [
        _weapon_attack(row, sheet)
        for row in _equipment_rows(sheet, "weapon")
        if row.get("category") == "weapon"
    ]
    return attacks or [dict(DEFAULT_UNARMED)]


def _monster_attacks(monster: Mapping[str, Any]) -> list[dict[str, Any]]:
    attacks: list[dict[str, Any]] = []
    for action in monster.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        action_type = _text(action.get("type")).casefold()
        if "weapon" not in action_type and action_type not in {"melee", "ranged"}:
            continue
        damage_rows = action.get("damage") if isinstance(action.get("damage"), list) else []
        damage = damage_rows[0] if damage_rows and isinstance(damage_rows[0], Mapping) else {}
        attacks.append(
            {
                "id": _text(action.get("name")),
                "name": _text(action.get("name")),
                "kind": "ranged" if "ranged" in action_type and "melee" not in action_type else "melee",
                "attack_bonus": _as_int(action.get("attack_bonus"), 0),
                "damage_bonus": 0,
                "damage_dice": _text(damage.get("dice") or "1d4"),
                "damage_type": _text(damage.get("type") or "bludgeoning"),
                "versatile_dice": "",
                "properties": [],
            }
        )
    return attacks


class Dnd5eCombatRules:
    """5e 战斗实现；核心按 ``attack_mode="d20"`` 委托整次攻击。"""

    ruleset_id = "dnd5e"
    display_name = "D&D 5e（SRD 5.1）"
    attack_mode = "d20"

    # -- 先攻与行动经济 ---------------------------------------------------
    def roll_initiative(self, combatant: Mapping[str, Any], rng: Any, settings: Any) -> int:
        return _roll_die(rng, 20) + self._dex_mod(combatant)

    def initiative_tiebreak(self, combatant: Mapping[str, Any]) -> int:
        return self._dex_mod(combatant)

    def actions_per_round(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def reaction_limit(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def action_budget(self, combatant: Mapping[str, Any], settings: Any) -> dict[str, int]:
        speed = effective_speed(combatant, _as_int(combatant.get("speed_ft"), 30))
        return {
            "action": 1,
            "bonus_action": 1,
            "reaction": 1,
            "movement": speed,
        }

    def is_incapacitated(self, combatant: Mapping[str, Any]) -> bool:
        if combatant.get("dead"):
            return True
        if combatant.get("dying"):
            # 0 HP 未稳定者仍要在自己回合开始掷死亡豁免，不能跳过。
            return False
        return _text(combatant.get("status")).casefold() in {"unconscious", "stable"}

    # -- 开战准备 ---------------------------------------------------------
    def prepare_combatant(
        self,
        combatant: dict[str, Any],
        *,
        card: Mapping[str, Any] | None = None,
        settings: Any = None,
    ) -> None:
        if combatant.get("is_player"):
            self._prepare_player(combatant, card)
            return
        monster_id = _text(combatant.get("monster_id"))
        if monster_id:
            monster = _pack_entry("monster", monster_id)
            if monster is None:
                raise ValueError(f"数据包 dnd5e-srd 中没有怪物“{monster_id}”")
            self._apply_monster(combatant, monster)
            return
        combatant.setdefault("abilities", self._ability_scores_from_flat(combatant))
        combatant.setdefault("armorClass", _as_int(combatant.get("ac"), 10 + self._dex_mod(combatant)))
        combatant.setdefault("attacks", [dict(DEFAULT_UNARMED)])
        combatant.setdefault("speed_ft", 30)
        combatant.setdefault("temp_hp", 0)

    def _prepare_player(self, combatant: dict[str, Any], card: Any) -> None:
        sheet = sheet_of(card) if isinstance(card, Mapping) else {}
        abilities = self._normalize_abilities(sheet.get("abilities") or sheet.get("attributes"))
        combatant["abilities"] = abilities
        combatant["level"] = max(1, min(20, _as_int(sheet.get("level"), 1)))
        profs = sheet.get("proficiencies")
        combatant["proficiencies"] = dict(profs) if isinstance(profs, Mapping) else {}
        combatant["dex"] = abilities["dex"]
        combatant["str"] = abilities["str"]
        combatant["con"] = abilities["con"]

        hp = sheet.get("hp") if isinstance(sheet.get("hp"), Mapping) else {}
        max_hp = max(
            1,
            _as_int(hp.get("max"), _as_int(sheet.get("maxHp"), _as_int(combatant.get("max_hp"), 1))),
        )
        current = max(0, min(max_hp, _as_int(hp.get("current"), _as_int(combatant.get("hp"), max_hp))))
        combatant["max_hp"] = max_hp
        combatant["hp"] = current
        combatant["temp_hp"] = max(0, _as_int(hp.get("temp"), 0))
        combatant["armorClass"] = armor_class_from_equipment(sheet)
        combatant["speed_ft"] = max(0, _as_int(sheet.get("speed"), 30))
        combatant["attacks"] = _player_attacks(sheet)
        # 玩家默认 humanoid；角色卡显式声明时以卡为准。
        combatant.setdefault(
            "creature_type",
            _text(sheet.get("creature_type")).casefold() or "humanoid",
        )
        combatant["damage_resistances"] = list(sheet.get("damage_resistances") or [])
        combatant["damage_vulnerabilities"] = list(sheet.get("damage_vulnerabilities") or [])
        combatant["damage_immunities"] = list(sheet.get("damage_immunities") or [])
        from dnd5e_plugin.spells import prepare_spellcasting

        prepare_spellcasting(combatant, sheet)
        if current <= 0:
            combatant["dying"] = True
            combatant["unconscious"] = True
            combatant["status"] = "dying"
            combatant.setdefault("death_saves", {"successes": 0, "failures": 0, "stable": False})

    def _apply_monster(self, combatant: dict[str, Any], monster: Mapping[str, Any]) -> None:
        ac_rows = monster.get("ac") if isinstance(monster.get("ac"), list) else []
        ac = _as_int(ac_rows[0].get("value"), 10) if ac_rows and isinstance(ac_rows[0], Mapping) else 10
        hp = monster.get("hp") if isinstance(monster.get("hp"), Mapping) else {}
        max_hp = max(1, _as_int(hp.get("average"), 1))
        combatant["armorClass"] = ac
        combatant["max_hp"] = max_hp
        combatant["hp"] = max_hp
        combatant["temp_hp"] = 0
        abilities = self._normalize_abilities(monster.get("abilities"))
        combatant["abilities"] = abilities
        combatant["dex"] = abilities["dex"]
        combatant["str"] = abilities["str"]
        combatant["con"] = abilities["con"]
        speed = monster.get("speed") if isinstance(monster.get("speed"), Mapping) else {}
        combatant["speed_ft"] = max(0, _as_int(speed.get("walk"), 30))
        combatant["attacks"] = _monster_attacks(monster)
        combatant["monster_name"] = _display_name(monster)
        # 目标限制（如只有 humanoid 生效）需要生物类型；SRD 的 type 是权威来源。
        creature_type = _text(monster.get("type")).casefold()
        if creature_type:
            combatant["creature_type"] = creature_type
        combatant["damage_resistances"] = list(monster.get("damage_resistances") or [])
        combatant["damage_vulnerabilities"] = list(monster.get("damage_vulnerabilities") or [])
        combatant["damage_immunities"] = list(monster.get("damage_immunities") or [])

    # -- 攻击结算 ---------------------------------------------------------
    def resolve_attack_action(
        self,
        attacker: dict[str, Any],
        target: dict[str, Any],
        action: Mapping[str, Any],
        *,
        attacker_card: Mapping[str, Any] | None = None,
        rng: Callable[[int], int] | None = None,
        settings: Any = None,
        combat: Mapping[str, Any] | None = None,
        ac_bonus: int = 0,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        slot = _text(action.get("slot") or action.get("action_slot") or "action").lower()
        if slot not in {"action", "bonus_action", "reaction"}:
            return {"error": f"d20 攻击不支持行动槽“{slot}”"}
        attack = self._select_attack(attacker, action.get("weapon"), attacker_card)
        if attack is None:
            return {"error": f"{attacker.get('name')} 没有可用的攻击"}

        condition_mods = attack_modifiers(
            attacker,
            target,
            ranged=_text(attack.get("kind")).casefold() == "ranged",
        )
        preset_rolls = action.get("attack_rolls")
        if isinstance(preset_rolls, list) and preset_rolls:
            rolls = [max(1, min(20, _as_int(item, 10))) for item in preset_rolls[:2]]
            natural = _as_int(action.get("attack_roll"), rolls[0])
            natural = max(1, min(20, natural)) if natural else rolls[0]
        else:
            attack_roll = action.get("attack_roll")
            rolls = []
            if attack_roll not in (None, ""):
                natural = max(1, min(20, _as_int(attack_roll, 10)))
                rolls = [natural]
            else:
                natural = _roll_die(rng, 20)
                rolls = [natural]
        advantage = bool(action.get("advantage") or action.get("adv")) or condition_mods["advantage"]
        disadvantage = (
            bool(action.get("disadvantage") or action.get("dis")) or condition_mods["disadvantage"]
        )
        if advantage and disadvantage:
            advantage = disadvantage = False
        if not isinstance(preset_rolls, list) or not preset_rolls:
            if advantage or disadvantage:
                second = _roll_die(rng, 20)
                rolls.append(second)
                natural = max(rolls) if advantage else min(rolls)

        target_ac = _as_int(target.get("armorClass") or target.get("ac"), 10)
        cover = _text(action.get("cover")).lower()
        if cover in {"half", "半", "half_cover"}:
            target_ac += 2
        elif cover in {"three_quarters", "3/4", "四分之三"}:
            target_ac += 5
        elif cover in {"full", "全", "full_cover"}:
            return self._attack_result(
                attacker,
                target,
                attack,
                natural,
                rolls,
                0,
                target_ac,
                False,
                False,
                None,
                "目标处于全掩体，不能被直接攻击。",
                advantage=advantage,
                disadvantage=disadvantage,
            )
        target_ac += max(0, _as_int(ac_bonus, 0))

        attack_bonus = _as_int(attack.get("attack_bonus"), 0)
        total = natural + attack_bonus
        critical = natural == 20
        hit = critical or (natural != 1 and total >= target_ac)
        if hit and condition_mods["auto_crit"]:
            critical = True

        damage_result = None
        if hit and not dry_run:
            terms, fixed = _parse_damage(attack.get("damage_dice"))
            if action.get("two_handed") and attack.get("versatile_dice"):
                terms, fixed = _parse_damage(attack.get("versatile_dice"))
            rolled, detail = _roll_terms(terms, fixed, rng, critical=critical)
            amount = max(0, rolled + _as_int(attack.get("damage_bonus"), 0))
            damage_result = self._apply_damage(
                target,
                amount,
                _text(attack.get("damage_type")),
                critical=critical,
                combat=combat,
                rng=rng,
            )
            damage_result["damage_detail"] = "，".join(
                f"{item['count']}d{item['sides']}={item['rolls']}" for item in detail
            )
        summary = self._summary(
            attacker, target, attack, natural, rolls, attack_bonus, total, target_ac, hit, critical, damage_result
        )
        return self._attack_result(
            attacker,
            target,
            attack,
            natural,
            rolls,
            attack_bonus,
            target_ac,
            hit,
            critical,
            damage_result,
            summary,
            advantage=advantage,
            disadvantage=disadvantage,
            ac_bonus=ac_bonus,
        )

    def plan_attack_action(
        self,
        attacker: dict[str, Any],
        target: dict[str, Any],
        action: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """只做命中判定、不落伤害，供核心在伤害前插入反应窗口。"""
        return self.resolve_attack_action(
            attacker, target, action, dry_run=True, **kwargs
        )

    def _select_attack(
        self,
        attacker: Mapping[str, Any],
        weapon: Any,
        card: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        attacks = attacker.get("attacks") if isinstance(attacker.get("attacks"), list) else []
        if not attacks and attacker.get("is_player") and isinstance(card, Mapping):
            attacks = _player_attacks(sheet_of(card))
        if not attacks:
            return None
        key = _text(weapon).casefold()
        if not key:
            return attacks[0]
        for attack in attacks:
            if isinstance(attack, Mapping) and key in {
                _text(attack.get("id")).casefold(),
                _text(attack.get("name")).casefold(),
            }:
                return dict(attack)
        for attack in attacks:
            if isinstance(attack, Mapping) and key in _text(attack.get("name")).casefold():
                return dict(attack)
        return None

    def _apply_damage(
        self,
        target: dict[str, Any],
        amount: int,
        damage_type: str,
        *,
        critical: bool = False,
        combat: Mapping[str, Any] | None = None,
        rng: Any = None,
    ) -> dict[str, Any]:
        amount = max(0, int(amount))
        dtype = _text(damage_type).casefold()
        immune = dtype in _normalized_set(target.get("damage_immunities"))
        vulnerable = dtype in _normalized_set(target.get("damage_vulnerabilities"))
        resistant = dtype in _normalized_set(target.get("damage_resistances"))
        if immune:
            effective = 0
        elif vulnerable:
            effective = amount * 2
        elif resistant:
            effective = amount // 2
        else:
            effective = amount

        before = max(0, _as_int(target.get("hp"), 0))
        max_hp = max(1, _as_int(target.get("max_hp"), 1))
        temp = max(0, _as_int(target.get("temp_hp"), 0))
        absorbed = min(temp, effective)
        target["temp_hp"] = temp - absorbed
        remaining = max(0, effective - absorbed)
        # 阈值跨越交给 E0 资源壳：HP 归零事件由核心计算，插件只消费事件。
        slot = {
            "kind": "hp",
            "cap_min": 0,
            "cap_max": max_hp,
            "thresholds": [{"at": 0, "when": "below", "event": "zero_hp"}],
        }
        change = apply_delta(slot, before, ResourceDelta(kind="hp", delta=-remaining, reason="damage"))
        after = change.after
        zero_crossed = any(event.event == "zero_hp" for event in change.events)
        target["hp"] = after
        result: dict[str, Any] = {
            "damage": amount,
            "effective": effective,
            "hp_before": before,
            "hp_after": after,
            "temp_hp_absorbed": absorbed,
            "resistance": resistant,
            "vulnerability": vulnerable,
            "immunity": immune,
            "damage_type": dtype,
            "dead": False,
            "dying": bool(target.get("dying")),
            "unconscious": bool(target.get("unconscious")),
            "death_saves": target.get("death_saves"),
            "threshold_events": [event.to_dict() for event in change.events],
            "note": "",
        }
        if target.get("dead"):
            target["hp"] = 0
            result.update(hp_after=0, dead=True, note="目标已经死亡，伤害不再改变状态")
            return result

        if before > 0 and zero_crossed:
            overflow = max(0, remaining - before)
            if overflow >= max_hp:
                target.update({"hp": 0, "dead": True, "dying": False, "unconscious": True, "status": "dead"})
                result.update(dead=True, dying=False, unconscious=True, note="溢出伤害达到生命上限，目标立即死亡")
            else:
                saves = target.get("death_saves")
                if not isinstance(saves, Mapping):
                    saves = {"successes": 0, "failures": 0, "stable": False}
                target["death_saves"] = dict(saves)
                target.update({"dying": True, "unconscious": True, "status": "dying"})
                result.update(dying=True, unconscious=True, death_saves=dict(saves))
        elif before == 0 and remaining > 0:
            failures = 2 if critical else 1
            if target.get("dying") or target.get("stable"):
                saves = target.get("death_saves")
                if not isinstance(saves, Mapping):
                    saves = {"successes": 0, "failures": 0, "stable": False}
                saves = dict(saves)
                saves["failures"] = _as_int(saves.get("failures"), 0) + failures
                saves["stable"] = False
                target["death_saves"] = saves
                target["dying"] = True
                target["stable"] = False
                target["unconscious"] = True
                target["status"] = "dying"
                if saves["failures"] >= 3:
                    target.update({"dead": True, "dying": False, "status": "dead"})
                result.update(
                    dying=not target.get("dead"),
                    unconscious=True,
                    dead=bool(target.get("dead")),
                    death_saves=dict(saves),
                    note="0 HP 时受伤，死亡豁免失败次数增加",
                )
        elif after > 0:
            target["dying"] = False
            target["unconscious"] = False
            if _text(target.get("status")).casefold() in {"dying", "unconscious", "stable"}:
                target["status"] = "active"
        result.update(
            dead=bool(target.get("dead")),
            dying=bool(target.get("dying")),
            unconscious=bool(target.get("unconscious")),
            death_saves=target.get("death_saves"),
        )
        if rng is not None and result.get("effective", 0) > 0:
            result["concentration"] = self._concentration_check(
                target, result["effective"], combat=combat, rng=rng
            )
        elif isinstance(target.get("concentration"), Mapping) and (
            target.get("dead") or target.get("unconscious")
        ):
            # 即使没有随机源（例如固定伤害/脚本结算），死亡与失能也必须打断专注。
            result["concentration"] = self._concentration_check(
                target, 0, combat=combat, rng=None
            )
        return result

    # -- 专注 -------------------------------------------------------------
    def _spells_engine(self) -> Any:
        engine = getattr(self, "spells", None)
        if engine is None:
            from dnd5e_plugin.spells import Dnd5eSpells

            engine = Dnd5eSpells(combat=self)
            self.spells = engine
        return engine

    def _concentration_check(
        self,
        combatant: dict[str, Any],
        damage: int,
        *,
        combat: Mapping[str, Any] | None = None,
        rng: Callable[[int], int] | None = None,
    ) -> list[str]:
        return self._spells_engine().concentration_check(
            combatant, damage, combat=combat, rng=rng
        )

    def end_concentration(
        self,
        combatant: dict[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> list[str]:
        return self._spells_engine().end_concentration(combatant, combat=combat, reason=reason)

    def on_damage_taken(
        self,
        combatant: dict[str, Any],
        outcome: Any,
        *,
        rng: Callable[[int], int] | None = None,
        settings: Any = None,
        combat: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """核心伤害工具受伤后的钩子：专注检定与打断。"""
        notes: list[str] = []
        effective = _as_int(getattr(outcome, "effective", 0), 0)
        if effective > 0:
            notes.extend(self._concentration_check(combatant, effective, combat=combat, rng=rng))
        if (combatant.get("dead") or combatant.get("unconscious")) and isinstance(
            combatant.get("concentration"), Mapping
        ):
            notes.extend(
                self.end_concentration(combatant, combat=combat, reason="失去意识")
            )
        return notes

    def _summary(
        self,
        attacker: Mapping[str, Any],
        target: Mapping[str, Any],
        attack: Mapping[str, Any],
        natural: int,
        rolls: list[int],
        attack_bonus: int,
        total: int,
        target_ac: int,
        hit: bool,
        critical: bool,
        damage: Mapping[str, Any] | None,
    ) -> str:
        parts = [
            f"{attacker.get('name')} 使用{attack.get('name')}攻击 {target.get('name')}",
            f"d20 [{', '.join(str(item) for item in rolls)}] {'+' if attack_bonus >= 0 else '-'} {abs(attack_bonus)} = {total} vs AC {target_ac}",
        ]
        if critical:
            parts.append("自然 20，暴击")
        elif natural == 1:
            parts.append("自然 1，必定失手")
        elif hit:
            parts.append("命中")
        else:
            parts.append("未命中")
        if damage:
            text = (
                f"伤害 {damage.get('effective', 0)}（原伤害 {damage.get('damage', 0)}，{damage.get('damage_type') or '无类型'}）"
                f"，HP {damage.get('hp_before')}→{damage.get('hp_after')}"
            )
            if damage.get("temp_hp_absorbed"):
                text += f"，临时 HP 吸收 {damage['temp_hp_absorbed']}"
            if damage.get("vulnerability"):
                text += "，易伤翻倍"
            elif damage.get("resistance"):
                text += "，抗性减半"
            elif damage.get("immunity"):
                text += "，免疫"
            parts.append(text)
            if damage.get("dead"):
                parts.append("目标死亡")
            elif damage.get("dying"):
                parts.append("目标陷入濒死")
            elif damage.get("unconscious"):
                parts.append("目标昏迷")
            if damage.get("note"):
                parts.append(str(damage["note"]))
            for note in damage.get("concentration") or []:
                parts.append(str(note))
        return "；".join(parts) + "。"

    def _attack_result(
        self,
        attacker: Mapping[str, Any],
        target: Mapping[str, Any],
        attack: Mapping[str, Any],
        natural: int,
        rolls: list[int],
        attack_bonus: int,
        target_ac: int,
        hit: bool,
        critical: bool,
        damage: Mapping[str, Any] | None,
        summary: str,
        *,
        advantage: bool = False,
        disadvantage: bool = False,
        ac_bonus: int = 0,
    ) -> dict[str, Any]:
        return {
            "attack_mode": "d20",
            "attacker": attacker.get("name"),
            "target": target.get("name"),
            "weapon": attack.get("name"),
            "attack_bonus": attack_bonus,
            "expression": "1d20",
            "roll": natural,
            "rolls": list(rolls),
            "natural": natural,
            "advantage": bool(advantage),
            "disadvantage": bool(disadvantage),
            "total": natural + attack_bonus,
            "target_ac": target_ac,
            "ac_bonus": int(ac_bonus),
            "success": bool(hit),
            "success_level": "critical" if critical else ("hit" if hit else "miss"),
            "critical": bool(critical),
            "damage": dict(damage) if isinstance(damage, Mapping) else None,
            "summary": summary,
        }

    # -- 直接伤害 / 轮末 / 资源 ------------------------------------------
    def resolve_damage(
        self,
        combatant: dict[str, Any],
        amount: int,
        *,
        armor: int,
        ignore_armor: bool,
        roll_d100: Any,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> DamageOutcome:
        result = self._apply_damage(combatant, int(amount), _text(combatant.get("damage_type")))
        return DamageOutcome(
            damage=result["damage"],
            armor=0,
            effective=result["effective"],
            hp_before=result["hp_before"],
            hp_after=result["hp_after"],
            major_wound=False,
            dying=bool(result.get("dying")),
            unconscious=bool(result.get("unconscious")),
            dead=bool(result.get("dead")),
            note=str(result.get("note") or ""),
            extra={
                "temp_hp": combatant.get("temp_hp"),
                "death_saves": combatant.get("death_saves"),
                "damage_type": result.get("damage_type"),
            },
        )

    def on_round_end(
        self,
        combatant: dict[str, Any],
        *,
        roll_d100: Any,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> RoundEndOutcome:
        return RoundEndOutcome()

    def start_turn(
        self,
        combatant: dict[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        settings: Any = None,
        rng: Any = None,
    ) -> list[str]:
        notes: list[str] = []
        budget = combatant.get("budget")
        if isinstance(budget, dict) and "reaction" in budget:
            budget["reaction"] = 1
        if isinstance(combat, Mapping) and _as_int(combat.get("round"), 1) == 1:
            surprised = {_text(item).casefold() for item in combat.get("surprised") or []}
            names = {_text(combatant.get("name")).casefold()}
            names.update(_text(item).casefold() for item in combatant.get("aliases") or [])
            if names & surprised:
                if isinstance(budget, dict):
                    for key in ("action", "bonus_action", "movement"):
                        if key in budget:
                            budget[key] = 0
                    if "reaction" in budget:
                        budget["reaction"] = 0
                notes.append(f"{combatant.get('name')} 被突袭，首个回合无法行动或移动。")
        if combatant.get("dying") and _as_int(combatant.get("hp"), 0) <= 0:
            roll = _roll_die(rng, 20)
            saves = combatant.get("death_saves")
            if not isinstance(saves, Mapping):
                saves = {"successes": 0, "failures": 0, "stable": False}
            saves = dict(saves)
            if roll == 20:
                combatant.update(
                    {
                        "hp": 1,
                        "dying": False,
                        "unconscious": False,
                        "stable": False,
                        "status": "active",
                    }
                )
                saves.update(successes=0, failures=0, stable=False)
                notes.append(f"{combatant.get('name')} 死亡豁免自然 20，恢复 1 HP。")
            elif roll == 1:
                saves["failures"] = _as_int(saves.get("failures"), 0) + 2
                notes.append(f"{combatant.get('name')} 死亡豁免自然 1，记两次失败。")
            elif roll >= 10:
                saves["successes"] = _as_int(saves.get("successes"), 0) + 1
                notes.append(f"{combatant.get('name')} 死亡豁免成功（d20={roll}）。")
            else:
                saves["failures"] = _as_int(saves.get("failures"), 0) + 1
                notes.append(f"{combatant.get('name')} 死亡豁免失败（d20={roll}）。")
            if saves.get("successes", 0) >= 3:
                saves["stable"] = True
                combatant.update({"dying": False, "unconscious": True, "status": "unconscious"})
                notes.append(f"{combatant.get('name')} 稳定下来，停止死亡豁免。")
            elif saves.get("failures", 0) >= 3:
                combatant.update({"dead": True, "dying": False, "unconscious": True, "status": "dead"})
                notes.append(f"{combatant.get('name')} 死亡豁免失败三次，死亡。")
            combatant["death_saves"] = saves
        if (combatant.get("dead") or combatant.get("unconscious")) and isinstance(
            combatant.get("concentration"), Mapping
        ):
            notes.extend(self.end_concentration(combatant, combat=combat, reason="失去意识"))
        return notes

    def end_turn(
        self,
        combatant: dict[str, Any],
        *,
        combat: Mapping[str, Any] | None = None,
        settings: Any = None,
        rng: Any = None,
    ) -> list[str]:
        return []

    # -- 其它协议方法 -----------------------------------------------------
    def resolve_melee(self, attack_level: str, defense_level: str, defense_kind: str) -> str:
        # d20 规则集不使用百分制对抗；保留中性实现满足协议。
        return "miss"

    def spell_timing(self, cast_time: str, dex: int) -> dict[str, int]:
        return {"round_offset": 0, "initiative_bonus": 0, "dex": dex}

    def spend_resource(
        self,
        combatant: dict[str, Any],
        resource: str,
        amount: int,
        *,
        roll_d100: Any = None,
    ) -> dict[str, int]:
        key = _text(resource).lower()
        current = max(0, _as_int(combatant.get(key), 0))
        spent = min(current, max(0, int(amount)))
        combatant[key] = current - spent
        return {"spent": spent, "overflow": max(0, int(amount)) - spent}

    def persist_combatant(self, combatant: Mapping[str, Any], card: dict[str, Any]) -> None:
        sheet = card.get("sheet") if isinstance(card.get("sheet"), Mapping) else {}
        sheet = dict(sheet)
        hp = sheet.get("hp") if isinstance(sheet.get("hp"), Mapping) else {}
        hp = dict(hp)
        hp["current"] = max(0, _as_int(combatant.get("hp"), 0))
        hp["max"] = max(1, _as_int(combatant.get("max_hp"), 1))
        hp["temp"] = max(0, _as_int(combatant.get("temp_hp"), 0))
        sheet["hp"] = hp
        card["sheet"] = sheet
        card["currentHp"] = hp["current"]
        card["maxHp"] = hp["max"]
        card["tempHp"] = hp["temp"]
        if combatant.get("armorClass") not in (None, ""):
            card["armorClass"] = _as_int(combatant.get("armorClass"), 10)
        from dnd5e_plugin.spells import persist_spellcasting

        persist_spellcasting(card, combatant)

    # -- 内部工具 ---------------------------------------------------------
    def _dex_mod(self, combatant: Mapping[str, Any]) -> int:
        abilities = combatant.get("abilities")
        if isinstance(abilities, Mapping):
            return ability_modifier(ability_score({"abilities": abilities}, "dex"))
        return ability_modifier(_as_int(combatant.get("dex"), 10))

    def _normalize_abilities(self, value: Any) -> dict[str, int]:
        source = value if isinstance(value, Mapping) else {}
        result: dict[str, int] = {}
        for key in ("str", "dex", "con", "int", "wis", "cha"):
            result[key] = max(1, min(30, _as_int(source.get(key), 10)))
        return result

    def _ability_scores_from_flat(self, combatant: Mapping[str, Any]) -> dict[str, int]:
        return {
            key: max(1, min(30, _as_int(combatant.get(key), 10)))
            for key in ("str", "dex", "con", "int", "wis", "cha")
        }
