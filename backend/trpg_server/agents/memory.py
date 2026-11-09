import time
from typing import Any
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic

MAX_MEMORY_ITEMS = 200
MAX_MEMORY_CONTENT_LENGTH = 1000


def _room_dir(context: Any):
    """兼容两种上下文：KP 工具把 room_dir 作为属性，网站 Agent 则提供同名方法。"""
    room_dir = getattr(context, "room_dir", None)
    return room_dir() if callable(room_dir) else room_dir


def _memory_file(context: Any):
    room_dir = _room_dir(context)
    if not room_dir:
        raise ValueError("room context is required for memory")
    return room_dir / "agent_memory.json"


def remember_room_fact(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    content = str(arguments.get("content") or "").strip()
    if not content:
        return {"error": "content is required"}
    item = {
        "id": uuid4().hex,
        "agent_id": getattr(context, "agent_id", "kp"),
        "kind": str(arguments.get("kind") or "fact")[:80],
        "content": content[:MAX_MEMORY_CONTENT_LENGTH],
        "importance": max(1, min(5, int(arguments.get("importance") or 1))),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = _memory_file(context)
    memory = read_json(path, default=[])
    memory.insert(0, item)
    write_json_atomic(path, memory[:MAX_MEMORY_ITEMS])
    return {"stored": True, "item": item}


def read_room_memory(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    path = _memory_file(context)
    query = str(arguments.get("query") or "").strip()
    limit = max(1, min(50, int(arguments.get("limit") or 10)))
    memory = read_json(path, default=[])
    if query:
        memory = [
            item
            for item in memory
            if query in str(item.get("content", "")) or query in str(item.get("kind", ""))
        ]
    # 合并长期记忆账本：早期对话/关键节点即使被移出滚动摘要，也能按查询召回。
    # 每条都带 source，模型可据此说明"来自哪条记录"，避免凭空补写。
    try:
        from trpg_server.agents.memory_consolidation import memory_recall

        seen = {str(item.get("content") or "").strip() for item in memory}
        for record in memory_recall(_room_dir(context), query, limit):
            content = str(record.get("text") or "").strip()
            if not content or content in seen:
                continue
            seen.add(content)
            memory.append(
                {
                    "id": record.get("id"),
                    "kind": record.get("kind") or "fact",
                    "content": content,
                    "importance": record.get("importance") or 1,
                    "created_at": record.get("created_at") or "",
                    "source": record.get("source") or {},
                }
            )
    except Exception:  # noqa: BLE001 - 记忆账本不可用时不影响原有记忆读取
        pass
    memory.sort(
        key=lambda item: (int(item.get("importance") or 1), item.get("created_at", "")),
        reverse=True,
    )
    return {"items": memory[:limit]}
