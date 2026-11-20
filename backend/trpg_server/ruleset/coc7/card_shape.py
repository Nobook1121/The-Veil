"""CoC7 旧卡形状（test 卡）与运行时卡的双向转换（C1 下沉）。

历史背景：本模块整块来自 ``routes/characters.py`` 的形状转换块
（``_is_test_character_shape`` / ``_runtime_to_test_character`` /
``_test_character_to_runtime`` 及其全部私有助手），按 C1 批次下沉为
coc7 插件自有能力。核心侧只保留信封外壳，经 ``ruleset.capabilities``
按卡所属规则集分派；未声明该能力的规则集（如 dnd5e）原样透传，
不再被 CoC 形状改写。

与原实现的两处刻意差异（均为 C1 设计内变更）：

- ``rulesetId`` 缺省值由「站点默认规则集」改为本插件自身 id（``coc7``）。
  核心外壳只会在解析出 coc7 之后才进入本模块，因此缺省值场景等价。
- 技能键查询直接读 ``Coc7Catalogs().skills()``（同源同值，
  ``_app_override`` 保留测试注入路径），不再绕核心目录能力层。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 本插件自身 id：旧卡缺 rulesetId 时的缺省归属。
RULESET_ID = "coc7"

SKILL_GROUP_CATEGORIES = {
    "special": "特殊",
    "explore": "探索",
    "social": "社交",
    "combat": "战斗",
    "medical": "医疗",
    "move": "运动",
    "knowledge": "知识",
    "tech": "技术",
    "drive": "操纵",
    "other": "其他",
}
CATEGORY_SKILL_GROUPS = {value: key for key, value in SKILL_GROUP_CATEGORIES.items()}
STATUS_BODY_FIELDS = {
    "majorWound": "重伤",
    "unconscious": "昏迷",
    "dead": "死亡",
}
STATUS_MENTAL_FIELDS = {
    "indefiniteInsanity": "不定期疯狂",
    "permanentInsanity": "永久疯狂",
    "temporaryInsanity": "临时疯狂",
}


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _as_text(value, fallback=""):
    if value is None:
        return fallback
    return str(value)


def _as_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _json_list_text(items):
    return json.dumps(items if isinstance(items, list) else [], ensure_ascii=False)


def _slug(value):
    slug = re.sub(r"\s+", "-", str(value or "").strip().lower())
    slug = re.sub(r"[^a-z0-9\-\u4e00-\u9fff]", "", slug)
    return slug or "skill"


def is_legacy_shape(character: Any) -> bool:
    """检测存储形态是否为旧卡（test 卡）形状。"""
    if not isinstance(character, dict):
        return False
    return any(
        key in character
        for key in (
            "deriveAttributes",
            "battleAttributes",
            "characterStatus",
            "skillGroups",
            "stories",
            "experiencedModules",
            "friends",
        )
    )


def _attribute_value(attributes, upper_key, lower_key, fallback=50):
    return _as_int(attributes.get(upper_key, attributes.get(lower_key)), fallback)


def _equipment_to_items_text(equipment):
    if isinstance(equipment, str):
        return equipment
    lines = []
    for item in _as_list(equipment):
        if isinstance(item, str):
            if item.strip():
                lines.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("name")).strip()
        if not name:
            continue
        quantity = _as_int(item.get("quantity"), 1)
        notes = _as_text(item.get("notes")).strip()
        line = f"{name} x{max(1, quantity)}"
        if notes:
            line = f"{line}：{notes}"
        lines.append(line)
    return "\n".join(lines)


def _items_text_to_equipment(items_text):
    text = _as_text(items_text).strip()
    if not text:
        return []
    return [
        {"name": line.strip(), "quantity": 1, "weight": 0, "notes": ""}
        for line in re.split(r"[\r\n;；]+", text)
        if line.strip()
    ]


def _skill_group_key(skill):
    category = _as_text(skill.get("category")).strip()
    if category in SKILL_GROUP_CATEGORIES:
        return category
    return CATEGORY_SKILL_GROUPS.get(category, "other")


def _skill_to_test_item(skill):
    occupation_points = _as_int(skill.get("occupationPoints"), 0)
    interest_points = _as_int(skill.get("interestPoints"), 0)
    growth_points = _as_int(skill.get("growthPoints"), 0)
    name = _as_text(skill.get("name"), "未命名技能")
    item = {
        "name": name,
        "base": _as_int(skill.get("base"), 0),
        "job": occupation_points,
        "interest": interest_points,
        "growth": growth_points,
        "isProfessional": bool(skill.get("occupation") or skill.get("checked")),
    }
    specialty_key = _as_text(skill.get("specialtyKey"), "").strip()
    if specialty_key:
        item["specialtyKey"] = specialty_key
    return item


def _skill_from_test_item(item, group_key, index):
    legacy_base = _as_int(item.get("base"), 0)
    occupation_points = _as_int(item.get("job"), 0)
    interest_points = _as_int(item.get("interest"), 0)
    growth_points = _as_int(item.get("growth"), 0)
    value = _as_int(item.get("value"), legacy_base + occupation_points + interest_points + growth_points)
    name = _as_text(item.get("name"), "未命名技能")
    skill_key = skill_key_from_name(name, _as_text(item.get("skillKey") or item.get("id") or _slug(name)))
    skill = {
        "id": skill_key if index == 0 else f"{skill_key}__{index + 1}",
        "skillKey": skill_key,
        "name": name,
        "base": legacy_base,
        "value": value,
        "category": _as_text(item.get("category") or SKILL_GROUP_CATEGORIES.get(group_key, "其他")),
        "checked": bool(item.get("checked") if "checked" in item else item.get("isProfessional")),
        "occupation": bool(item.get("isProfessional")),
        "occupationPoints": occupation_points,
        "interestPoints": interest_points,
        "growthPoints": growth_points,
    }
    specialty_key = _as_text(item.get("specialtyKey"), "").strip()
    if specialty_key:
        skill["specialtyKey"] = specialty_key
    return skill


def _skills_to_test_groups(skills):
    groups = {key: [] for key in SKILL_GROUP_CATEGORIES}
    for skill in _as_list(skills):
        if isinstance(skill, dict):
            groups.setdefault(_skill_group_key(skill), []).append(_skill_to_test_item(skill))
    return groups


def _skills_from_test_groups(groups):
    skills = []
    if not isinstance(groups, dict):
        return skills
    occurrences = {}
    for group_key, items in groups.items():
        for item in _as_list(items):
            if isinstance(item, dict):
                name = _as_text(item.get("name"))
                skill_key = skill_key_from_name(name, _as_text(item.get("skillKey") or item.get("id") or _slug(name)))
                occurrence = occurrences.get(skill_key, 0)
                occurrences[skill_key] = occurrence + 1
                skills.append(_skill_from_test_item(item, group_key, occurrence))
    return skills


def _weapon_to_test_item(weapon):
    name = _as_text(weapon.get("name")).strip()
    if not name or name in {"选择武器", "未选择武器", "未命名武器"}:
        return {
            "name": "",
            "skill": "",
            "damage": "",
            "range": "",
            "round": "",
            "tho": "",
            "num": "",
            "err": "",
            "weight": "",
            "note": "",
        }
    item = {
        "name": name,
        "skill": _as_text(weapon.get("skill")),
        "damage": _as_text(weapon.get("damage")),
        "range": _as_text(weapon.get("range")),
        "round": _as_text(weapon.get("attacks") or weapon.get("round")),
        "tho": _as_text(weapon.get("tho")),
        "num": _as_text(weapon.get("ammo") or weapon.get("num")),
        "err": _as_text(weapon.get("malfunction") or weapon.get("err")),
        "weight": _as_text(weapon.get("weight")),
        "note": _as_text(weapon.get("note")),
    }
    return item


def _weapon_from_test_item(weapon):
    name = _as_text(weapon.get("name"), "").strip()
    if not name or name in {"选择武器", "未选择武器", "未命名武器"}:
        return {
            "name": "",
            "skill": "",
            "damage": "",
            "range": "",
            "attacks": "",
            "ammo": "",
            "malfunction": "",
            "weight": "",
            "note": "",
        }
    return {
        "name": name,
        "skill": _as_text(weapon.get("skill"), ""),
        "damage": _as_text(weapon.get("damage"), ""),
        "range": _as_text(weapon.get("range"), ""),
        "attacks": _as_text(weapon.get("attacks") or weapon.get("round"), ""),
        "ammo": _as_text(weapon.get("ammo") or weapon.get("num"), ""),
        "malfunction": _as_text(weapon.get("malfunction") or weapon.get("err"), ""),
        "weight": _as_text(weapon.get("weight"), ""),
        "note": _as_text(weapon.get("note"), ""),
    }


def skill_key_from_name(name, fallback="") -> str:
    """技能名 → 技能键；目录由本插件 ``Coc7Catalogs`` 提供。

    目录读取失败（数据文件缺失 / 解析异常）时保留调用方兜底值。
    """
    from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs

    try:
        catalog = Coc7Catalogs().skills()
    except Exception:  # noqa: BLE001 - 目录缺失时保留调用方兜底值
        return fallback
    if not isinstance(catalog, dict):
        return fallback
    skills = catalog.get("skills")
    if not isinstance(skills, list):
        return fallback
    locales = catalog.get("locales")
    default_locale = _as_text(catalog.get("defaultLocale"), "zh-CN")
    labels = locales.get(default_locale) if isinstance(locales, dict) else {}
    if not isinstance(labels, dict):
        labels = {}
    lookup = {str(value): str(item.get("key") or "") for item in skills if isinstance(item, dict) for value in [labels.get(_as_text(item.get("labelKey"))), item.get("key")]}
    return _as_text(lookup.get(str(name)), fallback)


def runtime_to_legacy(character):
    """运行时卡 → 旧卡（test 卡）形状；era/maxSan/age 缺省随历史口径。"""
    attributes = _as_dict(character.get("attributes"))
    background = _as_dict(character.get("background"))
    assets = _as_dict(character.get("assets"))
    status = _as_dict(character.get("status"))
    now_text = _as_text(character.get("updatedAt") or character.get("createdAt"))
    # 规则扩展命名空间（如 extensions.mental）：旧卡没有时保持缺省，不写空键。
    extensions = _as_dict(character.get("extensions"))
    if not extensions:
        extensions = _as_dict(_as_dict(character.get("sheet")).get("extensions"))
    test = {
        "id": _as_text(character.get("id")),
        "rulesetId": _as_text(character.get("rulesetId"), RULESET_ID),
        "public_id": _as_text(character.get("public_id")),
        "publisher_id": character.get("publisher_id"),
        "publisher_name": _as_text(character.get("publisher_name")),
        "avatar": _as_text(character.get("avatar")),
        "name": _as_text(character.get("name")),
        "playerId": _as_text(character.get("playerId")),
        "playerName": _as_text(character.get("playerName")),
        "time": _as_text(character.get("era"), "1920s"),
        "job": _as_text(character.get("occupationName") or character.get("job")),
        "age": _as_text(character.get("age") or attributes.get("AGE")),
        "gender": _as_text(character.get("gender")),
        "location": _as_text(character.get("residence")),
        "hometown": _as_text(character.get("birthplace")),
        "attributes": {
            "str": _attribute_value(attributes, "STR", "str"),
            "dex": _attribute_value(attributes, "DEX", "dex"),
            "con": _attribute_value(attributes, "CON", "con"),
            "app": _attribute_value(attributes, "APP", "app"),
            "pow": _attribute_value(attributes, "POW", "pow"),
            "siz": _attribute_value(attributes, "SIZ", "siz"),
            "edu": _attribute_value(attributes, "EDU", "edu"),
            "int": _attribute_value(attributes, "INT", "int"),
            "luc": _attribute_value(attributes, "LUC", "luc"),
        },
        "deriveAttributes": {
            "sanity": {
                "current": _as_text(character.get("currentSan"), "0"),
                "start": _as_text(character.get("initialSan"), _as_text(character.get("currentSan"), "0")),
                "max": _as_text(character.get("maxSan"), "99"),
            },
            "hp": {
                "current": _as_text(character.get("currentHp"), "0"),
                "max": _as_text(character.get("maxHp"), "0"),
            },
            "mp": {
                "current": _as_text(character.get("currentMp") or character.get("magicPoints"), "0"),
                "max": _as_text(character.get("maxMp"), "0"),
            },
        },
        "battleAttributes": {
            "db": _as_text(character.get("damageBonus")),
            "build": _as_text(character.get("build")),
            "mov": _as_text(character.get("mov")),
            "movNote": _as_text(character.get("movNote")),
            "armor": _as_text(character.get("armor")),
        },
        "characterStatus": {
            "bodyStates": {label: bool(status.get(key)) for key, label in STATUS_BODY_FIELDS.items()},
            "mentalStates": {label: bool(status.get(key)) for key, label in STATUS_MENTAL_FIELDS.items()},
        },
        "pointValues": character.get("pointValues", {}),
        "proSkills": character.get("proSkills", []),
        "skillPoints": character.get("skillPoints", []),
        "weapons": [_weapon_to_test_item(weapon) for weapon in _as_list(character.get("weapons")) if isinstance(weapon, dict)],
        "stories": {
            "app": _as_text(background.get("appearance")),
            "belief": _as_text(background.get("ideology")),
            "IPerson": _as_text(background.get("significantPeople")),
            "IPlace": _as_text(background.get("meaningfulLocations")),
            "IItem": _as_text(background.get("treasuredPossessions")),
            "trait": _as_text(background.get("traits")),
            "scar": _as_text(background.get("injuriesScars")),
            "mad": _as_text(background.get("phobiasManias")),
            "desc": _as_text(background.get("story")),
        },
        "assets": {
            "cash": _as_text(assets.get("cash"), "0"),
            "consumption": _as_text(assets.get("spendingLevel"), "0"),
            "assets": _as_text(assets.get("assetsText")),
            "items": _equipment_to_items_text(character.get("equipment")),
            "magicItems": _as_text(background.get("arcaneTomes")),
            "magics": _as_text(background.get("spells")),
            "touches": _as_text(background.get("encounters")),
        },
        "experiencedModules": _json_list_text(
            [
                {"name": _as_text(item.get("name")), "experience": _as_text(item.get("experience"))}
                for item in _as_list(character.get("experiencedScenarios"))
                if isinstance(item, dict)
            ]
        ),
        "friends": _json_list_text(
            [
                {
                    "character": _as_text(item.get("name")),
                    "relationship": _as_text(item.get("description")),
                    "player": _as_text(item.get("player")),
                }
                for item in _as_list(character.get("relationships"))
                if isinstance(item, dict)
            ]
        ),
        "skillGroups": _skills_to_test_groups(character.get("skills")),
        "isEditable": bool(character.get("isEditable", True)),
        "createdAt": _as_text(character.get("createdAt") or now_text),
        "updatedAt": now_text,
    }
    if extensions:
        test["extensions"] = extensions
    return test


def legacy_to_runtime(character, fallback_id=""):
    """旧卡（test 卡）形状 → 运行时卡；maxSan 默认 99、age 默认 25、era 默认 1920s。"""
    attributes = _as_dict(character.get("attributes"))
    derived = _as_dict(character.get("deriveAttributes"))
    sanity = _as_dict(derived.get("sanity"))
    hp = _as_dict(derived.get("hp"))
    mp = _as_dict(derived.get("mp"))
    battle = _as_dict(character.get("battleAttributes"))
    status = _as_dict(character.get("characterStatus"))
    body_states = _as_dict(status.get("bodyStates"))
    mental_states = _as_dict(status.get("mentalStates"))
    stories = _as_dict(character.get("stories"))
    assets = _as_dict(character.get("assets"))
    age = _as_int(character.get("age"), _as_int(attributes.get("age"), 25))
    extensions = _as_dict(character.get("extensions")) or _as_dict(_as_dict(character.get("sheet")).get("extensions"))
    runtime = {
        "id": _as_text(character.get("id") or fallback_id),
        "rulesetId": _as_text(character.get("rulesetId"), RULESET_ID),
        "public_id": _as_text(character.get("public_id")),
        "publisher_id": character.get("publisher_id"),
        "publisher_name": _as_text(character.get("publisher_name")),
        "avatar": _as_text(character.get("avatar")),
        "name": _as_text(character.get("name"), "未命名角色卡"),
        "playerId": _as_text(character.get("playerId") or character.get("playerName")),
        "playerName": _as_text(character.get("playerName")),
        "era": _as_text(character.get("time"), "1920s"),
        "gender": _as_text(character.get("gender")),
        "age": age,
        "occupationName": _as_text(character.get("job")),
        "residence": _as_text(character.get("location")),
        "birthplace": _as_text(character.get("hometown")),
        "attributes": {
            "STR": _attribute_value(attributes, "STR", "str"),
            "DEX": _attribute_value(attributes, "DEX", "dex"),
            "CON": _attribute_value(attributes, "CON", "con"),
            "APP": _attribute_value(attributes, "APP", "app"),
            "POW": _attribute_value(attributes, "POW", "pow"),
            "SIZ": _attribute_value(attributes, "SIZ", "siz"),
            "EDU": _attribute_value(attributes, "EDU", "edu"),
            "INT": _attribute_value(attributes, "INT", "int"),
            "LUC": _attribute_value(attributes, "LUC", "luc"),
            "AGE": age,
        },
        "maxHp": _as_int(hp.get("max"), 0),
        "currentHp": _as_int(hp.get("current"), _as_int(hp.get("max"), 0)),
        "maxSan": _as_int(sanity.get("max"), 99),
        "initialSan": _as_int(sanity.get("start"), _as_int(sanity.get("current"), 0)),
        "currentSan": _as_int(sanity.get("current"), 0),
        "maxMp": _as_int(mp.get("max"), 0),
        "currentMp": _as_int(mp.get("current"), _as_int(mp.get("max"), 0)),
        "magicPoints": _as_int(mp.get("current"), _as_int(mp.get("max"), 0)),
        "damageBonus": _as_text(battle.get("db")),
        "build": _as_int(battle.get("build"), 0),
        "mov": _as_int(battle.get("mov"), 0),
        "armor": _as_int(battle.get("armor"), 0),
        "status": {
            key: bool(body_states.get(label))
            for key, label in STATUS_BODY_FIELDS.items()
        } | {
            key: bool(mental_states.get(label))
            for key, label in STATUS_MENTAL_FIELDS.items()
        },
        "skills": _skills_from_test_groups(character.get("skillGroups")),
        "weapons": [
            _weapon_from_test_item(weapon)
            for weapon in _as_list(character.get("weapons"))
            if isinstance(weapon, dict)
        ],
        "equipment": _items_text_to_equipment(assets.get("items")),
        "assets": {
            "cash": _as_int(assets.get("cash"), 0),
            "spendingLevel": _as_int(assets.get("consumption"), 0),
            "assetsText": _as_text(assets.get("assets")),
        },
        "background": {
            "appearance": _as_text(stories.get("app")),
            "ideology": _as_text(stories.get("belief")),
            "significantPeople": _as_text(stories.get("IPerson")),
            "meaningfulLocations": _as_text(stories.get("IPlace")),
            "treasuredPossessions": _as_text(stories.get("IItem")),
            "traits": _as_text(stories.get("trait")),
            "injuriesScars": _as_text(stories.get("scar")),
            "phobiasManias": _as_text(stories.get("mad")),
            "story": _as_text(stories.get("desc")),
            "arcaneTomes": _as_text(assets.get("magicItems")),
            "spells": _as_text(assets.get("magics")),
            "encounters": _as_text(assets.get("touches")),
        },
        "relationships": [
            {
                "name": _as_text(item.get("character")),
                "description": _as_text(item.get("relationship")),
                "player": _as_text(item.get("player")),
            }
            for item in _json_list(character.get("friends"))
            if isinstance(item, dict)
        ],
        "experiencedScenarios": [
            {"name": _as_text(item.get("name")), "experience": _as_text(item.get("experience"))}
            for item in _json_list(character.get("experiencedModules"))
            if isinstance(item, dict)
        ],
        "createdAt": _as_text(character.get("createdAt")),
        "updatedAt": _as_text(character.get("updatedAt")),
        # 规则自有卡面快照；前端优先按 sheet 渲染，顶层字段仅作镜像兜底。
        "sheet": _as_dict(character.get("sheet")),
    }
    if extensions:
        runtime["extensions"] = extensions
    return runtime
