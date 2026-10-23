"""网站 Agent 的存储层：会话、待确认操作、审计与用量。

所有路径都以 ``AGENT_DIR`` 为根（可通过 app.config 覆盖，便于测试）。写入使用
``write_json_atomic``，并在进程内用一把可重入锁串行化，避免会话文件被并发写坏。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import (
    AGENT_ACTION_TTL_SECONDS,
    AGENT_AUDIT_KEEP_FILES,
    AGENT_AUDIT_MAX_BYTES,
    AGENT_DIR,
    AGENT_MAX_CONVERSATIONS_PER_USER,
    AGENT_MAX_MESSAGES_PER_CONVERSATION,
)

_STORE_LOCK = threading.RLock()
_CONVERSATION_ID_RE = re.compile(r"^c_[0-9a-f]{8,32}$")
_ACTION_ID_RE = re.compile(r"^a_[0-9a-f]{8,32}$")


def _agent_dir() -> Path:
    path = Path(current_app.config.get("AGENT_DIR", AGENT_DIR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _conversations_dir(user_id: Any) -> Path:
    return _agent_dir() / "conversations" / str(user_id)


def _conversation_path(user_id: Any, conversation_id: str) -> Path | None:
    if not _CONVERSATION_ID_RE.match(str(conversation_id or "")):
        return None
    return _conversations_dir(user_id) / f"{conversation_id}.json"


def _usage_path(user_id: Any, day: str | None = None) -> Path:
    return _agent_dir() / "usage" / str(user_id) / f"{day or _today()}.json"


def _actions_dir() -> Path:
    return _agent_dir() / "actions"


def _audit_dir() -> Path:
    return _agent_dir() / "audit"


def _base_conversation(user_id: Any, title: str = "新会话", context: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": _new_id("c"),
        "user_id": int(user_id),
        "title": title,
        "created_at": now,
        "updated_at": now,
        "context": dict(context or {}),
        "messages": [],
    }


def create_conversation(
    user_id: Any,
    *,
    title: str = "新会话",
    context: dict[str, Any] | None = None,
    max_conversations: int = AGENT_MAX_CONVERSATIONS_PER_USER,
) -> dict[str, Any]:
    with _STORE_LOCK:
        existing = _list_conversations_unlocked(user_id)
        # 超出上限时删除最旧的会话，避免单用户无限占用磁盘。
        for stale in sorted(existing, key=lambda item: item.get("updated_at", ""))[: max(0, len(existing) - max_conversations + 1)]:
            _delete_unlocked(user_id, stale["id"])
        conversation = _base_conversation(user_id, title[:60] or "新会话", context)
        write_json_atomic(_conversation_path(user_id, conversation["id"]), conversation)
        return conversation


def _list_conversations_unlocked(user_id: Any) -> list[dict[str, Any]]:
    directory = _conversations_dir(user_id)
    if not directory.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for path in directory.glob("c_*.json"):
        data = read_json(path, default=None)
        if not isinstance(data, dict) or str(data.get("user_id")) != str(user_id):
            continue
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        summaries.append(
            {
                "id": data.get("id", path.stem),
                "title": data.get("title", "新会话"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "context": data.get("context", {}),
                "message_count": len(messages),
                "last_message": str((messages[-1] or {}).get("content", ""))[:120] if messages else "",
            }
        )
    return summaries


def list_conversations(user_id: Any, *, retention_days: int = 30) -> list[dict[str, Any]]:
    cutoff = time.time() - max(1, retention_days) * 86400
    with _STORE_LOCK:
        summaries = _list_conversations_unlocked(user_id)
        kept: list[dict[str, Any]] = []
        for item in summaries:
            try:
                updated = datetime.fromisoformat(str(item.get("updated_at") or "")).timestamp()
            except (TypeError, ValueError):
                updated = 0
            if updated and updated < cutoff:
                _delete_unlocked(user_id, item["id"])
                continue
            kept.append(item)
    kept.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return kept


def load_conversation(user_id: Any, conversation_id: str) -> dict[str, Any] | None:
    path = _conversation_path(user_id, conversation_id)
    if path is None or not path.exists():
        return None
    data = read_json(path, default=None)
    if not isinstance(data, dict) or str(data.get("user_id")) != str(user_id):
        return None
    if not isinstance(data.get("messages"), list):
        data["messages"] = []
    if not isinstance(data.get("context"), dict):
        data["context"] = {}
    return data


def save_conversation(conversation: dict[str, Any], *, max_messages: int = AGENT_MAX_MESSAGES_PER_CONVERSATION) -> None:
    messages = conversation.get("messages")
    if isinstance(messages, list) and len(messages) > max_messages:
        conversation["messages"] = messages[-max_messages:]
    conversation["updated_at"] = _now_iso()
    with _STORE_LOCK:
        write_json_atomic(_conversation_path(conversation["user_id"], conversation["id"]), conversation)


def _delete_unlocked(user_id: Any, conversation_id: str) -> bool:
    path = _conversation_path(user_id, conversation_id)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def delete_conversation(user_id: Any, conversation_id: str) -> bool:
    with _STORE_LOCK:
        return _delete_unlocked(user_id, conversation_id)


def append_message(
    conversation: dict[str, Any],
    *,
    role: str,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id or _new_id("m"),
        "role": role,
        "content": str(content or ""),
        "created_at": _now_iso(),
    }
    if attachments:
        message["attachments"] = attachments
    if usage:
        message["usage"] = usage
    conversation.setdefault("messages", []).append(message)
    # 首条用户消息自动成为会话标题，方便会话列表辨识。
    if role == "user" and conversation.get("title") in {"", "新会话"}:
        conversation["title"] = str(content or "").strip()[:30] or "新会话"
    return message


# ---------------------------------------------------------------- actions


def create_action(
    *,
    actor_id: Any,
    conversation_id: str,
    tool: str,
    args: dict[str, Any],
    summary: str,
    diff: dict[str, Any] | None = None,
    risk: str = "write",
    ttl_seconds: int = AGENT_ACTION_TTL_SECONDS,
    room_id: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    action = {
        "id": _new_id("a"),
        "actor_id": int(actor_id),
        "conversation_id": conversation_id,
        "tool": tool,
        "args": dict(args or {}),
        "summary": summary,
        "diff": dict(diff or {}),
        "risk": risk,
        "created_at": _now_iso(),
        "expires_at": datetime.fromtimestamp(now + max(30, ttl_seconds)).astimezone().isoformat(timespec="seconds"),
        "status": "pending",
    }
    if room_id:
        action["room_id"] = str(room_id)
    path = _actions_dir() / f"{action['id']}.json"
    with _STORE_LOCK:
        write_json_atomic(path, action)
    return action


def load_action(action_id: str, *, expire: bool = True) -> dict[str, Any] | None:
    if not _ACTION_ID_RE.match(str(action_id or "")):
        return None
    path = _actions_dir() / f"{action_id}.json"
    if not path.exists():
        return None
    action = read_json(path, default=None)
    if not isinstance(action, dict):
        return None
    if expire and action.get("status") == "pending":
        try:
            expired = datetime.fromisoformat(str(action.get("expires_at") or "")).timestamp() <= time.time()
        except (TypeError, ValueError):
            expired = False
        if expired:
            action["status"] = "expired"
            _write_action(action)
    return action


def update_action(action: dict[str, Any], **fields: Any) -> dict[str, Any]:
    action.update(fields)
    _write_action(action)
    return action


def _write_action(action: dict[str, Any]) -> None:
    path = _actions_dir() / f"{action['id']}.json"
    with _STORE_LOCK:
        write_json_atomic(path, action)


def expire_pending_actions() -> int:
    """把已过期的待确认操作批量落为 expired，供定时任务/测试调用。"""
    directory = _actions_dir()
    if not directory.exists():
        return 0
    changed = 0
    for path in directory.glob("a_*.json"):
        action = read_json(path, default=None)
        if isinstance(action, dict) and action.get("status") == "pending":
            refreshed = load_action(path.stem)
            if isinstance(refreshed, dict) and refreshed.get("status") == "expired":
                changed += 1
    return changed


# ---------------------------------------------------------------- audit


def append_audit(event: dict[str, Any]) -> None:
    """追加一条 JSONL 审计；超过大小上限时滚动，避免无限增长。"""
    directory = _audit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "site-agent.jsonl"
    line = json.dumps({"ts": _now_iso(), **event}, ensure_ascii=False) + "\n"
    with _STORE_LOCK:
        try:
            if path.exists() and path.stat().st_size > AGENT_AUDIT_MAX_BYTES:
                _rotate_audit(directory)
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _rotate_audit(directory: Path) -> None:
    current = directory / "site-agent.jsonl"
    for index in range(AGENT_AUDIT_KEEP_FILES - 1, 0, -1):
        source = directory / (f"site-agent.{index}.jsonl" if index > 1 else "site-agent.1.jsonl")
        target = directory / f"site-agent.{index + 1}.jsonl"
        if source.exists():
            try:
                source.replace(target)
            except OSError:
                pass
    try:
        current.replace(directory / "site-agent.1.jsonl")
    except OSError:
        pass


# ---------------------------------------------------------------- usage


def _load_usage_file(path: Path) -> dict[str, Any]:
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        data = {}
    return {
        "prompt_tokens": int(data.get("prompt_tokens") or 0),
        "completion_tokens": int(data.get("completion_tokens") or 0),
        "total_tokens": int(data.get("total_tokens") or 0),
        "turns": [float(item) for item in data.get("turns", []) if isinstance(item, (int, float))],
        "events": [item for item in data.get("events", []) if isinstance(item, list) and len(item) >= 4],
    }


def record_usage(
    user_id: Any,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    counted_turn: bool = True,
) -> dict[str, Any]:
    path = _usage_path(user_id)
    with _STORE_LOCK:
        usage = _load_usage_file(path)
        now = time.time()
        usage["prompt_tokens"] += max(0, int(prompt_tokens or 0))
        usage["completion_tokens"] += max(0, int(completion_tokens or 0))
        usage["total_tokens"] += max(0, int(total_tokens or 0))
        usage["events"].append([now, int(prompt_tokens or 0), int(completion_tokens or 0), int(total_tokens or 0)])
        if counted_turn:
            usage["turns"].append(now)
        usage["turns"] = [t for t in usage["turns"] if now - t <= 86400 * 2]
        write_json_atomic(path, {"date": _today(), **usage})
    return usage


def user_today_usage(user_id: Any) -> dict[str, Any]:
    return _load_usage_file(_usage_path(user_id))


def turns_last_hour(user_id: Any) -> int:
    now = time.time()
    return len([stamp for stamp in user_today_usage(user_id)["turns"] if now - stamp <= 3600])


def site_usage_window(hours: int = 24) -> dict[str, Any]:
    """统计过去 ``hours`` 小时内全站 Agent 的 token 消耗。"""
    hours = max(1, min(int(hours or 24), 24 * 90))
    now = time.time()
    cutoff = now - hours * 3600
    directory = _agent_dir() / "usage"
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "turns": 0, "requests": 0, "users": 0}
    if not directory.exists():
        return {**totals, "window_hours": hours, "since": datetime.fromtimestamp(cutoff).astimezone().isoformat(timespec="seconds"), "until": _now_iso()}
    users_with_activity = 0
    for day_dir in directory.iterdir():
        if not day_dir.is_dir():
            continue
        user_has_activity = False
        for path in day_dir.glob("*.json"):
            for event in _load_usage_file(path)["events"]:
                stamp = float(event[0])
                if stamp < cutoff:
                    continue
                user_has_activity = True
                totals["requests"] += 1
                totals["prompt_tokens"] += int(event[1] or 0)
                totals["completion_tokens"] += int(event[2] or 0)
                totals["total_tokens"] += int(event[3] or 0)
        if user_has_activity:
            users_with_activity += 1
    # 按天文件只保留当天事件；跨天窗口自然落在多个文件里，逐条按时间过滤即可。
    totals["users"] = users_with_activity
    totals["turns"] = totals["requests"]
    return {
        **totals,
        "window_hours": hours,
        "since": datetime.fromtimestamp(cutoff).astimezone().isoformat(timespec="seconds"),
        "until": _now_iso(),
    }


# ---------------------------------------------------------------- turn locks

_TURN_LOCKS: dict[str, threading.Lock] = {}
_TURN_LOCK_GUARD = threading.Lock()


def acquire_turn_lock(user_id: Any) -> bool:
    """同一用户同一时间只允许一个进行中的回合。"""
    key = str(user_id)
    with _TURN_LOCK_GUARD:
        lock = _TURN_LOCKS.setdefault(key, threading.Lock())
    return lock.acquire(blocking=False)


def release_turn_lock(user_id: Any) -> None:
    key = str(user_id)
    with _TURN_LOCK_GUARD:
        lock = _TURN_LOCKS.get(key)
    if lock is not None and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
