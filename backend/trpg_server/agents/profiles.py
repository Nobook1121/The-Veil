from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trpg_server.ruleset import builtin_registry


# 图谱模式：隐藏 redundant 场景模块工具，追加叙事运行工具（§5.6）。
GRAPH_EXCLUDED_TOOLS = {
    "room.get_scenario_module",
    "room.activate_scenario_scene",
    "trigger.reveal_scenario_trigger",
}
GRAPH_TOOL_NAMES = [
    "narrative.get_current_location",
    "narrative.lookup_location",
    "narrative.list_exits",
    "narrative.move_to_location",
    "narrative.get_area",
    "narrative.get_poi",
    "narrative.travel_to",
    "narrative.inspect_object",
    "narrative.resolve_interaction",
    "narrative.reveal_clue",
    "narrative.update_npc_state",
    "narrative.write_fact",
    "trigger.list_available",
    "trigger.request",
]

CORE_KP_TOOL_NAMES = [
    "room.get_room_snapshot",
    "room.get_scenario_context",
    "room.get_scenario_module",
    "room.activate_scenario_scene",
    "room.get_character_cards",
    "room.get_memory",
    "room.remember_fact",
    "room.request_interlude",
    "room.save_interlude_review",
    "dice.roll",
    "dice.roll_dice",
    "trigger.reveal_scenario_trigger",
    "knowledge.search_ruleset",
    "room.suggest_actions",
    "music.request_track",
    "character.add_item",
    "character.set_skill",
    "character.set_attribute",
    "combat.start",
    "combat.state",
    "combat.next",
    "combat.attack",
    "combat.shoot",
    "combat.damage",
    "combat.treat",
    "combat.end",
]


def _resolve_plugin(ruleset_id: str | None):
    """显式规则集不可用时抛错；只有「未指定规则集」才允许空降级。"""
    from trpg_server.ruleset import RulesetSelectionError

    try:
        return builtin_registry().resolve(ruleset_id)
    except RulesetSelectionError:
        if str(ruleset_id or "").strip():
            raise
        return None


def ruleset_tool_names(ruleset_id: str | None = None) -> list[str]:
    """当前规则集声明的专属工具（如 CoC7 的检定 / SAN 工具）。"""
    plugin = _resolve_plugin(ruleset_id)
    provider = getattr(plugin, "tools", None)
    if provider is None:
        return []
    return [str(name) for name in provider.tool_names() if str(name).strip()]


def combat_budget_tool_names(ruleset_id: str | None = None) -> list[str]:
    """多槽行动经济（动作/附赠/反应/移动）才下发通用扣减工具。"""
    from trpg_server.ruleset.session import turn_model_of

    plugin = _resolve_plugin(ruleset_id)
    if plugin is None:
        return []
    model = turn_model_of(plugin)
    return ["combat.spend"] if len(model.budget) > 1 else []


def spell_tool_names(ruleset_id: str | None = None) -> list[str]:
    """声明了 ``spells`` 能力的规则集才下发施法工具。"""
    plugin = _resolve_plugin(ruleset_id)
    if plugin is None or getattr(plugin, "spells", None) is None:
        return []
    return ["spell.list", "spell.cast", "spell.concentration"]


def effect_tool_names(ruleset_id: str | None = None) -> list[str]:
    """声明了 ``effects`` 能力的规则集才下发状态与效果工具。"""
    plugin = _resolve_plugin(ruleset_id)
    if plugin is None or getattr(plugin, "effects", None) is None:
        return []
    return ["effect.list", "effect.apply", "effect.remove", "scene.advance"]


def rest_tool_names(ruleset_id: str | None = None) -> list[str]:
    """声明了 ``rest`` 能力的规则集才下发休息/同调工具。"""
    plugin = _resolve_plugin(ruleset_id)
    if plugin is None or getattr(plugin, "rest", None) is None:
        return []
    from trpg_server.agents.tools.rest import REST_TOOL_NAMES

    return list(REST_TOOL_NAMES)


def workflow_tool_names(ruleset_id: str | None = None) -> list[str]:
    """自动化工具由规则集插件声明；核心不写死任何规则集 id。"""
    plugin = _resolve_plugin(ruleset_id)
    provider = getattr(plugin, "tools", None) if plugin is not None else None
    getter = getattr(provider, "workflow_tool_names", None)
    if not callable(getter):
        return []
    return [str(name) for name in getter() if str(name).strip()]


