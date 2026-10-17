from trpg_server.agents.cache import (
    ExactResponseCache,
    ProviderPrefixCache,
    SemanticCache,
    build_exact_response_key,
    stable_state_for_cache,
)


def test_provider_prefix_cache_tracks_ttl_and_hit():
    cache = ProviderPrefixCache(default_ttl=60)
    assert cache.lookup("scenario:key") is False
    assert cache.lookup("scenario:key") is True
    assert cache.stats("scenario:key")["hits"] == 1


def test_exact_cache_reuses_value_until_expiry():
    cache = ExactResponseCache(default_ttl=60)
    cache.set("rules", {"text": "result"})
    assert cache.get("rules") == {"text": "result"}
    assert cache.get("missing") is None


def test_semantic_cache_is_bound_to_state_fingerprint():
    cache = SemanticCache(default_ttl=60)
    cache.set("open door", {"active_scene_id": "scene-1"}, "result-1")
    assert cache.get("open door", {"active_scene_id": "scene-1"}) == "result-1"
    assert cache.get("open door", {"active_scene_id": "scene-2"}) is None


def test_exact_response_key_is_versioned_and_state_bound():
    first = build_exact_response_key("s", "1", "scene", {"items": ["key"]}, "look")
    second = build_exact_response_key("s", "2", "scene", {"items": ["key"]}, "look")
    third = build_exact_response_key("s", "1", "scene", {"items": ["torch"]}, "look")
    assert first != second
    assert first != third


def test_stable_state_for_cache_strips_volatile_fields_only():
    """剔除随机 id / 时间戳，但保留真正代表剧情推进的内容字段。"""
    volatile = {
        "active_scene_id": "scene-1",
        "event_log": [{"id": "abc", "kind": "clue", "content": "找到钥匙", "created_at": "2026-01-01 00:00:00"}],
        "clues": ["钥匙"],
    }
    other = {
        "active_scene_id": "scene-1",
        "event_log": [{"id": "zzz", "kind": "clue", "content": "找到钥匙", "created_at": "2026-02-02 00:00:00"}],
        "clues": ["钥匙"],
    }
    # 只有易变字段不同 → 稳定指纹相同，缓存才能命中
    assert stable_state_for_cache(volatile) == stable_state_for_cache(other)
    # 内容真正变化 → 指纹必须不同，避免复用过期叙事
    changed = {
        "active_scene_id": "scene-1",
        "event_log": [{"id": "abc", "kind": "clue", "content": "找到枪支", "created_at": "2026-01-01 00:00:00"}],
        "clues": ["钥匙"],
    }
    assert stable_state_for_cache(volatile) != stable_state_for_cache(changed)

