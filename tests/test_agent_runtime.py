from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.profiles import AgentProfile
from trpg_server.agents.runtime import _extract_usage_counts, run_agent_completion
from trpg_server.agents.tools.base import AgentTool, ToolRegistry


def test_usage_counts_read_provider_specific_cache_fields():
    """缓存字段各家命名不同：OpenAI 用 prompt_tokens_details.cached_tokens、
    DeepSeek 用顶层 prompt_cache_hit_tokens、Anthropic 用 cache_read_input_tokens。"""
    assert _extract_usage_counts({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}}) == (100, None, 0)
    # 合法的 0 不能被当成"没值"而穿透到后面的字段
    assert _extract_usage_counts({"usage": {"prompt_tokens": 100, "cached_tokens": 0, "prompt_cache_hit_tokens": 80}}) == (100, None, 0)
    assert _extract_usage_counts({"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}}) == (100, None, 80)
    # 只有拆分字段时自行还原 prompt_tokens
    assert _extract_usage_counts({"usage": {"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}}) == (100, None, 80)
    assert _extract_usage_counts({"usage": {"input_tokens_details": {"cache_read_input_tokens": 64}}}) == (None, None, 64)
    assert _extract_usage_counts({"usage": {}}) == (None, None, None)


def test_runtime_folds_only_stale_refetchable_tool_results():
    """较早的「可重取」工具结果会被折叠以省 token；检定结果永不折叠。"""
    from trpg_server.agents.runtime import FOLDED_TOOL_RESULT_NOTICE

    class SequenceRequester:
        def __init__(self, rounds):
            self.rounds = rounds
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) <= self.rounds:
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"call-{len(self.calls)}", "type": "function",
                     "function": {"name": self.tool_name, "arguments": "{}"}}
                ]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    def run(tool_name):
        requester = SequenceRequester(rounds=3)
        requester.tool_name = tool_name
        tool = AgentTool(
            name=tool_name,
            description="tool",
            parameters={"type": "object", "properties": {}},
            handler=lambda arguments, context: {"payload": "X" * 50},
        )
        run_agent_completion(
            requester=requester,
            base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "go"}]},
            profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=[tool_name]),
            registry=ToolRegistry([tool]),
            context=AgentRequestContext(room_id="room-1"),
        )
        return [message for message in requester.calls[-1]["messages"] if message.get("role") == "tool"]

    refetchable = run("room.get_character_cards")
    assert len(refetchable) == 3
    assert refetchable[0]["content"] == FOLDED_TOOL_RESULT_NOTICE
    assert refetchable[1]["content"] != FOLDED_TOOL_RESULT_NOTICE
    assert refetchable[2]["content"] != FOLDED_TOOL_RESULT_NOTICE

    # 检定结果必须保持原文，否则 KP 会看不到骰点而瞎编结果
    checks = run("check.roll_room_check")
    assert len(checks) == 3
    assert all(message["content"] != FOLDED_TOOL_RESULT_NOTICE for message in checks)


def test_runtime_short_circuits_repeated_identical_tool_calls():
    """同一轮内用相同参数重复调用同一工具：只执行一次，第二次回一条复用说明。

    这既避免重复掷骰/重复触发把状态搞乱，也省掉一次多余的执行与往返。
    """
    executions = {"count": 0}

    def handler(arguments, context):
        executions["count"] += 1
        return {"value": arguments.get("value"), "seq": executions["count"]}

    tool = AgentTool(
        name="dice.roll_coc_check",
        description="roll",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=handler,
    )

    class TwoIdenticalCallsRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "call-1", "type": "function", "function": {"name": "dice.roll_coc_check", "arguments": "{\"value\":\"listen\"}"}},
                                    {"id": "call-2", "type": "function", "function": {"name": "dice.roll_coc_check", "arguments": "{\"value\":\"listen\"}"}},
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    requester = TwoIdenticalCallsRequester()
    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "listen"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["dice.roll_coc_check"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )
    assert executions["count"] == 1
    assert result.content == "done"
    tool_messages = [m for m in requester.calls[-1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 2
    assert "reused" in tool_messages[1]["content"]

    # 关闭短路后两次调用都会真正执行（保留可配置性）。
    executions["count"] = 0
    run_agent_completion(
        requester=TwoIdenticalCallsRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "listen"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["dice.roll_coc_check"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
        dedupe_tool_calls=False,
    )
    assert executions["count"] == 2


def test_runtime_allows_refetching_stateful_tools_across_rounds():
    """随状态变化的读取类工具（房间/剧本快照）跨轮允许重新调用，拿到最新数据。

    这是「省 token」不能牺牲剧本遵守度的关键：场景推进后 KP 必须能重新取快照。
    """
    executions = {"count": 0}

    def handler(arguments, context):
        executions["count"] += 1
        return {"snapshot": executions["count"]}

    tool = AgentTool(
        name="room.get_room_snapshot",
        description="snapshot",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )

    class RefetchRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) <= 2:
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"call-{len(self.calls)}", "type": "function", "function": {"name": "room.get_room_snapshot", "arguments": "{}"}}
                ]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    run_agent_completion(
        requester=RefetchRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "look"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["room.get_room_snapshot"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )
    assert executions["count"] == 2


class FakeRequester:
    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if len(self.calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "test.echo", "arguments": "{\"value\":\"hello\"}"},
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}], "usage": {"total_tokens": 9}}


