"""音频块模型：互斥/叠加、管理员允许清单、房间子集与迁移。

这些测试只覆盖确定性解析逻辑（不启动 Flask、不播放音频），重点验证：
- 旧 ``music_engine`` 迁移后不会突然静音；
- 管理员收回块后房间优雅降级并带可见原因；
- 外链块与环境音共存，程序化配乐不再作为外链底床；
- 程序化 / 生成模型 / 外链 / 不使用四选一；
- 用户级配乐来源与房间块配置互不影响。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from trpg_server.immersive.config import (  # noqa: E402
    AUDIO_BLOCKS,
    audio_block_options,
    derive_allowed_audio_blocks,
    resolve_audio_blocks,
    resolve_room_switches,
    resolve_music_source,
)
from trpg_server.immersive.external_music import service as external_service  # noqa: E402


def _settings(**overrides):
    base = {
        "enabled": True,
        "music_enabled": True,
        "music_engine": "procedural",
        "audio_blocks_allowed": ["ambient", "procedural", "external", "off"],
        "music_source": "hybrid",
        "music_source_allowed": ["realtime", "bgm", "hybrid"],
        "music_source_allow_personal": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 迁移

@pytest.mark.parametrize(
    ("legacy_engine", "expected_music"),
    [("procedural", "procedural"), ("external", "external"), ("off", "off")],
)
def test_legacy_engine_migrates_to_blocks_without_silence(legacy_engine, expected_music):
    allowed = derive_allowed_audio_blocks(legacy_engine, None)
    assert "ambient" in allowed
    assert expected_music in allowed
    settings = _settings(music_engine=legacy_engine, audio_blocks_allowed=allowed)
    result = resolve_audio_blocks(settings, None)
    assert result["music"] == expected_music
    assert result["ambient"] is True
    assert result["degrade_reason"] == ""


def test_legacy_external_keeps_procedural_downgrade():
    allowed = derive_allowed_audio_blocks("external", None)
    assert "procedural" in allowed
    settings = _settings(music_engine="external", audio_blocks_allowed=allowed)
    engine, reason = external_service.resolve_engine(
        settings, {"immersive_music_engine": "procedural"}
    )
    assert (engine, reason) == ("procedural", "room_procedural")


def test_legacy_off_stays_off():
    allowed = derive_allowed_audio_blocks("off", None)
    settings = _settings(music_engine="off", audio_blocks_allowed=allowed)
    assert resolve_audio_blocks(settings, None)["music"] == "off"
    assert external_service.resolve_engine(settings, None)[0] == "off"


# ---------------------------------------------------------------- 管理员收回 → 房间降级

def test_admin_revoke_room_external_degrades_visibly():
    settings = _settings(audio_blocks_allowed=["ambient", "procedural", "off"])
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["ambient", "external"]})
    assert result["ambient"] is True
    # 不能静默变成静音：回落到管理员仍允许的配乐块。
    assert result["music"] == "procedural"
    assert result["degrade_reason"] == "room_music_block_revoked"
    assert result["degrade_message"]
    assert "external" in result["revoked"]
    assert "external" not in result["blocks"]


def test_admin_ambient_only_room_gets_off_with_reason():
    settings = _settings(audio_blocks_allowed=["ambient"])
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["ambient", "procedural"]})
    assert result["music"] == "off"
    assert result["ambient"] is True
    assert result["degrade_reason"] == "room_music_block_revoked"
    assert result["blocks"] == ["ambient"]


def test_room_block_not_in_admin_allowlist_never_selected():
    settings = _settings(audio_blocks_allowed=["ambient", "procedural", "off"])
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["external"]})
    assert "external" not in result["blocks"]
    assert set(result["blocks"]) <= set(settings["audio_blocks_allowed"])


# ---------------------------------------------------------------- 外链 + 环境音

def test_external_and_ambient_coexist():
    settings = _settings()
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["ambient", "external"]})
    assert result["ambient"] is True
    assert result["music"] == "external"
    assert result["blocks"] == ["ambient", "external"]
    engine, _ = external_service.resolve_engine(
        settings, {"immersive_audio_blocks": ["ambient", "external"]}
    )
    assert engine == "external"


def test_external_without_ambient_keeps_music_only():
    settings = _settings()
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["external"]})
    assert result["ambient"] is False
    assert result["music"] == "external"
    assert result["blocks"] == ["external"]


def test_room_music_disabled_still_keeps_ambient():
    settings = _settings()
    result = resolve_audio_blocks(
        settings,
        {"immersive_music_enabled": False, "immersive_audio_blocks": ["ambient", "procedural"]},
    )
    assert result["music"] == "off"
    assert result["ambient"] is True
    assert result["degrade_reason"] == "room_music_disabled"
    assert result["blocks"] == ["ambient"]


# ---------------------------------------------------------------- 互斥

def test_procedural_and_music_model_are_mutually_exclusive():
    settings = _settings(audio_blocks_allowed=list(AUDIO_BLOCKS))
    result = resolve_audio_blocks(
        settings,
        {"immersive_audio_blocks": ["ambient", "procedural", "music_model"]},
        music_model_available=True,
    )
    music_blocks = [block for block in result["blocks"] if block != "ambient"]
    assert len(music_blocks) == 1
    assert result["degrade_reason"] == "multiple_music_blocks"


def test_music_model_unavailable_degrades_with_reason():
    settings = _settings(audio_blocks_allowed=list(AUDIO_BLOCKS))
    result = resolve_audio_blocks(
        settings,
        {"immersive_audio_blocks": ["ambient", "music_model"]},
        music_model_available=False,
    )
    assert result["music"] != "music_model"
    assert result["degrade_reason"] == "music_model_unavailable"
    assert "music_model" in result["revoked"]
    assert result["ambient"] is True


def test_music_model_block_disabled_with_reason_when_no_platform():
    options = {item["value"]: item for item in audio_block_options(False)}
    assert options["music_model"]["available"] is False
    assert options["music_model"]["unavailable_reason"]
    assert options["procedural"]["available"] is True
    enabled = {item["value"]: item for item in audio_block_options(True)}
    assert enabled["music_model"]["available"] is True


def test_all_four_music_blocks_cover_audio_blocks():
    assert set(AUDIO_BLOCKS) == {"ambient", "procedural", "music_model", "external", "off"}


# ---------------------------------------------------------------- 用户级偏好独立

def test_user_music_source_is_independent_from_room_blocks():
    settings = _settings()
    room = {"immersive_audio_blocks": ["ambient", "external"]}
    before = resolve_music_source(settings, room)
    before_switches = resolve_room_switches(settings, room)
    resolve_audio_blocks(settings, room)
    assert resolve_music_source(settings, room) == before
    assert resolve_room_switches(settings, room) == before_switches
    # 个人关闭配乐只是前端本地开关，不会改写房规里的块选择。
    assert room["immersive_audio_blocks"] == ["ambient", "external"]


def test_site_music_disabled_turns_everything_off():
    settings = _settings(music_enabled=False)
    result = resolve_audio_blocks(settings, {"immersive_audio_blocks": ["ambient", "external"]})
    assert result["ambient"] is False
    assert result["music"] == "off"
    assert result["degrade_reason"] == "site_music_disabled"
    assert external_service.resolve_engine(settings, None)[0] == "off"
