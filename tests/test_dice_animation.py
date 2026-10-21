"""骰子动画档位、玩家投掷与贴图库的单元测试。"""
import json
import struct
import threading
import time

import pytest

from trpg_server import dice_textures
from trpg_server.agents import player_roll
from trpg_server.agents.dice_settings import resolve_dice_settings
from trpg_server.agents.tools.dice import (
    _dice_roll_metadata,
    _percentile_faces,
    _percentile_from_faces,
    _plan_groups,
    _roll_terms_from_faces,
    _standard_groups,
    roll_coc_check,
    roll_dice,
)
from trpg_server.room_console_commands import execute_room_command


class FakeContext:
    def __init__(self, room_id="room-1"):
        self.room_id = room_id
        self.room_dir = None
        self.user_id = "u1"
        self.config_dir = None
        self.tool_state = {"player_dice_roll_timeout": 30}


def _result(value=42):
    return {
        "summary": f"1d100 = {value}",
        "visible_message": {
            "content": f"1d100 = {value}",
            "metadata": {"dice_roll": {"mode": "percentile", "faces": [value // 10 or 10, value % 10], "roll": value}},
        },
    }


def _plan():
    return {"mode": "percentile", "expression": "1d100", "count": 1, "sides": 100, "candidates": [], "check": None}


def _wait_for_event(events, name, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event, payload in events:
            if event == name:
                return payload
        time.sleep(0.01)
    raise AssertionError(f"event {name} not emitted")


# ---------------------------------------------------------------------------
# 生效值解析
# ---------------------------------------------------------------------------


def _write_config(directory, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "general.toml").write_text(text, encoding="utf-8")


def test_resolve_uses_admin_defaults_when_room_rules_empty(tmp_path):
    config_dir = tmp_path / "config"
    _write_config(config_dir, "[ai]\ndice_animation_level = \"simple\"\ndice_player_roll = true\ndice_player_roll_timeout = 90\n")

    settings = resolve_dice_settings(config_dir, {})

    assert settings["level"] == "simple"
    assert settings["player_roll"] is True
    assert settings["timeout"] == 90
    assert settings["level_source"] == "admin"


def test_resolve_prefers_room_rules_and_ignores_invalid_level(tmp_path):
    config_dir = tmp_path / "config"
    _write_config(config_dir, "[ai]\ndice_animation_level = \"3d\"\ndice_player_roll = false\n")

    room = resolve_dice_settings(config_dir, {"dice_animation_level": "text", "player_dice_roll": True})
    assert room["level"] == "text"
    assert room["player_roll"] is True
    assert room["level_source"] == "room"
    assert room["player_roll_source"] == "room"

    invalid = resolve_dice_settings(config_dir, {"dice_animation_level": "ultra"})
    assert invalid["level"] == "3d"
    assert invalid["level_source"] == "admin"


def test_percentile_faces_split_tens_and_units():
    assert _percentile_faces(34) == ([3, 4], ["30", "4"])
    assert _percentile_faces(7) == ([0, 7], ["00", "7"])
    assert _percentile_faces(100) == ([0, 0], ["00", "0"])


def test_percentile_from_physical_faces_matches_rules():
    # 普通百分骰：十位 4、个位 7 -> 47。
    assert _percentile_from_faces([4, 7]) == (47, [])
    assert _percentile_from_faces([0, 0]) == (100, [])
    # 奖励骰取最小候选：十位 4/2、个位 7 -> 27。
    roll, candidates = _percentile_from_faces([4, 7, 2], bonus=1)
    assert roll == 27
    assert candidates == [47, 27]
    # 惩罚骰取最大候选。
    roll, candidates = _percentile_from_faces([4, 7, 8], penalty=1)
    assert roll == 87
    assert candidates == [47, 87]
    # 重复十位只保留一次，不足时按 0-9 补齐。
    roll, candidates = _percentile_from_faces([4, 7, 4], bonus=1)
    assert candidates[0] == 47
    assert roll == 7
    assert len(candidates) == 2


def test_physical_faces_feed_coc_check_and_generic_dice():
    check = roll_coc_check({"target": 50, "name": "侦查"}, faces=[4, 7])
    assert check["visible_message"]["metadata"]["dice_roll"]["roll"] == 47

    d100 = roll_dice({"expression": "1d100"}, faces=[4, 7])
    assert d100["visible_message"]["metadata"]["dice_roll"]["total"] == 47

    hundred = roll_dice({"expression": "1d100"}, faces=[0, 0])
    assert hundred["visible_message"]["metadata"]["dice_roll"]["total"] == 100

    terms = _roll_terms_from_faces([(3, 6)], [1, 2, 3])
    assert terms[0]["rolls"] == [1, 2, 3]
    assert terms[0]["total"] == 6

    with pytest.raises(ValueError):
        _roll_terms_from_faces([(3, 6)], [1, 2])
    with pytest.raises(ValueError):
        _roll_terms_from_faces([(1, 6)], [7])


def test_resolve_reroll_prefers_room_mode_and_clamps_limit(tmp_path):
    config_dir = tmp_path / "config"
    _write_config(config_dir, '[ai]\ndice_reroll_mode = "limited"\ndice_reroll_limit = 5\n')

    admin = resolve_dice_settings(config_dir, {})
    assert (admin["reroll_mode"], admin["reroll_limit"], admin["rerolls"], admin["reroll_source"]) == (
        "limited",
        5,
        5,
        "admin",
    )

    disabled = resolve_dice_settings(config_dir, {"dice_reroll_mode": "off"})
    assert disabled["rerolls"] == 0
    assert disabled["reroll_source"] == "room"

    unlimited = resolve_dice_settings(config_dir, {"dice_reroll_mode": "unlimited", "dice_reroll_limit": 2})
    assert unlimited["rerolls"] == -1
    assert unlimited["reroll_source"] == "room"

    clamped = resolve_dice_settings(config_dir, {"dice_reroll_mode": "limited", "dice_reroll_limit": 99})
    assert clamped["reroll_limit"] == 20

    invalid = resolve_dice_settings(config_dir, {"dice_reroll_mode": "sometimes"})
    assert invalid["reroll_mode"] == "limited"
    assert invalid["reroll_source"] == "admin"


# ---------------------------------------------------------------------------
# 玩家投掷
# ---------------------------------------------------------------------------


def test_wait_for_player_roll_blocks_until_target_player_resolves(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))

    holder = {}

    def run():
        holder["result"] = player_roll.wait_for_player_roll(
            FakeContext(), _plan(), _result, roller_user_id="u2", roller_name="player", tool_name="dice.roll"
        )

    thread = threading.Thread(target=run)
    thread.start()
    payload = _wait_for_event(events, "player_dice_request")
    request_id = payload["request_id"]
    assert payload["roller"]["user_id"] == "u2"
    assert [item["request_id"] for item in player_roll.list_pending("room-1")] == [request_id]

    result, error = player_roll.resolve_roll("room-1", request_id, "u2")
    assert error == ""
    assert result["summary"] == "1d100 = 42"
    assert player_roll.complete_roll("room-1", request_id, "u2") == (True, "")

    thread.join(2)
    assert holder["result"]["visible_message"]["metadata"]["dice_roll"]["request_id"] == request_id
    assert player_roll.list_pending("room-1") == []


def test_complete_before_roll_keeps_request_pending(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))

    holder = {}

    def run():
        holder["result"] = player_roll.wait_for_player_roll(
            FakeContext("room-5"), _plan(), _result, roller_user_id="u2", roller_name="player", tool_name="dice.roll"
        )

    thread = threading.Thread(target=run)
    thread.start()
    request_id = _wait_for_event(events, "player_dice_request")["request_id"]

    # 尚未投掷时动画结束回调必须被拒绝，否则工具线程会拿到空结果。
    assert player_roll.complete_roll("room-5", request_id, "u2") == (False, "not_rolled")
    assert player_roll.get_pending("room-5", request_id)["state"] == "pending"

    assert player_roll.resolve_roll("room-5", request_id, "u2")[1] == ""
    assert player_roll.complete_roll("room-5", request_id, "u2") == (True, "")
    thread.join(2)
    assert holder["result"]["visible_message"]["metadata"]["dice_roll"]["request_id"] == request_id


def test_wait_for_player_roll_rejects_non_target_player(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))

    thread = threading.Thread(
        target=player_roll.wait_for_player_roll,
        args=(FakeContext("room-2"), _plan(), _result),
        kwargs={"roller_user_id": "u2", "tool_name": "dice.roll"},
    )
    thread.start()
    request_id = _wait_for_event(events, "player_dice_request")["request_id"]

    assert player_roll.resolve_roll("room-2", request_id, "u1") == (None, "forbidden")
    player_roll.cancel_roll("room-2", request_id, operator="test")
    thread.join(2)


def test_wait_for_player_roll_times_out_and_rolls_automatically(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))
    monkeypatch.setattr(player_roll, "wait_timeout_seconds", lambda context: 0.15)

    result = player_roll.wait_for_player_roll(
        FakeContext("room-3"), _plan(), _result, roller_user_id="u2", roller_name="player", tool_name="dice.roll"
    )

    rolled = _wait_for_event(events, "player_dice_rolled")
    assert rolled["auto"] is True
    assert result["visible_message"]["metadata"]["dice_roll"]["auto"] is True
    assert player_roll.list_pending("room-3") == []


