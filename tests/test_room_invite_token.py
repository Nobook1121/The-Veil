"""房间邀请令牌（可轮换邀请链接）相关接口的回归测试。"""

import json

from trpg_server.app_factory import create_app


class FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="USER"):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(role),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, user_id=1):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role="USER", session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "room_code": "ABCD1234",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "tester", "role": "USER", "status": "active", "is_active": True, "room_role": "owner"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_room_info(tmp_path, room_id="r-1"):
    return json.loads((tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8"))


def test_creator_reads_existing_invite_token(tmp_path):
    _write_room(tmp_path, invite_token="existing-token")
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/invite-token")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["invite_token"] == "existing-token"
    assert data["can_rotate"] is True


def test_missing_token_is_lazily_generated_and_persisted(tmp_path):
    _write_room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/invite-token")

    assert response.status_code == 200
    token = response.get_json()["data"]["invite_token"]
    assert token
    assert _read_room_info(tmp_path)["invite_token"] == token

    again = client.get("/api/rooms/r-1/invite-token")
    assert again.get_json()["data"]["invite_token"] == token


def test_rotate_invalidates_old_link_and_keeps_new_one(tmp_path):
    _write_room(tmp_path, invite_token="old-token")
    client = _app(tmp_path).test_client()
    _login(client)

    rotated = client.post("/api/rooms/r-1/invite-token", headers={"X-CSRF-Token": "csrf"})
    assert rotated.status_code == 200
    new_token = rotated.get_json()["data"]["invite_token"]
    assert new_token and new_token != "old-token"
    assert _read_room_info(tmp_path)["invite_token"] == new_token

    # 旧令牌加入应立即失效
    stale = client.post(
        "/api/rooms/join",
        json={"invite_token": "old-token"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert stale.status_code == 404

    # 新令牌可以加入
    fresh = client.post(
        "/api/rooms/join",
        json={"invite_token": new_token},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert fresh.status_code == 200
    assert fresh.get_json()["data"]["id"] == "r-1"


def test_join_with_unknown_invite_token_returns_404(tmp_path):
    _write_room(tmp_path, invite_token="valid-token")
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/join",
        json={"invite_token": "nope"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 404


def test_non_member_cannot_read_or_rotate_invite_token(tmp_path):
    _write_room(tmp_path, invite_token="secret-token")
    client = _app(tmp_path).test_client()
    _login(client, user_id=2)

    read = client.get("/api/rooms/r-1/invite-token")
    assert read.status_code == 403

    rotate = client.post("/api/rooms/r-1/invite-token", headers={"X-CSRF-Token": "csrf"})
    assert rotate.status_code == 403
    assert _read_room_info(tmp_path)["invite_token"] == "secret-token"


def test_non_manager_member_cannot_rotate_invite_token(tmp_path):
    _write_room(
        tmp_path,
        invite_token="secret-token",
        members=[
            {"user_id": 1, "username": "owner", "role": "USER", "status": "active", "is_active": True, "room_role": "owner"},
            {"user_id": 2, "username": "tester", "role": "USER", "status": "active", "is_active": True, "room_role": "member"},
        ],
    )
    client = _app(tmp_path).test_client()
    _login(client, user_id=2)

    read = client.get("/api/rooms/r-1/invite-token")
    assert read.status_code == 200
    assert read.get_json()["data"]["can_rotate"] is False

    rotate = client.post("/api/rooms/r-1/invite-token", headers={"X-CSRF-Token": "csrf"})
    assert rotate.status_code == 403
