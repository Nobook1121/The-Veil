"""服务器正常关闭前的房间自动存档（save_all_rooms_on_shutdown）测试。"""
import json

from trpg_server.app_factory import create_app
from trpg_server.routes.rooms import save_all_rooms_on_shutdown


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _write_room(tmp_path, room_id, messages, **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True)
    info = {"id": room_id, "name": room_id, "creator_id": 1, "members": []}
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (room_dir / "messages.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return room_dir


def test_shutdown_saves_every_room_even_when_no_player_online(tmp_path):
    first = _write_room(tmp_path, "r-1", [{"role": "user", "content": "进行中的对话"}])
    second = _write_room(tmp_path, "r-2", [{"role": "user", "content": "另一段对话"}])
    archived = _write_room(tmp_path, "r-old", [{"role": "user", "content": "旧对话"}], archived=True)
    empty = _write_room(tmp_path, "r-empty", [])

    app = _app(tmp_path)
    with app.app_context():
        saved = save_all_rooms_on_shutdown()

    assert saved == 2
    for room_dir in (first, second):
        nodes = list((room_dir / "nodes").glob("autosave-*.json"))
        assert len(nodes) == 1
        node = json.loads(nodes[0].read_text(encoding="utf-8"))
        assert node["automatic"] is True
        assert node["messages"] == json.loads((room_dir / "messages.json").read_text(encoding="utf-8"))
    assert not (archived / "nodes").exists()
    assert not (empty / "nodes").exists()


def test_shutdown_skips_room_whose_latest_autosave_already_matches(tmp_path):
    room_dir = _write_room(tmp_path, "r-1", [{"role": "user", "content": "hi"}])

    app = _app(tmp_path)
    with app.app_context():
        assert save_all_rooms_on_shutdown() == 1
        # 第二次关闭（或重复触发）不应产生内容相同的重复节点。
        assert save_all_rooms_on_shutdown() == 0

    assert len(list((room_dir / "nodes").glob("autosave-*.json"))) == 1
