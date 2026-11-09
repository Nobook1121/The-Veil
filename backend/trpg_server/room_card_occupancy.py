"""角色卡占用检测：一张角色卡同一时刻只能绑定一个活跃房间。

占用事实不落表，按活跃房间的成员列表现算：房间未归档、成员仍在场、
成员绑定的 ``character_card.id`` 相同即视为占用。这样离开、被移出、
房间归档都会自动释放，不需要迁移，也不会出现第二份真相。

并发安全：占用是跨房间的事实，「查占用 + 落盘」必须整体串行，按房间
加锁挡不住两个房间同时通过校验。服务以单进程多线程运行（``server.py``
的 ``make_server`` 加 Socket.IO 线程），因此这里用进程级 RLock。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from trpg_server.json_store import read_json

_binding_lock = threading.RLock()


@contextmanager
def card_binding_lock() -> Iterator[None]:
    """串行化「查角色卡占用 + 写房间快照」的临界区。"""
    with _binding_lock:
        yield


def is_active_member(member: Any) -> bool:
    """与房间读取链路一致的在场判断：显式失活或被移除都算离场。"""
    if not isinstance(member, dict):
        return False
    if member.get("is_active", True) is False:
        return False
    return str(member.get("status") or "active") != "removed"


def card_id_of(character_card: Any) -> str:
    """取出角色卡 id；无效卡（没有 id）返回空串，表示不参与占用判断。"""
    if not isinstance(character_card, dict):
        return ""
    return str(character_card.get("id") or "").strip()


def find_card_occupancy(
    rooms_dir: str | Path,
    card_id: str,
    *,
    exclude_room_id: str | None = None,
) -> dict[str, Any] | None:
    """返回正在使用该卡的活跃房间摘要；无人占用时返回 ``None``。

    ``exclude_room_id`` 用于「已经在目标房间里」的场景，避免把自身当成冲突。
    """
    if not card_id:
        return None
    rooms_dir = Path(rooms_dir)
    if not rooms_dir.exists():
        return None
    for room_dir in sorted(rooms_dir.iterdir()):
        if not room_dir.is_dir():
            continue
        info = read_json(room_dir / "info.json", default=None)
        if not isinstance(info, dict) or info.get("archived"):
            continue
        room_id = str(info.get("id") or room_dir.name)
        if exclude_room_id is not None and room_id == str(exclude_room_id):
            continue
        for member in info.get("members") or []:
            if not is_active_member(member):
                continue
            card = member.get("character_card")
            if card_id_of(card) != card_id:
                continue
            return {
                "room_id": room_id,
                "room_name": str(info.get("name") or room_id),
                "card_id": card_id,
                "card_name": str((card or {}).get("name") or ""),
                "user_id": member.get("user_id"),
                "username": member.get("username"),
            }
    return None
