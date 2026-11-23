"""D&D 5e（SRD 5.1）战斗结算。

本模块只放 5e 的规则数学：先攻、攻击 vs AC、伤害骰与加值、暴击、抗性/易伤/
免疫、临时 HP、死亡豁免与行动经济。核心只通过 ``CombatRuleset`` 的可选钩子
调用这里，不解释命中、AC 或伤害类型。

武器、护甲、怪物数据全部来自 ``dnd5e-srd`` 数据包；插件不另存一份数值。
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any, Callable, Mapping

from dnd5e_plugin.effect_changes import effective_sheet, weapon_row as merge_weapon_row
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
_COMBATANT_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def combatant_id_for(name: Any) -> str:
    """战斗者的稳定 id：纯 ASCII 名字归一化，含中文等非 ASCII 时用短哈希兜底。"""
    text = _text(name)
    slug = _COMBATANT_ID_UNSAFE.sub("-", text).strip("-").lower()[:40]
    if slug and text.isascii():
        return slug
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"npc-{digest}"


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def house_rule(settings: Any, key: str, default: Any) -> Any:
    """读取房间房规里的 5e 字段。

    核心只把房规原文挂在 ``settings.house_rules``，不解释任何键；字段名、
    取值与默认值都属于插件。
    """
    raw = getattr(settings, "house_rules", None)
    if isinstance(raw, Mapping) and key in raw:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return default


def _rule_flag(settings: Any, key: str, default: bool = False) -> bool:
    value = house_rule(settings, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
    terms: list[tuple[int, int]],
    fixed: int,
    rng: Any,
    *,
    critical: bool = False,
    critical_mode: str = "double_dice",
) -> tuple[int, list[dict[str, Any]]]:
    """掷伤害骰。

    ``critical_mode`` 决定暴击时的骰子处理：``double_dice``（默认，骰数翻倍）
    或 ``max_dice``（骰数不变、每颗取最大值）。非暴击时两者一致。
    """
    total = max(0, int(fixed))
    detail: list[dict[str, Any]] = []
    for count, sides in terms:
        base = max(0, int(count))
        if critical and critical_mode == "max_dice":
            actual = base
            rolls = [max(1, int(sides)) for _ in range(actual)]
        else:
            actual = base * (2 if critical else 1)
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


def _carried_weight_lb(sheet: Mapping[str, Any]) -> float:
    """按装备条目累计负重；数据包未声明重量的条目按 0 计。"""
    total = 0.0
    for identifier, _equipped in _equipment_items(sheet):
        row = _pack_entry("equipment", identifier)
        if row is None:
            continue
        try:
            total += max(0.0, float(row.get("weight_lb") or 0))
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def encumbrance_level(strength: int, weight_lb: float) -> str:
    """变体负重分级：``heavy``（重载）、``light``（负重）、空串（正常）。

    阈值来自 SRD：超过力量 ×5 为负重，超过力量 ×10 为重载。
    """
    score = max(1, int(strength))
    weight = max(0.0, float(weight_lb))
    if weight > score * 10:
        return "heavy"
    if weight > score * 5:
        return "light"
    return ""


def armor_strength_requirement(sheet: Mapping[str, Any]) -> int:
    """已装备护甲里最高的力量需求；盾牌不设需求。"""
    requirement = 0
    for row in _equipment_rows(sheet, "armor"):
        if _text(row.get("armor_category")).casefold() == "shield":
            continue
        requirement = max(requirement, _as_int(row.get("strength_min"), 0))
    return requirement


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


# ---------------------------------------------------------------------------
# 声明式 workflow 钩子（effect_runtime DSL）：命中/未命中后执行白名单动作
# ---------------------------------------------------------------------------

def _combat_hooks(
    attacker_card: Mapping[str, Any] | None,
    attack: Mapping[str, Any],
    attacker: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """收集可执行的钩子：卡级 + 武器行，只跑 ``converted`` 的 DSL。

    ``attacker_card`` 缺席时（AI 直接使用已准备战斗者）回落到准备阶段
    写进战斗者的 ``workflow_hooks``，两条入口同一口径。
    """
    hooks: list[dict[str, Any]] = []
    sources: list[Any] = []
    if isinstance(attacker_card, Mapping):
        sources.append(sheet_of(attacker_card).get("workflow_hooks"))
    if isinstance(attacker, Mapping):
        sources.append(attacker.get("workflow_hooks"))
    for bucket in sources:
        for raw in bucket or []:
            if isinstance(raw, Mapping) and raw.get("status") == "converted" and isinstance(raw.get("dsl"), Mapping):
                hooks.append(dict(raw["dsl"]))
    row_hooks = attack.get("workflow_hooks") if isinstance(attack.get("workflow_hooks"), list) else []
    for raw in row_hooks:
        if isinstance(raw, Mapping) and raw.get("status") == "converted" and isinstance(raw.get("dsl"), Mapping):
            hooks.append(dict(raw["dsl"]))
    return hooks


def _apply_workflow_actions(
    rules: Any,
    attacker: dict[str, Any],
    target: dict[str, Any],
    attack: Mapping[str, Any],
    actions: list[Mapping[str, Any]],
    *,
    rng: Any,
    combat: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """执行 effect_runtime 产出的动作；只做战斗内安全变更，不改文档。"""
    applied: list[dict[str, Any]] = []
    for action in actions:
        kind = _text(action.get("type")).lower()
        if kind == "damage.add":
            dice = _text(action.get("dice") or action.get("amount") or "1d4")
            terms, fixed = _parse_damage(dice)
            rolled, _detail = _roll_terms(terms, fixed, rng, critical=False)
            amount = max(0, int(rolled))
            damage_type = _text(action.get("damage_type") or attack.get("damage_type") or "untyped")
            result = rules._apply_damage(target, amount, damage_type, combat=combat, rng=rng)
            applied.append(
                {
                    "type": kind,
                    "amount": amount,
                    "damage_type": damage_type,
                    "effective": result.get("effective"),
                }
            )
        elif kind == "heal":
            amount = max(0, _as_int(action.get("amount"), 0))
            before = max(0, _as_int(attacker.get("hp"), 0))
            max_hp = max(1, _as_int(attacker.get("max_hp"), 1))
            attacker["hp"] = min(max_hp, before + amount)
            applied.append({"type": kind, "amount": attacker["hp"] - before})
        elif kind == "temp_hp":
            amount = max(0, _as_int(action.get("amount"), 0))
            attacker["temp_hp"] = max(_as_int(attacker.get("temp_hp"), 0), amount)
            applied.append({"type": kind, "amount": amount})
        elif kind == "condition.add":
            condition = _text(action.get("condition"))
            if condition:
                bucket = target.setdefault("conditions", [])
                if isinstance(bucket, list) and condition not in bucket:
                    bucket.append(condition)
                applied.append({"type": kind, "condition": condition, "target": "target"})
        elif kind == "resource.spend":
            uses = attack.get("uses") if isinstance(attack.get("uses"), Mapping) else None
            if uses is not None:
                uses = dict(uses)
                uses["value"] = max(
                    0, _as_int(uses.get("value"), 0) - max(0, _as_int(action.get("amount"), 1))
                )
                attack["uses"] = uses
                applied.append({"type": kind, "value": uses["value"]})
        else:
            # attack.advantage / save.* 属于掷骰前决策，这里只记录不执行。
            applied.append({"type": kind, "skipped": "pre-roll"})
    return applied



def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _grants_attack_modes(effects: Any) -> dict[str, Any]:
    """``flags.midi-qol.grants.{advantage,disadvantage}.attack.all`` → 战斗者字段。

    值可以是 ``true``（所有攻击者）或类型列表（Protection from Evil and Good
    只对异怪/天界/元素/妖精/邪魔/亡灵生效）。
    """
    modes: dict[str, Any] = {}
    for kind, key in (
        ("advantage", "flags.midi-qol.grants.advantage.attack.all"),
        ("disadvantage", "flags.midi-qol.grants.disadvantage.attack.all"),
    ):
        value = effects.value(key)
        if isinstance(value, (list, tuple, set)):
            types = [str(item).casefold() for item in value if str(item).strip()]
            if types:
                modes[kind] = types
        elif value is not None and _truthy(value):
            modes[kind] = True
    return modes

def _run_workflow_hooks(
    rules: Any,
    attacker: dict[str, Any],
    target: dict[str, Any],
    attack: Mapping[str, Any],
    *,
    attacker_card: Mapping[str, Any] | None,
    hit: bool,
    rng: Any,
    combat: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    hooks = _combat_hooks(attacker_card, attack, attacker)
    if not hooks:
        return None
    try:
        from dnd5e_plugin import effect_runtime as runtime
    except Exception:  # noqa: BLE001 - 模块缺席时静默跳过
        return None
    context = {"self": attacker, "target": target, "weapon": dict(attack), "hit": bool(hit)}

    def hook_rng() -> float:
        """运行时求值器要 ``[0,1)`` 均匀随机；战斗引擎的 rng 是 ``rng(sides)``。"""
        try:
            roll = int(rng(20))
        except Exception:  # noqa: BLE001 - 不可用的 rng 退化为中值
            return 0.5
        return max(0.0, min(0.9999, (roll - 1) / 20.0))

    outcome = runtime.run("onHit" if hit else "onMiss", context, hooks, rng=hook_rng)
    actions = [item for item in outcome.get("actions") or [] if isinstance(item, Mapping)]
    applied = _apply_workflow_actions(
        rules, attacker, target, attack, actions, rng=rng, combat=combat
    )
    return {
        "trigger": "onHit" if hit else "onMiss",
        "hooks": len(hooks),
        "actions": applied,
        "notes": list(outcome.get("notes") or []),
    }


def _spellcasting_ability(sheet: Mapping[str, Any]) -> str:
    """卡面施法属性（``activities[attack].attack.ability = spellcasting`` 用）。"""
    casting = sheet.get("spellcasting")
    casting = casting if isinstance(casting, Mapping) else {}
    ability = _text(casting.get("ability")).casefold()
    return ability if ability in {"str", "dex", "con", "int", "wis", "cha"} else "int"


def _effective_proficiency(sheet: Mapping[str, Any], level: Any) -> int:
    """熟练加值：卡面派生值（效果引擎 ``attributes.prof`` 已并入）优先。"""
    explicit = sheet.get("proficiency_bonus")
    if explicit not in (None, ""):
        return _as_int(explicit, proficiency_bonus(level))
    return proficiency_bonus(level)


def _row_item_system(row: Mapping[str, Any]) -> dict[str, Any]:
    """行上规范化物品文档的 ``system``（``row["item"]``）；没有给 ``{}``。"""
    document = row.get("item")
    if not isinstance(document, Mapping):
        return {}
    system = document.get("system")
    return dict(system) if isinstance(system, Mapping) else {}


def _document_dice(base: Mapping[str, Any]) -> str:
    """``system.damage.base.{number,denomination}`` → ``"1d8"``（缺一半给空串）。"""
    number = _as_int(base.get("number"), 0)
    denomination = _as_int(base.get("denomination"), 0)
    if number and denomination:
        return f"{number}d{denomination}"
    return ""


def _document_damage_type(base: Mapping[str, Any]) -> str:
    types = base.get("types")
    if isinstance(types, (list, tuple)) and types:
        return _text(types[0]).casefold()
    return ""


def _document_uses(row: Mapping[str, Any], system: Mapping[str, Any]) -> dict[str, Any] | None:
    """行上 ``uses``（导入器写的）优先，其次读文档 ``system.uses`` / ``system.charges``。

    只做透传展示，**不消耗**：战斗结算不扣次数，卡面「物品」页签读同一个字段。
    """
    row_uses = row.get("uses")
    if isinstance(row_uses, Mapping) and row_uses:
        return dict(row_uses)
    for key in ("uses", "charges"):
        raw = system.get(key)
        if isinstance(raw, Mapping):
            if not (_text(raw.get("max")) or _text(raw.get("value")) or _text(raw.get("spent"))):
                continue
            maximum = _as_int(raw.get("max"), 0)
            current = raw.get("value")
            value = _as_int(current, 0) if _text(current) else max(0, maximum - _as_int(raw.get("spent"), 0))
            return {"value": max(0, value), "max": maximum, "recovery": [], "source": f"document.{key}"}
        number = _as_int(raw, 0) if _text(raw) else 0
        if _text(raw):
            return {"value": max(0, number), "max": max(0, number), "recovery": [], "source": f"document.{key}"}
    return None


def _weapon_attack(row: Mapping[str, Any], sheet: Mapping[str, Any]) -> dict[str, Any]:
    properties = [_text(item).casefold() for item in row.get("properties") or []]
    # 文档兜底：行上没投影到 ``properties`` 时（手工粘贴/旧导入的行），
    # 按物品文档的 ``system.properties`` 补一遍（文档 > 扁平字段）。
    system = _row_item_system(row)
    raw_props = system.get("properties")
    for prop in raw_props if isinstance(raw_props, (list, tuple)) else []:
        text = _text(prop).casefold()
        if text and text not in properties:
            properties.append(text)
    kind = _text(row.get("kind")).casefold() or ("ranged" if "ammunition" in properties else "melee")
    # 物品活动可显式指定攻击属性（``activities[…].attack.ability``）：
    # ``spellcasting`` 解析成卡面施法属性，其余按属性键直取。
    override = _text(row.get("attack_ability")).casefold()
    if override == "spellcasting":
        ability = _spellcasting_ability(sheet)
    elif override in {"finesse", "best"}:
        # FVTT ``attack.ability = finesse``：力/敏取高。
        ability = (
            "dex"
            if ability_score(sheet, "dex") >= ability_score(sheet, "str")
            else "str"
        )
    elif override in {"str", "dex", "con", "int", "wis", "cha"}:
        ability = override
    else:
        ability = "dex" if ("finesse" in properties or kind == "ranged") else "str"
    modifier = ability_modifier(ability_score(sheet, ability))
    level = sheet.get("level", 1)
    bonus = modifier + (_effective_proficiency(sheet, level) if _proficient_with_weapon(sheet, row) else 0)
    # ``activities[…].attack.bonus`` 这类物品活动覆盖值是「攻击检定加值」，叠加在
    # 属性+熟练之上（FVTT 语义：进攻击骰公式的额外项）。
    bonus += _as_int(row.get("attack_bonus"), 0)
    damage = row.get("damage") if isinstance(row.get("damage"), Mapping) else {}
    # 伤害骰 / 伤害类型 / 双手骰：行的扁平字段 → 物品文档 → 兜底（``1d4`` / 钝击）。
    doc_damage = system.get("damage") if isinstance(system.get("damage"), Mapping) else {}
    doc_base = doc_damage.get("base") if isinstance(doc_damage.get("base"), Mapping) else {}
    doc_versatile = (
        doc_damage.get("versatile") if isinstance(doc_damage.get("versatile"), Mapping) else {}
    )
    damage_dice = _text(damage.get("dice")) or _document_dice(doc_base) or "1d4"
    damage_type = _text(damage.get("type")) or _document_damage_type(doc_base) or "bludgeoning"
    versatile_dice = _text(row.get("versatile_damage")) or _document_dice(doc_versatile)
    attack = {
        "id": _text(row.get("id")),
        "name": _display_name(row) or _text(row.get("name")),
        "kind": kind,
        "ability": ability,
        "attack_bonus": bonus,
        "damage_bonus": modifier,
        "damage_dice": damage_dice,
        "damage_type": damage_type,
        "versatile_dice": versatile_dice,
        "properties": properties,
    }
    uses = _document_uses(row, system)
    if uses is not None:
        # 消耗品/有限次数物品：只透传展示（不消耗），战斗 UI 与卡面读同一份。
        attack["uses"] = uses
    range_override = row.get("range_override") or (
        row.get("range") if isinstance(row.get("range"), Mapping) else None
    )
    if isinstance(range_override, Mapping) and range_override:
        attack["range"] = dict(range_override)
    elif isinstance(system.get("range"), Mapping) and system.get("range"):
        # 文档射程（``system.range.{value,long,units}``）：行上没有射程时才用。
        doc_range = system.get("range") or {}
        normal = _as_int(doc_range.get("value"), 0)
        long_range = _as_int(doc_range.get("long"), 0)
        if normal or long_range:
            attack["range"] = {
                "normal_ft": max(0, normal),
                "long_ft": max(0, long_range),
                "units": _text(doc_range.get("units")) or "ft",
            }
    return attack


def _sheet_item_documents(sheet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``sheet.equipment[].item`` 按 id / item_id / 名字（大小写归一）索引。

    战斗行来自 ``dnd5e-srd`` 库（按 id/名字查表），卡面自带的规范化文档要
    按同一套键挂回去，``_weapon_attack`` 才能读到文档字段。
    """
    raw = sheet.get("equipment")
    docs: dict[str, dict[str, Any]] = {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, Mapping):
            continue
        document = row.get("item")
        if not isinstance(document, Mapping) or not document:
            continue
        keys = [
            _text(row.get("id")),
            _text(row.get("item_id")),
            _text(row.get("name")),
            _text(document.get("id")),
            _text(document.get("name")),
        ]
        for key in keys:
            if key:
                docs.setdefault(key.casefold(), dict(document))
    return docs


