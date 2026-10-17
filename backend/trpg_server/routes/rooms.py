import json
import logging
import tomllib
import time
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, current_app, request, session

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media_defaults import normalize_avatar
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response, success_response
from trpg_server.role_config import load_roles
from trpg_server.security import get_user_manager, is_socket_user_online, is_user_in_room, safe_join
from trpg_server.settings import CONFIG_DIR, ROOMS_DIR, SCENARIOS_DIR, ROOM_ARCHIVES_DIR, CHARACTERS_DIR, HISTORY_DIR
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.room_state import save_room_state
from trpg_server.agents.versioning import migrate_room_binding
from trpg_server.agents.trigger_system import find_trigger_definition, record_trigger, validate_trigger
from trpg_server.scenario_store import load_scenario_by_id

bp = Blueprint("rooms", __name__)
logger = logging.getLogger(__name__)

USER_ROOM_LIMIT = 3
ROOM_ROLE_OWNER = "owner"
ROOM_ROLE_ADMIN = "admin"
ROOM_ROLE_MEMBER = "member"
ROOM_MEMBER_ACTIVE = "active"
ROOM_MEMBER_REMOVED = "removed"
DEFAULT_AUTOSAVE_NODE_LIMIT = 3
ROOM_VISIBILITY_PRIVATE = "private"
ROOM_VISIBILITY_PUBLIC = "public"
ROOM_VISIBILITIES = {ROOM_VISIBILITY_PRIVATE, ROOM_VISIBILITY_PUBLIC}

# Structured per-room house rules. Only rules flagged ``ai_prompt`` are ever
# injected into the KP prompt (and only when active), keeping prompt tokens low;
# future non-AI rules can be registered here without prompt cost.
HOUSE_RULE_DEFINITIONS = {
    "action_suggestions_enabled": {"type": "boolean", "default": False, "ai_prompt": True},
    # 骰娘大成功/大失败阈值（全房间统一）。留空表示沿用管理员设置页配置的全局默认值。
    "dice_critical_threshold": {"type": "nullable_integer", "default": None, "min": 0, "max": 100, "ai_prompt": False},
    "dice_fumble_threshold": {"type": "nullable_integer", "default": None, "min": 0, "max": 100, "ai_prompt": False},
    # 技能基础值覆盖表：键为技能键（带专精时为 `skillKey.specialtyKey`），值为 0-99 整数。
    # 留空表示沿用下一优先级（用户个性化 > 管理员设置 > 技能目录默认值）。
    "skill_bases": {"type": "skill_bases", "default": {}, "ai_prompt": False},
}
DEFAULT_ROOM_HOUSE_RULES = {key: spec["default"] for key, spec in HOUSE_RULE_DEFINITIONS.items()}


def _get_rooms_dir():
    return current_app.config.get("ROOMS_DIR", ROOMS_DIR)


def _get_archives_dir():
    return current_app.config.get("ROOM_ARCHIVES_DIR", ROOM_ARCHIVES_DIR)


def _get_config_dir():
    return current_app.config.get("CONFIG_DIR", CONFIG_DIR)


def _get_ai_platform_dir():
    return current_app.config.get("AI_PLATFORM_DIR", _get_config_dir() / "aiplatform")


def _get_kp_prompt_file():
    return current_app.config.get("KP_PROMPT_FILE", _get_config_dir() / "roles" / "kp.md")


def _get_role_config_file():
    return current_app.config.get("ROLE_CONFIG_FILE", _get_config_dir() / "roles" / "roles.json")


def _autosave_node_limit():
    config_path = _get_config_dir() / "general.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        value = int((config.get("autosave") or {}).get("max_nodes", DEFAULT_AUTOSAVE_NODE_LIMIT))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        value = DEFAULT_AUTOSAVE_NODE_LIMIT
    return max(1, min(value, 50))


def _autosave_settings():
    """读取 general.toml 的 [autosave] 段，供服务端后台调度使用。"""
    defaults = {"enabled": True, "interval": 300}
    config_path = _get_config_dir() / "general.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    section = config.get("autosave") or {}
    try:
        interval = int(section.get("interval", defaults["interval"]))
    except (TypeError, ValueError):
        interval = defaults["interval"]
    return {"enabled": bool(section.get("enabled", defaults["enabled"])), "interval": max(30, min(interval, 3600))}


def _timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _is_elevated():
    config_dir = current_app.config.get("CONFIG_DIR")
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(session.get("role", "USER"), "rooms.manage_members", config_path)


def _denied(room_ref=None):
    log_access_denied(
        logger,
        user_action_text(session.get("username"), "访问房间被拒绝"),
        用户ID=session.get("user_id"),
        房间ID=room_ref,
    )
    return error_response("Permission denied", 403, "Permission denied")


def _iter_room_dirs():
    rooms_dir = _get_rooms_dir()
    if not rooms_dir.exists():
        return []
    return [path for path in rooms_dir.iterdir() if path.is_dir()]


def _read_room(room_dir):
    return read_json(room_dir / "info.json", default={})


def _write_room(room_dir, info):
    info["updated_at"] = _timestamp()
    write_json_atomic(room_dir / "info.json", info)


def _global_hints_enabled():
    """Global admin switch. When off, no room may enable action suggestions."""
    return bool(load_ai_runtime_config(_get_config_dir()).show_ai_hints)


