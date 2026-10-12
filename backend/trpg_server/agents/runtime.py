import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trpg_server.agents.profiles import AgentProfile
from trpg_server.agents.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# 仅供前端展示、不参与模型推理的字段。这些字段会在工具结果回传给模型前被剥离：
# 它们本身是 UI 载体（骰娘消息、直接消息），塞进 role="tool" 只会让后续每一轮
# prompt 重复计入同样的内容，白白放大输入 token。
UI_ONLY_TOOL_RESULT_KEYS = frozenset({"visible_message", "direct_message"})

# 各厂商 usage 的字段结构差异很大（尤其是缓存字段）。首次遇到一种新结构时打一条完整
# 原文，方便直接确认平台到底有没有上报缓存信息；相同结构不再重复打印，避免刷屏。
_usage_shapes_logged: set[tuple[str, ...]] = set()


def _log_usage_shape(usage: dict[str, Any]) -> None:
    shape = tuple(sorted(str(key) for key in usage))
    if not shape or shape in _usage_shapes_logged:
        return
    _usage_shapes_logged.add(shape)
    logger.info("AI usage fields from provider: %s", json.dumps(usage, ensure_ascii=False))


# 只有这些工具的结果才允许折叠：它们是大块、且随时可以重新获取的房间/剧本数据。
# 检定、掷骰、理智、触发器、知识检索、记忆写入等结果会影响叙事判断与状态推进，
# 永远保留原文——省 token 不能以 KP 忽略检定结果或凭空编造内容为代价。
FOLDABLE_TOOL_NAMES = frozenset(
    {
        "room.get_room_snapshot",
        "room.get_character_cards",
        "room.get_scenario_context",
        "room.get_scenario_module",
        "room.get_memory",
    }
)
# 最近 N 条工具结果始终保留原文，避免刚拿到的数据马上被折叠掉。
TOOL_RESULT_KEEP_RECENT = 2
# 这些工具的结果会随房间/剧本状态变化：同一轮内可去重，但跨轮必须允许重新调用，
# 否则 KP 在检定/触发器/转场改变了状态后拿到的仍是过期快照，会误判剧情。
STATEFUL_REFETCH_TOOL_NAMES = frozenset(
    {
        "room.get_room_snapshot",
        "room.get_scenario_context",
        "room.get_scenario_module",
        "room.get_character_cards",
        "room.get_memory",
    }
)
FOLDED_TOOL_RESULT_NOTICE = (
    '{"folded": true, "note": "较早的工具结果已折叠以节省上下文，不代表当前状态；'
    '如需该数据请重新调用该工具。"}'
)


def _fold_stale_tool_results(messages: list[dict[str, Any]]) -> None:
    """就地折叠较早的「可重取」工具结果，减少后续每一轮的重复输入。

    只处理 ``FOLDABLE_TOOL_NAMES``，并保留最近 ``TOOL_RESULT_KEEP_RECENT`` 条原文；
    折叠后的内容是一条明确的占位提示，模型据此知道数据已被移除、可重新获取，
    不会把缺失的数据当成"没有"而编造。
    """
    foldable_indexes = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool" and str(message.get("name") or "") in FOLDABLE_TOOL_NAMES
    ]
    stale_count = len(foldable_indexes) - TOOL_RESULT_KEEP_RECENT
    for index in foldable_indexes[: max(0, stale_count)]:
        message = messages[index]
        if str(message.get("content") or "") != FOLDED_TOOL_RESULT_NOTICE:
            message["content"] = FOLDED_TOOL_RESULT_NOTICE


@dataclass(frozen=True)
class AgentCompletionResult:
    content: str = ""
    token_count: int | None = None
    error: str | None = None
    response_data: dict[str, Any] | None = None
    tool_messages: list[dict[str, Any]] | None = None
    direct_messages: list[dict[str, Any]] | None = None
    prompt_token_count: int | None = None
    completion_token_count: int | None = None
    cached_token_count: int | None = None
    knowledge_usage: dict[str, Any] | None = None
    # 非致命异常（请求失败/超时、达到工具轮数上限）时，附带一段要展示给玩家的说明。
    notice: str | None = None
    # 是否因为达到 max_tool_rounds 而被迫收尾。
    rounds_exhausted: bool = False


def _extract_message(response_data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response_data, dict):
        return {}
    choices = response_data.get("choices", [])
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    return choices[0].get("message") or choices[0].get("delta") or {}


