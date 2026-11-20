"""规则集插件装配点（composition root）。

整个后端只有本模块允许写出具体插件的模块路径；其余核心代码一律通过
:func:`trpg_server.ruleset.registry.get_plugin` 取插件，不认识任何规则书。

插件以模块形式登记：模块需导出实例 ``PLUGIN``，或提供零参工厂 ``create_plugin()``。
模块缺失/导入失败只记录日志并跳过，保证「某个插件被移除」不会让核心启动失败。

可用环境变量覆盖（逗号分隔的模块路径）：

- ``TRPG_RULESET_PLUGINS``：启用的插件模块，默认 ``trpg_server.ruleset.coc7``。
- ``TRPG_RULESET_DEFAULT``：显式默认插件 id，优先级高于配置文件；用于运维临时
  覆盖。未设置时读配置项 ``[ruleset] default_id``（模板默认 ``coc7``，
  ``config/schema.py`` 的 ``DEFAULT_RULESET_ID`` 为单一真源）。
- ``TRPG_RULESET_COMBAT_FALLBACKS``：``id=模块路径`` 形式的战斗实现兼容表；
  核心默认不再登记任何具体规则集的兼容实现，只在运维显式配置时懒加载。
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
# 占位规则集：仅用于界面「开发中」灰卡，不注册为插件。具体规则集由插件提供，
# 核心不再写死任何规则集 id。
DEFAULT_DECLARED_RULESETS: tuple[DeclaredRuleset, ...] = ()
# 战斗实现的兼容加载表：默认为空。核心不再为任何具体规则集预置战斗实现；
# 规则集应通过插件 ``combat`` 能力提供，运维可用环境变量做临时兼容。
DEFAULT_COMBAT_MODULES: tuple[tuple[str, str], ...] = ()

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
    """默认规则集 id 的显式配置值；空串表示「未配置」。

    优先级：环境变量 ``TRPG_RULESET_DEFAULT``（运维临时覆盖）> 配置项
    ``[ruleset] default_id``（代码/模板默认 ``coc7``）。两者都为空时注册表
    回退到第一个成功加载的插件（注册顺序即确定性来源）。
    """
    env_hint = str(os.environ.get(ENV_DEFAULT_RULESET) or "").strip().lower()
    if env_hint:
        return env_hint
    return configured_default_id()


def configured_default_id() -> str:
    """从配置链读取 ``[ruleset] default_id``（经 load_settings 类型化）。

    配置层故障视为未配置：注册表回退第一个可用插件，不让启动失败。
    """
    from trpg_server.config import load_settings

    try:
        raw = load_settings().section("ruleset").get("default_id")
    except Exception:  # noqa: BLE001 - 配置层故障等价于「未配置」
        logger.warning("Failed to read [ruleset] default_id from config; treating as unconfigured", exc_info=True)
        return ""
    return str(raw or "").strip().lower()


def combat_module_map() -> dict[str, str]:
    """占位规则集 → 战斗实现模块路径；默认空表，仅环境变量可配置。"""
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
