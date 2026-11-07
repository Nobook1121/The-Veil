"""外链 BGM 的编排层：降级链、熔断、预算、两级缓存与异步任务。

降级链（与设计 §8.4 一致）::

    external  --曲库不可达/空结果/许可不合格-->  procedural（底床继续）  -->  hold（保持现状）

原则：

- 任何失败都返回结构化结果并附带 ``fallback="procedural"``，前端永远留在底床；
- 搜索在独立线程池执行，绝不阻塞叙事主流程或浏览器主线程；
- 同一房间同时只跑一个外链检索，避免连续回合把曲库打爆；
- URL 过期才重解析，元数据 TTL 内直接复用；
- 音频代理只允许 https 公网地址，拒绝私网/回环/非 80/443 端口。
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from trpg_server.immersive import broadcast
from trpg_server.immersive.config import load_immersive_settings
from trpg_server.immersive.external_music import cache, license as license_mod, providers, search

logger = logging.getLogger(__name__)

ENGINE_PROCEDURAL = "procedural"
ENGINE_EXTERNAL = "external"
ENGINE_OFF = "off"
ENGINES = (ENGINE_PROCEDURAL, ENGINE_EXTERNAL, ENGINE_OFF)
_PROVIDER_ORDER = ("jamendo", "openverse")

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="immersive-music-ext")
_IN_FLIGHT: dict[str, str] = {}
_IN_FLIGHT_LOCK = threading.Lock()
_LAST_ENGINE: dict[str, str] = {}

_CIRCUIT: dict[str, dict[str, Any]] = {}
_CIRCUIT_LOCK = threading.Lock()

_USAGE: dict[str, Any] = {"day": "", "count": 0, "rooms": {}}
_USAGE_LOCK = threading.Lock()


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


# ---------------------------------------------------------------- 引擎解析

def resolve_engine(
    settings: dict[str, Any],
    house_rules: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """解析生效引擎：站点默认 < 房间只能降级。

    返回 ``(engine, reason)``。``reason`` 只用于日志/前端提示，不参与播放决策。
    """
    if not settings.get("enabled") or not settings.get("music_enabled"):
        return ENGINE_OFF, "site_music_disabled"
    site = str(settings.get("music_engine") or ENGINE_PROCEDURAL).strip().lower()
    reason = "site"
    if site not in ENGINES:
        site = ENGINE_PROCEDURAL
        reason = "invalid_config"
    rules = house_rules if isinstance(house_rules, dict) else {}
    # 房间关掉配乐是「直接降到 off」，与是否单独指定引擎无关。
    if rules.get("immersive_music_enabled") is False:
        return ENGINE_OFF, "room_music_disabled"
    room = rules.get("immersive_music_engine")
    if room is None or str(room).strip() == "":
        return site, reason
    room_engine = str(room).strip().lower()
    if room_engine not in ENGINES:
        return site, "invalid_room_engine"
    if room_engine == ENGINE_OFF:
        return ENGINE_OFF, "room_off"
    if room_engine == ENGINE_PROCEDURAL:
        return (ENGINE_OFF if site == ENGINE_OFF else ENGINE_PROCEDURAL), "room_procedural"
    # 房间请求 external：只有站点本身是 external 才生效，否则只能降级。
    if site != ENGINE_EXTERNAL:
        return (site if site in ENGINES else ENGINE_PROCEDURAL), "room_cannot_upgrade"
    return ENGINE_EXTERNAL, "room_external"


def notify_engine(room_id: str, engine: str, *, fade_ms: int = 2200, force: bool = False) -> None:
    """把引擎状态广播给房间，供前端平滑过渡（不硬切）。"""
    previous = _LAST_ENGINE.get(room_id)
    if previous == engine and not force:
        return
    _LAST_ENGINE[room_id] = engine
    broadcast.emit(
        "immersive_music_engine",
        {
            "room_id": room_id,
            "engine": engine,
            "fade_ms": max(500, int(fade_ms)),
            "fallback": ENGINE_PROCEDURAL if engine == ENGINE_EXTERNAL else engine,
        },
    )


# ---------------------------------------------------------------- 预算 / 熔断

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _consume_budget(room_id: str, settings: dict[str, Any]) -> tuple[bool, str]:
    """站点每日预算 + 房间小时配额；返回 ``(是否放行, 原因)``。"""
    daily_cap = _as_int(settings.get("music_external_daily_budget"), 200, 0, 100000)
    hourly_cap = _as_int(settings.get("music_external_room_hourly_cap"), 30, 0, 10000)
    now = time.time()
    with _USAGE_LOCK:
        if _USAGE["day"] != _today():
            _USAGE["day"] = _today()
            _USAGE["count"] = 0
            _USAGE["rooms"] = {}
        if daily_cap > 0 and int(_USAGE["count"]) >= daily_cap:
            return False, "daily_budget_exceeded"
        stamps: list[float] = [float(item) for item in _USAGE["rooms"].get(room_id, []) if now - float(item) < 3600]
        if hourly_cap > 0 and len(stamps) >= hourly_cap:
            _USAGE["rooms"][room_id] = stamps
            return False, "room_hourly_cap"
        stamps.append(now)
        _USAGE["rooms"][room_id] = stamps
        _USAGE["count"] = int(_USAGE["count"]) + 1
    return True, ""


def _circuit_open(provider: str, settings: dict[str, Any]) -> bool:
    with _CIRCUIT_LOCK:
        state = _CIRCUIT.get(provider)
        if not state:
            return False
        return float(state.get("open_until") or 0) > time.time()


def _record_provider(provider: str, ok: bool, settings: dict[str, Any]) -> None:
    threshold = _as_int(settings.get("music_external_circuit_failures"), 3, 1, 20)
    cooldown = _as_int(settings.get("music_external_circuit_cooldown_s"), 600, 30, 7200)
    with _CIRCUIT_LOCK:
        state = _CIRCUIT.setdefault(provider, {"failures": 0, "open_until": 0.0})
        if ok:
            state["failures"] = 0
            state["open_until"] = 0.0
            return
        state["failures"] = int(state.get("failures") or 0) + 1
        if int(state["failures"]) >= threshold:
            state["open_until"] = time.time() + cooldown
            logger.warning(
                "external_music circuit opened provider=%s failures=%s cooldown=%ss",
                provider, state["failures"], cooldown,
            )


# ---------------------------------------------------------------- 工具函数

def _providers(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("music_external_providers") or "jamendo,openverse"
    items = [str(item or "").strip().lower() for item in str(raw).split(",")]
    result = [name for name in _PROVIDER_ORDER if name in items]
    return result or ["openverse"]


def _stream_path(room_id: str, track_key: str) -> str:
    return (
        f"/api/immersive/rooms/{quote(str(room_id), safe='')}"
        f"/music/external/audio/{track_key}"
    )


def _finalize_track(track: dict[str, Any], *, room_id: str, allow: list[str], elapsed: int) -> dict[str, Any]:
    canonical = license_mod.normalize_license(track.get("license"))
    track["license"] = canonical
    if not str(track.get("attribution") or "").strip():
        track["attribution"] = license_mod.build_attribution(
            str(track.get("title") or ""),
            str(track.get("artist") or ""),
            canonical,
            "Jamendo" if track.get("provider") == "jamendo" else "Openverse",
        )
    track["license_label"] = license_mod.license_label(canonical)
    track["track_key"] = cache.track_key(str(track.get("provider") or ""), str(track.get("track_id") or ""))
    allowed = set(allow)
    payload = {
        "track_key": track["track_key"],
        "provider": str(track.get("provider") or ""),
        "title": str(track.get("title") or ""),
        "artist": str(track.get("artist") or ""),
        "license": canonical,
        "license_label": track["license_label"],
        "license_url": str(track.get("license_url") or ""),
        "source_url": str(track.get("source_url") or ""),
        "attribution": str(track.get("attribution") or ""),
        "duration_ms": int(track.get("duration_ms") or 0),
        "tags": list(track.get("tags") or [])[:12],
        "instrumental": track.get("instrumental"),
        "stream_path": _stream_path(room_id, track["track_key"]),
        "url_expires_at": int(track.get("url_expires_at") or 0),
        "elapsed_ms": elapsed,
        "license_allowed": canonical in allowed,
    }
    return payload


def _search_providers(
    *,
    settings: dict[str, Any],
    query: str,
    tags: list[str],
    limit: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """按优先级检索；返回 ``(曲目, 失败原因, 耗时毫秒)``。"""
    started = time.monotonic()
    tracks: list[dict[str, Any]] = []
    reasons: list[str] = []
    timeout = float(_as_int(settings.get("music_external_timeout"), 6, 2, 30))
    client_id = providers.jamendo_client_id(settings)
    for provider in _providers(settings):
        if _circuit_open(provider, settings):
            reasons.append(f"{provider}:circuit_open")
            continue
        if provider == "jamendo":
            result = providers.search_jamendo(
                query=query, tags=tags, client_id=client_id, timeout=timeout, limit=limit
            )
        else:
            result = providers.search_openverse(query=query, tags=tags, timeout=timeout, limit=limit)
        ok = bool(result.get("ok")) and bool(result.get("tracks"))
        _record_provider(provider, ok, settings)
        if ok:
            tracks.extend(result.get("tracks") or [])
        else:
            reasons.append(f"{provider}:{result.get('reason') or 'empty'}")
    return tracks, reasons, int((time.monotonic() - started) * 1000)


def _prepare_candidates(
    tracks: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    room_id: str,
) -> tuple[list[dict[str, Any]], int]:
    allow = license_mod.allowed_licenses(settings.get("music_external_license_allow"))
    instrumental_only = bool(settings.get("music_external_instrumental_only", True))
    accepted: list[dict[str, Any]] = []
    rejected = 0
    for track in search.dedupe(tracks):
        if instrumental_only and track.get("instrumental") is False:
            rejected += 1
            continue
        canonical = license_mod.normalize_license(track.get("license"))
        track["license"] = canonical
        if not str(track.get("attribution") or "").strip():
            track["attribution"] = license_mod.build_attribution(
                str(track.get("title") or ""),
                str(track.get("artist") or ""),
                canonical,
                "Jamendo" if track.get("provider") == "jamendo" else "Openverse",
            )
        ok, _reason = license_mod.is_playable(track, allow)
        if not ok:
            rejected += 1
            continue
        accepted.append(track)
    return accepted, rejected


def _resolve_cached(
    keys: list[str],
    *,
    room_cache_dir: Path | None,
    scenario_cache_dir: Path | None,
    settings: dict[str, Any],
    room_id: str,
) -> list[dict[str, Any]]:
    allow = license_mod.allowed_licenses(settings.get("music_external_license_allow"))
    result: list[dict[str, Any]] = []
    for key in keys:
        entry = cache.get_track(room_cache_dir, key) or cache.get_track(scenario_cache_dir, key)
        if not isinstance(entry, dict):
            continue
        if license_mod.missing_attribution_fields(entry):
            continue
        ok, _ = license_mod.is_playable(entry, allow)
        if not ok:
            continue
        result.append(entry)
    return result


def _remember(
    tracks: list[dict[str, Any]],
    *,
    room_cache_dir: Path | None,
    scenario_cache_dir: Path | None,
    settings: dict[str, Any],
    record_scenario: bool,
    query: dict[str, Any] | None = None,
) -> list[str]:
    url_ttl = _as_int(settings.get("music_external_url_ttl"), 600, 60, 86400)
    keys: list[str] = []
    for track in tracks:
        key = cache.remember_track(room_cache_dir, track, url_ttl=url_ttl)
        if key:
            keys.append(key)
        if record_scenario and scenario_cache_dir is not None:
            cache.remember_track(scenario_cache_dir, track, url_ttl=url_ttl)
    if query is not None:
        search_ttl = _as_int(settings.get("music_external_search_ttl"), 86400, 0, 604800)
        for directory in (room_cache_dir, scenario_cache_dir if record_scenario else None):
            if directory is None:
                continue
            cache.remember_query(
                directory,
                key=str(query["key"]),
                track_keys=keys,
                fingerprint=str(query.get("fingerprint") or ""),
                ttl=search_ttl,
                meta=query.get("meta") if isinstance(query.get("meta"), dict) else None,
            )
    return keys


# ---------------------------------------------------------------- 公开 API

def search_and_select(
    *,
    room_dir: Path | str | None,
    room_id: str,
    scenario_id: str = "",
    profile: str = "daily",
    intensity: str = "medium",
    scene: str = "",
    extra_tags: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    config_dir: Path | str | None = None,
    house_rules: dict[str, Any] | None = None,
    trigger: str = "auto",
    request_id: str = "",
    force: bool = False,
    scenarios_dir: Path | str | None = None,
) -> dict[str, Any]:
    """检索并选出一首可播放曲目；任何失败都返回结构化的降级结果。"""
    started = time.monotonic()
    cfg = settings if isinstance(settings, dict) else load_immersive_settings(config_dir)
    engine, engine_reason = resolve_engine(cfg, house_rules)
    base: dict[str, Any] = {
        "ok": False,
        "status": "degraded",
        "engine": engine,
        "engine_reason": engine_reason,
        "fallback": ENGINE_PROCEDURAL,
        "track": None,
        "request_id": request_id,
        "trigger": trigger,
        "cached": False,
        "reason": "",
        "elapsed_ms": 0,
    }
    if engine != ENGINE_EXTERNAL:
        base.update({"status": "disabled", "reason": engine_reason})
        return base

    room_path = Path(room_dir) if room_dir else None
    room_cache_dir = cache.room_music_dir(room_path, for_write=True)
    scenario_cache_dir = cache.scenario_music_dir(
        scenario_id, for_write=bool(cfg.get("music_external_scenario_record")), scenarios_dir=scenarios_dir
    )
    prefer_scenario = bool(cfg.get("music_external_scenario_prefer", True))
    record_scenario = bool(cfg.get("music_external_scenario_record", False)) and scenario_cache_dir is not None

    tags = search.build_tags(profile, intensity, scene, extra_tags)
    qkey = cache.query_key(
        profile=profile,
        intensity=intensity,
        scene=scene,
        tags=tags,
        providers=_providers(cfg),
    )
    fingerprint = qkey
    recent = cache.recent_track_keys(room_cache_dir)
    instrumental_only = bool(cfg.get("music_external_instrumental_only", True))

    # 1) 查询缓存：优先剧本（若开启），其次房间；空列表即负缓存。
    cache_dirs = [scenario_cache_dir, room_cache_dir] if prefer_scenario else [room_cache_dir, scenario_cache_dir]
    for directory in cache_dirs:
        if directory is None:
            continue
        hit = cache.get_query(directory, qkey)
        if hit is None:
            continue
        keys = [str(item) for item in hit.get("tracks") or []]
        if not keys:
            base.update({"status": "empty", "reason": "negative_cache", "cached": True})
            base["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            return base
        cached_tracks = _resolve_cached(
            keys,
            room_cache_dir=room_cache_dir,
            scenario_cache_dir=scenario_cache_dir,
            settings=cfg,
            room_id=room_id,
        )
        ranked = search.rank(
            cached_tracks, tags=tags, recent_ids=recent, instrumental_only=instrumental_only, limit=10
        )
        if ranked:
            payload = _finalize_track(ranked[0], room_id=room_id, allow=license_mod.allowed_licenses(
                cfg.get("music_external_license_allow")
            ), elapsed=0)
            if not payload["license_allowed"]:
                continue
            base.update({"ok": True, "status": "selected", "track": payload, "cached": True})
            base["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            base["provider"] = payload["provider"]
            logger.info(
                "external_music cache_hit room=%s profile=%s provider=%s key=%s",
                room_id, profile, payload["provider"], payload["track_key"],
            )
            return base

    # 2) 曲库检索。
    allowed, budget_reason = _consume_budget(room_id, cfg)
    if not allowed:
        base.update({"status": "throttled", "reason": budget_reason})
        base["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        logger.info("external_music throttled room=%s reason=%s", room_id, budget_reason)
        return base
    limit = _as_int(cfg.get("music_external_result_limit"), 10, 3, 20)
    query_text = search.profile_query(profile)
    scene_hints = search.scene_tags(scene)[:3]
    if scene_hints:
        query_text = f"{query_text} {' '.join(scene_hints)}"
    tracks, reasons, elapsed = _search_providers(settings=cfg, query=query_text, tags=tags, limit=limit)
    accepted, rejected = _prepare_candidates(tracks, settings=cfg, room_id=room_id)
    ranked = search.rank(
        accepted, tags=tags, recent_ids=recent, instrumental_only=instrumental_only, limit=10
    )
    _remember(
        ranked,
        room_cache_dir=room_cache_dir,
        scenario_cache_dir=scenario_cache_dir,
        settings=cfg,
        record_scenario=record_scenario,
        query={"key": qkey, "fingerprint": fingerprint, "meta": {"profile": profile, "trigger": trigger}},
    )

    if not ranked:
        reason = ",".join(reasons) if reasons else ("all_rejected" if rejected else "empty")
        base.update({"status": "empty", "reason": reason})
        base["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        logger.info(
            "external_music empty room=%s profile=%s reasons=%s rejected=%s elapsed_ms=%s",
            room_id, profile, reason, rejected, base["elapsed_ms"],
        )
        return base

    payload = _finalize_track(
        ranked[0], room_id=room_id, allow=license_mod.allowed_licenses(cfg.get("music_external_license_allow")), elapsed=elapsed
    )
    alternatives = [
        _finalize_track(
            item, room_id=room_id, allow=license_mod.allowed_licenses(cfg.get("music_external_license_allow")), elapsed=elapsed
        )
        for item in ranked[1:4]
    ]
    base.update(
        {
            "ok": True,
            "status": "selected",
            "track": payload,
            "alternatives": alternatives,
            "provider": payload["provider"],
            "cached": False,
        }
    )
    base["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    logger.info(
        "external_music selected room=%s profile=%s provider=%s key=%s license=%s elapsed_ms=%s",
        room_id, profile, payload["provider"], payload["track_key"], payload["license"], base["elapsed_ms"],
    )
    return base


def in_flight(room_id: str) -> bool:
    with _IN_FLIGHT_LOCK:
        return str(room_id) in _IN_FLIGHT


def submit(
    *,
    room_dir: Path | str | None,
    room_id: str,
    scenario_id: str = "",
    profile: str = "daily",
    intensity: str = "medium",
    scene: str = "",
    extra_tags: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    config_dir: Path | str | None = None,
    house_rules: dict[str, Any] | None = None,
    trigger: str = "auto",
    request_id: str = "",
    force: bool = False,
) -> str | None:
    """提交后台检索任务；返回 request_id，已有任务在跑时返回 ``None``。"""
    rid = request_id or f"ext-{int(time.time() * 1000)}-{room_id}"
    with _IN_FLIGHT_LOCK:
        if room_id in _IN_FLIGHT:
            return None
        _IN_FLIGHT[room_id] = rid

    def _job() -> None:
        payload: dict[str, Any]
        try:
            payload = search_and_select(
                room_dir=room_dir,
                room_id=room_id,
                scenario_id=scenario_id,
                profile=profile,
                intensity=intensity,
                scene=scene,
                extra_tags=extra_tags,
                settings=settings,
                config_dir=config_dir,
                house_rules=house_rules,
                trigger=trigger,
                request_id=rid,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 - 外链任务失败绝不能影响主流程
            logger.warning("external_music job failed room=%s: %s", room_id, exc, exc_info=True)
            payload = {
                "ok": False,
                "status": "degraded",
                "engine": ENGINE_EXTERNAL,
                "fallback": ENGINE_PROCEDURAL,
                "track": None,
                "request_id": rid,
                "trigger": trigger,
                "reason": "internal_error",
                "elapsed_ms": 0,
            }
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.pop(room_id, None)
        if payload.get("status") == "disabled":
            return
        cfg = settings if isinstance(settings, dict) else load_immersive_settings(config_dir)
        payload["room_id"] = room_id
        payload["fade_ms"] = int(cfg.get("music_external_fade_ms") or 4200)
        payload["crossfade_ms"] = payload["fade_ms"]
        payload["phrase_bars"] = int(cfg.get("music_external_phrase_bars") or 8)
        broadcast.emit("immersive_music_external", payload)

    try:
        _EXECUTOR.submit(_job)
    except RuntimeError:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.pop(room_id, None)
        return None
    return rid


# ---------------------------------------------------------------- 音频代理

def _host_allowed(host: str, settings: dict[str, Any]) -> bool:
    rules = str(settings.get("music_external_allowed_hosts") or "").strip().lower()
    if not rules:
        return True
    hostname = host.lower()
    for item in rules.split(","):
        suffix = item.strip().lstrip(".")
        if suffix and (hostname == suffix or hostname.endswith("." + suffix)):
            return True
    return False


def is_safe_public_url(url: str, settings: dict[str, Any] | None = None) -> bool:
    """代理前的 SSRF 防护：只允许 https 公网、80/443、非私网地址。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.port not in (None, 443):
        return False
    host = parsed.hostname.lower().strip(".")
    if not host or host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return False
    return _host_allowed(host, settings if isinstance(settings, dict) else {})


