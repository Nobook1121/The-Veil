"""提示门控：房规未开启「行动建议」时，正文里的引导问句与选项列表必须被清掉。"""
import json

import pytest

from trpg_server.agents.hint_guard import strip_action_hints
from trpg_server.agents.runtime import AgentCompletionResult
from trpg_server.app_factory import create_app


@pytest.fixture(autouse=True)
def _reset_response_caches():
    """响应缓存是路由模块级全局对象，跨用例共享；不清空会让后一个用例拿到前一个的回复。"""
    import trpg_server.routes.chat as chat_routes

    for cache in (chat_routes._PREFIX_CACHE, chat_routes._EXACT_CACHE, chat_routes._SEMANTIC_CACHE):
        cache.clear()
    yield
    for cache in (chat_routes._PREFIX_CACHE, chat_routes._EXACT_CACHE, chat_routes._SEMANTIC_CACHE):
        cache.clear()


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = "ADMIN" if int(user_id) == 1 else "USER"
        username = "tester" if int(user_id) == 1 else f"user{user_id}"
        return {"id": user_id, "username": username, "role": role}


class FakeKnowledgeBase:
    def __init__(self, **_kwargs):
        pass

    def search(self, *_args, **_kwargs):
        return []

    def catalog(self, *_args, **_kwargs):
        return []


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
            "LOGS_DIR": tmp_path / "logs",
            "KNOWLEDGE_BASES_DIR": tmp_path / "knowledge",
        }
    )


def _write_room(tmp_path, *, suggestions_enabled):
    room_dir = tmp_path / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": "r-1",
        "name": "Test Room",
        "creator_id": 1,
        "room_mode": "freeform",
        "ai_kp_enabled": True,
        "scenario_binding": "none",
        "house_rules": {"action_suggestions_enabled": suggestions_enabled},
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "status": "active",
                "is_active": True,
                "room_role": "owner",
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")


def _stub_pipeline(monkeypatch, content):
    import trpg_server.agents.turn_pipeline as pipeline

    monkeypatch.setattr(
        pipeline,
        "_load_enabled_platform",
        lambda provider, purpose=None: ("fake", {"config": {"api_key": "k", "base_url": "http://fake.local/v1"}}),
    )
    monkeypatch.setattr(pipeline, "_load_enabled_platforms", lambda provider, purpose=None: [])
    monkeypatch.setattr(pipeline, "_post_ai_request", lambda *args, **kwargs: (lambda payload: {}))
    monkeypatch.setattr(pipeline, "KnowledgeBaseService", FakeKnowledgeBase)

    def fake_run_agent_completion(**_kwargs):
        return AgentCompletionResult(
            content=content,
            token_count=12,
            prompt_token_count=10,
            completion_token_count=2,
            direct_messages=[],
            tool_messages=[],
        )

    monkeypatch.setattr(pipeline, "run_agent_completion", fake_run_agent_completion)


LEAKY_REPLY = (
    "现在，祭司干尸已经被拖到十字架后方。\n"
    "\n"
    "**你们要做什么？**\n"
    "\n"
    "- 将祭司吊坠嵌入十字架凹槽？\n"
    "- 让某人躺入中央的凹陷，成为「自愿献祭者」？\n"
    "- 强行将干尸推入通道？\n"
    "\n"
    "墙上的倒计时数字钟，红色的数字依旧在跳动。\n"
    "\n"
    "**时间，正在流逝。**"
)


def test_strip_action_hints_removes_prompt_and_options():
    report = strip_action_hints(LEAKY_REPLY)

    assert report.changed is True
    assert report.prompts == 1
    assert report.options == 3
    assert "你们要做什么" not in report.text
    assert "十字架凹槽" not in report.text
    # 引导段之后的正常叙事必须保留
    assert "祭司干尸已经被拖到十字架后方" in report.text
    assert "墙上的倒计时数字钟" in report.text
    assert "**时间，正在流逝。**" in report.text


def test_strip_action_hints_keeps_npc_dialogue():
    text = "他盯着你，缓缓开口：「你们要做什么？」\n\n- 这是NPC的台词，不该动"
    report = strip_action_hints(text)

    assert report.changed is False
    assert report.text == text


def test_strip_action_hints_keeps_plain_narration():
    text = "你推开门，走廊尽头传来脚步声。\n\n你要怎么做呢，旅人？"
    report = strip_action_hints(text)

    assert report.changed is False
    assert report.text == text


def test_strip_action_hints_removes_numbered_options_with_header():
    text = "门在你面前打开。\n\n可选行动：\n1. 进去\n2. 离开\n\n远处传来钟声。"
    report = strip_action_hints(text)

    assert report.changed is True
    assert report.options == 2
    assert "进去" not in report.text
    assert "远处传来钟声" in report.text


def test_strip_action_hints_keeps_bold_narration():
    """``**加粗**`` 行不能被当成 Markdown 列表项。"""
    text = "**时间，正在流逝。**"
    report = strip_action_hints(text)

    assert report.changed is False
    assert report.text == text


def test_strip_action_hints_keeps_reply_when_only_hints():
    """整条回复都是提示时不返回空串，否则前端会显示成平台无响应。"""
    text = "你们要做什么？\n\n- 走进门\n- 后退"
    report = strip_action_hints(text)

    assert report.changed is False
    assert report.text == text


def test_chat_strips_hints_when_house_rule_disabled(tmp_path, monkeypatch):
    _write_room(tmp_path, suggestions_enabled=False)
    client = _app(tmp_path).test_client()
    _login(client)
    _stub_pipeline(monkeypatch, LEAKY_REPLY)

    response = client.post(
        "/api/chat",
        json={"room_id": "r-1", "user_id": 1, "content": "我把干尸拖过去"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["success"] is True
    assert "你们要做什么" not in payload["content"]
    assert "十字架凹槽" not in payload["content"]
    assert "墙上的倒计时数字钟" in payload["content"]
    assert payload["suggestions"] == []


def test_chat_keeps_hints_when_house_rule_enabled(tmp_path, monkeypatch):
    """房规开启提示功能时，正文保持模型原样输出（选项由提示工具负责）。"""
    _write_room(tmp_path, suggestions_enabled=True)
    client = _app(tmp_path).test_client()
    _login(client)
    _stub_pipeline(monkeypatch, LEAKY_REPLY)

    response = client.post(
        "/api/chat",
        json={"room_id": "r-1", "user_id": 1, "content": "我把干尸拖过去"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["content"] == LEAKY_REPLY
