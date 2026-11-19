import logging

from trpg_server.agents.tools.base import AgentTool, ToolRegistry
from trpg_server.agents.tools.dice import DND5E_ROLL_CHECK_TOOL, ROLL_COC_CHECK_TOOL, ROLL_ROOM_CHECK_TOOL, ROLL_DICE_TOOL, ROLL_DICE_FUNCTION_TOOL, ROLL_MADNESS_ALIAS_TOOL, ROLL_MADNESS_TOOL, ROLL_SANITY_CHECK_TOOL, ROLL_SANITY_CHECK_ALIAS_TOOL
from trpg_server.agents.tools.room import (
    GET_CHARACTER_CARDS_TOOL,
    GET_MEMORY_TOOL,
    GET_ROOM_SNAPSHOT_TOOL,
    GET_SCENARIO_CONTEXT_TOOL,
    GET_SCENARIO_MODULE_TOOL,
    ACTIVATE_SCENARIO_SCENE_TOOL,
    REMEMBER_FACT_TOOL,
)
from trpg_server.agents.tools.trigger import REVEAL_SCENARIO_TRIGGER_TOOL
from trpg_server.agents.tools.narrative import NARRATIVE_TOOLS, NARRATIVE_TOOL_NAMES
from trpg_server.agents.tools.narrative_triggers import (
    TRIGGER_RUNTIME_TOOL_NAMES,
    TRIGGER_RUNTIME_TOOLS,
)
from trpg_server.agents.tools.knowledge import SEARCH_RULESET_KNOWLEDGE_TOOL
from trpg_server.agents.tools.content import CONTENT_SEARCH_TOOL
from trpg_server.agents.tools.suggest import SUGGEST_ACTIONS_TOOL
from trpg_server.agents.tools.combat import COMBAT_TOOLS, COMBAT_TOOL_NAMES
from trpg_server.agents.tools.areas import AREA_TOOLS, AREA_TOOL_NAMES
from trpg_server.agents.tools.concentration import (
    CONCENTRATION_TOOLS,
    CONCENTRATION_TOOL_NAMES,
)
from trpg_server.agents.tools.effects import EFFECT_TOOLS, EFFECT_TOOL_NAMES
from trpg_server.agents.tools.reactions import REACTION_TOOLS, REACTION_TOOL_NAMES
from trpg_server.agents.tools.spells import SPELL_TOOLS, SPELL_TOOL_NAMES
from trpg_server.agents.tools.workflow import WORKFLOW_TOOLS, WORKFLOW_TOOL_NAMES
from trpg_server.agents.tools.music import MUSIC_REQUEST_TOOL
from trpg_server.agents.tools.character_items import ADD_CHARACTER_ITEM_TOOL
from trpg_server.agents.tools.coc7 import COC7_EXTRA_TOOL_NAMES, COC7_EXTRA_TOOLS
from trpg_server.agents.tools.chase import CHASE_TOOL_NAMES, CHASE_TOOLS
from trpg_server.agents.tools.magic import (
    MAGIC_CATALOG_TOOL_NAMES,
    MAGIC_CATALOG_TOOLS,
    MAGIC_TOOL_NAMES,
    MAGIC_TOOLS,
)
from trpg_server.agents.tools.sanity import SANITY_TOOL_NAMES, SANITY_TOOLS
from trpg_server.agents.tools.character_stats import (
    SET_CHARACTER_ATTRIBUTE_TOOL,
    SET_CHARACTER_SKILL_TOOL,
)
from trpg_server.agents.tools.interlude import INTERLUDE_TOOLS, INTERLUDE_TOOL_NAMES
from trpg_server.agents.tools.rest import REST_TOOLS, REST_TOOL_NAMES
from trpg_server.agents.tools.time import GAME_CLOCK_TOOL_NAMES, GAME_CLOCK_TOOLS
from trpg_server.agents.tools.props import PROPS_TOOL_NAMES, PROPS_TOOLS


def _core_tools() -> list[AgentTool]:
    """与具体规则集无关的核心工具。

    CoC 命名工具不再无条件混入这里：是否挂载由 :func:`_coc7_tools` 按
    注册表可用性组装（阶段 4 P0）。
    """
    return [
        GET_ROOM_SNAPSHOT_TOOL,
        GET_SCENARIO_CONTEXT_TOOL,
        GET_SCENARIO_MODULE_TOOL,
        ACTIVATE_SCENARIO_SCENE_TOOL,
        GET_CHARACTER_CARDS_TOOL,
        GET_MEMORY_TOOL,
        REMEMBER_FACT_TOOL,
        DND5E_ROLL_CHECK_TOOL,
        ROLL_DICE_TOOL,
        ROLL_DICE_FUNCTION_TOOL,
        REVEAL_SCENARIO_TRIGGER_TOOL,
        SEARCH_RULESET_KNOWLEDGE_TOOL,
        CONTENT_SEARCH_TOOL,
        SUGGEST_ACTIONS_TOOL,
        MUSIC_REQUEST_TOOL,
        ADD_CHARACTER_ITEM_TOOL,
        SET_CHARACTER_SKILL_TOOL,
        SET_CHARACTER_ATTRIBUTE_TOOL,
        *COMBAT_TOOLS,
        *SPELL_TOOLS,
        *EFFECT_TOOLS,
        *WORKFLOW_TOOLS,
        *AREA_TOOLS,
        *CONCENTRATION_TOOLS,
        *REACTION_TOOLS,
        *NARRATIVE_TOOLS,
        *TRIGGER_RUNTIME_TOOLS,
        *INTERLUDE_TOOLS,
        *GAME_CLOCK_TOOLS,
        *PROPS_TOOLS,
        *REST_TOOLS,
    ]


