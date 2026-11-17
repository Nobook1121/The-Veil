"""规则书插件包（只允许内置插件）。"""

from trpg_server.ruleset.check_outcome import (
    CHECK_OUTCOME_VERSION,
    CheckOutcome,
    coerce_outcome,
    from_resolution,
)
from trpg_server.ruleset.contracts import (
    RULESET_API_VERSION,
    CheckOutcomeProvider,
    CheckRequest,
    CheckResolution,
    RulesetPlugin,
)
from trpg_server.ruleset.registry import (
    STATUS_AVAILABLE,
    STATUS_DEVELOPMENT,
    STATUS_INCOMPATIBLE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    DeclaredRuleset,
    RulesetRegistry,
    RulesetResolution,
    RulesetSelectionError,
    builtin_registry,
    default_ruleset_id,
    get_plugin,
    reset_builtin_registry,
)

__all__ = [
    "CHECK_OUTCOME_VERSION",
    "RULESET_API_VERSION",
    "STATUS_AVAILABLE",
    "STATUS_DEVELOPMENT",
    "STATUS_INCOMPATIBLE",
    "STATUS_UNAVAILABLE",
    "STATUS_UNKNOWN",
    "CheckOutcome",
    "CheckOutcomeProvider",
    "CheckRequest",
    "CheckResolution",
    "DeclaredRuleset",
    "RulesetPlugin",
    "RulesetRegistry",
    "RulesetResolution",
    "RulesetSelectionError",
    "builtin_registry",
    "coerce_outcome",
    "default_ruleset_id",
    "from_resolution",
    "get_plugin",
    "reset_builtin_registry",
]
