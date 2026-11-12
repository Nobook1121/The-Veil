"""插件管理接口（仅站点管理员）。

- ``GET    /api/plugins``                    清单：已加载 / 已禁用 / 加载失败及原因
- ``GET    /api/plugins/schema``             清单 JSON Schema（给插件开发者）
- ``POST   /api/plugins/rescan``             重新扫描插件目录
- ``POST   /api/plugins/<plugin_id>/enable``  启用并落盘到 enabled.json
- ``POST   /api/plugins/<plugin_id>/disable`` 禁用并落盘
- ``DELETE /api/plugins/<plugin_id>``         卸载（需先禁用，body ``{"confirm": "<id>"}``）

插件代码以宿主进程权限运行，因此这里所有写操作都要求 ADMIN/OWNER；普通用户
连清单都看不到（清单里含本机绝对路径与失败原因，属于运维信息）。
"""

from __future__ import annotations

import logging

from flask import Blueprint, request, session

from trpg_server.plugins import bootstrap, plugin_registry
from trpg_server.plugins.manifest import manifest_json_schema
from trpg_server.plugins.registry import PluginNotFound, PluginStateError
from trpg_server.responses import error_response, server_error, success_response

bp = Blueprint("plugins", __name__)
logger = logging.getLogger(__name__)

_ADMIN_ROLES = {"ADMIN", "OWNER"}


def _is_admin() -> bool:
    return str(session.get("role") or "").upper() in _ADMIN_ROLES


def _guard():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _is_admin():
        return error_response("Permission denied", 403, "Permission denied")
    return None


def _registry():
    registry = plugin_registry()
    if registry is None:
        return None, error_response("Plugin registry unavailable", 503, "plugin.registry_unavailable")
    return registry, None


@bp.route("/api/plugins", methods=["GET"])
def list_plugins():
    denied = _guard()
    if denied:
        return denied
    registry, error = _registry()
    if error:
        return error
    return success_response(registry.summary(), "Plugins loaded")


@bp.route("/api/plugins/schema", methods=["GET"])
def plugin_manifest_schema():
    denied = _guard()
    if denied:
        return denied
    return success_response(manifest_json_schema(), "Plugin manifest schema")


@bp.route("/api/plugins/rescan", methods=["POST"])
def rescan_plugins():
    denied = _guard()
    if denied:
        return denied
    registry, error = _registry()
    if error:
        return error
    try:
        registry.load()
    except Exception:  # noqa: BLE001 - 扫描失败要回稳定 500，而不是 Flask 默认页
        logger.exception("plugin_rescan_failed root=%s", getattr(registry, "root", ""))
        return server_error("Plugin rescan failed")
    return success_response(registry.summary(), "Plugins rescanned")


@bp.route("/api/plugins/<plugin_id>/enable", methods=["POST"])
def enable_plugin(plugin_id: str):
    denied = _guard()
    if denied:
        return denied
    registry, error = _registry()
    if error:
        return error
    try:
        result = registry.enable(plugin_id)
    except PluginNotFound:
        return error_response("Plugin not found", 404, "plugin.not_found")
    except PluginStateError as exc:
        return error_response(str(exc), 400, "plugin.state_error")
    return success_response(result, "Plugin enabled")


@bp.route("/api/plugins/<plugin_id>/disable", methods=["POST"])
def disable_plugin(plugin_id: str):
    denied = _guard()
    if denied:
        return denied
    registry, error = _registry()
    if error:
        return error
    try:
        result = registry.disable(plugin_id)
    except PluginNotFound:
        return error_response("Plugin not found", 404, "plugin.not_found")
    except PluginStateError as exc:
        return error_response(str(exc), 400, "plugin.state_error")
    return success_response(result, "Plugin disabled")


@bp.route("/api/plugins/<plugin_id>", methods=["DELETE"])
def uninstall_plugin(plugin_id: str):
    denied = _guard()
    if denied:
        return denied
    registry, error = _registry()
    if error:
        return error
    payload = request.get_json(silent=True)
    confirm = str((payload or {}).get("confirm") or "") if isinstance(payload, dict) else ""
    try:
        result = registry.uninstall(plugin_id, confirm=confirm)
    except PluginNotFound:
        return error_response("Plugin not found", 404, "plugin.not_found")
    except PluginStateError as exc:
        return error_response(str(exc), 400, "plugin.state_error")
    return success_response(result, "Plugin uninstalled")


__all__ = ["bp"]