def _player_attacks(sheet: Mapping[str, Any], effects: Any = None) -> list[dict[str, Any]]:
    """装备行 → 攻击表。

    顺序：库行 → 挂上卡面物品文档（``equipment[].item``）→ 叠加物品作用域效果
    （``weapon_row``）→ 按扁平字段算攻击属性与伤害骰。没有文档且没有效果时
    逐字节同旧实现。
    """
    rows = [row for row in _equipment_rows(sheet, "weapon") if row.get("category") == "weapon"]
    documents = _sheet_item_documents(sheet)
    if documents:
        attached: list[dict[str, Any]] = []
        for row in rows:
            keys = [
                _text(row.get("id")).casefold(),
                _text(row.get("name")).casefold(),
                _display_name(row).casefold(),
            ]
            document = next((documents[key] for key in keys if key and key in documents), None)
            if document is not None:
                row = {**row, "item": document}
            attached.append(row)
        rows = attached
    has_effects = effects is not None and not effects.empty and getattr(effects, "items", None)
    if has_effects or documents:
        rows = [merge_weapon_row(row, effects if has_effects else None) for row in rows]
    attacks = [_weapon_attack(row, sheet) for row in rows]
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
        mode = _text(house_rule(settings, "dnd5e_initiative_mode", "roll")).lower()
        dex = self._dex_mod(combatant)
        if mode == "fixed":
            return 10 + dex
        if mode == "advantage":
            return max(_roll_die(rng, 20), _roll_die(rng, 20)) + dex
        if combatant.get("surprised") and _text(
            house_rule(settings, "dnd5e_surprise_rule", "standard")
        ).lower() == "initiative_disadvantage":
            return min(_roll_die(rng, 20), _roll_die(rng, 20)) + dex
        return _roll_die(rng, 20) + dex

    def initiative_tiebreak(self, combatant: Mapping[str, Any]) -> int:
        return self._dex_mod(combatant)

    def actions_per_round(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def reaction_limit(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def action_budget(self, combatant: Mapping[str, Any], settings: Any) -> dict[str, int]:
        speed = effective_speed(combatant, _as_int(combatant.get("speed_ft"), 30))
        # 变体负重的分级在开战准备时按房间开关写入，这里只消费结果。
        encumbrance = _text(combatant.get("encumbrance")).lower()
        if encumbrance == "heavy":
            speed = max(0, speed - 20)
        elif encumbrance == "light":
            speed = max(0, speed - 10)
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
            self._prepare_player(combatant, card, settings)
            return
        monster_id = _text(combatant.get("monster_id"))
        if monster_id:
            monster = _pack_entry("monster", monster_id)
            if monster is None:
                raise ValueError(f"数据包 dnd5e-srd 中没有怪物“{monster_id}”")
            self._apply_monster(combatant, monster)
            combatant.setdefault("combatant_id", combatant_id_for(combatant.get("name")))
            return
        combatant.setdefault("abilities", self._ability_scores_from_flat(combatant))
        combatant.setdefault("armorClass", _as_int(combatant.get("ac"), 10 + self._dex_mod(combatant)))
        combatant.setdefault("attacks", [dict(DEFAULT_UNARMED)])
        combatant.setdefault("speed_ft", 30)
        combatant.setdefault("temp_hp", 0)
        combatant.setdefault("combatant_id", combatant_id_for(combatant.get("name")))

    def prepare_combatant_from_module(
        self,
        combatant: dict[str, Any],
        module: Mapping[str, Any],
        *,
        settings: Any = None,
    ) -> None:
        """把剧本 NPC 模块映射成 5e 战斗者；核心只负责转交模块原文。"""
        from dnd5e_plugin.statblock import combatant_from_module

        spec = combatant_from_module(module)
        name = _text(combatant.get("name")) or spec["name"]
        side = _text(combatant.get("side")) or spec.get("side") or "enemy"
        abilities = dict(spec["abilities"])
        combatant.update(
            {
                "is_player": False,
                "is_npc": True,
                "side": side,
                "combatant_id": combatant_id_for(name),
                "module_id": spec.get("module_id") or _text(module.get("id")),
                "monster_id": spec.get("monster_id") or "",
                "monster_name": spec.get("monster_name") or spec.get("display_name") or name,
                "display_name": spec.get("display_name") or name,
                "abilities": abilities,
                "dex": abilities.get("dex", 10),
                "str": abilities.get("str", 10),
                "con": abilities.get("con", 10),
                "armorClass": _as_int(spec.get("armorClass"), 10),
                "max_hp": max(1, _as_int(spec.get("max_hp"), 1)),
                "hp": max(0, _as_int(spec.get("hp"), 1)),
                "temp_hp": max(0, _as_int(spec.get("temp_hp"), 0)),
                "speed_ft": max(0, _as_int(spec.get("speed_ft"), 30)),
                "attacks": [dict(row) for row in spec.get("attacks") or []],
                "creature_type": _text(spec.get("creature_type")).casefold() or "humanoid",
                "size": _text(spec.get("size")),
                "cr": spec.get("cr") or "",
                "xp": _as_int(spec.get("xp"), 0),
                "damage_resistances": list(spec.get("damage_resistances") or []),
                "damage_vulnerabilities": list(spec.get("damage_vulnerabilities") or []),
                "damage_immunities": list(spec.get("damage_immunities") or []),
                "statblock_source": spec.get("source") or "",
            }
        )
        # 施法透传：模块 statblock 自带的施法档案/法术位/法术清单落到战斗者上，
        # 核心法术管线（spells.py）按 spellcasting.spellSaveDC、spell_slots、
        # spells_known/spells_prepared 消费，反制与升环判定无需再查卡面。
        spell_profile = spec.get("spellcasting")
        if isinstance(spell_profile, Mapping):
            combatant["spellcasting"] = dict(spell_profile)
        slot_rows = spec.get("spell_slots")
        if isinstance(slot_rows, Mapping) and slot_rows:
            combatant["spell_slots"] = {
                str(key): dict(row) for key, row in slot_rows.items() if isinstance(row, Mapping)
            }
        known = spec.get("spells_known")
        if known:
            combatant["spells_known"] = list(known)
        prepared = spec.get("spells_prepared")
        if prepared:
            combatant["spells_prepared"] = list(prepared)
        if combatant["hp"] > combatant["max_hp"]:
            combatant["hp"] = combatant["max_hp"]

    def _prepare_player(
        self,
        combatant: dict[str, Any],
        card: Any,
        settings: Any = None,
    ) -> None:
        sheet = sheet_of(card) if isinstance(card, Mapping) else {}
        # 效果引擎统一视图：空视图时下面所有分支都不改一个数（回归铁律）。
        effects = effective_sheet(sheet)
        ability_source = sheet.get("abilities") or sheet.get("attributes")
        if not effects.empty:
            # ``system.abilities.*.value``：先合并底层属性值，再走派生。
            ability_source = effects.merged_abilities(ability_source)
        abilities = self._normalize_abilities(ability_source)
        combatant["abilities"] = abilities
        combatant["level"] = max(1, min(20, _as_int(sheet.get("level"), 1)))
        profs = sheet.get("proficiencies")
        combatant["proficiencies"] = dict(profs) if isinstance(profs, Mapping) else {}
        # 钩子随战斗者带上：AI/无卡的攻击路径也能跑同一批钩子（与效果是否为空无关）。
        if isinstance(sheet.get("workflow_hooks"), list):
            combatant["workflow_hooks"] = list(sheet["workflow_hooks"])
        combatant["dex"] = abilities["dex"]
        combatant["str"] = abilities["str"]
        combatant["con"] = abilities["con"]

        hp = sheet.get("hp") if isinstance(sheet.get("hp"), Mapping) else {}
        max_hp = max(
            1,
            _as_int(hp.get("max"), _as_int(sheet.get("maxHp"), _as_int(combatant.get("max_hp"), 1))),
        )
        if not effects.empty:
            # ``system.attributes.hp.bonus`` / ``hp.max``：效果级生命上限。
            max_hp = effects.max_hp(max_hp)
        card_current = card.get("currentHp") if isinstance(card, Mapping) else None
        if card_current in (None, ""):
            card_current = hp.get("current")
        # 先算效果后的上限，再夹当前 HP（顺序反了会把 30/24 的组合提前压到 24）。
        current = max(0, min(max_hp, _as_int(card_current, _as_int(combatant.get("hp"), max_hp))))
        combatant["max_hp"] = max_hp
        combatant["hp"] = current
        combatant["temp_hp"] = max(0, _as_int(hp.get("temp"), 0))
        combatant["armorClass"] = armor_class_from_equipment(sheet)
        if not effects.empty:
            combatant["armorClass"] = effects.armor_class(
                _as_int(combatant["armorClass"], 10),
                explicit=sheet.get("armorClass") not in (None, ""),
                dex_mod=ability_modifier(abilities.get("dex", 10)),
            )
        # 卡面 ``base_speed`` 是效果前原始速度（pack 写入）；没有时用 speed。
        base_speed = max(0, _as_int(sheet.get("base_speed"), _as_int(sheet.get("speed"), 30)))
        # ``system.attributes.movement.*`` → 分速模型；walk 继续落 ``speed_ft``，
        # fly/swim/climb/burrow/jump 只在有值时写进 ``speeds``（卡面只显示有值的）。
        if effects.empty:
            combatant["speed_ft"] = base_speed
        else:
            speeds = effects.speeds(base_speed, 30, sheet.get("base_speeds"))
            combatant["speed_ft"] = int(speeds.get("walk", base_speed))
            extra = {key: value for key, value in speeds.items() if key != "walk" and value}
            if extra:
                combatant["speeds"] = speeds
        if not effects.empty:
            # ``attributes.prof``：熟练加值覆盖/加值，战斗者带一份供攻击与豁免读取。
            combatant["proficiency_bonus"] = effects.proficiency_bonus(
                max(1, min(20, _as_int(sheet.get("level"), 1)))
            )
            dm_row = effects.damage_reduction_row()
            if dm_row:
                # ``traits.dm``：减伤在抗性/易伤之后、吸收临时生命之前结算。
                combatant["damage_reduction"] = dm_row
            grants = _grants_attack_modes(effects)
            if grants:
                # 目标侧：攻击者对「我」的掷骰优势/劣势（combat 结算时读）。
                combatant["grants_attack"] = grants
            if _truthy(effects.value("flags.dnd5e.halflingLucky")):
                combatant["halfling_lucky"] = True
            combatant["effect_attack_roll_mode"] = {
                "mwak": effects.attack_roll_mode("mwak"),
                "rwak": effects.attack_roll_mode("rwak"),
            }
            combatant["effect_critical_threshold"] = {
                "mwak": effects.critical_threshold("mwak"),
                "rwak": effects.critical_threshold("rwak"),
            }
        requirement = armor_strength_requirement(sheet)
        if requirement and abilities.get("str", 10) < requirement:
            # 力量不满足护甲需求：速度 -10 英尺（SRD 护甲表）。
            combatant["armor_strength_penalty"] = requirement
            combatant["speed_ft"] = max(0, combatant["speed_ft"] - 10)
        attacks = _player_attacks(effects.merged_sheet(sheet) if not effects.empty else sheet, effects)
        combatant["attacks"] = attacks if effects.empty else effects.apply_attacks(attacks)
        combatant["carried_weight_lb"] = _carried_weight_lb(sheet)
        # 变体负重是房间开关；关闭时不写任何标记，行动经济按原速度结算。
        level = ""
        if _rule_flag(settings, "dnd5e_variant_encumbrance"):
            level = encumbrance_level(abilities.get("str", 10), combatant["carried_weight_lb"])
        if level:
            combatant["encumbrance"] = level
        # 玩家默认 humanoid；角色卡显式声明时以卡为准。
        combatant.setdefault(
            "creature_type",
            _text(sheet.get("creature_type")).casefold() or "humanoid",
        )
        combatant["damage_resistances"] = list(sheet.get("damage_resistances") or [])
        combatant["damage_vulnerabilities"] = list(sheet.get("damage_vulnerabilities") or [])
        combatant["damage_immunities"] = list(sheet.get("damage_immunities") or [])
        if not effects.empty:
            # 抗性/免疫/易伤：工程里 traits.dr/dv/di 走同一份战斗者字段。
            combatant["damage_resistances"] = effects.extend_traits(
                combatant["damage_resistances"], "dr"
            )
            combatant["damage_vulnerabilities"] = effects.extend_traits(
                combatant["damage_vulnerabilities"], "dv"
            )
            combatant["damage_immunities"] = effects.extend_traits(
                combatant["damage_immunities"], "di"
            )
            # traits.ci.value：免疫的条件不会被 effects.apply 施加。
            combatant["condition_immunities"] = effects.extend_traits(
                combatant.get("condition_immunities"), "ci"
            )
        from dnd5e_plugin.spells import prepare_spellcasting

        prepare_spellcasting(combatant, sheet)
        if not effects.empty:
            profile = combatant.get("spellcasting")
            delta = effects.spell_attack_bonus()
            if isinstance(profile, dict) and delta:
                profile["spellAttackBonus"] = _as_int(profile.get("spellAttackBonus"), 0) + delta
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

        # 效果引擎（midi-qol 子集）：攻击 d20 模式与暴击阈值。卡面没效果时零影响。
        effect_roll_mode = 0
        crit_threshold = 20
        card_effects = None
        attack_kind = "rwak" if _text(attack.get("kind")).casefold() == "ranged" else "mwak"
        if isinstance(attacker_card, Mapping):
            card_effects = effective_sheet(sheet_of(attacker_card))
            if not card_effects.empty:
                effect_roll_mode = card_effects.attack_roll_mode(attack_kind)
                crit_threshold = card_effects.critical_threshold(attack_kind)
        if card_effects is None or card_effects.empty:
            # 卡面没传（例如已准备的好战斗者直接被 AI 工具使用）时，用
            # 准备阶段写进战斗者的效果值，口径一致。
            modes = attacker.get("effect_attack_roll_mode")
            if isinstance(modes, Mapping):
                effect_roll_mode = _as_int(modes.get(attack_kind), 0)
            thresholds = attacker.get("effect_critical_threshold")
            if isinstance(thresholds, Mapping):
                crit_threshold = max(2, min(20, _as_int(thresholds.get(attack_kind), 20)))

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
        if effect_roll_mode > 0:
            advantage = True
        elif effect_roll_mode < 0:
            disadvantage = True
        # 夹击：房间开启且本次近战攻击被标记为夹击时给优势。
        if (
            _rule_flag(settings, "dnd5e_flanking")
            and (action.get("flanking") or action.get("flank"))
            and _text(attack.get("kind")).casefold() != "ranged"
        ):
            advantage = True
        # 变体负重达到重载时，攻击检定带劣势（分级在开战准备时算好）。
        if _text(attacker.get("encumbrance")).lower() == "heavy":
            disadvantage = True
        # 目标侧 grants：持有者让「攻击者对其攻击」获得优势/劣势（可按类型过滤）。
        granted = target.get("grants_attack") if isinstance(target, Mapping) else None
        if isinstance(granted, Mapping):
            attacker_type = _text(attacker.get("creature_type")).casefold()
            for kind, value in granted.items():
                matched = (
                    attacker_type in {str(item).casefold() for item in value}
                    if isinstance(value, (list, tuple, set))
                    else bool(value)
                )
                if matched and kind == "advantage":
                    advantage = True
                elif matched and kind == "disadvantage":
                    disadvantage = True
        if advantage and disadvantage:
            advantage = disadvantage = False
        if not isinstance(preset_rolls, list) or not preset_rolls:
            if advantage or disadvantage:
                second = _roll_die(rng, 20)
                rolls.append(second)
                natural = max(rolls) if advantage else min(rolls)

        # 半身人幸运：d20 掷出 1 时重掷一次（本作按「每次攻击一次」实现）。
        if natural == 1 and attacker.get("halfling_lucky"):
            rerolled = _roll_die(rng, 20)
            rolls.append(rerolled)
            natural = rerolled

        target_ac = _as_int(target.get("armorClass") or target.get("ac"), 10)
        cover = _text(action.get("cover")).lower()
        # 掩体规则关闭时忽略所有掩体加值与全掩体禁攻。
        cover_enabled = _text(house_rule(settings, "dnd5e_cover_rule", "standard")).lower() != "none"
        if cover_enabled and cover in {"half", "半", "half_cover"}:
            target_ac += 2
        elif cover_enabled and cover in {"three_quarters", "3/4", "四分之三"}:
            target_ac += 5
        elif cover_enabled and cover in {"full", "全", "full_cover"}:
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
        # 暴击阈值：midi-qol ``critical.*`` 可给到 19/18（默认 20）。阈值只扩大
        # 「暴击候选」范围，仍要先命中才算暴击（20 依旧必中，19 不保证命中）。
        critical_candidate = natural >= crit_threshold
        hit = natural >= 20 or (natural != 1 and total >= target_ac)
        critical = bool(hit and critical_candidate)
        if hit and condition_mods["auto_crit"]:
            critical = True
        critical_mode = "double_dice"
        critical_rule = _text(house_rule(settings, "dnd5e_critical_rule", "standard")).lower()
        if critical_rule == "none":
            critical = False
        elif critical and critical_rule == "confirm":
            # 二次确认：再掷一次攻击，命中 AC 才保留暴击，否则按普通命中。
            confirm = _roll_die(rng, 20)
            if confirm + attack_bonus < target_ac:
                critical = False
        elif critical and critical_rule == "max_dice":
            critical_mode = "max_dice"

        damage_result = None
        if hit and not dry_run:
            terms, fixed = _parse_damage(attack.get("damage_dice"))
            if action.get("two_handed") and attack.get("versatile_dice"):
                terms, fixed = _parse_damage(attack.get("versatile_dice"))
            rolled, detail = _roll_terms(
                terms,
                fixed,
                rng,
                critical=critical,
                critical_mode=critical_mode,
            )
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
        # 声明式 workflow 钩子（导入包里的 midi-qol 宏转成 DSL）：命中/未命中后
        # 执行白名单动作（加骰伤害、条件、治疗、临时生命、资源消耗）。
        workflow = _run_workflow_hooks(
            self,
            attacker,
            target,
            attack,
            attacker_card=attacker_card,
            hit=hit,
            rng=rng,
            combat=combat,
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
            workflow=workflow,
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
            sheet = sheet_of(card)
            effects = effective_sheet(sheet)
            attacks = _player_attacks(effects.merged_sheet(sheet) if not effects.empty else sheet, effects)
            if not effects.empty:
                # 卡面没预置攻击时现算：同样要带上效果引擎的攻击/伤害加值。
                attacks = effects.apply_attacks(attacks)
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

        # ``system.traits.dm``：伤害减免。顺序 = 免疫 → 易伤/抗性 → 减伤，
        # 减伤不会把伤害压到负数（SRD：先算抗性倍率再扣固定减免）。
        reduction_row = target.get("damage_reduction")
        reduction = 0
        if isinstance(reduction_row, Mapping):
            declared = _text(reduction_row.get("type")).casefold()
            if not declared or declared in {"all", "any", "所有"} or declared == dtype:
                reduction = max(0, min(effective, _as_int(reduction_row.get("amount"), 0)))
                effective -= reduction

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
            "damage_reduction": reduction,
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
        workflow: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
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
        # 钩子执行结果单独挂载：没有钩子时结果字典与旧版逐字节一致。
        if workflow:
            result["workflow"] = dict(workflow)
        return result

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
            rule = _text(house_rule(settings, "dnd5e_surprise_rule", "standard")).lower()
            if names & surprised and rule == "standard":
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

    def on_combat_saved(self, combat: Mapping[str, Any], *, context: Any = None) -> None:
        """战斗状态每次落盘后同步地图棋子；地图不可用时静默降级。"""
        if context is None or getattr(context, "skip_combat_map_sync", False):
            return
        from dnd5e_plugin.map_link import sync_combat_tokens

        sync_combat_tokens(context, combat)

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
