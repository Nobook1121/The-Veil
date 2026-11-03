"""沉浸系统的 REST 接口：配置读取、生图平台管理、手动配乐/配图与管理员密钥。

权限遵循项目既有分层：
- 站点级开关/预算/生图平台/导演配置：权限节点 ``settings.general``（管理员）；
- 房间内手动配乐/配图：房主或房间管理员（复用 ``routes.rooms._can_manage``）；
- 普通成员只读状态与图片，不触发任何高成本调用。

「生图模型平台表」（``immersive.image_platforms``）是外部生图的唯一真相：
这里只读写平台条目，旧单条配置仅做一次性迁移，不再提供第二套接口。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote, urlparse

import requests
from flask import Blueprint, Response, current_app, request, send_file, session

from trpg_server.immersive import audit, director, image_platforms
from trpg_server.immersive.config import (
    DIRECTOR_FALLBACKS,
    INTENSITIES,
    PROFILES,
    load_immersive_settings,
    public_settings,
    resolve_room_switches,
)
from trpg_server.immersive.illustration import (
    MAX_IMAGE_BYTES,
    PUBLIC_IMAGE_PREFIX,
    history_for_room,
    resolve_image_path,
    usage_snapshot,
)
from trpg_server.immersive.service import (
    generate_illustration_now,
    immersive_status,
    set_music_directive,
)
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response, success_response
from trpg_server.routes.rooms import _can_access, _can_manage, _find_room
from trpg_server.security import require_permission_node

bp = Blueprint("immersive", __name__)
logger = logging.getLogger(__name__)


def _config_dir():
    return current_app.config.get("CONFIG_DIR")


def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _settings():
    return load_immersive_settings(_config_dir())


def _actor() -> dict[str, object]:
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "role": session.get("role"),
    }


def _site_manage_allowed() -> bool:
    """当前会话是否具备站点设置权限（与 ``require_permission_node`` 同一判定）。"""
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(_config_dir())
    return is_role_allowed(session.get("role", "USER"), "settings.general", config_path)


def _migrate_quietly() -> None:
    try:
        image_platforms.migrate_legacy(_config_dir(), actor=_actor())
    except Exception:  # noqa: BLE001 - 迁移失败不能阻塞配置读取
        logger.exception("immersive image platform legacy migration failed")


def _room_or_error(room_id):
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return None, None, error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return None, None, error_response("Permission denied", 403, "Permission denied")
    return room_dir, info, None


def _platform_payload(settings: dict) -> dict[str, object]:
    ttl = int(settings.get("music_director_cache_ttl") or 0)
    return {
        "platforms": image_platforms.list_platforms(_config_dir()),
        "presets": image_platforms.presets(),
        "protocols": [
            {"value": key, "label": image_platforms.PROTOCOL_LABELS.get(key, key)}
            for key in image_platforms.PROTOCOLS
        ],
        "default_platform_id": image_platforms.default_platform_id(_config_dir()),
        "director": {
            "usage": director.usage_snapshot(),
            "cache": director.cache_stats(ttl=ttl),
            "fallbacks": list(DIRECTOR_FALLBACKS),
        },
    }


@bp.route("/api/immersive/config", methods=["GET"])
def get_immersive_config():
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    room_id = str(request.args.get("room_id") or "").strip()
    payload = {"config": public_settings(settings), "profiles": list(PROFILES), "intensities": list(INTENSITIES)}
    if _site_manage_allowed():
        _migrate_quietly()
        payload.update(_platform_payload(settings))
    if room_id:
        room_dir, info = _find_room(room_id)
        if room_dir:
            rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
            payload["switches"] = resolve_room_switches(settings, rules)
            payload["room_id"] = info.get("id") or room_id
    return success_response(payload, "Immersive config loaded")


# ---------------------------------------------------------------- 生图平台表

@bp.route("/api/immersive/platforms", methods=["GET"])
@require_permission_node("settings.general")
def list_image_platforms():
    settings = _settings()
    _migrate_quietly()
    return success_response(_platform_payload(settings), "Image platforms loaded")


@bp.route("/api/immersive/platforms", methods=["POST"])
@require_permission_node("settings.general")
def save_image_platform():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return error_response("platform payload is required", 400, "Missing payload")
    try:
        platform = image_platforms.save_platform(
            _config_dir(),
            payload,
            actor=_actor(),
        )
    except ValueError as exc:
        return error_response(str(exc), 400, "Invalid platform")
    return success_response(
        {"platform": platform, "platforms": image_platforms.list_platforms(_config_dir())},
        "Image platform saved",
    )


@bp.route("/api/immersive/platforms/<platform_id>/default", methods=["POST"])
@require_permission_node("settings.general")
def set_image_platform_default(platform_id):
    if not image_platforms.set_default_platform(_config_dir(), platform_id, actor=_actor()):
        return error_response("Platform not found", 404, "Platform not found")
    return success_response(
        {"default_platform_id": platform_id, "platforms": image_platforms.list_platforms(_config_dir())},
        "Default image platform updated",
    )


@bp.route("/api/immersive/platforms/<platform_id>/secret", methods=["POST"])
@require_permission_node("settings.general")
def set_image_platform_secret(platform_id):
    payload = request.get_json(silent=True) or {}
    if "api_key" not in payload and not payload.get("clear"):
        return error_response("api_key is required", 400, "Missing api_key")
    api_key = None if payload.get("clear") else str(payload.get("api_key") or "")
    platform = image_platforms.set_platform_secret(
        _config_dir(), platform_id, api_key, actor=_actor()
    )
    if platform is None:
        return error_response("Platform not found", 404, "Platform not found")
    return success_response(
        {"platform": platform, "platforms": image_platforms.list_platforms(_config_dir())},
        "Image platform key updated",
    )


@bp.route("/api/immersive/platforms/<platform_id>/test", methods=["POST"])
@require_permission_node("settings.general")
def test_image_platform(platform_id):
    payload = request.get_json(silent=True) or {}
    allow_generation = bool(payload.get("allow_generation", True))
    result = image_platforms.probe_platform(
        _config_dir(), platform_id, allow_generation=allow_generation
    )
    audit.record(
        "platform.test",
        actor=_actor(),
        target=f"imageplatform:{platform_id}",
        result="ok" if result.get("ok") else "failed",
        detail={
            "method": result.get("method"),
            "elapsed_ms": result.get("elapsed_ms"),
            "models": result.get("models") or [],
        },
        error=str(result.get("error") or "")[:160],
    )
    return success_response(result, "Image platform tested")


@bp.route("/api/immersive/platforms/<platform_id>", methods=["DELETE"])
@require_permission_node("settings.general")
def delete_image_platform(platform_id):
    if not image_platforms.delete_platform(_config_dir(), platform_id, actor=_actor()):
        return error_response("Platform not found", 404, "Platform not found")
    return success_response(
        {"platforms": image_platforms.list_platforms(_config_dir())},
        "Image platform deleted",
    )


# ---------------------------------------------------------------- 兼容入口

@bp.route("/api/immersive/settings/secret", methods=["POST"])
@require_permission_node("settings.general")
def set_immersive_secret():
    """兼容旧前端：把密钥写到当前默认生图平台，不再产生第二份配置。"""
    payload = request.get_json(silent=True) or {}
    key = payload.get("api_key")
    if key is None:
        return error_response("api_key is required", 400, "Missing api_key")
    platform = image_platforms.resolve_platform(_config_dir(), _settings())
    if platform is None:
        return error_response(
            "请先在「生图模型平台配置」中创建并启用一条平台",
            400,
            "No image platform configured",
        )
    updated = image_platforms.set_platform_secret(
        _config_dir(), str(platform.get("id") or ""), str(key), actor=_actor()
    )
    configured = bool(str(key).strip())
    return success_response(
        {"configured": configured, "platform_id": (updated or {}).get("id")},
        "Image API key saved to default platform",
    )


@bp.route("/api/immersive/test", methods=["POST"])
@require_permission_node("settings.general")
def test_immersive_provider():
    """兼容旧前端：按当前默认平台做一次探测；未配置时给出明确原因。"""
    settings = _settings()
    if settings.get("illustration_provider") == "procedural":
        return success_response(
            {"provider": "procedural", "ready": True, "message": "程序化 SVG 始终可用，无需外部服务"},
            "Procedural provider ready",
        )
    platform = image_platforms.resolve_platform(_config_dir(), settings)
    if platform is None:
        return success_response(
            {
                "provider": settings.get("illustration_provider"),
                "ready": False,
                "problems": ["未配置可用的生图平台（请到「生图模型平台配置」创建并设为默认）"],
            },
            "Provider readiness checked",
        )
    result = image_platforms.probe_platform(_config_dir(), str(platform.get("id") or ""))
    return success_response(result, "Provider tested")


# ---------------------------------------------------------------- 导演与用量

@bp.route("/api/immersive/director", methods=["GET"])
@require_permission_node("settings.general")
def get_immersive_director():
    settings = _settings()
    return success_response(
        {
            "usage": director.usage_snapshot(),
            "cache": director.cache_stats(ttl=int(settings.get("music_director_cache_ttl") or 0)),
            "fallbacks": list(DIRECTOR_FALLBACKS),
            "audit": audit.read_recent(12),
        },
        "Music director status loaded",
    )


@bp.route("/api/immersive/director/cache/clear", methods=["POST"])
@require_permission_node("settings.general")
def clear_immersive_director_cache():
    cleared = director.clear_cache()
    audit.record(
        "director.cache_clear",
        actor=_actor(),
        target="immersive:director_cache",
        detail={"cleared": cleared},
    )
    return success_response({"cleared": cleared}, "Director cache cleared")


@bp.route("/api/immersive/usage", methods=["GET"])
@require_permission_node("settings.general")
def get_immersive_usage():
    settings = _settings()
    usage = usage_snapshot()
    return success_response(
        {
            "usage": usage,
            "daily_budget": settings.get("illustration_daily_budget"),
            "room_hourly_cap": settings.get("illustration_room_hourly_cap"),
            "director_usage": director.usage_snapshot(),
            "director_daily_budget": settings.get("music_director_daily_budget"),
        },
        "Immersive usage loaded",
    )


# ---------------------------------------------------------------- 房间侧

@bp.route("/api/immersive/rooms/<room_id>/state", methods=["GET"])
def get_room_immersive_state(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    settings = _settings()
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    state = immersive_status(
        room_id=str(info.get("id") or room_id),
        room_dir=room_dir,
        settings=settings,
        house_rules=rules,
        scenario_id=str(info.get("scenario_id") or ""),
    )
    state["config"] = public_settings(settings)
    # 前端据此显示/隐藏「手动配乐」「立即配图」等高成本入口（仅房主/房间管理员）。
    state["can_manage"] = bool(_can_manage(info))
    return success_response(state, "Room immersive state loaded")


@bp.route("/api/immersive/rooms/<room_id>/music", methods=["POST"])
def post_room_music(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _room_or_error(room_id)
    if error:
        return error
    settings = _settings()
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    switches = resolve_room_switches(settings, rules)
    if not switches["music"]:
        return error_response("Music is disabled for this room", 403, "Music disabled")
    payload = request.get_json(silent=True) or {}
    profile = str(payload.get("profile") or "").strip().lower() or None
    intensity = str(payload.get("intensity") or "").strip().lower() or None
    if profile is not None and profile not in PROFILES:
        return error_response(f"profile must be one of {list(PROFILES)}", 400, "Invalid profile")
    if intensity is not None and intensity not in INTENSITIES:
        return error_response(f"intensity must be one of {list(INTENSITIES)}", 400, "Invalid intensity")
    plan = set_music_directive(
        room_id=str(info.get("id") or room_id),
        room_dir=room_dir,
        settings=settings,
        profile=profile,
        intensity=intensity,
        scene=str(payload.get("scene") or "手动指令")[:48],
        scenario_id=str(info.get("scenario_id") or ""),
    )
    logger.info("immersive music manual room=%s user_id=%s profile=%s intensity=%s", room_id, session.get("user_id"), plan.get("profile"), plan.get("intensity"))
    return success_response(plan, "Music directive applied")


@bp.route("/api/immersive/rooms/<room_id>/illustration", methods=["POST"])
def post_room_illustration(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info, error = _room_or_error(room_id)
    if error:
        return error
    settings = _settings()
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    switches = resolve_room_switches(settings, rules)
    if not switches["illustration"]:
        return error_response("Illustration is disabled for this room", 403, "Illustration disabled")
    payload = request.get_json(silent=True) or {}
    profile = str(payload.get("profile") or settings.get("music_default_profile") or "daily")
    intensity = str(payload.get("intensity") or settings.get("music_default_intensity") or "medium")
    if profile not in PROFILES:
        profile = "daily"
    if intensity not in INTENSITIES:
        intensity = "medium"
    scene = str(payload.get("scene") or "手动场景")[:120]
    turn_raw = payload.get("turn")
    turn_index = turn_raw if isinstance(turn_raw, int) else 0
    result = generate_illustration_now(
        room_id=str(info.get("id") or room_id),
        room_dir=room_dir,
        settings=settings,
        scene=scene,
        profile=profile,
        intensity=intensity,
        turn_index=turn_index,
        force=True,
        config_dir=_config_dir(),
        scenario_id=str(info.get("scenario_id") or ""),
        house_rules=info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {},
    )
    logger.info(
        "immersive illustration manual room=%s user_id=%s provider=%s degraded=%s",
        room_id, session.get("user_id"), result.get("provider"), result.get("degraded"),
    )
    return success_response(result, "Illustration generated")


@bp.route("/api/immersive/rooms/<room_id>/illustration/download", methods=["GET"])
def download_room_illustration(room_id):
    """房间成员下载配图：本地缓存直接下发，外部 URL 经后端代理。

    外部地址只允许下载当前房间历史里出现过的图片，避免把接口变成任意 URL 代理；
    文件大小沿用生图上限，失败返回明确错误而不是空文件。
    """
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return error_response("Permission denied", 403, "Permission denied")

    raw_url = str(request.args.get("url") or "").strip()
    if not raw_url:
        return error_response("Missing image url", 400, "Missing image url")
    scene = str(request.args.get("scene") or "scene")[:80]
    suffix = "png"
    content: bytes | None = None
    mime = "image/png"
    if raw_url.startswith(PUBLIC_IMAGE_PREFIX):
        filename = raw_url[len(PUBLIC_IMAGE_PREFIX):].split("?", 1)[0]
        path = resolve_image_path(filename)
        if path is None:
            return error_response("Image not found", 404, "Image not found")
        suffix = path.suffix.lower().lstrip(".") or "png"
        mime = {
            "svg": "image/svg+xml",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        return send_file(path, mimetype=mime, as_attachment=True, download_name=_download_name(info, room_id, scene, suffix))

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        return error_response("Unsupported image url", 400, "Unsupported image url")
    known = {str(item.get("url") or "") for item in history_for_room(room_dir, 50)}
    if raw_url not in known:
        return error_response("Image is not part of this room history", 403, "Image not in history")
    try:
        response = requests.get(raw_url, timeout=(10, 40), stream=True)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in {"image/png", "image/jpeg", "image/webp", "image/svg+xml", "image/gif"}:
            return error_response("Remote resource is not an image", 415, "Not an image")
        mime = content_type
        suffix = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/svg+xml": "svg",
            "image/gif": "gif",
        }.get(content_type, "png")
        buffer = bytearray()
        for chunk in response.iter_content(64 * 1024):
            buffer.extend(chunk)
            if len(buffer) > MAX_IMAGE_BYTES:
                return error_response("Image exceeds download limit", 413, "Image too large")
        content = bytes(buffer)
    except requests.RequestException as exc:
        logger.warning("immersive illustration download proxy failed room=%s: %s", room_id, exc)
        return error_response("Failed to download remote image", 502, "Remote download failed")
    if not content:
        return error_response("Empty remote image", 502, "Empty image")
    name = _download_name(info, room_id, scene, suffix)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return Response(content, mimetype=mime, headers=headers)


def _download_name(info: dict, room_id: str, scene: str, suffix: str) -> str:
    """下载文件名包含剧本/房间/时间，便于归档；同时清理路径分隔符。"""
    scenario = str(info.get("scenario_title") or info.get("scenario_name") or "未命名剧本")
    room = str(info.get("name") or room_id or "房间")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = f"{scenario}-{room}-{scene or '场景'}-{stamp}"
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", raw).strip("_")[:160]
    return f"{safe or 'immersive'}.{suffix}"


@bp.route("/api/immersive/rooms/<room_id>/history", methods=["GET"])
def get_room_history(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    settings = _settings()
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    state = immersive_status(
        room_id=str(info.get("id") or room_id),
        room_dir=room_dir,
        settings=settings,
        house_rules=rules,
        scenario_id=str(info.get("scenario_id") or ""),
    )
    return success_response(
        {"music": state["music"], "illustration": state["illustration"]},
        "Room immersive history loaded",
    )


@bp.route("/api/immersive/images/<filename>", methods=["GET"])
def get_immersive_image(filename):
    login_error = _require_login()
    if login_error:
        return login_error
    path = resolve_image_path(filename)
    if path is None:
        return error_response("Image not found", 404, "Image not found")
    mimetype = "image/svg+xml" if path.suffix == ".svg" else None
    response = send_file(path, mimetype=mimetype, conditional=True, max_age=86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if path.suffix == ".svg":
        # 程序化 SVG 是本地生成的静态资源；禁止脚本与外部引用，避免被当作可执行文档。
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return response
