"""模板注册表：扫描 ``templates/*/manifest.json``、校验字段、进程内缓存（§4.6）。

模板目录就是未来插件化的单位：只要 ``manifest.json`` 合法，新增一个目录即等于
新增一个模板，不需要改代码。缓存以 ``manifest.json`` 的 ``mtime_ns`` + 大小做键，
开发时改模板无需重启服务。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
REQUIRED_MANIFEST_KEYS = ("id", "name", "fields")
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


class TemplateError(RuntimeError):
    """模板清单非法（缺字段、JSON 损坏、id 不合理）。"""


class UnknownTemplate(TemplateError):
    """请求的模板不存在；消息里带可用模板列表，便于模型自纠。"""


class FieldError(TemplateError):
    """提交的字段不满足模板 schema。"""


def template_dir(template_id: str) -> Path:
    key = str(template_id or "").strip()
    if not _TEMPLATE_ID_RE.match(key):
        raise UnknownTemplate(f"模板不存在，可用：{_available_text()}")
    return TEMPLATES_DIR / key


def _available_text() -> str:
    ids = available_ids()
    return ", ".join(ids) if ids else "（无）"


def available_ids() -> list[str]:
    return sorted(_manifests().keys())


def list_templates() -> list[dict[str, Any]]:
    """返回全部模板清单（含字段 schema），按 id 排序。"""
    return [copy.deepcopy(manifest) for _key, manifest in sorted(_manifests().items())]


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
        fresh: dict[str, dict[str, Any]] = {}
        roots = sorted(TEMPLATES_DIR.iterdir()) if TEMPLATES_DIR.is_dir() else []
        for root in roots:
            manifest_path = root / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                stat = manifest_path.stat()
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = _CACHE.get(root.name)
            if cached is not None and cached[0] == signature:
                fresh[root.name] = cached[1]
                continue
            try:
                manifest = _read_manifest(manifest_path)
            except TemplateError:
                logger.warning("props template invalid id=%s", root.name, exc_info=True)
                continue
            fresh[root.name] = manifest
            _CACHE[root.name] = (signature, manifest)
        for name in list(_CACHE):
            if name not in fresh:
                _CACHE.pop(name, None)
        return fresh


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
    "FieldError",
    "REQUIRED_MANIFEST_KEYS",
    "TEMPLATES_DIR",
    "TemplateError",
    "UnknownTemplate",
    "available_ids",
    "get_template",
    "list_templates",
    "template_dir",
    "validate_fields",
]
