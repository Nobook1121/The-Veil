"""插件装配点（composition root）。

只有本模块知道「插件根目录在哪、怎么读启用列表」。``app_factory`` 只调用
:func:`init_plugins` 与 :func:`register_plugin_blueprints`（注册插件代理
总蓝图），不认识任何具体插件。

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
    set_plugin_registry,
)
from .route_proxy import PROXY_BLUEPRINT_NAME, bp as route_proxy_blueprint
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
    """构造并加载插件注册表；任何异常都只记日志，绝不让主程序启动失败。

    阶段 3 接线后插件蓝图在这里就经 ``route_proxy.mount`` 进路由表
    （挂在插件自己的效果链上），不再收集到「稍后挂 Flask」的清单。
    """
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
    """把插件代理总蓝图注册进宿主 app（**核心蓝图之后**、只注册一次）。

    阶段 3 接线后插件蓝图不再直接挂 Flask：宿主只注册
    :data:`route_proxy.bp` 这一张 catch-all 总蓝图，插件路由全部经
    ``route_proxy.mount`` 进路由表、由总蓝图查表分发；禁用即摘表，
    路由立即 404。总蓝图必须晚于核心蓝图注册——同 pattern 同 method
    先注册者赢，核心管理面（``POST /api/plugins/<id>/enable|disable``、
    ``DELETE /api/plugins/<id>``、``GET /api/plugins/<id>/assets/...`` 等）
    不能被吞；``app_factory`` 的调用顺序就是这个约束的保障。
    """
    if PROXY_BLUEPRINT_NAME in getattr(app, "blueprints", {}):
        return []
    try:
        app.register_blueprint(route_proxy_blueprint)
    except Exception:  # noqa: BLE001
        logger.warning("plugin_route_proxy_registration_failed", exc_info=True)
        return []
    return [PROXY_BLUEPRINT_NAME]


__all__ = [
    "ENV_DISABLED",
    "init_plugins",
    "plugins_disabled_by_env",
    "register_plugin_blueprints",
    "resolve_plugin_root",
]
