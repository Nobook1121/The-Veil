"""Phase 3：CLI 解析、全局 CLI 覆盖与 config 控制台命令测试。

覆盖：``parse_cli_args`` 各形态、``paths.set_cli_overrides`` 全局注入、
``config_admin`` 迁移预览/执行/改名，以及 ``execute_console_command`` 的
config 域 dispatch（dry-run / --yes / 交互取消）。
"""

import pytest
from pathlib import Path

from trpg_server import config_admin
from trpg_server.config import load_settings
from trpg_server.config import paths as config_paths
from trpg_server.console_commands import execute_console_command


@pytest.fixture
def config_dir(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


@pytest.fixture
def override_config_dir(config_dir):
    """把全局配置目录指向临时目录，避免控制台命令碰到真实 data/config。"""
    config_paths.set_config_dir_override(config_dir)
    try:
        yield config_dir
    finally:
        config_paths.set_config_dir_override(None)


# ---------------------------------------------------------------------------
# parse_cli_args
# ---------------------------------------------------------------------------


def test_parse_cli_args_positional_port():
    args = config_paths.parse_cli_args(["server.py", "9000"])
    assert args.port == 9000
    assert args.config_dir is None
    assert args.overrides == {}


def test_parse_cli_args_flag_forms():
    args = config_paths.parse_cli_args(
        ["server.py", "--port", "9001", "--config-dir", "D:/cfg"]
    )
    assert args.port == 9001
    assert args.config_dir == Path("D:/cfg")

    args = config_paths.parse_cli_args(
        ["server.py", "--port=9002", "--config-dir=D:/cfg2"]
    )
    assert args.port == 9002
    assert args.config_dir == Path("D:/cfg2")


def test_parse_cli_args_flag_port_beats_positional():
    args = config_paths.parse_cli_args(["server.py", "9000", "--port", "9001"])
    assert args.port == 9001


def test_parse_cli_args_set_overrides():
    args = config_paths.parse_cli_args(
        [
            "server.py",
            "--set", "ai.max_tokens=2048",
            "--set=site.name=demo",
            "--set", "network.penetration.settings.auth_token=tok",
        ]
    )
    assert args.overrides == {
        "ai": {"max_tokens": "2048"},
        "site": {"name": "demo"},
        "network.penetration.settings": {"auth_token": "tok"},
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["server.py", "--unknown"],
        ["server.py", "9000", "9001"],
        ["server.py", "--port"],
        ["server.py", "--config-dir"],
        ["server.py", "--set"],
        ["server.py", "--set", "missing-equals"],
        ["server.py", "--set", "nokey.=1"],
        ["server.py", "--port=abc"],
    ],
)
def test_parse_cli_args_rejects_invalid_input(argv):
    with pytest.raises(ValueError):
        config_paths.parse_cli_args(argv)


# ---------------------------------------------------------------------------
# 全局 CLI 覆盖注入
# ---------------------------------------------------------------------------


