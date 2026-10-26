"""剧本世界树 / 图谱编辑 API（设计文档 §8.5）。

只读写剧本目录下的 `narrative.json`，不触碰运行中的房间状态；发布与版本绑定
仍由既有剧本保存流程负责。所有写接口都带 `base_revision` 冲突检测。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, current_app, request, session

from trpg_server.agents.narrative.logger import log_event
from trpg_server.agents.narrative.generate import GenerateOptions
from trpg_server.agents.narrative.keywords import suggest_module_keywords
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, TOPOLOGIES, normalize_start
from trpg_server.agents.narrative.store import (
    build_index,
    bundle_summary,
    find_scenario_storage,
    load_bundle,
    save_bundle,
)
from trpg_server.agents.narrative.triggers import normalize_asset, normalize_trigger_spec
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle
from trpg_server.agents.narrative.visibility import classify_visibility
from trpg_server.json_store import read_json
from trpg_server.narrative_jobs import public_job_payload, submit_generate_job
from trpg_server.permission_config import is_role_allowed, permission_config_path
from trpg_server.responses import error_response, server_error, success_response

bp = Blueprint("narrative_api", __name__)
logger = logging.getLogger(__name__)


def _scenarios_dir() -> Path:
    return Path(current_app.config.get("SCENARIOS_DIR", Path("data/scenarios")))


def _can_use_permission(node_id: str) -> bool:
    config_dir = current_app.config.get("CONFIG_DIR")
    config_path = current_app.config.get("PERMISSION_CONFIG_FILE") or permission_config_path(config_dir)
    return is_role_allowed(session.get("role", "USER"), node_id, config_path)


def _resolve(scenario_id: int) -> tuple[Path | None, Path | None]:
    return find_scenario_storage(_scenarios_dir(), str(scenario_id))


def _descriptor(storage: Path | None) -> dict[str, Any]:
    if storage is None:
        return {}
    payload = read_json(storage / "scenario.json", default={})
    return payload if isinstance(payload, Mapping) else {}


def _revision(bundle: NarrativeBundle | None) -> int:
    if bundle is None:
        return 0
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    try:
        return max(0, int(meta.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _is_readonly(descriptor: Mapping[str, Any]) -> bool:
    return str(descriptor.get("import_mode") or "") == "direct"


def _can_edit(descriptor: Mapping[str, Any]) -> bool:
    if _can_use_permission("scenarios.manage_all"):
        return True
    return str(descriptor.get("owner_id") or "") == str(session.get("user_id") or "")


def _permissions(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    readonly = _is_readonly(descriptor)
    can_edit = _can_edit(descriptor) and _can_use_permission("scenarios.edit")
    return {"can_view": True, "can_edit": can_edit and not readonly, "readonly": readonly}


def _open_bundle(scenario_id: int) -> tuple[Path | None, dict[str, Any], NarrativeBundle | None, Any]:
    descriptor_path, storage = _resolve(scenario_id)
    if storage is None:
        return None, {}, None, error_response("Scenario not found", 404, "Scenario not found")
    descriptor = _descriptor(storage)
    try:
        bundle = load_bundle(storage)
    except Exception:
        logger.exception("Failed to load narrative bundle: %s", scenario_id)
        return storage, descriptor, None, server_error("Failed to load narrative")
    return storage, descriptor, bundle, None


def _require_edit(storage: Path | None, descriptor: Mapping[str, Any]) -> Any:
    if not _permissions(descriptor)["can_edit"]:
        return error_response("Permission denied", 403, "Permission denied")
    if _is_readonly(descriptor):
        return error_response(
            "This scenario is read-only (direct import)",
            403,
            "Save it as an editable scenario to modify the world tree",
        )
    return None


def _revision_conflict(bundle: NarrativeBundle | None) -> Any:
    current = _revision(bundle)
    return error_response(
        "Scenario narrative has been modified",
        409,
        f"version conflict: current revision {current}",
    )


def _payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _check_base_revision(bundle: NarrativeBundle | None, payload: Mapping[str, Any]) -> Any:
    if "base_revision" not in payload:
        return _revision_conflict(bundle)
    try:
        base = int(payload.get("base_revision"))
    except (TypeError, ValueError):
        return _revision_conflict(bundle)
    if base != _revision(bundle):
        return _revision_conflict(bundle)
    return None


def _commit(storage: Path, bundle: NarrativeBundle, *, scenario_id: int) -> int:
    revision = _revision(bundle) + 1
    meta = dict(bundle.meta) if isinstance(bundle.meta, Mapping) else {}
    meta.update({
        "revision": revision,
        "updated_at": _timestamp(),
        "updated_by": str(session.get("user_id") or ""),
    })
    bundle.meta = meta
    save_bundle(storage, bundle)
    log_event(
        "narrative.save",
        scenario_id=scenario_id,
        revision=revision,
        user_id=session.get("user_id"),
        nodes=len(bundle.nodes),
        edges=len(bundle.edges),
        topology=bundle.topology,
    )
    return revision


def _timestamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _summary_payload(bundle: NarrativeBundle) -> dict[str, Any]:
    return bundle_summary(bundle, index=build_index(bundle))


@bp.route("/api/scenarios/<int:scenario_id>/narrative", methods=["GET"])
def get_narrative(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    return success_response(
        {
            "bundle": bundle.to_dict(),
            "summary": _summary_payload(bundle),
            "revision": _revision(bundle),
            "permissions": _permissions(descriptor),
            "storage": str(storage),
        },
        "Narrative loaded",
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative", methods=["PUT"])
def put_narrative(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, current, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    incoming = payload.get("bundle")
    if not isinstance(incoming, Mapping):
        return error_response("Invalid narrative data", 400, "No bundle")
    conflict = _check_base_revision(current, payload)
    if conflict is not None:
        return conflict

    data = dict(incoming)
    data["scenario_id"] = str(scenario_id)
    if not str(data.get("scenario_version") or "").strip():
        data["scenario_version"] = current.scenario_version if current else "1.0.0"
    bundle = NarrativeBundle.from_dict(data)
    if not bundle.nodes:
        return error_response("Narrative must contain at least one node", 400, "Empty nodes")
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response(
        {"bundle": bundle.to_dict(), "revision": revision, "summary": _summary_payload(bundle)},
        "Narrative saved",
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/nodes/<node_id>", methods=["PATCH"])
def patch_node(scenario_id: int, node_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    node = bundle.node(node_id)
    if node is None:
        return error_response("Node not found", 404, "Node not found")

    data = node.to_dict()
    data.update({key: value for key, value in payload.items() if key != "base_revision"})
    data["id"] = node.id
    updated = type(node).from_dict(data)
    if "visibility" in payload:
        # 作者显式改过可见性后，批量自动判定不再覆盖它（§8.7）。
        updated.extra["visibility_source"] = "author"
        updated.extra.pop("needs_review", None)
    bundle.nodes = [updated if item.id == node.id else item for item in bundle.nodes]
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"node": updated.to_dict(), "revision": revision}, "Node updated")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/edges/<edge_id>", methods=["PATCH"])
def patch_edge(scenario_id: int, edge_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    edge = next((item for item in bundle.edges if item.id == edge_id), None)
    if edge is None:
        return error_response("Edge not found", 404, "Edge not found")

    data = edge.to_dict()
    data.update({key: value for key, value in payload.items() if key != "base_revision"})
    data["id"] = edge.id
    updated = type(edge).from_dict(data)
    if updated is None or not bundle.node(updated.from_id) or not bundle.node(updated.to_id):
        return error_response("Invalid edge endpoints", 400, "Edge endpoints must exist")
    if "visibility" in payload:
        updated.extra["visibility_source"] = "author"
    bundle.edges = [updated if item.id == edge.id else item for item in bundle.edges]
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"edge": updated.to_dict(), "revision": revision}, "Edge updated")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/edges", methods=["POST"])
def create_edge(scenario_id: int):
    """在图谱连线模式下创建一条边（§8.3）；只写 narrative.json。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict

    data = {key: value for key, value in payload.items() if key != "base_revision"}
    data.setdefault("source", "author")
    edge = NarrativeEdge.from_dict(data)
    if edge is None:
        return error_response("Invalid edge", 400, "Edge requires from and to")
    if not bundle.node(edge.from_id) or not bundle.node(edge.to_id):
        return error_response("Invalid edge endpoints", 400, "Edge endpoints must exist")
    if edge.from_id == edge.to_id:
        return error_response("Invalid edge endpoints", 400, "Edge cannot link a node to itself")
    duplicate = any(
        item.id == edge.id
        or (item.from_id == edge.from_id and item.to_id == edge.to_id and item.type == edge.type)
        for item in bundle.edges
    )
    if duplicate:
        return error_response("Edge already exists", 409, f"duplicate edge {edge.id}")

    bundle.edges.append(edge)
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"edge": edge.to_dict(), "revision": revision}, "Edge created", 201)


