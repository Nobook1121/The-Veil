import json
import logging
import re
import secrets
import string
import time
from pathlib import Path

from flask import Blueprint, current_app, request, session

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server import character_card_io, character_service, room_ai_changes, weapon_catalog
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response, success_response
from trpg_server.ruleset import builtin_registry, default_ruleset_id, get_plugin
from trpg_server.ruleset.capabilities import catalog_items, catalog_value, clamp_resource_state, derive_card, pack_card
from trpg_server.security import normalize_filename, safe_join
from trpg_server.settings import CHARACTER_GALLERY_DIR, CHARACTERS_DIR, ROOMS_DIR

bp = Blueprint("characters", __name__)
logger = logging.getLogger(__name__)

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


def _get_characters_dir():
    return current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR)


def _get_character_gallery_dir():
    return current_app.config.get("CHARACTER_GALLERY_DIR", CHARACTER_GALLERY_DIR)


def _get_rooms_dir():
    return current_app.config.get("ROOMS_DIR", ROOMS_DIR)


def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _is_elevated():
    config_dir = current_app.config.get("CONFIG_DIR")
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(session.get("role", "USER"), "characters.manage_all", config_path)


def _can_use_permission(node_id):
    config_dir = current_app.config.get("CONFIG_DIR")
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(session.get("role", "USER"), node_id, config_path)


def _current_player_ids():
    return {str(session.get("user_id", "")), str(session.get("username", ""))}


def character_service_actor():
    """把当前会话转换成领域服务需要的 Actor（身份只来自服务端）。"""
    from trpg_server.site_agent_auth import Actor, AgentAuthError, build_actor, permission_set_for_role

    try:
        return build_actor()
    except AgentAuthError:
        role = str(session.get("role") or "USER").upper()
        return Actor(
            user_id=int(session.get("user_id") or 0),
            username=str(session.get("username") or ""),
            role=role,
            permissions=permission_set_for_role(role),
            room_ids=frozenset(),
        )


def _iter_character_files():
    """新目录结构 ``<id>/character.json`` 与旧平铺 ``<id>.json`` 都兼容。"""
    characters_dir = _get_characters_dir()
    if not characters_dir.exists():
        return []
    paths = []
    for item in sorted(characters_dir.iterdir()):
        if item.is_dir():
            card_path = item / "character.json"
            if card_path.exists():
                paths.append(card_path)
    paths.extend(sorted(characters_dir.glob("*.json")))
    return paths


def _character_id_from_path(path):
    return path.parent.name if path.name == "character.json" else path.stem


def _iter_gallery_character_files():
    gallery_dir = _get_character_gallery_dir()
    if not gallery_dir.exists():
        return []
    return sorted(gallery_dir.glob("*.json"))


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


def _is_test_character_shape(character):
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
    skill_key = _skill_key_from_name(name, _as_text(item.get("skillKey") or item.get("id") or _slug(name)))
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
                skill_key = _skill_key_from_name(name, _as_text(item.get("skillKey") or item.get("id") or _slug(name)))
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


def _runtime_to_test_character(character):
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
        "rulesetId": _as_text(character.get("rulesetId"), _fallback_ruleset_id()),
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


def _test_character_to_runtime(character, fallback_id=""):
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
        "rulesetId": _as_text(character.get("rulesetId"), _fallback_ruleset_id()),
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


def _character_from_storage(raw, fallback_id=""):
    if not isinstance(raw, dict):
        return None
    if _is_test_character_shape(raw):
        character = _test_character_to_runtime(raw, fallback_id)
        plugin = get_plugin(character.get("rulesetId"))
        character["rulesetId"] = plugin.id
        # 旧卡没有 sheet：读取时由插件按 schema 现算一份，前端无需回退到固定字段。
        if not character.get("sheet"):
            character["sheet"] = dict(pack_card(plugin, raw).get("sheet") or {})
        return character
    character = dict(raw)
    character.setdefault("id", fallback_id)
    return character


