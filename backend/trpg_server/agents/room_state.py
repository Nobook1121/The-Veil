from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic


STATE_FILENAME = "state.json"
MAX_EVENT_LOG = 200
MAX_SUMMARY_LENGTH = 4000


def _default_state() -> dict[str, Any]:
    return {
        "active_scene_id": None,
        "triggered_event_ids": [],
        "clues": [],
        "npc_attitudes": {},
        "items": [],
        "quests": [],
        "timeline": [],
        "rolling_summary": "",
        "event_log": [],
        "triggeredFiles": [],
        "triggerHistory": [],
        "completed": False,
        "ending_reached": False,
        "combat": None,
        "chase": None,
    }


def _normalize_state(value: Any) -> dict[str, Any]:
    state = _default_state()
    if isinstance(value, dict):
        state.update(value)
    for key in ("triggered_event_ids", "clues", "items", "quests", "timeline", "event_log", "triggeredFiles", "triggerHistory"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("npc_attitudes"), dict):
        state["npc_attitudes"] = {}
    if not isinstance(state.get("combat"), dict):
        state["combat"] = None
    if not isinstance(state.get("chase"), dict):
        state["chase"] = None
    state["rolling_summary"] = str(state.get("rolling_summary") or "")[:MAX_SUMMARY_LENGTH]
    state["event_log"] = [item for item in state["event_log"] if isinstance(item, dict)][-MAX_EVENT_LOG:]
    return state


def _state_path(room_dir: Path) -> Path:
    return Path(room_dir) / STATE_FILENAME


def load_room_state(room_dir: Path) -> dict[str, Any]:
    return _normalize_state(read_json(_state_path(room_dir), default={}))


def save_room_state(room_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_state(state)
    Path(room_dir).mkdir(parents=True, exist_ok=True)
    write_json_atomic(_state_path(room_dir), normalized)
    return normalized


def append_room_event(room_dir: Path, event: dict[str, Any]) -> dict[str, Any]:
    state = load_room_state(room_dir)
    item = {
        "id": str(event.get("id") or uuid4().hex),
        "kind": str(event.get("kind") or "event")[:80],
        "content": str(event.get("content") or "")[:500],
        "created_at": str(event.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
    }
    for key in ("scene_id", "actor", "metadata"):
        if key in event:
            item[key] = event[key]
    state["event_log"].append(item)
    state["event_log"] = state["event_log"][-MAX_EVENT_LOG:]
    save_room_state(room_dir, state)
    return item


def project_room_state(state: dict[str, Any] | None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = _normalize_state(state)
    snapshot_data = snapshot if isinstance(snapshot, dict) else {}
    scenario = snapshot_data.get("scenario") if isinstance(snapshot_data.get("scenario"), dict) else {}
    active_scene_id = normalized.get("active_scene_id") or scenario.get("active_scene_id")
    return {
        "scenario_version": snapshot_data.get("room", {}).get("scenario_version") if isinstance(snapshot_data.get("room"), dict) else None,
        "active_scene_id": active_scene_id,
        "triggered_event_ids": normalized["triggered_event_ids"][-30:],
        "clues": normalized["clues"][-30:],
        "npc_attitudes": normalized["npc_attitudes"],
        "items": normalized["items"][-30:],
        "quests": normalized["quests"][-30:],
        "timeline": normalized["timeline"][-30:],
        "rolling_summary": normalized["rolling_summary"],
        "event_log": normalized["event_log"][-20:],
        "triggeredFiles": normalized["triggeredFiles"][-100:],
        "triggerHistory": normalized["triggerHistory"][-50:],
        "chase": normalized.get("chase"),
    }
