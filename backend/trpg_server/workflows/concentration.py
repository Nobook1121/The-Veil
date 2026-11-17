"""专注注册表。

专注状态原本只挂在战斗者/角色快照上；跨请求查询、跨战斗与非战斗清理都需要
一个房间级索引。注册表不复制规则，只记录“谁在专注什么、关联了哪些效果”，
实际打断与效果移除仍由规则集插件的 ``end_concentration`` 完成。

条目结构::

    {holder, spell, name, level, dc, round, effects: [{target, id}], updated_at}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.workflows.store import room_write_lock

CONCENTRATION_FILENAME = "concentration.json"
CONCENTRATION_SCHEMA = 1


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def store_path(room_dir: Any) -> Path:
    return Path(room_dir) / CONCENTRATION_FILENAME


def _empty() -> dict[str, Any]:
    return {"schema": CONCENTRATION_SCHEMA, "revision": 0, "entries": {}}


def load(room_dir: Any) -> dict[str, Any]:
    raw = read_json(store_path(room_dir), default={})
    if not isinstance(raw, Mapping):
        return _empty()
    entries = raw.get("entries") if isinstance(raw.get("entries"), Mapping) else {}
    return {
        "schema": CONCENTRATION_SCHEMA,
        "revision": _as_int(raw.get("revision"), 0),
        "entries": {
            _text(name): dict(entry)
            for name, entry in entries.items()
            if _text(name) and isinstance(entry, Mapping)
        },
    }


def save(room_dir: Any, state: Mapping[str, Any]) -> None:
    write_json_atomic(
        store_path(room_dir),
        {
            "schema": CONCENTRATION_SCHEMA,
            "revision": _as_int(state.get("revision"), 0),
            "entries": dict(state.get("entries") or {}),
        },
    )


def list_entries(room_dir: Any) -> list[dict[str, Any]]:
    state = load(room_dir)
    return [dict(entry, holder=name) for name, entry in state["entries"].items()]


def get(room_dir: Any, holder: str) -> dict[str, Any] | None:
    return load(room_dir)["entries"].get(_text(holder))


def sync(room_dir: Any, holders: Mapping[str, Mapping[str, Any] | None]) -> dict[str, Any]:
    """用持有者当前状态刷新注册表：有 ``concentration`` 则登记，否则移除。"""
    with room_write_lock(room_dir):
        state = load(room_dir)
        changed = False
        for name, holder in (holders or {}).items():
            key = _text(name)
            if not key:
                continue
            record = holder.get("concentration") if isinstance(holder, Mapping) else None
            if isinstance(record, Mapping):
                entry = {
                    "spell": _text(record.get("spell")),
                    "name": _text(record.get("name")),
                    "level": _as_int(record.get("level"), 0),
                    "dc": _as_int(record.get("dc"), 0),
                    "round": _as_int(record.get("round"), 0),
                    "effects": [
                        dict(ref)
                        for ref in record.get("effects") or []
                        if isinstance(ref, Mapping)
                    ],
                }
                if state["entries"].get(key) != entry:
                    state["entries"][key] = entry
                    changed = True
            elif key in state["entries"]:
                state["entries"].pop(key, None)
                changed = True
        if changed:
            state["revision"] = _as_int(state.get("revision"), 0) + 1
            save(room_dir, state)
    return state


def remove(room_dir: Any, holder: str, *, reason: str = "") -> dict[str, Any] | None:
    key = _text(holder)
    with room_write_lock(room_dir):
        state = load(room_dir)
        entry = state["entries"].pop(key, None)
        if entry is not None:
            entry = dict(entry)
            if reason:
                entry["ended_reason"] = reason
            state["revision"] = _as_int(state.get("revision"), 0) + 1
            save(room_dir, state)
    return entry


def holders_from_combat(combat: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(combat, Mapping):
        return {}
    return {
        str(name): combatant
        for name, combatant in (combat.get("combatants") or {}).items()
        if isinstance(combatant, Mapping)
    }


def holders_from_members(members: Iterable[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    for member in members or []:
        if not isinstance(member, Mapping):
            continue
        state = member.get("character_state")
        if not isinstance(state, Mapping):
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), Mapping) else {}
        name = _text(card.get("name")) or _text(member.get("username"))
        if name:
            rows[name] = state
    return rows


__all__ = [
    "CONCENTRATION_FILENAME",
    "get",
    "holders_from_combat",
    "holders_from_members",
    "list_entries",
    "load",
    "remove",
    "save",
    "sync",
]
