"""骰子贴图库 REST 接口（管理员上传，所有登录用户读取）。"""
from __future__ import annotations

import logging

from flask import Blueprint, request, send_file, session

from trpg_server import dice_textures
from trpg_server.responses import error_response, success_response
from trpg_server.routes.rooms import _is_elevated, _require_login

bp = Blueprint("dice_textures", __name__)
logger = logging.getLogger(__name__)


def _admin_error():
    if not _is_elevated():
        return error_response("Administrator permission required", 403, "Forbidden")
    return None


@bp.route("/api/dice-textures", methods=["GET"])
def list_dice_textures():
    login_error = _require_login()
    if login_error:
        return login_error
    include_disabled = request.args.get("include_disabled") == "1" and _is_elevated()
    return success_response(dice_textures.library(include_disabled=include_disabled), "Dice textures loaded successfully")


@bp.route("/api/dice-textures/assets/<asset_id>", methods=["GET"])
def get_dice_texture_asset(asset_id):
    login_error = _require_login()
    if login_error:
        return login_error
    asset = dice_textures.find_asset(asset_id)
    if asset is None:
        return error_response("Texture not found", 404, "Not found")
    if not asset.get("enabled", True) and not _is_elevated():
        return error_response("Texture not found", 404, "Not found")
    try:
        path = dice_textures.asset_path(asset)
    except dice_textures.TextureError as exc:
        return error_response("Texture not found", exc.status, "Not found")
    response = send_file(path, mimetype=asset.get("content_type") or "application/octet-stream", conditional=True)
    response.headers["Cache-Control"] = "private, max-age=86400, immutable"
    return response


@bp.route("/api/dice-textures/assets", methods=["POST"])
def upload_dice_texture():
    login_error = _require_login()
    if login_error:
        return login_error
    admin_error = _admin_error()
    if admin_error:
        return admin_error
    upload = request.files.get("file")
    if upload is None:
        return error_response("Missing file", 400, "Missing file")
    kind = str(request.form.get("kind") or "table")
    name = str(request.form.get("name") or "")
    blob = upload.read()
    try:
        asset = dice_textures.create_asset(blob, name=name, kind=kind, uploaded_by=session.get("user_id"))
    except dice_textures.TextureError as exc:
        logger.info("Rejected dice texture upload: %s", exc.reason)
        return error_response(exc.reason, exc.status, exc.reason)
    return success_response(asset, "Texture uploaded successfully", 201)


@bp.route("/api/dice-textures/assets/<asset_id>", methods=["DELETE"])
def delete_dice_texture(asset_id):
    login_error = _require_login()
    if login_error:
        return login_error
    admin_error = _admin_error()
    if admin_error:
        return admin_error
    try:
        dice_textures.delete_asset(asset_id)
    except dice_textures.TextureError as exc:
        return error_response(exc.reason, exc.status, exc.reason)
    return success_response({"id": asset_id}, "Texture deleted successfully")


@bp.route("/api/dice-textures/table-skins", methods=["POST"])
def upsert_table_skin():
    login_error = _require_login()
    if login_error:
        return login_error
    admin_error = _admin_error()
    if admin_error:
        return admin_error
    payload = request.get_json(silent=True) or {}
    try:
        skin = dice_textures.upsert_table_skin(payload)
    except dice_textures.TextureError as exc:
        return error_response(exc.reason, exc.status, exc.reason)
    return success_response(skin, "Table skin saved successfully")


@bp.route("/api/dice-textures/dice-skins", methods=["POST"])
def upsert_dice_skin():
    login_error = _require_login()
    if login_error:
        return login_error
    admin_error = _admin_error()
    if admin_error:
        return admin_error
    payload = request.get_json(silent=True) or {}
    try:
        skin = dice_textures.upsert_dice_skin(payload)
    except dice_textures.TextureError as exc:
        return error_response(exc.reason, exc.status, exc.reason)
    return success_response(skin, "Dice skin saved successfully")


@bp.route("/api/dice-textures/skins/<skin_id>", methods=["DELETE"])
def delete_dice_skin(skin_id):
    login_error = _require_login()
    if login_error:
        return login_error
    admin_error = _admin_error()
    if admin_error:
        return admin_error
    try:
        dice_textures.delete_skin(skin_id)
    except dice_textures.TextureError as exc:
        return error_response(exc.reason, exc.status, exc.reason)
    return success_response({"id": skin_id}, "Skin deleted successfully")
