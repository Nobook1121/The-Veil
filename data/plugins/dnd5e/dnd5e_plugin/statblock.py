"""把剧本 NPC 模块桥接成 D&D 5e 战斗者。

剧本里的 NPC 卡通常只写叙事内容；真正开战需要的属性、AC、HP、攻击与 CR
以 ``dnd5e-srd`` 数据包为基线，剧本模块可以携带同名字段覆盖（例如给首领
单独调高 AC 或换一把武器）。没有可用的怪物基线时显式失败，不生成占位数值。

约定（全部是可选的剧本字段，核心不认识它们）：

- ``monster_id`` / ``ref_id``：数据包怪物 id；也可以写在 ``metadata`` 或
  ``battle`` / ``statblock`` 里。
- ``abilities``：六项属性覆盖（支持大小写键）。
- ``armorClass`` / ``armor_class`` / ``ac``：AC 覆盖。
- ``hp`` / ``maxHp`` / ``max_hp``：HP 覆盖；``hp`` 可以是数字或
  ``{"current": ..., "max": ...}``。
- ``attacks``：攻击列表覆盖；兼容 ``damage_dice`` 与 ``damage: [{dice, type}]``。
- ``speed`` / ``speed_ft``、``creature_type`` / ``type``、``cr``、``xp``、
  ``damage_resistances`` 等：按 5e 字段覆盖数据包。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from dnd5e_plugin.combat import (
    DEFAULT_UNARMED,
    _as_int,
    _display_name,
    _monster_attacks,
    _pack_entry,
    _text,
)
from dnd5e_plugin.effect_changes import effective_sheet
from dnd5e_plugin.rules import ability_modifier

_ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")
_TITLE_PREFIX = re.compile(r"^\s*(?:npc|monster|怪物|角色)\s*[:：]\s*", re.IGNORECASE)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def module_display_name(module: Mapping[str, Any]) -> str:
    """模块标题转成适合战斗面板/棋子显示的名字。"""
    raw = _text(module.get("title") or module.get("name") or module.get("code"))
    name = _TITLE_PREFIX.sub("", raw).strip()
    return (name or raw or "未知单位")[:80]


def _module_sources(module: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按优先级从高到低返回可读的覆盖来源。"""
    sources: list[dict[str, Any]] = []
    for key in ("statblock", "battle"):
        value = module.get(key)
        if isinstance(value, Mapping):
            sources.append(dict(value))
    sources.append(dict(module))
    return sources


def _first(source_list: list[dict[str, Any]], *keys: str) -> Any:
    for source in source_list:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", []):
                return value
    return None