def ruleset_kp_tool_policy(ruleset_id: str | None = None) -> tuple[frozenset[str], tuple[str, ...]]:
    """规则集对 KP 工具清单的 allow/deny 声明；未声明时不做任何裁剪。

    返回值 ``(deny, allow)``：``deny`` 从最终清单移除，``allow`` 补充到清单末尾。
    核心只执行声明，不解释任何规则集语义，也不写死规则集 id。
    """
    plugin = _resolve_plugin(ruleset_id)
    provider = getattr(plugin, "tools", None) if plugin is not None else None
    getter = getattr(provider, "kp_tool_policy", None)
    if not callable(getter):
        return frozenset(), ()
    policy = getter() or {}
    deny = frozenset(
        str(name).strip()
        for name in (policy.get("deny") or ())
        if str(name).strip()
    )
    allow = tuple(
        str(name).strip()
        for name in (policy.get("allow") or ())
        if str(name).strip()
    )
    return deny, allow


def plugin_tool_names(ruleset_id: str | None = None) -> list[str]:
    """插件注册的 Agent 工具；按工具声明的 ``rulesets`` 过滤。

    空元组表示工具对所有规则集通用；非空时只下发给包含当前规则集的房间。
    惰性 import runtime，避免核心 profiles 与插件运行时互相导入。
    """
    try:
        from trpg_server.plugins.runtime import extra_agent_tools

        tools = extra_agent_tools()
    except Exception:  # noqa: BLE001 - 插件钩子故障不影响核心工具
        return []
    current = str(ruleset_id or "").strip().lower()
    names: list[str] = []
    for tool in tools:
        scope = tuple(
            str(item).strip().lower()
            for item in (getattr(tool, "rulesets", ()) or ())
            if str(item).strip()
        )
        if scope and current not in scope:
            continue
        name = str(getattr(tool, "name", "") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def build_kp_tool_names(ruleset_id: str | None = None) -> list[str]:
    """按规则集组装 KP 工具：核心工具 + 插件声明 + 规则的 allow/deny 裁剪。"""
    names = list(CORE_KP_TOOL_NAMES)
    index = names.index("room.remember_fact") + 1
    extra = [
        name
        for name in (
            *ruleset_tool_names(ruleset_id),
            *combat_budget_tool_names(ruleset_id),
            *spell_tool_names(ruleset_id),
            *effect_tool_names(ruleset_id),
            *rest_tool_names(ruleset_id),
            *workflow_tool_names(ruleset_id),
            *plugin_tool_names(ruleset_id),
        )
        if name not in names
    ]
    names[index:index] = extra
    deny, allow = ruleset_kp_tool_policy(ruleset_id)
    if deny:
        names = [name for name in names if name not in deny]
    for name in allow:
        if name not in names:
            names.append(name)
    return names


# 默认插件在加载完成后即确定；None 表示「注册表默认规则集」。
DEFAULT_KP_TOOLS = build_kp_tool_names()



@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    prompt: str
    provider: str | None = None
    wake_words: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    context_providers: list[str] = field(default_factory=list)


def expand_graph_tool_names(tool_names: list[str], *, graph_enabled: bool) -> list[str]:
    """图谱模式替换 redundant 工具；普通房间保持原工具集不变。"""
    if not graph_enabled:
        return list(tool_names)
    names = [name for name in tool_names if name not in GRAPH_EXCLUDED_TOOLS]
    for name in GRAPH_TOOL_NAMES:
        if name not in names:
            names.append(name)
    return names


def with_graph_tools(profile: AgentProfile, *, graph_enabled: bool) -> AgentProfile:
    if not graph_enabled:
        return profile
    from dataclasses import replace

    return replace(profile, tool_names=expand_graph_tool_names(profile.tool_names, graph_enabled=True))


def resolve_agent_profile(
    role: dict[str, Any],
    prompt_file: Path,
    *,
    prefer_prompt_file: bool = False,
    ruleset_id: str | None = None,
) -> AgentProfile:
    role_id = str(role.get("id") or "kp")
    prompt = str(role.get("prompt") or "")
    if role_id == "kp" and prompt_file.exists() and (prefer_prompt_file or not prompt):
        prompt = prompt_file.read_text(encoding="utf-8")

    configured_tools = role.get("tools")
    if isinstance(configured_tools, list):
        tool_names = [str(name) for name in configured_tools if str(name).strip()]
    elif role_id == "kp":
        tool_names = build_kp_tool_names(ruleset_id)
    else:
        tool_names = []

    configured_context = role.get("context")
    context_providers = (
        [str(name) for name in configured_context if str(name).strip()]
        if isinstance(configured_context, list)
        else []
    )

    return AgentProfile(
        id=role_id,
        name=str(role.get("name") or role_id),
        prompt=prompt,
        provider=role.get("provider"),
        wake_words=[str(word) for word in role.get("wake_words", [])],
        tool_names=tool_names,
        context_providers=context_providers,
    )
