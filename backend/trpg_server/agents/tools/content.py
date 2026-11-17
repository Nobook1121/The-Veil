"""通用内容检索工具：按当前规则集插件声明的检索能力下发。

核心不认识法术、怪物或装备字段；插件通过 ``content_search`` 能力提供
``types()`` 与 ``search(type, query, limit)``，并自行补充区分信息。
"""

from __future__ import annotations

import logging
from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.room_rules import resolve_rule_id
from trpg_server.ruleset import get_plugin

logger = logging.getLogger(__name__)

CONTENT_SEARCH_DESCRIPTION = (
    "在当前规则集的内容包中检索单个条目（如法术、怪物、装备）。"
    "返回 status：match 表示唯一命中，ambiguous 表示有多个候选，none 表示没有命中。"
    "ambiguous 时绝不要自行取第一条，必须把 candidates 交给用户或调用方确认；"
    "分类信息由规则集插件提供。不传 type 时返回该规则集支持的内容类型。"
)


def _content_search(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = str((arguments or {}).get("query") or "").strip()
    type_name = str((arguments or {}).get("type") or "").strip().lower()
    try:
        limit = int((arguments or {}).get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(20, limit))

    info = context.room_info() if hasattr(context, "room_info") else {}
    ruleset_id = resolve_rule_id(info, scenarios_dir=getattr(context, "scenarios_dir", None))
    plugin = get_plugin(ruleset_id)
    provider = getattr(plugin, "content_search", None) if plugin is not None else None
    if provider is None:
        return {
            "ok": False,
            "error": f"规则集「{ruleset_id}」没有提供内容检索能力。",
        }
    try:
        types = [str(item) for item in provider.types()]
        if not type_name:
            return {
                "ok": True,
                "status": "none",
                "query": query,
                "type": "",
                "types": types,
                "matches": [],
                "total": 0,
            }
        if not query:
            return {
                "ok": False,
                "error": "query 不能为空。",
                "types": types,
            }
        result = provider.search(type_name, query, limit=limit)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001 - 插件检索失败不能拖垮对话
        logger.exception("content_search_failed ruleset=%s type=%s", ruleset_id, type_name)
        return {"ok": False, "error": "内容检索失败，请稍后重试。"}
    payload = dict(result)
    payload["ok"] = True
    payload["types"] = types
    return payload


CONTENT_SEARCH_TOOL = AgentTool(
    name="content.search",
    description=CONTENT_SEARCH_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "内容名称或 id，支持中文名、英文名与近似输入。",
            },
            "type": {
                "type": "string",
                "description": "内容类型（如 spell / monster / equipment）；省略时列出可用类型。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "最多返回的候选数量，默认 10。",
            },
        },
        "required": [],
    },
    handler=_content_search,
)
