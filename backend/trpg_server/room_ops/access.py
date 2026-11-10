"""成员与权限：登录校验、成员查找/角色、在线判定、房主移交、世界意志权限。"""
from datetime import datetime
from datetime import timezone
from flask import current_app
from flask import session
from trpg_server.logging_config import log_access_denied
from trpg_server.logging_config import user_action_text
from trpg_server.media_defaults import normalize_avatar
from trpg_server.responses import error_response
from trpg_server.room_access import can_access
from trpg_server.room_access import is_elevated
from trpg_server.room_mode import normalize_room_mode
from trpg_server.room_ops.cards import _initial_character_state, _sanitize_character_card
from trpg_server.room_ops.constants import (
    ROOM_MEMBER_ACTIVE,
    ROOM_MEMBER_REMOVED,
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    ROOM_ROLE_OWNER,
)
from trpg_server.room_ops.house_rules import _room_house_rules
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import _timestamp
from trpg_server.security import get_user_manager
from trpg_server.security import is_socket_user_online
from trpg_server.security import is_user_in_room

def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None

def _is_elevated():
    return is_elevated()

def _denied(room_ref=None):
    log_access_denied(
        logger,
        user_action_text(session.get("username"), "访问房间被拒绝"),
        用户ID=session.get("user_id"),
        房间ID=room_ref,
    )
    return error_response("Permission denied", 403, "Permission denied")

def _emit_room_members_changed(room_id):
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_members_changed", {"room_id": str(room_id)}, room=str(room_id))

def _joined_at_timestamp(member):
    """解析成员 ``joined_at``；缺失或格式无法识别时返回 ``None``。"""
    raw = str(member.get("joined_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

def _earliest_active_member(info, exclude_user_id):
    """按加入时间挑选最早的活跃成员，用于房主自动移交。

    以成员记录的 ``joined_at`` 为准；重新加入房间只会追加 ``rejoined_at``，
    不会覆盖 ``joined_at``。字段缺失/无法解析的成员排在可解析成员之后，
    同一时间按成员列表顺序、再按 user_id 兜底，确保结果稳定可复现。
    """
    candidates = [
        (index, member)
        for index, member in enumerate(info.get("members", []))
        if _is_active_member(member) and str(member.get("user_id")) != str(exclude_user_id)
    ]
    if not candidates:
        return None

    def sort_key(item):
        index, member = item
        stamp = _joined_at_timestamp(member)
        if stamp is None:
            return (1, 0.0, index, str(member.get("user_id") or ""))
        return (0, stamp, index, str(member.get("user_id") or ""))

    return min(candidates, key=sort_key)[1]

def _assign_room_owner(info, new_owner, previous_user_id=None):
    """把房主身份落到 ``new_owner``，并把原房主降为普通成员。"""
    new_user_id = str(new_owner.get("user_id"))
    for member in info.get("members", []):
        if str(member.get("user_id")) == new_user_id:
            member["room_role"] = ROOM_ROLE_OWNER
            member["status"] = ROOM_MEMBER_ACTIVE
            member["is_active"] = True
        elif previous_user_id is not None and str(member.get("user_id")) == str(previous_user_id):
            if member.get("room_role") == ROOM_ROLE_OWNER:
                member["room_role"] = ROOM_ROLE_MEMBER
    info["creator_id"] = new_user_id
    info["creator_name"] = str(new_owner.get("username") or "")

def world_override_allowed(info, user_id=None, role=None):
    """世界意志直执行权限：站点管理员恒可用，房主/房管按房规开关，白名单成员额外放行。"""
    mode = normalize_room_mode(info)
    # 世界意志只依赖 AI KP：无 AI 的房间不可用，无剧本房间（freeform）同样可用。
    if not mode["ai_kp_enabled"]:
        return False
    role_value = str(role or session.get("role") or "USER").upper()
    if role_value in {"ADMIN", "OWNER"} or _is_elevated():
        return True
    rules = _room_house_rules(info)
    if not rules.get("world_override_enabled", True):
        return False
    uid = str(user_id if user_id is not None else session.get("user_id") or "")
    if not uid:
        return False
    if uid in set(rules.get("world_override_user_ids") or []):
        return True
    member = _find_member(info, user_id=uid, active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}

def _can_access(info):
    return can_access(info, session.get("user_id"), session.get("role", "USER"))

def _can_manage(info):
    if _is_elevated() or str(info.get("creator_id")) == str(session.get("user_id")):
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}

def _can_bind_character_card(character_card, target_member):
    if _is_elevated():
        return True
    player_id = str((character_card or {}).get("playerId") or "")
    return player_id in {
        str(target_member.get("user_id")),
        str(target_member.get("username")),
    }


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
            # 房主自助退出后 creator_id 可能仍指向已移除成员；不要把他重新激活，
            # 否则房间会凭空多出一位「在场房主」。重新加入时由 join 流程恢复成员状态。
            if _is_active_member(member):
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
