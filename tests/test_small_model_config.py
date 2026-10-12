import json
from pathlib import Path

from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.role_config import provider_small_model_config, resolve_task_model
from trpg_server.routes.config import convert_to_toml


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

    assert load_ai_runtime_config(config_dir).max_tool_rounds == 8
    assert load_ai_runtime_config(config_dir).ai_request_timeout == 300

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


def test_convert_to_toml_emits_dotted_sections_without_quotes():
    content = convert_to_toml(
        {
            "ai": {"stream_output": True},
            "ai.small_models": {"summarization": "qwen-turbo"},
        }
    )

    assert "[ai]" in content
    assert "[ai.small_models]" in content
    assert '["ai.small_models"]' not in content


def test_convert_to_toml_collapses_legacy_escaped_section_names():
    # 模拟历史脏数据：段名被反复加引号 / 转义
    dirty = "ai.small_models"
    for _ in range(4):
        dirty = json.dumps(dirty, ensure_ascii=False)

    content = convert_to_toml({dirty: {"summarization": "qwen-turbo"}})

    assert "[ai.small_models]" in content
    assert "\\\\" not in content


def test_general_config_has_no_escaped_section_names():
    content = Path("data/config/general.toml").read_text(encoding="utf-8")

    assert "[ai.small_models]" in content
    assert "[ai.prompt]" in content
    assert '\\"' not in content


def test_shipped_general_config_exposes_knowledge_settings():
    config = load_ai_runtime_config(Path("data/config/general.toml").parent)

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


def test_openai_provider_template_is_openai_compatible():
    config = json.loads(open("data/config/aiplatform/openai.json", encoding="utf-8").read())
    assert config["enabled"] is False
    assert config["config"]["base_url"].endswith("/chat/completions")
    assert config["small_models"]["summarization"]["id"] == "gpt-4o-mini"


def test_aliyun_provider_includes_enabled_economy_model_and_embedding_endpoint():
    config = json.loads(open("data/config/aiplatform/aliyun.json", encoding="utf-8").read())
    assert any(model["id"] == "qwen-turbo" and model["enabled"] for model in config["models"])
    assert config["small_models"]["summarization"]["id"] == "qwen-turbo"
    assert config["embedding"]["base_url"].endswith("/embeddings")
