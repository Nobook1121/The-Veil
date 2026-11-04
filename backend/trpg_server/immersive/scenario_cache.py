"""剧本配图缓存：按「剧本 + 场景指纹 + 平台/模型/尺寸」持久化并复用生成图。

目录约定（写入时优先落在剧本自己的目录，旧式平铺剧本则落到 ``.immersive`` 区）：

```
data/scenarios/scenario-<id>/immersive/
    index.json          # 元数据索引（key -> 生成时间/平台/模型/尺寸/指纹）
    <key>.png|jpg|webp|svg
data/scenarios/.immersive/scenario-<id>/immersive/
```

设计要点：
- 指纹只来自场景文本 + 档位/强度，找不到指纹的历史图片一律不参与命中（不猜测）；
- key 由「剧本 ID + 指纹 + 平台 + 模型 + 尺寸」共同哈希，不同平台/尺寸不会串图；
- 容量治理按单剧本配额；超限时按 ``lru`` 驱逐最旧条目，或按 ``reject`` 拒绝写入；
- 所有文件名只允许十六进制 key + 白名单后缀，剧本 ID 只允许字母数字/下划线/短横线，
  写入前再做一次 ``resolve`` 边界检查，阻断目录穿越。
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
from trpg_server.security import safe_join
from trpg_server.settings import SCENARIOS_DIR

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
CACHE_DIR_NAME = "immersive"
FALLBACK_ROOT_NAME = ".immersive"
_ALLOWED_SUFFIXES = {"svg", "png", "jpg", "jpeg", "webp"}
_SAFE_SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SAFE_KEY_RE = re.compile(r"^[a-f0-9]{24,64}$")

_LOCK = threading.RLock()


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def safe_scenario_id(value: Any) -> str:
    """剧本 ID 只允许字母、数字、下划线与短横线，避免路径穿越。"""
    text = str(value or "").strip()
    return text if _SAFE_SCENARIO_ID_RE.match(text) else ""


def scene_fingerprint(scene: str, profile: str, intensity: str) -> str:
    """场景/上下文指纹：归一化文本 + 档位 + 强度，供缓存命中与追溯使用。"""
    normalized = re.sub(r"\s+", " ", str(scene or "").strip()).lower()
    raw = f"{normalized}|{str(profile or '').strip().lower()}|{str(intensity or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cache_key(
    scenario_id: str,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    category: str = "",
) -> str:
    """稳定 key：不同剧本/平台/模型/尺寸/画幅类别组合互不命中。

    ``category`` 为空时保持旧 key 格式，保证已有场景图缓存继续可命中；
    NPC 头像传入 ``portrait``，避免与同尺寸的场景图串图。
    """
    parts = [
        str(scenario_id or ""),
        str(fingerprint or ""),
        str(platform or "").strip().lower(),
        str(model or "").strip().lower(),
        str(size or "").strip().lower(),
    ]
    if category:
        parts.append(str(category).strip().lower())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _base_dir(scenarios_dir: Path | str | None = None) -> Path:
    return Path(scenarios_dir) if scenarios_dir else Path(SCENARIOS_DIR)


def _candidate_dirs(scenario_id: str, scenarios_dir: Path | str | None = None) -> list[Path]:
    safe_id = safe_scenario_id(scenario_id)
    if not safe_id:
        return []
    base = _base_dir(scenarios_dir).resolve()
    standard = safe_join(base, f"scenario-{safe_id}", CACHE_DIR_NAME)
    fallback = safe_join(base, FALLBACK_ROOT_NAME, f"scenario-{safe_id}", CACHE_DIR_NAME)
    return [standard, fallback]


def cache_dir(
    scenario_id: str,
    *,
    for_write: bool = False,
    scenarios_dir: Path | str | None = None,
) -> Path | None:
    """返回该剧本的缓存目录；读取时只返回已存在的目录。"""
    safe_id = safe_scenario_id(scenario_id)
    if not safe_id:
        return None
    for candidate in _candidate_dirs(safe_id, scenarios_dir):
        if candidate.is_dir():
            return candidate
    if not for_write:
        return None
    base = _base_dir(scenarios_dir).resolve()
    standard_parent = safe_join(base, f"scenario-{safe_id}")
    target = (
        standard_parent / CACHE_DIR_NAME
        if standard_parent.is_dir()
        else safe_join(base, FALLBACK_ROOT_NAME, f"scenario-{safe_id}", CACHE_DIR_NAME)
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("scenario_cache mkdir failed scenario=%s", safe_id, exc_info=True)
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
        path = directory / filename
        path.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _entry_exists(directory: Path, entry: dict[str, Any]) -> bool:
    return _entry_path(directory, entry) is not None


def lookup(
    scenario_id: str,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    *,
    scenarios_dir: Path | str | None = None,
    category: str = "",
) -> dict[str, Any] | None:
    """按 key 命中剧本缓存；缺少指纹或文件缺失的历史条目不参与命中。

    带 ``category`` 时先按新 key 命中；未命中且类别为场景时回退旧 key，
    以兼容升级前写入的剧本场景图。
    """
    safe_id = safe_scenario_id(scenario_id)
    if not safe_id or not fingerprint:
        return None
    keys = [cache_key(safe_id, fingerprint, platform, model, size, category)]
    if category and str(category).strip().lower() == "scene":
        legacy = cache_key(safe_id, fingerprint, platform, model, size)
        if legacy not in keys:
            keys.append(legacy)
    for key in keys:
        found = _lookup_by_key(safe_id, key, fingerprint, platform, model, size, scenarios_dir)
        if found is not None:
            return found
    logger.info(
        "immersive_scenario_cache miss scenario=%s key=%s category=%s platform=%s model=%s size=%s",
        safe_id, keys[0], category or "-", platform, model, size,
    )
    return None


def _lookup_by_key(
    safe_id: str,
    key: str,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    scenarios_dir: Path | str | None,
) -> dict[str, Any] | None:
    for directory in _candidate_dirs(safe_id, scenarios_dir):
        if not directory.is_dir():
            continue
        index = _load_index(directory)
        entry = index["entries"].get(key)
        if not isinstance(entry, dict):
            continue
        # 旧数据兜底：缺少指纹/平台/尺寸时直接跳过，避免不同场景互相串图。
        if str(entry.get("fingerprint") or "") != fingerprint:
            logger.info(
                "immersive_scenario_cache miss scenario=%s key=%s reason=fingerprint_mismatch", safe_id, key
            )
            continue
        if str(entry.get("platform") or "").lower() != str(platform or "").lower():
            continue
        if str(entry.get("model") or "").lower() != str(model or "").lower():
            continue
        if str(entry.get("size") or "").lower() != str(size or "").lower():
            continue
        path = _entry_path(directory, entry)
        if path is None:
            logger.info(
                "immersive_scenario_cache miss scenario=%s key=%s reason=file_missing", safe_id, key
            )
            index["entries"].pop(key, None)
            try:
                _save_index(directory, index)
            except OSError:
                pass
            continue
        logger.info(
            "immersive_scenario_cache hit scenario=%s key=%s category=%s platform=%s model=%s size=%s",
            safe_id, key, entry.get("category") or "-", platform, model, size,
        )
        return {"key": key, "path": path, "entry": entry, "directory": directory}
    return None


def _total_bytes(index: dict[str, Any], directory: Path) -> int:
    total = 0
    for entry in index["entries"].values():
        if isinstance(entry, dict) and _entry_exists(directory, entry):
            total += int(entry.get("bytes") or 0)
    return total


def _remove_entry(directory: Path, key: str, entry: dict[str, Any]) -> bool:
    path = _entry_path(directory, entry)
    removed = False
    if path is not None:
        try:
            path.unlink()
            removed = True
        except OSError:
            logger.warning("scenario_cache unlink failed path=%s", path, exc_info=True)
    return removed


def store(
    scenario_id: str,
    *,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    content: bytes,
    suffix: str,
    scene: str,
    profile: str,
    intensity: str,
    settings: dict[str, Any],
    room_id: str = "",
    platform_name: str = "",
    width: int = 0,
    height: int = 0,
    degraded: bool = False,
    scenarios_dir: Path | str | None = None,
    category: str = "scene",
    origin: str = "scenario",
    source_room: str = "",
) -> tuple[bool, str, dict[str, Any] | None]:
    """写入剧本缓存；返回 ``(是否写入, 原因, 条目)``。"""
    safe_id = safe_scenario_id(scenario_id)
    if not safe_id:
        return False, "invalid_scenario_id", None
    if not fingerprint:
        return False, "missing_fingerprint", None
    if degraded:
        # 降级回落的程序化图不写剧本缓存，避免后续命中时把外部失败固化成默认结果。
        return False, "degraded_skip", None
    normalized_suffix = str(suffix or "").lower().lstrip(".")
    if normalized_suffix not in _ALLOWED_SUFFIXES:
        return False, "invalid_suffix", None
    if not content:
        return False, "empty_content", None

    max_image_mb = _as_int(settings.get("illustration_scenario_max_image_mb"), 8, 1, 64)
    max_image_bytes = max_image_mb * 1024 * 1024
    if len(content) > max_image_bytes:
        logger.warning(
            "immersive_scenario_cache write rejected scenario=%s reason=image_too_large bytes=%s limit=%s",
            safe_id, len(content), max_image_bytes,
        )
        return False, "image_too_large", None

    quota_mb = _as_int(settings.get("illustration_scenario_quota_mb"), 256, 0, 102400)
    quota_bytes = quota_mb * 1024 * 1024
    policy = str(settings.get("illustration_scenario_overflow") or "lru").strip().lower()
    if policy not in {"lru", "reject"}:
        policy = "lru"
    key = cache_key(safe_id, fingerprint, platform, model, size, category)

    with _LOCK:
        directory = cache_dir(safe_id, for_write=True, scenarios_dir=scenarios_dir)
        if directory is None:
            return False, "io_error", None
        index = _load_index(directory)
        entries = index["entries"]
        existing = entries.get(key) if isinstance(entries.get(key), dict) else None

        # 同 key 覆盖：先删除旧后缀文件（例如平台从 png 改成 webp）。
        if existing:
            old_path = _entry_path(directory, existing)
            if old_path is not None:
                try:
                    old_path.unlink()
                except OSError:
                    logger.warning("scenario_cache replace unlink failed path=%s", old_path, exc_info=True)

        total = _total_bytes(index, directory)
        if existing:
            total = max(0, total - int(existing.get("bytes") or 0))
        if quota_bytes > 0 and total + len(content) > quota_bytes:
            if policy == "reject":
                logger.warning(
                    "immersive_scenario_cache write rejected scenario=%s reason=quota_exceeded policy=reject "
                    "total=%s incoming=%s quota=%s", safe_id, total, len(content), quota_bytes,
                )
                return False, "quota_exceeded", None
            evicted = _evict_lru(directory, index, need_bytes=total + len(content) - quota_bytes)
            total = _total_bytes(index, directory)
            logger.info(
                "immersive_scenario_cache lru evicted=%s scenario=%s remaining_bytes=%s",
                evicted, safe_id, total,
            )
            if total + len(content) > quota_bytes:
                logger.warning(
                    "immersive_scenario_cache write rejected scenario=%s reason=quota_exceeded after_lru "
                    "total=%s incoming=%s quota=%s", safe_id, total, len(content), quota_bytes,
                )
                return False, "quota_exceeded", None

        filename = f"{key}.{normalized_suffix}"
        target = directory / filename
        try:
            target.resolve().relative_to(directory.resolve())
            _write_bytes(target, content)
        except (OSError, ValueError):
            logger.warning("scenario_cache write failed scenario=%s key=%s", safe_id, key, exc_info=True)
            return False, "io_error", None

        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "key": key,
            "filename": filename,
            "bytes": len(content),
            "created_at": created_at,
            "generated_at": created_at,
            "scenario_id": safe_id,
            "fingerprint": fingerprint,
            "scene": str(scene or "")[:200],
            "profile": str(profile or ""),
            "intensity": str(intensity or ""),
            "platform": str(platform or ""),
            "platform_name": str(platform_name or ""),
            "model": str(model or ""),
            "size": str(size or ""),
            "category": str(category or "scene").strip().lower(),
            "origin": str(origin or "scenario").strip().lower()[:16],
            "source_room": str(source_room or room_id or "")[:80],
            "room_id": str(room_id or ""),
            "width": int(width or 0),
            "height": int(height or 0),
            "suffix": normalized_suffix,
        }
        entries[key] = entry
        try:
            _save_index(directory, index)
        except OSError:
            logger.warning("scenario_cache index write failed scenario=%s key=%s", safe_id, key, exc_info=True)
            return False, "index_write_failed", entry
        logger.info(
            "immersive_scenario_cache write ok scenario=%s key=%s bytes=%s platform=%s model=%s size=%s",
            safe_id, key, len(content), platform, model, size,
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
                logger.debug("scenario_cache temp cleanup failed path=%s", temp_path)


def _evict_lru(directory: Path, index: dict[str, Any], *, need_bytes: int) -> int:
    """按创建时间（缺失时按文件 mtime）驱逐最旧条目，返回删除数量。"""
    candidates: list[tuple[float, str]] = []
    for key, entry in index["entries"].items():
        if not isinstance(entry, dict):
            continue
        path = _entry_path(directory, entry)
        stamp = _entry_timestamp(entry, path)
        candidates.append((stamp, key))
    candidates.sort()
    removed = 0
    freed = 0
    for _, key in candidates:
        if freed >= need_bytes:
            break
        entry = index["entries"].get(key)
        if not isinstance(entry, dict):
            continue
        size = int(entry.get("bytes") or 0)
        if _remove_entry(directory, key, entry):
            freed += size
            removed += 1
        index["entries"].pop(key, None)
    if removed:
        try:
            _save_index(directory, index)
        except OSError:
            logger.warning("scenario_cache index write failed during eviction", exc_info=True)
    return removed


def _entry_timestamp(entry: dict[str, Any], path: Path | None) -> float:
    for key in ("created_at", "generated_at"):
        text = str(entry.get(key) or "")
        if text:
            try:
                return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
    if path is not None:
        try:
            return path.stat().st_mtime
        except OSError:
            pass
    return 0.0


def _iter_dirs(scenarios_dir: Path | str | None = None) -> list[tuple[str, Path]]:
    base = _base_dir(scenarios_dir).resolve()
    found: dict[str, Path] = {}
    try:
        for child in base.iterdir():
            if child.is_dir() and child.name.startswith("scenario-"):
                candidate = child / CACHE_DIR_NAME
                if candidate.is_dir():
                    found[child.name[len("scenario-"):]] = candidate
        fallback_root = base / FALLBACK_ROOT_NAME
        if fallback_root.is_dir():
            for child in fallback_root.iterdir():
                if child.is_dir() and child.name.startswith("scenario-"):
                    candidate = child / CACHE_DIR_NAME
                    if candidate.is_dir():
                        found.setdefault(child.name[len("scenario-"):], candidate)
    except OSError:
        logger.warning("scenario_cache enumerate failed", exc_info=True)
    return sorted(found.items())


def stats(scenario_id: str = "", *, scenarios_dir: Path | str | None = None) -> dict[str, Any]:
    """统计单剧本或全站剧本缓存（含未入索引的历史文件）。"""
    targets: list[tuple[str, Path]]
    safe_id = safe_scenario_id(scenario_id)
    if scenario_id:
        if not safe_id:
            return {"scenarios": [], "total_files": 0, "total_bytes": 0}
        targets = [(safe_id, d) for d in _candidate_dirs(safe_id, scenarios_dir) if d.is_dir()][:1]
    else:
        targets = _iter_dirs(scenarios_dir)
    rows: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0
    for sid, directory in targets:
        index = _load_index(directory)
        tracked = 0
        tracked_bytes = 0
        untracked = 0
        untracked_bytes = 0
        for entry in index["entries"].values():
            if isinstance(entry, dict) and _entry_exists(directory, entry):
                tracked += 1
                tracked_bytes += int(entry.get("bytes") or 0)
        try:
            for child in directory.iterdir():
                if child.is_file() and child.name != INDEX_FILENAME and child.suffix.lower().lstrip(".") in _ALLOWED_SUFFIXES:
                    if str(child.name).rpartition(".")[0] not in index["entries"]:
                        untracked += 1
                        untracked_bytes += child.stat().st_size
        except OSError:
            pass
        files = tracked + untracked
        size = tracked_bytes + untracked_bytes
        total_files += files
        total_bytes += size
        rows.append(
            {
                "scenario_id": sid,
                "directory": str(directory),
                "files": files,
                "bytes": size,
                "tracked_files": tracked,
                "untracked_files": untracked,
                "untracked_bytes": untracked_bytes,
            }
        )
    return {"scenarios": rows, "total_files": total_files, "total_bytes": total_bytes}


def clear(
    scenario_id: str = "",
    *,
    scenarios_dir: Path | str | None = None,
) -> tuple[int, int]:
    """删除单剧本或全站缓存文件与索引；返回（文件数，字节数）。"""
    safe_id = safe_scenario_id(scenario_id)
    if scenario_id and not safe_id:
        return 0, 0
    targets = (
        [(safe_id, d) for d in _candidate_dirs(safe_id, scenarios_dir) if d.is_dir()]
        if scenario_id
        else _iter_dirs(scenarios_dir)
    )
    removed_files = 0
    removed_bytes = 0
    for _, directory in targets:
        try:
            for child in directory.iterdir():
                if child.is_file() and child.name != INDEX_FILENAME and child.suffix.lower().lstrip(".") in _ALLOWED_SUFFIXES:
                    removed_bytes += child.stat().st_size
                    child.unlink(missing_ok=True)
                    removed_files += 1
        except OSError:
            logger.warning("scenario_cache clear failed dir=%s", directory, exc_info=True)
        try:
            _index_path(directory).unlink(missing_ok=True)
        except OSError:
            logger.warning("scenario_cache index clear failed dir=%s", directory, exc_info=True)
    logger.info(
        "immersive_scenario_cache cleared scenario=%s files=%s bytes=%s",
        safe_id or "all", removed_files, removed_bytes,
    )
    return removed_files, removed_bytes


def prune(
    *,
    scenario_id: str = "",
    settings: dict[str, Any],
    scenarios_dir: Path | str | None = None,
) -> tuple[int, int]:
    """按当前配额做一次 LRU 清理；返回（删除文件数，释放字节数）。"""
    quota_mb = _as_int(settings.get("illustration_scenario_quota_mb"), 256, 0, 102400)
    quota_bytes = quota_mb * 1024 * 1024
    if quota_bytes <= 0:
        return 0, 0
    safe_id = safe_scenario_id(scenario_id)
    if scenario_id and not safe_id:
        return 0, 0
    targets = (
        [(safe_id, d) for d in _candidate_dirs(safe_id, scenarios_dir) if d.is_dir()][:1]
        if scenario_id
        else _iter_dirs(scenarios_dir)
    )
    removed = 0
    freed = 0
    for _, directory in targets:
        index = _load_index(directory)
        total = _total_bytes(index, directory)
        if total <= quota_bytes:
            continue
        need = total - quota_bytes
        before = index["entries"].copy()
        count = _evict_lru(directory, index, need_bytes=need)
        removed += count
        for key, entry in before.items():
            if key not in index["entries"] and isinstance(entry, dict):
                freed += int(entry.get("bytes") or 0)
    return removed, freed
