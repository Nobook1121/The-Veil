"""房间级工作流存储：JSON 落盘、房间写锁与角色锁。

锁分两层：
- 房间写锁复用 ``room_clock.room_lock``：与地图、战斗、时钟共用同一把
  房间级锁（进程内重入 + 跨进程文件锁），避免工作流与战斗/地图互相覆盖；
- 持久化的锁记录带 holder 与 ``expires_at``，让跨请求的语义可观察、可超时，
  服务重启后残留锁也会按 TTL 自动失效。

角色锁用于阻止同一角色并发发起两个行动；房间写锁只包住一次短写入，
不会把整个待决流程（例如等待玩家反应）锁死。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.workflows.model import (
    MAX_EVENTS,
    MAX_FLOWS,
    SCHEMA_VERSION,
    TERMINAL_STATUSES,
    normalize_flow,
    now,
    stamp,
)

WORKFLOWS_FILENAME = "workflows.json"


@contextmanager
def room_write_lock(room_dir: Any) -> Iterator[None]:
    """统一的房间级写锁：工作流、地图、战斗与时钟共用。"""
    from trpg_server.room_clock import room_lock

    with room_lock(room_dir):
        yield


def store_path(room_dir: Any) -> Path:
    return Path(room_dir) / WORKFLOWS_FILENAME


def _default_store() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "flows": {},
        "locks": {"room": None, "characters": {}},
        "policy": {},
    }


def load(room_dir: Any) -> dict[str, Any]:
    raw = read_json(store_path(room_dir), default={})
    store = _default_store()
    if not isinstance(raw, Mapping):
        return store
    flows_raw = raw.get("flows") if isinstance(raw.get("flows"), Mapping) else {}
    flows: dict[str, Any] = {}
    for flow_id, item in flows_raw.items():
        flow = normalize_flow(item)
        if flow is not None:
            flows[str(flow_id)] = flow
    store["flows"] = _trim_flows(flows)
    locks = raw.get("locks") if isinstance(raw.get("locks"), Mapping) else {}
    characters = locks.get("characters") if isinstance(locks.get("characters"), Mapping) else {}
    store["locks"] = {
        "room": dict(locks.get("room")) if isinstance(locks.get("room"), Mapping) else None,
        "characters": {
            str(name): dict(record)
            for name, record in characters.items()
            if isinstance(record, Mapping)
        },
    }
    policy = raw.get("policy") if isinstance(raw.get("policy"), Mapping) else {}
    store["policy"] = {
        str(step): dict(rule)
        for step, rule in policy.items()
        if isinstance(rule, Mapping)
    }
    return store


def save(room_dir: Any, store: Mapping[str, Any]) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "flows": _trim_flows(
            {
                str(flow_id): flow
                for flow_id, flow in (store.get("flows") or {}).items()
                if isinstance(flow, Mapping)
            }
        ),
        "locks": store.get("locks") or {"room": None, "characters": {}},
        "policy": store.get("policy") or {},
    }
    write_json_atomic(store_path(room_dir), payload)


def _trim_flows(flows: Mapping[str, Any]) -> dict[str, Any]:
    """只保留最近的流程；未结束的流程优先保留。"""
    items = list(flows.items())
    items.sort(key=lambda item: float((item[1] or {}).get("created_epoch") or 0), reverse=True)
    kept: dict[str, Any] = {}
    pending: list[tuple[str, Any]] = []
    finished: list[tuple[str, Any]] = []
    for flow_id, flow in items:
        status = str((flow or {}).get("status") or "")
        (pending if status not in TERMINAL_STATUSES else finished).append((flow_id, flow))
    for flow_id, flow in [*pending, *finished]:
        if len(kept) >= MAX_FLOWS:
            break
        events = flow.get("events") if isinstance(flow.get("events"), list) else []
        if len(events) > MAX_EVENTS:
            flow = {**flow, "events": events[-MAX_EVENTS:]}
        kept[flow_id] = flow
    return kept


# ---------------------------------------------------------------------------
# 角色锁
# ---------------------------------------------------------------------------
def _lock_expired(record: Mapping[str, Any], moment: float) -> bool:
    try:
        expires_at = float(record.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return expires_at <= moment


def acquire_character_lock(
    store: dict[str, Any],
    character: str,
    *,
    flow_id: str,
    ttl: int,
    owner: str = "",
) -> tuple[bool, dict[str, Any] | None]:
    """尝试占用角色锁；返回 ``(ok, 现有持有者)``。"""
    name = str(character or "").strip()
    if not name:
        return True, None
    characters = store["locks"].setdefault("characters", {})
    moment = now()
    existing = characters.get(name)
    if isinstance(existing, Mapping):
        if not _lock_expired(existing, moment) and str(existing.get("flow_id") or "") != str(flow_id):
            return False, dict(existing)
        characters.pop(name, None)
    characters[name] = {
        "flow_id": str(flow_id),
        "owner": str(owner or ""),
        "acquired_at": stamp(moment),
        "expires_at": moment + max(1, int(ttl or 1)),
    }
    return True, None


def renew_character_lock(
    store: dict[str, Any],
    character: str,
    *,
    flow_id: str,
    ttl: int,
) -> bool:
    """延长当前流程持有的角色锁；锁已丢失或被他人持有时返回 ``False``。"""
    name = str(character or "").strip()
    if not name:
        return True
    characters = store["locks"].setdefault("characters", {})
    record = characters.get(name)
    if not isinstance(record, dict):
        return False
    if str(record.get("flow_id") or "") != str(flow_id):
        return False
    moment = now()
    record["expires_at"] = moment + max(1, int(ttl or 1))
    record["renewed_at"] = stamp(moment)
    return True


def release_character_lock(store: dict[str, Any], character: str, *, flow_id: str = "") -> None:
    name = str(character or "").strip()
    if not name:
        return
    characters = store["locks"].setdefault("characters", {})
    record = characters.get(name)
    if not isinstance(record, Mapping):
        return
    if flow_id and str(record.get("flow_id") or "") != str(flow_id):
        return
    characters.pop(name, None)


def prune_locks(store: dict[str, Any], moment: float | None = None) -> list[str]:
    """清理过期角色锁；返回被清理的角色名。"""
    moment = now() if moment is None else moment
    characters = store["locks"].setdefault("characters", {})
    released = [
        name for name, record in list(characters.items())
        if not isinstance(record, Mapping) or _lock_expired(record, moment)
    ]
    for name in released:
        characters.pop(name, None)
    return released


__all__ = [
    "WORKFLOWS_FILENAME",
    "acquire_character_lock",
    "load",
    "prune_locks",
    "release_character_lock",
    "renew_character_lock",
    "room_write_lock",
    "save",
    "store_path",
]
