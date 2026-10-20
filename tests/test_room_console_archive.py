"""房间控制台日志随房间归档一并归档的集成测试。"""
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


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "completed_at": "2026-01-01 00:00:00",
        "members": [
            {"user_id": 1, "username": "tester", "role": "ADMIN", "status": "active", "is_active": True, "room_role": "owner"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def test_archive_snapshot_includes_console_log(tmp_path):
    room_dir, _ = _write_room(tmp_path)
    room_log.append_room_log(room_dir, "创建了房间", room_id="r-1", actor="tester", max_entries=50)
    room_log.append_room_log(room_dir, "发送了房间消息", room_id="r-1", max_entries=50)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post("/api/rooms/r-1/archive", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    archive = json.loads((tmp_path / "room-archives" / "r-1.json").read_text(encoding="utf-8"))
    assert [item["message"] for item in archive["console_log"]] == ["创建了房间", "发送了房间消息"]
    # 房间目录内的房间日志保持原样，可继续查看（归档动作本身也会追加一条事件）。
    messages = [item["message"] for item in room_log.read_room_log(room_dir)["entries"]]
    assert "创建了房间" in messages
    assert "发送了房间消息" in messages


def test_archive_without_console_log_yields_empty_list(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post("/api/rooms/r-1/archive", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    archive = json.loads((tmp_path / "room-archives" / "r-1.json").read_text(encoding="utf-8"))
    assert archive["console_log"] == []
