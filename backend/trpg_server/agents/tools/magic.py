"""CoC7 批次 5 法术 / 典籍工具。

规则数学在 :mod:`trpg_server.ruleset.coc7.magic`，状态在
:mod:`trpg_server.room_magic`；本模块只做参数搬运与房规开关校验。
不内置法术表：法术名与消耗由 KP 传入。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_magic, room_rules
from trpg_server.agents.tools.base import AgentTool


def _options(context: Any):
    from trpg_server.ruleset.coc7.options import resolve_options

    return resolve_options(context.room_info())


def _disabled() -> dict[str, Any]:
    return {"ok": False, "error": "coc7 magic house rule is disabled"}


def _member(context: Any, name: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """一次读取房间信息，避免成员对象与落盘用的 info 来自两份拷贝。"""
    info = context.room_info()
    member = room_rules.find_member(info, str(name or "").strip())
    if not member:
        raise room_rules.RuleOperationError(f"找不到玩家 {name}", code="unknown_member")
    return info, member


def _actor(context: Any) -> str:
    return getattr(context, "username", None) or "AI KP"


def execute_magic_status(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).magic_enabled:
        return _disabled()
    try:
        _info, member = _member(context, arguments.get("player"))
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "magic": room_magic.magic_snapshot(member)}


def execute_magic_learn(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).magic_enabled:
        return _disabled()
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_magic.learn_spell(
            context.room_dir,
            info,
            member,
            arguments.get("spell"),
            weeks=arguments.get("weeks") or 0,
            source=str(arguments.get("source") or "book"),
            actor=_actor(context),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "learned": payload, "magic": room_magic.magic_snapshot(member)}


def _first_cast_level(arguments: dict[str, Any], context: Any, member: dict[str, Any]) -> str:
    explicit = str(arguments.get("result") or "").strip().lower()
    if explicit:
        return explicit
    check = room_rules.roll_check(
        context.room_dir,
        context.room_info(),
        member,
        name="POW",
        difficulty="hard",
        reason="首次施法",
        actor=_actor(context),
        actor_id=getattr(context, "user_id", None),
        config_dir=getattr(context, "config_dir", None),
        room_id=getattr(context, "room_id", None),
    )
    return str(check.get("success_level") or "failure")


def execute_magic_cast(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).magic_enabled:
        return _disabled()
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_magic.cast_spell(
            context.room_dir,
            info,
            member,
            name=arguments.get("spell"),
            mp_cost=arguments.get("mp_cost") or 0,
            san_cost=arguments.get("san_cost") or 0,
            pow_cost=arguments.get("pow_cost") or 0,
            first_cast=bool(arguments.get("first_cast")),
            success_level=(
                _first_cast_level(arguments, context, member)
                if arguments.get("first_cast")
                or room_magic.needs_first_cast(member, arguments.get("spell"))
                or arguments.get("result")
                else None
            ),
            pushed=bool(arguments.get("pushed")),
            multiplier=arguments.get("multiplier") or 1,
            side_effect_roll=arguments.get("side_effect_roll") or 1,
            actor=_actor(context),
            actor_id=getattr(context, "user_id", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "cast": payload, "magic": room_magic.magic_snapshot(member)}


def execute_magic_grimoire(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).magic_enabled:
        return _disabled()
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_magic.read_grimoire(
            context.room_dir,
            info,
            member,
            title=arguments.get("title"),
            mode=str(arguments.get("mode") or "study"),
            weeks=arguments.get("weeks") or 0,
            san_loss=arguments.get("san_loss") or 0,
            cmi=arguments.get("cmi") or 0,
            cmf=arguments.get("cmf") or 0,
            rating=arguments.get("rating") or 0,
            success_level=arguments.get("result"),
            spells=arguments.get("spells") or [],
            actor=_actor(context),
            actor_id=getattr(context, "user_id", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "grimoire": payload, "magic": room_magic.magic_snapshot(member)}


MAGIC_STATUS_TOOL = AgentTool(
    name="magic.status",
    description="Read a CoC7 investigator's learned spells, grimoires, MP and POW.",
    parameters={
        "type": "object",
        "properties": {"player": {"type": "string"}},
        "required": ["player"],
    },
    handler=execute_magic_status,
)

MAGIC_LEARN_TOOL = AgentTool(
    name="magic.learn",
    description=(
        "Record that an investigator learned a spell. Time is passed explicitly by the Keeper "
        "(2D6 weeks from a book or teacher); this tool does not invent a spell catalog."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string"},
            "spell": {"type": "string"},
            "weeks": {"type": "integer", "description": "Weeks spent learning."},
            "source": {"type": "string", "description": "book / teacher / grimoire name."},
        },
        "required": ["player", "spell"],
    },
    handler=execute_magic_learn,
)

MAGIC_CAST_TOOL = AgentTool(
    name="magic.cast",
    description=(
        "Resolve casting a known spell: spend MP (overflow costs HP one-for-one), SAN and POW. "
        "The first cast needs a hard POW check; a pushed failure still works but costs 1D6 times "
        "the normal cost. Spell costs are supplied by the Keeper."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string"},
            "spell": {"type": "string"},
            "mp_cost": {"type": "integer"},
            "san_cost": {"type": "integer"},
            "pow_cost": {"type": "integer"},
            "first_cast": {"type": "boolean", "description": "Force the hard POW check."},
            "pushed": {"type": "boolean", "description": "Push a failed first cast."},
            "multiplier": {"type": "integer", "description": "1D6 result for the pushed extra cost."},
            "side_effect_roll": {"type": "integer", "description": "1D8 result for the side effect."},
            "result": {
                "type": "string",
                "enum": ["critical", "extreme", "hard", "regular", "failure", "fumble"],
                "description": "Optional explicit POW check result.",
            },
        },
        "required": ["player", "spell"],
    },
    handler=execute_magic_cast,
)

MAGIC_GRIMOIRE_TOOL = AgentTool(
    name="magic.grimoire",
    description=(
        "Resolve reading a mythos tome. Skimming requires a language check and grants CMI; "
        "studying takes the weeks the Keeper states, grants CMF below the book's mythos rating "
        "and CMI at or above it, and costs SAN. No built-in grimoire list."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player": {"type": "string"},
            "title": {"type": "string"},
            "mode": {"type": "string", "enum": ["skim", "study"]},
            "weeks": {"type": "integer", "description": "Explicit reading time in weeks."},
            "san_loss": {"type": "integer"},
            "cmi": {"type": "integer", "description": "Cthulhu Mythos gain when studying."},
            "cmf": {"type": "integer", "description": "Cthulhu Mythos gain below the rating."},
            "rating": {"type": "integer", "description": "Grimoire mythos rating."},
            "spells": {"type": "array", "items": {"type": "string"}},
            "result": {
                "type": "string",
                "enum": ["critical", "extreme", "hard", "regular", "failure", "fumble"],
                "description": "Language check result for skimming.",
            },
        },
        "required": ["player", "title"],
    },
    handler=execute_magic_grimoire,
)

MAGIC_TOOLS = (MAGIC_STATUS_TOOL, MAGIC_LEARN_TOOL, MAGIC_CAST_TOOL, MAGIC_GRIMOIRE_TOOL)
MAGIC_TOOL_NAMES = tuple(tool.name for tool in MAGIC_TOOLS)
