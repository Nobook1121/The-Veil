"""网站 Agent 的剧本世界树工具（§8.4）测试。"""
from pathlib import Path

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.site_agent_tools import narrative as tools
from trpg_server.site_agent_tools.base import ToolContext


def _bundle() -> NarrativeBundle:
    bundle = NarrativeBundle(
        scenario_id="7",
        topology="hub",
        nodes=[
            NarrativeNode(id="room-a", kind="location", title="大厅", body="大厅", visibility="public"),
            NarrativeNode(id="clue-1", kind="clue", title="血字", body="血字", visibility="kp_only"),
        ],
        edges=[
            NarrativeEdge(id="e1", from_id="room-a", to_id="clue-1", type="reveals", visibility="hidden"),
        ],
    )
    bundle.start = {"location_id": "room-a"}
    bundle.meta = {"revision": 3}
    return bundle


def _context() -> ToolContext:
    return ToolContext(actor=None, config=None, conversation_id="c", turn_id="t")


def test_outline_and_node_reads(monkeypatch):
    bundle = _bundle()
    monkeypatch.setattr(tools, "_bundle", lambda actor, sid: ({"id": 7, "title": "测试"}, bundle, Path(".")))
    result = tools.get_outline(None, {"scenario_id": 7}, _context())
    assert result.ok
    assert result.data["topology"] == "hub"
    assert result.data["revision"] == 3
    assert result.data["items"][0]["id"] == "room-a"

    result = tools.get_node(None, {"scenario_id": 7, "node_id": "room-a"}, _context())
    assert result.ok
    assert result.data["edges"][0]["id"] == "e1"
    assert tools.get_node(None, {"scenario_id": 7, "node_id": "missing"}, _context()).ok is False


def test_patch_tightens_visibility_and_writes(tmp_path, monkeypatch):
    bundle = _bundle()
    monkeypatch.setattr(tools, "_bundle", lambda actor, sid: ({"id": 7, "title": "测试"}, bundle, tmp_path))
    monkeypatch.setattr(tools, "save_bundle", lambda storage, patched: storage / "narrative.json")

    arguments = {
        "scenario_id": 7,
        "base_version": 3,
        "operations": [
            {"op": "update_node", "node_id": "room-a", "node": {"body": "大厅中央有喷泉。"}},
            {"op": "add_node", "node": {"id": "room-b", "kind": "location", "title": "走廊", "body": "走廊"}},
            {"op": "set_visibility", "node_id": "clue-1", "visibility": "public", "reason": "想公开"},
        ],
    }
    preview = tools.preview_patch(None, arguments, _context())
    assert preview.ok, preview.error
    assert set(preview.data["diff"]["nodes"]["modified"]) == {"room-a", "clue-1"}
    assert "room-b" in preview.data["diff"]["nodes"]["added"]
    assert any("不能把" in item for item in preview.data["ignored"])

    apply_result = tools.apply_patch(None, arguments, _context())
    assert apply_result.ok, apply_result.error
    assert apply_result.data["revision"] == 4


def test_patch_rejects_stale_version(monkeypatch):
    monkeypatch.setattr(tools, "_bundle", lambda actor, sid: ({"id": 7}, _bundle(), Path(".")))
    result = tools.preview_patch(None, {"scenario_id": 7, "base_version": 2, "operations": []}, _context())
    assert not result.ok
    assert "已更新" in result.error


def test_readonly_import_is_rejected(monkeypatch):
    monkeypatch.setattr(tools, "_bundle", lambda actor, sid: ({"id": 7, "import_mode": "direct"}, _bundle(), Path(".")))
    result = tools.preview_patch(
        None,
        {"scenario_id": 7, "operations": [{"op": "delete_node", "node_id": "room-a"}]},
        _context(),
    )
    assert not result.ok
    assert "只读" in result.error
