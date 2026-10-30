import json
import logging
import time
from pathlib import Path
from urllib.parse import unquote

import requests
from flask import Blueprint, current_app, request, session

from trpg_server.json_store import read_json
from trpg_server import scenario_service
from trpg_server.media_defaults import normalize_cover
from trpg_server.logging_config import log_user_action, redact_sensitive, user_action_text
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.scenario_ai import load_enabled_platform, select_model, summary_role
from trpg_server.scenario_store import (
    delete_scenario_draft_file,
    delete_scenario_record,
    generate_public_id,
    iter_scenario_trigger_catalog,
    load_scenario_draft,
    load_scenario_record,
    save_scenario_draft_file,
    save_scenario_record,
    scenario_descriptor_paths,
    scenario_draft_path,
    scenario_public_asset_url,
    trigger_size_limit,
)
from trpg_server.agents.config import ai_debug_enabled, load_ai_runtime_config
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.agents.narrative.logger import log_event
from trpg_server.agents.narrative.publish import narrative_publish_blockers
from trpg_server.agents.narrative.store import find_scenario_storage
from trpg_server.agents.versioning import next_scenario_version, normalize_semver, scenario_content_changed
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.scenario_importer import convert_script_to_scenario, convert_with_ai, extract_script_text
from trpg_server.security import (
    build_public_asset_url,
    is_allowed_upload,
    normalize_filename,
    safe_join,
)
from trpg_server.settings import SCENARIO_COVERS_DIR, SCENARIOS_DIR, SCENARIO_DRAFTS_DIR

bp = Blueprint("scenarios", __name__)
logger = logging.getLogger(__name__)

_scenarios_cache = []
_cache_timestamp = 0
_cache_duration = 60
_allowed_cover_extensions = {"png", "jpg", "jpeg", "gif"}


def clear_scenarios_cache():
    global _scenarios_cache, _cache_timestamp

    _scenarios_cache = []
    _cache_timestamp = 0


def _current_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _cover_filename(value):
    return normalize_filename(unquote(str(value or "")))


def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _drafts_dir():
    return current_app.config.get("SCENARIO_DRAFTS_DIR", SCENARIO_DRAFTS_DIR)


def _draft_path():
    return scenario_draft_path(_drafts_dir(), session.get("user_id"))


def _scenarios_dir():
    """剧本根目录的唯一来源。

    统一走应用配置，避免本模块直接用模块级常量、而其它路由用 current_app.config，
    造成「一个地方写、另一个地方读」落到不同目录。
    """
    return Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))


def _index_scenario_knowledge(descriptor_path, scenario):
    """Persist vectors and emit an observable result for manual saves."""
    from trpg_server.agents.knowledge_base import persist_knowledge_index
    vector_store = current_app.extensions.get("vector_store")
    provider = current_app.extensions.get("embedding_provider")
    try:
        index_path = persist_knowledge_index(
            descriptor_path,
            scenario,
            vector_store=vector_store,
            embedding_provider=provider,
        )
        version = str(scenario.get("scenario_version") or scenario.get("version") or "1")
        count = int(vector_store.count({"scenario_id": str(scenario.get("id")), "scenario_version": version})) if vector_store else 0
        success = count > 0 or not scenario.get("modules")
        logger.info(
            "scenario_knowledge_vectorization scenario_id=%s version=%s success=%s vector_count=%d index_path=%s backend=%s",
            scenario.get("id"), version, success, count, index_path,
            (vector_store.health().get("backend") if vector_store else "none"),
        )
        return {"success": success, "vector_count": count, "path": str(index_path), "version": version}
    except Exception as exc:
        logger.exception("scenario_knowledge_vectorization scenario_id=%s success=False", scenario.get("id"))
        return {"success": False, "vector_count": 0, "error": str(exc)}