def test_global_cli_overrides_apply_to_load_settings(config_dir):
    (config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")
    config_paths.set_cli_overrides({"ai": {"max_tokens": "1024"}})
    try:
        settings = load_settings(config_dir)
    finally:
        config_paths.set_cli_overrides(None)

    assert settings.ai.max_tokens == 1024
    assert settings.locked["ai.max_tokens"] == "cli"


def test_global_cli_overrides_clear_restores_file_values(config_dir):
    (config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")
    config_paths.set_cli_overrides({"ai": {"max_tokens": "1024"}})
    config_paths.set_cli_overrides(None)

    assert load_settings(config_dir).ai.max_tokens == 4096


# ---------------------------------------------------------------------------
# config_admin 迁移
# ---------------------------------------------------------------------------


def test_config_path_info_respects_injected_dir(config_dir):
    info = config_admin.config_path_info(config_dir)
    assert info["config_dir"] == str(config_dir.resolve())
    assert info["general_config"].endswith("general.toml")
    assert info["template_dir"].endswith("templates")


def test_format_settings_marks_env_locked_keys(config_dir, monkeypatch):
    (config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")
    monkeypatch.setenv("AI_TRPG_CFG_AI_MAX_TOKENS", "8192")

    output = config_admin.format_settings("ai", config_dir)

    assert "[ai]" in output
    assert "max_tokens = 8192" in output
    assert "(locked: env)" in output


def test_format_settings_unknown_section(config_dir):
    output = config_admin.format_settings("no.such.section", config_dir)
    assert "未知段" in output


def test_migration_preview_without_legacy_files(config_dir):
    assert config_admin.legacy_files_present(config_dir) == []
    assert "无需迁移" in config_admin.format_migration_preview(config_dir)


def test_migrate_legacy_config_keeps_effective_values(config_dir):
    (config_dir / "general.toml").write_text(
        '# 用户注释\n[site]\nname = "toml-name"\n', encoding="utf-8"
    )
    (config_dir / "site.json").write_text(
        '{"name": "json-name", "setup_dismissed": true}', encoding="utf-8"
    )

    before = load_settings(config_dir)
    assert before.data["site"]["name"] == "toml-name"  # TOML 优先于遗留 JSON
    assert before.data["site"]["setup_dismissed"] is True

    pending = config_admin.legacy_migration_pending(config_dir)
    # 只有 TOML 缺失的键需要迁移，保证迁移前后生效配置一致
    assert pending == {"site": {"setup_dismissed": True}}

    preview = config_admin.format_migration_preview(config_dir)
    assert "setup_dismissed" in preview
    assert "site.json -> site.json.legacy" in preview

    actions = config_admin.migrate_legacy_config(config_dir)

    assert not (config_dir / "site.json").exists()
    assert (config_dir / "site.json.legacy").exists()
    content = (config_dir / "general.toml").read_text(encoding="utf-8")
    assert "# 用户注释" in content  # 注释保留
    assert "setup_dismissed = true" in content
    assert 'name = "toml-name"' in content
    assert "json-name" not in content
    assert any("site.json" in action for action in actions)

    after = load_settings(config_dir)
    assert after.data["site"] == before.data["site"]


def test_migrate_without_legacy_files_is_noop(config_dir):
    assert config_admin.migrate_legacy_config(config_dir) == []


# ---------------------------------------------------------------------------
# 控制台 dispatch
# ---------------------------------------------------------------------------


def test_console_config_path(override_config_dir):
    result = execute_console_command("config path")
    assert result.ok
    assert "配置目录" in result.message
    assert str(override_config_dir.resolve()) in result.message


def test_console_config_show_section(override_config_dir):
    (override_config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")

    result = execute_console_command("config show --section ai")

    assert result.ok
    assert "[ai]" in result.message
    assert "max_tokens = 4096" in result.message
    assert "[site]" not in result.message


def test_console_config_show_rejects_unknown_args(override_config_dir):
    assert not execute_console_command("config show --bogus").ok
    assert not execute_console_command("config show --section").ok


def test_console_config_migrate_dry_run_by_default(override_config_dir):
    (override_config_dir / "general.toml").write_text("[site]\n", encoding="utf-8")
    (override_config_dir / "site.json").write_text('{"setup_dismissed": true}', encoding="utf-8")

    result = execute_console_command("config migrate")

    assert result.ok
    assert "预演模式" in result.message
    assert (override_config_dir / "site.json").exists()  # 未执行


def test_console_config_migrate_cancelled_by_confirm(override_config_dir):
    (override_config_dir / "site.json").write_text('{"setup_dismissed": true}', encoding="utf-8")

    result = execute_console_command("config migrate", confirm=lambda _prompt: False)

    assert result.ok
    assert "已取消" in result.message
    assert (override_config_dir / "site.json").exists()


def test_console_config_migrate_yes_applies(override_config_dir):
    (override_config_dir / "general.toml").write_text("[site]\n", encoding="utf-8")
    (override_config_dir / "site.json").write_text('{"setup_dismissed": true}', encoding="utf-8")

    result = execute_console_command("config migrate --yes")

    assert result.ok
    assert not (override_config_dir / "site.json").exists()
    assert (override_config_dir / "site.json.legacy").exists()
    content = (override_config_dir / "general.toml").read_text(encoding="utf-8")
    assert "setup_dismissed = true" in content


def test_console_config_migrate_without_legacy_files(override_config_dir):
    result = execute_console_command("config migrate --yes")
    assert result.ok
    assert "无需迁移" in result.message


def test_console_config_help_lists_commands():
    result = execute_console_command("help")
    assert result.ok
    assert "config show" in result.message
    assert "config migrate" in result.message
    assert "config path" in result.message
