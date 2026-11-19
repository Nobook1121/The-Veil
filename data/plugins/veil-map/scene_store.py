"""veil-map 场景分片存储。

布局
----
::

    <room>/plugins/veil-map/scenes/active.json                 # 当前场景 id
    <room>/plugins/veil-map/scenes/<scene_id>/active.json      # 当前代（generation）
    <room>/plugins/veil-map/scenes/<scene_id>/g-000003/
        scene.json       # 场景元数据：网格、背景、边界、更新时间
        tokens.json      # 棋子
        walls.json       # 墙体、门、窗
        lights.json      # 光源与环境光
        fog.json         # 迷雾探索记录（格位掩码）
        journal.json     # 幂等请求记录

每次写入生成一个完整的新代目录，最后才原子替换 ``active.json``。因此
revision 覆盖所有分片，崩溃时读者会看到旧代而不是“写了一半”的场景；加载器
在 active 代不完整时会回退到上一代完整目录。

旧版单文件 ``map.json`` 仍然可读：没有 scenes 目录时直接读它，首次写入后
自动迁移到分片布局；旧文件保留不删，便于回滚排查。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, remove_tree, write_json_atomic

SCENE_SCHEMA = 1
# 显式关闭场景：棋子仍保留在旧场景，但房间当前不显示任何地图。
CLOSED_SCENE_ID = "__closed__"

_SCENE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GENERATION = re.compile(r"^g-\d{6,}$")

META_KEYS = (
    "version",
    "title",
    "background",
    "background_meta",
    "grid_size",
    "grid_offset",
    "calibration",
    "bounds_cells",
    # 模组场景自动绑定的记录（场景 id + 地图 URL），随场景持久化。
    "scene_binding",
    "updated_at",
)

# 状态键 → 分片文件名；每个分片都带同一个 revision，加载时逐片校验。
SHARDS = (
    ("tokens", "tokens.json"),
    ("walls", "walls.json"),
    ("lights", "lights.json"),
    ("fog", "fog.json"),
    ("recent_requests", "journal.json"),
)

# 后来加入的分片：旧代目录里没有这个文件也视为完整（读成空值）。
OPTIONAL_SHARDS = {"fog"}


def scenes_root(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "veil-map" / "scenes"


def legacy_map_path(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "veil-map" / "map.json"


def safe_scene_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SCENE_ID.match(text) else "default"


def _active_scene_id(root: Path) -> str:
    pointer = read_json(root / "active.json", default={})
    if isinstance(pointer, dict):
        raw = str(pointer.get("scene_id") or "").strip()
        if raw == CLOSED_SCENE_ID:
            return CLOSED_SCENE_ID
        scene_id = safe_scene_id(raw)
        if scene_id and (root / scene_id).is_dir():
            return scene_id
    return ""


def _scene_candidates(root: Path) -> list[str]:
    """没有房间级指针时的兜底：优先 default，其次最近修改的场景。"""
    items: list[tuple[str, float]] = []
    if not root.exists():
        return []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not (path / "active.json").exists():
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        items.append((path.name, stamp))
    items.sort(key=lambda item: (item[0] != "default", -item[1]))
    return [name for name, _stamp in items]


def _load_generation(path: Path, scene_id: str) -> dict[str, Any] | None:
    meta = read_json(path / "scene.json", default=None)
    if not isinstance(meta, dict):
        return None
    try:
        revision = int(meta.get("revision") or 0)
    except (TypeError, ValueError):
        return None
    if revision <= 0:
        return None
    state: dict[str, Any] = {key: meta.get(key) for key in META_KEYS if key in meta}
    state["revision"] = revision
    state["scene_id"] = safe_scene_id(meta.get("scene_id") or scene_id)
    for key, filename in SHARDS:
        payload = read_json(path / filename, default=None)
        if not isinstance(payload, dict):
            if key in OPTIONAL_SHARDS:
                state[key] = {}
                continue
            return None
        try:
            shard_revision = int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            return None
        # revision 必须与 scene.json 一致，否则视为未完成的代。
        if shard_revision != revision:
            return None
        state[key] = payload.get(key)
        if key == "lights":
            state["ambient_light"] = payload.get("ambient_light")
    return state


def _load_scene(root: Path, scene_id: str) -> dict[str, Any] | None:
    scene_dir = root / safe_scene_id(scene_id)
    active = read_json(scene_dir / "active.json", default={})
    candidates: list[str] = []
    if isinstance(active, dict):
        generation = str(active.get("generation") or "")
        if _GENERATION.match(generation):
            candidates.append(generation)
    if scene_dir.exists():
        for path in sorted(scene_dir.iterdir(), key=lambda item: item.name, reverse=True):
            if path.is_dir() and _GENERATION.match(path.name) and path.name not in candidates:
                candidates.append(path.name)
    for generation in candidates:
        state = _load_generation(scene_dir / generation, scene_id)
        if state is not None:
            return state
    return None


def active_scene_id(room_dir) -> str:
    """当前场景 id；没有分片或已关闭时返回空串（由调用方按旧 map.json 处理）。"""
    root = scenes_root(room_dir)
    scene_id = _active_scene_id(root)
    if scene_id == CLOSED_SCENE_ID:
        return ""
    if scene_id:
        return scene_id
    candidates = _scene_candidates(root)
    return candidates[0] if candidates else ""


def list_scenes(room_dir) -> list[dict[str, Any]]:
    """列出所有可用场景及其概览，按「当前场景在前、其余最近更新在前」排序。"""
    root = scenes_root(room_dir)
    active = active_scene_id(room_dir)
    items: list[dict[str, Any]] = []
    for candidate in _scene_candidates(root):
        state = _load_scene(root, candidate)
        if state is None:
            continue
        items.append(
            {
                "scene_id": candidate,
                "title": str(state.get("title") or candidate)[:80],
                "revision": int(state.get("revision") or 0),
                "updated_at": str(state.get("updated_at") or ""),
                "background": str(state.get("background") or ""),
                "tokens": len(state.get("tokens") or []),
                "active": candidate == active,
            }
        )
    items.sort(key=lambda item: (bool(item["active"]), item["updated_at"]), reverse=True)
    return items


def has_scene(room_dir, scene_id: str) -> bool:
    root = scenes_root(room_dir)
    sid = safe_scene_id(scene_id)
    return bool(sid) and _load_scene(root, sid) is not None


def activate(room_dir, scene_id: str) -> bool:
    """切换房间当前场景；场景不存在或没有完整代时返回 ``False``。"""
    root = scenes_root(room_dir)
    sid = safe_scene_id(scene_id)
    if _load_scene(root, sid) is None:
        return False
    write_json_atomic(root / "active.json", {"schema": SCENE_SCHEMA, "scene_id": sid})
    return True


def deactivate(room_dir) -> None:
    """关闭房间当前地图；不删除场景，棋子与墙体保留在原场景。"""
    root = scenes_root(room_dir)
    write_json_atomic(root / "active.json", {"schema": SCENE_SCHEMA, "scene_id": CLOSED_SCENE_ID})


def load(room_dir) -> tuple[dict[str, Any], str]:
    """读取场景；返回 ``(state, source)``，source 为 scene / legacy / closed / empty。"""
    root = scenes_root(room_dir)
    scene_id = _active_scene_id(root)
    if scene_id == CLOSED_SCENE_ID:
        return {}, "closed"
    if scene_id:
        state = _load_scene(root, scene_id)
        if state is not None:
            return state, "scene"
    for candidate in _scene_candidates(root):
        state = _load_scene(root, candidate)
        if state is not None:
            return state, "scene"
    legacy = read_json(legacy_map_path(room_dir), default={})
    if isinstance(legacy, dict) and legacy:
        return legacy, "legacy"
    return {}, "empty"


def _prune(scene_dir: Path) -> None:
    """只保留最近两代（当前 + 上一代）；更老的代在后台清理。"""
    generations: list[Path] = []
    try:
        for path in scene_dir.iterdir():
            if path.is_dir() and _GENERATION.match(path.name):
                generations.append(path)
    except OSError:
        return
    generations.sort(key=lambda item: item.name, reverse=True)
    for path in generations[2:]:
        try:
            remove_tree(path)
        except OSError:
            # 清理失败不能影响本次写入；下次保存会再试。
            shutil.rmtree(path, ignore_errors=True)


def save(room_dir, state: dict[str, Any]) -> None:
    """把完整状态写成新一代分片，最后原子提交 active.json。"""
    root = scenes_root(room_dir)
    scene_id = safe_scene_id(state.get("scene_id"))
    try:
        revision = max(1, int(state.get("revision") or 1))
    except (TypeError, ValueError):
        revision = 1
    scene_dir = root / scene_id
    generation = f"g-{revision:06d}"
    generation_dir = scene_dir / generation
    generation_dir.mkdir(parents=True, exist_ok=True)

    meta = {"schema": SCENE_SCHEMA, "scene_id": scene_id, "revision": revision}
    for key in META_KEYS:
        if key in state:
            meta[key] = state[key]
    write_json_atomic(generation_dir / "scene.json", meta)

    for key, filename in SHARDS:
        default: Any = {} if key == "fog" else []
        payload: dict[str, Any] = {"revision": revision, key: state.get(key) or default}
        if key == "lights":
            payload["ambient_light"] = state.get("ambient_light") or {}
        write_json_atomic(generation_dir / filename, payload)

    write_json_atomic(
        scene_dir / "active.json",
        {"schema": SCENE_SCHEMA, "scene_id": scene_id, "generation": generation, "revision": revision},
    )
    write_json_atomic(root / "active.json", {"schema": SCENE_SCHEMA, "scene_id": scene_id})
    _prune(scene_dir)
