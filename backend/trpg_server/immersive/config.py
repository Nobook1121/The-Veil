"""沉浸系统配置读取与房间级覆盖解析。

配置沿用项目的五层合并约定：代码默认 < 模板 < 遗留 JSON < general.toml <
环境变量 < CLI。这里只做「读取后归一化」，写入仍交给 ``config.writer``（tomlkit
保留注释）。生图密钥走管理员专用接口，不经过通用配置 POST。
"""
from __future__ import annotations

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

_MUSIC_KEYS = (
    "enabled", "music_enabled", "music_auto_switch", "music_default_profile",
    "music_default_intensity", "music_volume", "music_crossfade_ms",
    "music_hysteresis_turns", "music_seed", "music_duck_bgm",
    "music_director_enabled", "music_director_platform", "music_director_model",
    "music_director_interval_turns", "music_director_daily_budget", "music_director_timeout",
    "music_director_cache_ttl", "music_director_min_confidence", "music_director_fallback",
    "music_director_max_context_chars",
)
_ILLUSTRATION_KEYS = (
    "illustration_enabled", "illustration_provider", "illustration_auto",
    "illustration_platform_id", "illustration_base_url", "illustration_model",
    "illustration_size", "api_key", "illustration_timeout", "illustration_concurrency",
    "illustration_daily_budget", "illustration_room_hourly_cap", "illustration_min_turns",
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
        "music_director_enabled": _as_bool(raw.get("music_director_enabled"), False),
        "music_director_platform": str(raw.get("music_director_platform") or "").strip()[:64],
        "music_director_model": str(raw.get("music_director_model") or "").strip()[:120],
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
    data = {key: value for key, value in settings.items() if key not in {"api_key", "locked"}}
    data["has_api_key"] = bool(settings.get("api_key"))
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


def set_image_api_key(value: str, config_dir: Path | str | None = None) -> None:
    """写入生图密钥（空串表示清除），由管理员专用接口调用。"""
    key = str(value or "").strip()
    if len(key) > 4096:
        raise ValueError("API key 过长")
    target = general_config_path(config_dir)
    update_sections(target, {"immersive": {"api_key": key}})
