"""本地 TTS 音频缓存：按房间落盘、LRU / reject 两种超限策略。

缓存 key 固定为 ``sha256(engine | model | voice_id | speed | format | normalized_text)``，
文本归一化只处理首尾空白与连续空白，保留标点，避免把不同台词误判为同一条。

设计约定：
- 目录固定在 ``<room_dir>/immersive_cache/voice/``，随房间归档保留，绝不写到进程临时目录；
- 写临时文件后原子替换，降级或空音频不写缓存（由调用方保证）；
- 索引记录 ``bytes`` / ``hits`` / ``last_hit_at``，供控制台统计与 LRU 驱逐。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, replace_with_retry, write_json_atomic

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
CACHE_DIR_NAME = "immersive_cache"
SUBDIR_NAME = "voice"
ALLOWED_SUFFIXES = {"wav", "mp3", "ogg"}
_SAFE_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
_LOCK = threading.RLock()

_SPACES = re.compile(r"\s+")


def normalize_text(text: Any) -> str:
    """仅归一空白：strip + 连续空白折叠，标点原样保留。"""
    return _SPACES.sub(" ", str(text or "")).strip()


def cache_key(
    text: Any,
    voice_id: Any,
    speed: Any,
    *,
    engine: str = "local",
    model: str = "",
    fmt: str = "wav",
) -> str:
    normalized = normalize_text(text)
    raw = "|".join(
        (
            str(engine or "local").strip().lower(),
            str(model or "").strip(),
            str(voice_id).strip(),
            f"{float(speed or 1):.3f}",
            str(fmt or "wav").strip().lower(),
            normalized,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_dir(room_dir: Path | str | None, *, for_write: bool = False) -> Path | None:
    if not room_dir:
        return None
    try:
        base = Path(room_dir).resolve()
        target = (base / CACHE_DIR_NAME / SUBDIR_NAME).resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        logger.warning("voice_cache rejected unsafe room dir: %s", room_dir)
        return None
    if not target.is_dir():
        if not for_write:
            return None
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("voice_cache mkdir failed dir=%s", target, exc_info=True)
            return None
    return target


def _safe_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_KEY_RE.match(text) else ""


def _index_path(directory: Path) -> Path:
    return directory / INDEX_FILENAME


def _load_index(directory: Path) -> dict[str, Any]:
    data = read_json(_index_path(directory), default={})
    entries = data.get("entries") if isinstance(data, dict) else {}
    return {
        "version": 1,
        "entries": entries if isinstance(entries, dict) else {},
        "next_seq": int(data.get("next_seq") or 0) if isinstance(data, dict) else 0,
    }


def _save_index(directory: Path, index: dict[str, Any]) -> None:
    write_json_atomic(_index_path(directory), index)


def _entry_path(directory: Path, entry: dict[str, Any]) -> Path | None:
    filename = str(entry.get("filename") or "")
    stem, _, suffix = filename.rpartition(".")
    if not stem or suffix.lower() not in ALLOWED_SUFFIXES or not _SAFE_KEY_RE.match(stem):
        return None
    try:
        path = (directory / filename).resolve()
        path.relative_to(directory.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(entry: dict[str, Any]) -> float:
    for key in ("last_hit_at", "created_at"):
        text = str(entry.get(key) or "")
        if text:
            try:
                return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
    return 0.0


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _limits(settings: dict[str, Any] | None) -> tuple[int, int, str]:
    cfg = settings if isinstance(settings, dict) else {}
    quota_mb = _as_int(cfg.get("cache_quota_mb", cfg.get("voice_cache_quota_mb")), 64, 0, 1024)
    max_entries = _as_int(cfg.get("cache_max_entries", cfg.get("voice_cache_max_entries")), 300, 0, 10000)
    policy = str(cfg.get("cache_overflow", cfg.get("voice_cache_overflow")) or "lru").strip().lower()
    if policy not in {"lru", "reject"}:
        policy = "lru"
    return quota_mb, max_entries, policy


def lookup(room_dir: Path | str | None, key: str) -> dict[str, Any] | None:
    safe_key = _safe_key(key)
    directory = cache_dir(room_dir)
    if not safe_key or directory is None:
        return None
    with _LOCK:
        index = _load_index(directory)
        entry = index["entries"].get(safe_key)
        if not isinstance(entry, dict):
            return None
        path = _entry_path(directory, entry)
        if path is None:
            index["entries"].pop(safe_key, None)
            try:
                _save_index(directory, index)
            except OSError:
                pass
            return None
        entry["hits"] = int(entry.get("hits") or 0) + 1
        entry["last_hit_at"] = _now()
        index["next_seq"] = int(index.get("next_seq") or 0) + 1
        entry["last_hit_seq"] = index["next_seq"]
        try:
            _save_index(directory, index)
        except OSError:
            logger.debug("voice_cache hit bookkeeping failed key=%s", safe_key, exc_info=True)
        return {"key": safe_key, "path": path, "entry": entry, "directory": directory}


def _remove_entry(directory: Path, entry: dict[str, Any]) -> int:
    path = _entry_path(directory, entry)
    if path is None:
        return 0
    size = int(entry.get("bytes") or 0)
    try:
        path.unlink()
    except OSError:
        return 0
    return size


def _evict_lru(directory: Path, index: dict[str, Any], *, need_bytes: int, need_entries: int) -> int:
    # 时间戳只精确到秒，同秒写入时用单调递增的 seq 保证真正的「最久未使用」顺序。
    candidates = sorted(
        (
            int(entry.get("last_hit_seq") or entry.get("seq") or 0),
            _timestamp(entry),
            key,
        )
        for key, entry in index["entries"].items()
        if isinstance(entry, dict)
    )
    removed = 0
    freed = 0
    remaining = len(index["entries"])
    for _, _, key in candidates:
        if freed >= need_bytes and remaining <= need_entries:
            break
        entry = index["entries"].get(key)
        if not isinstance(entry, dict):
            continue
        freed += _remove_entry(directory, entry)
        index["entries"].pop(key, None)
        remaining -= 1
        removed += 1
    if removed:
        try:
            _save_index(directory, index)
        except OSError:
            logger.warning("voice_cache index write failed during eviction", exc_info=True)
    return removed


def store(
    room_dir: Path | str | None,
    key: str,
    *,
    content: bytes,
    voice_id: str,
    model: str,
    suffix: str = "wav",
    latency_ms: float = 0.0,
    audio_ms: float = 0.0,
    text_chars: int = 0,
    settings: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    safe_key = _safe_key(key)
    if not safe_key:
        return False, "invalid_key", None
    if not content:
        return False, "empty_audio", None
    normalized_suffix = str(suffix or "wav").lower().lstrip(".")
    if normalized_suffix not in ALLOWED_SUFFIXES:
        return False, "invalid_suffix", None
    directory = cache_dir(room_dir, for_write=True)
    if directory is None:
        return False, "io_error", None
    quota_mb, max_entries, policy = _limits(settings)
    quota_bytes = quota_mb * 1024 * 1024

    with _LOCK:
        index = _load_index(directory)
        entries = index["entries"]
        existing = entries.get(safe_key) if isinstance(entries.get(safe_key), dict) else None
        if existing:
            _remove_entry(directory, existing)
        total = sum(
            int(entry.get("bytes") or 0)
            for entry in entries.values()
            if isinstance(entry, dict) and _entry_path(directory, entry) is not None
        )
        count = len(entries)
        over_budget = (quota_bytes > 0 and total + len(content) > quota_bytes) or (
            max_entries > 0 and count + 1 > max_entries
        )
        if over_budget and policy == "reject":
            logger.info("voice_cache write rejected reason=quota_exceeded key=%s", safe_key)
            return False, "quota_exceeded", None
        if over_budget:
            removed = _evict_lru(
                directory,
                index,
                need_bytes=max(0, total + len(content) - quota_bytes) if quota_bytes > 0 else 0,
                need_entries=max_entries - 1 if max_entries > 0 else len(entries),
            )
            if removed:
                logger.info("voice_cache_evict count=%s policy=lru key=%s", removed, safe_key)
        filename = f"{safe_key}.{normalized_suffix}"
        target = directory / filename
        try:
            _write_bytes(target, content)
        except (OSError, ValueError):
            logger.warning("voice_cache write failed key=%s", safe_key, exc_info=True)
            return False, "io_error", None
        now = _now()
        index["next_seq"] = int(index.get("next_seq") or 0) + 1
        entry = {
            "key": safe_key,
            "filename": filename,
            "bytes": len(content),
            "created_at": now,
            "seq": index["next_seq"],
            "last_hit_at": "",
            "hits": 0,
            "voice_id": str(voice_id or ""),
            "model": str(model or ""),
            "latency_ms": round(float(latency_ms or 0), 1),
            "audio_ms": round(float(audio_ms or 0), 1),
            "text_chars": int(text_chars or 0),
        }
        entries[safe_key] = entry
        try:
            _save_index(directory, index)
        except OSError:
            return False, "index_write_failed", entry
        return True, "written", entry


def _write_bytes(path: Path, content: bytes) -> None:
    handle = tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False, suffix=".tmp")
    temp = Path(handle.name)
    try:
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        replace_with_retry(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def stats(room_dir: Path | str | None) -> dict[str, Any]:
    directory = cache_dir(room_dir)
    if directory is None:
        return {"entries": 0, "files": 0, "bytes": 0, "hits": 0, "last_hit_at": "", "directory": ""}
    index = _load_index(directory)
    files = 0
    total = 0
    hits = 0
    last_hit = ""
    for entry in index["entries"].values():
        if not isinstance(entry, dict):
            continue
        if _entry_path(directory, entry) is not None:
            files += 1
            total += int(entry.get("bytes") or 0)
        hits += int(entry.get("hits") or 0)
        stamp = str(entry.get("last_hit_at") or "")
        if stamp > last_hit:
            last_hit = stamp
    return {
        "entries": len(index["entries"]),
        "files": files,
        "bytes": total,
        "hits": hits,
        "last_hit_at": last_hit,
        "directory": str(directory),
    }


def clear(room_dir: Path | str | None) -> tuple[int, int]:
    directory = cache_dir(room_dir)
    if directory is None:
        return 0, 0
    removed_files = 0
    removed_bytes = 0
    try:
        for child in directory.iterdir():
            if not child.is_file():
                continue
            if child.name != INDEX_FILENAME and child.suffix.lower().lstrip(".") not in ALLOWED_SUFFIXES:
                continue
            try:
                size = child.stat().st_size if child.name != INDEX_FILENAME else 0
                child.unlink()
            except OSError:
                continue
            if child.name != INDEX_FILENAME:
                removed_files += 1
                removed_bytes += size
    except OSError:
        logger.warning("voice_cache clear failed dir=%s", directory, exc_info=True)
    logger.info("voice_cache cleared dir=%s files=%s bytes=%s", directory, removed_files, removed_bytes)
    return removed_files, removed_bytes


def prune(room_dir: Path | str | None, settings: dict[str, Any] | None) -> tuple[int, int]:
    directory = cache_dir(room_dir, for_write=True)
    if directory is None:
        return 0, 0
    quota_mb, max_entries, _ = _limits(settings)
    if quota_mb <= 0 and max_entries <= 0:
        return 0, 0
    with _LOCK:
        index = _load_index(directory)
        total = sum(
            int(entry.get("bytes") or 0)
            for entry in index["entries"].values()
            if isinstance(entry, dict) and _entry_path(directory, entry) is not None
        )
        count = len(index["entries"])
        need_bytes = total - quota_mb * 1024 * 1024 if quota_mb > 0 else 0
        need_entries = max_entries if max_entries > 0 else count
        if need_bytes <= 0 and count <= max_entries:
            return 0, 0
        before = {
            key: int(entry.get("bytes") or 0)
            for key, entry in index["entries"].items()
            if isinstance(entry, dict)
        }
        removed = _evict_lru(directory, index, need_bytes=max(0, need_bytes), need_entries=need_entries)
        freed = sum(size for key, size in before.items() if key not in index["entries"])
        if removed:
            logger.info("voice_cache prune removed=%s freed=%s", removed, freed)
        return removed, freed
