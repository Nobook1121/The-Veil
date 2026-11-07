"""实时沉浸系统：氛围分析、配乐导演、程序化配图、预算与房规、控制台命令。

这些测试只覆盖确定性逻辑与本地降级路径，不访问任何外部生图服务。
"""

import json
from pathlib import Path

from trpg_server.app_factory import create_app
from trpg_server.immersive import illustration as illus
from trpg_server.immersive.analyzer import analyze_narrative
from trpg_server.immersive.config import (
    load_immersive_settings,
    public_settings,
    resolve_room_switches,
)
from trpg_server.immersive.music import plan_music
from trpg_server.immersive.svg_scene import render_scene_svg


class FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(role),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, user_id=1, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", room_role="admin", creator_id=1, user_id=1):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Immersive Room",
        "creator_id": creator_id,
        "members": [
            {
                "user_id": user_id, "username": "tester", "role": "USER", "status": "active",
                "is_active": True, "room_role": room_role,
            }
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _music_settings(**overrides):
    base = {
        "music_enabled": True,
        "music_auto_switch": True,
        "music_default_profile": "daily",
        "music_default_intensity": "medium",
        "music_volume": 35,
        "music_crossfade_ms": 3000,
        "music_hysteresis_turns": 2,
        "music_seed": "",
        "music_duck_bgm": True,
    }
    base.update(overrides)
    return base


def _analysis(profile, intensity="medium", scene="场景", confidence="high"):
    return {"profile": profile, "intensity": intensity, "scene": scene, "confidence": confidence, "reason": "test"}


# ---------------------------------------------------------------- 氛围分析

def test_analyzer_deterministic_and_keyword_driven():
    text = "地板上躺着一具尸体，触手从阴影里蠕动。"
    first = analyze_narrative(text)
    second = analyze_narrative(text)
    assert first == second
    assert first["profile"] == "horror"
    assert first["scene"]


def test_analyzer_falls_back_to_default_on_low_confidence():
    result = analyze_narrative("我们走进阳光明媚的商店，买了面包。", default_profile="daily", default_intensity="low")
    assert result["profile"] == "daily"
    assert result["confidence"] == "low"


def test_analyzer_prefers_structured_scene_label():
    result = analyze_narrative("雾气很重。", structured={"next_scene": {"title": "旧车站"}})
    assert result["scene"] == "旧车站"


# ---------------------------------------------------------------- 配乐导演

def test_music_initial_and_hysteresis(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()

    first = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "medium"), settings=settings,
        scenario_id="s-1",
    )
    assert first["profile"] == "daily"
    assert first["section"] == 0
    assert first["turns"] == 1
    assert first["seed"].startswith("r-1:s-1:")

    # 第一次指向恐怖：进入 pending，不立刻切换。
    second = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("horror", "medium"), settings=settings,
        scenario_id="s-1",
    )
    assert second["profile"] == "daily"
    assert second["changed"] is False
    assert second["reason"] == "pending:1"

    # 连续第二次命中滞回阈值才切换，并推进段落。
    third = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("horror", "medium"), settings=settings,
        scenario_id="s-1",
    )
    assert third["profile"] == "horror"
    assert third["changed"] is True
    assert third["section"] == 1
    assert third["reason"] == "hysteresis"


def test_music_escalates_immediately_on_danger(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()
    plan_music(room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "low"), settings=settings)
    escalated = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("urgency", "high"), settings=settings,
    )
    assert escalated["profile"] == "urgency"
    assert escalated["intensity"] == "high"
    assert escalated["changed"] is True
    assert escalated["reason"] == "escalate"


def test_music_section_follows_cue_not_turn_count(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()
    sections = []
    positions = []
    epochs = []
    for _ in range(7):
        plan = plan_music(
            room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "medium"), settings=settings,
        )
        sections.append(plan["section"])
        positions.append(plan["sync"]["position_bars"])
        epochs.append(plan["sync"]["epoch"])
    # 段落只由内容变化驱动：同一氛围持续 7 回合仍在自己 cue 的同一段里推进，
    # 不再每 4 回合硬切一次（否则慢回合的 cue 会在高潮前被替换）。
    assert sections == [0] * 7
    assert len(set(epochs)) == 1
    assert positions == sorted(positions)


def test_music_sync_position_survives_new_turns(tmp_path):
    """同一谱面在时间推进后位置继续前进，而不是每次重新从 0 小节开始。"""
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()
    first = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("suspense", "medium"), settings=settings,
    )
    second = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("suspense", "medium"), settings=settings,
    )
    assert first["sync"]["epoch"] == second["sync"]["epoch"]
    assert second["sync"]["position_bars"] >= first["sync"]["position_bars"]


