from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO
from datetime import timedelta
import logging
import sqlite3
import os
import json
import threading
import time
import requests
from pathlib import Path

from trpg_server.config import ensure_user_config
from trpg_server.config.paths import resolve_config_dir
from trpg_server.logging_config import configure_logging
from trpg_server.security import register_session_guard
from trpg_server.socket_events import register_socket_events
from trpg_server.settings import LOGS_DIR, SECRET_KEY, SESSION_COOKIE_SECURE, USERS_DIR, WEAPONS_DIR, ROOM_ARCHIVES_DIR
from trpg_server.settings import SCENARIO_IMPORTS_DIR, SCENARIO_IMPORT_MAX_BYTES, SCENARIO_IMPORT_WORKERS, SETUP_EMBEDDING_UPLOAD_MAX_BYTES
from trpg_server.settings import NARRATIVE_JOBS_DIR, NARRATIVE_GENERATE_WORKERS
from trpg_server.settings import VECTOR_DB_URL, VECTOR_DB_PATH, VECTOR_DB_API_KEY, VECTOR_BACKEND, VECTOR_BACKEND_EXPLICIT, EMBEDDED_VECTOR_DB_PATH, SCENARIOS_DIR, EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, OCR_ENABLED, OCR_LANG, LOCAL_EMBEDDING_MODEL_PATH, PADDLEOCR_HOME, AI_PLATFORM_SECRET_DIR, CONFIG_DIR
from trpg_server.agents.vector_store import create_vector_store
from trpg_server.agents.embedding_provider import select_embedding_provider
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.ai_platform_config import load_platform_config
from trpg_server.agents.ocr_provider import PaddleOcrProvider
from trpg_server.scenario_import_jobs import ImportJobStore
from trpg_server.narrative_jobs import NarrativeJobStore
from concurrent.futures import ThreadPoolExecutor
from trpg_server.users.database import UserDatabase
from trpg_server.users.migrations import migrate_json_users
from trpg_server.users.service import UserService

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
logger = logging.getLogger(__name__)

