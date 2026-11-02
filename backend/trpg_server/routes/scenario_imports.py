from __future__ import annotations
import json
import logging
from pathlib import Path
from flask import Blueprint, Response, current_app, request, session
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.responses import error_response, success_response
from trpg_server.scenario_import_jobs import public_job_payload, submit_import_job
from trpg_server.scenario_documents import validate_scenario_upload, ScenarioDocumentError
from trpg_server.scenario_bundle import BundleError, build_narrative, normalize_bundle, scenario_from_result
from trpg_server.scenario_import_publish import publish_import_job
from trpg_server.scenario_store import (
    delete_scenario_record,
    find_scenario_descriptor_by_id,
    load_scenario_record,
    scenario_descriptor_paths,
)
from trpg_server.settings import SCENARIOS_DIR
from trpg_server.agents.knowledge_base import (
    KnowledgeBaseService,
    build_knowledge_chunks,
    list_knowledge_sections,
    load_knowledge_index,
    update_knowledge_section,
)

bp = Blueprint("scenario_imports", __name__)
logger = logging.getLogger(__name__)

def _store(): return current_app.extensions["scenario_import_store"]
def _job(job_id):
    job = _store().get(job_id)
    if not job or str(job.get("owner_id")) != str(session.get("user_id")): return None
    return job
def _login():
    if not session.get("user_id"): return error_response("Authentication required", 401, "Authentication required")
    return None

def _scenarios_root() -> Path:
    return Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))

def _script_descriptor(script_id):
    """按剧本 ID 定位描述符与记录，供知识块读写复用。"""
    root = _scenarios_root()
    for path in scenario_descriptor_paths(root):
        try:
            record = load_scenario_record(path, root)
        except Exception:
            continue
        if str(record.get("id")) == str(script_id):
            return path, record
    return None, None

def _can_edit_script(record) -> bool:
    role = str(session.get("role") or "").upper()
    return role in {"ADMIN", "OWNER"} or str(record.get("owner_id")) == str(session.get("user_id"))

def _script_chunks(descriptor, record):
    """优先读取已落盘的知识索引；缺失（尚未发布向量索引）时按剧本模块即时构建。"""
    version = record.get("scenario_version") or "1.0.0"
    chunks = load_knowledge_index(descriptor, version)
    return version, chunks or build_knowledge_chunks(record)

@bp.post("/api/scripts/import")
def create_import():
    if (e := _login()): return e
    # 每个玩家同时只允许一个导入任务：AI/向量化都在后台线程里跑，并发多个大剧本
    # 既会互相抢平台额度，也会让「后台队列」状态变得难以预测。
    active = _store().active_for_owner(str(session["user_id"]))
    if active:
        return error_response(
            "已有剧本正在后台导入，请等待完成或取消后再开始新的导入",
            409,
            "Import already running",
            jobId=active.get("id"),
            scriptId=active.get("script_id"),
            sourceFilename=active.get("source_filename"),
        )
    uploaded = request.files.get("file")
    if not uploaded: return error_response("Please provide a file", 400, "No file")
    raw = uploaded.read()
    try: validate_scenario_upload(uploaded.filename or "script.txt", len(raw), current_app.config["SCENARIO_IMPORT_MAX_BYTES"])
    except ScenarioDocumentError as exc: return error_response(str(exc), 400, "Invalid script")
    filename = Path(uploaded.filename or "script.txt").name
    mode = (request.form.get("mode") or "direct").strip().lower()
    if mode not in {"direct", "ai"}:
        mode = "direct"
    metadata = {k: request.form.get(k, "") for k in ("title", "author", "description", "public", "creator", "playerCount")}
    metadata["mode"] = mode
    metadata["autoPublish"] = request.form.get("autoPublish", "")
    job = _store().create(owner_id=str(session["user_id"]), filename=filename, metadata=metadata)
    source = Path(_store().root) / job["id"] / "source" / filename; source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(raw)
    submit_import_job(current_app._get_current_object(), job["id"])
    return success_response({"jobId": job["id"], "scriptId": job["script_id"]}, "Import started", 202)

