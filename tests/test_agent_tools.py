from trpg_server.agents.profiles import DEFAULT_KP_TOOLS
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.dice import (
    execute_roll_coc_check,
    execute_roll_dice,
    execute_roll_room_check,
    roll_coc_check,
    roll_room_check,
)

import json

from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.memory import read_room_memory, remember_room_fact
from trpg_server.agents.tools.room import (
    get_room_character_cards,
    get_room_scenario_context,
    get_room_scenario_module,
    get_room_snapshot,
)
from trpg_server.agents.tools.knowledge import search_ruleset_knowledge
from trpg_server.agents.tools.suggest import suggest_actions


def test_coc_check_regular_success_with_fixed_rng():
    result = roll_coc_check(
        {
            "skill": "锁匠",
            "target": 45,
            "difficulty": "regular",
            "reason": "打开地下室门锁",
        },
        rng=lambda sides: 34,
    )

    assert result["roll"] == 34
    assert result["target"] == 45
    assert result["threshold"] == 45
    assert result["success"] is True
    assert result["success_level"] == "regular"
    assert "锁匠" in result["summary"]


def test_coc_check_hard_failure_uses_half_threshold():
    result = roll_coc_check(
        {"skill": "侦查", "target": 50, "difficulty": "hard"},
        rng=lambda sides: 31,
    )

    assert result["threshold"] == 25
    assert result["success"] is False
    assert result["success_level"] == "failure"


def test_coc_check_extreme_success_uses_fifth_threshold():
    result = roll_coc_check(
        {"skill": "图书馆使用", "target": 80, "difficulty": "extreme"},
        rng=lambda sides: 16,
    )

    assert result["threshold"] == 16
    assert result["success"] is True
    assert result["success_level"] == "extreme"


def test_coc_check_rejects_invalid_target():
    result = roll_coc_check({"skill": "锁匠", "target": 0}, rng=lambda sides: 1)

    assert result["error"] == "target must be between 1 and 100"


def _context_with_check_member(tmp_path, member, house_rules=None, config_dir=None):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    info = {"id": "room-1", "members": [member]}
    if house_rules is not None:
        info["house_rules"] = house_rules
    (room_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False),
        encoding="utf-8",
    )
    return AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp", config_dir=config_dir)


def _write_admin_dice_config(tmp_path, critical, fumble):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text(
        f"[ai]\ndice_critical_threshold = {critical}\ndice_fumble_threshold = {fumble}\n",
        encoding="utf-8",
    )
    return config_dir


def test_room_check_reads_skill_from_bound_character_by_username(tmp_path):
    skill_name = "\u4fa6\u5bdf"
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "skills": [{"name": skill_name, "value": 60}],
            },
        },
    )

    result = roll_room_check({"player_name": "testplayer", "name": skill_name}, context, rng=lambda sides: 30)

    assert result["roll"] == 30
    assert result["target"] == 60
    # threshold 与 target 完全重复，返回值里已去掉，避免回传模型时白占 token。
    assert "threshold" not in result
    assert result["success"] is True
    assert result["summary"] == "\u4fa6\u5bdf d%: [30] = 30 / 60 \u6210\u529f"
    assert result["visible_message"]["type"] == "dice"
    assert result["visible_message"]["content"] == result["summary"]


def test_room_check_applies_difficulty_and_adjustment(tmp_path):
    skill_name = "\u4fa6\u5bdf"
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "skills": [{"name": skill_name, "value": 60}],
            },
        },
    )

    result = roll_room_check(
        {"player_name": "testplayer", "name": skill_name, "difficulty": "\u56f0\u96be", "adjustment": "+10"},
        context,
        rng=lambda sides: 40,
    )

    assert result["base_target"] == 60
    assert result["target"] == 40
    assert result["success"] is True
    assert result["summary"] == "\u56f0\u96be\u4fa6\u5bdf d%: [40] = 40 / 40 \u6210\u529f"


def test_room_check_reads_attribute_alias_from_bound_character(tmp_path):
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "attributes": {"DEX": 55},
                "skills": [],
            },
        },
    )

    result = roll_room_check(
        {"player_name": "testplayer", "name": "\u654f\u6377", "difficulty": "\u6781\u96be"},
        context,
        rng=lambda sides: 12,
    )

    assert result["base_target"] == 55
    assert result["target"] == 11
    assert result["success"] is False
    assert result["summary"] == "\u6781\u96be\u654f\u6377 d%: [12] = 12 / 11 \u5931\u8d25"


