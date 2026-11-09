"""角色卡导入 / 导出共用的结构定义、校验与编解码。

导出与导入使用同一份 ``CARD_FIELD_SPECS``，避免两边字段漂移：

- 导出：按 spec 从运行时角色卡取值，附带头像（data URI）与自定义武器库。
- 导入：按同一份 spec 逐字段校验类型、补齐可选默认值，拒绝非法数据。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import time
import zlib
from dataclasses import dataclass
from typing import Any

from trpg_server import character_service, weapon_catalog
from trpg_server.ruleset.registry import builtin_registry

# 单次导入的硬上限，避免超大文件与压缩炸弹。
MAX_IMPORT_TEXT_CHARS = 12 * 1024 * 1024
MAX_IMPORT_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_IMPORT_JSON_BYTES = 12 * 1024 * 1024
MAX_IMPORT_REQUEST_BYTES = 16 * 1024 * 1024
MAX_AVATAR_BYTES = 4 * 1024 * 1024

ATTRIBUTE_KEYS = ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")
STATUS_KEYS = (
    "majorWound",
    "unconscious",
    "dead",
    "indefiniteInsanity",
    "permanentInsanity",
    "temporaryInsanity",
)
BACKGROUND_KEYS = (
    "appearance",
    "ideology",
    "significantPeople",
    "meaningfulLocations",
    "treasuredPossessions",
    "traits",
    "injuriesScars",
    "phobiasManias",
    "story",
    "arcaneTomes",
    "spells",
    "encounters",
)
_SKILL_STRING_KEYS = ("id", "skillKey", "specialtyKey", "name", "category")
_SKILL_INT_KEYS = ("base", "value", "occupationPoints", "interestPoints", "growthPoints")
_SKILL_BOOL_KEYS = ("checked", "occupation", "isProfessional", "customBase")
_WEAPON_STRING_KEYS = (
    "name",
    "skill",
    "skillKey",
    "specialtyKey",
    "damage",
    "range",
    "attacks",
    "ammo",
    "malfunction",
    "weight",
    "note",
)
_AVATAR_DATA_PATTERN = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$")


class CharacterCardIOError(Exception):
    """携带稳定错误码与 HTTP 状态的可展示异常。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    required: bool = False


def _spec(name, kind, default=None, minimum=None, maximum=None, required=False):
    return FieldSpec(name, kind, default, minimum, maximum, required)


# 运行时角色卡的完整字段清单；导出按此清单取值，导入按同一清单校验。
CARD_FIELD_SPECS: tuple[FieldSpec, ...] = (
    _spec("id", "str", ""),
    _spec("rulesetId", "str", "coc7"),
    _spec("public_id", "str", ""),
    _spec("publisher_id", "any", None),
    _spec("publisher_name", "str", ""),
    _spec("avatar", "str", ""),
    _spec("name", "str", "", required=True),
    _spec("playerId", "str", ""),
    _spec("playerName", "str", ""),
    _spec("era", "str", "1920s"),
    _spec("gender", "str", ""),
    _spec("age", "int", 25, 0, 999),
    _spec("occupationName", "str", ""),
    _spec("residence", "str", ""),
    _spec("birthplace", "str", ""),
    _spec("attributes", "dict", {}, required=True),
    _spec("maxHp", "int", 0, 0, 999),
    _spec("currentHp", "int", 0, 0, 999),
    _spec("maxSan", "int", 99, 0, 999),
    _spec("initialSan", "int", 0, 0, 999),
    _spec("currentSan", "int", 0, 0, 999),
    _spec("maxMp", "int", 0, 0, 999),
    _spec("currentMp", "int", 0, 0, 999),
    _spec("magicPoints", "int", 0, 0, 999),
    _spec("damageBonus", "str", ""),
    _spec("build", "int", 0, -2, 99),
    _spec("mov", "int", 0, 0, 99),
    _spec("armor", "int", 0, 0, 99),
    _spec("status", "dict", {}),
    _spec("skills", "list", [], required=True),
    _spec("weapons", "list", [], required=True),
    _spec("equipment", "list", [], required=True),
    _spec("assets", "dict", {}),
    _spec("background", "dict", {}),
    _spec("relationships", "list", []),
    _spec("experiencedScenarios", "list", []),
    _spec("createdAt", "str", ""),
    _spec("updatedAt", "str", ""),
)

# 导出时额外携带的字段（导入同样校验）。
EXPORT_EXTRA_FIELD_SPECS: tuple[FieldSpec, ...] = (
    _spec("customWeapons", "list", []),
    _spec("avatarData", "str", ""),
)

