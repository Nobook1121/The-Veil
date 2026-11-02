from __future__ import annotations

import logging

from flask import Blueprint, current_app, request, session
from pathlib import Path

from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, success_response
from trpg_server.security import is_allowed_upload, require_permission_node
from trpg_server.settings import KNOWLEDGE_BASES_DIR, ROOMS_DIR, SCENARIOS_DIR, EMBEDDED_VECTOR_DB_PATH
from trpg_server.scenario_store import load_scenario_record, scenario_descriptor_paths
from trpg_server.agents.knowledge_base import knowledge_index_path

bp = Blueprint("knowledge_bases", __name__)
logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {"txt", "md", "markdown", "text", "doc", "docx", "pdf"}


def _store() -> RulesetKnowledgeStore:
    root = current_app.config.get("KNOWLEDGE_BASES_DIR", KNOWLEDGE_BASES_DIR)
    rooms = current_app.config.get("ROOMS_DIR", ROOMS_DIR)
    return RulesetKnowledgeStore(root, rooms_dir=rooms, ocr_provider=current_app.extensions.get("ocr_provider"))


def _scenario_knowledge_items(search: str = "") -> list[dict]:
    configured_dir = current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)
    scenario_roots = [Path(configured_dir)]
    if Path(SCENARIOS_DIR) not in scenario_roots:
        scenario_roots.append(Path(SCENARIOS_DIR))
    vector_store = current_app.extensions.get("vector_store")
    needle = str(search or "").strip().casefold()
    items = []
    descriptors = [descriptor for root in scenario_roots for descriptor in scenario_descriptor_paths(root)]
    for descriptor in descriptors:
        try:
            scenario = load_scenario_record(descriptor, descriptor.parents[1])
            scenario_id = scenario.get("id")
            title = str(scenario.get("title") or descriptor.parent.name)
            if needle and needle not in f"{title} {scenario_id}".casefold():
                continue
            version = str(scenario.get("scenario_version") or scenario.get("version") or "1")
            index_path = knowledge_index_path(descriptor, version)
            # A scenario's persisted vectors can predate semver normalization
            # (legacy payloads use versions such as ``1`` or ``2``).  The
            # management view represents the knowledge base for the scenario,
            # so count all of its vectors regardless of the historical version.
            vector_filter = {"scenario_id": str(scenario_id)}
            count = int(vector_store.count(vector_filter)) if vector_store else 0
            index_exists = index_path.exists()
            # 知识已通过「删除知识库」清理过（无索引且无向量）的剧本不再出现在列表里，
            # 否则用户点完删除后仍会看到一条「向量 0」的空记录，误以为没删掉。
            if not index_exists and count <= 0:
                continue
            health = vector_store.health() if vector_store else {"backend": "none"}
            items.append({
                "scenario_id": scenario_id,
                "title": title,
                "version": version,
                "vector_count": count,
                "path": str(index_path),
                "descriptor_path": str(descriptor),
                "vector_db_path": str(getattr(vector_store, "db_path", current_app.config.get("EMBEDDED_VECTOR_DB_PATH", EMBEDDED_VECTOR_DB_PATH))),
                "backend": health.get("backend", "unknown"),
                "index_exists": index_exists,
                # 归档剧本已从剧本列表隐藏（用户视角等同删除），但可能仍被进行中的
                # 房间引用；列表里标记出来，避免误以为知识库是删除残留。
                "archived": bool(scenario.get("archived")),
            })
        except (OSError, ValueError, TypeError):
            continue
    return sorted(items, key=lambda item: (str(item.get("title", "")).casefold(), int(item.get("scenario_id") or 0)))


@bp.route("/api/knowledge-bases/scenarios", methods=["GET"])
@require_permission_node("settings.knowledge_bases")
def list_scenario_knowledge():
    return success_response(data=_scenario_knowledge_items(request.args.get("search", "")))


