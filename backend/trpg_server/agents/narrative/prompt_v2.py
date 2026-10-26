"""Prompt v2 的确定性状态摘要（设计文档 §5.2、§6.4.4）。

`render_state_summary()` 只读状态与 bundle，不调用模型、不做检索；
它解决「KP 记得玩家在哪、队友在哪、拿了什么、什么状态」，并作为
每轮必注入的 ≤300 字状态层。玩家未知的线索与隐藏时钟不会出现。
"""
from __future__ import annotations

from typing import Any, Mapping

from trpg_server.agents.narrative.conditions import evaluate_condition
from trpg_server.agents.narrative.model import (
    BIDIRECTIONAL_DEFAULT,
    VISIBILITY_PUBLIC,
    NarrativeBundle,
)
from trpg_server.agents.narrative.state_store import normalize_narrative_state

MOVEMENT_EDGE_TYPES = {"adjacent", "portal", "next", "leads_to"}
MAX_SUMMARY_CHARS = 300


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _node_title(bundle: NarrativeBundle | None, node_id: str | None) -> str:
    key = _text(node_id)
    if not key:
        return ""
    if bundle is not None:
        node = bundle.node(key)
        if node and node.title:
            return node.title
    return key


def _pc_name(pc_id: str, entry: Mapping[str, Any] | None) -> str:
    if isinstance(entry, Mapping):
        name = _text(entry.get("name") or entry.get("title"))
        if name:
            return name
    return pc_id


