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
from trpg_server.ruleset.capabilities import (
    catalog_items,
    catalog_value,
    clamp_resource_state,
    derive_card,
    is_legacy_card_shape,
    legacy_card_to_runtime,
    pack_card,
    runtime_card_to_legacy,
)
from trpg_server.security import normalize_filename, safe_join
from trpg_server.settings import CHARACTER_GALLERY_DIR, CHARACTERS_DIR, ROOMS_DIR

bp = Blueprint("characters", __name__)
logger = logging.getLogger(__name__)


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


# -- 旧卡形状分派（C1 下沉后的信封外壳）--------------------------------
#
# 旧卡（test 卡）形状的检测与转换已下沉到规则集插件（coc7 实现见
# ``ruleset/coc7/card_shape.py``，经 ``ruleset.capabilities`` 的三个访问器
# 暴露）；本模块只保留按 raw.rulesetId 分派的外壳，签名与历史实现逐位一致。
# 未声明该能力的规则集（如 dnd5e）读取 / 保存都原样透传，不再被 CoC 形状改写。


def _shape_plugin(raw):
    """按 raw.rulesetId 解析形状归属插件；未知 / 异常时降级到默认插件或 None。"""
    if not isinstance(raw, dict):
        return None
    ruleset_id = raw.get("rulesetId")
    try:
        return get_plugin(str(ruleset_id) if ruleset_id else None)
    except Exception:  # noqa: BLE001 - 未知 id 时降级到默认插件
        try:
            return get_plugin(None)
        except Exception:  # noqa: BLE001 - 连默认插件都不可用时按无形状能力处理
            return None


def _is_test_character_shape(character):
    """旧卡形状检测外壳；检测逻辑由卡所属规则集插件声明。"""
    if not isinstance(character, dict):
        return False
    return is_legacy_card_shape(_shape_plugin(character), character)


def _runtime_to_test_character(character):
    """运行时卡 → 存储形状外壳；未声明能力的规则集原样返回。"""
    return runtime_card_to_legacy(_shape_plugin(character), character)


def _test_character_to_runtime(character, fallback_id=""):
    """旧卡形状 → 运行时卡外壳；未声明能力的规则集原样返回（不转换）。"""
    converted = legacy_card_to_runtime(_shape_plugin(character), character, fallback_id)
    if converted is None:
        return character
    return converted


def _character_from_storage(raw, fallback_id=""):
    if not isinstance(raw, dict):
        return None
    if _is_test_character_shape(raw):
        try:
            character = _test_character_to_runtime(raw, fallback_id)
        except Exception:  # noqa: BLE001 - 转换失败时不转换，原样返回
            character = raw
        if character is not raw:
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
    """技能名 → 技能键外壳；目录由规则集插件提供，核心不直接读全局数据文件。"""
    from trpg_server.ruleset.capabilities import schema_of

    try:
        schema = schema_of(get_plugin(None))
    except Exception:  # noqa: BLE001 - 插件缺失时保留调用方兜底值
        return fallback
    resolver = getattr(schema, "skill_key_from_name", None)
    if not callable(resolver):
        return fallback
    try:
        result = resolver(name, fallback)
    except Exception:  # noqa: BLE001 - 插件异常时保留调用方兜底值
        return fallback
    return result if isinstance(result, str) else fallback


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
    # 规则自有卡面快照：插件按 schema 解释，核心只负责原样携带。
    sheet = character.get("sheet")
    if isinstance(sheet, dict) and sheet:
        sanitized["sheet"] = dict(sheet)
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
            from trpg_server import room_rules

            room_rules.save_room_info(info_path.parent, info)


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
            from trpg_server import room_rules

            room_rules.save_room_info(info_path.parent, info)


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

