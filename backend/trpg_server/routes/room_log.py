"""团录导出 REST 接口（设计稿 §5.2 / §6.2）。

- ``GET    /api/rooms/<room_id>/log/preview``                 预览归约结果 + 权限/模板
- ``POST   /api/rooms/<room_id>/log/exports``                 创建导出任务
- ``GET    /api/rooms/<room_id>/log/exports/<export_id>``     查询任务状态
- ``GET    /api/rooms/<room_id>/log/exports/<export_id>/file``下载 PDF
- ``DELETE /api/rooms/<room_id>/log/exports/<export_id>``     清理任务与产物

导出逻辑本身全部在 ``room_log`` 包内，这里只做鉴权、参数收敛和错误码映射。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping

from flask import Blueprint, current_app, request, send_file, session

from trpg_server.responses import error_response, server_error, success_response
from trpg_server.room_log.fonts import FontMissing, resolve_font
from trpg_server.room_log.jobs import STATUS_DONE, get_store, room_log_settings
from trpg_server.room_log.model import build_document
from trpg_server.room_log.render import TEMPLATES_DIR, load_template
from trpg_server.routes.rooms import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_OWNER,
    _can_access,
    _find_member,
    _find_room,
    _is_elevated,
    _require_login,
    _room_permission,
)

bp = Blueprint("room_log", __name__)
logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f\\/:*?"<>|]+')
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class OptionError(ValueError):
    """客户端传入的选项非法。"""


def _settings() -> dict[str, Any]:
    return room_log_settings(current_app.config.get("CONFIG_DIR"))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def _option_source() -> Mapping[str, Any]:
    if request.method == "GET":
        return request.args
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, Mapping) else {}


def _available_templates() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not TEMPLATES_DIR.is_dir():
        return entries
    for root in sorted(TEMPLATES_DIR.iterdir()):
        if not (root / "template.json").is_file():
            continue
        try:
            meta = load_template(root.name)
        except Exception:  # noqa: BLE001 - 坏模板不应拖垮整个列表
            logger.warning("room_log template invalid id=%s", root.name, exc_info=True)
            continue
        entries.append(
            {
                "id": str(meta.get("id") or root.name),
                "name": str(meta.get("name") or root.name),
                "description": str(meta.get("description") or ""),
            }
        )
    return entries


def _parse_options(source: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    default_template = str(settings.get("default_template") or "archive")
    template = str(source.get("template") or default_template).strip()
    try:
        load_template(template)
    except Exception as exc:  # noqa: BLE001
        if source.get("template"):
            raise OptionError("template") from exc
        template = "archive"
        load_template(template)
    options: dict[str, Any] = {
        "template": template,
        "include_kp_only": _as_bool(source.get("include_kp_only"), False),
        "include_dice": _as_bool(source.get("include_dice"), _as_bool(settings.get("include_dice"), True)),
        "include_ooc": _as_bool(source.get("include_ooc"), _as_bool(settings.get("include_ooc"), True)),
        "include_images": _as_bool(
            source.get("include_images"), _as_bool(settings.get("include_images"), True)
        ),
        "date_from": None,
        "date_to": None,
        "max_messages": int(settings.get("max_messages_per_export") or 0) or None,
    }
    for key in ("date_from", "date_to"):
        raw = str(source.get(key) or "").strip()
        if raw and not _DATE_RE.match(raw):
            raise OptionError(key)
        options[key] = raw or None
    if options["date_from"] and options["date_to"] and options["date_from"] > options["date_to"]:
        raise OptionError("date_range")
    return options


def _font_state(settings: Mapping[str, Any]) -> tuple[bool, str | None]:
    try:
        _family, path = resolve_font(str(settings.get("font_file") or ""))
    except FontMissing:
        return False, None
    return True, path


def _permission_state(info: Mapping[str, Any], settings: Mapping[str, Any]) -> tuple[bool, str | None]:
    """返回 ``(can_export, reason)``；调用方已确认用户可进入房间。"""
    if _is_elevated():
        return True, None
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    role = _room_permission(member, info)
    if role in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}:
        return True, None
    if _as_bool(settings.get("members_can_export"), True):
        return True, None
    return False, "members_disabled"


def _load_room(room_id: str):
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return None, None, error_response("Room not found", 404, "Room not found")
    return room_dir, info, None


def _forbidden(room_id: str, message: str = "Room log export is not allowed"):
    return error_response(message, 403, "log.forbidden")


@bp.route("/api/rooms/<room_id>/log/preview", methods=["GET"])
def preview_room_log(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    settings = _settings()
    if not _as_bool(settings.get("enabled"), True):
        return _forbidden(room_id, "Room log export is disabled")
    if not _can_access(info):
        return _forbidden(room_id, "Not a room member")

    try:
        options = _parse_options(_option_source(), settings)
    except OptionError:
        return error_response("Invalid export options", 400, "log.invalid_option")

    can_export, reason = _permission_state(info, settings)
    font_ok, _font_path = _font_state(settings)
    if can_export and not font_ok:
        can_export, reason = False, "font_missing"

    try:
        document = build_document(room_dir, options)
    except Exception:  # noqa: BLE001 - 读取房间数据失败按渲染失败处理
        logger.exception("room_log preview failed room=%s", room_id)
        return server_error("Room log preview failed")

    if document["stats"]["messages_exported"] == 0:
        return error_response("This room has no exportable messages", 400, "log.empty")

    templates = _available_templates()
    for entry in templates:
        entry["available"] = font_ok
        if not font_ok:
            entry["reason"] = "font_missing"
    data = dict(document)
    data["can_export"] = bool(can_export)
    data["can_export_reason"] = reason
    data["templates"] = templates
    return success_response(data, "Room log preview loaded successfully")


@bp.route("/api/rooms/<room_id>/log/exports", methods=["POST"])
def create_room_log_export(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    settings = _settings()
    if not _as_bool(settings.get("enabled"), True):
        return _forbidden(room_id, "Room log export is disabled")
    if not _can_access(info):
        return _forbidden(room_id, "Not a room member")

    can_export, _reason = _permission_state(info, settings)
    if not can_export:
        return _forbidden(room_id)
    font_ok, font_path = _font_state(settings)
    if not font_ok:
        return error_response("No usable CJK font", 503, "log.font_missing")

    try:
        options = _parse_options(_option_source(), settings)
    except OptionError:
        return error_response("Invalid export options", 400, "log.invalid_option")

    try:
        document = build_document(room_dir, options)
    except Exception:  # noqa: BLE001
        logger.exception("room_log export build failed room=%s", room_id)
        return server_error("Room log export failed")
    if document["stats"]["messages_exported"] == 0:
        return error_response("This room has no exportable messages", 400, "log.empty")

    store = get_store()
    store.cleanup(int(settings.get("keep_exports_days") or 0))
    job = store.create(room_id, options, session.get("user_id"))
    store.start(
        current_app._get_current_object(),
        room_dir,
        document,
        job,
        font_path=font_path,
    )
    return success_response(
        {"export_id": job["export_id"], "status": job["status"]},
        "Room log export queued",
        status=202,
    )


@bp.route("/api/rooms/<room_id>/log/exports/<export_id>", methods=["GET"])
def get_room_log_export(room_id, export_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _forbidden(room_id, "Not a room member")
    job = get_store().get(room_id, export_id)
    if job is None:
        return error_response("Export not found", 404, "log.export_not_found")
    return success_response(job, "Room log export status loaded successfully")


@bp.route("/api/rooms/<room_id>/log/exports/<export_id>/file", methods=["GET"])
def download_room_log_export(room_id, export_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, error = _load_room(room_id)
    if error:
        return error
    settings = _settings()
    if not _as_bool(settings.get("enabled"), True):
        return _forbidden(room_id, "Room log export is disabled")
    if not _can_access(info):
        return _forbidden(room_id, "Not a room member")
    can_export, _reason = _permission_state(info, settings)
    if not can_export:
        return _forbidden(room_id)

    store = get_store()
    job = store.get(room_id, export_id)
    if job is None:
        return error_response("Export not found", 404, "log.export_not_found")
    if job.get("status") != STATUS_DONE:
        return error_response("Export is not ready", 409, "log.not_ready")
    path = store.pdf_path(room_id, export_id)
    if path is None or not path.is_file():
        return error_response("Export file is missing", 404, "log.export_not_found")

    room_name = _UNSAFE_FILENAME_RE.sub("_", str(info.get("name") or "团录")).strip(" ._") or "团录"
    download_name = f"团录-{room_name}-{time.strftime('%Y%m%d')}.pdf"
    response = send_file(
        str(path),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/rooms/<room_id>/log/exports/<export_id>", methods=["DELETE"])
def delete_room_log_export(room_id, export_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _room_dir, info, error = _load_room(room_id)
    if error:
        return error
    settings = _settings()
    if not _as_bool(settings.get("enabled"), True):
        return _forbidden(room_id, "Room log export is disabled")
    can_export, _reason = _permission_state(info, settings)
    if not can_export:
        return _forbidden(room_id)
    deleted = get_store().delete(room_id, export_id)
    # 幂等：任务已过期或已被清理时同样返回成功，前端无需区分。
    return success_response({"deleted": bool(deleted)}, "Room log export deleted")


__all__ = ["bp"]