def test_second_request_in_same_room_is_rejected(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))

    thread = threading.Thread(
        target=player_roll.wait_for_player_roll,
        args=(FakeContext("room-4"), _plan(), _result),
        kwargs={"roller_user_id": "u2", "tool_name": "dice.roll"},
    )
    thread.start()
    request_id = _wait_for_event(events, "player_dice_request")["request_id"]

    rejected = player_roll.wait_for_player_roll(
        FakeContext("room-4"), _plan(), _result, roller_user_id="u2", tool_name="dice.roll"
    )
    # 不再即时投掷：重复检定必须被拒绝，让 AI 等第一次投掷完成。
    assert rejected["error"] == "dice_request_pending"
    assert rejected["pending_request_id"] == request_id
    assert "visible_message" not in rejected

    player_roll.cancel_roll("room-4", request_id)
    thread.join(2)


# ---------------------------------------------------------------------------
# 重投与确认
# ---------------------------------------------------------------------------


def _reroll_context(room_id, mode, rerolls=3):
    context = FakeContext(room_id)
    context.tool_state = {
        "player_dice_roll_timeout": 30,
        "dice_reroll_mode": mode,
        "dice_rerolls": rerolls,
    }
    return context


def _start_roll(monkeypatch, room_id, context):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))
    holder = {}

    def run():
        holder["result"] = player_roll.wait_for_player_roll(
            context, _plan(), _result, roller_user_id="u2", roller_name="player", tool_name="dice.roll"
        )

    thread = threading.Thread(target=run)
    thread.start()
    request_id = _wait_for_event(events, "player_dice_request")["request_id"]
    return events, holder, thread, request_id


