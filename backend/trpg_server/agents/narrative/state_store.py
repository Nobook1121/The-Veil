"""房间叙事状态的读写、归一化、事件应用与关键帧（设计文档 §4.3、§6.4）。

权威数据仍是房间目录下的 `state.json`：旧字段保持原样，新增 `narrative`
键保存图谱运行态。所有写入都经过 `apply_event()` 或 `save_narrative_state()`，
通过 `room_state` 的原子写落盘；旧字段只在这里做一次投影同步，避免多套写入口。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.room_state import load_room_state, save_room_state
from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

NARRATIVE_KEY = "narrative"
STORY_KEYFRAMES_FILENAME = "story_keyframes.json"
MAX_VISIT_LOG = 200
MAX_APPLIED_EVENT_IDS = 200
MAX_KEYFRAMES = 20
KEYFRAME_KEEP_FIRST = 1

_LOCATION_OF_STATE_KEYS = ("location", "cursor", "active_scene_id")
_PC_STATE_DEFAULTS = {
    "location": None,
    "position": "",
    "pose": "",
    "inventory": [],
    "conditions": {},
    "status": [],
    "known_clues": [],
}
_CLOCK_DEFAULTS = {"turn": 0, "in_game_time": "", "scene_enter_turn": 0}
_NODE_STATUSES = {"unvisited", "visited", "active", "completed"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def default_narrative_state() -> dict[str, Any]:
    """§4.3 运行态默认值；未填写的字段一律为空，不做业务推断。"""
    return {
        "mode": "graph",
        "topology": "hybrid",
        "scenario_version": "1",
        "location": None,
        "party_locations": {},
        "party_split": False,
        "visit_log": [],
        "node_status": {},
        "visited_objects": [],
        "beats_done": [],
        "trigger_fires": {},
        "revealed_clues": [],
        "pc_known_clues": {},
        "flags": {},
        "clocks": {},
        "provisional_facts": [],
        "revealed": [],
        "interaction_results": {},
        "checks": [],
        "applied_event_ids": [],
        "last_transition_turn": 0,
        "pc_state": {},
        "npc_state": {},
        "clock": dict(_CLOCK_DEFAULTS),
    }


def _normalize_pc_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for pc_id, raw in value.items():
        key = str(pc_id or "").strip()
        if not key:
            continue
        entry = dict(_PC_STATE_DEFAULTS)
        if isinstance(raw, Mapping):
            entry.update(raw)
        entry["inventory"] = _str_list(entry.get("inventory"))
        entry["status"] = _str_list(entry.get("status"))
        entry["known_clues"] = _str_list(entry.get("known_clues"))
        if not isinstance(entry.get("conditions"), Mapping):
            entry["conditions"] = {}
        result[key] = entry
    return result


def _normalize_npc_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for npc_id, raw in value.items():
        key = str(npc_id or "").strip()
        if not key:
            continue
        entry = raw if isinstance(raw, Mapping) else {"attitude": raw}
        normalized = dict(entry)
        if "attitude" in normalized:
            normalized["attitude"] = _as_int(normalized.get("attitude"), 0)
        if "visible" in normalized:
            normalized["visible"] = _as_bool(normalized.get("visible"), True)
        normalized["location"] = str(normalized.get("location") or "").strip() or None
        result[key] = normalized
    return result


def _normalize_clocks(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for clock_id, raw in value.items():
        key = str(clock_id or "").strip()
        if not key:
            continue
        if isinstance(raw, Mapping):
            entry = dict(raw)
            entry["value"] = _as_int(entry.get("value", entry.get("current")), 0)
            if "threshold" in entry:
                entry["threshold"] = _as_int(entry.get("threshold"), 0)
        else:
            entry = {"value": _as_int(raw, 0)}
        result[key] = entry
    return result


def normalize_narrative_state(value: Any, *, fallback_location: str | None = None) -> dict[str, Any]:
    """把任意历史形状归一为当前 `narrative` 结构（只做字段归一化，不改剧情数据）。"""
    state = default_narrative_state()
    if isinstance(value, Mapping):
        state.update({key: item for key, item in value.items() if key not in state})
        for key in state:
            if key in value:
                state[key] = value[key]

    location = None
    for key in _LOCATION_OF_STATE_KEYS:
        candidate = state.get(key)
        if isinstance(candidate, str) and candidate.strip():
            location = candidate.strip()
            break
    location = location or (str(fallback_location).strip() if fallback_location else None)
    state["location"] = location

    for key in (
        "visit_log", "node_status", "visited_objects", "beats_done", "trigger_fires",
        "revealed_clues", "pc_known_clues", "flags", "clocks", "provisional_facts",
        "revealed", "interaction_results", "checks", "applied_event_ids",
    ):
        if not isinstance(state.get(key), (list, dict)):
            state[key] = default_narrative_state()[key]

    state["visit_log"] = _str_list(state.get("visit_log"))
    if not state["visit_log"] and isinstance(value, Mapping):
        state["visit_log"] = _str_list(value.get("path"))
    state["visit_log"] = state["visit_log"][-MAX_VISIT_LOG:]

    node_status: dict[str, str] = {}
    raw_status = state.get("node_status")
    if isinstance(raw_status, Mapping):
        for node_id, status in raw_status.items():
            key = str(node_id or "").strip()
            status_text = str(status or "").strip().lower()
            if key and status_text in _NODE_STATUSES:
                node_status[key] = status_text
    if location:
        for node_id, status in list(node_status.items()):
            if status == "active" and node_id != location:
                node_status[node_id] = "visited"
        node_status.setdefault(location, "active")
    state["node_status"] = node_status

    state["visited_objects"] = _str_list(state.get("visited_objects"))
    state["beats_done"] = _str_list(state.get("beats_done"))
    state["revealed_clues"] = _str_list(state.get("revealed_clues"))
    state["provisional_facts"] = _str_list(state.get("provisional_facts"))
    state["applied_event_ids"] = _str_list(state.get("applied_event_ids"))[-MAX_APPLIED_EVENT_IDS:]

    state["flags"] = {
        str(key): value for key, value in (state.get("flags") or {}).items() if str(key).strip()
    }
    pc_clues = state.get("pc_known_clues")
    state["pc_known_clues"] = (
        {str(key): _str_list(values) for key, values in pc_clues.items()}
        if isinstance(pc_clues, Mapping)
        else {}
    )
    state["trigger_fires"] = (
        {str(key): dict(value) if isinstance(value, Mapping) else {"count": _as_int(value, 0)}
         for key, value in (state.get("trigger_fires") or {}).items()}
    )
    state["interaction_results"] = (
        {str(key): str(result) for key, result in (state.get("interaction_results") or {}).items()}
    )
    state["revealed"] = _dict_list(state.get("revealed"))
    state["checks"] = _dict_list(state.get("checks"))[-50:]
    state["clocks"] = _normalize_clocks(state.get("clocks"))
    state["pc_state"] = _normalize_pc_state(state.get("pc_state"))
    state["npc_state"] = _normalize_npc_state(state.get("npc_state"))

    clock = dict(_CLOCK_DEFAULTS)
    if isinstance(state.get("clock"), Mapping):
        clock.update(state["clock"])
    clock["turn"] = _as_int(clock.get("turn"), 0)
    clock["scene_enter_turn"] = _as_int(clock.get("scene_enter_turn"), 0)
    state["clock"] = clock

    party_locations = state.get("party_locations")
    state["party_locations"] = (
        {str(key): str(value) for key, value in party_locations.items() if str(value or "").strip()}
        if isinstance(party_locations, Mapping)
        else {}
    )
    state["party_split"] = _as_bool(state.get("party_split"), False)
    state["last_transition_turn"] = _as_int(state.get("last_transition_turn"), 0)
    return state


def narrative_from_room_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """从完整 `state.json` 取 `narrative`；旧房间回退到旧字段并做单向合并（§12.1）。"""
    data = state if isinstance(state, Mapping) else {}
    raw = data.get(NARRATIVE_KEY)
    fallback = data.get("active_scene_id") or data.get("cursor")
    narrative = normalize_narrative_state(raw, fallback_location=str(fallback or "") or None)
    scenario_version = data.get("scenario_version")
    if isinstance(scenario_version, str) and scenario_version.strip():
        narrative["scenario_version"] = scenario_version.strip()

    # 旧房间没有 `narrative.path`：用旧字段补历史路径，保证位置历史不丢。
    if not narrative.get("visit_log"):
        legacy_path = _str_list(data.get("path"))
        if legacy_path:
            narrative["visit_log"] = legacy_path[-MAX_VISIT_LOG:]

    # 旧字段可能由尚未迁移的旧代码写入：读时并集合并，写时再投影回去。
    legacy_clues = _str_list(data.get("clues"))
    if legacy_clues:
        merged = _str_list(narrative.get("revealed_clues"))
        narrative["revealed_clues"] = list(dict.fromkeys(merged + legacy_clues))
    legacy_items = _str_list(data.get("items"))
    if legacy_items:
        pc_state = narrative.get("pc_state") or {}
        party = dict(pc_state.get("party") or _PC_STATE_DEFAULTS)
        party["inventory"] = list(dict.fromkeys(_str_list(party.get("inventory")) + legacy_items))
        pc_state["party"] = party
        narrative["pc_state"] = pc_state
    legacy_attitudes = data.get("npc_attitudes")
    if isinstance(legacy_attitudes, Mapping):
        npc_state = narrative.get("npc_state") or {}
        for npc_id, attitude in legacy_attitudes.items():
            key = str(npc_id or "").strip()
            if not key:
                continue
            entry = dict(npc_state.get(key) or {})
            entry.setdefault("attitude", _as_int(attitude, 0))
            npc_state[key] = entry
        narrative["npc_state"] = npc_state
    return narrative


def load_narrative_state(room_dir: Path | str) -> dict[str, Any]:
    return narrative_from_room_state(load_room_state(Path(room_dir)))


def save_narrative_state(room_dir: Path | str, narrative: Mapping[str, Any]) -> dict[str, Any]:
    """唯一写入口：写 `narrative` 并把旧字段投影同步，避免两套状态不一致。"""
    directory = Path(room_dir)
    normalized = normalize_narrative_state(narrative)
    state = load_room_state(directory)
    state[NARRATIVE_KEY] = normalized

    location = normalized.get("location")
    if location:
        state["active_scene_id"] = location
        state["cursor"] = location
    state["path"] = list(normalized.get("visit_log") or [])
    if normalized.get("revealed_clues"):
        state["clues"] = list(normalized["revealed_clues"])

    attitudes = state.get("npc_attitudes")
    attitudes = dict(attitudes) if isinstance(attitudes, Mapping) else {}
    for npc_id, entry in (normalized.get("npc_state") or {}).items():
        if isinstance(entry, Mapping) and "attitude" in entry:
            attitudes[npc_id] = entry["attitude"]
    state["npc_attitudes"] = attitudes

    inventory: list[str] = []
    for entry in (normalized.get("pc_state") or {}).values():
        if isinstance(entry, Mapping):
            inventory.extend(_str_list(entry.get("inventory")))
    if inventory:
        state["items"] = list(dict.fromkeys(inventory))

    saved = save_room_state(directory, state)
    logger.debug(
        "narrative.state.saved room=%s location=%s turn=%s",
        directory.name, location, (normalized.get("clock") or {}).get("turn"),
    )
    return saved


def _apply_move(narrative: dict[str, Any], event: Mapping[str, Any], turn: int) -> None:
    target = str(event.get("to") or event.get("location_id") or "").strip()
    if not target:
        return
    source = str(event.get("from") or narrative.get("location") or "").strip()
    narrative["location"] = target
    if source != target:
        visit_log = _str_list(narrative.get("visit_log"))
        visit_log.append(target)
        narrative["visit_log"] = visit_log[-MAX_VISIT_LOG:]
    status = dict(narrative.get("node_status") or {})
    for node_id, value in list(status.items()):
        if value == "active" and node_id != target:
            status[node_id] = "visited"
    status[target] = "active"
    narrative["node_status"] = status
    narrative["last_transition_turn"] = turn
    clock = dict(narrative.get("clock") or {})
    clock["scene_enter_turn"] = turn
    narrative["clock"] = clock

    actor = str(event.get("actor") or "").strip()
    if actor:
        party = dict(narrative.get("party_locations") or {})
        party[actor] = target
        narrative["party_locations"] = party
        narrative["party_split"] = len(set(party.values())) > 1


def _apply_clue(narrative: dict[str, Any], event: Mapping[str, Any]) -> None:
    clue_ids = _str_list(event.get("clue_ids"))
    single = str(event.get("clue_id") or "").strip()
    if single and single not in clue_ids:
        clue_ids.append(single)
    if not clue_ids:
        return
    revealed = _str_list(narrative.get("revealed_clues"))
    for clue_id in clue_ids:
        if clue_id not in revealed:
            revealed.append(clue_id)
    narrative["revealed_clues"] = revealed

    pc = str(event.get("pc") or "").strip()
    if pc:
        known = narrative.get("pc_known_clues") or {}
        clues = _str_list(known.get(pc))
        for clue_id in clue_ids:
            if clue_id not in clues:
                clues.append(clue_id)
        known[pc] = clues
        narrative["pc_known_clues"] = known


def _apply_clock(narrative: dict[str, Any], event: Mapping[str, Any]) -> None:
    clock_id = str(event.get("clock_id") or "").strip()
    if not clock_id:
        return
    clocks = dict(narrative.get("clocks") or {})
    entry = dict(clocks.get(clock_id) or {})
    value = _as_int(entry.get("value", 0), 0)
    if "value" in event:
        value = _as_int(event.get("value"), value)
    else:
        value += _as_int(event.get("delta"), 0)
    entry["value"] = max(0, value)
    if "threshold" in event:
        entry["threshold"] = _as_int(event.get("threshold"), 0)
    if "on_reach" in event:
        entry["on_reach"] = str(event.get("on_reach") or "")
    if "visibility" in event:
        entry["visibility"] = str(event.get("visibility") or "public")
    clocks[clock_id] = entry
    narrative["clocks"] = clocks


def _apply_item(narrative: dict[str, Any], event: Mapping[str, Any]) -> None:
    item_id = str(event.get("item_id") or "").strip()
    if not item_id:
        return
    pc = str(event.get("pc") or "").strip()
    pc_state = narrative.get("pc_state") or {}
    key = pc or "party"
    entry = dict(pc_state.get(key) or _PC_STATE_DEFAULTS)
    inventory = _str_list(entry.get("inventory"))
    action = str(event.get("action") or "add").strip().lower()
    if action == "remove":
        inventory = [value for value in inventory if value != item_id]
    elif item_id not in inventory:
        inventory.append(item_id)
    entry["inventory"] = inventory
    pc_state[key] = entry
    narrative["pc_state"] = pc_state


def _apply_attitude(narrative: dict[str, Any], event: Mapping[str, Any]) -> None:
    npc_id = str(event.get("npc_id") or "").strip()
    if not npc_id:
        return
    npc_state = narrative.get("npc_state") or {}
    entry = dict(npc_state.get(npc_id) or {})
    if "value" in event:
        value = _as_int(event.get("value"), 0)
    else:
        value = _as_int(entry.get("attitude"), 0) + _as_int(event.get("delta"), 0)
    entry["attitude"] = value
    if "location" in event:
        entry["location"] = str(event.get("location") or "").strip() or None
    if "visible" in event:
        entry["visible"] = _as_bool(event.get("visible"), True)
    if "known_clues" in event:
        entry["known_clues"] = list(dict.fromkeys(
            _str_list(entry.get("known_clues")) + _str_list(event.get("known_clues"))
        ))
    npc_state[npc_id] = entry
    narrative["npc_state"] = npc_state


def _apply_trigger_fire(narrative: dict[str, Any], event: Mapping[str, Any], turn: int) -> None:
    trigger_id = str(event.get("trigger_id") or "").strip()
    if not trigger_id:
        return
    fires = dict(narrative.get("trigger_fires") or {})
    entry = dict(fires.get(trigger_id) or {})
    entry["count"] = _as_int(entry.get("count"), 0) + 1
    entry["last_turn"] = turn
    check_id = str(event.get("check_id") or "").strip()
    if check_id:
        entry["check_id"] = check_id
    pc = str(event.get("pc") or "").strip()
    if pc:
        pcs = _str_list(entry.get("pcs"))
        if pc not in pcs:
            pcs.append(pc)
        entry["pcs"] = pcs[-50:]
    fires[trigger_id] = entry
    narrative["trigger_fires"] = fires


def apply_to_narrative(narrative: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    """纯函数版本：不读写磁盘，供重放与测试使用。"""
    state = normalize_narrative_state(narrative)
    event_id = str(event.get("id") or "").strip()
    applied = _str_list(state.get("applied_event_ids"))
    if event_id and event_id in applied:
        return state
    if event_id:
        applied.append(event_id)
        state["applied_event_ids"] = applied[-MAX_APPLIED_EVENT_IDS:]

    turn = _as_int(event.get("turn"), 0)
    clock = dict(state.get("clock") or {})
    if turn > _as_int(clock.get("turn"), 0):
        clock["turn"] = turn
        state["clock"] = clock

    kind = str(event.get("kind") or "").strip().lower()
    if kind == "move":
        _apply_move(state, event, turn)
    elif kind == "clue":
        _apply_clue(state, event)
    elif kind == "clock":
        _apply_clock(state, event)
    elif kind == "flag":
        name = str(event.get("name") or "").strip()
        if name:
            flags = dict(state.get("flags") or {})
            flags[name] = event.get("value", True)
            state["flags"] = flags
    elif kind == "item":
        _apply_item(state, event)
    elif kind == "attitude":
        _apply_attitude(state, event)
    elif kind == "visited_object":
        object_id = str(event.get("object_id") or "").strip()
        if object_id:
            visited = _str_list(state.get("visited_objects"))
            if object_id not in visited:
                visited.append(object_id)
            state["visited_objects"] = visited
    elif kind == "beat":
        beat_id = str(event.get("beat_id") or "").strip()
        if beat_id:
            beats = _str_list(state.get("beats_done"))
            if beat_id not in beats:
                beats.append(beat_id)
            state["beats_done"] = beats
    elif kind == "node_status":
        node_id = str(event.get("node_id") or "").strip()
        status = str(event.get("status") or "").strip().lower()
        if node_id and status in _NODE_STATUSES:
            statuses = dict(state.get("node_status") or {})
            statuses[node_id] = status
            state["node_status"] = statuses
    elif kind == "interaction":
        interaction_id = str(event.get("interaction_id") or "").strip()
        result = str(event.get("result") or "").strip().lower()
        if interaction_id and result:
            results = dict(state.get("interaction_results") or {})
            results[interaction_id] = result
            state["interaction_results"] = results
        check = event.get("check")
        if isinstance(check, Mapping):
            checks = _dict_list(state.get("checks"))
            checks.append({**dict(check), "turn": turn, "interaction_id": interaction_id})
            state["checks"] = checks[-50:]
    elif kind == "check":
        check = event.get("check")
        if isinstance(check, Mapping):
            checks = _dict_list(state.get("checks"))
            checks.append(dict(check))
            state["checks"] = checks[-50:]
    elif kind == "trigger_fire":
        _apply_trigger_fire(state, event, turn)
    elif kind == "reveal":
        entry = {
            "id": str(event.get("object_id") or event.get("node_id") or event.get("edge_id") or "").strip(),
            "kind": str(event.get("object_kind") or "node").strip().lower(),
            "reason": str(event.get("reason") or "").strip(),
            "turn": turn,
        }
        if entry["id"]:
            revealed = _dict_list(state.get("revealed"))
            revealed.append(entry)
            state["revealed"] = revealed
    return state


def apply_event(room_dir: Path | str, event: Mapping[str, Any]) -> dict[str, Any]:
    """确定性状态事件唯一入口；事件 id 重复时幂等返回。"""
    narrative = load_narrative_state(room_dir)
    updated = apply_to_narrative(narrative, event)
    # 归一化会重排部分字段；只有内容变化时才落盘，避免无意义的原子写。
    if updated != narrative:
        save_narrative_state(room_dir, updated)
    return updated


def _keyframes_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / STORY_KEYFRAMES_FILENAME


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def state_checksum(payload: Mapping[str, Any]) -> str:
    return "sha1:" + hashlib.sha1(_canonical(payload).encode("utf-8")).hexdigest()


def load_keyframes(room_dir: Path | str) -> list[dict[str, Any]]:
    payload = read_json(_keyframes_path(room_dir), default={})
    items = payload.get("keyframes") if isinstance(payload, Mapping) else None
    return _dict_list(items)


def write_keyframe(
    room_dir: Path | str,
    narrative: Mapping[str, Any] | None = None,
    *,
    turn: int | None = None,
    reason: str = "manual",
    ledger_seq: int = 0,
) -> dict[str, Any]:
    """写关键帧；保留第一个与最近 `MAX_KEYFRAMES - 1` 个（§6.4.2）。"""
    directory = Path(room_dir)
    current = narrative if isinstance(narrative, Mapping) else load_narrative_state(directory)
    normalized = normalize_narrative_state(current)
    if turn is None:
        turn = _as_int((normalized.get("clock") or {}).get("turn"), 0)
    state_payload = {
        NARRATIVE_KEY: normalized,
        "active_scene_id": normalized.get("location"),
        "cursor": normalized.get("location"),
    }
    keyframes = load_keyframes(directory)
    next_number = 1 + max(
        (_as_int(str(item.get("keyframe_id") or "").replace("kf_", ""), 0) for item in keyframes),
        default=0,
    )
    keyframe = {
        "keyframe_id": f"kf_{next_number:04d}",
        "turn": int(turn),
        "location_id": normalized.get("location"),
        "reason": str(reason or "manual"),
        "state": state_payload,
        "ledger_seq": _as_int(ledger_seq, 0),
        "created_at": _now_text(),
    }
    keyframe["checksum"] = state_checksum(keyframe["state"])
    if len(keyframes) >= MAX_KEYFRAMES:
        # 先腾出一格再追加，确保首帧 + 最近 N 帧后总数不超过 MAX_KEYFRAMES。
        keyframes = [keyframes[0], *keyframes[-(MAX_KEYFRAMES - 2):]]
    keyframes.append(keyframe)
    write_json_atomic(_keyframes_path(directory), {"keyframes": keyframes})
    logger.info(
        "narrative.keyframe.write room=%s turn=%s keyframe=%s reason=%s",
        directory.name, turn, keyframe["keyframe_id"], keyframe["reason"],
    )
    return keyframe


def latest_keyframe(room_dir: Path | str) -> dict[str, Any] | None:
    keyframes = load_keyframes(room_dir)
    return keyframes[-1] if keyframes else None


def replay_narrative(room_dir: Path | str, *, events: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """关键帧 + 账本重放（§6.4.3）；校验失败时仍返回可用状态并记日志。"""
    directory = Path(room_dir)
    keyframe = latest_keyframe(directory)
    if keyframe and isinstance(keyframe.get("state"), Mapping):
        payload = keyframe["state"]
        checksum = str(keyframe.get("checksum") or "")
        if checksum and checksum != state_checksum(payload):
            logger.warning("narrative.keyframe.checksum_mismatch room=%s keyframe=%s", directory.name, keyframe.get("keyframe_id"))
        narrative = normalize_narrative_state(payload.get(NARRATIVE_KEY))
        after_seq = _as_int(keyframe.get("ledger_seq"), 0)
    else:
        narrative = default_narrative_state()
        after_seq = 0

    if events is None:
        from trpg_server.agents.narrative.ledger import read_ledger_events

        events = read_ledger_events(directory, after_seq=after_seq)
    for event in events:
        narrative = apply_to_narrative(narrative, event)
    return narrative


def rebuild_narrative_state(room_dir: Path | str) -> dict[str, Any]:
    """从关键帧 + 账本重建并写回 `state.json`（控制台救场用）。"""
    narrative = replay_narrative(room_dir)
    saved = save_narrative_state(room_dir, narrative)
    return saved.get(NARRATIVE_KEY) or narrative


def _now_text() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "NARRATIVE_KEY",
    "STORY_KEYFRAMES_FILENAME",
    "apply_event",
    "apply_to_narrative",
    "default_narrative_state",
    "latest_keyframe",
    "load_keyframes",
    "load_narrative_state",
    "narrative_from_room_state",
    "normalize_narrative_state",
    "rebuild_narrative_state",
    "replay_narrative",
    "save_narrative_state",
    "state_checksum",
    "write_keyframe",
]
