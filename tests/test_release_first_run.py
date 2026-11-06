"""发布包首启必须进入安装向导：模板、种子配置、状态接口与打包守卫。

首启状态的真实来源是 ``data/config/general.toml`` 的 ``[site] setup_dismissed``：
首次启动时由 ``config/templates/general.toml`` 复制生成；只要该值为 false 且系统里
没有任何 OWNER，``/api/setup/status`` 就会返回 ``setup_required=true``。
发布包不得包含 ``data/config`` / ``data/runtime`` / 用户库，否则解压后可能直接跳过向导。
"""

from pathlib import Path

import tomlkit

from trpg_server.app_factory import create_app
from trpg_server.config.bootstrap import ensure_user_config
from trpg_server.users.database import UserDatabase
from trpg_server.users.service import UserService

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "config" / "templates" / "general.toml"
PACKAGE_SCRIPT = REPO_ROOT / "scripts" / "package-release.ps1"


def _template_site() -> dict:
    return tomlkit.parse(TEMPLATE.read_text(encoding="utf-8"))["site"]


def _fresh_app(tmp_path: Path):
    config_dir = tmp_path / "config"
    database = UserDatabase(tmp_path / "users" / "users.sqlite3")
    database.initialize()
    manager = UserService(database, ip_config_dir=tmp_path / "users" / "ip_configs")
    base = {
        "TESTING": True,
        "SECRET_KEY": "test",
        "USERS_DIR": tmp_path / "users",
        "LOGS_DIR": tmp_path / "logs",
        "CONFIG_DIR": config_dir,
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOMS_DIR": tmp_path / "rooms",
        "OCCUPATIONS_DIR": tmp_path / "occupations",
        "WEAPONS_DIR": tmp_path / "weapons",
        "SCENARIO_IMPORTS_DIR": tmp_path / "scenario_imports",
        "NARRATIVE_JOBS_DIR": tmp_path / "narrative_jobs",
        "USER_MANAGER": manager,
    }
    return create_app(base), config_dir


def _site_status(app) -> dict:
    response = app.test_client().get("/api/setup/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    return payload["data"]


def test_template_keeps_setup_wizard_enabled():
    """配置模板必须让首启进入向导；改成 true 时本测试先红。"""
    assert _template_site()["setup_dismissed"] is False


def test_fresh_config_seeds_setup_dismissed_false(tmp_path):
    """空配置目录首次启动时，种子 general.toml 必须保持 setup_dismissed=false。"""
    config_dir = tmp_path / "config"

    created = ensure_user_config(config_dir)
    assert config_dir / "general.toml" in created

    seeded = tomlkit.parse((config_dir / "general.toml").read_text(encoding="utf-8"))
    assert seeded["site"]["setup_dismissed"] is False


def test_fresh_deployment_reports_setup_required(tmp_path):
    """干净部署（无配置、无 OWNER）的状态接口必须要求进入安装向导。"""
    app, config_dir = _fresh_app(tmp_path)

    status = _site_status(app)

    assert (config_dir / "general.toml").exists()
    assert status["owner_exists"] is False
    assert status["setup_dismissed"] is False
    assert status["setup_required"] is True


def test_package_script_guards_first_run_state():
    """打包脚本必须把首启规则固化成失败即中止的校验。"""
    script = PACKAGE_SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("data/config", "data/runtime", "data/users", "backend/data"):
        assert forbidden in script, f"打包脚本缺少禁止目录: {forbidden}"

    assert "Assert-FirstRunReady" in script
    assert "Assert-ZipFirstRunReady" in script
    assert "setup_dismissed" in script
    assert "dist/public/js/setup-wizard.js" in script
