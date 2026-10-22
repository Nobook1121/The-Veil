"""AI 运行时配置的兼容门面。

实现已迁入 ``trpg_server.config``（分层加载：代码默认值 < 包内模板 < 用户配置
< 环境变量/CLI）。本模块仅为既有调用方保留导入路径与函数签名；新增代码请直接
使用 ``trpg_server.config``。
"""

from pathlib import Path
from typing import Any

from trpg_server.config import (
    AIRuntimeConfig,
    general_config_path,
    load_settings,
    update_sections,
)
from trpg_server.config.schema import SPECS_BY_SECTION_KEY

__all__ = ["AIRuntimeConfig", "ai_debug_enabled", "load_ai_runtime_config", "write_ai_setting"]


def _resolve_dir(config_dir: str | Path | None) -> str | Path:
    if config_dir is None:
        from trpg_server.settings import CONFIG_DIR

        return CONFIG_DIR
    return config_dir


def load_ai_runtime_config(config_dir: str | Path | None = None) -> AIRuntimeConfig:
    """加载 AI 运行时配置（模板 + 用户配置 + env/CLI 合并后的生效值）。"""
    return load_settings(_resolve_dir(config_dir)).ai


def ai_debug_enabled(config_dir: str | Path | None = None) -> bool:
    """是否开启 AI 调试模式。

    完整 AI 请求/响应会包含整段提示词与模型输出，只有显式开启调试模式时才写日志，
    避免正常运行（尤其是大剧本导入）把日志刷满。
    """
    return load_settings(_resolve_dir(config_dir)).ai.debug_mode


def write_ai_setting(key: str, value: Any, config_dir: str | Path | None = None) -> Path:
    """把单个 ``[ai]`` 键写回 ``general.toml``。

    值先经 FieldSpec 归一化（字符串 ``"8"`` 会按整数键落成 ``8``），写入保留
    其余键、注释与顺序，临时文件 + ``os.replace`` 原子替换。
    """
    resolved = _resolve_dir(config_dir)
    spec = SPECS_BY_SECTION_KEY.get(("ai", key))
    canonical_key = key
    if spec is not None:
        value = spec.convert(value, spec.default)
        canonical_key = spec.keys[0]
    return update_sections(general_config_path(resolved), {"ai": {canonical_key: value}})