@bp.route("/api/scenarios/draft", methods=["GET"])
def get_scenario_draft():
    login_error = _require_login()
    if login_error: return login_error
    try:
        draft = load_scenario_draft(_drafts_dir(), session.get("user_id"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load draft")
        return server_error("Failed to load draft")
    if draft is None: return success_response(data=None, message="No draft")
    return success_response(data=draft, message="Draft loaded")


@bp.route("/api/scenarios/draft", methods=["POST"])
def save_scenario_draft():
    login_error = _require_login()
    if login_error: return login_error
    if not _can_use_permission("scenarios.create"): return error_response("Permission denied", 403, "Permission denied")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict): return error_response("Invalid draft data", 400)
    payload = dict(payload)
    payload.pop("id", None); payload["owner_id"] = session.get("user_id"); payload["updatedAt"] = _current_timestamp()
    try:
        saved = save_scenario_draft_file(_drafts_dir(), session.get("user_id"), payload)
        return success_response(data=saved, message="Draft saved")
    except OSError:
        logger.exception("Failed to save draft")
        return server_error("Failed to save draft")


@bp.route("/api/scenarios/draft", methods=["DELETE"])
def delete_scenario_draft():
    login_error = _require_login()
    if login_error: return login_error
    try:
        delete_scenario_draft_file(_drafts_dir(), session.get("user_id"))
        return success_response(message="Draft discarded")
    except OSError:
        logger.exception("Failed to discard draft")
        return server_error("Failed to discard draft")


def _can_use_permission(node_id):
    config_dir = current_app.config.get("CONFIG_DIR")
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(session.get("role", "USER"), node_id, config_path)


def _can_modify_scenario(scenario):
    if _can_use_permission("scenarios.manage_all"):
        return True
    return scenario.get("owner_id") == session.get("user_id")


def scenario_service_actor():
    """把当前会话转换成剧本领域服务需要的 Actor。"""
    from trpg_server.site_agent_auth import Actor, AgentAuthError, build_actor, permission_set_for_role

    try:
        return build_actor()
    except AgentAuthError:
        role = str(session.get("role") or "USER").upper()
        return Actor(
            user_id=int(session.get("user_id") or 0),
            username=str(session.get("username") or ""),
            role=role,
            permissions=permission_set_for_role(role),
            room_ids=frozenset(),
        )


def _trigger_size_limit():
    return trigger_size_limit(current_app.config.get("CONFIG_DIR"))


def _iter_scenario_files():
    return scenario_descriptor_paths(_scenarios_dir())


def _find_scenario_file(scenario_id):
    for path in _iter_scenario_files():
        try:
            data = load_scenario_record(path, _scenarios_dir())
        except (json.JSONDecodeError, OSError, ValueError):
            logger.exception("Failed to read scenario file: %s", path.name)
            continue

        if data.get("id") == scenario_id or str(data.get("id")) == str(scenario_id):
            return path, data

    return None, None


def _rooms_using_scenario(scenario_id):
    rooms_dir = Path(current_app.config.get("ROOMS_DIR", Path("data/runtime/rooms")))
    result = []
    if not rooms_dir.exists():
        return result
    for room_dir in rooms_dir.iterdir():
        if not room_dir.is_dir():
            continue
        info = read_json(room_dir / "info.json", default={})
        if str(info.get("scenario_id")) == str(scenario_id):
            result.append((room_dir, info))
    return result


def _remove_scenario_knowledge(scenario_id, descriptor_path=None):
    """删除剧本时同步清理知识库。

    剧本的向量条目保存在全局向量库中，删除剧本文件并不会移除它们；如果不显式
    清理，已删除剧本的知识仍可被检索到。这里同时移除向量库条目与知识索引文件。
    """
    deleted_vectors = 0
    vector_store = current_app.extensions.get("vector_store")
    if vector_store is not None:
        try:
            deleted_vectors = int(vector_store.delete_by_filter({"scenario_id": str(scenario_id)}))
        except Exception:
            logger.exception("Failed to delete scenario vectors: %s", scenario_id)
    if descriptor_path is not None:
        index_dir = Path(descriptor_path).parent / "knowledge-index"
        if index_dir.exists():
            for path in index_dir.glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    logger.exception("Failed to delete knowledge index file: %s", path)
            try:
                index_dir.rmdir()
            except OSError:
                pass
    return deleted_vectors


def load_scenarios():
    global _scenarios_cache, _cache_timestamp

    current_time = time.time()
    if current_time - _cache_timestamp < _cache_duration and _scenarios_cache:
        return _scenarios_cache

    scenarios_by_id = {}
    public_ids = set()
    for path in _iter_scenario_files():
        try:
            scenario = load_scenario_record(path, _scenarios_dir())
        except json.JSONDecodeError:
            logger.exception("Failed to parse scenario file: %s", path.name)
            continue
        except (OSError, ValueError):
            logger.exception("Failed to read scenario file: %s", path.name)
            continue

        if "id" not in scenario:
            try:
                scenario["id"] = int(path.stem.split("_")[-1])
            except (ValueError, IndexError):
                scenario["id"] = int(time.time() * 1000)
        public_id = str(scenario.get("public_id") or "")
        if len(public_id) != 6 or not public_id.isalnum() or public_id in public_ids:
            public_id = generate_public_id(public_ids)
            scenario["public_id"] = public_id
        trigger_catalog = iter_scenario_trigger_catalog(scenario)
        if trigger_catalog:
            scenario["trigger_catalog"] = trigger_catalog
        public_ids.add(public_id)
        scenario_id = str(scenario.get("id"))
        existing = scenarios_by_id.get(scenario_id)
        if existing is None or path.parent != _scenarios_dir():
            scenarios_by_id[scenario_id] = scenario

    scenarios = sorted(
        scenarios_by_id.values(),
        key=lambda item: item.get("updatedAt") or item.get("createdAt") or "",
        reverse=True,
    )

    _scenarios_cache = scenarios
    _cache_timestamp = current_time
    return scenarios


@bp.route("/api/scenarios", methods=["GET"])
def get_all_scenarios():
    try:
        scenarios = load_scenarios()
        # 归档（例如删除时仍有房间占用）的剧本对所有列表都应不可见，否则删除后
        # 其他玩家/其他链接仍会看到该剧本。
        visible = [scenario for scenario in scenarios if not scenario.get("archived")]
        return success_response(
            visible,
            f"Successfully loaded {len(visible)} scenarios",
        )
    except Exception as exc:
        logger.exception("Failed to load scenarios")
        return server_error("Failed to load scenarios")


@bp.route("/api/scenarios/<int:scenario_id>", methods=["GET"])
def get_scenario(scenario_id):
    try:
        login_error = _require_login()
        if login_error:
            return login_error
        if not _can_use_permission("scenarios.preview"):
            return error_response("Permission denied", 403, "Permission denied")

        scenario = next(
            (item for item in load_scenarios() if item.get("id") == scenario_id),
            None,
        )
        if scenario is None:
            return error_response("Scenario not found", 404, "Scenario not found")

        return success_response(scenario, "Scenario loaded successfully")
    except Exception as exc:
        logger.exception("Failed to load scenario: %s", scenario_id)
        return server_error("Failed to load scenario")


@bp.route("/api/scenarios/<int:scenario_id>/knowledge", methods=["GET"])
def get_scenario_knowledge(scenario_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    scenarios_dir = _scenarios_dir()
    descriptor = next((path for path in scenario_descriptor_paths(scenarios_dir)
                       if str(load_scenario_record(path, scenarios_dir).get("id")) == str(scenario_id)), None)
    if descriptor is None:
        return error_response("Scenario not found", 404, "Scenario not found")
    scenario = load_scenario_record(descriptor, scenarios_dir)
    version = str(scenario.get("scenario_version") or scenario.get("version") or "1")
    from trpg_server.agents.knowledge_base import knowledge_index_path
    vector_store = current_app.extensions.get("vector_store")
    # Include vectors written by older releases that stored numeric versions
    # (for example ``1``) before scenario versions were normalized to semver.
    count = int(vector_store.count({"scenario_id": str(scenario_id)})) if vector_store else 0
    health = vector_store.health() if vector_store else {"backend": "none"}
    return success_response({"scenario_id": scenario_id, "version": version, "vector_count": count,
                             "path": str(knowledge_index_path(descriptor, version)),
                             "backend": health.get("backend", "unknown")})


@bp.route("/api/scenarios/import", methods=["POST"])
@bp.route("/api/scenarios/import-script", methods=["POST"])
def import_script():
    """将剧本原文转换为可在编辑器中预览/导入的模块 JSON。

    此接口只做结构化，不自动创建剧本，避免一次误转换覆盖原稿；客户端确认
    后可把返回的 JSON 提交到普通创建接口。支持 JSON ``text`` 或 multipart ``file``。
    """
    try:
        login_error = _require_login()
        if login_error:
            return login_error
        if not (_can_use_permission("scenarios.create") or _can_use_permission("scenarios.edit")):
            return error_response("Permission denied", 403, "Permission denied")
        payload = request.get_json(silent=True) if request.is_json else None
        metadata = ({k: payload[k] for k in ("title", "description") if k in payload}
                    if isinstance(payload, dict) else {})
        if isinstance(payload, dict) and payload.get("text"):
            text = str(payload["text"])
        elif "file" in request.files:
            uploaded = request.files["file"]
            text = extract_script_text(
                uploaded.read(),
                uploaded.filename or "script.txt",
                ocr_provider=current_app.extensions.get("ocr_provider"),
            )
            fallback_title = Path(uploaded.filename or "Imported scenario").stem or "Imported scenario"
            metadata = {"title": str(request.form.get("title") or fallback_title),
                        "source_filename": uploaded.filename or ""}
        else:
            return error_response("Please provide script text or file", 400, "No script")
        logger.info("scenario_import.start user=%s filename=%s chars=%d", session.get("user_id"), metadata.get("source_filename", "text"), len(text))
        logger.debug("scenario_import.source_preview=%s", text[:2000].replace("\n", "\\n"))
        # Record the deterministic boundaries supplied to the AI.  This makes
        # it possible to distinguish an AI classification error from an
        # extraction/heading-detection error when reviewing a report.
        try:
            from trpg_server.scenario_importer import analyze_script_structure, _split_sections
            structure = analyze_script_structure(text)
            logger.info(
                "scenario_import.structure sections=%d headings=%s types=%s",
                structure["section_count"],
                json.dumps(structure["headings"], ensure_ascii=False),
                json.dumps(structure["detected_types"], ensure_ascii=False),
            )
            logger.debug(
                "scenario_import.section_manifest=%s",
                json.dumps(
                    [{"source_order": i, "title": title, "chars": len(content)} for i, (title, content) in enumerate(_split_sections(text), 1)],
                    ensure_ascii=False,
                ),
            )
        except ValueError:
            raise
        # Use the configured scenario conversion role when an AI provider is
        # available. The local converter remains the safe fallback.
        result = None
        role = summary_role()
        selected_platform, platform_config = load_enabled_platform(role.get("provider"))
        if platform_config:
            api_key = platform_config.get("config", {}).get("api_key")
            base_url = platform_config.get("config", {}).get("base_url")
            if selected_platform == "lmstudio" and not api_key: api_key = "lm-studio"
            base_url = chat_completions_endpoint(base_url)
            if base_url and api_key:
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                model = select_model(platform_config)
                # Conversion prompts contain the full source document and ask
                # for structured JSON. They legitimately take longer than a
                # short chat completion; never reuse the 20s chat timeout.
                configured_timeout = int(platform_config.get("config", {}).get("timeout", 60) or 60)
                timeout = max(120, configured_timeout)
                runtime_config = load_ai_runtime_config(current_app.config.get("CONFIG_DIR"))
                def request_ai(ai_payload):
                    # 完整的请求/响应体会包含整段提示词与模型输出，只有开启调试模式
                    # 才写日志；正常导入时保持安静，避免日志被大剧本刷满。
                    debug = ai_debug_enabled(current_app.config.get("CONFIG_DIR"))
                    if debug:
                        request_log = {
                            "url": base_url,
                            "timeout": timeout,
                            "headers": redact_sensitive(headers),
                            "json": redact_sensitive(ai_payload),
                        }
                        logger.info(
                            "scenario_import.ai_http_request_full=%s",
                            json.dumps(request_log, ensure_ascii=False, default=str),
                        )
                    started_at = time.monotonic()
                    try:
                        response = post_with_retry(
                            base_url,
                            headers=headers,
                            payload=ai_payload,
                            timeout=timeout,
                            max_retries=runtime_config.ai_request_max_retries,
                            post=requests.post,
                            label="scenario_import",
                        )
                    except (requests.exceptions.RequestException, RuntimeError):
                        logger.exception(
                            "scenario_import.ai_http_transport_error elapsed_seconds=%.3f",
                            time.monotonic() - started_at,
                        )
                        raise
                    try:
                        response_data = response.json()
                    except ValueError:
                        logger.exception(
                            "scenario_import.ai_http_invalid_response status=%s elapsed_seconds=%.3f body=%s",
                            response.status_code,
                            time.monotonic() - started_at,
                            response.text,
                        )
                        raise
                    if debug:
                        logger.info(
                            "scenario_import.ai_http_response_full=%s",
                            json.dumps(
                                redact_sensitive({
                                    "status": response.status_code,
                                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                                    "headers": dict(response.headers),
                                    "json": response_data,
                                }),
                                ensure_ascii=False,
                                default=str,
                            ),
                        )
                    if response.status_code != 200: raise ValueError(str(response_data.get("error") or f"AI request failed: {response.status_code}"))
                    return response_data
                try:
                    result = convert_with_ai(request_ai, text, model=model, metadata=metadata)
                except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
                    # AI enrichment is optional; a transient provider timeout
                    # must not discard the user's document. Return the local
                    # section-preserving conversion and keep the exact cause
                    # in the log for diagnosis.
                    logger.warning("scenario_import.ai_failed_fallback error_type=%s error=%s", type(exc).__name__, exc)
                    result = convert_script_to_scenario(text, metadata)
                    result.setdefault("conversion", {})["ai"] = {"status": "fallback", "error": str(exc)}
            else:
                logger.warning("scenario_import.ai_unavailable provider=%s reason=incomplete_config", selected_platform)
        if result is None:
            result = convert_script_to_scenario(text, metadata)
            logger.info("scenario_import.local_conversion modules=%d", len(result.get("modules", [])))
        log_user_action(logger, user_action_text(session.get("username"), "导入并解析剧本"),
                        用户ID=session.get("user_id"), 模块数=len(result.get("modules", [])),
                        文件名=metadata.get("source_filename", "text"), 转换版本=result.get("conversion", {}).get("version"))
        return success_response(result, "Script converted successfully")
    except (OSError, ValueError) as exc:
        logger.warning("Script import rejected: %s", exc)
        return error_response(str(exc), 400, "Invalid script")
    except Exception as exc:
        logger.exception("Failed to import script")
        return server_error("Failed to import script")


@bp.route("/api/scenarios", methods=["POST"])
def create_scenario():
    try:
        login_error = _require_login()
        if login_error:
            return login_error
        if not _can_use_permission("scenarios.create"):
            return error_response("Permission denied", 403, "Permission denied")

        scenario_data = request.get_json(silent=True)
        if not scenario_data:
            return error_response("Please provide scenario data", 400, "No data")
        try:
            saved_scenario = scenario_service.apply_create(scenario_service_actor(), scenario_data)
        except scenario_service.ScenarioServiceError as exc:
            return error_response(exc.message, exc.status, exc.message)

        draft_path = _draft_path()
        if draft_path.exists():
            draft_path.unlink()
        title = scenario_data.get("title", "unnamed")
        scenario_id = saved_scenario.get("id")
        log_user_action(
            logger,
            user_action_text(session.get("username"), "创建了剧本"),
            用户ID=session.get("user_id"),
            剧本ID=scenario_id,
            标题=title,
        )
        return success_response(
            saved_scenario,
            "Scenario created successfully",
            201,
        )
    except Exception as exc:
        logger.exception("Failed to create scenario")
        return server_error("Failed to create scenario")


@bp.route("/api/scenarios/<int:scenario_id>", methods=["PUT"])
def update_scenario(scenario_id):
    try:
        login_error = _require_login()
        if login_error:
            return login_error

        scenario_data = request.get_json(silent=True)
        if not isinstance(scenario_data, dict) or not scenario_data:
            return error_response("Please provide scenario data", 400, "No data")
        scenario_input = dict(scenario_data)
        force_narrative = bool(scenario_input.pop("force_narrative", False))

        target_file, existing_scenario = _find_scenario_file(scenario_id)
        if target_file is None:
            return error_response(
                f"Scenario with ID {scenario_id} does not exist",
                404,
                "Scenario not found",
            )

        if not _can_modify_scenario(existing_scenario):
            return error_response("Permission denied", 403, "Permission denied")
        if not _can_use_permission("scenarios.edit"):
            return error_response("Permission denied", 403, "Permission denied")

        _, narrative_dir = find_scenario_storage(_scenarios_dir(), str(scenario_id))
        blockers = narrative_publish_blockers(narrative_dir) if narrative_dir is not None else None
        if blockers is not None:
            if not force_narrative:
                return error_response(
                    "发布被阻止：世界树校验未通过",
                    409,
                    "narrative.publish_blocked",
                    data=blockers,
                    code="narrative_publish_blocked",
                )
            log_event(
                "publish.forced",
                scenario_id=scenario_id,
                user_id=session.get("user_id"),
                errors=blockers["errors"],
                needs_review=blockers["needs_review"],
            )

        try:
            saved_scenario = scenario_service.apply_update(
                scenario_service_actor(),
                scenario_id,
                scenario_input,
            )
        except scenario_service.ScenarioServiceError as exc:
            return error_response(exc.message, exc.status, exc.message)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "更新了剧本"),
            用户ID=session.get("user_id"),
            剧本ID=scenario_id,
            标题=saved_scenario.get("title", "unknown"),
        )
        return success_response(saved_scenario, "Scenario updated successfully")
    except Exception as exc:
        logger.exception("Failed to update scenario: %s", scenario_id)
        return server_error("Failed to update scenario")