_AVATAR_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# -- 导出 -------------------------------------------------------------------


def _avatar_data_uri(character_id: str) -> str:
    """把角色卡目录里的头像读成 data URI；没有头像或不可读时返回空串。"""
    if not character_id:
        return ""
    directory = character_service.character_dir(character_id)
    if not directory.exists():
        return ""
    for path in sorted(directory.glob("avatar.*")):
        mime = _AVATAR_MIME_BY_SUFFIX.get(path.suffix.lower())
        if not mime:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return ""


def build_export_payload(character: dict[str, Any]) -> dict[str, Any]:
    """返回可直接写入 JSON 文件的完整角色卡数据。"""
    source = character or {}
    payload: dict[str, Any] = {}
    for spec in CARD_FIELD_SPECS:
        value = source.get(spec.name)
        payload[spec.name] = copy.deepcopy(spec.default) if value is None else value
    for spec in EXPORT_EXTRA_FIELD_SPECS:
        payload[spec.name] = copy.deepcopy(spec.default)
    character_id = str(payload.get("id") or "")
    payload["customWeapons"] = weapon_catalog.read_custom_weapons(
        character_service.character_dir(character_id)
    )
    payload["avatarData"] = _avatar_data_uri(character_id)
    return payload


def dump_export_json(payload: dict[str, Any]) -> str:
    # 统一使用这一份文本：JSON 文件与分享文本串解出来的内容完全一致。
    return json.dumps(payload, ensure_ascii=False, indent=2)


def encode_export_text(json_text: str) -> str:
    """对 JSON 文本做 zlib 压缩后 base64 编码，返回裸串（无外壳、无前缀）。"""
    compressed = zlib.compress(json_text.encode("utf-8"), 9)
    return base64.b64encode(compressed).decode("ascii")


def export_filename(character: dict[str, Any]) -> str:
    name = _FILENAME_UNSAFE.sub("_", str(character.get("name") or "")).strip(" ._")
    character_id = _FILENAME_UNSAFE.sub("_", str(character.get("id") or "")).strip(" ._")
    stem = "-".join(part for part in (name[:40], character_id[:80]) if part) or "character"
    return f"{stem}.json"


def build_export_bundle(character: dict[str, Any]) -> dict[str, str]:
    """一次性生成导出所需的三部分：文件名、JSON 文本、分享文本串。"""
    payload = build_export_payload(character)
    json_text = dump_export_json(payload)
    return {
        "filename": export_filename(character),
        "jsonText": json_text,
        "encoded": encode_export_text(json_text),
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# -- 导入：解压 / 解析 -------------------------------------------------------


def _error(message: str, code: str = "invalid_import_payload") -> CharacterCardIOError:
    return CharacterCardIOError(code, message, 400)


def decode_share_text(text: str) -> str:
    """把裸的 base64(zlib(json)) 分享串解回 JSON 文本，带完整体积与截断校验。"""
    compact = "".join(str(text or "").split())
    if not compact:
        raise _error("分享文本串为空。", "empty_import_payload")
    if len(compact) > MAX_IMPORT_TEXT_CHARS:
        raise CharacterCardIOError("import_too_large", "分享文本串过长，超过导入上限。", 413)
    try:
        compressed = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise _error("分享文本串不是合法的 base64 数据。", "invalid_share_text")
    if len(compressed) > MAX_IMPORT_COMPRESSED_BYTES:
        raise CharacterCardIOError("import_too_large", "分享文本串的压缩数据过大。", 413)
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed, MAX_IMPORT_JSON_BYTES + 1)
    except zlib.error:
        raise _error("分享文本串不是有效的 zlib 压缩数据。", "invalid_share_text")
    if len(raw) > MAX_IMPORT_JSON_BYTES or decompressor.unconsumed_tail:
        raise CharacterCardIOError("import_too_large", "分享文本串解压后超过大小上限。", 413)
    try:
        raw += decompressor.flush()
    except zlib.error:
        raise _error("分享文本串不是有效的 zlib 压缩数据。", "invalid_share_text")
    if len(raw) > MAX_IMPORT_JSON_BYTES:
        raise CharacterCardIOError("import_too_large", "分享文本串解压后超过大小上限。", 413)
    if not decompressor.eof:
        raise _error("分享文本串不完整（zlib 数据被截断）。", "truncated_share_text")
    if decompressor.unused_data.strip():
        raise _error("分享文本串包含多余的尾部数据。", "invalid_share_text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _error("分享文本串解压后不是 UTF-8 文本。", "invalid_share_text")


def parse_import_text(text: str, mode: str = "auto") -> Any:
    """解析导入文本：完整 JSON，或 zlib + base64 分享串。返回原始 JSON 对象。"""
    raw = str(text or "").lstrip("\ufeff").strip()
    if not raw:
        raise _error("导入内容为空。", "empty_import_payload")
    if len(raw) > MAX_IMPORT_TEXT_CHARS:
        raise CharacterCardIOError("import_too_large", "导入内容过长，超过导入上限。", 413)
    normalized_mode = str(mode or "auto").strip().lower()
    if normalized_mode not in {"auto", "json", "text"}:
        raise _error("不支持的导入模式。")
    if normalized_mode == "text" or (normalized_mode == "auto" and raw[:1] not in "{["):
        json_text = decode_share_text(raw)
    else:
        json_text = raw
    if len(json_text.encode("utf-8")) > MAX_IMPORT_JSON_BYTES:
        raise CharacterCardIOError("import_too_large", "角色卡 JSON 超过大小上限。", 413)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise _error(f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}", "invalid_json")


# -- 导入：结构校验 ---------------------------------------------------------


def _as_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"字段 {path} 必须是整数。", "invalid_field_type")
    number = int(value)
    if number != value:
        raise _error(f"字段 {path} 必须是整数。", "invalid_field_type")
    if number < minimum or number > maximum:
        raise _error(f"字段 {path} 超出允许范围（{minimum}-{maximum}）。", "invalid_field_value")
    return number


