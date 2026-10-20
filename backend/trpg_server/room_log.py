"""房间级日志（内嵌 Web 控制台）存储与事件桥接。

房间事件不再写入 root logger（避免刷屏服务器控制台），而是落到房间目录下的
``console.jsonl``（JSONL 追加写，超限裁剪），随房间目录一起保留、随房间归档
一并归档。本模块不依赖 Flask 上下文，仅使用 ``json_store`` / ``settings``，
并通过注入的广播回调把新条目实时推送给房间内的在线用户。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from trpg_server.json_store import read_json
from trpg_server.logging_config import log_user_action, redact_sensitive
from trpg_server.settings import CONFIG_DIR, ROOMS_DIR

logger = logging.getLogger(__name__)

ROOM_LOG_FILENAME = "console.jsonl"
MAX_ROOM_LOG_ENTRIES = 500
ARCHIVE_LOG_LIMIT = 1000
ROOM_LOG_LEVELS = ("debug", "info", "warning", "error")

DEFAULT_ROOM_CONSOLE_SETTINGS = {
    "enabled": True,
    "mirror_to_server_console": False,
    "max_entries": MAX_ROOM_LOG_ENTRIES,
    "view_role": "admin",
    "execute_role": "admin",
}

# 读取 general.toml 的 (路径, mtime) 缓存，避免每条日志都重新解析配置文件。
_settings_cache: tuple[str, float, dict[str, Any]] | None = None
# 并发追加保护：后台自动存档线程与请求线程都可能写同一房间的日志。
_log_lock = threading.Lock()
# 房间日志广播回调：(room_id, entry) -> None。由 socket 层注入，保持本模块与 Flask 解耦。
_broadcast_callback: Callable[[str, dict[str, Any]], None] | None = None


def set_room_log_broadcaster(callback: Callable[[str, dict[str, Any]], None] | None) -> None:
    """注入房间日志广播回调（传入 ``None`` 可清除）。"""
    global _broadcast_callback
    _broadcast_callback = callback


def room_log_path(room_dir: Any) -> Path:
    return Path(room_dir) / ROOM_LOG_FILENAME


def room_console_settings(config_dir: Any = None) -> dict[str, Any]:
    """读取 ``general.toml`` 的 ``[room_console]`` 段（带 mtime 缓存）。"""
    global _settings_cache
    path = Path(config_dir or CONFIG_DIR) / "general.toml"
    cache_key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return dict(DEFAULT_ROOM_CONSOLE_SETTINGS)
    if _settings_cache is not None and _settings_cache[0] == cache_key and _settings_cache[1] == mtime:
        return dict(_settings_cache[2])
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    section = config.get("room_console") or {}
    defaults = DEFAULT_ROOM_CONSOLE_SETTINGS
    try:
        max_entries = int(section.get("max_entries", defaults["max_entries"]))
    except (TypeError, ValueError):
        max_entries = defaults["max_entries"]
    result = {
        "enabled": bool(section.get("enabled", defaults["enabled"])),
        "mirror_to_server_console": bool(section.get("mirror_to_server_console", defaults["mirror_to_server_console"])),
        "max_entries": max(50, min(max_entries, 5000)),
        "view_role": str(section.get("view_role") or defaults["view_role"]),
        "execute_role": str(section.get("execute_role") or defaults["execute_role"]),
    }
    _settings_cache = (cache_key, mtime, result)
    return dict(result)


def _read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        return []
    return entries


def _write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    temp_path = Path(temp.name)
    try:
        try:
            for entry in entries:
                temp.write(json.dumps(entry, ensure_ascii=False))
                temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        finally:
            temp.close()
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def append_room_log(
    room_dir: Any,
    message: str,
    *,
    level: str = "info",
    source: str = "event",
    actor: Any = None,
    room_id: Any = None,
    details: dict[str, Any] | None = None,
    max_entries: int | None = None,
    broadcast: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """追加一条房间日志；房间目录不存在时静默跳过，不影响主业务流程。"""
    if room_dir is None:
        return None
    target_dir = Path(room_dir)
    if not target_dir.exists():
        return None

    resolved_room_id = str(room_id) if room_id else _room_id_for(target_dir)
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": str(level or "info").lower(),
        "source": str(source or "event"),
        "actor": str(actor) if actor else None,
        "room_id": resolved_room_id,
        "message": str(message or ""),
        "details": redact_sensitive(details) if isinstance(details, dict) else {},
    }
    limit = max_entries if max_entries is not None else room_console_settings()["max_entries"]
    path = room_log_path(target_dir)
    try:
        with _log_lock:
            entries = _read_entries(path)
            entries.append(entry)
            if len(entries) > limit:
                entries = entries[-limit:]
            _write_entries(path, entries)
    except OSError:
        logger.warning("Failed to append room log for %s", target_dir, exc_info=True)
        return None

    sender = broadcast or _broadcast_callback
    if sender is not None and resolved_room_id:
        try:
            sender(resolved_room_id, entry)
        except Exception:  # noqa: BLE001 - 广播失败不得影响主流程
            logger.debug("Room log broadcast failed", exc_info=True)
    return entry


def read_room_log(
    room_dir: Any,
    *,
    limit: int = 200,
    level: str | None = None,
    search: str = "",
) -> dict[str, Any]:
    """读取房间日志（倒序保留最近 ``limit`` 条，可按级别/关键字过滤）。"""
    entries = _read_entries(room_log_path(room_dir))
    if level:
        wanted = str(level).lower()
        entries = [entry for entry in entries if str(entry.get("level")) == wanted]
    needle = str(search or "").strip().lower()
    if needle:
        entries = [
            entry
            for entry in entries
            if needle in json.dumps(entry, ensure_ascii=False).lower()
        ]
    total = len(entries)
    if limit and limit > 0:
        entries = entries[-limit:]
    return {"entries": entries, "truncated": total > len(entries)}


def load_room_log(room_dir: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    """加载房间日志用于归档（容忍文件缺失/损坏）。"""
    entries = _read_entries(room_log_path(room_dir))
    if limit and limit > 0:
        entries = entries[-limit:]
    return entries


def clear_room_log(room_dir: Any) -> int:
    """清空房间日志，返回被清除的条目数。"""
    with _log_lock:
        entries = _read_entries(room_log_path(room_dir))
        _write_entries(room_log_path(room_dir), [])
    return len(entries)


def _room_id_for(room_dir: Path) -> str:
    info = read_json(room_dir / "info.json", default={})
    if isinstance(info, dict) and info.get("id"):
        return str(info["id"])
    return room_dir.name


def _locate_room_dir(room_id: Any, rooms_dir: Any = None) -> Path | None:
    if not room_id:
        return None
    root = Path(rooms_dir) if rooms_dir else ROOMS_DIR
    direct = root / str(room_id)
    if (direct / "info.json").exists():
        return direct
    if not root.exists():
        return None
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        info = read_json(candidate / "info.json", default={})
        if isinstance(info, dict) and str(info.get("id")) == str(room_id):
            return candidate
    return None


def log_room_event(
    room_dir: Any = None,
    message: str = "",
    *,
    level: str = "info",
    actor: Any = None,
    source: str = "event",
    room_id: Any = None,
    rooms_dir: Any = None,
    logger_: logging.Logger | None = None,
    broadcast: Callable[[str, dict[str, Any]], None] | None = None,
    **details: Any,
) -> dict[str, Any] | None:
    """记录一条房间事件：写房间日志 + 广播；按设置可选镜像到服务器控制台。

    与 ``logging_config.log_user_action`` 的用法一致（位置参数为消息、其余为
    结构化详情），但默认不再进入服务器控制台 / 全局日志文件。
    """
    settings = room_console_settings()
    if not settings.get("enabled", True):
        if logger_ is not None:
            log_user_action(logger_, message, **details)
        return None

    target_dir = Path(room_dir) if room_dir is not None else _locate_room_dir(room_id, rooms_dir)
    if target_dir is None:
        if logger_ is not None:
            log_user_action(logger_, message, **details)
        return None

    entry = append_room_log(
        target_dir,
        message,
        level=level,
        source=source,
        actor=actor,
        room_id=room_id,
        details=details,
        max_entries=settings.get("max_entries"),
        broadcast=broadcast,
    )
    if settings.get("mirror_to_server_console") and logger_ is not None:
        log_user_action(logger_, message, **details)
    return entry
