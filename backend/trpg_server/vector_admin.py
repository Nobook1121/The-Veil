"""向量库巡检与孤儿向量清理（控制台专用）。

「孤儿向量」指向量库中仍存在的 ``scenario_id``，但剧本目录里已不存在对应剧本
（剧本被删时并不会自动清理其向量条目，见 :mod:`trpg_server.scenario_admin`）。
本模块只做只读扫描与按条件删除，不依赖 Flask 请求上下文。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server.scenario_admin import list_scenarios

logger = logging.getLogger(__name__)


def vector_health(
    vector_store: Any = None,
    embedding_provider: Any = None,
    ocr_provider: Any = None,
) -> dict[str, Any]:
    """向量库后端 / 总条数 + embedding / OCR 状态。"""

    store_health = (
        vector_store.health()
        if vector_store is not None and hasattr(vector_store, "health")
        else {"available": False}
    )
    total: int | None = None
    if vector_store is not None and hasattr(vector_store, "count"):
        try:
            total = int(vector_store.count())
        except Exception:  # noqa: BLE001 - 计数失败不应让巡检中断
            logger.exception("Failed to count vectors")
    if embedding_provider is not None and hasattr(embedding_provider, "health"):
        embedding = embedding_provider.health()
    else:
        embedding = {"configured": bool(embedding_provider), "loaded": False}
    ocr = {
        "enabled": bool(ocr_provider),
        "available": bool(ocr_provider and getattr(ocr_provider, "available", False)),
    }
    return {"backend": store_health, "total": total, "embedding": embedding, "ocr": ocr}


def scan_orphans(vector_store: Any, scenarios_dir: Any) -> dict[str, Any]:
    """扫描孤儿向量。

    返回 ``{supported, scenario_ids, orphans}``；后端不支持枚举（如 Qdrant）时
    ``supported`` 为 ``False``，此时无法判断孤儿，调用方应提示用户。
    """

    if vector_store is None or not hasattr(vector_store, "scenario_ids"):
        return {"supported": False, "scenario_ids": [], "orphans": []}
    ids = vector_store.scenario_ids()
    if ids is None:
        return {"supported": False, "scenario_ids": [], "orphans": []}
    known = {str(item["id"]) for item in list_scenarios(Path(scenarios_dir))}
    return {
        "supported": True,
        "scenario_ids": sorted(str(sid) for sid in ids),
        "orphans": sorted(str(sid) for sid in ids if str(sid) not in known),
    }


def prune_orphan_vectors(vector_store: Any, scenario_ids: Any) -> dict[str, Any]:
    """删除给定 ``scenario_id`` 的全部向量条目，返回删除计数。"""

    deleted = 0
    per_scenario: dict[str, int] = {}
    for sid in scenario_ids or []:
        try:
            removed = int(vector_store.delete_by_filter({"scenario_id": str(sid)}))
        except Exception:  # noqa: BLE001 - 单个失败不阻断其余清理
            logger.exception("Failed to delete orphan vectors: %s", sid)
            continue
        per_scenario[str(sid)] = removed
        deleted += removed
    return {"deleted": deleted, "per_scenario": per_scenario}
