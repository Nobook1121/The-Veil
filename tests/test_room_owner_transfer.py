"""房主显式移交与退出时自动移交。"""

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


def _member(user_id, **extra):
    row = {
        "user_id": user_id,
        "username": f"user{user_id}",
        "role": "USER",
        "room_role": "member",
        "status": "active",
        "is_active": True,
    }
    row.update(extra)
    return row


def _make_room(rooms_dir, room_id, *, creator_id=1, members=None):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": f"房间 {room_id}",
        "room_code": f"CODE-{room_id}",
        "ai_kp_enabled": True,
        "archived": False,
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


def _client(app, user_id, role="USER"):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=f"user{user_id}",
            role=role,
            session_token="token",
            csrf_token="csrf",
        )
    return client


def _transfer(client, room_id, target_user_id):
    return client.put(
        f"/api/rooms/{room_id}/owner",
        json={"user_id": str(target_user_id)},
        headers={"X-CSRF-Token": "csrf"},
    )


def _leave(client, room_id, **body):
    return client.post(f"/api/rooms/{room_id}/leave", json=body, headers={"X-CSRF-Token": "csrf"})


def test_owner_can_transfer_to_active_member(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    fake_socketio = FakeSocketIO()
    app.extensions["socketio"] = fake_socketio
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2)])

    response = _transfer(_client(app, 1), "r-a", 2)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["creator_id"] == "2"
    info = _read_info(rooms_dir, "r-a")
    assert info["creator_id"] == "2"
    members = {row["user_id"]: row for row in info["members"]}
    assert members[1]["room_role"] == "member"
    assert members[1]["is_active"] is True
    assert members[2]["room_role"] == "owner"
    assert members[2]["is_active"] is True
    event, payload, kwargs = fake_socketio.events[-1]
    assert event == "room_members_changed"
    assert payload["room_id"] == "r-a"
    assert kwargs["room"] == "r-a"
    assert "移交给了" in (rooms_dir / "r-a" / "console.jsonl").read_text(encoding="utf-8")


def test_owner_cannot_transfer_to_self(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2)])

    response = _transfer(_client(app, 1), "r-a", 1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "cannot_transfer_to_self"
    assert _read_info(rooms_dir, "r-a")["creator_id"] == 1


def test_non_owner_member_cannot_transfer(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2), _member(3)])

    response = _transfer(_client(app, 2), "r-a", 3)

    assert response.status_code == 403


def test_site_admin_can_transfer(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2)])

    response = _transfer(_client(app, 9, role="ADMIN"), "r-a", 2)

    assert response.status_code == 200
    assert _read_info(rooms_dir, "r-a")["creator_id"] == "2"


def test_admin_cannot_transfer_to_current_owner(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2)])

    response = _transfer(_client(app, 9, role="ADMIN"), "r-a", 1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "already_room_owner"


def test_transfer_target_must_be_active_member(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-a",
        creator_id=1,
        members=[_member(1), _member(2, status="removed", is_active=False), _member(3)],
    )

    response = _transfer(_client(app, 1), "r-a", 2)

    assert response.status_code == 404
    assert _read_info(rooms_dir, "r-a")["creator_id"] == 1
    assert _transfer(_client(app, 1), "r-a", 99).status_code == 404


def test_auto_transfer_falls_back_to_member_order_when_joined_at_missing(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", creator_id=1, members=[_member(1), _member(2), _member(3)])

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    # 全部缺失 joined_at 时按成员列表顺序兜底。
    assert response.get_json()["data"]["owner_transferred_to"] == "2"


def test_auto_transfer_tie_breaks_by_member_order(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    stamp = "2026-01-01 10:00:00"
    _make_room(
        rooms_dir,
        "r-a",
        creator_id=1,
        members=[
            _member(1, joined_at="2025-12-31 10:00:00"),
            _member(2, joined_at=stamp),
            _member(3, joined_at=stamp),
        ],
    )

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["owner_transferred_to"] == "2"


def test_auto_transfer_prefers_parseable_joined_at_over_missing(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-a",
        creator_id=1,
        members=[_member(1), _member(2), _member(3, joined_at="2026-01-01T10:00:00Z")],
    )

    response = _leave(_client(app, 1), "r-a")

    assert response.status_code == 200
    assert response.get_json()["data"]["owner_transferred_to"] == "3"
