"""网站 Agent 的统一鉴权。

身份只来自服务端会话与用户数据库；模型传入的任何身份字段都不会被读取。
``authorize_tool`` 是纯检查函数，不产生副作用，便于单元测试与审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import current_app, session

from trpg_server.permission_config import is_role_allowed, permission_config_path, permission_node_ids
from trpg_server.security import (
    ACTIVE_SESSION_REGISTRY_KEY,
    SESSION_TOKEN_KEY,
    USER_ROOM_MEMBERSHIP_KEY,
    get_user_manager,
    user_room_ids,
)
from trpg_server.settings import CONFIG_DIR

ROLE_RANK = {"USER": 1, "ADMIN": 2, "OWNER": 3}
AGENT_USE_NODE = "agent.use"
# 这些权限节点只有 OWNER 能通过 Agent 修改，避免管理员给自己或同僚扩权。
OWNER_ONLY_NODES = frozenset(
    {"settings.permissions", "navigation.admin_tabs", "accounts.manage_users", "agent.use"}
)


class AgentAuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Actor:
    user_id: int
    username: str
    role: str
    permissions: frozenset[str]
    room_ids: frozenset[str]
    session_current: bool = True

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(str(self.role).upper(), 0)

    @property
    def is_admin(self) -> bool:
        return self.rank >= ROLE_RANK["ADMIN"]

    @property
    def is_owner(self) -> bool:
        return self.rank >= ROLE_RANK["OWNER"]

    def has(self, node: str | None) -> bool:
        if node is None:
            return True
        return node in self.permissions


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False
    confirmation_level: str = ""

    @classmethod
    def allow(cls, *, requires_confirmation: bool = False, level: str = "normal") -> "Decision":
        return cls(True, "allow", requires_confirmation, level if requires_confirmation else "")

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(False, reason, False, "")


def _permission_path():
    return current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(
        current_app.config.get("CONFIG_DIR", CONFIG_DIR)
    )


def permission_set_for_role(role: str) -> frozenset[str]:
    path = _permission_path()
    return frozenset(node for node in permission_node_ids() if is_role_allowed(role, node, path))


def is_session_current() -> bool:
    if "user_id" not in session:
        return False
    registry = current_app.config.setdefault(ACTIVE_SESSION_REGISTRY_KEY, {})
    active_token = registry.get(str(session["user_id"]))
    return not active_token or session.get(SESSION_TOKEN_KEY) == active_token


def build_actor() -> Actor:
    user_id = session.get("user_id")
    if user_id is None:
        raise AgentAuthError("not_logged_in", "请先登录", 401)
    user = get_user_manager().get_user_by_id(user_id)
    if not user:
        raise AgentAuthError("not_logged_in", "登录状态已失效，请重新登录", 401)
    # 角色以数据库当前值为准，而不是 session 中可能已经过期的旧值。
    role = str(user.get("role") or session.get("role") or "USER").upper()
    membership = current_app.config.get(USER_ROOM_MEMBERSHIP_KEY, {}) or {}
    room_ids = user_room_ids(user_id, membership)
    return Actor(
        user_id=int(user_id),
        username=str(user.get("username") or session.get("username") or ""),
        role=role,
        permissions=permission_set_for_role(role),
        room_ids=frozenset(room_ids),
        session_current=is_session_current(),
    )


def authorize_tool(
    actor: Actor,
    spec: Any,
    args: dict[str, Any],
    *,
    config: Any,
    room_id: str | None = None,
) -> Decision:
    """按固定顺序做鉴权；任何一步失败都不执行工具。"""
    if not actor.session_current:
        return Decision.deny("session_expired")
    if not getattr(config, "enabled", False):
        return Decision.deny("agent_disabled")
    if not actor.has(AGENT_USE_NODE):
        return Decision.deny("agent_forbidden")
    if spec.required_permission and not actor.has(spec.required_permission):
        return Decision.deny("permission_denied")
    if spec.scope == "room":
        if not room_id:
            return Decision.deny("room_context_required")
        if room_id not in actor.room_ids:
            return Decision.deny("room_not_joined")
    if callable(spec.check):
        reason = spec.check(actor, args)
        if reason:
            return Decision.deny(reason)
    if spec.risk in {"write", "danger"}:
        return Decision.allow(requires_confirmation=True, level=spec.risk)
    return Decision.allow()


def owner_count() -> int:
    try:
        users = get_user_manager().get_all_users()
    except Exception:
        return 0
    return sum(1 for user in users if str(user.get("role")).upper() == "OWNER")


def can_modify_role(actor: Actor, target_role: str, new_role: str, *, target_is_self: bool) -> str | None:
    """用户角色变更的等级规则；返回拒绝原因或 ``None``。"""
    target_rank = ROLE_RANK.get(str(target_role).upper(), 0)
    if target_is_self:
        return "self_role_change_forbidden"
    if target_rank > actor.rank:
        return "target_outranks_actor"
    if ROLE_RANK.get(str(new_role).upper(), 0) > actor.rank:
        return "role_above_actor"
    return None


def can_modify_permission_row(actor: Actor, target_role: str) -> str | None:
    """ADMIN 只能修改 USER 行；OWNER 不受限。"""
    if actor.is_owner:
        return None
    if not actor.is_admin:
        return "permission_denied"
    if ROLE_RANK.get(str(target_role).upper(), 0) > ROLE_RANK["USER"]:
        return "admin_cannot_modify_elevated_row"
    return None


def sensitive_node_allowed(actor: Actor, node_id: str) -> bool:
    if node_id not in OWNER_ONLY_NODES:
        return True
    return actor.is_owner
