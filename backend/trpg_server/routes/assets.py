import json
from pathlib import Path
import re
from uuid import uuid4

from flask import Blueprint, Response, current_app, request, send_from_directory, session

from trpg_server.ai_platform_config import load_public_platform_config
from trpg_server.responses import error_response, success_response
from trpg_server.security import is_allowed_upload, normalize_filename, safe_join

from trpg_server.settings import (
    AI_PLATFORM_ASSETS_DIR,
    AI_PLATFORM_SECRET_DIR,
    AVATARS_DIR,
    CHARACTERS_DIR,
    CONFIG_DIR,
    RUNTIME_DIR,
    SCENARIO_COVERS_DIR,
    SCENARIOS_DIR,
    THEME_ASSETS_DIR,
    TOOLS_DIR,
    VENDOR_ASSETS_DIR,
)

bp = Blueprint("assets", __name__)

# 不再允许静态直出的设置文件（含站点/网络/穿透设置，避免密钥与内网信息外泄）。
_BLOCKED_CONFIG_FILES = frozenset({"general.toml", "site.json", "network.json", "penetration.json"})

# `/data/characters` 只对外提供角色头像；角色卡 JSON（investigator-*.json 与
# `<id>/character.json`）含完整属性/背景/随身物品，不能匿名直出。
_CHARACTER_DATA_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

# 主页聊天附件上传目录与限制（图片/视频/常用文档）。
CHAT_UPLOAD_DIR = RUNTIME_DIR / "uploads" / "chat"
CHAT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
_CHAT_UPLOAD_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
# 图片魔数：防止把任意内容改个扩展名就伪装成图片上传（后续还会作为静态资源对外提供）。
_IMAGE_SIGNATURES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
}
CHAT_UPLOAD_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp4", "webm", "ogg", "mov", "m4v",
    "pdf", "txt", "md", "markdown", "json", "csv", "zip", "rar", "7z",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf",
}


def _image_content_matches(raw: bytes, suffix: str) -> bool:
    suffix = suffix.lower()
    if suffix == "webp":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    signatures = _IMAGE_SIGNATURES.get(suffix)
    return bool(signatures) and any(raw.startswith(signature) for signature in signatures)


def _chat_upload_dir() -> Path:
    return Path(current_app.config.get("CHAT_UPLOAD_DIR", CHAT_UPLOAD_DIR))


def _get_config_dir():
    return current_app.config.get("CONFIG_DIR", CONFIG_DIR)


def _get_ai_platform_secret_dir():
    return current_app.config.get("AI_PLATFORM_SECRET_DIR", AI_PLATFORM_SECRET_DIR)


def _with_no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# 素材按内容寻址（`assets/<hash16>[_{thumb,poster}].<ext>`），内容变化必然换名，
# 因此可以长期缓存；其余剧本静态文件仍保持 no-cache 以便覆盖更新。
_HASH_ASSET_PATH = re.compile(
    r"(?:^|/)assets/[0-9a-f]{16}(?:_(?:thumb|poster))?\.[A-Za-z0-9]{1,8}$"
)


def _with_immutable_cache(response):
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@bp.route("/assets/avatars/<path:filename>")
def serve_avatar(filename):
    return _with_no_cache(send_from_directory(AVATARS_DIR, filename))


@bp.route("/assets/scenario_covers/<path:filename>")
def serve_scenario_cover(filename):
    return _with_no_cache(send_from_directory(SCENARIO_COVERS_DIR, filename))


@bp.route("/assets/scenarios/<path:filename>")
def serve_scenario_asset(filename):
    response = send_from_directory(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR), filename)
    normalized = filename.replace("\\", "/")
    if _HASH_ASSET_PATH.search(normalized):
        return _with_immutable_cache(response)
    return _with_no_cache(response)


@bp.route("/assets/aiplatform/<path:filename>")
def serve_aiplatform_icon(filename):
    return _with_no_cache(send_from_directory(AI_PLATFORM_ASSETS_DIR, filename))


