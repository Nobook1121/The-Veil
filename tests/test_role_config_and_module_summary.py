from __future__ import annotations

import json
from pathlib import Path

from trpg_server.app_factory import create_app


class FakeAdminUserManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {
            "id": user_id,
            "username": "admin",
            "role": "ADMIN",
        }


def _build_app(tmp_path: Path):
    config_dir = tmp_path / "config"
    platform_dir = config_dir / "aiplatform"
    platform_dir.mkdir(parents=True, exist_ok=True)
    (platform_dir / "deepseek.json").write_text(
        json.dumps(
            {
                "platform": "deepseek",
                "name": "DeepSeek",
                "enabled": True,
                "config": {
                    "api_key": "public-secret",
                    "base_url": "https://example.test/chat",
                    "timeout": 30,
                },
                "models": [{"id": "deepseek-chat", "name": "DeepSeek Chat", "enabled": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeAdminUserManager(),
            "CONFIG_DIR": config_dir,
            "AI_PLATFORM_DIR": platform_dir,
            "AI_PLATFORM_SECRET_DIR": tmp_path / "runtime" / "config" / "aiplatform",
        }
    )


def test_role_config_save_persists_description(tmp_path: Path):
    app = _build_app(tmp_path)
    client = app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "ADMIN"
        session["session_token"] = "token"
        session["csrf_token"] = "csrf"

    response = client.post(
        "/api/config/roles/kp",
        json={
            "name": "KP",
            "avatar": "",
            "description": "模块摘要、调试 KP 或检定助手",
            "wake_words": ["@KP"],
            "provider": "deepseek",
            "prompt": "你是KP。",
        },
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert any(role["description"] == "模块摘要、调试 KP 或检定助手" for role in payload["data"]["roles"] if role["id"] == "kp")

    saved_text = (tmp_path / "config" / "roles" / "roles.json").read_text(encoding="utf-8")
    assert "模块摘要、调试 KP 或检定助手" in saved_text


def test_module_summary_endpoint_returns_clean_summary_and_token_count(tmp_path: Path, monkeypatch):
    app = _build_app(tmp_path)
    client = app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "ADMIN"
        session["session_token"] = "token"
        session["csrf_token"] = "csrf"

    from trpg_server.routes import scenario_summary

    monkeypatch.setattr(scenario_summary, "_can_use_permission", lambda node_id: True)
    monkeypatch.setattr(
        scenario_summary,
        "summary_role",
        lambda: {"id": "module_summarizer", "provider": "deepseek", "prompt": "摘要提示词"},
    )
    monkeypatch.setattr(
        scenario_summary,
        "load_enabled_platform",
        lambda provider_id=None: (
            "deepseek",
            {
                "config": {
                    "api_key": "test-secret",
                    "base_url": "https://example.test/chat",
                    "timeout": 30,
                },
                "models": [{"id": "deepseek-chat", "name": "DeepSeek Chat", "enabled": True}],
            },
        ),
    )

    captured = {}

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "  场景摘要  "}}], "usage": {"total_tokens": 19}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setattr(scenario_summary.requests, "post", fake_post)

    response = client.post(
        "/api/scenarios/module-summary",
        json={
            "scenario_title": "测试地域",
            "module": {"module_type": "scene", "title": "场景 2", "summary": "", "content": "二楼内容"},
        },
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["summary"] == "场景摘要"
    assert payload["data"]["token_count"] == 19
    # 只填主机名/前缀时，后端会在发请求前补齐 chat/completions 端点（问题 10）。
    assert captured["url"] == "https://example.test/chat/v1/chat/completions"
    # 默认开启全局约束且模板未声明：按管理员设置的默认值统一下发 max_tokens。
    assert captured["json"]["max_tokens"] == 16384
    assert captured["json"]["messages"][1]["content"].startswith("请为下面的剧本模块生成摘要")

