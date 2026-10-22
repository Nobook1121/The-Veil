"""分层加载优先级：代码默认值 < 包内模板 < 用户配置 < 环境变量 < CLI。

模板（L2）的 ``ai_request_timeout = 1800``、``stream_output = true``、
``theme = "pattern_cyber_2"`` 与代码默认值（300/False/"light"）有意不同，
用于区分「取到了哪一层」。
"""

from trpg_server.config.merge import load_settings


def _write_user(config_dir, text):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text(text, encoding="utf-8")


def test_template_applies_when_user_file_missing(tmp_path):
    settings = load_settings(tmp_path, env={})
    assert settings.ai.ai_request_timeout == 1800  # 模板值，非代码默认 300
    assert settings.ai.stream_output is True
    assert settings.section("appearance")["theme"] == "pattern_cyber_2"


def test_code_default_when_template_and_user_both_missing(tmp_path):
    # skill_bases 按 schema 约定不进模板，用户也未写 → 代码默认 ""。
    settings = load_settings(tmp_path, env={})
    assert settings.section("character_rules")["skill_bases"] == ""


def test_user_overrides_template(tmp_path):
    _write_user(tmp_path, "[ai]\nai_request_timeout = 600\n")
    settings = load_settings(tmp_path, env={})
    assert settings.ai.ai_request_timeout == 600
    assert settings.ai.stream_output is True  # 未覆盖的键仍取模板值


def test_env_overrides_user_and_locks_key(tmp_path):
    _write_user(tmp_path, "[ai]\nai_request_timeout = 600\n")
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_AI_AI_REQUEST_TIMEOUT": "900"})
    assert settings.ai.ai_request_timeout == 900
    assert settings.locked["ai.ai_request_timeout"] == "env"


def test_cli_overrides_env(tmp_path):
    settings = load_settings(
        tmp_path,
        env={"AI_TRPG_CFG_AI_AI_REQUEST_TIMEOUT": "900"},
        cli_overrides={"ai": {"ai_request_timeout": 1200}},
    )
    assert settings.ai.ai_request_timeout == 1200
    assert settings.locked["ai.ai_request_timeout"] == "cli"


def test_invalid_env_value_falls_back_to_user_layer(tmp_path):
    _write_user(tmp_path, "[ai]\nai_request_timeout = 600\n")
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_AI_AI_REQUEST_TIMEOUT": "garbage"})
    assert settings.ai.ai_request_timeout == 600


def test_out_of_range_value_is_clamped(tmp_path):
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_AI_AI_REQUEST_TIMEOUT": "999999"})
    assert settings.ai.ai_request_timeout == 1800


def test_invalid_enum_in_user_falls_back_to_template(tmp_path):
    _write_user(tmp_path, '[ai]\ndice_animation_level = "hologram"\n')
    settings = load_settings(tmp_path, env={})
    assert settings.ai.dice_animation_level == "3d"


def test_bool_string_parsing_for_env(tmp_path):
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_AI_STREAM_OUTPUT": "true"})
    assert settings.ai.stream_output is True


def test_alias_keys_merge_to_canonical(tmp_path):
    _write_user(tmp_path, "[ai]\nshow_kp_hints = false\n")
    settings = load_settings(tmp_path, env={})
    assert settings.ai.show_ai_hints is False
    assert "show_kp_hints" not in settings.section("ai")


def test_small_model_task_from_env(tmp_path):
    settings = load_settings(
        tmp_path, env={"AI_TRPG_CFG_AI_SMALL_MODELS_INTENT_CLASSIFICATION": "gpt-mini"})
    assert settings.ai.small_model_tasks["intent_classification"] == "gpt-mini"


def test_unknown_env_var_ignored(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="trpg_server.config.merge"):
        settings = load_settings(tmp_path, env={"AI_TRPG_CFG_NOPE_KEY": "1"})
    assert settings.ai.ai_request_timeout == 1800  # 未受影响
    assert any("AI_TRPG_CFG_NOPE_KEY" in rec.message for rec in caplog.records)


def test_settings_cached_until_layer_changes(tmp_path):
    _write_user(tmp_path, "[ai]\nai_request_timeout = 600\n")
    first = load_settings(tmp_path, env={})
    assert first is load_settings(tmp_path, env={})  # 同键命中缓存
    _write_user(tmp_path, "[ai]\nai_request_timeout = 601\n")
    again = load_settings(tmp_path, env={})
    assert again.ai.ai_request_timeout == 601
