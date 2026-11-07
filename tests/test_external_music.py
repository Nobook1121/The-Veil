"""外链 BGM（P3）与管理员引擎开关（P4）的确定性测试。

覆盖：
- 许可归一化 / 白名单 / 署名完整性；
- 缓存键稳定性、两级缓存、负缓存；
- 引擎降级链（房间不能升级）；
- 曲库解析与许可过滤（完全本地桩，不访问网络）；
- 代理 SSRF 防护。

所有测试都不发真实网络请求。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.immersive.config import load_immersive_settings
from trpg_server.immersive.external_music import cache, license as lic, providers, search, service


def _settings(tmp_path: Path, **overrides):
    settings = load_immersive_settings(tmp_path / "config")
    settings.update(
        {
            "enabled": True,
            "music_enabled": True,
            "music_engine": "external",
            "music_external_providers": "jamendo,openverse",
            "music_external_license_allow": "cc0,by,by-sa",
            "music_external_instrumental_only": True,
            "music_external_url_ttl": 600,
            "music_external_search_ttl": 3600,
            "music_external_daily_budget": 1000,
            "music_external_room_hourly_cap": 1000,
            "music_external_circuit_failures": 3,
            "music_external_circuit_cooldown_s": 60,
        }
    )
    settings.update(overrides)
    return settings


def _track(**overrides):
    track = {
        "provider": "jamendo",
        "track_id": "t-1",
        "title": "Quiet Halls",
        "artist": "Test Artist",
        "license": "https://creativecommons.org/licenses/by-sa/4.0/",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "source_url": "https://www.jamendo.com/track/t-1",
        "stream_url": "https://prod-1.storage.jamendo.com/audio/t-1.mp3",
        "duration_ms": 240_000,
        "attribution": "",
        "instrumental": True,
        "tags": ["ambient", "dark-ambient", "drone"],
    }
    track.update(overrides)
    return track


class _Response:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self._response = response
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return self._response


# ---------------------------------------------------------------- 许可

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://creativecommons.org/licenses/by/4.0/", "by"),
        ("http://creativecommons.org/licenses/by-sa/3.0/deed.zh", "by-sa"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
        ("cc-by-nc", "by-nc"),
        ("CC BY-ND 4.0", "by-nd"),
        ("by", "by"),
        ("CC0", "cc0"),
        ("", "unknown"),
        ("all rights reserved", "unknown"),
    ],
)
def test_normalize_license(raw, expected):
    assert lic.normalize_license(raw) == expected


def test_allowed_licenses_filters_unknown_and_keeps_default():
    assert lic.allowed_licenses("cc0,by,by-sa,by-nc") == ["cc0", "by", "by-sa"]
    assert lic.allowed_licenses("nonsense") == ["cc0", "by", "by-sa"]


def test_license_and_attribution_gate():
    ok, reason = lic.is_playable(_track(), ["cc0", "by", "by-sa"])
    assert ok and reason == "ok"

    rejected, reason = lic.is_playable(_track(license="by-nc"), ["cc0", "by", "by-sa"])
    assert not rejected and reason == "license_rejected:by-nc"

    missing, reason = lic.is_playable(_track(source_url=""), ["by-sa"])
    assert not missing and reason == "attribution_incomplete"

    insecure, reason = lic.is_playable(_track(stream_url="http://example.com/a.mp3"), ["by-sa"])
    assert not insecure and reason == "insecure_stream"


# ---------------------------------------------------------------- 缓存

def test_track_key_and_query_key_are_stable_and_scoped():
    key = cache.track_key("jamendo", "42")
    assert key == cache.track_key("jamendo", "42")
    assert key != cache.track_key("openverse", "42")
    assert key.isalnum() and len(key) >= 24

    first = cache.query_key(
        profile="horror", intensity="high", scene="雨夜教堂",
        tags=["drone", "dark-ambient"], providers=["jamendo", "openverse"],
    )
    second = cache.query_key(
        profile="horror", intensity="high", scene="雨夜教堂",
        tags=["dark-ambient", "drone"], providers=["openverse", "jamendo"],
    )
    assert first == second
    assert first != cache.query_key(
        profile="daily", intensity="high", scene="雨夜教堂",
        tags=["drone"], providers=["jamendo"],
    )


def test_two_level_cache_roundtrip_and_negative_cache(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    scenarios_dir = tmp_path / "scenarios"
    (scenarios_dir / "scenario-s1").mkdir(parents=True)
    room_cache = cache.room_music_dir(room_dir, for_write=True)
    scenario_cache = cache.scenario_music_dir("s1", for_write=True, scenarios_dir=scenarios_dir)
    assert room_cache and room_cache.is_dir()
    assert scenario_cache and scenario_cache.is_dir()
    assert str(scenario_cache).replace("\\", "/").endswith(
        "scenarios/scenario-s1/immersive/music"
    )

    key = cache.remember_track(room_cache, _track(), url_ttl=600)
    assert cache.get_track(room_cache, key)["title"] == "Quiet Halls"

    # 键只接受 24–64 位十六进制（防路径穿越），这里直接构造合法键。
    query_id = "a" * 24
    cache.remember_query(
        room_cache, key=query_id, track_keys=[key], fingerprint="fp", ttl=60
    )
    hit = cache.get_query(room_cache, query_id)
    assert hit and hit["tracks"] == [key]
    assert cache.recent_track_keys(room_cache) == [key]

    # 负缓存：空结果也要能命中，避免同场景反复打曲库。
    negative_id = "b" * 24
    cache.remember_query(room_cache, key=negative_id, track_keys=[], fingerprint="fp", ttl=60)
    negative = cache.get_query(room_cache, negative_id)
    assert negative and negative["tracks"] == []


def test_room_cache_rejects_path_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    assert cache.room_music_dir(outside, for_write=True) is not None
    assert cache.track_key("", "") != ""
    assert cache.audio_path(outside, "../../etc/passwd") is None


# ---------------------------------------------------------------- 引擎降级链

def test_engine_matrix_site_default_and_room_downgrade():
    base = {"enabled": True, "music_enabled": True, "music_engine": "external"}

    assert service.resolve_engine(base, None) == ("external", "site")
    assert service.resolve_engine(base, {"immersive_music_engine": "procedural"}) == (
        "procedural",
        "room_procedural",
    )
    assert service.resolve_engine(base, {"immersive_music_engine": "off"}) == ("off", "room_off")
    assert service.resolve_engine(base, {"immersive_music_engine": "bogus"}) == ("external", "invalid_room_engine")
    assert service.resolve_engine(base, {"immersive_music_enabled": False}) == (
        "off",
        "room_music_disabled",
    )


def test_room_cannot_upgrade_engine():
    procedural_site = {"enabled": True, "music_enabled": True, "music_engine": "procedural"}
    assert service.resolve_engine(procedural_site, {"immersive_music_engine": "external"}) == (
        "procedural",
        "room_cannot_upgrade",
    )

    off_site = {"enabled": True, "music_enabled": True, "music_engine": "off"}
    assert service.resolve_engine(off_site, {"immersive_music_engine": "external"}) == (
        "off",
        "room_cannot_upgrade",
    )


def test_invalid_site_engine_falls_back_to_procedural():
    settings = {"enabled": True, "music_enabled": True, "music_engine": "turbo"}
    assert service.resolve_engine(settings, None) == ("procedural", "invalid_config")


def test_engine_disabled_when_music_off():
    settings = {"enabled": True, "music_enabled": False, "music_engine": "external"}
    assert service.resolve_engine(settings, None) == ("off", "site_music_disabled")


# ---------------------------------------------------------------- 曲库解析

def test_jamendo_parser_normalizes_fields(monkeypatch):
    payload = {
        "results": [
            {
                "id": "100",
                "name": "Rain Hall",
                "artist_name": "Unit Artist",
                "duration": 180,
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "audio": "https://prod-1.storage.jamendo.com/?trackid=100&format=mp32",
                "shareurl": "https://www.jamendo.com/track/100",
                "musicinfo": {
                    "vocalinstrumental": "instrumental",
                    "tags": {"genres": ["ambient"], "vartags": ["dark"]},
                },
            }
        ]
    }
    monkeypatch.setattr(providers, "_session", lambda: _Session(_Response(payload)))
    result = providers.search_jamendo(query="ambient", tags=["ambient"], client_id="cid")
    assert result["ok"] and len(result["tracks"]) == 1
    track = result["tracks"][0]
    assert track["provider"] == "jamendo"
    assert track["license"] == "https://creativecommons.org/licenses/by/4.0/"
    assert track["duration_ms"] == 180_000
    assert track["instrumental"] is True
    assert "ambient" in track["tags"]


def test_jamendo_without_client_id_is_structured_failure():
    result = providers.search_jamendo(query="ambient", tags=[], client_id="")
    assert result["ok"] is False and result["reason"] == "missing_credentials"


def test_openverse_parser_and_empty(monkeypatch):
    payload = {
        "results": [
            {
                "id": "ov-1",
                "title": "Hollow",
                "creator": "Someone",
                "license": "by-sa",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "foreign_landing_url": "https://example.org/hollow",
                "url": "https://cdn.example.org/hollow.mp3",
                "duration": 95_000,
                "tags": [{"name": "drone"}],
            }
        ]
    }
    monkeypatch.setattr(providers, "_session", lambda: _Session(_Response(payload)))
    result = providers.search_openverse(query="drone", tags=["drone"])
    assert result["ok"] and result["tracks"][0]["license"] == "by-sa"

    monkeypatch.setattr(providers, "_session", lambda: _Session(_Response({"results": []})))
    empty = providers.search_openverse(query="drone", tags=[])
    assert empty["ok"] and empty["tracks"] == [] and empty["reason"] == "empty"


# ---------------------------------------------------------------- 选曲与降级

def test_search_and_select_filters_licenses_and_caches(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    room_dir = tmp_path / "room"
    room_dir.mkdir()

    allowed = _track(track_id="ok-1", title="Allowed", tags=["ambient", "drone"])
    noncommercial = _track(
        track_id="bad-1",
        title="No Commercial",
        license="https://creativecommons.org/licenses/by-nc/4.0/",
        license_url="https://creativecommons.org/licenses/by-nc/4.0/",
    )
    missing_attribution = _track(track_id="bad-2", title="", artist="")
    monkeypatch.setattr(
        providers, "search_jamendo",
        lambda **kwargs: {"ok": True, "provider": "jamendo", "tracks": [noncommercial, allowed], "reason": ""},
    )
    monkeypatch.setattr(
        providers, "search_openverse",
        lambda **kwargs: {"ok": True, "provider": "openverse", "tracks": [missing_attribution], "reason": ""},
    )

    result = service.search_and_select(
        room_dir=room_dir, room_id="r-1", profile="daily", intensity="low",
        scene="安静的大厅", settings=settings, force=True,
    )
    assert result["ok"] and result["status"] == "selected"
    track = result["track"]
    assert track["track_key"] == cache.track_key("jamendo", "ok-1")
    assert track["license"] == "by-sa"
    assert track["attribution"]
    assert track["stream_path"].startswith("/api/immersive/rooms/r-1/music/external/audio/")

    # 第二次命中查询缓存，不再调用曲库。
    calls = {"count": 0}

    def _boom(**kwargs):
        calls["count"] += 1
        raise AssertionError("should not hit provider on cache hit")

    monkeypatch.setattr(providers, "search_jamendo", _boom)
    cached = service.search_and_select(
        room_dir=room_dir, room_id="r-1", profile="daily", intensity="low",
        scene="安静的大厅", settings=settings,
    )
    assert cached["cached"] is True and cached["track"]["track_key"] == track["track_key"]
    assert calls["count"] == 0


def test_search_and_select_empty_is_negative_cached(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    monkeypatch.setattr(
        providers, "search_jamendo",
        lambda **kwargs: {"ok": False, "provider": "jamendo", "tracks": [], "reason": "network_error"},
    )
    monkeypatch.setattr(
        providers, "search_openverse",
        lambda **kwargs: {"ok": True, "provider": "openverse", "tracks": [], "reason": "empty"},
    )
    first = service.search_and_select(
        room_dir=room_dir, room_id="r-2", profile="horror", intensity="high",
        scene="洞穴", settings=settings,
    )
    assert first["status"] == "empty" and first["fallback"] == "procedural"
    second = service.search_and_select(
        room_dir=room_dir, room_id="r-2", profile="horror", intensity="high",
        scene="洞穴", settings=settings,
    )
    assert second["cached"] is True and second["status"] == "empty"


def test_search_and_select_disabled_engine_returns_procedural(tmp_path):
    settings = _settings(tmp_path, music_engine="procedural")
    result = service.search_and_select(room_dir=tmp_path, room_id="r-3", settings=settings)
    assert result["status"] == "disabled"
    assert result["engine"] == "procedural"
    assert result["fallback"] == "procedural"


def test_search_and_select_room_cannot_upgrade(tmp_path, monkeypatch):
    settings = _settings(tmp_path, music_engine="procedural")
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    called = {"hit": False}

    def _mark(**kwargs):
        called["hit"] = True
        return {"ok": True, "provider": "jamendo", "tracks": [_track()], "reason": ""}

    monkeypatch.setattr(providers, "search_jamendo", _mark)
    monkeypatch.setattr(providers, "search_openverse", _mark)
    result = service.search_and_select(
        room_dir=room_dir, room_id="r-4", settings=settings,
        house_rules={"immersive_music_engine": "external"},
    )
    assert result["status"] == "disabled" and result["reason"] == "room_cannot_upgrade"
    assert called["hit"] is False


def test_circuit_breaker_opens_after_failures(tmp_path, monkeypatch):
    settings = _settings(tmp_path, music_external_circuit_failures=1, music_external_circuit_cooldown_s=60)
    service._CIRCUIT.clear()
    monkeypatch.setattr(
        providers, "search_jamendo",
        lambda **kwargs: {"ok": False, "provider": "jamendo", "tracks": [], "reason": "network_error"},
    )
    monkeypatch.setattr(
        providers, "search_openverse",
        lambda **kwargs: {"ok": True, "provider": "openverse", "tracks": [], "reason": "empty"},
    )
    service.search_and_select(
        room_dir=tmp_path / "room", room_id="r-5", settings=settings, force=True,
    )
    assert service._circuit_open("jamendo", settings) is True
    service._CIRCUIT.clear()


# ---------------------------------------------------------------- 代理安全

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.mp3",
        "https://127.0.0.1/a.mp3",
        "https://10.0.0.5/a.mp3",
        "https://localhost/a.mp3",
        "https://192.168.1.2/a.mp3",
        "https://example.com:8443/a.mp3",
        "not-a-url",
    ],
)
def test_unsafe_public_url_rejected(url):
    assert service.is_safe_public_url(url, {}) is False


def test_public_url_and_host_allowlist():
    assert service.is_safe_public_url("https://prod-1.storage.jamendo.com/audio/a.mp3", {}) is True
    settings = {"music_external_allowed_hosts": "jamendo.com,example.org"}
    assert service.is_safe_public_url("https://cdn.jamendo.com/a.mp3", settings) is True
    assert service.is_safe_public_url("https://evil.example.net/a.mp3", settings) is False


# ---------------------------------------------------------------- 打分

def test_rank_prefers_instrumental_longer_tracks():
    short_vocal = _track(
        track_id="v", title="Vocal", duration_ms=20_000, instrumental=False, tags=["ambient"],
    )
    long_instrumental = _track(
        track_id="i", title="Instrumental", duration_ms=240_000, instrumental=True, tags=["ambient", "drone"],
    )
    ranked = search.rank([short_vocal, long_instrumental], tags=["ambient", "drone"], instrumental_only=True)
    assert ranked[0]["track_id"] == "i"


def test_dedupe_keeps_first_provider():
    first = _track(provider="jamendo", track_id="1", title="Same Song", artist="Same Artist")
    second = _track(provider="openverse", track_id="2", title="same song!", artist="same artist")
    assert len(search.dedupe([first, second])) == 1
