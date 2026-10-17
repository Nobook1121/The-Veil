"""知识库巡检与重建（控制台专用）。

覆盖两类知识：① 各剧本的派生知识索引（``knowledge-index/*.json``）与其向量数；
② 外部规则集（:class:`trpg_server.agents.ruleset_knowledge.RulesetKnowledgeStore`）。
规则集重建复用 Web 路由同一实现，因此控制台与网页结果一致。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server.agents.knowledge_base import load_knowledge_index
from trpg_server.scenario_store import load_scenario_record, scenario_descriptor_paths

logger = logging.getLogger(__name__)


def _scenario_version(record: dict[str, Any]) -> str:
    return str(record.get("scenario_version") or record.get("version") or "1")


def list_knowledge(
    scenarios_dir: Any,
    vector_store: Any = None,
    ruleset_store: Any = None,
    search: str = "",
) -> dict[str, Any]:
    """列出规则集，以及每个剧本的知识索引块数与向量数。"""

    root = Path(scenarios_dir)
    needle = str(search or "").strip().casefold()
    scenarios: list[dict[str, Any]] = []
    for descriptor in scenario_descriptor_paths(root):
        try:
            record = load_scenario_record(descriptor, root)
        except (OSError, ValueError):
            continue
        scenario_id = str(record.get("id") or "")
        if not scenario_id:
            continue
        title = str(record.get("title") or scenario_id)
        if needle and needle not in f"{scenario_id} {title}".casefold():
            continue
        version = _scenario_version(record)
        chunks = load_knowledge_index(descriptor, version)
        vector_count: int | None = None
        if vector_store is not None:
            try:
                vector_count = int(vector_store.count({"scenario_id": scenario_id}))
            except Exception:  # noqa: BLE001
                logger.exception("Failed to count vectors for scenario %s", scenario_id)
        scenarios.append(
            {
                "id": scenario_id,
                "title": title,
                "version": version,
                "chunks": len(chunks),
                "vectors": vector_count,
            }
        )
    scenarios.sort(key=lambda item: int(item["id"]) if item["id"].isdigit() else 0)

    rulesets: list[dict[str, Any]] = []
    if ruleset_store is not None:
        try:
            rulesets = list(ruleset_store.list_rulesets() or [])
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list rulesets")
        if needle:
            rulesets = [
                item
                for item in rulesets
                if needle in f"{item.get('ruleset_id')} {item.get('title') or ''}".casefold()
            ]
    return {"scenarios": scenarios, "rulesets": rulesets}


def reindex_rulesets(ruleset_store: Any, ruleset_id: str | None = None) -> list[dict[str, Any]]:
    """重建规则集索引；``ruleset_id`` 省略时重建全部已知规则集。"""

    if ruleset_store is None:
        raise ValueError("规则集知识库不可用（未配置存储）")
    if ruleset_id:
        target_ids = [str(ruleset_id)]
    else:
        target_ids = [str(item.get("ruleset_id")) for item in ruleset_store.list_rulesets() or []]
    results: list[dict[str, Any]] = []
    for rid in target_ids:
        results.append(ruleset_store.reindex(rid))
    return results
