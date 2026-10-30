"""触发器数据层：TriggerSpec / Attachment / MediaAsset 归一化与旧数据适配（§4.5）。

运行时只读归一化后的结构；旧 `modules[].triggers[]`、`attachments[]` 与
`trigger_cards[]` 在构建 `narrative.json` 时映射一次，不维护第二套运行时。
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.narrative.coerce import as_str_list_or_scalar as _str_list
from trpg_server.json_store import read_json

BINDING_KINDS = {
    "interaction",
    "enter",
    "clock",
    "event",
    "clue",
    "npc",
    "manual",
}
ATTACHMENT_KINDS = {"image", "video", "audio", "richtext", "file"}
TRIGGER_VISIBILITIES = {"player_visible", "kp_only"}
ONCE_SCOPES = {"room", "pc"}

_TRUE = {"1", "true", "yes", "on"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8')).hexdigest()[:16]}"


def normalize_asset(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把 assets.json / resourceRef / 旧 asset_url 统一成 MediaAsset。"""
    if not isinstance(raw, Mapping):
        return None
    url = _text(raw.get("url"))
    path = _text(raw.get("path"))
    digest = _text(raw.get("sha256") or raw.get("hash"))
    identifier = _text(raw.get("id") or raw.get("asset_id"))
    if not identifier:
        identifier = (digest[:16] if digest else "") or _stable_id("asset", url or path)
    if not identifier.startswith("asset_"):
        identifier = f"asset_{identifier}"
    kind = _text(raw.get("kind") or raw.get("type")).lower()
    if kind not in ATTACHMENT_KINDS:
        guessed = mimetypes.guess_type(url or path)[0] or ""
        if guessed.startswith("image/"):
            kind = "image"
        elif guessed.startswith("video/"):
            kind = "video"
        elif guessed.startswith("audio/"):
            kind = "audio"
        elif guessed.startswith("text/"):
            kind = "richtext"
        else:
            kind = "file"
    size = _int(raw.get("bytes", raw.get("size")), 0)
    return {
        "id": identifier,
        "sha256": digest,
        "kind": kind,
        "mime": _text(raw.get("mime")) or mimetypes.guess_type(url or path)[0] or "application/octet-stream",
        "bytes": max(0, size),
        "url": url,
        "thumb_url": _text(raw.get("thumb_url")),
        "width": _int(raw.get("width"), 0) or None,
        "height": _int(raw.get("height"), 0) or None,
        "duration_ms": _int(raw.get("duration_ms"), 0) or None,
        "poster_url": _text(raw.get("poster_url")),
        "origin": _text(raw.get("origin")) or "upload",
        "alt": _text(raw.get("alt")),
        "caption": _text(raw.get("caption")),
        "body": _text(raw.get("body")),
        "name": _text(raw.get("name") or raw.get("title")),
        "visibility": _text(raw.get("visibility")) or "public",
    }


def asset_from_resource_ref(resource: Mapping[str, Any] | None, *, origin: str = "upload") -> dict[str, Any] | None:
    if not isinstance(resource, Mapping):
        return None
    url = _text(resource.get("url"))
    path = _text(resource.get("path"))
    digest = _text(resource.get("hash"))
    return normalize_asset({
        "id": _text(resource.get("asset_id")) or (digest[:16] if digest else "") or _stable_id("asset", url or path),
        "sha256": digest,
        "kind": resource.get("type"),
        "mime": resource.get("mime"),
        "bytes": resource.get("size"),
        "url": url,
        "alt": resource.get("alt"),
        "origin": origin,
    })


def load_scenario_assets(scenario_dir: Path | str | None) -> list[dict[str, Any]]:
    if scenario_dir is None:
        return []
    value = read_json(Path(scenario_dir) / "assets.json", default=[])
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        normalized = normalize_asset(item) if isinstance(item, Mapping) else None
        if normalized:
            result.append(normalized)
    return result


