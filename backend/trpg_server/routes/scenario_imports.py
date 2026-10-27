from __future__ import annotations
import json, time
import logging
from pathlib import Path
from flask import Blueprint, Response, current_app, request, session
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.media_defaults import normalize_cover
from trpg_server.responses import error_response, success_response
from trpg_server.scenario_import_jobs import public_job_payload, submit_import_job
from trpg_server.scenario_documents import validate_scenario_upload, ScenarioDocumentError
from trpg_server.scenario_store import generate_public_id, load_scenario_record, save_scenario_record, scenario_descriptor_paths
from trpg_server.settings import SCENARIOS_DIR
from trpg_server.agents.knowledge_base import (
    KnowledgeBaseService,
    KnowledgeChunk,
    build_knowledge_chunks,
    index_knowledge_chunks,
    list_knowledge_sections,
    load_knowledge_index,
    persist_knowledge_index,
    update_knowledge_section,
    write_knowledge_index,
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

def _current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"

def _existing_public_ids(root: Path, *, exclude_id: object = None) -> set[str]:
    """收集已落盘的剧本编号，供导入发布时生成不重复的 6 位 public_id。"""
    values: set[str] = set()
    for path in scenario_descriptor_paths(root):
        try:
            record = load_scenario_record(path, root)
        except Exception:
            continue
        if exclude_id is not None and str(record.get("id")) == str(exclude_id):
            continue
        public_id = str(record.get("public_id") or "")
        if public_id:
            values.add(public_id)
    return values

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
    uploaded = request.files.get("file")
    if not uploaded: return error_response("Please provide a file", 400, "No file")
    raw = uploaded.read()
    try: validate_scenario_upload(uploaded.filename or "script.txt", len(raw), current_app.config["SCENARIO_IMPORT_MAX_BYTES"])
    except ScenarioDocumentError as exc: return error_response(str(exc), 400, "Invalid script")
    filename = Path(uploaded.filename or "script.txt").name
    job = _store().create(owner_id=str(session["user_id"]), filename=filename, metadata={k: request.form.get(k, "") for k in ("title", "author", "description", "public", "creator", "playerCount")})
    source = Path(_store().root) / job["id"] / "source" / filename; source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(raw)
    submit_import_job(current_app._get_current_object(), job["id"])
    return success_response({"jobId": job["id"], "scriptId": job["script_id"]}, "Import started", 202)

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

@bp.post("/api/scripts/import/<job_id>/retry")
def retry_import(job_id):
    if (e := _login()): return e
    job = _job(job_id)
    if not job: return error_response("Import job not found", 404, "Not found")
    stage = (request.get_json(silent=True) or {}).get("stage") or job.get("failed_stage") or "parsing"
    _store().update(job_id, status="pending", retry_count=int(job.get("retry_count", 0)) + 1, error=None)
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
    scenario = job.get("preview") or _store().load_intermediate(job["id"], "preview", {})
    scenario["id"] = script_id; scenario["scenario_version"] = str(job.get("target_version") or "1.0.0"); scenario["owner_id"] = session["user_id"]
    # 直接导入的剧本要和正常创建的剧本一样拥有 6 位 public_id；否则前端卡片/预览
    # 会回退显示毫秒时间戳，看起来像「一串数字」。
    # 编号查重与落盘必须用同一个根目录，否则会往 A 目录查重、往 B 目录写入。
    root = _scenarios_root()
    existing_public_ids = _existing_public_ids(root, exclude_id=script_id)
    public_id = str(scenario.get("public_id") or "")
    if len(public_id) != 6 or not public_id.isalnum() or public_id in existing_public_ids:
        scenario["public_id"] = generate_public_id(existing_public_ids)
    if not scenario.get("creator_username"):
        scenario["creator_username"] = session.get("username", "")
    timestamp = _current_timestamp()
    scenario.setdefault("createdAt", timestamp)
    scenario["updatedAt"] = timestamp
    scenario["cover"] = normalize_cover(scenario.get("cover"))
    descriptor = save_scenario_record(root, scenario)
    stored = load_scenario_record(descriptor, root)
    vector_store = current_app.extensions.get("vector_store")
    provider = current_app.extensions.get("embedding_provider")
    if str(scenario.get("import_mode") or "") == "direct":
        # 直接导入的剧本没有场景卡，知识块在管线中已按标题切分并嵌入；这里把
        # 这些块同时写入 JSON 索引，保证离线重建与检索仍然可用。
        raw_chunks = _store().load_intermediate(job["id"], "knowledge", []) or []
        chunks = []
        for item in raw_chunks:
            if isinstance(item, dict):
                try:
                    chunks.append(KnowledgeChunk(**item))
                except TypeError:
                    continue
        chunks = index_knowledge_chunks(chunks, vector_store=vector_store, embedding_provider=provider)
        write_knowledge_index(descriptor, stored.get("scenario_version") or scenario.get("scenario_version"), chunks)
    else:
        persist_knowledge_index(descriptor, stored, vector_store=vector_store, embedding_provider=provider)
    _store().update(job["id"], status="published", progress=100)
    # 世界树不再在导入发布时自动构建：作者打开图谱页签后点「自动建图」，
    # 或运行 narrative build 才会生成，避免未检查的图谱被静默写入。
    log_user_action(
        logger,
        user_action_text(session.get("username"), "发布了导入任务"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
    )
    return success_response(load_scenario_record(descriptor, root), "Published", 201)

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
