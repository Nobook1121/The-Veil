"""房间生命周期路由：创建/加入/查询/场景切换/删除/归档/邀请令牌。"""
from flask import current_app
from flask import request
from flask import session
from trpg_server.agents.room_state import save_room_state
from trpg_server.agents.versioning import migrate_room_binding
from trpg_server.json_store import read_json
from trpg_server.json_store import remove_tree
from trpg_server.json_store import write_json_atomic
from trpg_server.logging_config import log_user_action
from trpg_server.logging_config import user_action_text
from trpg_server.responses import error_response
from trpg_server.responses import success_response
from trpg_server.room_card_occupancy import card_binding_lock
from trpg_server.room_log import log_room_event
from trpg_server.room_mode import MODE_PRESETS
from trpg_server.room_mode import ROOM_MODES
from trpg_server.room_mode import ROOM_MODE_FREEFORM
from trpg_server.room_mode import ROOM_MODE_SCRIPTED
from trpg_server.room_mode import SCENARIO_BINDING_REQUIRED
from trpg_server.room_mode import accepts_scenario
from trpg_server.room_mode import normalize_room_mode
from trpg_server.room_ops.access import (
    _can_access,
    _can_manage,
    _current_member,
    _denied,
    _find_member,
    _is_active_member,
    _is_elevated,
    _require_login,
)
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.cards import _bind_character, _card_in_use_response, _find_card_conflict
from trpg_server.room_ops.constants import (
    ROOM_MEMBER_ACTIVE,
    ROOM_ROLE_OWNER,
    ROOM_VISIBILITY_PRIVATE,
    ROOM_VISIBILITY_PUBLIC,
    USER_ROOM_LIMIT,
)
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _created_room_count,
    _ensure_invite_token,
    _find_room,
    _find_room_by_code,
    _find_room_by_invite_token,
    _get_archives_dir,
    _iter_room_dirs,
    _new_invite_token,
    _new_room_code,
    _read_room,
    _room_dir,
    _room_name_exists,
    _timestamp,
    _write_messages,
    _write_room,
)
from trpg_server.room_ops.views import (
    _archive_visible,
    _build_room_archive,
    _invisible_room_view,
    _room_summary,
    _room_usage_for,
    _room_visibility,
    _scenario_finished,
)
from trpg_server.room_ops.visibility import visible_messages
from trpg_server.room_usage import load_room_usage
from trpg_server.ruleset import RulesetSelectionError
from trpg_server.ruleset import builtin_registry
from trpg_server.ruleset import default_ruleset_id
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.settings import SCENARIOS_DIR
from uuid import uuid4

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
            summary = _room_summary(info)
            usage = _room_usage_for(info, room_dir)
            if usage is not None:
                summary["usage"] = usage
            rooms.append(summary)
    rooms.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return success_response(rooms, "Rooms loaded successfully")