def _as_text(value: Any, path: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _error(f"字段 {path} 必须是字符串。", "invalid_field_type")
    return value


def _as_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"字段 {path} 必须是对象。", "invalid_field_type")
    return value


def _as_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(f"字段 {path} 必须是数组。", "invalid_field_type")
    return value


def _optional_int(mapping: dict[str, Any], key: str, path: str, minimum: int, maximum: int, default: int = 0) -> int:
    if key not in mapping or mapping.get(key) is None:
        return default
    return _as_int(mapping[key], f"{path}.{key}", minimum, maximum)


def _validate_attributes(raw: Any, age: int) -> dict[str, int]:
    attributes = _as_dict(raw, "attributes")
    normalized: dict[str, int] = {}
    for key in ATTRIBUTE_KEYS:
        if key not in attributes:
            raise _error(f"缺少必填字段 attributes.{key}。", "missing_field")
        normalized[key] = _as_int(attributes[key], f"attributes.{key}", 0, 999)
    normalized["AGE"] = _optional_int(attributes, "AGE", "attributes", 0, 999, default=age)
    return normalized


def _validate_status(raw: Any) -> dict[str, bool]:
    status = _as_dict(raw, "status")
    normalized: dict[str, bool] = {}
    for key in STATUS_KEYS:
        value = status.get(key)
        if value is None:
            normalized[key] = False
        elif isinstance(value, bool):
            normalized[key] = value
        else:
            raise _error(f"字段 status.{key} 必须是布尔值。", "invalid_field_type")
    return normalized


def _validate_skills(raw: Any) -> list[dict[str, Any]]:
    items = _as_list(raw, "skills")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"skills[{index}]"
        skill = dict(_as_dict(item, path))
        if not str(skill.get("name") or "").strip():
            raise _error(f"字段 {path}.name 不能为空。", "missing_field")
        for key in _SKILL_STRING_KEYS:
            if key in skill and skill[key] is not None:
                skill[key] = _as_text(skill[key], f"{path}.{key}")
        for key in _SKILL_INT_KEYS:
            if key in skill and skill[key] is not None:
                skill[key] = _as_int(skill[key], f"{path}.{key}", 0, 999)
        for key in _SKILL_BOOL_KEYS:
            if key in skill and skill[key] is not None and not isinstance(skill[key], bool):
                raise _error(f"字段 {path}.{key} 必须是布尔值。", "invalid_field_type")
        normalized.append(skill)
    return normalized


def _validate_weapons(raw: Any) -> list[dict[str, Any]]:
    items = _as_list(raw, "weapons")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"weapons[{index}]"
        weapon = dict(_as_dict(item, path))
        for key in _WEAPON_STRING_KEYS:
            if key in weapon and weapon[key] is not None:
                weapon[key] = _as_text(weapon[key], f"{path}.{key}")
        if "impale" in weapon and weapon["impale"] is not None and not isinstance(weapon["impale"], bool):
            raise _error(f"字段 {path}.impale 必须是布尔值。", "invalid_field_type")
        normalized.append(weapon)
    return normalized


