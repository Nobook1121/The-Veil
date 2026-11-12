"""更新服务契约：版本比较、渠道过滤、状态机、下载与 sha256 校验。

所有用例都 monkeypatch ``update_service.requests.get``，保证测试期间不出网；
conftest 默认注入 ``AI_TRPG_DISABLE_UPDATE_CHECK=1``，本文件按用例显式清除。
"""

from __future__ import annotations

import hashlib
import json

import pytest
import requests
from flask import Flask

from trpg_server import update_service
from trpg_server.version import APP_VERSION


class _FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text="", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeStreamResponse(_FakeResponse):
    """模拟 ``requests.get(..., stream=True)`` 的上下文管理器。"""

    def __init__(self, content: bytes, *, status_code=200):
        super().__init__(None, status_code=status_code, headers={"Content-Length": str(len(content))})
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]


def _app(tmp_path, *, testing=False):
    app = Flask(__name__)
    app.config.update(
        TESTING=testing,
        CONFIG_DIR=str(tmp_path / "config"),
        UPDATE_DIR=str(tmp_path / "update"),
    )
    return app


def _release(tag, *, prerelease=False, draft=False, assets=None, body="更新说明"):
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "body": body,
        "html_url": f"https://github.com/Nobook1121/The-Veil/releases/tag/{tag}",
        "published_at": "2026-11-01T00:00:00Z",
        "assets": assets or [],
    }


def _assets(tag, *, size=4096):
    return [
        {
            "name": f"the-veil-{tag}.zip",
            "size": size,
            "browser_download_url": f"https://example.test/the-veil-{tag}.zip",
        },
        {
            "name": f"the-veil-{tag}.zip.sha256",
            "size": 96,
            "browser_download_url": f"https://example.test/the-veil-{tag}.zip.sha256",
        },
    ]


@pytest.fixture(autouse=True)
def _isolated_update_globals(monkeypatch):
    """清掉 conftest 默认的禁用开关与模块级缓存/节流状态。"""
    monkeypatch.delenv(update_service.DISABLE_ENV, raising=False)
    monkeypatch.setattr(update_service, "_last_check_at", 0.0)
    monkeypatch.setattr(update_service, "_download_progress", 0)
    monkeypatch.setattr(update_service, "_last_stars", None)
    yield


def _fake_github(monkeypatch, releases, *, stars=7):
    """按 URL 分派 releases / stars 两个请求，并记录调用。"""
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/releases" in url:
            return _FakeResponse(releases)
        return _FakeResponse({"stargazers_count": stars})

    monkeypatch.setattr(update_service.requests, "get", fake_get)
    return calls


def _forbid_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("fetcher must not be called")

    monkeypatch.setattr(update_service.requests, "get", boom)


def _persisted_state(app):
    return json.loads(update_service._state_path(app).read_text(encoding="utf-8"))


def test_compare_versions_edge_cases():
    # 0.6.0 比 0.10.0 旧：必须按数字段比较，不能按字典序。
    assert update_service.compare_versions("0.6.0", "0.10.0") == -1
    assert update_service.compare_versions("0.10.0", "0.6.0") == 1
    # v 前缀等价。
    assert update_service.compare_versions("v0.6.0", "0.6.0") == 0
    # 预发布低于同号正式版。
    assert update_service.compare_versions("1.0.0-beta.1", "1.0.0") == -1
    assert update_service.compare_versions("1.0.0", "1.0.0-beta.1") == 1
    assert update_service.compare_versions("1.0.0+build.3", "1.0.0") == 0
    # 不规范的 "0.6.0b" 不能抛异常（数字前缀按 0.6.0 解析）。
    assert update_service.compare_versions("0.6.0b", "0.6.0") == 0


def test_select_release_filters_draft_and_prerelease():
    stable = _release("v2.0.0")
    beta = _release("v2.1.0-beta.1", prerelease=True)
    draft = _release("v3.0.0", draft=True)
    assert update_service._select_release([draft, beta, stable], "stable") is stable
    assert update_service._select_release([draft, beta, stable], "prerelease") is beta
    assert update_service._select_release([draft, beta], "stable") is None


