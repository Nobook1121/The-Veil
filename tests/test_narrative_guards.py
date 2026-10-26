"""输出侧护栏：KP-only / 未揭示专名删句、揭示后放行与降级策略。"""
from __future__ import annotations

from trpg_server.agents.narrative.guards import (
    GuardPolicy,
    apply_output_guard,
    forbidden_entries,
    scan_output,
)
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(nodes=[
        NarrativeNode(id="room_hall", kind="location", title="门厅"),
        NarrativeNode(id="secret", kind="clue", title="黑幕真相", visibility="kp_only"),
        NarrativeNode(id="hidden", kind="location", title="密室", visibility="hidden"),
        NarrativeNode(
            id="auto", kind="clue", title="自动线索", visibility="hidden",
            reveal_when={"flag": "opened"},
        ),
        NarrativeNode(
            id="room_hall_secret", kind="location", title="门厅",
            objects=[{
                "id": "obj_wall", "name": "墙板后的暗格", "visible": True,
                "visibility": "hidden", "reveal_when": {"flag": "wall_opened"},
            }],
        ),
    ])


def test_forbidden_entries_excludes_revealed_and_auto_revealed() -> None:
    entries = forbidden_entries(_bundle(), {"revealed": [{"id": "hidden"}], "flags": {"opened": True}})
    names = {entry["name"] for entry in entries}

    assert "黑幕真相" in names
    assert "密室" not in names
    assert "自动线索" not in names


def test_scan_output_removes_contaminated_sentences() -> None:
    report = scan_output(
        "你推开黑幕真相的门。门厅很安静。密室里传来响动。",
        _bundle(),
        {},
    )

    assert report.changed is True
    assert report.text == "门厅很安静。"
    assert report.removed_sentences == 2
    assert {item["object_id"] for item in report.violations} == {"secret", "hidden"}


def test_scan_output_keeps_public_text_untouched() -> None:
    report = scan_output("门厅很安静。", _bundle(), {})
    assert report.text == "门厅很安静。"
    assert report.ok is True


def test_scan_output_replaces_fully_redacted_text() -> None:
    report = scan_output("黑幕真相就在地下室。", _bundle(), {})
    assert report.text == "（这段描述涉及尚未解锁的内容，暂时无法呈现。）"


def test_hidden_object_on_public_location_is_blocked_until_revealed() -> None:
    report = scan_output("你摸到墙板后的暗格。", _bundle(), {})
    assert report.changed is True

    allowed = scan_output("你摸到墙板后的暗格。", _bundle(), {"flags": {"wall_opened": True}})
    assert allowed.text == "你摸到墙板后的暗格。"
    assert allowed.ok is True


def test_apply_output_guard_without_bundle_is_noop() -> None:
    assert apply_output_guard("黑幕真相", None, {}) == "黑幕真相"


def test_degraded_policy_keeps_spoiler_gate() -> None:
    policy = GuardPolicy.for_mode("degraded")
    assert policy.spoiler_gate is True
    assert policy.move_gate is False
    assert apply_output_guard("黑幕真相。门厅很安静。", _bundle(), {}, policy) == "门厅很安静。"
