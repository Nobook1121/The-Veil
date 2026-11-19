"""插件运行期注册表（给核心的扩展钩子）。

核心只在两个地方读这里：

- :func:`trpg_server.agents.tools.default_tool_registry` 合并 ``extra_agent_tools()``；
- :func:`trpg_server.ruleset.bootstrap.build_registry` 合并 ``extra_ruleset_plugins()``。

这样核心不需要 import 任何插件包，插件被移除/加载失败也不会让核心导入失败。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_EXTRA_TOOLS: dict[str, tuple[Any, ...]] = {}
_EXTRA_RULESETS: dict[str, Any] = {}
_CONTENT_PACKS: dict[str, Any] = {}
_SERVICES: dict[str, dict[str, Any]] = {}


def register_agent_tools(plugin_id: str, tools: list[Any]) -> None:
    with _LOCK:
        _EXTRA_TOOLS[str(plugin_id)] = tuple(tools)


def extra_agent_tools() -> list[Any]:
    with _LOCK:
        return [tool for tools in _EXTRA_TOOLS.values() for tool in tools]


def register_ruleset_plugin(plugin_id: str, plugin: Any) -> None:
    with _LOCK:
        _EXTRA_RULESETS[str(plugin_id)] = plugin


def extra_ruleset_plugins() -> list[Any]:
    with _LOCK:
        return list(_EXTRA_RULESETS.values())


def register_content_packs(plugin_id: str, library: Any) -> None:
    with _LOCK:
        _CONTENT_PACKS[str(plugin_id)] = library


def content_packs_for(ruleset_id: str = "") -> Any:
    """按规则集聚合所有已装载的数据包；无数据包时返回空库。"""
    from .content_packs import ContentPackLibrary

    with _LOCK:
        libraries = list(_CONTENT_PACKS.values())
    packs = [
        pack
        for library in libraries
        for pack in getattr(library, "packs", ())
        if not ruleset_id or pack.ruleset_id == str(ruleset_id).strip().lower()
    ]
    return ContentPackLibrary(tuple(packs))


def content_pack_ids() -> list[str]:
    with _LOCK:
        return sorted(_CONTENT_PACKS)


def register_service(plugin_id: str, name: str, service: Any) -> None:
    """注册插件内部能力（如地图服务），核心按名字探测，不 import 插件包。"""
    with _LOCK:
        bucket = _SERVICES.setdefault(str(plugin_id), {})
        bucket[str(name)] = service


def get_service(plugin_id: str, name: str) -> Any | None:
    with _LOCK:
        return (_SERVICES.get(str(plugin_id)) or {}).get(str(name))


def services_snapshot() -> dict[str, tuple[str, ...]]:
    """全部已注册服务的只读快照（插件激活窗口前后对拍用）。"""
    with _LOCK:
        return {plugin_id: tuple(sorted(bucket)) for plugin_id, bucket in _SERVICES.items()}


def unregister_service(plugin_id: str, name: str) -> None:
    """只摘除一个具名服务（效果撤销回执用）。"""
    with _LOCK:
        bucket = _SERVICES.get(str(plugin_id))
        if bucket is None:
            return
        bucket.pop(str(name), None)
        if not bucket:
            _SERVICES.pop(str(plugin_id), None)


def clear_plugin_runtime() -> None:
    """卸载/重载时清空扩展项（已注册的 Flask 蓝图无法撤销，需重启）。"""
    with _LOCK:
        _EXTRA_TOOLS.clear()
        _EXTRA_RULESETS.clear()
        _CONTENT_PACKS.clear()
        _SERVICES.clear()
    try:
        from trpg_server.workflows import areas
        from trpg_server.workflows.guards import clear_action_guards

        clear_action_guards()
        # 插件卸载后不能继续把旧地图的坐标 / 场景注入核心区域引擎。
        areas.clear_position_provider()
        areas.clear_scene_id_provider()
    except Exception:  # noqa: BLE001 - 核心未加载工作流时忽略
        logger.exception("插件运行时清理钩子失败")


def unregister_plugin(plugin_id: str) -> None:
    """只移除某个插件注册的扩展项（单个插件整体回滚用）。"""
    with _LOCK:
        _EXTRA_TOOLS.pop(str(plugin_id), None)
        _EXTRA_RULESETS.pop(str(plugin_id), None)
        _CONTENT_PACKS.pop(str(plugin_id), None)
        _SERVICES.pop(str(plugin_id), None)


# 按能力粒度的撤销：阶段 2 的效果撤销链（ctx.dispose 的 disposer）需要
# 「谁注册的谁撤销」，不能用整体 unregister_plugin 把别的能力一并带走。
def unregister_agent_tools(plugin_id: str) -> None:
    """只摘除某插件注册的 Agent 工具（效果撤销回执用）。"""
    with _LOCK:
        _EXTRA_TOOLS.pop(str(plugin_id), None)


def unregister_ruleset_plugin(plugin_id: str) -> None:
    """只摘除某插件注册的规则集（效果撤销回执用）。"""
    with _LOCK:
        _EXTRA_RULESETS.pop(str(plugin_id), None)


def unregister_content_packs(plugin_id: str) -> None:
    """只摘除某插件装载的内容数据包（效果撤销回执用）。"""
    with _LOCK:
        _CONTENT_PACKS.pop(str(plugin_id), None)


def runtime_snapshot() -> dict[str, Any]:
    with _LOCK:
        return {
            "agent_tools": {key: len(value) for key, value in _EXTRA_TOOLS.items()},
            "rulesets": sorted(_EXTRA_RULESETS),
            "content_packs": sorted(_CONTENT_PACKS),
        }


__all__ = [
    "clear_plugin_runtime",
    "content_pack_ids",
    "content_packs_for",
    "extra_agent_tools",
    "extra_ruleset_plugins",
    "get_service",
    "register_agent_tools",
    "register_content_packs",
    "register_ruleset_plugin",
    "register_service",
    "runtime_snapshot",
    "services_snapshot",
    "unregister_agent_tools",
    "unregister_content_packs",
    "unregister_plugin",
    "unregister_ruleset_plugin",
    "unregister_service",
]