@bp.route("/api/rooms", methods=["POST"])
def create_room():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    # 房间模式预设：scripted（剧本 + AI）/ freeform（无剧本 + AI）/ manual（无 AI）。
    # 默认 scripted，旧客户端不传该字段时行为不变。
    room_mode = str(data.get("room_mode") or ROOM_MODE_SCRIPTED).strip().lower()
    if room_mode not in ROOM_MODES:
        return error_response("Unknown room mode", 400, "Unknown room mode")
    scenario_id = data.get("scenario_id")
    if scenario_id in (None, ""):
        scenario_id = None
    scenario_title = str(data.get("scenario_title", "")).strip()
    # 规则集在创建时固化：显式请求必须可选择（dnd5e 等开发中规则集会在此被拒绝），
    # 未指定时用默认规则集；剧本房间随后以剧本的 ruleset_id 为准。
    requested_ruleset = str(data.get("ruleset_id") or data.get("rulesetId") or "").strip()
    try:
        ruleset_id = builtin_registry().assert_selectable(requested_ruleset or default_ruleset_id()).id
    except RulesetSelectionError as exc:
        return error_response(exc.message, 400, exc.reason_code)
    # 房间可见性默认为私人房间（仅能通过房间码加入）；公开房间会展示给所有玩家。
    visibility = str(data.get("visibility", ROOM_VISIBILITY_PRIVATE)).strip().lower()
    if visibility not in {ROOM_VISIBILITY_PUBLIC, ROOM_VISIBILITY_PRIVATE}:
        visibility = ROOM_VISIBILITY_PRIVATE
    if not name:
        return error_response("Please enter room name", 400, "Room name is required")
    if room_mode == ROOM_MODE_FREEFORM and scenario_id is not None:
        return error_response("Freeform room cannot bind a scenario", 400, "Scenario binding is not allowed")
    if room_mode == ROOM_MODE_SCRIPTED and scenario_id is None:
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
    # 目录由建房方显式创建：房间锁不再替调用方 mkdir（否则已删除的房间会被
    # 后续写入“复活”，删除与写入在 Windows 上互相拉扯出 145）。
    room_dir.mkdir(parents=True, exist_ok=True)
    member = _current_member(data.get("character_card"))
    member["room_role"] = ROOM_ROLE_OWNER
    now = _timestamp()
    preset = MODE_PRESETS[room_mode]
    info = {
        "id": room_id,
        "name": name,
        "room_code": _new_room_code(),
        # 可轮换的邀请令牌：邀请链接/二维码用它生成，刷新后旧链接立即失效。
        "invite_token": _new_invite_token(),
        # 模式能力位：room_mode 负责展示与预设，两个布尔/枚举位负责实际校验。
        "room_mode": room_mode,
        "ai_kp_enabled": preset["ai_kp_enabled"],
        "scenario_binding": preset["scenario_binding"],
        "ruleset_id": ruleset_id,
        "visibility": visibility,
        "creator_id": session["user_id"],
        "creator_name": session.get("username", "user"),
        "members": [member],
        "created_at": now,
        "updated_at": now,
    }
    if scenario_id is not None:
        # manual 下剧本只作玩家自查资料，不驱动任何 AI 能力。
        info["scenario_id"] = scenario_id
        info["scenario_title"] = scenario_title
    # Pin a deterministic starting scene in room state. The KP receives this
    # pointer and can only move it via room.activate_scenario_scene.
    # 只有 scripted 房间需要场景指针；freeform 无剧本，manual 不跑 AI 流程。
    if room_mode == ROOM_MODE_SCRIPTED:
        try:
            from trpg_server.scenario_store import load_scenario_by_id
            _, bound_scenario = load_scenario_by_id(
                current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR), scenario_id
            )
            if bound_scenario:
                scenario_ruleset = str(bound_scenario.get("ruleset_id") or "").strip()
                if scenario_ruleset:
                    scenario_plugin = builtin_registry().resolve(scenario_ruleset)
                    if requested_ruleset and scenario_plugin.id != ruleset_id:
                        return error_response(
                            "房间规则集必须与剧本一致",
                            400,
                            "ruleset_mismatch",
                        )
                    ruleset_id = scenario_plugin.id
                    info["ruleset_id"] = ruleset_id
                info["scenario_version"] = str(bound_scenario.get("scenario_version") or bound_scenario.get("version") or "1")
                scene = next((m for m in bound_scenario.get("modules", [])
                              if isinstance(m, dict) and str(m.get("module_type")) == "scene"), None)
                if scene:
                    info["active_scene_id"] = str(scene.get("scene_id") or scene.get("id"))
                    info["active_scene_title"] = scene.get("title")
        except Exception:
            logger.exception("Failed to initialize room active scene")
    # 与加入房间共用同一把占用锁：建房也会绑定角色卡，同一张卡不能同时存在于两个房间。
    conflict = None
    with card_binding_lock():
        conflict = _find_card_conflict(member.get("character_card"))
        if not conflict:
            _write_room(room_dir, info)
    if conflict:
        return _card_in_use_response(conflict)
    _write_messages(room_dir, [])

    write_json_atomic(room_dir / "autosave.json", {"updated_at": now, "messages": []})

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "创建了房间"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间码=info["room_code"],
        房间名=name,
        模式=room_mode,
        剧本=scenario_title,
    )
    data = _room_summary(info)
    data["messages"] = []
    data["usage"] = load_room_usage(room_dir, str(room_id))
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

@bp.route("/api/rooms/lookup", methods=["GET"])
def lookup_room_by_code():
    """按房间码/邀请令牌查询最小的房间信息。

    加入房间前需要知道房间模式（无 AI 房间允许不绑定角色卡进入），
    但不能泄露成员列表与角色卡，因此只返回模式、名称与剧本标题。
    """
    login_error = _require_login()
    if login_error:
        return login_error
    invite_token = str(request.args.get("invite_token") or "").strip()
    if invite_token:
        room_dir, info = _find_room_by_invite_token(invite_token)
    else:
        room_dir, info = _find_room_by_code(request.args.get("code"))
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    mode = normalize_room_mode(info)
    return success_response(
        {
            "id": info.get("id"),
            "name": info.get("name"),
            "room_mode": mode["room_mode"],
            "ai_kp_enabled": mode["ai_kp_enabled"],
            "scenario_binding": mode["scenario_binding"],
            "scenario_title": info.get("scenario_title"),
            "archived": bool(info.get("archived")),
        }
    )