def _validate_equipment(raw: Any) -> list[dict[str, Any]]:
    items = _as_list(raw, "equipment")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"equipment[{index}]"
        entry = dict(_as_dict(item, path))
        if not str(entry.get("name") or "").strip():
            raise _error(f"字段 {path}.name 不能为空。", "missing_field")
        entry["name"] = _as_text(entry.get("name"), f"{path}.name")
        entry["quantity"] = _optional_int(entry, "quantity", path, 1, 9999, default=1)
        entry["notes"] = _as_text(entry.get("notes"), f"{path}.notes")
        if "weight" in entry and entry["weight"] is not None:
            if isinstance(entry["weight"], bool) or not isinstance(entry["weight"], (int, float)):
                raise _error(f"字段 {path}.weight 必须是数字。", "invalid_field_type")
        normalized.append(entry)
    return normalized


def _validate_assets(raw: Any) -> dict[str, Any]:
    assets = _as_dict(raw, "assets")
    return {
        "cash": _optional_int(assets, "cash", "assets", 0, 999999),
        "spendingLevel": _optional_int(assets, "spendingLevel", "assets", 0, 999999),
        "assetsText": _as_text(assets.get("assetsText"), "assets.assetsText"),
    }


def _validate_background(raw: Any) -> dict[str, str]:
    background = _as_dict(raw, "background")
    normalized: dict[str, str] = {}
    for key in BACKGROUND_KEYS:
        normalized[key] = _as_text(background.get(key), f"background.{key}")
    return normalized


def _validate_relationships(raw: Any) -> list[dict[str, str]]:
    items = _as_list(raw, "relationships")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items):
        path = f"relationships[{index}]"
        entry = _as_dict(item, path)
        normalized.append(
            {
                "name": _as_text(entry.get("name"), f"{path}.name"),
                "description": _as_text(entry.get("description"), f"{path}.description"),
                "player": _as_text(entry.get("player"), f"{path}.player"),
            }
        )
    return normalized


def _validate_experienced_scenarios(raw: Any) -> list[dict[str, str]]:
    items = _as_list(raw, "experiencedScenarios")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items):
        path = f"experiencedScenarios[{index}]"
        entry = _as_dict(item, path)
        normalized.append(
            {
                "name": _as_text(entry.get("name"), f"{path}.name"),
                "experience": _as_text(entry.get("experience"), f"{path}.experience"),
                "sanChange": _as_text(entry.get("sanChange"), f"{path}.sanChange"),
                "otherChanges": _as_text(entry.get("otherChanges"), f"{path}.otherChanges"),
            }
        )
    return normalized


def _validate_avatar_data(raw: Any) -> str:
    value = _as_text(raw, "avatarData").strip()
    if not value:
        return ""
    match = _AVATAR_DATA_PATTERN.match(value)
    if not match:
        raise _error("字段 avatarData 必须是 data:image/...;base64 格式的头像数据。", "invalid_avatar")
    try:
        payload = "".join(match.group(2).split())
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise _error("字段 avatarData 的头像数据损坏。", "invalid_avatar")
    if not data:
        raise _error("字段 avatarData 的头像内容为空。", "invalid_avatar")
    if len(data) > MAX_AVATAR_BYTES:
        raise CharacterCardIOError("import_too_large", "头像文件超过 4MB 上限。", 413)
    return value


def _validate_custom_weapons(raw: Any) -> list[dict[str, Any]]:
    items = _as_list(raw, "customWeapons")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"customWeapons[{index}]"
        weapon = dict(_as_dict(item, path))
        if not str(weapon.get("name") or "").strip():
            raise _error(f"字段 {path}.name 不能为空。", "missing_field")
        for key in ("name", "id", "category", "skillKey", "specialtyKey", "damage", "range", "attacks", "ammo", "malfunction", "weight", "note", "price"):
            if key in weapon and weapon[key] is not None:
                weapon[key] = _as_text(weapon[key], f"{path}.{key}")
        skill = weapon.get("skill")
        if skill is not None and not isinstance(skill, str):
            skill_data = dict(_as_dict(skill, f"{path}.skill"))
            for key in ("skillKey", "specialtyKey", "label"):
                if key in skill_data and skill_data[key] is not None:
                    skill_data[key] = _as_text(skill_data[key], f"{path}.skill.{key}")
            weapon["skill"] = skill_data
        if "impale" in weapon and weapon["impale"] is not None and not isinstance(weapon["impale"], bool):
            raise _error(f"字段 {path}.impale 必须是布尔值。", "invalid_field_type")
        if "eras" in weapon and weapon["eras"] is not None:
            eras = _as_list(weapon["eras"], f"{path}.eras")
            weapon["eras"] = [_as_text(era, f"{path}.eras") for era in eras]
        if "categoryOrder" in weapon and weapon["categoryOrder"] is not None:
            weapon["categoryOrder"] = _as_int(weapon["categoryOrder"], f"{path}.categoryOrder", 0, 999)
        normalized.append(weapon)
    return normalized