def _revealed_ids(narrative: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    revealed = narrative.get("revealed")
    if isinstance(revealed, list):
        for item in revealed:
            if isinstance(item, Mapping) and _text(item.get("id")):
                result.add(_text(item.get("id")))
    return result


def _is_visited(narrative: Mapping[str, Any], node_id: str) -> bool:
    status = narrative.get("node_status") or {}
    if isinstance(status, Mapping) and str(status.get(node_id) or "") in {"visited", "active", "completed"}:
        return True
    return node_id in {str(item) for item in narrative.get("visit_log") or []}


def _is_visible(visibility: Any, revealed: set[str], object_id: str) -> bool:
    state = _text(visibility) or VISIBILITY_PUBLIC
    if state == VISIBILITY_PUBLIC:
        return True
    return object_id in revealed


def _exit_entries(bundle: NarrativeBundle, narrative: Mapping[str, Any], location: str) -> list[dict[str, Any]]:
    revealed = _revealed_ids(narrative)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(edge: Any, target: str) -> None:
        if target in seen or target == location:
            return
        if not _is_visible(edge.visibility, revealed, str(edge.id)):
            return
        blocked = False
        missing: list[dict[str, Any]] = []
        if edge.hard and edge.condition:
            result = evaluate_condition(edge.condition, narrative)
            blocked = not result.ok
            missing = result.missing
        label = _text(edge.label) or _node_title(bundle, target)
        entries.append({
            "to": target,
            "label": label,
            "type": edge.type,
            "blocked": blocked,
            "missing": missing,
            "visited": _is_visited(narrative, target),
        })
        seen.add(target)

    for edge in bundle.edges:
        if edge.type not in MOVEMENT_EDGE_TYPES:
            continue
        if edge.from_id == location:
            _add(edge, edge.to_id)
        if edge.bidirectional:
            reverse_default = edge.type in BIDIRECTIONAL_DEFAULT
            if edge.to_id == location and reverse_default:
                _add(edge, edge.from_id)
    return entries


def _missing_label(bundle: NarrativeBundle | None, item: Mapping[str, Any]) -> str:
    for key in ("clock", "clue", "item", "visited", "visited_object", "flag"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return _node_title(bundle, value)
    if item.get("type"):
        target = _text(item.get("id") or item.get("name"))
        return f"{_text(item.get('type'))}" + (f"：{_node_title(bundle, target)}" if target else "")
    return ""


def _conditions_text(entry: Mapping[str, Any]) -> str:
    conditions = entry.get("conditions")
    if not isinstance(conditions, Mapping):
        return ""
    parts: list[str] = []
    for key in ("hp", "san", "mp"):
        if key not in conditions:
            continue
        value = _as_int(conditions.get(key), 0)
        maximum = conditions.get(f"{key}_max")
        parts.append(f"{key.upper()} {value}/{_as_int(maximum, 0)}" if maximum else f"{key.upper()} {value}")
    status = entry.get("status")
    if isinstance(status, list):
        parts.extend(_text(item) for item in status[:3] if _text(item))
    return "，".join(parts)


def _known_clue_labels(narrative: Mapping[str, Any], bundle: NarrativeBundle | None) -> list[str]:
    labels: list[str] = []
    revealed = [str(item) for item in narrative.get("revealed_clues") or [] if str(item)]
    pc_clues = narrative.get("pc_known_clues") or {}
    if isinstance(pc_clues, Mapping) and pc_clues:
        for pc_id, clues in pc_clues.items():
            for clue_id in clues or []:
                label = _node_title(bundle, str(clue_id))
                if label and label not in labels:
                    labels.append(label)
    else:
        labels = [_node_title(bundle, clue_id) for clue_id in revealed]
    return [label for label in dict.fromkeys(labels) if label]


def render_state_summary(
    narrative: Mapping[str, Any] | None,
    bundle: NarrativeBundle | None = None,
    *,
    max_chars: int = MAX_SUMMARY_CHARS,
) -> str:
    """渲染每轮注入的状态摘要；无位置且无回合时返回空串。"""
    state = normalize_narrative_state(narrative)
    location = _text(state.get("location"))
    clock = state.get("clock") if isinstance(state.get("clock"), Mapping) else {}
    turn = _as_int(clock.get("turn"), 0)
    if not location and turn <= 0:
        return ""

    lines: list[str] = []
    time_text = _text(clock.get("in_game_time"))
    header = f"【当前状态】第 {turn} 轮" + (f" / 游戏内 {time_text}" if time_text else "")
    lines.append(header)

    location_title = _node_title(bundle, location)
    area_title = ""
    if bundle is not None:
        node = bundle.node(location)
        if node and node.parent:
            area_title = _node_title(bundle, node.parent)
    lines.append(f"位置：{location_title}（{location}）" + (f" / {area_title}" if area_title else ""))

    pc_state = state.get("pc_state") if isinstance(state.get("pc_state"), Mapping) else {}
    party_locations = state.get("party_locations") if isinstance(state.get("party_locations"), Mapping) else {}
    party_split = bool(state.get("party_split")) and len(set(party_locations.values())) > 1
    if party_split:
        members: list[str] = []
        for pc_id, pc_location in list(party_locations.items())[:3]:
            name = _pc_name(str(pc_id), pc_state.get(pc_id))
            members.append(f"{name}在{_node_title(bundle, str(pc_location))}")
        lines.append("队伍：" + "；".join(members) + "（已分队）")

    for pc_id, entry in list(pc_state.items())[:2]:
        if not isinstance(entry, Mapping):
            continue
        name = _pc_name(str(pc_id), entry)
        details = _conditions_text(entry)
        lines.append(f"角色：{name}" + (f"（{details}）" if details else ""))

    inventory: list[str] = []
    for entry in pc_state.values():
        if isinstance(entry, Mapping):
            for item_id in entry.get("inventory") or []:
                label = _node_title(bundle, str(item_id))
                if label and label not in inventory:
                    inventory.append(label)
    if inventory:
        lines.append("背包：" + "、".join(inventory[:8]))

    clues = _known_clue_labels(state, bundle)
    if clues:
        suffix = ""
        pc_known = state.get("pc_known_clues")
        if isinstance(pc_known, Mapping) and len(pc_known) == 1:
            pc_id = next(iter(pc_known))
            suffix = f"（仅{_pc_name(str(pc_id), pc_state.get(pc_id))}）"
        lines.append("已知线索：" + "、".join(clues[:6]) + suffix)

    npc_state = state.get("npc_state") if isinstance(state.get("npc_state"), Mapping) else {}
    present = [
        _node_title(bundle, str(npc_id))
        for npc_id, entry in npc_state.items()
        if isinstance(entry, Mapping)
        and _text(entry.get("location")) == location
        and entry.get("visible", True)
    ]
    if present:
        lines.append("在场：" + "、".join(present[:4]))

    if bundle is not None and location:
        exits = _exit_entries(bundle, state, location)
        if exits:
            parts: list[str] = []
            for entry in exits[:4]:
                if entry["blocked"]:
                    missing = entry.get("missing") or []
                    missing_text = "、".join(
                        label for label in (_missing_label(bundle, item) for item in missing[:2]) if label
                    )
                    parts.append(f"{entry['label']}（封锁" + (f"：{missing_text}" if missing_text else "") + "）")
                else:
                    parts.append(f"{entry['label']}（{'已访问' if entry['visited'] else '可达'}）")
            lines.append("出口：" + "、".join(parts))

    clocks = state.get("clocks") if isinstance(state.get("clocks"), Mapping) else {}
    clock_parts: list[str] = []
    for clock_id, entry in list(clocks.items())[:6]:
        if not isinstance(entry, Mapping) or entry.get("visibility") == "kp_only":
            continue
        value = _as_int(entry.get("value"), 0)
        threshold = _as_int(entry.get("threshold"), 0)
        label = _node_title(bundle, str(clock_id))
        part = f"{label} {value}" + (f"/{threshold}" if threshold else "")
        if part not in clock_parts:
            clock_parts.append(part)
    if clock_parts:
        lines.append("全局：" + "、".join(clock_parts[:3]))

    text = "\n".join(line for line in lines if line)
    limit = max(60, _as_int(max_chars, MAX_SUMMARY_CHARS))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


__all__ = ["MAX_SUMMARY_CHARS", "render_state_summary"]
