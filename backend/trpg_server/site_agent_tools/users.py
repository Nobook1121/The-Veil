"""用户管理工具：列表、详情、角色与状态变更、强制重置密码。"""

from __future__ import annotations

from typing import Any

from flask import current_app

from trpg_server.security import get_user_manager
from trpg_server.site_agent_auth import ROLE_RANK, can_modify_role, owner_count
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

VALID_ROLES = ("USER", "ADMIN", "OWNER")
VALID_STATUSES = ("active", "inactive", "banned")


def _find_user(user_id: Any) -> dict[str, Any] | None:
    try:
        return get_user_manager().get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None


def _summary(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "status": user.get("status"),
        "created_at": user.get("created_at"),
        "last_login": user.get("last_login"),
    }


def list_users(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    query = str(arguments.get("query") or "").strip().lower()
    limit = max(1, min(int(arguments.get("limit") or 20), 100))
    users = [_summary(user) for user in get_user_manager().get_all_users()]
    if query:
        users = [
            user
            for user in users
            if query in str(user.get("username") or "").lower()
            or query == str(user.get("user_id"))
        ]
    users.sort(key=lambda user: int(user.get("user_id") or 0))
    return ToolResult.success({"items": users[:limit], "total": len(users)}, summary=f"共 {len(users)} 个用户")


def get_user(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return ToolResult.failure("用户不存在")
    return ToolResult.success(_summary(user), summary=f"读取用户 {user.get('username')}")


def preview_set_role(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return ToolResult.failure("用户不存在")
    role = str(arguments.get("role") or "").upper()
    preview = {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "before": user.get("role"),
        "after": role,
        "summary": f"将用户 {user.get('username')} 的角色从 {user.get('role')} 改为 {role}",
        "apply_args": {"user_id": int(user["id"]), "role": role},
    }
    return ToolResult.success(preview, summary=preview["summary"])


def apply_set_role(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user_id = int(arguments.get("user_id") or 0)
    role = str(arguments.get("role") or "").upper()
    success, message = get_user_manager().update_user_role(user_id, role)
    if not success:
        return ToolResult.failure(message)
    _notify_role_change(user_id, role)
    return ToolResult.success({"user_id": user_id, "role": role}, summary=f"已把用户 {user_id} 的角色改为 {role}")


def _check_set_role(actor: Any, arguments: dict[str, Any]) -> str | None:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return "用户不存在"
    role = str(arguments.get("role") or "").upper()
    if role not in VALID_ROLES:
        return "role 必须是 USER、ADMIN 或 OWNER"
    reason = can_modify_role(
        actor,
        str(user.get("role") or "USER"),
        role,
        target_is_self=str(user.get("id")) == str(actor.user_id),
    )
    mapping = {
        "self_role_change_forbidden": "不能修改自己的角色",
        "target_outranks_actor": "不能操作权限高于自己的账号",
        "role_above_actor": "不能把用户提升到高于自己的角色",
    }
    return mapping.get(reason or "", reason)


def preview_set_status(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return ToolResult.failure("用户不存在")
    status = str(arguments.get("status") or "")
    preview = {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "before": user.get("status"),
        "after": status,
        "summary": f"将用户 {user.get('username')} 的状态从 {user.get('status')} 改为 {status}",
        "apply_args": {"user_id": int(user["id"]), "status": status},
    }
    return ToolResult.success(preview, summary=preview["summary"])


def apply_set_status(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user_id = int(arguments.get("user_id") or 0)
    status = str(arguments.get("status") or "")
    success, message = get_user_manager().update_user_status(user_id, status)
    if not success:
        return ToolResult.failure(message)
    return ToolResult.success({"user_id": user_id, "status": status}, summary=f"已把用户 {user_id} 的状态改为 {status}")


def _check_set_status(actor: Any, arguments: dict[str, Any]) -> str | None:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return "用户不存在"
    status = str(arguments.get("status") or "")
    if status not in VALID_STATUSES:
        return "status 必须是 active、inactive 或 banned"
    if str(user.get("id")) == str(actor.user_id):
        return "不能修改自己的账号状态"
    if ROLE_RANK.get(str(user.get("role")), 0) > actor.rank:
        return "不能操作权限高于自己的账号"
    if str(user.get("role")).upper() == "OWNER" and status != "active" and owner_count() <= 1:
        return "不能停用或封禁最后一名 OWNER"
    return None


def preview_reset_password(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return ToolResult.failure("用户不存在")
    preview = {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "summary": f"为用户 {user.get('username')} 生成一次性密码重置令牌",
        "apply_args": {"user_id": int(user["id"])},
    }
    return ToolResult.success(preview, summary=preview["summary"])


def apply_reset_password(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user_id = int(arguments.get("user_id") or 0)
    success, message, _token = get_user_manager().force_password_reset(user_id)
    if not success:
        return ToolResult.failure(message)
    # 令牌属于敏感凭据，不进入 Agent 消息；管理员可到用户管理页复制。
    return ToolResult.success(
        {"user_id": user_id},
        summary="已生成密码重置令牌，请在「用户管理」页面复制重置链接",
    )


def _check_reset_password(actor: Any, arguments: dict[str, Any]) -> str | None:
    user = _find_user(arguments.get("user_id"))
    if not user:
        return "用户不存在"
    if str(user.get("id")) == str(actor.user_id):
        return "不能用 Agent 重置自己的密码"
    if ROLE_RANK.get(str(user.get("role")), 0) > actor.rank:
        return "不能操作权限高于自己的账号"
    return None


def _notify_role_change(user_id: int, role: str) -> None:
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("role_changed", {"user_id": user_id, "role": role}, room=f"user:{user_id}")


USER_TOOLS = [
    ToolSpec(
        name="users.list",
        description="列出用户摘要（ID、用户名、角色、状态），不含密码或令牌。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 40},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
        risk="read",
        required_permission="accounts.manage_users",
        handler=list_users,
    ),
    ToolSpec(
        name="users.get",
        description="读取单个用户的公开管理信息。",
        parameters=schema({"user_id": {"type": "integer"}}, ["user_id"]),
        risk="read",
        required_permission="accounts.manage_users",
        handler=get_user,
    ),
    ToolSpec(
        name="users.set_role",
        description="修改用户角色；必须低于或等于操作者等级，且不能修改自己。",
        parameters=schema(
            {
                "user_id": {"type": "integer"},
                "role": {"type": "string", "enum": list(VALID_ROLES)},
            },
            ["user_id", "role"],
        ),
        risk="danger",
        required_permission="accounts.manage_users",
        preview=preview_set_role,
        apply=apply_set_role,
        check=_check_set_role,
    ),
    ToolSpec(
        name="users.set_status",
        description="修改用户状态（active/inactive/banned），不能操作自己或更高等级账号。",
        parameters=schema(
            {
                "user_id": {"type": "integer"},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
            },
            ["user_id", "status"],
        ),
        risk="danger",
        required_permission="accounts.manage_users",
        preview=preview_set_status,
        apply=apply_set_status,
        check=_check_set_status,
    ),
    ToolSpec(
        name="users.force_password_reset",
        description="为用户生成一次性密码重置令牌；令牌不会出现在对话中。",
        parameters=schema({"user_id": {"type": "integer"}}, ["user_id"]),
        risk="danger",
        required_permission="accounts.manage_users",
        preview=preview_reset_password,
        apply=apply_reset_password,
        check=_check_reset_password,
    ),
]
