from pathlib import Path

from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.role_config import provider_small_model_config, resolve_task_model


def test_runtime_config_reads_small_model_task_mapping(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        "[ai.small_models]\nintent_classification = \"qwen-turbo\"\n",
        encoding="utf-8",
    )

    config = load_ai_runtime_config(config_dir)

    assert config.small_model_tasks["intent_classification"] == "qwen-turbo"
    assert "summarization" in config.small_model_tasks


def test_runtime_config_reads_request_limits(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        "[ai]\nmax_tool_rounds = 12\nai_request_timeout = 600\n",
        encoding="utf-8",
    )

    config = load_ai_runtime_config(config_dir)

    assert config.max_tool_rounds == 12
    assert config.ai_request_timeout == 600


def test_runtime_config_clamps_and_defaults_request_limits(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # 无用户配置时模板层（L2）生效：随包模板即「默认配置」。
    assert load_ai_runtime_config(config_dir).max_tool_rounds == 8
    assert load_ai_runtime_config(config_dir).ai_request_timeout == 1800

    (config_dir / "general.toml").write_text(
        "[ai]\nmax_tool_rounds = 999\nai_request_timeout = 5\n",
        encoding="utf-8",
    )
    config = load_ai_runtime_config(config_dir)

    assert config.max_tool_rounds == 30
    assert config.ai_request_timeout == 30


def test_runtime_config_reads_knowledge_retrieval_settings(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    defaults = load_ai_runtime_config(config_dir)
    assert defaults.retrieval_top_k_default == 5
    assert defaults.retrieval_top_k_direct == 6
    assert defaults.chunk_child_max_chars == 800
    assert defaults.chunk_parent_max_chars == 2400
    assert defaults.sticky_rounds == 1
    assert defaults.cooldown_rounds == 3

    (config_dir / "general.toml").write_text(
        "[ai.knowledge]\ntop_k_default = 3\ntop_k_direct = 12\n"
        "chunk_child_max_chars = 600\nchunk_parent_max_chars = 3200\n"
        "sticky_rounds = 2\ncooldown_rounds = 5\n",
        encoding="utf-8",
    )
    config = load_ai_runtime_config(config_dir)

    assert config.retrieval_top_k_default == 3
    assert config.retrieval_top_k_direct == 12
    assert config.chunk_child_max_chars == 600
    assert config.chunk_parent_max_chars == 3200
    assert config.sticky_rounds == 2
    assert config.cooldown_rounds == 5


def test_runtime_config_clamps_knowledge_retrieval_settings(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        "[ai.knowledge]\ntop_k_default = 0\ntop_k_direct = 999\n"
        "chunk_child_max_chars = 10\nchunk_parent_max_chars = 999999\n"
        "sticky_rounds = 99\ncooldown_rounds = 99\n"
        "keyword_channel_enabled = false\nrecursive_scanning = false\n"
        "max_recursion_depth = 99\nlorebook_token_budget = -5\n",
        encoding="utf-8",
    )

    config = load_ai_runtime_config(config_dir)

    assert config.retrieval_top_k_default == 1
    assert config.retrieval_top_k_direct == 32
    assert config.chunk_child_max_chars == 200
    assert config.chunk_parent_max_chars == 16000
    assert config.sticky_rounds == 10
    assert config.cooldown_rounds == 20
    assert config.keyword_channel_enabled is False
    assert config.recursive_scanning is False
    assert config.max_recursion_depth == 10
    assert config.lorebook_token_budget == 0


def test_runtime_config_reads_quoted_dotted_sections(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        '["ai.small_models"]\nintent_classification = "qwen-turbo"\n'
        '["ai.prompt"]\nmax_tool_result_chars = 2500\n',
        encoding="utf-8",
    )

    config = load_ai_runtime_config(config_dir)

    assert config.small_model_tasks["intent_classification"] == "qwen-turbo"
    assert config.max_tool_result_chars == 2500


def test_config_field_specs_cover_every_runtime_field():
    """规格表是配置项的唯一接线点，漏登记会让字段永远读不到 general.toml。"""
    from dataclasses import fields

    from trpg_server.config.schema import AI_SPECS, AIRuntimeConfig

    spec_names = [spec.name for spec in AI_SPECS]

    assert len(spec_names) == len(set(spec_names))
    # small_model_tasks 是字典字段，由解析循环单独累积，不参与单值规格表。
    assert set(spec_names) == {item.name for item in fields(AIRuntimeConfig)} - {"small_model_tasks"}


def test_runtime_config_rejects_unknown_dice_animation_level(tmp_path, caplog):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text('[ai]\ndice_animation_level = "ultra"\n', encoding="utf-8")

    with caplog.at_level("WARNING"):
        config = load_ai_runtime_config(config_dir)

    assert config.dice_animation_level == "3d"
    assert "dice_animation_level" in caplog.text


def test_runtime_config_reads_and_clamps_dice_reroll_settings(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    defaults = load_ai_runtime_config(config_dir)
    assert defaults.dice_reroll_mode == "off"
    assert defaults.dice_reroll_limit == 3

    (config_dir / "general.toml").write_text(
        '[ai]\ndice_reroll_mode = "limited"\ndice_reroll_limit = 30\n',
        encoding="utf-8",
    )
    config = load_ai_runtime_config(config_dir)

    assert config.dice_reroll_mode == "limited"
    assert config.dice_reroll_limit == 20

    (config_dir / "general.toml").write_text(
        '[ai]\ndice_reroll_mode = "sometimes"\ndice_reroll_limit = 0\n',
        encoding="utf-8",
    )
    clamped = load_ai_runtime_config(config_dir)

    assert clamped.dice_reroll_mode == "off"
    assert clamped.dice_reroll_limit == 1


def test_write_ai_setting_preserves_other_sections_and_comments(tmp_path):
    from trpg_server.agents.config import write_ai_setting

    path = tmp_path / "general.toml"
    path.write_text(
        '# 文件头注释\n[ai]\ndice_animation_level = "3d"\n# 保留注释\n'
        "dice_player_roll = false\n\n"
        "[ai.knowledge]\ntop_k_default = 5\n",
        encoding="utf-8",
    )

    write_ai_setting("dice_reroll_mode", "limited", tmp_path)
    write_ai_setting("dice_reroll_limit", 7, tmp_path)
    write_ai_setting("dice_player_roll", True, tmp_path)

    content = path.read_text(encoding="utf-8")
    assert "# 文件头注释" in content
    assert "# 保留注释" in content
    assert '[ai.knowledge]\ntop_k_default = 5' in content
    assert 'dice_reroll_mode = "limited"' in content
    assert "dice_reroll_limit = 7" in content
    assert "dice_player_roll = true" in content
    assert not (tmp_path / "general.toml.tmp").exists()

    config = load_ai_runtime_config(tmp_path)
    assert config.dice_reroll_mode == "limited"
    assert config.dice_reroll_limit == 7
    assert config.dice_player_roll is True


def test_update_sections_collapses_legacy_quoted_dotted_sections(tmp_path):
    """历史脏数据里的引号段名（``["ai.small_models"]``）在写入时折叠为嵌套表，
    避免与新建的点分表并存导致文件非法。"""
    from trpg_server.config import update_sections

    path = tmp_path / "general.toml"
    path.write_text('["ai.small_models"]\nsummarization = "qwen-turbo"\n', encoding="utf-8")

    update_sections(path, {"ai.small_models": {"intent_classification": "qwen-plus"}})

    content = path.read_text(encoding="utf-8")
    assert '["ai.small_models"]' not in content
    config = load_ai_runtime_config(tmp_path)
    assert config.small_model_tasks["summarization"] == "qwen-turbo"
    assert config.small_model_tasks["intent_classification"] == "qwen-plus"


def test_general_config_has_no_escaped_section_names():
    content = Path("config/templates/general.toml").read_text(encoding="utf-8")

    assert "[ai.small_models]" in content
    assert "[ai.prompt]" in content
    assert '\\"' not in content


def test_runtime_config_reads_cloud_embedding_flag(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        "[ai]\ncloud_embedding_enabled = true\n",
        encoding="utf-8",
    )

    config = load_ai_runtime_config(config_dir)

    assert config.cloud_embedding_enabled is True


def test_shipped_general_config_disables_cloud_embedding_by_default(tmp_path):
    # 空目录 → 模板层生效，等价于首次部署用户拿到的随包配置。
    config = load_ai_runtime_config(tmp_path)

    assert config.cloud_embedding_enabled is False


def test_shipped_general_config_exposes_knowledge_settings(tmp_path):
    config = load_ai_runtime_config(tmp_path)

    assert config.retrieval_top_k_default == 5
    assert config.retrieval_top_k_direct == 6
    assert config.chunk_child_max_chars == 800
    assert config.chunk_parent_max_chars == 2400
    assert config.sticky_rounds == 1
    assert config.cooldown_rounds == 3
    assert config.keyword_channel_enabled is True
    assert config.recursive_scanning is True
    assert config.max_recursion_depth == 3
    assert config.lorebook_token_budget == 0


def test_frontend_parser_unquotes_toml_section_names():
    source = Path("frontend/src/app/config/ConfigManager.ts").read_text(encoding="utf-8")

    assert "normalizeTomlKey" in source
    assert "normalizeTomlKey(sectionMatch[1]" in source


def test_provider_small_model_config_prefers_task_model_and_falls_back():
    provider = {
        "models": [{"id": "primary", "enabled": True}],
        "small_models": {
            "summarization": {"id": "small-summary", "enabled": True},
        },
    }

    assert provider_small_model_config(provider, "summarization")["id"] == "small-summary"
    assert provider_small_model_config(provider, "state_update")["id"] == "primary"


def test_resolve_task_model_prefers_override_then_small_then_primary():
    """优先级：general.toml 显式覆盖 > 平台 small_models > 主模型。"""
    from trpg_server.agents.config import AIRuntimeConfig

    provider = {
        "models": [{"id": "primary", "enabled": True}],
        "small_models": {"summarization": {"id": "small-summary", "enabled": True}},
    }
    default_runtime = AIRuntimeConfig()
    # 未覆盖 → 用平台声明的小模型
    assert resolve_task_model(default_runtime, provider, "summarization", "primary") == "small-summary"
    # 平台未声明 → 回退主模型
    assert resolve_task_model(default_runtime, provider, "state_update", "primary") == "primary"
    # 管理员显式覆盖 → 覆盖两者
    overridden = AIRuntimeConfig(small_model_tasks={"summarization": "pinned-model", "state_update": ""})
    assert resolve_task_model(overridden, provider, "summarization", "primary") == "pinned-model"
    assert resolve_task_model(overridden, provider, "state_update", "primary") == "primary"
