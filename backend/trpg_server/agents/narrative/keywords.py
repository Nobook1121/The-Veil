"""世界书关键词候选（设计文档 §8.5）。

确定性派生：复用导入流水线的 `derive_keywords`，扣除模块已有的主/次关键词，
只返回候选供作者在编辑器中确认。不写剧本、不调用模型。
"""
from __future__ import annotations

from typing import Any, Mapping

MAX_SUGGESTIONS = 8
MAX_SECONDARY = 4


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _module_text(module: Mapping[str, Any]) -> str:
    return str(module.get("content") or module.get("text") or module.get("summary") or "").strip()


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


__all__ = ["suggest_module_keywords"]
