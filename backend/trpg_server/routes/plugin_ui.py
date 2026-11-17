"""插件 UI 宿主对前端暴露的两个只读接口。

- ``GET /api/plugins/<plugin_id>/assets/<path>``：只服务插件清单
  ``capabilities.frontend_assets`` 里声明过的文件，避免把 ``entry.py`` 等
  代码文件当静态资源读出去。
- ``GET /api/rooms/<room_id>/ui``：按房间规则集与当前用户权限裁剪
  ``capabilities.ui.slots``，供前端 PluginUiHost 决定挂载哪些插件 UI。

核心不认识任何具体插件：这里只转述声明、计算权限并与插件注册表对齐。
"""

from __future__ import annotations

import mimetypes

from flask import Blueprint, send_file, session

from trpg_server.plugins import plugin_registry
from trpg_server.plugins.security import safe_plugin_path
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated

bp = Blueprint("plugin_ui", __name__)

_PLUGIN_ASSET_PREFIX = "/api/plugins"
_OWNER_ROLES = {"owner", "admin"}


def _login_required():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _room_permissions(info: dict) -> tuple[list[str], bool]:
    """返回当前用户在房间内可获得的能力集合，以及是否为房管。"""
    user_id = str(session.get("user_id") or "")
    member = None
    for item in (info or {}).get("members") or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_active", True) is False or item.get("status") == "removed":
            continue
        if str(item.get("user_id")) == user_id:
            member = item
            break
    role = str((member or {}).get("room_role") or "").strip().lower()
    if member is not None and str(member.get("user_id")) == str((info or {}).get("creator_id")):
        role = "owner"
    manager = is_elevated() or role in _OWNER_ROLES
    permissions = ["room.read", "token.read", "token.move"]
    if manager:
        permissions.extend(["token.move.dm", "map.edit"])
    return permissions, manager


def _room_ruleset(info: dict) -> str:
    """房间规则集解析失败时显式抛错，由全局错误处理返回 409。"""
    from trpg_server.room_ops.views import _room_ruleset_id

    return _room_ruleset_id(info)


def _slot_assets(plugin_id: str, capabilities: dict) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    for asset in capabilities.get("frontend_assets") or []:
        path = str(asset.get("path") or "").strip()
        if not path:
            continue
        assets.append(
            {
                "url": f"{_PLUGIN_ASSET_PREFIX}/{plugin_id}/assets/{path}",
                "type": str(asset.get("type") or "script"),
            }
        )
    return assets


def _visible_slots(ruleset_id: str, granted: set[str]) -> list[dict]:
    registry = plugin_registry()
    if registry is None:
        return []
    slots: list[dict] = []
    for record in registry.records():
        if not record.loaded or record.manifest is None:
            continue
        capabilities = record.manifest.capabilities or {}
        ui = capabilities.get("ui") or {}
        declared_slots = ui.get("slots") if isinstance(ui, dict) else None
        if not isinstance(declared_slots, list):
            continue
        assets = _slot_assets(record.id, capabilities)
        for slot in declared_slots:
            if not isinstance(slot, dict):
                continue
            rulesets = [str(item or "").strip().lower() for item in slot.get("rulesets") or []]
            if rulesets and ruleset_id.lower() not in rulesets:
                continue
            # 只要求最基础的读取能力；token.move 等交给插件按用户能力自行降级，
            # 真正的写权限仍由插件服务端接口校验。
            if "room.read" not in granted:
                continue
            slots.append(
                {
                    "plugin_id": record.id,
                    "id": str(slot.get("id") or ""),
                    "slot": str(slot.get("slot") or ""),
                    "title_i18n": str(slot.get("title_i18n") or ""),
                    "mount": str(slot.get("mount") or ""),
                    "layout": dict(slot.get("layout") or {}),
                    "priority": int(slot.get("priority") or 0),
                    "assets": assets,
                }
            )
    slots.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    return slots


@bp.route("/api/plugins/<plugin_id>/assets/<path:asset_path>", methods=["GET"])
def plugin_asset(plugin_id: str, asset_path: str):
    denied = _login_required()
    if denied:
        return denied
    registry = plugin_registry()
    if registry is None:
        return error_response("Plugin registry unavailable", 503, "plugin.registry_unavailable")
    record = registry.get(plugin_id)
    if record is None or not record.loaded or record.manifest is None:
        return error_response("Plugin not found", 404, "plugin.not_found")
    declared = {
        str(asset.get("path") or "")
        for asset in (record.manifest.capabilities or {}).get("frontend_assets") or []
    }
    normalized = str(asset_path or "").strip().replace("\\", "/")
    if normalized not in declared:
        return error_response("Asset not found", 404, "plugin.asset_not_found")
    try:
        path = safe_plugin_path(record.plugin_dir, normalized)
    except Exception:  # noqa: BLE001 - 越界或符号链接统一按未找到处理
        return error_response("Asset not found", 404, "plugin.asset_not_found")
    if not path.is_file():
        return error_response("Asset not found", 404, "plugin.asset_not_found")
    mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = send_file(path, mimetype=mimetype, conditional=True, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/api/rooms/<room_id>/ui", methods=["GET"])
def room_ui(room_id: str):
    denied = _login_required()
    if denied:
        return denied
    _room_dir, info = find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return error_response("Permission denied", 403, "Permission denied")
    permissions, _manager = _room_permissions(info)
    ruleset_id = _room_ruleset(info)
    slots = _visible_slots(ruleset_id, set(permissions))
    return success_response(
        {
            "room_id": str(room_id),
            "ruleset_id": ruleset_id,
            "permissions": permissions,
            "slots": slots,
        },
        "Room UI declarations",
    )


__all__ = ["bp"]