@bp.get("/api/scripts/import/jobs")
def list_imports():
    """当前用户的导入任务列表（含一次都没打开过的后台任务）。

    列表每 4 秒被前端轮询一次，因此不带 ``preview``：审核时再按需拉取完整任务。
    """
    if (e := _login()): return e
    jobs = []
    for job in _store().list_by_owner(str(session["user_id"])):
        payload = public_job_payload(job)
        payload.pop("preview", None)
        jobs.append(payload)
    return success_response(jobs)

@bp.post("/api/scripts/import/bundle")
def import_bundle():
    """导入 Skill / 外部 AI 生成的剧本包（JSON）。不重新调用 AI，只做校验与修复。"""
    if (e := _login()): return e
    uploaded = request.files.get("file")
    payload = request.get_json(silent=True)
    if uploaded:
        raw = uploaded.read()
        if len(raw) > int(current_app.config["SCENARIO_IMPORT_MAX_BYTES"]):
            return error_response("剧本包过大", 413, "Bundle too large")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_response("剧本包不是合法 JSON", 400, "Invalid bundle")
    if not isinstance(payload, dict):
        return error_response("请提供剧本包 JSON", 400, "Missing bundle")
    bundle = payload.get("bundle") if isinstance(payload.get("bundle"), dict) else payload
    filename = Path(getattr(uploaded, "filename", "") or "scenario-bundle.json").name
    title = str((bundle.get("scenario") or {}).get("title") or "").strip() if isinstance(bundle.get("scenario"), dict) else ""
    result = normalize_bundle(
        bundle,
        fallback_title=Path(filename).stem,
        default_scenario_id="",
        default_version="1.0.0",
    )
    narrative, repair, validation = build_narrative(result) if result.ok else (None, None, None)
    if result.errors:
        return error_response(
            "剧本包校验失败",
            400,
            "；".join(str(item.get("message")) for item in result.errors[:5]),
        )
    if narrative is None or not narrative.nodes:
        return error_response("剧本包没有任何图谱节点", 400, "Empty nodes")
    metadata = {
        "title": title or result.data["scenario"].get("title") or Path(filename).stem,
        "author": str(result.data["scenario"].get("author") or ""),
        "creator": session.get("username", ""),
        "playerCount": str(result.data["scenario"].get("playerCount") or 1),
        "description": str(result.data["scenario"].get("notes") or ""),
        "mode": "bundle",
    }
    job = _store().create(owner_id=str(session["user_id"]), filename=filename, metadata=metadata)
    scenario = scenario_from_result(result)
    scenario["id"] = job["script_id"]
    scenario["scenario_version"] = "1.0.0"
    scenario["import_mode"] = "ai"
    scenario["import_exclusions"] = result.data.get("exclusions") or []
    scenario["conversion"] = {
        "version": 5,
        "source_preserved": True,
        "document_import": True,
        "bundle_import": True,
        "warnings": result.warnings,
        "repair": repair.to_dict() if repair else {},
        "stages": ["bundle", "normalize", "graph", "embed"],
    }
    scenario["narrative_summary"] = {
        "nodes": len(narrative.nodes),
        "edges": len(narrative.edges),
        "locations": len(narrative.location_nodes),
        "endings": len(narrative.ending_nodes),
        "repair_count": len(repair.applied) if repair else 0,
        "error_count": validation.to_dict().get("error_count", 0) if validation else 0,
        "warning_count": validation.to_dict().get("warning_count", 0) if validation else 0,
    }
    narrative.scenario_id = str(job["script_id"])
    narrative.meta = {
        **dict(narrative.meta or {}),
        "generator": "bundle_import",
        "bundle_format": result.data.get("format"),
        "exclusions": result.data.get("exclusions") or [],
    }
    _store().save_intermediate(job["id"], "bundle", result.payload())
    _store().save_intermediate(job["id"], "narrative", narrative.to_dict())
    _store().save_intermediate(job["id"], "preview", scenario)
    _store().update(job["id"], status="done", current_stage="done", progress=100, stage_progress=100, preview=scenario, stage_meta={"mode": "bundle"})
    log_user_action(
        logger,
        user_action_text(session.get("username"), "导入了剧本包"),
        用户ID=session.get("user_id"),
        剧本ID=job["script_id"],
    )
    return success_response({"jobId": job["id"], "scriptId": job["script_id"], "preview": scenario}, "Bundle imported", 201)