@bp.route("/api/rooms/join", methods=["POST"])
def join_room_by_code():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    invite_token = str(data.get("invite_token") or "").strip()
    if invite_token:
        # 通过邀请链接（?invite=<token>）加入：令牌被刷新后旧链接即失效。
        room_dir, info = _find_room_by_invite_token(invite_token)
        if not room_dir:
            return error_response(
                "Invite link is invalid or has expired",
                404,
                "Invite link is invalid or has expired",
            )
    else:
        room_dir, info = _find_room_by_code(data.get("room_code"))
        if not room_dir:
            return error_response("Room not found", 404, "Room not found")

    user_id = session["user_id"]
    character_card = data.get("character_card")
    # 「查占用 + 落盘」放在同一把锁里：同一张卡并发加入两个房间时只有一个能成功，
    # 同时也避免两个请求基于同一份旧快照互相覆盖。已在房间里的重复加入不算冲突。
    with card_binding_lock():
        info = _read_room(room_dir)
        member = _find_member(info, user_id=user_id)
        will_bind_card = bool(character_card) and (
            member is None or not _is_active_member(member) or not member.get("character_card")
        )
        if will_bind_card:
            conflict = _find_card_conflict(character_card, exclude_room_id=info.get("id") or room_dir.name)
            if conflict:
                return _card_in_use_response(conflict)
        changed = True
        if not member:
            info.setdefault("members", []).append(_current_member(character_card))
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
        elif character_card and not member.get("character_card"):
            _bind_character(member, character_card)
        else:
            # 已在房间里且无需绑卡：重复加入保持幂等，不写盘也不报冲突。
            changed = False
        if changed:
            _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "加入了房间"),
        actor=session.get("username"),
        room_id=info["id"],
        logger_=logger,
        用户ID=user_id,
        房间码=info.get("room_code"),
        房间名=info.get("name"),
    )
    data = _room_summary(info)
    data["messages"] = visible_messages(room_dir, info, user_id)
    usage = _room_usage_for(info, room_dir)
    if usage is not None:
        data["usage"] = usage
    return success_response(data, "Room joined successfully")

@bp.route("/api/rooms/<room_id>/invite-token", methods=["GET"])
def get_room_invite_token(room_id):
    """获取房间当前的邀请令牌（无则惰性生成），供前端拼出邀请链接与二维码。"""
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    token = _ensure_invite_token(room_dir, info)
    return success_response(
        {"invite_token": token, "can_rotate": _can_manage(info)},
        "Invite token loaded successfully",
    )

@bp.route("/api/rooms/<room_id>/invite-token", methods=["POST"])
def rotate_room_invite_token(room_id):
    """刷新房间邀请令牌：旧邀请链接/二维码随即失效（仅房主或管理员可操作）。"""
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    token = _new_invite_token()
    info["invite_token"] = token
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "刷新了房间邀请链接"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        房间码=info.get("room_code"),
        房间名=info.get("name"),
    )
    return success_response({"invite_token": token}, "Invite link refreshed successfully")

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
    data["messages"] = visible_messages(room_dir, info, session.get("user_id"))
    usage = _room_usage_for(info, room_dir)
    if usage is not None:
        data["usage"] = usage
    return success_response(data, "Room loaded successfully")

@bp.route("/api/rooms/<room_id>/usage", methods=["GET"])
def get_room_usage(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    return success_response(load_room_usage(room_dir, str(room_id)), "Room usage loaded successfully")

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
    if not accepts_scenario(info):
        return error_response("Freeform room has no scenario to migrate", 400, "Scenario binding is not allowed")
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
    updated["ruleset_id"] = builtin_registry().resolve(
        str(target.get("ruleset_id") or updated.get("ruleset_id") or "")
    ).id
    _write_room(room_dir, updated)
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
    if not accepts_scenario(info):
        return error_response("Freeform room cannot bind a scenario", 400, "Scenario binding is not allowed")

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
    updated["ruleset_id"] = builtin_registry().resolve(str(target.get("ruleset_id") or "")).id
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
    _write_room(room_dir, updated)

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

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "强制切换了房间剧本"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
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

    remove_tree(room_dir)
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
    # 只有 scripted 房间需要「到达结局」；freeform 与 manual 没有剧本结局可判定，
    # 房主/房管显式归档即视为结束。
    if normalize_room_mode(info)["scenario_binding"] == SCENARIO_BINDING_REQUIRED and not _scenario_finished(info, state):
        return error_response("Room is not finished", 409, "Room must reach an ending before archiving")
    now = _timestamp()
    # 房规在归档后会被移除，写回持久角色卡必须发生在这一步之前。
    from trpg_server import room_rules

    room_rules.persist_room_items_on_archive(room_dir, info, app=current_app._get_current_object())
    info["completed_at"] = info.get("completed_at") or now
    info["archived_at"] = now
    info["archived"] = True
    # House rules are scoped to an active room; an archived room no longer needs
    # them (the immutable archive snapshot is built from a fixed field whitelist).
    info.pop("house_rules", None)
    _write_room(room_dir, info)
    archive = _build_room_archive(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "归档了房间"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间名=info.get("name"),
    )
    return success_response({"id": archive["id"], "name": archive["name"], "archived_at": archive["archived_at"]}, "Room archived successfully")

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