def test_limited_reroll_requires_confirmation_and_blocks_ai(monkeypatch):
    events, holder, thread, request_id = _start_roll(monkeypatch, "room-r1", _reroll_context("room-r1", "limited", 2))
    request = _wait_for_event(events, "player_dice_request")
    assert request["reroll"] == {"enabled": True, "mode": "limited", "limit": 2, "used": 0, "remaining": 2}

    result, error = player_roll.resolve_roll("room-r1", request_id, "u2")
    assert error == ""
    assert player_roll.get_pending("room-r1", request_id)["state"] == "awaiting_confirm"
    # 动画结束 ≠ 确认：重投开启时仍保持阻塞，等待玩家确认或超时。
    assert player_roll.complete_roll("room-r1", request_id, "u2") == (True, "")
    assert player_roll.get_pending("room-r1", request_id)["state"] == "awaiting_confirm"

    assert player_roll.reroll("room-r1", request_id, "u1") == (None, "forbidden")
    assert player_roll.reroll("room-r1", request_id, "u2")[1] == ""
    assert player_roll.reroll("room-r1", request_id, "u2")[1] == ""
    assert player_roll.reroll("room-r1", request_id, "u2") == (None, "reroll_exhausted")

    state = player_roll.get_pending("room-r1", request_id)
    assert state["reroll"]["used"] == 2
    assert state["reroll"]["remaining"] == 0
    rerolled = _wait_for_event(events, "player_dice_rerolled")
    assert rerolled["reroll_index"] == 1

    confirmed, error = player_roll.confirm_roll("room-r1", request_id, "u2")
    assert error == ""
    thread.join(2)
    assert not thread.is_alive()
    dice = holder["result"]["visible_message"]["metadata"]["dice_roll"]
    assert dice["confirmed"] is True
    assert dice["reroll"] == {"enabled": True, "mode": "limited", "limit": 2, "used": 2, "remaining": 0}
    assert player_roll.list_pending("room-r1") == []


