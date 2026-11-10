"""AI 对话语音配音（P1）：标记解析、正文剥离、人设稳定性、房间收紧、控制台与权限。"""
from __future__ import annotations

import json

import pytest

from trpg_server.app_factory import create_app
from trpg_server.config import update_sections
from trpg_server.config.paths import general_config_path, resolve_config_dir
from trpg_server.console_commands import execute_console_command
from trpg_server.voice import events
from trpg_server.voice.config import (
    load_room_voice,
    load_voice_settings,
    resolve_personal_voice,
    resolve_room_voice,
    save_room_voice,
)
from trpg_server.voice.director import prepare_voice
from trpg_server.voice.engine import REASON_ENGINE_UNAVAILABLE, VoiceSynthesisError, synthesize
from trpg_server.voice.markup import NpcEntry, build_npc_roster, parse_narration
from trpg_server.voice.profiles import PROFILE_MAP, auto_profile_for, normalize_speaker_name

ROSTER = [
    NpcEntry(name="艾德温", aliases=("老艾", "Edwin"), data={"gender": "male", "age": "中年"}),
    NpcEntry(name="玛丽", aliases=("Mary",), data={"gender": "female", "age": "青年"}),
]


class FakeUserManager:
    def __init__(self, role="USER", user_id=1):
        self.role = role
        self.user_id = user_id

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="USER", user_id=1):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeUserManager(role, user_id),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, user_id=1, role="USER"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", user_id=1):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Voice Room",
        "creator_id": user_id,
        "members": [
            {"user_id": user_id, "username": "tester", "role": "USER", "status": "active", "is_active": True}
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _write_site_config(tmp_path, **values):
    path = general_config_path(resolve_config_dir(tmp_path / "config"))
    update_sections(path, {"immersive": values})
    return tmp_path / "config"


def test_markup_fullwidth_and_halfwidth_colons_and_spaces():
    text = "【NPC:艾 德 温】“你们来了。”【NPC：玛丽】“嗯。”"
    parsed = parse_narration(text, roster=ROSTER)
    assert parsed.clean_text == "“你们来了。”“嗯。”"
    assert [item["display_name"] for item in parsed.segments] == ["艾 德 温", "玛丽"]
    assert [item["source"] for item in parsed.segments] == ["markup", "markup"]
    assert all(item["kind"] == "npc" for item in parsed.segments)
    assert parsed.segments[0]["speaker_id"] == "npc:艾德温"  # 归一忽略空白，命中别名表


def test_markup_mid_line_and_consecutive_markers():
    parsed = parse_narration("前文【旁白】中段【NPC:玛丽】后文", roster=ROSTER)
    assert "【" not in parsed.clean_text and "】" not in parsed.clean_text
    assert parsed.clean_text == "前文中段后文"
    assert [item["kind"] for item in parsed.segments] == ["narrator", "narrator", "npc"]
    assert [item["display_name"] for item in parsed.segments][-1] == "玛丽"


def test_markup_unclosed_marker_does_not_leak_or_crash():
    parsed = parse_narration("【NPC:艾德温 还没闭合", roster=ROSTER)
    assert "【" not in parsed.clean_text and "】" not in parsed.clean_text
    assert isinstance(parsed.segments, list)


def test_pure_narrator_has_single_segment_and_source_narrator():
    parsed = parse_narration("雨声渐密，走廊尽头没有回音。", roster=ROSTER)
    assert parsed.clean_text == "雨声渐密，走廊尽头没有回音。"
    assert len(parsed.segments) == 1
    assert parsed.segments[0]["kind"] == "narrator"
    assert parsed.segments[0]["source"] == "narrator"


def test_heuristic_fallback_matches_alias_and_keeps_unknown_as_narrator():
    parsed = parse_narration(
        "老艾：“你们终于来了。”\n陌生人：“我也听见了。”",
        roster=ROSTER,
    )
    assert parsed.segments[0]["display_name"] == "艾德温"
    assert parsed.segments[0]["source"] == "heuristic"
    assert parsed.segments[0]["kind"] == "npc"
    assert parsed.segments[1]["kind"] == "narrator"
    assert parsed.segments[1]["source"] == "narrator"


def test_segment_and_turn_caps_are_enforced():
    text = "【旁白】" + "一二三四五六七八九十" * 10
    parsed = parse_narration(text, max_segment_chars=10, max_turn_chars=25, max_segments=2)
    assert len(parsed.segments) <= 2
    assert sum(len(item["text"]) for item in parsed.segments) <= 25
    assert all(len(item["text"]) <= 10 for item in parsed.segments)
    assert parsed.truncated


def test_segment_metadata_contract():
    parsed = parse_narration("【NPC:艾德温】你好。【旁白】雨停。", roster=ROSTER)
    for segment in parsed.segments:
        assert set(segment) == {"speaker_id", "display_name", "kind", "text", "source"}
        assert segment["kind"] in {"narrator", "npc"}
        assert segment["source"] in {"markup", "heuristic", "narrator"}
        assert segment["text"]


def test_auto_profile_is_stable_and_respects_gender_age():
    first = auto_profile_for("艾德温", {"gender": "male", "age": "中年"})
    second = auto_profile_for("艾德温", {"gender": "male", "age": "中年"})
    assert first.id == second.id == "npc_male_middle"
    assert auto_profile_for("玛丽", {"gender": "female", "age": "青年"}).id == "npc_female_youth"
    # 同一名字跨进程稳定（哈希路径），不受调用顺序影响。
    assert auto_profile_for("无名者").id == auto_profile_for("无名者").id
    assert len(PROFILE_MAP) == 9


def test_build_npc_roster_reads_scenario_and_scene():
    roster = build_npc_roster(
        {"npcs": [{"name": "艾德温", "aliases": ["老艾"], "gender": "男", "age": "中年"}]},
        {"npcs": [{"title": "玛丽", "gender": "女"}]},
    )
    assert [entry.name for entry in roster] == ["艾德温", "玛丽"]
    assert normalize_speaker_name("艾 德 温（队长）") == "艾德温"


def test_room_voice_only_tightens_limits_and_raises_ducking():
    settings = {
        "enabled": True, "engine": "system", "engine_allowed": ["system"],
        "autoplay": False, "volume": 70, "rate": 100,
        "max_turn_chars": 800, "max_segment_chars": 40, "max_segments": 24,
        "interrupt_policy": "interrupt", "ducking": True, "ducking_level": 20,
        "room_hourly_cap": 120, "allow_personal": True,
    }
    tight = resolve_room_voice(settings, {
        "voice_enabled": True, "voice_max_turn_chars": 900, "voice_max_segment_chars": 100,
        "voice_max_segments": 40, "voice_ducking_level": 60, "voice_room_hourly_cap": 200,
    })
    assert tight["enabled"] is True
    assert tight["max_turn_chars"] == 800
    assert tight["max_segment_chars"] == 40
    assert tight["max_segments"] == 24
    assert tight["ducking_level"] == 60
    assert tight["room_hourly_cap"] == 120
    off = resolve_room_voice(settings, {"voice_enabled": False})
    assert off["enabled"] is False


def test_personal_overrides_site_defaults_and_respects_admin_lock():
    site = {"volume": 70, "rate": 100, "autoplay": False}
    prefs = {"voice_volume": 30, "voice_rate": 140, "voice_autoplay": True, "voice_text_only": False}
    merged = resolve_personal_voice(site, prefs, allow_personal=True)
    assert merged["volume"] == 30 and merged["rate"] == 140 and merged["autoplay"] is True
    locked = resolve_personal_voice(site, prefs, allow_personal=False)
    assert locked["volume"] == 70 and locked["rate"] == 100 and locked["autoplay"] is False
    text_only = resolve_personal_voice(site, {"voice_text_only": True}, allow_personal=True)
    assert text_only["enabled"] is False and text_only["text_only"] is True


def test_engine_local_and_cloud_degrade_with_explicit_reason():
    with pytest.raises(VoiceSynthesisError) as cloud:
        synthesize({"text": "hi"}, {"id": "narrator"}, engine="cloud", engine_allowed=["system", "cloud"])
    assert cloud.value.reason == REASON_ENGINE_UNAVAILABLE
    with pytest.raises(VoiceSynthesisError):
        synthesize({"text": "hi"}, {"id": "narrator"}, engine="local", engine_allowed=["system"])
    ok = synthesize({"text": "hi"}, {"id": "narrator", "pitch": 1.0, "rate": 1.0}, engine="system", engine_allowed=["system"])
    assert ok["mode"] == "client" and ok["engine_ready"] is True


def test_prepare_voice_strips_markers_when_disabled_and_degrades_when_cloud(tmp_path):
    room_dir = _write_room(tmp_path)
    config_dir = _write_site_config(tmp_path, voice_enabled=False)
    result = prepare_voice(
        "【NPC:艾德温】“你好。”\n【旁白】雨停。",
        room_id="r-1", room_dir=room_dir, house_rules={},
        scenario={"npcs": [{"name": "艾德温", "gender": "男", "age": "中年"}]},
        config_dir=config_dir,
    )
    assert result["content"] == "“你好。”\n雨停。"
    assert result["voice"] is None
    assert "【NPC" not in result["content"]

    config_dir = _write_site_config(
        tmp_path, voice_enabled=True, voice_engine="cloud", voice_engine_allowed="system,cloud"
    )
    result = prepare_voice(
        "【NPC:艾德温】“你好。”",
        room_id="r-1", room_dir=room_dir, house_rules={},
        config_dir=config_dir,
    )
    assert result["voice"] is not None
    assert result["voice"]["playable"] is False
    assert result["voice"]["reason"] == "engine_unavailable"
    assert result["content"] == "“你好。”"


def test_prepare_voice_produces_profiles_and_room_mapping(tmp_path):
    room_dir = _write_room(tmp_path)
    save_room_voice(room_dir, {"npc_profiles": {"艾德温": "npc_male_elder"}, "muted": [], "usage": {}})
    config_dir = _write_site_config(
        tmp_path, voice_enabled=True, voice_engine="system",
        voice_max_segment_chars=40, voice_max_turn_chars=800,
    )
    result = prepare_voice(
        "【NPC:艾德温】“你们终于来了。”\n【旁白】雨还在下。",
        room_id="r-1", room_dir=room_dir, house_rules={},
        scenario={"npcs": [{"name": "艾德温", "gender": "男", "age": "中年"}]},
        message_id="m-1", turn=3, config_dir=config_dir,
    )
    voice = result["voice"]
    assert voice["playable"] is True
    assert voice["segments"][0]["profile_id"] == "npc_male_elder"
    assert voice["segments"][0]["pitch"] == PROFILE_MAP["npc_male_elder"].pitch
    assert voice["segments"][-1]["kind"] == "narrator"
    assert voice["segments"][-1]["profile_id"] == "narrator"
    assert load_room_voice(room_dir)["usage"]["count"] == 1
    assert events.recent(5)  # voice_ready / voice_segment 已写入结构化日志


def test_room_hourly_cap_blocks_without_playback(tmp_path):
    room_dir = _write_room(tmp_path)
    config_dir = _write_site_config(tmp_path, voice_enabled=True, voice_engine="system", voice_room_hourly_cap=1)
    save_room_voice(room_dir, {"npc_profiles": {}, "muted": [], "usage": {}})
    first = prepare_voice("【旁白】第一回合。", room_id="r-1", room_dir=room_dir, config_dir=config_dir)
    assert first["voice"]["playable"] is True
    second = prepare_voice("【旁白】第二回合。", room_id="r-1", room_dir=room_dir, config_dir=config_dir)
    assert second["voice"]["playable"] is False
    assert second["voice"]["reason"] == "room_hourly_cap"


def test_console_map_set_is_dry_run_until_yes(tmp_path):
    room_dir = _write_room(tmp_path)
    preview = execute_console_command(
        "voice map set 艾德温 npc_male_elder --room r-1",
        rooms_dir=tmp_path / "rooms",
    )
    assert preview.ok and "预演" in preview.message
    assert not (room_dir / "voice.json").exists()
    done = execute_console_command(
        "voice map set 艾德温 npc_male_elder --room r-1 --yes",
        rooms_dir=tmp_path / "rooms",
    )
    assert done.ok
    assert load_room_voice(room_dir)["npc_profiles"]["艾德温"] == "npc_male_elder"


def test_console_site_status_and_toggle(tmp_path):
    config_dir = _write_site_config(tmp_path, voice_enabled=False)
    status = execute_console_command("voice status", app=_app(tmp_path))
    assert status.ok
    assert "总开关: 关" in status.message
    turned = execute_console_command("voice on", app=_app(tmp_path))
    assert turned.ok
    assert load_voice_settings(config_dir)["enabled"] is True
    profiles = execute_console_command("voice profile list")
    assert profiles.ok and "npc_male_elder" in profiles.message and "narrator" in profiles.message


def test_voice_http_permissions(tmp_path):
    app = _app(tmp_path, role="USER")
    _write_room(tmp_path)
    client = app.test_client()

    assert client.get("/api/voice/config").status_code == 401

    _login(client, role="USER")
    assert client.get("/api/voice/config").status_code == 200
    denied = client.post("/api/voice/config", json={"settings": {"voice_enabled": True}})
    assert denied.status_code == 403

    playback = client.post(
        "/api/voice/playback",
        json={"room_id": "r-1", "reason": "playing"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert playback.status_code == 200

    admin_app = _app(tmp_path, role="ADMIN")
    admin_client = admin_app.test_client()
    _login(admin_client, role="ADMIN")
    saved = admin_client.post(
        "/api/voice/config",
        json={"settings": {"voice_enabled": True}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert saved.status_code == 200


def test_voice_playback_requires_room_membership(tmp_path):
    app = _app(tmp_path, role="USER", user_id=2)
    _write_room(tmp_path, user_id=1)
    client = app.test_client()
    _login(client, user_id=2, role="USER")
    denied = client.post(
        "/api/voice/playback",
        json={"room_id": "r-1"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert denied.status_code == 403


def test_voice_config_rejects_invalid_room_id(tmp_path):
    app = _app(tmp_path, role="USER", user_id=1)
    _write_room(tmp_path, user_id=1)
    client = app.test_client()
    _login(client, user_id=1, role="USER")

    for bad_id in ("../secret", "r-1/../../x", "r 1", "r.1", "%2e%2e"):
        response = client.get("/api/voice/config", query_string={"room_id": bad_id})
        assert response.status_code == 400, bad_id
        assert response.get_json()["error"] == "invalid_room_id"


def test_voice_config_read_requires_room_membership(tmp_path):
    app = _app(tmp_path, role="USER", user_id=2)
    _write_room(tmp_path, room_id="r-1", user_id=1)
    client = app.test_client()
    _login(client, user_id=2, role="USER")

    response = client.get("/api/voice/config", query_string={"room_id": "r-1"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "Permission denied"


def test_voice_config_member_can_read_room_config(tmp_path):
    app = _app(tmp_path, role="USER", user_id=1)
    _write_room(tmp_path, room_id="r-1", user_id=1)
    client = app.test_client()
    _login(client, user_id=1, role="USER")

    response = client.get("/api/voice/config", query_string={"room_id": "r-1"})

    assert response.status_code == 200
    settings = response.get_json()["data"]["settings"]
    assert "room" in settings and "npc_profiles" in settings
