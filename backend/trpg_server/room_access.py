"""房间访问判定的共享实现。

HTTP 路由与 Socket.IO 都需要回答「房间是否存在、当前用户能否进入」。
把规则放在独立模块里，既能让两边保持一致，也避免 socket 层反向依赖 routes。
"""

from __future__ import annotations

from flask import current_app, session

from trpg_server.json_store import read_json
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.security import safe_join
from trpg_server.settings import CONFIG_DIR, ROOMS_DIR

ROOM_MEMBER_ACTIVE = "active"
ROOM_MEMBER_REMOVED = "removed"
MANAGE_MEMBERS_NODE = "rooms.manage_members"


def rooms_dir():
    return current_app.config.get("ROOMS_DIR", ROOMS_DIR)


def is_elevated_role(role):
    config_dir = current_app.config.get("CONFIG_DIR", CONFIG_DIR)
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(role or "USER", MANAGE_MEMBERS_NODE, config_path)


def is_elevated():
    return is_elevated_role(session.get("role", "USER"))


def find_room(room_id):
    """返回 ``(room_dir, info)``；房间不存在或路径非法时返回 ``(None, None)``。"""
    if not room_id:
        return None, None
    try:
        room_dir = safe_join(rooms_dir(), room_id)
    except (TypeError, ValueError):
        return None, None
    info_file = room_dir / "info.json"
    if not info_file.exists():
        return None, None
    return room_dir, read_json(info_file, default={})


def is_active_member(member):
    return member.get("is_active", True) is not False and member.get("status", ROOM_MEMBER_ACTIVE) != ROOM_MEMBER_REMOVED


def find_active_member(info, user_id):
    for member in (info or {}).get("members", []) or []:
        if not isinstance(member, dict) or not is_active_member(member):
            continue
        if str(member.get("user_id")) == str(user_id):
            return member
    return None


def can_access(info, user_id, role=None):
    """站点管理员或房间有效成员可访问；房间不存在时一律拒绝。"""
    if not info:
        return False
    if is_elevated_role(role or session.get("role", "USER")):
        return True
    return find_active_member(info, user_id) is not None
