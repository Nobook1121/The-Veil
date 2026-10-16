"""剧本级运维操作（控制台专用）：列出、改所有者、删除。

与 Web 路由共用同一套落盘函数（:mod:`trpg_server.scenario_store`），因此控制台操作
与网页操作结果一致。本模块只做文件系统操作，不依赖 Flask 请求上下文；需要清理向量
库时由调用方传入 ``vector_store``（控制台从 ``app.extensions`` 取出后在启动时注入）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from trpg_server.agents.knowledge_base import (
    build_knowledge_chunks,
    index_knowledge_chunks,
    knowledge_index_path,
    load_knowledge_index,
    write_knowledge_index,
)
from trpg_server.json_store import read_json
from trpg_server.scenario_store import (
    delete_scenario_record,
    find_scenario_descriptor_by_id,
    load_scenario_record,
    save_scenario_record,
    scenario_descriptor_paths,
)
from trpg_server.settings import SCENARIO_COVERS_DIR

logger = logging.getLogger(__name__)


def list_scenarios(scenarios_dir: Path, vector_store: Any = None) -> list[dict[str, Any]]:
    """列出全部剧本（含所有者、版本、向量数），用于控制台巡检。"""

    root = Path(scenarios_dir)
    items: list[dict[str, Any]] = []
    for descriptor in scenario_descriptor_paths(root):
        try:
            record = load_scenario_record(descriptor, root)
        except (OSError, ValueError):
            continue
        scenario_id = str(record.get("id") or "")
        if not scenario_id:
            continue
        vector_count = int(vector_store.count({"scenario_id": scenario_id})) if vector_store else None
        items.append(
            {
                "id": scenario_id,
                "title": str(record.get("title") or scenario_id),
                "owner_id": record.get("owner_id"),
                "version": str(record.get("scenario_version") or record.get("version") or "1"),
                "archived": bool(record.get("archived")),
                "vector_count": vector_count,
            }
        )
    items.sort(key=lambda item: int(item["id"]) if item["id"].isdigit() else 0)
    return items


def set_owner(scenarios_dir: Path, scenario_id: Any, owner_id: Any) -> dict[str, Any]:
    """改写剧本记录的 ``owner_id``（决定谁有编辑权限）；剧本不存在时抛 ``LookupError``。"""

    root = Path(scenarios_dir)
    descriptor = find_scenario_descriptor_by_id(root, scenario_id)
    if descriptor is None:
        raise LookupError(f"scenario not found: {scenario_id}")
    record = load_scenario_record(descriptor, root)
    previous = record.get("owner_id")
    record["owner_id"] = owner_id
    save_scenario_record(root, record, existing_descriptor=descriptor)
    return {
        "id": str(scenario_id),
        "title": str(record.get("title") or scenario_id),
        "previous_owner": previous,
        "owner_id": owner_id,
    }


def remove_knowledge(scenarios_dir: Path, scenario_id: Any, vector_store: Any = None) -> dict[str, int]:
    """只清理某剧本的知识库：向量条目 + 知识索引文件（保留剧本本体）。

    与设置页「删除剧本知识库」语义一致：删除剧本文件不会自动移除向量条目，若不显式
    清理，已删剧本的知识仍会被检索到。
    """

    root = Path(scenarios_dir)
    deleted_vectors = 0
    if vector_store is not None:
        try:
            deleted_vectors = int(vector_store.delete_by_filter({"scenario_id": str(scenario_id)}))
        except Exception:  # noqa: BLE001 - 向量清理失败不应阻断索引清理
            logger.exception("Failed to delete scenario vectors: %s", scenario_id)
    deleted_files = 0
    descriptor = find_scenario_descriptor_by_id(root, scenario_id)
    if descriptor is not None:
        index_dir = descriptor.parent / "knowledge-index"
        if index_dir.exists():
            for path in index_dir.glob("*.json"):
                try:
                    path.unlink()
                    deleted_files += 1
                except OSError:
                    logger.exception("Failed to delete knowledge index file: %s", path)
            try:
                index_dir.rmdir()
            except OSError:
                pass
    return {"deleted_vectors": deleted_vectors, "deleted_files": deleted_files}


def rooms_using_scenario(rooms_dir: Path | None, scenario_id: Any) -> list[str]:
    """返回仍在使用该剧本的房间 id 列表（用于判断是否允许直接删除）。"""

    if not rooms_dir:
        return []
    root = Path(rooms_dir)
    if not root.exists():
        return []
    result: list[str] = []
    for room_dir in sorted(root.iterdir()):
        if not room_dir.is_dir():
            continue
        info = read_json(room_dir / "info.json", default={})
        if isinstance(info, dict) and str(info.get("scenario_id")) == str(scenario_id):
            result.append(room_dir.name)
    return result


def delete_scenario(
    scenarios_dir: Path,
    rooms_dir: Path | None,
    scenario_id: Any,
    vector_store: Any = None,
    *,
    vectors_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """删除剧本，行为由参数决定：

    - 默认：与网页一致——仍有房间占用时**归档**，否则删除记录 + 知识库 + 封面；
    - ``vectors_only=True``：只删向量库与知识索引，保留剧本本体；
    - ``force=True``：即使仍有房间占用，也直接删除剧本本体。

    剧本不存在时抛 ``LookupError``。
    """

    root = Path(scenarios_dir)
    descriptor = find_scenario_descriptor_by_id(root, scenario_id)
    if descriptor is None:
        raise LookupError(f"scenario not found: {scenario_id}")
    record = load_scenario_record(descriptor, root)
    title = str(record.get("title") or scenario_id)

    if vectors_only:
        removed = remove_knowledge(root, scenario_id, vector_store)
        return {"mode": "vectors-only", "id": str(scenario_id), "title": title, **removed}

    active_rooms = rooms_using_scenario(rooms_dir, scenario_id)
    if active_rooms and not force:
        record["archived"] = True
        record["archivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
        save_scenario_record(root, record, existing_descriptor=descriptor)
        return {
            "mode": "archived",
            "id": str(scenario_id),
            "title": title,
            "active_rooms": len(active_rooms),
        }

    delete_scenario_record(descriptor)
    removed = remove_knowledge(root, scenario_id, vector_store)
    cover_path = Path(SCENARIO_COVERS_DIR) / f"{scenario_id}.png"
    deleted_cover = cover_path.exists()
    if deleted_cover:
        try:
            cover_path.unlink()
        except OSError:
            deleted_cover = False
    return {
        "mode": "force" if active_rooms else "deleted",
        "id": str(scenario_id),
        "title": title,
        "active_rooms": len(active_rooms),
        "deleted_cover": deleted_cover,
        **removed,
    }


def _scenario_version(record: dict[str, Any]) -> str:
    return str(record.get("scenario_version") or record.get("version") or "1")


def validate_scenario(
    scenarios_dir: Path,
    scenario_id: Any,
    vector_store: Any = None,
) -> dict[str, Any]:
    """校验剧本 JSON / 知识索引 / 向量库三者是否一致（只读）。

    剧本不存在时抛 ``LookupError``。返回的 ``issues`` 为空表示三者自洽。
    """

    root = Path(scenarios_dir)
    descriptor = find_scenario_descriptor_by_id(root, scenario_id)
    if descriptor is None:
        raise LookupError(f"scenario not found: {scenario_id}")
    try:
        record = load_scenario_record(descriptor, root)
    except (OSError, ValueError) as exc:
        return {
            "id": str(scenario_id),
            "title": str(scenario_id),
            "version": "-",
            "index_chunks": 0,
            "vectors": None,
            "issues": [f"剧本 JSON 无法解析: {exc}"],
            "ok": False,
        }

    title = str(record.get("title") or scenario_id)
    version = _scenario_version(record)
    issues: list[str] = []
    chunks = load_knowledge_index(descriptor, version)
    index_path = knowledge_index_path(descriptor, version)
    if not index_path.exists():
        issues.append(f"缺少知识索引文件：{index_path}")

    vectors: int | None = None
    if vector_store is not None:
        try:
            vectors = int(vector_store.count({"scenario_id": str(record.get("id") or scenario_id)}))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to count vectors for scenario %s", scenario_id)
            issues.append("查询向量库失败")
    if vectors is not None:
        if chunks and vectors != len(chunks):
            issues.append(f"知识索引 {len(chunks)} 块 与 向量 {vectors} 条 数量不一致")
        elif chunks and vectors == 0:
            issues.append(f"向量库缺少该剧本向量（索引有 {len(chunks)} 块）")

    return {
        "id": str(record.get("id") or scenario_id),
        "title": title,
        "version": version,
        "index_chunks": len(chunks),
        "vectors": vectors,
        "issues": issues,
        "ok": not issues,
    }


def reindex_scenario(
    scenarios_dir: Path,
    scenario_id: Any,
    *,
    vector_store: Any = None,
    embedding_provider: Any = None,
) -> dict[str, Any]:
    """重建某剧本的知识索引与向量。

    优先复用磁盘上已有的知识索引分块（为直接导入、无场景卡的剧本保留分块），
    仅当索引缺失时才从场景卡 ``modules`` 重建，避免把已导入的知识覆盖为空。
    剧本不存在时抛 ``LookupError``；没有任何可重建分块时抛 ``ValueError``。
    """

    root = Path(scenarios_dir)
    descriptor = find_scenario_descriptor_by_id(root, scenario_id)
    if descriptor is None:
        raise LookupError(f"scenario not found: {scenario_id}")
    record = load_scenario_record(descriptor, root)
    version = _scenario_version(record)

    chunks = load_knowledge_index(descriptor, version)
    source = "index"
    if not chunks:
        chunks = list(build_knowledge_chunks(record))
        source = "scenario"
    if not chunks:
        raise ValueError("该剧本没有可重建的知识块（既无索引文件，场景卡也无 modules）")

    prepared = index_knowledge_chunks(
        chunks, vector_store=vector_store, embedding_provider=embedding_provider
    )
    path = write_knowledge_index(descriptor, version, prepared)
    return {
        "id": str(record.get("id") or scenario_id),
        "title": str(record.get("title") or scenario_id),
        "version": version,
        "chunks": len(prepared),
        "source": source,
        "index_path": str(path),
    }