def test_unlimited_reroll_reports_negative_remaining(monkeypatch):
    events, holder, thread, request_id = _start_roll(monkeypatch, "room-r2", _reroll_context("room-r2", "unlimited"))
    assert _wait_for_event(events, "player_dice_request")["reroll"]["remaining"] == -1

    player_roll.resolve_roll("room-r2", request_id, "u2")
    player_roll.reroll("room-r2", request_id, "u2")
    state = player_roll.get_pending("room-r2", request_id)
    assert state["reroll"] == {"enabled": True, "mode": "unlimited", "limit": 0, "used": 1, "remaining": -1}

    player_roll.confirm_roll("room-r2", request_id, "u2")
    thread.join(2)


def test_reroll_disabled_rejects_reroll(monkeypatch):
    events, holder, thread, request_id = _start_roll(monkeypatch, "room-r3", _reroll_context("room-r3", "off", 0))
    assert _wait_for_event(events, "player_dice_request")["reroll"]["enabled"] is False

    player_roll.resolve_roll("room-r3", request_id, "u2")
    assert player_roll.reroll("room-r3", request_id, "u2") == (None, "reroll_disabled")
    assert player_roll.complete_roll("room-r3", request_id, "u2") == (True, "")
    thread.join(2)


def test_wait_timeout_auto_confirms_last_reroll(monkeypatch):
    monkeypatch.setattr(player_roll, "wait_timeout_seconds", lambda context: 0.3)
    events, holder, thread, request_id = _start_roll(monkeypatch, "room-r4", _reroll_context("room-r4", "limited", 3))

    player_roll.resolve_roll("room-r4", request_id, "u2")
    player_roll.reroll("room-r4", request_id, "u2")
    thread.join(2)
    assert not thread.is_alive()

    confirmed = _wait_for_event(events, "player_dice_confirmed")
    assert confirmed["reason"] == "timeout"
    dice = holder["result"]["visible_message"]["metadata"]["dice_roll"]
    assert dice["confirmed"] is True
    assert dice["reroll"]["used"] == 1
    assert player_roll.list_pending("room-r4") == []


# ---------------------------------------------------------------------------
# 骰组元数据
# ---------------------------------------------------------------------------


