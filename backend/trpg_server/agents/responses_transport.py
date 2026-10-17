"""Responses API（``/responses``）传输层：让多轮工具调用复用 ``previous_response_id``。

本项目其余部分都按 chat/completions 的形状（``choices[0].message``）读写结果，
所以这里做双向翻译：把 ``messages`` 的增量转成 ``input``，再把响应转回 chat 形状。

只有平台被探测为支持、且用户在设置里显式开启时才会启用；一旦这条路径出现任何
异常，会立即锁定并回退到原本的 chat/completions 请求，保证聊天不会因为该能力中断。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from trpg_server.agents.http_retry import post_with_retry

logger = logging.getLogger(__name__)


def tools_to_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """chat/completions 的 tools 结构是 ``{"type":"function","function":{...}}``，
    Responses API 要求把函数字段平铺到同一层。"""
    converted: list[dict[str, Any]] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if not function:
            converted.append(tool)
            continue
        converted.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 chat 消息转成 Responses API 的 ``input`` 项。

    助手发起的工具调用要展开成 ``function_call`` 项，工具结果要变成
    ``function_call_output``，其余普通消息直接按 role/content 传递。
    """
    items: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                items.append({"role": "assistant", "content": content})
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or function.get("name"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "{}",
                    }
                )
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or message.get("name"),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        items.append({"role": role, "content": message.get("content") or ""})
    return items


def response_to_chat_completion(data: dict[str, Any] | None) -> dict[str, Any]:
    """把 Responses API 响应翻译回 chat/completions 形状，供 runtime 直接消费。"""
    data = data if isinstance(data, dict) else {}
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in data.get("output") if isinstance(data.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"},
                }
            )
        elif item_type == "message":
            for part in item.get("content") if isinstance(item.get("content"), list) else []:
                if isinstance(part, dict) and str(part.get("type")) in {"output_text", "text"}:
                    text_parts.append(str(part.get("text") or ""))

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    chat_usage: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        chat_usage["prompt_tokens_details"] = {"cached_tokens": details.get("cached_tokens")}
    return {"choices": [{"message": message}], "usage": chat_usage, "id": data.get("id")}


class ResponsesRequester:
    """带状态的请求器：首轮发送全部 input，之后只发送 messages 的增量。

    复用服务端的会话（``previous_response_id``），避免每轮把静态层、历史、
    工具结果整包重发。构造时传入的 ``fallback`` 是原本的 chat/completions 请求器，
    一旦本路径失败就整体回退给它。

    请求本身走 :func:`trpg_server.agents.http_retry.post_with_retry`：瞬时网络抖动
    会先在预算内重试，只有重试耗尽才锁定并回退，避免一次卡顿就永久丢掉增量收益。
    """

    def __init__(
        self,
        endpoint: str,
        headers: dict[str, str],
        model: str,
        timeout: int,
        fallback: Callable[[dict[str, Any]], dict[str, Any]],
        post: Callable[..., Any] | None = None,
        max_retries: int = 0,
    ) -> None:
        self.endpoint = endpoint
        self.headers = headers
        self.model = model
        self.timeout = timeout
        self._fallback = fallback
        self._post = post
        self._max_retries = max(0, max_retries)
        self._previous_response_id: str | None = None
        self._sent = 0
        self._disabled = False

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._disabled:
            return self._fallback(payload)
        try:
            return self._request(payload)
        except Exception as exc:
            logger.warning("Responses API request failed, falling back to chat/completions: %s", exc)
            self._disabled = True
            return self._fallback(payload)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        delta = messages[self._sent :]
        body: dict[str, Any] = {"model": self.model, "input": messages_to_input(delta), "store": True}
        if self._previous_response_id:
            body["previous_response_id"] = self._previous_response_id
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            body["tools"] = tools_to_responses(tools)
            body["tool_choice"] = payload.get("tool_choice") or "auto"
        extra_body = payload.get("extra_body")
        if isinstance(extra_body, dict):
            body.update(extra_body)

        logger.info("Responses API request: %s", json.dumps({"round_input_items": len(body["input"]), "has_previous": bool(self._previous_response_id)}, ensure_ascii=False))
        response = post_with_retry(
            self.endpoint,
            headers=self.headers,
            payload=body,
            timeout=self.timeout,
            max_retries=self._max_retries,
            post=self._post,
        )
        if not response.ok:
            raise RuntimeError(f"Responses API returned HTTP {response.status_code}")

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Responses API returned a non-object body")
        self._sent = len(messages)
        if data.get("id"):
            self._previous_response_id = str(data["id"])
        return response_to_chat_completion(data)