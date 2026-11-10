"""房间持久化与查找：目录解析、info/messages 读写、按 id/code/name/token 定位房间。"""
from flask import current_app
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.config import load_settings
from trpg_server.json_store import read_json
from trpg_server.json_store import write_json_atomic
from trpg_server.role_config import load_roles
from trpg_server.room_access import find_room
from trpg_server.security import safe_join
from trpg_server.settings import CONFIG_DIR
from trpg_server.settings import ROOMS_DIR
from trpg_server.settings import ROOM_ARCHIVES_DIR
from trpg_server.settings import SCENARIOS_DIR
from uuid import uuid4
import secrets
import time

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
    # schema 已收敛到 [1, 50]，默认 3。
    return int(load_settings(_get_config_dir()).data["autosave"]["max_nodes"])

def _autosave_settings():
    """读取生效配置的 [autosave] 段，供服务端后台调度使用（schema 保证键齐全）。"""
    section = load_settings(_get_config_dir()).data["autosave"]
    return {"enabled": bool(section["enabled"]), "interval": int(section["interval"])}

def _room_cleanup_days():
    """读取生效配置的 [rooms] cleanup_inactive_days；0 表示不回收。"""
    section = load_settings(_get_config_dir()).data["rooms"]
    return int(section["cleanup_inactive_days"])

def _timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _iter_room_dirs():
    rooms_dir = _get_rooms_dir()
    if not rooms_dir.exists():
        return []
    return [path for path in rooms_dir.iterdir() if path.is_dir()]

def _read_room(room_dir):
    return read_json(room_dir / "info.json", default={})

def _write_room(room_dir, info):
    # 统一走 room_rules.save_room_info：房间锁 + 锁内合并 game_clock，
    # 避免加入/重连/房规保存这类旧快照把时间推进覆盖回去。
    from trpg_server import room_rules

    room_rules.save_room_info(room_dir, info)

def _global_hints_enabled():
    """Global admin switch. When off, no room may enable action suggestions."""
    return bool(load_ai_runtime_config(_get_config_dir()).show_ai_hints)

def _room_dir(room_id):
    return safe_join(_get_rooms_dir(), room_id)

def _find_room(room_id):
    return find_room(room_id)

def _find_room_by_code(room_code):
    normalized = str(room_code or "").strip().upper()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if info.get("room_code") == normalized:
            return room_dir, info
    return None, None

def _new_invite_token():
    """生成可轮换的房间邀请令牌（与固定的房间码分离，便于「刷新」后旧链接失效）。"""
    return secrets.token_urlsafe(16)

def _find_room_by_invite_token(invite_token):
    normalized = str(invite_token or "").strip()
    if not normalized:
        return None, None
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        token = str(info.get("invite_token") or "")
        if token and secrets.compare_digest(token, normalized):
            return room_dir, info
    return None, None

def _ensure_invite_token(room_dir, info):
    """读取房间的邀请令牌；老房间缺失时惰性补发并落盘。"""
    token = str(info.get("invite_token") or "")
    if token:
        return token
    token = _new_invite_token()
    info["invite_token"] = token
    _write_room(room_dir, info)
    return token

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

def _get_scenarios_dir():
    return current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)

def _messages_file(room_dir):
    return room_dir / "messages.json"

def _read_messages(room_dir):
    return read_json(_messages_file(room_dir), default=[])

def _write_messages(room_dir, messages):
    write_json_atomic(_messages_file(room_dir), messages)

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

def _created_room_count(user_id):
    return sum(1 for room_dir in _iter_room_dirs() if _read_room(room_dir).get("creator_id") == user_id)
