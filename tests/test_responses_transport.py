from trpg_server.ai_capabilities import probe_responses_api, responses_endpoint
from trpg_server.agents.responses_transport import (
    ResponsesRequester,
    messages_to_input,
    response_to_chat_completion,
    tools_to_responses,
)
import requests


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def test_responses_endpoint_derives_from_chat_completions_url():
    assert responses_endpoint("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/responses"
    assert responses_endpoint("https://api.example.com/v1/") == "https://api.example.com/v1/responses"
    assert responses_endpoint("") == ""


def test_probe_reports_supported_only_when_previous_response_id_is_accepted():
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        if "previous_response_id" in json:
            return FakeResponse(200, {"id": "resp-2", "output": []})
        return FakeResponse(200, {"id": "resp-1", "output": []})

    result = probe_responses_api("https://api.example.com/v1/chat/completions", "key", "model-x", post=post)

    assert result["supported"] is True
    assert result["endpoint"] == "https://api.example.com/v1/responses"
    assert sent[1]["previous_response_id"] == "resp-1"


def test_probe_reports_unsupported_when_previous_response_id_is_rejected():
    def post(url, headers=None, json=None, timeout=None):
        if "previous_response_id" in json:
            return FakeResponse(400, {"error": {"message": "unknown parameter"}})
        return FakeResponse(200, {"id": "resp-1", "output": []})

    result = probe_responses_api("https://api.example.com/v1/chat/completions", "key", "model-x", post=post)

    assert result["supported"] is False
    assert "previous_response_id" in result["detail"]


def test_probe_reports_unsupported_when_endpoint_returns_404():
    result = probe_responses_api(
        "https://api.example.com/v1/chat/completions",
        "key",
        "model-x",
        post=lambda *args, **kwargs: FakeResponse(404, {"error": {"message": "not found"}}),
    )

    assert result["supported"] is False
    assert result["status"] == 404


def test_probe_requires_a_model():
    called = []

    def post(*args, **kwargs):
        called.append(True)
        return FakeResponse(200, {"id": "resp-1", "output": []})

    result = probe_responses_api("https://api.example.com/v1/chat/completions", "key", "", post=post)

    assert result["supported"] is False
    assert called == []


def test_tools_to_responses_flattens_function_fields():
    converted = tools_to_responses(
        [
            {
                "type": "function",
                "function": {"name": "check.roll_room_check", "description": "d", "parameters": {"type": "object", "properties": {}}},
            }
        ]
    )

    assert converted == [
        {"type": "function", "name": "check.roll_room_check", "description": "d", "parameters": {"type": "object", "properties": {}}}
    ]


def test_messages_to_input_expands_tool_calls_and_results():
    items = messages_to_input(
        [
            {"role": "system", "content": "SYS"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "check.roll_room_check", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "check.roll_room_check", "content": '{"success": true}'},
        ]
    )

    assert items[0] == {"role": "system", "content": "SYS"}
    assert items[1] == {"type": "function_call", "call_id": "call-1", "name": "check.roll_room_check", "arguments": "{}"}
    assert items[2] == {"type": "function_call_output", "call_id": "call-1", "output": '{"success": true}'}


def test_response_to_chat_completion_maps_text_tool_calls_and_usage():
    result = response_to_chat_completion(
        {
            "id": "resp-9",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "调查员发现了线索。"}]},
                {"type": "function_call", "call_id": "call-7", "name": "check.roll_room_check", "arguments": '{"player_name": "alice"}'},
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 8,
                "total_tokens": 128,
                "input_tokens_details": {"cached_tokens": 64},
            },
        }
    )

    message = result["choices"][0]["message"]
    assert message["content"] == "调查员发现了线索。"
    assert message["tool_calls"][0]["id"] == "call-7"
    assert message["tool_calls"][0]["function"]["name"] == "check.roll_room_check"
    # 缓存字段必须能被 runtime 的 _extract_usage_counts 读到
    assert result["usage"]["prompt_tokens"] == 120
    assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 64


def test_responses_requester_sends_only_the_delta_after_the_first_round():
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return FakeResponse(
            200,
            {
                "id": f"resp-{len(sent)}",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {},
            },
        )

    fallback_calls = []
    requester = ResponsesRequester(
        "https://x/v1/responses",
        {},
        "model-x",
        30,
        lambda payload: fallback_calls.append(payload) or {"choices": [{"message": {"content": "fb"}}]},
        post=post,
    )

    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    requester(
        {
            "messages": messages,
            "tools": [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}],
        }
    )
    messages.append(
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
    )
    messages.append({"role": "tool", "tool_call_id": "c1", "name": "t", "content": "{}"})
    requester({"messages": messages})

    assert "previous_response_id" not in sent[0]
    assert len(sent[0]["input"]) == 2
    assert sent[0]["tools"][0]["name"] == "t"
    assert sent[1]["previous_response_id"] == "resp-1"
    # 第二轮只把新增的助手工具调用与工具结果发过去，静态层与历史不再重复
    assert [item.get("type") or item.get("role") for item in sent[1]["input"]] == ["function_call", "function_call_output"]
    assert fallback_calls == []


def test_responses_requester_latches_to_fallback_after_a_failure():
    def post(*args, **kwargs):
        raise RuntimeError("boom")

    fallback_calls = []
    requester = ResponsesRequester(
        "https://x/v1/responses",
        {},
        "model-x",
        30,
        lambda payload: fallback_calls.append(payload) or {"choices": [{"message": {"content": "fb"}}]},
        post=post,
    )

    first = requester({"messages": [{"role": "user", "content": "hi"}]})
    second = requester({"messages": [{"role": "user", "content": "hi"}]})

    assert first["choices"][0]["message"]["content"] == "fb"
    assert second["choices"][0]["message"]["content"] == "fb"
    assert len(fallback_calls) == 2


def test_responses_requester_retries_transient_timeout_before_falling_back():
    """一次瞬时超时不应立刻锁定回退，否则会白白丢掉增量传输的省 token 收益。"""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            raise requests.exceptions.Timeout("boom")
        return FakeResponse(
            200,
            {
                "id": "resp-1",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {},
            },
        )

    fallback_calls = []
    requester = ResponsesRequester(
        "https://x/v1/responses",
        {},
        "model-x",
        30,
        lambda payload: fallback_calls.append(payload) or {"choices": [{"message": {"content": "fb"}}]},
        post=post,
        max_retries=1,
    )

    result = requester({"messages": [{"role": "user", "content": "hi"}]})

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2
    assert fallback_calls == []