def _skill_key_from_name(name, fallback=""):
    """技能名 → 技能键；目录由规则集插件提供，核心不直接读全局数据文件。"""
    from trpg_server.ruleset.capabilities import catalog_value

    try:
        catalog = catalog_value(get_plugin(None), "skills", {})
    except Exception:  # noqa: BLE001 - 插件缺失时保留调用方兜底值
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


def _character_filename(character_id):
    return normalize_filename(f"{character_id or f'investigator-{int(time.time() * 1000)}'}.json")


def _short_public_id(existing_ids):
    alphabet = string.ascii_letters + string.digits
    for _ in range(200):
        value = "".join(secrets.choice(alphabet) for _ in range(6))
        if value not in existing_ids:
            return value
    raise RuntimeError("Failed to generate unique public id")


def _is_short_public_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9]{6}", str(value or "")))


def _gallery_public_ids(excluded_path=None):
    ids = set()
    for path in _iter_gallery_character_files():
        if excluded_path and Path(path) == Path(excluded_path):
            continue
        data = read_json(path, default={})
        if isinstance(data, dict) and data.get("public_id"):
            ids.add(str(data["public_id"]))
    return ids


def _gallery_character_filename(public_id):
    return normalize_filename(f"{public_id}.json")


def _gallery_character_path(public_id):
    return safe_join(_get_character_gallery_dir(), _gallery_character_filename(public_id))


def _can_modify_gallery_character(character, permission_node):
    if _is_elevated() or (
        session.get("role") in {"ADMIN", "OWNER"}
        and _can_use_permission(permission_node)
    ):
        return True
    return str(character.get("publisher_id") or "") == str(session.get("user_id") or "")


def _character_path(character_id):
    return character_service.character_json_path(character_id)


def _legacy_character_path(character_id):
    return character_service.legacy_character_path(character_id)


def _character_storage_path(character_id):
    """优先返回新目录结构，其次回退到旧平铺文件。"""
    path = _character_path(character_id)
    if path.exists():
        return path
    legacy = _legacy_character_path(character_id)
    return legacy if legacy.exists() else path


def _iter_room_info_files():
    rooms_dir = _get_rooms_dir()
    if not rooms_dir.exists():
        return []
    return sorted(path / "info.json" for path in rooms_dir.iterdir() if path.is_dir() and (path / "info.json").exists())


def _can_access_character(character):
    if _is_elevated():
        return True
    player_id = str(character.get("playerId") or "")
    return bool(player_id) and player_id in _current_player_ids()


