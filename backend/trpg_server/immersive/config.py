"""沉浸系统配置读取与房间级覆盖解析。

配置沿用项目的五层合并约定：代码默认 < 模板 < 遗留 JSON < general.toml <
环境变量 < CLI。这里只做「读取后归一化」，写入仍交给 ``config.writer``（tomlkit
保留注释）。生图密钥走管理员专用接口，不经过通用配置 POST。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from trpg_server.config import load_settings, update_sections
from trpg_server.config.paths import general_config_path

PROFILES = ("daily", "suspense", "unease", "horror", "urgency", "sorrow", "awe", "aftermath")
INTENSITIES = ("low", "medium", "high")
# ``openai_compatible`` 是旧的单条配置别名，保留读取兼容；新配置一律用 ``platform``，
# 外部生图的地址/模型/密钥只认 ``imageplatform`` 平台表，避免两套真相。
PROVIDERS = ("procedural", "platform", "openai_compatible")
DIRECTOR_FALLBACKS = ("rule", "hold")
MUSIC_SOURCES = ("realtime", "bgm", "hybrid")
MUSIC_ENGINES = ("procedural", "external", "off")
EXTERNAL_PROVIDERS = ("jamendo", "openverse")
ILLUSTRATION_TRIGGERS = ("off", "important", "turn_interval", "frequent")
# 档位越低越「收紧」：房间只能把触发档位往下压，不能抬高。
_TRIGGER_RANK = {"off": 0, "important": 1, "turn_interval": 2, "frequent": 3}

_MUSIC_KEYS = (
    "enabled", "music_enabled", "music_auto_switch", "music_default_profile",
    "music_default_intensity", "music_volume", "music_crossfade_ms",
    "music_hysteresis_turns", "music_seed", "music_duck_bgm", "music_source",
    "music_source_allowed", "music_source_allow_personal",
    "music_engine", "music_engine_allowed",
    "music_external_providers", "music_external_license_allow",
    "music_external_instrumental_only", "music_external_timeout",
    "music_external_search_ttl", "music_external_url_ttl",
    "music_external_daily_budget", "music_external_room_hourly_cap",
    "music_external_cache_audio", "music_external_cache_quota_mb",
    "music_external_cache_max_entries", "music_external_cache_max_track_mb",
    "music_external_cache_overflow",
    "music_external_scenario_record", "music_external_scenario_prefer",
    "music_external_circuit_failures", "music_external_circuit_cooldown_s",
    "music_external_jamendo_client_id", "music_external_allowed_hosts",
    "music_external_phrase_bars", "music_external_fade_ms",
    "music_director_enabled",
    "music_director_interval_turns", "music_director_daily_budget", "music_director_timeout",
    "music_director_cache_ttl", "music_director_min_confidence", "music_director_fallback",
    "music_director_max_context_chars",
)
_ILLUSTRATION_KEYS = (
    "illustration_enabled", "illustration_provider", "illustration_auto",
    "illustration_platform_id", "illustration_base_url", "illustration_model",
    "illustration_size", "api_key", "illustration_timeout", "illustration_concurrency",
    "illustration_daily_budget", "illustration_room_hourly_cap", "illustration_min_turns",
    "illustration_trigger", "illustration_trigger_allowed",
    "illustration_kp_timeout",
    "illustration_kp_daily_budget", "illustration_kp_max_context_chars",
    "illustration_npc_enabled", "illustration_npc_size", "illustration_npc_max_per_turn",
    "illustration_room_cache_max_entries", "illustration_room_cache_quota_mb",
    "illustration_room_cache_max_image_mb", "illustration_room_cache_overflow",
    "illustration_scenario_record", "illustration_scenario_prefer",
    "illustration_scenario_quota_mb", "illustration_scenario_max_image_mb",
    "illustration_scenario_overflow",
)


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


def _as_choice(value: Any, default: str, choices: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _as_mode_list(value: Any, default: tuple[str, ...], choices: tuple[str, ...]) -> list[str]:
    """把逗号分隔的档位配置归一化成去重、保序的合法列表。"""
    if isinstance(value, (list, tuple)):
        raw_items = [str(item or "").strip().lower() for item in value]
    else:
        raw_items = [item.strip().lower() for item in str(value or "").split(",")]
    result: list[str] = []
    for item in raw_items:
        if item in choices and item not in result:
            result.append(item)
    return result or list(default)


def load_immersive_settings(config_dir: Path | str | None = None) -> dict[str, Any]:
    """读取生效配置并归一化，保证后续逻辑不依赖模板/用户配置是否完整。"""
    settings = load_settings(config_dir)
    raw = settings.data.get("immersive", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": _as_bool(raw.get("enabled"), True),
        "music_enabled": _as_bool(raw.get("music_enabled"), True),
        "music_auto_switch": _as_bool(raw.get("music_auto_switch"), True),
        "music_default_profile": _as_choice(raw.get("music_default_profile"), "daily", PROFILES),
        "music_default_intensity": _as_choice(raw.get("music_default_intensity"), "medium", INTENSITIES),
        "music_volume": _as_int(raw.get("music_volume"), 35, 0, 100),
        "music_crossfade_ms": _as_int(raw.get("music_crossfade_ms"), 3000, 500, 10000),
        "music_hysteresis_turns": _as_int(raw.get("music_hysteresis_turns"), 2, 0, 10),
        "music_seed": str(raw.get("music_seed") or "").strip()[:64],
        "music_duck_bgm": _as_bool(raw.get("music_duck_bgm"), True),
        "music_source": _as_choice(raw.get("music_source"), "hybrid", MUSIC_SOURCES),
        "music_source_allowed": _as_mode_list(
            raw.get("music_source_allowed"), MUSIC_SOURCES, MUSIC_SOURCES
        ),
        "music_source_allow_personal": _as_bool(raw.get("music_source_allow_personal"), True),
        # 引擎矩阵：站点默认 < 房间只能降级；invalid 值在运行时回落到 procedural（并 WARN）。
        "music_engine": _as_choice(raw.get("music_engine"), "procedural", MUSIC_ENGINES),
        "music_engine_allowed": _as_mode_list(
            raw.get("music_engine_allowed"), ("procedural", "external"), MUSIC_ENGINES
        ),
        "music_external_providers": ",".join(
            _as_mode_list(raw.get("music_external_providers"), EXTERNAL_PROVIDERS, EXTERNAL_PROVIDERS)
        ),
        "music_external_license_allow": ",".join(
            _as_mode_list(
                raw.get("music_external_license_allow"),
                ("cc0", "by", "by-sa"),
                ("cc0", "by", "by-sa", "by-nc", "by-nd", "by-nc-sa", "by-nc-nd"),
            )
        ),
        "music_external_instrumental_only": _as_bool(raw.get("music_external_instrumental_only"), True),
        "music_external_timeout": _as_int(raw.get("music_external_timeout"), 6, 2, 30),
        "music_external_result_limit": _as_int(raw.get("music_external_result_limit"), 10, 3, 20),
        # 元数据 TTL 24h；URL 10min；两者都只影响缓存新鲜度，不影响是否播放。
        "music_external_search_ttl": _as_int(raw.get("music_external_search_ttl"), 86400, 0, 604800),
        "music_external_url_ttl": _as_int(raw.get("music_external_url_ttl"), 600, 60, 86400),
        "music_external_daily_budget": _as_int(raw.get("music_external_daily_budget"), 200, 0, 100000),
        "music_external_room_hourly_cap": _as_int(raw.get("music_external_room_hourly_cap"), 30, 0, 10000),
        # 默认不缓存音频本体；打开后按单曲/总量/条目数做 LRU。
        "music_external_cache_audio": _as_bool(raw.get("music_external_cache_audio"), False),
        "music_external_cache_quota_mb": _as_int(raw.get("music_external_cache_quota_mb"), 256, 0, 102400),
        "music_external_cache_max_entries": _as_int(raw.get("music_external_cache_max_entries"), 60, 0, 1000),
        "music_external_cache_max_track_mb": _as_int(raw.get("music_external_cache_max_track_mb"), 12, 1, 64),
        "music_external_cache_overflow": _as_choice(
            raw.get("music_external_cache_overflow"), "lru", ("lru", "reject")
        ),
        "music_external_scenario_record": _as_bool(raw.get("music_external_scenario_record"), False),
        "music_external_scenario_prefer": _as_bool(raw.get("music_external_scenario_prefer"), True),
        "music_external_circuit_failures": _as_int(raw.get("music_external_circuit_failures"), 3, 1, 20),
        "music_external_circuit_cooldown_s": _as_int(
            raw.get("music_external_circuit_cooldown_s"), 600, 30, 7200
        ),
        "music_external_jamendo_client_id": str(raw.get("music_external_jamendo_client_id") or "").strip()[:128],
        "music_external_allowed_hosts": str(raw.get("music_external_allowed_hosts") or "").strip()[:512],
        "music_external_phrase_bars": _as_int(raw.get("music_external_phrase_bars"), 8, 1, 16),
        "music_external_fade_ms": _as_int(raw.get("music_external_fade_ms"), 4200, 1500, 12000),
        "music_director_enabled": _as_bool(raw.get("music_director_enabled"), False),
        "music_director_interval_turns": _as_int(raw.get("music_director_interval_turns"), 2, 1, 20),
        "music_director_daily_budget": _as_int(raw.get("music_director_daily_budget"), 0, 0, 100000),
        "music_director_timeout": _as_int(raw.get("music_director_timeout"), 45, 20, 300),
        "music_director_cache_ttl": _as_int(raw.get("music_director_cache_ttl"), 3600, 0, 86400),
        "music_director_min_confidence": _as_int(raw.get("music_director_min_confidence"), 55, 0, 100),
        "music_director_fallback": _as_choice(raw.get("music_director_fallback"), "rule", DIRECTOR_FALLBACKS),
        "music_director_max_context_chars": _as_int(raw.get("music_director_max_context_chars"), 1200, 200, 4000),
        "illustration_enabled": _as_bool(raw.get("illustration_enabled"), False),
        "illustration_provider": _as_choice(raw.get("illustration_provider"), "procedural", PROVIDERS),
        "illustration_auto": _as_bool(raw.get("illustration_auto"), True),
        "illustration_platform_id": str(raw.get("illustration_platform_id") or "").strip()[:64],
        "illustration_base_url": str(raw.get("illustration_base_url") or "").strip().rstrip("/")[:512],
        "illustration_model": str(raw.get("illustration_model") or "dall-e-3").strip()[:120],
        "illustration_size": str(raw.get("illustration_size") or "1024x1024").strip()[:32],
        "api_key": str(raw.get("api_key") or "").strip(),
        "illustration_timeout": _as_int(raw.get("illustration_timeout"), 120, 20, 600),
        "illustration_concurrency": _as_int(raw.get("illustration_concurrency"), 2, 1, 4),
        "illustration_daily_budget": _as_int(raw.get("illustration_daily_budget"), 0, 0, 100000),
        "illustration_room_hourly_cap": _as_int(raw.get("illustration_room_hourly_cap"), 12, 0, 500),
        "illustration_min_turns": _as_int(raw.get("illustration_min_turns"), 4, 1, 50),
        "illustration_trigger": _as_choice(
            raw.get("illustration_trigger"), "important", ILLUSTRATION_TRIGGERS
        ),
        "illustration_trigger_allowed": _as_mode_list(
            raw.get("illustration_trigger_allowed"),
            ("important", "turn_interval", "frequent"),
            ILLUSTRATION_TRIGGERS,
        ),
        "illustration_kp_timeout": _as_int(raw.get("illustration_kp_timeout"), 45, 10, 300),
        "illustration_kp_daily_budget": _as_int(raw.get("illustration_kp_daily_budget"), 200, 0, 10000),
        "illustration_kp_max_context_chars": _as_int(
            raw.get("illustration_kp_max_context_chars"), 1600, 200, 8000
        ),
        "illustration_npc_enabled": _as_bool(raw.get("illustration_npc_enabled"), False),
        "illustration_npc_size": str(raw.get("illustration_npc_size") or "768x768").strip()[:32],
        "illustration_npc_max_per_turn": _as_int(
            raw.get("illustration_npc_max_per_turn"), 1, 0, 4
        ),
        # 房间级缓存：落盘在房间目录内，默认 40 条 / 64MB / 单图 8MB，超限 LRU。
        "illustration_room_cache_max_entries": _as_int(
            raw.get("illustration_room_cache_max_entries"), 40, 0, 200
        ),
        "illustration_room_cache_quota_mb": _as_int(
            raw.get("illustration_room_cache_quota_mb"), 64, 0, 1024
        ),
        "illustration_room_cache_max_image_mb": _as_int(
            raw.get("illustration_room_cache_max_image_mb"), 8, 1, 64
        ),
        "illustration_room_cache_overflow": _as_choice(
            raw.get("illustration_room_cache_overflow"), "lru", ("lru", "reject")
        ),
        # 剧本配图缓存：默认「不写剧本、优先复用」，避免无声修改作者剧本与磁盘膨胀。
        "illustration_scenario_record": _as_bool(raw.get("illustration_scenario_record"), False),
        "illustration_scenario_prefer": _as_bool(raw.get("illustration_scenario_prefer"), True),
        "illustration_scenario_quota_mb": _as_int(raw.get("illustration_scenario_quota_mb"), 256, 0, 102400),
        "illustration_scenario_max_image_mb": _as_int(raw.get("illustration_scenario_max_image_mb"), 8, 1, 64),
        "illustration_scenario_overflow": _as_choice(
            raw.get("illustration_scenario_overflow"), "lru", ("lru", "reject")
        ),
        "locked": dict(settings.locked or {}),
    }


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """发给前端的配置：去掉密钥，仅暴露是否已配置。"""
    secret_keys = {"api_key", "locked", "music_external_jamendo_client_id"}
    data = {key: value for key, value in settings.items() if key not in secret_keys}
    available = bool(settings.get("music_external_jamendo_client_id"))
    data["has_api_key"] = bool(settings.get("api_key"))
    data["has_jamendo_client_id"] = available
    # 环境变量提供 client_id 时同样视为可用，但不回显具体值。
    if not available:
        data["has_jamendo_client_id"] = bool(os.environ.get("TRPG_JAMENDO_CLIENT_ID"))
    return data


def resolve_room_switches(settings: dict[str, Any], house_rules: dict[str, Any] | None) -> dict[str, bool]:
    """房间级开关只能「关」，不能越过站点总开关重新打开。"""
    rules = house_rules if isinstance(house_rules, dict) else {}
    site = bool(settings.get("enabled"))
    music_room = rules.get("immersive_music_enabled")
    music_auto_room = rules.get("immersive_music_auto_switch")
    illustration_room = rules.get("immersive_illustration_enabled")
    auto_room = rules.get("immersive_illustration_auto")
    scenario_record_room = rules.get("immersive_scenario_record")
    scenario_prefer_room = rules.get("immersive_scenario_prefer")
    return {
        "music": bool(settings.get("music_enabled")) and site and (True if music_room is None else bool(music_room)),
        "music_auto": bool(settings.get("music_auto_switch")) and (
            True if music_auto_room is None else bool(music_auto_room)
        ),
        "illustration": bool(settings.get("illustration_enabled")) and site and (
            True if illustration_room is None else bool(illustration_room)
        ),
        "illustration_auto": bool(settings.get("illustration_auto")) and (
            True if auto_room is None else bool(auto_room)
        ),
        # 两项剧本缓存配置同样只允许房间收紧：站点关闭时房间无法开启。
        "scenario_record": bool(settings.get("illustration_scenario_record")) and site and (
            True if scenario_record_room is None else bool(scenario_record_room)
        ),
        "scenario_prefer": bool(settings.get("illustration_scenario_prefer")) and site and (
            True if scenario_prefer_room is None else bool(scenario_prefer_room)
        ),
    }


def resolve_music_source(
    settings: dict[str, Any],
    house_rules: dict[str, Any] | None,
    personal_override: str | None = None,
) -> str:
    """解析生效的音乐来源。

    优先级：站点默认 < 个人覆盖（站点允许时）< 房间收紧。
    房间只能通过既有 ``immersive_music_enabled=false`` 把实时配乐关掉，
    因此收紧结果为 ``bgm``；不会把站点/个人选择的 ``bgm`` 重新放宽为混合或实时。
    """
    rules = house_rules if isinstance(house_rules, dict) else {}
    site_source = str(settings.get("music_source") or "hybrid")
    allowed = settings.get("music_source_allowed") or ["realtime", "bgm", "hybrid"]
    effective = site_source
    candidate = str(personal_override or "").strip().lower()
    if (
        settings.get("music_source_allow_personal", True)
        and candidate in MUSIC_SOURCES
        and candidate in allowed
    ):
        effective = candidate
    room_music = rules.get("immersive_music_enabled")
    if room_music is False:
        effective = "bgm"
    if effective not in MUSIC_SOURCES:
        effective = "hybrid"
    return effective


def resolve_illustration_trigger(
    settings: dict[str, Any],
    house_rules: dict[str, Any] | None,
    personal_override: str | None = None,
) -> str:
    """解析生效的配图触发档位：站点默认 < 个人覆盖 < 房间收紧。

    个人可选项受 ``illustration_trigger_allowed`` 限制；``off`` 始终允许。
    房间通过 ``immersive_illustration_trigger``（important/off）与既有开关收紧，
    取两者中更低的档位，绝不抬高。
    """
    rules = house_rules if isinstance(house_rules, dict) else {}
    if not bool(settings.get("illustration_enabled")):
        return "off"
    if not bool(settings.get("illustration_auto")):
        return "off"
    effective = str(settings.get("illustration_trigger") or "important")
    if effective not in ILLUSTRATION_TRIGGERS:
        effective = "important"
    allowed = settings.get("illustration_trigger_allowed") or []
    candidate = str(personal_override or "").strip().lower()
    if candidate in ILLUSTRATION_TRIGGERS and (candidate == "off" or candidate in allowed):
        effective = candidate
    if rules.get("immersive_illustration_enabled") is False:
        return "off"
    if rules.get("immersive_illustration_auto") is False:
        return "off"
    room_trigger = str(rules.get("immersive_illustration_trigger") or "").strip().lower()
    if room_trigger in ("off", "important"):
        if _TRIGGER_RANK[room_trigger] <= _TRIGGER_RANK.get(effective, 1):
            effective = room_trigger
    return effective


def set_image_api_key(value: str, config_dir: Path | str | None = None) -> None:
    """写入生图密钥（空串表示清除），由管理员专用接口调用。"""
    key = str(value or "").strip()
    if len(key) > 4096:
        raise ValueError("API key 过长")
    target = general_config_path(config_dir)
    update_sections(target, {"immersive": {"api_key": key}})
