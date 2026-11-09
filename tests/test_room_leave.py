"""玩家自助退出房间：占用释放、未归档改动二次确认、房主退出规则。"""

import json

from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = "ADMIN" if user_id == 9 else "USER"
        return {"id": user_id, "username": f"user{user_id}", "role": role}


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, **kwargs):
        self.events.append((event, payload, kwargs))


def _card(card_id="card-1", name="调查员甲"):
    return {"id": card_id, "name": name, "maxHp": 10, "currentHp": 10}


def _member(user_id, card=None, **extra):
    row = {
        "user_id": user_id,
        "username": f"user{user_id}",
        "role": "USER",
        "room_role": "member",
        "status": "active",
        "is_active": True,
    }
    if card is not None:
        row["character_card"] = card
    row.update(extra)
    return row


def _make_room(rooms_dir, room_id, *, code=None, creator_id=1, members=None, archived=False):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": f"房间 {room_id}",
        "room_code": code or f"CODE-{room_id}",
        "ai_kp_enabled": True,
        "archived": archived,
        "creator_id": creator_id,
        "members": members or [],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _read_info(rooms_dir, room_id):
    return json.loads((rooms_dir / room_id / "info.json").read_text(encoding="utf-8"))


def _make_app(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": rooms_dir,
            "CHARACTERS_DIR": tmp_path / "characters",
            "HISTORY_DIR": history_dir,
        }
    )
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


def _leave(client, room_id, **body):
    return client.post(f"/api/rooms/{room_id}/leave", json=body, headers={"X-CSRF-Token": "csrf"})


def _join(client, code, card=None):
    payload = {"room_code": code}
    if card is not None:
        payload["character_card"] = card
    return client.post("/api/rooms/join", json=payload, headers={"X-CSRF-Token": "csrf"})


def test_member_leave_releases_card_for_other_rooms(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A", members=[_member(1), _member(2, _card())])
    _make_room(rooms_dir, "r-b", code="CODE-B")

    assert _join(_client(app, 3), "CODE-B", _card()).status_code == 409

    response = _leave(_client(app, 2), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["released_card_id"] == "card-1"
    member = next(row for row in _read_info(rooms_dir, "r-a")["members"] if row["user_id"] == 2)
    assert member["status"] == "removed"
    assert member["is_active"] is False

    # 退出后占用立刻释放，其它房间可以用这张卡。
    assert _join(_client(app, 3), "CODE-B", _card()).status_code == 200


def test_leave_requires_confirmation_when_changes_are_unarchived(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-a",
        members=[
            _member(1),
            _member(
                2,
                _card(),
                character_state={"pending_changes": [{"type": "skill", "key": "spot_hidden", "to": 40}]},
            ),
        ],
    )

    response = _leave(_client(app, 2), "r-a")

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"] == "pending_changes_unarchived"
    assert payload["pending_changes"] == 1
    assert "尚未归档" in payload["message"]
    member = next(row for row in _read_info(rooms_dir, "r-a")["members"] if row["user_id"] == 2)
    assert member["is_active"] is True

    confirmed = _leave(_client(app, 2), "r-a", confirm_pending=True)

    assert confirmed.status_code == 200
    member = next(row for row in _read_info(rooms_dir, "r-a")["members"] if row["user_id"] == 2)
    assert member["is_active"] is False


def test_leave_detects_legacy_pending_persist_flag(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-a",
        members=[_member(1), _member(2, _card(), character_state={"pending_persist": True})],
    )

    response = _leave(_client(app, 2), "r-a")

    assert response.status_code == 409
    assert response.get_json()["error"] == "pending_changes_unarchived"


def test_owner_leave_auto_transfers_to_earliest_joined_member(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-a",
        creator_id=1,
        members=[
            _member(1, joined_at="2026-01-01 10:00:00"),
            _member(2, _card(), joined_at="2026-01-03 10:00:00"),
            _member(3, joined_at="2026-01-02 10:00:00"),
        ],
    )

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["owner_transferred_to"] == "3"
    info = _read_info(rooms_dir, "r-a")
    assert info["creator_id"] == "3"
    assert next(row for row in info["members"] if row["user_id"] == 1)["is_active"] is False
    assert next(row for row in info["members"] if row["user_id"] == 3)["room_role"] == "owner"
    assert next(row for row in info["members"] if row["user_id"] == 2)["room_role"] == "member"
    # 自动移交要写房间日志，说明为什么房主换了。
    log_lines = (rooms_dir / "r-a" / "console.jsonl").read_text(encoding="utf-8")
    assert "自动移交" in log_lines


def test_owner_can_leave_as_last_member(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1)])

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    assert _read_info(rooms_dir, "r-a")["members"][0]["is_active"] is False


def test_owner_can_leave_archived_room_with_others(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, archived=True, members=[_member(1), _member(2, _card())])

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["owner_transferred_to"] == "2"
    assert _read_info(rooms_dir, "r-a")["creator_id"] == "2"


def test_leave_is_idempotent_for_non_member(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", members=[_member(1)])

    response = _leave(_client(app, 3), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["already_left"] is True


def test_leave_broadcasts_member_change(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    fake_socketio = FakeSocketIO()
    app.extensions["socketio"] = fake_socketio
    _make_room(rooms_dir, "r-a", members=[_member(1), _member(2, _card())])

    assert _leave(_client(app, 2), "r-a").status_code == 200

    event, payload, kwargs = fake_socketio.events[-1]
    assert event == "room_members_changed"
    assert payload["room_id"] == "r-a"
    assert kwargs["room"] == "r-a"
