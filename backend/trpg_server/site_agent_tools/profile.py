"""个人设置工具：只允许修改当前登录用户自己的昵称与邮箱。"""

from __future__ import annotations

import re
from typing import Any

from trpg_server.security import get_user_manager
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ALLOWED_PRESENCE = {"online", "dnd", "invisible"}
_FIELD_LABELS = {"nickname": "昵称", "email": "邮箱", "presence": "在线状态"}


def _profile(actor: Any) -> dict[str, Any] | None:
    user = get_user_manager().get_user_by_id(int(actor.user_id))
    return user if isinstance(user, dict) else None


def _public_profile(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": user.get("username", ""),
        "nickname": user.get("nickname", ""),
        "email": user.get("email", ""),
        "role": user.get("role", ""),
        "status": user.get("status", ""),
        "presence": user.get("presence", "online"),
    }


def get_my_profile(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _profile(actor)
    if not user:
        return ToolResult.failure("当前用户不存在")
    return ToolResult.success(_public_profile(user), summary="读取个人资料")


def _validate(arguments: dict[str, Any], user: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    nickname_raw = arguments.get("nickname")
    email_raw = arguments.get("email")
    presence_raw = arguments.get("presence")
    if nickname_raw is None and email_raw is None and presence_raw is None:
        return {}, "至少提供 nickname、email 或 presence 其中一项"
    changes: dict[str, Any] = {}
    if nickname_raw is not None:
        nickname = str(nickname_raw).strip()
        if not nickname:
            return {}, "昵称不能为空"
        if len(nickname) > 64:
            return {}, "昵称不能超过 64 个字符"
        if nickname != user.get("nickname"):
            changes["nickname"] = nickname
    if email_raw is not None:
        email = str(email_raw).strip()
        if not _EMAIL_RE.match(email):
            return {}, "邮箱格式不正确"
        if len(email) > 254:
            return {}, "邮箱过长"
        if email.lower() != str(user.get("email") or "").lower():
            changes["email"] = email
    if presence_raw is not None:
        presence = str(presence_raw).strip().lower()
        if presence not in _ALLOWED_PRESENCE:
            return {}, "在线状态只能是 online、dnd 或 invisible"
        if presence != str(user.get("presence") or "online"):
            changes["presence"] = presence
    if not changes:
        return {}, "没有需要修改的内容"
    return changes, None


def preview_update_my_profile(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _profile(actor)
    if not user:
        return ToolResult.failure("当前用户不存在")
    changes, error = _validate(arguments, user)
    if error:
        return ToolResult.failure(error)
    rows = [
        {"field": _FIELD_LABELS.get(field, field), "before": user.get(field, ""), "after": value}
        for field, value in changes.items()
    ]
    summary = "将更新个人资料：" + "、".join(row["field"] for row in rows)
    return ToolResult.success(
        {"changes": rows, "summary": summary, "apply_args": changes},
        summary=summary,
    )


def apply_update_my_profile(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    user = _profile(actor)
    if not user:
        return ToolResult.failure("当前用户不存在")
    changes, error = _validate(arguments, user)
    if error:
        return ToolResult.failure(error)
    manager = get_user_manager()
    if "nickname" in changes or "email" in changes:
        ok, message = manager.update_profile(
            int(actor.user_id),
            str(user.get("username") or ""),
            str(changes.get("email") or user.get("email") or ""),
            nickname=str(changes.get("nickname") or user.get("nickname") or ""),
        )
        if not ok:
            return ToolResult.failure(message)
    if "presence" in changes:
        ok, message = manager.update_presence(int(actor.user_id), str(changes["presence"]))
        if not ok:
            return ToolResult.failure(message)
    names = "、".join(_FIELD_LABELS.get(field, field) for field in changes)
    return ToolResult.success({"updated": changes}, summary=f"已更新个人资料：{names}")


def _check_update_profile(actor: Any, arguments: dict[str, Any]) -> str | None:
    user = _profile(actor)
    if not user:
        return "当前用户不存在"
    _, error = _validate(arguments, user)
    return error


PROFILE_TOOLS = [
    ToolSpec(
        name="profile.get_my_profile",
        description="读取当前登录用户自己的个人资料（昵称、邮箱、角色等）。",
        parameters=schema({}),
        risk="read",
        handler=get_my_profile,
    ),
    ToolSpec(
        name="profile.update_my_profile",
        description="修改当前登录用户自己的昵称、邮箱或在线状态；不能修改其他用户。执行前会生成待确认操作。",
        parameters=schema(
            {
                "nickname": {"type": "string", "maxLength": 64},
                "email": {"type": "string", "maxLength": 254},
                "presence": {"type": "string", "enum": ["online", "dnd", "invisible"]},
            }
        ),
        risk="write",
        preview=preview_update_my_profile,
        apply=apply_update_my_profile,
        check=_check_update_profile,
    ),
]