def _room_house_rules(info):
    """Normalize stored house rules against ``HOUSE_RULE_DEFINITIONS``.

    Missing keys fall back to defaults and wrong types are coerced, so a room
    written by an older version always yields a complete, well-typed dict.
    """
    stored = info.get("house_rules") if isinstance(info, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    normalized = {}
    for key, spec in HOUSE_RULE_DEFINITIONS.items():
        value = stored.get(key, spec["default"])
        if spec["type"] == "boolean":
            value = bool(value)
        elif spec["type"] == "nullable_integer":
            value = _normalize_nullable_int(value, spec.get("min", 0), spec.get("max", 100))
        elif spec["type"] == "skill_bases":
            value = _normalize_skill_bases(value)
        normalized[key] = value
    return normalized


def _normalize_skill_bases(value):
    """把房规中的技能基础值覆盖表规范化：键为非空字符串，值为 0-99 整数。

    非法键值直接丢弃（等同「未填写」），从而回退到下一优先级。
    """
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or raw_value is None or raw_value == "":
            continue
        try:
            number = int(raw_value)
        except (TypeError, ValueError):
            continue
        normalized[key] = max(0, min(99, number))
    return normalized


def _normalize_nullable_int(value, minimum, maximum):
    """把房规中的可空整数字段规范化；空值表示沿用全局默认值。"""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _admin_dice_thresholds():
    """管理员设置页配置的骰娘默认阈值（general.toml 的 [ai] 段）。"""
    config = load_ai_runtime_config(_get_config_dir())
    return {"critical": config.dice_critical_threshold, "fumble": config.dice_fumble_threshold}


def _room_dice_thresholds(info):
    """房间生效阈值：房规内配置优先，未配置则回退到管理员默认值。"""
    rules = _room_house_rules(info)
    defaults = _admin_dice_thresholds()
    critical = rules.get("dice_critical_threshold")
    fumble = rules.get("dice_fumble_threshold")
    return {
        "critical": defaults["critical"] if critical is None else critical,
        "fumble": defaults["fumble"] if fumble is None else fumble,
    }


def _room_dir(room_id):
    return safe_join(_get_rooms_dir(), room_id)


def _find_room(room_id):
    room_dir = _room_dir(room_id)
    info_file = room_dir / "info.json"
    if not info_file.exists():
        return None, None
    return room_dir, read_json(info_file, default={})


def _find_room_by_code(room_code):
    normalized = str(room_code or "").strip().upper()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if info.get("room_code") == normalized:
            return room_dir, info
    return None, None


def _normalize_room_name(room_name):
    return str(room_name or "").strip().casefold()


def _find_room_by_name(room_name):
    normalized = _normalize_room_name(room_name)
    if not normalized:
        return None, None
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if _normalize_room_name(info.get("name")) == normalized:
            return room_dir, info
    return None, None


def _room_name_exists(room_name):
    room_dir, _ = _find_room_by_name(room_name)
    return room_dir is not None


def _can_access(info):
    if _is_elevated():
        return True
    user_id = session.get("user_id")
    return _find_member(info, user_id=user_id, active_only=True) is not None


def _can_manage(info):
    if _is_elevated() or str(info.get("creator_id")) == str(session.get("user_id")):
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _archive_visible(archive):
    if _is_elevated():
        return True
    user_id = str(session.get("user_id"))
    return any(str(member.get("user_id")) == user_id for member in archive.get("members", []) if isinstance(member, dict))


def _scenario_finished(info, state=None):
    if info.get("completed_at") or info.get("ended_at"):
        return True
    scene_id = str(info.get("active_scene_id") or ((state or {}).get("active_scene_id") if isinstance(state, dict) else "") or "")
    if isinstance(state, dict) and (state.get("completed_at") or state.get("completed") or state.get("ending_reached")):
        return True
    if not scene_id:
        return False
    _, scenario = load_scenario_by_id(_get_scenarios_dir(), info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    for module in (scenario or {}).get("modules", []):
        if isinstance(module, dict) and str(module.get("scene_id") or module.get("id")) == scene_id:
            return str(module.get("module_type") or module.get("type") or "").lower() == "ending"
    return False


def _get_scenarios_dir():
    return current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)


def _current_member(character_card=None):
    user_id = session["user_id"]
    user = None
    try:
        user = current_app.config.get("USER_MANAGER").get_user_by_id(user_id)
    except AttributeError:
        from trpg_server.users.manager import user_manager

        user = user_manager.get_user_by_id(user_id)

    avatar = ""
    if user:
        avatar = normalize_avatar(user.get("avatar"))

    member = {
        "user_id": user_id,
        "username": session.get("username", "user"),
        "role": session.get("role", "USER"),
        "room_role": ROOM_ROLE_MEMBER,
        "status": ROOM_MEMBER_ACTIVE,
        "is_active": True,
        "avatar": avatar,
        "joined_at": _timestamp(),
    }
    if character_card:
        member["character_card"] = _sanitize_character_card(character_card)
        member["character_state"] = _initial_character_state(member["character_card"])
    return member


def _sanitize_character_card(character_card):
    if not isinstance(character_card, dict):
        return None

    name = str(character_card.get("name", "")).strip()
    card_id = str(character_card.get("id", "")).strip()
    if not name or not card_id:
        return None

    attributes = character_card.get("attributes") if isinstance(character_card.get("attributes"), dict) else {}
    sanitized = {
        "id": card_id[:80],
        "name": name[:80],
        "occupationId": str(character_card.get("occupationId", character_card.get("occupation_id", "")))[:80],
        "attributes": attributes,
        "maxHp": _bounded_int(character_card.get("maxHp", character_card.get("max_hp")), 1, 999, 1),
        "maxSan": _bounded_int(character_card.get("maxSan", character_card.get("max_san")), 0, 999, 0),
        "mov": _bounded_int(character_card.get("mov"), 0, 99, 0),
        "skills": character_card.get("skills", []) if isinstance(character_card.get("skills", []), list) else [],
        "equipment": character_card.get("equipment", []) if isinstance(character_card.get("equipment", []), list) else [],
        "background": character_card.get("background", {}) if isinstance(character_card.get("background", {}), dict) else {},
    }
    return sanitized


def _bounded_int(value, minimum, maximum, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _initial_character_state(character_card):
    max_hp = _bounded_int(character_card.get("maxHp"), 1, 999, 1)
    max_san = _bounded_int(character_card.get("maxSan"), 0, 999, 0)
    return {
        "max_hp": max_hp,
        "current_hp": max_hp,
        "max_san": max_san,
        "current_san": max_san,
        "injury_records": [],
        "sanity_records": [],
        "skill_changes": [],
        "item_changes": [],
    }


def _ensure_character_state(member):
    if not member.get("character_state"):
        member["character_state"] = _initial_character_state(member.get("character_card") or {})
    state = member["character_state"]
    state.setdefault("injury_records", [])
    state.setdefault("sanity_records", [])
    state.setdefault("skill_changes", [])
    state.setdefault("item_changes", [])
    state["max_hp"] = _bounded_int(state.get("max_hp"), 1, 999, 1)
    state["current_hp"] = _bounded_int(state.get("current_hp"), 0, state["max_hp"], state["max_hp"])
    state["max_san"] = _bounded_int(state.get("max_san"), 0, 999, 0)
    state["current_san"] = _bounded_int(state.get("current_san"), 0, state["max_san"], state["max_san"])
    return state


def _bind_character(member, character_card):
    sanitized = _sanitize_character_card(character_card)
    if not sanitized:
        return False
    member["character_card"] = sanitized
    member["character_state"] = _initial_character_state(sanitized)
    return True


def _can_bind_character_card(character_card, target_member):
    if _is_elevated():
        return True
    player_id = str((character_card or {}).get("playerId") or "")
    return player_id in {
        str(target_member.get("user_id")),
        str(target_member.get("username")),
    }


def _is_active_member(member):
    return member.get("is_active", True) is not False and member.get("status", ROOM_MEMBER_ACTIVE) != ROOM_MEMBER_REMOVED


def _find_member(info, user_id=None, username=None, active_only=False):
    for member in info.get("members", []):
        if active_only and not _is_active_member(member):
            continue
        if user_id is not None and str(member.get("user_id")) == str(user_id):
            return member
        if username and str(member.get("username", "")).lower() == str(username).lower():
            return member
    return None


def _normalize_members(info):
    creator_id = str(info.get("creator_id"))
    for member in info.setdefault("members", []):
        member.setdefault("status", ROOM_MEMBER_ACTIVE)
        member.setdefault("is_active", member.get("status") != ROOM_MEMBER_REMOVED)
        if str(member.get("user_id")) == creator_id:
            member["room_role"] = ROOM_ROLE_OWNER
            member["status"] = ROOM_MEMBER_ACTIVE
            member["is_active"] = True
        else:
            member.setdefault("room_role", ROOM_ROLE_MEMBER)
    return info["members"]


def _room_permission(member, info):
    if _is_elevated():
        return ROOM_ROLE_ADMIN
    if not member or not _is_active_member(member):
        return ""
    if str(member.get("user_id")) == str(info.get("creator_id")):
        return ROOM_ROLE_OWNER
    return member.get("room_role") or ROOM_ROLE_MEMBER


def _can_manage_members(info):
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _is_elevated() or _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _recalculate_character_state(state):
    damage = sum(_bounded_int(record.get("value", record.get("damage")), 0, 999, 0) for record in state.get("injury_records", []))
    san_loss = sum(_bounded_int(record.get("value", record.get("loss")), 0, 999, 0) for record in state.get("sanity_records", []))
    state["current_hp"] = max(0, state["max_hp"] - damage)
    state["current_san"] = max(0, state["max_san"] - san_loss)


def _messages_file(room_dir):
    return room_dir / "messages.json"


def _read_messages(room_dir):
    return read_json(_messages_file(room_dir), default=[])


def _write_messages(room_dir, messages):
    write_json_atomic(_messages_file(room_dir), messages)


def _character_delta(member):
    """Return ``(san_change_text, other_changes_text)`` for a room member.

    ``other_changes_text`` aggregates HP/injury/skill/item changes so it can be
    rendered as the third line of a "参与过的模组" record.
    """
    state = member.get("character_state") if isinstance(member, dict) else {}
    state = state if isinstance(state, dict) else {}
    max_san = int(state.get("max_san") or 0)
    current_san = int(state.get("current_san") if state.get("current_san") is not None else max_san)
    max_hp = int(state.get("max_hp") or 0)
    current_hp = int(state.get("current_hp") if state.get("current_hp") is not None else max_hp)
    san_change = current_san - max_san
    other = []
    if current_hp != max_hp:
        other.append(f"HP {max_hp}->{current_hp}")
    for record in (state.get("injury_records") or [])[:20]:
        if isinstance(record, dict) and record.get("reason"):
            other.append(str(record["reason"])[:120])
    for record in (state.get("skill_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    for record in (state.get("item_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    san_text = f"SAN {san_change:+d}" if san_change else "SAN 无变化"
    return san_text, "；".join(dict.fromkeys(other))


def _change_record_text(record):
    """Format a structured skill/item change record into a short line."""
    if not isinstance(record, dict):
        return ""
    change_type = str(record.get("type") or "")
    target = str(record.get("target") or record.get("name") or record.get("item") or "").strip()
    if not target:
        return ""
    verb = record.get("change") or record.get("operation") or record.get("action") or "变化"
    amount = record.get("amount")
    if amount is None and record.get("value") is not None:
        amount = record["value"]
    reason = str(record.get("reason") or "").strip()
    prefix = "技能" if change_type == "skill" else "物品" if change_type == "item" else "变更"
    if amount is not None:
        try:
            amount_signed = f"{int(amount):+d}"
        except (TypeError, ValueError):
            amount_signed = str(amount)
        line = f"{prefix} {target} {verb} {amount_signed}".strip() + (f"（{reason}）" if reason else "")
    else:
        line = f"{prefix} {target} {verb}".strip() + (f"（{reason}）" if reason else "")
    return line[:160]


def _append_character_archive_records(info):
    """Copy the finished room outcome to each participating character card.

    Each entry is stored with ``name`` (module/scenario name), ``san_change``
    and ``other_changes`` so UIs can render the required three-line format:
    参与过的模组名 / SAN 值变化 / 其他变化.
    """
    scenario_name = str(info.get("scenario_title") or info.get("name") or "未命名剧本").strip()
    for member in info.get("members", []):
        card = member.get("character_card") if isinstance(member, dict) else None
        card_id = str((card or {}).get("id") or "").strip()
        if not card_id:
            continue
        path = Path(current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR)) / f"{card_id}.json"
        if not path.exists():
            continue
        stored = read_json(path, default={})
        if not isinstance(stored, dict):
            continue
        san_change, other_changes = _character_delta(member)
        entry = {
            "name": scenario_name,
            "san_change": san_change,
            "other_changes": other_changes,
            "experience": f"{san_change}" + (f"；{other_changes}" if other_changes else ""),
        }
        existing = stored.get("experiencedModules")
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (TypeError, ValueError):
                existing = []
        if not isinstance(existing, list):
            # Runtime-shaped character cards use experiencedScenarios.
            existing = stored.get("experiencedScenarios") if isinstance(stored.get("experiencedScenarios"), list) else []
        # One immutable outcome entry per archived room/scenario.
        if not any(isinstance(item, dict) and item.get("name") == scenario_name and item.get("experience") == entry["experience"] for item in existing):
            existing.append(entry)
        if "experiencedModules" in stored or "experiencedScenarios" not in stored:
            stored["experiencedModules"] = json.dumps(existing[-100:], ensure_ascii=False)
        else:
            stored["experiencedScenarios"] = existing[-100:]
        write_json_atomic(path, stored)


def _build_room_archive(room_dir, info):
    """Build the room archive snapshot. Rooms are archived in place (kept usable
    for dice and history) rather than deleted/moved; the archive JSON is retained
    as an immutable history copy and participating players' character cards are
    updated with a three-line "参与过的模组" record."""
    archive = {
        "id": info.get("id"),
        "name": info.get("name"),
        "scenario_id": info.get("scenario_id"),
        "scenario_title": info.get("scenario_title"),
        "scenario_version": info.get("scenario_version"),
        "created_at": info.get("created_at"),
        "completed_at": info.get("completed_at") or _timestamp(),
        "archived_at": info.get("archived_at") or _timestamp(),
        "archived": True,
        "creator_id": info.get("creator_id"),
        "members": info.get("members", []),
        "messages": _read_messages(room_dir),
    }
    history_dir = current_app.config.get("HISTORY_DIR", HISTORY_DIR)
    if history_dir:
        history_file = Path(history_dir) / f"room-{info.get('id')}-kp.json"
        archive["ai_history"] = read_json(history_file, default=[])
    archive_dir = _get_archives_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{info.get('id')}.json"
    write_json_atomic(archive_path, archive)
    _append_character_archive_records(info)
    return archive


def _configured_role(role_id="kp"):
    roles = load_roles(_get_role_config_file(), _get_kp_prompt_file(), _get_ai_platform_dir())
    expected = str(role_id or "kp")
    return next((role for role in roles if str(role.get("id")) == expected), roles[0] if roles else {})


def _new_room_code():
    existing = {
        _read_room(room_dir).get("room_code")
        for room_dir in _iter_room_dirs()
    }
    while True:
        code = uuid4().hex[:8].upper()
        if code not in existing:
            return code


def _room_permission_label(member, info):
    if str(member.get("role") or "") in {"ADMIN", "OWNER"}:
        label = "\u7ba1\u7406\u5458"
    elif member.get("room_role") == ROOM_ROLE_OWNER or str(member.get("user_id")) == str(info.get("creator_id")):
        label = "\u623f\u4e3b"
    elif member.get("room_role") == ROOM_ROLE_ADMIN:
        label = "\u7ba1\u7406\u5458"
    else:
        label = "\u6210\u5458"
    if not _is_active_member(member):
        return f"{label}\uff08\u5df2\u79fb\u9664\uff09"
    return label


def _is_member_online(member, room_id=None):
    """成员在该房间是否显示为「在线」。

    仅当用户既在线、又已经进入这个房间时才算在线：用户在线但停留在别的房间/页面时，
    不应在本房间的成员列表里显示为在线。
    """
    if not _is_active_member(member):
        return False
    user_id = member.get("user_id")
    if user_id is None or not is_socket_user_online(user_id):
        return False
    # 未提供 room_id 时退回到旧的「全局在线」判定，兼容不传房间的调用方。
    if room_id in (None, ""):
        return True
    return is_user_in_room(user_id, room_id)


def _room_has_online_member(info):
    """房间内是否存在「已进入该房间且在线」的成员（用于自动存档判定）。"""
    room_id = (info or {}).get("id")
    for member in (info or {}).get("members") or []:
        if isinstance(member, dict) and _is_member_online(member, room_id):
            return True
    return False


def _live_member_role(member):
    """实时读取账号角色：用户被降级/升级后，房间成员快照里的旧角色不再生效。"""
    fallback = member.get("role")
    user_id = member.get("user_id")
    if user_id is None:
        return fallback
    try:
        manager = get_user_manager()
        user = manager.get_user_by_id(user_id) if hasattr(manager, "get_user_by_id") else None
    except Exception:  # noqa: BLE001 - 用户查询失败时回退到房间快照，不影响房间读取
        user = None
    return (user or {}).get("role") or fallback


def _room_visibility(info):
    value = str((info or {}).get("visibility") or ROOM_VISIBILITY_PRIVATE).strip().lower()
    return value if value in ROOM_VISIBILITIES else ROOM_VISIBILITY_PRIVATE


def _room_summary(info):
    _normalize_members(info)

    def _member_view(member):
        # 角色以账号数据库为准，避免降级后成员仍显示为管理员配色/标签。
        role = _live_member_role(member)
        view = {**member, "role": role}
        view["is_active"] = _is_active_member(member)
        view["is_online"] = _is_member_online(member, info.get("id"))
        view["permission_label"] = _room_permission_label(view, info)
        return view

    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "room_code": info.get("room_code"),
        "scenario_id": info.get("scenario_id"),
        "scenario_version": info.get("scenario_version"),
        "scenario_title": info.get("scenario_title"),
        "visibility": _room_visibility(info),
        "scenario_started_at": info.get("scenario_started_at"),
        "scenario_started_by": info.get("scenario_started_by"),
        "archived": bool(info.get("archived")),
        "archived_at": info.get("archived_at"),
        "completed_at": info.get("completed_at"),
        "creator_id": info.get("creator_id"),
        "creator_name": info.get("creator_name"),
        "members": [_member_view(member) for member in info.get("members", [])],
        "house_rules": _room_house_rules(info),
        # 全房间统一的骰娘大成功/大失败阈值（房规优先，其次管理员默认值）。
        "dice_thresholds": _room_dice_thresholds(info),
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
    }


def _invisible_room_view(room_dir, info):
    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "messages": _read_messages(room_dir),
        "invisible_view": True,
    }


def _created_room_count(user_id):
    return sum(1 for room_dir in _iter_room_dirs() if _read_room(room_dir).get("creator_id") == user_id)


@bp.route("/api/rooms", methods=["GET"])
def list_rooms():
    login_error = _require_login()
    if login_error:
        return login_error

    rooms = []
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        # 公开房间展示给所有玩家；私人房间仅成员可访问。
        if _can_access(info) or (_room_visibility(info) == ROOM_VISIBILITY_PUBLIC and not info.get("archived")):
            rooms.append(_room_summary(info))
    rooms.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return success_response(rooms, "Rooms loaded successfully")


@bp.route("/api/rooms", methods=["POST"])
def create_room():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    scenario_id = data.get("scenario_id")
    scenario_title = str(data.get("scenario_title", "")).strip()
    # 房间可见性默认为私人房间（仅能通过房间码加入）；公开房间会展示给所有玩家。
    visibility = str(data.get("visibility", ROOM_VISIBILITY_PRIVATE)).strip().lower()
    if visibility not in {ROOM_VISIBILITY_PUBLIC, ROOM_VISIBILITY_PRIVATE}:
        visibility = ROOM_VISIBILITY_PRIVATE
    if not name:
        return error_response("Please enter room name", 400, "Room name is required")
    if scenario_id is None:
        return error_response("Please choose a scenario", 400, "Scenario is required")
    if _room_name_exists(name):
        return error_response("Room name already exists", 409, "Room name already exists")
    if not _is_elevated() and _created_room_count(session["user_id"]) >= USER_ROOM_LIMIT:
        return error_response(
            "Room creation limit reached",
            403,
            "Room creation limit reached",
        )
    room_id = uuid4().hex
    room_dir = _room_dir(room_id)
    member = _current_member(data.get("character_card"))
    member["room_role"] = ROOM_ROLE_OWNER
    now = _timestamp()
    info = {
        "id": room_id,
        "name": name,
        "room_code": _new_room_code(),
        "scenario_id": scenario_id,
        "scenario_title": scenario_title,
        "visibility": visibility,
        "creator_id": session["user_id"],
        "creator_name": session.get("username", "user"),
        "members": [member],
        "created_at": now,
        "updated_at": now,
    }
    # Pin a deterministic starting scene in room state. The KP receives this
    # pointer and can only move it via room.activate_scenario_scene.
    try:
        from trpg_server.scenario_store import load_scenario_by_id
        _, bound_scenario = load_scenario_by_id(
            current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR), scenario_id
        )
        if bound_scenario:
            info["scenario_version"] = str(bound_scenario.get("scenario_version") or bound_scenario.get("version") or "1")
            scene = next((m for m in bound_scenario.get("modules", [])
                          if isinstance(m, dict) and str(m.get("module_type")) == "scene"), None)
            if scene:
                info["active_scene_id"] = str(scene.get("scene_id") or scene.get("id"))
                info["active_scene_title"] = scene.get("title")
    except Exception:
        logger.exception("Failed to initialize room active scene")
    write_json_atomic(room_dir / "info.json", info)
    _write_messages(room_dir, [])
    write_json_atomic(room_dir / "autosave.json", {"updated_at": now, "messages": []})

    log_user_action(
        logger,
        user_action_text(session.get("username"), "创建了房间"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        房间码=info["room_code"],
        房间名=name,
        剧本=scenario_title,
    )
    data = _room_summary(info)
    data["messages"] = []
    return success_response(data, "Room created successfully", 201)


@bp.route("/api/rooms/spectate", methods=["POST"])
def spectate_room_by_code():
    login_error = _require_login()
    if login_error:
        return login_error
    if not _is_elevated():
        return _denied()

    data = request.get_json(silent=True) or {}
    room_dir, info = _find_room_by_code(data.get("room_code"))
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return success_response(_invisible_room_view(room_dir, info), "Room spectated successfully")


@bp.route("/api/rooms/join", methods=["POST"])
def join_room_by_code():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    room_dir, info = _find_room_by_code(data.get("room_code"))
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")

    user_id = session["user_id"]
    character_card = data.get("character_card")
    member = _find_member(info, user_id=user_id)
    if not member:
        info.setdefault("members", []).append(_current_member(character_card))
        _write_room(room_dir, info)
    elif not _is_active_member(member):
        current_member = _current_member(character_card)
        member.update(
            {
                "username": current_member["username"],
                "role": current_member["role"],
                "avatar": current_member["avatar"],
                "status": ROOM_MEMBER_ACTIVE,
                "is_active": True,
                "rejoined_at": _timestamp(),
            }
        )
        if character_card:
            _bind_character(member, character_card)
        _write_room(room_dir, info)
    elif character_card and not member.get("character_card"):
        _bind_character(member, character_card)
        _write_room(room_dir, info)

    log_user_action(
        logger,
        user_action_text(session.get("username"), "加入了房间"),
        用户ID=user_id,
        房间ID=info["id"],
        房间码=info.get("room_code"),
        房间名=info.get("name"),
    )
    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    return success_response(data, "Room joined successfully")


@bp.route("/api/rooms/<room_id>/members/<user_id>/character", methods=["PUT"])
def bind_room_member_character(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if str(target.get("user_id")) != str(session["user_id"]) and not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    character_card = data.get("character_card")
    if not _can_bind_character_card(character_card, target):
        return _denied(room_id)
    if not _bind_character(target, character_card):
        return error_response("Character card is required", 400, "Character card is required")
    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "绑定了房间角色卡"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        目标用户=user_id,
        角色卡ID=target.get("character_card", {}).get("id"),
        角色名=target.get("character_card", {}).get("name"),
    )
    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    return success_response(data, "Character bound successfully")


@bp.route("/api/rooms/<room_id>/members/<user_id>", methods=["DELETE"])
def delete_room_member(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner cannot be removed", 400, "Room owner cannot be removed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    target["status"] = ROOM_MEMBER_REMOVED
    target["is_active"] = False
    target["removed_at"] = _timestamp()
    if target.get("room_role") == ROOM_ROLE_ADMIN:
        target["room_role"] = ROOM_ROLE_MEMBER
    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "移除了房间成员"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        目标用户=user_id,
        房间名=info.get("name"),
    )
    return success_response(_room_summary(info), "Player removed")


@bp.route("/api/rooms/<room_id>/members/<user_id>/role", methods=["PUT"])
def update_room_member_role(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner role cannot be changed", 400, "Room owner role cannot be changed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    data = request.get_json(silent=True) or {}
    role = data.get("room_role")
    if role not in {ROOM_ROLE_ADMIN, ROOM_ROLE_MEMBER}:
        return error_response("Invalid room role", 400, "Invalid room role")
    target["room_role"] = role
    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "更改了房间成员权限"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        目标用户=user_id,
        权限=role,
    )
    return success_response(_room_summary(info), "Room role updated")


@bp.route("/api/rooms/<room_id>", methods=["GET"])
def get_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    return success_response(data, "Room loaded successfully")


@bp.route("/api/rooms/<room_id>/scenario-migration", methods=["POST"])
def migrate_room_scenario(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    payload = request.get_json(silent=True) or {}
    target_version = payload.get("scenario_version")
    _, target = load_scenario_by_id(
        current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR),
        info.get("scenario_id"),
        scenario_version=target_version,
    )
    if not target:
        return error_response("Target scenario version not found", 404, "Scenario version not found")
    state = read_json(room_dir / "state.json", default={})
    old_entities = {str(item.get("id")) for item in payload.get("old_entities", []) if isinstance(item, dict)}
    if not old_entities:
        for key in ("clues", "items", "quests", "triggered_event_ids"):
            values = state.get(key, []) if isinstance(state, dict) else []
            if isinstance(values, list):
                old_entities.update(str(item.get("id") if isinstance(item, dict) else item) for item in values)
    new_entities = {str(item.get("id")) for item in target.get("modules", []) if isinstance(item, dict) and item.get("id")}
    try:
        updated = migrate_room_binding(info, target, payload.get("mapping") or {}, old_entities, new_entities)
    except ValueError as exc:
        return error_response(str(exc), 400, "Scenario migration rejected")
    write_json_atomic(room_dir / "info.json", updated)
    return success_response(_room_summary(updated), "Room migrated to scenario version")


def _first_scene(target: dict) -> dict | None:
    """取剧本的第一个场景模块，作为换绑后的开场场景指针。"""
    return next(
        (m for m in (target or {}).get("modules", [])
         if isinstance(m, dict) and str(m.get("module_type")) == "scene"),
        None,
    )


@bp.route("/api/rooms/<room_id>/scenario-switch", methods=["POST"])
def switch_room_scenario(room_id):
    """管理员/房主强制把房间切换到另一个剧本（高风险：会重置剧情进度）。

    与 ``scenario-migration``（同一剧本内换版本、尽量保留绑定）不同，本接口是换到
    一个完全不同的剧本，旧的线索/物品/任务/已触发事件/场景指针/滚动摘要都会失效，
    因此整体重置为新剧本的开场状态，并要求重新「开启剧本」。
    """
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("archived"):
        return error_response("Archived room cannot switch scenario", 409, "Room archived")

    payload = request.get_json(silent=True) or {}
    target_id = payload.get("scenario_id")
    if target_id in (None, ""):
        return error_response("Please choose a scenario", 400, "Scenario is required")
    # 剧情进度是否继承：换到同一剧本的新版本时应保留进度继续游玩，换到不同剧本时
    # 通常需要重置。由调用方决定，默认重置（更安全）。
    preserve_progress = bool(payload.get("preserve_progress"))

    scenarios_dir = current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)
    _, target = load_scenario_by_id(scenarios_dir, target_id)
    if not target:
        return error_response("Target scenario not found", 404, "Scenario not found")

    previous_title = info.get("scenario_title")
    updated = dict(info)
    updated["scenario_id"] = target_id
    updated["scenario_version"] = str(target.get("scenario_version") or target.get("version") or "1")
    updated["scenario_title"] = str(target.get("title") or payload.get("scenario_title") or "").strip() or f"剧本 {target_id}"
    # 场景指针：继承进度时，只要旧场景在新剧本中仍存在就保留，避免打断当前场景；
    # 否则回退到新剧本的第一个场景。
    target_scene_ids = {
        str(m.get("scene_id") or m.get("id"))
        for m in target.get("modules", [])
        if isinstance(m, dict) and str(m.get("module_type")) == "scene"
    }
    first_scene = _first_scene(target)
    first_scene_id = str(first_scene.get("scene_id") or first_scene.get("id")) if first_scene else ""
    current_scene_id = str(updated.get("active_scene_id") or "")
    if not (preserve_progress and current_scene_id and current_scene_id in target_scene_ids):
        if first_scene_id:
            updated["active_scene_id"] = first_scene_id
            updated["active_scene_title"] = first_scene.get("title")
        else:
            updated.pop("active_scene_id", None)
            updated.pop("active_scene_title", None)
    # 重置模式下新剧本必须重新开场：清掉旧剧本的「已开始」标记，避免沿用其开场门控。
    # 继承模式下保留该标记，视为同一场剧情的延续。
    if not preserve_progress:
        updated.pop("scenario_started_at", None)
        updated.pop("scenario_started_by", None)
    updated["updated_at"] = _timestamp()
    write_json_atomic(room_dir / "info.json", updated)

    if preserve_progress:
        # 继承进度：保留线索/物品/任务/已触发事件/滚动摘要与开场状态，仅同步场景指针。
        state = read_json(room_dir / "state.json", default={})
        if not isinstance(state, dict):
            state = {}
        if updated.get("active_scene_id"):
            state["active_scene_id"] = updated["active_scene_id"]
        save_room_state(room_dir, state)
    else:
        # 重置剧情进度：线索/物品/任务/已触发事件/时间线/事件日志/滚动摘要全部清空，
        # 否则旧剧本的进度会被当作新剧本的状态继续注入，正是「剧本错乱」的来源。
        # save_room_state 会以默认空状态为底做归一化，因此这里只给出新的场景指针。
        save_room_state(room_dir, {"active_scene_id": first_scene_id or None})
        # 知识库检索游标按「剧本@版本」绑定，换剧本后本就会失效；显式删除以清理残留。
        try:
            (room_dir / "knowledge_cursor.json").unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove knowledge cursor on scenario switch", exc_info=True)

    log_user_action(
        logger,
        user_action_text(session.get("username"), "强制切换了房间剧本"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        房间码=updated.get("room_code"),
        原剧本=previous_title,
        新剧本=updated["scenario_title"],
    )
    return success_response(_room_summary(updated), "Room scenario switched")


@bp.route("/api/rooms/<room_id>/spectate", methods=["GET"])
def spectate_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if not _is_elevated():
        return _denied(room_id)

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return success_response(_invisible_room_view(room_dir, info), "Room spectated successfully")


@bp.route("/api/rooms/<room_id>", methods=["DELETE"])
def delete_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("completed_at") and not info.get("archived"):
        return error_response("Please archive the completed room first", 409, "Completed rooms must be archived so their messages are preserved")

    import shutil

    shutil.rmtree(room_dir)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了房间"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        房间名=info.get("name"),
    )
    return success_response(message="Room deleted successfully")


