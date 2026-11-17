"""插件运行期注册表（给核心的扩展钩子）。

核心只在两个地方读这里：

- :func:`trpg_server.agents.tools.default_tool_registry` 合并 ``extra_agent_tools()``；
- :func:`trpg_server.ruleset.bootstrap.build_registry` 合并 ``extra_ruleset_plugins()``。

这样核心不需要 import 任何插件包，插件被移除/加载失败也不会让核心导入失败。
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()
_EXTRA_TOOLS: dict[str, tuple[Any, ...]] = {}
_EXTRA_RULESETS: dict[str, Any] = {}
_CONTENT_PACKS: dict[str, Any] = {}


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


def clear_plugin_runtime() -> None:
    """卸载/重载时清空扩展项（已注册的 Flask 蓝图无法撤销，需重启）。"""
    with _LOCK:
        _EXTRA_TOOLS.clear()
        _EXTRA_RULESETS.clear()
        _CONTENT_PACKS.clear()


def unregister_plugin(plugin_id: str) -> None:
    """只移除某个插件注册的扩展项（单个插件回滚用）。"""
    with _LOCK:
        _EXTRA_TOOLS.pop(str(plugin_id), None)
        _EXTRA_RULESETS.pop(str(plugin_id), None)
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
    "register_agent_tools",
    "register_content_packs",
    "register_ruleset_plugin",
    "runtime_snapshot",
    "unregister_plugin",
]
