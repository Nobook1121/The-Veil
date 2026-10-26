"""图谱模式运行工具（设计文档 §5.6）。"""
from __future__ import annotations

from typing import Any

from trpg_server.agents.narrative.ledger import append_fact, load_facts
from trpg_server.agents.narrative.runtime import NarrativeRuntime
from trpg_server.agents.tools.base import AgentTool


def _runtime(context: Any) -> NarrativeRuntime:
    return NarrativeRuntime.load(context)


def get_current_location(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _runtime(context).location_card()


def lookup_location(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    return _runtime(context).lookup_location(query)


def list_exits(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    runtime = _runtime(context)
    return {
        "location_id": runtime.location_id,
        "title": runtime.title(runtime.location_id),
        "exits": runtime.exits(include_hidden=bool(arguments.get("include_hidden"))),
    }


def move_to_location(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    target = arguments.get("location") or arguments.get("target")
    if not target:
        return {"error": "location is required"}
    return _runtime(context).move(target, actor=arguments.get("actor"))


def inspect_object(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    obj = arguments.get("object") or arguments.get("object_id")
    if not obj:
        return {"error": "object is required"}
    return _runtime(context).inspect_object(obj)


def resolve_interaction(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    obj = arguments.get("object") or arguments.get("object_id")
    result = arguments.get("result")
    if not obj or not result:
        return {"error": "object and result are required"}
    return _runtime(context).resolve_interaction(
        obj,
        arguments.get("interaction_id") or arguments.get("interaction"),
        str(result),
        skill=arguments.get("skill"),
        total=_int_or_none(arguments.get("total")),
        roll=_int_or_none(arguments.get("roll")),
        pc=arguments.get("pc"),
    )


def reveal_clue(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    clue = arguments.get("clue") or arguments.get("clue_id")
    if not clue:
        return {"error": "clue is required"}
    return _runtime(context).reveal_clue(clue, pc=arguments.get("pc"))


def update_npc_state(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    npc = arguments.get("npc") or arguments.get("npc_id")
    if not npc:
        return {"error": "npc is required"}
    allowed = {
        key: arguments[key]
        for key in ("value", "delta", "location", "visible", "known_clues")
        if key in arguments
    }
    if not allowed:
        return {"error": "at least one of value/delta/location/visible/known_clues is required"}
    return _runtime(context).update_npc_state(npc, allowed)


def write_fact(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    subject = str(arguments.get("subject") or "").strip()
    predicate = str(arguments.get("predicate") or "").strip()
    object_value = str(arguments.get("object") or "").strip()
    if not subject or not predicate or not object_value:
        return {"error": "subject, predicate and object are required"}
    status = str(arguments.get("status") or "provisional").strip().lower()
    if status == "canon":
        conflict = [
            item for item in load_facts(context.room_dir)
            if item.get("status") == "canon"
            and item.get("subject") == subject
            and item.get("predicate") == predicate
            and item.get("object") != object_value
        ]
        if conflict:
            return {"error": "canon fact conflict: same subject/predicate already has a different object"}
    fact = append_fact(
        context.room_dir,
        {
            "subject": subject,
            "predicate": predicate,
            "object": object_value,
            "status": status,
            "confidence": arguments.get("confidence", 0.5),
            "evidence": arguments.get("evidence") or [],
            "turn": _current_turn(context),
            "visibility": "kp_only",
        },
    )
    return {"written": True, "fact": fact}


def _current_turn(context: Any) -> int:
    from trpg_server.agents.narrative.state_store import load_narrative_state

    try:
        return int((load_narrative_state(context.room_dir).get("clock") or {}).get("turn") or 0)
    except (OSError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


GET_CURRENT_LOCATION_TOOL = AgentTool(
    name="narrative.get_current_location",
    description=(
        "读取当前地点的结构化信息：已向玩家揭示的标题/外观、可见物件与可用交互、出口及硬条件缺口。"
        "KP-only 备注单独放在 kp_notes_do_not_narrate 字段，绝不能直接叙述。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=get_current_location,
)

LOOKUP_LOCATION_TOOL = AgentTool(
    name="narrative.lookup_location",
    description="按名称或 id 查询地点，返回短摘要、是否访问过/可达/可见。不返回未揭示地点的正文。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "地点名称、别名或节点 id。"},
        },
        "required": ["query"],
    },
    handler=lookup_location,
)

LIST_EXITS_TOOL = AgentTool(
    name="narrative.list_exits",
    description="列出当前地点可见出口；hidden 出口默认不返回名称与方向，include_hidden 仅供 KP 调试。",
    parameters={
        "type": "object",
        "properties": {
            "include_hidden": {
                "type": "boolean",
                "description": "是否返回尚未揭示的出口（默认 false；返回内容不可叙述给玩家）。",
            },
        },
        "required": [],
    },
    handler=list_exits,
)

MOVE_TO_LOCATION_TOOL = AgentTool(
    name="narrative.move_to_location",
    description=(
        "把队伍移动到相邻地点并写入状态。引擎会校验邻接、hard 条件与结局门槛；"
        "条件未满足时返回缺少项，模型不得绕过或改述为已到达。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "目标地点名称、别名或 id。"},
            "actor": {"type": "string", "description": "可选：发起移动的 PC id。"},
        },
        "required": ["location"],
    },
    handler=move_to_location,
)

INSPECT_OBJECT_TOOL = AgentTool(
    name="narrative.inspect_object",
    description=(
        "检查当前地点的一个可见物件，返回公开描述与可尝试的交互/技能。"
        "隐藏层只有 reveal_when 成立时才会揭示并写入状态。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "object": {"type": "string", "description": "物件名称或 id。"},
        },
        "required": ["object"],
    },
    handler=inspect_object,
)

RESOLVE_INTERACTION_TOOL = AgentTool(
    name="narrative.resolve_interaction",
    description=(
        "按已完成的检定结果结算物件交互，返回该结果对应的唯一文本并写入状态/时钟/线索效果。"
        "必须先有真实检定；不得自行编造成败或改写结果文本。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "object": {"type": "string", "description": "物件名称或 id。"},
            "interaction_id": {"type": "string", "description": "交互 id；只有一个交互时可省略。"},
            "result": {
                "type": "string",
                "enum": ["extreme_success", "hard_success", "success", "fail", "fumble", "大成功", "困难成功", "成功", "失败", "大失败"],
                "description": "检定结果。",
            },
            "skill": {"type": "string", "description": "使用的技能，用于记录检定。"},
            "total": {"type": "integer", "description": "检定总值。"},
            "roll": {"type": "integer", "description": "骰点。"},
            "pc": {"type": "string", "description": "执行检定的 PC id。"},
        },
        "required": ["object", "result"],
    },
    handler=resolve_interaction,
)

REVEAL_CLUE_TOOL = AgentTool(
    name="narrative.reveal_clue",
    description=(
        "在非检定发现或检定通过后揭示一条线索；会校验 reveal_when，KP-only 线索永远拒绝。"
        "返回线索正文供 KP 叙述，重复揭示不会重复记录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "clue": {"type": "string", "description": "线索名称、别名或 id。"},
            "pc": {"type": "string", "description": "可选：得知线索的 PC id。"},
        },
        "required": ["clue"],
    },
    handler=reveal_clue,
)

UPDATE_NPC_STATE_TOOL = AgentTool(
    name="narrative.update_npc_state",
    description=(
        "更新 NPC 的态度、位置、可见性或已知线索。只能影响 NPC 状态，"
        "不得写入玩家已知线索或玩家背包。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "npc": {"type": "string", "description": "NPC 名称、别名或 id。"},
            "value": {"type": "integer", "description": "直接设定态度值。"},
            "delta": {"type": "integer", "description": "态度增减量。"},
            "location": {"type": "string", "description": "NPC 所在位置。"},
            "visible": {"type": "boolean", "description": "玩家是否看得到该 NPC。"},
            "known_clues": {"type": "array", "items": {"type": "string"}, "description": "NPC 知晓的线索 id。"},
        },
        "required": ["npc"],
    },
    handler=update_npc_state,
)

WRITE_FACT_TOOL = AgentTool(
    name="narrative.write_fact",
    description=(
        "记录一条 KP 侧事实三元组，默认 provisional；只有明确 canon 且有证据时才升级。"
        "与既有 canon 冲突时拒绝。事实不会自动进入玩家叙事。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
            "status": {"type": "string", "enum": ["provisional", "canon", "learned", "rumor"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["subject", "predicate", "object"],
    },
    handler=write_fact,
)

NARRATIVE_TOOLS = [
    GET_CURRENT_LOCATION_TOOL,
    LOOKUP_LOCATION_TOOL,
    LIST_EXITS_TOOL,
    MOVE_TO_LOCATION_TOOL,
    INSPECT_OBJECT_TOOL,
    RESOLVE_INTERACTION_TOOL,
    REVEAL_CLUE_TOOL,
    UPDATE_NPC_STATE_TOOL,
    WRITE_FACT_TOOL,
]

NARRATIVE_TOOL_NAMES = [tool.name for tool in NARRATIVE_TOOLS]


__all__ = [
    "NARRATIVE_TOOLS",
    "NARRATIVE_TOOL_NAMES",
    "GET_CURRENT_LOCATION_TOOL",
    "LOOKUP_LOCATION_TOOL",
    "LIST_EXITS_TOOL",
    "MOVE_TO_LOCATION_TOOL",
    "INSPECT_OBJECT_TOOL",
    "RESOLVE_INTERACTION_TOOL",
    "REVEAL_CLUE_TOOL",
    "UPDATE_NPC_STATE_TOOL",
    "WRITE_FACT_TOOL",
]