def test_room_check_uses_room_house_rule_thresholds(tmp_path):
    skill_name = "\u4fa6\u5bdf"
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "skills": [{"name": skill_name, "value": 60}],
            },
        },
        house_rules={"dice_critical_threshold": 5, "dice_fumble_threshold": 70},
    )

    fumble = roll_room_check({"player_name": "testplayer", "name": skill_name}, context, rng=lambda sides: 80)
    assert fumble["fumble"] is True
    assert fumble["fumble_threshold"] == 70
    assert "\uff08\u5927\u5931\u8d25\uff09" in fumble["summary"]

    critical = roll_room_check({"player_name": "testplayer", "name": skill_name}, context, rng=lambda sides: 3)
    assert critical["critical"] is True
    assert critical["critical_threshold"] == 5


def test_room_check_falls_back_to_admin_default_thresholds(tmp_path):
    skill_name = "\u4fa6\u5bdf"
    config_dir = _write_admin_dice_config(tmp_path, 4, 90)
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "skills": [{"name": skill_name, "value": 60}],
            },
        },
        house_rules={"dice_critical_threshold": None, "dice_fumble_threshold": None},
        config_dir=config_dir,
    )

    fumble = roll_room_check({"player_name": "testplayer", "name": skill_name}, context, rng=lambda sides: 92)
    assert fumble["fumble"] is True
    assert fumble["fumble_threshold"] == 90

    critical = roll_room_check({"player_name": "testplayer", "name": skill_name}, context, rng=lambda sides: 4)
    assert critical["critical"] is True
    assert critical["critical_threshold"] == 4


def test_room_check_arguments_override_configured_thresholds(tmp_path):
    skill_name = "\u4fa6\u5bdf"
    context = _context_with_check_member(
        tmp_path,
        {
            "username": "testplayer",
            "status": "active",
            "character_card": {
                "name": "\u8c03\u67e5\u5458",
                "skills": [{"name": skill_name, "value": 60}],
            },
        },
        house_rules={"dice_critical_threshold": 5, "dice_fumble_threshold": 70},
    )

    result = roll_room_check(
        {"player_name": "testplayer", "name": skill_name, "fumble_threshold": 96},
        context,
        rng=lambda sides: 80,
    )

    assert result["fumble"] is False
    assert result["fumble_threshold"] == 96


