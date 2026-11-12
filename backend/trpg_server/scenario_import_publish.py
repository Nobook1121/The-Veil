"""把已完成（或用户已审核）的导入任务发布成正式剧本。

路由「立即发布」与后台任务的「完成后自动发布」共用同一套落盘逻辑，避免两条路径
各写一份导致 public_id、知识索引或图谱行为出现差异。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.knowledge_base import (
    KnowledgeChunk,
    index_knowledge_chunks,
    persist_knowledge_index,
    write_knowledge_index,
)
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.media_defaults import normalize_cover
from trpg_server.scenario_store import (
    generate_public_id,
    load_scenario_record,
    save_scenario_record,
    scenario_descriptor_paths,
)
from trpg_server.settings import SCENARIOS_DIR

logger = logging.getLogger(__name__)


def _current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _existing_public_ids(root: Path, *, exclude_id: object = None) -> set[str]:
    values: set[str] = set()
    for path in scenario_descriptor_paths(root):
        try:
            record = load_scenario_record(path, root)
        except Exception:  # noqa: BLE001 - 单个描述符损坏不应阻断发布
            continue
        if exclude_id is not None and str(record.get("id")) == str(exclude_id):
            continue
        public_id = str(record.get("public_id") or "")
        if public_id:
            values.add(public_id)
    return values


def publish_import_job(
    store,
    config: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    root: Path | None = None,
    username: str = "",
    user_id: Any = None,
) -> dict[str, Any]:
    """发布导入任务并返回落盘后的剧本记录；调用方负责权限校验。"""
    job_id = str(job.get("id") or "")
    script_id = int(job.get("script_id") or 0)
    scenario = dict(job.get("preview") or store.load_intermediate(job_id, "preview", {}) or {})
    scenario["id"] = script_id
    scenario["scenario_version"] = str(job.get("target_version") or "1.0.0")
    scenario["owner_id"] = job.get("owner_id")

    root = Path(root or config.get("SCENARIOS_DIR", SCENARIOS_DIR))
    existing_public_ids = _existing_public_ids(root, exclude_id=script_id)
    public_id = str(scenario.get("public_id") or "")
    if len(public_id) != 6 or not public_id.isalnum() or public_id in existing_public_ids:
        scenario["public_id"] = generate_public_id(existing_public_ids)
    if not scenario.get("creator_username"):
        scenario["creator_username"] = username
    timestamp = _current_timestamp()
    scenario.setdefault("createdAt", timestamp)
    scenario["updatedAt"] = timestamp
    scenario["cover"] = normalize_cover(scenario.get("cover"))
    descriptor = save_scenario_record(root, scenario)
    stored = load_scenario_record(descriptor, root)

    vector_store = config.get("VECTOR_STORE")
    provider = config.get("EMBEDDING_PROVIDER")
    raw_chunks = store.load_intermediate(job_id, "knowledge", []) or []
    chunks: list[KnowledgeChunk] = []
    for item in raw_chunks:
        if isinstance(item, dict):
            try:
                chunks.append(KnowledgeChunk(**item))
            except TypeError:
                continue
    if chunks:
        chunks = index_knowledge_chunks(chunks, vector_store=vector_store, embedding_provider=provider)
        write_knowledge_index(
            descriptor,
            stored.get("scenario_version") or scenario.get("scenario_version"),
            chunks,
        )
    else:
        persist_knowledge_index(descriptor, stored, vector_store=vector_store, embedding_provider=provider)

    # 导入阶段已生成图谱；发布会再修复一次结构，确保端点、parent、起点可靠。
    if str(scenario.get("import_mode") or "") in {"ai", "direct", "bundle"}:
        from trpg_server.agents.narrative.model import NarrativeBundle
        from trpg_server.agents.narrative.repair import repair_bundle
        from trpg_server.agents.narrative.store import save_bundle

        narrative_data = store.load_intermediate(job_id, "narrative", {}) or {}
        if isinstance(narrative_data, dict) and narrative_data.get("nodes"):
            narrative = NarrativeBundle.from_dict(narrative_data)
            narrative.scenario_id = str(script_id)
            narrative.scenario_version = (
                stored.get("scenario_version") or scenario.get("scenario_version") or "1.0.0"
            )
            repair = repair_bundle(narrative)
            narrative.meta = {
                **dict(narrative.meta or {}),
                "scenario_id": str(script_id),
                "published_from": "import",
                "repair": repair.to_dict(),
            }
            save_bundle(descriptor.parent, narrative)
            log_user_action(
                logger,
                user_action_text(username, "发布了导入剧本图谱"),
                用户ID=user_id,
                剧本ID=script_id,
                节点数=len(narrative.nodes),
                边数=len(narrative.edges),
            )

    store.update(job_id, status="published", progress=100, script_id=script_id)

    # 可选后置：把 clue 模块 AI 映射为道具包。默认关闭；任何失败只 warning，
    # 绝不影响剧本发布结果。
    try:
        from trpg_server.props import props_settings
        from trpg_server.props.prepare import prepare_scenario_props

        prop_settings = props_settings(config.get("CONFIG_DIR"))
        if prop_settings.get("auto_prepare_on_import"):
            prepared = prepare_scenario_props(
                scenario,
                scenario_dir=descriptor.parent,
                config_dir=config.get("CONFIG_DIR"),
                settings=prop_settings,
            )
            if prepared.get("count"):
                current = store.get(job_id) or {}
                meta = dict(current.get("stage_meta") or {})
                meta["props_prepared"] = prepared["count"]
                meta["message"] = f"{prepared['count']} 件道具已备好"
                store.update(job_id, stage_meta=meta)
    except Exception as exc:  # noqa: BLE001 - 后置步骤不得影响发布
        logger.warning("scenario_import.props_prepare_failed job=%s error=%s", job_id, exc)

    log_user_action(
        logger,
        user_action_text(username, "发布了导入任务"),
        用户ID=user_id,
        剧本ID=script_id,
    )
    return load_scenario_record(descriptor, root)
