"""外链曲目两级缓存：剧本级 + 房间级。

目录（与设计要求一致）::

    data/scenarios/scenario-<id>/immersive/music/index.json
    data/runtime/rooms/<room_id>/immersive_cache/music/index.json

默认**只缓存元数据与 URL**，不落音频本体：

- 许可要求随曲目分发署名与来源，缓存 URL 已足够复现；
- 音频本体动辄 3–10MB，跨房间复制会让磁盘不可控；
- 播放本身走同源代理，浏览器侧还有 HTTP 缓存，重复流量有限。

如果管理员显式打开 ``music_external_cache_audio``，则在
``<cache dir>/audio/<key>.<ext>`` 下保存音频，并按「单曲上限 / 总容量 / 条目数」
做 LRU 驱逐；默认关闭，配额 0 表示不限制（仅受条目数约束）。

索引结构::

    {
      "version": 1,
      "tracks":  {key: {track 字段..., "cached_at": epoch, "url_expires_at": epoch}},
      "queries": {qkey: {"fingerprint", "tracks": [key...], "expires_at": epoch}},
      "audio":   {key: {"filename", "bytes", "created_at", "last_hit_at"}}
    }
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.immersive.scenario_cache import safe_scenario_id
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.security import safe_join
from trpg_server.settings import SCENARIOS_DIR

logger = logging.getLogger(__name__)

ROOM_CACHE_DIR = "immersive_cache"
SCENARIO_CACHE_DIR = "immersive"
MUSIC_SUBDIR = "music"
AUDIO_SUBDIR = "audio"
INDEX_FILENAME = "index.json"

_SAFE_KEY_RE = re.compile(r"^[a-f0-9]{24,64}$")
_AUDIO_SUFFIXES = {"mp3", "ogg", "opus", "m4a", "wav", "webm", "flac"}

_LOCK = threading.RLock()


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def track_key(provider: str, track_id: str) -> str:
    raw = f"{str(provider or '').strip().lower()}:{str(track_id or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def query_key(
    *,
    profile: str,
    intensity: str,
    scene: str,
    tags: list[str] | tuple[str, ...] | None,
    providers: list[str] | tuple[str, ...] | None,
) -> str:
    """稳定查询键：档位/强度/场景词/标签/数据源参与，但顺序归一化。"""
    parts = [
        str(profile or "").strip().lower(),
        str(intensity or "").strip().lower(),
        re.sub(r"\s+", " ", str(scene or "").strip().lower())[:120],
        ",".join(sorted(str(tag or "").strip().lower() for tag in (tags or []) if str(tag or "").strip())),
        ",".join(sorted(str(item or "").strip().lower() for item in (providers or []))),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _safe_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_KEY_RE.match(text) else ""


def room_music_dir(room_dir: Path | str | None, *, for_write: bool = False) -> Path | None:
    if not room_dir:
        return None
    try:
        base = Path(room_dir).resolve()
        target = (base / ROOM_CACHE_DIR / MUSIC_SUBDIR).resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        logger.warning("external_music rejected unsafe room dir: %s", room_dir)
        return None
    return _ensure_dir(target, for_write)


def scenario_music_dir(
    scenario_id: str,
    *,
    for_write: bool = False,
    scenarios_dir: Path | str | None = None,
) -> Path | None:
    safe_id = safe_scenario_id(scenario_id)
    if not safe_id:
        return None
    base = Path(scenarios_dir) if scenarios_dir else Path(SCENARIOS_DIR)
    try:
        base = base.resolve()
        standard_parent = safe_join(base, f"scenario-{safe_id}")
        target = safe_join(base, f"scenario-{safe_id}", SCENARIO_CACHE_DIR, MUSIC_SUBDIR)
        if not for_write and not target.is_dir():
            # 旧式平铺剧本的回退目录，与 scenario_cache 的约定保持一致。
            fallback = safe_join(base, ".immersive", f"scenario-{safe_id}", SCENARIO_CACHE_DIR, MUSIC_SUBDIR)
            return fallback if fallback.is_dir() else None
        if for_write and not standard_parent.is_dir():
            target = safe_join(base, ".immersive", f"scenario-{safe_id}", SCENARIO_CACHE_DIR, MUSIC_SUBDIR)
        target.resolve().relative_to(base)
    except (OSError, ValueError):
        logger.warning("external_music rejected unsafe scenario dir: %s", scenario_id)
        return None
    return _ensure_dir(target, for_write)


def _ensure_dir(target: Path, for_write: bool) -> Path | None:
    if not target.is_dir():
        if not for_write:
            return None
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("external_music mkdir failed dir=%s", target, exc_info=True)
            return None
    return target


def _index_path(directory: Path) -> Path:
    return directory / INDEX_FILENAME


def load_index(directory: Path | None) -> dict[str, Any]:
    if directory is None:
        return {"version": 1, "tracks": {}, "queries": {}, "audio": {}}
    data = read_json(_index_path(directory), default={})
    if not isinstance(data, dict):
        data = {}
    result: dict[str, Any] = {"version": 1}
    for field in ("tracks", "queries", "audio"):
        value = data.get(field)
        result[field] = value if isinstance(value, dict) else {}
    return result


def save_index(directory: Path, index: dict[str, Any]) -> None:
    write_json_atomic(_index_path(directory), index)


def _prune_expired(index: dict[str, Any], now: float) -> int:
    removed = 0
    for key, entry in list(index["queries"].items()):
        if not isinstance(entry, dict):
            index["queries"].pop(key, None)
            removed += 1
            continue
        if float(entry.get("expires_at") or 0) <= now:
            index["queries"].pop(key, None)
            removed += 1
    return removed


def remember_track(
    directory: Path | None,
    track: dict[str, Any],
    *,
    url_ttl: int,
    now: float | None = None,
) -> str:
    """写入曲目元数据；返回 track_key（无法写入时也返回 key，方便上游继续广播）。"""
    key = _safe_key(track.get("track_key")) or track_key(
        str(track.get("provider") or ""), str(track.get("track_id") or "")
    )
    if directory is None or not key:
        return key
    stamp = time.time() if now is None else now
    entry = dict(track)
    entry["track_key"] = key
    entry["cached_at"] = stamp
    entry["url_expires_at"] = stamp + max(0, int(url_ttl))
    with _LOCK:
        index = load_index(directory)
        _prune_expired(index, stamp)
        previous = index["tracks"].get(key)
        if isinstance(previous, dict):
            entry["generated_at"] = previous.get("generated_at") or entry.get("cached_at")
        index["tracks"][key] = entry
        try:
            save_index(directory, index)
        except OSError:
            logger.warning("external_music index write failed dir=%s", directory, exc_info=True)
    return key


def get_track(directory: Path | None, key: str, *, max_age: int = 0) -> dict[str, Any] | None:
    safe_key = _safe_key(key)
    if directory is None or not safe_key:
        return None
    with _LOCK:
        index = load_index(directory)
        entry = index["tracks"].get(safe_key)
        if not isinstance(entry, dict):
            return None
        if max_age > 0 and float(entry.get("cached_at") or 0) + max_age < time.time():
            return None
        return dict(entry)


def remember_query(
    directory: Path | None,
    *,
    key: str,
    track_keys: list[str],
    fingerprint: str,
    ttl: int,
    meta: dict[str, Any] | None = None,
    now: float | None = None,
) -> None:
    """记录查询结果（空列表即负缓存），避免同场景反复打曲库。"""
    safe_key = _safe_key(key)
    if directory is None or not safe_key:
        return
    stamp = time.time() if now is None else now
    entry = {
        "fingerprint": str(fingerprint or "")[:200],
        "tracks": [item for item in track_keys if _safe_key(item)][:20],
        "created_at": stamp,
        "expires_at": stamp + max(0, int(ttl)),
    }
    if meta:
        entry["meta"] = {str(k): v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
    with _LOCK:
        index = load_index(directory)
        _prune_expired(index, stamp)
        index["queries"][safe_key] = entry
        try:
            save_index(directory, index)
        except OSError:
            logger.warning("external_music query write failed dir=%s", directory, exc_info=True)


def recent_track_keys(directory: Path | None, *, limit: int = 12) -> list[str]:
    """最近若干次查询命中过的曲目 key，用于给「刚刚播过」的曲子降权。"""
    if directory is None:
        return []
    with _LOCK:
        index = load_index(directory)
        rows = [
            entry
            for entry in index["queries"].values()
            if isinstance(entry, dict) and entry.get("tracks")
        ]
        rows.sort(key=lambda entry: float(entry.get("created_at") or 0), reverse=True)
        result: list[str] = []
        for entry in rows:
            for key in entry.get("tracks") or []:
                safe_key = _safe_key(key)
                if safe_key and safe_key not in result:
                    result.append(safe_key)
                if len(result) >= max(1, int(limit)):
                    return result
        return result


def get_query(directory: Path | None, key: str, *, now: float | None = None) -> dict[str, Any] | None:
    safe_key = _safe_key(key)
    if directory is None or not safe_key:
        return None
    stamp = time.time() if now is None else now
    with _LOCK:
        index = load_index(directory)
        entry = index["queries"].get(safe_key)
        if not isinstance(entry, dict):
            return None
        if float(entry.get("expires_at") or 0) <= stamp:
            return None
        return dict(entry)


# ---------------------------------------------------------------- 可选音频缓存

def audio_path(directory: Path | None, key: str) -> Path | None:
    safe_key = _safe_key(key)
    if directory is None or not safe_key:
        return None
    child = directory / AUDIO_SUBDIR
    try:
        resolved_child = child.resolve()
        resolved_child.relative_to(directory.resolve())
    except (OSError, ValueError):
        return None
    if not resolved_child.is_dir():
        return None
    for candidate in resolved_child.glob(f"{safe_key}.*"):
        if candidate.is_file() and candidate.suffix.lower().lstrip(".") in _AUDIO_SUFFIXES:
            return candidate
    return None


def store_audio(
    directory: Path | None,
    key: str,
    *,
    content: bytes,
    suffix: str,
    settings: dict[str, Any] | None,
) -> tuple[bool, str]:
    """写入可选音频缓存；默认配置下调用方根本不会走到这里。"""
    safe_key = _safe_key(key)
    normalized_suffix = str(suffix or "").lower().lstrip(".")
    if directory is None or not safe_key:
        return False, "invalid_target"
    if normalized_suffix not in _AUDIO_SUFFIXES or not content:
        return False, "invalid_content"
    cfg = settings if isinstance(settings, dict) else {}
    max_track_mb = _as_int(cfg.get("music_external_cache_max_track_mb"), 12, 1, 64)
    if len(content) > max_track_mb * 1024 * 1024:
        return False, "track_too_large"
    quota_mb = _as_int(cfg.get("music_external_cache_quota_mb"), 256, 0, 102400)
    max_entries = _as_int(cfg.get("music_external_cache_max_entries"), 60, 0, 1000)
    policy = str(cfg.get("music_external_cache_overflow") or "lru").strip().lower()
    if policy not in {"lru", "reject"}:
        policy = "lru"

    with _LOCK:
        index = load_index(directory)
        total = sum(int(item.get("bytes") or 0) for item in index["audio"].values() if isinstance(item, dict))
        count = len(index["audio"])
        over = (quota_mb > 0 and total + len(content) > quota_mb * 1024 * 1024) or (
            max_entries > 0 and count + 1 > max_entries
        )
        if over and policy == "reject":
            return False, "quota_exceeded"
        if over:
            _evict_audio(directory, index, need_bytes=(total + len(content)) - quota_mb * 1024 * 1024 if quota_mb > 0 else 0,
                          need_entries=max_entries - 1 if max_entries > 0 else count)
            total = sum(int(item.get("bytes") or 0) for item in index["audio"].values() if isinstance(item, dict))
            if quota_mb > 0 and total + len(content) > quota_mb * 1024 * 1024:
                return False, "quota_exceeded"
        audio_dir = directory / AUDIO_SUBDIR
        try:
            audio_dir.mkdir(parents=True, exist_ok=True)
            target = audio_dir / f"{safe_key}.{normalized_suffix}"
            target.resolve().relative_to(audio_dir.resolve())
            target.write_bytes(content)
        except (OSError, ValueError):
            logger.warning("external_music audio write failed key=%s", safe_key, exc_info=True)
            return False, "io_error"
        stamp = time.time()
        index["audio"][safe_key] = {
            "filename": target.name,
            "bytes": len(content),
            "created_at": stamp,
            "last_hit_at": stamp,
        }
        try:
            save_index(directory, index)
        except OSError:
            logger.warning("external_music audio index write failed key=%s", safe_key, exc_info=True)
            return False, "index_write_failed"
    return True, "written"


def touch_audio(directory: Path | None, key: str) -> None:
    safe_key = _safe_key(key)
    if directory is None or not safe_key:
        return
    with _LOCK:
        index = load_index(directory)
        entry = index["audio"].get(safe_key)
        if not isinstance(entry, dict):
            return
        entry["last_hit_at"] = time.time()
        try:
            save_index(directory, index)
        except OSError:
            logger.debug("external_music audio touch failed key=%s", safe_key, exc_info=True)


def _evict_audio(directory: Path, index: dict[str, Any], *, need_bytes: int, need_entries: int) -> None:
    audio_dir = directory / AUDIO_SUBDIR
    candidates = sorted(
        (
            (float(item.get("last_hit_at") or item.get("created_at") or 0), key)
            for key, item in index["audio"].items()
            if isinstance(item, dict)
        )
    )
    freed = 0
    remaining = len(index["audio"])
    for _, key in candidates:
        if freed >= max(0, need_bytes) and remaining <= max(0, need_entries):
            break
        entry = index["audio"].pop(key, None)
        remaining -= 1
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("filename") or "")
        if not name:
            continue
        try:
            path = (audio_dir / name).resolve()
            path.relative_to(audio_dir.resolve())
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
        freed += int(entry.get("bytes") or 0)


def stats(directory: Path | None) -> dict[str, Any]:
    if directory is None:
        return {"directory": "", "tracks": 0, "queries": 0, "audio_files": 0, "audio_bytes": 0}
    index = load_index(directory)
    audio_files = 0
    audio_bytes = 0
    for key, entry in index["audio"].items():
        if isinstance(entry, dict) and audio_path(directory, key) is not None:
            audio_files += 1
            audio_bytes += int(entry.get("bytes") or 0)
    return {
        "directory": str(directory),
        "tracks": len(index["tracks"]),
        "queries": len(index["queries"]),
        "audio_files": audio_files,
        "audio_bytes": audio_bytes,
    }
