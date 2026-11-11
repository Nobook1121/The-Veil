"""本地多音色语音（P2）：缓存、降级链、并发闸门、模型清单、权限与命令预演。"""
from __future__ import annotations

import json

import pytest

from trpg_server.app_factory import create_app
from trpg_server.config import update_sections
from trpg_server.config.paths import general_config_path, resolve_config_dir
from trpg_server.console_commands import execute_console_command
from trpg_server.immersive import task_gate
from trpg_server.voice import local_cache, local_models, local_service
from trpg_server.voice.director import prepare_voice
from trpg_server.voice.engine import REASON_ENGINE_UNAVAILABLE, VoiceSynthesisError, engine_health, synthesize


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


# --- 缓存 ---------------------------------------------------------------

def test_cache_key_normalizes_only_whitespace_and_keeps_punctuation():
    first = local_cache.cache_key("  你好，   世界。 ", 57, 1.0, model="m")
    second = local_cache.cache_key("你好， 世界。", 57, 1.0, model="m")
    assert first == second
    assert local_cache.cache_key("你好，世界。", 57, 1.0, model="m") != first
    assert local_cache.cache_key("你好， 世界。", 58, 1.0, model="m") != first
    assert local_cache.cache_key("你好， 世界。", 57, 1.2, model="m") != first
    assert local_cache.cache_key("你好， 世界。", 57, 1.0, model="m", engine="cloud") != first
    assert local_cache.normalize_text(" a\n\tb ") == "a b"


