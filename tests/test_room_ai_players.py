"""AI 托管与 AI 玩家：控制权互斥、角色转移与房间摘要字段。"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": f"user{user_id}", "role": "USER"}


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, **kwargs):
        self.events.append((event, payload, kwargs))


def _card(card_id="card-1", name="调查员甲", player_id="1"):
    return {
        "id": card_id,
        "name": name,
        "playerId": player_id,
        "maxHp": 10,
        "currentHp": 10,
        "maxSan": 50,
        "currentSan": 50,
    }


def _member(user_id, card=None, **extra):
    row = {
        "user_id": user_id,
        "username": f"user{user_id}",
        "role": "USER",
        "room_role": "member",
        "status": "active",
        "is_active": True,
        "avatar": "",
    }
    if card is not None:
        row["character_card"] = card
    row.update(extra)
    return row


def _make_room(rooms_dir, room_id="r-ai", members=None):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "AI 测试房间",
        "room_code": "CODE-AI",
        "ai_kp_enabled": True,
        "creator_id": 1,
        "members": members or [],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (room_dir / "messages.json").write_text("[]", encoding="utf-8")
    return room_dir


def _read_info(rooms_dir, room_id="r-ai"):
    return json.loads((rooms_dir / room_id / "info.json").read_text(encoding="utf-8"))


def _make_app(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir()
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": rooms_dir,
            "CHARACTERS_DIR": characters_dir,
            "HISTORY_DIR": tmp_path / "history",
        }
    )
    app.extensions["socketio"] = FakeSocketIO()
    return app, rooms_dir


def _client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=f"user{user_id}",
            role="USER",
            session_token="token",
            csrf_token="csrf",
        )
    return client


def _headers():
    return {"X-CSRF-Token": "csrf"}


def test_create_ai_player_and_takeover_transfers_character(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, members=[_member(1, _card()), _member(2)])

    response = _client(app, 1).post(
        "/api/rooms/r-ai/ai-players",
        json={"character_card": _card("card-ai", "AI 调查员", player_id="1")},
        headers=_headers(),
    )

    assert response.status_code == 201
    info = _read_info(rooms_dir)
    ai_member = next(row for row in info["members"] if row.get("is_ai_player"))
    assert ai_member["control_mode"] == "ai"
    assert ai_member["character_card"]["id"] == "card-ai"

    # 普通成员不能接管 KP/房管创建的 AI 玩家。
    denied = _client(app, 2).post(
        f"/api/rooms/r-ai/ai-players/{ai_member['user_id']}/takeover",
        json={},
        headers=_headers(),
    )
    assert denied.status_code == 403
    info = _read_info(rooms_dir)
    assert next(row for row in info["members"] if row.get("is_ai_player"))["is_active"] is True

    # 房主 / 原创建者可以接管；已有角色卡时需 replace 确认。
    takeover = _client(app, 1).post(
        f"/api/rooms/r-ai/ai-players/{ai_member['user_id']}/takeover",
        json={"replace": True},
        headers=_headers(),
    )

    assert takeover.status_code == 200
    info = _read_info(rooms_dir)
    assert next(row for row in info["members"] if row["user_id"] == 1)["character_card"]["id"] == "card-ai"
    taken = next(row for row in info["members"] if row["user_id"] == ai_member["user_id"])
    assert taken["is_active"] is False
    assert taken.get("taken_over_by") == 1
    assert "character_card" not in taken


def test_ai_hosting_blocks_human_messages_until_reclaimed(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, members=[_member(1, _card())])
    client = _client(app, 1)

    enabled = client.put("/api/rooms/r-ai/members/1/ai-control", json={"enabled": True}, headers=_headers())
    assert enabled.status_code == 200
    assert enabled.get_json()["data"]["members"][0]["control_mode"] == "ai"

    blocked = client.post(
        "/api/rooms/r-ai/messages",
        json={"type": "player", "content": "我先看看四周"},
        headers=_headers(),
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "ai_controlled"

    reclaimed = client.put("/api/rooms/r-ai/members/1/ai-control", json={"enabled": False}, headers=_headers())
    assert reclaimed.status_code == 200

    allowed = client.post(
        "/api/rooms/r-ai/messages",
        json={"type": "player", "content": "我先看看四周"},
        headers=_headers(),
    )
    assert allowed.status_code == 201


def test_ai_turn_persists_messages_with_ai_attribution(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, members=[_member(1, _card())])
    client = _client(app, 1)
    created = client.post(
        "/api/rooms/r-ai/ai-players",
        json={"character_card": _card("card-ai", "AI 调查员", player_id="1")},
        headers=_headers(),
    )
    ai_member = next(row for row in created.get_json()["data"]["members"] if row.get("is_ai_player"))
    app.extensions["socketio"].events.clear()

    with patch(
        "trpg_server.routes.rooms_ai_players.run_player_turn",
        return_value={
            "ok": True,
            "content": "我推开门，先观察 room 里的动静。",
            "tool_messages": [{"type": "dice", "content": "侦察检定：成功", "metadata": {"player_name": "AI 调查员"}}],
        },
    ):
        acted = client.post(f"/api/rooms/r-ai/members/{ai_member['user_id']}/ai-act", json={}, headers=_headers())

    assert acted.status_code == 200
    messages = json.loads((rooms_dir / "r-ai" / "messages.json").read_text(encoding="utf-8"))
    assert len(messages) == 2
    assert all(message["metadata"]["ai_controlled"] is True for message in messages)
    assert messages[-1]["type"] == "player"
    assert messages[-1]["metadata"]["ai_player"] is True
    assert messages[-1]["sender_id"] == ai_member["user_id"]
    assert len(app.extensions["socketio"].events) == 2

    history_files = list((tmp_path / "history").glob("*.json"))
    assert history_files
    history = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert any("我推开门" in item.get("content", "") for item in history)


def test_player_agent_uses_player_tools_and_ai_turn_context(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    room_dir = _make_room(
        rooms_dir,
        members=[
            _member(1, _card()),
            _member(
                "ai-player:test",
                _card("card-ai", "AI 调查员", player_id="1"),
                is_ai_player=True,
                control_mode="ai",
            ),
        ],
    )
    info = _read_info(rooms_dir)
    ai_member = next(row for row in info["members"] if row.get("is_ai_player"))
    captured = {}

    def fake_run(requester, payload, profile, registry, context, max_tool_rounds=8):
        captured.update(
            payload=payload,
            profile=profile,
            context=context,
            tool_names=[tool.name for tool in registry.select(profile.tool_names)],
        )
        return SimpleNamespace(content="我贴着墙根前进。", tool_messages=[], error=None)

    with app.app_context(), patch.multiple(
        "trpg_server.agents.player_agent",
        _load_enabled_platform=lambda provider, purpose=None: (
            "mock",
            {"config": {"api_key": "test-key", "base_url": "http://127.0.0.1:9"}},
        ),
        _select_model=lambda config: "mock-model",
        build_request_payload=lambda platform, model, messages, **kwargs: {"model": model, "messages": messages},
        run_agent_completion=fake_run,
    ):
        from trpg_server.agents.player_agent import run_player_turn

        result = run_player_turn(
            room_id="r-ai",
            room_dir=room_dir,
            info=info,
            member=ai_member,
        )

    assert result["ok"] is True
    assert result["content"] == "我贴着墙根前进。"
    assert captured["context"].tool_state["ai_player_turn"] is True
    assert "check.roll_room_check" in captured["tool_names"]
    assert "room.get_room_snapshot" not in captured["tool_names"]
    assert captured["profile"].tool_names == [
        "check.roll_room_check",
        "dice.roll_coc_check",
        "san.roll_sanity_check",
        "dice.roll",
        "knowledge.search_ruleset",
    ]
    system_text = captured["payload"]["messages"][0]["content"]
    assert "AI 调查员" in system_text
    assert "只扮演" in captured["profile"].prompt


def test_takeover_conflict_requires_confirmation_flag(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, members=[_member(1, _card()), _member(2, _card("card-2", "调查员乙", player_id="2"))])
    client = _client(app, 1)
    created = client.post(
        "/api/rooms/r-ai/ai-players",
        json={"character_card": _card("card-ai", "AI 调查员", player_id="1")},
        headers=_headers(),
    )
    ai_member = next(row for row in created.get_json()["data"]["members"] if row.get("is_ai_player"))

    # 由 AI 的创建者/房主发起接管，验证已有角色卡时的二次确认（普通成员权限
    # 在 test_create_ai_player_and_takeover_transfers_character 中覆盖）。
    conflict = _client(app, 1).post(
        f"/api/rooms/r-ai/ai-players/{ai_member['user_id']}/takeover",
        json={},
        headers=_headers(),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "character_already_bound"

    replaced = _client(app, 1).post(
        f"/api/rooms/r-ai/ai-players/{ai_member['user_id']}/takeover",
        json={"replace": True},
        headers=_headers(),
    )
    assert replaced.status_code == 200
    info = _read_info(rooms_dir)
    assert next(row for row in info["members"] if row["user_id"] == 1)["character_card"]["id"] == "card-ai"


def test_room_summary_reports_ai_counts(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, members=[_member(1, _card())])
    client = _client(app, 1)

    created = client.post(
        "/api/rooms/r-ai/ai-players",
        json={"character_card": _card("card-ai", "AI 调查员", player_id="1")},
        headers=_headers(),
    )
    assert created.status_code == 201
    data = created.get_json()["data"]
    assert data["ai_player_count"] == 1
    assert data["ai_hosted_count"] == 0
    assert data["online_count"] == 0
