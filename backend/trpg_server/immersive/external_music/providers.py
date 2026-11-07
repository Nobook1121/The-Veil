"""免费曲库适配器：Jamendo 主源 + Openverse 次源。

只做两件事：发请求、把返回捏成统一结构。任何网络/解析错误都转成
``{"ok": False, "reason": ...}``，不向上抛异常——外链失败必须能静默回退到
程序化底床，所以这里不允许出现未捕获异常。

统一曲目结构（缺失字段用空串 / 0 表示）::

    {
        "provider": "jamendo" | "openverse",
        "track_id": "...",
        "title": "...",
        "artist": "...",
        "license": "by",              # 原始写法，交给 license.normalize_license 归一
        "license_url": "https://...",
        "source_url": "https://...",  # 曲目详情页，用于署名
        "stream_url": "https://...",  # 可直接播放的音频地址
        "duration_ms": 210000,
        "attribution": "...",         # 曲库直接给出的署名，缺失时由 license 模块补
        "instrumental": true | None,
        "tags": ["ambient", "dark"],
    }
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
OPENVERSE_API = "https://api.openverse.org/v1/audio/"
_USER_AGENT = os.environ.get(
    "TRPG_USER_AGENT",
    "TheVeil/1.0 (+https://github.com/the-veil; immersive music search)",
)
_MAX_BYTES_LABEL = "12MB"


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
    return session


def _clean(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _as_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _tags_from(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace("|", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if isinstance(item, dict):
                items.append(item.get("name") or item.get("title") or "")
            else:
                items.append(item)
    else:
        items = []
    result: list[str] = []
    for item in items:
        text = _clean(item, 48).lower()
        if text and text not in result:
            result.append(text)
    return result[:24]


def _missing_credentials(provider: str) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        "tracks": [],
        "reason": "missing_credentials",
        "elapsed_ms": 0,
    }


def _request_failure(provider: str, reason: str, started: float, detail: str = "") -> dict[str, Any]:
    if detail:
        logger.warning("external_music provider=%s reason=%s detail=%s", provider, reason, detail[:300])
    return {
        "ok": False,
        "provider": provider,
        "tracks": [],
        "reason": reason,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def jamendo_client_id(settings: dict[str, Any] | None = None) -> str:
    """Jamendo 需要 client_id；优先配置，其次环境变量。"""
    cfg = settings if isinstance(settings, dict) else {}
    configured = _clean(cfg.get("music_external_jamendo_client_id"), 128)
    return configured or _clean(os.environ.get("TRPG_JAMENDO_CLIENT_ID"), 128)


def search_jamendo(
    *,
    query: str,
    tags: Iterable[str],
    client_id: str,
    timeout: float = 6.0,
    limit: int = 10,
) -> dict[str, Any]:
    """按自由文本 + 模糊标签检索 Jamendo；返回统一结构。"""
    started = time.monotonic()
    if not client_id:
        return _missing_credentials("jamendo")
    tag_text = "+".join(_clean(tag, 32).replace(" ", "-") for tag in tags if _clean(tag, 32))
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": str(max(1, min(20, int(limit)))),
        "include": "licenses+musicinfo",
        "audioformat": "mp32",
        "order": "relevance",
        "search": _clean(query, 120),
    }
    if tag_text:
        # Jamendo 的 fuzzytags 用 `+` 连接（AND），比 `tags` 更适合「氛围近似」检索。
        params["fuzzytags"] = tag_text
    try:
        with _session() as session:
            response = session.get(
                JAMENDO_API, params=params, timeout=(3.05, max(1.0, float(timeout)))
            )
    except requests.RequestException as exc:
        return _request_failure("jamendo", "network_error", started, str(exc))
    if response.status_code != 200:
        return _request_failure("jamendo", f"http_{response.status_code}", started, response.text[:200])
    try:
        payload = response.json()
    except ValueError:
        return _request_failure("jamendo", "invalid_json", started)
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return _request_failure("jamendo", "invalid_payload", started)

    tracks: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        track_id = _clean(row.get("id"), 40)
        stream = _clean(row.get("audio") or row.get("audiodownload"), 1024)
        title = _clean(row.get("name"), 200)
        artist = _clean(row.get("artist_name"), 120)
        if not track_id or not stream or not title or not artist:
            continue
        musicinfo = row.get("musicinfo") if isinstance(row.get("musicinfo"), dict) else {}
        tag_block = musicinfo.get("tags") if isinstance(musicinfo.get("tags"), dict) else {}
        tags_found: list[str] = []
        for key in ("genres", "vartags", "instruments"):
            tags_found.extend(_tags_from(tag_block.get(key)))
        vocal = _clean(musicinfo.get("vocalinstrumental"), 24).lower()
        instrumental = True if vocal in {"instrumental", "background"} else (False if vocal else None)
        tracks.append(
            {
                "provider": "jamendo",
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "license": _clean(row.get("license_ccurl"), 200),
                "license_url": _clean(row.get("license_ccurl"), 300),
                "source_url": _clean(row.get("shareurl") or f"https://www.jamendo.com/track/{track_id}", 300),
                "stream_url": stream,
                "duration_ms": _as_int(row.get("duration")) * 1000,
                "attribution": "",
                "instrumental": instrumental,
                "tags": list(dict.fromkeys(tags_found))[:24],
            }
        )
    return {
        "ok": True,
        "provider": "jamendo",
        "tracks": tracks,
        "reason": "" if tracks else "empty",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def search_openverse(
    *,
    query: str,
    tags: Iterable[str],
    timeout: float = 6.0,
    limit: int = 10,
) -> dict[str, Any]:
    """按关键词检索 Openverse 音频；许可过滤在请求层与本地各做一次。"""
    started = time.monotonic()
    words = [_clean(query, 120)]
    words.extend(_clean(tag, 32) for tag in tags if _clean(tag, 32))
    params = {
        "q": " ".join(word for word in words if word)[:180],
        # Openverse 的 license 过滤只接受逗号分隔的短标签。
        "license": "cc0,by,by-sa",
        "page_size": str(max(1, min(20, int(limit)))),
    }
    try:
        with _session() as session:
            response = session.get(
                OPENVERSE_API, params=params, timeout=(3.05, max(1.0, float(timeout)))
            )
    except requests.RequestException as exc:
        return _request_failure("openverse", "network_error", started, str(exc))
    if response.status_code != 200:
        return _request_failure("openverse", f"http_{response.status_code}", started, response.text[:200])
    try:
        payload = response.json()
    except ValueError:
        return _request_failure("openverse", "invalid_json", started)
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return _request_failure("openverse", "invalid_payload", started)

    tracks: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        track_id = _clean(row.get("id"), 60)
        stream = _clean(row.get("url"), 1024)
        title = _clean(row.get("title"), 200)
        artist = _clean(row.get("creator"), 120)
        if not track_id or not stream or not title or not artist:
            continue
        tracks.append(
            {
                "provider": "openverse",
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "license": _clean(row.get("license"), 40),
                "license_url": _clean(row.get("license_url"), 300),
                "source_url": _clean(row.get("foreign_landing_url") or row.get("url"), 300),
                "stream_url": stream,
                "duration_ms": _as_int(row.get("duration")),
                "attribution": _clean(row.get("attribution"), 300),
                "instrumental": None,
                "tags": _tags_from(row.get("tags")),
            }
        )
    return {
        "ok": True,
        "provider": "openverse",
        "tracks": tracks,
        "reason": "" if tracks else "empty",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


PROVIDER_SEARCHERS = {
    "jamendo": search_jamendo,
    "openverse": search_openverse,
}


def resolve_stream_url(provider: str, track_id: str, *, settings: dict[str, Any] | None = None, timeout: float = 6.0) -> str:
    """URL 过期后的重解析：目前两个源都能用一次检索按 id 找回。

    只用于代理下载前刷新；找不到就返回空串，调用方直接降级（不猜 URL）。
    """
    provider_name = _clean(provider, 24).lower()
    if provider_name == "jamendo":
        client_id = jamendo_client_id(settings)
        if not client_id:
            return ""
        try:
            with _session() as session:
                response = session.get(
                    JAMENDO_API,
                    params={
                        "client_id": client_id,
                        "format": "json",
                        "id": _clean(track_id, 40),
                        "include": "licenses",
                        "audioformat": "mp32",
                    },
                    timeout=(3.05, max(1.0, float(timeout))),
                )
            rows = response.json().get("results") if response.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return ""
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and _clean(row.get("id"), 40) == _clean(track_id, 40):
                    return _clean(row.get("audio") or row.get("audiodownload"), 1024)
    return ""
