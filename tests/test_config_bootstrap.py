"""首次启动种子复制：幂等、不覆盖已有文件、模板缺失时按 schema 渲染兜底。"""

import tomlkit

from trpg_server.config.bootstrap import ensure_user_config, render_minimal_general
from trpg_server.config.merge import load_settings
from trpg_server.config.paths import template_general_path

SEED_NAMES = {"general.toml", "roles/kp.md", "roles/debug-kp.md", "aiplatform/default-request.json"}


def test_seeds_all_files_on_first_run(tmp_path):
    created = ensure_user_config(tmp_path)
    assert {p.relative_to(tmp_path).as_posix() for p in created} == SEED_NAMES
    for name in SEED_NAMES:
        assert (tmp_path / name).exists()


def test_idempotent_and_never_overwrites(tmp_path):
    ensure_user_config(tmp_path)
    user_file = tmp_path / "general.toml"
    user_file.write_text("[ai]\nai_request_timeout = 777\n", encoding="utf-8")
    assert ensure_user_config(tmp_path) == []
    assert "777" in user_file.read_text(encoding="utf-8")


def test_seeded_general_matches_template(tmp_path):
    ensure_user_config(tmp_path)
    assert (tmp_path / "general.toml").read_text(encoding="utf-8") == \
        template_general_path().read_text(encoding="utf-8")


def test_missing_template_renders_minimal_from_schema(tmp_path):
    empty_templates = tmp_path / "empty-templates"
    empty_templates.mkdir()
    target_dir = tmp_path / "cfg"
    created = ensure_user_config(target_dir, template_dir=empty_templates)
    # 模板缺失时只有 general.toml 有 schema 兜底渲染，其余种子文件跳过。
    assert [p.name for p in created] == ["general.toml"]
    doc = tomlkit.parse((target_dir / "general.toml").read_text(encoding="utf-8"))
    assert doc["ai"]["ai_request_timeout"] == 300  # 代码默认值，非模板 1800
    assert doc["ai"]["small_models"]["intent_classification"] == ""
    assert doc["site_agent"]["action_ttl_seconds"] == 300
    assert "skill_bases" not in doc["character_rules"]  # 空字符串默认不渲染


def test_render_minimal_general_roundtrip_loadable(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "general.toml").write_text(render_minimal_general(), encoding="utf-8")
    settings = load_settings(cfg, env={})
    assert settings.ai.ai_request_timeout == 300  # 用户层渲染值覆盖模板 1800
    assert settings.ai.small_model_tasks["summarization"] == ""
