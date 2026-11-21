"""dnd5e-vtt AI DM 战桌工具（第五刀）：四工具 + KP 战桌状态行。

覆盖（夹具照 tests/test_dnd5e_vtt_templates.py：假 socket via
``app.extensions["socketio"]``、真房间目录、``PluginRegistry(PLUGIN_ROOT).load()``
真激活）：

- 清单校验：manifest 声明四工具且插件 loaded、``create_tools()`` 与清单一致、
  ``plugin_tool_names("dnd5e")`` 含四工具、``coc7`` 不含；
- ``place_template``：DM 门闸（AI KP / 房主）、``request_id`` 幂等重放、
  玩家身份可读拒绝（不落盘）、非法 kind 拒绝、模板广播走既有通道；
- ``switch_scene``：id / 标题前缀解析、查不到附可用场景列表、AI 玩家拒绝、
  切换事件 ``dnd5e_vtt_scene_switch`` + veil-map 的 ``room_map_changed`` 广播；
- ``roll_broadcast``：走 ``roll_dice`` 管线、``dice_roll`` 元数据存在、
  ``show_table`` 透传、``visible_message`` 载体回传；
- ``worldtree_recall``：无 narrative.json 的房间优雅降级、关键词命中返回
  节点 + 邻边、kp_notes 对 DM 视角返回；
- KP 状态行：``kp_prompt_fragments`` → ``build_prompt_layers`` 动态段含该行；
  非 dnd5e 房间 / 插件缺席 → 零注入；
- veil-map 缺席：四工具降级路径（place_template 照常、switch_scene 可读
  错误不崩、状态行如实说「未启用地图」）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask

from trpg_server.agents.narrative.model import (
    VISIBILITY_KP_ONLY,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.agents.profiles import plugin_tool_names
from trpg_server.agents.prompt_builder import build_prompt_layers
from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins.contracts import STATE_LOADED
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"

FOUR_TOOLS = [
    "dnd5e-vtt.place_template",
    "dnd5e-vtt.switch_scene",
    "dnd5e-vtt.roll_broadcast",
    "dnd5e-vtt.worldtree_recall",
]


# ---------------------------------------------------------------- 装置


class _FakeSocket:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        self.events.append({"event": event, "payload": payload})


@pytest.fixture()
def plugins():
    """装载真实插件根：dnd5e 规则集并入 builtin registry，四工具真激活。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _vtt_module():
    """registry 真激活时挂进 sys.modules 的 agent_tools 模块。"""
    module = sys.modules.get("trpg_plugin_dnd5e_vtt_agent_tools")
    assert module is not None, "dnd5e-vtt 插件应已按注册名装载 agent_tools"
    return module


def _tools() -> dict[str, Any]:
    return {str(tool.name): tool for tool in plugin_runtime.extra_agent_tools()}


