"""D&D 5e 角色卡 schema。

存储形态沿用平台约定：公共信封 + ``sheet``。本文件只负责把 5e 必需字段
整理成稳定结构、补齐派生值并给出告警，不负责落盘。
"""

from __future__ import annotations

from typing import Any, Mapping

from dnd5e_plugin.effect_changes import EffectiveSheet, effective_sheet
from dnd5e_plugin.rules import (
    ABILITY_KEYS,
    ability_key,
    ability_modifier,
    ability_score,
    check_bonus,
    parse_check,
    proficiency_bonus,
    sheet_of,
)

ENVELOPE_FIELDS = (
    "id",
    "public_id",
    "publisher_id",
    "publisher_name",
    "avatar",
    "name",
    "playerId",
    "playerName",
    "isEditable",
    "createdAt",
    "updatedAt",
)

SHEET_FIELDS = (
    "level",
    "class",
    "subclass",
    "race",
    "background",
    "alignment",
    "abilities",
    "proficiencies",
    "hp",
    "armorClass",
    "speed",
    "initiative",
    "hitDice",
    "equipment",
    "features",
    "spellcasting",
    "notes",
    "xp",
    # 效果引擎载体（FVTT transfer buff）：声明进字段表，前端/校验侧与
    # ``pack()`` 的顶层回填都按同一份口径认这个键。
    "effect_changes",
)

DERIVED_KEYS = (
    "maxHp",
    "armorClass",
    "proficiencyBonus",
    "initiative",
    "passivePerception",
    "spellSaveDC",
    "spellAttackBonus",
)

CARD_DERIVED_KEYS = DERIVED_KEYS