def _normalize_binding(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    kind = _text(raw.get("kind") or raw.get("type")).lower()
    if kind not in BINDING_KINDS:
        return None
    binding: dict[str, Any] = {"kind": kind}
    if kind == "interaction":
        for key in ("location_id", "object_id", "interaction_id", "check_id", "skill"):
            value = _text(raw.get(key))
            if value:
                binding[key] = value
        result = _text(raw.get("result") or raw.get("outcome")).lower()
        if result:
            binding["result"] = result
    elif kind == "enter":
        location = _text(raw.get("location_id") or raw.get("id"))
        if location:
            binding["location_id"] = location
        if "first_time" in raw:
            binding["first_time"] = _bool(raw.get("first_time"), False)
    elif kind == "clock":
        clock_id = _text(raw.get("clock_id") or raw.get("id"))
        if clock_id:
            binding["clock_id"] = clock_id
        for key in ("gte", "lte", "gt", "lt", "eq"):
            if key in raw:
                binding[key] = raw[key]
        if "threshold" in raw and "gte" not in binding:
            binding["gte"] = raw["threshold"]
    elif kind in {"event", "clue", "npc"}:
        key = {"event": "event_id", "clue": "clue_id", "npc": "npc_id"}[kind]
        value = _text(raw.get(key) or raw.get("id"))
        if value:
            binding[key] = value
        if kind == "npc" and raw.get("event") not in (None, ""):
            binding["event"] = _text(raw.get("event"))
    elif kind == "manual":
        for key in ("action", "keyword", "note"):
            value = _text(raw.get(key))
            if value:
                binding[key] = value
    return binding


def _normalize_condition(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = _text(raw)
    if not text:
        return None
    # 旧自然语言条件只保留提示，运行时不猜；由 needs_review 标记人工确认。
    return {"note": text}


def _normalize_attachment(
    raw: Any,
    *,
    assets_by_id: Mapping[str, dict[str, Any]],
    index: int,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"id": raw, "asset_id": raw}
    if not isinstance(raw, Mapping):
        return None
    asset_id = _text(raw.get("asset_id") or raw.get("assetId"))
    asset = assets_by_id.get(asset_id) if asset_id else None
    kind = _text(raw.get("kind") or (asset or {}).get("kind")).lower()
    if kind not in ATTACHMENT_KINDS:
        kind = "file"
    attachment_id = _text(raw.get("id")) or asset_id or f"att_{index}"
    visibility = _text(raw.get("visibility") or (asset or {}).get("visibility") or "player_visible").lower()
    if visibility not in TRIGGER_VISIBILITIES:
        visibility = "player_visible"
    spoiler = max(0, min(5, _int(raw.get("spoiler_level", raw.get("spoilerLevel")), 0)))
    url = _text(raw.get("url") or (asset or {}).get("url"))
    body = raw.get("body")
    return {
        "id": attachment_id,
        "asset_id": asset_id or None,
        "kind": kind,
        "url": url,
        "thumb_url": _text(raw.get("thumb_url") or (asset or {}).get("thumb_url")),
        "mime": _text(raw.get("mime") or (asset or {}).get("mime")),
        "alt": _text(raw.get("alt") or (asset or {}).get("alt")),
        "caption": _text(raw.get("caption")),
        "body": str(body) if body is not None else None,
        "format": _text(raw.get("format")) or ("markdown" if body is not None else ""),
        "order": max(0, _int(raw.get("order"), index)),
        "visibility": visibility,
        "spoiler_level": spoiler,
        "width": _int(raw.get("width") or (asset or {}).get("width"), 0) or None,
        "height": _int(raw.get("height") or (asset or {}).get("height"), 0) or None,
        "duration_ms": _int(raw.get("duration_ms") or (asset or {}).get("duration_ms"), 0) or None,
        "poster_url": _text(raw.get("poster_url") or (asset or {}).get("poster_url")),
    }


def normalize_trigger_spec(
    raw: Mapping[str, Any] | None,
    *,
    assets_by_id: Mapping[str, dict[str, Any]] | None = None,
    index: int = 0,
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    assets_by_id = assets_by_id or {}
    binding = _normalize_binding(raw.get("binding") if isinstance(raw.get("binding"), Mapping) else raw)
    if binding is None:
        return None
    trigger_id = _text(raw.get("id") or raw.get("trigger_id")) or f"trg_{index}"
    visibility = _text(raw.get("visibility") or "player_visible").lower()
    if visibility not in TRIGGER_VISIBILITIES:
        visibility = "player_visible"
    once_scope = _text(raw.get("once_scope") or raw.get("onceScope") or "room").lower()
    if once_scope not in ONCE_SCOPES:
        once_scope = "room"
    attachments = [
        normalized
        for position, item in enumerate(raw.get("attachments") or [])
        if (normalized := _normalize_attachment(item, assets_by_id=assets_by_id, index=position))
    ]
    return {
        "id": trigger_id,
        "label": _text(raw.get("label") or raw.get("name")) or trigger_id,
        "binding": binding,
        "condition": _normalize_condition(raw.get("condition")),
        "content": _text(raw.get("content")),
        "attachments": attachments,
        "group": _text(raw.get("group")),
        "visibility": visibility,
        "spoiler_level": max(0, min(5, _int(raw.get("spoiler_level", raw.get("spoilerLevel")), 0))),
        "once": _bool(raw.get("once"), True),
        "once_scope": once_scope,
        "cooldown": max(0, _int(raw.get("cooldown"), 0)),
        "priority": _int(raw.get("priority"), 0),
        "enabled": _bool(raw.get("enabled"), True),
        "needs_review": _bool(raw.get("needs_review"), False),
        "legacy_source": _text(raw.get("legacy_source")) or None,
    }


# ---------------------------------------------------------------------------
# 旧数据适配（§4.5.4）
# ---------------------------------------------------------------------------


def _legacy_binding(trigger: Mapping[str, Any], *, scene_id: str | None) -> tuple[dict[str, Any], bool]:
    trigger_type = _text(trigger.get("type") or trigger.get("trigger_type")).lower()
    if not trigger_type:
        if trigger.get("clueId") or trigger.get("clue_id"):
            trigger_type = "clue_found"
        elif trigger.get("npcId") or trigger.get("npc_id"):
            trigger_type = "npc_dialogue"
        elif trigger.get("eventId") or trigger.get("event_id"):
            trigger_type = "event_triggered"
        elif scene_id:
            trigger_type = "scene_enter"
        elif trigger.get("keyword"):
            trigger_type = "player_action"
        else:
            trigger_type = "custom"

    needs_review = False
    if trigger_type == "scene_enter":
        location = _text(trigger.get("sceneId") or trigger.get("scene_id") or scene_id)
        return {"kind": "enter", "location_id": location}, bool(location)
    if trigger_type == "clue_found":
        clue = _text(trigger.get("clueId") or trigger.get("clue_id") or trigger.get("clue"))
        return {"kind": "clue", "clue_id": clue}, bool(clue)
    if trigger_type == "npc_dialogue":
        npc = _text(trigger.get("npcId") or trigger.get("npc_id") or trigger.get("npc"))
        return {"kind": "npc", "npc_id": npc, "event": "dialogue"}, bool(npc)
    if trigger_type == "event_triggered":
        event = _text(trigger.get("eventId") or trigger.get("event_id") or trigger.get("event"))
        return {"kind": "event", "event_id": event}, bool(event)
    if trigger_type == "player_action":
        keyword = _text(trigger.get("keyword") or trigger.get("action"))
        return {"kind": "manual", "keyword": keyword}, bool(keyword)
    return {"kind": "manual", "note": _text(trigger.get("naturalLanguage") or trigger.get("condition"))}, False


def _legacy_condition_atoms(condition: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
    """把旧 TriggerCondition 映射成条件 DSL；无法表达时要求人工复核。"""
    if not isinstance(condition, Mapping):
        return None, False
    kind = _text(condition.get("type")).lower()
    if kind == "scene_enter":
        scene = _text(condition.get("sceneId"))
        return ({"type": "location", "id": scene} if scene else None), bool(scene)
    if kind == "clue_found":
        clue = _text(condition.get("clueId"))
        return ({"clue": clue} if clue else None), bool(clue)
    if kind == "player_action":
        keyword = _text(condition.get("keyword"))
        return ({"note": keyword} if keyword else None), False
    return None, bool(kind not in {"", "event_triggered", "npc_dialogue"})


def _legacy_trigger_spec(
    trigger: Mapping[str, Any],
    *,
    scenario_id: str,
    module: Mapping[str, Any] | None,
    position: int,
    assets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    scene_id = _text((module or {}).get("scene_id") or (module or {}).get("id")) or None
    binding, binding_ok = _legacy_binding(trigger, scene_id=scene_id)
    if binding.get("kind") == "interaction":
        # 旧数据没有结构化 interaction binding；文本条件里的检定只作为复核提示。
        binding = {"kind": "manual", "note": _text(trigger.get("condition"))}
    trigger_id = _text(trigger.get("id") or trigger.get("trigger_id")) or _stable_id(
        "trg", f"{scenario_id}:{scene_id}:{position}:{trigger.get('display_name') or trigger.get('name')}"
    )
    label = _text(trigger.get("display_name") or trigger.get("name")) or trigger_id
    content_mode = _text(trigger.get("content_mode") or "text").lower()
    content = _text(trigger.get("content"))
    asset_url = _text(trigger.get("asset_url"))
    asset_name = _text(trigger.get("asset_name"))
    attachments: list[dict[str, Any]] = []
    if content_mode in {"image", "file"} and asset_url:
        asset = next((item for item in assets if _text(item.get("url")) == asset_url), None)
        if asset is None:
            asset = normalize_asset({
                "id": _stable_id("asset", asset_url),
                "kind": content_mode,
                "url": asset_url,
                "mime": trigger.get("asset_mime"),
                "bytes": trigger.get("asset_size"),
                "alt": asset_name or label,
                "origin": "docx_media",
            })
            if asset:
                assets.append(asset)
        if asset:
            attachments = [{
                "id": f"{trigger_id}_att",
                "asset_id": asset["id"],
                "kind": asset["kind"],
                "alt": asset["alt"],
                "caption": "",
                "order": 0,
                "visibility": "player_visible",
                "spoiler_level": 0,
            }]
    elif content_mode == "richtext" and content:
        attachments = [{
            "id": f"{trigger_id}_att",
            "kind": "richtext",
            "body": content,
            "format": "markdown",
            "caption": "",
            "order": 0,
            "visibility": "player_visible",
            "spoiler_level": 0,
        }]
    needs_review = not binding_ok or (content_mode not in {"text", "richtext", "image", "file"})
    if content_mode == "text" and not content:
        needs_review = True
    return {
        "id": trigger_id,
        "label": label,
        "binding": binding,
        "condition": None,
        "content": content if content_mode == "text" else "",
        "attachments": attachments,
        "group": "",
        "visibility": "player_visible",
        "spoiler_level": 0,
        "once": True,
        "once_scope": "room",
        "cooldown": 0,
        "priority": 0,
        "enabled": True,
        "needs_review": needs_review,
        "legacy_source": f"module:{_text((module or {}).get('id')) or scene_id or ''}",
    }


def _legacy_card_spec(
    card: Mapping[str, Any],
    *,
    scenario_id: str,
    position: int,
    assets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    card_id = _text(card.get("id") or card.get("trigger_id"))
    if not card_id:
        return None
    scene_id = _text(card.get("sceneId") or card.get("scene_id"))
    if scene_id:
        binding: dict[str, Any] = {"kind": "enter", "location_id": scene_id}
        needs_review = False
    else:
        binding = {"kind": "manual", "note": _text(card.get("unlockCondition"))}
        needs_review = True

    attachments: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    for item in card.get("attachments") or []:
        if not isinstance(item, Mapping):
            continue
        resource = item.get("resourceRef") if isinstance(item.get("resourceRef"), Mapping) else {}
        asset = asset_from_resource_ref(resource, origin="upload")
        if asset:
            assets.append(asset)
        atom, atom_ok = _legacy_condition_atoms(item.get("condition"))
        if atom:
            conditions.append(atom)
        if not atom_ok:
            needs_review = True
        attachment = _normalize_attachment(
            {
                "id": item.get("triggerId") or item.get("trigger_id") or f"att_{position}",
                "asset_id": asset.get("id") if asset else "",
                "kind": resource.get("type"),
                "url": resource.get("url"),
                "mime": resource.get("mime"),
                "alt": resource.get("alt") or card.get("text"),
                "caption": str(resource.get("content") or ""),
                "visibility": item.get("visibility") or card.get("visibility"),
                "spoiler_level": item.get("spoilerLevel") or card.get("spoilerLevel") or 0,
                "order": len(attachments),
            },
            assets_by_id={asset["id"]: asset for asset in assets if asset},
            index=len(attachments),
        )
        if attachment:
            attachments.append(attachment)
    visibility = _text(card.get("visibility") or "player_visible").lower()
    return {
        "id": card_id,
        "label": _text(card.get("text")) or card_id,
        "binding": binding,
        "condition": ({"all": conditions} if len(conditions) > 1 else conditions[0] if conditions else None),
        "content": "",
        "attachments": attachments,
        "group": "",
        "visibility": visibility if visibility in TRIGGER_VISIBILITIES else "player_visible",
        "spoiler_level": max(0, min(5, _int(card.get("spoilerLevel"), 0))),
        "once": True,
        "once_scope": "room",
        "cooldown": 0,
        "priority": 0,
        "enabled": True,
        "needs_review": needs_review,
        "legacy_source": "trigger_card",
    }


def adapt_triggers(
    scenario: Mapping[str, Any] | None,
    *,
    assets: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """返回 {'triggers': [...], 'assets': [...], 'warnings': [...]}。"""
    scenario = scenario if isinstance(scenario, Mapping) else {}
    scenario_id = _text(scenario.get("id"))
    raw_assets = assets if assets is not None else scenario.get("assets")
    normalized_assets = [
        normalized
        for item in (raw_assets if isinstance(raw_assets, list) else [])
        if isinstance(item, Mapping) and (normalized := normalize_asset(item))
    ]
    assets_by_id = {item["id"]: item for item in normalized_assets}
    warnings: list[dict[str, Any]] = []

    narrative = scenario.get("narrative") if isinstance(scenario.get("narrative"), Mapping) else {}
    structured = scenario.get("triggers")
    if not isinstance(structured, list):
        structured = narrative.get("triggers")
    structured = [item for item in structured or [] if isinstance(item, Mapping) and isinstance(item.get("binding"), Mapping)]

    triggers: list[dict[str, Any]] = []
    if structured:
        for index, item in enumerate(structured):
            spec = normalize_trigger_spec(item, assets_by_id=assets_by_id, index=index)
            if spec:
                triggers.append(spec)
    else:
        for module_index, module in enumerate(scenario.get("modules") or []):
            if not isinstance(module, Mapping):
                continue
            for trigger_index, trigger in enumerate(module.get("triggers") or []):
                if not isinstance(trigger, Mapping):
                    continue
                spec = _legacy_trigger_spec(
                    trigger,
                    scenario_id=scenario_id,
                    module=module,
                    position=trigger_index,
                    assets=normalized_assets,
                )
                if spec:
                    triggers.append(spec)
        for card_index, card in enumerate(scenario.get("trigger_cards") or []):
            if not isinstance(card, Mapping):
                continue
            spec = _legacy_card_spec(
                card,
                scenario_id=scenario_id,
                position=card_index,
                assets=normalized_assets,
            )
            if spec:
                triggers.append(spec)

    deduped_assets: dict[str, dict[str, Any]] = {}
    for asset in normalized_assets:
        deduped_assets.setdefault(asset["id"], asset)
    for spec in triggers:
        if spec.get("needs_review"):
            warnings.append({
                "code": "trigger.review_needed",
                "level": "warn",
                "message": f"触发器 {spec['id']} 的旧条件无法结构化，发布前需人工确认",
            })
        for attachment in spec.get("attachments") or []:
            asset_id = _text(attachment.get("asset_id"))
            if asset_id and asset_id not in deduped_assets and not _text(attachment.get("url")):
                warnings.append({
                    "code": "trigger.missing_asset",
                    "level": "error",
                    "message": f"触发器 {spec['id']} 引用的素材 {asset_id} 不存在",
                })
    return {
        "triggers": triggers,
        "assets": list(deduped_assets.values()),
        "warnings": warnings,
    }


__all__ = [
    "ATTACHMENT_KINDS",
    "BINDING_KINDS",
    "ONCE_SCOPES",
    "TRIGGER_VISIBILITIES",
    "adapt_triggers",
    "asset_from_resource_ref",
    "load_scenario_assets",
    "normalize_asset",
    "normalize_trigger_spec",
]
