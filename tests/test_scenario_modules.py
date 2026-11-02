import json

from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.profiles import DEFAULT_KP_TOOLS
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.room import get_room_scenario_context, get_room_scenario_module
from trpg_server.agents.tools.trigger import reveal_scenario_trigger
from trpg_server.scenario_store import build_trigger_message, normalize_scenario_payload, normalize_trigger
from trpg_server.scenario_importer import convert_script_to_scenario
from trpg_server.scenario_importer import analyze_script_structure, build_ai_conversion_prompt


def test_kp_tools_include_scenario_module_reader():
    registry = default_tool_registry()

    assert "room.get_scenario_module" in DEFAULT_KP_TOOLS
    assert registry.get("room.get_scenario_module") is not None


def test_script_importer_preserves_source_and_builds_scene_manifest():
    result = convert_script_to_scenario("# 场景 一\n7号车厢。\n\n## 结局\n列车抵达终点。", {"title": "测试"})
    assert result["title"] == "测试"
    assert result["conversion"]["source_preserved"] is True
    assert result["modules"][0]["content"] == "7号车厢。"
    assert result["modules"][0]["module_type"] == "scene"
    assert any(item["module_type"] == "ending" for item in result["modules"])


def test_script_importer_splits_location_and_ending_headings():
    result = convert_script_to_scenario(
        "概要\n推荐人数 2人\n\n<6号车厢>\n入口线索\n\n<7号车厢>\n尸体\n\nTrue End\n成功脱出"
    )
    assert [item["module_type"] for item in result["modules"]] == ["background", "scene", "scene", "ending"]
    assert [item["title"] for item in result["modules"]][1:3] == ["6号车厢", "7号车厢"]


def test_ai_conversion_prompt_describes_editor_fields_and_source_sections():
    prompt = build_ai_conversion_prompt("<6号车厢>\n入口\n\nTrue End\n成功")
    assert "SOURCE_SECTION 1" in prompt
    assert "每个车厢、房间必须独立" in prompt
    assert "不要输出 content" in prompt


def test_structure_analysis_reports_multiple_sections():
    analysis = analyze_script_structure("<6号车厢>\n入口\n\n<7号车厢>\n出口")
    assert analysis["section_count"] == 2
    assert analysis["detected_types"] == ["scene", "scene"]


def test_script_importer_does_not_split_skill_check_prose_into_modules():
    result = convert_script_to_scenario(
        "背景\n开场说明\n\n<6号车厢>\n《侦查》成功：发现一张纸条\n《侦查》失败：什么也没发现\n\n<7号车厢>\n《敏捷》对抗成功，逃离敌人\n\nTrue End\n成功脱出\n\nBAD END\n死亡"
    )
    assert [item["title"] for item in result["modules"]] == ["背景", "6号车厢", "7号车厢", "True End", "BAD END"]
    assert result["modules"][1]["content"].count("侦查") == 2
    assert result["modules"][2]["content"].count("敏捷") == 1


def test_room_scenario_context_returns_module_summaries(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)

    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "scenario_id": 77, "members": []}),
        encoding="utf-8",
    )
    (scenarios_dir / "module.json").write_text(
        json.dumps(
            {
                "id": 77,
                "title": "module scenario",
                "modules": [
                    {"id": "background-1", "module_type": "background", "title": "Background", "summary": "bg", "content": "background full"},
                    {"id": "scene-1", "module_type": "scene", "title": "Scene 1", "summary": "hall", "content": "hall full"},
                ],
            }
        ),
        encoding="utf-8",
    )

    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)
    result = get_room_scenario_context({"query": "hall"}, context)

    assert result["scenario"]["module_count"] == 2
    assert result["matches"][0]["module_type"] == "scene"
    assert result["matches"][0]["summary"] == "hall"


def test_room_scenario_module_loads_full_module_content(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)

    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "scenario_id": 77, "members": []}),
        encoding="utf-8",
    )
    (scenarios_dir / "module.json").write_text(
        json.dumps(
            {
                "id": 77,
                "title": "module scenario",
                "modules": [
                    {"id": "scene-1", "module_type": "scene", "title": "Scene 1", "summary": "hall", "content": "hall full"},
                    {"id": "npc-1", "module_type": "npc", "title": "NPC", "summary": "guard", "content": "guard full"},
                ],
            }
        ),
        encoding="utf-8",
    )

    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)
    result = get_room_scenario_module({"module_id": "npc-1"}, context)

    assert result["module"]["id"] == "npc-1"
    assert result["module"]["content"] == "guard full"


