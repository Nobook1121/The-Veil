from __future__ import annotations

from trpg_server.scenario_bundle import (
    BUNDLE_FORMAT,
    build_narrative,
    normalize_bundle,
)


def _base_payload(nodes, edges, modules=None):
    return {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "scenario": {"title": "旧宅", "author": "Alice", "playerCount": 2},
        "modules": modules or [],
        "narrative": {"topology": "hybrid", "start": {"location_id": "hall"}, "nodes": nodes, "edges": edges},
    }


def test_bundle_fixes_bad_edges_and_fills_missing_module_nodes():
    modules = [
        {"id": "hall", "title": "旧宅大厅", "module_type": "scene", "content": "大厅很暗。", "summary": "大厅"},
        {"id": "cellar", "title": "地下室", "module_type": "scene", "content": "地下室有一扇铁门。"},
        {"id": "finale", "title": "逃出旧宅", "module_type": "ending", "content": "玩家离开旧宅。"},
    ]
    payload = _base_payload(
        nodes=[
            {"id": "hall", "kind": "location", "title": "旧宅大厅"},
            # cellar 与 finale 故意缺失：程序必须按模块补齐。
            {"id": "butler", "kind": "npc", "title": "管家"},
            {"id": "key", "kind": "item", "title": "铜钥匙"},
        ],
        edges=[
            {"from": "hall", "to": "cellar", "type": "adjacent"},
            {"from": "hall", "to": "ghost", "type": "adjacent"},  # 端点不存在
            {"from": "butler", "to": "butler", "type": "knows"},  # 自环
            {"from": "butler", "to": "hall", "type": "relationship"},  # 非 NPC 对端
            {"from": "key", "to": "hall", "type": "located_in"},  # 反向别名 → 地点 contains 物品
        ],
        modules=modules,
    )
    result = normalize_bundle(payload, default_scenario_id="42", default_version="1.0.0")
    assert result.ok, result.errors

    nodes = {node["id"]: node for node in result.data["narrative"]["nodes"]}
    assert {"hall", "cellar", "finale", "butler", "key"} <= set(nodes)
    assert nodes["cellar"]["kind"] == "location"
    assert nodes["finale"]["kind"] == "ending"
    # 模块与补齐节点绑定，避免同一地点重复生成节点。
    assert [module["scene_id"] for module in result.data["modules"]] == ["hall", "cellar", "finale"]

    edges = {(edge["from"], edge["to"], edge["type"]) for edge in result.data["narrative"]["edges"]}
    assert ("hall", "cellar", "adjacent") in edges
    assert ("hall", "key", "contains") in edges
    assert ("hall", "ghost", "adjacent") not in edges
    assert ("butler", "butler", "knows") not in edges
    assert ("butler", "hall", "relationship") not in edges

    codes = {item["code"] for item in result.warnings}
    assert {"edge.endpoint", "edge.self", "edge.relationship"} <= codes
    assert "node.module_missing" in codes  # cellar / finale 缺节点，程序按模块补齐并记录

    narrative, _repair, validation = build_narrative(result)
    assert narrative.scenario_id == "42"
    assert narrative.start.get("location_id") == "hall"
    assert validation.to_dict().get("error_count") == 0


def test_bundle_drops_unknown_kind_and_resets_invalid_start():
    payload = _base_payload(
        nodes=[
            {"id": "hall", "kind": "location", "title": "大厅"},
            {"id": "weird", "kind": "weather", "title": "天气"},  # 非法 kind
        ],
        edges=[{"from": "hall", "to": "weird", "type": "contains"}],
        modules=[
            {"id": "hall", "title": "大厅", "module_type": "scene", "content": "大厅内容"},
        ],
    )
    payload["narrative"]["start"] = {"location_id": "weird"}
    result = normalize_bundle(payload)

    ids = {node["id"] for node in result.data["narrative"]["nodes"]}
    assert ids == {"hall"}
    assert result.data["narrative"]["start"]["location_id"] == "hall"
    assert any(item["code"] == "node.kind" for item in result.warnings)
    assert any(item["code"] == "start.invalid" for item in result.warnings)


def test_bundle_reports_missing_title_and_modules():
    result = normalize_bundle({"format": BUNDLE_FORMAT, "scenario": {}, "modules": []})
    assert result.ok is False
    codes = {item["code"] for item in result.errors}
    assert {"scenario.title", "modules.empty"} <= codes
