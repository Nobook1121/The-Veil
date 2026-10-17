from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trpg_server.media_defaults import normalize_avatar
from trpg_server.users.database import UserDatabase


VALID_ROLES = {"OWNER", "ADMIN", "USER"}
VALID_STATUSES = {"active", "inactive", "banned"}
MAPPED_USER_KEYS = {
    "id",
    "username",
    "password",
    "password_hash",
    "email",
    "role",
    "status",
    "avatar",
    "nickname",
    "bio",
    "created_at",
    "updated_at",
    "last_login",
    "last_login_at",
}


def migrate_json_users(users_json_path: str | Path, db: UserDatabase) -> None:
    path = Path(users_json_path)
    if not path.exists():
        return

    with db.connect() as conn:
        existing_users = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        if existing_users["count"] > 0:
            return

        data = json.loads(path.read_text(encoding="utf-8"))
        legacy_users = data.get("users", [])
        for index, legacy_user in enumerate(legacy_users):
            _validate_legacy_user(legacy_user, index)

        for legacy_user in legacy_users:
            user_id = _insert_user(conn, legacy_user)
            _insert_profile(conn, user_id, legacy_user)


def _insert_user(conn, legacy_user: dict[str, Any]) -> int:
    columns = [
        "username",
        "username_normalized",
        "email",
        "email_normalized",
        "password_hash",
        "role",
        "status",
    ]
    values = [
        legacy_user.get("username", ""),
        str(legacy_user.get("username", "")).lower(),
        legacy_user.get("email", ""),
        str(legacy_user.get("email", "")).lower(),
        _password_hash(legacy_user),
        _valid_or_default(legacy_user.get("role"), VALID_ROLES, "USER"),
        _valid_or_default(legacy_user.get("status"), VALID_STATUSES, "active"),
    ]

    if legacy_user.get("id") is not None:
        columns.insert(0, "id")
        values.insert(0, legacy_user["id"])
    if legacy_user.get("last_login_at") or legacy_user.get("last_login"):
        columns.append("last_login_at")
        values.append(legacy_user.get("last_login_at") or legacy_user.get("last_login"))
    if legacy_user.get("created_at"):
        columns.append("created_at")
        values.append(legacy_user["created_at"])
    if legacy_user.get("updated_at"):
        columns.append("updated_at")
        values.append(legacy_user["updated_at"])

    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(legacy_user.get("id") or cursor.lastrowid)


def _insert_profile(conn, user_id: int, legacy_user: dict[str, Any]) -> None:
    extra_profile = {
        key: value
        for key, value in legacy_user.items()
        if key not in MAPPED_USER_KEYS
    }
    extra_json = (
        json.dumps(extra_profile, ensure_ascii=True, sort_keys=True)
        if extra_profile
        else None
    )

    conn.execute(
        """
        INSERT INTO user_profiles (
            user_id,
            nickname,
            avatar,
            bio,
            profile_extra_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            legacy_user.get("nickname"),
            normalize_avatar(legacy_user.get("avatar")),
            legacy_user.get("bio"),
            extra_json,
        ),
    )


def _valid_or_default(value: Any, valid_values: set[str], default: str) -> str:
    if value in valid_values:
        return value
    return default


def _validate_legacy_user(legacy_user: dict[str, Any], index: int | None = None) -> None:
    if not isinstance(legacy_user, dict):
        raise ValueError(f"legacy user at index {index} must be an object")

    for field_name in ("username", "email"):
        if not _has_text(legacy_user.get(field_name)):
            raise ValueError(
                f"{_legacy_user_label(legacy_user)} missing required field "
                f"{field_name}"
            )

    has_password = _has_text(legacy_user.get("password_hash")) or _has_text(
        legacy_user.get("password")
    )
    if not has_password:
        raise ValueError(
            f"{_legacy_user_label(legacy_user)} missing required field "
            "password_hash or password"
        )


def _password_hash(legacy_user: dict[str, Any]) -> str:
    if _has_text(legacy_user.get("password_hash")):
        return str(legacy_user["password_hash"])
    return str(legacy_user["password"])


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _legacy_user_label(legacy_user: dict[str, Any]) -> str:
    if legacy_user.get("id") is not None:
        return f"legacy user {legacy_user['id']}"
    if _has_text(legacy_user.get("username")):
        return f"legacy user {legacy_user['username']}"
    return "legacy user <unknown>"
