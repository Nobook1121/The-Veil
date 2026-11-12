"""插件清单（``plugin.json``）的解析与校验。

清单是**纯数据**：解析阶段只做结构校验，不 import 任何插件代码、不触碰网络。
``docs/schemas/plugin-manifest.schema.json`` 是同一份规则的 JSON Schema 视图，
供编辑器补全与第三方开发者参考；真正的准入以本模块实现为准
（``tests/test_plugins.py`` 会断言两者一致）。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    CAPABILITY_NAMES,
    MANIFEST_FILENAME,
    MAX_PLUGIN_MANIFEST_BYTES,
    PLUGIN_API_VERSION,
    PLUGIN_ID_RE,
    PLUGIN_MANIFEST_VERSION,
    RESERVED_PLUGIN_IDS,
    VERSION_RE,
    ManifestError,
    PluginManifest,
)
from .security import read_text_limited

_FRONTEND_ASSET_TYPES = ("script", "style", "image", "font")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,200}$")
_MODULE_SUFFIXES = (".py",)
_VERSION_TOKEN_RE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*v?(\d+\.\d+\.\d+)$")


# ---------------------------------------------------------------- 版本区间


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(str(text or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def satisfies_version(version: str, spec: str) -> bool:
    """``spec`` 形如 ``">=1.0.0 <2.0.0"``；空串表示不限制。"""
    current = parse_version(version)
    if current is None:
        return False
    spec = str(spec or "").strip()
    if not spec:
        return True
    for token in re.split(r"[\s,]+", spec):
        if not token:
            continue
        match = _VERSION_TOKEN_RE.match(token)
        if not match:
            return False
        operator = match.group(1) or "=="
        target = parse_version(match.group(2))
        if target is None:
            return False
        if operator == ">=" and not current >= target:
            return False
        if operator == "<=" and not current <= target:
            return False
        if operator == ">" and not current > target:
            return False
        if operator == "<" and not current < target:
            return False
        if operator == "==" and not current == target:
            return False
        if operator == "!=" and current == target:
            return False
    return True


# ---------------------------------------------------------------- 字段工具


def _require_str(data: Mapping[str, Any], key: str, *, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ManifestError(f"清单字段 {key} 必须是字符串")
    return value.strip()


def _normalize_rel_path(value: Any, label: str) -> str:
    """归一化插件内的相对路径；拒绝绝对路径、盘符与 ``..``。"""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise ManifestError(f"{label} 不能为空")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ManifestError(f"{label} 必须是插件内的相对路径：{text}")
    if not _REL_PATH_RE.match(text):
        raise ManifestError(f"{label} 含不安全字符：{text}")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ManifestError(f"{label} 不允许包含 . 或 ..：{text}")
    return "/".join(parts)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} 必须是对象")
    return value


def _require_str_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ManifestError(f"{label} 必须是非空字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise ManifestError(f"{label} 不能包含空字符串")
        result.append(text)
    return result


# ---------------------------------------------------------------- 清单解析


def load_manifest(plugin_dir: str | Path, *, expected_id: str | None = None) -> PluginManifest:
    """读取并校验 ``<plugin_dir>/plugin.json``。"""
    directory = Path(plugin_dir)
    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        raise ManifestError("plugin.json 不允许是符号链接")
    if not manifest_path.is_file():
        raise ManifestError(f"缺少 {MANIFEST_FILENAME}")
    text = read_text_limited(manifest_path, MAX_PLUGIN_MANIFEST_BYTES, MANIFEST_FILENAME)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"{MANIFEST_FILENAME} 不是合法 JSON") from exc
    return parse_manifest(data, directory, expected_id=expected_id)


def parse_manifest(
    data: Any,
    plugin_dir: str | Path,
    *,
    expected_id: str | None = None,
) -> PluginManifest:
    """把已读入的清单对象校验成 :class:`PluginManifest`。"""
    if not isinstance(data, Mapping):
        raise ManifestError("清单必须是 JSON 对象")

    manifest_version = data.get("schema_version", PLUGIN_MANIFEST_VERSION)
    if manifest_version != PLUGIN_MANIFEST_VERSION:
        raise ManifestError(
            f"schema_version 不受支持：{manifest_version}（当前 {PLUGIN_MANIFEST_VERSION}）"
        )

    plugin_id = _require_str(data, "id")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ManifestError("id 只能用小写字母、数字与 -，以字母开头，2–64 字符")
    if plugin_id in RESERVED_PLUGIN_IDS:
        raise ManifestError(f"id 与核心 Agent 工具命名空间冲突：{plugin_id}")
    directory = Path(plugin_dir)
    if expected_id is not None and plugin_id != expected_id:
        raise ManifestError(f"清单 id 与目录名不一致：{plugin_id} != {expected_id}")

    version = _require_str(data, "version")
    if parse_version(version) is None:
        raise ManifestError("version 必须是语义化版本，例如 1.0.0")

    api_version = data.get("api_version", PLUGIN_API_VERSION)
    if not isinstance(api_version, int) or isinstance(api_version, bool):
        raise ManifestError("api_version 必须是整数")

    requires = data.get("requires") or {}
    requires = _require_mapping(requires, "requires")
    requires_app = _require_str(requires, "app")
    raw_requires_plugins = requires.get("plugins") or {}
    raw_requires_plugins = _require_mapping(raw_requires_plugins, "requires.plugins")
    requires_plugins: dict[str, str] = {}
    for key, value in raw_requires_plugins.items():
        dep_id = str(key or "").strip()
        if not PLUGIN_ID_RE.match(dep_id):
            raise ManifestError(f"requires.plugins 里的插件 id 不合法：{dep_id}")
        requires_plugins[dep_id] = str(value or "").strip()

    dependencies = _optional_str_list(data.get("dependencies"), "dependencies")
    conflicts = _optional_str_list(data.get("conflicts"), "conflicts")

    capabilities = _parse_capabilities(_require_mapping(data.get("capabilities") or {}, "capabilities"), plugin_id)

    code = data.get("code") or {}
    code = _require_mapping(code, "code")
    code_entry = ""
    if code:
        code_entry = _normalize_rel_path(code.get("entry"), "code.entry")
        if not code_entry.endswith(_MODULE_SUFFIXES):
            raise ManifestError("code.entry 必须是 .py 文件")

    if capabilities and not code_entry:
        needs_code = any(
            name in capabilities and name in {"rulesets", "agent_tools", "http_blueprints"}
            for name in capabilities
        )
        if needs_code:
            raise ManifestError("声明了需要代码的能力（rulesets/agent_tools/http_blueprints），必须同时给出 code.entry")

    return PluginManifest(
        id=plugin_id,
        name=_require_str(data, "name") or plugin_id,
        version=version,
        description=_require_str(data, "description"),
        author=_require_str(data, "author"),
        manifest_version=PLUGIN_MANIFEST_VERSION,
        api_version=api_version,
        requires_app=requires_app,
        requires_plugins=requires_plugins,
        dependencies=tuple(dict.fromkeys(dependencies)),
        conflicts=tuple(dict.fromkeys(conflicts)),
        capabilities=capabilities,
        code_entry=code_entry or None,
        plugin_dir=directory,
    )


def _optional_str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"{label} 必须是字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not PLUGIN_ID_RE.match(text):
            raise ManifestError(f"{label} 里的插件 id 不合法：{text}")
        result.append(text)
    return result


def _parse_capabilities(raw: Mapping[str, Any], plugin_id: str) -> dict[str, Any]:
    unknown = [str(key) for key in raw if str(key) not in CAPABILITY_NAMES]
    if unknown:
        raise ManifestError("未知能力：" + ", ".join(sorted(unknown)))

    result: dict[str, Any] = {}

    if "prop_templates" in raw:
        section = _require_mapping(raw["prop_templates"], "capabilities.prop_templates")
        dirs = section.get("dirs", ["templates"])
        if not isinstance(dirs, list) or not dirs:
            raise ManifestError("capabilities.prop_templates.dirs 必须是非空数组")
        result["prop_templates"] = {
            "dirs": [_normalize_rel_path(item, "prop_templates.dirs") for item in dirs]
        }

    if "rulesets" in raw:
        section = _require_mapping(raw["rulesets"], "capabilities.rulesets")
        module = _normalize_rel_path(section.get("module"), "rulesets.module")
        if not module.endswith(_MODULE_SUFFIXES):
            raise ManifestError("capabilities.rulesets.module 必须是 .py 文件")
        ids = _optional_str_list(section.get("ids"), "capabilities.rulesets.ids")
        result["rulesets"] = {"module": module, "ids": ids}

    if "agent_tools" in raw:
        section = _require_mapping(raw["agent_tools"], "capabilities.agent_tools")
        module = _normalize_rel_path(section.get("module"), "agent_tools.module")
        if not module.endswith(_MODULE_SUFFIXES):
            raise ManifestError("capabilities.agent_tools.module 必须是 .py 文件")
        tools = _require_str_list(section.get("tools"), "capabilities.agent_tools.tools")
        for tool in tools:
            if not tool.startswith(f"{plugin_id}."):
                raise ManifestError(f"Agent 工具名必须以插件 id 为命名空间：{plugin_id}.<name>")
        result["agent_tools"] = {"module": module, "tools": tools}

    if "http_blueprints" in raw:
        section = _require_mapping(raw["http_blueprints"], "capabilities.http_blueprints")
        module = _normalize_rel_path(section.get("module"), "http_blueprints.module")
        if not module.endswith(_MODULE_SUFFIXES):
            raise ManifestError("capabilities.http_blueprints.module 必须是 .py 文件")
        blueprints = _require_str_list(section.get("blueprints"), "capabilities.http_blueprints.blueprints")
        result["http_blueprints"] = {"module": module, "blueprints": blueprints}

    if "settings_sections" in raw:
        sections = raw["settings_sections"]
        if not isinstance(sections, list) or not sections:
            raise ManifestError("capabilities.settings_sections 必须是非空数组")
        normalized_sections: list[dict[str, Any]] = []
        for item in sections:
            section = _require_mapping(item, "settings_sections[]")
            key = _require_str(section, "key")
            if not re.match(r"^[a-z][a-z0-9_]{1,31}$", key):
                raise ManifestError(f"设置段 key 不合法：{key}")
            fields = section.get("fields") or []
            if not isinstance(fields, list):
                raise ManifestError("settings_sections[].fields 必须是数组")
            normalized_sections.append(
                {
                    "key": key,
                    "title": _require_str(section, "title") or key,
                    "fields": [dict(item) for item in fields if isinstance(item, Mapping)],
                }
            )
        result["settings_sections"] = normalized_sections

    if "frontend_assets" in raw:
        assets = raw["frontend_assets"]
        if not isinstance(assets, list) or not assets:
            raise ManifestError("capabilities.frontend_assets 必须是非空数组")
        normalized_assets: list[dict[str, str]] = []
        for item in assets:
            asset = _require_mapping(item, "frontend_assets[]")
            path = _normalize_rel_path(asset.get("path"), "frontend_assets[].path")
            kind = str(asset.get("type") or "script").strip().lower()
            if kind not in _FRONTEND_ASSET_TYPES:
                raise ManifestError(f"frontend_assets[].type 不支持：{kind}")
            normalized_assets.append({"path": path, "type": kind})
        result["frontend_assets"] = normalized_assets

    return result


# ---------------------------------------------------------------- JSON Schema


def manifest_json_schema() -> dict[str, Any]:
    """清单的 JSON Schema（draft-07）视图，与上面的校验实现保持同步。"""
    relative_path = {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": r"^[A-Za-z0-9._\-/]+$",
        "description": "插件内的相对路径；不允许绝对路径、盘符或 ..",
    }
    plugin_id = {
        "type": "string",
        "pattern": PLUGIN_ID_RE.pattern,
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://the-veil.local/schemas/plugin-manifest.schema.json",
        "title": "The Veil 插件清单",
        "type": "object",
        "additionalProperties": True,
        "required": ["id", "version", "name"],
        "properties": {
            "schema_version": {"const": PLUGIN_MANIFEST_VERSION},
            "id": plugin_id,
            "name": {"type": "string"},
            "version": {
                "type": "string",
                "pattern": VERSION_RE.pattern,
            },
            "description": {"type": "string"},
            "author": {"type": "string"},
            "api_version": {"const": PLUGIN_API_VERSION},
            "requires": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "目标 App 版本区间，如 >=1.0.0 <2.0.0",
                    },
                    "plugins": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
            "dependencies": {"type": "array", "items": plugin_id, "uniqueItems": True},
            "conflicts": {"type": "array", "items": plugin_id, "uniqueItems": True},
            "code": {
                "type": "object",
                "required": ["entry"],
                "properties": {"entry": relative_path},
                "additionalProperties": False,
            },
            "capabilities": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prop_templates": {
                        "type": "object",
                        "properties": {
                            "dirs": {
                                "type": "array",
                                "minItems": 1,
                                "items": relative_path,
                            }
                        },
                        "additionalProperties": False,
                    },
                    "rulesets": {
                        "type": "object",
                        "required": ["module"],
                        "properties": {
                            "module": relative_path,
                            "ids": {"type": "array", "items": plugin_id},
                        },
                        "additionalProperties": False,
                    },
                    "agent_tools": {
                        "type": "object",
                        "required": ["module", "tools"],
                        "properties": {
                            "module": relative_path,
                            "tools": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "pattern": r"^[a-z][a-z0-9-]*\.[a-z0-9_]+$"},
                            },
                        },
                        "additionalProperties": False,
                    },
                    "http_blueprints": {
                        "type": "object",
                        "required": ["module", "blueprints"],
                        "properties": {
                            "module": relative_path,
                            "blueprints": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                        },
                        "additionalProperties": False,
                    },
                    "settings_sections": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["key"],
                            "properties": {
                                "key": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{1,31}$"},
                                "title": {"type": "string"},
                                "fields": {"type": "array", "items": {"type": "object"}},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "frontend_assets": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["path"],
                            "properties": {
                                "path": relative_path,
                                "type": {"enum": list(_FRONTEND_ASSET_TYPES)},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
            },
        },
    }


def schema_copy() -> dict[str, Any]:
    return copy.deepcopy(manifest_json_schema())


__all__ = [
    "load_manifest",
    "manifest_json_schema",
    "parse_manifest",
    "parse_version",
    "satisfies_version",
    "schema_copy",
]
