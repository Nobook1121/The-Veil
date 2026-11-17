"""场景级持续区域。

持续区域不是棋子上的临时效果，而是挂在场景上的实体：创建一次、多回合存在，
每个目标每回合最多触发一次。区域本身只描述几何、持续时间、豁免与伤害事实；
真正的判定与伤害仍由规则集插件执行。

存储放在房间目录的 ``scene_areas.json``，按 ``scene_id`` 分组并整体带 revision。
地图插件存在时调用方可以传入当前场景 id；没有地图时使用 ``default``。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.workflows.store import room_write_lock

AREAS_FILENAME = "scene_areas.json"
AREAS_SCHEMA = 1
SHAPES = {"sphere", "cube", "cylinder", "cone", "line", "square", "circle"}
TRIGGERS = {"enter", "start_turn", "end_turn", "enter_or_end_turn", "enter_or_start_turn"}


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def store_path(room_dir: Any) -> Path:
    return Path(room_dir) / AREAS_FILENAME


def _empty() -> dict[str, Any]:
    return {"schema": AREAS_SCHEMA, "revision": 0, "areas": []}


def load(room_dir: Any) -> dict[str, Any]:
    raw = read_json(store_path(room_dir), default={})
    if not isinstance(raw, Mapping):
        return _empty()
    areas = [
        dict(item)
        for item in (raw.get("areas") if isinstance(raw.get("areas"), list) else [])
        if isinstance(item, Mapping) and _text(item.get("id"))
    ]
    return {
        "schema": AREAS_SCHEMA,
        "revision": _as_int(raw.get("revision"), 0),
        "areas": areas,
    }


def save(room_dir: Any, state: Mapping[str, Any]) -> None:
    write_json_atomic(
        store_path(room_dir),
        {
            "schema": AREAS_SCHEMA,
            "revision": _as_int(state.get("revision"), 0),
            "areas": list(state.get("areas") or []),
        },
    )


def scene_id_of(room_dir: Any) -> str:
    root = Path(room_dir) / "plugins" / "veil-map" / "scenes"
    pointer = read_json(root / "active.json", default={})
    if isinstance(pointer, Mapping):
        scene = _text(pointer.get("scene_id"))
        if scene:
            return scene
    return "default"


def list_areas(room_dir: Any, *, scene_id: str = "", active_only: bool = True) -> list[dict[str, Any]]:
    state = load(room_dir)
    wanted = _text(scene_id)
    rows = [
        dict(area)
        for area in state["areas"]
        if not wanted or _text(area.get("scene_id")) == wanted
    ]
    if active_only:
        rows = [area for area in rows if not area.get("ended_at")]
    return rows


def create_area(room_dir: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """创建区域实体；几何与持续时间只做基本归一化，不做规则解释。"""
    shape = _text(spec.get("shape") or "sphere").lower()
    if shape not in SHAPES:
        return {"error": f"不支持的区域形状“{shape}”", "code": "area.bad_shape"}
    size = max(0, _as_int(spec.get("size_ft"), 0))
    rounds = max(0, _as_int(spec.get("rounds"), 0))
    duration = spec.get("duration") if isinstance(spec.get("duration"), Mapping) else {}
    if rounds <= 0:
        rounds = _as_int(duration.get("rounds"), 0)
    origin = spec.get("origin") if isinstance(spec.get("origin"), Mapping) else None
    area = {
        "id": f"area-{uuid4().hex[:12]}",
        "scene_id": _text(spec.get("scene_id")) or scene_id_of(room_dir),
        "name": _text(spec.get("name") or spec.get("spell") or "持续区域"),
        "source": _text(spec.get("source")),
        "spell": _text(spec.get("spell")),
        "owner": _text(spec.get("owner")),
        "shape": shape,
        "size_ft": size,
        "width_ft": _as_int(spec.get("width_ft"), 0),
        "origin": dict(origin) if origin else None,
        "trigger": _text(spec.get("trigger") or "enter_or_end_turn"),
        "save": dict(spec.get("save")) if isinstance(spec.get("save"), Mapping) else None,
        "damage": [dict(row) for row in (spec.get("damage") or []) if isinstance(row, Mapping)],
        "effects": dict(spec.get("effects")) if isinstance(spec.get("effects"), Mapping) else {},
        "duration": {
            "unit": _text(duration.get("unit") or "round"),
            "rounds": rounds,
            "remaining": rounds if rounds else None,
        },
        "concentration": bool(spec.get("concentration")),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_round": _as_int(spec.get("created_round"), 0),
        "triggered": {},
    }
    with room_write_lock(room_dir):
        state = load(room_dir)
        state["areas"].append(area)
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"area": area, "revision": state["revision"]}


def remove_area(room_dir: Any, area_id: str, *, reason: str = "") -> dict[str, Any]:
    area_id = _text(area_id)
    with room_write_lock(room_dir):
        state = load(room_dir)
        kept = [area for area in state["areas"] if _text(area.get("id")) != area_id]
        removed = [area for area in state["areas"] if _text(area.get("id")) == area_id]
        if not removed:
            return {"error": "区域不存在", "code": "area.not_found"}
        if reason:
            for area in removed:
                area["ended_reason"] = reason
        state["areas"] = kept
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"removed": removed[0], "revision": state["revision"]}


def tick_areas(room_dir: Any, *, rounds: int = 1) -> dict[str, Any]:
    """推进持续区域；剩余回合归零时移除并标记结束原因。"""
    step = max(1, int(rounds or 1))
    expired: list[dict[str, Any]] = []
    with room_write_lock(room_dir):
        state = load(room_dir)
        kept: list[dict[str, Any]] = []
        for area in state["areas"]:
            duration = area.get("duration") if isinstance(area.get("duration"), Mapping) else {}
            remaining = duration.get("remaining")
            if remaining is None:
                kept.append(area)
                continue
            remaining = max(0, _as_int(remaining, 0) - step)
            duration = dict(duration)
            duration["remaining"] = remaining
            area["duration"] = duration
            if remaining <= 0:
                area["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                area["ended_reason"] = "duration"
                expired.append(dict(area))
            else:
                kept.append(area)
        state["areas"] = kept
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"expired": expired, "revision": state["revision"]}


def area_spec_from_cast(
    result: Mapping[str, Any],
    *,
    caster: str = "",
    dc: int = 0,
    scene_id: str = "",
) -> dict[str, Any] | None:
    """从 ``spells.cast`` 结果提取持续区域描述；瞬时法术返回 ``None``。"""
    descriptor = result.get("descriptor") if isinstance(result.get("descriptor"), Mapping) else {}
    area = descriptor.get("area") if isinstance(descriptor.get("area"), Mapping) else {}
    duration = descriptor.get("duration") if isinstance(descriptor.get("duration"), Mapping) else {}
    upcast = result.get("upcast") if isinstance(result.get("upcast"), Mapping) else {}
    damage = [dict(row) for row in upcast.get("damage") or [] if isinstance(row, Mapping)]
    unit = _text(duration.get("unit")).lower()
    if not area or unit in {"", "instant"} or not damage:
        return None
    value = max(1, _as_int(duration.get("value"), 1))
    per_unit = {"round": 1, "minute": 10, "hour": 600, "day": 14400}.get(unit, 10)
    resolution = descriptor.get("resolution") if isinstance(descriptor.get("resolution"), Mapping) else {}
    save = None
    if _text(resolution.get("kind")).lower() == "save":
        save = {
            "ability": _text(resolution.get("ability")) or "dex",
            "dc": max(1, int(dc or 10)),
            "half_on_success": bool(resolution.get("half_on_success")),
        }
    spell_info = result.get("spell") if isinstance(result.get("spell"), Mapping) else {}
    concentration_info = (
        result.get("concentration") if isinstance(result.get("concentration"), Mapping) else {}
    )
    spell_name = _text(spell_info.get("name"))
    return {
        "scene_id": _text(scene_id),
        "name": spell_name or "持续区域",
        "spell": _text(spell_info.get("id")),
        "source": f"{caster}:{spell_name}" if caster else spell_name,
        "owner": _text(caster),
        "shape": _text(area.get("shape")) or "sphere",
        "size_ft": area.get("size_ft") or area.get("radius_ft") or 0,
        "width_ft": area.get("width_ft") or area.get("length_ft") or 0,
        "trigger": "enter_or_start_turn",
        "save": save,
        "damage": damage,
        "duration": {"unit": "round", "rounds": value * per_unit},
        "concentration": bool(duration.get("concentration")),
        "created_round": _as_int(concentration_info.get("round"), 0),
    }


def trigger(
    room_dir: Any,
    area_id: str,
    target: str,
    *,
    round_number: int,
    apply: Callable[[dict[str, Any], str], Any] | None = None,
) -> dict[str, Any]:
    """触发一次区域；同一目标同一回合只结算一次。"""
    area_id = _text(area_id)
    target = _text(target)
    round_number = _as_int(round_number, 0)
    if not target:
        return {"error": "缺少 target", "code": "area.missing_target"}
    with room_write_lock(room_dir):
        state = load(room_dir)
        area = next((item for item in state["areas"] if _text(item.get("id")) == area_id), None)
        if area is None:
            ended = [
                item for item in load(room_dir)["areas"]
                if _text(item.get("id")) == area_id
            ]
            return {"error": "区域不存在或已结束", "code": "area.not_found", "ended": ended}
        triggered = area.get("triggered") if isinstance(area.get("triggered"), Mapping) else {}
        if round_number and _as_int(triggered.get(target), -1) == round_number:
            return {"duplicate": True, "area": dict(area)}
        if round_number:
            triggered = dict(triggered)
            triggered[target] = round_number
            area["triggered"] = triggered
    result = apply(area, target) if callable(apply) else {"area": dict(area), "target": target}
    with room_write_lock(room_dir):
        state = load(room_dir)
        for item in state["areas"]:
            if _text(item.get("id")) == area_id:
                item["triggered"] = dict(area["triggered"])
                item["last_triggered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        state["revision"] = _as_int(state.get("revision"), 0) + 1
        save(room_dir, state)
    return {"duplicate": False, "area": dict(area), "result": result}


__all__ = [
    "AREAS_FILENAME",
    "SHAPES",
    "TRIGGERS",
    "create_area",
    "list_areas",
    "load",
    "remove_area",
    "scene_id_of",
    "tick_areas",
    "trigger",
]
