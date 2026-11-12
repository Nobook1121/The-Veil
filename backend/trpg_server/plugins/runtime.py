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


def clear_plugin_runtime() -> None:
    """卸载/重载时清空扩展项（已注册的 Flask 蓝图无法撤销，需重启）。"""
    with _LOCK:
        _EXTRA_TOOLS.clear()
        _EXTRA_RULESETS.clear()


def unregister_plugin(plugin_id: str) -> None:
    """只移除某个插件注册的扩展项（单个插件回滚用）。"""
    with _LOCK:
        _EXTRA_TOOLS.pop(str(plugin_id), None)
        _EXTRA_RULESETS.pop(str(plugin_id), None)


def runtime_snapshot() -> dict[str, Any]:
    with _LOCK:
        return {
            "agent_tools": {key: len(value) for key, value in _EXTRA_TOOLS.items()},
            "rulesets": sorted(_EXTRA_RULESETS),
        }


__all__ = [
    "clear_plugin_runtime",
    "extra_agent_tools",
    "extra_ruleset_plugins",
    "register_agent_tools",
    "register_ruleset_plugin",
    "runtime_snapshot",
    "unregister_plugin",
]