# 安全响应头。CSP 的白名单与前端实际引用的资源保持一致：
# 脚本来自自身站点、jsDelivr（Bootstrap/marked/DOMPurify）与 cdn.socket.io；
# 图片/媒体/请求目标放宽到 http(s)，因为 KP 可在消息里引用外链素材，
# 且 AI 平台的「测试连接」会直连管理员配置的任意地址（含本机 LM Studio）。
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.socket.io",
    "style-src 'self' 'unsafe-inline' https:",
    "img-src 'self' data: blob: https: http:",
    "media-src 'self' data: blob: https: http:",
    "font-src 'self' data: https:",
    "connect-src 'self' https: http: wss: ws:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.secret_key = SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=max(SCENARIO_IMPORT_MAX_BYTES, SETUP_EMBEDDING_UPLOAD_MAX_BYTES),
        SETUP_EMBEDDING_UPLOAD_MAX_BYTES=SETUP_EMBEDDING_UPLOAD_MAX_BYTES,
        USER_DATABASE_FILE=USERS_DIR / "users.sqlite3",
        USERS_FILE=USERS_DIR / "users.json",
        USER_IP_CONFIG_DIR=USERS_DIR / "ip_configs",
        WEAPONS_DIR=WEAPONS_DIR,
        ROOM_ARCHIVES_DIR=ROOM_ARCHIVES_DIR,
        SCENARIO_IMPORTS_DIR=SCENARIO_IMPORTS_DIR,
        SCENARIO_IMPORT_MAX_BYTES=SCENARIO_IMPORT_MAX_BYTES,
        SCENARIO_IMPORT_WORKERS=SCENARIO_IMPORT_WORKERS,
        NARRATIVE_JOBS_DIR=NARRATIVE_JOBS_DIR,
        NARRATIVE_GENERATE_WORKERS=NARRATIVE_GENERATE_WORKERS,
        VECTOR_DB_URL=VECTOR_DB_URL, VECTOR_DB_PATH=VECTOR_DB_PATH, VECTOR_DB_API_KEY=VECTOR_DB_API_KEY,
        VECTOR_BACKEND=VECTOR_BACKEND, VECTOR_BACKEND_EXPLICIT=VECTOR_BACKEND_EXPLICIT, EMBEDDED_VECTOR_DB_PATH=EMBEDDED_VECTOR_DB_PATH,
        SCENARIOS_DIR=SCENARIOS_DIR,
        EMBEDDING_BASE_URL=EMBEDDING_BASE_URL, EMBEDDING_API_KEY=EMBEDDING_API_KEY, EMBEDDING_MODEL=EMBEDDING_MODEL,
        EMBEDDING_DIMENSIONS=EMBEDDING_DIMENSIONS, OCR_ENABLED=OCR_ENABLED, OCR_LANG=OCR_LANG,
        LOCAL_EMBEDDING_MODEL_PATH=LOCAL_EMBEDDING_MODEL_PATH,
    )
    if config:
        app.config.update(config)
    # --config-dir CLI 覆盖经 paths.resolve_config_dir() 生效；测试注入的 CONFIG_DIR 优先。
    app.config.setdefault("CONFIG_DIR", resolve_config_dir())
    # 首次启动（含测试注入的临时 CONFIG_DIR）：从模板生成缺失的用户配置，幂等不覆盖。
    ensure_user_config(app.config.get("CONFIG_DIR", CONFIG_DIR))
    # The global body limit is the largest of the per-feature allowances (scenario
    # imports, setup embedding model uploads).  Each route still enforces its own
    # smaller limit internally (or via its own validator), so Flask must not reject
    # a legitimate request first with an HTML 413 page.
    configure_logging(app.config.get("LOGS_DIR", LOGS_DIR), app.config.get("CONFIG_DIR", CONFIG_DIR))
    if "USER_MANAGER" not in app.config:
        _configure_user_service(app)
    _clear_legacy_default_scenario_covers(app)
    CORS(app)
    socketio.init_app(app)
    app.extensions["socketio"] = socketio
    register_session_guard(app)
    register_blueprints(app)
    app.extensions["scenario_import_store"] = ImportJobStore(app.config["SCENARIO_IMPORTS_DIR"])
    app.extensions["scenario_import_store"].recover_interrupted()
    app.extensions["scenario_import_executor"] = ThreadPoolExecutor(max_workers=app.config["SCENARIO_IMPORT_WORKERS"])
    # 世界树生成向导：任务状态持久化在独立目录，预览结果只落 result.json，
    # 不触碰剧本目录下的 narrative.json（应用草稿仍走 PUT /narrative）。
    app.extensions["narrative_generate_store"] = NarrativeJobStore(app.config["NARRATIVE_JOBS_DIR"])
    app.extensions["narrative_generate_store"].recover_interrupted()
    app.extensions["narrative_generate_executor"] = ThreadPoolExecutor(
        max_workers=max(1, int(app.config.get("NARRATIVE_GENERATE_WORKERS") or 1))
    )
    # 云端 embedding 接口默认关闭：环境变量没显式配置时，只有管理员在设置页开启后才会读取
    # AI 平台配置里的 embedding 段；关闭时一律回落到本地模型（可用时）或哈希向量。
    cloud_embedding_enabled = load_ai_runtime_config(app.config["CONFIG_DIR"]).cloud_embedding_enabled
    embedding_url, embedding_key, embedding_model, embedding_dimensions = app.config["EMBEDDING_BASE_URL"], app.config["EMBEDDING_API_KEY"], app.config["EMBEDDING_MODEL"], app.config["EMBEDDING_DIMENSIONS"]
    if cloud_embedding_enabled and not (embedding_url and embedding_key and embedding_model):
        platform_dir = Path(app.config.get("AI_PLATFORM_DIR", app.config["CONFIG_DIR"] / "aiplatform"))
        secret_dir = Path(app.config.get("AI_PLATFORM_SECRET_DIR", AI_PLATFORM_SECRET_DIR))
        for public_path in sorted(platform_dir.glob("*.json")):
            candidate = load_platform_config(public_path, secret_dir / public_path.name)
            embedding = candidate.get("embedding") if isinstance(candidate.get("embedding"), dict) else {}
            key = candidate.get("config", {}).get("api_key")
            if candidate.get("enabled") and embedding.get("base_url") and embedding.get("model") and key:
                embedding_url, embedding_key, embedding_model = embedding["base_url"], key, embedding["model"]
                embedding_dimensions = int(embedding.get("dimensions") or embedding_dimensions)
                break
    app.extensions["embedding_provider"] = select_embedding_provider(local_model_path=app.config["LOCAL_EMBEDDING_MODEL_PATH"], base_url=embedding_url, api_key=embedding_key, model=embedding_model, dimensions=embedding_dimensions)
    embedding_health = app.extensions["embedding_provider"].health()
    if embedding_health.get("loaded"):
        logger.info("embedding_model_loaded backend=%s model=%s dimensions=%s", embedding_health.get("backend"), embedding_health.get("model", ""), embedding_health.get("dimensions"))
        if embedding_health.get("backend") == "hashed":
            # 已降级到哈希向量：给出可操作的补救方式，而不是只报一条含混的“已加载”。
            logger.warning("embedding_backend_degraded backend=hashed hint=install_sentence_transformers_or_enable_cloud_embedding")
    else:
        logger.warning("embedding_model_load_failed backend=%s model=%s error=%s", embedding_health.get("backend"), embedding_health.get("model", ""), embedding_health.get("error", "unknown"))
    provider_dimensions = getattr(app.extensions["embedding_provider"], "dimensions", None) or embedding_dimensions
    selected_backend = app.config["VECTOR_BACKEND"]
    if app.config.get("VECTOR_DB_URL") and not app.config.get("VECTOR_BACKEND_EXPLICIT"):
        selected_backend = "qdrant"
    vector_path = app.config["EMBEDDED_VECTOR_DB_PATH"] if selected_backend == "embedded" else app.config["VECTOR_DB_PATH"]
    app.extensions["vector_store"] = create_vector_store(
        backend=selected_backend,
        url=app.config["VECTOR_DB_URL"],
        path=vector_path,
        api_key=app.config["VECTOR_DB_API_KEY"],
        dimensions=provider_dimensions,
    )
    if selected_backend == "embedded" and app.extensions["vector_store"].count() == 0:
        try:
            from trpg_server.agents.vector_migration import migrate_json_indexes

            # Startup migration uses the lightweight deterministic fallback; normal
            # imports still use the configured provider in their embedding stage.
            migrate_json_indexes(app.config["SCENARIOS_DIR"], app.extensions["vector_store"])
        except Exception:
            logger.debug("Skipping automatic JSON vector migration", exc_info=True)
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(PADDLEOCR_HOME))
    app.extensions["ocr_provider"] = PaddleOcrProvider(app.config["OCR_LANG"]) if app.config["OCR_ENABLED"] else None
    app.config["OCR_PROVIDER"] = app.extensions["ocr_provider"]
    app.config["EMBEDDING_PROVIDER"] = app.extensions["embedding_provider"]
    app.config["VECTOR_STORE"] = app.extensions["vector_store"]
    _configure_scenario_chunk_analyzer(app)
    register_socket_events(socketio)
    _start_autosave_scheduler(app)

    @app.errorhandler(404)
    def _api_not_found(error):
        if request.path.startswith("/api/"):
            from trpg_server.responses import error_response

            return error_response("API endpoint not found", 404, str(error))
        return error

    @app.errorhandler(405)
    def _api_method_not_allowed(error):
        if request.path.startswith("/api/"):
            from trpg_server.responses import error_response

            return error_response("API method not allowed", 405, str(error))
        return error

    @app.errorhandler(413)
    def _api_request_too_large(error):
        if request.path.startswith("/api/"):
            from trpg_server.responses import error_response

            return error_response("Uploaded file is too large", 413, str(error))
        return error

    @app.errorhandler(500)
    def _api_internal_error(error):
        if request.path.startswith("/api/"):
            logger.exception("Unhandled API exception: %s", error)
            from trpg_server.responses import error_response

            return error_response("Internal server error", 500)
        return error

    @app.after_request
    def _apply_security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    return app


