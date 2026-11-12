"""规则集插件装配点（composition root）。

整个后端只有本模块允许写出具体插件的模块路径；其余核心代码一律通过
:func:`trpg_server.ruleset.registry.get_plugin` 取插件，不认识任何规则书。

插件以模块形式登记：模块需导出实例 ``PLUGIN``，或提供零参工厂 ``create_plugin()``。
模块缺失/导入失败只记录日志并跳过，保证「某个插件被移除」不会让核心启动失败。

可用环境变量覆盖（逗号分隔的模块路径）：

- ``TRPG_RULESET_PLUGINS``：启用的插件模块，默认 ``trpg_server.ruleset.coc7``。
- ``TRPG_RULESET_DEFAULT``：显式默认插件 id；留空时取第一个成功加载的插件。
- ``TRPG_RULESET_COMBAT_FALLBACKS``：``id=模块路径`` 形式的战斗实现兼容表，
  仅用于「已声明但尚未插件化」的占位规则集。
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Iterable

from trpg_server.ruleset.registry import DeclaredRuleset, RulesetRegistry

logger = logging.getLogger(__name__)

# 默认内置插件清单。新增插件时只需改这一处（或环境变量），核心无需改动。
DEFAULT_PLUGIN_MODULES: tuple[str, ...] = ("trpg_server.ruleset.coc7",)
# 仅作「开发中」占位：不可选、不注册为插件、不提供任何规则能力。
DEFAULT_DECLARED_RULESETS: tuple[DeclaredRuleset, ...] = (
    DeclaredRuleset(id="dnd5e", display_name="DnD 5e"),
)
# 战斗实现的兼容加载表：仅覆盖尚未通过插件 ``combat`` 能力提供实现的占位规则集。
DEFAULT_COMBAT_MODULES: tuple[tuple[str, str], ...] = (
    ("dnd5e", "trpg_server.combat.rulesets.dnd5e"),
)

ENV_PLUGIN_MODULES = "TRPG_RULESET_PLUGINS"
ENV_DEFAULT_RULESET = "TRPG_RULESET_DEFAULT"
ENV_COMBAT_MODULES = "TRPG_RULESET_COMBAT_FALLBACKS"


def _split_env(name: str) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def plugin_module_paths() -> tuple[str, ...]:
    """当前启用的插件模块路径；环境变量存在时以它为准（允许显式置空）。"""
    if os.environ.get(ENV_PLUGIN_MODULES) is not None:
        return tuple(_split_env(ENV_PLUGIN_MODULES))
    return DEFAULT_PLUGIN_MODULES


def default_ruleset_hint() -> str:
    return str(os.environ.get(ENV_DEFAULT_RULESET) or "").strip().lower()


def combat_module_map() -> dict[str, str]:
    """占位规则集 → 战斗实现模块路径。"""
    raw = os.environ.get(ENV_COMBAT_MODULES)
    if raw is None:
        return dict(DEFAULT_COMBAT_MODULES)
    mapping: dict[str, str] = {}
    for item in _split_env(ENV_COMBAT_MODULES):
        ruleset_id, _, module_path = item.partition("=")
        ruleset_id = ruleset_id.strip().lower()
        module_path = module_path.strip()
        if ruleset_id and module_path:
            mapping[ruleset_id] = module_path
    return mapping


def _load_plugin(module_path: str) -> Any | None:
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - 插件缺失不应阻断核心启动
        logger.warning("Ruleset plugin module unavailable: %s", module_path, exc_info=True)
        return None
    plugin = getattr(module, "PLUGIN", None)
    if plugin is not None:
        return plugin
    factory = getattr(module, "create_plugin", None)
    if callable(factory):
        try:
            return factory()
        except Exception:  # noqa: BLE001
            logger.warning("Ruleset plugin factory failed: %s", module_path, exc_info=True)
            return None
    logger.warning("Ruleset plugin module exports neither PLUGIN nor create_plugin(): %s", module_path)
    return None


def load_plugins(module_paths: Iterable[str]) -> list[Any]:
    plugins: list[Any] = []
    for module_path in module_paths:
        plugin = _load_plugin(module_path)
        if plugin is not None:
            plugins.append(plugin)
    return plugins


def build_registry() -> RulesetRegistry:
    """按登记表构造进程级注册表；不抛异常，最坏情况返回空注册表。"""
    plugins = load_plugins(plugin_module_paths())
    # 第三方插件通过 capabilities.rulesets 提供的规则集（未启用时为空）。
    try:
        from trpg_server.plugins.runtime import extra_ruleset_plugins

        plugins.extend(extra_ruleset_plugins())
    except Exception:  # noqa: BLE001 - 插件钩子故障不影响内置规则集
        logger.debug("plugin_ruleset_hook_failed", exc_info=True)
    return RulesetRegistry(
        plugins,
        declared=DEFAULT_DECLARED_RULESETS,
        default_id=default_ruleset_hint(),
    )