def _write_room(rooms: Path, room_id: str = "room1") -> tuple[Path, dict[str, Any]]:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "战桌工具测试房",
        "room_code": room_id.upper(),
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "scenario_id": "s1",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "艾莉丝"},
            },
            {
                "user_id": "u2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "鲍勃"},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room, info


def _write_combat(room: Path) -> None:
    state = {
        "combat": {
            "active": True,
            "round": 2,
            "turn": 0,
            "order": ["强盗头目", "艾莉丝"],
            "combatants": {},
        }
    }
    (room / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _scenario(tmp_path: Path, *, with_worldtree: bool = True) -> Path:
    """剧本 s1：矿坑地图模块（声明玩家版底图）+ 强盗头目 NPC 模块 + 世界树。"""
    scenarios = tmp_path / "scenarios"
    scenario_dir = scenarios / "s1"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    scenario = {
        "id": "s1",
        "title": "战桌工具剧本",
        "ruleset_id": "dnd5e",
        "start": {"location_id": "site_trail"},
        "modules": [
            {"id": "area_map", "title": "矿坑"},
            {
                "id": "npc_bandit",
                "module_type": "npc",
                "title": "强盗头目",
                "content": "守在矿坑入口，索要过路费。",
            },
        ],
        "assets": [
            {
                "id": "asset_map_player",
                "kind": "image",
                "name": "矿坑（玩家版）",
                "url": "/assets/scenarios/s1/map.webp",
                "width": 1600,
                "height": 1200,
                "visibility": "public",
            }
        ],
        "triggers": [
            {
                "id": "trg_map_area",
                "binding": {"kind": "enter", "location_id": "area_map"},
                "attachments": [
                    {
                        "asset_id": "asset_map_player",
                        "kind": "image",
                        "visibility": "player_visible",
                        "order": 0,
                    }
                ],
                "group": "地图",
                "enabled": True,
            }
        ],
    }
    (scenario_dir / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False), encoding="utf-8"
    )
    if with_worldtree:
        nodes = [
            NarrativeNode(id="site_trail", kind="location", title="小径", extra={"node_role": "site"}),
            NarrativeNode(
                id="area_map",
                kind="location",
                title="矿坑",
                extra={
                    "node_role": "map",
                    "map": {"scene_id": "area_map", "entry_pois": [], "environment": {"lighting": "昏暗"}},
                },
            ),
            NarrativeNode(
                id="npc_bandit",
                kind="npc",
                title="强盗头目",
                keywords=["强盗"],
                aliases=["强盗队长"],
                summary="守在矿坑入口的强盗头目。",
                body="强盗头目带着两名手下守在矿坑入口，向过路者索要十枚银币。",
                kp_notes="强盗头目其实是前任守卫队长，被矿主辞退后落草。",
            ),
            NarrativeNode(
                id="item_silver_key",
                kind="item",
                title="银钥匙",
                keywords=["银钥匙"],
                summary="一把发黑的银钥匙。",
                kp_notes="能打开矿坑深处的墓穴门。",
                visibility=VISIBILITY_KP_ONLY,
            ),
        ]
        edges = [
            NarrativeEdge(
                id="edge_bandit_map",
                from_id="npc_bandit",
                to_id="area_map",
                type="adjacent",
                label="守卫矿坑",
                kp_notes="强盗只在白天守卫。",
            ),
            NarrativeEdge(
                id="edge_trail_map",
                from_id="site_trail",
                to_id="area_map",
                type="portal",
                label="进入矿坑",
                bidirectional=True,
            ),
        ]
        save_bundle(scenario_dir, NarrativeBundle(scenario_id="s1", nodes=nodes, edges=edges))
    return scenarios


def _app(tmp_path: Path, rooms: Path, scenarios: Path) -> Flask:
    app = Flask(__name__)
    app.secret_key = "test"
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(config), SCENARIOS_DIR=str(scenarios))
    app.extensions["socketio"] = _FakeSocket()
    return app


def _context(
    room: Path,
    info: dict[str, Any],
    *,
    agent_id: str = "kp",
    user_id: str = "",
    scenarios: Path | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        room_id=str(info.get("id") or "room1"),
        room_dir=room,
        scenarios_dir=scenarios,
        agent_id=agent_id,
        user_id=user_id,
        room_info=lambda: info,
        room_state=lambda: {},
        config_dir=None,
        turn=0,
    )


def _stored_templates(room: Path) -> list[dict[str, Any]]:
    path = room / "plugins" / "dnd5e-vtt" / "templates.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("templates") or []


# ---------------------------------------------------------------- 清单校验


