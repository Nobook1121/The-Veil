"""世界书关键词候选（设计文档 §8.2 / §8.5）。

确定性派生：复用导入流水线的 `derive_keywords`，扣除已有的主关键词、别名与
否定词，只返回候选供作者在编辑器中确认。不写剧本、不调用模型。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

MAX_SUGGESTIONS = 8
MAX_SECONDARY = 4

_QUOTED_TERM = re.compile(r"[“\"「『']([^”\"」』']{2,20})[”\"」』']")


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _module_text(module: Mapping[str, Any]) -> str:
    return str(module.get("content") or module.get("text") or module.get("summary") or "").strip()


def _node_field(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def _node_text(node: Any) -> str:
    body = str(_node_field(node, "body") or "")
    summary = str(_node_field(node, "summary") or "")
    return f"{body}\n{summary}".strip()


def _node_source(term: str, title: str, body: str) -> str:
    if term and term.casefold() in title.casefold():
        return "title"
    if term and term in _QUOTED_TERM.findall(body):
        return "quoted"
    return "body"


def _node_confidence(term: str, body: str, source: str) -> float:
    """确定性置信度：标题/引号专名更高，正文重复次数越多越高。"""
    if source == "title":
        return 0.95
    if source == "quoted":
        return 0.9
    count = body.count(term)
    return round(min(0.9, 0.5 + 0.1 * max(0, count - 1)), 2)


def suggest_node_keywords(
    nodes: Any,
    *,
    limit: int = MAX_SUGGESTIONS,
    node_ids: list[str] | None = None,
) -> dict[str, Any]:
    """按节点正文派生关键词候选，附带来源与置信度（§8.2）。"""
    from trpg_server.scenario_import_pipeline import derive_keywords

    limit = max(1, min(20, int(limit)))
    wanted = {str(item) for item in (node_ids or []) if str(item).strip()}
    suggestions: list[dict[str, Any]] = []
    for node in nodes or []:
        node_id = str(_node_field(node, "id") or "").strip()
        if not node_id or (wanted and node_id not in wanted):
            continue
        title = str(_node_field(node, "title") or "").strip()
        body = _node_text(node)
        if not body:
            continue
        current = _as_list(_node_field(node, "keywords"))
        aliases = _as_list(_node_field(node, "aliases"))
        negative = _as_list(_node_field(node, "negative_keywords"))
        taken = {item.casefold() for item in (*current, *aliases, *negative)}
        candidates = derive_keywords(body, title, limit=max(limit * 2, 12))
        suggested: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in candidates:
            key = str(term).strip()
            folded = key.casefold()
            if not key or folded in taken or folded in seen:
                continue
            seen.add(folded)
            source = _node_source(key, title, body)
            suggested.append(
                {
                    "key": key,
                    "source": source,
                    "confidence": _node_confidence(key, body, source),
                }
            )
            if len(suggested) >= limit:
                break
        if not suggested:
            continue
        suggestions.append(
            {
                "node_id": node_id,
                "title": title,
                "current": current,
                "current_aliases": aliases,
                "current_negative": negative,
                "suggested": suggested,
            }
        )
    return {
        "suggestions": suggestions,
        "count": len(suggestions),
        "limit": limit,
    }


def suggest_module_keywords(
    descriptor: Mapping[str, Any],
    *,
    limit: int = MAX_SUGGESTIONS,
    module_ids: list[str] | None = None,
) -> dict[str, Any]:
    from trpg_server.scenario_import_pipeline import derive_keywords

    limit = max(1, min(20, int(limit)))
    modules = descriptor.get("modules") if isinstance(descriptor, Mapping) else None
    wanted = {str(item) for item in (module_ids or []) if str(item).strip()}
    suggestions: list[dict[str, Any]] = []
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("id") or "").strip()
        if wanted and module_id not in wanted:
            continue
        text = _module_text(module)
        if not text:
            continue
        title = str(module.get("title") or module.get("name") or "").strip()
        current = _as_list(module.get("keywords"))
        current_secondary = _as_list(module.get("secondary_keywords"))
        taken = {item.casefold() for item in (*current, *current_secondary)}
        pool = derive_keywords(text, title, limit=max(limit * 2, 12))
        suggested = [item for item in pool if item.casefold() not in taken][:limit]
        suggested_keys = {item.casefold() for item in suggested}
        secondary_pool = derive_keywords(text, "", limit=max(limit * 2, 12))
        secondary = [
            item
            for item in secondary_pool
            if item.casefold() not in taken and item.casefold() not in suggested_keys
        ][:MAX_SECONDARY]
        if not suggested and not secondary:
            continue
        suggestions.append(
            {
                "module_id": module_id,
                "title": title,
                "current": current,
                "current_secondary": current_secondary,
                "suggested": suggested,
                "suggested_secondary": secondary,
            }
        )
    return {
        "suggestions": suggestions,
        "count": len(suggestions),
        "limit": limit,
    }


__all__ = ["suggest_module_keywords", "suggest_node_keywords"]
