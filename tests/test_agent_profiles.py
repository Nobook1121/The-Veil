from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.profiles import DEFAULT_KP_TOOLS, resolve_agent_profile


def test_kp_profile_gets_default_tools_when_role_has_none(tmp_path):
    prompt_file = tmp_path / "kp.md"
    prompt_file.write_text("你是KP。", encoding="utf-8")
    role = {
        "id": "kp",
        "name": "KP",
        "prompt": "你是KP。",
        "provider": "openrouter",
        "wake_words": ["@KP"],
    }

    profile = resolve_agent_profile(role, prompt_file=prompt_file)

    assert profile.id == "kp"
    assert profile.name == "KP"
    assert profile.provider == "openrouter"
    assert profile.prompt == "你是KP。"
    assert profile.tool_names == DEFAULT_KP_TOOLS
    assert "room.get_room_snapshot" in profile.tool_names
    assert profile.wake_words == ["@KP"]


def test_kp_profile_can_prefer_current_prompt_file(tmp_path):
    prompt_file = tmp_path / "kp.md"
    prompt_file.write_text("current kp rules", encoding="utf-8")

    profile = resolve_agent_profile(
        {"id": "kp", "name": "KP", "prompt": "stale embedded rules"},
        prompt_file=prompt_file,
        prefer_prompt_file=True,
    )

    assert profile.prompt == "current kp rules"


def test_non_kp_profile_defaults_to_no_tools(tmp_path):
    prompt_file = tmp_path / "kp.md"
    prompt_file.write_text("你是KP。", encoding="utf-8")
    profile = resolve_agent_profile(
        {
            "id": "narrator",
            "name": "Narrator",
            "prompt": "旁白。",
            "provider": "lmstudio",
        },
        prompt_file=prompt_file,
    )

    assert profile.id == "narrator"
    assert profile.tool_names == []


def test_role_can_explicitly_select_tools(tmp_path):
    prompt_file = tmp_path / "kp.md"
    prompt_file.write_text("你是KP。", encoding="utf-8")
    profile = resolve_agent_profile(
        {
            "id": "rules",
            "name": "Rules Helper",
            "prompt": "规则助手。",
            "tools": ["dice.roll_coc_check"],
            "context": ["room.recent_messages"],
        },
        prompt_file=prompt_file,
    )

    assert profile.tool_names == ["dice.roll_coc_check"]
    assert profile.context_providers == ["room.recent_messages"]


def test_ai_runtime_config_reads_stream_flag(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text("[ai]\nstream_output = true\n", encoding="utf-8")

    config = load_ai_runtime_config(config_dir)

    assert config.stream_output is True


def test_ai_runtime_config_reads_debug_mode(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text("[ai]\ndebug_mode = true\n", encoding="utf-8")

    config = load_ai_runtime_config(config_dir)

    assert config.debug_mode is True


def test_ai_runtime_config_reads_max_tokens_constraint(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        "[ai]\nconstrain_max_tokens = true\nmax_tokens = 2048\n", encoding="utf-8"
    )

    config = load_ai_runtime_config(config_dir)

    # 默认开启约束（模板未声明时按全局值下发），显式配置后按配置生效。
    assert config.constrain_max_tokens is True
    assert config.max_tokens == 2048
    assert load_ai_runtime_config(tmp_path / "missing").constrain_max_tokens is True


def test_build_kp_tool_names_dedupes_overlapping_ruleset_and_plugin_tools(monkeypatch):
    """规则集与插件 agent_tools 声明同名工具时只能下发一次。

    线上表现是平台直接 400「Tool names must be unique」（dnd5e.start_encounter
    同时出现在 DND5E_TOOL_NAMES 与 plugin.json 的 agent_tools 列表）。
    """
    from trpg_server.agents import profiles

    monkeypatch.setattr(profiles, "ruleset_tool_names", lambda _: ["dnd5e.start_encounter", "dnd5e.roll_check"])
    monkeypatch.setattr(profiles, "plugin_tool_names", lambda _: ["dnd5e.start_encounter", "dnd5e.formula_eval"])
    monkeypatch.setattr(profiles, "combat_budget_tool_names", lambda _: [])
    monkeypatch.setattr(profiles, "spell_tool_names", lambda _: [])
    monkeypatch.setattr(profiles, "effect_tool_names", lambda _: [])
    monkeypatch.setattr(profiles, "rest_tool_names", lambda _: [])
    monkeypatch.setattr(profiles, "workflow_tool_names", lambda _: [])
    monkeypatch.setattr(profiles, "ruleset_kp_tool_policy", lambda _: (frozenset(), ()))

    names = profiles.build_kp_tool_names("dnd5e")

    assert names.count("dnd5e.start_encounter") == 1
    assert len(names) == len(set(names))
    assert "dnd5e.formula_eval" in names
