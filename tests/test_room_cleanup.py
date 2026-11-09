"""未激活房间自动回收：只删未归档、无活跃成员且超期的房间。"""

import json
import time

from trpg_server import room_admin
from trpg_server.app_factory import create_app
from trpg_server.routes.rooms import run_inactive_room_cleanup


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": f"user{user_id}", "role": "USER"}


def _stamp(days_ago):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - days_ago * 86400))


def _member(user_id, active=True):
    return {
        "user_id": user_id,
        "username": f"user{user_id}",
        "role": "USER",
        "room_role": "member",
        "status": "active" if active else "removed",
        "is_active": active,
    }


def _make_room(rooms_dir, room_id, *, updated_at=None, created_at=None, members=None, archived=False, completed_at=None):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": f"房间 {room_id}",
        "archived": archived,
        "creator_id": 1,
        "members": members if members is not None else [],
    }
    if updated_at is not None:
        info["updated_at"] = updated_at
    if created_at is not None:
        info["created_at"] = created_at
    if completed_at is not None:
        info["completed_at"] = completed_at
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _make_app(tmp_path, rooms_dir):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    history_dir = tmp_path / "history"
    history_dir.mkdir(exist_ok=True)
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir(exist_ok=True)
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": config_dir,
            "ROOMS_DIR": rooms_dir,
            "CHARACTERS_DIR": characters_dir,
            "HISTORY_DIR": history_dir,
        }
    )
    return app, config_dir, history_dir, characters_dir


def test_cleanup_only_removes_inactive_unarchived_rooms(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir()
    (characters_dir / "card-1.json").write_text("{}", encoding="utf-8")

    old = _stamp(10)
    _make_room(rooms_dir, "stale", updated_at=old, members=[_member(1, active=False)])
    _make_room(rooms_dir, "fresh", updated_at=_stamp(0), members=[_member(1, active=False)])
    _make_room(rooms_dir, "active", updated_at=old, members=[_member(1, active=True)])
    _make_room(rooms_dir, "archived", updated_at=old, archived=True)
    _make_room(rooms_dir, "completed", updated_at=old, completed_at=_stamp(10))
    _make_room(rooms_dir, "unknown-time", members=[_member(1, active=False)])

    for room_id in ("stale", "fresh", "active", "archived", "completed"):
        (history_dir / f"room-{room_id}-kp.json").write_text("{}", encoding="utf-8")

    removed = room_admin.cleanup_inactive_rooms(rooms_dir, 7, history_dir=history_dir)

    assert [item["id"] for item in removed] == ["stale"]
    assert not (rooms_dir / "stale").exists()
    assert not (history_dir / "room-stale-kp.json").exists()
    for room_id in ("fresh", "active", "archived", "completed", "unknown-time"):
        assert (rooms_dir / room_id).exists(), room_id
        if room_id != "unknown-time":
            assert (history_dir / f"room-{room_id}-kp.json").exists(), room_id
    # 角色卡/用户数据不在清理范围内。
    assert (characters_dir / "card-1.json").exists()
    assert (history_dir / "room-unknown-time-kp.json").exists() is False


def test_cleanup_disabled_when_days_is_zero(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    _make_room(rooms_dir, "stale", updated_at=_stamp(30), members=[_member(1, active=False)])

    assert room_admin.cleanup_inactive_rooms(rooms_dir, 0) == []
    assert (rooms_dir / "stale").exists()


def test_run_inactive_room_cleanup_reads_setting_and_skips_testing_scheduler(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    _make_room(rooms_dir, "stale", updated_at=_stamp(10), members=[_member(1, active=False)])
    app, config_dir, history_dir, _ = _make_app(tmp_path, rooms_dir)
    (config_dir / "general.toml").write_text("[rooms]\ncleanup_inactive_days = 7\n", encoding="utf-8")

    # TESTING 下不应启动后台线程，避免测试误删真实 data/ 下的房间。
    assert app.extensions.get("room_cleanup_scheduler_started") is None

    with app.app_context():
        removed = run_inactive_room_cleanup()

    assert [item["id"] for item in removed] == ["stale"]
    assert not (rooms_dir / "stale").exists()


def test_run_inactive_room_cleanup_respects_zero_setting(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    _make_room(rooms_dir, "stale", updated_at=_stamp(30), members=[_member(1, active=False)])
    app, config_dir, _, _ = _make_app(tmp_path, rooms_dir)
    (config_dir / "general.toml").write_text("[rooms]\ncleanup_inactive_days = 0\n", encoding="utf-8")

    with app.app_context():
        assert run_inactive_room_cleanup() == []

    assert (rooms_dir / "stale").exists()