def _normalize_character_payload(payload, existing=None):
    if not isinstance(payload, dict):
        return None

    character = dict(existing or {})
    character.update(payload)
    character_id = str(character.get("id") or "").strip()
    name = str(character.get("name") or "").strip()
    if not character_id or not name:
        return None

    player_id = str(character.get("playerId") or "").strip()
    if not _is_elevated():
        player_id = str(session.get("user_id"))

    character["id"] = character_id[:80]
    character["name"] = name[:80]
    character["playerId"] = player_id
    character["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    character.setdefault("createdAt", character["updatedAt"])
    return character


def _bounded_int(value, minimum, maximum, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _sanitize_room_character_card(character):
    attributes = character.get("attributes") if isinstance(character.get("attributes"), dict) else {}
    max_hp = _bounded_int(character.get("maxHp", character.get("max_hp")), 1, 999, 1)
    max_san = _bounded_int(character.get("maxSan", character.get("max_san")), 0, 999, 0)
    max_mp = _bounded_int(character.get("maxMp", character.get("max_mp")), 0, 999, 0)
    sanitized = {
        "id": str(character.get("id", ""))[:80],
        "name": str(character.get("name", ""))[:80],
        "rulesetId": str(character.get("rulesetId") or _fallback_ruleset_id())[:40],
        "occupationId": str(character.get("occupationId", character.get("occupation_id", "")))[:80],
        "attributes": attributes,
        "maxHp": max_hp,
        "currentHp": _bounded_int(character.get("currentHp", character.get("current_hp")), 0, max_hp, max_hp),
        "maxSan": max_san,
        "initialSan": _bounded_int(character.get("initialSan"), 0, 999, max_san),
        "currentSan": _bounded_int(character.get("currentSan", character.get("current_san")), 0, max_san, max_san),
        "maxMp": max_mp,
        "currentMp": _bounded_int(character.get("currentMp", character.get("magicPoints")), 0, max_mp, max_mp),
        "mov": _bounded_int(character.get("mov"), 0, 99, 0),
        "build": _bounded_int(character.get("build"), -2, 99, 0),
        "damageBonus": str(character.get("damageBonus") or "")[:20],
        "armor": _bounded_int(character.get("armor"), 0, 99, 0),
        "skills": character.get("skills", []) if isinstance(character.get("skills", []), list) else [],
        "weapons": weapon_catalog.sanitize_character_weapons(character.get("weapons")),
        "customWeapons": weapon_catalog.sanitize_character_weapons(character.get("customWeapons")),
        "equipment": character.get("equipment", []) if isinstance(character.get("equipment", []), list) else [],
        "background": character.get("background", {}) if isinstance(character.get("background", {}), dict) else {},
        "status": character.get("status") if isinstance(character.get("status"), dict) else {},
    }
    try:
        return derive_card(get_plugin(sanitized["rulesetId"]), sanitized)
    except Exception:  # noqa: BLE001 - 插件异常时按未派生结果写入
        return sanitized


def _sync_room_character_snapshots(character):
    character_id = str(character.get("id") or "")
    if not character_id:
        return
    sanitized = _sanitize_room_character_card(character)
    for info_path in _iter_room_info_files():
        info = read_json(info_path, default={})
        members = info.get("members") if isinstance(info, dict) else None
        if not isinstance(members, list):
            continue
        changed = False
        for member in members:
            bound = member.get("character_card") if isinstance(member, dict) else None
            if not isinstance(bound, dict) or str(bound.get("id") or "") != character_id:
                continue
            member["character_card"] = dict(sanitized)
            state = member.setdefault("character_state", {})
            if isinstance(state, dict):
                # 资源键与上限由卡所属规则集声明，核心不写死 HP / SAN / MP 字段名。
                clamp_resource_state(get_plugin(sanitized.get("rulesetId")), sanitized, state)
            changed = True
        if changed:
            info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            write_json_atomic(info_path, info)


def _unbind_deleted_character_from_rooms(character_id):
    """Remove stale room snapshots when a character card is deleted."""
    character_id = str(character_id or "")
    if not character_id:
        return
    for info_path in _iter_room_info_files():
        info = read_json(info_path, default={})
        members = info.get("members") if isinstance(info, dict) else None
        if not isinstance(members, list):
            continue
        changed = False
        for member in members:
            if not isinstance(member, dict):
                continue
            card = member.get("character_card")
            if isinstance(card, dict) and str(card.get("id") or "") == character_id:
                member.pop("character_card", None)
                member.pop("character_state", None)
                changed = True
        if changed:
            info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            write_json_atomic(info_path, info)


def _fallback_ruleset_id() -> str:
    """当前默认插件 id；无可用插件时返回空串，避免读取旧卡时抛错。"""
    try:
        return default_ruleset_id()
    except RuntimeError:
        return ""


def _request_ruleset():
    """目录接口按 ?ruleset= 选择插件；缺省用站点默认。"""
    return builtin_registry().resolve(str(request.args.get("ruleset") or "").strip())


@bp.route("/api/character-catalogs/occupations", methods=["GET"])
def list_builtin_occupations():
    login_error = _require_login()
    if login_error:
        return login_error

    occupations = catalog_items(_request_ruleset(), "occupations")
    return success_response(occupations, "Occupation catalogs loaded successfully")


@bp.route("/api/character-catalogs/skills", methods=["GET"])
def list_character_skills():
    login_error = _require_login()
    if login_error:
        return login_error

    catalog = catalog_value(_request_ruleset(), "skills", {})
    return success_response(catalog, "Skill catalog loaded successfully")


@bp.route("/api/character-catalogs/weapons", methods=["GET"])
def list_builtin_weapons():
    login_error = _require_login()
    if login_error:
        return login_error

    weapons = catalog_items(_request_ruleset(), "weapons")
    return success_response(weapons, "Weapon catalogs loaded successfully")


def _age_adjustment_checks():
    """取当前规则集的建卡年龄计算能力；不支持时返回错误响应。"""
    plugin = _request_ruleset()
    checks = getattr(plugin, "checks", None)
    if checks is None or not callable(getattr(checks, "age_adjustment_preview", None)):
        return None, error_response("当前规则集不支持建卡年龄调整", 400)
    return checks, None


@bp.route("/api/characters/age-adjustment/preview", methods=["POST"])
def preview_age_adjustment():
    """按年龄返回建卡修正，只读、不写角色卡。"""
    login_error = _require_login()
    if login_error:
        return login_error

    checks, error = _age_adjustment_checks()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        effects = checks.age_adjustment_preview(payload.get("age"))
    except (TypeError, ValueError) as exc:
        return error_response(str(exc), 400)
    return success_response(effects, "Age adjustment preview loaded")


@bp.route("/api/characters/age-adjustment/edu-roll", methods=["POST"])
def roll_age_adjustment_edu():
    """EDU 增强检定：一次只掷一次，由玩家逐次触发。"""
    login_error = _require_login()
    if login_error:
        return login_error

    checks, error = _age_adjustment_checks()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = checks.edu_enhancement_step(payload.get("edu"))
    except (TypeError, ValueError) as exc:
        return error_response(str(exc), 400)
    return success_response(result, "EDU enhancement roll resolved")


@bp.route("/api/characters/age-adjustment/apply", methods=["POST"])
def apply_age_adjustment():
    """校验玩家分配并返回属性增量；实际写入仍走原有角色卡保存流程。"""
    login_error = _require_login()
    if login_error:
        return login_error

    checks, error = _age_adjustment_checks()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = checks.apply_age_adjustment(
            payload.get("age"),
            str_siz_allocation=payload.get("str_siz_allocation"),
            str_con_dex_allocation=payload.get("str_con_dex_allocation"),
            edu_gain=payload.get("edu_gain") or 0,
        )
    except (TypeError, ValueError) as exc:
        return error_response(str(exc), 400)
    return success_response(result, "Age adjustment applied")


@bp.route("/api/characters", methods=["GET"])
def list_characters():
    login_error = _require_login()
    if login_error:
        return login_error

    characters = []
    for path in _iter_character_files():
        character = _character_from_storage(read_json(path, default=None), _character_id_from_path(path))
        if character and _can_access_character(character):
            characters.append(character)
    characters.sort(key=lambda item: item.get("updatedAt") or item.get("createdAt") or "", reverse=True)
    return success_response(characters, "Characters loaded successfully")


@bp.route("/api/character-gallery", methods=["GET"])
def list_character_gallery():
    login_error = _require_login()
    if login_error:
        return login_error

    gallery = []
    public_ids = set()
    for path in _iter_gallery_character_files():
        character = _character_from_storage(read_json(path, default=None), path.stem)
        if character:
            public_id = str(character.get("public_id") or "")
            if not _is_short_public_id(public_id) or public_id in public_ids:
                public_id = _short_public_id(public_ids)
                character["id"] = public_id
                character["public_id"] = public_id
                stored = _runtime_to_test_character(character) | {
                    "public_id": public_id,
                    "publisher_id": character.get("publisher_id"),
                    "publisher_name": character.get("publisher_name", ""),
                }
                write_json_atomic(path, stored)
            else:
                character["id"] = public_id
            public_ids.add(public_id)
            gallery.append(character)
    gallery.sort(key=lambda item: item.get("updatedAt") or item.get("createdAt") or "", reverse=True)
    return success_response(gallery, "Character gallery loaded successfully")


@bp.route("/api/character-gallery", methods=["POST"])
def publish_character_gallery():
    login_error = _require_login()
    if login_error:
        return login_error
    if not _can_use_permission("characters.gallery.publish"):
        return error_response("Permission denied", 403, "Permission denied")

    payload = request.get_json(silent=True) or {}
    if _is_test_character_shape(payload):
        payload = _test_character_to_runtime(payload)
    character = _normalize_character_payload(payload)
    if character is None:
        return error_response("Invalid character card", 400, "Invalid character card")

    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    public_id = _short_public_id(_gallery_public_ids())
    character["id"] = public_id
    character["public_id"] = public_id
    character["publisher_id"] = session.get("user_id")
    character["publisher_name"] = session.get("username")
    character["createdAt"] = character.get("createdAt") or now
    character["updatedAt"] = now

    path = _gallery_character_path(public_id)
    write_json_atomic(path, _runtime_to_test_character(character) | {
        "public_id": public_id,
        "publisher_id": character["publisher_id"],
        "publisher_name": character["publisher_name"],
    })
    log_user_action(
        logger,
        user_action_text(session.get("username"), "发布了角色卡到广场"),
        用户ID=session.get("user_id"),
        角色卡ID=public_id,
        角色名=character["name"],
    )
    return success_response(character, "Character published successfully", 201)


@bp.route("/api/character-gallery/<public_id>", methods=["PUT"])
def update_character_gallery(public_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if not _can_use_permission("characters.gallery.edit"):
        return error_response("Permission denied", 403, "Permission denied")

    path = _gallery_character_path(public_id)
    if not path.exists():
        return error_response("Character gallery card not found", 404, "Not found")
    existing = _character_from_storage(read_json(path, default={}), public_id)
    if not existing or not _can_modify_gallery_character(existing, "characters.gallery.edit"):
        return error_response("Permission denied", 403, "Permission denied")

    payload = request.get_json(silent=True) or {}
    if _is_test_character_shape(payload):
        payload = _test_character_to_runtime(payload, public_id)
    character = _normalize_character_payload(payload, existing=existing)
    if character is None:
        return error_response("Invalid character card", 400, "Invalid character card")

    character["id"] = str(public_id)
    character["public_id"] = str(public_id)
    character["publisher_id"] = existing.get("publisher_id")
    character["publisher_name"] = existing.get("publisher_name")
    character["createdAt"] = existing.get("createdAt") or character.get("createdAt")
    stored = _runtime_to_test_character(character) | {
        "public_id": public_id,
        "publisher_id": character["publisher_id"],
        "publisher_name": character["publisher_name"],
    }
    write_json_atomic(path, stored)
    log_user_action(logger, user_action_text(session.get("username"), "更新了角色卡广场内容"), 角色卡ID=public_id)
    return success_response(character, "Character gallery card updated successfully")


@bp.route("/api/character-gallery/<public_id>", methods=["DELETE"])
def delete_character_gallery(public_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if not _can_use_permission("characters.gallery.delete"):
        return error_response("Permission denied", 403, "Permission denied")

    path = _gallery_character_path(public_id)
    if not path.exists():
        return error_response("Character gallery card not found", 404, "Not found")
    existing = _character_from_storage(read_json(path, default={}), public_id)
    if not existing or not _can_modify_gallery_character(existing, "characters.gallery.delete"):
        return error_response("Permission denied", 403, "Permission denied")

    path.unlink()
    log_user_action(logger, user_action_text(session.get("username"), "删除了角色卡广场内容"), 角色卡ID=public_id)
    return success_response(message="Character gallery card deleted successfully")


@bp.route("/api/character-gallery/<public_id>/export", methods=["GET"])
def export_character_gallery(public_id):
    """导出广场角色卡；仅发布者本人或具备管理权限的账号可取，与编辑规则一致。"""
    login_error = _require_login()
    if login_error:
        return login_error

    path = _gallery_character_path(public_id)
    if not path.exists():
        return error_response("Character gallery card not found", 404, "Not found")
    character = _character_from_storage(read_json(path, default={}), public_id)
    if not character:
        return error_response("Character gallery card not found", 404, "Not found")
    if not _can_modify_gallery_character(character, "characters.gallery.edit"):
        return error_response("Permission denied", 403, "Permission denied")

    bundle = character_card_io.build_export_bundle(character)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "导出了广场角色卡"),
        用户ID=session.get("user_id"),
        角色卡ID=public_id,
        角色名=character.get("name"),
    )
    return success_response(bundle, "Character exported successfully")


@bp.route("/api/characters/<character_id>", methods=["PUT"])
def save_character(character_id):
    login_error = _require_login()
    if login_error:
        return login_error

    path = _character_storage_path(character_id)
    is_new_character = not path.exists()
    payload = request.get_json(silent=True) or {}
    try:
        character = character_service.apply_save(
            character_service_actor(),
            character_id,
            payload,
        )
    except character_service.CharacterServiceError as exc:
        return error_response(exc.message, exc.status, exc.message)

    log_user_action(
        logger,
        user_action_text(session.get("username"), "保存了角色卡"),
        用户ID=session.get("user_id"),
        操作="创建" if is_new_character else "更新",
        角色卡ID=character["id"],
        角色名=character["name"],
    )
    return success_response(character, "Character saved successfully")


@bp.route("/api/characters/<character_id>", methods=["DELETE"])
def delete_character(character_id):
    login_error = _require_login()
    if login_error:
        return login_error

    path = _character_storage_path(character_id)
    if not path.exists():
        return success_response(message="Character already deleted")
    character = _character_from_storage(read_json(path, default={}), character_id) or {}
    if character and not _can_access_character(character):
        return error_response("Permission denied", 403, "Permission denied")
    _unbind_deleted_character_from_rooms(character_id)
    character_service.delete_character_storage(character_id)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了角色卡"),
        用户ID=session.get("user_id"),
        角色卡ID=character_id,
        角色名=character.get("name"),
    )
    return success_response(message="Character deleted successfully")


@bp.route("/api/characters/<character_id>/weapons", methods=["GET"])
def list_character_weapons(character_id):
    login_error = _require_login()
    if login_error:
        return login_error
    try:
        weapons = character_service.get_custom_weapons(character_service_actor(), character_id)
    except character_service.CharacterServiceError as exc:
        return error_response(exc.message, exc.status, exc.message)
    return success_response({"weapons": weapons, "count": len(weapons)}, "Custom weapons loaded successfully")


@bp.route("/api/characters/<character_id>/weapons", methods=["PUT"])
def save_character_weapons(character_id):
    login_error = _require_login()
    if login_error:
        return login_error
    payload = request.get_json(silent=True) or {}
    weapons = payload.get("weapons") if isinstance(payload, dict) else None
    if not isinstance(weapons, list):
        return error_response("weapons 必须是数组", 400, "Invalid weapons payload")
    try:
        saved = character_service.save_custom_weapons(character_service_actor(), character_id, weapons)
    except character_service.CharacterServiceError as exc:
        return error_response(exc.message, exc.status, exc.message)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "更新了角色卡自定义武器"),
        用户ID=session.get("user_id"),
        角色卡ID=character_id,
        武器数量=len(saved),
    )
    return success_response({"weapons": saved, "count": len(saved)}, "Custom weapons saved successfully")


