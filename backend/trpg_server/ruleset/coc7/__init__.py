"""CoC7 规则书插件入口。

插件只做组装：schema / 目录 / 判定 / 战斗 / 工具各自独立，
核心通过 ``ruleset.contracts`` 的协议访问，不 import 本包内部实现。
"""

from __future__ import annotations

import logging
from typing import Any

from trpg_server.ruleset.contracts import RULESET_API_VERSION
from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs
from trpg_server.ruleset.coc7.checks import Coc7Checks
from trpg_server.ruleset.coc7 import growth as coc7_growth
from trpg_server.ruleset.coc7.schema import Coc7CharacterSchema
from trpg_server.ruleset.coc7.tools import Coc7Tools

logger = logging.getLogger(__name__)


def _load_combat_ruleset() -> Any | None:
    """复用已插件化的战斗规则；导入失败时降级为无战斗能力，不影响其它功能。"""
    try:
        from trpg_server.combat.rulesets.coc7 import Coc7Ruleset

        return Coc7Ruleset()
    except Exception:  # pragma: no cover - 仅在战斗包损坏时触发
        logger.exception("Failed to load CoC7 combat ruleset")
        return None


class Coc7Plugin:
    id = "coc7"
    display_name = "克苏鲁的呼唤 7e"
    api_version = RULESET_API_VERSION
    ruleset_version = "1.0"
    schema_version = 1
    supported_locales = ("zh-CN", "en-US")
    capabilities = frozenset(
        {"character_schema", "catalogs", "checks", "combat", "sanity", "items", "migration"}
    )

    def __init__(self) -> None:
        self.schema = Coc7CharacterSchema()
        self.catalogs = Coc7Catalogs()
        self.checks = Coc7Checks()
        self.tools = Coc7Tools()
        # 成长能力：纯计算模块，供通用层按规则集能力访问，不进入注册表结构。
        self.growth = coc7_growth
        self.combat = _load_combat_ruleset()
        if self.combat is None:
            self.capabilities = self.capabilities - {"combat"}