def test_runtime_executes_enabled_tool_and_finishes():
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=lambda arguments, context: {"echo": arguments["value"], "room_id": context.room_id},
    )
    requester = FakeRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "final answer"
    assert result.token_count == 9
    assert len(requester.calls) == 2
    assert requester.calls[0]["tools"][0]["function"]["name"] == "test.echo"
    assert requester.calls[1]["messages"][-1]["role"] == "tool"


def test_runtime_records_request_rounds_and_tool_trace():
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=lambda arguments, context: {"echo": arguments["value"]},
    )
    context = AgentRequestContext(room_id="room-1")
    result = run_agent_completion(
        requester=FakeRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=context,
    )
    assert result.content == "final answer"
    assert context.tool_state["agent_request_rounds"] == 2
    assert context.tool_state["tool_call_trace"] == ["test.echo"]


def test_runtime_reports_tool_stage_through_context_callback():
    stages = []
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True},
    )

    class Requester:
        def __init__(self):
            self.calls = 0

        def __call__(self, payload):
            self.calls += 1
            if self.calls == 1:
                return {"choices": [{"message": {"tool_calls": [{"id": "call", "function": {"name": "test.echo", "arguments": "{}"}}]}}]}
            return {"choices": [{"message": {"content": "done"}}]}

    context = AgentRequestContext(room_id="room-1")
    context.tool_state["thinking_stage_callback"] = lambda stage, label: stages.append((stage, label))
    result = run_agent_completion(
        requester=Requester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=context,
    )

    assert result.content == "done"
    assert stages == [
        ("ai_request", "正在请求 AI"),
        ("tool_call", "正在执行工具：test.echo"),
        ("ai_request", "正在请求 AI"),
    ]


def test_runtime_accumulates_token_count_across_multiple_tool_rounds():
    class MultiRoundRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "test.echo", "arguments": "{}"},
                            }],
                        }
                    }],
                    "usage": {"total_tokens": 11},
                }
            if len(self.calls) == 2:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "test.echo", "arguments": "{}"},
                            }],
                        }
                    }],
                    "usage": {"total_tokens": 13},
                }
            return {
                "choices": [{"message": {"role": "assistant", "content": "final answer"}}],
                "usage": {"total_tokens": 17},
            }

    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True},
    )

    result = run_agent_completion(
        requester=MultiRoundRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "final answer"
    assert result.token_count == 41


def test_runtime_returns_tool_result_to_model_and_collects_visible_message():
    class VisibleToolRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "test.check", "arguments": "{}"},
                            }],
                        }
                    }]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "continued"}}]}

    tool_result = {
        "roll": 17,
        "summary": "侦查 d%: [17] = 17 / 50 成功",
        "visible_message": {
            "type": "dice",
            "sender_name": "骰娘",
            "content": "侦查 d%: [17] = 17 / 50 成功",
        },
    }
    requester = VisibleToolRequester()
    tool = AgentTool(
        name="test.check",
        description="Check",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: tool_result,
    )

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "check"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.check"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "continued"
    assert result.tool_messages == [tool_result["visible_message"]]
    assert '"roll": 17' in requester.calls[1]["messages"][-1]["content"]
    assert "侦查 d%: [17] = 17 / 50 成功" in requester.calls[1]["messages"][-1]["content"]
    # visible_message 是 UI 载体，已由 tool_messages 承载，不应再回传给模型。
    assert "visible_message" not in requester.calls[1]["messages"][-1]["content"]


