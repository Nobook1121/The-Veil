"""AI 平台用途解析：默认值、顺序、停用、用途不匹配、失败降级与健康缓存。"""

import json

import pytest

from trpg_server import ai_platform_resolver as resolver
from trpg_server.ai_platform_resolver import (
    CAPABILITY_AGENT,
    CAPABILITY_CHAT,
    CAPABILITY_ILLUSTRATION,
    CAPABILITY_MUSIC_DIRECTOR,
    CAPABILITY_SCENARIO,
    CAPABILITY_VOICE,
    CONFIG_FAILURE_TTL_SECONDS,
    DEFAULT_CAPABILITIES,
    TRANSIENT_FAILURE_TTL_SECONDS,
    is_platform_available,
    mark_platform_failure,
    mark_platform_success,
    normalize_capabilities,
    platform_supports,
    resolve_platform_target,
    resolve_platform_targets,
)


def _write_platform(config_dir, platform, *, order, enabled=True, capabilities=None, model=None):
    platform_dir = config_dir / "aiplatform"
    platform_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "platform": platform,
        "name": platform.upper(),
        "enabled": enabled,
        "order": order,
        "config": {
            "base_url": f"https://{platform}.test/v1",
            "timeout": 30,
            "default_model": model or f"{platform}-model",
        },
        "models": [{"id": model or f"{platform}-model", "name": "m", "enabled": True}],
    }
    if capabilities is not None:
        config["capabilities"] = capabilities
    (platform_dir / f"{platform}.json").write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_health():
    resolver.reset_health()
    yield
    resolver.reset_health()


def test_missing_capabilities_fall_back_to_defaults():
    assert normalize_capabilities(None) == list(DEFAULT_CAPABILITIES)
    config = {"config": {}}
    assert platform_supports(config, CAPABILITY_CHAT)
    assert platform_supports(config, CAPABILITY_ILLUSTRATION)
    # 语音是本版新增占位，旧平台不默认勾选。
    assert not platform_supports(config, CAPABILITY_VOICE)


def test_explicit_empty_capabilities_means_no_purpose():
    assert normalize_capabilities([]) == []
    assert not platform_supports({"capabilities": []}, CAPABILITY_CHAT)


def test_unknown_capabilities_are_dropped_and_deduplicated():
    assert normalize_capabilities(["chat", "chat", "bogus", CAPABILITY_AGENT]) == [
        CAPABILITY_CHAT,
        CAPABILITY_AGENT,
    ]


def test_first_platform_without_illustration_falls_through_to_second(tmp_path):
    _write_platform(tmp_path, "alpha", order=1, capabilities=[CAPABILITY_CHAT])
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_ILLUSTRATION])

    chat = resolve_platform_target(tmp_path, CAPABILITY_CHAT)
    illustration = resolve_platform_target(tmp_path, CAPABILITY_ILLUSTRATION)

    assert chat["platform"] == "alpha"
    # 第一个平台不含配图用途，必须落到第二个平台。
    assert illustration["platform"] == "beta"


def test_disabled_first_platform_is_skipped(tmp_path):
    _write_platform(tmp_path, "alpha", order=1, enabled=False)
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_CHAT])

    target = resolve_platform_target(tmp_path, CAPABILITY_CHAT)
    assert target["platform"] == "beta"


def test_disabled_first_platform_without_capabilities_is_skipped(tmp_path):
    _write_platform(tmp_path, "alpha", order=1, enabled=False)
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_CHAT])

    targets = resolve_platform_targets(tmp_path, CAPABILITY_CHAT)
    assert [item["platform"] for item in targets] == ["beta"]


def test_failed_first_platform_is_skipped_until_ttl_expires(tmp_path):
    _write_platform(tmp_path, "alpha", order=1, capabilities=[CAPABILITY_CHAT])
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_CHAT])

    assert resolve_platform_target(tmp_path, CAPABILITY_CHAT)["platform"] == "alpha"

    ttl = mark_platform_failure("alpha", CAPABILITY_CHAT)
    assert ttl == TRANSIENT_FAILURE_TTL_SECONDS
    assert not is_platform_available("alpha", CAPABILITY_CHAT)
    # 首次平台被记为失败后，解析必须顺延到下一个可用平台。
    assert resolve_platform_target(tmp_path, CAPABILITY_CHAT)["platform"] == "beta"

    # TTL 到期后恢复为候选；成功调用会立即清除失败记录。
    resolver._FAILED_UNTIL[("alpha", CAPABILITY_CHAT)] = resolver.now() - 1
    assert is_platform_available("alpha", CAPABILITY_CHAT)
    assert resolve_platform_target(tmp_path, CAPABILITY_CHAT)["platform"] == "alpha"
    mark_platform_failure("alpha", CAPABILITY_CHAT)
    mark_platform_success("alpha", CAPABILITY_CHAT)
    assert is_platform_available("alpha", CAPABILITY_CHAT)


def test_auth_failure_uses_longer_ttl():
    ttl = mark_platform_failure("alpha", CAPABILITY_CHAT, status_code=401)
    assert ttl == CONFIG_FAILURE_TTL_SECONDS
    assert mark_platform_failure("alpha", CAPABILITY_CHAT, status_code=500) == TRANSIENT_FAILURE_TTL_SECONDS