def test_coc_check_tool_uses_room_house_rule_thresholds(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": "room-1",
                "house_rules": {"dice_critical_threshold": 5, "dice_fumble_threshold": 70},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")

    result = execute_roll_coc_check({"skill": "\u4fa6\u5bdf", "target": 60}, context)

    assert result["critical_threshold"] == 5
    assert result["fumble_threshold"] == 70


def test_coc_check_tool_uses_admin_default_thresholds(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "members": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    config_dir = _write_admin_dice_config(tmp_path, 7, 88)
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp", config_dir=config_dir)

    result = execute_roll_coc_check({"skill": "\u4fa6\u5bdf", "target": 60}, context)

    assert result["critical_threshold"] == 7
    assert result["fumble_threshold"] == 88


def _context_with_check_trigger(tmp_path):
    """房间成员 + 一个「侦查检定成功」才揭示的场景栏位触发器。"""
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "10.json").write_text(
        json.dumps(
            {
                "id": 10,
                "title": "旧宅",
                "scenes": [
                    {
                        "id": 1,
                        "title": "门厅",
                        "triggers": [
                            {
                                "id": 3,
                                "display_name": "墙缝里的钥匙",
                                "keyword": "钥匙",
                                "condition": "侦查检定成功",
                                "content": "你在墙缝里摸到一枚铜钥匙。",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": "room-1",
                "scenario_id": 10,
                "members": [
                    {
                        "username": "testplayer",
                        "status": "active",
                        "character_card": {"name": "调查员", "skills": [{"name": "侦查", "value": 60}]},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir, agent_id="kp")


def test_room_check_reveals_bound_trigger_in_same_round_on_success(tmp_path, monkeypatch):
    """检定成功时，随检定传入的 trigger_id 应在同一轮内揭示触发器，省掉中间 AI 往返。"""
    context = _context_with_check_trigger(tmp_path)
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 30)

    result = execute_roll_room_check({"player_name": "testplayer", "name": "侦查", "trigger_id": 3}, context)

    assert result["success"] is True
    assert result["direct_message"]["type"] == "trigger"
    assert "铜钥匙" in result["direct_message"]["content"]


def test_room_check_withholds_bound_trigger_when_check_fails(tmp_path, monkeypatch):
    """检定失败时不得揭示触发器内容，只回一条说明，符合 kp.md 的硬要求。"""
    context = _context_with_check_trigger(tmp_path)
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 90)

    result = execute_roll_room_check({"player_name": "testplayer", "name": "侦查", "trigger_id": 3}, context)

    assert result["success"] is False
    assert "direct_message" not in result
    assert result["trigger_note"]


def test_room_check_without_trigger_id_keeps_original_behaviour(tmp_path, monkeypatch):
    context = _context_with_check_trigger(tmp_path)
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 30)

    result = execute_roll_room_check({"player_name": "testplayer", "name": "侦查"}, context)

    assert "direct_message" not in result
    assert "trigger_note" not in result


def _enable_room_log(monkeypatch):
    """用例不受本机 general.toml 的运行时开关影响。"""
    from trpg_server.room_log import DEFAULT_ROOM_CONSOLE_SETTINGS

    monkeypatch.setattr(
        "trpg_server.room_log.room_console_settings",
        lambda config_dir=None: dict(DEFAULT_ROOM_CONSOLE_SETTINGS),
    )


def test_room_check_result_is_written_to_room_log(tmp_path, monkeypatch):
    """房间检定结果必须进入房间日志，供房间控制台查看。"""
    _enable_room_log(monkeypatch)
    context = _context_with_check_trigger(tmp_path)
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 30)

    execute_roll_room_check({"player_name": "testplayer", "name": "侦查"}, context)

    from trpg_server.room_log import read_room_log

    entries = read_room_log(context.room_dir)["entries"]
    dice_entries = [entry for entry in entries if entry["source"] == "dice"]
    assert len(dice_entries) == 1
    assert "侦查" in dice_entries[0]["message"]
    assert dice_entries[0]["actor"] == "testplayer"
    assert dice_entries[0]["details"]["工具"] == "check.roll_room_check"


def test_generic_dice_roll_result_is_written_to_room_log(tmp_path, monkeypatch):
    _enable_room_log(monkeypatch)
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"id": "room-1"}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 4)

    execute_roll_dice({"expression": "1d6"}, context)

    from trpg_server.room_log import read_room_log

    entries = read_room_log(room_dir)["entries"]
    assert entries[-1]["source"] == "dice"
    assert "投掷 1d6" in entries[-1]["message"]
    assert entries[-1]["details"]["工具"] == "dice.roll"


def test_dark_dice_roll_is_logged_with_secret_flag(tmp_path, monkeypatch):
    _enable_room_log(monkeypatch)
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"id": "room-1"}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")
    monkeypatch.setattr("trpg_server.agents.tools.dice.random.randint", lambda low, high: 7)

    execute_roll_dice({"expression": "1d100", "dark": True}, context)

    from trpg_server.room_log import read_room_log

    entries = read_room_log(room_dir)["entries"]
    assert entries[-1]["details"]["暗投"] == "是"


def test_dice_roll_error_is_not_written_to_room_log(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"id": "room-1"}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")

    execute_roll_dice({"expression": "bad"}, context)

    from trpg_server.room_log import read_room_log

    assert read_room_log(room_dir)["entries"] == []


def test_room_check_returns_error_for_missing_bound_character(tmp_path):
    context = _context_with_check_member(tmp_path, {"username": "testplayer", "status": "active"})

    result = roll_room_check({"player_name": "testplayer", "name": "\u4fa6\u5bdf"}, context, rng=lambda sides: 1)

    assert result["error"] == "player testplayer has no bound character card"


def test_kp_default_tools_include_room_check_function():
    registry = default_tool_registry()

    assert "check.roll_room_check" in DEFAULT_KP_TOOLS
    assert registry.get("check.roll_room_check") is not None


def test_kp_default_tools_include_suggest_actions_function():
    registry = default_tool_registry()

    assert "room.suggest_actions" in DEFAULT_KP_TOOLS
    assert registry.get("room.suggest_actions") is not None


def test_default_tool_schemas_use_provider_safe_function_names():
    """DeepSeek/OpenAI 要求 function name 匹配 ^[a-zA-Z0-9_-]+$；内部点号命名空间
    （如 room.get_room_snapshot）必须在对外 schema 里被规整化，否则请求会被平台 400 拒绝。"""
    import re

    registry = default_tool_registry()
    for name in DEFAULT_KP_TOOLS:
        tool = registry.get(name)
        assert tool is not None
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", tool.schema()["function"]["name"])


def test_suggest_actions_blocked_when_gate_disabled(tmp_path):
    context = AgentRequestContext(
        room_id="room-1",
        room_dir=tmp_path,
        tool_state={"allow_action_suggestions": False},
    )

    result = suggest_actions({"actions": ["\u56db\u5904\u8d70\u52a8"]}, context)

    assert "error" in result
    assert "suggested_actions" not in context.tool_state


def test_suggest_actions_cleans_and_stages_when_enabled(tmp_path):
    context = AgentRequestContext(
        room_id="room-1",
        room_dir=tmp_path,
        user_id=7,
        tool_state={"allow_action_suggestions": True},
    )

    result = suggest_actions(
        {"actions": ["\u56db\u5904\u8d70\u52a8", " \u56db\u5904\u8d70\u52a8 ", "", "\u770b\u770b\u5468\u56f4", "x" * 200, "a", "b"]},
        context,
    )

    assert result["status"] == "displayed_to_players"
    assert context.tool_state["suggested_actions"] == [
        "\u56db\u5904\u8d70\u52a8",
        "\u770b\u770b\u5468\u56f4",
        "x" * 80,
        "a",
        "b",
    ]
    assert context.tool_state["suggested_actions_owner"] == 7


def test_suggest_actions_falls_back_to_room_house_rule_without_chat_gate(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "house_rules": {"action_suggestions_enabled": True}}),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir)

    result = suggest_actions({"actions": ["\u56db\u5904\u8d70\u52a8"]}, context)

    assert result["status"] == "displayed_to_players"