def _start_autosave_scheduler(app):
    """启动服务端自动存档调度线程（不再依赖玩家浏览器定时触发）。"""
    if app.config.get("TESTING") or app.config.get("AUTOSAVE_SCHEDULER_DISABLED"):
        return
    if app.extensions.get("autosave_scheduler_started"):
        return
    app.extensions["autosave_scheduler_started"] = True
    tick_seconds = max(15, int(app.config.get("AUTOSAVE_SCHEDULER_TICK", 30)))

    def _loop():
        from trpg_server.routes.rooms import run_scheduled_autosaves

        while True:
            time.sleep(tick_seconds)
            try:
                with app.app_context():
                    run_scheduled_autosaves()
            except Exception:
                logger.debug("Autosave scheduler tick failed", exc_info=True)

    threading.Thread(target=_loop, name="autosave-scheduler", daemon=True).start()


def _configure_scenario_chunk_analyzer(app):
    """Attach an optional per-chunk JSON analyzer using the configured AI API.

    The callback is deliberately small and injectable so tests and offline
    installations can use deterministic metadata.  When an enabled platform is
    configured, each chunk is sent as a separate request rather than sending the
    complete source document in one oversized prompt.
    """
    if callable(app.config.get("SCENARIO_CHUNK_ANALYZER")):
        return
    platform_dir = Path(app.config.get("AI_PLATFORM_DIR", app.config["CONFIG_DIR"] / "aiplatform"))
    secret_dir = Path(app.config.get("AI_PLATFORM_SECRET_DIR", AI_PLATFORM_SECRET_DIR))
    for public_path in sorted(platform_dir.glob("*.json")):
        try:
            candidate = load_platform_config(public_path, secret_dir / public_path.name)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.debug("Skipping invalid AI platform config for chunk analyzer: %s", public_path, exc_info=True)
            continue
        config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
        base_url = chat_completions_endpoint(config.get("base_url"))
        api_key = str(config.get("api_key") or "").strip()
        if not candidate.get("enabled") or not base_url or not api_key:
            continue
        models = candidate.get("models") if isinstance(candidate.get("models"), list) else []
        model = str(next((item.get("id") for item in models if isinstance(item, dict) and item.get("enabled", True)), "local-model"))
        timeout = max(15, min(int(config.get("timeout", 60) or 60), 90))

        def analyze(chunk, index, total, *, _url=base_url, _key=api_key, _model=model, _timeout=timeout):
            payload = {
                "model": _model,
                "messages": [
                    {"role": "system", "content": "只根据原文抽取 JSON，不要创作。字段：card_type, scene_id, spoiler_level, visibility, unlock_condition, summary。"},
                    {"role": "user", "content": json.dumps({"chunk_index": index, "chunk_total": total, "text": chunk.text}, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            response = requests.post(_url, headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key}"}, json=payload, timeout=_timeout)
            response.raise_for_status()
            body = response.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            value = json.loads(content) if isinstance(content, str) else content
            return value if isinstance(value, dict) else {}

        app.config["SCENARIO_CHUNK_ANALYZER"] = analyze
        logger.info("scenario_chunk_analyzer_configured platform=%s model=%s", public_path.stem, model)
        return


def _configure_user_service(app):
    db = UserDatabase(app.config["USER_DATABASE_FILE"])
    db.initialize()
    try:
        migrate_json_users(app.config["USERS_FILE"], db)
    except (ValueError, sqlite3.Error):
        logger.exception("Skipping legacy user migration because users JSON is invalid")
    app.config["USER_MANAGER"] = UserService(
        db,
        ip_config_dir=app.config["USER_IP_CONFIG_DIR"],
    )


def _clear_legacy_default_scenario_covers(app):
    """一次性清理历史剧本封面：项目已不再使用内置默认封面图。"""

    from trpg_server.scenario_store import clear_legacy_default_covers

    try:
        cleaned = clear_legacy_default_covers(Path(app.config["SCENARIOS_DIR"]))
    except Exception:
        logger.debug("Skipping legacy scenario cover cleanup", exc_info=True)
        return
    if cleaned:
        logger.info("cleared_legacy_default_scenario_covers count=%s", cleaned)


def register_blueprints(app):
    from trpg_server.routes.assets import bp as assets_bp
    from trpg_server.routes.auth import bp as auth_bp
    from trpg_server.routes.chat import bp as chat_bp
    from trpg_server.routes.characters import bp as characters_bp
    from trpg_server.routes.character_ai import bp as character_ai_bp
    from trpg_server.routes.config import bp as config_bp
    from trpg_server.routes.network import bp as network_bp
    from trpg_server.routes.narrative import bp as narrative_bp
    from trpg_server.routes.knowledge_bases import bp as knowledge_bases_bp
    from trpg_server.routes.pages import bp as pages_bp
    from trpg_server.routes.rooms import bp as rooms_bp
    from trpg_server.routes.room_narrative import bp as room_narrative_bp
    from trpg_server.routes.room_console import bp as room_console_bp
    from trpg_server.routes.dice_rolls import bp as dice_rolls_bp
    from trpg_server.routes.dice_textures import bp as dice_textures_bp
    from trpg_server.routes.scenarios import bp as scenarios_bp
    from trpg_server.routes.scenario_summary import bp as scenario_summary_bp
    from trpg_server.routes.setup import bp as setup_bp
    from trpg_server.routes.site_agent import bp as site_agent_bp
    from trpg_server.routes.scenario_imports import bp as scenario_imports_bp
    from trpg_server.routes.vector_health import bp as vector_health_bp
    from trpg_server.routes.telemetry import bp as telemetry_bp
    from trpg_server.routes.themes import bp as themes_bp
    from trpg_server.routes.triggers import bp as triggers_bp
    from trpg_server.routes.users import bp as users_bp

    app.register_blueprint(assets_bp)
    app.register_blueprint(scenarios_bp)
    app.register_blueprint(scenario_summary_bp)
    app.register_blueprint(scenario_imports_bp)
    app.register_blueprint(vector_health_bp)
    app.register_blueprint(characters_bp)
    app.register_blueprint(character_ai_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(site_agent_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(rooms_bp)
    app.register_blueprint(room_narrative_bp)
    app.register_blueprint(room_console_bp)
    app.register_blueprint(dice_rolls_bp)
    app.register_blueprint(dice_textures_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(knowledge_bases_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(telemetry_bp)
    app.register_blueprint(themes_bp)
    app.register_blueprint(triggers_bp)
    app.register_blueprint(narrative_bp)