SECTIONS = (
    {
        "key": "identity",
        "title_i18n": "sheet.dnd5e.identity",
        "layout": "grid",
        "fields": [
            {"key": "level", "type": "integer", "label_i18n": "sheet.dnd5e.level", "min": 1, "max": 20, "required": True},
            {"key": "class", "type": "string", "label_i18n": "sheet.dnd5e.class", "required": True},
            {"key": "subclass", "type": "string", "label_i18n": "sheet.dnd5e.subclass"},
            {"key": "race", "type": "string", "label_i18n": "sheet.dnd5e.race"},
            {"key": "background", "type": "string", "label_i18n": "sheet.dnd5e.background"},
            {"key": "alignment", "type": "string", "label_i18n": "sheet.dnd5e.alignment"},
            {"key": "xp", "type": "integer", "label_i18n": "sheet.dnd5e.xp", "min": 0},
        ],
    },
    {
        "key": "abilities",
        "title_i18n": "sheet.dnd5e.abilities",
        "layout": "grid",
        "fields": [
            {"key": key, "type": "integer", "label_i18n": f"sheet.dnd5e.ability.{key}", "min": 1, "max": 30}
            for key in ABILITY_KEYS
        ],
    },
    {
        "key": "proficiencies",
        "title_i18n": "sheet.dnd5e.proficiencies",
        "layout": "grid",
        "fields": [
            {"key": "saves", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.saves"},
            {"key": "skills", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.skills"},
            {"key": "expertise", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.expertise"},
            {"key": "armor", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.armor"},
            {"key": "weapons", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.weapons"},
            {"key": "tools", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.tools"},
            {"key": "languages", "type": "string_list", "label_i18n": "sheet.dnd5e.proficiencies.languages"},
        ],
    },
    {
        "key": "combat",
        "title_i18n": "sheet.dnd5e.combat",
        "layout": "grid",
        "fields": [
            {"key": "hp", "type": "resource", "label_i18n": "sheet.dnd5e.hp"},
            {"key": "armorClass", "type": "derived", "label_i18n": "sheet.dnd5e.armor_class", "read_only": True},
            {"key": "speed", "type": "integer", "label_i18n": "sheet.dnd5e.speed", "min": 0},
            {"key": "initiative", "type": "derived", "label_i18n": "sheet.dnd5e.initiative", "read_only": True},
            {"key": "hitDice", "type": "string", "label_i18n": "sheet.dnd5e.hit_dice"},
        ],
    },
    {
        "key": "spellcasting",
        "title_i18n": "sheet.dnd5e.spellcasting",
        "layout": "grid",
        "fields": [
            {"key": "spellcasting", "type": "json", "label_i18n": "sheet.dnd5e.spellcasting_profile"},
            {"key": "spellSaveDC", "type": "derived", "label_i18n": "sheet.dnd5e.spell_save_dc", "read_only": True},
            {"key": "spellAttackBonus", "type": "derived", "label_i18n": "sheet.dnd5e.spell_attack", "read_only": True},
        ],
    },
    {
        "key": "inventory",
        "title_i18n": "sheet.dnd5e.inventory",
        "layout": "list",
        "fields": [
            {"key": "equipment", "type": "table", "label_i18n": "sheet.dnd5e.equipment"},
            {"key": "features", "type": "string_list", "label_i18n": "sheet.dnd5e.features"},
            {"key": "notes", "type": "text", "label_i18n": "sheet.dnd5e.notes"},
        ],
    },
)

_RESOURCE_SLOTS = (
    {
        "kind": "hp",
        "state": "current_hp",
        "mirror": "currentHp",
        "mirror_aliases": ("current_hp",),
        "cap_state": "max_hp",
        "cap_derived": "maxHp",
        "cap_min": 0,
        # HP 归零是 5e 的濒死触发点；核心只报事件，状态转换仍由 combat 处理。
        "thresholds": [{"at": 0, "when": "below", "event": "zero_hp"}],
    },
)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bounded(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    number = _as_int(value, fallback)
    return max(minimum, min(maximum, number))


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _normalize_abilities(value: Any) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, int] = {}
    for raw_key, raw_value in source.items():
        key = ability_key(raw_key)
        if key and key not in result:
            result[key] = _bounded(raw_value, 1, 30, 10)
    for key in ABILITY_KEYS:
        result.setdefault(key, 10)
    return result


def _normalize_proficiencies(value: Any) -> dict[str, list[str]]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, list[str]] = {}
    for key in ("saves", "skills", "expertise", "armor", "weapons", "tools", "languages"):
        result[key] = _strings(source.get(key))
    # 兼容常见的复数写法。
    if not result["saves"]:
        result["saves"] = _strings(source.get("savingThrows"))
    return result


def _normalize_hp(value: Any, stored: Mapping[str, Any]) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    maximum = max(
        1,
        _as_int(
            source.get("max", source.get("maxHp", stored.get("maxHp", stored.get("max_hp")))),
            1,
        ),
    )
    current = _bounded(
        source.get("current", source.get("currentHp", stored.get("currentHp", stored.get("current_hp")))),
        0,
        maximum,
        maximum,
    )
    temporary = max(0, _as_int(source.get("temp", source.get("tempHp", stored.get("tempHp"))), 0))
    return {"current": current, "max": maximum, "temp": temporary}


def _armor_class(sheet: Mapping[str, Any]) -> int:
    explicit = sheet.get("armorClass")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass
    return 10 + ability_modifier(ability_score(sheet, "dex"))


def _dex_modifier(sheet: Mapping[str, Any]) -> int:
    return ability_modifier(ability_score(sheet, "dex"))


def _effective_armor_class(sheet: Mapping[str, Any], effects: EffectiveSheet | None = None) -> int:
    """基础 AC → 效果引擎处理后的 AC（无效果时与原实现完全一致）。"""
    view = effects if effects is not None else effective_sheet(sheet)
    base = _armor_class(sheet)
    if view.empty:
        return base
    return view.armor_class(base, explicit=sheet.get("armorClass") is not None, dex_mod=_dex_modifier(sheet))


def _spellcasting(sheet: Mapping[str, Any], effects: EffectiveSheet | None = None) -> dict[str, Any]:
    profile = sheet.get("spellcasting")
    profile = dict(profile) if isinstance(profile, Mapping) else {}
    ability = ability_key(profile.get("ability"))
    if not ability:
        return {}
    bonus = proficiency_bonus(sheet.get("level", 1)) + ability_modifier(ability_score(sheet, ability))
    dc_bonus = 0
    if effects is not None and not effects.empty:
        bonus += effects.spell_attack_bonus()
        dc_bonus = effects.spell_save_dc_bonus()
    return {
        "ability": ability,
        "spellSaveDC": 8 + bonus + dc_bonus,
        "spellAttackBonus": bonus,
    }


class Dnd5eCharacterSchema:
    """5e 角色卡结构声明、打包与派生。"""

    ruleset_id = "dnd5e"
    ruleset_version = "5.1"
    schema_version = 1
    envelope_fields = ENVELOPE_FIELDS
    sheet_fields = SHEET_FIELDS
    attribute_keys = ABILITY_KEYS
    resource_keys = ("hp",)
    derived_keys = DERIVED_KEYS
    sections = SECTIONS

    def status_groups(self) -> list[dict[str, Any]]:
        return []

    def extension_namespaces(self) -> tuple[str, ...]:
        return ()

    def default_extensions(self) -> dict[str, dict[str, Any]]:
        return {}

    def normalize_extensions(self, raw: Any) -> dict[str, Any]:
        return dict(raw) if isinstance(raw, Mapping) else {}

    def derived_card_keys(self) -> tuple[str, ...]:
        return CARD_DERIVED_KEYS

    def resource_slots(self) -> list[dict[str, Any]]:
        return [dict(slot) for slot in _RESOURCE_SLOTS]

    def pack(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        """整理成信封 + sheet，并把房间结算需要的值镜像到顶层。"""
        data = dict(stored or {})
        raw_sheet = data.get("sheet")
        sheet = dict(raw_sheet) if isinstance(raw_sheet, Mapping) else {}
        for key in SHEET_FIELDS:
            if key not in sheet and key in data:
                sheet[key] = data[key]
        # 效果引擎的载体：``effect_changes`` 已在 SHEET_FIELDS 里，写在顶层
        # （导入卡/手工补丁）时上面那轮回填已并进 sheet；这里只做形态归一，
        # 非列表一律视为「没有效果」，不让脏数据进派生计算。
        if "effect_changes" in sheet and not isinstance(sheet["effect_changes"], list):
            sheet.pop("effect_changes")
        sheet["level"] = _bounded(sheet.get("level", data.get("level")), 1, 20, 1)
        sheet["abilities"] = _normalize_abilities(
            sheet.get("abilities") or sheet.get("attributes") or data.get("abilities")
        )
        sheet["proficiencies"] = _normalize_proficiencies(sheet.get("proficiencies"))
        sheet["hp"] = _normalize_hp(sheet.get("hp"), {**data, **sheet})
        sheet["speed"] = max(0, _as_int(sheet.get("speed"), 30))
        sheet["xp"] = max(0, _as_int(sheet.get("xp"), 0))
        # 效果引擎的计算视图：先取视图，再写 ``sheet.armorClass``——镜像里
        # 存的始终是**基础值**（含护甲），效果加值只进顶层派生镜像，
        # 否则下次 pack 会把加值当基础值再叠一次。
        effects = effective_sheet(sheet)
        sheet["armorClass"] = _armor_class(sheet)
        sheet.setdefault("equipment", [])
        sheet.setdefault("features", [])
        if not isinstance(sheet.get("equipment"), list):
            sheet["equipment"] = []
        # 首次打包时按职业表补一份法术位，卡面无需等第一次施法才出现法术位。
        # 已存在的 slots（含 used）优先保留，避免覆盖房间内消耗。
        from dnd5e_plugin.spells import spell_slots_from_sheet

        slots = spell_slots_from_sheet(sheet)
        if slots:
            casting = dict(sheet.get("spellcasting") or {}) if isinstance(sheet.get("spellcasting"), Mapping) else {}
            existing = casting.get("slots")
            if not isinstance(existing, Mapping) or not existing:
                casting["slots"] = slots
            sheet["spellcasting"] = casting

        data["rulesetId"] = self.ruleset_id
        data["rulesetVersion"] = self.ruleset_version
        data["rulesetSchemaVersion"] = self.schema_version
        data["sheet"] = sheet

        hp = sheet["hp"]
        data["level"] = sheet["level"]
        data["class"] = str(sheet.get("class") or "")
        data["race"] = str(sheet.get("race") or "")
        data["maxHp"] = hp["max"]
        data["currentHp"] = hp["current"]
        data["tempHp"] = hp["temp"]
        data["armorClass"] = _effective_armor_class(sheet, effects)
        data["proficiencyBonus"] = proficiency_bonus(sheet["level"])
        data["initiative"] = ability_modifier(ability_score(sheet, "dex")) + effects.initiative_bonus
        data["passivePerception"] = self._passive_perception(sheet, effects)
        spell = _spellcasting(sheet, effects)
        if spell:
            data["spellSaveDC"] = spell["spellSaveDC"]
            data["spellAttackBonus"] = spell["spellAttackBonus"]
        return data

    def validate(self, sheet: Mapping[str, Any]) -> list[str]:
        data = sheet if isinstance(sheet, Mapping) else {}
        warnings: list[str] = []
        level = _as_int(data.get("level"), 0)
        if not 1 <= level <= 20:
            warnings.append("level must be between 1 and 20")
        abilities = data.get("abilities")
        if not isinstance(abilities, Mapping):
            warnings.append("missing abilities")
        else:
            for key in ABILITY_KEYS:
                if key not in abilities:
                    warnings.append(f"missing ability: {key}")
        hp = data.get("hp")
        if not isinstance(hp, Mapping):
            warnings.append("missing hp")
        return warnings

    def derive(self, sheet: Mapping[str, Any]) -> dict[str, Any]:
        """重算派生值；输入可以是完整卡，也可以只是 sheet。"""
        data = dict(sheet or {})
        source = sheet_of(data)
        level = _bounded(source.get("level"), 1, 20, 1)
        result = dict(data)
        effects = effective_sheet(source)
        result["maxHp"] = _normalize_hp(source.get("hp"), source)["max"]
        result["armorClass"] = _effective_armor_class(source, effects)
        result["proficiencyBonus"] = proficiency_bonus(level)
        result["initiative"] = ability_modifier(ability_score(source, "dex")) + effects.initiative_bonus
        result["passivePerception"] = self._passive_perception(source, effects)
        result["attributeCap"] = 30
        spell = _spellcasting(source, effects)
        if spell:
            result["spellSaveDC"] = spell["spellSaveDC"]
            result["spellAttackBonus"] = spell["spellAttackBonus"]
        return result

    @staticmethod
    def _passive_perception(sheet: Mapping[str, Any], effects: EffectiveSheet | None = None) -> int:
        parsed = parse_check("perception") or {"kind": "skill", "skill": "perception", "ability": "wis"}
        bonus, _detail = check_bonus(sheet, parsed)
        if effects is not None and not effects.empty:
            bonus += effects.check_bonus(parsed.get("ability"), parsed.get("skill"))
        return 10 + bonus