def audio_target(
    *,
    room_dir: Path | str | None,
    room_id: str,
    track_key: str,
    scenario_id: str = "",
    settings: dict[str, Any] | None = None,
    config_dir: Path | str | None = None,
) -> dict[str, Any]:
    """解析代理目标：优先本地音频缓存，其次（必要时刷新）上游 URL。"""
    cfg = settings if isinstance(settings, dict) else load_immersive_settings(config_dir)
    room_cache_dir = cache.room_music_dir(room_dir)
    scenario_cache_dir = cache.scenario_music_dir(scenario_id)
    entry = cache.get_track(room_cache_dir, track_key) or cache.get_track(scenario_cache_dir, track_key)
    if not isinstance(entry, dict):
        return {"ok": False, "reason": "not_found"}
    ok, reason = license_mod.is_playable(
        entry, license_mod.allowed_licenses(cfg.get("music_external_license_allow"))
    )
    if not ok:
        return {"ok": False, "reason": reason}

    if cfg.get("music_external_cache_audio"):
        for directory in (room_cache_dir, scenario_cache_dir):
            path = cache.audio_path(directory, track_key)
            if path is not None:
                cache.touch_audio(directory, track_key)
                return {"ok": True, "local": path, "track": entry, "reason": "local_cache"}

    stream_url = str(entry.get("stream_url") or "")
    expires_at = float(entry.get("url_expires_at") or 0)
    if expires_at and expires_at < time.time():
        refreshed = providers.resolve_stream_url(
            str(entry.get("provider") or ""), str(entry.get("track_id") or ""), settings=cfg
        )
        if refreshed:
            entry["stream_url"] = refreshed
            stream_url = refreshed
            cache.remember_track(
                room_cache_dir,
                entry,
                url_ttl=_as_int(cfg.get("music_external_url_ttl"), 600, 60, 86400),
            )
    if not is_safe_public_url(stream_url, cfg):
        return {"ok": False, "reason": "unsafe_url"}
    return {"ok": True, "url": stream_url, "track": entry, "reason": "upstream"}


def cache_audio_bytes(
    *,
    room_dir: Path | str | None,
    track_key: str,
    content: bytes,
    suffix: str,
    settings: dict[str, Any],
    scenario_id: str = "",
) -> bool:
    """代理层把整段响应落盘（仅在显式开启 ``music_external_cache_audio`` 时调用）。"""
    ok = False
    room_cache_dir = cache.room_music_dir(room_dir, for_write=True)
    if room_cache_dir is not None:
        ok, _ = cache.store_audio(
            room_cache_dir, track_key, content=content, suffix=suffix, settings=settings
        )
    if settings.get("music_external_scenario_record"):
        scenario_cache_dir = cache.scenario_music_dir(scenario_id, for_write=True)
        if scenario_cache_dir is not None:
            cache.store_audio(
                scenario_cache_dir, track_key, content=content, suffix=suffix, settings=settings
            )
    return ok
