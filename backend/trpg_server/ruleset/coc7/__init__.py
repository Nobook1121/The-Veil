"""CoC7 规则书插件入口。

插件只做组装：schema / 目录 / 判定 / 战斗 / 工具各自独立，
核心通过 ``ruleset.contracts`` 的协议访问，不 import 本包内部实现。
"""

from __future__ import annotations

import logging
from typing import Any

from trpg_server.dice_policy import PercentileRollPolicy
from trpg_server.ruleset.contracts import RULESET_API_VERSION
from trpg_server.ruleset.coc7.house_rules import coc7_house_rule_definitions
from trpg_server.ruleset.session import ActionBudgetSlot, TurnModel
from trpg_server.ruleset.ui import LeftRailSpec, SidebarSpec, TopbarSpec, WorkspaceSpec
from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs
from trpg_server.ruleset.coc7.checks import Coc7Checks
from trpg_server.ruleset.coc7 import growth as coc7_growth
from trpg_server.ruleset.coc7 import combat_rules as coc7_combat_rules
from trpg_server.ruleset.coc7 import magic_content as coc7_magic_content
from trpg_server.ruleset.coc7 import options as coc7_options
from trpg_server.ruleset.coc7 import training as coc7_training
from trpg_server.ruleset.coc7.sanity import SANITY
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
    description = "基于《克苏鲁的呼唤》第七版规则管理调查员的属性、技能、装备、伤势与理智记录。"
    api_version = RULESET_API_VERSION
    ruleset_version = "1.0"
    schema_version = 1
    supported_locales = ("zh-CN", "en-US")
    capabilities = frozenset(
        {"character_schema", "catalogs", "checks", "combat", "sanity", "items", "migration", "combat_rules"}
    )

    def __init__(self) -> None:
        self.schema = Coc7CharacterSchema()
        self.catalogs = Coc7Catalogs()
        self.checks = Coc7Checks(self.catalogs)
        self.tools = Coc7Tools()
        # 成长能力：纯计算模块，供通用层按规则集能力访问，不进入注册表结构。
        self.growth = coc7_growth
        # 幕间训练（批次 2 可选规则）：同样是纯计算模块。
        self.training = coc7_training
        # 房间级房规开关解析（Coc7RuleOptions）：工具层经能力层取用，
        # 核心 agents/tools 不再直接 import 本包内部模块（阶段 4 P0）。
        self.options = coc7_options
        # 法术 / 典籍内容表（批次 6 按需查询）：同样作为能力暴露。
        self.magic_content = coc7_magic_content
        # 本规则集使用的骰子策略：核心只调抽象接口，不认识十位/个位。
        self.roll_policy = PercentileRollPolicy()
        # 骰型声明：前端骰桌据此保持百分骰，不因规则集切换显示 d20。
        self.dice = {
            "type": "d100",
            "sides": 100,
            "expression": "1d100",
            "modes": ["normal", "bonus", "penalty"],
            "critical": [1],
            "fumble": [96, 97, 98, 99, 100],
        }
        # 理智 / 疯狂：上限、1/5 阈值、INT 检定与疯狂表现表都在插件内。
        self.sanity = SANITY
        # 战斗细节（批次 4）：瞄准、掩体闪避、误伤、击晕、部位命中的纯规则。
        self.combat_rules = coc7_combat_rules
        self.combat = _load_combat_ruleset()
        if self.combat is None:
            self.capabilities = self.capabilities - {"combat"}
        # 房规 schema 由规则集自己声明；核心只做命名空间、校验与读写。
        self.house_rules_definitions = coc7_house_rule_definitions
        # 通用会话壳：只声明 CoC7 的回合事实，回合逻辑仍在现有战斗模块里。
        self.turn = TurnModel(
            mode="round",
            initiative="dexterity",
            initiative_formula="DEX（已准备武器 +50）",
            economy="slots",
            budget=(ActionBudgetSlot(key="action", per_round=1, refresh="round"),),
            advance="manual",
        )
        # 通用 UI 壳：coc7 继续聊天主列 + 260px 成员栏，不启用 dnd5e 的布局数值。
        self.workspace = WorkspaceSpec(
            layout_id="coc7-conversation",
            rulesets=("coc7",),
            primary="conversation",
            primary_min_width=480,
            sidebar=SidebarSpec(
                position="right",
                width=260,
                min_width=240,
                max_width=420,
                collapsible=False,
                collapsed_width=48,
            ),
            topbar=TopbarSpec(height=0, slots=()),
            left_rail=LeftRailSpec(width=0, visible=False),
            panels={"chat": "primary", "character": "sidebar", "combat": "inline"},
        )


# 插件登记入口：装配点按模块路径导入本包后取这个实例。
PLUGIN = Coc7Plugin()
