"""配置路径解析。

只依赖标准库，不 import Flask，保证 ``server.py`` 在创建 app 之前即可读取配置。
``--config-dir`` CLI 覆盖通过 ``set_config_dir_override`` 注入；显式传参优先于覆盖值。
``--set section.key=value`` 覆盖通过 ``set_cli_overrides`` 注入，由 merge 层读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

BASE_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
TEMPLATE_DIR = BASE_DIR / "config" / "templates"
GENERAL_CONFIG_FILENAME = "general.toml"

_config_dir_override: Path | None = None
_cli_overrides: dict[str, dict[str, Any]] = {}


def set_config_dir_override(path: str | Path | None) -> None:
    """设置全局配置目录覆盖（CLI ``--config-dir``）。``None`` 表示清除覆盖。"""
    global _config_dir_override
    _config_dir_override = Path(path).resolve() if path else None


def set_cli_overrides(overrides: dict[str, dict[str, Any]] | None) -> None:
    """设置全局 CLI 覆盖（CLI ``--set``，优先级高于环境变量）。``None`` 表示清除。"""
    global _cli_overrides
    _cli_overrides = {section: dict(values) for section, values in (overrides or {}).items()}


def get_cli_overrides() -> dict[str, dict[str, Any]]:
    return {section: dict(values) for section, values in _cli_overrides.items()}


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


def parse_set_assignment(text: str) -> tuple[str, str, str]:
    """解析 ``section.key=value`` 为 (section, key, value)。

    段名可含点号（``network.penetration.settings.auth_token=x``），按最后一个
    点切分键名；value 一律为字符串，由 FieldSpec 转换器在合并时类型化。
    """
    if "=" not in text:
        raise ValueError(f"invalid --set assignment (missing '='): {text!r}")
    target, value = text.split("=", 1)
    target = target.strip()
    if "." not in target:
        raise ValueError(f"invalid --set assignment (expected section.key): {text!r}")
    section, key = target.rsplit(".", 1)
    if not section or not key:
        raise ValueError(f"invalid --set assignment: {text!r}")
    return section, key, value


@dataclass(frozen=True)
class CliArgs:
    """``server.py`` 命令行解析结果。"""

    port: int | None = None
    config_dir: Path | None = None
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_cli_args(argv: Sequence[str]) -> CliArgs:
    """解析 ``[port] [--port N] [--config-dir PATH] [--set section.key=value ...]``。

    位置参数 ``port`` 保持兼容；``--port`` 为显式形式，两者同时出现时后者胜出。
    未知参数直接报错，避免拼错的 ``--set`` 被静默忽略。
    """
    port: int | None = None
    config_dir: Path | None = None
    overrides: dict[str, dict[str, Any]] = {}

    args = list(argv[1:])  # 跳过程序名
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--port":
            index += 1
            if index >= len(args):
                raise ValueError("--port requires a value")
            port = int(args[index])
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg == "--config-dir":
            index += 1
            if index >= len(args):
                raise ValueError("--config-dir requires a value")
            config_dir = Path(args[index])
        elif arg.startswith("--config-dir="):
            config_dir = Path(arg.split("=", 1)[1])
        elif arg == "--set":
            index += 1
            if index >= len(args):
                raise ValueError("--set requires a value like section.key=value")
            section, key, value = parse_set_assignment(args[index])
            overrides.setdefault(section, {})[key] = value
        elif arg.startswith("--set="):
            section, key, value = parse_set_assignment(arg.split("=", 1)[1])
            overrides.setdefault(section, {})[key] = value
        elif arg.startswith("-"):
            raise ValueError(f"unknown argument: {arg}")
        elif port is None:
            # 兼容位置参数端口；非数字的位置参数同样报错（与历史行为一致，记录 warning 后忽略交由调用方处理）
            port = int(arg)
        else:
            raise ValueError(f"unexpected positional argument: {arg}")
        index += 1

    return CliArgs(port=port, config_dir=config_dir, overrides=overrides)