def _monster_reference(module: Mapping[str, Any], sources: list[dict[str, Any]]) -> str:
    for source in (sources[:2] + [_mapping(module.get("metadata"))] + sources[2:]):
        for key in ("monster_id", "ref_id", "monster"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_abilities(value: Any) -> dict[str, int] | None:
    source = value if isinstance(value, Mapping) else None
    if not source:
        return None
    abilities: dict[str, int] = {}
    for key in _ABILITY_KEYS:
        raw = source.get(key, source.get(key.upper()))
        if raw in (None, ""):
            continue
        abilities[key] = max(1, min(30, _as_int(raw, 10)))
    return abilities or None


def _armor_class(value: Any) -> int | None:
    if isinstance(value, Mapping):
        for key in ("value", "ac", "armorClass"):
            if value.get(key) not in (None, ""):
                return max(0, _as_int(value.get(key), 0))
        return None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        return _armor_class(first) if isinstance(first, Mapping) else _as_int(first, 0)
    if value in (None, ""):
        return None
    return max(0, _as_int(value, 0))


def _hp_values(value: Any) -> tuple[int, int] | None:
    """返回 ``(current, max)``；只给一个数时两者相同。"""
    if isinstance(value, Mapping):
        max_hp = value.get("max")
        if max_hp in (None, ""):
            max_hp = value.get("average")
        if max_hp in (None, ""):
            max_hp = value.get("value")
        current = value.get("current")
        if current in (None, ""):
            current = value.get("hp")
        if max_hp in (None, ""):
            return None
        maximum = max(1, _as_int(max_hp, 1))
        return max(0, min(maximum, _as_int(current, maximum))), maximum
    if value in (None, ""):
        return None
    maximum = max(1, _as_int(value, 1))
    return maximum, maximum


def _speed_feet(value: Any) -> int | None:
    if isinstance(value, Mapping):
        for key in ("walk", "walk_ft", "speed"):
            if value.get(key) not in (None, ""):
                return max(0, _as_int(value.get(key), 0))
        return None
    if value in (None, ""):
        return None
    return max(0, _as_int(value, 0))


def _attack_rows(value: Any) -> list[dict[str, Any]] | None:
    """把剧本攻击列表归一成 5e 攻击表；格式不认识时返回 ``None``。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name") or item.get("title"))
        if not name:
            continue
        kind = _text(item.get("kind")).casefold()
        action_type = _text(item.get("type")).casefold()
        if not kind:
            kind = (
                "ranged"
                if "ranged" in action_type and "melee" not in action_type
                else "melee"
            )
        dice = _text(item.get("damage_dice"))
        damage_type = _text(item.get("damage_type"))
        damage = item.get("damage")
        if not dice:
            if isinstance(damage, str):
                dice = damage
            elif isinstance(damage, list) and damage and isinstance(damage[0], Mapping):
                dice = _text(damage[0].get("dice"))
                damage_type = damage_type or _text(damage[0].get("type"))
            elif isinstance(damage, Mapping):
                dice = _text(damage.get("dice"))
                damage_type = damage_type or _text(damage.get("type"))
        if not dice:
            continue
        rows.append(
            {
                "id": _text(item.get("id")) or name,
                "name": name,
                "kind": kind or "melee",
                "attack_bonus": _as_int(item.get("attack_bonus", item.get("bonus")), 0),
                "damage_bonus": _as_int(item.get("damage_bonus"), 0),
                "damage_dice": dice,
                "damage_type": damage_type or "bludgeoning",
                "versatile_dice": _text(item.get("versatile_dice")),
                "properties": [
                    _text(prop).casefold()
                    for prop in item.get("properties") or []
                    if _text(prop)
                ],
            }
        )
    return rows or None


def _text_list(value: Any) -> list[str]:
    return [_text(item) for item in value or [] if _text(item)]


def _spellcasting_profile(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """模块施法档案 ``{ability, spellSaveDC, spellAttackBonus}``。

    施法属性缺席时不透传（DC/攻击加值无从消费，法术清单仍可用）；
    DC/加值缺失时按 ability 现算的口径由战斗层兜底，这里不做二次推导。
    """
    raw = _first(sources, "spellcasting")
    if not isinstance(raw, Mapping):
        return None
    ability = _text(raw.get("ability")).casefold()
    if not ability:
        return None
    return {
        "ability": ability,
        "spellSaveDC": _as_int(raw.get("spellSaveDC"), 0),
        "spellAttackBonus": _as_int(raw.get("spellAttackBonus"), 0),
    }


def _slot_rows(value: Any) -> dict[str, dict[str, int]] | None:
    """模块法术位表 → ``{"1": {"max", "used"}}``（1-9 环，键外值忽略）。"""
    if not isinstance(value, Mapping) or not value:
        return None
    rows: dict[str, dict[str, int]] = {}
    for key, raw in value.items():
        level = _text(key)
        if not level.isdigit() or not 1 <= int(level) <= 9:
            continue
        maximum = _as_int(raw.get("max"), 0) if isinstance(raw, Mapping) else _as_int(raw, 0)
        if maximum <= 0:
            continue
        used = max(0, min(maximum, _as_int(raw.get("used"), 0) if isinstance(raw, Mapping) else 0))
        rows[level] = {"max": maximum, "used": used}
    return rows or None


def combatant_from_module(module: Mapping[str, Any]) -> dict[str, Any]:
    """按剧本 NPC 模块组装 5e 战斗参与者规格。

    数据包怪物是数值基线，模块字段覆盖基线；没有基线且模块也没有完整的
    显式数值时抛 ``ValueError``，由调用方转换成可读错误。
    """
    if not isinstance(module, Mapping):
        raise ValueError("剧本 NPC 数据无效")
    sources = _module_sources(module)
    reference = _monster_reference(module, sources)
    monster = _pack_entry("monster", reference) if reference else None
    if reference and monster is None:
        raise ValueError(f"数据包 dnd5e-srd 中没有怪物“{reference}”")

    ability_overrides = _normalize_abilities(_first(sources, "abilities"))
    pack_abilities = _normalize_abilities(monster.get("abilities")) if monster is not None else None
    if not ability_overrides and not pack_abilities:
        raise ValueError("剧本 NPC 缺少 abilities，数据包也没有可用的怪物数值")
    abilities = {**(pack_abilities or {}), **(ability_overrides or {})}
    if len(abilities) < len(_ABILITY_KEYS):
        raise ValueError("剧本 NPC 的 abilities 不完整，数据包也没有补全的怪物数值")

    armor = _armor_class(_first(sources, "armorClass", "armor_class", "ac"))
    if armor is None and monster is not None:
        armor = _armor_class(monster.get("ac"))
    if armor is None:
        raise ValueError("剧本 NPC 缺少 armorClass，数据包也没有可用的怪物数值")

    hp_value = _first(sources, "maxHp", "max_hp")
    hp_pair = _hp_values(hp_value) if hp_value is not None else None
    if hp_pair is None:
        hp_pair = _hp_values(_first(sources, "hp"))
    if hp_pair is None and monster is not None:
        hp_pair = _hp_values(monster.get("hp"))
    if hp_pair is None:
        raise ValueError("剧本 NPC 缺少 HP，数据包也没有可用的怪物数值")
    hp, max_hp = hp_pair

    attacks = _attack_rows(_first(sources, "attacks", "weapons"))
    if attacks is None and monster is not None:
        attacks = _monster_attacks(monster)
    if not attacks:
        attacks = [dict(DEFAULT_UNARMED)]

    speed = _speed_feet(_first(sources, "speed_ft", "speed"))
    if speed is None and monster is not None:
        speed = _speed_feet(monster.get("speed"))
    if speed is None:
        speed = 30

    monster_name = _display_name(monster) if monster is not None else ""
    display_name = module_display_name(module)
    spec = {
        "name": display_name,
        "display_name": display_name,
        "monster_id": reference,
        "monster_name": monster_name,
        "module_id": _text(module.get("id")),
        "is_npc": True,
        "side": _text(_first(sources, "side")) or "enemy",
        "abilities": abilities,
        "armorClass": armor,
        "max_hp": max_hp,
        "hp": hp,
        "temp_hp": 0,
        "speed_ft": speed,
        "attacks": attacks,
        "creature_type": (
            _text(_first(sources, "creature_type", "type"))
            or _text((monster or {}).get("type")).casefold()
            or "humanoid"
        ),
        "size": _text(_first(sources, "size")) or _text((monster or {}).get("size")),
        "cr": _text(_first(sources, "cr", "challenge_rating"))
        or _text((monster or {}).get("cr")),
        "xp": _as_int(_first(sources, "xp"), _as_int((monster or {}).get("xp"), 0)),
        "damage_resistances": _text_list(
            _first(sources, "damage_resistances") or (monster or {}).get("damage_resistances")
        ),
        "damage_vulnerabilities": _text_list(
            _first(sources, "damage_vulnerabilities")
            or (monster or {}).get("damage_vulnerabilities")
        ),
        "damage_immunities": _text_list(
            _first(sources, "damage_immunities") or (monster or {}).get("damage_immunities")
        ),
        "source": "dnd5e-srd" if monster is not None else "scenario",
    }
    # 效果引擎：NPC 与角色走同一套视图。模块可以带 ``effect_changes`` 或
    # ``equipment[].effects``（例如「石肤护符」给的 AC/抗性），空视图时
    # 下面的分支一个数都不改。
    effects = effective_sheet(
        {
            "effect_changes": _first(sources, "effect_changes") or [],
            "equipment": _first(sources, "equipment") or [],
            "abilities": abilities,
        }
    )
    if not effects.empty:
        spec["armorClass"] = effects.armor_class(
            _as_int(spec["armorClass"], 10),
            explicit=True,
            dex_mod=ability_modifier(abilities.get("dex", 10)),
        )
        spec["attacks"] = effects.apply_attacks(spec["attacks"])
        spec["damage_resistances"] = effects.extend_traits(spec["damage_resistances"], "dr")
        spec["damage_vulnerabilities"] = effects.extend_traits(spec["damage_vulnerabilities"], "dv")
        spec["damage_immunities"] = effects.extend_traits(spec["damage_immunities"], "di")
        if "spellcasting" in spec and effects.spell_attack_bonus():
            spec["spellcasting"]["spellAttackBonus"] = (
                _as_int(spec["spellcasting"].get("spellAttackBonus"), 0)
                + effects.spell_attack_bonus()
            )
    # 施法档案 / 法术位 / 已知与已准备法术清单：剧本 NPC 模块自带时透传，
    # 由 prepare_combatant_from_module 落到战斗者上（核心 spells 管线消费
    # ``spellcasting.spellSaveDC``、``spell_slots``、``spells_known/prepared``）。
    spell_profile = _spellcasting_profile(sources)
    if spell_profile is not None:
        spec["spellcasting"] = spell_profile
    slot_rows = _slot_rows(_first(sources, "spell_slots"))
    if slot_rows is not None:
        spec["spell_slots"] = slot_rows
    known = _text_list(_first(sources, "spells_known"))
    if known:
        spec["spells_known"] = known
    prepared = _text_list(_first(sources, "spells_prepared"))
    if prepared:
        spec["spells_prepared"] = prepared
    return spec


__all__ = ["combatant_from_module", "module_display_name"]