def test_health_cache_is_scoped_by_purpose():
    mark_platform_failure("alpha", CAPABILITY_ILLUSTRATION)
    assert not is_platform_available("alpha", CAPABILITY_ILLUSTRATION)
    # 配图失败不影响同一平台的对话用途。
    assert is_platform_available("alpha", CAPABILITY_CHAT)


def test_chat_and_illustration_resolve_to_different_platforms(tmp_path):
    _write_platform(tmp_path, "chatbox", order=1, capabilities=[CAPABILITY_CHAT, CAPABILITY_AGENT])
    _write_platform(tmp_path, "painter", order=2, capabilities=[CAPABILITY_ILLUSTRATION])

    assert resolve_platform_target(tmp_path, CAPABILITY_CHAT)["platform"] == "chatbox"
    assert resolve_platform_target(tmp_path, CAPABILITY_AGENT)["platform"] == "chatbox"
    assert resolve_platform_target(tmp_path, CAPABILITY_ILLUSTRATION)["platform"] == "painter"
    # 互不影响：对话落 A、配图落 B。
    assert resolve_platform_target(tmp_path, CAPABILITY_MUSIC_DIRECTOR) is None


def test_order_is_respected_over_filename(tmp_path):
    _write_platform(tmp_path, "zzz", order=1, capabilities=[CAPABILITY_SCENARIO])
    _write_platform(tmp_path, "aaa", order=2, capabilities=[CAPABILITY_SCENARIO])

    targets = resolve_platform_targets(tmp_path, CAPABILITY_SCENARIO)
    assert [item["platform"] for item in targets] == ["zzz", "aaa"]


def test_incomplete_base_url_is_treated_as_unavailable(tmp_path):
    platform_dir = tmp_path / "aiplatform"
    platform_dir.mkdir(parents=True)
    (platform_dir / "broken.json").write_text(
        json.dumps(
            {
                "platform": "broken",
                "name": "Broken",
                "enabled": True,
                "order": 1,
                "config": {"base_url": "", "api_key": "k"},
                "models": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets").mkdir(exist_ok=True)
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_CHAT])

    assert (
        resolve_platform_target(
            tmp_path, CAPABILITY_CHAT, secret_dir_override=tmp_path / "secrets"
        )["platform"]
        == "beta"
    )


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _fake_post_failing_alpha(success_payload):
    """alpha 平台返回 500，beta 平台返回正常 JSON。"""

    def post(url, *, headers=None, json=None, timeout=None):  # noqa: A002 - 与 requests 同名
        if "alpha.test" in url:
            return _FakeResponse(500, {"error": "upstream boom"})
        return _FakeResponse(
            200, {"choices": [{"message": {"content": success_payload}}], "usage": {}}
        )

    return post


def test_illustration_request_falls_back_when_first_platform_errors(tmp_path):
    from trpg_server.immersive import illustration_director

    _write_platform(tmp_path, "alpha", order=1, capabilities=[CAPABILITY_ILLUSTRATION])
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_ILLUSTRATION])

    result = illustration_director.request_decision(
        config_dir=tmp_path,
        context="雨夜的站台",
        analysis={"scene": "车站", "profile": "suspense", "intensity": "medium"},
        structured=None,
        settings={},
        secret_dir_override=tmp_path / "secrets",
        post=_fake_post_failing_alpha(
            json.dumps({"should_illustrate": True, "scene_prompt": "雨夜站台", "npcs": []})
        ),
    )

    assert result["platform"] == "beta"
    # 失败平台进入短期记忆，后续请求直接跳过。
    assert not is_platform_available("alpha", CAPABILITY_ILLUSTRATION)


def test_music_director_request_falls_back_when_first_platform_errors(tmp_path):
    from trpg_server.immersive import director

    _write_platform(tmp_path, "alpha", order=1, capabilities=[CAPABILITY_MUSIC_DIRECTOR])
    _write_platform(tmp_path, "beta", order=2, capabilities=[CAPABILITY_MUSIC_DIRECTOR])

    score = {
        "scene": "雨夜车站",
        "mood": "suspense",
        "mode": "minor",
        "tempo": 80,
        "chords": [{"root": 0, "quality": "minor"}],
        "instrumentation": ["pad"],
        "motif": [0, 3],
        "sections": [{"label": "靠近", "dynamic": "low", "bars": 2}],
        "dynamics": [0.3, 0.6],
        "confidence": 70,
    }
    result = director.request_score(
        config_dir=tmp_path,
        context="雨夜的站台",
        analysis={"scene": "车站", "profile": "suspense", "intensity": "medium"},
        audio_state={"profile": "daily", "intensity": "low", "section": 0},
        settings={"music_director_timeout": 20},
        secret_dir_override=tmp_path / "secrets",
        post=_fake_post_failing_alpha(json.dumps(score, ensure_ascii=False)),
    )

    assert result["platform"] == "beta"
    assert not is_platform_available("alpha", CAPABILITY_MUSIC_DIRECTOR)
    # 配图用途不受音乐导演失败影响。
    assert is_platform_available("alpha", CAPABILITY_ILLUSTRATION)
