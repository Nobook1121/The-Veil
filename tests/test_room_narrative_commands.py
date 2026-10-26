"""房间控制台 narrative 命令（§11.2）的单元测试。"""
import json

from trpg_server.agents.narrative.store import narrative_path
from trpg_server.console_commands import execute_console_command
from trpg_server.json_store import write_json_atomic
from trpg_server.room_console_commands import execute_room_command, room_command_help_text


def _prepare(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenario_dir = scenarios_dir / "scenario-7"
    scenario_dir.mkdir(parents=True)
    write_json_atomic(
        scenario_dir / "scenario.json",
        {
            "id": "7",
            "modules": [
                {"id": "scene-1", "module_type": "scene", "title": "大厅", "content": "大厅里有一座雕像。"},
                {"id": "ending-1", "module_type": "ending", "title": "结局 离开", "content": "调查员离开了。"},
            ],
        },
    )
    execute_console_command("narrative build 7 --yes", scenarios_dir=scenarios_dir)
    rooms_dir = tmp_path / "rooms"
    room_dir = rooms_dir / "r-1"
    room_dir.mkdir(parents=True)
    write_json_atomic(
        room_dir / "info.json",
        {
            "id": "r-1",
            "name": "房间 r-1",
            "creator_id": 1,
            "scenario_id": "7",
            "members": [
                {"user_id": 1, "username": "tester", "role": "ADMIN", "room_role": "owner", "status": "active", "is_active": True},
            ],
        },
    )
    write_json_atomic(room_dir / "messages.json", [])
    return scenarios_dir, rooms_dir, room_dir


def _run(rooms_dir, room_dir, line, app=None, confirm=None):
    return execute_room_command(
        line,
        room_id="r-1",
        room_dir=room_dir,
        rooms_dir=rooms_dir,
        app=app,
        confirm=confirm,
    )


def test_help_mentions_narrative_commands(tmp_path):
    text = room_command_help_text()
    assert "narrative status" in text
    assert "narrative rebuild-state" in text


def test_status_state_map_and_exits(tmp_path):
    scenarios_dir, rooms_dir, room_dir = _prepare(tmp_path)

    class _App:
        config = {"SCENARIOS_DIR": scenarios_dir}

    app = _App()
    result = _run(rooms_dir, room_dir, "narrative", app=app)
    assert result.ok, result.message
    assert "世界树" in result.message

    result = _run(rooms_dir, room_dir, "narrative state --json", app=app)
    assert result.ok
    payload = json.loads(result.message)
    assert "location" in payload and "clocks" in payload

    result = _run(rooms_dir, room_dir, "narrative map --depth 2", app=app)
    assert result.ok
    assert "当前地点" in result.message

    result = _run(rooms_dir, room_dir, "narrative exits", app=app)
    assert result.ok
    assert "出口" in result.message


def test_ledger_facts_clocks_triggers_and_trace(tmp_path):
    scenarios_dir, rooms_dir, room_dir = _prepare(tmp_path)

    class _App:
        config = {"SCENARIOS_DIR": scenarios_dir}

    app = _App()
    for line in (
        "narrative ledger",
        "narrative facts",
        "narrative clocks",
        "narrative triggers --fired",
        "narrative media",
        "narrative trace",
        "narrative summary",
    ):
        result = _run(rooms_dir, room_dir, line, app=app)
        assert result.ok, f"{line}: {result.message}"


def test_guard_and_profile_require_confirmation(tmp_path):
    scenarios_dir, rooms_dir, room_dir = _prepare(tmp_path)

    class _App:
        config = {"SCENARIOS_DIR": scenarios_dir}

    app = _App()
    result = _run(rooms_dir, room_dir, "narrative guard shadow", app=app)
    assert result.ok
    assert "预演" in result.message
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert "narrative_guard" not in (info.get("house_rules") or {})

    result = _run(rooms_dir, room_dir, "narrative guard shadow --yes", app=app)
    assert result.ok
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["narrative_guard"] == "shadow"

    result = _run(rooms_dir, room_dir, "narrative profile degraded --yes", app=app)
    assert result.ok
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    assert info["house_rules"]["narrative_profile"] == "degraded"

    result = _run(rooms_dir, room_dir, "narrative guard nope", app=app)
    assert not result.ok


def test_reset_unlock_and_rebuild_state(tmp_path):
    scenarios_dir, rooms_dir, room_dir = _prepare(tmp_path)

    class _App:
        config = {"SCENARIOS_DIR": scenarios_dir}

    app = _App()
    bundle = json.loads(narrative_path(scenarios_dir / "scenario-7").read_text(encoding="utf-8"))
    location_id = next(node["id"] for node in bundle["nodes"] if node.get("kind") == "location")

    result = _run(rooms_dir, room_dir, f"narrative reset {location_id} --yes", app=app)
    assert result.ok, result.message
    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert state["narrative"]["location"] == location_id

    result = _run(rooms_dir, room_dir, f"narrative unlock {location_id} --yes", app=app)
    assert result.ok
    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert location_id in state["narrative"]["unlocked"]

    result = _run(rooms_dir, room_dir, "narrative rebuild-state --yes", app=app)
    assert result.ok, result.message

    result = _run(rooms_dir, room_dir, "narrative summary --rebuild --yes", app=app)
    assert result.ok, result.message
