"""遗留 JSON（site/network/penetration）兼容读取与整合段的读写回归测试。

兼容期语义：遗留 JSON 作为低于用户 ``general.toml`` 的一层参与合并；
TOML 已有的键胜出，缺失的键回落到遗留 JSON；全部缺失时走模板/代码默认值。
"""

import json

from trpg_server.config.merge import load_settings
from trpg_server.network_discovery import (
    get_network_config,
    get_penetration_config,
    save_network_config,
    save_penetration_config,
)


def _write_json(config_dir, name, data):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / name).write_text(json.dumps(data), encoding="utf-8")


def _write_user_toml(config_dir, text):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text(text, encoding="utf-8")


def test_legacy_site_json_applies_when_toml_missing(tmp_path):
    _write_json(tmp_path, "site.json", {"name": "遗留站", "domain": "old.example", "setup_dismissed": True})
    settings = load_settings(tmp_path, env={})
    site = settings.section("site")
    assert site["name"] == "遗留站"
    assert site["domain"] == "old.example"
    assert site["setup_dismissed"] is True


def test_user_toml_wins_over_legacy_json(tmp_path):
    _write_json(tmp_path, "site.json", {"name": "遗留站", "domain": "old.example"})
    _write_user_toml(tmp_path, '[site]\nname = "新站"\n')
    settings = load_settings(tmp_path, env={})
    site = settings.section("site")
    assert site["name"] == "新站"
    assert site["domain"] == "old.example"  # TOML 未覆盖的键仍回落遗留 JSON


def test_legacy_network_json_flattens_access_control(tmp_path):
    _write_json(tmp_path, "network.json", {
        "port": 9000,
        "discovery_enabled": False,
        "share_base_url": "http://10.0.0.1:9000",
        "access_control": {"enabled": True, "allowed_ips": ["10.0.0.2", "10.0.0.3"]},
    })
    config = get_network_config(tmp_path)
    assert config["port"] == 9000
    assert config["discovery_enabled"] is False
    assert config["share_base_url"] == "http://10.0.0.1:9000"
    assert config["access_control"] == {"enabled": True, "allowed_ips": ["10.0.0.2", "10.0.0.3"]}


def test_legacy_penetration_json_flattens_settings(tmp_path):
    _write_json(tmp_path, "penetration.json", {
        "enabled": True,
        "type": "ngrok",
        "settings": {"auth_token": "tok", "region": "eu", "subdomain": "demo"},
        "port_mappings": [{"proto": "http", "local": 8086}],
    })
    config = get_penetration_config(tmp_path)
    assert config["enabled"] is True
    assert config["type"] == "ngrok"
    assert config["settings"] == {"auth_token": "tok", "region": "eu", "subdomain": "demo"}
    assert config["port_mappings"] == [{"proto": "http", "local": 8086}]


def test_corrupt_legacy_json_falls_back_to_defaults(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "network.json").write_text("{not json", encoding="utf-8")
    config = get_network_config(tmp_path)
    assert config["port"] == 8086  # 模板值兜底，不崩溃


def test_env_overrides_legacy_network_port(tmp_path):
    _write_json(tmp_path, "network.json", {"port": 9000})
    settings = load_settings(tmp_path, env={"AI_TRPG_CFG_NETWORK_PORT": "7000"})
    assert settings.section("network")["port"] == 7000
    assert settings.locked["network.port"] == "env"


def test_save_network_config_writes_toml_and_keeps_shape(tmp_path):
    save_network_config(
        {"port": 9100, "share_base_url": "http://192.168.1.5:9100",
         "access_control": {"enabled": True, "allowed_ips": ["192.168.1.8"]}},
        tmp_path,
    )
    config = get_network_config(tmp_path)
    assert config["port"] == 9100
    assert config["share_base_url"] == "http://192.168.1.5:9100"
    assert config["access_control"] == {"enabled": True, "allowed_ips": ["192.168.1.8"]}
    text = (tmp_path / "general.toml").read_text(encoding="utf-8")
    assert "[network]" in text and "[network.access_control]" in text
    # 未提交的键不被写入用户文件（仍由模板/默认兜底）。
    assert "discovery_enabled" not in text


def test_save_penetration_config_splits_settings_subtable(tmp_path):
    save_penetration_config(
        {"enabled": True, "settings": {"auth_token": "abc", "region": "jp"}},
        tmp_path,
    )
    config = get_penetration_config(tmp_path)
    assert config["enabled"] is True
    assert config["settings"]["auth_token"] == "abc"
    assert config["settings"]["region"] == "jp"
    assert config["settings"]["subdomain"] == ""  # 未提交的子键回落默认
    text = (tmp_path / "general.toml").read_text(encoding="utf-8")
    assert "[network.penetration]" in text
    assert "[network.penetration.settings]" in text