@bp.route("/assets/vendor/<path:filename>")
def serve_vendor_asset(filename):
    return send_from_directory(VENDOR_ASSETS_DIR, filename)


@bp.route("/assets/theme/<path:filename>")
def serve_theme_asset(filename):
    return send_from_directory(THEME_ASSETS_DIR, filename)


def _normalize_static_path(filename):
    """把静态文件名规范化为相对 POSIX 路径。

    拒绝绝对路径、隐藏文件与 ``.``/``..`` 段，避免 ``send_from_directory``
    在解析路径时绕过按文件名做的黑名单（例如 ``./general.toml``）。
    """

    raw = str(filename or "").replace("\\", "/").strip()
    if not raw or raw.startswith(("/", ".")):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


@bp.route("/config/<path:filename>")
def serve_config(filename):
    # 人类设置文件改经 /api/config/general（JSON，密钥掩码）读取；直接静态暴露
    # general.toml 会把穿透令牌等密钥回传给未登录访客。aiplatform JSON 仍走此处
    # （load_public_platform_config 会剔除 api_key）。
    filename = _normalize_static_path(filename)
    if filename is None or filename.rsplit("/", 1)[-1].lower() in _BLOCKED_CONFIG_FILES:
        return error_response("Not found", 404, "Not found")
    if filename.startswith("aiplatform/") and filename.endswith(".json"):
        config_path = safe_join(_get_config_dir(), filename)
        if config_path.exists():
            secret_path = safe_join(_get_ai_platform_secret_dir(), filename.split("/", 1)[1])
            config = load_public_platform_config(config_path, secret_path)
            response = Response(json.dumps(config, ensure_ascii=False, indent=2) + "\n", mimetype="application/json")
            return _with_no_cache(response)
    return send_from_directory(_get_config_dir(), filename)


@bp.route("/data/characters/<path:filename>")
def serve_character_data(filename):
    # 仅头像图片可匿名访问；角色卡 JSON 需要登录后走 API（归属校验在 characters 路由）。
    if not is_allowed_upload(filename, _CHARACTER_DATA_IMAGE_EXTENSIONS):
        return error_response("Not found", 404, "Not found")
    characters_dir = current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR)
    return _with_no_cache(send_from_directory(characters_dir, filename))


@bp.route("/data/tools/<path:filename>")
def serve_tool_script(filename):
    return _with_no_cache(send_from_directory(TOOLS_DIR, filename))


@bp.route("/api/assets/upload", methods=["POST"])
def upload_chat_asset():
    """接收主页聊天附件，保存到 uploads 目录并返回可访问 URL。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return error_response("Please provide a file", 400, "No file")
    if not is_allowed_upload(uploaded.filename, CHAT_UPLOAD_EXTENSIONS):
        return error_response("Unsupported file type", 400, "Invalid file type")
    raw = uploaded.read(CHAT_UPLOAD_MAX_BYTES + 1)
    if len(raw) > CHAT_UPLOAD_MAX_BYTES:
        return error_response("File is too large", 413, "File too large")
    suffix = Path(uploaded.filename).suffix.lstrip(".").lower()
    if suffix in _CHAT_UPLOAD_IMAGE_EXTENSIONS and not _image_content_matches(raw, suffix):
        return error_response("Invalid image file", 400, "Invalid file content")

    upload_dir = _chat_upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    display_name = normalize_filename(uploaded.filename, "attachment")
    stored_name = f"{uuid4().hex}_{display_name}"
    (upload_dir / stored_name).write_bytes(raw)
    return success_response(
        {
            "url": f"/assets/uploads/{stored_name}",
            "name": display_name,
            "size": len(raw),
            "content_type": uploaded.mimetype or "",
        },
        "File uploaded successfully",
        201,
    )


@bp.route("/assets/uploads/<path:filename>")
def serve_chat_upload(filename):
    return _with_no_cache(send_from_directory(_chat_upload_dir(), filename))
