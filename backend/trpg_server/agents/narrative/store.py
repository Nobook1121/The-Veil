"""`narrative.json` 的读写与派生索引（设计文档 §3.1、§4.1）。

存储位置与 `scenario.json` 同级：`data/scenarios/scenario-<id>/narrative.json`。
写盘使用原子写入；索引只在保存/导入时重建，热路径为内存查表。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.narrative.model import (
    NarrativeBundle,
    content_fingerprint,
    utc_now_iso,
)
from trpg_server.agents.narrative.visibility import normalize_visibility
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.security import safe_join

logger = logging.getLogger(__name__)

NARRATIVE_FILENAME = "narrative.json"
LOCATION_KINDS = {"act", "area", "location", "ending"}
MOVEMENT_EDGE_TYPES = {"adjacent", "portal", "next", "leads_to"}
# contains/mentions 等只用于展示或检索，不参与走位。
_NON_MOVEMENT_EDGE_TYPES = {
    "requires",
    "reveals",
    "knows",
    "inhabits",
    "uses",
    "contains",
    "triggers",
    "conflicts",
    "mentions",
    "alias",
}


class NarrativeStoreError(RuntimeError):
    """读取或写入 narrative.json 失败。"""


def scenario_dir_for_descriptor(descriptor_path: Path | str) -> Path:
    return Path(descriptor_path).resolve().parent


def narrative_path(scenario_dir: Path | str) -> Path:
    return Path(scenario_dir) / NARRATIVE_FILENAME


def narrative_path_for_descriptor(descriptor_path: Path | str) -> Path:
    return narrative_path(scenario_dir_for_descriptor(descriptor_path))


def has_narrative(scenario_dir: Path | str) -> bool:
    return narrative_path(scenario_dir).exists()


def find_scenario_storage(scenarios_dir: Path | str, scenario_ref: str) -> tuple[Path | None, Path | None]:
    """按 id / 目录名 / 文件名定位剧本存储目录。

    返回 `(descriptor_path, storage_dir)`；找不到时返回 `(None, None)`。
    """
    root = Path(scenarios_dir)
    ref = str(scenario_ref or "").strip()
    if not ref or not root.exists():
        return None, None

    candidates: list[Path] = [root / f"scenario-{ref}", root / ref]
    for path in candidates:
        descriptor = path / "scenario.json"
        if path.is_dir() and descriptor.exists():
            return descriptor, path

    direct = root / f"{ref}.json"
    if direct.exists():
        return direct, direct.parent

    for descriptor in sorted(root.glob("*/scenario.json")):
        payload = read_json(descriptor, default={})
        if not isinstance(payload, Mapping):
            continue
        keys = {str(payload.get("id") or ""), descriptor.parent.name}
        if ref in keys:
            return descriptor, descriptor.parent
    for descriptor in sorted(root.glob("*.json")):
        if descriptor.name == "scenario.json":
            continue
        payload = read_json(descriptor, default={})
        if isinstance(payload, Mapping) and ref in {str(payload.get("id") or ""), descriptor.stem}:
            return descriptor, descriptor.parent
    return None, None


_cache_lock = threading.Lock()
_bundle_cache: dict[str, tuple[float, NarrativeBundle]] = {}


def load_bundle(scenario_dir: Path | str, *, use_cache: bool = True) -> NarrativeBundle | None:
    path = narrative_path(scenario_dir)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise NarrativeStoreError(f"无法读取 {path}: {exc}") from exc
    cache_key = str(path.resolve())
    if use_cache:
        with _cache_lock:
            cached = _bundle_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        payload = read_json(path, default=None)
    except Exception as exc:  # noqa: BLE001 - 统一转换为存储错误
        raise NarrativeStoreError(f"narrative.json 解析失败: {path}") from exc
    if not isinstance(payload, Mapping):
        raise NarrativeStoreError(f"narrative.json 内容不是对象: {path}")
    bundle = NarrativeBundle.from_dict(payload)
    if use_cache:
        with _cache_lock:
            _bundle_cache[cache_key] = (mtime, bundle)
    return bundle


def invalidate_cache(scenario_dir: Path | str | None = None) -> None:
    with _cache_lock:
        if scenario_dir is None:
            _bundle_cache.clear()
            return
        _bundle_cache.pop(str(narrative_path(scenario_dir).resolve()), None)


def save_bundle(scenario_dir: Path | str, bundle: NarrativeBundle, *, update_hash: bool = True) -> Path:
    directory = Path(scenario_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if update_hash:
        bundle.build_hash = content_fingerprint(bundle)
    if not bundle.generated_at:
        bundle.generated_at = utc_now_iso()
    path = narrative_path(directory)
    try:
        write_json_atomic(path, bundle.to_dict())
    except Exception as exc:  # noqa: BLE001
        raise NarrativeStoreError(f"写入 narrative.json 失败: {path}") from exc
    invalidate_cache(directory)
    logger.info(
        "narrative.saved path=%s nodes=%d edges=%d topology=%s hash=%s",
        path, len(bundle.nodes), len(bundle.edges), bundle.topology, bundle.build_hash,
    )
    return path


def delete_bundle(scenario_dir: Path | str) -> bool:
    path = narrative_path(scenario_dir)
    if not path.exists():
        return False
    path.unlink()
    invalidate_cache(scenario_dir)
    return True


@dataclass
class NarrativeIndex:
    """内存派生索引：邻接表、可达集合与质量计数。"""

    start: str | None = None
    adjacency: dict[str, list[str]] = field(default_factory=dict)
    exits: dict[str, list[str]] = field(default_factory=dict)
    reachable: set[str] = field(default_factory=set)
    unreachable_locations: list[str] = field(default_factory=list)
    orphan_assets: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    degree: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "adjacency": {key: list(value) for key, value in self.adjacency.items()},
            "exits": {key: list(value) for key, value in self.exits.items()},
            "reachable": sorted(self.reachable),
            "unreachable_locations": list(self.unreachable_locations),
            "orphan_assets": list(self.orphan_assets),
            "counts": dict(self.counts),
            "degree": dict(self.degree),
        }


def resolve_start(bundle: NarrativeBundle) -> str | None:
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    start = meta.get("start") or meta.get("start_location")
    if isinstance(start, str) and start and bundle.node(start):
        return start
    for node in bundle.nodes:
        if node.kind == "location" and (node.scene_role in {"stage", "hub"} or node.access == "free"):
            return node.id
    for node in bundle.nodes:
        if node.kind == "location":
            return node.id
    return None


def build_index(bundle: NarrativeBundle, *, start: str | None = None) -> NarrativeIndex:
    node_ids = {node.id for node in bundle.nodes}
    index = NarrativeIndex(start=start or resolve_start(bundle))
    index.adjacency = {node_id: [] for node_id in node_ids}
    index.exits = {node_id: [] for node_id in node_ids}

    for edge in bundle.edges:
        if edge.type in _NON_MOVEMENT_EDGE_TYPES:
            continue
        if edge.from_id not in node_ids or edge.to_id not in node_ids:
            continue
        index.adjacency[edge.from_id].append(edge.to_id)
        if edge.bidirectional:
            index.adjacency[edge.to_id].append(edge.from_id)
        if edge.visibility == "public":
            index.exits[edge.from_id].append(edge.to_id)
            if edge.bidirectional:
                index.exits[edge.to_id].append(edge.from_id)

    for key, targets in index.adjacency.items():
        index.adjacency[key] = sorted(set(targets))
        index.exits[key] = sorted(set(index.exits[key]))

    if index.start:
        seen = {index.start}
        queue = [index.start]
        while queue:
            current = queue.pop(0)
            for target in index.adjacency.get(current, []):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        index.reachable = seen

    locations = [node for node in bundle.nodes if node.kind == "location"]
    index.unreachable_locations = sorted(
        node.id for node in locations if index.start and node.id not in index.reachable
    )

    linked = {node_id for edge in bundle.edges for node_id in (edge.from_id, edge.to_id)}
    index.orphan_assets = sorted(
        node.id for node in bundle.nodes
        if node.kind not in LOCATION_KINDS and node.id not in linked
    )

    counts: dict[str, int] = {"nodes": len(bundle.nodes), "edges": len(bundle.edges)}
    for node in bundle.nodes:
        counts[f"kind:{node.kind}"] = counts.get(f"kind:{node.kind}", 0) + 1
        state = normalize_visibility(node.visibility)
        counts[f"visibility:{state}"] = counts.get(f"visibility:{state}", 0) + 1
        index.degree[node.id] = len(index.adjacency.get(node.id, []))
    for edge in bundle.edges:
        counts[f"edge:{edge.type}"] = counts.get(f"edge:{edge.type}", 0) + 1
    index.counts = counts
    return index


def bundle_summary(bundle: NarrativeBundle | None, *, index: NarrativeIndex | None = None) -> dict[str, Any]:
    if bundle is None:
        return {"exists": False}
    index = index or build_index(bundle)
    return {
        "exists": True,
        "scenario_id": bundle.scenario_id,
        "scenario_version": bundle.scenario_version,
        "schema_version": bundle.schema_version,
        "topology": bundle.topology,
        "topology_confidence": bundle.topology_confidence,
        "topology_source": bundle.topology_source,
        "build_hash": bundle.build_hash,
        "generated_at": bundle.generated_at,
        "start": index.start,
        "counts": index.counts,
        "unreachable_locations": index.unreachable_locations,
        "orphan_assets": index.orphan_assets,
        "warnings": len(bundle.warnings),
    }


def scenario_id_from_dir(scenario_dir: Path | str) -> str:
    """`scenario-12` → `12`；其它目录名原样返回。"""
    name = Path(scenario_dir).name
    return name[len("scenario-"):] if name.startswith("scenario-") else name


__all__ = [
    "NARRATIVE_FILENAME",
    "NarrativeIndex",
    "NarrativeStoreError",
    "bundle_summary",
    "build_index",
    "delete_bundle",
    "find_scenario_storage",
    "has_narrative",
    "invalidate_cache",
    "load_bundle",
    "narrative_path",
    "narrative_path_for_descriptor",
    "resolve_start",
    "safe_join",
    "save_bundle",
    "scenario_dir_for_descriptor",
    "scenario_id_from_dir",
]
