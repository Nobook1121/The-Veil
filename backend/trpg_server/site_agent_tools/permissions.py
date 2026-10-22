"""权限矩阵工具：只允许在自身权限范围内改动。"""

from __future__ import annotations

from typing import Any

from flask import current_app

from trpg_server.permission_config import (
    DEFAULT_ROLE_ORDER,
    load_permission_config,
    permission_config_path,
    permission_node_ids,
    save_permission_config,
)
from trpg_server.site_agent_auth import can_modify_permission_row, sensitive_node_allowed
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema


def _config_path():
    return current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(
        current_app.config.get("CONFIG_DIR")
    )


def _grouped_nodes() -> dict[str, dict[str, str]]:
    config = load_permission_config(_config_path())
    result: dict[str, dict[str, str]] = {}
    for group in config.get("groups", []):
        result[str(group.get("id"))] = {
            str(node.get("id")): str(node.get("label")) for node in group.get("nodes", [])
        }
    return result


def list_matrix(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    config = load_permission_config(_config_path())
    return ToolResult.success(
        {"matrix": config.get("matrix", {}), "groups": _grouped_nodes()},
        summary="读取权限矩阵",
    )


def preview_set_permission(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    node_id = str(arguments.get("node_id") or "")
    role = str(arguments.get("role") or "").upper()
    allowed = bool(arguments.get("allowed"))
    config = load_permission_config(_config_path())
    before = role in config["matrix"].get(node_id, [])
    preview = {
        "node_id": node_id,
        "role": role,
        "before": before,
        "after": allowed,
        "summary": f"将 {role} 的 {node_id} 权限改为{'允许' if allowed else '拒绝'}",
        "apply_args": {"node_id": node_id, "role": role, "allowed": allowed},
    }
    return ToolResult.success(preview, summary=preview["summary"])


def apply_set_permission(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    node_id = str(arguments.get("node_id") or "")
    role = str(arguments.get("role") or "").upper()
    allowed = bool(arguments.get("allowed"))
    config = load_permission_config(_config_path())
    roles = [item for item in config["matrix"].get(node_id, []) if item != role]
    if allowed:
        roles = [item for item in DEFAULT_ROLE_ORDER if item in {*roles, role}]
    config["matrix"][node_id] = roles
    save_permission_config(_config_path(), config)
    return ToolResult.success(
        {"node_id": node_id, "role": role, "allowed": allowed},
        summary=f"已更新 {role} 的 {node_id} 权限",
    )


def _check_set_permission(actor: Any, arguments: dict[str, Any]) -> str | None:
    node_id = str(arguments.get("node_id") or "")
    role = str(arguments.get("role") or "").upper()
    if role not in DEFAULT_ROLE_ORDER:
        return "role 必须是 USER、ADMIN 或 OWNER"
    if node_id not in permission_node_ids():
        return "权限节点不存在"
    if not sensitive_node_allowed(actor, node_id):
        return "该权限节点只有 OWNER 可以通过 Agent 修改"
    reason = can_modify_permission_row(actor, role)
    if reason:
        return {
            "permission_denied": "缺少 settings.permissions 权限",
            "admin_cannot_modify_elevated_row": "管理员只能修改 USER 行的权限",
        }.get(reason, reason)
    if node_id == "agent.use" and actor.role != "OWNER" and not actor.has("agent.use"):
        return "不能给自己授予 agent.use 权限"
    return None


PERMISSION_TOOLS = [
    ToolSpec(
        name="permissions.list_matrix",
        description="读取当前权限矩阵与节点分组。",
        parameters=schema({}),
        risk="read",
        required_permission="settings.permissions",
        handler=list_matrix,
    ),
    ToolSpec(
        name="permissions.set_role_permission",
        description=(
            "修改某个角色对某个权限节点的允许状态。ADMIN 只能改 USER 行，"
            "敏感节点仅 OWNER 可改；执行前会生成待确认操作。"
        ),
        parameters=schema(
            {
                "node_id": {"type": "string", "maxLength": 80},
                "role": {"type": "string", "enum": list(DEFAULT_ROLE_ORDER)},
                "allowed": {"type": "boolean"},
            },
            ["node_id", "role", "allowed"],
        ),
        risk="danger",
        required_permission="settings.permissions",
        preview=preview_set_permission,
        apply=apply_set_permission,
        check=_check_set_permission,
    ),
]