def test_runtime_collects_knowledge_usage_from_executed_tool():
    class KnowledgeRequester:
        def __init__(self):
            self.calls = 0

        def __call__(self, payload):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "knowledge-call",
                                "type": "function",
                                "function": {"name": "knowledge.search_ruleset", "arguments": "{\"query\":\"角色死亡\"}"},
                            }],
                        }
                    }]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "handled"}}]}

    tool = AgentTool(
        name="knowledge.search_ruleset",
        description="Search rules",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {
            "used": True,
            "references": [{"text": "respect player agency"}],
            "knowledge_usage": {
                "ruleset_chunks": 2,
                "ruleset_sources": 1,
                "ruleset_ids": ["coc7"],
                "knowledge_versions": ["4"],
                "topics": ["keeper_guidance"],
                "citations": ["Keeper Rulebook p.42"],
            },
        },
    )

    result = run_agent_completion(
        requester=KnowledgeRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["knowledge.search_ruleset"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "handled"
    assert result.knowledge_usage == {
        "ruleset_chunks": 2,
        "ruleset_sources": 1,
        "ruleset_ids": ["coc7"],
        "knowledge_versions": ["4"],
        "topics": ["keeper_guidance"],
        "citations": ["Keeper Rulebook p.42"],
        "tool_calls": 1,
    }


def test_runtime_continues_after_direct_message_tool_and_collects_both_messages():
    class TriggerRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call-trigger",
                                "type": "function",
                                "function": {"name": "test.trigger", "arguments": "{\"trigger_id\": 2}"},
                            }],
                        },
                    }],
                }
            return {"choices": [{"message": {"role": "assistant", "content": "KP decision"}}]}

    direct_message = {
        "type": "trigger",
        "sender_name": "note",
        "content": "hidden note",
    }
    visible_message = {
        "type": "dice",
        "sender_name": "dice",
        "content": "check failed",
    }
    requester = TriggerRequester()
    tool = AgentTool(
        name="test.trigger",
        description="Reveal trigger",
        parameters={"type": "object", "properties": {"trigger_id": {"type": "integer"}}},
        handler=lambda arguments, context: {
            "direct_message": direct_message,
            "visible_message": visible_message,
            "trigger_id": arguments["trigger_id"],
        },
    )

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "check"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.trigger"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert len(requester.calls) == 2
    assert result.content == "KP decision"
    assert result.tool_messages == [visible_message]
    assert result.direct_messages == [direct_message]


def test_runtime_sanitizes_assistant_content_when_tool_calls_are_present():
    class NoisyToolRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "noisy protocol text that must not be replayed",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "test.echo", "arguments": "{\"value\":\"hello\"}"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}

    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=lambda arguments, context: {"echo": arguments["value"]},
    )
    requester = NoisyToolRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    replayed_assistant_message = requester.calls[1]["messages"][-2]
    assert result.content == "final answer"
    assert replayed_assistant_message["role"] == "assistant"
    assert replayed_assistant_message["content"] == ""
    assert "noisy protocol text" not in str(requester.calls[1]["messages"])


def test_runtime_recovers_tool_calls_embedded_in_json_content():
    """部分厂商在强制 JSON 输出时会把 tool_calls 写进正文，必须还原并执行。"""

    class EmbeddedCallRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"reply": "你推开了门。",'
                                    ' "tool_calls": [{"name": "test.echo", "arguments": {"value": "hello"}}]}'
                                ),
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}

    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=lambda arguments, context: {"echo": arguments["value"]},
    )
    requester = EmbeddedCallRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert len(requester.calls) == 2
    assert result.content == "final answer"
    tool_message = requester.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert '"hello"' in tool_message["content"]


def test_runtime_treats_non_object_tool_arguments_as_empty_dict():
    class NumericArgumentRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "test.echo", "arguments": "10"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}

    seen_arguments = []
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: seen_arguments.append(arguments) or {"ok": True},
    )

    result = run_agent_completion(
        requester=NumericArgumentRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "final answer"
    assert seen_arguments == [{}]


def test_runtime_resolves_underscore_tool_name_aliases():
    class AliasRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "room_get_room_snapshot",
                                            "arguments": "{\"include_inactive\":false}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}

    seen_calls = []
    tool = AgentTool(
        name="room.get_room_snapshot",
        description="Load snapshot",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: seen_calls.append(arguments) or {"ok": True},
    )

    result = run_agent_completion(
        requester=AliasRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["room.get_room_snapshot"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.content == "final answer"
    assert seen_calls == [{"include_inactive": False}]


def test_runtime_rejects_unauthorized_tool():
    requester = FakeRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "@KP hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=[]),
        registry=ToolRegistry([]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.error == "Tool test.echo is not enabled for agent kp"


def test_build_agent_context_resolves_room_dir_and_scenarios_dir(tmp_path):
    from trpg_server.agents.context import build_agent_context

    rooms_dir = tmp_path / "rooms"
    scenarios_dir = tmp_path / "scenarios"
    room_dir = rooms_dir / "room-1"
    room_dir.mkdir(parents=True)
    scenarios_dir.mkdir()

    context = build_agent_context(
        room_id="room-1",
        rooms_dir=rooms_dir,
        scenarios_dir=scenarios_dir,
        user_id=7,
        agent_id="kp",
    )

    assert context.room_id == "room-1"
    assert context.room_dir == room_dir
    assert context.scenarios_dir == scenarios_dir
    assert context.user_id == 7


def test_runtime_retries_once_when_trigger_tool_is_followed_by_empty_completion():
    class EmptyAfterToolRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-trigger",
                        "type": "function",
                        "function": {"name": "test.trigger", "arguments": "{}"},
                    }],
                }}]}
            if len(self.calls) == 2:
                return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "KP continued"}}]}

    requester = EmptyAfterToolRequester()
    tool = AgentTool(
        name="test.trigger",
        description="Reveal trigger",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"direct_message": {"type": "trigger", "content": "secret"}},
    )

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "check"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.trigger"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert len(requester.calls) == 3
    assert result.content == "KP continued"


