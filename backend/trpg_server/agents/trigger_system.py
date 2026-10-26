"""Trigger/resource domain model and runtime validation.

The module deliberately keeps the storage format JSON based so old scenario files
remain readable.  Resources are references only; binary data lives below the
owning scenario directory.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media import cleanup_derivatives, image_derivatives, video_derivatives
from trpg_server.security import normalize_filename, safe_join

TRIGGER_TYPES = {"scene_enter", "event_triggered", "clue_found", "npc_dialogue", "player_action", "custom"}
RESOURCE_TYPES = {"image", "video", "audio", "richtext", "file"}
VISIBILITIES = {"kp_only", "player_visible"}


@dataclass
class ResourceRef:
    type: str
    path: str
    url: str
    alt: str
    mime: str
    size: int
    hash: str
    content: str | None = None
    thumb_path: str | None = None
    thumb_url: str | None = None
    poster_path: str | None = None
    poster_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class TriggerCondition:
    type: str
    sceneId: str | None = None
    eventId: str | None = None
    clueId: str | None = None
    npcId: str | None = None
    keyword: str | None = None
    naturalLanguage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "")}


@dataclass
class Attachment:
    triggerId: str
    resourceRef: ResourceRef | dict[str, Any]
    condition: TriggerCondition | dict[str, Any]
    relatedCards: list[str] = field(default_factory=list)
    spoilerLevel: int = 0
    visibility: str = "player_visible"
    repeatable: bool = False
    priority: int = 0
    enabled: bool = True
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["resourceRef"] = self.resourceRef.to_dict() if isinstance(self.resourceRef, ResourceRef) else self.resourceRef
        result["condition"] = self.condition.to_dict() if isinstance(self.condition, TriggerCondition) else self.condition
        return result


@dataclass
class TriggerCard:
    id: str
    scriptId: str
    scriptVersion: str
    cardType: str = "trigger"
    sceneId: str | None = None
    spoilerLevel: int = 0
    visibility: str = "player_visible"
    unlockCondition: str | None = None
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_resource(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("resourceRef must be an object")
    resource_type = str(value.get("type") or "file").strip().lower()
    if resource_type not in RESOURCE_TYPES:
        raise ValueError("invalid resource type")
    path = str(value.get("path") or "").replace("\\", "/").lstrip("/")
    if not path or path.startswith("../") or "/../" in path:
        raise ValueError("resource path must stay inside the scenario")
    alt = str(value.get("alt") or "").strip()
    if not alt:
        raise ValueError("resource alt is required")
    result = {
        "type": resource_type,
        "path": path,
        "url": str(value.get("url") or ""),
        "alt": alt[:500],
        "mime": str(value.get("mime") or mimetypes.guess_type(path)[0] or "application/octet-stream"),
        "size": max(0, _int(value.get("size"))),
        "hash": str(value.get("hash") or ""),
        **({"content": str(value.get("content"))} if value.get("content") is not None else {}),
    }
    # 服务端派生字段（缩略图 / poster / 尺寸 / 时长）需在再次保存时保留，
    # 否则前端回写资源引用会丢掉 media.py 生成的结果。
    for key in ("thumb_path", "poster_path"):
        relative = str(value.get(key) or "").replace("\\", "/").lstrip("/")
        if relative and not relative.startswith("../") and "/../" not in relative:
            result[key] = relative
    for key in ("thumb_url", "poster_url"):
        url = str(value.get(key) or "").strip()
        if url:
            result[key] = url
    for key in ("width", "height", "duration_ms"):
        number = _int(value.get(key), -1)
        if number >= 0:
            result[key] = number
    return result


def normalize_condition(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"type": "custom", "naturalLanguage": value}
    if not isinstance(value, dict):
        raise ValueError("condition is required")
    condition_type = str(value.get("type") or "custom").strip().lower()
    if condition_type not in TRIGGER_TYPES:
        raise ValueError("invalid trigger condition type")
    result = {"type": condition_type}
    for key in ("sceneId", "eventId", "clueId", "npcId", "keyword", "naturalLanguage"):
        if value.get(key) not in (None, ""):
            result[key] = str(value[key]).strip()[:500]
    return result


def normalize_attachment(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("attachment must be an object")
    trigger_id = str(value.get("triggerId") or value.get("trigger_id") or uuid4().hex)
    resource = normalize_resource(value.get("resourceRef") or value.get("resource_ref") or {})
    visibility = str(value.get("visibility") or "player_visible")
    if visibility not in VISIBILITIES:
        raise ValueError("invalid attachment visibility")
    spoiler = _int(value.get("spoilerLevel", value.get("spoiler_level")), 0)
    if not 0 <= spoiler <= 5:
        raise ValueError("spoilerLevel must be between 0 and 5")
    return {
        "triggerId": trigger_id,
        "resourceRef": resource,
        "condition": normalize_condition(value.get("condition")),
        "relatedCards": [str(item) for item in (value.get("relatedCards") or value.get("related_cards") or [])][:50],
        "spoilerLevel": spoiler,
        "visibility": visibility,
        "repeatable": _bool(value.get("repeatable"), False),
        "priority": max(-100000, min(100000, _int(value.get("priority"), 0))),
        "enabled": _bool(value.get("enabled"), True),
        **({"note": str(value["note"])[:1000]} if value.get("note") is not None else {}),
    }


def _scenario_root(scenarios_dir: Path, script_id: str | int) -> Path:
    root = safe_join(Path(scenarios_dir), f"scenario-{script_id}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assets_file(root: Path) -> Path:
    return root / "assets.json"


def load_resources(scenarios_dir: Path, script_id: str | int) -> list[dict[str, Any]]:
    value = read_json(_assets_file(_scenario_root(scenarios_dir, script_id)), default=[])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def save_resources(scenarios_dir: Path, script_id: str | int, resources: list[dict[str, Any]]) -> None:
    write_json_atomic(_assets_file(_scenario_root(scenarios_dir, script_id)), resources)


def persist_uploaded_resource(scenarios_dir: Path, script_id: str | int, uploaded: Any, alt: str, resource_type: str | None = None) -> dict[str, Any]:
    raw = uploaded.read()
    if not raw:
        raise ValueError("empty resource")
    root = _scenario_root(scenarios_dir, script_id)
    digest = hashlib.sha256(raw).hexdigest()
    original = normalize_filename(getattr(uploaded, "filename", "resource.bin") or "resource.bin")
    suffix = Path(original).suffix
    filename = f"{digest[:16]}{suffix.lower() or '.bin'}"
    path = root / "assets" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    mime = getattr(uploaded, "mimetype", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    inferred = resource_type or ("image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "file")
    if inferred not in RESOURCE_TYPES:
        inferred = "file"
    ref = ResourceRef(inferred, f"assets/{filename}", f"/assets/scenarios/scenario-{script_id}/assets/{filename}", alt, mime, len(raw), digest)
    derivatives = {}
    if inferred == "image":
        derivatives = image_derivatives(path, root / "assets", digest[:16])
    elif inferred == "video":
        derivatives = video_derivatives(path, root / "assets", digest[:16])
    for key, value in derivatives.items():
        setattr(ref, key, value)
    resources = load_resources(scenarios_dir, script_id)
    resources = [item for item in resources if item.get("hash") != digest]
    resources.append(ref.to_dict())
    save_resources(scenarios_dir, script_id, resources)
    return ref.to_dict()


def delete_resource(scenarios_dir: Path, script_id: str | int, asset_id: str) -> bool:
    root = _scenario_root(scenarios_dir, script_id)
    resources = load_resources(scenarios_dir, script_id)
    kept = []
    removed = None
    for item in resources:
        identifier = str(item.get("hash") or item.get("path") or "")
        if identifier == str(asset_id):
            removed = item
        else:
            kept.append(item)
    if removed is None:
        return False
    save_resources(scenarios_dir, script_id, kept)
    relative = str(removed.get("path") or "")
    if relative and not relative.startswith("../"):
        path = safe_join(root, relative)
        if path.exists() and path.is_file():
            path.unlink()
    cleanup_derivatives(root, removed)
    return True


def trigger_cards(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    cards = scenario.get("trigger_cards") if isinstance(scenario.get("trigger_cards"), list) else []
    result = [item for item in cards if isinstance(item, dict)]
    for module in scenario.get("modules", []) if isinstance(scenario.get("modules"), list) else []:
        if not isinstance(module, dict):
            continue
        for attachment in module.get("attachments", []) if isinstance(module.get("attachments"), list) else []:
            if isinstance(attachment, dict):
                result.append({"id": attachment.get("triggerId"), "scriptId": scenario.get("id"), "scriptVersion": scenario.get("scenario_version", "1.0.0"), "cardType": "trigger", "sceneId": module.get("scene_id") or module.get("id"), "attachments": [attachment], "text": attachment.get("note", "")})
    return result


def find_trigger_definition(scenario: dict[str, Any], trigger_id: str) -> dict[str, Any] | None:
    for card in trigger_cards(scenario):
        if str(card.get("id")) == str(trigger_id):
            return card
    for module in scenario.get("modules", []) if isinstance(scenario.get("modules"), list) else []:
        if not isinstance(module, dict):
            continue
        for attachment in module.get("attachments", []) if isinstance(module.get("attachments"), list) else []:
            if str(attachment.get("triggerId")) == str(trigger_id):
                return {"id": trigger_id, "scriptId": scenario.get("id"), "scriptVersion": scenario.get("scenario_version", "1.0.0"), "cardType": "trigger", "sceneId": module.get("scene_id") or module.get("id"), "attachments": [attachment]}
    return None


def _condition_met(condition: dict[str, Any], state: dict[str, Any], current_scene: str | None) -> bool:
    kind = str(condition.get("type") or "custom")
    if kind == "scene_enter":
        return not condition.get("sceneId") or str(condition["sceneId"]) == str(current_scene)
    if kind == "event_triggered":
        return str(condition.get("eventId")) in {str(x) for x in state.get("triggered_event_ids", [])}
    if kind == "clue_found":
        return str(condition.get("clueId")) in {str(x) for x in state.get("clues", [])}
    if kind == "npc_dialogue":
        npc_id = str(condition.get("npcId") or "")
        return any(
            isinstance(item, dict)
            and npc_id in {str(item.get("npc_id") or ""), str(item.get("actor") or ""), str((item.get("metadata") or {}).get("npc_id") if isinstance(item.get("metadata"), dict) else "")}
            for item in state.get("event_log", [])
        )
    if kind == "player_action":
        keyword = str(condition.get("keyword") or "").casefold()
        return bool(keyword and keyword in str(state.get("last_player_action") or "").casefold())
    return True


def validate_trigger(trigger_id: str, room_state: dict[str, Any], scenario: dict[str, Any], *, audience: str = "player", current_scene: str | None = None) -> dict[str, Any]:
    trigger = find_trigger_definition(scenario, trigger_id)
    if not trigger:
        return {"ok": False, "reason": "trigger_not_found"}
    if str(trigger.get("scriptId") or "") != str(scenario.get("id") or ""):
        return {"ok": False, "reason": "scenario_mismatch"}
    expected_version = str(room_state.get("scenario_version") or "")
    if expected_version and expected_version != str(trigger.get("scriptVersion") or scenario.get("scenario_version") or expected_version):
        return {"ok": False, "reason": "scenario_version_mismatch"}
    active_scene = current_scene or room_state.get("active_scene_id")
    if trigger.get("sceneId") not in (None, "") and str(trigger.get("sceneId")) != str(active_scene):
        return {"ok": False, "reason": "scene_mismatch"}
    if int(trigger.get("spoilerLevel", 0) or 0) > int(room_state.get("spoiler_level", room_state.get("current_spoiler_level", 5)) or 0):
        return {"ok": False, "reason": "spoiler_level_denied"}
    if audience != "kp" and trigger.get("visibility") == "kp_only":
        return {"ok": False, "reason": "visibility_denied"}
    unlock = str(trigger.get("unlockCondition") or "").strip()
    unlocked = {str(item) for key in ("unlocked_conditions", "clues", "triggered_event_ids") for item in room_state.get(key, []) if isinstance(room_state.get(key), list)}
    if unlock and unlock not in unlocked:
        return {"ok": False, "reason": "unlock_condition_not_met"}
    attachments = [item for item in trigger.get("attachments", []) if isinstance(item, dict)]
    if not attachments:
        return {"ok": False, "reason": "no_attachment"}
    for attachment in attachments:
        if not attachment.get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        if audience != "kp" and attachment.get("visibility") == "kp_only":
            return {"ok": False, "reason": "visibility_denied"}
        if int(attachment.get("spoilerLevel", 0)) > int(room_state.get("spoiler_level", room_state.get("current_spoiler_level", 5)) or 0):
            return {"ok": False, "reason": "spoiler_level_denied"}
        if not attachment.get("repeatable") and str(trigger_id) in {str(x) for x in room_state.get("triggeredFiles", [])}:
            return {"ok": False, "reason": "already_triggered"}
        if not _condition_met(attachment.get("condition") or {}, room_state, active_scene):
            return {"ok": False, "reason": "condition_not_met"}
    return {"ok": True, "trigger": trigger}


def record_trigger(room_dir: Path, trigger_id: str, *, reason: str = "", push: Callable[[dict[str, Any]], Any] | None = None, message: dict[str, Any] | None = None) -> dict[str, Any]:
    state_path = Path(room_dir) / "state.json"
    state = read_json(state_path, default={})
    state = state if isinstance(state, dict) else {}
    ids = state.get("triggeredFiles") if isinstance(state.get("triggeredFiles"), list) else []
    if str(trigger_id) not in {str(item) for item in ids}:
        ids.append(str(trigger_id))
    state["triggeredFiles"] = ids[-200:]
    history = state.get("triggerHistory") if isinstance(state.get("triggerHistory"), list) else []
    history.append({"trigger_id": str(trigger_id), "reason": str(reason)[:500], "created_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")})
    state["triggerHistory"] = history[-200:]
    write_json_atomic(state_path, state)
    if push and message:
        push(message)
    return state
