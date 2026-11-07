"""语音配音配置：站点默认、房间收紧与房间级映射/用量。

分层遵循项目既有约定：
- 站点键写入 ``general.toml`` 的 ``[immersive]``，前缀 ``voice_``；
- 房间只允许「收紧」：开关只关不开、字符上限只降不升、小时上限只降不升、ducking 只升不降；
- 房间级 NPC 映射、静音名单与小时用量落在房间目录 ``voice.json``，随房间归档保留。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from trpg_server.config import load_settings
from trpg_server.voice.profiles import NPC_PROFILE_IDS, PROFILE_MAP, normalize_speaker_name

VOICE_ENGINES = ("system", "local", "cloud")
INTERRUPT_POLICIES = ("interrupt", "queue")
CACHE_OVERFLOW_POLICIES = ("lru", "reject")
ROOM_VOICE_FILE = "voice.json"

_DEFAULT_ALLOWED = ("system",)


def _as_speaker_map(value: Any) -> dict[str, int]:
    """把站点 JSON 文本或房间 dict 归一为 ``profile_id -> sid``。"""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    from trpg_server.voice.profiles import PROFILE_MAP

    result: dict[str, int] = {}
    for key, item in value.items():
        profile_id = str(key or "").strip()
        if profile_id not in PROFILE_MAP:
            continue
        try:
            sid = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= sid <= 102:
            result[profile_id] = sid
    return result


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return default


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_choice(value: Any, default: str, choices: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _as_list(value: Any, default: tuple[str, ...], choices: tuple[str, ...]) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_items = [str(item or "").strip().lower() for item in value]
    else:
        raw_items = [item.strip().lower() for item in str(value or "").split(",")]
    result: list[str] = []
    for item in raw_items:
        if item in choices and item not in result:
            result.append(item)
    return result or list(default)


def load_voice_settings(config_dir: Path | str | None = None) -> dict[str, Any]:
    """读取站点级语音配置并归一化，保证缺字段时也有稳定默认值。"""
    settings = load_settings(config_dir)
    raw = settings.data.get("immersive", {})
    if not isinstance(raw, dict):
        raw = {}
    volume = _as_int(raw.get("voice_volume"), 70, 0, 100)
    rate = _as_int(raw.get("voice_rate"), 100, 50, 200)
    return {
        "enabled": _as_bool(raw.get("voice_enabled"), False),
        "engine": _as_choice(raw.get("voice_engine"), "system", VOICE_ENGINES),
        "engine_allowed": _as_list(raw.get("voice_engine_allowed"), _DEFAULT_ALLOWED, VOICE_ENGINES),
        "autoplay": _as_bool(raw.get("voice_autoplay"), False),
        "volume": volume,
        "rate": rate,
        "max_turn_chars": _as_int(raw.get("voice_max_turn_chars"), 800, 40, 4000),
        "max_segment_chars": _as_int(raw.get("voice_max_segment_chars"), 40, 10, 500),
        "max_segments": _as_int(raw.get("voice_max_segments"), 24, 1, 200),
        "interrupt_policy": _as_choice(raw.get("voice_interrupt_policy"), "interrupt", INTERRUPT_POLICIES),
        "ducking": _as_bool(raw.get("voice_ducking"), True),
        "ducking_level": _as_int(raw.get("voice_ducking_level"), 35, 0, 90),
        "room_hourly_cap": _as_int(raw.get("voice_room_hourly_cap"), 120, 0, 10000),
        "cloud_base_url": str(raw.get("voice_cloud_base_url") or "").strip()[:500],
        "cloud_model": str(raw.get("voice_cloud_model") or "").strip()[:120],
        # 本地引擎（子进程回环服务 + 磁盘缓存）。
        "local_enabled": _as_bool(raw.get("voice_local_enabled"), False),
        "local_base_url": str(raw.get("voice_local_base_url") or "").strip()[:500],
        "local_model": str(raw.get("voice_local_model") or "kokoro-int8-multi-lang-v1_1").strip()[:120],
        "local_concurrency": _as_int(raw.get("voice_local_concurrency"), 1, 1, 8),
        "local_timeout": _as_int(raw.get("voice_local_timeout"), 30, 5, 180),
        "local_speakers": _as_speaker_map(raw.get("voice_local_speakers")),
        "cache_enabled": _as_bool(raw.get("voice_cache_enabled"), True),
        "cache_quota_mb": _as_int(raw.get("voice_cache_quota_mb"), 64, 0, 1024),
        "cache_max_entries": _as_int(raw.get("voice_cache_max_entries"), 300, 0, 10000),
        "cache_overflow": _as_choice(raw.get("voice_cache_overflow"), "lru", CACHE_OVERFLOW_POLICIES),
        # 个人覆盖权限：默认允许，管理员可关闭后强制跟随站点。
        "allow_personal": _as_bool(raw.get("voice_allow_personal"), True),
    }


def _house_rules(house_rules: dict[str, Any] | None) -> dict[str, Any]:
    return house_rules if isinstance(house_rules, dict) else {}


def _lower_limit(site_value: int, room_value: Any, minimum: int) -> int:
    """房间上限只降不升；0 在站点侧表示不限。"""
    if room_value in (None, ""):
        return site_value
    try:
        candidate = int(room_value)
    except (TypeError, ValueError):
        return site_value
    candidate = max(minimum, candidate)
    if site_value <= 0:
        return candidate
    return min(site_value, candidate)


def resolve_room_voice(
    settings: dict[str, Any], house_rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    """合并站点默认与房间房规，返回房间内真正生效的语音参数。"""
    rules = _house_rules(house_rules)
    room_enabled = rules.get("voice_enabled")
    enabled = bool(settings.get("enabled")) and (room_enabled is not False)
    return {
        "enabled": enabled,
        "engine": str(settings.get("engine") or "system"),
        "engine_allowed": list(settings.get("engine_allowed") or _DEFAULT_ALLOWED),
        "autoplay": bool(settings.get("autoplay")),
        "volume": int(settings.get("volume") or 70),
        "rate": int(settings.get("rate") or 100),
        "max_turn_chars": _lower_limit(int(settings.get("max_turn_chars") or 800), rules.get("voice_max_turn_chars"), 40),
        "max_segment_chars": _lower_limit(int(settings.get("max_segment_chars") or 40), rules.get("voice_max_segment_chars"), 10),
        "max_segments": _lower_limit(int(settings.get("max_segments") or 24), rules.get("voice_max_segments"), 1),
        "interrupt_policy": str(settings.get("interrupt_policy") or "interrupt"),
        "ducking": bool(settings.get("ducking")),
        "ducking_level": max(
            int(settings.get("ducking_level") or 0),
            _as_int(rules.get("voice_ducking_level"), 0, 0, 90) if rules.get("voice_ducking_level") not in (None, "") else 0,
        ),
        "room_hourly_cap": _lower_limit(int(settings.get("room_hourly_cap") or 0), rules.get("voice_room_hourly_cap"), 0),
        "allow_personal": bool(settings.get("allow_personal")),
        "engine_available": str(settings.get("engine") or "system") == "system",
        # 本地引擎：房间只能收紧并发/超时/缓存额度，不能开启站点未启用的引擎。
        "local_enabled": bool(settings.get("local_enabled"))
        and rules.get("voice_local_enabled") is not False,
        "local_base_url": str(settings.get("local_base_url") or ""),
        "local_model": str(settings.get("local_model") or "kokoro-int8-multi-lang-v1_1"),
        "local_concurrency": _lower_limit(
            int(settings.get("local_concurrency") or 1), rules.get("voice_local_concurrency"), 1
        ),
        "local_timeout": _lower_limit(
            int(settings.get("local_timeout") or 30), rules.get("voice_local_timeout"), 5
        ),
        "local_speakers": dict(settings.get("local_speakers") or {}),
        "room_local_speakers": dict(_as_speaker_map(rules.get("local_speakers"))),
        "cache_enabled": bool(settings.get("cache_enabled")),
        "cache_quota_mb": _lower_limit(
            int(settings.get("cache_quota_mb") or 0), rules.get("voice_cache_quota_mb"), 0
        ),
        "cache_max_entries": _lower_limit(
            int(settings.get("cache_max_entries") or 0), rules.get("voice_cache_max_entries"), 0
        ),
        "cache_overflow": str(settings.get("cache_overflow") or "lru"),
    }


def load_room_voice(room_dir: Path | str | None) -> dict[str, Any]:
    """读取房间语音文件；损坏或缺失时返回空结构，不影响叙事。"""
    if not room_dir:
        return {"npc_profiles": {}, "muted": [], "usage": {}}
    path = Path(room_dir) / ROOM_VOICE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    profiles = data.get("npc_profiles") if isinstance(data.get("npc_profiles"), dict) else {}
    muted = data.get("muted") if isinstance(data.get("muted"), list) else []
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "npc_profiles": {str(key): str(value) for key, value in profiles.items() if value in PROFILE_MAP},
        "muted": [str(item) for item in muted if str(item).strip()],
        "usage": usage,
        "local_speakers": _as_speaker_map(data.get("local_speakers")),
    }


def save_room_voice(room_dir: Path | str | None, data: dict[str, Any]) -> bool:
    if not room_dir:
        return False
    path = Path(room_dir) / ROOM_VOICE_FILE
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def current_hour() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H")


def room_usage_snapshot(room_voice: dict[str, Any]) -> dict[str, Any]:
    usage = room_voice.get("usage") if isinstance(room_voice.get("usage"), dict) else {}
    hour = str(usage.get("hour") or "")
    count = _as_int(usage.get("count"), 0, 0, 10_000_000)
    return {"hour": hour, "count": count}


def consume_room_quota(room_voice: dict[str, Any], cap: int) -> tuple[bool, dict[str, Any]]:
    """为一个叙事回合占用一次播放配额；``cap=0`` 表示不限。"""
    usage = room_usage_snapshot(room_voice)
    hour = current_hour()
    if usage["hour"] != hour:
        usage = {"hour": hour, "count": 0}
    if cap > 0 and usage["count"] >= cap:
        return False, usage
    usage["count"] += 1
    room_voice["usage"] = usage
    return True, usage


def resolve_profile_id(
    speaker_id: str,
    display_name: str,
    *,
    room_voice: dict[str, Any],
    npc_entry: Any = None,
) -> str:
    """解析优先级：房间显式映射 > 剧本自动分配 > 人设兜底 > 旁白。"""
    from trpg_server.voice.profiles import auto_profile_for

    if speaker_id == "narrator" or not display_name:
        return "narrator"
    mappings = room_voice.get("npc_profiles") if isinstance(room_voice.get("npc_profiles"), dict) else {}
    names = {normalize_speaker_name(display_name)}
    if npc_entry is not None:
        names.add(normalize_speaker_name(getattr(npc_entry, "name", "")))
    # 映射键也做同样的归一，避免「老艾」与「艾德温」因写法不同而漏命中。
    for key, value in mappings.items():
        if normalize_speaker_name(key) in names and value in PROFILE_MAP and PROFILE_MAP[value].kind == "npc":
            return str(value)
    profile = auto_profile_for(display_name, getattr(npc_entry, "data", None))
    return profile.id if profile.id in NPC_PROFILE_IDS else "npc_male_youth"


def is_room_muted(room_voice: dict[str, Any], display_name: str) -> bool:
    muted = room_voice.get("muted") if isinstance(room_voice.get("muted"), list) else []
    lowered = {str(item).strip().lower() for item in muted}
    return str(display_name or "").strip().lower() in lowered


def resolve_personal_voice(
    settings: dict[str, Any],
    personal: dict[str, Any] | None = None,
    *,
    allow_personal: bool = True,
) -> dict[str, Any]:
    """「站点默认 < 个人覆盖」的纯函数实现，供测试与后续服务端预判复用。"""
    prefs = personal if isinstance(personal, dict) else {}
    effective = {
        "volume": _as_int(settings.get("volume"), 70, 0, 100),
        "rate": _as_int(settings.get("rate"), 100, 50, 200),
        "autoplay": _as_bool(settings.get("autoplay"), False),
        "enabled": True,
        "text_only": _as_bool(prefs.get("voice_text_only"), False),
        "muted_speakers": [str(item) for item in (prefs.get("voice_muted_speakers") or []) if str(item).strip()],
    }
    if allow_personal:
        effective["volume"] = _as_int(prefs.get("voice_volume"), effective["volume"], 0, 100)
        effective["rate"] = _as_int(prefs.get("voice_rate"), effective["rate"], 50, 200)
        effective["autoplay"] = _as_bool(prefs.get("voice_autoplay"), effective["autoplay"])
        if prefs.get("voice_enabled") is False:
            effective["enabled"] = False
    if effective["text_only"]:
        effective["enabled"] = False
    return effective
