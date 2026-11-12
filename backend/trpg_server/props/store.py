"""房间级道具存储（设计稿 §3.2）。

布局::

    data/runtime/rooms/<room_id>/props/
      index.json                     # 轻量索引
      <prop_id>.json                 # 单条 PropSpec
      <prop_id>/photo.png            # 该道具的位图资产（可选）
      renders/<prop_id>@<hash>.pdf   # 导出缓存

``index.json`` 与单条 spec 都走 :func:`trpg_server.json_store.write_json_atomic`；
写入「spec → 重建 index」之间的交错由每房间一把重入锁挡掉。所有路径都经
:func:`trpg_server.security.safe_join`，``prop_id`` 先过正则白名单再拼路径。
"""
from __future__ import annotations

import copy
import logging
import random
import re
import string
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.json_store import remove_tree, write_json_atomic
from trpg_server.security import safe_join

logger = logging.getLogger(__name__)

PROP_ID_RE = re.compile(r"^prop-\d+-[a-z0-9]{6}$")
INDEX_FILENAME = "index.json"
RENDERS_DIRNAME = "renders"
_ALPHABET = string.ascii_lowercase + string.digits
_ID_RANDOM = random.SystemRandom()

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class PropStoreError(RuntimeError):
    """存储层可读错误（路径非法、spec 损坏等）。"""


# ---------------------------------------------------------------- 路径


def props_root(room_dir: str | Path) -> Path:
    try:
        return safe_join(room_dir, "props")
    except ValueError as exc:
        raise PropStoreError("unsafe room path") from exc


def index_path(room_dir: str | Path) -> Path:
    return safe_join(props_root(room_dir), INDEX_FILENAME)


def prop_path(room_dir: str | Path, prop_id: str) -> Path:
    return safe_join(props_root(room_dir), f"{_require_prop_id(prop_id)}.json")


def assets_dir(room_dir: str | Path, prop_id: str) -> Path:
    return safe_join(props_root(room_dir), _require_prop_id(prop_id))


def renders_dir(room_dir: str | Path) -> Path:
    return safe_join(props_root(room_dir), RENDERS_DIRNAME)


def render_path(room_dir: str | Path, prop_id: str, spec_hash: str, ext: str) -> Path:
    suffix = str(ext or "pdf").strip().lstrip(".").lower()
    return safe_join(
        renders_dir(room_dir),
        f"{_require_prop_id(prop_id)}@{_require_hash(spec_hash)}.{suffix}",
    )


def is_valid_prop_id(prop_id: Any) -> bool:
    return bool(PROP_ID_RE.match(str(prop_id or "").strip()))


def _require_prop_id(prop_id: Any) -> str:
    value = str(prop_id or "").strip()
    if not PROP_ID_RE.match(value):
        raise PropStoreError(f"invalid prop id: {value!r}")
    return value


def _require_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not re.match(r"^[a-f0-9]{8,64}$", text):
        raise PropStoreError("invalid spec hash")
    return text


def new_prop_id(now: float | None = None) -> str:
    stamp = int((time.time() if now is None else now) * 1000)
    suffix = "".join(_ID_RANDOM.choice(_ALPHABET) for _ in range(6))
    return f"prop-{stamp}-{suffix}"


