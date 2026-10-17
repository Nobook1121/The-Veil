import json
from pathlib import Path


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _make_post(responses):
    """返回一个按调用顺序吐出预设响应的假发送函数。"""
    calls = []

    def _post(url, *, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return responses[len(calls) - 1]

    _post.calls = calls
    return _post


def test_extract_cache_usage_supports_openai_and_deepseek_shapes():
    from trpg_server.agents.cache_probe import extract_cache_usage

    openai = {
        "usage": {
            "prompt_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 1024},
        }
    }
    deepseek = {"usage": {"prompt_tokens": 800, "prompt_cache_hit_tokens": 640}}
    missing = {"usage": {"prompt_tokens": 500}}

    assert extract_cache_usage(openai) == {"prompt_tokens": 1200, "cached_tokens": 1024}
    assert extract_cache_usage(deepseek) == {"prompt_tokens": 800, "cached_tokens": 640}
    # 字段缺失时 cached_tokens 为 None，表示「平台不回报」，与「回报为 0」区分。
    assert extract_cache_usage(missing) == {"prompt_tokens": 500, "cached_tokens": None}
    assert extract_cache_usage({}) is None


def test_probe_prefix_cache_reports_hit_when_second_response_cached():
    from trpg_server.agents.cache_probe import probe_prefix_cache

    post = _make_post([
        FakeResponse(200, {"usage": {"prompt_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 0}}}),
        FakeResponse(200, {"usage": {"prompt_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 1152}}}),
    ])

    result = probe_prefix_cache("https://example.com/v1", "key", "m1", post=post)

    assert result["ok"] is True
    assert result["supported"] is True
    assert result["second"]["cached_tokens"] == 1152
    # 两次请求必须发往同一端点、使用同一 payload，才构成前缀缓存 A/B。
    assert len(post.calls) == 2
    assert post.calls[0]["url"] == post.calls[1]["url"]
    assert post.calls[0]["json"] == post.calls[1]["json"]


def test_probe_prefix_cache_reports_unobservable_when_field_absent():
    from trpg_server.agents.cache_probe import probe_prefix_cache

    post = _make_post([
        FakeResponse(200, {"usage": {"prompt_tokens": 900}}),
        FakeResponse(200, {"usage": {"prompt_tokens": 900}}),
    ])

    result = probe_prefix_cache("https://example.com/v1", "key", "m1", post=post)

    assert result["ok"] is True
    assert result["supported"] is None
    assert "无法判定" in result["detail"]


def test_probe_prefix_cache_reports_miss_when_second_reports_zero():
    from trpg_server.agents.cache_probe import probe_prefix_cache

    post = _make_post([
        FakeResponse(200, {"usage": {"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 0}}}),
        FakeResponse(200, {"usage": {"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 0}}}),
    ])

    result = probe_prefix_cache("https://example.com/v1", "key", "m1", post=post)

    assert result["supported"] is False
    assert "未命中" in result["detail"]


def test_probe_enabled_platform_reads_first_enabled_platform(tmp_path):
    from trpg_server.agents.cache_probe import probe_enabled_platform

    platform_dir = tmp_path / "aiplatform"
    platform_dir.mkdir()
    (platform_dir / "aliyun.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "order": 1,
                "config": {"base_url": "https://example.com/v1"},
                "models": [{"id": "qwen-max", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )

    post = _make_post([
        FakeResponse(200, {"usage": {"prompt_tokens": 100}}),
        FakeResponse(200, {"usage": {"prompt_tokens": 100}}),
    ])

    result = probe_enabled_platform(platform_dir=platform_dir, secret_dir=tmp_path / "secret", post=post)

    assert result["platform"] == "aliyun"
    assert result["model"] == "qwen-max"
    assert post.calls[0]["url"].endswith("/v1/chat/completions")


def test_probe_enabled_platform_reports_missing_platform(tmp_path):
    from trpg_server.agents.cache_probe import probe_enabled_platform

    result = probe_enabled_platform("nope", platform_dir=tmp_path / "empty", secret_dir=tmp_path / "secret")

    assert result["ok"] is False
    assert result["platform"] == "nope"


def test_console_cache_probe_command_uses_injected_platform_dir(tmp_path, monkeypatch):
    from trpg_server.agents import cache_probe
    from trpg_server.console_commands import execute_console_command

    platform_dir = tmp_path / "aiplatform"
    platform_dir.mkdir()
    (platform_dir / "inkstone.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "config": {"base_url": "https://example.com/v1"},
                "models": [{"id": "model-x", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )

    post = _make_post([
        FakeResponse(200, {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}}),
        FakeResponse(200, {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 96}}}),
    ])
    monkeypatch.setattr(cache_probe.requests, "post", post)

    result = execute_console_command(
        "ai cache_probe",
        ai_platform_dir=platform_dir,
        ai_platform_secret_dir=tmp_path / "secret",
    )

    assert result.ok
    assert "inkstone" in result.message
    assert "命中缓存" in result.message


def test_console_cache_probe_rejects_extra_args():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("ai cache_probe a b")

    assert not result.ok
    assert "Usage: ai cache_probe" in result.message