@bp.route("/api/characters/<character_id>/export", methods=["GET"])
def export_character(character_id):
    """导出角色卡的完整持久化数据；仅有查看权限的本人/管理员可取。"""
    login_error = _require_login()
    if login_error:
        return login_error
    try:
        character = character_service.get_for_actor(character_service_actor(), character_id)
    except character_service.CharacterServiceError as exc:
        return error_response(exc.message, exc.status, exc.message)

    bundle = character_card_io.build_export_bundle(character)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "导出了角色卡"),
        用户ID=session.get("user_id"),
        角色卡ID=character.get("id"),
        角色名=character.get("name"),
    )
    return success_response(bundle, "Character exported successfully")


@bp.route("/api/rooms/<room_id>/members/<user_id>/character-diff", methods=["GET"])
def room_member_character_diff(room_id, user_id):
    """房间快照与持久角色卡的差异；仅房间成员或管理员可读。

    用于卡面标记「房间内临时」：只返回实际变化的字段（当前值 / 保存值），
    不返回整张持久卡，避免扩大可见范围。房间未绑定持久卡时返回空差异。
    """
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir = safe_join(_get_rooms_dir(), room_id)
    info = read_json(room_dir / "info.json", default=None)
    if not isinstance(info, dict):
        return error_response("房间不存在", 404, "room_not_found")
    members = info.get("members") if isinstance(info.get("members"), list) else []
    member = next(
        (
            row
            for row in members
            if isinstance(row, dict) and str(row.get("user_id")) == str(user_id)
        ),
        None,
    )
    if member is None:
        return error_response("房间成员不存在", 404, "member_not_found")
    if not _is_elevated():
        viewer = str(session.get("user_id") or "")
        is_room_member = any(
            isinstance(row, dict)
            and str(row.get("user_id")) == viewer
            and row.get("status") != "removed"
            for row in members
        )
        if not is_room_member:
            return error_response("无权查看该房间角色卡", 403, "permission_denied")

    current = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
    diff = {}
    if current:
        persistent = _load_room_member_card(member, current)
        diff = room_ai_changes.diff_room_card(persistent, current)
    return success_response(
        {"room_id": str(room_id), "user_id": str(user_id), "diff": diff},
        "Room character diff loaded",
    )