@bp.route("/api/knowledge-bases/scenarios/<int:scenario_id>", methods=["DELETE"])
@require_permission_node("settings.knowledge_bases")
def delete_scenario_knowledge(scenario_id: int):
    configured_dir = Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))
    roots = [configured_dir] + ([] if configured_dir == Path(SCENARIOS_DIR) else [Path(SCENARIOS_DIR)])
    descriptor = next((path for root in roots for path in scenario_descriptor_paths(root)
                       if str(load_scenario_record(path, root).get("id")) == str(scenario_id)), None)
    if descriptor is None:
        return error_response("Scenario not found", 404, "Scenario not found")
    record = load_scenario_record(descriptor, Path(configured_dir))
    purge = str(request.args.get("purge") or "").strip().lower() in {"1", "true", "yes"}
    if purge:
        # 归档剧本已从剧本页隐藏，网页端没有入口再次删除；知识库管理页提供「彻底删除」
        # 作为清理入口（会连同剧本记录、知识索引、向量与封面一起移除）。
        if not record.get("archived"):
            return error_response("仅可彻底删除已归档剧本；正常剧本请到剧本页删除", 400, "Scenario not archived")
        role = str(session.get("role") or "").upper()
        if role not in {"ADMIN", "OWNER"} and str(record.get("owner_id")) != str(session.get("user_id")):
            return error_response("Permission denied", 403, "Permission denied")
        from trpg_server.scenario_admin import delete_scenario as force_delete_scenario
        result = force_delete_scenario(
            configured_dir,
            current_app.config.get("ROOMS_DIR", ROOMS_DIR),
            scenario_id,
            current_app.extensions.get("vector_store"),
            force=True,
        )
        log_user_action(
            logger,
            user_action_text(session.get("username"), "彻底删除了归档剧本"),
            用户ID=session.get("user_id"),
            剧本ID=scenario_id,
        )
        return success_response(data=result, message="Scenario purged")
    vector_store = current_app.extensions.get("vector_store")
    deleted_vectors = int(vector_store.delete_by_filter({"scenario_id": str(scenario_id)})) if vector_store else 0
    index_dir = descriptor.parent / "knowledge-index"
    deleted_files = 0
    if index_dir.exists():
        for path in index_dir.glob("*.json"):
            path.unlink()
            deleted_files += 1
        try:
            index_dir.rmdir()
        except OSError:
            pass
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了知识库源文件"),
        用户ID=session.get("user_id"),
        剧本ID=scenario_id,
        文件数=deleted_files,
    )
    return success_response(data={"scenario_id": scenario_id, "deleted_vectors": deleted_vectors, "deleted_files": deleted_files})


@bp.route("/api/knowledge-bases/rulesets", methods=["GET"])
@require_permission_node("settings.knowledge_bases")
def list_rulesets():
    return success_response(data=_store().list_rulesets())


@bp.route("/api/knowledge-bases/<ruleset_id>/sources", methods=["GET"])
@require_permission_node("settings.knowledge_bases")
def list_sources(ruleset_id):
    return success_response(data=_store()._meta(ruleset_id).get("sources", []))


@bp.route("/api/knowledge-bases/<ruleset_id>/sources", methods=["POST"])
@require_permission_node("settings.knowledge_bases")
def upload_source(ruleset_id):
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename or not is_allowed_upload(uploaded.filename, ALLOWED_EXTENSIONS):
        return error_response("Unsupported or missing file", 400)
    raw = uploaded.read()
    max_size = int(current_app.config.get("KNOWLEDGE_BASE_MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
    if len(raw) > max_size:
        return error_response("File is too large", 413)
    try:
        source = _store().upload_source(ruleset_id, uploaded.filename, raw, request.form.get("locale", "zh-CN"))
        result = _store().reindex(ruleset_id)
        log_user_action(
            logger,
            user_action_text(session.get("username"), "上传了知识库源文件"),
            用户ID=session.get("user_id"),
            文件名=uploaded.filename,
        )
        return success_response(data={"source": source, "version": result}, status=201)
    except (ValueError, OSError) as exc:
        return error_response(str(exc), 400)
    except Exception:
        logger.exception("Failed to upload and index ruleset source: %s", ruleset_id)
        return error_response("Failed to upload and index source", 500)


@bp.route("/api/knowledge-bases/<ruleset_id>/reindex", methods=["POST"])
@require_permission_node("settings.knowledge_bases")
def reindex(ruleset_id):
    try:
        return success_response(data=_store().reindex(ruleset_id))
    except (ValueError, OSError) as exc:
        return error_response(str(exc), 400)
    except Exception:
        logger.exception("Failed to reindex ruleset knowledge base: %s", ruleset_id)
        return error_response("Failed to reindex knowledge base", 500)


@bp.route("/api/knowledge-bases/<ruleset_id>/enable", methods=["POST"])
@require_permission_node("settings.knowledge_bases")
def enable(ruleset_id):
    store = _store(); meta = store._meta(ruleset_id); meta["enabled"] = True; data = store._registry(); data[ruleset_id] = meta; store._save_registry(data)
    return success_response(data=meta)


@bp.route("/api/knowledge-bases/<ruleset_id>/archive", methods=["POST"])
@require_permission_node("settings.knowledge_bases")
def archive(ruleset_id):
    return success_response(data=_store().archive(ruleset_id))


@bp.route("/api/rooms/<room_id>/rulesets", methods=["POST"])
@require_permission_node("settings.knowledge_bases")
def bind_room_ruleset(room_id):
    payload = request.get_json(silent=True) or {}
    ruleset_id = payload.get("ruleset_id")
    if not ruleset_id:
        return error_response("ruleset_id is required", 400)
    try:
        return success_response(data=_store().bind_room(room_id, ruleset_id, payload.get("knowledge_version")))
    except (ValueError, OSError) as exc:
        return error_response(str(exc), 400)
