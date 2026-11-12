"""第三方插件扩展体系（``docs/plugin-development.md``）。

公共入口：

- :func:`init_plugins`：装配点，由 ``app_factory.create_app`` 调用；
- :func:`plugin_registry`：取当前进程的注册表（可能为 ``None``）；
- :mod:`trpg_server.plugins.contracts`：清单契约与常量；
- :mod:`trpg_server.plugins.manifest`：清单校验与 JSON Schema。

注意：这里刻意不 import 具体插件，也不 import Flask 之外的第三方依赖，
保证「插件体系自己坏掉」不会连带核心模块导入失败。
"""

from __future__ import annotations

from .bootstrap import init_plugins, plugins_disabled_by_env, register_plugin_blueprints
from .contracts import PLUGIN_API_VERSION, PLUGIN_MANIFEST_VERSION, PluginManifest, PluginRecord
from .registry import (
    PluginNotFound,
    PluginRegistry,
    PluginStateError,
    PluginStateStore,
    plugin_registry,
    reset_plugin_registry,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_MANIFEST_VERSION",
    "PluginManifest",
    "PluginNotFound",
    "PluginRecord",
    "PluginRegistry",
    "PluginStateError",
    "PluginStateStore",
    "init_plugins",
    "plugin_registry",
    "plugins_disabled_by_env",
    "register_plugin_blueprints",
    "reset_plugin_registry",
]