def _coc7_tools() -> list[AgentTool]:
    """CoC 命名工具：coc7 在注册表里可用才挂载（阶段 4 P0 组装点）。

    工具实现仍在核心模块（dice / coc7 / chase / magic / sanity），这里只决定
    「这一组要不要进默认注册表」。coc7 在场时集合与旧实现逐项一致；缺席时
    整组不挂载，同名工具可以让给第三方插件。把这组工具搬进插件包、改为
    插件声明（ToolBundle）是后续批次的事，本批只解除注册耦合。
    """
    try:
        from trpg_server.ruleset import builtin_registry

        if builtin_registry().get("coc7") is None:
            return []
    except Exception:  # noqa: BLE001 - 注册表构建异常不阻断核心工具
        return []
    return [
        ROLL_COC_CHECK_TOOL,
        ROLL_ROOM_CHECK_TOOL,
        ROLL_SANITY_CHECK_TOOL,
        ROLL_SANITY_CHECK_ALIAS_TOOL,
        ROLL_MADNESS_TOOL,
        ROLL_MADNESS_ALIAS_TOOL,
        *COC7_EXTRA_TOOLS,
        *CHASE_TOOLS,
        *MAGIC_TOOLS,
        *MAGIC_CATALOG_TOOLS,
        *SANITY_TOOLS,
    ]


def core_agent_tool_names() -> set[str]:
    """核心 Agent 工具名集合；插件激活时用来拒绝同名覆盖。

    coc7 可用时 CoC 命名工具一体受核心命名保护（与旧实现同集合）；
    缺席时它们不在注册表里，名字自然让出。
    """
    return {str(getattr(tool, "name", "") or "") for tool in _core_tools() + _coc7_tools()}


def default_tool_registry() -> ToolRegistry:
    tools = _core_tools() + _coc7_tools()
    # 第三方插件注册的 Agent 工具（插件未启用时为空列表）。
    try:
        from trpg_server.plugins.runtime import extra_agent_tools

        tools.extend(extra_agent_tools())
    except Exception:  # noqa: BLE001 - 插件钩子故障不影响核心工具
        logging.getLogger(__name__).debug("plugin_agent_tool_hook_failed", exc_info=True)
    return ToolRegistry(tools)


__all__ = [
    "CONTENT_SEARCH_TOOL",
    "COC7_EXTRA_TOOL_NAMES",
    "COC7_EXTRA_TOOLS",
    "CHASE_TOOL_NAMES",
    "CHASE_TOOLS",
    "MAGIC_CATALOG_TOOL_NAMES",
    "MAGIC_CATALOG_TOOLS",
    "MAGIC_TOOL_NAMES",
    "MAGIC_TOOLS",
    "SANITY_TOOL_NAMES",
    "SANITY_TOOLS",
    "COMBAT_TOOLS",
    "COMBAT_TOOL_NAMES",
    "EFFECT_TOOLS",
    "EFFECT_TOOL_NAMES",
    "SPELL_TOOLS",
    "SPELL_TOOL_NAMES",
    "WORKFLOW_TOOLS",
    "WORKFLOW_TOOL_NAMES",
    "AREA_TOOLS",
    "AREA_TOOL_NAMES",
    "CONCENTRATION_TOOLS",
    "CONCENTRATION_TOOL_NAMES",
    "REACTION_TOOLS",
    "REACTION_TOOL_NAMES",
    "INTERLUDE_TOOLS",
    "INTERLUDE_TOOL_NAMES",
    "GAME_CLOCK_TOOLS",
    "GAME_CLOCK_TOOL_NAMES",
    "NARRATIVE_TOOLS",
    "NARRATIVE_TOOL_NAMES",
    "PROPS_TOOLS",
    "PROPS_TOOL_NAMES",
    "REST_TOOLS",
    "REST_TOOL_NAMES",
    "TRIGGER_RUNTIME_TOOL_NAMES",
    "TRIGGER_RUNTIME_TOOLS",
    "core_agent_tool_names",
    "default_tool_registry",
]
