"""房间级配图缓存：图片与索引都落在房间目录内，可随房间归档保留。

与剧本缓存（``scenario_cache``）的区别：

- 作用域只在本房间，用于场景/地点来回切换时直接复用，不重复扣生图预算；
- 目录固定在 ``<room_dir>/immersive_cache/``，**不写进程临时目录**；
- key 只接受十六进制摘要，文件名后缀白名单，读写前都做 ``resolve`` 边界检查；
- 单图大小、单房间配额与条目数上限可配，超限按 ``lru`` 驱逐或 ``reject`` 拒绝；
- 命中会累计 ``hits`` 与 ``last_hit_at``，供控制台审计与费用核算。
"""
from __future__ import annotations

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
_ALLOWED_SUFFIXES = {"svg", "png", "jpg", "jpeg", "webp"}
_SAFE_KEY_RE = re.compile(r"^[a-f0-9]{24,64}$")

_LOCK = threading.RLock()


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_KEY_RE.match(text) else ""


def cache_dir(room_dir: Path | str | None, *, for_write: bool = False) -> Path | None:
    """返回房间缓存目录；拒绝解析到房间目录之外的路径。"""
    if not room_dir:
        return None
    try:
        base = Path(room_dir).resolve()
        target = (base / CACHE_DIR_NAME).resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        logger.warning("room_cache rejected unsafe room dir: %s", room_dir)
        return None
    if not target.is_dir():
        if not for_write:
            return None
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("room_cache mkdir failed dir=%s", target, exc_info=True)
            return None
    return target


def _index_path(directory: Path) -> Path:
    return directory / INDEX_FILENAME


def _load_index(directory: Path) -> dict[str, Any]:
    data = read_json(_index_path(directory), default={})
    if not isinstance(data, dict):
        data = {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"version": 1, "entries": entries}


def _save_index(directory: Path, index: dict[str, Any]) -> None:
    write_json_atomic(_index_path(directory), index)


def _entry_path(directory: Path, entry: dict[str, Any]) -> Path | None:
    filename = str(entry.get("filename") or "")
    stem, _, suffix = filename.rpartition(".")
    if not stem or suffix.lower() not in _ALLOWED_SUFFIXES or not _SAFE_KEY_RE.match(stem):
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


def _total_bytes(index: dict[str, Any], directory: Path) -> int:
    return sum(
        int(entry.get("bytes") or 0)
        for entry in index["entries"].values()
        if isinstance(entry, dict) and _entry_path(directory, entry) is not None
    )


def lookup(
    room_dir: Path | str | None,
    key: str,
    *,
    fingerprint: str = "",
    platform: str = "",
    model: str = "",
    size: str = "",
    category: str = "",
) -> dict[str, Any] | None:
    """按 key + 元数据命中房间缓存；元数据不一致视为未命中，避免串图。"""
    safe_key = _safe_key(key)
    directory = cache_dir(room_dir)
    if not safe_key or directory is None:
        return None
    with _LOCK:
        index = _load_index(directory)
        entry = index["entries"].get(safe_key)
        if not isinstance(entry, dict):
            logger.info("immersive_room_cache miss key=%s reason=no_entry", safe_key)
            return None
        checks = (
            ("fingerprint", fingerprint),
            ("platform", platform),
            ("model", model),
            ("size", size),
            ("category", category),
        )
        for field, expected in checks:
            if expected and str(entry.get(field) or "").lower() != str(expected).lower():
                logger.info(
                    "immersive_room_cache miss key=%s reason=%s_mismatch", safe_key, field
                )
                return None
        path = _entry_path(directory, entry)
        if path is None:
            logger.info("immersive_room_cache miss key=%s reason=file_missing", safe_key)
            index["entries"].pop(safe_key, None)
            try:
                _save_index(directory, index)
            except OSError:
                pass
            return None
        entry["hits"] = int(entry.get("hits") or 0) + 1
        entry["last_hit_at"] = _now()
        try:
            _save_index(directory, index)
        except OSError:
            logger.debug("room_cache hit bookkeeping failed key=%s", safe_key, exc_info=True)
        logger.info(
            "immersive_room_cache hit key=%s category=%s scene=%s npc=%s",
            safe_key, entry.get("category") or "-", entry.get("scene") or "-",
            entry.get("npc_name") or "-",
        )
        return {"key": safe_key, "path": path, "entry": entry, "directory": directory}


