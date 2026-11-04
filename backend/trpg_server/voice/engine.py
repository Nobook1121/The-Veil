"""语音引擎适配层。

- ``system``：服务端不合成音频，只把片段与音色参数下发给浏览器，由 Web Speech 发声；
- ``local``：调用本机 ``127.0.0.1`` 子服务（``voice.local_service``）合成真实 WAV，
  浏览器按段拉取音频播放，失败时按 ``local → system → 仅文字`` 降级；
- ``cloud``：本阶段仍只保留适配位，明确返回 ``engine_unavailable``，不发起外部请求。
"""
from __future__ import annotations

from typing import Any

from trpg_server.voice import local_service, local_models

ENGINE_SYSTEM = "system"
ENGINE_LOCAL = "local"
ENGINE_CLOUD = "cloud"
REASON_ENGINE_UNAVAILABLE = "engine_unavailable"
REASON_ENGINE_NOT_ALLOWED = "engine_not_allowed"


class VoiceSynthesisError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def synthesize(
    segment: dict[str, Any],
    profile: dict[str, Any],
    *,
    engine: str,
    engine_allowed: list[str] | tuple[str, ...],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一合成接口：成功返回播放描述，失败抛出带 reason 的异常。

    ``segment`` / ``profile`` 只读；``mode=client`` 表示由前端发声，
    ``mode=server`` 表示前端按片段向 ``/api/voice/synthesize`` 拉取真实音频。
    """
    selected = str(engine or ENGINE_SYSTEM).strip().lower()
    allowed = [str(item).strip().lower() for item in (engine_allowed or ())]
    if selected != ENGINE_SYSTEM and selected not in allowed:
        raise VoiceSynthesisError(REASON_ENGINE_NOT_ALLOWED)
    if selected == ENGINE_LOCAL:
        cfg = settings if isinstance(settings, dict) else {}
        if not cfg.get("local_enabled"):
            raise VoiceSynthesisError(REASON_ENGINE_UNAVAILABLE)
        binding = local_service.speaker_binding(str(profile.get("id") or "narrator"), cfg)
        return {
            "engine": ENGINE_LOCAL,
            "mode": "server",
            "engine_ready": True,
            "text": segment.get("text"),
            "profile_id": profile.get("id"),
            "voice_id": binding["voice_id"],
            "speaker_id": binding["sid"],
            "sample_rate": local_models.SAMPLE_RATE,
            "format": "wav",
            "speed": float(profile.get("rate") or 1.0),
        }
    if selected == ENGINE_CLOUD:
        # 云端引擎本阶段未实现：明确失败，由上层按降级记录，不假装可用。
        raise VoiceSynthesisError(REASON_ENGINE_UNAVAILABLE)
    return {
        "engine": ENGINE_SYSTEM,
        "mode": "client",
        "engine_ready": True,
        "text": segment.get("text"),
        "profile_id": profile.get("id"),
        "pitch": profile.get("pitch"),
        "rate": profile.get("rate"),
        "preferred_voices": profile.get("preferred_voices") or [],
    }


def engine_health(settings: dict[str, Any]) -> dict[str, Any]:
    """引擎健康度：system 始终可用；local 探测子服务；cloud 明确未实现。"""
    engine = str(settings.get("engine") or ENGINE_SYSTEM)
    allowed = list(settings.get("engine_allowed") or [])
    if engine == ENGINE_SYSTEM:
        return {"engine": engine, "ok": True, "reason": "", "mode": "client"}
    if engine not in allowed:
        return {"engine": engine, "ok": False, "reason": REASON_ENGINE_NOT_ALLOWED, "mode": "server"}
    if engine == ENGINE_LOCAL:
        return local_service.health(settings)
    return {"engine": engine, "ok": False, "reason": REASON_ENGINE_UNAVAILABLE, "mode": "server"}
