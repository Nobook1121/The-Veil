"""规则书插件包（只允许内置插件）。"""

from trpg_server.ruleset.contracts import (
    DEFAULT_RULESET_ID,
    RULESET_API_VERSION,
    CheckRequest,
    CheckResolution,
    RulesetPlugin,
)
from trpg_server.ruleset.registry import (
    STATUS_AVAILABLE,
    STATUS_DEVELOPMENT,
    DeclaredRuleset,
    RulesetRegistry,
    RulesetSelectionError,
    builtin_registry,
    get_plugin,
)

__all__ = [
    "DEFAULT_RULESET_ID",
    "RULESET_API_VERSION",
    "STATUS_AVAILABLE",
    "STATUS_DEVELOPMENT",
    "CheckRequest",
    "CheckResolution",
    "DeclaredRuleset",
    "RulesetPlugin",
    "RulesetRegistry",
    "RulesetSelectionError",
    "builtin_registry",
    "get_plugin",
]
