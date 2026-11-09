"""内置战斗实现。

包导入本身不加载任何具体规则包：核心删除某个实现后，只有真正引用该实现的
调用点才会失败，注册表与启动路径不受影响。
"""

from __future__ import annotations

from typing import Any

__all__ = ["Coc7Ruleset", "Dnd5eRuleset"]

_MODULE_ATTRS = {
    "Coc7Ruleset": ".coc7",
    "Dnd5eRuleset": ".dnd5e",
}


def __getattr__(name: str) -> Any:
    module_path = _MODULE_ATTRS.get(name)
    if module_path is None:
        raise AttributeError(name)
    from importlib import import_module

    module = import_module(module_path, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
