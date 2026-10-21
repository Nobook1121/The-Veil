"""骰子贴图库：桌面贴图、逐面骰子贴图与皮肤套装。

索引以原子方式写入 ``data/dice-textures/index.json``；图片只做文件头魔数与
尺寸校验，不引入 Pillow。文件名为 UUID，天然避免路径穿越。
"""
from __future__ import annotations

import struct
import time
import uuid
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import DATA_DIR

DICE_TEXTURES_DIR = DATA_DIR / "dice-textures"
INDEX_VERSION = 1
MAX_BYTES = 3 * 1024 * 1024
MIN_DIMENSION = 128
MAX_DIMENSION = 2048
DIE_TYPES = {"d4": 4, "d6": 6, "d8": 8, "d10": 10, "d12": 12, "d20": 20, "d100": 10}
ASSET_KINDS = {"table", "dice", "face"}


class TextureError(Exception):
    """贴图操作失败，``reason`` 用于返回给前端。"""

    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _base_dir() -> Path:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            configured = current_app.config.get("DICE_TEXTURES_DIR")
            if configured:
                return Path(configured)
    except Exception:  # noqa: BLE001
        pass
    return DICE_TEXTURES_DIR


def _index_path() -> Path:
    return _base_dir() / "index.json"


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "assets": [], "table_skins": [], "dice_skins": []}