def _supported_ruleset_ids() -> list[str]:
    return builtin_registry().ids()


def merge_sheet_fields(data: Any) -> Any:
    """把 ``sheet`` 中的字段合并到顶层；顶层已有值时以顶层为准。"""
    if not isinstance(data, dict):
        return data
    merged = dict(data)
    sheet = merged.get("sheet")
    if isinstance(sheet, dict):
        for key, value in sheet.items():
            merged.setdefault(key, value)
    return merged


def validate_import_payload(data: Any) -> dict[str, Any]:
    """校验并规范化导入数据；任何非法字段都会抛错，不做部分写入。"""
    if isinstance(data, list):
        raise _error("一次只能导入一张角色卡；请拆分数组后重试。", "invalid_character_shape")
    if not isinstance(data, dict):
        raise _error("角色卡 JSON 顶层必须是对象。", "invalid_character_shape")
    if data.get("sheet") is not None and not isinstance(data.get("sheet"), dict):
        raise _error("字段 sheet 必须是对象。", "invalid_field_type")

    merged = merge_sheet_fields(data)

    ruleset_id = str(merged.get("rulesetId") or merged.get("ruleset_id") or "coc7").strip().lower()
    supported = _supported_ruleset_ids()
    if ruleset_id not in supported:
        raise _error(
            f"不支持规则集「{ruleset_id}」；当前仅支持：{', '.join(supported) or '无'}。",
            "unsupported_ruleset",
        )

    card: dict[str, Any] = {}
    for spec in CARD_FIELD_SPECS:
        present = spec.name in merged and merged.get(spec.name) is not None
        if not present:
            if spec.required:
                raise _error(f"缺少必填字段 {spec.name}。", "missing_field")
            card[spec.name] = copy.deepcopy(spec.default)
            continue
        value = merged[spec.name]
        if spec.kind == "str":
            card[spec.name] = _as_text(value, spec.name)
        elif spec.kind == "int":
            card[spec.name] = _as_int(value, spec.name, spec.minimum or 0, spec.maximum or 999)
        elif spec.kind == "dict":
            card[spec.name] = _as_dict(value, spec.name)
        elif spec.kind == "list":
            card[spec.name] = _as_list(value, spec.name)
        else:
            card[spec.name] = value

    name = str(card.get("name") or "").strip()
    if not name:
        raise _error("字段 name 不能为空。", "missing_field")
    card["name"] = name[:80]
    card["rulesetId"] = ruleset_id
    card["attributes"] = _validate_attributes(merged.get("attributes"), int(card.get("age") or 25))
    card["status"] = _validate_status(merged.get("status") or {})
    card["skills"] = _validate_skills(merged.get("skills") or [])
    card["weapons"] = _validate_weapons(merged.get("weapons") or [])
    card["equipment"] = _validate_equipment(merged.get("equipment") or [])
    card["assets"] = _validate_assets(merged.get("assets") or {})
    card["background"] = _validate_background(merged.get("background") or {})
    card["relationships"] = _validate_relationships(merged.get("relationships") or [])
    card["experiencedScenarios"] = _validate_experienced_scenarios(merged.get("experiencedScenarios") or [])
    card["customWeapons"] = _validate_custom_weapons(merged.get("customWeapons") or [])
    card["avatarData"] = _validate_avatar_data(merged.get("avatarData"))
    return card


# -- 导入：命名与落盘辅助 ---------------------------------------------------


def unique_character_name(desired: str, existing_names: set[str]) -> str:
    base = (desired or "").strip()[:80] or "未命名角色卡"
    if base not in existing_names:
        return base
    index = 2
    while True:
        suffix = f"（{index}）"
        candidate = f"{base[: max(1, 80 - len(suffix))]}{suffix}"
        if candidate not in existing_names:
            return candidate
        index += 1


def new_unique_character_id() -> str:
    """生成不会覆盖已有文件的新角色卡 ID。"""
    base = character_service.new_character_id(None)
    candidate = base
    index = 2
    while (
        character_service.character_json_path(candidate).exists()
        or character_service.legacy_character_path(candidate).exists()
    ):
        candidate = f"{base}-{index}"
        index += 1
    return candidate