def test_runtime_requests_final_completion_without_tools_after_budget():
    class BudgetRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            # 带工具时持续请求工具调用，直到轮次用尽。
            if "tools" in payload:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "test.echo", "arguments": "{}"},
                    }],
                }}]}
            # 收尾请求（无工具）返回最终叙事。
            return {"choices": [{"message": {"role": "assistant", "content": "最终叙事"}}]}

    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True},
    )
    requester = BudgetRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
        max_tool_rounds=2,
    )

    assert result.error is None
    assert result.content == "最终叙事"
    # 收尾请求必须移除工具，强制模型给出文本回复。
    assert "tools" not in requester.calls[-1]
    assert "tool_choice" not in requester.calls[-1]


def test_runtime_falls_back_to_tool_summary_when_budget_exhausted_and_empty():
    class AlwaysToolRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            if "tools" in payload:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "test.echo", "arguments": "{}"},
                    }],
                }}]}
            # 收尾请求也返回空内容。
            return {"choices": [{"message": {"role": "assistant", "content": ""}}]}

    visible_message = {"type": "dice", "sender_name": "dice", "content": "check result"}
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True, "visible_message": visible_message},
    )
    requester = AlwaysToolRequester()

    result = run_agent_completion(
        requester=requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
        max_tool_rounds=2,
    )

    assert result.error is None
    # 即使模型始终不返回文本，也必须有兜底回复而不是空内容。
    assert result.content
    assert result.tool_messages and result.tool_messages[0] == visible_message


def test_runtime_flags_rounds_exhausted_with_visible_notice():
    class AlwaysToolRequester:
        def __call__(self, payload):
            if "tools" in payload:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "test.echo", "arguments": "{}"},
                    }],
                }}]}
            return {"choices": [{"message": {"role": "assistant", "content": "收尾叙事"}}]}

    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True},
    )

    result = run_agent_completion(
        requester=AlwaysToolRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
        max_tool_rounds=2,
    )

    assert result.error is None
    # 超过最大轮数时必须带出明确说明，供聊天框与日志展示。
    assert result.rounds_exhausted is True
    assert result.notice and "最大工具调用轮数 2" in result.notice


def test_runtime_keeps_partial_result_and_notice_when_request_fails():
    class FlakyRequester:
        def __init__(self):
            self.calls = 0

        def __call__(self, payload):
            self.calls += 1
            if self.calls == 1:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "test.echo", "arguments": "{}"},
                    }],
                }}]}
            raise RuntimeError("AI 平台请求超时（等待 300 秒未响应）")

    visible_message = {"type": "dice", "sender_name": "dice", "content": "check result"}
    tool = AgentTool(
        name="test.echo",
        description="Echo value",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments, context: {"ok": True, "visible_message": visible_message},
    )

    result = run_agent_completion(
        requester=FlakyRequester(),
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=["test.echo"]),
        registry=ToolRegistry([tool]),
        context=AgentRequestContext(room_id="room-1"),
    )

    # 已经拿到的工具结果不能因为后续请求失败而被丢弃。
    assert result.error is None
    assert result.content
    assert result.tool_messages == [visible_message]
    assert result.notice and "超时" in result.notice


def test_runtime_reports_error_when_first_request_fails():
    def failing_requester(payload):
        raise RuntimeError("AI 平台请求超时（等待 300 秒未响应）")

    result = run_agent_completion(
        requester=failing_requester,
        base_payload={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
        profile=AgentProfile(id="kp", name="KP", prompt="prompt", tool_names=[]),
        registry=ToolRegistry([]),
        context=AgentRequestContext(room_id="room-1"),
    )

    assert result.error and "超时" in result.error
    assert result.notice is None