def test_percentile_metadata_exposes_tens_ones_and_candidate_groups():
    result = {
        "roll": 34,
        "rolls": [64, 34],
        "bonus_dice": 1,
        "penalty_dice": 0,
        "success": True,
        "success_level": "regular",
    }
    meta = _dice_roll_metadata(result, mode="percentile", check_name="侦查", target=60)

    assert [group["role"] for group in meta["groups"]] == ["tens", "ones", "candidate"]
    assert [group["type"] for group in meta["groups"]] == ["d10", "d10", "d10"]
    assert meta["groups"][0]["labels"] == ["30"]
    assert meta["groups"][1]["labels"] == ["4"]
    assert meta["groups"][2]["labels"] == ["60"]
    assert meta["selected"] == {"group": 0, "index": 0}
    assert meta["reroll"] == {"enabled": False, "mode": "off", "limit": 0, "used": 0, "remaining": 0}
    assert meta["confirmed"] is True


def test_percentile_metadata_without_candidates_does_not_select_a_die():
    result = {"roll": 42, "success": True, "success_level": "regular"}
    meta = _dice_roll_metadata(result, mode="percentile", check_name="侦查", target=60)

    assert [group["role"] for group in meta["groups"]] == ["tens", "ones"]
    assert meta["selected"] is None


def test_standard_metadata_splits_percentile_into_two_d10_groups():
    result = {"expression": "1d100", "rolls": [7], "total": 7}
    meta = _dice_roll_metadata(result, mode="standard")

    assert [group["role"] for group in meta["groups"]] == ["tens", "ones"]
    assert meta["groups"][0]["labels"] == ["00"]
    assert meta["groups"][1]["labels"] == ["7"]
    assert meta["selected"] is None


def test_plan_groups_expands_d100_and_keeps_other_dice_types():
    groups = _plan_groups([(1, 100), (2, 6)])

    assert [(group["role"], group["type"], group["count"]) for group in groups] == [
        ("tens", "d10", 1),
        ("ones", "d10", 1),
        ("normal", "d6", 2),
    ]
    assert all(group["labels"] == [] for group in groups)


def test_standard_groups_keeps_real_die_type():
    groups = _standard_groups([{"sides": 8, "rolls": [3, 8]}])

    assert groups == [{"role": "normal", "type": "d8", "count": 2, "faces": [3, 8], "labels": ["3", "8"]}]


# ---------------------------------------------------------------------------
# 房间控制台
# ---------------------------------------------------------------------------


def _room(rooms_dir, room_id="r-1"):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    (room_dir / "info.json").write_text(
        '{"id": "r-1", "name": "房间", "members": [], "house_rules": {}}',
        encoding="utf-8",
    )
    return room_dir


def test_room_console_reports_and_updates_dice_settings(tmp_path):
    rooms_dir = tmp_path / "rooms"
    room_dir = _room(rooms_dir)

    def run(line):
        return execute_room_command(line, room_id="r-1", room_dir=room_dir, rooms_dir=rooms_dir)

    status = run("dice")
    assert status.ok is True
    assert "骰子动画" in status.message
    assert "玩家投掷" in status.message

    assert run("dice level ultra").ok is False
    assert run("dice level simple --yes").ok is True
    assert run("dice player on --yes").ok is True

    info = __import__("json").loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["dice_animation_level"] == "simple"
    assert info["house_rules"]["player_dice_roll"] is True

    inherit = run("dice level inherit --yes")
    assert inherit.ok is True
    info = __import__("json").loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["dice_animation_level"] is None

    assert "没有等待玩家投掷" in run("dice pending").message


def test_site_console_dice_settings_and_pending():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("dice settings")
    assert result.ok is True
    assert "管理员默认" in result.message

    pending = execute_console_command("dice pending")
    assert pending.ok is True

    usage = execute_console_command("dice")
    assert usage.ok is True
    assert "Usage: dice" in usage.message


def test_site_console_reroll_commands(monkeypatch):
    from trpg_server.console_commands import execute_console_command

    recorded = []
    monkeypatch.setattr(
        "trpg_server.agents.config.write_ai_setting",
        lambda key, value, config_dir=None: recorded.append((key, value)),
    )

    assert execute_console_command("dice animation show").ok is True
    assert execute_console_command("dice reroll unlimited --yes").ok is True
    assert execute_console_command("dice reroll-limit 5 --yes").ok is True
    assert recorded == [("dice_reroll_mode", "unlimited"), ("dice_reroll_limit", 5)]

    assert execute_console_command("dice reroll sometimes --yes").ok is False
    assert execute_console_command("dice reroll-limit 21 --yes").ok is False
    assert execute_console_command("dice player-roll show").ok is True


