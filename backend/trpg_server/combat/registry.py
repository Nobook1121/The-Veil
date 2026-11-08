"""规则书插件注册表。"""

from __future__ import annotations

from .rulesets.coc7 import Coc7Ruleset
from .rulesets.dnd5e import Dnd5eRuleset

_RULESETS = {
    Coc7Ruleset.ruleset_id: Coc7Ruleset(),
    Dnd5eRuleset.ruleset_id: Dnd5eRuleset(),
}


def get_ruleset(ruleset_id: str = "coc7"):
    key = str(ruleset_id or "coc7").strip().lower()
    return _RULESETS.get(key) or _RULESETS["coc7"]


def list_rulesets() -> list[dict[str, str]]:
    return [
        {"id": plugin.ruleset_id, "name": plugin.display_name}
        for plugin in _RULESETS.values()
    ]