def read_index() -> dict[str, Any]:
    data = read_json(_index_path(), default=None)
    if not isinstance(data, dict):
        return _empty_index()
    for key in ("assets", "table_skins", "dice_skins"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data["version"] = int(data.get("version") or INDEX_VERSION)
    return data


def write_index(data: dict[str, Any]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, data)


def _detect_image(blob: bytes) -> tuple[str, int, int]:
    """通过文件头魔数识别图片并读取尺寸，不信任扩展名与 MIME。"""
    if len(blob) >= 24 and blob[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", blob[16:24])
        return "image/png", int(width), int(height)
    if blob[:2] == b"\xff\xd8":
        index = 2
        length = len(blob)
        while index + 9 < length:
            if blob[index] != 0xFF:
                index += 1
                continue
            marker = blob[index + 1]
            if marker in (0xD8, 0xD9):
                index += 2
                continue
            segment = struct.unpack(">H", blob[index + 2:index + 4])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", blob[index + 5:index + 9])
                return "image/jpeg", int(width), int(height)
            index += 2 + segment
        raise TextureError("unsupported_type")
    if len(blob) >= 30 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        chunk = blob[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(blob[24:27], "little")
            height = 1 + int.from_bytes(blob[27:30], "little")
            return "image/webp", width, height
        if chunk == b"VP8L":
            bits = int.from_bytes(blob[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return "image/webp", width, height
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", blob[26:30])
            return "image/webp", width & 0x3FFF, height & 0x3FFF
        raise TextureError("unsupported_type")
    raise TextureError("unsupported_type")


def validate_image(blob: bytes) -> tuple[str, int, int]:
    if not blob:
        raise TextureError("unsupported_type")
    if len(blob) > MAX_BYTES:
        raise TextureError("too_large")
    content_type, width, height = _detect_image(blob)
    if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
        raise TextureError("bad_dimensions")
    return content_type, width, height


def face_keys(die_type: str) -> list[str]:
    sides = DIE_TYPES.get(str(die_type).lower())
    if not sides:
        raise TextureError("bad_die_type")
    if die_type in {"d10", "d100"}:
        return [str(value) for value in range(10)]
    return [str(value) for value in range(1, sides + 1)]


def create_asset(blob: bytes, *, name: str, kind: str, uploaded_by: Any = None) -> dict[str, Any]:
    if kind not in ASSET_KINDS:
        raise TextureError("unsupported_type")
    content_type, width, height = validate_image(blob)
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[content_type]
    asset_id = f"asset-{uuid.uuid4().hex}"
    relative = Path("assets") / f"{asset_id}{extension}"
    target = _base_dir() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    asset = {
        "id": asset_id,
        "name": str(name or "").strip() or Path(relative).stem,
        "kind": kind,
        "file": relative.as_posix(),
        "content_type": content_type,
        "width": width,
        "height": height,
        "size": len(blob),
        "uploaded_by": uploaded_by,
        "created_at": time.time(),
        "enabled": True,
    }
    index = read_index()
    index["assets"].append(asset)
    write_index(index)
    return asset


def find_asset(asset_id: str) -> dict[str, Any] | None:
    return next((item for item in read_index()["assets"] if item.get("id") == asset_id), None)


def asset_path(asset: dict[str, Any]) -> Path:
    file = str(asset.get("file") or "")
    if not file:
        raise TextureError("unsupported_type", 404)
    base = _base_dir().resolve()
    target = (base / file).resolve()
    if base not in target.parents:
        raise TextureError("unsupported_type", 404)
    return target


def delete_asset(asset_id: str) -> None:
    index = read_index()
    asset = next((item for item in index["assets"] if item.get("id") == asset_id), None)
    if asset is None:
        raise TextureError("not_found", 404)
    for skin in index["table_skins"]:
        if skin.get("asset_id") == asset_id:
            raise TextureError("asset_in_use", 409)
    for skin in index["dice_skins"]:
        faces = skin.get("faces") if isinstance(skin.get("faces"), dict) else {}
        if asset_id in faces.values():
            raise TextureError("asset_in_use", 409)
    index["assets"] = [item for item in index["assets"] if item.get("id") != asset_id]
    write_index(index)
    try:
        asset_path(asset).unlink(missing_ok=True)
    except OSError:
        pass


def _normalize_faces(die_type: str, faces: Any) -> dict[str, str | None]:
    allowed = set(face_keys(die_type))
    if not isinstance(faces, dict):
        raise TextureError("bad_face_key")
    normalized: dict[str, str | None] = {}
    for key, value in faces.items():
        text = str(key)
        if text not in allowed:
            raise TextureError("bad_face_key")
        normalized[text] = str(value) if value else None
    return normalized


def upsert_table_skin(payload: dict[str, Any]) -> dict[str, Any]:
    index = read_index()
    asset_id = str(payload.get("asset_id") or "")
    if not asset_id or find_asset(asset_id) is None:
        raise TextureError("asset_in_use")
    skin_id = str(payload.get("id") or f"table-skin-{uuid.uuid4().hex}")
    skin = {
        "id": skin_id,
        "name": str(payload.get("name") or "").strip() or "桌面皮肤",
        "asset_id": asset_id,
        "enabled": bool(payload.get("enabled", True)),
    }
    index["table_skins"] = [item for item in index["table_skins"] if item.get("id") != skin_id] + [skin]
    write_index(index)
    return skin


def upsert_dice_skin(payload: dict[str, Any]) -> dict[str, Any]:
    die_type = str(payload.get("die_type") or "").strip().lower()
    if die_type not in DIE_TYPES:
        raise TextureError("bad_die_type")
    faces = _normalize_faces(die_type, payload.get("faces") or {})
    for asset_id in {value for value in faces.values() if value}:
        if find_asset(asset_id) is None:
            raise TextureError("bad_face_key")
    index = read_index()
    skin_id = str(payload.get("id") or f"dice-skin-{uuid.uuid4().hex}")
    skin = {
        "id": skin_id,
        "name": str(payload.get("name") or "").strip() or f"{die_type} 皮肤",
        "die_type": die_type,
        "faces": faces,
        "enabled": bool(payload.get("enabled", True)),
    }
    index["dice_skins"] = [item for item in index["dice_skins"] if item.get("id") != skin_id] + [skin]
    write_index(index)
    return skin


def delete_skin(skin_id: str) -> None:
    index = read_index()
    before = len(index["table_skins"]) + len(index["dice_skins"])
    index["table_skins"] = [item for item in index["table_skins"] if item.get("id") != skin_id]
    index["dice_skins"] = [item for item in index["dice_skins"] if item.get("id") != skin_id]
    if len(index["table_skins"]) + len(index["dice_skins"]) == before:
        raise TextureError("not_found", 404)
    write_index(index)


def library(include_disabled: bool = False) -> dict[str, Any]:
    index = read_index()
    assets = [item for item in index["assets"] if include_disabled or item.get("enabled", True)]
    table_skins = [item for item in index["table_skins"] if include_disabled or item.get("enabled", True)]
    dice_skins = [item for item in index["dice_skins"] if include_disabled or item.get("enabled", True)]
    return {"assets": assets, "table_skins": table_skins, "dice_skins": dice_skins}