@bp.route("/api/scenarios/<int:scenario_id>", methods=["DELETE"])
def delete_scenario(scenario_id):
    try:
        login_error = _require_login()
        if login_error:
            return login_error

        user_id = "unknown"
        request_data = request.get_json(silent=True)
        if request_data:
            user_id = request_data.get("user_id", "unknown")

        target_file, scenario_data = _find_scenario_file(scenario_id)
        if target_file is None:
            return error_response(
                f"Scenario with ID {scenario_id} does not exist",
                404,
                "Scenario not found",
            )

        if not _can_modify_scenario(scenario_data):
            return error_response("Permission denied", 403, "Permission denied")
        if not _can_use_permission("scenarios.delete"):
            return error_response("Permission denied", 403, "Permission denied")

        active_rooms = _rooms_using_scenario(scenario_id)
        if active_rooms:
            scenario_data["archived"] = True
            scenario_data["archivedAt"] = _current_timestamp()
            save_scenario_record(_scenarios_dir(), scenario_data, existing_descriptor=target_file, trigger_max_file_size=_trigger_size_limit())
            clear_scenarios_cache()
            return success_response({"archived": True, "active_rooms": len(active_rooms)}, "Scenario archived because active rooms still use it")
        delete_scenario_record(target_file)
        _remove_scenario_knowledge(scenario_id, target_file)
        cover_url = str((scenario_data or {}).get("cover") or "")
        if cover_url.startswith("/assets/scenarios/"):
            cover_path = safe_join(_scenarios_dir(), cover_url.replace("/assets/scenarios/", ""))
        else:
            cover_path = safe_join(SCENARIO_COVERS_DIR, f"{scenario_id}.png")
        if cover_path.exists():
            cover_path.unlink()

        clear_scenarios_cache()
        log_user_action(
            logger,
            user_action_text(session.get("username"), "删除了剧本"),
            用户ID=session.get("user_id") or user_id,
            剧本ID=scenario_id,
            标题=scenario_data.get("title", "unknown"),
        )
        return success_response(message="Scenario deleted successfully")
    except Exception as exc:
        logger.exception("Failed to delete scenario: %s", scenario_id)
        return server_error("Failed to delete scenario")