def test_music_cue_restarts_only_when_content_changes(tmp_path):
    """换档位才开启新 cue；强度变化只影响演奏，不算换歌。"""
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()
    base = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "low"), settings=settings,
    )
    louder = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "high"), settings=settings,
    )
    assert louder["sync"]["epoch"] == base["sync"]["epoch"]
    # 档位滞回需要连续两帧确认（settings.music_hysteresis_turns=2）。
    for _ in range(2):
        switched = plan_music(
            room_dir=room_dir, room_id="r-1", analysis=_analysis("urgency", "high"), settings=settings,
        )
    assert switched["profile"] == "urgency"
    assert switched["sync"]["epoch"] > base["sync"]["epoch"]


def test_music_seed_is_reproducible_across_rooms(tmp_path):
    settings = _music_settings(music_seed="fixed-seed")
    plans = []
    for name in ("a", "b"):
        room_dir = tmp_path / name
        room_dir.mkdir()
        plans.append(plan_music(
            room_dir=room_dir, room_id="same-room", analysis=_analysis("suspense", "medium"),
            settings=settings, scenario_id="s-1",
        ))
    assert plans[0]["seed"] == plans[1]["seed"] == "fixed-seed:0"
    assert plans[0]["motif"] == plans[1]["motif"]


def test_music_auto_switch_disabled_keeps_profile(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings(music_auto_switch=False)
    plan_music(room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "medium"), settings=settings)
    frozen = plan_music(room_dir=room_dir, room_id="r-1", analysis=_analysis("horror", "high"), settings=settings)
    assert frozen["profile"] == "daily"
    assert frozen["reason"] == "auto_disabled"


def test_music_manual_directive_applies_and_advances_section(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    settings = _music_settings()
    plan_music(room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "medium"), settings=settings)
    manual = plan_music(
        room_dir=room_dir, room_id="r-1", analysis=_analysis("daily", "medium"), settings=settings,
        force_profile="awe", force_intensity="low",
    )
    assert manual["profile"] == "awe"
    assert manual["intensity"] == "low"
    assert manual["reason"] == "manual"
    assert manual["section"] == 1


# ---------------------------------------------------------------- 程序化 SVG

def test_svg_scene_is_deterministic_and_mood_specific():
    same_a = render_scene_svg("horror", "high", "教堂地下的血池", "r-1:1")
    same_b = render_scene_svg("horror", "high", "教堂地下的血池", "r-1:1")
    other = render_scene_svg("daily", "low", "阳光下的街道", "r-1:2")
    assert same_a == same_b
    assert same_a != other
    assert same_a.startswith("<svg")
    assert "教堂地下的血池" in same_a


# ---------------------------------------------------------------- 预算 / 配额 / 降级

def test_room_switches_only_tighten_site_settings():
    settings = {"enabled": True, "music_enabled": True, "music_auto_switch": True,
                "illustration_enabled": True, "illustration_auto": True}
    closed = resolve_room_switches(settings, {
        "immersive_music_enabled": False,
        "immersive_illustration_enabled": False,
    })
    assert closed["music"] is False
    assert closed["illustration"] is False
    # 房间不能把站点关闭的能力重新打开。
    site_off = dict(settings, enabled=False)
    reopened = resolve_room_switches(site_off, {"immersive_music_enabled": True})
    assert reopened["music"] is False


def test_public_settings_redacts_api_key():
    settings = load_immersive_settings()
    settings["api_key"] = "sk-secret"
    view = public_settings(settings)
    assert "api_key" not in view
    assert view["has_api_key"] is True
    assert "locked" not in view


