import json
from typing import Any

from trpg_server.agents.memory import read_room_memory, remember_room_fact
from trpg_server.agents.room_state import load_room_state, save_room_state
from trpg_server.agents.tools.base import AgentTool
from trpg_server.scenario_store import build_trigger_message, iter_scenario_trigger_catalog, load_scenario_by_id
from trpg_server.json_store import write_json_atomic


def _find_scenario(scenarios_dir, scenario_id, scenario_version=None):
    if not scenarios_dir or scenario_id is None or not scenarios_dir.exists():
        return None
    _, scenario = load_scenario_by_id(scenarios_dir, scenario_id, scenario_version=scenario_version)
    return scenario


def _matches_query(value: Any, query: str) -> bool:
    if not query:
        return True
    # 序列化为 JSON 后统一大小写匹配，可覆盖嵌套字段中的文本检索。
    return query.casefold() in json.dumps(value, ensure_ascii=False).casefold()


def _module_type(module: dict[str, Any]) -> str:
    return str(module.get("module_type") or module.get("type") or "").strip().lower()


def _scenario_modules(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    modules = scenario.get("modules")
    if isinstance(modules, list):
        return [module for module in modules if isinstance(module, dict)]
    return []


def _summarize_module(module: dict[str, Any], include_content: bool = False) -> dict[str, Any]:
    summary = {
        "id": module.get("id"),
        "module_type": _module_type(module),
        "title": module.get("title"),
        "summary": module.get("summary") or module.get("marker"),
        "visibility": module.get("visibility"),
        "send_to_ai": module.get("send_to_ai"),
    }
    if module.get("code"):
        summary["code"] = module.get("code")
    if module.get("open_ending") is not None:
        summary["open_ending"] = module.get("open_ending")
    if _module_type(module) == "opening":
        summary["fixed_opening"] = bool(module.get("fixed_opening", False))
    if include_content and module.get("content"):
        summary["content"] = module.get("content")
    if _module_type(module) == "scene" and isinstance(module.get("triggers"), list):
        summary["trigger_count"] = len(module["triggers"])
    if _module_type(module) == "custom" and isinstance(module.get("inputs"), list):
        summary["input_count"] = len(module["inputs"])
    return summary


def _collect_available_sections(scenario: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for module in _scenario_modules(scenario):
        module_type = _module_type(module)
        counts[module_type] = counts.get(module_type, 0) + 1
    if counts:
        if counts.get("scene"):
            counts["scenes"] = counts["scene"]
        if counts.get("ending"):
            counts["endings"] = counts["ending"]
        return counts
    return counts


def _scene_manifest(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a small deterministic index; full text is never included here."""
    manifest = []
    for order, module in enumerate(_scenario_modules(scenario), 1):
        if _module_type(module) not in {"scene", "ending"}:
            continue
        summary = str(module.get("summary") or module.get("marker") or "").strip()
        manifest.append({
            "id": str(module.get("scene_id") or module.get("id")),
            "module_id": str(module.get("id")),
            "type": _module_type(module),
            "title": str(module.get("title") or ""),
            "summary": summary[:120],
            "order": module.get("source_order", order),
        })
    return manifest


def _global_manifest(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact immutable facts sent with the room index."""
    result = []
    for module in _scenario_modules(scenario):
        module_type = _module_type(module)
        if module_type not in {"opening", "background", "public_info", "timeline"}:
            continue
        result.append({
            "id": str(module.get("id")),
            "type": module_type,
            "title": str(module.get("title") or ""),
            "summary": str(module.get("summary") or module.get("marker") or "")[:180],
        })
    return result


def _has_combat_stats(module: dict[str, Any]) -> bool:
    for key in ("battle", "attributes"):
        value = module.get(key)
        if isinstance(value, dict) and value:
            return True
    for key in ("skills", "weapons"):
        value = module.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _entity_manifest(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for module in _scenario_modules(scenario):
        module_type = _module_type(module)
        if module_type not in {"npc", "custom", "monster", "item", "clue", "scene", "ending"}:
            continue
        identifier = module.get("id") or module.get("code")
        if identifier in (None, ""):
            continue
        result.append({
            "id": str(identifier),
            "type": str(module.get("card_type") or module_type),
            "title": str(module.get("title") or ""),
            "scene_id": str(module.get("scene_id") or "") or None,
            "has_stats": _has_combat_stats(module),
        })
    return result


def _find_module(scenario: dict[str, Any], module_id: str | None = None, module_type: str | None = None, scene_id: str | None = None) -> dict[str, Any] | None:
    for module in _scenario_modules(scenario):
        if module_id and str(module.get("id")) == module_id:
            return module
        if scene_id and str(module.get("scene_id") or module.get("id")) == scene_id:
            return module
        if module_type and _module_type(module) == module_type:
            return module
    return None


def get_room_scenario_context(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    scenario = _find_scenario(context.scenarios_dir, info.get("scenario_id"), info.get("scenario_version"))
    if not scenario:
        return {"scenario": None, "matches": [], "message": "current room scenario was not found"}

    query = str(arguments.get("query") or "").strip()
    scene_id = str(arguments.get("scene_id") or "").strip()
    module_type = str(arguments.get("module_type") or "").strip().lower()
    max_items = max(1, min(20, int(arguments.get("max_items") or 5)))

    candidates = []
    for module in _scenario_modules(scenario):
        if scene_id and str(module.get("scene_id") or module.get("id")) != scene_id:
            continue
        if module_type and _module_type(module) != module_type:
            continue
        if _matches_query(
            {
                "title": module.get("title"),
                "summary": module.get("summary") or module.get("marker"),
                "content": module.get("content"),
                "code": module.get("code"),
                "module_type": module.get("module_type"),
            },
            query,
        ):
            candidates.append(_summarize_module(module, include_content=False))

    return {
        "scenario": {
            "id": scenario.get("id"),
            "title": scenario.get("title"),
            "description": scenario.get("description") or scenario.get("notes"),
            "allow_open_ending": scenario.get("allow_open_ending", False),
            "module_count": len(_scenario_modules(scenario)),
            "available_sections": _collect_available_sections(scenario),
            "active_scene_id": info.get("active_scene_id"),
            "scene_manifest": _scene_manifest(scenario),
            "global_manifest": _global_manifest(scenario),
            "entity_manifest": _entity_manifest(scenario),
        },
        "matches": candidates[:max_items],
        "triggers": iter_scenario_trigger_catalog(scenario, scene_id=scene_id or None),
    }


def get_room_scenario_module(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    scenario = _find_scenario(context.scenarios_dir, info.get("scenario_id"), info.get("scenario_version"))
    if not scenario:
        return {"error": "current room scenario was not found"}

    module_id = str(arguments.get("module_id") or arguments.get("id") or "").strip()
    module_type = str(arguments.get("module_type") or "").strip().lower() or None
    scene_id = str(arguments.get("scene_id") or "").strip() or None
    module = _find_module(scenario, module_id=module_id or None, module_type=module_type, scene_id=scene_id)
    if not module:
        return {"error": "scenario module was not found"}

    # A source module can be tens of thousands of characters. Returning it in
    # one tool result makes every later tool round resend that entire text.
    # Provide a bounded, pageable excerpt while retaining all structured fields.
    offset = max(0, int(arguments.get("offset") or 0))
    max_chars = max(1000, min(8000, int(arguments.get("max_chars") or 5000)))
    content = str(module.get("content") or "")
    module_payload = {key: value for key, value in module.items() if key != "content"}
    module_payload["content"] = content[offset : offset + max_chars]
    module_payload["content_offset"] = offset
    module_payload["content_length"] = len(content)
    module_payload["content_truncated"] = offset + max_chars < len(content)
    if module_payload["content_truncated"]:
        module_payload["next_offset"] = offset + max_chars

    return {
        "scenario": {
            "id": scenario.get("id"),
            "title": scenario.get("title"),
        },
        "module": module_payload,
    }


def activate_scenario_scene(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Persist the active scene after an explicit player/KP transition.

    The scene must exist in the bound scenario. This gives the model a stable
    pointer between turns and makes it impossible to silently switch to an
    invented location.
    """
    if not context.room_dir:
        return {"error": "room context is required"}
    info = context.room_info()
    scenario = _find_scenario(context.scenarios_dir, info.get("scenario_id"), info.get("scenario_version"))
    if not scenario:
        return {"error": "current room scenario was not found"}
    scene_id = str(arguments.get("scene_id") or arguments.get("module_id") or "").strip()
    if not scene_id:
        return {"error": "scene_id is required"}
    # Direct tool users (e.g. administrators/tests) may omit request_content;
    # model calls from chat must be backed by an explicit transition request.
    if getattr(context, "request_content", "") and not getattr(context, "tool_state", {}).get("allow_scene_transition"):
        return {"error": "scene transition requires an explicit player transition"}
    if getattr(context, "request_content", "") and getattr(context, "tool_state", {}).get("allow_scene_transition"):
        context.tool_state["scene_transition_verified"] = True
    module = _find_module(scenario, scene_id=scene_id, module_id=scene_id)
    if not module or _module_type(module) not in {"scene", "ending"}:
        return {"error": "scene_id is not present in the bound scenario", "scene_id": scene_id}
    info["active_scene_id"] = str(module.get("scene_id") or module.get("id"))
    info["active_scene_title"] = module.get("title")
    write_json_atomic(context.room_dir / "info.json", info)
    # Retrieval (KnowledgeBaseService._room_info) and the runtime snapshot both
    # prefer state.json.active_scene_id, so a stale value there would override
    # the scene this tool just persisted. Mirror the pointer into state.json.
    state = load_room_state(context.room_dir)
    if str(state.get("active_scene_id") or "") != info["active_scene_id"]:
        state["active_scene_id"] = info["active_scene_id"]
        save_room_state(context.room_dir, state)
    context.tool_state["active_scene_id"] = info["active_scene_id"]
    return {"activated": True, "active_scene_id": info["active_scene_id"],
            "module": _summarize_module(module, include_content=False)}


def get_room_character_cards(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    include_inactive = bool(arguments.get("include_inactive", False))
    members = []
    for member in context.room_info().get("members", []):
        active = member.get("is_active", True) is not False and member.get("status", "active") != "removed"
        if not include_inactive and not active:
            continue
        members.append(
            {
                "user_id": member.get("user_id"),
                "username": member.get("username"),
                "active": active,
                "character_card": member.get("character_card"),
                "character_state": member.get("character_state"),
            }
        )
    return {"members": members}


def _summarize_scenario(scenario: dict[str, Any] | None, room_info: dict[str, Any]) -> dict[str, Any] | None:
    if not scenario:
        scenario_id = room_info.get("scenario_id")
        if scenario_id is None:
            return None
        return {
            "id": scenario_id,
            "title": room_info.get("scenario_title"),
            "scenario_version": room_info.get("scenario_version") or "1",
            "found": False,
            "available_sections": {},
        }

    return {
        "id": scenario.get("id"),
        "title": scenario.get("title"),
        "scenario_version": scenario.get("scenario_version") or scenario.get("version") or "1",
        "description": scenario.get("description") or scenario.get("notes"),
        "found": True,
        "import_mode": scenario.get("import_mode"),
        "available_sections": _collect_available_sections(scenario),
        "allow_open_ending": scenario.get("allow_open_ending", False),
        "module_count": len(_scenario_modules(scenario)),
        "trigger_count": len(iter_scenario_trigger_catalog(scenario)),
        "active_scene_id": room_info.get("active_scene_id"),
        "scene_manifest": _scene_manifest(scenario),
        "global_manifest": _global_manifest(scenario),
        "entity_manifest": _entity_manifest(scenario),
    }


def _summarize_character_card(character_card: Any) -> dict[str, Any] | None:
    if not isinstance(character_card, dict):
        return None

    summary = {}
    for key in ("id", "name", "occupation", "age", "gender", "sex", "build", "damageBonus"):
        value = character_card.get(key)
        if value not in (None, ""):
            summary[key] = value
    return summary or None


def _summarize_character_state(character_state: Any) -> dict[str, Any] | None:
    if not isinstance(character_state, dict):
        return None

    summary = {}
    for key in ("max_hp", "current_hp", "max_san", "current_san", "major_wound", "unconscious", "dying", "dead", "armor"):
        value = character_state.get(key)
        if value not in (None, ""):
            summary[key] = value
    return summary or None


def _summarize_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized = []
    for member in members:
        summary = {
            "user_id": member.get("user_id"),
            "username": member.get("username"),
            "active": member.get("active"),
        }
        character = _summarize_character_card(member.get("character_card"))
        if character:
            summary["character"] = character
        character_state = _summarize_character_state(member.get("character_state"))
        if character_state:
            summary["character_state"] = character_state
        summarized.append(summary)
    return summarized


def _active_combat_snapshot(context: Any) -> dict[str, Any] | None:
    """当前战斗轮摘要；没有战斗时返回 None，避免快照体积无谓增大。"""
    try:
        from trpg_server.agents.tools.combat import _combat_summary_state, _load_combat

        combat = _load_combat(context)
        return _combat_summary_state(combat) if combat else None
    except Exception:  # 战斗模块缺失或状态损坏时不影响快照
        return None


def _active_chase_snapshot(context: Any) -> dict[str, Any] | None:
    """当前追逐摘要；没有追逐时返回 None。"""
    try:
        from trpg_server import room_chase

        return room_chase.chase_snapshot(room_chase.get_chase(context.room_info()))
    except Exception:  # 追逐模块缺失或状态损坏时不影响快照
        return None


def get_room_snapshot(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    scenario = _find_scenario(context.scenarios_dir, info.get("scenario_id"), info.get("scenario_version"))
    characters = get_room_character_cards(
        {"include_inactive": bool(arguments.get("include_inactive", False))},
        context,
    )
    return {
        "room": {
            "id": info.get("id") or context.room_id,
            "name": info.get("name"),
            "scenario_id": info.get("scenario_id"),
            "scenario_version": info.get("scenario_version") or (scenario or {}).get("scenario_version") or (scenario or {}).get("version") or "1",
            "scenario_title": info.get("scenario_title"),
            "active_scene_id": info.get("active_scene_id"),
            "active_scene_title": info.get("active_scene_title"),
        },
        "scenario": _summarize_scenario(scenario, info),
        "members": _summarize_members(characters["members"]),
        "combat": _active_combat_snapshot(context),
        "chase": _active_chase_snapshot(context),
        "memory": read_room_memory({"limit": int(arguments.get("memory_limit") or 20)}, context),
        # Trigger metadata is loaded only when the model asks for scenario context.
        # Keeping it out of every snapshot reduces prompt size and prevents unrelated
        # scene triggers from being treated as current actions.
        "triggers": [],
    }


GET_ROOM_SNAPSHOT_TOOL = AgentTool(
    name="room.get_room_snapshot",
    description=(
        "Load a compact current-room snapshot with bound scenario identity, member names, "
        "character identities, HP/SAN state, and memory. Use room.get_character_cards for full stats."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_inactive": {"type": "boolean"},
            "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
    handler=get_room_snapshot,
)


GET_SCENARIO_CONTEXT_TOOL = AgentTool(
    name="room.get_scenario_context",
    description="Load scenario module summaries for the current room, including scenes, endings, NPCs, monsters, and custom modules.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "scene_id": {"type": "string"},
            "module_type": {"type": "string"},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    },
    handler=get_room_scenario_context,
)


GET_SCENARIO_MODULE_TOOL = AgentTool(
    name="room.get_scenario_module",
    description="Load one bounded scenario-module excerpt by id/type/scene. Use offset only when the first excerpt is insufficient; do not load every page preemptively.",
    parameters={
        "type": "object",
        "properties": {
            "module_id": {"type": "string"},
            "module_type": {"type": "string"},
            "scene_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 8000},
        },
    },
    handler=get_room_scenario_module,
)


ACTIVATE_SCENARIO_SCENE_TOOL = AgentTool(
    name="room.activate_scenario_scene",
    description=(
        "Persist the current scene/ending by an id from scene_manifest. Use only when the player "
        "explicitly enters, leaves, or transitions to that scene; never invent an id."
    ),
    parameters={
        "type": "object",
        "properties": {"scene_id": {"type": "string"}, "module_id": {"type": "string"}},
        "required": ["scene_id"],
    },
    handler=activate_scenario_scene,
)


GET_CHARACTER_CARDS_TOOL = AgentTool(
    name="room.get_character_cards",
    description="Load character cards and HP/SAN state for current room members.",
    parameters={"type": "object", "properties": {"include_inactive": {"type": "boolean"}}},
    handler=get_room_character_cards,
)


GET_MEMORY_TOOL = AgentTool(
    name="room.get_memory",
    description="Read remembered facts for the current room.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    },
    handler=read_room_memory,
)


REMEMBER_FACT_TOOL = AgentTool(
    name="room.remember_fact",
    description="Persist an important room fact for future KP continuity.",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "content": {"type": "string"},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["content"],
    },
    handler=remember_room_fact,
)