def test_trigger_keeps_display_name_and_condition_separate_from_keyword():
    trigger = normalize_trigger(
        {
            "id": 2,
            "display_name": "note content",
            "keyword": "reveal_note",
            "condition": "successful check",
            "content": "You find a note.",
        }
    )

    assert trigger == {
        "id": 2,
        "display_name": "note content",
        "keyword": "reveal_note",
        "condition": "successful check",
        "content_mode": "text",
        "content": "You find a note.",
    }
    message = build_trigger_message(
        {"modules": [{"module_type": "scene", "id": "scene-1", "triggers": [trigger]}]},
        2,
    )
    assert message["sender_name"] == "note content"
    assert message["sender_name"] != trigger["keyword"]


def test_trigger_without_display_name_uses_generic_sender_not_keyword():
    message = build_trigger_message(
        {"modules": [{"module_type": "scene", "id": "scene-1", "triggers": [{"id": 1, "keyword": "secret", "content": "content"}]}]},
        1,
    )

    assert message["sender_name"] == "触发器1"
    assert message["sender_name"] != "secret"


def test_old_module_titles_are_migrated_to_type_numbered_titles():
    normalized = normalize_scenario_payload(
        {
            "id": 1,
            "modules": [
                {"id": "a", "module_type": "scene", "title": "模块 1"},
                {"id": "b", "module_type": "scene", "title": "模块2"},
                {"id": "c", "module_type": "ending", "title": "结局"},
                {"id": "d", "module_type": "custom", "title": "自定义模块 1"},
            ],
        }
    )

    assert [module["title"] for module in normalized["modules"]] == ["场景1", "场景2", "结局1", "自定义模块1"]


def test_legacy_scenario_without_author_or_player_count_is_normalized():
    """历史半成品（只有 title 与空 modules）补齐基础字段，否则广场会把它过滤掉。"""
    normalized = normalize_scenario_payload({"id": 1, "title": "T", "modules": [], "import_mode": "direct"})

    assert normalized["title"] == "T"
    assert normalized["author"] == "未知作者"
    assert normalized["playerCount"] == 1
    assert normalized["modules"] == []


def test_conditional_trigger_requires_a_verified_check_and_matching_decision(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    scenario = {
        "id": 7,
        "modules": [{
            "id": "scene-1",
            "module_type": "scene",
            "triggers": [{
                "id": 2,
                "display_name": "纸条内容",
                "keyword": "reveal_note",
                "condition": "检定侦察成功",
                "content": "你发现了一条纸条。",
            }],
        }],
    }
    (scenarios_dir / "scenario-record.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"scenario_id": 7}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    missing_check = reveal_scenario_trigger({"trigger_id": 2, "condition_met": True}, context)
    assert "preceding dice/check" in missing_check["error"]

    context.tool_state["last_check"] = {"name": "侦察", "success": False}
    rejected = reveal_scenario_trigger({"trigger_id": 2, "condition_met": True, "check_name": "侦察"}, context)
    assert "does not match" in rejected["error"]

    context.tool_state["last_check"] = {"name": "侦察", "success": True}
    revealed = reveal_scenario_trigger({"trigger_id": 2, "condition_met": True, "check_name": "侦察"}, context)
    assert revealed["triggered"] is True
    assert revealed["direct_message"]["sender_name"] == "纸条内容"


def test_conditional_trigger_does_not_reveal_content_when_condition_fails(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "scenario-record.json").write_text(
        json.dumps({
            "id": 7,
            "modules": [{
                "id": "scene-1",
                "module_type": "scene",
                "triggers": [{"id": 1, "keyword": "secret", "condition": "玩家同意", "content": "secret"}],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"scenario_id": 7}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    result = reveal_scenario_trigger({"trigger_id": 1, "condition_met": False}, context)

    assert result["triggered"] is False
    assert "direct_message" not in result
    assert "secret" not in json.dumps(result, ensure_ascii=False)
