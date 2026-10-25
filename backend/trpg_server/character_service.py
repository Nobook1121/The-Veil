"""角色卡的领域服务。

路由与网站 Agent 都从这里进入同一套保存逻辑，避免两条写路径产生不同结果。
本模块刻意不依赖 Flask 的全局 session：身份全部来自 :class:`Actor`。
"""

from __future__ import annotations

import base64
import binascii
import re
import shutil
import time
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server import weapon_catalog
from trpg_server.config import load_settings
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import CHARACTERS_DIR, CONFIG_DIR, ROOMS_DIR, WEAPONS_DIR

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
    "avatar",
    "weapons",
    "customWeapons",
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


def _get_weapons_dir() -> Path:
    return Path(current_app.config.get("WEAPONS_DIR", WEAPONS_DIR))


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        return ""
    # 文件名只允许安全字符，与路由保存时的 normalize_filename 保持一致语义。
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_.")[:80]


def character_dir(character_id: str) -> Path:
    """角色卡独立目录：头像、自定义武器等卡片专属资源都在这里。"""
    return _characters_dir() / _normalize_id(character_id)


def character_json_path(character_id: str) -> Path:
    return character_dir(character_id) / "character.json"


def legacy_character_path(character_id: str) -> Path:
    return _characters_dir() / f"{_normalize_id(character_id)}.json"


def iter_character_paths() -> list[tuple[str, Path]]:
    """兼容旧版平铺 ``<id>.json``，优先读取新目录结构。"""
    directory = _characters_dir()
    if not directory.exists():
        return []
    entries: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in sorted(directory.iterdir()):
        if not item.is_dir():
            continue
        card_path = item / "character.json"
        if card_path.exists():
            entries.append((item.name, card_path))
            seen.add(item.name)
    for path in sorted(directory.glob("*.json")):
        if path.stem not in seen:
            entries.append((path.stem, path))
    return entries


def _avatar_dir(character_id: str) -> Path:
    return character_dir(character_id)


_AVATAR_DATA_PATTERN = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)
_AVATAR_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_MAX_AVATAR_BYTES = 4 * 1024 * 1024


def _is_elevated(actor: Any) -> bool:
    return bool(actor.has("characters.manage_all"))


def _player_ids(actor: Any) -> set[str]:
    return {str(actor.user_id), str(actor.username or "")}


def _max_cards_per_user() -> int:
    # 统一走分层加载（含模板/env/CLI），schema 已保证返回 [1, 50] 的 int。
    config_dir = Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
    return int(load_settings(config_dir).data["character_rules"]["max_cards_per_user"])


def _load_stored(character_id: str) -> dict[str, Any] | None:
    from trpg_server.routes.characters import _character_from_storage

    normalized = _normalize_id(character_id)
    if not normalized:
        return None
    for path in (character_json_path(normalized), legacy_character_path(normalized)):
        if path.exists():
            return _character_from_storage(read_json(path, default={}), normalized)
    return None


def _owned_count(actor: Any, *, exclude: str | None = None) -> int:
    from trpg_server.routes.characters import _character_from_storage

    players = _player_ids(actor)
    count = 0
    for character_id, path in iter_character_paths():
        if exclude and character_id == exclude:
            continue
        character = _character_from_storage(read_json(path, default=None), character_id)
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

    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    entries = sorted(iter_character_paths(), key=lambda entry: entry[1].stat().st_mtime, reverse=True)
    for character_id, path in entries:
        character = _character_from_storage(read_json(path, default=None), character_id)
        if not character or not can_access(actor, character):
            continue
        name = str(character.get("name") or "")
        if needle and needle not in name.lower() and needle not in str(character.get("id") or "").lower():
            continue
        items.append(
            {
                "id": str(character.get("id") or character_id),
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


def _persist_avatar(character: dict[str, Any]) -> dict[str, Any]:
    """把 data URI 头像写入角色卡目录，并把卡内字段替换为可访问 URL。"""
    character_id = _normalize_id(character.get("id"))
    if not character_id:
        return character
    card_dir = _avatar_dir(character_id)
    value = str(character.get("avatar") or "").strip()
    if not value:
        if card_dir.exists():
            for path in card_dir.glob("avatar.*"):
                path.unlink(missing_ok=True)
        character["avatar"] = ""
        return character
    match = _AVATAR_DATA_PATTERN.match(value)
    if not match:
        return character
    ext = _AVATAR_MIME_EXT.get(match.group(1).lower())
    if not ext:
        raise CharacterServiceError("invalid_avatar", "不支持的头像格式", 400)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise CharacterServiceError("invalid_avatar", "头像数据损坏", 400)
    if not data:
        raise CharacterServiceError("invalid_avatar", "头像内容为空", 400)
    if len(data) > _MAX_AVATAR_BYTES:
        raise CharacterServiceError("invalid_avatar", "头像文件过大（上限 4MB）", 400)
    card_dir.mkdir(parents=True, exist_ok=True)
    for path in card_dir.glob("avatar.*"):
        path.unlink(missing_ok=True)
    avatar_path = card_dir / f"avatar.{ext}"
    avatar_path.write_bytes(data)
    character["avatar"] = f"/data/characters/{character_id}/avatar.{ext}"
    return character


def _sync_weapon_payload(character: dict[str, Any], raw_payload: dict[str, Any]) -> dict[str, Any]:
    """解析 Agent 传入的 presetId/custom 武器，并同步角色卡自定义武器库。"""
    custom_values = raw_payload.get("customWeapons")
    additions = list(custom_values) if isinstance(custom_values, list) else []
    rows = raw_payload.get("weapons")
    if isinstance(rows, list):
        presets = weapon_catalog.load_preset_weapons(_get_weapons_dir())
        resolved_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            preset_ref = str(row.get("presetId") or row.get("preset_id") or "").strip()
            preset = weapon_catalog.find_preset_weapon(presets, preset_ref) if preset_ref else None
            if preset:
                resolved_rows.append(weapon_catalog.merge_character_weapon_row(row, preset))
                continue
            if row.get("custom") is True or row.get("customId"):
                additions.append(dict(row))
            resolved_rows.append(
                {key: value for key, value in row.items() if key not in {"presetId", "preset_id", "customId", "custom"}}
            )
        character["weapons"] = resolved_rows
    character.pop("customWeapons", None)
    if additions:
        card_dir = character_dir(character["id"])
        existing = weapon_catalog.read_custom_weapons(card_dir)
        merged = weapon_catalog.upsert_custom_weapons(existing, additions)
        weapon_catalog.write_custom_weapons(card_dir, merged)
    return character


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
    character = _sync_weapon_payload(character, raw_payload)
    character = _persist_avatar(character)
    write_json_atomic(character_json_path(character["id"]), _runtime_to_test_character(character))
    legacy_path = legacy_character_path(character["id"])
    if legacy_path.exists():
        legacy_path.unlink(missing_ok=True)
    _sync_room_character_snapshots(character)
    return character


def get_custom_weapons(actor: Any, character_id: str) -> list[dict[str, Any]]:
    character = get_for_actor(actor, character_id)
    return weapon_catalog.read_custom_weapons(character_dir(character["id"]))


def save_custom_weapons(actor: Any, character_id: str, weapons: list[Any]) -> list[dict[str, Any]]:
    character = get_for_actor(actor, character_id)
    return weapon_catalog.write_custom_weapons(character_dir(character["id"]), weapons)


def delete_character_storage(character_id: str) -> None:
    normalized = _normalize_id(character_id)
    if not normalized:
        return
    directory = character_dir(normalized)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    legacy_character_path(normalized).unlink(missing_ok=True)


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