def test_manifest_declares_tools_and_registry_activates(plugins):
    record = plugins.get("dnd5e-vtt")
    assert record is not None
    assert record.state == STATE_LOADED
    assert "agent_tools" in (record.loaded_capabilities or ())
    declared = list(record.manifest.capabilities["agent_tools"]["tools"])
    assert declared == FOUR_TOOLS

    # 真激活后的注册表与清单一致，且全部限定 dnd5e 规则集。
    tools = _tools()
    assert sorted(name for name in tools if name.startswith("dnd5e-vtt.")) == sorted(FOUR_TOOLS)
    for name in FOUR_TOOLS:
        assert tools[name].rulesets == ("dnd5e",)

    # 工厂产出与清单一致（核心激活期调用的同一入口）。
    module = _vtt_module()
    assert sorted(tool.name for tool in module.create_tools()) == sorted(FOUR_TOOLS)

    # 规则集过滤：dnd5e 房间下发四工具；coc7 房间不下发。
    dnd5e_names = plugin_tool_names("dnd5e")
    for name in FOUR_TOOLS:
        assert name in dnd5e_names
    coc7_names = plugin_tool_names("coc7")
    assert not (set(FOUR_TOOLS) & set(coc7_names))


# ---------------------------------------------------------------- place_template


def test_place_template_dm_gate_idempotency_and_broadcast(tmp_path, plugins):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    place = _tools()["dnd5e-vtt.place_template"].handler
    cone = {
        "kind": "cone",
        "cell_x": 0,
        "cell_y": 0,
        "size_cells": 3,
        "direction": {"dx": 1, "dy": 0},
        "label": "燃烧之手",
        "request_id": "req-ai-1",
    }
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)
        first = place(dict(cone), kp)
        assert first["ok"] is True
        assert first["duplicate"] is False
        assert str(first["template_id"]).startswith("tpl-")
        assert "燃烧之手" in first["summary"]

        # request_id 幂等重放：不再产生第二个模板。
        replay = place(dict(cone), kp)
        assert replay["ok"] is True
        assert replay["duplicate"] is True
        stored = _stored_templates(room)
        assert len(stored) == 1
        assert stored[0]["placed_by"] == "ai"
        assert stored[0]["kind"] == "cone"
        assert stored[0]["direction"] == {"dx": 1.0, "dy": 0.0}

        # 广播走既有模板通道。
        events = [
            item
            for item in app.extensions["socketio"].events
            if item["event"] == "dnd5e_vtt_templates"
        ]
        assert events
        assert events[-1]["payload"]["revision"] == 1

        # 真人房主（无 AI 身份）同样视为 DM。
        owner = _context(room, info, agent_id="", user_id="u1", scenarios=scenarios)
        second = place(
            {"kind": "circle", "cell_x": 3, "cell_y": 4, "size_cells": 4, "request_id": "req-ai-2"},
            owner,
        )
        assert second["ok"] is True

        # 玩家身份可读拒绝：不落盘、不给模板 id。
        player = _context(room, info, agent_id="", user_id="u2", scenarios=scenarios)
        denied = place(
            {"kind": "circle", "cell_x": 1, "cell_y": 1, "size_cells": 2, "request_id": "req-ai-3"},
            player,
        )
        assert denied["ok"] is False
        assert denied["code"] == "dnd5e_vtt.dm_only"
        assert "DM" in denied["error"]

        # 非法 kind 拒绝。
        bad = place(
            {"kind": "sphere", "cell_x": 1, "cell_y": 1, "size_cells": 2, "request_id": "req-ai-4"},
            kp,
        )
        assert bad["ok"] is False
        assert bad["code"] == "dnd5e_vtt.invalid_template"

    assert len(_stored_templates(room)) == 2


# ---------------------------------------------------------------- switch_scene


