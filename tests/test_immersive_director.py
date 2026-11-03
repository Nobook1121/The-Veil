"""生图平台表与 LLM 音乐导演：schema、回退、缓存、预算、权限与迁移。"""

import json

from trpg_server.app_factory import create_app
from trpg_server.config import load_settings
from trpg_server.immersive import audit, director, image_platforms
from trpg_server.immersive.director_schema import validate_score
from trpg_server.immersive.music import apply_director, load_state, plan_music


class FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    manager = FakeManager(role)
    app = create_app({
        "TESTING": True,
        "USER_MANAGER": manager,
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })
    app.test_manager = manager
    return app


def _login(client, user_id=1, role="ADMIN"):
    manager = getattr(client.application, "test_manager", None)
    if manager is not None:
        manager.role = role
    # 写接口需要与会话一致的 CSRF token（session 守卫在 /api/ 下强制校验）。
    client.environ_base["HTTP_X_CSRF_TOKEN"] = "csrf"
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _score(**overrides):
    base = {
        "scene": "雨夜的车站，远处传来汽笛",
        "mood": "suspense",
        "mode": "dorian",
        "tempo": 84,
        "chords": [{"root": 2, "quality": "minor"}, {"root": 7, "quality": "sus4"}],
        "instrumentation": ["pad", "bass", "bells"],
        "motif": [0, 3, 7, 2],
        "sections": [
            {"label": "靠近", "dynamic": "low", "bars": 4},
            {"label": "发现", "dynamic": "high", "bars": 6},
        ],
        "dynamics": [0.35, 0.8],
        "confidence": 82,
    }
    base.update(overrides)
    return base


def _analysis():
    return {"profile": "suspense", "intensity": "medium", "scene": "车站", "confidence": "high", "reason": "test"}