@bp.get("/api/scripts/import/<job_id>")
def get_import(job_id):
    if (e := _login()): return e
    job = _job(job_id)
    return success_response(public_job_payload(job)) if job else error_response("Import job not found", 404, "Not found")

@bp.get("/api/scripts/import/<job_id>/stream")
def stream_import(job_id):
    if (e := _login()): return e
    if not _job(job_id): return error_response("Import job not found", 404, "Not found")
    def events():
        last = None
        while True:
            job = _job(job_id)
            if not job: break
            marker = job.get("updated_at")
            if marker != last:
                last = marker; yield f"event: progress\ndata: {json.dumps(public_job_payload(job), ensure_ascii=False)}\n\n"
            if job.get("status") in {"done", "failed", "cancelled", "published"}: break
            time.sleep(1)
    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@bp.post("/api/scripts/import/<job_id>/cancel")
def cancel_import(job_id):
    if (e := _login()): return e
    if not _job(job_id): return error_response("Import job not found", 404, "Not found")
    return success_response(public_job_payload(_store().request_cancel(job_id)), "Cancellation requested")

@bp.delete("/api/scripts/import/<job_id>")
def abandon_import(job_id):
    """放弃一次失败/已取消的导入：删除任务目录、已写入的向量与知识索引、半成品剧本。

    直接导入会在向量化阶段边算边写，失败后向量条目会留在全局向量库中；单纯删除
    任务文件不足以清理，必须按 scenario_id 显式删除。
    """
    if (e := _login()): return e
    job = _job(job_id)
    if not job: return error_response("Import job not found", 404, "Not found")
    if job.get("status") not in {"failed", "cancelled"}:
        return error_response("只能放弃失败或已取消的导入；进行中的任务请先取消", 409, "Import still running")
    script_id = job.get("script_id")
    root = _scenarios_root()
    deleted_vectors = 0
    try:
        from trpg_server.scenario_admin import remove_knowledge
        removed = remove_knowledge(root, script_id, current_app.extensions.get("vector_store"))
        deleted_vectors = int(removed.get("deleted_vectors", 0))
    except Exception:  # noqa: BLE001 - 清理失败不应阻止任务目录删除
        logger.exception("scenario_import.abandon_knowledge_failed job=%s", job_id)
    # 正常流程只有发布时才写剧本记录；这里兜底清理「发布中途失败」留下的半成品。
    descriptor = find_scenario_descriptor_by_id(root, script_id)
    if descriptor is not None:
        try:
            delete_scenario_record(descriptor)
        except Exception:  # noqa: BLE001
            logger.exception("scenario_import.abandon_record_failed job=%s", job_id)
    _store().discard(job_id)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "放弃了一次剧本导入"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        清理向量=deleted_vectors,
    )
    return success_response(
        {"jobId": job_id, "scriptId": script_id, "deletedVectors": deleted_vectors},
        "Import abandoned",
    )

@bp.post("/api/scripts/import/<job_id>/retry")
def retry_import(job_id):
    if (e := _login()): return e
    job = _job(job_id)
    if not job: return error_response("Import job not found", 404, "Not found")
    active = _store().active_for_owner(str(session["user_id"]))
    if active and str(active.get("id")) != str(job_id):
        return error_response("已有剧本正在后台导入，请等待完成或取消后再重试", 409, "Import already running", jobId=active.get("id"))
    stage = (request.get_json(silent=True) or {}).get("stage") or job.get("failed_stage") or "parsing"
    # 允许同一用户重试：守卫只拦「新建导入」，重试会复用已完成检查点继续跑。
    _store().update(
        job_id,
        status="pending",
        retry_count=int(job.get("retry_count", 0)) + 1,
        error=None,
        cancel_requested=False,
    )
    submit_import_job(current_app._get_current_object(), job_id, start_stage=stage)
    return success_response(public_job_payload(_store().get(job_id)), "Retry started")

