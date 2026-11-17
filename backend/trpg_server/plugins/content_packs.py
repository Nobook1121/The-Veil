"""插件内容数据包（只读、纯数据）的加载与校验容器。

核心不认识任何具体规则集的内容类型；数据包自己声明：

- ``pack.json``：包 id / 版本 / 规则集 / 内容版本 / 来源与许可 / 文件清单；
- 每个文件：一个 JSON 数组，或 ``{"entries": [...]}``；
- 每条内容：至少 ``id`` 与 ``name``，可选 ``locales`` 做显示名。

校验只做通用约束：路径不越界、文件大小/条目数/JSON 深度上限、ID 合法且
同类型唯一、来源与许可声明存在、每个条目带名称。数据包不执行任何代码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .contracts import REASON_CAPABILITY, PluginError
from .security import safe_plugin_path

PACK_MANIFEST_FILENAME = "pack.json"
CONTENT_PACK_CONTRACT_VERSION = 1

MAX_PACK_FILE_BYTES = 4 * 1024 * 1024
MAX_PACK_ENTRIES = 5000
MAX_PACK_JSON_DEPTH = 12
MAX_PACK_TEXT_CHARS = 40_000
MAX_PACKS_PER_PLUGIN = 8

_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_ENTRY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ContentPackError(PluginError):
    """数据包不合法；插件装载阶段按「能力失败」处理。"""

    reason_code = REASON_CAPABILITY


@dataclass(frozen=True)
class PackSource:
    system: str
    document: str
    license: str
    attribution: str
    edition: str = ""
    license_url: str = ""
    source_url: str = ""
    modifications: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "document": self.document,
            "edition": self.edition,
            "license": self.license,
            "license_url": self.license_url,
            "source_url": self.source_url,
            "attribution": self.attribution,
            "modifications": self.modifications,
        }


@dataclass(frozen=True)
class PackFile:
    path: str
    type: str


@dataclass
class ContentPack:
    """一个数据包的元数据与懒加载入口；条目只在首次访问时读盘。"""

    plugin_id: str
    pack_id: str
    pack_version: str
    ruleset_id: str
    content_version: str
    source: PackSource
    files: tuple[PackFile, ...]
    pack_dir: Path
    _cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _index: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict, repr=False)

    def types(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.type for item in self.files))

    def entries(self, type_name: str) -> list[dict[str, Any]]:
        key = str(type_name or "").strip().lower()
        if key in self._cache:
            return self._cache[key]
        merged: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for item in self.files:
            if item.type != key:
                continue
            for entry in _load_entries(self.pack_dir / item.path, item.path):
                entry_id = str(entry["id"])
                if entry_id in index:
                    raise ContentPackError(
                        f"数据包 {self.pack_id} 的 {key} 条目 id 重复：{entry_id}"
                    )
                index[entry_id] = entry
                merged.append(entry)
        self._cache[key] = merged
        self._index[key] = index
        return merged

    def get(self, type_name: str, entry_id: str) -> dict[str, Any] | None:
        key = str(type_name or "").strip().lower()
        if key not in self._index:
            self.entries(key)
        return self._index.get(key, {}).get(str(entry_id or ""))

    def search(self, type_name: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = str(query or "").strip().casefold()
        if not text:
            return []
        found: list[dict[str, Any]] = []
        for entry in self.entries(type_name):
            if text in _search_blob(entry):
                found.append(entry)
                if len(found) >= max(1, min(100, limit)):
                    break
        return found

    def meta(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "contract_version": CONTENT_PACK_CONTRACT_VERSION,
            "ruleset_id": self.ruleset_id,
            "content_version": self.content_version,
            "source": self.source.to_dict(),
            "types": list(self.types()),
        }


@dataclass
class ContentPackLibrary:
    """多个数据包的只读视图；调用方按 ``ruleset_id + type`` 取内容。"""

    packs: tuple[ContentPack, ...] = ()

    @classmethod
    def from_manifest(cls, manifest: Any) -> "ContentPackLibrary":
        capability = manifest.capability("content_packs")
        if not isinstance(capability, Mapping):
            raise ContentPackError("插件没有声明 capabilities.content_packs")
        dirs = capability.get("dirs") or []
        if not isinstance(dirs, list) or not dirs:
            raise ContentPackError("content_packs.dirs 必须是非空数组")
        if len(dirs) > MAX_PACKS_PER_PLUGIN:
            raise ContentPackError(f"content_packs.dirs 超过上限（{MAX_PACKS_PER_PLUGIN}）")
        packs: list[ContentPack] = []
        for relative in dirs:
            pack_dir = safe_plugin_path(manifest.plugin_dir, str(relative))
            if not pack_dir.is_dir():
                raise ContentPackError(f"数据包目录不存在：{relative}")
            packs.append(_load_pack_manifest(manifest.id, pack_dir))
        return cls(tuple(packs))

    @property
    def pack_ids(self) -> tuple[str, ...]:
        return tuple(pack.pack_id for pack in self.packs)

    def packs_for(self, ruleset_id: str) -> tuple[ContentPack, ...]:
        key = str(ruleset_id or "").strip().lower()
        if not key:
            return self.packs
        return tuple(pack for pack in self.packs if pack.ruleset_id == key)

    def types(self, ruleset_id: str = "") -> tuple[str, ...]:
        result: list[str] = []
        for pack in self.packs_for(ruleset_id):
            for type_name in pack.types():
                if type_name not in result:
                    result.append(type_name)
        return tuple(result)

    def entries(self, ruleset_id: str, type_name: str) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for pack in self.packs_for(ruleset_id):
            for entry in pack.entries(type_name):
                entry_id = str(entry["id"])
                if entry_id in index:
                    raise ContentPackError(f"规则集 {ruleset_id} 的 {type_name} 条目 id 在多个数据包中重复：{entry_id}")
                index[entry_id] = entry
                merged.append(entry)
        return merged

    def get(self, ruleset_id: str, type_name: str, entry_id: str) -> dict[str, Any] | None:
        for pack in self.packs_for(ruleset_id):
            found = pack.get(type_name, entry_id)
            if found is not None:
                return found
        return None

    def search(self, ruleset_id: str, type_name: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = str(query or "").strip().casefold()
        if not text:
            return []
        found: list[dict[str, Any]] = []
        for pack in self.packs_for(ruleset_id):
            for entry in pack.entries(type_name):
                if text in _search_blob(entry):
                    found.append(entry)
                    if len(found) >= max(1, min(100, limit)):
                        return found
        return found

    def sources(self, ruleset_id: str = "") -> list[dict[str, Any]]:
        return [pack.meta() for pack in self.packs_for(ruleset_id)]

    def attribution(self, ruleset_id: str = "") -> tuple[str, ...]:
        texts: list[str] = []
        for pack in self.packs_for(ruleset_id):
            text = pack.source.attribution.strip()
            if text and text not in texts:
                texts.append(text)
        return tuple(texts)


# ---------------------------------------------------------------- 内部加载


def _load_pack_manifest(plugin_id: str, pack_dir: Path) -> ContentPack:
    manifest_path = pack_dir / PACK_MANIFEST_FILENAME
    data = _read_json(manifest_path, PACK_MANIFEST_FILENAME)
    if not isinstance(data, Mapping):
        raise ContentPackError(f"{PACK_MANIFEST_FILENAME} 必须是对象")

    pack_id = _require_text(data, "pack_id")
    if not _PACK_ID_RE.match(pack_id):
        raise ContentPackError(f"pack_id 不合法：{pack_id}")
    pack_version = _require_text(data, "pack_version")
    ruleset_id = _require_text(data, "ruleset_id").lower()
    content_version = _require_text(data, "content_version")

    contract_version = data.get("contract_version", CONTENT_PACK_CONTRACT_VERSION)
    if contract_version != CONTENT_PACK_CONTRACT_VERSION:
        raise ContentPackError(
            f"数据包契约版本不受支持：{contract_version}（当前 {CONTENT_PACK_CONTRACT_VERSION}）"
        )

    source_raw = data.get("source")
    if not isinstance(source_raw, Mapping):
        raise ContentPackError("source 必须是对象")
    source = PackSource(
        system=_require_text(source_raw, "system", label="source.system"),
        document=_require_text(source_raw, "document", label="source.document"),
        license=_require_text(source_raw, "license", label="source.license"),
        attribution=_require_text(source_raw, "attribution", label="source.attribution"),
        edition=str(source_raw.get("edition") or "").strip(),
        license_url=str(source_raw.get("license_url") or "").strip(),
        source_url=str(source_raw.get("source_url") or "").strip(),
        modifications=str(source_raw.get("modifications") or "").strip(),
    )

    files_raw = data.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise ContentPackError("files 必须是非空数组")
    files: list[PackFile] = []
    seen_paths: set[str] = set()
    for item in files_raw:
        if not isinstance(item, Mapping):
            raise ContentPackError("files[] 必须是对象")
        relative = _normalize_pack_path(item.get("path"))
        if relative in seen_paths:
            raise ContentPackError(f"files 路径重复：{relative}")
        type_name = str(item.get("type") or "").strip().lower()
        if not _TYPE_RE.match(type_name):
            raise ContentPackError(f"files[].type 不合法：{type_name}")
        target = safe_plugin_path(pack_dir, relative)
        if not target.is_file():
            raise ContentPackError(f"数据文件不存在：{relative}")
        seen_paths.add(relative)
        files.append(PackFile(path=relative, type=type_name))

    return ContentPack(
        plugin_id=plugin_id,
        pack_id=pack_id,
        pack_version=pack_version,
        ruleset_id=ruleset_id,
        content_version=content_version,
        source=source,
        files=tuple(files),
        pack_dir=pack_dir,
    )


def _load_entries(path: Path, label: str) -> list[dict[str, Any]]:
    data = _read_json(path, label)
    if isinstance(data, Mapping):
        data = data.get("entries")
    if not isinstance(data, list):
        raise ContentPackError(f"{label} 必须是数组或 {{entries: [...]}}")
    if len(data) > MAX_PACK_ENTRIES:
        raise ContentPackError(f"{label} 条目数超过上限（{MAX_PACK_ENTRIES}）")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, Mapping):
            raise ContentPackError(f"{label} 的条目必须是对象")
        entry = dict(raw)
        entry_id = str(entry.get("id") or "").strip()
        if not _ENTRY_ID_RE.match(entry_id):
            raise ContentPackError(f"{label} 含不合法 id：{entry_id or '<empty>'}")
        if entry_id in seen:
            raise ContentPackError(f"{label} 含重复 id：{entry_id}")
        seen.add(entry_id)
        entry["id"] = entry_id
        if not _has_name(entry):
            raise ContentPackError(f"{label} 的条目 {entry_id} 缺少 name 或 locales")
        _check_strings(entry, label)
        result.append(entry)
    return result


def _read_json(path: Path, label: str) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContentPackError(f"{label} 无法读取") from exc
    if size > MAX_PACK_FILE_BYTES:
        raise ContentPackError(f"{label} 超过大小上限（{MAX_PACK_FILE_BYTES // 1024} KB）")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContentPackError(f"{label} 无法按 UTF-8 读取") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ContentPackError(f"{label} 不是合法 JSON") from exc
    _check_depth(data, label)
    return data


def _check_depth(value: Any, label: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_PACK_JSON_DEPTH:
            raise ContentPackError(f"{label} 的 JSON 嵌套超过上限（{MAX_PACK_JSON_DEPTH}）")
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _check_strings(value: Any, label: str) -> None:
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if len(current) > MAX_PACK_TEXT_CHARS:
                raise ContentPackError(f"{label} 含超过 {MAX_PACK_TEXT_CHARS} 字符的文本")
        elif isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _normalize_pack_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ContentPackError(f"files[].path 必须是包内相对路径：{text}")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ContentPackError(f"files[].path 不允许包含 . 或 ..：{text}")
    if not text.lower().endswith(".json"):
        raise ContentPackError(f"files[].path 必须是 .json：{text}")
    return "/".join(parts)


def _require_text(data: Mapping[str, Any], key: str, *, label: str = "") -> str:
    name = label or key
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContentPackError(f"{name} 不能为空")
    return value.strip()


def _has_name(entry: Mapping[str, Any]) -> bool:
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return True
    locals_raw = entry.get("locales")
    if isinstance(locals_raw, Mapping):
        for value in locals_raw.values():
            if isinstance(value, Mapping) and isinstance(value.get("name"), str) and value["name"].strip():
                return True
    return False


def _search_blob(entry: Mapping[str, Any]) -> str:
    parts: list[str] = [str(entry.get("id") or ""), str(entry.get("name") or ""), str(entry.get("name_en") or "")]
    for alias in entry.get("aliases") or ():
        if isinstance(alias, str):
            parts.append(alias)
    locals_raw = entry.get("locales")
    if isinstance(locals_raw, Mapping):
        for value in locals_raw.values():
            if isinstance(value, Mapping):
                parts.extend(str(item) for item in value.values() if isinstance(item, str))
    return " ".join(parts).casefold()


__all__ = [
    "CONTENT_PACK_CONTRACT_VERSION",
    "MAX_PACK_ENTRIES",
    "MAX_PACK_FILE_BYTES",
    "MAX_PACK_JSON_DEPTH",
    "MAX_PACK_TEXT_CHARS",
    "PACK_MANIFEST_FILENAME",
    "ContentPack",
    "ContentPackError",
    "ContentPackLibrary",
    "PackFile",
    "PackSource",
]
