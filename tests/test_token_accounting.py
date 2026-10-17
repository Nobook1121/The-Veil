from trpg_server.agents.prompt_builder import build_prompt_layers
from trpg_server.agents.telemetry import load_daily_ai_usage, record_ai_usage
from trpg_server.agents.token_accounting import (
    MESSAGE_OVERHEAD_TOKENS,
    attribute_prompt_segments,
    estimate_tokens,
    format_segment_summary,
)
from trpg_server.agents.config import load_ai_runtime_config


def test_estimate_tokens_counts_cjk_per_character_and_ascii_per_quarter():
    assert estimate_tokens("") == 0
    # 纯 CJK：每字约 1 token，不应因向上取整多算。
    assert estimate_tokens("你好世界") == 4
    # 纯 ASCII："hello world" 共 11 字符 → ceil(11/4) = 3。
    assert estimate_tokens("hello world") == 3


def test_attribute_prompt_segments_groups_by_label_and_adds_message_overhead():
    messages = [
        {"role": "system", "content": "abcde"},
        {"role": "user", "content": "你好"},
    ]
    totals = attribute_prompt_segments(messages, ["rules", "user_input"])
    # "abcde" 5 字符 → ceil(5/4) = 2；"你好" 2 字 → 2。
    assert totals["rules"] == 2 + MESSAGE_OVERHEAD_TOKENS
    assert totals["user_input"] == 2 + MESSAGE_OVERHEAD_TOKENS


def test_attribute_prompt_segments_defaults_missing_labels_to_unknown():
    totals = attribute_prompt_segments([{"role": "user", "content": "x"}], [])
    assert totals == {"unknown": 1 + MESSAGE_OVERHEAD_TOKENS}


def test_prompt_layers_segments_align_with_messages():
    result = build_prompt_layers(
        global_rules="GLOBAL",
        scenario={"id": "s", "core": "CORE"},
        scene={"id": "scene-1", "content": "SCENE"},
        room_state={"room_id": "room-1"},
        history=[{"role": "assistant", "content": "old"}],
        user_input="look",
        retrieval_results=[{"text": "brass key", "spoiler_level": 0}],
        room_snapshot_message="SNAPSHOT",
    )
    # 分段标签与消息一一对应，才能保证归因不串位。
    assert len(result.segments) == len(result.messages)
    assert result.segments[0] == "rules"
    assert result.segments[1] == "scenario_static"
    assert result.segments[2] == "scene_static"
    assert result.segments[-1] == "user_input"
    assert "knowledge" in result.segments
    assert "room_state" in result.segments
    assert "history" in result.segments


def test_daily_usage_aggregates_prompt_segments(tmp_path):
    record_ai_usage(
        tmp_path,
        {
            "agent_id": "kp",
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_segments": {"rules": 60, "history": 40},
        },
    )
    report = load_daily_ai_usage(tmp_path)
    assert report["prompt_segments"]["rules"] == 60
    assert report["prompt_segments"]["history"] == 40
    assert report["roles"]["kp"]["prompt_segments"]["rules"] == 60


def test_format_segment_summary_uses_labels_and_is_compact():
    text = format_segment_summary({"rules": 60, "unknown_key": 40})
    assert "系统规则=60" in text
    assert "unknown_key=40" in text
    assert format_segment_summary({}) == "（无分段数据）"


def test_record_prompt_segments_toggle_is_parsed_from_config(tmp_path):
    config_file = tmp_path / "general.toml"
    config_file.write_text("[ai]\nrecord_prompt_segments = false\n", encoding="utf-8")
    assert load_ai_runtime_config(tmp_path).record_prompt_segments is False

    config_file.write_text("[ai]\nrecord_prompt_segments = true\n", encoding="utf-8")
    assert load_ai_runtime_config(tmp_path).record_prompt_segments is True


def test_cache_and_tool_dedupe_settings_are_parsed_from_config(tmp_path):
    config_file = tmp_path / "general.toml"
    config_file.write_text(
        "[ai.prompt]\ndedupe_tool_calls = false\n"
        "[ai.cache]\nsemantic_enabled = false\nsemantic_ttl_seconds = 600\nexact_ttl_seconds = 900\n",
        encoding="utf-8",
    )
    config = load_ai_runtime_config(tmp_path)
    assert config.dedupe_tool_calls is False
    assert config.semantic_cache_enabled is False
    assert config.semantic_cache_ttl == 600
    assert config.exact_cache_ttl == 900

    # 越界值被夹回合理区间，避免设置页填错导致缓存永不失效。
    config_file.write_text(
        "[ai.cache]\nsemantic_ttl_seconds = 1\nexact_ttl_seconds = 99999999\n",
        encoding="utf-8",
    )
    clamped = load_ai_runtime_config(tmp_path)
    assert clamped.semantic_cache_ttl == 30
    assert clamped.exact_cache_ttl == 86400


def test_console_ai_usage_command_reports_segments(tmp_path):
    from trpg_server.console_commands import execute_console_command

    record_ai_usage(
        tmp_path,
        {
            "agent_id": "kp",
            "prompt_tokens": 120,
            "completion_tokens": 20,
            "total_tokens": 140,
            "prompt_segments": {"rules": 80, "user_input": 40},
        },
    )

    result = execute_console_command("ai usage 1", log_dir=tmp_path)

    assert result.ok
    assert "系统规则=80" in result.message
    assert "kp" in result.message
    assert "140" in result.message


def test_console_ai_usage_rejects_invalid_days(tmp_path):
    from trpg_server.console_commands import execute_console_command

    invalid = execute_console_command("ai usage 0", log_dir=tmp_path)
    non_numeric = execute_console_command("ai usage abc", log_dir=tmp_path)

    assert not invalid.ok
    assert not non_numeric.ok