@bp.route("/api/scenarios/<int:scenario_id>/archive", methods=["POST"])
def archive_scenario(scenario_id):
    login_error = _require_login()
    if login_error:
        return login_error
    target_file, scenario_data = _find_scenario_file(scenario_id)
    if target_file is None:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_modify_scenario(scenario_data) or not _can_use_permission("scenarios.delete"):
        return error_response("Permission denied", 403, "Permission denied")
    scenario_data["archived"] = True
    scenario_data["archivedAt"] = _current_timestamp()
    save_scenario_record(_scenarios_dir(), scenario_data, existing_descriptor=target_file, trigger_max_file_size=_trigger_size_limit())
    clear_scenarios_cache()
    return success_response(load_scenario_record(target_file, _scenarios_dir()), "Scenario archived")


@bp.route("/api/scenarios/cover", methods=["POST"])
def upload_scenario_cover():
    try:
        login_error = _require_login()
        if login_error:
            return login_error

        user_id = session.get("user_id") or request.form.get("user_id", "unknown")
        if "cover" not in request.files:
            return error_response(
                "Please choose a scenario cover image",
                400,
                "No file",
            )

        cover = request.files["cover"]
        if not is_allowed_upload(cover.filename or "", _allowed_cover_extensions):
            return error_response(
                "Please upload an image file",
                400,
                "Invalid file type",
            )

        if (cover.content_length or 0) > 5 * 1024 * 1024:
            return error_response(
                "Cover file size must not exceed 5MB",
                400,
                "File too large",
            )

        scenario_title = request.form.get("scenario_title", str(int(time.time() * 1000)))
        filename = normalize_filename(f"{scenario_title}.png")
        file_path = safe_join(SCENARIO_COVERS_DIR, filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            file_path.unlink()

        cover.save(file_path)
        cover_url = build_public_asset_url("/assets/scenario_covers", filename)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "上传了剧本封面"),
            用户ID=user_id,
            文件=filename,
            剧本标题=scenario_title,
        )
        return success_response(
            {"cover_url": cover_url},
            "Cover uploaded successfully",
        )
    except Exception as exc:
        logger.exception("Failed to upload scenario cover")
        return server_error("Failed to upload cover")


