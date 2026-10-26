"""图谱质量指标（§7.9）测试。"""
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.quality import compute_quality


def _bundle(topology: str = "linear") -> NarrativeBundle:
    nodes = [
        NarrativeNode(id="a", kind="location", title="入口", visibility="public", body="入口"),
        NarrativeNode(id="b", kind="location", title="走廊", visibility="public", body="走廊"),
        NarrativeNode(id="c", kind="ending", title="结局", visibility="public", body="结束"),
        NarrativeNode(id="clue-1", kind="clue", title="线索", visibility="hidden", body="线索", reveal_when={"flag": "x"}),
    ]
    edges = [
        NarrativeEdge(id="e1", from_id="a", to_id="b", type="next", hard=True, condition={"flag": "x"}),
        NarrativeEdge(id="e2", from_id="b", to_id="c", type="next", source="author"),
    ]
    bundle = NarrativeBundle(scenario_id="t", topology=topology, nodes=nodes, edges=edges)
    bundle.start = {"location_id": "a"}
    return bundle


def test_quality_report_has_all_metrics():
    report = compute_quality(_bundle(), order_independent=True).to_dict()
    keys = {item["key"] for item in report["metrics"]}
    assert {"reachability", "clue_usage", "hard_ratio", "degree", "linearity", "opening"} <= keys
    assert report["passed"] > 0
    by_key = {item["key"]: item for item in report["metrics"]}
    assert by_key["reachability"]["passed"] is True
    assert by_key["order_independence"]["value"] is True
    assert by_key["opening"]["passed"] is True


def test_quality_flags_hidden_without_reveal_and_review():
    bundle = _bundle("hub")
    bundle.nodes.append(
        NarrativeNode(id="d", kind="location", title="密室", visibility="hidden", body="密室", extra={"needs_review": True})
    )
    report = compute_quality(bundle, order_independent=False)
    by_key = {item.key: item for item in report.metrics}
    assert by_key["hidden_missing"].value == 1
    assert by_key["hidden_missing"].passed is False
    assert by_key["order_independence"].passed is False
    assert "hidden_missing" in [item.key for item in report.failed]