def _music_settings(**overrides):
    base = {
        "music_enabled": True,
        "music_auto_switch": True,
        "music_default_profile": "daily",
        "music_default_intensity": "medium",
        "music_crossfade_ms": 3000,
        "music_hysteresis_turns": 2,
        "music_seed": "",
        "music_duck_bgm": True,
        "music_director_enabled": True,
        "music_director_platform": "",
        "music_director_model": "",
        "music_director_interval_turns": 2,
        "music_director_daily_budget": 0,
        "music_director_timeout": 45,
        "music_director_cache_ttl": 3600,
        "music_director_min_confidence": 55,
        "music_director_fallback": "rule",
        "music_director_max_context_chars": 1200,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- schema

def test_validate_score_accepts_valid_payload_and_whitelists():
    score, errors, confidence = validate_score(_score(extra="ignored"))
    assert errors == []
    assert confidence == 82
    assert "extra" not in score
    assert score["chords"][0]["intervals"] == [0, 3, 7]
    assert score["tempo"] == 84


def test_validate_score_rejects_type_and_range_errors():
    bad = _score(tempo=999, chords=[{"root": 12, "quality": "major"}], dynamics=[2.0, 0.5])
    score, errors, confidence = validate_score(bad)
    assert score is None
    assert any("tempo" in item for item in errors)
    assert any("chords" in item for item in errors)
    assert any("dynamics" in item for item in errors)
    assert confidence == 82


def test_validate_score_rejects_low_confidence():
    score, errors, confidence = validate_score(_score(confidence=20), min_confidence=55)
    assert score is None
    assert errors == ["low_confidence"]
    assert confidence == 20


# ---------------------------------------------------------------- 导演缓存/预算/回退

def test_director_cache_hit_skips_second_call(tmp_path):
    cache_path = tmp_path / "cache.json"
    usage_path = tmp_path / "usage.json"
    calls = {"count": 0}

    def request_fn(**kwargs):
        calls["count"] += 1
        return {"raw": _score(), "model": "m", "platform": "p", "elapsed_ms": 12}

    kwargs = dict(
        room_dir=None,
        room_id="r-1",
        text="列车进站，雾里有人影。",
        analysis=_analysis(),
        plan={"profile": "suspense"},
        audio_state={"profile": "daily", "intensity": "low", "section": 1},
        settings=_music_settings(),
        cache_path=cache_path,
        usage_path=usage_path,
        request_fn=request_fn,
    )
    first = director.run(**kwargs)
    assert first["accepted"] is True
    assert first["cached"] is False
    assert first["validation"] == "ok"
    assert calls["count"] == 1

    def _boom(**kwargs):
        calls["count"] += 1
        raise AssertionError("cache hit should not call the model")

    second = director.run(**{**kwargs, "request_fn": _boom})
    assert second["accepted"] is True
    assert second["cached"] is True
    assert second["validation"] == "cache_hit"
    assert calls["count"] == 1


def test_director_budget_exhausted_falls_back(tmp_path):
    usage_path = tmp_path / "usage.json"
    settings = _music_settings(music_director_daily_budget=1)

    def request_fn(**kwargs):
        return {"raw": _score(), "model": "m", "platform": "p", "elapsed_ms": 5}

    first = director.run(
        room_dir=None, room_id="r-1", text="第一回合", analysis=_analysis(),
        plan={}, audio_state={}, settings=settings,
        cache_path=tmp_path / "cache.json", usage_path=usage_path, request_fn=request_fn,
    )
    assert first["accepted"] is True

    second = director.run(
        room_dir=None, room_id="r-1", text="第二回合", analysis=_analysis(),
        plan={}, audio_state={}, settings=settings,
        cache_path=tmp_path / "cache-second.json", usage_path=usage_path, request_fn=request_fn,
    )
    assert second["accepted"] is False
    assert second["fallback_reason"] == "director_daily_budget_exhausted"
    assert second["score"] is None


def test_director_invalid_output_and_error_fall_back(tmp_path):
    invalid = director.run(
        room_dir=None, room_id="r-1", text="场景", analysis=_analysis(),
        plan={}, audio_state={}, settings=_music_settings(),
        cache_path=tmp_path / "c.json", usage_path=tmp_path / "u.json",
        request_fn=lambda **kwargs: {"raw": {"mood": "unknown"}, "model": "m", "platform": "p", "elapsed_ms": 3},
    )
    assert invalid["accepted"] is False
    assert invalid["validation"] == "invalid"
    assert invalid["fallback_reason"]

    def _boom(**kwargs):
        raise RuntimeError("upstream down")

    failed = director.run(
        room_dir=None, room_id="r-1", text="场景", analysis=_analysis(),
        plan={}, audio_state={}, settings=_music_settings(),
        cache_path=tmp_path / "c2.json", usage_path=tmp_path / "u2.json", request_fn=_boom,
    )
    assert failed["accepted"] is False
    assert failed["validation"] == "error"
    assert "upstream down" in failed["fallback_reason"]


def test_should_trigger_event_and_interval():
    settings = _music_settings()
    due, reason = director.should_trigger({"changed": True, "reason": "escalate", "turns": 1}, settings=settings)
    assert due is True
    assert reason == "event:escalate"
    due, reason = director.should_trigger({"changed": False, "turns": 4}, settings=settings)
    assert due is True
    assert reason == "interval:2"
    due, reason = director.should_trigger({"changed": False, "turns": 3}, settings=settings)
    assert due is False
    assert reason == "not_due"


def test_apply_director_persists_score_and_section(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    plan = plan_music(room_dir=room_dir, room_id="r-1", analysis=_analysis(), settings=_music_settings())
    updated = apply_director(
        room_dir=room_dir, plan=plan, score=_score(), fingerprint="fp-1",
        model="m", platform="p", confidence=82,
    )
    assert updated["profile"] == "suspense"
    assert updated["intensity"] == "low"
    assert updated["score"]["mode"] == "dorian"
    state = load_state(room_dir)
    assert state["director"]["fingerprint"] == "fp-1"
    assert state["director"]["score"]["mood"] == "suspense"
    # 同一指纹再次命中时轮换乐谱内部段落。
    again = apply_director(
        room_dir=room_dir, plan=updated, score=_score(), fingerprint="fp-1",
        model="m", platform="p", confidence=82, cached=True,
    )
    assert again["intensity"] == "high"
    assert load_state(room_dir)["director"]["section_index"] == 1


# ---------------------------------------------------------------- 生图平台

def _save_platform(config_dir, secrets, **overrides):
    payload = {
        "id": "openai-main",
        "name": "OpenAI 主账号",
        "protocol": "openai_images",
        "base_url": "https://api.openai.com",
        "model": "gpt-image-1",
        "size": "1024x1024",
        "timeout": 120,
        "enabled": True,
        "api_key": "sk-test-123456789",
    }
    payload.update(overrides)
    return image_platforms.save_platform(
        config_dir, payload, secret_dir_override=secrets,
        audit_path=config_dir / "audit.jsonl",
    )


def test_platform_secret_is_masked_and_default_is_written(tmp_path):
    config_dir = tmp_path / "config"
    secrets = tmp_path / "secrets"
    saved = _save_platform(config_dir, secrets)
    assert saved["has_api_key"] is True
    assert saved["api_key_masked"].startswith("sk-t")
    assert "api_key" not in saved

    listed = image_platforms.list_platforms(config_dir, secrets)
    assert listed[0]["id"] == "openai-main"
    assert listed[0]["is_default"] is False

    assert image_platforms.set_default_platform(config_dir, "openai-main") is True
    settings = load_settings(config_dir)
    section = settings.data["immersive"]
    assert section["illustration_provider"] == "platform"
    assert section["illustration_platform_id"] == "openai-main"
    assert image_platforms.default_platform_id(config_dir) == "openai-main"

    rotated = image_platforms.set_platform_secret(config_dir, "openai-main", "sk-new-987654", secret_dir_override=secrets)
    assert rotated["api_key_masked"].startswith("sk-n")
    cleared = image_platforms.set_platform_secret(config_dir, "openai-main", "", secret_dir_override=secrets)
    assert cleared["has_api_key"] is False
    assert cleared["api_key_masked"] == ""


def test_platform_delete_default_falls_back_to_procedural(tmp_path):
    config_dir = tmp_path / "config"
    secrets = tmp_path / "secrets"
    _save_platform(config_dir, secrets)
    image_platforms.set_default_platform(config_dir, "openai-main")
    assert image_platforms.delete_platform(config_dir, "openai-main") is True
    section = load_settings(config_dir).data["immersive"]
    assert section["illustration_provider"] == "procedural"
    assert section["illustration_platform_id"] == ""
    assert image_platforms.list_platforms(config_dir, secrets) == []


def test_legacy_migration_clears_old_single_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "[immersive]\n"
        'illustration_provider = "openai_compatible"\n'
        'illustration_base_url = "https://legacy.example.com"\n'
        'illustration_model = "dall-e-3"\n'
        'illustration_size = "1792x1024"\n'
        'api_key = "sk-legacy"\n',
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets"
    migrated = image_platforms.migrate_legacy(
        config_dir, secret_dir_override=secrets, audit_path=config_dir / "audit.jsonl"
    )
    assert migrated == "legacy-default"
    platforms = image_platforms.list_platforms(config_dir, secrets)
    assert len(platforms) == 1
    assert platforms[0]["base_url"] == "https://legacy.example.com"
    assert platforms[0]["size"] == "1792x1024"
    assert platforms[0]["has_api_key"] is True

    section = load_settings(config_dir).data["immersive"]
    assert section["illustration_provider"] == "platform"
    assert section["illustration_platform_id"] == "legacy-default"
    assert section["illustration_base_url"] == ""
    assert section["api_key"] == ""

    # 第二次调用不应重复迁移。
    assert image_platforms.migrate_legacy(config_dir, secret_dir_override=secrets) is None


def test_platform_audit_records_actor_and_redacts_key(tmp_path):
    config_dir = tmp_path / "config"
    secrets = tmp_path / "secrets"
    audit_path = config_dir / "audit.jsonl"
    image_platforms.save_platform(
        config_dir,
        {
            "id": "p1", "name": "P1", "protocol": "openai_images",
            "base_url": "https://example.com", "model": "m", "size": "1024x1024",
            "api_key": "sk-super-secret",
        },
        secret_dir_override=secrets,
        actor={"user_id": 7, "username": "admin", "role": "ADMIN"},
        audit_path=audit_path,
    )
    text = audit_path.read_text(encoding="utf-8")
    assert "sk-super-secret" not in text
    entry = json.loads(text.splitlines()[-1])
    assert entry["action"] == "platform.save"
    assert entry["actor"].startswith("admin")
    assert entry["result"] == "ok"
    assert entry["detail"]["key_state"] == "updated"


# ---------------------------------------------------------------- 路由权限与接口

def test_platform_routes_require_admin_and_mask_secrets(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(image_platforms, "secret_dir", lambda config_dir=None, secret_dir_override=None: secrets)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()

    _login(client, role="USER")
    denied = client.get("/api/immersive/platforms")
    assert denied.status_code == 403

    _login(client, role="ADMIN")
    created = client.post("/api/immersive/platforms", json={
        "id": "r1", "name": "Route Platform", "protocol": "openai_images",
        "base_url": "https://example.com", "model": "gpt-image-1",
        "size": "1024x1024", "api_key": "sk-route-secret",
    })
    assert created.status_code == 200
    platform = created.get_json()["data"]["platform"]
    assert platform["has_api_key"] is True
    assert "sk-route-secret" not in json.dumps(created.get_json())

    defaulted = client.post("/api/immersive/platforms/r1/default")
    assert defaulted.status_code == 200
    assert defaulted.get_json()["data"]["default_platform_id"] == "r1"

    rotated = client.post("/api/immersive/platforms/r1/secret", json={"clear": True})
    assert rotated.status_code == 200
    assert rotated.get_json()["data"]["platform"]["has_api_key"] is False

    deleted = client.delete("/api/immersive/platforms/r1")
    assert deleted.status_code == 200
    assert deleted.get_json()["data"]["platforms"] == []


def test_immersive_config_exposes_platforms_only_to_admin(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(image_platforms, "secret_dir", lambda config_dir=None, secret_dir_override=None: secrets)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    app = _app(tmp_path, role="USER")
    client = app.test_client()

    _login(client, role="USER")
    response = client.get("/api/immersive/config")
    assert response.status_code == 200
    assert "platforms" not in response.get_json()["data"]

    admin_client = _app(tmp_path, role="ADMIN").test_client()
    _login(admin_client, role="ADMIN")
    admin_response = admin_client.get("/api/immersive/config")
    data = admin_response.get_json()["data"]
    assert "platforms" in data
    assert "presets" in data
    assert "director" in data


def test_director_cache_clear_route(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "CACHE_PATH", tmp_path / "director_cache.json")
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    director.put_cache("fp-x", _score(), model="m", platform="p", confidence=82)
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()
    _login(client, role="ADMIN")
    response = client.post("/api/immersive/director/cache/clear")
    assert response.status_code == 200
    assert response.get_json()["data"]["cleared"] == 1
    assert not (tmp_path / "director_cache.json").exists()