def _room_lock(room_dir: str | Path) -> threading.RLock:
    key = str(Path(room_dir).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


# ---------------------------------------------------------------- 读写


def read_prop(room_dir: str | Path, prop_id: str) -> dict[str, Any] | None:
    """读取单条 PropSpec；文件缺失或内容不是对象时返回 ``None``。"""
    try:
        path = prop_path(room_dir, prop_id)
    except PropStoreError:
        return None
    from trpg_server.json_store import read_json

    data = read_json(path, default=None)
    return data if isinstance(data, dict) else None


def write_prop(room_dir: str | Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    """写入单条 spec 并重建索引，返回写入的副本。"""
    if not isinstance(spec, Mapping):
        raise PropStoreError("spec must be an object")
    prop_id = _require_prop_id(spec.get("id"))
    payload = copy.deepcopy(dict(spec))
    with _room_lock(room_dir):
        write_json_atomic(prop_path(room_dir, prop_id), payload)
        _rebuild_index(room_dir)
    return payload


def update_prop(
    room_dir: str | Path,
    prop_id: str,
    mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
) -> dict[str, Any] | None:
    """锁内读改写。``mutator`` 返回 ``None`` 表示放弃写入。"""
    with _room_lock(room_dir):
        current = read_prop(room_dir, prop_id)
        if current is None:
            return None
        updated = mutator(copy.deepcopy(current))
        if updated is None:
            return None
        if not isinstance(updated, Mapping):
            raise PropStoreError("mutator must return an object or None")
        payload = dict(updated)
        payload["id"] = current.get("id") or prop_id
        write_json_atomic(prop_path(room_dir, prop_id), payload)
        _rebuild_index(room_dir)
        return copy.deepcopy(payload)


def delete_prop(room_dir: str | Path, prop_id: str) -> bool:
    """删除 spec、资产目录与该道具的全部渲染缓存，返回是否真的删了 spec。"""
    try:
        spec_file = prop_path(room_dir, prop_id)
    except PropStoreError:
        return False
    with _room_lock(room_dir):
        existed = spec_file.is_file()
        if existed:
            try:
                spec_file.unlink()
            except OSError:
                logger.warning("props spec unlink failed id=%s", prop_id, exc_info=True)
                raise
        assets = assets_dir(room_dir, prop_id)
        if assets.exists():
            remove_tree(assets)
        renders = renders_dir(room_dir)
        if renders.is_dir():
            for path in renders.glob(f"{prop_id}@*"):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("props render unlink failed path=%s", path, exc_info=True)
        _rebuild_index(room_dir)
        return existed


def read_index(room_dir: str | Path) -> list[dict[str, Any]]:
    from trpg_server.json_store import read_json

    try:
        path = index_path(room_dir)
    except PropStoreError:
        return []
    data = read_json(path, default=[])
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def list_index(room_dir: str | Path) -> list[dict[str, Any]]:
    """返回索引条目（``created_at`` 倒序，时间相同按 id 倒序）。"""
    entries = read_index(room_dir)
    if not entries:
        # 索引缺失但 spec 存在（例如手工拷贝房间目录）时按需重建一次。
        entries = _rebuild_index(room_dir)
    return sorted(
        entries,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        reverse=True,
    )


def index_entry(spec: Mapping[str, Any]) -> dict[str, Any]:
    visibility = spec.get("visibility") if isinstance(spec.get("visibility"), Mapping) else {}
    return {
        "id": str(spec.get("id") or ""),
        "template_id": str(spec.get("template_id") or ""),
        "title": str(spec.get("title") or ""),
        "visibility": dict(visibility),
        "created_at": str(spec.get("created_at") or ""),
    }


def _rebuild_index(room_dir: str | Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    root = props_root(room_dir)
    if root.is_dir():
        for path in root.glob("prop-*.json"):
            if not path.is_file():
                continue
            spec = read_prop(room_dir, path.stem)
            if spec is None:
                logger.warning("props spec unreadable path=%s", path)
                continue
            entries.append(index_entry(spec))
    entries.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    write_json_atomic(index_path(room_dir), entries)
    return entries


def prune_renders(room_dir: str | Path, days: int) -> int:
    """清理超过 ``days`` 天的渲染缓存；``days <= 0`` 表示不清理。"""
    try:
        keep_days = int(days)
    except (TypeError, ValueError):
        return 0
    if keep_days <= 0:
        return 0
    directory = renders_dir(room_dir)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in directory.glob("prop-*@*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.debug("props render prune failed path=%s", path, exc_info=True)
    return removed


__all__ = [
    "INDEX_FILENAME",
    "PROP_ID_RE",
    "PropStoreError",
    "assets_dir",
    "delete_prop",
    "index_entry",
    "index_path",
    "is_valid_prop_id",
    "list_index",
    "new_prop_id",
    "prune_renders",
    "prop_path",
    "props_root",
    "read_index",
    "read_prop",
    "render_path",
    "renders_dir",
    "update_prop",
    "write_prop",
]
