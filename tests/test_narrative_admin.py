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


def test_rebuild_index_and_stats(tmp_path):
    root = _prepare(tmp_path)
    handle_narrative_command(["build", "7", "--yes"], root)

    ok, text = handle_narrative_command(["rebuild-index", "7", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["index"]["counts"]["nodes"] >= 1
    assert payload["summary"]["counts"]["nodes"] >= 1

    ok, text = handle_narrative_command(["stats", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["totals"]["built"] == 1
    assert payload["totals"]["nodes"] >= 1


def test_diff_between_versions(tmp_path):
    root = _prepare(tmp_path)
    scenario_dir = root / "scenario-7"
    versions = scenario_dir / "versions"
    versions.mkdir()
    descriptor = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
    write_json_atomic(versions / "1.json", descriptor)
    changed = dict(descriptor)
    changed["modules"] = list(descriptor["modules"]) + [
        {"id": "scene-2", "module_type": "scene", "title": "侧厅", "content": "侧厅空无一人。"},
    ]
    write_json_atomic(versions / "2.json", changed)

    ok, text = handle_narrative_command(["diff", "7", "1", "2", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["from_version"] == "1"
    assert payload["to_version"] == "2"
    assert "nodes" in payload and "added" in payload["nodes"]

    ok, text = handle_narrative_command(["diff", "7", "1", "9"], root)
    assert not ok
    assert "不存在版本" in text


def test_backfill_preview_then_write(tmp_path):
    root = _prepare(tmp_path)

    ok, text = handle_narrative_command(["backfill"], root, confirm=lambda _: False)
    assert ok
    assert "预演" in text
    assert not narrative_path(root / "scenario-7").exists()

    ok, text = handle_narrative_command(["backfill", "--yes"], root)
    assert ok
    assert narrative_path(root / "scenario-7").exists()

    ok, text = handle_narrative_command(["backfill", "--yes"], root)
    assert "无需回填" in text


def test_media_and_visibility_commands(tmp_path):
    root = _prepare(tmp_path)
    handle_narrative_command(["build", "7", "--yes"], root)

    ok, text = handle_narrative_command(["media", "7", "--json"], root)
    assert ok
    assert isinstance(json.loads(text), list)

    ok, text = handle_narrative_command(["media", "verify", "7", "--json"], root)
    payload = json.loads(text)
    assert payload["broken"] == 0

    ok, text = handle_narrative_command(["visibility", "7", "--json"], root)
    assert ok
    payload = json.loads(text)
    assert payload["rows"]
    assert any("needs_review" in row for row in payload["rows"])


def test_topology_and_opening_set(tmp_path):
    root = _prepare(tmp_path)
    handle_narrative_command(["build", "7", "--yes"], root)

    ok, text = handle_narrative_command(["topology", "7", "--set", "hybrid", "--yes"], root)
    assert ok
    assert "hybrid" in text
    ok, text = handle_narrative_command(["status", "7", "--json"], root)
    assert json.loads(text)["topology"] == "hybrid"

    ok, text = handle_narrative_command(["topology", "7", "--set", "nope"], root)
    assert not ok

    bundle = json.loads(narrative_path(root / "scenario-7").read_text(encoding="utf-8"))
    location_id = next(
        node["id"] for node in bundle["nodes"] if node.get("kind") == "location"
    )
    ok, text = handle_narrative_command(
        ["opening", "set", "7", "--location", location_id, "--mode", "model", "--yes"], root
    )
    assert ok, text
    bundle = json.loads(narrative_path(root / "scenario-7").read_text(encoding="utf-8"))
    assert bundle["start"]["location_id"] == location_id
    assert bundle["start"]["opening_mode"] == "model"

    ok, text = handle_narrative_command(
        ["opening", "set", "7", "--location", "missing", "--yes"], root
    )
    assert not ok
