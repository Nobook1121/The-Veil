"""网站设置工具：读取与修改 general.toml。

写操作统一要求 ``settings.general`` 权限；网络安全与网站 Agent 自身配置仅 OWNER 可改。
任何密钥字段都不允许通过 Agent 读取明文或写入。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from flask import current_app

from trpg_server.config import (
    general_config_path,
    invalidate_settings_cache,
    load_settings,
    update_sections,
)
from trpg_server.config.schema import SMALL_MODELS_SECTION, SPECS_BY_SECTION_KEY
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

# 只有 OWNER 可以通过 Agent 修改的段：网络暴露面、穿透设置、Agent 自身配额。
OWNER_ONLY_SECTIONS = frozenset(
    {
        "network",
        "network.access_control",
        "network.penetration",
        "network.penetration.settings",
        "site_agent",
    }
)

# 密钥字段：读取时打码，写入时直接拒绝。
SECRET_FIELDS = frozenset({("network.penetration.settings", "auth_token")})

# 可编辑键只取主键名，避免模型写入历史别名造成同一配置出现两份。
EDITABLE_SPECS = {
    (spec.section, spec.keys[0]): spec
    for spec in SPECS_BY_SECTION_KEY.values()
    if spec.section != SMALL_MODELS_SECTION
}


def _config_dir():
    return current_app.config.get("CONFIG_DIR")


def _settings():
    return load_settings(_config_dir())


def _section_values(data: dict[str, Any], section: str) -> dict[str, Any]:
    current: Any = data
    for part in section.split("."):
        if not isinstance(current, dict):
            return {}
        current = current.get(part, {})
    return current if isinstance(current, dict) else {}


def _read_value(data: dict[str, Any], section: str, key: str) -> Any:
    return _section_values(data, section).get(key)


def _redacted_snapshot() -> dict[str, Any]:
    data = deepcopy(_settings().data)
    for section, key in SECRET_FIELDS:
        values = _section_values(data, section)
        if key in values:
            values[key] = "******"
    return data


def _coerce_value(spec: Any, raw: Any) -> tuple[Any, str | None]:
    default = spec.default
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            return spec.convert(raw, default), None
        return None, "必须是布尔值 true/false"
    if isinstance(default, int):
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return None, "必须是整数"
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, "必须是整数"
        value = spec.convert(parsed, default)
        if value != parsed:
            return None, "数值超出允许范围"
        return value, None
    if isinstance(default, list):
        if not isinstance(raw, (list, str)):
            return None, "必须是数组或逗号分隔文本"
        return spec.convert(raw, default), None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None, "必须是文本"
    return spec.convert(raw, default), None


def _parse_changes(arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    changes = arguments.get("changes")
    if not isinstance(changes, list) or not changes:
        return [], "changes 不能为空"
    if len(changes) > 30:
        return [], "一次最多修改 30 项设置"
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in changes:
        if not isinstance(item, dict):
            return [], "changes 中每一项都必须是对象"
        section = str(item.get("section") or "").strip()
        key = str(item.get("key") or "").strip()
        if not section or not key:
            return [], "每一项都必须包含 section 与 key"
        pair = (section, key)
        if pair in seen:
            return [], f"重复修改 {section}.{key}"
        seen.add(pair)
        if pair in SECRET_FIELDS:
            return [], f"{section}.{key} 是密钥字段，不能通过 Agent 修改"
        spec = EDITABLE_SPECS.get(pair)
        if spec is None:
            return [], f"不支持的设置项：{section}.{key}"
        value, error = _coerce_value(spec, item.get("value"))
        if error:
            return [], f"{section}.{key} {error}"
        parsed.append({"section": section, "key": key, "value": value})
    return parsed, None


def _locked(settings: Any, section: str, key: str) -> str | None:
    return settings.locked.get(f"{section}.{key}")


def get_general_settings(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    settings = _settings()
    return ToolResult.success(
        {
            "sections": _redacted_snapshot(),
            "locked_keys": sorted(settings.locked.keys()),
            "owner_only_sections": sorted(OWNER_ONLY_SECTIONS),
            "note": "密钥字段已打码；修改后部分网络设置需要重启服务才会生效。",
        },
        summary="读取网站设置",
    )


def preview_update_settings(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    changes, error = _parse_changes(arguments)
    if error:
        return ToolResult.failure(error)
    settings = _settings()
    rows: list[dict[str, Any]] = []
    for change in changes:
        before = _read_value(settings.data, change["section"], change["key"])
        rows.append(
            {
                "section": change["section"],
                "key": change["key"],
                "before": before,
                "after": change["value"],
            }
        )
    summary = "将更新网站设置：" + "、".join(f"{row['section']}.{row['key']}" for row in rows)
    preview = {
        "changes": rows,
        "summary": summary,
        "apply_args": {"changes": changes},
    }
    return ToolResult.success(preview, summary=summary)


def apply_update_settings(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    changes, error = _parse_changes(arguments)
    if error:
        return ToolResult.failure(error)
    grouped: dict[str, dict[str, Any]] = {}
    for change in changes:
        grouped.setdefault(change["section"], {})[change["key"]] = change["value"]
    update_sections(general_config_path(_config_dir()), grouped)
    invalidate_settings_cache()
    names = "、".join(f"{change['section']}.{change['key']}" for change in changes)
    return ToolResult.success(
        {"updated": changes},
        summary=f"已更新 {len(changes)} 项网站设置：{names}。部分网络设置需重启服务后生效。",
    )


def _check_site_admin(actor: Any, arguments: dict[str, Any]) -> str | None:
    # 即使权限矩阵把 settings.general 授予了 USER，也不允许普通用户经 Agent 触碰网站设置。
    return None if actor.is_admin else "只有管理员或 OWNER 可以通过 Agent 访问网站设置"


def _check_update_settings(actor: Any, arguments: dict[str, Any]) -> str | None:
    guard = _check_site_admin(actor, arguments)
    if guard:
        return guard
    changes, error = _parse_changes(arguments)
    if error:
        return error
    settings = _settings()
    for change in changes:
        section = change["section"]
        if section in OWNER_ONLY_SECTIONS and not actor.is_owner:
            return f"{section} 段仅 OWNER 可以通过 Agent 修改"
        locked = _locked(settings, section, change["key"])
        if locked:
            return f"{section}.{change['key']} 已被部署配置（{locked}）锁定"
    return None


SITE_SETTING_TOOLS = [
    ToolSpec(
        name="site.get_general_settings",
        description="读取网站设置（general.toml 全部生效配置），密钥字段已打码。",
        parameters=schema({}),
        risk="read",
        required_permission="settings.general",
        check=_check_site_admin,
        handler=get_general_settings,
    ),
    ToolSpec(
        name="site.update_general_settings",
        description=(
            "修改网站设置。仅支持 schema 中登记的键；ADMIN 只能改普通设置，"
            "network / network.access_control / network.penetration / site_agent 段仅 OWNER 可改。"
            "执行前会生成待确认操作。"
        ),
        parameters=schema(
            {
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string", "maxLength": 80},
                            "key": {"type": "string", "maxLength": 80},
                            "value": {},
                        },
                        "required": ["section", "key", "value"],
                    },
                }
            },
            ["changes"],
        ),
        risk="danger",
        required_permission="settings.general",
        preview=preview_update_settings,
        apply=apply_update_settings,
        check=_check_update_settings,
    ),
]