@bp.put("/api/scripts/import/<job_id>/preview")
def save_preview(job_id):
    if (e := _login()): return e
    if not _job(job_id): return error_response("Import job not found", 404, "Not found")
    payload = request.get_json(silent=True) or {}; _store().save_intermediate(job_id, "preview", payload); _store().update(job_id, preview=payload)
    return success_response(payload, "Preview saved")

@bp.post("/api/scripts/<int:script_id>/publish")
def publish_import(script_id):
    if (e := _login()): return e
    payload = request.get_json(silent=True) or {}; job = _job(str(payload.get("jobId", "")))
    if not job or int(job.get("script_id", 0)) != script_id: return error_response("Import job not found", 404, "Not found")
    stored = publish_import_job(
        _store(),
        current_app.config,
        job,
        root=_scenarios_root(),
        username=session.get("username", ""),
        user_id=session.get("user_id"),
    )
    return success_response(stored, "Published", 201)

@bp.get("/api/scripts/<int:script_id>/versions")
def versions(script_id):
    root = _scenarios_root()
    descriptor = next((p for p in scenario_descriptor_paths(root) if str(load_scenario_record(p, root).get("id")) == str(script_id)), None)
    if not descriptor: return error_response("Scenario not found", 404, "Not found")
    values = []
    for p in sorted((descriptor.parent / "versions").glob("*.json")):
        data = load_scenario_record(p, root); values.append({"version": p.stem, "created_at": data.get("updatedAt") or data.get("createdAt"), "card_count": len(data.get("modules", []))})
    return success_response(values)

@bp.post("/api/scripts/<int:script_id>/search")
def script_search(script_id):
    if (e := _login()): return e
    data = request.get_json(silent=True) or {}; return success_response(KnowledgeBaseService(rooms_dir=current_app.config.get("ROOMS_DIR"), scenarios_dir=_scenarios_root(), vector_store=current_app.extensions.get("vector_store"), embedding_provider=current_app.extensions.get("embedding_provider")).search(str(data.get("roomId", "")), str(data.get("query", "")), top_k=data.get("topK", 5)))

@bp.get("/api/scripts/<int:script_id>/knowledge")
def list_knowledge(script_id):
    """列出剧本各章节的世界书字段（触发词/常驻/分层等），供管理端编辑。"""
    if (e := _login()): return e
    descriptor, record = _script_descriptor(script_id)
    if not descriptor: return error_response("Scenario not found", 404, "Not found")
    if not _can_edit_script(record):
        log_access_denied(logger, user_action_text(session.get("username"), "访问剧本知识块被拒绝"), 用户ID=session.get("user_id"), 剧本ID=script_id)
        return error_response("Permission denied", 403, "Forbidden")
    _version, chunks = _script_chunks(descriptor, record)
    return success_response(list_knowledge_sections(chunks))

@bp.put("/api/scripts/<int:script_id>/knowledge")
def update_knowledge(script_id):
    """按章节更新世界书字段并落盘到知识索引（不影响已生成的向量，仅改写元数据）。"""
    if (e := _login()): return e
    descriptor, record = _script_descriptor(script_id)
    if not descriptor: return error_response("Scenario not found", 404, "Not found")
    if not _can_edit_script(record):
        log_access_denied(logger, user_action_text(session.get("username"), "修改剧本知识块被拒绝"), 用户ID=session.get("user_id"), 剧本ID=script_id)
        return error_response("Permission denied", 403, "Forbidden")
    payload = request.get_json(silent=True) or {}
    section_key = str(payload.get("sectionKey") or "").strip()
    values = payload.get("fields")
    if not section_key or not isinstance(values, dict):
        return error_response("sectionKey and fields are required", 400, "Invalid payload")
    version, chunks = _script_chunks(descriptor, record)
    if not chunks: return error_response("Knowledge base is empty", 404, "Not found")
    chunks, count = update_knowledge_section(chunks, section_key, values)
    if not count: return error_response("Knowledge section not found", 404, "Not found")
    write_knowledge_index(descriptor, version, chunks)
    log_user_action(logger, user_action_text(session.get("username"), "更新了剧本世界书字段"), 用户ID=session.get("user_id"), 剧本ID=script_id, 章节=section_key)
    return success_response(list_knowledge_sections(chunks), "Knowledge updated")