def test_switch_scene_resolution_errors_and_events(tmp_path, plugins):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    switch = _tools()["dnd5e-vtt.switch_scene"].handler
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)

        # id 精确匹配：世界树大节点首次建场景。
        first = switch({"scene": "area_map", "reason": "众人推开矿坑木门"}, kp)
        assert first["ok"] is True, first
        assert first["scene_id"] == "area_map"
        assert first["area_id"] == "area_map"
        assert first["title"] == "矿坑"
        assert first["created"] is True
        assert first["changed"] is True
        assert "矿坑" in first["summary"]

        # 标题前缀匹配：场景已存在，切到自身 → changed=False。
        second = switch({"scene": "矿"}, kp)
        assert second["ok"] is True
        assert second["scene_id"] == "area_map"
        assert second["changed"] is False
        assert "已是当前场景" in second["summary"]

        # 切换事件与地图广播都走了假 socket；重复切同场景不再发事件。
        events = app.extensions["socketio"].events
        switch_events = [item for item in events if item["event"] == "dnd5e_vtt_scene_switch"]
        assert len(switch_events) == 1
        assert switch_events[0]["payload"]["scene_id"] == "area_map"
        assert switch_events[0]["payload"]["title"] == "矿坑"
        assert switch_events[0]["payload"]["by"] == "ai_kp"
        assert any(item["event"] == "room_map_changed" for item in events)

        # 查不到时错误信息附可用场景列表。
        missing = switch({"scene": "亚特兰蒂斯"}, kp)
        assert missing["ok"] is False
        assert missing["code"] == "dnd5e_vtt.scene_not_found"
        assert "亚特兰蒂斯" in missing["error"]
        assert "矿坑" in missing["error"]

        # 缺 scene 参数可读拒绝。
        empty = switch({}, kp)
        assert empty["ok"] is False
        assert empty["code"] == "dnd5e_vtt.scene_required"

        # AI 玩家身份拒绝。
        ai_player = _context(room, info, agent_id="player", user_id="u2", scenarios=scenarios)
        denied = switch({"scene": "area_map"}, ai_player)
        assert denied["ok"] is False
        assert denied["code"] == "dnd5e_vtt.dm_only"


# ---------------------------------------------------------------- roll_broadcast


def test_roll_broadcast_uses_dice_pipeline_with_metadata(tmp_path, plugins):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    roll = _tools()["dnd5e-vtt.roll_broadcast"].handler
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)

        result = roll(
            {"expression": "1d20", "label": "火焰喷吐伤害", "roller": "强盗头目"}, kp
        )
        assert result["ok"] is True
        assert result["broadcast"] is True
        assert len(result["rolls"]) == 1
        assert 1 <= result["rolls"][0] <= 20
        assert result["total"] == result["rolls"][0]
        assert "强盗头目的火焰喷吐伤害" in result["summary"]

        # visible_message 载体回传：runtime 靠它把骰卡广播进聊天流。
        message = result.get("visible_message")
        assert isinstance(message, dict)
        assert message["content"] == result["summary"]
        metadata = message.get("metadata") or {}
        dice = metadata.get("dice_roll")
        assert isinstance(dice, dict)
        assert dice["expression"] == "1d20"
        assert dice["show_table"] is True
        assert dice["roller"] == "强盗头目"
        assert dice["label"] == "火焰喷吐伤害"
        assert metadata["roller"] == "强盗头目"

        # 裸掷（无 label/roller）与加法混合表达式也走同一条管线。
        plain = roll({"expression": "2d6"}, kp)
        assert plain["ok"] is True
        assert len(plain["rolls"]) == 2
        assert plain["total"] == sum(plain["rolls"])
        assert isinstance(plain.get("visible_message"), dict)
        mixed = roll({"expression": "1d100+2d6"}, kp)
        assert mixed["ok"] is True
        assert len(mixed["rolls"]) == 3

        # 非法表达式可读拒绝。
        bad = roll({"expression": "banana"}, kp)
        assert bad["ok"] is False
        assert bad["code"] == "dnd5e_vtt.invalid_expression"

        # 缺表达式可读拒绝。
        empty = roll({}, kp)
        assert empty["ok"] is False
        assert empty["code"] == "dnd5e_vtt.expression_required"

        # 玩家身份拒绝。
        player = _context(room, info, agent_id="", user_id="u2", scenarios=scenarios)
        denied = roll({"expression": "1d20"}, player)
        assert denied["ok"] is False
        assert denied["code"] == "dnd5e_vtt.dm_only"


