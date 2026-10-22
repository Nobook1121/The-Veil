"""角色卡的领域服务。

路由与网站 Agent 都从这里进入同一套保存逻辑，避免两条写路径产生不同结果。
本模块刻意不依赖 Flask 的全局 session：身份全部来自 :class:`Actor`。
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import CHARACTERS_DIR, CONFIG_DIR, ROOMS_DIR

DEFAULT_MAX_CARDS_PER_USER = 3
CHARACTER_WRITE_FIELDS = (
    "name",
    "occupationId",
    "attributes",
    "skills",
    "maxHp",
    "maxSan",
    "mov",
    "equipment",
    "background",
    "playerId",
    "age",
    "gender",
    "residence",
    "birthplace",
    "portrait",
)


class CharacterServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _characters_dir() -> Path:
    return Path(current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR))


def _get_rooms_dir() -> Path:
    return Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR))


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        return ""
    # 文件名只允许安全字符，与路由保存时的 normalize_filename 保持一致语义。
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_.")[:80]


def _character_path(character_id: str) -> Path:
    return _characters_dir() / f"{_normalize_id(character_id)}.json"


def _is_elevated(actor: Any) -> bool:
    return bool(actor.has("characters.manage_all"))


def _player_ids(actor: Any) -> set[str]:
    return {str(actor.user_id), str(actor.username or "")}


def _max_cards_per_user() -> int:
    path = Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR)) / "general.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_MAX_CARDS_PER_USER
    value = (data.get("character_rules") or {}).get("max_cards_per_user", DEFAULT_MAX_CARDS_PER_USER)
    try:
        return max(1, min(int(value), 999))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CARDS_PER_USER


def _load_stored(character_id: str) -> dict[str, Any] | None:
    from trpg_server.routes.characters import _character_from_storage

    path = _character_path(character_id)
    if not path.exists():
        return None
    return _character_from_storage(read_json(path, default={}), character_id)


def _owned_count(actor: Any, *, exclude: str | None = None) -> int:
    from trpg_server.routes.characters import _character_from_storage

    players = _player_ids(actor)
    count = 0
    directory = _characters_dir()
    if not directory.exists():
        return 0
    for path in directory.glob("*.json"):
        if exclude and path.stem == exclude:
            continue
        character = _character_from_storage(read_json(path, default=None), path.stem)
        if character and str(character.get("playerId") or "") in players:
            count += 1
    return count


def can_access(actor: Any, character: dict[str, Any] | None) -> bool:
    if character is None:
        return False
    if _is_elevated(actor):
        return True
    player_id = str(character.get("playerId") or "")
    return bool(player_id) and player_id in _player_ids(actor)


def list_for_actor(actor: Any, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    from trpg_server.routes.characters import _character_from_storage

    directory = _characters_dir()
    if not directory.exists():
        return []
    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        character = _character_from_storage(read_json(path, default=None), path.stem)
        if not character or not can_access(actor, character):
            continue
        name = str(character.get("name") or "")
        if needle and needle not in name.lower() and needle not in str(character.get("id") or "").lower():
            continue
        items.append(
            {
                "id": str(character.get("id") or path.stem),
                "name": name,
                "occupationId": str(character.get("occupationId") or ""),
                "playerId": str(character.get("playerId") or ""),
                "updatedAt": str(character.get("updatedAt") or ""),
            }
        )
        if len(items) >= max(1, min(int(limit or 20), 50)):
            break
    return items


def get_for_actor(actor: Any, character_id: str) -> dict[str, Any]:
    character = _load_stored(character_id)
    if not character:
        raise CharacterServiceError("character_not_found", "角色卡不存在", 404)
    if not can_access(actor, character):
        raise CharacterServiceError("permission_denied", "无权访问该角色卡", 403)
    return character


def _normalize_payload(actor: Any, payload: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    character = dict(existing or {})
    character.update(payload or {})
    character_id = _normalize_id(character.get("id"))
    name = str(character.get("name") or "").strip()
    if not character_id or not name:
        raise CharacterServiceError("invalid_character", "角色卡缺少 ID 或名称", 400)
    if not _is_elevated(actor):
        character["playerId"] = str(actor.user_id)
    else:
        character["playerId"] = str(character.get("playerId") or actor.user_id)
    character["id"] = character_id
    character["name"] = name[:80]
    character.pop("_agent_action", None)
    character["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    character.setdefault("createdAt", character["updatedAt"])
    return character


def preview_save(actor: Any, character_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """生成待确认摘要，不落盘；权限错误会直接抛出。"""
    existing = _load_stored(character_id)
    if existing and not can_access(actor, existing):
        raise CharacterServiceError("permission_denied", "无权修改该角色卡", 403)
    if not existing and not actor.has("characters.create"):
        raise CharacterServiceError("permission_denied", "缺少 characters.create 权限", 403)
    if not existing and not _is_elevated(actor) and _owned_count(actor) >= _max_cards_per_user():
        raise CharacterServiceError("character_limit", "角色卡数量已达上限", 403)
    candidate = _normalize_payload(actor, {"id": character_id, **payload}, existing or {})
    changed_fields = [
        field for field in CHARACTER_WRITE_FIELDS if field in payload and payload.get(field) != (existing or {}).get(field)
    ]
    return {
        "character_id": candidate["id"],
        "name": candidate["name"],
        "is_new": not existing,
        "changed_fields": changed_fields,
        "playerId": candidate.get("playerId", ""),
        "summary": f"{'创建' if not existing else '更新'}角色卡「{candidate['name']}」",
    }


def apply_save(actor: Any, character_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from trpg_server.routes.characters import (
        _runtime_to_test_character,
        _sync_room_character_snapshots,
        _test_character_to_runtime,
    )

    existing = _load_stored(character_id)
    if existing and not can_access(actor, existing):
        raise CharacterServiceError("permission_denied", "无权修改该角色卡", 403)
    if not existing and not actor.has("characters.create"):
        raise CharacterServiceError("permission_denied", "缺少 characters.create 权限", 403)
    if not existing and not _is_elevated(actor) and _owned_count(actor) >= _max_cards_per_user():
        raise CharacterServiceError("character_limit", "角色卡数量已达上限", 403)

    raw_payload = dict(payload or {})
    if _looks_like_test_shape(raw_payload):
        raw_payload = _test_character_to_runtime(raw_payload, character_id)
    raw_payload["id"] = character_id
    character = _normalize_payload(actor, raw_payload, existing or {})
    write_json_atomic(_character_path(character["id"]), _runtime_to_test_character(character))
    _sync_room_character_snapshots(character)
    return character


def _looks_like_test_shape(character: dict[str, Any]) -> bool:
    return any(
        key in character
        for key in (
            "deriveAttributes",
            "battleAttributes",
            "characterStatus",
            "skillGroups",
            "stories",
            "experiencedModules",
            "friends",
        )
    )


def new_character_id(actor: Any) -> str:
    return f"investigator-{int(time.time() * 1000)}"
