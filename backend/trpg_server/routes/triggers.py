from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, redirect, request, send_file, session

from trpg_server.agents.trigger_system import (
    delete_resource,
    load_resources,
    normalize_attachment,
    persist_uploaded_resource,
    replace_uploaded_resource,
    resource_identifier,
    resource_matches,
    trigger_cards,
)
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.agents.narrative.logger import log_event, log_warning
from trpg_server.responses import error_response, success_response
from trpg_server.scenario_store import load_scenario_by_id, load_scenario_record, save_scenario_record, scenario_descriptor_paths
from trpg_server.settings import ROOMS_DIR, SCENARIOS_DIR

bp = Blueprint("triggers", __name__)
logger = logging.getLogger(__name__)


def _scenario_dir() -> Path:
    return Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))


def _login_required():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _scenario(scenario_id):
    descriptor, value = load_scenario_by_id(_scenario_dir(), scenario_id)
    if descriptor and value:
        return descriptor, value
    if _scenario_dir() != Path(SCENARIOS_DIR):
        return load_scenario_by_id(Path(SCENARIOS_DIR), scenario_id)
    return descriptor, value


def _can_edit(value: dict) -> bool:
    role = str(session.get("role") or "").upper()
    return role in {"ADMIN", "OWNER"} or str(value.get("owner_id")) == str(session.get("user_id"))


