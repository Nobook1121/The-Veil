import secrets
import time
from functools import wraps
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from flask import current_app, request, session

from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response

ACTIVE_SESSION_REGISTRY_KEY = "ACTIVE_USER_SESSIONS"
# 记录每个用户当前加入的房间集合（由 socket_events 维护），用于判定「在房间内」。
USER_ROOM_MEMBERSHIP_KEY = "USER_ROOM_MEMBERSHIP"
SESSION_TOKEN_KEY = "session_token"
CSRF_SESSION_KEY = "csrf_token"
IMPERSONATION_SESSION_PREFIX = "impersonation:"
# 模拟登录与普通会话同寿命（7 天）；令牌内嵌过期时间，避免被无限期复用。
IMPERSONATION_TTL_SECONDS = 7 * 24 * 60 * 60
CSRF_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    # 首次部署引导发生在登录之前，此时会话中还没有 CSRF token；
    # 这些接口以“系统中不存在 OWNER”作为安全门闸，因此豁免 CSRF 与会话校验。
    # （embedding/status 是只读接口，仅回传本机模型与下载进度状态。）
    "/api/setup/status",
    "/api/setup/dismiss",
    "/api/setup/site",
    "/api/setup/ai",
    "/api/setup/ai/test",
    "/api/setup/embedding/status",
    "/api/setup/embedding/download",
    "/api/setup/embedding/upload",
    "/api/setup/embedding/cloud",
    "/api/setup/owner",
}
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_join(base, *parts):
    base_path = Path(base).resolve()
    target_path = base_path.joinpath(*parts).resolve()

    try:
        target_path.relative_to(base_path)
    except ValueError as exc:
        raise ValueError("Unsafe path") from exc

    return target_path


def is_allowed_upload(filename, allowed_extensions):
    suffix = Path(filename).suffix
    if not suffix:
        return False

    suffix = suffix.lower().lstrip(".")
    return suffix in {extension.lower().lstrip(".") for extension in allowed_extensions}


def normalize_filename(filename, fallback="file"):
    raw_name = str(filename or "").replace("\\", "/")
    basename = PurePosixPath(raw_name).name.strip()
    if not basename:
        basename = fallback

    normalized = "".join(
        character if character.isalnum() or character in {" ", ".", "-", "_"} else "_"
        for character in basename
    ).strip(" .")
    if not normalized:
        normalized = fallback

    if "." in normalized:
        stem, suffix = normalized.rsplit(".", 1)
        if stem.upper() in WINDOWS_RESERVED_FILENAMES:
            stem = f"_{stem}"
        normalized = f"{stem}.{suffix}"
    elif normalized.upper() in WINDOWS_RESERVED_FILENAMES:
        normalized = f"_{normalized}"

    return normalized[:160]


def build_public_asset_url(prefix, filename):
    normalized_prefix = "/" + str(prefix or "").strip("/")
    return f"{normalized_prefix}/{quote(normalize_filename(filename), safe='')}"


def get_user_manager():
    from trpg_server.users.manager import user_manager

    return current_app.config.get("USER_MANAGER", user_manager)


def issue_session_token(user_id):
    token = uuid4().hex
    active_sessions = current_app.config.setdefault(ACTIVE_SESSION_REGISTRY_KEY, {})
    active_sessions[str(user_id)] = token
    return token


def clear_session_token(user_id, token):
    active_sessions = current_app.config.setdefault(ACTIVE_SESSION_REGISTRY_KEY, {})
    user_key = str(user_id)
    if active_sessions.get(user_key) == token:
        active_sessions.pop(user_key, None)


def is_socket_user_online(user_id):
    active_sockets = current_app.config.setdefault("ACTIVE_SOCKET_SESSIONS", {})
    return str(user_id) in active_sockets


def user_room_ids(user_id, membership=None):
    """返回用户当前加入的房间集合。

    membership 按 socket（浏览器标签页）维护：``{user_id: {sid: {room_id}}}``。
    同一用户可能同时开着多个标签页，只要任意一个仍在房间内就算「在房间」。
    兼容旧结构 ``{user_id: set(room_id)}``。
    """
    if user_id is None:
        return set()
    if membership is None:
        membership = current_app.config.get(USER_ROOM_MEMBERSHIP_KEY) or {}
    entry = membership.get(str(user_id))
    if isinstance(entry, dict):
        return {str(room) for rooms in entry.values() if isinstance(rooms, (set, list, tuple)) for room in rooms if room}
    if isinstance(entry, (set, list, tuple)):
        return {str(room) for room in entry if room}
    return set()


