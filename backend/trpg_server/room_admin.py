"""房间巡检与维护（控制台专用）。

房间按目录组织（``rooms/<room_id>/info.json``）。本模块直接读写这些 JSON，
不在依赖 Flask 上下文的路径上运行；仅「归档」需要对外的不可变归档快照，
此时才在调用方注入的 app 上下文中复用 Web 路由的 ``_build_room_archive``。
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

ROOM_MEMBER_ACTIVE = "active"
ROOM_MEMBER_REMOVED = "removed"


def _timestamp() -> str:
    """与 Web 路由保持一致的本地时间格式。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _room_dirs(rooms_dir: Any) -> list[Path]:
    root = Path(rooms_dir)
    if not root.exists():
        return []
    return sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)


def _read_info(room_dir: Path) -> dict[str, Any]:
    value = read_json(room_dir / "info.json", default={})
    return value if isinstance(value, dict) else {}


def _is_active_member(member: dict[str, Any]) -> bool:
    return (
        member.get("is_active", True) is not False
        and member.get("status", ROOM_MEMBER_ACTIVE) != ROOM_MEMBER_REMOVED
    )


def _active_members(info: dict[str, Any]) -> list[dict[str, Any]]:
    members = info.get("members") or []
    return [m for m in members if isinstance(m, dict) and _is_active_member(m)]


def _find_room_dir(rooms_dir: Any, room_id: Any) -> Path | None:
    """按目录名 / 房间 id / 房间码定位房间目录。"""

    root = Path(rooms_dir)
    direct = root / str(room_id)
    if (direct / "info.json").exists():
        return direct
    reference = str(room_id).strip()
    reference_upper = reference.upper()
    for room_dir in _room_dirs(root):
        info = _read_info(room_dir)
        if str(info.get("id")) == reference:
            return room_dir
        if str(info.get("room_code") or "").upper() == reference_upper:
            return room_dir
    return None


def list_rooms_summary(rooms_dir: Any, search: str = "") -> list[dict[str, Any]]:
    """列出房间：ID / 名称 / 房间码 / 剧本 / 成员数 / 是否归档。"""

    needle = str(search or "").strip().casefold()
    rooms: list[dict[str, Any]] = []
    for room_dir in _room_dirs(rooms_dir):
        info = _read_info(room_dir)
        room_id = str(info.get("id") or room_dir.name)
        name = str(info.get("name") or room_id)
        haystack = (
            f"{room_id} {name} {info.get('room_code') or ''} {info.get('scenario_title') or ''}"
        ).casefold()
        if needle and needle not in haystack:
            continue
        rooms.append(
            {
                "id": room_id,
                "name": name,
                "room_code": info.get("room_code"),
                "scenario_id": info.get("scenario_id"),
                "scenario_title": info.get("scenario_title"),
                "members": len(_active_members(info)),
                "archived": bool(info.get("archived")),
                "created_at": info.get("created_at"),
                "updated_at": info.get("updated_at"),
            }
        )
    rooms.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rooms


def room_members(rooms_dir: Any, room_id: Any) -> dict[str, Any]:
    """查看某房间的成员与其房间角色。"""

    room_dir = _find_room_dir(rooms_dir, room_id)
    if room_dir is None:
        raise LookupError(f"room not found: {room_id}")
    info = _read_info(room_dir)
    creator_id = str(info.get("creator_id") or "")
    members: list[dict[str, Any]] = []
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        room_role = member.get("room_role")
        if not room_role:
            room_role = "owner" if str(member.get("user_id")) == creator_id else "member"
        members.append(
            {
                "user_id": member.get("user_id"),
                "username": member.get("username") or member.get("name"),
                "role": member.get("role"),
                "room_role": room_role,
                "active": _is_active_member(member),
            }
        )
    return {"id": info.get("id") or str(room_id), "name": info.get("name"), "members": members}


def _is_finished(info: dict[str, Any], state: dict[str, Any]) -> bool:
    if info.get("completed_at") or info.get("ended_at"):
        return True
    if isinstance(state, dict):
        return bool(state.get("completed_at") or state.get("completed") or state.get("ending_reached"))
    return False


def archive_room(rooms_dir: Any, room_id: Any, *, app: Any = None) -> dict[str, Any]:
    """归档房间（与网页一致：保留消息、生成不可变归档快照）。

    ``app`` 为 Flask 应用实例时会在其上下文中生成归档快照；未注入时仅落盘
    归档标记，仍保留房间可读性。
    """

    room_dir = _find_room_dir(rooms_dir, room_id)
    if room_dir is None:
        raise LookupError(f"room not found: {room_id}")
    info = _read_info(room_dir)
    if info.get("archived"):
        raise ValueError("房间已经归档，无需重复操作")
    state = read_json(room_dir / "state.json", default={})
    state = state if isinstance(state, dict) else {}
    if not _is_finished(info, state):
        raise ValueError("房间尚未到达结局，无法归档（网页规则一致）")

    now = _timestamp()
    info["completed_at"] = info.get("completed_at") or now
    info["archived_at"] = now
    info["archived"] = True
    info.pop("house_rules", None)
    info["updated_at"] = now
    write_json_atomic(room_dir / "info.json", info)

    snapshot_built = False
    if app is not None:
        try:
            with app.app_context():
                from trpg_server.routes.rooms import _build_room_archive

                _build_room_archive(room_dir, info)
                snapshot_built = True
        except Exception:  # noqa: BLE001 - 快照失败不应回滚归档标记
            logger.exception("Failed to build room archive snapshot")
    return {
        "id": info.get("id") or str(room_id),
        "name": info.get("name"),
        "archived_at": now,
        "snapshot": snapshot_built,
    }


def _parse_time(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def find_stale_rooms(rooms_dir: Any, days: int = 30, now: float | None = None) -> list[dict[str, Any]]:
    """找出僵尸房间：未归档、无有效成员、且长期无活动（默认 30 天）。"""

    horizon = (now if now is not None else time.time()) - max(1, int(days)) * 86400
    stale: list[dict[str, Any]] = []
    for room_dir in _room_dirs(rooms_dir):
        info = _read_info(room_dir)
        if info.get("archived"):
            continue
        if _active_members(info):
            continue
        stamp = _parse_time(info.get("updated_at") or info.get("created_at"))
        if stamp is None or stamp > horizon:
            continue
        stale.append(
            {
                "id": str(info.get("id") or room_dir.name),
                "name": info.get("name"),
                "scenario_title": info.get("scenario_title"),
                "last_active": info.get("updated_at") or info.get("created_at"),
            }
        )
    return stale


def remove_room(rooms_dir: Any, room_id: Any) -> bool:
    """删除房间目录（含消息）。调用方必须已做二次确认。"""

    room_dir = _find_room_dir(rooms_dir, room_id)
    if room_dir is None:
        return False
    shutil.rmtree(room_dir)
    return True


def trigger_autosave(app: Any) -> dict[str, Any]:
    """立即触发一次自动存档检查（复用后台调度入口，需要 app 上下文）。"""

    if app is None:
        raise ValueError("autosave 需要运行中的服务实例（未注入 app）")
    with app.app_context():
        from trpg_server.routes.rooms import run_scheduled_autosaves

        run_scheduled_autosaves()
    return {"triggered": True}