# ---------------------------------------------------------------- worldtree_recall


def test_worldtree_recall_degrades_without_narrative(tmp_path, plugins):
    scenarios = _scenario(tmp_path, with_worldtree=False)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    recall = _tools()["dnd5e-vtt.worldtree_recall"].handler
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)
        degraded = recall({"query": "强盗"}, kp)
    assert degraded["ok"] is False
    assert degraded["code"] == "dnd5e_vtt.worldtree_unavailable"
    assert "世界树" in degraded["error"]


def test_worldtree_recall_returns_nodes_edges_and_kp_notes(tmp_path, plugins):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    recall = _tools()["dnd5e-vtt.worldtree_recall"].handler
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)

        hit = recall({"query": "强盗 银钥匙"}, kp)
        assert hit["ok"] is True
        assert hit["kp_view"] is True
        ids = [item["node_id"] for item in hit["matches"]]
        assert "npc_bandit" in ids
        assert "item_silver_key" in ids

        bandit = next(item for item in hit["matches"] if item["node_id"] == "npc_bandit")
        assert bandit["kp_notes"] == "强盗头目其实是前任守卫队长，被矿主辞退后落草。"
        assert "前任守卫队长" not in bandit["summary"]
        edge_rows = [row for row in bandit["edges"] if row["to"] == "area_map"]
        assert edge_rows
        assert edge_rows[0]["label"] == "守卫矿坑"
        assert edge_rows[0]["to_title"] == "矿坑"
        assert edge_rows[0]["kp_notes"] == "强盗只在白天守卫。"

        key = next(item for item in hit["matches"] if item["node_id"] == "item_silver_key")
        assert key["visibility"] == VISIBILITY_KP_ONLY
        assert key["kp_notes"] == "能打开矿坑深处的墓穴门。"

        # DM 视角提示随结果返回。
        assert "不得原文复述" in hit["note"]

        # 无命中：ok=True 空清单 + 提示换词。
        miss = recall({"query": "亚特兰蒂斯"}, kp)
        assert miss["ok"] is True
        assert miss["matches"] == []
        assert "没有匹配" in miss["summary"]

        # 缺关键词可读拒绝。
        empty = recall({}, kp)
        assert empty["ok"] is False
        assert empty["code"] == "dnd5e_vtt.query_required"

        # AI 玩家身份拒绝：拿不到 kp_only 内容。
        ai_player = _context(room, info, agent_id="player", user_id="u2", scenarios=scenarios)
        denied = recall({"query": "强盗"}, ai_player)
        assert denied["ok"] is False
        assert denied["code"] == "dnd5e_vtt.dm_only"


# ---------------------------------------------------------------- KP 状态行与提示词注入