def _remove_entry(directory: Path, entry: dict[str, Any]) -> int:
    path = _entry_path(directory, entry)
    if path is None:
        return 0
    size = int(entry.get("bytes") or 0)
    try:
        path.unlink()
    except OSError:
        logger.warning("room_cache unlink failed path=%s", path, exc_info=True)
        return 0
    return size


def _evict_lru(directory: Path, index: dict[str, Any], *, need_bytes: int, need_entries: int) -> int:
    candidates = sorted(
        (
            (_timestamp(entry), key)
            for key, entry in index["entries"].items()
            if isinstance(entry, dict)
        )
    )
    removed = 0
    freed = 0
    remaining = len(index["entries"])
    for _, key in candidates:
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
            logger.warning("room_cache index write failed during eviction", exc_info=True)
    return removed


def store(
    room_dir: Path | str | None,
    key: str,
    *,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    category: str,
    content: bytes,
    suffix: str,
    scene: str = "",
    npc_name: str = "",
    provider: str = "",
    platform_id: str = "",
    width: int = 0,
    height: int = 0,
    source_room: str = "",
    settings: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """写入房间缓存；返回 ``(是否写入, 原因, 条目)``。"""
    safe_key = _safe_key(key)
    if not safe_key:
        return False, "invalid_key", None
    directory = cache_dir(room_dir, for_write=True)
    if directory is None:
        return False, "io_error", None
    normalized_suffix = str(suffix or "").lower().lstrip(".")
    if normalized_suffix not in _ALLOWED_SUFFIXES:
        return False, "invalid_suffix", None
    if not content:
        return False, "empty_content", None
    cfg = settings if isinstance(settings, dict) else {}
    max_image_mb = _as_int(cfg.get("illustration_room_cache_max_image_mb"), 8, 1, 64)
    if len(content) > max_image_mb * 1024 * 1024:
        logger.warning("room_cache write rejected reason=image_too_large key=%s bytes=%s", safe_key, len(content))
        return False, "image_too_large", None
    quota_mb = _as_int(cfg.get("illustration_room_cache_quota_mb"), 64, 0, 1024)
    quota_bytes = quota_mb * 1024 * 1024
    max_entries = _as_int(cfg.get("illustration_room_cache_max_entries"), 40, 0, 200)
    policy = str(cfg.get("illustration_room_cache_overflow") or "lru").strip().lower()
    if policy not in {"lru", "reject"}:
        policy = "lru"

    with _LOCK:
        index = _load_index(directory)
        entries = index["entries"]
        existing = entries.get(safe_key) if isinstance(entries.get(safe_key), dict) else None
        if existing:
            _remove_entry(directory, existing)
        total = _total_bytes(index, directory)
        count = len(entries)
        over_budget = (quota_bytes > 0 and total + len(content) > quota_bytes) or (
            max_entries > 0 and count + 1 > max_entries
        )
        if over_budget and policy == "reject":
            logger.warning(
                "room_cache write rejected reason=quota_exceeded key=%s total=%s count=%s",
                safe_key, total, count,
            )
            return False, "quota_exceeded", None
        if over_budget:
            _evict_lru(
                directory,
                index,
                need_bytes=max(0, total + len(content) - quota_bytes) if quota_bytes > 0 else 0,
                need_entries=max_entries - 1 if max_entries > 0 else len(entries),
            )
            total = _total_bytes(index, directory)
            count = len(entries)
            if quota_bytes > 0 and total + len(content) > quota_bytes:
                return False, "quota_exceeded", None
            if max_entries > 0 and count + 1 > max_entries:
                return False, "quota_exceeded", None

        filename = f"{safe_key}.{normalized_suffix}"
        target = directory / filename
        try:
            target.resolve().relative_to(directory.resolve())
            _write_bytes(target, content)
        except (OSError, ValueError):
            logger.warning("room_cache write failed key=%s", safe_key, exc_info=True)
            return False, "io_error", None
        now = _now()
        entry = {
            "key": safe_key,
            "filename": filename,
            "bytes": len(content),
            "created_at": now,
            "last_hit_at": "",
            "hits": 0,
            "scene": str(scene or "")[:200],
            "npc_name": str(npc_name or "")[:64],
            "category": str(category or "scene").strip().lower(),
            "fingerprint": str(fingerprint or ""),
            "platform": str(platform or ""),
            "model": str(model or ""),
            "size": str(size or ""),
            "provider": str(provider or ""),
            "platform_id": str(platform_id or ""),
            "width": int(width or 0),
            "height": int(height or 0),
            "source_room": str(source_room or "")[:80],
        }
        entries[safe_key] = entry
        try:
            _save_index(directory, index)
        except OSError:
            logger.warning("room_cache index write failed key=%s", safe_key, exc_info=True)
            return False, "index_write_failed", entry
        logger.info(
            "immersive_room_cache write key=%s category=%s bytes=%s platform=%s model=%s size=%s",
            safe_key, entry["category"], len(content), platform, model, size,
        )
        return True, "written", entry


def _write_bytes(path: Path, content: bytes) -> None:
    temp_file = tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False, suffix=".tmp")
    temp_path = Path(temp_file.name)
    try:
        try:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        finally:
            temp_file.close()
        replace_with_retry(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.debug("room_cache temp cleanup failed path=%s", temp_path)


def stats(room_dir: Path | str | None) -> dict[str, Any]:
    directory = cache_dir(room_dir)
    if directory is None:
        return {"entries": 0, "files": 0, "bytes": 0, "hits": 0, "last_hit_at": ""}
    index = _load_index(directory)
    files = 0
    total_bytes = 0
    hits = 0
    last_hit = ""
    for entry in index["entries"].values():
        if not isinstance(entry, dict):
            continue
        path = _entry_path(directory, entry)
        if path is not None:
            files += 1
            total_bytes += int(entry.get("bytes") or 0)
        hits += int(entry.get("hits") or 0)
        stamp = str(entry.get("last_hit_at") or "")
        if stamp > last_hit:
            last_hit = stamp
    return {
        "entries": len(index["entries"]),
        "files": files,
        "bytes": total_bytes,
        "hits": hits,
        "last_hit_at": last_hit,
        "directory": str(directory),
    }


def clear(room_dir: Path | str | None) -> tuple[int, int]:
    """删除本房间缓存目录内的图片与索引；绝不触碰剧本目录。"""
    directory = cache_dir(room_dir)
    if directory is None:
        return 0, 0
    removed_files = 0
    removed_bytes = 0
    try:
        for child in directory.iterdir():
            if not child.is_file():
                continue
            if child.name != INDEX_FILENAME and child.suffix.lower().lstrip(".") not in _ALLOWED_SUFFIXES:
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
        logger.warning("room_cache clear failed dir=%s", directory, exc_info=True)
    logger.info("immersive_room_cache cleared dir=%s files=%s bytes=%s", directory, removed_files, removed_bytes)
    return removed_files, removed_bytes


def prune(room_dir: Path | str | None, settings: dict[str, Any]) -> tuple[int, int]:
    """按当前单房间配额/条目上限做一次 LRU 清理。"""
    directory = cache_dir(room_dir, for_write=True)
    if directory is None:
        return 0, 0
    cfg = settings if isinstance(settings, dict) else {}
    quota_mb = _as_int(cfg.get("illustration_room_cache_quota_mb"), 64, 0, 1024)
    max_entries = _as_int(cfg.get("illustration_room_cache_max_entries"), 40, 0, 200)
    if quota_mb <= 0 and max_entries <= 0:
        return 0, 0
    with _LOCK:
        index = _load_index(directory)
        total = _total_bytes(index, directory)
        count = len(index["entries"])
        need_bytes = total - quota_mb * 1024 * 1024 if quota_mb > 0 else 0
        need_entries = max_entries if max_entries > 0 else count
        if need_bytes <= 0 and count <= max_entries:
            return 0, 0
        before = {key: int(entry.get("bytes") or 0) for key, entry in index["entries"].items() if isinstance(entry, dict)}
        removed = _evict_lru(
            directory, index, need_bytes=max(0, need_bytes), need_entries=need_entries
        )
        freed = sum(size for key, size in before.items() if key not in index["entries"])
        return removed, freed
