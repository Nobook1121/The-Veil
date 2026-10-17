from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trpg_server.agents.room_state import load_room_state, save_room_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructuredKPResponse:
    narration: str = ""
    options: list[str] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)
    next_scene: str | None = None
    npc_actions: list[dict[str, Any]] = field(default_factory=list)
    triggered_files: list[dict[str, str]] = field(default_factory=list)


def _extract_json_object(content: str) -> dict[str, Any] | None:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def _narration_text(data: dict[str, Any]) -> str:
    """提取叙事正文，兼容不同厂商的字段命名（narration/reply/content/...）。"""
    for key in ("narration", "reply", "content", "message", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_kp_response(content: str) -> StructuredKPResponse | None:
    data = _extract_json_object(content)
    if not data or not any(
        key in data
        for key in (
            "narration", "reply", "content", "message", "text",
            "options", "state_updates", "next_scene", "npc_actions", "triggered_files",
        )
    ):
        return None
    options = data.get("options") if isinstance(data.get("options"), list) else []
    actions = data.get("npc_actions") if isinstance(data.get("npc_actions"), list) else []
    updates = data.get("state_updates") if isinstance(data.get("state_updates"), dict) else {}
    triggered = data.get("triggered_files") if isinstance(data.get("triggered_files"), list) else []
    return StructuredKPResponse(
        narration=_narration_text(data),
        options=[str(item).strip()[:200] for item in options if str(item).strip()][:8],
        state_updates=updates,
        next_scene=str(data.get("next_scene")).strip() if data.get("next_scene") not in (None, "") else None,
        npc_actions=[item for item in actions if isinstance(item, dict)][:20],
        triggered_files=[
            {"trigger_id": str(item.get("trigger_id") or "").strip(), "reason": str(item.get("reason") or "").strip()[:500]}
            for item in triggered if isinstance(item, dict) and str(item.get("trigger_id") or "").strip()
        ][:20],
    )


ALLOWED_STATE_FIELDS = {
    "active_scene_id",
    "triggered_event_ids",
    "clues",
    "npc_attitudes",
    "items",
    "quests",
    "timeline",
    "rolling_summary",
    "completed",
    "ending_reached",
}


def validate_structured_response(
    response: StructuredKPResponse | None,
    scenario: dict[str, Any] | None,
) -> StructuredKPResponse | None:
    """Apply current-version entity boundaries to the complete model response."""
    if response is None:
        return None
    scenario_data = scenario if isinstance(scenario, dict) else {}
    scene_ids = {
        str(item.get("id") or item.get("module_id"))
        for item in scenario_data.get("scene_manifest", [])
        if isinstance(item, dict)
    }
    scene_ids.update(
        str(item.get("scene_id") or item.get("id"))
        for item in scenario_data.get("modules", [])
        if isinstance(item, dict) and str(item.get("module_type") or "").casefold() in {"scene", "ending"}
    )
    npc_ids: set[str] = set()
    modules = scenario_data.get("modules") if isinstance(scenario_data.get("modules"), list) else []
    for module in modules:
        if isinstance(module, dict) and str(module.get("card_type") or module.get("module_type") or "").casefold() in {"npc", "npcs", "character"}:
            identifier = module.get("id") or module.get("code")
            if identifier not in (None, ""):
                npc_ids.add(str(identifier))
    entity_manifest = scenario_data.get("entity_manifest") if isinstance(scenario_data.get("entity_manifest"), list) else []
    npc_ids.update(
        str(item.get("id"))
        for item in entity_manifest
        if isinstance(item, dict)
        and str(item.get("type") or "").casefold() in {"npc", "npcs", "character"}
        and item.get("id") not in (None, "")
    )
    actions = []
    for action in response.npc_actions:
        if not isinstance(action, dict):
            continue
        npc_id = action.get("npc_id") or action.get("id")
        text = str(action.get("action") or "").strip()
        if text and (not npc_ids or str(npc_id) in npc_ids):
            actions.append({"npc_id": str(npc_id), "action": text[:500]})
    # 守剧本护栏的「丢弃」必须可见：否则 KP 引用了剧本外的场景/角色，只是被静默忽略，
    # 管理员既看不到 KP 在编造，也无法据此调整提示词。这里把丢弃原因写进日志。
    if response.next_scene and scene_ids and response.next_scene not in scene_ids:
        logger.warning(
            "KP 结构化响应引用了剧本外的场景 next_scene=%s，已忽略（已知场景数=%d）",
            response.next_scene,
            len(scene_ids),
        )
    dropped_actions = len(response.npc_actions) - len(actions)
    if dropped_actions > 0:
        logger.warning("KP 结构化响应引用了 %d 个剧本外的 NPC 行动，已忽略", dropped_actions)
    return StructuredKPResponse(
        narration=response.narration,
        options=response.options,
        state_updates=validate_state_updates(response.state_updates, {}, scenario_data),
        next_scene=response.next_scene if response.next_scene in scene_ids else None,
        npc_actions=actions[:20],
        triggered_files=response.triggered_files[:20],
    )


def validate_state_updates(
    updates: dict[str, Any] | None,
    room_state: dict[str, Any] | None,
    scenario: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(updates, dict):
        return {}
    result: dict[str, Any] = {}
    scenario_data = scenario if isinstance(scenario, dict) else {}
    manifest = scenario_data.get("scene_manifest") if isinstance(scenario_data.get("scene_manifest"), list) else []
    scene_ids = {str(item.get("id") or item.get("module_id")) for item in manifest if isinstance(item, dict)}
    entity_ids: dict[str, set[str]] = {"clues": set(), "items": set(), "npc_attitudes": set()}
    modules = scenario_data.get("modules") if isinstance(scenario_data.get("modules"), list) else []
    for module in modules:
        if not isinstance(module, dict):
            continue
        identifier = module.get("id") or module.get("code")
        if identifier in (None, ""):
            continue
        module_type = str(module.get("card_type") or module.get("module_type") or "").casefold()
        if module_type in {"clue", "clues", "evidence"}:
            entity_ids["clues"].add(str(identifier))
        elif module_type in {"item", "items", "inventory"}:
            entity_ids["items"].add(str(identifier))
        elif module_type in {"npc", "character", "npcs"}:
            entity_ids["npc_attitudes"].add(str(identifier))
        if module_type in {"scene", "ending"}:
            scene_ids.add(str(module.get("scene_id") or identifier))
    for field_name, keys in {
        "clues": ("clue_ids", "allowed_clue_ids"),
        "items": ("item_ids", "allowed_item_ids"),
        "npc_attitudes": ("npc_ids", "allowed_npc_ids"),
    }.items():
        for key in keys:
            values = scenario_data.get(key)
            if isinstance(values, (list, tuple, set)):
                entity_ids[field_name].update(str(item) for item in values)
    for item in scenario_data.get("entity_manifest", []) if isinstance(scenario_data.get("entity_manifest"), list) else []:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        kind = str(item.get("type") or "").casefold()
        if kind in {"clue", "clues", "evidence"}:
            entity_ids["clues"].add(str(item["id"]))
        elif kind in {"item", "items", "inventory"}:
            entity_ids["items"].add(str(item["id"]))
        elif kind in {"npc", "npcs", "character"}:
            entity_ids["npc_attitudes"].add(str(item["id"]))
    dropped_scene = None
    dropped_entities = 0
    for key, value in updates.items():
        if key not in ALLOWED_STATE_FIELDS:
            continue
        if key == "active_scene_id":
            if value not in (None, "") and str(value) not in scene_ids:
                dropped_scene = str(value)
                continue
            result[key] = str(value) if value not in (None, "") else None
        elif key == "npc_attitudes":
            if isinstance(value, dict):
                allowed = entity_ids["npc_attitudes"]
                kept = {str(k)[:100]: str(v)[:200] for k, v in value.items() if not allowed or str(k) in allowed}
                dropped_entities += len(value) - len(kept)
                result[key] = kept
        elif key == "rolling_summary":
            result[key] = str(value or "")[:4000]
        elif key in {"completed", "ending_reached"}:
            result[key] = bool(value)
        elif isinstance(value, list):
            allowed = entity_ids.get(key, set())
            kept = [item for item in value[:50] if not allowed or str(item) in allowed]
            dropped_entities += len(value[:50]) - len(kept)
            result[key] = kept
    # 状态更新里引用了剧本外实体的部分被丢弃时必须留痕，便于排查 KP 是否在编造。
    if dropped_scene:
        logger.warning("KP 状态更新引用了剧本外的 active_scene_id=%s，已忽略", dropped_scene)
    if dropped_entities:
        logger.warning("KP 状态更新引用了 %d 个剧本外的线索/物品/NPC，已忽略", dropped_entities)
    return result


def apply_state_updates(room_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    state = load_room_state(room_dir)
    state.update(updates)
    return save_room_state(room_dir, state)
