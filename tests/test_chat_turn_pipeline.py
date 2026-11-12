"""回合流水线冒烟：假房间 + stub LLM 走完 /api/chat 全链路。"""
import json

from trpg_server.agents.runtime import AgentCompletionResult
from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = "ADMIN" if int(user_id) == 1 else "USER"
        username = "tester" if int(user_id) == 1 else f"user{user_id}"
        return {"id": user_id, "username": username, "role": role}


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


def _write_room(tmp_path):
    room_dir = tmp_path / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": "r-1",
        "name": "Test Room",
        "creator_id": 1,
        "room_mode": "freeform",
        "ai_kp_enabled": True,
        "scenario_binding": "none",
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


def _login(client, user_id=1, username="tester", role="ADMIN"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=username, role=role, session_token="token", csrf_token="csrf")


class FakeKnowledgeBase:
    def __init__(self, **_kwargs):
        pass

    def search(self, *_args, **_kwargs):
        return []

    def catalog(self, *_args, **_kwargs):
        return []


def test_chat_turn_pipeline_smoke(tmp_path, monkeypatch):
    import trpg_server.agents.turn_pipeline as pipeline

    _write_room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    monkeypatch.setattr(
        pipeline,
        "_load_enabled_platform",
        lambda provider, purpose=None: (
            "fake",
            {"config": {"api_key": "k", "base_url": "http://fake.local/v1"}},
        ),
    )
    monkeypatch.setattr(pipeline, "_load_enabled_platforms", lambda provider, purpose=None: [])
    monkeypatch.setattr(pipeline, "_post_ai_request", lambda *args, **kwargs: (lambda payload: {}))
    monkeypatch.setattr(pipeline, "KnowledgeBaseService", FakeKnowledgeBase)

    def fake_run_agent_completion(**_kwargs):
        return AgentCompletionResult(
            content="门后的走廊安静得反常。",
            token_count=12,
            prompt_token_count=10,
            completion_token_count=2,
            direct_messages=[],
            tool_messages=[],
        )

    monkeypatch.setattr(pipeline, "run_agent_completion", fake_run_agent_completion)

    response = client.post(
        "/api/chat",
        json={"room_id": "r-1", "user_id": 1, "content": "我推开门"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["content"] == "门后的走廊安静得反常。"
    assert payload["tool_messages"] == []


def test_chat_requires_login(tmp_path):
    _write_room(tmp_path)
    client = _app(tmp_path).test_client()
    response = client.post("/api/chat", json={"room_id": "r-1", "content": "在吗"})
    assert response.status_code == 401


def test_prop_question_uses_session_identity_and_room_membership(tmp_path, monkeypatch):
    """问问 KP：身份取会话，不能靠请求体 user_id 冒充房主/KP 视角。"""
    import trpg_server.agents.turn_pipeline as pipeline

    _write_room(tmp_path)
    info_path = tmp_path / "rooms" / "r-1" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["members"].append(
        {
            "user_id": 3,
            "username": "player3",
            "role": "USER",
            "status": "active",
            "is_active": True,
            "room_role": "member",
        }
    )
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    app = _app(tmp_path)
    owner = app.test_client()
    _login(owner)
    created = owner.post(
        "/api/rooms/r-1/props",
        json={
            "template_id": "letter-1920",
            "title": "私人书信",
            "fields": {
                "salutation": "致 何清越",
                "body": "钥匙藏在书房。",
                "signature": "冯亦阳",
            },
            "redaction": [{"field": "body", "start": 0, "end": 2, "mode": "burn"}],
        },
        headers={"X-CSRF-Token": "csrf"},
    ).get_json()["data"]
    assert owner.post(
        f"/api/rooms/r-1/props/{created['id']}/reveal",
        json={"to": [3]},
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 200

    monkeypatch.setattr(
        pipeline,
        "_load_enabled_platform",
        lambda provider, purpose=None: (
            "fake",
            {"config": {"api_key": "k", "base_url": "http://fake.local/v1"}},
        ),
    )
    monkeypatch.setattr(pipeline, "_load_enabled_platforms", lambda provider, purpose=None: [])
    monkeypatch.setattr(pipeline, "_post_ai_request", lambda *args, **kwargs: (lambda payload: {}))
    monkeypatch.setattr(pipeline, "KnowledgeBaseService", FakeKnowledgeBase)

    captured = {}

    def fake_run_agent_completion(**kwargs):
        captured["prompt"] = json.dumps(kwargs["base_payload"], ensure_ascii=False)
        return AgentCompletionResult(
            content="（他低声回答）",
            token_count=8,
            prompt_token_count=6,
            completion_token_count=2,
            direct_messages=[],
            tool_messages=[],
        )

    monkeypatch.setattr(pipeline, "run_agent_completion", fake_run_agent_completion)

    member = app.test_client()
    _login(member, user_id=3, username="player3", role="USER")
    response = member.post(
        "/api/chat",
        json={
            "room_id": "r-1",
            # 旧实现会拿这个字段当视角：user_id=1 是房主，能拿到未脱敏正文。
            "user_id": 1,
            "content": "这份文件写了什么？",
            "prop_id": created["id"],
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200, response.get_json()
    assert "钥匙" not in captured["prompt"]
    assert "████" in captured["prompt"]

    outsider = app.test_client()
    _login(outsider, user_id=9, username="outsider", role="USER")
    denied = outsider.post(
        "/api/chat",
        json={"room_id": "r-1", "content": "我看看门里"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert denied.status_code == 403
