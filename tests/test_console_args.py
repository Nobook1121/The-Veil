"""共享控制台参数解析器的回归测试。"""

from __future__ import annotations

from trpg_server.console_args import ConsoleArgSpec, first_positional, parse_console_args

SPEC = ConsoleArgSpec.of(
    flags={"yes", "persist", "weapon"},
    values={"qty", "reason"},
)


def test_pure_word_flags_and_values():
    parsed = parse_console_args(["item", "手电筒", "qty", "2", "weapon", "persist"], SPEC)

    assert parsed.positionals == ["item", "手电筒"]
    assert parsed.options == {"qty": "2"}
    assert {"--qty=2", "--weapon", "--persist"} <= parsed.flags


def test_negative_numbers_stay_positional():
    parsed = parse_console_args(["attr", "-5"], SPEC)

    assert parsed.positionals == ["attr", "-5"]
    assert not parsed.flags


def test_legacy_forms_stay_compatible():
    parsed = parse_console_args(
        ["item", "手电筒", "--qty=2", "--weapon", "--reason", "剧情"],
        SPEC,
    )

    assert parsed.positionals == ["item", "手电筒"]
    assert parsed.options == {"qty": "2", "reason": "剧情"}
    assert {"--qty=2", "--weapon", "--reason=剧情"} <= parsed.flags


def test_first_positional_is_never_eaten_by_value_option():
    assert first_positional(["--yes", "status"]) == "status"
    parsed = parse_console_args(["status", "json"], SPEC)

    assert parsed.positionals == ["status", "json"]


def test_trailing_option_strips_trailing_flag():
    parsed = parse_console_args(["finish", "reason", "撤退", "yes"], SPEC)

    assert parsed.positionals == ["finish"]
    assert parsed.options == {"reason": "撤退"}
    assert "--yes" in parsed.flags
