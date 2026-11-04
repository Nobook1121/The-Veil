"""语音配音导演：标记剥离、说话人解析、音色分配与配额/降级。

入口 :func:`prepare_voice` 由 ``routes/chat.py`` 在叙事返回前调用，**永不抛错**：
- 无论语音开关是否开启，都必须先剥离标记，保证正文与历史里不出现 ``【NPC:…】``；
- 语音关闭、引擎不可用、配额耗尽、全员静音时只降级记录，不阻塞叙事。
"""
from __future__ import annotations

import time
from typing import Any

from trpg_server.voice import engine as voice_engine
from trpg_server.voice import events
from trpg_server.voice.config import (
    consume_room_quota,
    is_room_muted,
    load_room_voice,
    load_voice_settings,
    resolve_profile_id,
    resolve_room_voice,
    save_room_voice,
)
from trpg_server.voice.markup import build_npc_roster, parse_narration
from trpg_server.voice.profiles import PROFILE_MAP, normalize_speaker_name


def _npc_entry_index(roster: list[Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for entry in roster:
        for name in (entry.name, *entry.aliases):
            key = normalize_speaker_name(name)
            if key and key not in index:
                index[key] = entry
    return index


def prepare_voice(
    raw_text: str,
    *,
    room_id: str = "",
    room_dir: Any = None,
    house_rules: dict[str, Any] | None = None,
    scenario: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
    message_id: str = "",
    turn: int = 0,
    config_dir: Any = None,
) -> dict[str, Any]:
    """返回 ``{"content": 无标记正文, "voice": 播放载荷或 None}``。"""
    started = time.monotonic()
    try:
        settings = load_voice_settings(config_dir)
        effective = resolve_room_voice(settings, house_rules)
        roster = build_npc_roster(scenario, scene)
        parsed = parse_narration(
            raw_text,
            roster=roster,
            max_segment_chars=int(effective["max_segment_chars"]),
            max_turn_chars=int(effective["max_turn_chars"]),
            max_segments=int(effective["max_segments"]),
        )
    except Exception:  # noqa: BLE001 - 解析失败也要保证原文可用
        return {"content": str(raw_text or ""), "voice": None}

    clean_text = parsed.clean_text
    if not room_id or not room_dir or not effective["enabled"]:
        return {"content": clean_text, "voice": None}
    if not parsed.segments:
        return {"content": clean_text, "voice": None}

    base = {
        "room_id": str(room_id),
        "message_id": str(message_id or ""),
        "turn": int(turn or 0),
        "engine": effective["engine"],
        "segment_count": len(parsed.segments),
        "text_chars": sum(len(str(item.get("text") or "")) for item in parsed.segments),
    }
    latency_ms = (time.monotonic() - started) * 1000

    # 引擎不可用：优先回落到 system（若站点允许），否则仅文字；
    # 两种情况都保留片段与具体降级原因，不阻塞叙事。
    room_voice = load_room_voice(room_dir)
    try:
        engine_health = voice_engine.engine_health(effective)
    except Exception:  # noqa: BLE001
        engine_health = {"engine": "system", "ok": True, "reason": "", "mode": "client"}
    if not engine_health.get("ok"):
        reason = str(engine_health.get("reason") or voice_engine.REASON_ENGINE_UNAVAILABLE)
        allowed_engines = list(effective.get("engine_allowed") or [])
        can_fallback = (
            effective["engine"] == voice_engine.ENGINE_LOCAL
            and "system" in allowed_engines
        )
        fallback = "system" if can_fallback else str(engine_health.get("engine") or effective["engine"])
        events.emit(
            "degraded", **base, degraded=True, reason=reason, latency_ms=latency_ms
        )
        return {
            "content": clean_text,
            "voice": _payload(
                effective, parsed, room_id=str(room_id), message_id=message_id, turn=turn,
                engine=fallback, playable=can_fallback, degraded=True, reason=reason,
                roster=roster, room_voice=room_voice, fallback_from=effective["engine"],
            ),
        }

    cap = int(effective["room_hourly_cap"])
    allowed, usage = consume_room_quota(room_voice, cap)
    if not allowed:
        events.emit(
            "degraded", **base, degraded=True, reason="room_hourly_cap", latency_ms=latency_ms
        )
        return {
            "content": clean_text,
            "voice": _payload(
                effective, parsed, room_id=str(room_id), message_id=message_id, turn=turn,
                engine=effective["engine"], playable=False, degraded=True,
                reason="room_hourly_cap", roster=roster, room_voice=room_voice,
            ),
        }

    voice_room = dict(room_voice)
    voice_room["usage"] = usage
    save_room_voice(room_dir, voice_room)

    payload = _payload(
        effective, parsed, room_id=str(room_id), message_id=message_id, turn=turn,
        engine=effective["engine"], playable=True, degraded=False, reason="",
        roster=roster, room_voice=room_voice,
    )
    latency_ms = (time.monotonic() - started) * 1000
    if not payload["segments"]:
        payload["playable"] = False
        payload["reason"] = "all_muted"
        events.emit(
            "degraded", **base, degraded=True, reason="all_muted", latency_ms=latency_ms
        )
        return {"content": clean_text, "voice": payload}

    events.emit(
        "ready",
        **base,
        degraded=parsed.truncated,
        reason="truncated" if parsed.truncated else "",
        latency_ms=latency_ms,
    )
    for segment in payload["segments"][:24]:
        events.emit(
            "segment",
            room_id=str(room_id),
            message_id=str(message_id or ""),
            turn=int(turn or 0),
            speaker=segment["display_name"],
            speaker_source=segment["source"],
            profile_id=segment["profile_id"],
            engine=effective["engine"],
            text_chars=len(segment["text"]),
            segment_count=len(payload["segments"]),
            latency_ms=latency_ms,
            degraded=parsed.truncated,
            reason="truncated" if parsed.truncated else "",
        )
    return {"content": clean_text, "voice": payload}


def _payload(
    effective: dict[str, Any],
    parsed: Any,
    *,
    room_id: str,
    message_id: str,
    turn: int,
    engine: str,
    playable: bool,
    degraded: bool,
    reason: str,
    roster: list[Any],
    room_voice: dict[str, Any],
    fallback_from: str = "",
) -> dict[str, Any]:
    entry_index = _npc_entry_index(roster)
    segments: list[dict[str, Any]] = []
    for item in parsed.segments:
        display_name = str(item.get("display_name") or "")
        entry = entry_index.get(normalize_speaker_name(display_name))
        if item.get("kind") == "npc" and (
            is_room_muted(room_voice, display_name)
            or (entry is not None and is_room_muted(room_voice, entry.name))
        ):
            continue
        profile_id = resolve_profile_id(
            str(item.get("speaker_id") or ""),
            display_name,
            room_voice=room_voice,
            npc_entry=entry,
        )
        profile = PROFILE_MAP.get(profile_id) or PROFILE_MAP["narrator"]
        segment = {
            **item,
            "profile_id": profile.id,
            "profile_label": profile.label,
            "profile_i18n_key": f"voice.profile.{profile.id}",
            "pitch": profile.pitch,
            "rate": profile.rate,
            "preferred_voices": list(profile.preferred_voices),
            "engine": engine,
        }
        if engine == voice_engine.ENGINE_LOCAL:
            from trpg_server.voice import local_service

            binding = local_service.speaker_binding(profile.id, effective, room_voice)
            segment.update(
                {
                    "voice_id": binding["voice_id"],
                    "speaker_sid": binding["sid"],
                    "voice_gender": binding["gender"],
                    "voice_source": binding["source"],
                }
            )
        segments.append(segment)
    payload = {
        "room_id": str(room_id or ""),
        "message_id": str(message_id or ""),
        "turn": int(turn or 0),
        "engine": engine,
        "engine_allowed": list(effective.get("engine_allowed") or []),
        "playable": bool(playable) and bool(segments),
        "degraded": bool(degraded),
        "reason": str(reason or ""),
        "fallback_from": str(fallback_from or ""),
        "segments": segments,
        "settings": {
            "volume": int(effective.get("volume") or 70),
            "rate": int(effective.get("rate") or 100),
            "autoplay": bool(effective.get("autoplay")),
            "interrupt_policy": str(effective.get("interrupt_policy") or "interrupt"),
            "ducking": bool(effective.get("ducking")),
            "ducking_level": int(effective.get("ducking_level") or 0),
            "max_segment_chars": int(effective.get("max_segment_chars") or 40),
            "max_turn_chars": int(effective.get("max_turn_chars") or 800),
            "max_segments": int(effective.get("max_segments") or 24),
            "allow_personal": bool(effective.get("allow_personal")),
        },
    }
    if engine == voice_engine.ENGINE_LOCAL:
        payload["audio"] = {
            "endpoint": "/api/voice/synthesize",
            "format": "wav",
            "sample_rate": 24000,
            "model": str(effective.get("local_model") or ""),
        }
    return payload


def log_playback(
    *,
    room_id: str,
    message_id: str = "",
    turn: int = 0,
    speaker: str = "",
    profile_id: str = "",
    engine: str = "system",
    text_chars: int = 0,
    segment_count: int = 0,
    degraded: bool = False,
    reason: str = "",
) -> None:
    """前端播放器回报「开始/结束/失败/被打断」时调用，统一事件字段。"""
    events.emit(
        "playback",
        room_id=room_id,
        message_id=message_id,
        turn=turn,
        speaker=speaker,
        profile_id=profile_id,
        engine=engine,
        text_chars=text_chars,
        segment_count=segment_count,
        degraded=degraded,
        reason=reason,
    )
