"""房间控制台的战斗命令回归测试。"""

from __future__ import annotations

import json

from trpg_server.room_console_commands import execute_room_command, room_command_help_text


def _member(user_id: int, name: str, dex: int = 50) -> dict:
    return {
        "user_id": user_id,
        "username": name,
        "role": "USER",
        "status": "active",
        "is_active": True,
        "room_role": "member",
        "character_card": {
            "name": name,
            "attributes": {"DEX": dex, "STR": 50, "SIZ": 50, "CON": 50, "POW": 50},
            "skills": {"格斗（斗殴）": 50},
            "weapons": [],
            "status": {},
        },
        "character_state": {"current_hp": 10, "max_hp": 10},
    }


def _room(tmp_path):
    room_dir = tmp_path / "rooms" / "r-combat"
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": "r-combat",
        "name": "Combat Room",
        "creator_id": 1,
        "members": [_member(1, "alice", dex=70), _member(2, "bob", dex=40)],
        "house_rules": {},
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _run(room_dir, line, **kwargs):
    return execute_room_command(
        line,
        room_id="r-combat",
        room_dir=room_dir,
        actor="tester",
        actor_id=1,
        actor_rank=3,
        **kwargs,
    )


def test_combat_commands_are_in_help():
    help_text = room_command_help_text()
    assert "combat start" in help_text
    assert "combat next" in help_text
    assert "combat damage" in help_text
    assert "combat end" in help_text
    # 五个辅助动作必须能从房间控制台发现。
    for sub in ("combat delay", "combat disengage", "combat reload", "combat ready", "combat cast"):
        assert sub in help_text, sub


def test_combat_console_full_flow(tmp_path):
    room_dir = _room(tmp_path)

    result = _run(room_dir, "combat")
    assert result.ok is True
    assert "没有进行中的战斗" in result.message

    result = _run(room_dir, "combat start")
    assert result.ok is True, result.message
    assert "战斗开始" in result.message
    assert "alice" in result.message and "bob" in result.message

    result = _run(room_dir, "combat")
    assert result.ok is True
    assert "第 1 轮" in result.message
    assert "alice" in result.message

    result = _run(room_dir, "combat damage alice 3")
    assert result.ok is True, result.message
    assert "alice" in result.message

    result = _run(room_dir, "combat heal alice 2")
    assert result.ok is True, result.message
    assert "恢复" in result.message

    result = _run(room_dir, "combat next")
    assert result.ok is True, result.message
    assert "当前行动者" in result.message

    result = _run(room_dir, "combat end --yes")
    assert result.ok is True, result.message
    assert "战斗已结束" in result.message


def test_combat_accepts_pure_word_options(tmp_path):
    room_dir = _room(tmp_path)
    assert _run(room_dir, "combat start alice initiative-check").ok is True

    result = _run(room_dir, "combat damage alice 3 ignore-armor")
    assert result.ok is True, result.message
    assert "alice" in result.message

    result = _run(room_dir, "combat end reason 撤退 yes")
    assert result.ok is True, result.message
    assert "战斗已结束" in result.message


def test_combat_start_requires_admin(tmp_path):
    room_dir = _room(tmp_path)
    result = execute_room_command(
        "combat start",
        room_id="r-combat",
        room_dir=room_dir,
        actor="player",
        actor_id=2,
        actor_rank=1,
    )
    assert result.ok is False
    assert "房主" in result.message


def test_combat_end_previews_without_yes(tmp_path):
    room_dir = _room(tmp_path)
    assert _run(room_dir, "combat start").ok is True

    result = _run(room_dir, "combat end")
    assert result.ok is True
    assert "yes" in result.message
    assert "--yes" not in result.message

    assert _run(room_dir, "combat").ok is True
    assert "第 1 轮" in _run(room_dir, "combat").message


def test_combat_join_appends_member_mid_combat(tmp_path):
    room_dir = _room(tmp_path)
    assert _run(room_dir, "combat start alice").ok is True

    result = _run(room_dir, "combat join bob")
    assert result.ok is True, result.message
    assert "加入" in result.message or "bob" in result.message

    state = _run(room_dir, "combat")
    assert "bob" in state.message


def test_combat_unknown_subcommand_is_rejected(tmp_path):
    room_dir = _room(tmp_path)
    result = _run(room_dir, "combat dance")
    assert result.ok is False
    # 未知子命令由白名单先拦下，错误信息保持与其他房间命令一致。
    assert "不属于房间范围" in result.message


def test_combat_console_utility_actions_are_routable(tmp_path):
    """delay/disengage/reload/ready/cast 必须真的走到战斗工具，而不是白名单放过却没人处理。"""
    room_dir = _room(tmp_path)
    assert _run(room_dir, "combat start alice bob").ok is True

    delayed = _run(room_dir, "combat delay")
    assert delayed.ok is True, delayed.message
    assert "延迟" in delayed.message

    disengaged = _run(room_dir, "combat disengage alice")
    assert disengaged.ok is True, disengaged.message
    assert "脱离" in disengaged.message

    readied = _run(room_dir, "combat ready bob")
    assert readied.ok is True, readied.message

    cast = _run(room_dir, "combat cast Curse --caster=alice --mp=1")
    assert cast.ok is True, cast.message

    # reload 会进入处理器；没有可装填武器时返回业务错误而非“不属于房间范围”。
    reloaded = _run(room_dir, "combat reload bob")
    assert "不属于房间范围" not in reloaded.message

