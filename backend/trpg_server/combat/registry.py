"""战斗规则注册表。

对外仍暴露 ``get_ruleset`` / ``list_rulesets``，内部优先转发到规则书插件注册表：
插件通过 ``combat`` 能力提供实现。尚未插件化的兼容实现保留在本地回退表里，
避免影响既有战斗流程。
"""

from __future__ import annotations

from trpg_server.ruleset import builtin_registry

from .rulesets.coc7 import Coc7Ruleset
from .rulesets.dnd5e import Dnd5eRuleset

# 兼容回退：插件未声明 combat 能力时使用。dnd5e 仅保留既有实现，规则选择入口
# 由规则注册表标记为「开发中」，不可选、不可导入。
_COMBAT_FALLBACKS = {
    Coc7Ruleset.ruleset_id: Coc7Ruleset(),
    Dnd5eRuleset.ruleset_id: Dnd5eRuleset(),
}


def _plugin_combat(ruleset_id: str) -> object | None:
    plugin = builtin_registry().get(ruleset_id)
    return getattr(plugin, "combat", None) if plugin is not None else None


def get_ruleset(ruleset_id: str = "coc7"):
    key = str(ruleset_id or "coc7").strip().lower()
    combat = _plugin_combat(key)
    if combat is not None:
        return combat
    return _COMBAT_FALLBACKS.get(key) or _COMBAT_FALLBACKS["coc7"]


def list_rulesets() -> list[dict[str, str]]:
    items: dict[str, dict[str, str]] = {}
    for ruleset_id in builtin_registry().ids():
        combat = _plugin_combat(ruleset_id)
        if combat is not None:
            items[ruleset_id] = {
                "id": ruleset_id,
                "name": str(getattr(combat, "display_name", ruleset_id)),
            }
    for ruleset_id, plugin in _COMBAT_FALLBACKS.items():
        items.setdefault(
            ruleset_id,
            {"id": ruleset_id, "name": str(getattr(plugin, "display_name", ruleset_id))},
        )
    return list(items.values())
