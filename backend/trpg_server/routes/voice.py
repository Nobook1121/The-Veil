"""语音配音 HTTP 接口：配置读取、管理员写入与播放回报。

权限矩阵：
- GET 配置：任意已登录用户（不返回云端密钥）；
- POST 配置：仅 ADMIN / OWNER（未登录 401，普通玩家 403）；
- POST 播放回报：仅本房间成员，用于补齐 voice_playback 结构化日志。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, current_app, request, session

from trpg_server.config import update_sections
from trpg_server.config.paths import general_config_path, resolve_config_dir
from trpg_server.responses import error_response, success_response
from trpg_server.voice import events, local_cache, local_models, local_service
from trpg_server.voice.config import load_voice_settings, load_room_voice, resolve_room_voice
from trpg_server.voice.director import log_playback
from trpg_server.voice.profiles import PROFILE_MAP, PROFILES

bp = Blueprint("voice", __name__)
logger = logging.getLogger(__name__)

ALLOWED_CONFIG_KEYS = {
    "voice_enabled", "voice_engine", "voice_engine_allowed", "voice_autoplay",
    "voice_volume", "voice_rate", "voice_max_turn_chars", "voice_max_segment_chars",
    "voice_max_segments", "voice_interrupt_policy", "voice_ducking", "voice_ducking_level",
    "voice_room_hourly_cap", "voice_allow_personal", "voice_cloud_base_url", "voice_cloud_model",
    "voice_local_enabled", "voice_local_base_url", "voice_local_model", "voice_local_concurrency",
    "voice_local_timeout", "voice_local_speakers", "voice_cache_enabled", "voice_cache_quota_mb",
    "voice_cache_max_entries", "voice_cache_overflow",
}

_SYNTH_LOCK = threading.Lock()
_SYNTH_WINDOW: dict[int, tuple[int, float]] = {}
_SYNTH_MAX_PER_MINUTE = 60


def _require_login():
    if not session.get("user_id"):
        return error_response("Please login first", 401, "Not logged in")
    return None


def _is_admin() -> bool:
    return str(session.get("role") or "").upper() in {"ADMIN", "OWNER"}


def _config_dir():
    return resolve_config_dir(current_app.config.get("CONFIG_DIR"))


def _rooms_dir() -> Path:
    return Path(current_app.config.get("ROOMS_DIR") or (Path(current_app.root_path).parent / "runtime" / "rooms"))


def _public_settings(settings: dict[str, object], room_id: str = "") -> dict[str, object]:
    view = dict(settings)
    room_dir = _rooms_dir() / room_id if room_id else None
    if room_id and room_dir and room_dir.is_dir():
        room_info_path = room_dir / "info.json"
        try:
            info = json.loads(room_info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
        view["room"] = resolve_room_voice(settings, rules)
        view["npc_profiles"] = load_room_voice(room_dir)["npc_profiles"]
    return view


@bp.route("/api/voice/config", methods=["GET"])
def get_voice_config():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    try:
        settings = load_voice_settings(_config_dir())
        model_status = local_models.installed_status(str(settings.get("local_model") or local_models.MODEL_ID))
        return success_response(
            data={
                "settings": _public_settings(settings, str(request.args.get("room_id") or "")),
                "profiles": [profile.to_dict() for profile in PROFILES],
                "engine": {
                    "system": True,
                    "local": bool(settings.get("local_enabled") and model_status["installed"]),
                    "cloud": False,
                },
                "local": {
                    "installed": model_status["installed"],
                    "model": model_status["model"],
                    "label": model_status["label"],
                    "license": model_status["license"],
                    "path": model_status["path"],
                    "bytes": model_status["bytes"],
                    "install": model_status["install"],
                },
                "event_log": str(events.path()),
            }
        )
    except Exception:
        logger.exception("voice config load failed")
        return error_response("Load failed", 500)


@bp.route("/api/voice/config", methods=["POST"])
def save_voice_config():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    if not _is_admin():
        return error_response("Permission denied", 403, "Permission denied")
    payload = request.get_json(silent=True) or {}
    values = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    if not isinstance(values, dict):
        return error_response("Invalid config data", 400)
    changes = {key: value for key, value in values.items() if key in ALLOWED_CONFIG_KEYS}
    if not changes:
        return error_response("No voice settings supplied", 400)
    try:
        update_sections(general_config_path(_config_dir()), {"immersive": changes})
        return success_response(message="Voice config saved successfully")
    except Exception:
        logger.exception("voice config save failed")
        return error_response("Save failed", 500)


def _room_member(room_id: str, user_id: int) -> bool:
    room_dir = _rooms_dir() / room_id
    try:
        info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    members = info.get("members") if isinstance(info.get("members"), list) else []
    for member in members:
        if not isinstance(member, dict):
            continue
        if int(member.get("user_id") or 0) == int(user_id):
            return True
    return False


@bp.route("/api/voice/playback", methods=["POST"])
def report_playback():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        return error_response("room_id is required", 400)
    try:
        user_id = int(session.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not _room_member(room_id, user_id):
        return error_response("Permission denied", 403, "Permission denied")
    try:
        log_playback(
            room_id=room_id,
            message_id=str(payload.get("message_id") or ""),
            turn=int(payload.get("turn") or 0),
            speaker=str(payload.get("speaker") or "")[:80],
            profile_id=str(payload.get("profile_id") or "")[:40],
            engine=str(payload.get("engine") or "system")[:20],
            text_chars=int(payload.get("text_chars") or 0),
            segment_count=int(payload.get("segment_count") or 0),
            degraded=bool(payload.get("degraded")),
            reason=str(payload.get("reason") or "")[:80],
        )
    except Exception:
        logger.exception("voice playback report failed")
    return success_response(message="Recorded")


def _room_rules(room_dir: Path) -> dict:
    try:
        info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rules = info.get("house_rules") if isinstance(info, dict) else {}
    return rules if isinstance(rules, dict) else {}


def _effective_for_room(room_id: str) -> tuple[dict, Path]:
    room_dir = _rooms_dir() / room_id
    settings = load_voice_settings(_config_dir())
    return resolve_room_voice(settings, _room_rules(room_dir)), room_dir


def _allow_synth(user_id: int) -> bool:
    now = time.time()
    with _SYNTH_LOCK:
        count, window = _SYNTH_WINDOW.get(user_id, (0, now))
        if now - window > 60:
            count, window = 0, now
        count += 1
        _SYNTH_WINDOW[user_id] = (count, window)
    return count <= _SYNTH_MAX_PER_MINUTE


@bp.route("/api/voice/synthesize", methods=["POST"])
def synthesize_segment():
    """按片段合成真实音频：普通玩家只读，仅限本房间成员；结果写入房间缓存。"""
    login_error = _require_login()
    if login_error is not None:
        return login_error
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        return error_response("room_id is required", 400)
    try:
        user_id = int(session.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not _room_member(room_id, user_id):
        return error_response("Permission denied", 403, "Permission denied")
    if not _allow_synth(user_id):
        return error_response("Too many requests", 429, "rate_limited")
    text = str(payload.get("text") or "").strip()
    if not text:
        return error_response("text is required", 400)
    if len(text) > 240:
        return error_response("text too long", 413, "text_too_long")
    effective, room_dir = _effective_for_room(room_id)
    if not effective.get("enabled") or not effective.get("local_enabled") or effective.get("engine") != "local":
        return error_response("Local engine is not active", 409, "local_not_active")
    profile_id = str(payload.get("profile_id") or "narrator")
    profile = PROFILE_MAP.get(profile_id) or PROFILE_MAP["narrator"]
    room_voice = load_room_voice(room_dir)
    binding = local_service.speaker_binding(profile.id, effective, room_voice)
    sid = binding["sid"]
    raw_sid = payload.get("sid")
    if raw_sid not in (None, ""):
        try:
            candidate = int(raw_sid)
        except (TypeError, ValueError):
            candidate = -1
        if 0 <= candidate <= local_models.SPEAKER_COUNT - 1:
            sid = candidate
    try:
        speed = float(payload.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed * float(profile.rate) * int(effective.get("rate") or 100) / 100))
    result = local_service.synthesize(
        room_dir,
        text=text,
        voice_id=sid,
        speed=speed,
        settings=effective,
        room_id=room_id,
        message_id=str(payload.get("message_id") or ""),
        turn=int(payload.get("turn") or 0),
        profile_id=profile.id,
        speaker=str(payload.get("speaker") or ""),
    )
    if not result.get("ok"):
        return error_response(
            "Synthesis failed", 503, str(result.get("reason") or local_service.REASON_LOCAL_ERROR)
        )
    response = Response(result["audio"], mimetype="audio/wav")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Voice-Cache"] = "hit" if result.get("cache_hit") else "miss"
    response.headers["X-Voice-Id"] = str(sid)
    response.headers["X-Voice-Profile"] = profile.id
    response.headers["X-Voice-Model"] = str(effective.get("local_model") or "")
    response.headers["X-Audio-Ms"] = str(round(float(result.get("audio_ms") or 0), 1))
    response.headers["X-Rtf"] = str(round(float(result.get("rtf") or 0), 3))
    response.headers["X-Latency-Ms"] = str(round(float(result.get("latency_ms") or 0), 1))
    return response


@bp.route("/api/voice/local/status", methods=["GET"])
def local_status():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    try:
        room_id = str(request.args.get("room_id") or "").strip()
        effective = load_voice_settings(_config_dir())
        if room_id:
            effective, _ = _effective_for_room(room_id)
        data = local_service.service_status(effective)
        room_voice = load_room_voice(_rooms_dir() / room_id) if room_id else {"local_speakers": {}}
        data["bindings"] = {
            profile.id: local_service.speaker_binding(profile.id, effective, room_voice)
            for profile in PROFILES
        }
        data["room_id"] = room_id
        return success_response(data=data)
    except Exception:
        logger.exception("voice local status failed")
        return error_response("Load failed", 500)


@bp.route("/api/voice/local/install", methods=["POST"])
def local_install():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    if not _is_admin():
        return error_response("Permission denied", 403, "Permission denied")
    payload = request.get_json(silent=True) or {}
    ok, message = local_models.install_in_background(force=bool(payload.get("force")))
    events.emit("model_install", engine="local", model=local_models.MODEL_ID, degraded=not ok, reason=message)
    if not ok:
        return error_response(message, 409, "install_running")
    return success_response(message=message, data={"install": dict(local_models._INSTALL_STATE)})


@bp.route("/api/voice/local/remove", methods=["POST"])
def local_remove():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    if not _is_admin():
        return error_response("Permission denied", 403, "Permission denied")
    ok, message = local_models.remove()
    local_service.stop_service()
    events.emit("model_install", engine="local", model=local_models.MODEL_ID, degraded=not ok, reason=message)
    if not ok:
        return error_response(message, 409, "remove_failed")
    return success_response(message=message)


@bp.route("/api/voice/cache/stats", methods=["GET"])
def cache_stats():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    room_id = str(request.args.get("room_id") or "").strip()
    if not room_id or not _room_member(room_id, int(session.get("user_id") or 0)):
        return error_response("Permission denied", 403, "Permission denied")
    effective, room_dir = _effective_for_room(room_id)
    data = local_cache.stats(room_dir)
    data["limits"] = {
        "quota_mb": int(effective.get("cache_quota_mb") or 0),
        "max_entries": int(effective.get("cache_max_entries") or 0),
        "overflow": str(effective.get("cache_overflow") or "lru"),
        "enabled": bool(effective.get("cache_enabled")),
    }
    return success_response(data=data)


@bp.route("/api/voice/cache/clear", methods=["POST"])
def cache_clear():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id or not _room_member(room_id, int(session.get("user_id") or 0)):
        return error_response("Permission denied", 403, "Permission denied")
    room_dir = _rooms_dir() / room_id
    files, size = local_cache.clear(room_dir)
    events.emit("cache_clear", room_id=room_id, engine="local", reason=f"{files} files")
    return success_response(message="Cache cleared", data={"files": files, "bytes": size})


@bp.route("/api/voice/cache/prune", methods=["POST"])
def cache_prune():
    login_error = _require_login()
    if login_error is not None:
        return login_error
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id or not _room_member(room_id, int(session.get("user_id") or 0)):
        return error_response("Permission denied", 403, "Permission denied")
    effective, room_dir = _effective_for_room(room_id)
    removed, freed = local_cache.prune(room_dir, effective)
    events.emit("cache_evict", room_id=room_id, engine="local", reason=f"{removed} files")
    return success_response(message="Cache pruned", data={"removed": removed, "bytes": freed})