@bp.route("/api/rooms/<room_id>/archive", methods=["POST"])
def archive_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("archived"):
        return error_response("Room already archived", 409, "Room already archived")
    state = read_json(room_dir / "state.json", default={})
    if not _scenario_finished(info, state):
        return error_response("Room is not finished", 409, "Room must reach an ending before archiving")
    now = _timestamp()
    info["completed_at"] = info.get("completed_at") or now
    info["archived_at"] = now
    info["archived"] = True
    # House rules are scoped to an active room; an archived room no longer needs
    # them (the immutable archive snapshot is built from a fixed field whitelist).
    info.pop("house_rules", None)
    _write_room(room_dir, info)
    archive = _build_room_archive(room_dir, info)
    log_user_action(logger, user_action_text(session.get("username"), "归档了房间"), 用户ID=session.get("user_id"), 房间ID=room_id, 房间名=info.get("name"))
    return success_response({"id": archive["id"], "name": archive["name"], "archived_at": archive["archived_at"]}, "Room archived successfully")


@bp.route("/api/rooms/<room_id>/house-rules", methods=["GET"])
def get_room_house_rules(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    return success_response(
        {
            "house_rules": _room_house_rules(info),
            "definitions": HOUSE_RULE_DEFINITIONS,
            "global_hints_enabled": _global_hints_enabled(),
            "visibility": _room_visibility(info),
            "dice_thresholds": _room_dice_thresholds(info),
            "dice_threshold_defaults": _admin_dice_thresholds(),
        },
        "House rules loaded successfully",
    )


@bp.route("/api/rooms/<room_id>/house-rules", methods=["PUT"])
def update_room_house_rules(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    payload = request.get_json(silent=True) or {}
    updates = payload.get("house_rules", payload)
    if not isinstance(updates, dict):
        return error_response("Invalid house rules payload", 400, "house_rules must be an object")

    current = _room_house_rules(info)
    for key, value in updates.items():
        spec = HOUSE_RULE_DEFINITIONS.get(key)
        if not spec:
            return error_response(f"Unknown house rule: {key}", 400, "Unknown house rule")
        if spec["type"] == "boolean":
            if not isinstance(value, bool):
                return error_response(f"House rule {key} must be a boolean", 400, "Invalid house rule type")
            current[key] = value
        elif spec["type"] == "nullable_integer":
            if value is not None and value != "":
                try:
                    int(value)
                except (TypeError, ValueError):
                    return error_response(f"House rule {key} must be an integer", 400, "Invalid house rule type")
            current[key] = _normalize_nullable_int(value, spec.get("min", 0), spec.get("max", 100))
        elif spec["type"] == "skill_bases":
            if not isinstance(value, dict):
                return error_response(f"House rule {key} must be an object", 400, "Invalid house rule type")
            current[key] = _normalize_skill_bases(value)
        else:
            current[key] = value

    enabled_by_request = {
        key for key, spec in HOUSE_RULE_DEFINITIONS.items()
        if spec.get("ai_prompt") and current.get(key) and updates.get(key)
    }
    if enabled_by_request and not _global_hints_enabled():
        return error_response(
            "全局 AI 提示开关已关闭，无法开启该房规",
            400,
            "Global AI hints are disabled",
        )

    info["house_rules"] = current
    # 房间可见性可以在后续房间规则内更改（私人 <-> 公开）。
    visibility_update = payload.get("visibility")
    if visibility_update is not None:
        candidate = str(visibility_update).strip().lower()
        if candidate not in ROOM_VISIBILITIES:
            return error_response("Invalid room visibility", 400, "Invalid visibility")
        info["visibility"] = candidate
    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "更新了房间房规"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        房间名=info.get("name"),
    )
    return success_response(
        {
            "house_rules": _room_house_rules(info),
            "definitions": HOUSE_RULE_DEFINITIONS,
            "global_hints_enabled": _global_hints_enabled(),
            "visibility": _room_visibility(info),
            "dice_thresholds": _room_dice_thresholds(info),
            "dice_threshold_defaults": _admin_dice_thresholds(),
        },
        "House rules updated successfully",
    )


@bp.route("/api/room-archives", methods=["GET"])
@bp.route("/api/rooms/archives", methods=["GET"])
def list_room_archives():
    login_error = _require_login()
    if login_error:
        return login_error
    archives = []
    archive_dir = _get_archives_dir()
    for path in archive_dir.glob("*.json") if archive_dir.exists() else []:
        archive = read_json(path, default={})
        if isinstance(archive, dict) and _archive_visible(archive):
            archives.append({key: archive.get(key) for key in ("id", "name", "scenario_title", "archived_at", "completed_at")})
    archives.sort(key=lambda item: str(item.get("archived_at") or ""), reverse=True)
    return success_response(archives, "Room archives loaded successfully")


@bp.route("/api/room-archives/<room_id>", methods=["GET"])
@bp.route("/api/rooms/<room_id>/archive", methods=["GET"])
def get_room_archive(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    archive = read_json(_get_archives_dir() / f"{room_id}.json", default={})
    if not isinstance(archive, dict) or not archive:
        return error_response("Room archive not found", 404, "Room archive not found")
    if not _archive_visible(archive):
        return _denied(room_id)
    return success_response(archive, "Room archive loaded successfully")


@bp.route("/api/rooms/<room_id>/messages", methods=["GET"])
def get_room_messages(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    return success_response(_read_messages(room_dir), "Room messages loaded successfully")


@bp.route("/api/rooms/<room_id>/messages", methods=["POST"])
def create_room_message(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return error_response("Please provide message content", 400, "No message content")

    member = _find_member(info, user_id=session["user_id"], active_only=True)
    if not member:
        return _denied(room_id)
    message = {
        "id": uuid4().hex,
        "type": data.get("type", "player"),
        "sender_id": member["user_id"],
        "sender_name": member["username"],
        "avatar": member["avatar"],
        "content": content,
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": data.get("metadata", {}),
    }
    if message["type"] == "kp":
        role = _configured_role((message.get("metadata") or {}).get("roleId") or data.get("role_id") or "kp")
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or role.get("name") or "KP"
        message["avatar"] = normalize_avatar(data.get("avatar") or role.get("avatar"))
    elif message["type"] == "dice":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "骰娘")
        message["avatar"] = ""
    elif message["type"] == "system":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "系统")
        message["avatar"] = ""
    elif message["type"] == "trigger":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or "触发器"
        message["avatar"] = normalize_avatar(data.get("avatar"))

    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)

    log_user_action(
        logger,
        user_action_text(session.get("username"), "发送了房间消息"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        类型=message["type"],
        内容长度=len(content),
    )
    return success_response(message, "Room message saved successfully", 201)


@bp.route("/api/rooms/<room_id>/triggers", methods=["POST"])
def trigger_room_scenario(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    trigger_id = data.get("trigger_id")
    if trigger_id in (None, ""):
        return error_response("Please provide trigger id", 400, "No trigger id")

    scenarios_dir = _get_scenarios_dir()
    from trpg_server.scenario_store import build_trigger_message, load_scenario_by_id

    _, scenario = load_scenario_by_id(scenarios_dir, info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    if not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")

    room_state = read_json(room_dir / "state.json", default={})
    new_trigger = find_trigger_definition(scenario, str(trigger_id))
    if new_trigger:
        validation = validate_trigger(str(trigger_id), {**(room_state if isinstance(room_state, dict) else {}), "scenario_version": info.get("scenario_version")}, scenario, audience="kp")
        if not validation.get("ok"):
            logger.info("trigger_rejected room_id=%s trigger_id=%s reason=%s", room_id, trigger_id, validation.get("reason"))
            return error_response("Trigger condition is not satisfied", 409, validation.get("reason", "Trigger rejected"))
        attachments = validation["trigger"].get("attachments", [])
        contents = []
        for attachment in attachments:
            resource = attachment.get("resourceRef") or {}
            if resource.get("content"):
                contents.append(str(resource["content"]))
            elif resource.get("url"):
                contents.append(f"[{resource.get('alt')}]({resource.get('url')})")
        trigger_message = {"sender_name": validation["trigger"].get("text") or f"触发器{trigger_id}", "content": "\n\n".join(contents), "metadata": {"trigger_id": trigger_id, "content_mode": "attachment"}}
    else:
        trigger_message = build_trigger_message(scenario, trigger_id)
    if not trigger_message:
        return error_response("Trigger not found", 404, "Trigger not found")

    message = {
        "id": uuid4().hex,
        "type": "trigger",
        "sender_id": None,
        "sender_name": trigger_message.get("sender_name", "触发器"),
        "avatar": normalize_avatar(trigger_message.get("avatar")),
        "content": trigger_message.get("content", ""),
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": trigger_message.get("metadata", {}),
    }
    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)
    if new_trigger:
        record_trigger(room_dir, str(trigger_id), reason=str(data.get("reason") or "manual trigger"))
    log_user_action(
        logger,
        user_action_text(session.get("username"), "触发了场景触发器"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        触发器ID=trigger_id,
    )
    return success_response(message, "Trigger executed successfully", 201)


@bp.route("/api/rooms/<room_id>/character-records", methods=["POST"])
def create_character_record(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    return _create_character_record_for_room(room_dir, info)


def _create_character_record_for_room(room_dir, info):
    data = request.get_json(silent=True) or {}
    record_type = str(data.get("type", "")).strip().lower()
    if record_type not in {"damage", "san", "skill", "item"}:
        return error_response("Invalid record type", 400, "Invalid record type")

    target = _find_member(info, user_id=data.get("user_id"), username=data.get("username"), active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if not target.get("character_card"):
        return error_response("Player has no bound character card", 400, "No bound character card")

    value = _bounded_int(data.get("value"), 1, 999, 1)
    reason = str(data.get("reason") or "未知").strip() or "未知"
    state = _ensure_character_state(target)
    record = {
        "id": uuid4().hex,
        "type": record_type,
        "value": value,
        "reason": reason[:200],
        "created_at": _timestamp(),
        "created_by": session.get("user_id"),
        "created_by_name": session.get("username", "user"),
    }

    if record_type == "damage":
        state["current_hp"] = max(0, state["current_hp"] - value)
        record["hp_after"] = state["current_hp"]
        state["injury_records"].insert(0, record)
    elif record_type == "san":
        state["current_san"] = max(0, state["current_san"] - value)
        if isinstance(target.get("character_card"), dict):
            target["character_card"]["currentSan"] = state["current_san"]
            target["character_card"]["current_san"] = state["current_san"]
        record["san_after"] = state["current_san"]
        state["sanity_records"].insert(0, record)
    else:
        # skill / item change records (e.g. 技能增减、获得物品) feed the
        # "其他变化" line of the participating-module record on the player card.
        record["target"] = str(data.get("target") or record.get("name") or "").strip()[:80]
        record["change"] = str(data.get("change") or data.get("operation") or data.get("action") or "").strip()[:40] or "变化"
        record["amount"] = data.get("amount")
        collection = "skill_changes" if record_type == "skill" else "item_changes"
        state.setdefault(collection, []).insert(0, record)

    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "记录了角色状态变化"),
        用户ID=session.get("user_id"),
        房间ID=info.get("id"),
        类型=record_type,
        目标玩家=target.get("username"),
        数值=value,
        原因=reason,
    )
    return success_response({"member": target, "record": record}, "Character record created", 201)


@bp.route("/api/rooms/by-name/<path:room_name>/character-records", methods=["POST"])
def create_character_record_by_room_name(room_name):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room_by_name(room_name)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return _create_character_record_for_room(room_dir, info)


@bp.route("/api/rooms/<room_id>/character-records/<record_id>", methods=["DELETE"])
def delete_character_record(room_id, record_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _is_elevated():
        return _denied(room_id)

    for member in info.get("members", []):
        state = _ensure_character_state(member)
        for collection_name in ("injury_records", "sanity_records", "skill_changes", "item_changes"):
            records = state.get(collection_name, [])
            next_records = [record for record in records if record.get("id") != record_id]
            if len(next_records) != len(records):
                state[collection_name] = next_records
                _recalculate_character_state(state)
                _write_room(room_dir, info)
                log_user_action(
                    logger,
                    user_action_text(session.get("username"), "删除了角色状态记录"),
                    用户ID=session.get("user_id"),
                    房间ID=room_id,
                    记录ID=record_id,
                )
                return success_response({"member": member}, "Character record deleted")

    return error_response("Record not found", 404, "Record not found")


@bp.route("/api/rooms/<room_id>/nodes", methods=["GET"])
def list_room_nodes(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    nodes_dir = room_dir / "nodes"
    nodes = []
    for node_file in nodes_dir.glob("*.json") if nodes_dir.exists() else []:
        if node_file.name == "autosave.json":
            continue
        node = read_json(node_file, default={})
        nodes.append(
            {
                "filename": node_file.name,
                "created_at": node.get("created_at", ""),
                "message_count": len(node.get("messages", [])),
                "automatic": bool(node.get("automatic")),
            }
        )
    nodes.sort(key=lambda item: item["created_at"], reverse=True)
    return success_response({"info": _room_summary(info), "nodes": nodes}, "Room nodes loaded successfully")


@bp.route("/api/rooms/<room_id>/nodes", methods=["POST"])
def create_room_node(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    timestamp = int(time.time() * 1000)
    node = {
        "filename": f"{timestamp}.json",
        "created_at": _timestamp(),
        "automatic": False,
        "messages": _read_messages(room_dir),
    }
    write_json_atomic(room_dir / "nodes" / node["filename"], node)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "创建了房间回档节点"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        文件=node["filename"],
    )
    return success_response(node, "Room node created successfully", 201)


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["GET"])
def get_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")
    return success_response(read_json(node_file, default={}), "Room node loaded successfully")


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>/restore", methods=["POST"])
def restore_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")

    node = read_json(node_file, default={"messages": []})
    _write_messages(room_dir, node.get("messages", []))
    _write_room(room_dir, info)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "恢复了房间回档节点"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        文件=node_filename,
    )
    return success_response(node, "Room node restored successfully")


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["DELETE"])
def delete_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if node_file.exists():
        node_file.unlink()
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了房间回档节点"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        文件=node_filename,
    )
    return success_response(message="Room node deleted successfully")


def _write_room_autosave(room_dir, room_id, messages, *, username=None, user_id=None):
    """写入自动存档节点并裁剪旧节点。

    ``username`` 为空表示由服务端后台调度触发（记录为系统动作）；传入时保留
    玩家手动触发路径的操作日志。
    """
    timestamp = int(time.time() * 1000)
    autosave = {
        "filename": f"autosave-{timestamp}.json",
        "updated_at": _timestamp(),
        "created_at": _timestamp(),
        "automatic": True,
        "messages": messages,
    }
    nodes_dir = room_dir / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(nodes_dir / autosave["filename"], autosave)
    write_json_atomic(room_dir / "autosave.json", autosave)
    automatic_nodes = []
    for node_file in nodes_dir.glob("autosave-*.json"):
        node = read_json(node_file, default={})
        if node.get("automatic"):
            automatic_nodes.append((str(node.get("created_at") or ""), node_file))
    automatic_nodes.sort(key=lambda item: item[0], reverse=True)
    for _, old_file in automatic_nodes[_autosave_node_limit():]:
        old_file.unlink(missing_ok=True)
    if username:
        log_user_action(
            logger,
            user_action_text(username, "保存了房间自动存档"),
            用户ID=user_id,
            房间ID=room_id,
            消息数=len(messages),
        )
    else:
        log_user_action(logger, "系统保存了房间自动存档", 房间ID=room_id, 消息数=len(messages))
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_autosave_created", {"room_id": room_id, "filename": autosave["filename"]}, room=room_id)
    return autosave


def run_scheduled_autosaves():
    """后台调度入口：为开启自动存档且到期的房间写入自动存档节点。

    由 app_factory 启动的守护线程周期调用，需要处于 Flask 应用上下文中。
    """
    settings = _autosave_settings()
    if not settings["enabled"]:
        return
    interval = settings["interval"]
    now = time.time()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if not info or info.get("archived"):
            continue
        # 仅当房间内确实有「已进入该房间且在线」的玩家时才自动存档：
        # 玩家在线但没进入这个房间，不应对该房间产生自动存档。
        if not _room_has_online_member(info):
            continue
        autosave_path = room_dir / "autosave.json"
        try:
            if autosave_path.exists() and now - autosave_path.stat().st_mtime < interval:
                continue
        except OSError:
            continue
        messages = _read_messages(room_dir)
        if not messages:
            continue
        room_id = info.get("id") or room_dir.name
        try:
            _write_room_autosave(room_dir, room_id, messages)
        except OSError:
            logger.debug("Scheduled autosave failed for room %s", room_id, exc_info=True)


@bp.route("/api/rooms/<room_id>/autosave", methods=["POST"])
def save_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = _write_room_autosave(
        room_dir,
        room_id,
        _read_messages(room_dir),
        username=session.get("username"),
        user_id=session.get("user_id"),
    )
    return success_response(autosave, "Room autosave saved successfully")


@bp.route("/api/rooms/<room_id>/autosave", methods=["GET"])
def load_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = read_json(room_dir / "autosave.json", default={"messages": []})
    return success_response(autosave, "Room autosave loaded successfully")