def test_kp_prompt_fragments_reach_dynamic_prompt_layer(tmp_path, plugins):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    _write_combat(room)
    app = _app(tmp_path, rooms, scenarios)
    module = _vtt_module()
    with app.app_context():
        service = plugin_runtime.get_service("dnd5e-vtt", "kp_context")
        assert service is not None

        kp = _context(room, info, scenarios=scenarios)
        fragments = service.kp_prompt_fragments(kp)
        assert len(fragments) == 1
        line = fragments[0]
        # 状态行如实摘要：零场景、战斗回合、先攻与工具指引。
        assert line.startswith("【战桌状态】")
        assert "无地图场景" in line
        assert "第 2 回合" in line
        assert "当前行动：强盗头目" in line
        assert "测量模板 0 个" in line
        assert "dnd5e-vtt.place_template" in line
        assert "dnd5e-vtt.worldtree_recall" in line

        # build_prompt_layers 动态段含该行（turn_pipeline 同一通道拼接）。
        layers = build_prompt_layers(
            "全局规则", None, None, None, [], "玩家输入", ruleset_fragments=fragments
        )
        assert "ruleset" in layers.segments
        ruleset_message = next(
            message for message in layers.messages if "规则集判定口径" in message["content"]
        )
        assert "【战桌状态】" in ruleset_message["content"]

        # 模板落盘后状态行计数随之更新。
        _tools()["dnd5e-vtt.place_template"].handler(
            {"kind": "circle", "cell_x": 1, "cell_y": 1, "size_cells": 2, "request_id": "kp-line-1"},
            kp,
        )
        assert "测量模板 1 个" in service.kp_prompt_fragments(kp)[0]

        # 非 dnd5e 房间：服务侧零注入。
        coc_info = dict(info, ruleset_id="coc7")
        coc_kp = _context(room, coc_info, scenarios=scenarios)
        assert service.kp_prompt_fragments(coc_kp) == []

        # 无房间上下文：零注入、不抛异常。
        assert service.kp_prompt_fragments(SimpleNamespace(room_dir=None, room_info=lambda: {})) == []

    # 空片段列表 → 动态段零注入。
    empty_layers = build_prompt_layers(
        "全局规则", None, None, None, [], "玩家输入", ruleset_fragments=[]
    )
    assert "ruleset" not in empty_layers.segments


def test_fragment_channel_zero_injection_when_plugin_absent(plugins):
    from trpg_server.plugins.runtime import unregister_plugin

    # 模拟插件缺席：服务/工具一并注销，注入通道拿不到任何东西。
    unregister_plugin("dnd5e-vtt")
    assert plugin_runtime.get_service("dnd5e-vtt", "kp_context") is None
    assert not (set(plugin_tool_names("dnd5e")) & set(FOUR_TOOLS))
    layers = build_prompt_layers(
        "全局规则", None, None, None, [], "玩家输入", ruleset_fragments=None
    )
    assert "ruleset" not in layers.segments


# ---------------------------------------------------------------- veil-map 缺席降级


def test_tools_degrade_when_veil_map_absent(tmp_path, plugins, monkeypatch):
    scenarios = _scenario(tmp_path)
    rooms = tmp_path / "rooms"
    room, info = _write_room(rooms)
    app = _app(tmp_path, rooms, scenarios)
    module = _vtt_module()
    monkeypatch.setattr(module, "_map_service", lambda: None)
    tools = _tools()
    with app.app_context():
        kp = _context(room, info, scenarios=scenarios)

        # switch_scene：可读错误，不崩。
        result = tools["dnd5e-vtt.switch_scene"].handler({"scene": "矿坑"}, kp)
        assert result["ok"] is False
        assert result["code"] == "dnd5e_vtt.map_unavailable"
        assert "veil-map" in result["error"]

        # place_template：模板不依赖地图服务，照常落盘。
        placed = tools["dnd5e-vtt.place_template"].handler(
            {"kind": "line", "cell_x": 0, "cell_y": 0, "size_cells": 6,
             "direction": {"dx": 1, "dy": 0}, "request_id": "no-map-1"},
            kp,
        )
        assert placed["ok"] is True
        assert len(_stored_templates(room)) == 1

        # roll_broadcast：骰管线照常。
        rolled = tools["dnd5e-vtt.roll_broadcast"].handler({"expression": "1d6"}, kp)
        assert rolled["ok"] is True

        # worldtree_recall：纯叙事检索不受地图缺席影响。
        recalled = tools["dnd5e-vtt.worldtree_recall"].handler({"query": "强盗"}, kp)
        assert recalled["ok"] is True
        assert any(item["node_id"] == "npc_bandit" for item in recalled["matches"])

        # KP 状态行：地图段如实说「未启用地图」，其余字段照常。
        line = module.battle_status_line(room, info)
        assert "未启用地图" in line
        assert "未开战" in line
        assert "测量模板 1 个" in line
