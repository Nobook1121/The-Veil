"""剧本控制台命令：列出 / 改所有者 / 删除（默认、仅向量、强制）。"""
import json
from pathlib import Path

from trpg_server.scenario_store import find_scenario_descriptor_by_id, load_scenario_record, save_scenario_record


class FakeUserManager:
    def __init__(self):
        self.users = [
            {"id": 1, "username": "owner_a", "role": "USER"},
            {"id": 2, "username": "owner_b", "role": "USER"},
        ]

    def get_all_users(self):
        return self.users

    def get_user_by_id(self, user_id):
        return next((u for u in self.users if u["id"] == user_id), None)


class FakeVectorStore:
    def __init__(self, count=3):
        self._count = count
        self.deleted = []

    def count(self, filter=None):  # noqa: A002 - 对齐真实向量库签名
        return self._count

    def delete_by_filter(self, filter):  # noqa: A002
        self.deleted.append(filter)
        return self._count


def _make_scenario(scenarios_dir: Path, scenario_id: int, title: str, owner_id: int = 1) -> Path:
    record = {
        "id": scenario_id,
        "title": title,
        "owner_id": owner_id,
        "modules": [
            {"id": "scene", "module_type": "scene", "title": "Room", "summary": "s", "content": "secret room"},
        ],
    }
    descriptor = save_scenario_record(scenarios_dir, record)
    index_dir = descriptor.parent / "knowledge-index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "1.0.0.json").write_text(json.dumps([{"chunk_id": "c1"}]), encoding="utf-8")
    return descriptor


def _make_room(rooms_dir: Path, room_id: str, scenario_id: int) -> None:
    room = rooms_dir / room_id
    room.mkdir(parents=True, exist_ok=True)
    (room / "info.json").write_text(json.dumps({"scenario_id": scenario_id}), encoding="utf-8")


def test_scenario_list_shows_owner_and_vectors(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影", owner_id=2)

    result = execute_console_command(
        "scenario list", scenarios_dir=scenarios_dir, vector_store=FakeVectorStore(5)
    )

    assert result.ok
    assert "#7" in result.message
    assert "灯塔之影" in result.message
    assert "所有者=2" in result.message
    assert "向量=5" in result.message


def test_scenario_owner_changes_owner_id_on_disk(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影", owner_id=1)

    result = execute_console_command(
        "scenario owner 7 owner_b", scenarios_dir=scenarios_dir, user_manager=FakeUserManager()
    )

    assert result.ok
    descriptor = find_scenario_descriptor_by_id(scenarios_dir, 7)
    assert load_scenario_record(descriptor, scenarios_dir)["owner_id"] == 2


def test_scenario_owner_reports_unknown_user(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影")

    result = execute_console_command(
        "scenario owner 7 ghost", scenarios_dir=scenarios_dir, user_manager=FakeUserManager()
    )

    assert not result.ok
    assert "User not found" in result.message


def test_scenario_owner_reports_missing_scenario(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影")

    result = execute_console_command(
        "scenario owner 999 owner_b", scenarios_dir=scenarios_dir, user_manager=FakeUserManager()
    )

    assert not result.ok
    assert "not found" in result.message


def test_scenario_delete_removes_record_and_vectors(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    rooms_dir = tmp_path / "rooms"
    _make_scenario(scenarios_dir, 7, "灯塔之影")
    vector_store = FakeVectorStore(4)

    result = execute_console_command(
        "scenario delete 7", scenarios_dir=scenarios_dir, rooms_dir=rooms_dir, vector_store=vector_store
    )

    assert result.ok
    assert find_scenario_descriptor_by_id(scenarios_dir, 7) is None
    assert vector_store.deleted == [{"scenario_id": "7"}]


def test_scenario_delete_vectors_only_keeps_record(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    rooms_dir = tmp_path / "rooms"
    descriptor = _make_scenario(scenarios_dir, 7, "灯塔之影")
    vector_store = FakeVectorStore(4)

    result = execute_console_command(
        "scenario delete 7 --vectors-only",
        scenarios_dir=scenarios_dir,
        rooms_dir=rooms_dir,
        vector_store=vector_store,
    )

    assert result.ok
    assert "剧本保留" in result.message
    # 剧本本体仍在，但知识索引目录被清除。
    assert descriptor.exists()
    assert not (descriptor.parent / "knowledge-index").exists()
    assert vector_store.deleted == [{"scenario_id": "7"}]


def test_scenario_delete_archives_when_rooms_use_it(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    rooms_dir = tmp_path / "rooms"
    _make_scenario(scenarios_dir, 7, "灯塔之影")
    _make_room(rooms_dir, "room-1", 7)

    result = execute_console_command(
        "scenario delete 7", scenarios_dir=scenarios_dir, rooms_dir=rooms_dir
    )

    assert result.ok
    assert "已归档" in result.message
    descriptor = find_scenario_descriptor_by_id(scenarios_dir, 7)
    assert descriptor is not None
    assert load_scenario_record(descriptor, scenarios_dir)["archived"] is True


def test_scenario_delete_force_removes_even_when_rooms_use_it(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    rooms_dir = tmp_path / "rooms"
    _make_scenario(scenarios_dir, 7, "灯塔之影")
    _make_room(rooms_dir, "room-1", 7)

    result = execute_console_command(
        "scenario delete 7 --force", scenarios_dir=scenarios_dir, rooms_dir=rooms_dir
    )

    assert result.ok
    assert find_scenario_descriptor_by_id(scenarios_dir, 7) is None


def test_scenario_delete_rejects_unknown_option(tmp_path):
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("scenario delete 7 --nuke", scenarios_dir=tmp_path / "scenarios")

    assert not result.ok
    assert "--nuke" in result.message


def test_scenario_delete_rejects_conflicting_options(tmp_path):
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command(
        "scenario delete 7 --vectors-only --force", scenarios_dir=tmp_path / "scenarios"
    )

    assert not result.ok
    assert "not both" in result.message


def test_scenario_command_writes_user_action_log(tmp_path, caplog):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影", owner_id=1)

    with caplog.at_level("INFO", logger="trpg_server.console_commands"):
        execute_console_command(
            "scenario owner 7 owner_b", scenarios_dir=scenarios_dir, user_manager=FakeUserManager()
        )

    # 记录里必须体现「控制台」这一操作者（具体措辞随日志语言本地化，故不写死）。
    assert "控制台" in caplog.text
    assert "7" in caplog.text