@bp.route("/api/scenarios/<int:scenario_id>/narrative/edges/<edge_id>", methods=["DELETE"])
def delete_edge(scenario_id: int, edge_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    if "base_revision" not in payload and request.args.get("base_revision") not in (None, ""):
        payload["base_revision"] = request.args.get("base_revision")
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    kept = [item for item in bundle.edges if item.id != edge_id]
    if len(kept) == len(bundle.edges):
        return error_response("Edge not found", 404, "Edge not found")
    bundle.edges = kept
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"revision": revision, "edge_id": edge_id}, "Edge deleted")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/visibility/reclassify", methods=["POST"])
def reclassify_visibility(scenario_id: int):
    """批量重新自动判定可见性（§8.7）：默认只预演，`apply=true` 才落盘。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    apply = bool(payload.get("apply"))

    changes: list[dict[str, Any]] = []
    updated_nodes: list[dict[str, Any]] = []
    auto_count = 0
    for node in bundle.nodes:
        if str(node.extra.get("visibility_source") or "auto") == "author":
            continue
        auto_count += 1
        decision = classify_visibility(
            kind=node.kind,
            module_type="ending" if node.kind == "ending" else "",
            title=node.title,
            summary=node.summary,
            body=node.body,
            kp_notes=node.kp_notes or "",
            keywords=node.keywords,
            reveal_when=node.reveal_when,
        )
        if decision.visibility == node.visibility:
            continue
        changes.append({
            "id": node.id,
            "title": node.title or node.id,
            "from": node.visibility,
            "to": decision.visibility,
            "confidence": round(decision.confidence, 2),
            "reasons": list(decision.reasons),
        })
        if not apply:
            continue
        node.visibility = decision.visibility
        node.spoiler_level = 2 if decision.visibility == "kp_only" else (1 if decision.visibility == "hidden" else 0)
        node.extra["visibility_source"] = "auto"
        node.extra["visibility_confidence"] = round(decision.confidence, 2)
        node.extra["visibility_reasons"] = list(decision.reasons)
        if decision.needs_review:
            node.extra["needs_review"] = True
        else:
            node.extra.pop("needs_review", None)
        updated_nodes.append(node.to_dict())

    revision = _commit(storage, bundle, scenario_id=scenario_id) if apply and changes else _revision(bundle)
    return success_response(
        {
            "applied": bool(apply and changes),
            "scanned": len(bundle.nodes),
            "auto": auto_count,
            "changed": len(changes),
            "changes": changes,
            "nodes": updated_nodes,
            "revision": revision,
        },
        "Visibility reclassified" if apply and changes else "Visibility reclassification preview",
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/start", methods=["GET"])
def get_narrative_start(scenario_id: int):
    """读取开场配置（§8.7），附可选的起始地点列表。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    locations = [
        {"id": node.id, "title": node.title or node.id, "kind": node.kind}
        for node in bundle.nodes
        if node.kind in ("act", "area", "location")
    ]
    return success_response(
        {
            "start": normalize_start(bundle.start),
            "locations": locations,
            "revision": _revision(bundle),
            "permissions": _permissions(descriptor),
        }
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/start", methods=["PUT"])
def put_narrative_start(scenario_id: int):
    """保存开场配置；只写 `narrative.json` 的 `start`。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    start = payload.get("start")
    if not isinstance(start, Mapping):
        return error_response("start is required", 400, "Invalid payload")
    bundle.start = normalize_start(start)
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"start": bundle.start, "revision": revision}, "Opening saved")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/validate", methods=["POST"])
def validate_narrative(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")

    payload = _payload()
    report = validate_bundle(bundle)
    data = report.to_dict()
    if payload.get("all_orders"):
        issues = simulate_any_order(bundle)
        data["order_simulation"] = {
            "ok": not any(issue.level == "error" for issue in issues),
            "issues": [issue.to_dict() for issue in issues],
        }
    data["revision"] = _revision(bundle)
    return success_response(data, "Validation finished")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/topology", methods=["GET"])
def get_topology(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    return success_response(
        {
            "topology": bundle.topology,
            "confidence": bundle.topology_confidence,
            "source": bundle.topology_source,
            "revision": _revision(bundle),
            "permissions": _permissions(descriptor),
        },
        "Topology loaded",
    )


def _asset_map(bundle: NarrativeBundle) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in bundle.assets if isinstance(item, Mapping)}


def _trigger_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("trigger")
    return dict(raw) if isinstance(raw, Mapping) else dict(payload)


def _trigger_id_conflict(bundle: NarrativeBundle, trigger_id: str, *, exclude: str = "") -> bool:
    return any(
        str(item.get("id")) == trigger_id and trigger_id != exclude
        for item in bundle.triggers
        if isinstance(item, Mapping)
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/triggers", methods=["GET"])
def list_triggers(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    return success_response(
        {
            "triggers": bundle.triggers,
            "assets": bundle.assets,
            "revision": _revision(bundle),
            "permissions": _permissions(descriptor),
        },
        "Triggers loaded",
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/triggers", methods=["POST"])
def create_trigger(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    spec = normalize_trigger_spec(
        _trigger_values(payload),
        assets_by_id=_asset_map(bundle),
        index=len(bundle.triggers),
    )
    if spec is None:
        return error_response("Invalid trigger", 400, "A supported binding is required")
    if _trigger_id_conflict(bundle, str(spec["id"])):
        return error_response("Trigger already exists", 409, f"duplicate trigger {spec['id']}")
    bundle.triggers.append(spec)
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"trigger": spec, "revision": revision}, "Trigger created", 201)


@bp.route("/api/scenarios/<int:scenario_id>/narrative/triggers/<trigger_id>", methods=["PUT"])
def update_trigger(scenario_id: int, trigger_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    current = next((item for item in bundle.triggers if str(item.get("id")) == trigger_id), None)
    if current is None:
        return error_response("Trigger not found", 404, "Trigger not found")
    data = dict(current)
    data.update(_trigger_values(payload))
    data["id"] = trigger_id
    spec = normalize_trigger_spec(data, assets_by_id=_asset_map(bundle), index=0)
    if spec is None:
        return error_response("Invalid trigger", 400, "A supported binding is required")
    bundle.triggers = [spec if str(item.get("id")) == trigger_id else item for item in bundle.triggers]
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"trigger": spec, "revision": revision}, "Trigger updated")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/triggers/<trigger_id>", methods=["DELETE"])
def delete_trigger(scenario_id: int, trigger_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied
    payload = _payload()
    if "base_revision" not in payload and request.args.get("base_revision") not in (None, ""):
        payload["base_revision"] = request.args.get("base_revision")
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    kept = [item for item in bundle.triggers if str(item.get("id")) != trigger_id]
    if len(kept) == len(bundle.triggers):
        return error_response("Trigger not found", 404, "Trigger not found")
    bundle.triggers = kept
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"revision": revision, "trigger_id": trigger_id}, "Trigger deleted")


def _resolve_binding(bundle: NarrativeBundle, binding: Mapping[str, Any]) -> tuple[bool, str]:
    kind = str(binding.get("kind") or "")
    if kind == "enter":
        location = str(binding.get("location_id") or "")
        return (bool(bundle.node(location)), location) if location else (False, "enter requires location_id")
    if kind == "interaction":
        location = str(binding.get("location_id") or "")
        if location and not bundle.node(location):
            return False, f"location {location} not found"
        return True, location or "any location"
    if kind in {"clue", "npc"}:
        key = "clue_id" if kind == "clue" else "npc_id"
        node_id = str(binding.get(key) or "")
        if not node_id:
            return False, f"{kind} requires {key}"
        return (bundle.node(node_id) is not None, node_id)
    if kind == "event":
        event_id = str(binding.get("event_id") or "")
        if not event_id:
            return False, "event requires event_id"
        known = {str(item.get("id")) for item in bundle.flags if isinstance(item, Mapping)}
        known |= {str(item.get("id")) for item in bundle.clocks if isinstance(item, Mapping)}
        return (event_id in known or bundle.node(event_id) is not None, event_id)
    if kind == "clock":
        clock_id = str(binding.get("clock_id") or "")
        if not clock_id:
            return False, "clock requires clock_id"
        known = {str(item.get("id")) for item in bundle.clocks if isinstance(item, Mapping)}
        return (clock_id in known, clock_id)
    return True, "manual"


@bp.route("/api/scenarios/<int:scenario_id>/narrative/triggers/<trigger_id>/test", methods=["POST"])
def test_trigger(scenario_id: int, trigger_id: str):
    """确定性预览：只解析绑定与附件，不写房间状态（§8.6）。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    spec = next((item for item in bundle.triggers if str(item.get("id")) == trigger_id), None)
    if spec is None:
        return error_response("Trigger not found", 404, "Trigger not found")

    binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else {}
    resolved, target = _resolve_binding(bundle, binding)
    assets = _asset_map(bundle)
    attachments = []
    warnings = []
    for attachment in spec.get("attachments") or []:
        asset_id = str(attachment.get("asset_id") or "")
        asset = assets.get(asset_id) if asset_id else None
        url = attachment.get("url") or (asset or {}).get("url") or ""
        body = attachment.get("body")
        if not url and body in (None, ""):
            warnings.append(f"附件 {attachment.get('id')} 没有可用内容")
        attachments.append(
            {
                "id": attachment.get("id"),
                "kind": attachment.get("kind"),
                "url": url,
                "body": body,
                "caption": attachment.get("caption"),
                "alt": attachment.get("alt") or (asset or {}).get("alt"),
                "visibility": attachment.get("visibility"),
                "spoiler_level": attachment.get("spoiler_level", 0),
            }
        )
    return success_response(
        {
            "ok": resolved,
            "trigger_id": trigger_id,
            "binding": binding,
            "resolved_target": target,
            "content": spec.get("content"),
            "attachments": attachments,
            "warnings": warnings,
        },
        "Trigger preview ready",
    )


@bp.route("/api/scenarios/<int:scenario_id>/narrative/assets/<asset_id>", methods=["PATCH"])
def patch_asset(scenario_id: int, asset_id: str):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied
    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    current = next((item for item in bundle.assets if str(item.get("id")) == asset_id), None)
    if current is None:
        return error_response("Asset not found", 404, "Asset not found")
    data = dict(current)
    for key in ("alt", "caption", "name", "body", "visibility"):
        if key in payload:
            data[key] = payload[key]
    updated = normalize_asset(data)
    if updated is None:
        return error_response("Invalid asset", 400, "Asset metadata is invalid")
    bundle.assets = [updated if str(item.get("id")) == asset_id else item for item in bundle.assets]
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"asset": updated, "revision": revision}, "Asset updated")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/topology", methods=["PATCH"])
def patch_topology(scenario_id: int):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    if bundle is None:
        return error_response("Narrative not generated", 404, "Run narrative build first")
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    conflict = _check_base_revision(bundle, payload)
    if conflict is not None:
        return conflict
    topology = str(payload.get("topology") or "").strip().lower()
    if topology not in TOPOLOGIES:
        return error_response("Invalid topology", 400, f"Supported: {', '.join(sorted(TOPOLOGIES))}")
    bundle.topology = topology
    bundle.topology_source = "author"
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response(
        {
            "topology": bundle.topology,
            "confidence": bundle.topology_confidence,
            "source": bundle.topology_source,
            "revision": revision,
        },
        "Topology updated",
    )


