"""统一配置加载包。

四层优先级：代码默认值（schema）< 包内模板 < 用户配置 < 环境变量/CLI。
公共 API 见下；模块内聚：paths（路径）、fields（字段规格）、schema（默认值）、
sources（各层读取）、merge（合并与缓存）、writer（注释保留写入）、bootstrap（首启生成）。
"""

from trpg_server.config.bootstrap import ensure_user_config, render_minimal_general
from trpg_server.config.merge import Settings, invalidate_settings_cache, load_settings
from trpg_server.config.paths import (
    BASE_DIR,
    CONFIG_DIR,
    DATA_DIR,
    GENERAL_CONFIG_FILENAME,
    TEMPLATE_DIR,
    general_config_path,
    resolve_config_dir,
    set_config_dir_override,
    template_general_path,
)
from trpg_server.config.schema import AIRuntimeConfig
from trpg_server.config.writer import update_sections

__all__ = [
    "AIRuntimeConfig",
    "BASE_DIR",
    "CONFIG_DIR",
    "DATA_DIR",
    "GENERAL_CONFIG_FILENAME",
    "Settings",
    "TEMPLATE_DIR",
    "ensure_user_config",
    "general_config_path",
    "invalidate_settings_cache",
    "load_settings",
    "render_minimal_general",
    "resolve_config_dir",
    "set_config_dir_override",
    "template_general_path",
    "update_sections",
]
