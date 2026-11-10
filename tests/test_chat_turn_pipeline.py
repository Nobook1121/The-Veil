"""回合流水线冒烟：假房间 + stub LLM 走完 /api/chat 全链路。"""
import json

from trpg_server.agents.runtime import AgentCompletionResult
from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


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


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")


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
