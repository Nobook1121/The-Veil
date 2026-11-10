"""CoC7 批次 5 法术 / 典籍工具。

规则数学在 :mod:`trpg_server.ruleset.coc7.magic`，状态在
:mod:`trpg_server.room_magic`；本模块只做参数搬运与房规开关校验。

批次 6 追加 ``magic.lookup``：按需查询规则书第十二章法术与第十一章典籍。
内容表固定在插件目录，不进入系统提示；``magic.cast`` / ``magic.grimoire``
仍以显式参数为准，``use_catalog`` 只补空缺值。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_magic, room_rules
from trpg_server.agents.tools.base import AgentTool
from trpg_server.ruleset.coc7 import magic_content


def _options(context: Any):
    from trpg_server.ruleset.coc7.options import resolve_options

    return resolve_options(context.room_info())


def _disabled() -> dict[str, Any]:
    return {"ok": False, "error": "coc7 magic house rule is disabled"}


def _catalog_disabled() -> dict[str, Any]:
    return {"ok": False, "error": "coc7 magic catalog is disabled"}


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
    mp_cost = arguments.get("mp_cost") or 0
    san_cost = arguments.get("san_cost") or 0
    pow_cost = arguments.get("pow_cost") or 0
    catalog: dict[str, Any] | None = None
    if arguments.get("use_catalog"):
        stats = magic_content.spell_costs(arguments.get("spell"))
        if stats is None:
            catalog = {"found": False, "note": "内容表里没有这个法术，按显式参数结算"}
        else:
            filled: dict[str, int] = {}
            pending: dict[str, str] = {}
            for key, current in (("mp", mp_cost), ("san", san_cost), ("pow", pow_cost)):
                if current:
                    continue
                value = stats.get(key)
                unit_text = (stats.get("units") or {}).get(key, "")
                if isinstance(value, int):
                    filled[key] = value
                    if key == "mp":
                        mp_cost = value
                    elif key == "san":
                        san_cost = value
                    else:
                        pow_cost = value
                elif unit_text:
                    pending[key] = unit_text
            catalog = {
                "found": True,
                "name": stats.get("name"),
                "raw": stats.get("raw"),
                "filled": filled,
                "pending": pending,
            }
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_magic.cast_spell(
            context.room_dir,
            info,
            member,
            name=arguments.get("spell"),
            mp_cost=mp_cost,
            san_cost=san_cost,
            pow_cost=pow_cost,
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
    result = {"ok": True, "cast": payload, "magic": room_magic.magic_snapshot(member)}
    if catalog is not None:
        result["catalog"] = catalog
    return result


def execute_magic_grimoire(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _options(context).magic_enabled:
        return _disabled()
    weeks = arguments.get("weeks") or 0
    san_loss = arguments.get("san_loss") or 0
    cmi = arguments.get("cmi") or 0
    cmf = arguments.get("cmf") or 0
    rating = arguments.get("rating") or 0
    spells = arguments.get("spells") or []
    catalog: dict[str, Any] | None = None
    if arguments.get("use_catalog"):
        stats = magic_content.grimoire_stats(arguments.get("title"), arguments.get("language"))
        if stats is None:
            catalog = {"found": False, "note": "内容表里没有这本典籍，按显式参数结算"}
        else:
            filled: dict[str, Any] = {}
            for key, current in (("cmi", cmi), ("cmf", cmf), ("rating", rating)):
                if current:
                    continue
                value = stats.get(key)
                if value is not None:
                    filled[key] = value
                    if key == "cmi":
                        cmi = value
                    elif key == "cmf":
                        cmf = value
                    else:
                        rating = value
            if not spells and stats.get("spells"):
                spells = stats["spells"]
                filled["spells"] = spells
            catalog = {
                "found": True,
                "title": stats.get("title"),
                "language": stats.get("language"),
                "filled": filled,
                "san_loss_hint": stats.get("san_loss"),
                "weeks_hint": stats.get("weeks"),
            }
    try:
        info, member = _member(context, arguments.get("player"))
        payload = room_magic.read_grimoire(
            context.room_dir,
            info,
            member,
            title=arguments.get("title"),
            mode=str(arguments.get("mode") or "study"),
            weeks=weeks,
            san_loss=san_loss,
            cmi=cmi,
            cmf=cmf,
            rating=rating,
            success_level=arguments.get("result"),
            spells=spells,
            actor=_actor(context),
            actor_id=getattr(context, "user_id", None),
        )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    result = {"ok": True, "grimoire": payload, "magic": room_magic.magic_snapshot(member)}
    if catalog is not None:
        result["catalog"] = catalog
    return result


def execute_magic_lookup(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """按名称 / 关键词查询单条法术或典籍；不返回整张表。"""
    if not _options(context).magic_catalog_enabled:
        return _catalog_disabled()
    kind = str(arguments.get("kind") or "any").strip().lower()
    query = str(arguments.get("query") or "").strip()
    detail = bool(arguments.get("detail"))
    limit = arguments.get("limit") or 6
    try:
        limit = max(1, min(int(limit), 12))
    except (TypeError, ValueError):
        limit = 6

    if kind in {"spell", "法术"}:
        if detail and query:
            found = magic_content.describe_spell(query)
            if found:
                return {"ok": True, "spell": found}
        results = magic_content.find_spells(
            query,
            group=arguments.get("group"),
            max_san=arguments.get("max_san"),
            max_mp=arguments.get("max_mp"),
            limit=limit,
        )
        return {"ok": True, "kind": "spell", "count": len(results), "results": results}

    if kind in {"grimoire", "book", "典籍"}:
        if detail and query:
            found = magic_content.describe_grimoire(query)
            if found:
                return {"ok": True, "grimoire": found}
        results = magic_content.find_grimoires(
            query,
            language=arguments.get("language"),
            max_san=arguments.get("max_san"),
            limit=limit,
        )
        return {"ok": True, "kind": "grimoire", "count": len(results), "results": results}

    if detail and query:
        found = magic_content.describe_spell(query) or magic_content.describe_grimoire(query)
        if found:
            key = "spell" if found.get("casting_time") is not None else "grimoire"
            return {"ok": True, key: found}
    spells_found = magic_content.find_spells(
        query, max_san=arguments.get("max_san"), max_mp=arguments.get("max_mp"), limit=limit
    )
    books_found = magic_content.find_grimoires(
        query,
        language=arguments.get("language"),
        max_san=arguments.get("max_san"),
        limit=limit,
    )
    return {
        "ok": True,
        "kind": "any",
        "spells": spells_found,
        "grimoires": books_found,
    }


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
        "(2D6 weeks from a book or teacher). Use magic.lookup when the room has the spell index "
        "enabled and you need the official name or cost."
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
        "the normal cost. Spell costs are supplied by the Keeper; set use_catalog to fill omitted "
        "costs from the room's official spell index."
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
            "use_catalog": {
                "type": "boolean",
                "description": (
                    "Fill omitted MP/SAN/POW costs from the room's CoC7 spell index. "
                    "Explicit costs always win; dice costs are returned as pending for the Keeper to roll."
                ),
            },
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
        "and CMI at or above it, and costs SAN. Set use_catalog to fill CMI/CMF/rating and the "
        "book's spell list from the CoC7 grimoire index; SAN and weeks stay explicit."
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
            "use_catalog": {
                "type": "boolean",
                "description": "Fill omitted CMI/CMF/rating/spells from the CoC7 grimoire index.",
            },
            "language": {
                "type": "string",
                "description": "Pick the edition language when the tome has several versions.",
            },
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

MAGIC_LOOKUP_TOOL = AgentTool(
    name="magic.lookup",
    description=(
        "Look up one CoC7 spell or mythos tome by name or keyword in the built-in rulebook index. "
        "Returns costs, casting time, required checks and a short summary; detail=true returns the "
        "full entry text (truncated). Use it on demand instead of reciting the catalog from memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Spell or tome name, or a keyword."},
            "kind": {"type": "string", "enum": ["spell", "grimoire", "any"], "default": "any"},
            "detail": {"type": "boolean", "description": "Return one full entry instead of a list."},
            "group": {"type": "string", "description": "Spell family, e.g. 联络术 / 召唤术 / 通神术."},
            "language": {"type": "string", "description": "Grimoire edition language filter."},
            "max_san": {"type": "integer", "description": "Only entries whose SAN cost cannot exceed this."},
            "max_mp": {"type": "integer", "description": "Only spells whose MP cost cannot exceed this."},
            "limit": {"type": "integer", "description": "Max results, 1-12 (default 6)."},
        },
        "required": ["query"],
    },
    handler=execute_magic_lookup,
)

MAGIC_TOOLS = (MAGIC_STATUS_TOOL, MAGIC_LEARN_TOOL, MAGIC_CAST_TOOL, MAGIC_GRIMOIRE_TOOL)
MAGIC_TOOL_NAMES = tuple(tool.name for tool in MAGIC_TOOLS)
MAGIC_CATALOG_TOOLS = (MAGIC_LOOKUP_TOOL,)
MAGIC_CATALOG_TOOL_NAMES = tuple(tool.name for tool in MAGIC_CATALOG_TOOLS)
