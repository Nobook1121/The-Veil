"""narrative 控制台命令的集成测试。"""
import json

from trpg_server.agents.narrative.model import TOPOLOGIES
from trpg_server.agents.narrative.store import narrative_path
from trpg_server.console_commands import command_help_text
from trpg_server.json_store import write_json_atomic
from trpg_server.narrative_admin import handle_narrative_command


def _prepare(tmp_path):
    scenario_dir = tmp_path / "scenario-7"
    scenario_dir.mkdir()
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
    return tmp_path


def test_help_mentions_narrative_commands():
    text = command_help_text()
    assert "narrative status" in text
    assert "narrative build" in text


def test_build_status_validate_topology(tmp_path):
    root = _prepare(tmp_path)

    ok, text = handle_narrative_command(["build", "7", "--yes"], root)
    assert narrative_path(root / "scenario-7").exists()
    assert "已写入" in text

    ok, text = handle_narrative_command(["status", "7", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["exists"]
    assert payload["counts"]["kind:location"] >= 1

    ok, text = handle_narrative_command(["validate", "7"], root)
    assert "校验" in text
    assert "error" in text

    ok, text = handle_narrative_command(["topology", "7", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["detected"]["topology"] in TOPOLOGIES
    assert isinstance(payload["detected"]["evidence"], list)


def test_status_lists_all_scenarios(tmp_path):
    root = _prepare(tmp_path)
    handle_narrative_command(["build", "7", "--yes"], root)
    ok, text = handle_narrative_command(["status"], root)
    assert ok
    assert "scenario-7" in text
    assert "已生成 1 个" in text


def test_build_preview_does_not_write_without_confirmation(tmp_path):
    root = _prepare(tmp_path)
    ok, text = handle_narrative_command(["build", "7"], root, confirm=lambda _: False)
    assert not narrative_path(root / "scenario-7").exists()
    assert "预演" in text
