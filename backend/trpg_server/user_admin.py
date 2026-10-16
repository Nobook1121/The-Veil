"""用户运维（控制台专用）：列出、封禁/解封、强制下线、建号、删除、转移 OWNER。

全部操作复用 :class:`trpg_server.users.service.UserService`（经 ``UserManager`` 代理），
不依赖 Flask 请求上下文。破坏性策略由调用方（控制台）负责二次确认。
"""
from __future__ import annotations

import logging
import secrets
import string
from pathlib import Path
from typing import Any

from trpg_server.scenario_admin import list_scenarios
from trpg_server.users.service import USERNAME_PATTERN

logger = logging.getLogger(__name__)


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(char.isalpha() for char in candidate) and any(char.isdigit() for char in candidate):
            return candidate


def list_users(user_manager: Any, search: str = "") -> list[dict[str, Any]]:
    """列出用户：ID / 用户名 / 邮箱 / 角色 / 状态 / 最近登录。"""

    users = list(user_manager.get_all_users() or [])
    needle = str(search or "").strip().casefold()
    if needle:
        users = [
            user
            for user in users
            if needle in f"{user.get('id')} {user.get('username')} {user.get('email')}".casefold()
        ]
    return [
        {
            "id": user.get("id"),
            "username": user.get("username"),
            "email": user.get("email"),
            "role": user.get("role"),
            "status": user.get("status"),
            "last_login_at": user.get("last_login_at"),
        }
        for user in users
    ]


def set_user_status(user_manager: Any, user_id: int, status: str) -> tuple[bool, str]:
    """封禁 / 解封 / 停用账号（``active`` / ``banned`` / ``inactive``）。"""

    if status not in {"active", "banned", "inactive"}:
        return False, f"Invalid status: {status}（可选 active / banned / inactive）"
    return user_manager.update_user_status(int(user_id), status)


def revoke_user_sessions(user_manager: Any, user_id: int) -> int:
    """撤销该用户全部会话（强制下线），返回被撤销的会话数。"""

    return int(user_manager.revoke_all_sessions(int(user_id)))


def create_user(user_manager: Any, username: str, role: str = "USER", email: str | None = None) -> dict[str, Any]:
    """本地创建账号，返回一次性临时密码。

    ``register`` 只能建 USER，因此建号后如需更高权限再单独提权。
    """

    username = str(username or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Invalid username（3-32 位字母/数字/_.-）")
    role = str(role or "USER").upper()
    if role not in {"USER", "ADMIN", "OWNER"}:
        raise ValueError(f"Invalid role: {role}")
    resolved_email = str(email or f"{username.lower()}@local.invalid").strip()

    password = _generate_password()
    success, message, user = user_manager.register(username, password, resolved_email, terms_accepted=True)
    if not success or not user:
        raise ValueError(message)
    user_id = int(user["id"])
    if role != "USER":
        updated, update_message = user_manager.update_user_role(user_id, role)
        if not updated:
            # 提权失败则回滚账号，避免留下一个半成品。
            user_manager.delete_user(user_id)
            raise ValueError(update_message)
    return {"id": user_id, "username": username, "email": resolved_email, "role": role, "temp_password": password}


def delete_user(
    user_manager: Any,
    user_id: int,
    *,
    force: bool = False,
    scenarios_dir: Any = None,
) -> tuple[bool, str]:
    """删除账号。

    默认拒绝删除两种情况（需 ``force`` 才继续）：系统 OWNER、仍持有剧本的用户。
    """

    user = user_manager.get_user_by_id(int(user_id))
    if not user:
        return False, f"User not found: {user_id}"
    role = str(user.get("role") or "USER").upper()
    name = user.get("username") or user_id

    if role == "OWNER" and not force:
        return False, f"{name} 是系统 OWNER，拒绝删除；确定要删请加 --force"
    if not force and scenarios_dir is not None:
        owned = [item for item in list_scenarios(Path(scenarios_dir)) if int(item.get("owner_id") or -1) == int(user_id)]
        if owned:
            titles = ", ".join(f"#{item['id']}「{item['title']}」" for item in owned[:5])
            return False, f"{name} 仍持有 {len(owned)} 个剧本（{titles}），拒绝删除；确定要删请加 --force"

    if not user_manager.delete_user(int(user_id)):
        return False, f"Failed to delete user: {name}"
    return True, f"已删除用户 {name}({user_id})"


def transfer_owner(user_manager: Any, user_id: int) -> dict[str, Any]:
    """把系统 OWNER 身份转移给目标用户：原 OWNER 降为 ADMIN，目标升为 OWNER。"""

    target = user_manager.get_user_by_id(int(user_id))
    if not target:
        raise LookupError(f"User not found: {user_id}")

    demoted: list[str] = []
    for user in user_manager.get_all_users() or []:
        if str(user.get("role") or "").upper() == "OWNER" and int(user["id"]) != int(user_id):
            user_manager.update_user_role(int(user["id"]), "ADMIN")
            demoted.append(f"{user.get('username') or user['id']}({user['id']})")

    success, message = user_manager.update_user_role(int(user_id), "OWNER")
    if not success:
        raise ValueError(message)
    return {
        "id": int(user_id),
        "username": target.get("username") or user_id,
        "demoted": demoted,
    }