def test_ruleset_knowledge_tool_limits_context_and_reports_usage(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")
    context.tool_state["ruleset_search"] = lambda query, top_k: [
        {
            "ruleset_id": "coc7",
            "knowledge_version": "4",
            "chunk_id": f"chunk-{index}",
            "topic": "keeper_guidance",
            "citation": f"Keeper Rulebook p.{index}",
            "text": "死亡场景应尊重玩家体验。" * 200,
        }
        for index in range(1, 6)
    ]

    result = search_ruleset_knowledge({"query": "玩家角色死亡时如何处理", "top_k": 20}, context)

    assert result["used"] is True
    assert len(result["references"]) == 3
    assert all(len(item["text"]) <= 1200 for item in result["references"])
    assert result["knowledge_usage"] == {
        "ruleset_chunks": 3,
        "ruleset_sources": 3,
        "ruleset_ids": ["coc7"],
        "knowledge_versions": ["4"],
        "topics": ["keeper_guidance"],
        "citations": ["Keeper Rulebook p.1", "Keeper Rulebook p.2", "Keeper Rulebook p.3"],
    }


def test_frontend_registers_check_command():
    manager_source = open("frontend/src/tools/toolManager.ts", encoding="utf-8").read()
    chat_source = open("frontend/src/app/chat.ts", encoding="utf-8").read()

    assert '"/check"' in manager_source
    assert "handleCheckCommand" in manager_source
    assert "/check {*\u73a9\u5bb6\u540d} {*\u6280\u80fd/\u5c5e\u6027\u540d}" in chat_source


def test_kp_prompt_requires_room_check_function():
    prompt = open("config/templates/roles/kp.md", encoding="utf-8").read()

    assert "check.roll_room_check" in prompt
    assert "/check {*\u73a9\u5bb6\u540d} {*\u6280\u80fd/\u5c5e\u6027\u540d}" in prompt


def test_room_scenario_context_loads_current_room_scenario(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "scenario_id": 7, "members": []}),
        encoding="utf-8",
    )
    (scenarios_dir / "haunted.json").write_text(
        json.dumps(
            {
                "id": 7,
                "title": "雨夜来客",
                "description": "暴雨中的宅邸调查。",
                "scenes": [{"id": "hall", "title": "门厅", "description": "潮湿的地毯。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    result = get_room_scenario_context({"query": "门厅"}, context)

    assert result["scenario"]["title"] == "雨夜来客"
    assert result["matches"][0]["title"] == "门厅"


def test_room_scenario_module_returns_page_instead_of_unbounded_content(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "7.json").write_text(
        json.dumps({"id": 7, "modules": [{"id": "scene-long", "module_type": "scene", "scene_id": "1", "content": "x" * 12000}]}),
        encoding="utf-8",
    )
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(json.dumps({"id": "room-1", "scenario_id": 7}), encoding="utf-8")
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    result = get_room_scenario_module({"module_id": "scene-long", "offset": 5000, "max_chars": 2000}, context)

    assert len(result["module"]["content"]) == 2000
    assert result["module"]["content_offset"] == 5000
    assert result["module"]["content_length"] == 12000
    assert result["module"]["content_truncated"] is True
    assert result["module"]["next_offset"] == 7000


def test_room_character_cards_returns_active_bound_cards(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": "room-1",
                "members": [
                    {
                        "user_id": 1,
                        "username": "alice",
                        "status": "active",
                        "character_card": {
                            "id": "investigator",
                            "name": "林见山",
                            "background": {"story": "失踪记者。"},
                            "skills": [{"name": "锁匠", "value": 45}],
                        },
                        "character_state": {"current_hp": 10, "current_san": 55},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir)

    result = get_room_character_cards({}, context)

    assert result["members"][0]["username"] == "alice"
    assert result["members"][0]["character_card"]["name"] == "林见山"
    assert result["members"][0]["character_state"]["current_san"] == 55


def test_room_memory_write_and_read(tmp_path):
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, agent_id="kp")

    remembered = remember_room_fact(
        {"kind": "npc_state", "content": "图书管理员开始怀疑调查员。", "importance": 3},
        context,
    )
    memory = read_room_memory({"query": "图书管理员", "limit": 5}, context)

    assert remembered["stored"] is True
    assert memory["items"][0]["content"] == "图书管理员开始怀疑调查员。"


def test_room_snapshot_omits_full_character_card_and_scenario_details(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": "room-1",
                "name": "test room",
                "scenario_id": 7,
                "scenario_title": "Long Life Figurine",
                "members": [
                    {
                        "user_id": 9,
                        "username": "ADMIN",
                        "status": "active",
                        "is_active": True,
                        "character_card": {
                            "id": "investigator-1",
                            "name": "Wu Mingshan",
                            "attributes": {"DEX": 55},
                            "skills": [{"name": "Spot Hidden", "value": 70}],
                            "background": {"story": "Long character history"},
                        },
                        "character_state": {"current_hp": 15, "current_san": 50},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (scenarios_dir / "scenario.json").write_text(
        json.dumps(
            {
                "id": 7,
                "title": "Long Life Figurine",
                "background": "Large background should not be in snapshot",
                "scenes": [{"id": 1, "content": "Large scene body should not be in snapshot"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    result = get_room_snapshot({}, context)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["members"][0]["character"]["name"] == "Wu Mingshan"
    assert result["members"][0]["character_state"]["current_san"] == 50
    assert "skills" not in serialized
    assert "attributes" not in serialized
    assert "Spot Hidden" not in serialized
    assert "DEX" not in serialized
    assert "Large scene body should not be in snapshot" not in serialized
    assert "Large background should not be in snapshot" not in serialized


def test_room_snapshot_returns_bound_scenario_and_character_cards(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": "room-1",
                "name": "测试房间",
                "scenario_id": 1776085966397,
                "scenario_title": "长生俑",
                "members": [
                    {
                        "user_id": 9,
                        "username": "ADMIN",
                        "status": "active",
                        "is_active": True,
                        "character_card": {
                            "id": "investigator-1",
                            "name": "吴明山",
                            "background": {"story": "失踪记者。"},
                            "skills": [{"skillKey": "locksmith", "name": "锁匠", "value": 45}],
                        },
                        "character_state": {"current_hp": 15, "current_san": 50},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (scenarios_dir / "长生俑.json").write_text(
        json.dumps(
            {
                "id": 1776085966397,
                "title": "长生俑",
                "background": "秦俑与长生药的现代调查。",
                "scenes": [{"id": 1, "content": "西安高铁站。冯教授迎接调查员。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (room_dir / "agent_memory.json").write_text(
        json.dumps(
            [{"id": "memory-1", "kind": "fact", "content": "ADMIN 已经找到门厅暗格。", "importance": 4}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir)

    result = get_room_snapshot({}, context)

    assert result["room"]["id"] == "room-1"
    assert result["scenario"]["id"] == 1776085966397
    assert result["scenario"]["title"] == "长生俑"
    assert result["scenario"]["available_sections"]["scenes"] == 1
    assert result["members"][0]["character"]["name"] == "吴明山"
    assert result["members"][0]["character_state"]["current_san"] == 50
    assert result["memory"]["items"][0]["content"] == "ADMIN 已经找到门厅暗格。"
    assert "skills" not in json.dumps(result, ensure_ascii=False)