@bp.route("/api/scenarios/<int:scenario_id>/knowledge/suggest-keywords", methods=["POST"])
def suggest_keywords(scenario_id: int):
    """世界书关键词候选（§8.5）。只读计算，不写剧本。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    _descriptor_path, storage = _resolve(scenario_id)
    if storage is None:
        return error_response("Scenario not found", 404, "Scenario not found")
    descriptor = _descriptor(storage)

    payload = _payload()
    module_ids = payload.get("module_ids")
    result = suggest_module_keywords(
        descriptor,
        limit=payload.get("limit") or 8,
        module_ids=[str(item) for item in module_ids] if isinstance(module_ids, list) else None,
    )
    return success_response(result, "Keyword suggestions loaded")


@bp.route("/api/scenarios/<int:scenario_id>/narrative/generate", methods=["POST"])
def generate_narrative(scenario_id: int):
    """启动一次生成预览（§8.5）。只创建任务，不写 narrative.json。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    storage, descriptor, _bundle, failure = _open_bundle(scenario_id)
    if failure is not None:
        return failure
    denied = _require_edit(storage, descriptor)
    if denied is not None:
        return denied

    payload = _payload()
    options = GenerateOptions.from_payload(payload.get("options") if isinstance(payload.get("options"), Mapping) else payload)
    store = current_app.extensions["narrative_generate_store"]
    job = store.create(
        owner_id=str(session.get("user_id") or ""),
        scenario_id=scenario_id,
        options=options.to_dict(),
    )
    submit_generate_job(current_app._get_current_object(), job["id"])
    log_event(
        "narrative.generate.start",
        scenario_id=scenario_id,
        job_id=job["id"],
        user_id=session.get("user_id"),
        source=options.source,
        topology=options.topology,
    )
    return success_response({"job_id": job["id"], "job": public_job_payload(job)}, "Generation started", 202)


@bp.route("/api/scenarios/<int:scenario_id>/narrative/jobs/<job_id>", methods=["GET"])
def get_generate_job(scenario_id: int, job_id: str):
    """轮询生成进度；完成时一并返回预览结果（bundle + diff）。"""
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _can_use_permission("scenarios.preview"):
        return error_response("Permission denied", 403, "Permission denied")
    store = current_app.extensions["narrative_generate_store"]
    job = store.get(job_id)
    if not isinstance(job, Mapping) or int(job.get("scenario_id") or 0) != scenario_id:
        return error_response("Job not found", 404, "Job not found")
    if str(job.get("owner_id") or "") != str(session.get("user_id") or "") and not _can_use_permission("scenarios.manage_all"):
        return error_response("Permission denied", 403, "Permission denied")

    data: dict[str, Any] = {"job": public_job_payload(job)}
    if job.get("status") == "done":
        result = store.load_result(job_id)
        if isinstance(result, Mapping):
            data["result"] = result
    return success_response(data, "Job loaded")

