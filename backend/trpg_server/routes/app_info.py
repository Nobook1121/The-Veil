"""关于页与更新检查的公开接口。"""

from __future__ import annotations

import threading

from flask import Blueprint, current_app, session

from trpg_server import update_service
from trpg_server.app_info import build_app_info
from trpg_server.responses import error_response, success_response
from trpg_server.security import require_permission_node

bp = Blueprint("app_info", __name__)


@bp.route("/api/app/info", methods=["GET"])
def app_info():
    return success_response(data=build_app_info(current_app))


def _is_owner() -> bool:
    return str(session.get("role") or "").upper() == "OWNER"


@bp.route("/api/app/update", methods=["GET"])
def app_update_state():
    data = update_service.get_state(current_app)
    # 前端只给 OWNER 显示应用内更新入口；是否允许应用由服务端再次校验。
    data["owner"] = _is_owner()
    return success_response(data=data)


@bp.route("/api/app/update/check", methods=["POST"])
@require_permission_node("settings.general")
def app_update_check():
    return success_response(data=update_service.check_for_update(force=True, app=current_app._get_current_object()))


@bp.route("/api/app/update/apply", methods=["POST"])
def app_update_apply():
    if not _is_owner():
        return error_response(
            "只有 OWNER 可以应用更新",
            403,
            "owner_only",
        )
    return success_response(data=update_service.apply_update(current_app._get_current_object()))


@bp.route("/api/app/update/download", methods=["POST"])
@require_permission_node("settings.general")
def app_update_download():
    app = current_app._get_current_object()
    threading.Thread(
        target=update_service.download_update,
        kwargs={"app": app},
        name="update-download",
        daemon=True,
    ).start()
    return success_response(data={"state": "downloading", "progress": 0})
