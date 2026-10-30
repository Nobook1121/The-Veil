from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
import re
import secrets
import sqlite3
import string
import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media_defaults import normalize_avatar
from trpg_server.settings import USERS_DIR
from trpg_server.users.database import UserDatabase
from trpg_server.users.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 登录失败按账号做指数退避（不区分 IP）：连续失败达到阈值后，每次重试都要等待，
# 延迟从 1 秒翻倍到 60 秒封顶；成功登录立即清零。阈值内不影响正常用户。
LOGIN_FAILURE_LIMIT = 5
LOGIN_BACKOFF_BASE_SECONDS = 1.0
LOGIN_BACKOFF_MAX_SECONDS = 60.0
# 记录条数上限，避免用随机用户名刷爆内存（超限时先清理已过退避期的记录）。
LOGIN_FAILURE_TRACK_LIMIT = 5000

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, db: UserDatabase, ip_config_dir: str | Path | None = None) -> None:
        self.db = db
        self.ip_config_dir = Path(ip_config_dir) if ip_config_dir is not None else USERS_DIR / "ip_configs"
        self._login_failure_lock = threading.Lock()
        self._login_failures: dict[str, dict[str, float]] = {}

    def register(
        self,
        username: str,
        password: str,
        email: str,
        terms_accepted: bool,
        ip_address: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        if not terms_accepted:
            return False, "Terms must be accepted", None

        username = username.strip()
        email = email.strip()
        if not _is_valid_username(username):
            return False, "Invalid username", None
        if not _is_valid_email(email):
            return False, "Invalid email", None

        password_ok, password_message = validate_password_strength(password)
        if not password_ok:
            return False, password_message, None

        username_normalized = username.lower()
        email_normalized = email.lower()

        with self.db.connect() as connection:
            if _identity_exists(connection, "username_normalized", username_normalized):
                return False, "Username already exists", None
            if _identity_exists(connection, "email_normalized", email_normalized):
                return False, "Email already exists", None

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        username,
                        username_normalized,
                        email,
                        email_normalized,
                        password_hash,
                        role,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        username_normalized,
                        email,
                        email_normalized,
                        hash_password(password),
                        "USER",
                        "active",
                    ),
                )
            except sqlite3.IntegrityError:
                return _duplicate_identity_result(connection, username_normalized)

            user_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO user_profiles (
                    user_id,
                    nickname,
                    avatar,
                    presence
                )
                VALUES (?, ?, ?, ?)
                """,
                (user_id, username, "", "online"),
            )
            connection.execute(
                """
                INSERT INTO audit_logs (
                    user_id,
                    event_type,
                    ip_address
                )
                VALUES (?, ?, ?)
                """,
                (user_id, "user_registered", ip_address),
            )

            row = connection.execute(
                """
                SELECT
                    id,
                    username,
                    email,
                    role,
                    status,
                    created_at,
                    updated_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()

        return True, "User registered", dict(row)

    def login(
        self,
        identifier: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None, str | None]:
        identifier_normalized = identifier.strip().lower()
        retry_after = self._login_retry_after(identifier_normalized)
        if retry_after > 0:
            logger.warning(
                "登录失败次数过多，按账号退避 identifier=%s retry_after=%.1fs",
                identifier_normalized,
                retry_after,
            )
            return False, f"Too many failed attempts. Try again in {math.ceil(retry_after)} seconds", None, None

        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    username,
                    email,
                    password_hash,
                    role,
                    status,
                    last_login_at,
                    must_change_password,
                    created_at,
                    updated_at
                FROM users
                WHERE username_normalized = ?
                    OR email_normalized = ?
                """,
                (identifier_normalized, identifier_normalized),
            ).fetchone()

            if row is None:
                self._record_login_failure(identifier_normalized)
                return False, "Invalid username/email or password", None, None

            user_id = int(row["id"])
            if not verify_password(password, row["password_hash"]):
                self._record_login_failure(identifier_normalized)
                _write_audit_log(
                    connection,
                    user_id,
                    "user_login_failed",
                    ip_address,
                    user_agent,
                )
                return False, "Invalid password", None, None

            if row["status"] != "active":
                return False, f"User is {row['status']}", None, None

            self._clear_login_failures(identifier_normalized)
            now = _utc_now()
            now_iso = _to_iso(now)
            token = secrets.token_urlsafe(32)
            connection.execute(
                """
                UPDATE user_sessions
                SET revoked_at = ?
                WHERE user_id = ?
                    AND revoked_at IS NULL
                """,
                (now_iso, user_id),
            )
            connection.execute(
                """
                INSERT INTO user_sessions (
                    user_id,
                    session_token_hash,
                    ip_address,
                    user_agent,
                    expires_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    _hash_token(token),
                    ip_address,
                    user_agent,
                    _to_iso(now + timedelta(days=7)),
                    now_iso,
                ),
            )
            connection.execute(
                """
                UPDATE users
                SET last_login_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, user_id),
            )
            _write_audit_log(
                connection,
                user_id,
                "user_login_success",
                ip_address,
                user_agent,
            )
            user_row = connection.execute(
                """
                SELECT
                    id,
                    username,
                    email,
                    role,
                    status,
                    last_login_at,
                    must_change_password,
                    created_at,
                    updated_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()

        return True, "Login successful", dict(user_row), token

    def _login_retry_after(self, identifier: str) -> float:
        """返回还需等待的秒数；未达到阈值或已过退避期时返回 0。"""
        with self._login_failure_lock:
            record = self._login_failures.get(identifier)
            if not record:
                return 0.0
            count = int(record["count"])
            if count < LOGIN_FAILURE_LIMIT:
                return 0.0
            delay = min(
                LOGIN_BACKOFF_MAX_SECONDS,
                LOGIN_BACKOFF_BASE_SECONDS * (2 ** (count - LOGIN_FAILURE_LIMIT)),
            )
            return max(0.0, delay - (time.monotonic() - float(record["at"])))

    def _record_login_failure(self, identifier: str) -> None:
        now = time.monotonic()
        with self._login_failure_lock:
            if len(self._login_failures) >= LOGIN_FAILURE_TRACK_LIMIT:
                stale_before = now - LOGIN_BACKOFF_MAX_SECONDS
                self._login_failures = {
                    key: value
                    for key, value in self._login_failures.items()
                    if float(value["at"]) >= stale_before
                }
                if len(self._login_failures) >= LOGIN_FAILURE_TRACK_LIMIT:
                    self._login_failures.clear()
            record = self._login_failures.get(identifier)
            count = int(record["count"]) + 1 if record else 1
            self._login_failures[identifier] = {"count": float(count), "at": now}

    def _clear_login_failures(self, identifier: str) -> None:
        with self._login_failure_lock:
            self._login_failures.pop(identifier, None)

    def is_session_current(self, user_id: int, token: str | None) -> bool:
        if not token:
            return False

        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT expires_at
                FROM user_sessions
                WHERE user_id = ?
                    AND session_token_hash = ?
                    AND revoked_at IS NULL
                """,
                (user_id, _hash_token(token)),
            ).fetchone()

        if row is None:
            return False
        return _parse_iso(row["expires_at"]) > _utc_now()

    def revoke_session(self, user_id: int, token: str | None) -> bool:
        if not token:
            return False

        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions
                SET revoked_at = ?
                WHERE user_id = ?
                    AND session_token_hash = ?
                    AND revoked_at IS NULL
                """,
                (_to_iso(_utc_now()), user_id, _hash_token(token)),
            )

        return cursor.rowcount > 0

    def revoke_all_sessions(self, user_id: int) -> int:
        """撤销该用户全部未撤销的会话（强制下线，但不改动账号角色/状态）。"""

        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions
                SET revoked_at = ?
                WHERE user_id = ?
                    AND revoked_at IS NULL
                """,
                (_to_iso(_utc_now()), user_id),
            )
        return cursor.rowcount

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    users.id,
                    users.username,
                    users.email,
                    users.role,
                    users.status,
                    users.must_change_password,
                    user_profiles.nickname,
                    user_profiles.avatar,
                    user_profiles.presence
                FROM users
                LEFT JOIN user_profiles
                    ON user_profiles.user_id = users.id
                WHERE users.id = ?
                """,
                (user_id,),
            ).fetchone()

        if row is None:
            return None

        user = dict(row)
        user["avatar"] = normalize_avatar(user.get("avatar"))
        user["nickname"] = user.get("nickname") or user.get("username", "")
        user["presence"] = user.get("presence") or "online"
        user["two_factor_enabled"] = False
        return user

    def update_profile(
        self,
        user_id: int,
        username: str,
        email: str,
        nickname: str = "",
        avatar: str | None = None,
        password: str | None = None,
    ) -> tuple[bool, str]:
        username = username.strip()
        email = email.strip()
        if not _is_valid_username(username):
            return False, "Invalid username"
        if not _is_valid_email(email):
            return False, "Invalid email"

        if password:
            return False, "Use /api/auth/password/change to change password"

        username_normalized = username.lower()
        email_normalized = email.lower()
        now_iso = _to_iso(_utc_now())

        with self.db.connect() as connection:
            if _identity_exists_for_other_user(
                connection,
                "username_normalized",
                username_normalized,
                user_id,
            ):
                return False, "Username already exists"
            if _identity_exists_for_other_user(
                connection,
                "email_normalized",
                email_normalized,
                user_id,
            ):
                return False, "Email already exists"

            existing_user = connection.execute(
                "SELECT id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if existing_user is None:
                return False, "User not found"

            try:
                connection.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        username_normalized = ?,
                        email = ?,
                        email_normalized = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        username_normalized,
                        email,
                        email_normalized,
                        now_iso,
                        user_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                return _profile_update_integrity_error_result(exc)

            current_profile = connection.execute(
                "SELECT avatar FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            avatar_value = avatar
            if avatar_value is None and current_profile is not None:
                avatar_value = current_profile["avatar"]
            avatar_value = normalize_avatar(avatar_value)

            connection.execute(
                """
                INSERT INTO user_profiles (
                    user_id,
                    nickname,
                    avatar,
                    presence,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    avatar = excluded.avatar,
                    updated_at = excluded.updated_at
                """,
                (user_id, nickname, avatar_value, "online", now_iso),
            )

        return True, "Profile updated"

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> tuple[bool, str]:
        password_ok, password_message = validate_password_strength(new_password)
        if not password_ok:
            return False, password_message

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return False, "User not found"
            if not verify_password(current_password, row["password_hash"]):
                return False, "Current password is incorrect"

            connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (hash_password(new_password), _to_iso(_utc_now()), user_id),
            )

        return True, "Password changed"

    def force_password_reset(self, user_id: int) -> tuple[bool, str, str | None]:
        """强制用户下次登录修改密码。

        生成一个满足密码强度要求的随机临时密码并覆盖原密码，同时把
        ``must_change_password`` 置 1，并撤销该用户所有活跃会话，使其必须用
        临时密码重新登录后再修改密码。返回的临时密码由调用方（控制台命令）
        打印给管理员。
        """

        temp_password = _generate_temp_password()
        now_iso = _to_iso(_utc_now())
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (hash_password(temp_password), now_iso, user_id),
            )
            if cursor.rowcount == 0:
                return False, "User not found", None
            connection.execute(
                """
                UPDATE user_sessions
                SET revoked_at = ?
                WHERE user_id = ?
                    AND revoked_at IS NULL
                """,
                (now_iso, user_id),
            )

        return True, "Password reset; user must change it on next login", temp_password

    def update_presence(self, user_id: int, presence: str) -> tuple[bool, str]:
        if presence not in {"online", "dnd", "invisible"}:
            return False, "Invalid presence"

        now_iso = _to_iso(_utc_now())
        with self.db.connect() as connection:
            user = connection.execute(
                "SELECT username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return False, "User not found"

            connection.execute(
                """
                INSERT INTO user_profiles (
                    user_id,
                    nickname,
                    avatar,
                    presence,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    presence = excluded.presence,
                    updated_at = excluded.updated_at
                """,
                (user_id, user["username"], "", presence, now_iso),
            )

        return True, "Presence updated"

    def touch_session(self, user_id: int, token: str | None) -> bool:
        if not token:
            return False

        now_iso = _to_iso(_utc_now())
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_sessions
                SET last_seen_at = ?
                WHERE user_id = ?
                    AND session_token_hash = ?
                    AND revoked_at IS NULL
                    AND expires_at > ?
                """,
                (now_iso, user_id, _hash_token(token), now_iso),
            )

        return cursor.rowcount > 0

    def get_all_users(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    users.id AS id,
                    users.username AS username,
                    users.email AS email,
                    users.role AS role,
                    users.status AS status,
                    COALESCE(user_profiles.presence, 'online') AS presence,
                    users.created_at AS created_at,
                    users.last_login_at AS last_login_at
                FROM users
                LEFT JOIN user_profiles
                    ON user_profiles.user_id = users.id
                ORDER BY users.id
                """
            ).fetchall()

        return [_admin_user_payload(row) for row in rows]

    def update_user_role(self, user_id: int, role: str) -> tuple[bool, str]:
        if role not in {"OWNER", "ADMIN", "USER"}:
            return False, "Invalid role"
        return self._update_user_field(user_id, "role", role, "Role updated successfully")

    def update_user_status(self, user_id: int, status: str) -> tuple[bool, str]:
        if status not in {"active", "inactive", "banned"}:
            return False, "Invalid status"
        return self._update_user_field(user_id, "status", status, "Status updated successfully")

    def delete_user(self, user_id: int) -> bool:
        """删除账号。关联表由外键 ``ON DELETE CASCADE`` 清理，审计日志改为保留但解除关联。"""

        with self.db.connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0

    def check_permission(self, user_id: int, required_role: str) -> bool:
        hierarchy = {"USER": 1, "ADMIN": 2, "OWNER": 3}
        if required_role not in hierarchy:
            return False
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT role, status FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None or row["status"] != "active":
            return False
        return hierarchy.get(row["role"], 0) >= hierarchy.get(required_role, 0)

    def get_ip_config(self, ip_address: str) -> dict[str, Any] | None:
        return read_json(_ip_config_path(self.ip_config_dir, ip_address), default=None)

    def create_ip_config(self, ip_address: str) -> bool:
        config_path = _ip_config_path(self.ip_config_dir, ip_address)
        if config_path.exists():
            return True
        write_json_atomic(
            config_path,
            {
                "ip_address": ip_address,
                "settings": {},
                "preferences": {},
            },
        )
        return True

    def update_ip_config(self, ip_address: str, config_data: dict[str, Any]) -> bool:
        config = self.get_ip_config(ip_address)
        if config is None:
            self.create_ip_config(ip_address)
            config = self.get_ip_config(ip_address)
        config.update(config_data)
        config["ip_address"] = ip_address
        write_json_atomic(_ip_config_path(self.ip_config_dir, ip_address), config)
        return True

    def get_all_ip_configs(self) -> list[dict[str, Any]]:
        if not self.ip_config_dir.exists():
            return []
        configs = []
        for path in sorted(self.ip_config_dir.glob("*.json")):
            config = read_json(path, default=None)
            if isinstance(config, dict):
                configs.append(config)
        return configs

    def _update_user_field(
        self,
        user_id: int,
        field_name: str,
        value: str,
        success_message: str,
    ) -> tuple[bool, str]:
        now_iso = _to_iso(_utc_now())
        with self.db.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE users
                SET {field_name} = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (value, now_iso, user_id),
            )
        if cursor.rowcount == 0:
            return False, "User does not exist"
        return True, success_message


