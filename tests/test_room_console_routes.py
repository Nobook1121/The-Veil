"""房间内嵌控制台 REST 接口（routes/room_console）的集成测试。"""
import json

from trpg_server.app_factory import create_app
from trpg_server import room_log


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
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


def _login(client, role="ADMIN", user_id=1, username="tester"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=username, role=role, session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "tester", "role": "ADMIN", "status": "active", "is_active": True, "room_role": "owner"},
            {"user_id": 2, "username": "player", "role": "USER", "status": "active", "is_active": True, "room_role": "member"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _disable_room_console(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text("[room_console]\nenabled = false\n", encoding="utf-8")


def test_get_log_returns_room_entries(tmp_path):
    _write_room(tmp_path)
    room_log.append_room_log(tmp_path / "rooms" / "r-1", "创建了房间", room_id="r-1", max_entries=50)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    entries = response.get_json()["data"]["entries"]
    assert [item["message"] for item in entries] == ["创建了房间"]


def test_get_commands_returns_whitelist_help(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/console/commands", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "房间控制台命令" in data["help"]
    assert "members" in data["domains"]


def test_post_command_executes_for_admin(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "info"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["ok"] is True
    # 命令本身与其结果都会进入房间日志。
    entries = room_log.read_room_log(tmp_path / "rooms" / "r-1")["entries"]
    assert entries[0]["message"] == "$ info"
    assert entries[0]["source"] == "command"


def test_post_global_command_is_rejected_but_still_logged(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "shutdown"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ok"] is False
    assert "不属于房间范围" in data["message"]


def test_member_cannot_view_or_execute(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER", user_id=2, username="player")

    # 房间控制台只对网站管理员/所有者与房间所有者/管理员开放，普通成员不可见。
    assert client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"}).status_code == 403
    assert client.get("/api/rooms/r-1/console/commands", headers={"X-CSRF-Token": "csrf"}).status_code == 403
    denied = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "info"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert denied.status_code == 403


def test_room_admin_can_view_and_execute(tmp_path):
    room_dir, info = _write_room(tmp_path)
    info["members"][1]["room_role"] = "admin"
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER", user_id=2, username="player")

    assert client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"}).status_code == 200
    allowed = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "info"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert allowed.status_code == 200


def test_non_member_cannot_view(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER", user_id=99, username="outsider")

    response = client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 403


def test_console_disabled_blocks_access(tmp_path):
    _write_room(tmp_path)
    _disable_room_console(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    assert client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"}).status_code == 403
    assert client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "info"},
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 403


def test_delete_log_clears_for_admin(tmp_path):
    _write_room(tmp_path)
    room_log.append_room_log(tmp_path / "rooms" / "r-1", "创建了房间", room_id="r-1", max_entries=50)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.delete("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    assert response.get_json()["data"]["cleared"] == 1
    assert room_log.read_room_log(tmp_path / "rooms" / "r-1")["entries"] == []


def test_archived_room_blocks_destructive_commands(tmp_path):
    _write_room(tmp_path, archived=True)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    archived_cmd = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "archive"},
        headers={"X-CSRF-Token": "csrf"},
    )
    cleared = client.delete("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"})

    assert archived_cmd.status_code == 409
    assert cleared.status_code == 409


def test_command_requires_body(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/console/command",
        json={},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400


def test_unauthenticated_request_is_rejected(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()

    response = client.get("/api/rooms/r-1/console/log", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 401
