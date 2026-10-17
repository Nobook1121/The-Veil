"""服务器巡检与诊断（控制台专用）。

汇总向量库 / embedding / OCR / AI 平台 / 用户库 / 关键数据目录的状态，并支持读取
最近日志尾部。全部为只读操作，不依赖 Flask 请求上下文，便于控制台线程与单测调用。
"""
from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path
from typing import Any

from trpg_server.agents.cache_probe import load_enabled_platforms
from trpg_server.settings import (
    AI_PLATFORM_SECRET_DIR,
    CONFIG_DIR,
    DATA_DIR,
    EMBEDDED_VECTOR_DB_PATH,
    KNOWLEDGE_BASES_DIR,
    LOGS_DIR,
    ROOMS_DIR,
    SCENARIOS_DIR,
    USERS_DIR,
)

logger = logging.getLogger(__name__)


def _dir_stats(path: Path) -> dict[str, Any]:
    """统计目录是否存在、文件数与总字节数（容错，不因个别文件失败而中断）。"""

    if not path.exists():
        return {"exists": False, "files": 0, "bytes": 0}
    total = 0
    files = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
            files += 1
        except OSError:
            continue
    return {"exists": True, "files": files, "bytes": total}


def data_directories() -> dict[str, Path]:
    """控制台巡检使用的关键目录集合。"""

    return {
        "data": Path(DATA_DIR),
        "config": Path(CONFIG_DIR),
        "scenarios": Path(SCENARIOS_DIR),
        "rooms": Path(ROOMS_DIR),
        "knowledge-bases": Path(KNOWLEDGE_BASES_DIR),
        "users": Path(USERS_DIR),
        "logs": Path(LOGS_DIR),
        "vector-db": Path(EMBEDDED_VECTOR_DB_PATH),
    }


def server_health(
    *,
    vector_store: Any = None,
    embedding_provider: Any = None,
    ocr_provider: Any = None,
    ai_platform_dir: Any = None,
    ai_platform_secret_dir: Any = None,
    user_manager: Any = None,
) -> dict[str, Any]:
    """收集各子系统健康状态，供 ``server health`` 打印或前端消费。"""

    vector = vector_store.health() if vector_store is not None and hasattr(vector_store, "health") else {"available": False}
    if embedding_provider is not None and hasattr(embedding_provider, "health"):
        embedding = embedding_provider.health()
    else:
        embedding = {"configured": bool(embedding_provider), "loaded": False}
    ocr = {"enabled": bool(ocr_provider), "available": bool(ocr_provider and getattr(ocr_provider, "available", False))}

    platform_dir = Path(ai_platform_dir) if ai_platform_dir else Path(CONFIG_DIR / "aiplatform")
    secret_dir = Path(ai_platform_secret_dir) if ai_platform_secret_dir else Path(AI_PLATFORM_SECRET_DIR)
    try:
        enabled = load_enabled_platforms(platform_dir, secret_dir)
        total_platforms = len([p for p in platform_dir.glob("*.json") if p.name != "default-request.json"]) if platform_dir.exists() else 0
        platforms = {"enabled": len(enabled), "total": total_platforms, "ids": [pid for pid, _ in enabled]}
    except Exception:  # noqa: BLE001 - 平台配置损坏不应影响巡检
        logger.exception("Failed to inspect AI platform configs")
        platforms = {"enabled": 0, "total": 0, "ids": []}

    users: dict[str, Any] = {"count": None, "owner_exists": None}
    if user_manager is not None and hasattr(user_manager, "get_all_users"):
        try:
            all_users = list(user_manager.get_all_users() or [])
            users = {
                "count": len(all_users),
                "owner_exists": any(str(u.get("role") or "").upper() == "OWNER" for u in all_users),
            }
        except Exception:  # noqa: BLE001
            logger.exception("Failed to inspect users")

    return {
        "vector": vector,
        "embedding": embedding,
        "ocr": ocr,
        "platforms": platforms,
        "users": users,
    }


def server_info() -> dict[str, Any]:
    """进程与磁盘信息（版本、Python、各数据目录占用）。"""

    version = "unknown"
    try:  # 后端包未必声明版本号，取不到就退回 unknown。
        import trpg_server

        version = str(getattr(trpg_server, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        pass
    directories = {name: _dir_stats(path) for name, path in data_directories().items()}
    return {
        "version": version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "data_dir": str(DATA_DIR),
        "directories": directories,
    }


def tail_log(log_dir: Any = None, lines: int = 50) -> dict[str, Any]:
    """读取日志目录中「最新修改」的日志文件末尾若干行。"""

    directory = Path(log_dir) if log_dir else Path(LOGS_DIR)
    if not directory.exists():
        return {"path": None, "lines": [], "detail": "log directory does not exist"}
    candidates = [p for p in directory.glob("*.log") if p.is_file()]
    if not candidates:
        return {"path": None, "lines": [], "detail": "no log file found"}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        content = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"path": str(latest), "lines": [], "detail": f"failed to read log: {exc}"}
    return {"path": str(latest), "lines": content[-max(1, lines):], "detail": ""}