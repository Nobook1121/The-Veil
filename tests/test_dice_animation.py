"""骰子动画档位、玩家投掷与贴图库的单元测试。"""
import struct
import threading
import time

import pytest

from trpg_server import dice_textures
from trpg_server.agents import player_roll
from trpg_server.agents.dice_settings import resolve_dice_settings
from trpg_server.agents.tools.dice import _percentile_faces
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


def test_second_request_in_same_room_resolves_immediately(monkeypatch):
    events = []
    monkeypatch.setattr(player_roll, "_BROADCASTER", lambda event, payload: events.append((event, payload)))

    thread = threading.Thread(
        target=player_roll.wait_for_player_roll,
        args=(FakeContext("room-4"), _plan(), _result),
        kwargs={"roller_user_id": "u2", "tool_name": "dice.roll"},
    )
    thread.start()
    request_id = _wait_for_event(events, "player_dice_request")["request_id"]

    immediate = player_roll.wait_for_player_roll(
        FakeContext("room-4"), _plan(), _result, roller_user_id="u2", tool_name="dice.roll"
    )
    assert immediate["visible_message"]["metadata"]["dice_roll"]["player_roll"] is False

    player_roll.cancel_roll("room-4", request_id)
    thread.join(2)


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
