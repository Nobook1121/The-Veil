"""提示词分层顺序快照：拆分 chat 流水线前锁死 layers/segments 的注入顺序。"""
from trpg_server.agents.prompt_builder import build_prompt_layers

BASE_KWARGS = {
    "global_rules": "GLOBAL",
    "scenario": {"id": "sc-1", "version": "2", "title": "剧本"},
    "scene": {"id": "makefile", "version": "1"},
    "room_state": {"clock": {"turn": 3}},
    "history": [
        {"role": "user", "content": "我推开门"},
        {"role": "assistant", "content": "门后是走廊"},
    ],
    "user_input": "继续前进",
}


def test_prompt_layer_order_with_all_optional_blocks():
    layers = build_prompt_layers(
        **BASE_KWARGS,
        rules_version="3",
        retrieval_results=[{"chunk_id": "c1", "text": "剧情块", "metadata": {"title": "开场"}}],
        ruleset_results=[{"ruleset_id": "coc7", "chunk_id": "r1", "text": "规则块"}],
        narrative_summary="位置：门厅",
        opening_pack="开场包",
        room_snapshot_message="房间快照",
        system_directive="系统授权",
        memory_context="长期记忆",
        cache_variant="latex",
    )

    assert layers.segments == (
        "rules",
        "scenario_static",
        "scene_static",
        "knowledge",
        "knowledge",
        "room_state",
        "opening_pack",
        "narrative_state",
        "memory",
        "room_state",
        "system_directive",
        "history",
        "history",
        "user_input",
    )
    assert [message["role"] for message in layers.messages] == [
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert layers.messages[0]["content"] == "GLOBAL"
    assert layers.messages[3]["content"].startswith("External ruleset references")
    assert layers.messages[4]["content"].startswith("Knowledge retrieval:")
    assert layers.messages[5]["content"].startswith("房间动态状态：")
    assert layers.messages[10]["content"] == "系统授权"
    assert layers.messages[-1]["content"].startswith("---->USER_INPUT<----")
    assert layers.static_prefix == "\n".join(message["content"] for message in layers.messages[:3])
    assert layers.cache_key.startswith("scenario:sc-1@2:scene:makefile@1:rules:3")


def test_prompt_layer_order_minimal_input():
    layers = build_prompt_layers(**BASE_KWARGS)

    assert layers.segments == (
        "rules",
        "scenario_static",
        "scene_static",
        "room_state",
        "history",
        "history",
        "user_input",
    )
    assert len(layers.messages) == 7
