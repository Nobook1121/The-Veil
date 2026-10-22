"""配置路径解析。

只依赖标准库，不 import Flask，保证 ``server.py`` 在创建 app 之前即可读取配置。
``--config-dir`` CLI 覆盖通过 ``set_config_dir_override`` 注入；显式传参优先于覆盖值。
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
TEMPLATE_DIR = BASE_DIR / "config" / "templates"
GENERAL_CONFIG_FILENAME = "general.toml"

_config_dir_override: Path | None = None


def set_config_dir_override(path: str | Path | None) -> None:
    """设置全局配置目录覆盖（CLI ``--config-dir``）。``None`` 表示清除覆盖。"""
    global _config_dir_override
    _config_dir_override = Path(path).resolve() if path else None


def resolve_config_dir(config_dir: str | Path | None = None) -> Path:
    """解析配置目录：显式参数 > CLI 覆盖 > 默认 ``data/config``。"""
    if config_dir is not None:
        return Path(config_dir)
    if _config_dir_override is not None:
        return _config_dir_override
    return CONFIG_DIR


def general_config_path(config_dir: str | Path | None = None) -> Path:
    return resolve_config_dir(config_dir) / GENERAL_CONFIG_FILENAME


def template_general_path(template_dir: str | Path | None = None) -> Path:
    return (Path(template_dir) if template_dir else TEMPLATE_DIR) / GENERAL_CONFIG_FILENAME