def _load_room_member_card(member, current):
    """读取房间成员绑定的持久卡；无绑定或读取失败时返回 None。"""
    from trpg_server.site_agent_auth import Actor

    card_id = str(current.get("id") or "")
    if not card_id:
        return None
    try:
        user_id = int(str(member.get("user_id") or "0"))
    except (TypeError, ValueError):
        user_id = 0
    actor = Actor(
        user_id=user_id,
        username=str(member.get("username") or ""),
        role="USER",
        permissions=frozenset(),
        room_ids=frozenset(),
    )
    try:
        return character_service.get_for_actor(actor, card_id)
    except character_service.CharacterServiceError:
        return None


@bp.route("/api/characters/import", methods=["POST"])
def import_character():
    """从 JSON 文本或 zlib + base64 分享串导入一张新角色卡，不覆盖已有卡。"""
    login_error = _require_login()
    if login_error:
        return login_error
    request_length = request.content_length or 0
    if request_length > character_card_io.MAX_IMPORT_REQUEST_BYTES:
        return error_response("导入内容过大，超过请求体上限。", 413, "import_too_large")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "invalid_import_payload")
    text = payload.get("payload")
    if not isinstance(text, str) or not text.strip():
        return error_response("导入内容为空。", 400, "empty_import_payload")

    actor = character_service_actor()
    try:
        parsed = character_card_io.parse_import_text(text, str(payload.get("mode") or "auto"))
        merged = character_card_io.merge_sheet_fields(parsed)
        if _is_test_character_shape(merged):
            merged = _test_character_to_runtime(merged)
        card = character_card_io.validate_import_payload(merged)

        own_names = {
            str(item.get("name") or "")
            for item in character_service.list_for_actor(actor, limit=50)
            if str(item.get("playerId") or "") == str(getattr(actor, "user_id", ""))
        }
        desired_name = str(card.get("name") or "")
        card["name"] = character_card_io.unique_character_name(desired_name, own_names)

        character_id = character_card_io.new_unique_character_id()
        card["id"] = character_id
        # 导入的卡固定归当前登录用户，忽略来源里的归属与发布信息。
        card["playerId"] = str(getattr(actor, "user_id", ""))
        card["playerName"] = str(getattr(actor, "username", "") or "")
        card["public_id"] = ""
        card["publisher_id"] = None
        card["publisher_name"] = ""
        avatar_data = card.pop("avatarData", "")
        if avatar_data:
            card["avatar"] = avatar_data
        saved = character_service.apply_save(actor, character_id, card)
    except character_card_io.CharacterCardIOError as exc:
        log_user_action(
            logger,
            user_action_text(session.get("username"), "导入角色卡失败"),
            用户ID=session.get("user_id"),
            失败原因=exc.code,
        )
        return error_response(exc.message, exc.status, exc.code)
    except character_service.CharacterServiceError as exc:
        return error_response(exc.message, exc.status, exc.message)

    renamed = str(saved.get("name") or "") != desired_name
    log_user_action(
        logger,
        user_action_text(session.get("username"), "导入了角色卡"),
        用户ID=session.get("user_id"),
        角色卡ID=saved.get("id"),
        角色名=saved.get("name"),
        是否重命名=renamed,
    )
    return success_response(
        {"character": saved, "renamed": renamed},
        "Character imported successfully",
        201,
    )

