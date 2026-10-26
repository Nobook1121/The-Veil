import os
import secrets
from pathlib import Path

# 路径常量的权威定义在 trpg_server.config.paths（纯 stdlib，可在 Flask 启动前使用），
# 这里导入后再导出，保持既有 ``settings.CONFIG_DIR`` 等引用不变。
from trpg_server.config.paths import BASE_DIR, CONFIG_DIR, DATA_DIR, TEMPLATE_DIR  # noqa: F401
from trpg_server.config.schema import DEFAULT_NETWORK_PORT

RUNTIME_DIR = DATA_DIR / "runtime"
FRONTEND_DIST_DIR = BASE_DIR / "dist" / "public"

SCENARIOS_DIR = DATA_DIR / "scenarios"
SCENARIO_DRAFTS_DIR = RUNTIME_DIR / "scenario_drafts"
CHARACTERS_DIR = RUNTIME_DIR / "characters"
CHARACTER_GALLERY_DIR = RUNTIME_DIR / "character_gallery"
SCENARIO_COVERS_DIR = DATA_DIR / "assets" / "scenario_covers"
AVATARS_DIR = DATA_DIR / "assets" / "avatars"
AI_PLATFORM_ASSETS_DIR = DATA_DIR / "assets" / "aiplatform"
VENDOR_ASSETS_DIR = DATA_DIR / "assets" / "vendor"
THEME_ASSETS_DIR = DATA_DIR / "assets" / "theme"
TOOLS_DIR = FRONTEND_DIST_DIR / "data" / "tools"
ROOMS_DIR = RUNTIME_DIR / "rooms"
ROOM_ARCHIVES_DIR = RUNTIME_DIR / "room_archives"
DEBUG_KP_PROMPT_FILE = CONFIG_DIR / "roles" / "debug-kp.md"
AI_PLATFORM_SECRET_DIR = RUNTIME_DIR / "config" / "aiplatform"
OCCUPATIONS_DIR = DATA_DIR / "occupations"
WEAPONS_DIR = DATA_DIR / "weapons"
# COC7 技能目录是随包发布的规则参考数据（不是用户配置），与职业/武器目录同级。
CHARACTER_SKILLS_FILE = DATA_DIR / "character_skills.json"
USERS_DIR = RUNTIME_DIR / "users"
HISTORY_DIR = RUNTIME_DIR / "history"
LOGS_DIR = RUNTIME_DIR / "logs"
KNOWLEDGE_BASES_DIR = RUNTIME_DIR / "knowledge-bases"
SCENARIO_IMPORTS_DIR = RUNTIME_DIR / "scenario_imports"
# 世界树生成向导的任务状态与预览结果；与剧本目录分离，避免污染已发布版本。
NARRATIVE_JOBS_DIR = RUNTIME_DIR / "narrative_jobs"
NARRATIVE_GENERATE_WORKERS = 2
# 网站 Agent：会话、待确认操作、审计与用量都隔离在独立目录，避免与房间数据混用。
AGENT_DIR = RUNTIME_DIR / "agent"
AGENT_MAX_CONVERSATIONS_PER_USER = 30
AGENT_MAX_MESSAGES_PER_CONVERSATION = 200
AGENT_ACTION_TTL_SECONDS = 300
AGENT_AUDIT_MAX_BYTES = 10 * 1024 * 1024
AGENT_AUDIT_KEEP_FILES = 5
SCENARIO_IMPORT_MAX_BYTES = 32 * 1024 * 1024
SCENARIO_IMPORT_WORKERS = 2
# 首次部署引导允许用户拖入本地 embedding 模型（bge-small-zh-v1.5 约 100MB），
# 需要一个比剧本导入更大的请求体上限；各接口仍会自行校验自己的体积限制。
SETUP_EMBEDDING_UPLOAD_MAX_BYTES = 256 * 1024 * 1024
VECTOR_DB_URL = os.environ.get("AI_TRPG_VECTOR_DB_URL", "")
VECTOR_DB_PATH = RUNTIME_DIR / "vector-db" / "qdrant"
VECTOR_DB_API_KEY = os.environ.get("AI_TRPG_VECTOR_DB_API_KEY", "")
VECTOR_BACKEND = os.environ.get("AI_TRPG_VECTOR_BACKEND", "").strip().lower() or "embedded"
VECTOR_BACKEND_EXPLICIT = bool(os.environ.get("AI_TRPG_VECTOR_BACKEND"))
EMBEDDED_VECTOR_DB_PATH = RUNTIME_DIR / "vector-db" / "embedded"
EMBEDDING_BASE_URL = os.environ.get("AI_TRPG_EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.environ.get("AI_TRPG_EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("AI_TRPG_EMBEDDING_MODEL", "")
EMBEDDING_DIMENSIONS = int(os.environ.get("AI_TRPG_EMBEDDING_DIMENSIONS", "256"))
OCR_ENABLED = os.environ.get("AI_TRPG_OCR_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
OCR_LANG = os.environ.get("AI_TRPG_OCR_LANG", "ch")
LOCAL_EMBEDDING_MODEL_PATH = Path(os.environ.get("AI_TRPG_LOCAL_EMBEDDING_MODEL_PATH", str(RUNTIME_DIR / "models" / "embedding" / "bge-small-zh-v1.5")))
PADDLEOCR_HOME = Path(os.environ.get("PADDLE_PDX_CACHE_HOME", str(RUNTIME_DIR / "models" / "paddleocr")))

# 遗留 JSON（site/network/penetration）已由 general.toml 的对应段取代，文件路径
# 常量随之删除；兼容读取见 trpg_server.config.sources.LEGACY_JSON_FILES。

DEFAULT_PORT = DEFAULT_NETWORK_PORT
PORT_RETRY_COUNT = 5
PORT_RETRY_INTERVAL = 2
DISCOVERY_PORT = 50000

def _load_secret_key() -> str:
    # 优先取环境变量；否则持久化到运行时目录，保证服务重启后已签发的会话仍可校验。
    env_secret = os.environ.get("AI_TRPG_SECRET_KEY")
    if env_secret:
        return env_secret

    secret_file = RUNTIME_DIR / "flask_secret_key.txt"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_key = secrets.token_urlsafe(64)
    secret_file.write_text(secret_key, encoding="utf-8")
    return secret_key


SECRET_KEY = _load_secret_key()
SESSION_COOKIE_SECURE = os.environ.get("AI_TRPG_SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}
