import pytest
import requests

from trpg_server.agents.http_retry import post_with_retry


class _OkResponse:
    ok = True
    status_code = 200


def test_post_with_retry_retries_transient_timeouts_within_budget():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.exceptions.Timeout("boom")
        return _OkResponse()

    result = post_with_retry("u", headers={}, payload={}, timeout=30, max_retries=2, post=post)

    assert isinstance(result, _OkResponse)
    assert len(calls) == 2


def test_post_with_retry_exhausts_budget_and_reports_retry_count():
    calls = []
    retries = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(timeout)
        raise requests.exceptions.Timeout("boom")

    with pytest.raises(RuntimeError) as excinfo:
        post_with_retry(
            "u",
            headers={},
            payload={},
            timeout=30,
            max_retries=2,
            post=post,
            on_retry=lambda attempt, maximum: retries.append((attempt, maximum)),
        )

    assert len(calls) == 3
    assert retries == [(1, 2), (2, 2)]
    assert "已重试 2 次" in str(excinfo.value)


def test_post_with_retry_wraps_connection_errors_without_retrying():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(True)
        raise requests.exceptions.ConnectionError("refused")

    with pytest.raises(RuntimeError) as excinfo:
        post_with_retry("u", headers={}, payload={}, timeout=30, max_retries=3, post=post)

    assert len(calls) == 1
    assert "AI 平台连接失败" in str(excinfo.value)