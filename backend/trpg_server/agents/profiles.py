from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trpg_server.ruleset import DEFAULT_RULESET_ID, builtin_registry


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


def ruleset_tool_names(ruleset_id: str | None = None) -> list[str]:
    """当前规则集声明的专属工具（如 CoC7 的检定 / SAN 工具）。"""
    plugin = builtin_registry().resolve(ruleset_id)
    provider = getattr(plugin, "tools", None)
    if provider is None:
        return []
    return [str(name) for name in provider.tool_names() if str(name).strip()]


def build_kp_tool_names(ruleset_id: str | None = None) -> list[str]:
    """按规则集组装 KP 工具：核心工具 + 插件声明的规则专属工具。"""
    names = list(CORE_KP_TOOL_NAMES)
    index = names.index("room.remember_fact") + 1
    extra = [name for name in ruleset_tool_names(ruleset_id) if name not in names]
    names[index:index] = extra
    return names


DEFAULT_KP_TOOLS = build_kp_tool_names(DEFAULT_RULESET_ID)



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
