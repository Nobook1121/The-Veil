"""战斗规则注册表。

插件通过 ``combat`` 能力提供实现；本模块只做「按规则集 id 取实现」与兼容兜底，
不静态 import 任何具体规则包，因此删除某个战斗实现不会让核心导入失败。

尚未通过插件能力提供实现的占位规则集，其兼容实现登记在
:mod:`trpg_server.ruleset.bootstrap` 的 ``combat_module_map()`` 里，按需懒加载。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Mapping

from trpg_server.combat.contracts import DEFAULT_LEVEL_RANK, DEFAULT_SKIPPED_STATUSES
from trpg_server.ruleset import builtin_registry

logger = logging.getLogger(__name__)


class CombatRulesetUnavailable(LookupError):
    """当前规则集没有战斗实现；调用方应提示「该规则集暂不支持战斗」。"""


_FALLBACK_CACHE: dict[str, Any] = {}


def _fallback_module_map() -> dict[str, str]:
    from trpg_server.ruleset.bootstrap import combat_module_map

    return combat_module_map()


def _fallback_ruleset(ruleset_id: str) -> Any | None:
    if ruleset_id in _FALLBACK_CACHE:
        return _FALLBACK_CACHE[ruleset_id]
    module_path = _fallback_module_map().get(ruleset_id)
    if not module_path:
        return None
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - 兼容实现缺失不应阻断核心
        logger.warning("Combat ruleset module unavailable: %s", module_path, exc_info=True)
        return None
    factory = getattr(module, "COMBAT_RULESET", None)
    if factory is None:
        logger.warning("Combat ruleset module exports no COMBAT_RULESET: %s", module_path)
        return None
    ruleset = factory() if isinstance(factory, type) else factory
    _FALLBACK_CACHE[ruleset_id] = ruleset
    return ruleset


def _plugin_combat(ruleset_id: str) -> Any | None:
    plugin = builtin_registry().get(ruleset_id)
    return getattr(plugin, "combat", None) if plugin is not None else None


def get_ruleset(ruleset_id: str | None = None):
    """按规则集 id 取战斗实现；未找到时回退默认插件的战斗能力。"""
    registry = builtin_registry()
    key = str(ruleset_id or "").strip().lower()
    if not key:
        key = registry.default_id() or ""
    combat = _plugin_combat(key)
    if combat is not None:
        return combat
    fallback = _fallback_ruleset(key)
    if fallback is not None:
        return fallback
    default_id = registry.default_id()
    if default_id:
        combat = _plugin_combat(default_id)
        if combat is not None:
            return combat
        fallback = _fallback_ruleset(default_id)
        if fallback is not None:
            return fallback
    raise CombatRulesetUnavailable(f"规则集 {key or '<none>'} 没有可用的战斗实现")


def list_rulesets() -> list[dict[str, str]]:
    items: dict[str, dict[str, str]] = {}
    registry = builtin_registry()
    for ruleset_id in registry.ids():
        combat = _plugin_combat(ruleset_id)
        if combat is not None:
            items[ruleset_id] = {
                "id": ruleset_id,
                "name": str(getattr(combat, "display_name", ruleset_id)),
            }
    for ruleset_id in _fallback_module_map():
        ruleset = _fallback_ruleset(ruleset_id)
        if ruleset is not None:
            items.setdefault(
                ruleset_id,
                {"id": ruleset_id, "name": str(getattr(ruleset, "display_name", ruleset_id))},
            )
    return list(items.values())


def _capability(name: str, ruleset_id: str | None = None) -> Any | None:
    try:
        ruleset = get_ruleset(ruleset_id)
    except CombatRulesetUnavailable:
        return None
    return getattr(ruleset, name, None)


def level_rank(ruleset_id: str | None = None) -> dict[str, int]:
    """成功等级排序表；插件可用 ``level_rank()`` 提供，缺省用核心默认顺序。"""
    value = _capability("level_rank", ruleset_id)
    table = value() if callable(value) else value
    return dict(table) if isinstance(table, Mapping) else dict(DEFAULT_LEVEL_RANK)


def skipped_statuses(ruleset_id: str | None = None) -> frozenset[str]:
    """失去行动能力的状态集合；插件可用 ``skipped_statuses()`` 覆盖。"""
    value = _capability("skipped_statuses", ruleset_id)
    statuses = value() if callable(value) else value
    if isinstance(statuses, (set, frozenset, list, tuple)):
        return frozenset(str(item) for item in statuses)
    return DEFAULT_SKIPPED_STATUSES


def build_and_damage_bonus(
    str_value: Any, siz_value: Any, ruleset_id: str | None = None
) -> tuple[int, str, list[tuple[int, int]]]:
    """伤害加值/体格；由插件的伤害模型提供，缺失时返回中性值。"""
    handler = _capability("build_and_damage_bonus", ruleset_id)
    if callable(handler):
        try:
            build, bonus, terms = handler(str_value, siz_value)
            return int(build), str(bonus), list(terms or [])
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("build_and_damage_bonus failed", exc_info=True)
    return 0, "", []
