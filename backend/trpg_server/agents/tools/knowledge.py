from typing import Any

from trpg_server.agents.tools.base import AgentTool


MAX_RULESET_RESULTS = 3
MAX_REFERENCE_TEXT_LENGTH = 1200


def search_ruleset_knowledge(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    requested_top_k = arguments.get("top_k", MAX_RULESET_RESULTS)
    try:
        top_k = max(1, min(int(requested_top_k), MAX_RULESET_RESULTS))
    except (TypeError, ValueError):
        top_k = MAX_RULESET_RESULTS

    tool_state = getattr(context, "tool_state", None)
    search = tool_state.get("ruleset_search") if isinstance(tool_state, dict) else None
    if not callable(search):
        return {"error": "ruleset knowledge search is unavailable"}

    raw_results = search(query, top_k)
    references = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        reference = {
            "ruleset_id": item.get("ruleset_id"),
            "knowledge_version": item.get("knowledge_version"),
            "chunk_id": item.get("chunk_id"),
            "topic": item.get("topic"),
            "citation": item.get("citation"),
            "text": str(item.get("text"))[:MAX_REFERENCE_TEXT_LENGTH],
        }
        # 来源与许可随结果给出；没有声明的旧检索结果保持原结构不变。
        for key in ("title", "source", "license", "license_url"):
            if item.get(key):
                reference[key] = item.get(key)
        references.append(reference)
        if len(references) >= top_k:
            break

    def unique_values(key: str) -> list[str]:
        return sorted({str(item.get(key)) for item in references if item.get(key)})

    citations = [str(item.get("citation")) for item in references if item.get("citation")]
    usage = {
        "ruleset_chunks": len(references),
        "ruleset_sources": len(set(citations)),
        "ruleset_ids": unique_values("ruleset_id"),
        "knowledge_versions": unique_values("knowledge_version"),
        "topics": unique_values("topic"),
        "citations": citations,
    }
    return {
        "used": bool(references),
        "references": references,
        "knowledge_usage": usage,
    }


SEARCH_RULESET_KNOWLEDGE_TOOL = AgentTool(
    name="knowledge.search_ruleset",
    description=(
        "按需检索当前房间绑定的规则书。仅在需要精确规则、检定流程、伤害/死亡处理，"
        "或守秘人主持与玩家体验原则时调用；普通叙事、问候和剧本事实不调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "简短、聚焦的规则问题或主持问题，不要复制整段对话。",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RULESET_RESULTS,
                "default": MAX_RULESET_RESULTS,
            },
        },
        "required": ["query"],
    },
    handler=search_ruleset_knowledge,
)