def is_user_in_room(user_id, room_id):
    """用户当前是否「停留在该房间内」。

    仅凭全局在线（``is_socket_user_online``）不足以判断用户是否在某个房间里：
    用户可能在线却停留在别的页面/房间。这里以 Socket.IO 的 join_room/leave_room
    维护的 membership 为准，用于房间内在线列表与自动存档判定。
    """
    if room_id in (None, "") or user_id is None:
        return False
    return str(room_id) in user_room_ids(user_id)


def build_impersonation_token(initiator_id, target_id):
    """模拟登录令牌：内嵌发起者、目标用户与过期时间，供会话判定做绑定校验。"""
    expires_at = int(time.time()) + IMPERSONATION_TTL_SECONDS
    return f"{IMPERSONATION_SESSION_PREFIX}{int(initiator_id)}:{int(target_id)}:{expires_at}:{uuid4().hex}"


def impersonation_token_is_valid(token, *, initiator_id=None, target_id=None):
    if not isinstance(token, str) or not token.startswith(IMPERSONATION_SESSION_PREFIX):
        return False
    parts = token[len(IMPERSONATION_SESSION_PREFIX):].split(":")
    if len(parts) != 4:
        return False
    raw_initiator, raw_target, raw_expires, nonce = parts
    if not nonce:
        return False
    try:
        expires_at = int(raw_expires)
    except (TypeError, ValueError):
        return False
    if expires_at <= int(time.time()):
        return False
    if initiator_id is not None and not secrets.compare_digest(raw_initiator, str(initiator_id)):
        return False
    if target_id is not None and not secrets.compare_digest(raw_target, str(target_id)):
        return False
    return True


def _impersonation_session_is_current(token):
    return bool(session.get("impersonation_mode")) and impersonation_token_is_valid(
        token,
        initiator_id=session.get("impersonation_original_user_id"),
        target_id=session.get("user_id"),
    )


def _is_session_current(manager, user_id, token):
    # 模拟登录使用独立 token 前缀，此时只要求当前会话处于 impersonation 模式且
    # 令牌与发起者/目标绑定，不走真实用户的单会话约束，避免管理员模拟时踢掉被模拟用户的登录态。
    if token and str(token).startswith(IMPERSONATION_SESSION_PREFIX):
        return _impersonation_session_is_current(token)
    if hasattr(manager, "is_session_current"):
        return manager.is_session_current(user_id, token)

    active_sessions = current_app.config.setdefault(ACTIVE_SESSION_REGISTRY_KEY, {})
    active_token = active_sessions.get(str(user_id))
    return not (active_token and token != active_token)


def session_is_current():
    """统一的会话有效性判定（HTTP 守卫、网站 Agent、Socket.IO 共用）。"""
    if "user_id" not in session:
        return False
    return _is_session_current(get_user_manager(), session["user_id"], session.get(SESSION_TOKEN_KEY))


def register_session_guard(app):
    @app.before_request
    def _enforce_single_session():
        if not request.path.startswith("/api/"):
            return None
        if request.path in CSRF_EXEMPT_PATHS:
            return None
        if "user_id" not in session:
            return None

        manager = get_user_manager()
        if not _is_session_current(
            manager,
            session["user_id"],
            session.get(SESSION_TOKEN_KEY),
        ):
            session.clear()
            return error_response("Session expired", 401, "Session expired")
        _refresh_session_user_fields(manager)

        # 写操作必须携带与会话一致的 CSRF token，防止跨站请求伪造。
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_token = session.get(CSRF_SESSION_KEY)
            request_token = request.headers.get("X-CSRF-Token", "")
            if not csrf_token or not request_token or not secrets.compare_digest(str(csrf_token), str(request_token)):
                return error_response("CSRF validation failed", 403, "Invalid CSRF token")

        return None


def _validate_current_session():
    if "user_id" not in session:
        return None, error_response("Please login first", 401, "Not logged in")

    manager = get_user_manager()
    if not _is_session_current(
        manager,
        session["user_id"],
        session.get(SESSION_TOKEN_KEY),
    ):
        session.clear()
        return None, error_response("Session expired", 401, "Session expired")
    _refresh_session_user_fields(manager)

    return manager, None


def _refresh_session_user_fields(manager) -> None:
    if "user_id" not in session or not hasattr(manager, "get_user_by_id"):
        return
    user = manager.get_user_by_id(session["user_id"])
    if not user:
        return
    session["username"] = user.get("username", session.get("username"))
    session["role"] = user.get("role", session.get("role", "USER"))


def require_permission_node(node_id):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _manager, error = _validate_current_session()
            if error:
                return error

            config_dir = current_app.config.get("CONFIG_DIR")
            config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
            if not is_role_allowed(session.get("role", "USER"), node_id, config_path):
                return error_response("Permission denied", 403, "Permission denied")

            return func(*args, **kwargs)

        return wrapper

    return decorator
