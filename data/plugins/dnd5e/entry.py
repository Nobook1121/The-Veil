"""D&D 5e（SRD 5.1）规则集插件入口。

插件只做组装：角色卡 schema、检定与 workspace 声明各自独立；核心通过
``ruleset.contracts`` 的协议访问，不认识任何 5e 专属字段或成功等级。
"""

from __future__ import annotations

from dnd5e_plugin.checks import Dnd5eChecks
from dnd5e_plugin.combat import Dnd5eCombatRules
from dnd5e_plugin.content_search import Dnd5eContentSearch
from dnd5e_plugin.effects import Dnd5eEffects
from dnd5e_plugin.house_rules import house_rules as dnd5e_house_rules
from dnd5e_plugin.http import bp
from dnd5e_plugin.rest import Dnd5eRest
from dnd5e_plugin.schema import Dnd5eCharacterSchema
from dnd5e_plugin.spells import Dnd5eSpells
from dnd5e_plugin.tools import Dnd5eTools
from trpg_server.ruleset.contracts import RULESET_API_VERSION
from trpg_server.ruleset.session import ActionBudgetSlot, TurnModel
from trpg_server.ruleset.ui import LeftRailSpec, SidebarSpec, TopbarSpec, WorkspaceSpec


class Dnd5ePlugin:
    id = "dnd5e"
    display_name = "D&D 5e（SRD 5.1）"
    description = "基于 D&D 5e SRD 5.1 的属性检定、豁免、角色卡、战斗、法术专注与状态效果兼容层。"
    api_version = RULESET_API_VERSION
    ruleset_version = "5.1"
    schema_version = 1
    supported_locales = ("zh-CN", "en-US")
    capabilities = frozenset(
        {
            "character_schema",
            "checks",
            "combat",
            "spells",
            "effects",
            "progression",
            "content_search",
        }
    )

    def __init__(self) -> None:
        self.schema = Dnd5eCharacterSchema()
        self.checks = Dnd5eChecks()
        self.effects = Dnd5eEffects()
        self.combat = Dnd5eCombatRules()
        self.spells = Dnd5eSpells(combat=self.combat, checks=self.checks, effects=self.effects)
        # 通用内容检索能力：核心按能力下发，5e 词表与区分字段留在这里。
        self.content_search = Dnd5eContentSearch()
        # 战斗与法术共享同一个专注/法术引擎，避免两套状态。
        self.combat.spells = self.spells
        # 休息/生命骰/调谐能力；CoC7 不声明，核心不会给出这些入口。
        self.rest = Dnd5eRest()
        # 回合调度器通过战斗规则对象读取效果能力（核心不认插件类型）。
        self.combat.effects = self.effects
        # KP 提示词片段与（当前为空的）专属工具声明；核心只负责拼接。
        self.tools = Dnd5eTools()
        # 骰型声明：前端骰桌按此渲染 d20，并高亮自然 1/20。
        self.dice = {
            "type": "d20",
            "sides": 20,
            "expression": "1d20",
            "modes": ["normal", "advantage", "disadvantage"],
            "critical": [20],
            "fumble": [1],
        }
        # 5e 行动经济：动作用/附赠每回合，反应每轮（回合开始恢复），移动按英尺。
        self.turn = TurnModel(
            mode="initiative",
            initiative="roll",
            initiative_formula="1d20 + DEX 调整值",
            economy="slots",
            budget=(
                ActionBudgetSlot(key="action", per_turn=1, refresh="turn", label_i18n="动作"),
                ActionBudgetSlot(key="bonus_action", per_turn=1, refresh="turn", label_i18n="附赠动作"),
                ActionBudgetSlot(key="reaction", per_round=1, refresh="round", label_i18n="反应"),
                ActionBudgetSlot(
                    key="movement",
                    per_turn=30,
                    refresh="turn",
                    label_i18n="移动",
                    detail={"unit": "ft"},
                ),
            ),
            advance="manual",
        )
        # workspace 参数按已定方案声明；前端实际挂载在 E7。
        self.workspace = WorkspaceSpec(
            layout_id="dnd5e-canvas",
            rulesets=("dnd5e",),
            primary="canvas",
            primary_min_width=720,
            sidebar=SidebarSpec(
                position="right",
                width=380,
                min_width=360,
                max_width=420,
                collapsible=True,
                collapsed_width=48,
            ),
            topbar=TopbarSpec(
                height=48,
                slots=("scene", "players", "turn", "status", "settings"),
            ),
            left_rail=LeftRailSpec(width=48, visible=True),
            panels={"chat": "sidebar", "character": "drawer", "combat": "topbar"},
            priority=100,
        )

    def house_rules(self) -> dict[str, dict]:
        """返回 5e 房规 schema；核心只按 type/values/default 做通用校验。"""
        return dnd5e_house_rules()


PLUGIN = Dnd5ePlugin()


def create_tools():
    """插件级 Agent 工具：5e 专属的开战入口（核心不认识具体规则集）。"""
    from dnd5e_plugin.encounter import create_tools as _encounter_tools

    return _encounter_tools()

