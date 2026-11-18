"""网站 Agent 的规则集分派。

核心不在本模块写死任何规则集 id 或专属工具名：当前规则集由房间/剧本上下文解析，
工具、创作提示词、创作规范与角色卡 schema 全部来自对应插件声明的 profile。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trpg_server.agents import profiles
from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore, search_ruleset
from trpg_server.agents.tools import default_tool_registry
from trpg_server.ruleset import RulesetSelectionError, builtin_registry
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec

# 只读工具：不改变房间规则状态，不需要两段式确认。
_READ_TOOL_NAMES = frozenset(
    {
        "content.search",
        "knowledge.search_ruleset",
        "dice.roll",
        "spell.list",
        "combat.state",
        "room.get_room_snapshot",
        "room.get_scenario_context",
        "room.get_scenario_module",
        "room.get_character_cards",
        "room.get_memory",
    }
)

# 可以在未进入房间时使用的工具；其余规则集工具都要求房间上下文。
_SITE_TOOL_NAMES = frozenset({"content.search", "knowledge.search_ruleset"})


@dataclass(frozen=True)
class SiteAgentRulesetContext(AgentRequestContext):
    """给核心 AgentTool 用的上下文；没有房间时也带上当前规则集。"""

    ruleset_id: str = ""

    def room_info(self) -> dict[str, Any]:
        info = dict(super().room_info())
        if self.ruleset_id:
            info.setdefault("ruleset_id", self.ruleset_id)
        return info


def resolve_current_ruleset(context: ToolContext, explicit: str | None = None):
    """按显式 id、房间、剧本、默认值的顺序解析当前规则集。

    显式 id 不可用时抛 :class:`RulesetSelectionError`，绝不静默回退到其它规则集。
    """
    registry = builtin_registry()
    requested = str(explicit or "").strip()
    if requested:
        return registry.resolve(requested)
    current = str(getattr(context, "ruleset_id", "") or "").strip()
    if current:
        return registry.resolve(current)
    if context.room_info:
        return registry.resolve_for_room(context.room_info)
    scenario_context = context.scenario_context or {}
    scenario_ruleset = str(scenario_context.get("ruleset_id") or "").strip()
    if scenario_ruleset:
        return registry.resolve(scenario_ruleset)
    scenario_id = scenario_context.get("scenario_id")
    if scenario_id not in (None, "", 0):
        try:
            from trpg_server import scenario_service

            scenario = scenario_service.get_for_actor(context.actor, scenario_id)
            scenario_ruleset = str(scenario.get("ruleset_id") or "").strip()
            if scenario_ruleset:
                return registry.resolve(scenario_ruleset)
        except Exception:  # noqa: BLE001 - 剧本读取失败时继续走默认规则集
            pass
    return registry.resolve("")


def site_agent_profile(plugin: Any) -> dict[str, Any]:
    """读取插件声明的网站 Agent profile；没有声明时返回保守的通用结构。"""
    provider = getattr(plugin, "tools", None)
    getter = getattr(provider, "site_agent_profile", None)
    raw = getter() if callable(getter) else {}
    profile = dict(raw) if isinstance(raw, dict) else {}
    profile.setdefault("ruleset_id", str(getattr(plugin, "id", "") or ""))
    profile.setdefault("display_name", str(getattr(plugin, "display_name", "") or profile["ruleset_id"]))
    profile.setdefault("tool_names", [])
    profile.setdefault("prompt", "")
    profile.setdefault("guide_sections", {})
    profile.setdefault("character_prompt", "")
    profile.setdefault("character_schema", {})
    return profile


def _tool_names(plugin: Any, profile: dict[str, Any]) -> list[str]:
    """合并插件 profile、能力声明与插件 agent_tools 的工具名。"""
    names: list[str] = [str(name) for name in profile.get("tool_names") or [] if str(name).strip()]
    capabilities = (
        profiles.spell_tool_names(plugin.id),
        profiles.effect_tool_names(plugin.id),
        profiles.rest_tool_names(plugin.id),
        profiles.workflow_tool_names(plugin.id),
        profiles.plugin_tool_names(plugin.id),
    )
    for group in capabilities:
        names.extend(group)
    names.extend(["knowledge.search_ruleset", "dice.roll"])
    if getattr(plugin, "content_search", None) is not None:
        names.append("content.search")
    if not any(name.startswith("combat.") for name in names):
        names.extend(["combat.start", "combat.state", "combat.next", "combat.attack", "combat.damage", "combat.end"])
    deduped: list[str] = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _risk(name: str) -> str:
    if name in _READ_TOOL_NAMES or name.startswith(("knowledge.", "content.")):
        return "read"
    if name.startswith("spell.") and name.endswith(".list"):
        return "read"
    return "write"


def _build_context(context: ToolContext, ruleset_id: str) -> SiteAgentRulesetContext:
    from flask import current_app

    from trpg_server.settings import CONFIG_DIR, SCENARIOS_DIR

    config_dir = Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))
    rooms_dir = Path(current_app.config.get("ROOMS_DIR", "")) if current_app.config.get("ROOMS_DIR") else None
    agent_context = SiteAgentRulesetContext(
        room_id=context.room_id,
        room_dir=context.room_dir(),
        scenarios_dir=scenarios_dir,
        config_dir=config_dir,
        user_id=getattr(context.actor, "user_id", None),
        agent_id="site_agent",
        request_content="",
        ruleset_id=ruleset_id,
    )
    if context.room_id:
        store = RulesetKnowledgeStore(
            config_dir / "ruleset_knowledge",
            rooms_dir=rooms_dir,
        )
        agent_context.tool_state["ruleset_search"] = lambda query, top_k=3: search_ruleset(
            context.room_id,
            query,
            store=store,
            ruleset_id=ruleset_id,
            top_k=top_k,
        )
    return agent_context


def _tool_result(tool: Any, raw: Any) -> ToolResult:
    if isinstance(raw, dict):
        error = raw.get("error")
        if error or raw.get("ok") is False:
            return ToolResult.failure(str(error or "规则集工具执行失败"))
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            summary = f"已执行 {tool.name}"
        return ToolResult.success(raw, summary=summary)
    return ToolResult.success(raw, summary=f"已执行 {tool.name}")


def _call_tool(tool: Any, arguments: dict[str, Any], context: SiteAgentRulesetContext) -> ToolResult:
    try:
        return _tool_result(tool, tool.handler(arguments, context))
    except Exception as exc:  # noqa: BLE001 - 工具失败要返回可读原因，不能让整轮崩掉
        return ToolResult.failure(f"{tool.name} 执行失败：{exc}")


def _spec_for_tool(tool: Any, context: ToolContext, ruleset_id: str) -> ToolSpec:
    risk = _risk(tool.name)
    scope = "site" if tool.name in _SITE_TOOL_NAMES else "room"

    def handler(actor: Any, arguments: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return _call_tool(tool, arguments, _build_context(_ctx, ruleset_id))

    if risk == "read":
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
            risk="read",
            scope=scope,
            required_permission=None,
            handler=handler,
        )

    def preview(actor: Any, arguments: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult.success(
            {"apply_args": arguments, "tool": tool.name, "arguments": arguments},
            summary=f"待确认执行：{tool.description.splitlines()[0][:60]}",
        )

    def apply(actor: Any, arguments: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return handler(actor, arguments, _ctx)

    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        risk="write",
        scope=scope,
        required_permission=None,
        preview=preview,
        apply=apply,
    )


def ruleset_tool_specs(plugin: Any, context: ToolContext) -> list[ToolSpec]:
    """构建当前规则集插件声明的网站 Agent 工具。"""
    profile = site_agent_profile(plugin)
    registry = default_tool_registry()
    specs: list[ToolSpec] = []
    for name in _tool_names(plugin, profile):
        tool = registry.get(name)
        if tool is None:
            continue
        specs.append(_spec_for_tool(tool, context, plugin.id))
    return specs


def resolve_ruleset_spec(name: str, *, actor: Any, config: Any, room_id: str | None) -> ToolSpec | None:
    """为已确认的待确认操作重新解析动态规则集工具。"""
    from trpg_server.json_store import read_json

    context = ToolContext(
        actor=actor,
        config=config,
        conversation_id="",
        turn_id="",
        room_id=room_id,
        room_info=None,
    )
    if room_id:
        room_dir = context.room_dir()
        if room_dir:
            info = read_json(room_dir / "info.json", default={})
            context.room_info = info if isinstance(info, dict) else {}
    try:
        plugin = resolve_current_ruleset(context)
    except RulesetSelectionError:
        return None
    for spec in ruleset_tool_specs(plugin, context):
        if spec.name == name:
            return spec
    return None
