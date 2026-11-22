import inspect
from typing import Any

from trpg_server.agents.tools.base import AgentTool


MAX_RULESET_RESULTS = 3
MAX_REFERENCE_TEXT_LENGTH = 1200

# 每条参考始终带的基础字段（旧消费方依赖这份清单）。
_BASE_KEYS = ("ruleset_id", "knowledge_version", "chunk_id", "topic", "citation")
# 有值才带（老结果没有来源声明时结构不变）。
_OPTIONAL_KEYS = ("title", "source", "license", "license_url")
# 深库附加字段：命中深库时才有，**只加不删**，老消费方忽略即可。
_DEEP_KEYS = (
    "deep_layer",
    "deep_link",
    "category",
    "uid",
    "name",
    "eng_name",
    "rules_version",
    "is_machine_translated",
    "translator_ok",
    "translator_note",
)


def _observer_is_dm(tool_state: Any) -> bool:
    """观察者视角：AI 玩家角色（``ai_player_turn``）不看 DM 侧类目，其余按 KP 走。"""
    if not isinstance(tool_state, dict):
        return True
    return not bool(tool_state.get("ai_player_turn"))


def _invoke_search(search: Any, query: str, top_k: int, observer_is_dm: bool) -> Any:
    """把「观察者是否 DM」传给支持它的检索可调用对象。

    历史上的 ``ruleset_search`` 只吃 ``(query, top_k)`` 两个位置参数，所以
    这里先看签名：不接受 ``observer_is_dm`` 就按老方式调用（零回归）。
    """
    try:
        parameters = inspect.signature(search).parameters
    except (TypeError, ValueError):
        parameters = None
    accepts = bool(parameters) and (
        "observer_is_dm" in parameters
        or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    )
    if accepts:
        return search(query, top_k, observer_is_dm=observer_is_dm)
    return search(query, top_k)


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

    raw_results = _invoke_search(search, query, top_k, _observer_is_dm(tool_state))
    references = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        reference = {key: item.get(key) for key in _BASE_KEYS}
        reference["text"] = str(item.get("text"))[:MAX_REFERENCE_TEXT_LENGTH]
        # 来源与许可随结果给出；没有声明的旧检索结果保持原结构不变。
        for key in _OPTIONAL_KEYS:
            if item.get(key):
                reference[key] = item.get(key)
        # 深库命中时附带的深链/机翻标记（新增字段，不影响旧结构）。
        for key in _DEEP_KEYS:
            if item.get(key) not in (None, ""):
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
        "命中内容深库（kiwee / 5etools 中文镜像）时，结果会带 deep_link（原文链接）、"
        "rules_version（2014/2024）与 translator_note（如「机翻，未校对」）；"
        "深库未建时自动回落到规则书内容包，用法不变。"
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