def test_legacy_immersive_ai_fields_are_ignored(tmp_path):
    """旧配置里残留的 AI 平台/模型/密钥字段不应报错，也不应再被读取。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "[immersive]\n"
        'music_director_platform = "legacy-platform"\n'
        'music_director_model = "legacy-model"\n'
        'illustration_kp_platform = "legacy-platform"\n'
        'illustration_kp_model = "legacy-model"\n'
        'voice_cloud_api_key = "sk-legacy"\n'
        "music_director_interval_turns = 3\n",
        encoding="utf-8",
    )
    settings = load_immersive_settings(config_dir)
    for legacy in (
        "music_director_platform",
        "music_director_model",
        "illustration_kp_platform",
        "illustration_kp_model",
        "voice_cloud_api_key",
    ):
        assert legacy not in settings
    assert settings["music_director_interval_turns"] == 3


def test_budget_and_room_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(illus, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(illus, "USAGE_PATH", tmp_path / "usage.json")
    settings = {"illustration_daily_budget": 1, "illustration_room_hourly_cap": 0}

    assert illus.reserve_external("r-1", settings)[0] is True
    allowed, reason = illus.reserve_external("r-1", settings)
    assert allowed is False
    assert reason == "daily_budget_exhausted"

    room_capped = {"illustration_daily_budget": 0, "illustration_room_hourly_cap": 1}
    assert illus.reserve_external("r-2", room_capped)[0] is True
    allowed, reason = illus.reserve_external("r-2", room_capped)
    assert allowed is False
    assert reason == "room_hourly_cap"


def test_turn_counter_and_cooldown(monkeypatch, tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    assert illus.next_turn_index(room_dir) == 1
    assert illus.next_turn_index(room_dir) == 2
    assert illus.should_auto_generate(room_dir, 2, 4) == (True, "")
    illus._record_history(room_dir, {"turn": 2, "url": "/x.svg"})
    allowed, reason = illus.should_auto_generate(room_dir, 3, 4)
    assert allowed is False
    assert reason == "cooldown:1/4"


def test_procedural_generation_is_not_marked_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(illus, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(illus, "USAGE_PATH", tmp_path / "usage.json")
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    payload = illus.generate_now(
        room_dir=room_dir,
        room_id="r-1",
        scene="雨夜的旧车站",
        profile="suspense",
        intensity="medium",
        seed="r-1:1",
        turn_index=1,
        settings={"illustration_provider": "procedural", "enabled": True, "illustration_enabled": True},
    )
    assert payload["provider"] == "procedural"
    assert payload["degraded"] is False
    assert payload["error"] == ""
    assert payload["url"].startswith(illus.PUBLIC_IMAGE_PREFIX)
    assert (room_dir / illus.ILLUSTRATION_STATE_FILENAME).is_file()


def test_resolve_image_path_blocks_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(illus, "IMAGE_CACHE_DIR", tmp_path / "images")
    (tmp_path / "images").mkdir()
    name = "a" * 24 + ".svg"
    (tmp_path / "images" / name).write_text("<svg/>", encoding="utf-8")
    assert illus.resolve_image_path(name) is not None
    assert illus.resolve_image_path("../../etc/passwd") is None
    assert illus.resolve_image_path(f"{name}/../../x") is None


# ---------------------------------------------------------------- 控制台

def test_console_help_lists_immersive_commands():
    from trpg_server.console_commands import command_help_text

    text = command_help_text()
    assert "immersive status" in text
    assert "immersive illustration provider" in text
    assert "immersive key set" in text


def test_console_destructive_command_is_dry_run_by_default(tmp_path):
    from trpg_server.immersive_admin import handle_immersive_command

    config_dir = tmp_path / "config"
    ok, message = handle_immersive_command(["off"], config_dir=config_dir)
    assert ok is True
    assert "预演" in message
    assert not (config_dir / "general.toml").exists()

    ok, message = handle_immersive_command(["off", "--yes"], config_dir=config_dir)
    assert ok is True
    assert "已关闭" in message
    written = (config_dir / "general.toml").read_text(encoding="utf-8")
    assert "enabled = false" in written


def test_console_status_json_redacts_key(tmp_path):
    from trpg_server.immersive_admin import handle_immersive_command

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text('[immersive]\napi_key = "sk-secret"\n', encoding="utf-8")
    ok, message = handle_immersive_command(["status", "json"], config_dir=config_dir)
    assert ok is True
    payload = json.loads(message)
    assert "sk-secret" not in message
    assert payload["settings"]["api_key"] == "已配置"


# ---------------------------------------------------------------- 路由权限

def test_room_state_exposes_manage_flag_by_role(tmp_path):
    _write_room(tmp_path, room_role="admin")
    # 站点角色为普通玩家：此时房间管理员仍可管理，房间成员不可。
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, user_id=1, role="USER")
    response = client.get("/api/immersive/rooms/r-1/state")
    data = response.get_json()
    assert response.status_code == 200
    assert data["data"]["can_manage"] is True
    assert data["data"]["room_id"] == "r-1"

    _write_room(tmp_path, room_role="member", creator_id=99, user_id=2)
    _login(client, user_id=2, role="USER")
    response = client.get("/api/immersive/rooms/r-1/state")
    data = response.get_json()
    assert response.status_code == 200
    assert data["data"]["can_manage"] is False


def test_room_state_requires_login(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    response = client.get("/api/immersive/rooms/r-1/state")
    assert response.status_code == 401


def test_site_settings_route_requires_admin(tmp_path):
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER")
    response = client.get("/api/immersive/usage")
    assert response.status_code == 403