def _extract_token_count(response_data: dict[str, Any]) -> int | None:
    usage = response_data.get("usage") or {}
    if "total_tokens" in usage:
        return usage["total_tokens"]
    if "completion_tokens" in usage and "prompt_tokens" in usage:
        return usage["completion_tokens"] + usage["prompt_tokens"]
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_usage_counts(response_data: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    """解析单次响应的 (prompt, completion, cached) token。

    各厂商的缓存字段命名不统一，这里逐个探测而不是用 ``or`` 串起来：
    ``or`` 会把合法的 ``0`` 当成"没有值"而穿透到下一个字段，导致命中数被误读为别的值。
    """
    usage = (response_data or {}).get("usage") or {}
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    prompt = _as_int(usage.get("prompt_tokens"))
    cached = None
    for candidate in (
        details.get("cached_tokens"),
        usage.get("cached_tokens"),
        usage.get("prompt_cache_hit_tokens"),  # DeepSeek 及部分兼容端点
        details.get("cache_read_input_tokens"),  # Anthropic 风格
        usage.get("cache_read_input_tokens"),
    ):
        cached = _as_int(candidate)
        if cached is not None:
            break
    if prompt is None:
        # 少数端点只给命中/未命中拆分，没有 prompt_tokens，需要自己还原。
        hit = _as_int(usage.get("prompt_cache_hit_tokens"))
        miss = _as_int(usage.get("prompt_cache_miss_tokens"))
        if hit is not None or miss is not None:
            prompt = (hit or 0) + (miss or 0)
    return (prompt, _as_int(usage.get("completion_tokens")), cached)


def _parse_arguments(raw_arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not raw_arguments:
        return {}
    parsed = json.loads(raw_arguments)
    return parsed if isinstance(parsed, dict) else {}


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(message.get("tool_calls"), list):
        return message["tool_calls"]
    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        return [{"id": "function-call", "type": "function", "function": function_call}]
    return []


def _extract_json_dict(content: str) -> dict[str, Any] | None:
    """从可能带 Markdown 围栏或前后缀文字的正文里取出一个 JSON 对象。"""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _coerce_tool_call(raw: Any, index: int) -> dict[str, Any] | None:
    """把正文里内嵌的调用项规范化为原生 ``tool_calls`` 形状。"""
    if isinstance(raw, str):
        raw = {"name": raw, "arguments": {}}
    if not isinstance(raw, dict):
        return None
    function = raw.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments")
    else:
        name = str(raw.get("name") or raw.get("tool") or raw.get("tool_name") or "").strip()
        arguments = raw.get("arguments")
        if arguments is None:
            arguments = raw.get("parameters", raw.get("args"))
    if not name:
        return None
    if isinstance(arguments, (dict, list)):
        arguments = json.dumps(arguments, ensure_ascii=False)
    elif not isinstance(arguments, str):
        arguments = "{}"
    return {
        "id": str(raw.get("id") or f"embedded-call-{index + 1}"),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _embedded_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """回收被写进正文 JSON 的工具调用（部分厂商在强制 JSON 输出时会这样）。

    期望形状（无 ``function`` 包装）::

        {"reply": "...", "tool_calls": [{"name": "room.x", "arguments": {...}}]}

    也兼容标准 ``{"id":..., "type":"function", "function": {...}}`` 形状。正文里的
    叙事文本会被丢弃（它早于工具结果生成、已过期），并与原生工具调用语义保持一致。
    """
    content = message.get("content")
    if not isinstance(content, str) or "{" not in content:
        return []
    data = _extract_json_dict(content)
    if not isinstance(data, dict):
        return []
    raw_calls = data.get("tool_calls") or data.get("toolCalls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return []
    return [call for call in (_coerce_tool_call(raw, index) for index, raw in enumerate(raw_calls)) if call]


def _resolve_tool(tool_name: str, enabled_by_name: dict[str, Any]) -> tuple[str, Any] | tuple[None, None]:
    tool = enabled_by_name.get(tool_name)
    if tool:
        return tool_name, tool
    if "_" in tool_name:
        alias_name = tool_name.replace("_", ".", 1)
        tool = enabled_by_name.get(alias_name)
        if tool:
            return alias_name, tool
    return None, None


def _assistant_tool_call_message(message: dict[str, Any]) -> dict[str, Any]:
    clean_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": message.get("tool_calls", []),
    }
    if "function_call" in message:
        clean_message["function_call"] = message["function_call"]
    return clean_message


def _truncate_tool_result(result: dict[str, Any], max_chars: int | None) -> tuple[str, str]:
    """Serialize a tool result for re-sending to the model, truncating when it
    exceeds ``max_chars``. Returns ``(payload, display_text)`` where payload is
    what is sent back to the model and display_text is a human-friendly marker.
    The original result is not truncated for UI/tool_messages purposes.
    """
    payload = json.dumps(result, ensure_ascii=False)
    if not max_chars or len(payload) <= max_chars:
        return payload, payload
    truncated = payload[:max_chars]
    marker = "\n…[工具结果已截断：完整结果共 %d 字符，仅保留前 %d 字符]" % (
        len(payload),
        max_chars,
    )
    return truncated + marker, truncated + marker


def _fallback_tool_summary(
    tool_messages: list[dict[str, Any]], direct_messages: list[dict[str, Any]]
) -> str:
    """当模型最终没有返回可显示内容时，用已有工具结果兜底一句话回复。

    保证玩家始终能收到消息，而不是空响应或 500 错误。
    """
    if direct_messages:
        return "已完成场景处理，详情见上方记录。"
    if tool_messages:
        return "已完成检定/场景处理，详情见上方记录。"
    return "本次工具调用较多，但没有生成可显示的回复，请补充你的行动后重试。"


def _canonical_call_signature(tool_name: str, arguments: Any) -> str:
    """同一工具 + 同一参数的稳定签名，用于检测本轮内的重复调用。

    ``arguments`` 可能是已解析的字典，也可能是模型给出的原始 JSON 字符串；
    两者都归一化成「排序后的紧凑 JSON」，保证同样的调用得到同样的签名。
    """
    if isinstance(arguments, str):
        normalized = arguments.strip()
    else:
        normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool_name}:{normalized}"


def _dedupe_reuse_notice(tool_name: str) -> str:
    """重复调用被短路时回传给模型的说明，避免它以为"没调用成功"而反复重试。"""
    return (
        json.dumps(
            {
                "reused": True,
                "note": f"{tool_name} 已用相同参数调用过，本次未重复执行；请直接使用上一条同名工具结果继续。",
            },
            ensure_ascii=False,
        )
    )


def run_agent_completion(
    requester: Callable[[dict[str, Any]], dict[str, Any]],
    base_payload: dict[str, Any],
    profile: AgentProfile,
    registry: ToolRegistry,
    context: Any,
    max_tool_rounds: int = 8,
    max_tool_result_chars: int | None = None,
    excluded_tools: set[str] | None = None,
    dedupe_tool_calls: bool = True,
) -> AgentCompletionResult:
    payload = {**base_payload}
    messages = list(payload.get("messages", []))
    payload["messages"] = messages
    excluded = {str(name) for name in (excluded_tools or set())}
    enabled_tools = [tool for tool in registry.select(profile.tool_names) if tool.name not in excluded]
    if enabled_tools:
        payload["tools"] = [tool.schema() for tool in enabled_tools]
        payload["tool_choice"] = "auto"

    enabled_by_name = {tool.name: tool for tool in enabled_tools}
    last_response = None
    tool_messages: list[dict[str, Any]] = []
    direct_messages: list[dict[str, Any]] = []
    empty_completion_retries = 0
    total_token_count = 0
    has_token_count = False
    prompt_token_count = 0
    completion_token_count = 0
    cached_token_count = 0
    has_prompt_count = False
    has_completion_count = False
    has_cached_count = False
    knowledge_usage: dict[str, Any] = {
        "ruleset_chunks": 0,
        "ruleset_sources": 0,
        "ruleset_ids": [],
        "knowledge_versions": [],
        "topics": [],
        "citations": [],
        "tool_calls": 0,
    }
    tool_state = getattr(context, "tool_state", None)
    stage_callback = tool_state.get("thinking_stage_callback") if isinstance(tool_state, dict) else None
    # 本轮发言内已执行过的 「工具+参数」 签名：重复调用直接短路，避免重复掷骰 /
    # 重复触发导致状态错乱，也省掉重复执行的往返与 token。
    # ``executed_calls`` 跨轮生效；``round_signatures`` 每轮清空，让「随状态变化的
    # 读取类工具」在场景推进后仍能被重新调用、拿到最新数据。
    executed_calls: dict[str, str] = {}
    round_signatures: set[str] = set()

    def _record_usage(response_data: dict[str, Any] | None) -> None:
        """累计单次请求的 token 用量（含最终的无工具收尾请求）。"""
        nonlocal total_token_count, has_token_count
        nonlocal prompt_token_count, has_prompt_count
        nonlocal completion_token_count, has_completion_count
        nonlocal cached_token_count, has_cached_count
        if not isinstance(response_data, dict):
            return
        usage = response_data.get("usage")
        if isinstance(usage, dict):
            _log_usage_shape(usage)
        round_token_count = _extract_token_count(response_data)
        round_prompt, round_completion, round_cached = _extract_usage_counts(response_data)
        if round_prompt is not None:
            prompt_token_count += round_prompt
            has_prompt_count = True
        if round_completion is not None:
            completion_token_count += round_completion
            has_completion_count = True
        if round_cached is not None:
            cached_token_count += round_cached
            has_cached_count = True
        if round_token_count is not None:
            total_token_count += round_token_count
            has_token_count = True

    for _round in range(max_tool_rounds + 1):
        round_signatures.clear()
        if isinstance(tool_state, dict):
            tool_state["agent_request_rounds"] = _round + 1
        _fold_stale_tool_results(messages)
        if callable(stage_callback):
            stage_callback("ai_request", "正在请求 AI")
        try:
            response_data = requester(payload)
        except Exception as exc:
            # 请求失败或超时时，不要静默丢弃已经拿到的工具结果；
            # 有可展示内容就带提示返回，否则才作为错误上报。
            logger.warning("Agent request failed on round %s: %s", _round + 1, exc)
            if tool_messages or direct_messages:
                return AgentCompletionResult(
                    content=_fallback_tool_summary(tool_messages, direct_messages),
                    token_count=total_token_count if has_token_count else None,
                    tool_messages=tool_messages,
                    direct_messages=direct_messages,
                    prompt_token_count=prompt_token_count if has_prompt_count else None,
                    completion_token_count=completion_token_count if has_completion_count else None,
                    cached_token_count=cached_token_count if has_cached_count else None,
                    knowledge_usage=knowledge_usage if knowledge_usage["tool_calls"] else None,
                    notice=f"（AI 请求中断：{exc}）",
                )
            return AgentCompletionResult(error=str(exc))
        last_response = response_data
        _record_usage(response_data)
        message = _extract_message(response_data)
        calls = _tool_calls(message)
        if not calls:
            embedded_calls = _embedded_tool_calls(message)
            if embedded_calls:
                # 部分厂商在强制 JSON 输出时会把工具调用写进正文 JSON。这里把它还原成
                # 原生调用继续执行，避免工具被静默丢弃、原始 JSON 被当成 KP 回复展示。
                calls = embedded_calls
                message = {**message, "tool_calls": embedded_calls}
        if not calls:
            content = str(message.get("content") or "")
            # Some providers occasionally return an empty assistant message
            # after a tool call (or transiently on a normal completion). Give
            # the model one chance to continue before reporting no response.
            if not content and empty_completion_retries < 2:
                empty_completion_retries += 1
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "上一轮生成没有返回可显示内容。请继续生成简洁的KP回复；"
                            "如果刚才使用了工具，请根据工具结果作出叙事判断，除非条件确实满足，否则不要再次调用触发器工具。"
                        ),
                    }
                )
                continue
            # 重试用尽仍为空：用工具结果兜底，避免返回空内容导致前端报错。
            if not content:
                content = _fallback_tool_summary(tool_messages, direct_messages)
            return AgentCompletionResult(
                content=content,
                token_count=total_token_count if has_token_count else None,
                response_data=response_data,
                tool_messages=tool_messages,
                direct_messages=direct_messages,
                prompt_token_count=prompt_token_count if has_prompt_count else None,
                completion_token_count=completion_token_count if has_completion_count else None,
                cached_token_count=cached_token_count if has_cached_count else None,
                knowledge_usage=knowledge_usage if knowledge_usage["tool_calls"] else None,
            )

        messages.append(_assistant_tool_call_message(message))
        for call in calls:
            function = call.get("function") or {}
            tool_name = str(function.get("name") or "")
            resolved_name, tool = _resolve_tool(tool_name, enabled_by_name)
            if not tool:
                return AgentCompletionResult(error=f"Tool {tool_name} is not enabled for agent {profile.id}")
            if callable(stage_callback):
                stage_callback("tool_call", f"正在执行工具：{resolved_name}")
            if isinstance(tool_state, dict):
                trace = tool_state.setdefault("tool_call_trace", [])
                if isinstance(trace, list):
                    trace.append(resolved_name)
            # 重复调用（同工具同参数）短路：不再执行，只回一条说明让模型继续用上次结果。
            # 随状态变化的读取类工具只在同一轮内去重，跨轮允许重新取最新数据。
            signature = _canonical_call_signature(resolved_name, function.get("arguments"))
            refetchable = resolved_name in STATEFUL_REFETCH_TOOL_NAMES
            already_called = signature in round_signatures or (
                not refetchable and signature in executed_calls
            )
            if dedupe_tool_calls and already_called:
                logger.info("Agent tool %s called again with identical arguments; reused", resolved_name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or tool_name,
                        "name": resolved_name,
                        "content": _dedupe_reuse_notice(resolved_name),
                    }
                )
                continue
            try:
                arguments = _parse_arguments(function.get("arguments"))
                result = tool.handler(arguments, context)
            except Exception as exc:
                result = {"error": str(exc)}
            if isinstance(result, dict) and result.get("error"):
                import logging
                logging.getLogger(__name__).warning("Agent tool %s failed: %s", resolved_name, result.get("error"))

            reported_usage = result.get("knowledge_usage") if isinstance(result, dict) else None
            if isinstance(reported_usage, dict):
                knowledge_usage["ruleset_chunks"] += int(reported_usage.get("ruleset_chunks") or 0)
                knowledge_usage["ruleset_sources"] += int(reported_usage.get("ruleset_sources") or 0)
                knowledge_usage["tool_calls"] += 1
                for key in ("ruleset_ids", "knowledge_versions", "topics", "citations"):
                    values = reported_usage.get(key)
                    if isinstance(values, list):
                        knowledge_usage[key] = list(dict.fromkeys([*knowledge_usage[key], *[str(value) for value in values if value]]))

            if isinstance(tool_state, dict) and isinstance(result, dict):
                tool_state["last_tool"] = resolved_name
                if resolved_name in {"check.roll_room_check", "dice.roll_coc_check"}:
                    tool_state["last_check"] = result

            if isinstance(result, dict) and result.get("direct_message"):
                direct_message = result["direct_message"]
                if isinstance(direct_message, dict):
                    direct_messages.append(direct_message)

            if isinstance(result, dict) and isinstance(result.get("visible_message"), dict):
                tool_messages.append(result["visible_message"])

            # UI 载体字段（visible_message / direct_message）已经收集到 tool_messages /
            # direct_messages 里，回传模型时剥离，避免多轮工具调用重复计入 token。
            model_result = result
            if isinstance(result, dict):
                model_result = {
                    key: value for key, value in result.items() if key not in UI_ONLY_TOOL_RESULT_KEYS
                }
            result_payload, _ = _truncate_tool_result(model_result, max_tool_result_chars)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or tool_name,
                    "name": resolved_name,
                    "content": result_payload,
                }
            )
            # 只登记成功结果：工具报错时允许模型用相同参数重试，避免把失败固定下来。
            if not (isinstance(result, dict) and result.get("error")):
                round_signatures.add(signature)
                if not refetchable:
                    executed_calls[signature] = result_payload

    # 工具轮次用尽：先移除工具、强制模型给出最终叙事；若仍无内容，则用工具结果兜底，
    # 保证玩家一定能收到一条回复，而不是空响应或 500 错误。
    final_payload = {**payload}
    final_payload.pop("tools", None)
    final_payload.pop("tool_choice", None)
    final_payload["messages"] = [
        *messages,
        {
            "role": "system",
            "content": (
                "已达到工具调用上限。请不要再调用任何工具，直接根据以上工具结果生成简洁、"
                "连贯的 KP 叙事回复；若确实没有可叙述的内容，就用一句话说明当前状态。"
            ),
        },
    ]
    _fold_stale_tool_results(messages)
    if callable(stage_callback):
        stage_callback("ai_request", "正在请求 AI")
    try:
        final_response = requester(final_payload)
    except Exception as exc:
        logger.warning("Agent wrap-up request failed: %s", exc)
        final_response = None
    _record_usage(final_response)
    final_content = str(_extract_message(final_response).get("content") or "").strip()
    if not final_content:
        final_content = _fallback_tool_summary(tool_messages, direct_messages)
    return AgentCompletionResult(
        content=final_content,
        token_count=total_token_count if has_token_count else None,
        response_data=final_response if isinstance(final_response, dict) else last_response,
        tool_messages=tool_messages,
        direct_messages=direct_messages,
        prompt_token_count=prompt_token_count if has_prompt_count else None,
        completion_token_count=completion_token_count if has_completion_count else None,
        cached_token_count=cached_token_count if has_cached_count else None,
        knowledge_usage=knowledge_usage if knowledge_usage["tool_calls"] else None,
        rounds_exhausted=True,
        notice=(
            f"（本次回复已达到最大工具调用轮数 {max_tool_rounds}，KP 已停止继续调用工具并收尾。"
            "如需继续，请再发送一条消息。）"
        ),
    )
