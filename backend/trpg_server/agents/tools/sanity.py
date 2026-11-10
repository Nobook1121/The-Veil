"""CoC7 批次 3 理智结构化工具：发作 / 习惯恐惧 / 现实认知 / 恐惧躁狂表。

规则数学在 :mod:`trpg_server.ruleset.coc7.sanity` 与 ``sanity_state``，
状态在 :mod:`trpg_server.room_sanity`；本模块只做参数搬运与开关校验。
恐惧症 / 躁狂症表按需查询，工具不会把整表注入上下文。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_rules, room_sanity
from trpg_server.agents.tools.base import AgentTool
from trpg_server.ruleset.coc7.options import resolve_options


def _options(context: Any):
    return resolve_options(context.room_info())


def _member(context: Any, name: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    info = context.room_info()
    member = room_rules.find_member(info, str(name or "").strip())
    if not member:
        raise room_rules.RuleOperationError(f"找不到玩家 {name}", code="unknown_member")
    return info, member


def _actor(context: Any) -> str:
    return getattr(context, "username", None) or "AI KP"


def _disabled(feature: str) -> dict[str, Any]:
    return {"ok": False, "error": f"coc7 sanity house rule is disabled: {feature}"}


def execute_san_bout(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).sanity_state:
        return _disabled("coc7_sanity_state")
    info = context.room_info()
    try:
        if str(arguments.get("player") or "").strip():
            _info, member = _member(context, arguments.get("player"))
            return {"ok": True, "bout": room_sanity.mental_snapshot(member, info).get("bout") or {}}
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    bouts = []
    for item in info.get("members") or []:
        if not isinstance(item, dict) or not item.get("character_card"):
            continue
        try:
            snapshot = room_sanity.mental_snapshot(item, info)
        except room_rules.RuleOperationError:
            continue
        if snapshot.get("activeBout"):
            bouts.append({"player": room_rules.member_display_name(item), "bout": snapshot.get("bout")})
    return {"ok": True, "active_bouts": bouts}


def execute_san_habituation(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    options = _options(context)
    if not options.sanity_state:
        return _disabled("coc7_sanity_state")
    try:
        info, member = _member(context, arguments.get("player"))
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    action = str(arguments.get("action") or "status").strip().lower()
    try:
        if action == "record":
            if not options.sanity_habituation:
                return _disabled("coc7_sanity_habituation")
            payload = room_sanity.record_habituation(
                context.room_dir,
                info,
                member,
                source=str(arguments.get("source") or ""),
                loss=int(arguments.get("loss") or 0),
                cap=int(arguments["cap"]) if arguments.get("cap") is not None else None,
                actor=_actor(context),
                actor_id=getattr(context, "user_id", None),
            )
            return {"ok": True, "result": payload}
        if action in {"interlude", "tick", "decay"}:
            if not options.sanity_habituation:
                return _disabled("coc7_sanity_habituation")
            payload = room_sanity.interlude_decay(context.room_dir, info, member, reason="幕间（AI KP）")
            return {"ok": True, "result": payload}
        return {"ok": True, "player": room_rules.member_display_name(member), "habituations": room_sanity.habituation_rows(member, info)}
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}


def execute_san_reality_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    options = _options(context)
    if not options.sanity_state:
        return _disabled("coc7_sanity_state")
    if not options.sanity_reality:
        return _disabled("coc7_sanity_reality")
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_sanity.reality_check(
            context.room_dir,
            info,
            member,
            result=arguments.get("result"),
            source=str(arguments.get("source") or ""),
            actor=_actor(context),
            actor_id=getattr(context, "user_id", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "reality_check": payload}


def execute_san_traits(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).sanity_traits:
        return _disabled("coc7_sanity_traits")
    payload = room_sanity.trait_query(
        arguments.get("kind"),
        arguments.get("keyword"),
        limit=int(arguments.get("limit") or 10),
        roll=bool(arguments.get("roll")),
    )
    return {"ok": True, **payload}


SAN_BOUT_TOOL = AgentTool(
    name="san.bout",
    description=(
        "Read the current bout of madness (type, symptoms, and its duration: 1D10 combat rounds "
        "for immediate symptoms or 1D10 hours for summary symptoms) for one investigator or all "
        "active bouts in the room. Returns no game math."
    ),
    parameters={
        "type": "object",
        "properties": {"player": {"type": "string", "description": "Omit to list every active bout."}},
    },
    handler=execute_san_bout,
)

SAN_HABITUATION_TOOL = AgentTool(
    name="san.habituation",
    description=(
        "Track CoC7 habituation. status: list cumulative SAN loss per source; record: add a loss "
        "from a source and get the applied/waived amount; interlude: reduce every source's cumulative "
        "loss by 1. Pass cap (the maximum single-encounter loss for that being, e.g. 6 for 0/1D6) when "
        "known. Only call when the room enables the habituation rule."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string"},
            "action": {"type": "string", "enum": ["status", "record", "interlude"]},
            "source": {"type": "string", "description": "Creature or phenomenon name, used as the key."},
            "loss": {"type": "integer", "description": "SAN loss just suffered from that source."},
            "cap": {"type": "integer", "description": "Maximum single-encounter SAN loss for that source (e.g. 6 for 0/1D6)."},
        },
        "required": ["player"],
    },
    handler=execute_san_habituation,
)

SAN_REALITY_CHECK_TOOL = AgentTool(
    name="san.reality_check",
    description=(
        "Resolve a CoC7 reality check with a Sanity roll (target = current SAN). Success grants "
        "resistance to hallucinations until the next real Sanity loss; failure costs 1 SAN and "
        "starts a bout of madness. Pass result only when the player rolled physically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string"},
            "source": {"type": "string", "description": "What the illusion claims to be."},
            "result": {
                "type": "string",
                "enum": ["critical", "extreme", "hard", "regular", "failure", "fumble"],
                "description": "Optional explicit POW result from a physical roll.",
            },
        },
        "required": ["player"],
    },
    handler=execute_san_reality_check,
)

SAN_TRAITS_TOOL = AgentTool(
    name="san.traits",
    description=(
        "Query the 100-entry CoC7 phobia/mania tables on demand. Never list the whole table: pass "
        "keyword to search, or roll=true to draw one entry when madness demands a new phobia/mania."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["phobia", "mania"], "description": "Omit to search both."},
            "keyword": {"type": "string", "description": "Chinese/English name, tag or description fragment."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "roll": {"type": "boolean", "description": "Draw one random entry instead of searching."},
        },
    },
    handler=execute_san_traits,
)

SANITY_TOOLS = [SAN_BOUT_TOOL, SAN_HABITUATION_TOOL, SAN_REALITY_CHECK_TOOL, SAN_TRAITS_TOOL]
SANITY_TOOL_NAMES = {tool.name for tool in SANITY_TOOLS}
