"""端到端流水线：extract → edges → topology → validate → index。"""
from trpg_server.agents.narrative.edges import build_structural_edges, merge_edges
from trpg_server.agents.narrative.extract import extract_narrative
from trpg_server.agents.narrative.model import NarrativeBundle, TOPOLOGIES
from trpg_server.agents.narrative.store import build_index
from trpg_server.agents.narrative.topology import detect_topology
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle

SCRIPT = """# 第一幕 雨夜
## 大厅
大厅与玄关相连，从大厅可以通往书房。
### 玄关
从玄关可以回到大厅。
### 书房
书房与大厅相邻，需要找到钥匙才能打开书房的门。
- 书桌：堆满信件。
  - 侦查：查看书桌。
    成功：发现一把铜钥匙。
    失败：只看到发霉的纸。
线索：铜钥匙
# 结局 逃出庄园
"""


def _bundle() -> NarrativeBundle:
    extracted = extract_narrative(SCRIPT, scenario_id="pipe-1")
    bundle = NarrativeBundle(scenario_id="pipe-1", nodes=list(extracted.nodes))
    bundle.meta = {"start": next(node.id for node in bundle.nodes if node.kind == "location")}
    text_by_node = {node.id: node.body for node in bundle.nodes}
    incoming = build_structural_edges(bundle.nodes, sections=extracted.sections, text_by_node=text_by_node)
    bundle.edges = merge_edges(bundle.edges, incoming)
    return bundle


def test_full_pipeline_produces_stable_outputs():
    bundle = _bundle()
    decision = detect_topology(bundle)
    bundle.topology = decision.topology
    index = build_index(bundle, start=bundle.meta.get("start"))
    report = validate_bundle(bundle, index=index)

    assert decision.topology in TOPOLOGIES
    assert 0.0 < decision.confidence <= 1.0
    assert index.start == bundle.meta["start"]
    assert index.counts["kind:location"] == 3
    assert any(edge.type == "contains" for edge in bundle.edges)

    payload = report.to_dict()
    assert payload["topology"] == bundle.topology
    assert isinstance(payload["issues"], list)
    assert payload["error_count"] >= 0

    simulated = simulate_any_order(bundle)
    assert isinstance(simulated, list)


def test_extract_edges_do_not_create_next_from_order():
    bundle = _bundle()
    assert not any(edge.type == "next" for edge in bundle.edges)
    # 正文明确写了“与玄关相连”，应产生移动边。
    assert any(edge.type == "adjacent" for edge in bundle.edges)