def test_cache_store_lookup_and_empty_audio_rejected(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    key = local_cache.cache_key("你好。", 57, 1.0, model="m")
    rejected, reason, _ = local_cache.store(
        room_dir, key, content=b"", voice_id="57", model="m",
        settings={"cache_quota_mb": 64, "cache_max_entries": 300},
    )
    assert rejected is False and reason == "empty_audio"
    stored, reason, entry = local_cache.store(
        room_dir, key, content=b"RIFF-audio", voice_id="57", model="m",
        settings={"cache_quota_mb": 64, "cache_max_entries": 300},
    )
    assert stored is True and reason == "written"
    assert entry["bytes"] == len(b"RIFF-audio")
    hit = local_cache.lookup(room_dir, key)
    assert hit is not None
    assert hit["path"].read_bytes() == b"RIFF-audio"
    assert hit["entry"]["hits"] == 1
    report = local_cache.stats(room_dir)
    assert report["files"] == 1 and report["hits"] == 1


def test_cache_lru_evicts_oldest_and_reject_keeps_existing(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    lru = {"cache_quota_mb": 0, "cache_max_entries": 2, "cache_overflow": "lru"}
    keys = [local_cache.cache_key(f"第{i}句。", 57, 1.0, model="m") for i in range(3)]
    for key in keys[:2]:
        assert local_cache.store(room_dir, key, content=b"a", voice_id="57", model="m", settings=lru)[0]
    assert local_cache.store(room_dir, keys[2], content=b"b", voice_id="57", model="m", settings=lru)[0]
    assert local_cache.lookup(room_dir, keys[0]) is None
    assert local_cache.lookup(room_dir, keys[1]) is not None
    assert local_cache.stats(room_dir)["files"] == 2

    reject_dir = tmp_path / "reject"
    reject_dir.mkdir()
    reject = {"cache_quota_mb": 0, "cache_max_entries": 1, "cache_overflow": "reject"}
    assert local_cache.store(reject_dir, keys[0], content=b"a", voice_id="57", model="m", settings=reject)[0]
    ok, reason, _ = local_cache.store(reject_dir, keys[1], content=b"b", voice_id="57", model="m", settings=reject)
    assert ok is False and reason == "quota_exceeded"
    assert local_cache.lookup(reject_dir, keys[0]) is not None


# --- 降级链与音色绑定 ---------------------------------------------------

def test_local_health_and_synthesis_degrade_with_specific_reason():
    health = engine_health({"engine": "local", "engine_allowed": ["system", "local"], "local_enabled": False})
    assert health["ok"] is False and health["reason"] == local_service.REASON_LOCAL_DISABLED
    with pytest.raises(VoiceSynthesisError) as error:
        synthesize({"text": "hi"}, {"id": "narrator"}, engine="local", engine_allowed=["local"], settings={})
    assert error.value.reason == REASON_ENGINE_UNAVAILABLE


def test_director_falls_back_to_system_when_local_unavailable(tmp_path, monkeypatch):
    room_dir = _write_room(tmp_path)
    config_dir = _write_site_config(
        tmp_path, voice_enabled=True, voice_engine="local",
        voice_engine_allowed="system,local", voice_local_enabled=True,
    )
    monkeypatch.setattr(
        "trpg_server.voice.director.voice_engine.engine_health",
        lambda settings: {"engine": "local", "ok": False, "reason": "local_unavailable", "mode": "server"},
    )
    result = prepare_voice(
        "【NPC:艾德温】“你们终于来了。”",
        room_id="r-1", room_dir=room_dir, config_dir=config_dir,
        scenario={"npcs": [{"name": "艾德温", "gender": "男", "age": "中年"}]},
    )
    voice = result["voice"]
    assert voice["playable"] is True
    assert voice["engine"] == "system" and voice["fallback_from"] == "local"
    assert voice["degraded"] is True and voice["reason"] == "local_unavailable"
    assert voice["segments"][0]["engine"] == "system"


def test_speaker_binding_precedence_and_default_coverage():
    settings = {"local_speakers": {"narrator": 52}, "local_model": "m"}
    site = local_service.speaker_binding("narrator", settings)
    assert site["sid"] == 52 and site["source"] == "site"
    room = local_service.speaker_binding("narrator", settings, {"local_speakers": {"narrator": 62}})
    assert room["sid"] == 62 and room["source"] == "room"
    assert local_service.speaker_binding("npc_male_elder", {"local_model": "m"})["source"] == "default"
    assert set(local_models.DEFAULT_SPEAKERS) == {
        "narrator", "npc_male_teen", "npc_male_youth", "npc_male_middle", "npc_male_elder",
        "npc_female_teen", "npc_female_youth", "npc_female_middle", "npc_female_elder",
    }
    assert local_models.speaker_gender(local_models.DEFAULT_SPEAKERS["narrator"]) == "female"
    assert local_models.speaker_gender(local_models.DEFAULT_SPEAKERS["npc_male_elder"]) == "male"


# --- 并发闸门 -----------------------------------------------------------

def test_concurrency_gate_queues_while_illustration_runs_and_enforces_limit():
    with task_gate.heavy_task():
        assert local_service._acquire(1, 0.3) is False
    assert local_service._acquire(1, 0.3) is True
    # 占用后第二个请求在超时内拿不到第二个名额。
    assert local_service._acquire(1, 0.3) is False
    local_service._release()
    assert local_service.active_requests() == 0


# --- 模型清单 -----------------------------------------------------------

def test_installed_status_detects_missing_and_size_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(local_models, "models_root", lambda: tmp_path / "models")
    directory = tmp_path / "models" / local_models.MODEL_ID
    directory.mkdir(parents=True)
    status = local_models.installed_status()
    assert status["installed"] is False and status["missing"]

    files: dict[str, int] = {}
    for name in (*local_models.REQUIRED_FILES, *local_models.ESPEAK_FILES):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        files[name] = 1
    for name in local_models.REQUIRED_DIRS:
        (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / local_models.INSTALL_META).write_text(
        json.dumps({"files": files, "total_bytes": len(files), "installed_at": "2026-01-01", "source": "test"}),
        encoding="utf-8",
    )
    assert local_models.installed_status()["installed"] is True
    (directory / "lexicon-zh.txt").write_bytes(b"xx")
    broken = local_models.installed_status()
    assert broken["installed"] is False and broken["size_mismatch"] == ["lexicon-zh.txt"]


# --- HTTP 权限 -----------------------------------------------------------

def test_local_endpoints_permissions_and_room_cache_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, role="USER")
    _write_room(tmp_path)
    client = app.test_client()
    assert client.get("/api/voice/local/status").status_code == 401

    _login(client, role="USER")
    assert client.get("/api/voice/local/status").status_code == 200
    denied = client.post("/api/voice/local/install", json={}, headers={"X-CSRF-Token": "csrf"})
    assert denied.status_code == 403

    from trpg_server.routes import voice as voice_routes

    monkeypatch.setattr(voice_routes.local_models, "install_in_background", lambda force=False: (True, "fake"))
    monkeypatch.setattr(voice_routes.local_models, "remove", lambda: (True, "fake"))
    admin_client = _app(tmp_path, role="ADMIN").test_client()
    _login(admin_client, role="ADMIN")
    assert admin_client.post("/api/voice/local/install", json={}, headers={"X-CSRF-Token": "csrf"}).status_code == 200
    assert admin_client.post("/api/voice/local/remove", json={}, headers={"X-CSRF-Token": "csrf"}).status_code == 200

    outsider = _app(tmp_path, role="USER", user_id=2).test_client()
    _login(outsider, user_id=2)
    assert outsider.post("/api/voice/cache/clear", json={"room_id": "r-1"},
                         headers={"X-CSRF-Token": "csrf"}).status_code == 403
    assert client.post("/api/voice/cache/clear", json={"room_id": "r-1"},
                       headers={"X-CSRF-Token": "csrf"}).status_code == 200


# --- 命令预演 -----------------------------------------------------------

def test_console_local_install_and_cache_clear_are_dry_run(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    preview = execute_console_command("voice local install --force", app=app)
    assert preview.ok and "预演" in preview.message
    local_cache.store(
        tmp_path / "rooms" / "r-1",
        local_cache.cache_key("你好。", 57, 1.0, model="m"),
        content=b"voice", voice_id="57", model="m",
    )
    clear_preview = execute_console_command("voice cache clear --room r-1", app=app, rooms_dir=tmp_path / "rooms")
    assert clear_preview.ok and "预演" in clear_preview.message
    assert local_cache.stats(tmp_path / "rooms" / "r-1")["files"] == 1
    stats = execute_console_command("voice cache stats --room r-1", app=app, rooms_dir=tmp_path / "rooms")
    assert stats.ok and "语音缓存" in stats.message