@bp.route("/api/scenarios/cover", methods=["DELETE"])
def delete_scenario_cover():
    try:
        login_error = _require_login()
        if login_error:
            return login_error

        user_id = session.get("user_id", "unknown")
        data = request.get_json(silent=True)
        if not data or "cover_path" not in data:
            return error_response("Please provide cover path", 400, "No data")

        filename = _cover_filename(Path(data["cover_path"]).name)

        file_path = safe_join(SCENARIO_COVERS_DIR, filename)
        scenario_file_path = safe_join(_scenarios_dir(), data["cover_path"].replace("/assets/scenarios/", ""))
        if file_path.exists():
            file_path.unlink()
        elif scenario_file_path.exists():
            scenario_file_path.unlink()
        else:
            return error_response("Cover file does not exist", 404, "File not found")
        log_user_action(
            logger,
            user_action_text(session.get("username"), "删除了剧本封面"),
            用户ID=user_id,
            文件=filename,
        )
        return success_response(message="Cover deleted successfully")
    except Exception as exc:
        logger.exception("Failed to delete scenario cover")
        return server_error("Failed to delete cover")


@bp.route("/api/scenarios/cover/rename", methods=["POST"])
def rename_scenario_cover():
    try:
        login_error = _require_login()
        if login_error:
            return login_error

        user_id = session.get("user_id", "unknown")
        data = request.get_json(silent=True)
        if not data or ("old_filename" not in data and "old_path" not in data) or ("new_filename" not in data and "new_path" not in data):
            return error_response(
                "Please provide old and new filenames",
                400,
                "No data",
            )

        old_value = str(data.get("old_path") or data.get("old_filename") or "")
        new_value = str(data.get("new_path") or data.get("new_filename") or "")
        old_filename = _cover_filename(Path(old_value).name)

        old_candidates = [
            safe_join(SCENARIO_COVERS_DIR, old_filename),
            safe_join(_scenarios_dir(), old_value.replace("/assets/scenarios/", "")),
        ]
        old_file_path = next((candidate for candidate in old_candidates if candidate.exists()), None)
        if old_file_path is None:
            return error_response("Cover file does not exist", 404, "File not found")

        new_file_path = safe_join(_scenarios_dir(), new_value.replace("/assets/scenarios/", ""))
        new_file_path.parent.mkdir(parents=True, exist_ok=True)
        if new_file_path.exists():
            new_file_path.unlink()
        old_file_path.rename(new_file_path)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "重命名了剧本封面"),
            用户ID=user_id,
            原文件=old_value,
            新文件=new_value,
        )
        return success_response({"cover_url": build_public_asset_url("/assets/scenarios", new_value.replace("/assets/scenarios/", ""))}, "Cover renamed successfully")
    except Exception as exc:
        logger.exception("Failed to rename scenario cover")
        return server_error("Failed to rename cover")


@bp.route("/api/scenarios/list", methods=["GET"])
def get_scenario_list():
    try:
        files = []
        for path in _iter_scenario_files():
            try:
                stat = path.stat()
            except OSError:
                logger.exception("Failed to stat scenario file: %s", path.name)
                continue

            files.append(
                {
                    "filename": path.relative_to(_scenarios_dir()).as_posix(),
                    "size": stat.st_size,
                    "kind": "folder" if path.parent != _scenarios_dir() else "file",
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                    ),
                }
            )

        return success_response(
            {"files": files, "total": len(files)},
            "Scenario list loaded successfully",
        )
    except Exception as exc:
        logger.exception("Failed to list scenario files")
        return server_error("Failed to list scenario files")
