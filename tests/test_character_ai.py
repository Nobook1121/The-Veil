from __future__ import annotations

import json
from pathlib import Path

from trpg_server.app_factory import create_app
from trpg_server.character_ai import character_generator_role, parse_character_generation


class FakeAdminUserManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "admin", "role": "ADMIN"}


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

    scenarios_dir = tmp_path / "scenarios"
    scenario_dir = scenarios_dir / "scenario-1"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.json").write_text(
        json.dumps(
            {
                "id": 1,
                "title": "雾港疑云",
                "notes": "1920 年代港口调查",
                "modules": [
                    {
                        "module_type": "background",
                        "title": "背景",
                        "summary": "",
                        "content": "调查员应为雾港本地居民或长期居住者。",
                    },
                    {
                        "module_type": "scene",
                        "title": "码头",
                        "summary": "夜色下的码头",
                        "content": "深夜的码头雾气弥漫。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeAdminUserManager(),
            "CONFIG_DIR": config_dir,
            "SCENARIOS_DIR": scenarios_dir,
            "AI_PLATFORM_DIR": platform_dir,
            "AI_PLATFORM_SECRET_DIR": tmp_path / "runtime" / "config" / "aiplatform",
        }
    )
    return app


def _login(client):
    with client.session_transaction() as session:
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "ADMIN"
        session["session_token"] = "token"
        session["csrf_token"] = "csrf"


_CHARACTER_JSON = {
    "name": "林清",
    "gender": "female",
    "age": 31,
    "era": "1920s",
    "occupationId": "doctor",
    "occupationName": "医生",
    "creditRating": 45,
    "residence": "雾港",
    "birthplace": "广州",
    "attributes": {
        "STR": 50, "CON": 55, "SIZ": 60, "DEX": 65, "APP": 60,
        "INT": 70, "POW": 50, "EDU": 80, "LUC": 45, "AGE": 31,
    },
    "background": {"appearance": "身形清瘦", "raceType": "人类"},
    "skills": [
        {"skillKey": "medicine", "name": "医学", "category": "知识", "occupation": True, "occupationPoints": 60, "interestPoints": 0}
    ],
    "weapons": [],
    "equipment": [{"name": "手术刀", "quantity": 1, "weight": 0, "notes": ""}],
    "relationships": [],
}


def _patch_ai(monkeypatch, content: str, captured: dict):
    from trpg_server import character_ai as generator
    from trpg_server.routes import character_ai as route

    monkeypatch.setattr(route, "_can_use_permission", lambda node_id: True)
    monkeypatch.setattr(
        generator,
        "character_generator_role",
        lambda: {"id": "character_generator", "provider": "deepseek", "prompt": "角色卡生成提示词"},
    )

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 321}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setattr(generator.requests, "post", fake_post)


def test_character_generator_role_is_registered_by_default(tmp_path: Path):
    app = _build_app(tmp_path)
    with app.app_context():
        role = character_generator_role()
    assert role["id"] == "character_generator"
    assert role["provider"] == "deepseek"
    assert role["prompt"]


def test_generate_character_returns_card_and_calls_ai_platform(tmp_path: Path, monkeypatch):
    app = _build_app(tmp_path)
    client = app.test_client()
    _login(client)

    captured: dict = {}
    _patch_ai(monkeypatch, json.dumps(_CHARACTER_JSON, ensure_ascii=False), captured)

    response = client.post(
        "/api/characters/generate",
        json={"scenario_id": 1, "description": "一位精通旧书鉴定的女医生"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["character"]["name"] == "林清"
    assert payload["data"]["token_count"] == 321
    assert payload["data"]["model"] == "deepseek-chat"

    assert captured["url"] == "https://example.test/chat/v1/chat/completions"
    assert captured["json"]["messages"][0]["content"] == "角色卡生成提示词"
    user_prompt = captured["json"]["messages"][1]["content"]
    # 用户描述与剧本同时存在时，以用户描述为主。
    assert "以「角色描述」为主" in user_prompt
    assert "一位精通旧书鉴定的女医生" in user_prompt
    assert "雾港疑云" in user_prompt


def test_generate_character_prefers_scenario_when_no_description(tmp_path: Path, monkeypatch):
    app = _build_app(tmp_path)
    client = app.test_client()
    _login(client)

    captured: dict = {}
    _patch_ai(monkeypatch, json.dumps(_CHARACTER_JSON, ensure_ascii=False), captured)

    response = client.post(
        "/api/characters/generate",
        json={"scenario_id": 1, "description": ""},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    user_prompt = captured["json"]["messages"][1]["content"]
    assert "依据剧本背景与角色要求" in user_prompt


def test_generate_character_allows_free_generation_without_scenario(tmp_path: Path, monkeypatch):
    app = _build_app(tmp_path)
    client = app.test_client()
    _login(client)

    captured: dict = {}
    _patch_ai(monkeypatch, json.dumps(_CHARACTER_JSON, ensure_ascii=False), captured)

    response = client.post(
        "/api/characters/generate",
        json={"scenario_id": "", "description": ""},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    user_prompt = captured["json"]["messages"][1]["content"]
    assert "自由生成" in user_prompt


def test_generate_character_rejects_invalid_ai_payload(tmp_path: Path, monkeypatch):
    app = _build_app(tmp_path)
    client = app.test_client()
    _login(client)

    captured: dict = {}
    _patch_ai(monkeypatch, "这不是 JSON", captured)

    response = client.post(
        "/api/characters/generate",
        json={"description": "随便一位调查员"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 502
    assert response.get_json()["success"] is False


def test_generate_character_reports_empty_content_as_budget_issue(tmp_path: Path, monkeypatch):
    """推理模型思考 token 挤占输出预算导致 content 为空时，应给出可诊断的提示。"""

    app = _build_app(tmp_path)
    client = app.test_client()
    _login(client)

    captured: dict = {}
    _patch_ai(monkeypatch, "", captured)

    response = client.post(
        "/api/characters/generate",
        json={"description": "随便一位调查员"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 502
    assert "max_tokens" in (response.get_json().get("error") or "")


def test_build_request_payload_applies_template_and_token_policy(tmp_path):
    from trpg_server.agents.ai_request import build_request_payload
    from trpg_server.agents.config import AIRuntimeConfig

    # 请求模板：默认模板 + 模型模板（均未声明 max_tokens）。
    (tmp_path / "aiplatform").mkdir()
    (tmp_path / "aiplatform" / "default-request.json").write_text(
        json.dumps({"temperature": 0.7, "stop": ["</s>"]}), encoding="utf-8"
    )
    (tmp_path / "aimodel" / "deepseek").mkdir(parents=True)
    (tmp_path / "aimodel" / "deepseek" / "m1.json").write_text(
        json.dumps({"top_p": 0.3, "temperature": 0.9}), encoding="utf-8"
    )

    messages = [{"role": "user", "content": "hi"}]
    # 关闭全局约束：不下发 max_tokens，输出上限交还平台自身。
    unconstrained = AIRuntimeConfig(constrain_max_tokens=False)
    payload = build_request_payload(
        "deepseek",
        "m1",
        messages,
        runtime_config=unconstrained,
        config_dir=tmp_path,
        params={"temperature": 0.6},
    )
    assert "max_tokens" not in payload
    assert payload["messages"] == messages
    # 调用方声明的 temperature 优先于模板；模板独有的 stop 仍然并入。
    assert payload["temperature"] == 0.6
    assert payload["stop"] == ["</s>"]
    assert payload["top_p"] == 0.3
    # 调用方未声明的采样参数则由模板提供（模型模板覆盖默认模板）。
    assert (
        build_request_payload(
            "deepseek", "m1", messages, runtime_config=unconstrained, config_dir=tmp_path
        )["temperature"]
        == 0.9
    )

    # 开启全局约束：模板未声明 max_tokens 时，按管理员设置统一下发。
    constrained = AIRuntimeConfig(constrain_max_tokens=True, max_tokens=3000)
    payload2 = build_request_payload(
        "deepseek", "m1", messages, runtime_config=constrained, config_dir=tmp_path
    )
    assert payload2["max_tokens"] == 3000

    # 请求模板显式声明 max_tokens 时以模板为准（模板即真实请求体，用户改动真正生效）。
    (tmp_path / "aiplatform" / "default-request.json").write_text(
        json.dumps({"temperature": 0.7, "max_tokens": 4096}), encoding="utf-8"
    )
    payload3 = build_request_payload(
        "deepseek", "m1", messages, runtime_config=constrained, config_dir=tmp_path
    )
    assert payload3["max_tokens"] == 4096


def test_parse_character_generation_handles_fences_and_wrappers():
    raw = "```json\n{\n  \"attributes\": {\"STR\": 50}, \"name\": \"张三\"\n}\n```"
    parsed = parse_character_generation(raw)
    assert parsed == {"attributes": {"STR": 50}, "name": "张三"}

    wrapped = json.dumps({"character": {"attributes": {"STR": 40}, "name": "李四"}}, ensure_ascii=False)
    assert parse_character_generation(wrapped) == {"attributes": {"STR": 40}, "name": "李四"}

    # 缺少 attributes 的负载视为无效。
    assert parse_character_generation(json.dumps({"name": "无名"})) is None
    assert parse_character_generation("") is None