def _log_denied(script_id):
    log_access_denied(
        logger,
        user_action_text(session.get("username"), "访问剧本被拒绝"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
    )


def _save_scenario(descriptor, scenario):
    return save_scenario_record(
        _scenario_dir(), scenario, existing_descriptor=descriptor,
        trigger_max_file_size=int(current_app.config.get("TRIGGER_MAX_FILE_SIZE", 5 * 1024 * 1024)),
    )


def _storage_root(descriptor: Path) -> Path:
    return descriptor.parent.parent


def _usage_map(descriptor: Path, scenario: dict) -> dict[str, int]:
    """统计每个素材被剧本触发器 / 世界树触发器引用的次数（§8.6）。"""
    import json

    blob_parts: list[str] = []
    for key in ("triggers", "opening", "assets"):
        value = scenario.get(key)
        if value:
            blob_parts.append(json.dumps(value, ensure_ascii=False, default=str))
    narrative_path = descriptor.parent / "narrative.json"
    payload = read_json(narrative_path, default={})
    if isinstance(payload, dict):
        for key in ("triggers", "assets"):
            if payload.get(key):
                blob_parts.append(json.dumps(payload[key], ensure_ascii=False, default=str))
    blob = "\n".join(blob_parts)
    usage: dict[str, int] = {}
    for item in load_resources(_storage_root(descriptor), str(scenario.get("id") or descriptor.parent.parent.name)):
        identifiers = [
            str(item.get("hash") or ""),
            str(item.get("path") or ""),
            str(item.get("url") or ""),
        ]
        usage[resource_identifier(item)] = sum(
            blob.count(value) for value in identifiers if value
        )
    return usage


def _replace_exact(value, mapping: dict[str, str]):
    """递归替换完全匹配的字符串（仅用于资源 hash/path/url 这类精确引用）。"""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace_exact(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _replace_exact(item, mapping) for key, item in value.items()}
    return value


def _sync_replaced_references(descriptor: Path, scenario: dict, previous: dict, current: dict) -> None:
    """素材替换后同步剧本与 `narrative.json` 中的旧引用（§8.6）。"""
    mapping = {}
    for key in ("hash", "path", "url"):
        old = str(previous.get(key) or "").strip()
        new = str(current.get(key) or "").strip()
        if old and new and old != new:
            mapping[old] = new
    if mapping:
        for key in ("triggers", "trigger_cards", "opening", "assets", "modules"):
            if key in scenario:
                scenario[key] = _replace_exact(scenario[key], mapping)

    narrative_path = descriptor.parent / "narrative.json"
    payload = read_json(narrative_path, default={})
    if not isinstance(payload, dict):
        return
    changed = False
    identifier = str(previous.get("asset_id") or resource_identifier(previous))
    assets = payload.get("assets")
    if isinstance(assets, list):
        for item in assets:
            if not isinstance(item, dict):
                continue
            asset_key = str(item.get("id") or item.get("asset_id") or "")
            if asset_key:
                matched = asset_key == identifier or resource_matches(previous, asset_key)
            else:
                matched = bool(previous.get("hash")) and str(item.get("sha256") or "") == str(previous.get("hash"))
            if matched:
                for field in ("url", "thumb_url", "poster_url", "sha256", "mime", "kind"):
                    source = {"sha256": "hash", "kind": "type", "mime": "mime", "url": "url",
                              "thumb_url": "thumb_url", "poster_url": "poster_url"}.get(field)
                    if source and current.get(source):
                        value = current[source]
                        if field == "kind":
                            value = str(current.get("type") or value)
                        item[field] = value
                item["bytes"] = int(current.get("size") or 0)
                for field in ("width", "height", "duration_ms"):
                    if current.get(field) is not None:
                        item[field] = current[field]
                changed = True
    if changed:
        write_json_atomic(narrative_path, payload)


@bp.route("/api/scripts/<int:script_id>/assets", methods=["GET"])
def list_assets(script_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    resources = load_resources(_storage_root(descriptor), script_id)
    usage = _usage_map(descriptor, scenario)
    for item in resources:
        item.setdefault("asset_id", resource_identifier(item))
        item["used_by"] = usage.get(resource_identifier(item), 0)
    return success_response(resources)


@bp.route("/api/scripts/<int:script_id>/assets", methods=["POST"])
def upload_assets(script_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    uploads = request.files.getlist("files") or request.files.getlist("file")
    if not uploads:
        return error_response("Please provide one or more files", 400, "No files")
    alt_values = request.form.getlist("alt")
    results = []
    try:
        for index, uploaded in enumerate(uploads):
            alt = (alt_values[index] if index < len(alt_values) else request.form.get("alt", "")).strip()
            if not alt:
                return error_response("alt is required for every resource", 400, "Invalid metadata")
            results.append(persist_uploaded_resource(_storage_root(descriptor), script_id, uploaded, alt, request.form.get("type")))
    except ValueError as exc:
        log_warning("media.sanitize.denied", scenario_id=script_id, reason=str(exc))
        return error_response(str(exc), 400, "Invalid resource")
    log_event(
        "media.asset.register",
        scenario_id=script_id,
        user_id=session.get("user_id"),
        count=len(results),
        assets=[str(item.get("id") or "") for item in results if isinstance(item, dict)],
    )
    log_user_action(
        logger,
        user_action_text(session.get("username"), "上传了触发器资源"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        资源数=len(results),
    )
    return success_response(results, "Resources uploaded successfully", 201)


@bp.route("/api/scripts/<int:script_id>/assets/<asset_id>", methods=["PUT"])
def update_asset(script_id, asset_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    payload = request.get_json(silent=True) or {}
    resources = load_resources(_storage_root(descriptor), script_id)
    for item in resources:
        if resource_matches(item, asset_id):
            if "alt" in payload:
                alt = str(payload.get("alt") or "").strip()
                if not alt:
                    return error_response("alt is required", 400, "Invalid metadata")
                item["alt"] = alt[:500]
            if "type" in payload and str(payload["type"]) not in {"image", "video", "audio", "richtext", "file"}:
                return error_response("invalid resource type", 400, "Invalid metadata")
            for key in ("type", "url", "content"):
                if key in payload:
                    item[key] = payload[key]
            from trpg_server.agents.trigger_system import save_resources
            save_resources(_storage_root(descriptor), script_id, resources)
            return success_response(item)
    return error_response("Resource not found", 404, "Resource not found")


@bp.route("/api/scripts/<int:script_id>/assets/<asset_id>/replace", methods=["POST"])
def replace_asset(script_id, asset_id):
    """替换素材文件但保留 `asset_id`（§8.6），避免触发器引用失效。"""
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    uploaded = request.files.get("file") or (request.files.getlist("files") or [None])[0]
    if uploaded is None:
        return error_response("Please provide a file", 400, "No files")
    previous = next(
        (item for item in load_resources(_storage_root(descriptor), script_id) if resource_matches(item, asset_id)),
        None,
    )
    if previous is None:
        return error_response("Resource not found", 404, "Resource not found")
    try:
        item = replace_uploaded_resource(
            _storage_root(descriptor),
            script_id,
            asset_id,
            uploaded,
            alt=request.form.get("alt"),
            resource_type=request.form.get("type"),
        )
    except ValueError as exc:
        return error_response(str(exc), 400, "Invalid resource")
    _sync_replaced_references(descriptor, scenario, previous, item)
    _save_scenario(descriptor, scenario)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "替换了触发器资源"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        资源ID=asset_id,
    )
    return success_response(item, "Resource replaced successfully")


@bp.route("/api/scripts/<int:script_id>/assets/<asset_id>", methods=["DELETE"])
def remove_asset(script_id, asset_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    if not delete_resource(_storage_root(descriptor), script_id, asset_id):
        return error_response("Resource not found", 404, "Resource not found")
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了触发器资源"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        文件=asset_id,
    )
    return success_response(message="Resource deleted successfully")


@bp.route("/api/scripts/<int:script_id>/assets/<asset_id>/thumb", methods=["GET"])
def asset_thumb(script_id, asset_id):
    """缩略图（§8.5）：优先 thumb_path，其次 thumb_url；图片资源回退原图。"""
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    root = descriptor.parent
    resource = next(
        (
            item
            for item in load_resources(_storage_root(descriptor), script_id)
            if resource_matches(item, asset_id)
        ),
        None,
    )
    if resource is None:
        return error_response("Resource not found", 404, "Resource not found")

    immutable = "private, max-age=31536000, immutable"
    for key in ("thumb_path", "poster_path"):
        relative = str(resource.get(key) or "").strip()
        if not relative:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            response = send_file(candidate, conditional=True)
            response.headers["Cache-Control"] = immutable
            return response

    for key in ("thumb_url", "poster_url"):
        remote = str(resource.get(key) or "").strip()
        if not remote:
            continue
        response = redirect(remote, code=302)
        response.headers["Cache-Control"] = immutable
        return response

    if str(resource.get("type") or "") != "image":
        return error_response("Thumbnail not available", 404, "Thumbnail not available")
    original = str(resource.get("path") or "").strip()
    if original:
        candidate = (root / original).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return error_response("Resource not found", 404, "Resource not found")
        if candidate.is_file():
            response = send_file(candidate, conditional=True)
            response.headers["Cache-Control"] = immutable
            return response

    url = str(resource.get("url") or "").strip()
    if url.startswith("/"):
        response = redirect(url, code=302)
        response.headers["Cache-Control"] = immutable
        return response
    return error_response("Thumbnail not available", 404, "Thumbnail not available")


def _find_card_attachment(scenario: dict, card_id: str):
    for module in scenario.get("modules", []) if isinstance(scenario.get("modules"), list) else []:
        if not isinstance(module, dict) or str(module.get("id")) != str(card_id):
            continue
        return module.setdefault("attachments", [])
    for card in scenario.get("trigger_cards", []) if isinstance(scenario.get("trigger_cards"), list) else []:
        if isinstance(card, dict) and str(card.get("id")) == str(card_id):
            return card.setdefault("attachments", [])
    return None


@bp.route("/api/scripts/<int:script_id>/cards/<card_id>/attachments", methods=["POST"])
def add_attachment(script_id, card_id):
    return _attachment_mutation(script_id, card_id, None, "POST")


@bp.route("/api/scripts/<int:script_id>/cards/<card_id>/attachments/<trigger_id>", methods=["PUT"])
def edit_attachment(script_id, card_id, trigger_id):
    return _attachment_mutation(script_id, card_id, trigger_id, "PUT")


@bp.route("/api/scripts/<int:script_id>/cards/<card_id>/attachments/<trigger_id>", methods=["DELETE"])
def remove_attachment(script_id, card_id, trigger_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario or not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    attachments = _find_card_attachment(scenario, card_id)
    if attachments is None:
        return error_response("Card not found", 404, "Card not found")
    kept = [item for item in attachments if str(item.get("triggerId")) != str(trigger_id)]
    if len(kept) == len(attachments):
        return error_response("Attachment not found", 404, "Attachment not found")
    attachments[:] = kept
    _save_scenario(descriptor, scenario)
    return success_response(message="Attachment deleted successfully")


def _attachment_mutation(script_id, card_id, trigger_id, method):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    attachments = _find_card_attachment(scenario, card_id)
    if attachments is None:
        return error_response("Card not found", 404, "Card not found")
    payload = request.get_json(silent=True) or {}
    if method == "PUT":
        current = next((item for item in attachments if str(item.get("triggerId")) == str(trigger_id)), None)
        if current is None:
            return error_response("Attachment not found", 404, "Attachment not found")
        payload = {**current, **payload, "triggerId": trigger_id}
        attachments.remove(current)
    else:
        payload = {**payload, "triggerId": payload.get("triggerId") or f"trg_{uuid4().hex[:12]}"}
    try:
        item = normalize_attachment(payload)
    except ValueError as exc:
        return error_response(str(exc), 400, "Invalid attachment")
    attachments.append(item)
    _save_scenario(descriptor, scenario)
    return success_response(item, "Attachment saved successfully", 201 if method == "POST" else 200)


@bp.route("/api/scripts/<int:script_id>/triggers", methods=["GET", "POST"])
def manage_trigger_cards(script_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")
    if request.method == "GET":
        return success_response(trigger_cards(scenario))
    if not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    payload = request.get_json(silent=True) or {}
    attachments = []
    try:
        attachments = [normalize_attachment(item) for item in payload.get("attachments", [])]
    except ValueError as exc:
        return error_response(str(exc), 400, "Invalid attachment")
    if not attachments:
        return error_response("At least one attachment is required", 400, "Invalid trigger")
    card = {
        "id": str(payload.get("id") or f"trg_{uuid4().hex[:12]}"),
        "scriptId": str(script_id),
        "scriptVersion": str(payload.get("scriptVersion") or scenario.get("scenario_version") or "1.0.0"),
        "cardType": "trigger",
        "sceneId": payload.get("sceneId"),
        "spoilerLevel": max(0, min(5, int(payload.get("spoilerLevel", 0)))),
        "visibility": payload.get("visibility", "player_visible"),
        "unlockCondition": payload.get("unlockCondition"),
        "text": str(payload.get("text") or ""),
        "attachments": attachments,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }
    scenario.setdefault("trigger_cards", []).append(card)
    _save_scenario(descriptor, scenario)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "创建了触发器卡"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        触发器ID=card["id"],
    )
    return success_response(card, "Trigger card created successfully", 201)


@bp.route("/api/scripts/<int:script_id>/triggers/<trigger_id>", methods=["PUT"])
def update_trigger_card(script_id, trigger_id):
    if (error := _login_required()) is not None:
        return error
    descriptor, scenario = _scenario(script_id)
    if not descriptor or not scenario or not _can_edit(scenario):
        _log_denied(script_id)
        return error_response("Permission denied", 403, "Permission denied")
    raw_cards = scenario.get("trigger_cards")
    cards: list[Any] = raw_cards if isinstance(raw_cards, list) else []
    card = next((item for item in cards if isinstance(item, dict) and str(item.get("id")) == str(trigger_id)), None)
    if card is None:
        return error_response("Trigger card not found", 404, "Trigger card not found")
    payload = request.get_json(silent=True) or {}
    if "attachments" in payload:
        try:
            card["attachments"] = [normalize_attachment(item) for item in payload["attachments"]]
        except ValueError as exc:
            return error_response(str(exc), 400, "Invalid attachment")
    for key in ("sceneId", "spoilerLevel", "visibility", "unlockCondition", "text", "metadata"):
        if key in payload:
            card[key] = payload[key]
    _save_scenario(descriptor, scenario)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "更新了触发器卡"),
        用户ID=session.get("user_id"),
        剧本ID=script_id,
        触发器ID=trigger_id,
    )
    return success_response(card, "Trigger card updated successfully")


@bp.route("/api/rooms/<room_id>/triggers", methods=["GET"])
def room_trigger_status(room_id):
    if (error := _login_required()) is not None:
        return error
    rooms_dir = Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR))
    room_dir = rooms_dir / str(room_id)
    info = read_json(room_dir / "info.json", default={})
    if not info:
        return error_response("Room not found", 404, "Room not found")
    state = read_json(room_dir / "state.json", default={})
    raw_scenario_id = info.get("scenario_id") if isinstance(info, dict) else None
    if raw_scenario_id in (None, ""):
        return error_response("Room has no scenario", 404, "Room has no scenario")
    _, scenario = load_scenario_by_id(_scenario_dir(), raw_scenario_id, scenario_version=info.get("scenario_version"))
    cards = trigger_cards(scenario or {})
    return success_response({"triggeredFiles": state.get("triggeredFiles", []), "triggerHistory": state.get("triggerHistory", []), "triggers": cards})