def _is_valid_username(username: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(username))


def _is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(email))


def _identity_exists(
    connection: sqlite3.Connection,
    column_name: str,
    normalized_value: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM users WHERE {column_name} = ?",
        (normalized_value,),
    ).fetchone()
    return row is not None


def _identity_exists_for_other_user(
    connection: sqlite3.Connection,
    column_name: str,
    normalized_value: str,
    user_id: int,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM users WHERE {column_name} = ? AND id <> ?",
        (normalized_value, user_id),
    ).fetchone()
    return row is not None


def _duplicate_identity_result(
    connection: sqlite3.Connection,
    username_normalized: str,
) -> tuple[bool, str, None]:
    if _identity_exists(connection, "username_normalized", username_normalized):
        return False, "Username already exists", None
    return False, "Email already exists", None


def _profile_update_integrity_error_result(exc: sqlite3.IntegrityError) -> tuple[bool, str]:
    message = str(exc).lower()
    if "username" in message:
        return False, "Username already exists"
    if "email" in message:
        return False, "Email already exists"
    raise exc


def _admin_user_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "presence": row["presence"],
        "created_at": row["created_at"],
        "last_login": row["last_login_at"],
    }


def _ip_config_path(ip_config_dir: Path, ip_address: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(ip_address)).replace(".", "_")
    return ip_config_dir / f"{safe_name or 'unknown'}.json"


def _write_audit_log(
    connection: sqlite3.Connection,
    user_id: int,
    event_type: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs (
            user_id,
            event_type,
            ip_address,
            user_agent
        )
        VALUES (?, ?, ?, ?)
        """,
        (user_id, event_type, ip_address, user_agent),
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_temp_password(length: int = 12) -> str:
    """生成满足密码强度要求（长度、含字母、含数字）的随机临时密码。"""

    length = max(8, length)
    letters = string.ascii_letters
    digits = string.digits
    alphabet = letters + digits
    chars = [secrets.choice(letters), secrets.choice(digits)]
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
