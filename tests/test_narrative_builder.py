"""建图流水线与存储的集成测试（§7.2）。"""
import json

from trpg_server.agents.narrative.builder import (
    build_from_scenario,
    build_narrative,
    rebuild_from_descriptor,
)
from trpg_server.agents.narrative.model import TOPOLOGIES, VISIBILITY_KP_ONLY
from trpg_server.agents.narrative.store import load_bundle, narrative_path
from trpg_server.json_store import write_json_atomic


def _scenario() -> dict:
    return {
        "id": "42",
        "version": "0.1",
        "modules": [
            {
                "id": "scene-1",
                "module_type": "scene",
                "title": "大厅",
                "visibility": "public",
                "content": "大厅与书房相连。\n- 书架：五层书架。\n  - 图书馆使用：翻找书架。\n    成功：找到一张旧地图。\n    失败：什么也没找到。",
            },
            {
                "id": "ending-1",
                "module_type": "ending",
                "title": "结局 真相",
                "visibility": "kp",
                "content": "调查员终于知道了真相。",
            },
        ],
    }


def test_build_and_save_roundtrip(tmp_path):
    scenario_dir = tmp_path / "scenario-42"
    result = build_from_scenario(_scenario(), scenario_dir=scenario_dir, save=True)

    assert narrative_path(scenario_dir).exists()
    assert result.path == narrative_path(scenario_dir)
    assert result.bundle.topology in TOPOLOGIES
    assert result.bundle.build_hash.startswith("sha1:")
    assert result.bundle.generated_at
    assert result.index.counts["kind:location"] == 1

    location = next(node for node in result.bundle.nodes if node.kind == "location")
    assert location.layout["x"] == 0
    assert location.objects[0]["name"] == "书架"

    ending = next(node for node in result.bundle.nodes if node.kind == "ending")
    assert ending.visibility == VISIBILITY_KP_ONLY

    loaded = load_bundle(scenario_dir)
    assert loaded is not None
    assert loaded.build_hash == result.bundle.build_hash
    assert len(loaded.nodes) == len(result.bundle.nodes)


def test_build_without_save_does_not_touch_disk(tmp_path):
    scenario_dir = tmp_path / "scenario-43"
    result = build_from_scenario(_scenario(), scenario_dir=scenario_dir, save=False)
    assert result.path is None
    assert not narrative_path(scenario_dir).exists()
    assert result.bundle.warnings is not None


def test_build_from_markdown_document(tmp_path):
    document = "# 第一幕\n### 大厅\n大厅与玄关相连。\n### 玄关\n从玄关可以回到大厅。\n# 结局 离开\n"
    result = build_narrative(document, scenario_id="44", scenario_dir=tmp_path / "scenario-44", save=True)
    assert [node.title for node in result.bundle.nodes if node.kind == "location"] == ["大厅", "玄关"]
    assert any(edge.type == "adjacent" for edge in result.bundle.edges)
    assert result.bundle.meta["start"]
    assert result.report.topology in TOPOLOGIES


def test_rebuild_from_descriptor(tmp_path):
    scenario_dir = tmp_path / "scenario-45"
    scenario_dir.mkdir()
    descriptor = scenario_dir / "scenario.json"
    write_json_atomic(descriptor, {"id": "45", "modules": _scenario()["modules"]})

    result = rebuild_from_descriptor(descriptor)
    assert narrative_path(scenario_dir).exists()
    assert result.bundle.scenario_id == "45"
    payload = json.loads(narrative_path(scenario_dir).read_text(encoding="utf-8"))
    assert payload["scenario_id"] == "45"
    assert payload["schema_version"] == 1
