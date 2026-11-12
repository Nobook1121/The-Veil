"""插件装配点（composition root）。

只有本模块知道「插件根目录在哪、怎么读启用列表」。``app_factory`` 只调用
:func:`init_plugins` 与 :meth:`PluginRegistry.register_blueprints`，不认识任何
具体插件。

一键禁用全部第三方插件（回滚手段）：

- 启动参数 ``--no-plugins``；
- 环境变量 ``TRPG_PLUGINS_DISABLED=1``；
- 测试/嵌入时 ``create_app({"PLUGINS_DISABLED": True})``。

任一命中时只读取清单做展示，不读取资源、不导入任何插件代码。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .registry import (
    PluginRegistry,
    PluginStateStore,
    plugin_registry,
    set_plugin_registry,
)
from .security import default_plugin_root, enabled_state_path

logger = logging.getLogger(__name__)

ENV_DISABLED = "TRPG_PLUGINS_DISABLED"


def plugins_disabled_by_env() -> bool:
    raw = str(os.environ.get(ENV_DISABLED, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_plugin_root(app: Any = None) -> Path:
    if app is not None:
        configured = app.config.get("PLUGIN_DIR")
        if configured:
            return Path(configured)
        return Path(app.config.get("DATA_DIR") or default_plugin_root().parent) / "plugins"
    return default_plugin_root()


def init_plugins(app: Any = None) -> PluginRegistry | None:
    """构造并加载插件注册表；任何异常都只记日志，绝不让主程序启动失败。"""
    root = resolve_plugin_root(app)
    disabled = bool(app.config.get("PLUGINS_DISABLED")) if app is not None else False
    disabled = disabled or plugins_disabled_by_env()
    app_version = None
    if app is not None:
        app_version = str(app.config.get("APP_VERSION") or "") or None
    try:
        registry = PluginRegistry(
            root,
            app_version=app_version,
            disabled_all=disabled,
            state_store=PluginStateStore(enabled_state_path(root)),
        )
        if app is not None:
            registry.attach_app(app)
        registry.load()
    except Exception:  # noqa: BLE001 - 插件体系自身故障不能阻断服务
        logger.warning("plugin_registry_init_failed root=%s", root, exc_info=True)
        set_plugin_registry(None)
        return None

    set_plugin_registry(registry)
    if app is not None:
        app.extensions["plugin_registry"] = registry
        if disabled:
            logger.warning("third_party_plugins_disabled_by_operator root=%s", root)
    return registry


def register_plugin_blueprints(app: Any) -> list[str]:
    registry = plugin_registry()
    if registry is None:
        return []
    try:
        return registry.register_blueprints(app)
    except Exception:  # noqa: BLE001
        logger.warning("plugin_blueprint_registration_failed", exc_info=True)
        return []


__all__ = [
    "ENV_DISABLED",
    "init_plugins",
    "plugins_disabled_by_env",
    "register_plugin_blueprints",
    "resolve_plugin_root",
]