def test_room_console_reroll_commands(tmp_path):
    rooms_dir = tmp_path / "rooms"
    room_dir = _room(rooms_dir)

    def run(line):
        return execute_room_command(line, room_id="r-1", room_dir=room_dir, rooms_dir=rooms_dir)

    assert run("dice reroll show").ok is True
    assert run("dice reroll unlimited --yes").ok is True
    assert run("dice reroll-limit 5 --yes").ok is True
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["dice_reroll_mode"] == "unlimited"
    assert info["house_rules"]["dice_reroll_limit"] == 5

    assert run("dice reroll-limit 0 --yes").ok is False
    assert run("dice reroll sometimes --yes").ok is False

    assert run("dice reroll inherit --yes").ok is True
    assert run("dice reroll-limit inherit --yes").ok is True
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["dice_reroll_mode"] is None
    assert info["house_rules"]["dice_reroll_limit"] is None


def test_house_rule_definitions_include_reroll_settings():
    from trpg_server.routes.rooms import HOUSE_RULE_DEFINITIONS

    mode = HOUSE_RULE_DEFINITIONS["dice_reroll_mode"]
    assert mode["type"] == "nullable_enum"
    assert mode["default"] is None
    assert mode["values"] == ["off", "limited", "unlimited"]

    limit = HOUSE_RULE_DEFINITIONS["dice_reroll_limit"]
    assert limit["type"] == "nullable_integer"
    assert (limit["min"], limit["max"]) == (1, 20)


# ---------------------------------------------------------------------------
# 贴图库
# ---------------------------------------------------------------------------


def _png(width, height):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 8
    )


@pytest.fixture()
def texture_dir(tmp_path, monkeypatch):
    directory = tmp_path / "dice-textures"
    monkeypatch.setattr(dice_textures, "DICE_TEXTURES_DIR", directory)
    return directory


def test_texture_assets_are_validated_and_protected(texture_dir):
    asset = dice_textures.create_asset(_png(256, 256), name="木桌", kind="table", uploaded_by=1)
    assert asset["content_type"] == "image/png"
    assert asset["width"] == 256
    assert dice_textures.asset_path(asset).exists()

    skin = dice_textures.upsert_table_skin({"name": "深色木纹", "asset_id": asset["id"]})
    with pytest.raises(dice_textures.TextureError) as excinfo:
        dice_textures.delete_asset(asset["id"])
    assert excinfo.value.reason == "asset_in_use"

    dice_textures.delete_skin(skin["id"])
    dice_textures.delete_asset(asset["id"])
    assert dice_textures.library()["assets"] == []


def test_texture_rejects_bad_image_and_bad_face_key(texture_dir):
    with pytest.raises(dice_textures.TextureError):
        dice_textures.validate_image(b"not an image")

    with pytest.raises(dice_textures.TextureError) as excinfo:
        dice_textures.validate_image(_png(64, 64))
    assert excinfo.value.reason == "bad_dimensions"

    with pytest.raises(dice_textures.TextureError):
        dice_textures.upsert_dice_skin({"die_type": "d6", "faces": {"7": "asset-x"}})


def test_dice_skin_round_trip(texture_dir):
    asset = dice_textures.create_asset(_png(128, 128), name="红面", kind="face")
    skin = dice_textures.upsert_dice_skin({
        "name": "红方",
        "die_type": "d6",
        "faces": {"1": asset["id"], "6": asset["id"]},
    })

    data = dice_textures.library()
    assert data["dice_skins"][0]["id"] == skin["id"]
    assert data["dice_skins"][0]["faces"]["1"] == asset["id"]
