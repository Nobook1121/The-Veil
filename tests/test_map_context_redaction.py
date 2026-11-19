"""M7：地图底图 / DM 标记不进入 AI 工具出口。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode
from trpg_server.agents.narrative.redaction import (
    redact_map_text,
    scenario_has_map_layer,
    strip_map_fields,
    strip_trigger_asset_fields,
)
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.agents.tools.room import get_room_scenario_context, get_room_scenario_module


MAP_MODULE = {
    "id": "p1_hideout_2_lookout",
    "module_type": "scene",
    "scene_id": "p1_hideout",
    "title": "2. 地精暗哨",
    "summary": "灌木后传来压抑的呼吸声。",
    "marker": "地精暗哨",
    "content": "灌木后传来呼吸声。\n![克拉摩窝点](/assets/scenarios/maps/hideout.webp)\n地精正在探头。",
    "metadata": {
        "map_asset": "asset_map_cragmaw_hideout_player",
        "map_config": {"cell_px": 64, "offset_x": 3, "offset_y": 4},
        "markers": [{"id": "poi-2", "x": 12, "y": 8}],
        "background_url": "/assets/scenarios/maps/hideout.webp",
        "source": "lmop",
    },
    "triggers": [
        {
            "id": "trg_map",
            "display_name": "展示地图：克拉摩窝点",
            "keyword": "地图",
            "content_mode": "image",
            "asset_url": "/assets/scenarios/maps/hideout.webp",
            "asset_name": "克拉摩窝点（玩家版）",
        }
    ],
}


def _scenario_dir(tmp_path, *, map_layer: bool):
    storage = tmp_path / "scenarios" / "scenario-s-map"
    storage.mkdir(parents=True)
    (storage / "scenario.json").write_text(
        json.dumps({"id": "s-map", "title": "地图剧本", "modules": [MAP_MODULE]}, ensure_ascii=False),
        encoding="utf-8",
    )
    if map_layer:
        save_bundle(
            storage,
            NarrativeBundle(
                scenario_id="s-map",
                nodes=[
                    NarrativeNode(
                        id="p1_hideout",
                        kind="location",
                        title="克拉摩窝点",
                        extra={"node_role": "map", "map": {"scene_id": "p1_hideout"}},
                    ),
                    NarrativeNode(
                        id="p1_hideout_2_lookout",
                        kind="location",
                        title="2. 地精暗哨",
                        parent="p1_hideout",
                        extra={"node_role": "poi", "poi": {"area_id": "p1_hideout"}},
                    ),
                ],
                edges=[],
            ),
        )
    else:
        # 没有 narrative.json：旧剧本（含 CoC7）保持原输出。
        (storage / "narrative.json").unlink(missing_ok=True)
    return storage


def _context(tmp_path, *, map_layer: bool):
    _scenario_dir(tmp_path, map_layer=map_layer)
    return SimpleNamespace(
        scenarios_dir=tmp_path / "scenarios",
        room_dir=tmp_path / "room",
        room_id="r-map",
        room_info=lambda: {"scenario_id": "s-map"},
    )


def test_redact_map_text_replaces_only_image_syntax():
    text = "地图：![克拉摩窝点](/scenarios/maps/hideout.webp)\n<img src=\"/scenarios/maps/dm.webp\">\n[普通链接](https://example.com)"
    redacted = redact_map_text(text)

    assert "hideout.webp" not in redacted
    assert "dm.webp" not in redacted
    assert "[图片：克拉摩窝点]" in redacted
    assert "[普通链接](https://example.com)" in redacted


def test_strip_map_fields_keeps_summary_marker():
    payload = {
        "marker": "地精暗哨",
        "summary": "灌木后传来呼吸声。",
        "metadata": {"map_asset": "x", "source": "lmop"},
        "triggers": [{"id": "t1", "asset_url": "/maps/x.webp", "keyword": "地图"}],
    }
    stripped = strip_trigger_asset_fields(strip_map_fields(payload))

    assert stripped["marker"] == "地精暗哨"
    assert stripped["summary"] == "灌木后传来呼吸声。"
    assert stripped["metadata"] == {"source": "lmop"}
    assert stripped["triggers"] == [{"id": "t1", "keyword": "地图"}]


def test_scenario_module_strips_map_assets_and_dm_markers(tmp_path):
    context = _context(tmp_path, map_layer=True)
    result = get_room_scenario_module({"module_id": "p1_hideout_2_lookout"}, context)
    module = result["module"]

    assert "map_asset" not in module["metadata"]
    assert "map_config" not in module["metadata"]
    assert "markers" not in module["metadata"]
    assert "background_url" not in module["metadata"]
    assert module["metadata"]["source"] == "lmop"
    # 单数 marker 是摘要文本，不能误删。
    assert module["marker"] == "地精暗哨"
    assert module["summary"] == "灌木后传来压抑的呼吸声。"
    # 模块内触发器目录与正文图片同样剥离。
    module_trigger = next(item for item in module["triggers"] if item.get("keyword") == "地图")
    assert "asset_url" not in module_trigger
    assert "asset_name" not in module_trigger
    assert "hideout.webp" not in module["content"]
    assert "[图片" in module["content"]


def test_scenario_context_trigger_catalog_hides_asset_urls(tmp_path):
    context = _context(tmp_path, map_layer=True)
    result = get_room_scenario_context({}, context)
    trigger = next(item for item in result["triggers"] if item.get("keyword") == "地图")

    assert "asset_url" not in trigger
    assert "asset_name" not in trigger
    assert trigger["keyword"] == "地图"


def test_legacy_scenario_without_map_layer_is_unchanged(tmp_path):
    context = _context(tmp_path, map_layer=False)
    assert scenario_has_map_layer(context.scenarios_dir, "s-map") is False

    module = get_room_scenario_module({"module_id": "p1_hideout_2_lookout"}, context)["module"]
    assert module["metadata"]["map_asset"] == "asset_map_cragmaw_hideout_player"
    trigger = next(item for item in module["triggers"] if item.get("keyword") == "地图")
    assert trigger["asset_url"] == "/assets/scenarios/maps/hideout.webp"
    assert trigger["asset_name"] == "克拉摩窝点（玩家版）"
    assert "hideout.webp" in module["content"]

    catalog_trigger = next(
        item
        for item in get_room_scenario_context({}, context)["triggers"]
        if item.get("keyword") == "地图"
    )
    assert catalog_trigger["asset_url"] == "/assets/scenarios/maps/hideout.webp"
    assert catalog_trigger["asset_name"] == "克拉摩窝点（玩家版）"


def test_scenario_has_map_layer_resolves_versioned_descriptor(tmp_path):
    storage = _scenario_dir(tmp_path, map_layer=True)
    versions = storage / "versions"
    versions.mkdir()
    (versions / "1.0.0.json").write_text(
        json.dumps({"id": "s-map", "scenario_version": "1.0.0", "modules": [MAP_MODULE]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert scenario_has_map_layer(tmp_path / "scenarios", "s-map", "1.0.0") is True
