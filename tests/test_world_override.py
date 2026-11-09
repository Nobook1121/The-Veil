"""世界意志直执行：留痕、频率限制与既定事实注入。"""

import json

from trpg_server import world_override


def _state(room_dir):
    return json.loads((room_dir / "state.json").read_text(encoding="utf-8"))


def test_record_override_persists_fact_and_usage(tmp_path):
    result = world_override.record_override(tmp_path, 1, "tester", "我已战胜管家", now=1000.0)

    assert result["ok"] is True
    fact = result["fact"]
    assert fact["text"] == "我已战胜管家"
    assert fact["username"] == "tester"
    assert fact["id"]
    stored = _state(tmp_path)
    assert stored["world_override"]["facts"][-1]["id"] == fact["id"]
    assert stored["world_override"]["usage"][-1]["user_id"] == "1"


def test_second_override_within_cooldown_is_rate_limited(tmp_path):
    assert world_override.record_override(tmp_path, 1, "tester", "第一次", now=1000.0)["ok"] is True

    blocked = world_override.record_override(tmp_path, 1, "tester", "第二次", now=1000.0 + 5)

    assert blocked["ok"] is False
    assert blocked["rate"]["retry_after"] > 0
    assert "秒内只能使用一次" in blocked["rate"]["reason"]
    stored = _state(tmp_path)
    assert [fact["text"] for fact in stored["world_override"]["facts"]] == ["第一次"]


def test_cooldown_expires_per_user(tmp_path):
    world_override.record_override(tmp_path, 1, "tester", "第一次", now=1000.0)

    later = 1000.0 + world_override.PER_USER_COOLDOWN_SECONDS + 1
    assert world_override.record_override(tmp_path, 1, "tester", "第二次", now=later)["ok"] is True
    # 另一个玩家不受前一个玩家的个人冷却影响。
    assert world_override.record_override(tmp_path, 2, "player", "我也要", now=later + 1)["ok"] is True


def test_room_hourly_limit_is_enforced(tmp_path):
    now = 1000.0
    for index in range(world_override.PER_ROOM_HOURLY_LIMIT):
        result = world_override.record_override(tmp_path, index + 1, f"u{index}", f"事实{index}", now=now)
        assert result["ok"] is True, result

    blocked = world_override.record_override(tmp_path, 999, "late", "超出配额", now=now + 1)

    assert blocked["ok"] is False
    assert "每小时最多" in blocked["rate"]["reason"]


def test_facts_block_is_authoritative_and_keeps_ids(tmp_path):
    fact = world_override.record_override(tmp_path, 1, "tester", "管家已被击败", now=1000.0)["fact"]

    block = world_override.format_facts_block([fact])

    assert "既定事实" in block
    assert "管家已被击败" in block
    assert fact["id"] in block
    assert "不得重新检定" in block


def test_facts_from_state_returns_latest_entries(tmp_path):
    for index in range(12):
        world_override.record_override(
            tmp_path,
            1,
            "tester",
            f"事实{index}",
            now=1000.0 + index * (world_override.PER_USER_COOLDOWN_SECONDS + 1),
        )

    facts = world_override.facts_from_state(_state(tmp_path), limit=8)

    assert len(facts) == 8
    assert facts[-1]["text"] == "事实11"


def test_evidence_entries_drop_internal_timestamps(tmp_path):
    world_override.record_override(tmp_path, 1, "tester", "已战胜管家", now=1000.0)

    entries = world_override.evidence_entries(_state(tmp_path))

    assert entries
    assert "created_ts" not in entries[0]
    assert entries[0]["text"] == "已战胜管家"


def test_override_turn_blocks_combat_and_check_tools():
    from trpg_server.routes.chat import WORLD_OVERRIDE_BLOCKED_TOOLS

    assert "combat.attack" in WORLD_OVERRIDE_BLOCKED_TOOLS
    assert "check.roll_room_check" in WORLD_OVERRIDE_BLOCKED_TOOLS
    assert "dice.roll_coc_check" in WORLD_OVERRIDE_BLOCKED_TOOLS
