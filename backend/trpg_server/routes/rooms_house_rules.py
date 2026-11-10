"""房间房规路由：世界意志状态、房规读取与更新。"""
from flask import request
from flask import session
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.immersive.config import AUDIO_BLOCKS
from trpg_server.immersive.config import MUSIC_BLOCKS
from trpg_server.json_store import read_json
from trpg_server.logging_config import user_action_text
from trpg_server.responses import error_response
from trpg_server.responses import success_response
from trpg_server.room_log import log_room_event
from trpg_server.room_mode import normalize_room_mode
from trpg_server.room_ops.access import (
    _can_access,
    _can_manage,
    _denied,
    _is_active_member,
    _require_login,
    world_override_allowed,
)
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.constants import HOUSE_RULE_DEFINITIONS, ROOM_VISIBILITIES
from trpg_server.room_ops.house_rules import (
    _admin_dice_thresholds,
    _normalize_bgm_url,
    _normalize_bgm_url_list,
    _normalize_nullable_int,
    _normalize_skill_bases,
    _normalize_user_ids,
    _room_dice_animation,
    _room_dice_thresholds,
    _room_house_rules,
)
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _find_room,
    _get_config_dir,
    _global_hints_enabled,
    _write_room,
)
from trpg_server.room_ops.views import _room_visibility
from trpg_server.world_override import rate_limit_snapshot
from trpg_server.world_override import resolve_policy

@bp.route("/api/rooms/<room_id>/world-override", methods=["GET"])
def get_world_override_status(room_id):
    """世界意志直执行：当前用户是否有权使用，以及房管可配置的成员白名单。"""
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    rules = _room_house_rules(info)
    mode = normalize_room_mode(info)
    # 世界意志只依赖 AI KP：scripted 与 freeform 都可用，manual 不提供（前端据此隐藏入口）。
    supported = bool(mode["ai_kp_enabled"])
    state = read_json(room_dir / "state.json", default={}) if room_dir else {}
    rate = rate_limit_snapshot(
        state,
        session.get("user_id"),
        policy=resolve_policy(_get_config_dir(), rules),
    )
    members = []
    for member in info.get("members") or []:
        if not isinstance(member, dict) or not _is_active_member(member):
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        members.append(
            {
                "user_id": str(member.get("user_id") or ""),
                "username": str(member.get("username") or ""),
                "character_name": str(card.get("name") or ""),
            }
        )
    return success_response(
        {
            "supported": supported,
            "allowed": supported and world_override_allowed(info),
            "enabled": supported and bool(rules.get("world_override_enabled", True)),
            "user_ids": rules.get("world_override_user_ids") or [],
            "can_configure": supported and _can_manage(info),
            "rate": rate,
            "members": members,
        },
        "World override status loaded successfully",
    )

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
            "dice_animation": _room_dice_animation(info),
            "dice_animation_defaults": {
                "level": load_ai_runtime_config(_get_config_dir()).dice_animation_level,
                "player_roll": load_ai_runtime_config(_get_config_dir()).dice_player_roll,
                "reroll_mode": load_ai_runtime_config(_get_config_dir()).dice_reroll_mode,
                "reroll_limit": load_ai_runtime_config(_get_config_dir()).dice_reroll_limit,
            },
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
        elif spec["type"] == "nullable_boolean":
            if value is not None and not isinstance(value, bool):
                return error_response(f"House rule {key} must be a boolean or null", 400, "Invalid house rule type")
            current[key] = value
        elif spec["type"] == "nullable_enum":
            if value is not None:
                candidate = str(value).strip().lower()
                if candidate not in spec.get("values", []):
                    return error_response(f"House rule {key} must be one of {spec.get('values')} or null", 400, "Invalid house rule type")
                current[key] = candidate
            else:
                current[key] = None
        elif spec["type"] == "nullable_integer":
            if value is not None and value != "":
                try:
                    int(value)
                except (TypeError, ValueError):
                    return error_response(f"House rule {key} must be an integer", 400, "Invalid house rule type")
            current[key] = _normalize_nullable_int(value, spec.get("min", 0), spec.get("max", 100))
        elif spec["type"] == "audio_blocks":
            if value is None:
                current[key] = None
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return error_response(
                    f"House rule {key} must be a list of audio blocks",
                    400,
                    "Invalid house rule type",
                )
            blocks = []
            for item in value:
                block = str(item or "").strip().lower()
                if block not in AUDIO_BLOCKS:
                    return error_response(
                        f"House rule {key} contains unknown block: {item}",
                        400,
                        "Invalid house rule type",
                    )
                if block not in blocks:
                    blocks.append(block)
            if len([block for block in blocks if block in MUSIC_BLOCKS]) > 1:
                return error_response(
                    f"House rule {key} allows at most one music block",
                    400,
                    "Invalid house rule type",
                )
            current[key] = blocks
        elif spec["type"] == "url":
            if value is None:
                current[key] = ""
                continue
            if not isinstance(value, str):
                return error_response(f"House rule {key} must be a URL string", 400, "Invalid house rule type")
            normalized = _normalize_bgm_url(value)
            if value.strip() and not normalized:
                return error_response(
                    f"House rule {key} must be an http(s) URL or a local music library path",
                    400,
                    "Invalid house rule type",
                )
            current[key] = normalized
        elif spec["type"] == "enum":
            candidate = str(value).strip().lower() if value is not None else ""
            if candidate not in spec.get("values", []):
                return error_response(
                    f"House rule {key} must be one of {spec.get('values')}",
                    400,
                    "Invalid house rule type",
                )
            current[key] = candidate
        elif spec["type"] == "url_list":
            if value is None:
                current[key] = []
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return error_response(
                    f"House rule {key} must be a list of URL strings",
                    400,
                    "Invalid house rule type",
                )
            raw_urls = [item.strip() for item in value if item.strip()]
            if any(not _normalize_bgm_url(item) for item in raw_urls):
                return error_response(
                    f"House rule {key} must contain only http(s) URLs or local music library paths",
                    400,
                    "Invalid house rule type",
                )
            current[key] = _normalize_bgm_url_list(raw_urls, spec.get("max_items", 20))
        elif spec["type"] == "skill_bases":
            if not isinstance(value, dict):
                return error_response(f"House rule {key} must be an object", 400, "Invalid house rule type")
            current[key] = _normalize_skill_bases(value)
        elif spec["type"] == "user_ids":
            if value is None:
                current[key] = []
                continue
            if not isinstance(value, list) or any(not isinstance(item, (str, int)) for item in value):
                return error_response(f"House rule {key} must be a list of user IDs", 400, "Invalid house rule type")
            current[key] = _normalize_user_ids(value, spec.get("max_items", 50))
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
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "更新了房间房规"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
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
            "dice_animation": _room_dice_animation(info),
            "dice_animation_defaults": {
                "level": load_ai_runtime_config(_get_config_dir()).dice_animation_level,
                "player_roll": load_ai_runtime_config(_get_config_dir()).dice_player_roll,
                "reroll_mode": load_ai_runtime_config(_get_config_dir()).dice_reroll_mode,
                "reroll_limit": load_ai_runtime_config(_get_config_dir()).dice_reroll_limit,
            },
        },
        "House rules updated successfully",
    )