def test_stable_channel_skips_prerelease_and_reports_error(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _fake_github(monkeypatch, [_release("v9.9.9", prerelease=True, assets=_assets("v9.9.9"))])
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "error"
    assert state["message"] == "没有可用的发布版本"


def test_prerelease_channel_allows_prerelease(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRPG_CFG_UPDATE_CHANNEL", "prerelease")
    app = _app(tmp_path)
    _fake_github(monkeypatch, [_release("v9.9.9", prerelease=True, assets=_assets("v9.9.9"))])
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "available"
    assert state["latest"] == "9.9.9"


def test_update_config_normalizes_channel_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRPG_CFG_UPDATE_CHANNEL", "beta")  # 非法值回退 stable
    monkeypatch.setenv("AI_TRPG_CFG_UPDATE_REQUEST_TIMEOUT_SECONDS", "1")
    app = _app(tmp_path)
    config = update_service._update_config(app)
    assert config["channel"] == "stable"
    assert config["timeout"] == 3.0  # schema 下限 3 秒

    monkeypatch.setenv("AI_TRPG_CFG_UPDATE_REQUEST_TIMEOUT_SECONDS", "999")
    # 计划 §8.3.2：实际请求超时上限 min(timeout, 15)；§8.5 配置范围 3-30。
    assert update_service._update_config(app)["timeout"] == 15.0


def test_check_available_builds_download_payload(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _fake_github(monkeypatch, [_release("v9.9.9", assets=_assets("v9.9.9"))])
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "available"
    assert state["latest"] == "9.9.9"
    assert state["message"] == "发现新版本 9.9.9"
    assert update_service.get_cached_stars() == 7

    download = _persisted_state(app)["download"]
    assert download["file"] == "the-veil-v9.9.9.zip"
    assert download["size"] == 4096
    assert download["url"].endswith("/the-veil-v9.9.9.zip")
    assert download["sha256_url"].endswith("/the-veil-v9.9.9.zip.sha256")


def test_check_up_to_date_when_tag_matches_current(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _fake_github(monkeypatch, [_release(f"v{APP_VERSION}", assets=_assets(f"v{APP_VERSION}"))])
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "up_to_date"
    assert state["latest"] == APP_VERSION
    assert _persisted_state(app)["download"] == {}


def test_network_timeout_returns_error_and_keeps_latest(tmp_path, monkeypatch):
    app = _app(tmp_path)
    update_service._write_state(
        app,
        {
            **update_service._default_state(),
            "state": "available",
            "latest": "9.9.9",
            "download": {"url": "https://example.test/old.zip", "file": "old.zip"},
        },
    )

    def timeout(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(update_service.requests, "get", timeout)
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "error"
    assert state["latest"] == "9.9.9"
    assert state["message"] == "无法连接 GitHub，请检查网络或代理"
    persisted = _persisted_state(app)
    assert persisted["state"] == "error"
    assert persisted["latest"] == "9.9.9"


def test_github_404_maps_to_readable_error(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(
        update_service.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({}, status_code=404),
    )
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "error"
    assert state["message"] == "仓库不存在或不可访问"


def test_disable_env_short_circuits_without_fetch(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setenv(update_service.DISABLE_ENV, "1")
    _forbid_network(monkeypatch)
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "disabled"
    assert state["enabled"] is False


def test_testing_app_is_disabled_without_fetch(tmp_path, monkeypatch):
    app = _app(tmp_path, testing=True)
    _forbid_network(monkeypatch)
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "disabled"
    assert state["enabled"] is False
    assert state["message"] == "更新检查已停用"


def test_empty_repo_is_disabled_without_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRPG_CFG_UPDATE_REPO", "")
    app = _app(tmp_path)
    _forbid_network(monkeypatch)
    state = update_service.check_for_update(force=True, app=app)
    assert state["state"] == "disabled"
    assert state["message"] == "未配置更新仓库"


def _seed_available(app, *, file_name="pkg.zip"):
    update_service._write_state(
        app,
        {
            **update_service._default_state(),
            "state": "available",
            "latest": "9.9.9",
            "tag": "v9.9.9",
            "download": {
                "file": file_name,
                "size": 10,
                "url": f"https://example.test/{file_name}",
                "sha256_url": f"https://example.test/{file_name}.sha256",
                "progress": 0,
            },
        },
    )


def test_download_verifies_sha256_and_marks_ready(tmp_path, monkeypatch):
    app = _app(tmp_path)
    content = b"the-veil-update-package"
    digest = hashlib.sha256(content).hexdigest()
    _seed_available(app)

    def fake_get(url, **kwargs):
        if url.endswith(".sha256"):
            return _FakeResponse(text=f"{digest}  pkg.zip")
        if url.endswith(".zip"):
            return _FakeStreamResponse(content)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(update_service.requests, "get", fake_get)
    state = update_service.download_update(app)
    assert state["state"] == "ready"
    assert state["progress"] == 100
    target = update_service._downloads_dir(app) / "pkg.zip"
    assert target.read_bytes() == content
    assert _persisted_state(app)["download"]["sha256"] == digest


def test_download_checksum_mismatch_removes_file(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _seed_available(app)

    def fake_get(url, **kwargs):
        if url.endswith(".sha256"):
            return _FakeResponse(text="a" * 64)
        return _FakeStreamResponse(b"corrupted")

    monkeypatch.setattr(update_service.requests, "get", fake_get)
    state = update_service.download_update(app)
    assert state["state"] == "error"
    assert state["message"] == "更新包校验失败，已删除损坏文件"
    assert not (update_service._downloads_dir(app) / "pkg.zip").exists()


def test_download_network_error_marks_error(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _seed_available(app)

    def fake_get(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(update_service.requests, "get", fake_get)
    state = update_service.download_update(app)
    assert state["state"] == "error"
    assert state["message"] == "下载失败，请检查网络后重试"
    assert not (update_service._downloads_dir(app) / "pkg.zip").exists()


def test_download_without_available_state_is_noop(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _forbid_network(monkeypatch)
    state = update_service.download_update(app)
    assert state["state"] == "unknown"
