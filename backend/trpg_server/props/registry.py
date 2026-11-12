"""模板注册表：合并多来源模板、校验字段、进程内缓存（§4.6）。

来源分三类（结构相同）：

- 内置：``backend/trpg_server/props/templates/<id>/``，``source="builtin"``；
- 插件：由插件清单 ``capabilities.prop_templates.dirs`` 声明，经
  :func:`register_template_source` 注入，``source="plugin:<插件 id>"``；
- 用户：``data/props/templates/<id>/``，``source="user"``（历史轻量模板包路径）。

非内置来源额外经过 ``template_validation`` 的标签/资源安全校验。
id 冲突时内置优先，其余来源标记为不可用并 warning。任何一个模板坏掉
只影响它自己，不会打挂整个模板列表。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from trpg_server.config.paths import DATA_DIR

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
USER_TEMPLATES_DIR = DATA_DIR / "props" / "templates"
# 兼容旧引用：渲染器仍可从 ``TEMPLATES_DIR`` 推导内置资源目录。
TEMPLATES_DIR = BUILTIN_TEMPLATES_DIR
REQUIRED_MANIFEST_KEYS = ("id", "name", "fields")
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_CACHE: dict[str, tuple[tuple, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()
_ROOTS: dict[str, Path] = {}
_UNAVAILABLE: list[dict[str, Any]] = []
# 插件声明的模板来源：``source 名``（``plugin:<id>``）→ 模板目录列表。
# 由 trpg_server.plugins 在插件激活后注入；未激活的插件不会产生任何来源。
_PLUGIN_SOURCES: dict[str, list[Path]] = {}


class TemplateError(RuntimeError):
    """模板清单非法（缺字段、JSON 损坏、id 不合理或安全校验失败）。"""


class UnknownTemplate(TemplateError):
    """请求的模板不存在；消息里带可用模板列表，便于模型自纠。"""


class FieldError(TemplateError):
    """提交的字段不满足模板 schema。"""


def template_dir(template_id: str) -> Path:
    key = str(template_id or "").strip()
    if not _TEMPLATE_ID_RE.match(key):
        raise UnknownTemplate(f"模板不存在，可用：{_available_text()}")
    _manifests()
    root = _ROOTS.get(key)
    if root is None:
        raise UnknownTemplate(f"模板不存在，可用：{_available_text()}")
    return root


def _available_text() -> str:
    ids = available_ids()
    return ", ".join(ids) if ids else "（无）"


def available_ids() -> list[str]:
    return sorted(_manifests().keys())


def register_template_source(name: str, paths: list[str | Path]) -> None:
    """注册一组插件模板目录（来源名会写进 manifest 的 ``source``）。"""
    key = str(name or "").strip()
    if not key:
        raise TemplateError("模板来源名不能为空")
    normalized = [Path(path) for path in paths]
    with _CACHE_LOCK:
        _PLUGIN_SOURCES[key] = normalized
        _CACHE.clear()


def unregister_template_source(name: str) -> None:
    with _CACHE_LOCK:
        if _PLUGIN_SOURCES.pop(str(name or ""), None) is not None:
            _CACHE.clear()


def reset_template_sources() -> None:
    """清空全部插件来源（测试与插件全量重载用）。"""
    with _CACHE_LOCK:
        if _PLUGIN_SOURCES:
            _PLUGIN_SOURCES.clear()
            _CACHE.clear()


def registered_sources() -> dict[str, list[str]]:
    with _CACHE_LOCK:
        return {key: [str(path) for path in paths] for key, paths in _PLUGIN_SOURCES.items()}


def list_templates() -> list[dict[str, Any]]:
    """返回全部可用模板（含 ``source``），按 manifest.order 排序。

    目录扫描顺序受文件系统影响；``order`` 由模板自己声明，缺失时回落到 id，
    保证 ``templates`` 接口的顺序在进程重启后仍然一致。
    """
    return [
        copy.deepcopy(manifest)
        for _key, manifest in sorted(
            _manifests().items(),
            key=lambda item: (_order_of(item[1]), item[0]),
        )
    ]


def list_template_groups() -> list[dict[str, Any]]:
    """按 manifest 声明顺序返回稳定分组（给前端模板选择器用）。"""
    groups: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for manifest in list_templates():
        group_id = str(manifest.get("group") or "other")
        group_name = str(manifest.get("group_name") or group_id)
        group = index.get(group_id)
        if group is None:
            group = {"id": group_id, "name": group_name, "template_ids": []}
            index[group_id] = group
            groups.append(group)
        group["template_ids"].append(str(manifest.get("id")))
    return groups


def list_unavailable() -> list[dict[str, Any]]:
    """返回不可用模板及原因（用户模板为主，含 id 冲突与安全校验失败）。"""
    _manifests()
    return copy.deepcopy(_UNAVAILABLE)


def rescan() -> dict[str, Any]:
    """清空缓存并重新扫描；供「重新扫描」接口调用，无需重启服务。"""
    with _CACHE_LOCK:
        _CACHE.clear()
    manifests = _manifests()
    return {"templates": len(manifests), "unavailable": list_unavailable()}


def _order_of(manifest: Mapping[str, Any]) -> int:
    try:
        return int(manifest.get("order"))
    except (TypeError, ValueError):
        return 9999


def get_template(template_id: str) -> dict[str, Any]:
    key = str(template_id or "").strip()
    manifest = _manifests().get(key)
    if manifest is None:
        raise UnknownTemplate(f"模板不存在，可用：{_available_text()}")
    return copy.deepcopy(manifest)


def validate_fields(
    template_id: str,
    fields: Mapping[str, Any] | None,
    *,
    max_field_chars: int | None = None,
) -> dict[str, str]:
    """按模板 schema 收敛字段值。

    - 未知键直接报错（避免 AI 把内容写进不存在的槽位后静默丢失）；
    - 必填缺失报错；
    - 超长按 ``min(field.max, max_field_chars)`` 截断（设计稿 §12）。
    """
    manifest = get_template(template_id)
    if fields is None:
        fields = {}
    if not isinstance(fields, Mapping):
        raise FieldError("fields 必须是对象")

    allowed = {str(field["key"]): field for field in manifest.get("fields") or []}
    unknown = [str(key) for key in fields if str(key) not in allowed]
    if unknown:
        raise FieldError("未知字段：" + ", ".join(sorted(unknown)))

    global_limit = _positive_int(max_field_chars)
    result: dict[str, str] = {}
    for key, spec in allowed.items():
        raw = fields.get(key, "")
        text = "" if raw is None else str(raw)
        limit = _positive_int(spec.get("max"))
        if global_limit is not None:
            limit = global_limit if limit is None else min(limit, global_limit)
        if limit is not None and len(text) > limit:
            text = text[:limit]
        if spec.get("required") and not text.strip():
            raise FieldError(f"缺少必填字段：{spec.get('label') or key}")
        result[key] = text
    return result


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _manifests() -> dict[str, dict[str, Any]]:
    with _CACHE_LOCK:
        return _scan()


def _template_sources() -> list[tuple[str, Path]]:
    """按「内置 → 插件 → 用户」顺序返回来源；先出现者优先。"""
    sources: list[tuple[str, Path]] = [("builtin", TEMPLATES_DIR)]
    for name, paths in sorted(_PLUGIN_SOURCES.items()):
        for path in paths:
            sources.append((name, path))
    sources.append(("user", USER_TEMPLATES_DIR))
    return sources


def _scan() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    roots: dict[str, Path] = {}
    unavailable: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    # 用模块级 TEMPLATES_DIR / USER_TEMPLATES_DIR 而不是常量：测试与部分部署
    # 会临时改写这两个目录；插件来源由 plugins 包在激活后注入。
    for source, base in _template_sources():
        if not base.is_dir():
            continue
        for root in sorted(base.iterdir()):
            if not root.is_dir():
                continue
            manifest_path = root / "manifest.json"
            if not manifest_path.is_file():
                if source != "builtin":
                    unavailable.append(_unavailable_entry(root, source, "缺少 manifest.json"))
                continue
            signature = _signature(root)
            cache_key = f"{source}:{root.name}"
            seen_keys.add(cache_key)
            cached = _CACHE.get(cache_key)
            manifest: dict[str, Any] | None = None
            if cached is not None and cached[0] == signature:
                manifest = cached[1]
            else:
                try:
                    manifest = _read_manifest(manifest_path)
                    if source != "builtin":
                        from .template_validation import validate_user_template

                        validate_user_template(root, manifest)
                    manifest["source"] = source
                except TemplateError as exc:
                    _CACHE.pop(cache_key, None)
                    unavailable.append(_unavailable_entry(root, source, str(exc)))
                    logger.warning(
                        "props template invalid id=%s source=%s reason=%s",
                        root.name,
                        source,
                        exc,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - 单个坏模板不影响列表
                    _CACHE.pop(cache_key, None)
                    unavailable.append(_unavailable_entry(root, source, f"校验失败：{exc}"))
                    logger.warning(
                        "props template validation crashed id=%s source=%s",
                        root.name,
                        source,
                        exc_info=True,
                    )
                    continue
                _CACHE[cache_key] = (signature, manifest)

            template_id = str(manifest.get("id") or "")
            if template_id in manifests:
                existing_source = str(manifests[template_id].get("source") or "")
                if existing_source == "builtin" and source == "user":
                    reason = "与内置模板 id 冲突，用户模板已跳过"
                elif existing_source == "builtin":
                    reason = f"与内置模板 id 冲突，{source} 已跳过"
                else:
                    reason = f"与已加载模板 id 冲突（{existing_source}），{source} 已跳过"
                unavailable.append(_unavailable_entry(root, source, reason))
                logger.warning("props template conflict id=%s source=%s", template_id, source)
                continue
            manifests[template_id] = manifest
            roots[template_id] = root

    for key in list(_CACHE):
        if key not in seen_keys:
            _CACHE.pop(key, None)
    _ROOTS.clear()
    _ROOTS.update(roots)
    _UNAVAILABLE[:] = unavailable
    return manifests


def _unavailable_entry(root: Path, source: str, reason: str) -> dict[str, Any]:
    return {"id": root.name, "dir": str(root), "source": source, "reason": reason}


def _signature(root: Path) -> tuple:
    """缓存签名：manifest + page/style + assets 的 mtime/size 摘要。"""
    signature: list[Any] = [root.name]
    for name in ("manifest.json", "page.html", "style.css"):
        try:
            stat = (root / name).stat()
            signature.extend([stat.st_mtime_ns, stat.st_size])
        except OSError:
            signature.extend([0, 0])
    assets = root / "assets"
    if assets.is_dir():
        signature.append(assets.stat().st_mtime_ns)
        for entry in sorted(assets.iterdir()):
            if entry.is_file():
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                signature.extend([entry.name, stat.st_mtime_ns, stat.st_size])
    return tuple(signature)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateError(f"模板清单无法读取：{path.parent.name}") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"模板清单必须是对象：{path.parent.name}")
    for key in REQUIRED_MANIFEST_KEYS:
        if key not in data:
            raise TemplateError(f"模板 {path.parent.name} 的 manifest 缺少字段：{key}")

    template_id = str(data.get("id") or "").strip()
    if not _TEMPLATE_ID_RE.match(template_id):
        raise TemplateError(f"模板 id 不合法：{template_id or path.parent.name}")
    if template_id != path.parent.name:
        raise TemplateError(f"模板 id 与目录名不一致：{template_id} != {path.parent.name}")

    raw_fields = data.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise TemplateError(f"模板 {template_id} 的 fields 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in raw_fields:
        if not isinstance(field, dict):
            raise TemplateError(f"模板 {template_id} 的字段必须是对象")
        key = str(field.get("key") or "").strip()
        if not key:
            raise TemplateError(f"模板 {template_id} 的字段缺少 key")
        if key in seen:
            raise TemplateError(f"模板 {template_id} 的字段 key 重复：{key}")
        seen.add(key)
        normalized.append(
            {
                **field,
                "key": key,
                "label": str(field.get("label") or key),
                "type": str(field.get("type") or "text"),
            }
        )
    data["fields"] = normalized
    data["id"] = template_id
    data["name"] = str(data.get("name") or template_id)
    return data


__all__ = [
    "BUILTIN_TEMPLATES_DIR",
    "FieldError",
    "REQUIRED_MANIFEST_KEYS",
    "TEMPLATES_DIR",
    "TemplateError",
    "USER_TEMPLATES_DIR",
    "UnknownTemplate",
    "available_ids",
    "get_template",
    "list_template_groups",
    "list_templates",
    "list_unavailable",
    "register_template_source",
    "registered_sources",
    "reset_template_sources",
    "rescan",
    "template_dir",
    "unregister_template_source",
    "validate_fields",
]
