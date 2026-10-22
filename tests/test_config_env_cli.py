"""环境变量 / CLI 覆盖与部署锁定键的端到端测试。

覆盖：env 名派生、冲突检测、CLI > env 优先级、非法 env 值回退，
以及设置 API 对锁定键与密钥字段的保护。
"""

from trpg_server.app_factory import create_app
from trpg_server.config import load_settings
from trpg_server.config.schema import SMALL_MODEL_TASK_KEYS, SMALL_MODELS_SECTION, SPECS
from trpg_server.config.sources import env_key_name


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "admin", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(role),
            "CONFIG_DIR": config_dir,
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
        }
    )


def _login(client, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(
            user_id=1,
            username="tester",
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def test_env_key_name_derivation():
    assert env_key_name("ai", "max_tokens") == "AI_TRPG_CFG_AI_MAX_TOKENS"
    assert env_key_name("ai.cache", "semantic_enabled") == "AI_TRPG_CFG_AI_CACHE_SEMANTIC_ENABLED"
    assert env_key_name("network", "port") == "AI_TRPG_CFG_NETWORK_PORT"


def test_schema_env_names_have_no_conflicts():
    """所有注册键派生的 env 名必须唯一映射到同一 (section, key)。"""
    seen = {}
    for spec in SPECS:
        for key in spec.keys:
            name = env_key_name(spec.section, key)
            assert seen.get(name) in (None, (spec.section, key)), name
            seen[name] = (spec.section, key)
    for task in SMALL_MODEL_TASK_KEYS:
        name = env_key_name(SMALL_MODELS_SECTION, task)
        assert seen.get(name) in (None, (SMALL_MODELS_SECTION, task)), name


def test_env_layer_overrides_user_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")

    settings = load_settings(config_dir, env={"AI_TRPG_CFG_AI_MAX_TOKENS": "8192"})

    assert settings.ai.max_tokens == 8192
    assert settings.locked["ai.max_tokens"] == "env"


def test_cli_overrides_env(tmp_path):
    settings = load_settings(
        tmp_path,
        env={"AI_TRPG_CFG_AI_MAX_TOKENS": "8192"},
        cli_overrides={"ai": {"max_tokens": 2048}},
    )

    assert settings.ai.max_tokens == 2048
    assert settings.locked["ai.max_tokens"] == "cli"


def test_invalid_env_value_falls_back_to_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text("[ai]\nmax_tokens = 4096\n", encoding="utf-8")

    settings = load_settings(config_dir, env={"AI_TRPG_CFG_AI_MAX_TOKENS": "not-a-number"})

    assert settings.ai.max_tokens == 4096


def test_unknown_env_var_does_not_break_loading(tmp_path, caplog):
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_TYPO_KEY": "1"})

    assert settings.ai.max_tokens == 16384
    assert "AI_TRPG_CFG_TYPO_KEY" in caplog.text


def test_general_config_api_returns_effective_config_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRPG_CFG_AI_MAX_TOKENS", "8192")
    client = _app(tmp_path).test_client()

    response = client.get("/api/config/general")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["config"]["ai"]["max_tokens"] == 8192
    assert payload["overrides"]["ai.max_tokens"] == "env"


def test_general_config_api_masks_penetration_auth_token(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        '[network.penetration.settings]\nauth_token = "secret-token"\n',
        encoding="utf-8",
    )
    client = _app(tmp_path).test_client()

    payload = client.get("/api/config/general").get_json()["data"]

    assert payload["config"]["network.penetration.settings"]["auth_token"] == ""


def test_save_config_skips_env_locked_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRPG_CFG_AI_MAX_TOKENS", "8192")
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/config/general",
        json={"ai": {"max_tokens": 4096, "stream_output": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    config_dir = tmp_path / "config"
    content = (config_dir / "general.toml").read_text(encoding="utf-8")
    assert "max_tokens = 4096" not in content
    assert "stream_output = true" in content
    # 锁定键仍按部署层生效
    assert client.get("/api/config/general").get_json()["data"]["config"]["ai"]["max_tokens"] == 8192


def test_save_config_never_writes_redacted_keys(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        '[network.penetration.settings]\nauth_token = "secret-token"\n',
        encoding="utf-8",
    )
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/config/general",
        json={"network.penetration.settings": {"auth_token": "stolen"}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    content = (config_dir / "general.toml").read_text(encoding="utf-8")
    assert "stolen" not in content
    assert "secret-token" in content


def test_save_config_preserves_comments_and_untouched_keys(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "# 头部注释\n[ai]\n# 保留这行\nstream_output = false\nmax_tokens = 16384\n",
        encoding="utf-8",
    )
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/config/general",
        json={"ai": {"stream_output": True, "max_tokens": 16384}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    content = (config_dir / "general.toml").read_text(encoding="utf-8")
    assert "# 头部注释" in content
    assert "# 保留这行" in content
    assert "stream_output = true" in content
    assert "max_tokens = 16384" in content


def test_static_config_files_are_not_served(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text("[ai]\n", encoding="utf-8")
    (config_dir / "penetration.json").write_text("{}", encoding="utf-8")
    client = _app(tmp_path).test_client()

    assert client.get("/config/general.toml").status_code == 404
    assert client.get("/config/penetration.json").status_code == 404
