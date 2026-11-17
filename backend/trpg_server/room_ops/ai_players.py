"""AI 托管与 AI 玩家的成员状态、消息归属与回合互斥。

设计要点：

- 控制权只有一个来源：成员的 ``control_mode``。真人托管与 AI 玩家都落成
  ``ai``，真人成员默认 ``human``。同一角色不会出现 AI 与真人同时操作。
- AI 玩家是房间成员的一种形态（``is_ai_player`` + 合成 ``user_id``），
  仍复用成员角色卡、角色状态、角色卡占用与消息可见性，不另建角色体系。
- AI 生成的消息沿用现有消息结构，只增加 ``metadata.ai_controlled`` 等标记，
  供前端区分「AI 代打 / AI 玩家」与真人发言。
"""
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import uuid4

from trpg_server.room_ops.cards import _initial_character_state, _sanitize_character_card
from trpg_server.room_ops.constants import ROOM_MEMBER_ACTIVE, ROOM_ROLE_MEMBER
from trpg_server.room_ops.storage import _read_messages, _timestamp, _write_messages

AI_PLAYER_USER_PREFIX = "ai-player:"
CONTROL_MODE_HUMAN = "human"
CONTROL_MODE_AI = "ai"
CONTROL_MODES = (CONTROL_MODE_HUMAN, CONTROL_MODE_AI)

# 同一成员同一时间只允许一个 AI 回合；按 (room_id, user_id) 分锁。
_TURN_LOCKS: dict[str, threading.Lock] = {}
_TURN_LOCKS_GUARD = threading.Lock()
_LAST_RUN: dict[str, float] = {}
_LAST_RUN_GUARD = threading.Lock()
_TURN_COOLDOWN_SECONDS = 3.0


def is_ai_player(member: Any) -> bool:
    return bool(isinstance(member, dict) and member.get("is_ai_player"))


def control_mode_of(member: Any) -> str:
    """成员当前控制权。旧数据没有该字段时：AI 玩家按 ai，真人按 human。"""
    if not isinstance(member, dict):
        return CONTROL_MODE_HUMAN
    mode = str(member.get("control_mode") or "").strip().lower()
    if mode in CONTROL_MODES:
        return mode
    return CONTROL_MODE_AI if is_ai_player(member) else CONTROL_MODE_HUMAN


def ai_controlled(member: Any) -> bool:
    """该成员当前是否由 AI 操作（AI 玩家或被托管的人类角色）。"""
    return bool(isinstance(member, dict) and control_mode_of(member) == CONTROL_MODE_AI)


def human_controlled(member: Any) -> bool:
    return bool(isinstance(member, dict) and control_mode_of(member) == CONTROL_MODE_HUMAN)


def make_ai_player_member(
    character_card: Any,
    *,
    created_by: Any,
    username: str | None = None,
    avatar: str = "",
) -> dict[str, Any] | None:
    """构造一个 AI 玩家成员记录；角色卡无效时返回 ``None``。"""
    card = _sanitize_character_card(character_card)
    if not card:
        return None
    display_name = str(username or card.get("name") or "AI 玩家").strip()[:80]
    now = _timestamp()
    member = {
        "user_id": f"{AI_PLAYER_USER_PREFIX}{uuid4().hex}",
        "username": display_name,
        "role": "USER",
        "room_role": ROOM_ROLE_MEMBER,
        "status": ROOM_MEMBER_ACTIVE,
        "is_active": True,
        "is_ai_player": True,
        "control_mode": CONTROL_MODE_AI,
        "created_by": created_by,
        "avatar": str(avatar or "").strip(),
        "joined_at": now,
        "character_card": card,
        "character_state": _initial_character_state(card),
    }
    return member


def set_control_mode(member: dict[str, Any], mode: str, *, actor: Any) -> bool:
    """写入控制权与审计字段；模式非法时返回 ``False``。"""
    if not isinstance(member, dict) or mode not in CONTROL_MODES:
        return False
    member["control_mode"] = mode
    member["ai_control"] = {
        "enabled": mode == CONTROL_MODE_AI,
        "updated_by": actor,
        "updated_at": _timestamp(),
    }
    return True


def ai_badge_metadata(member: Any) -> dict[str, Any]:
    """消息上用于区分 AI 发言的元数据。"""
    if not isinstance(member, dict):
        return {}
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    return {
        "ai_controlled": True,
        "control_mode": CONTROL_MODE_AI,
        "ai_player": is_ai_player(member),
        "ai_member_id": str(member.get("user_id") or ""),
        "ai_character_name": str(card.get("name") or ""),
    }


def _player_message(member: dict[str, Any], content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    merged = ai_badge_metadata(member)
    merged.update(metadata or {})
    return {
        "id": uuid4().hex,
        "type": "player",
        "sender_id": member.get("user_id"),
        "sender_name": str(member.get("username") or card.get("name") or "玩家"),
        "avatar": str(member.get("avatar") or ""),
        "content": str(content or "").strip(),
        "time": time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": merged,
    }


def _tool_message(member: dict[str, Any], tool_message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool_message, dict):
        return None
    content = str(tool_message.get("content") or "").strip()
    if not content:
        return None
    metadata = tool_message.get("metadata") if isinstance(tool_message.get("metadata"), dict) else {}
    merged_metadata = dict(metadata)
    merged_metadata.update(ai_badge_metadata(member))
    return {
        "id": uuid4().hex,
        "type": str(tool_message.get("type") or "dice"),
        "sender_id": None,
        "sender_name": str(tool_message.get("sender_name") or tool_message.get("sender") or "骰娘"),
        "avatar": str(tool_message.get("avatar") or ""),
        "content": content,
        "time": time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": merged_metadata,
    }


def persist_player_turn(
    room_dir,
    member: dict[str, Any],
    content: str,
    tool_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把一次 AI 玩家回合的消息一次性写入房间消息文件，返回实际落库的消息。"""
    created: list[dict[str, Any]] = []
    messages = _read_messages(room_dir)
    if not isinstance(messages, list):
        messages = []
    for tool_message in tool_messages or []:
        normalized = _tool_message(member, tool_message)
        if normalized:
            messages.append(normalized)
            created.append(normalized)
    final = _player_message(member, content)
    if final["content"]:
        messages.append(final)
        created.append(final)
    if created:
        _write_messages(room_dir, messages)
    return created


def turn_lock(room_id: Any, user_id: Any) -> threading.Lock:
    """返回该成员专属的 AI 回合锁；淘汰只清理未被持有的锁。"""
    key = f"{room_id}:{user_id}"
    with _TURN_LOCKS_GUARD:
        if len(_TURN_LOCKS) > 512:
            for stale_key, lock in list(_TURN_LOCKS.items()):
                if not lock.locked():
                    _TURN_LOCKS.pop(stale_key, None)
        return _TURN_LOCKS.setdefault(key, threading.Lock())


def cooldown_remaining(room_id: Any, user_id: Any) -> float:
    key = f"{room_id}:{user_id}"
    now = time.monotonic()
    with _LAST_RUN_GUARD:
        last = _LAST_RUN.get(key, 0.0)
        return max(0.0, _TURN_COOLDOWN_SECONDS - (now - last))


def mark_turn_started(room_id: Any, user_id: Any) -> None:
    key = f"{room_id}:{user_id}"
    with _LAST_RUN_GUARD:
        _LAST_RUN[key] = time.monotonic()
        if len(_LAST_RUN) > 512:
            cutoff = time.monotonic() - 300
            for stale_key, stamp in list(_LAST_RUN.items()):
                if stamp < cutoff:
                    _LAST_RUN.pop(stale_key, None)
