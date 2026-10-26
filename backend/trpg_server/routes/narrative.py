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
from trpg_server.agents.narrative.model import NarrativeBundle, TOPOLOGIES
from trpg_server.agents.narrative.store import (
    build_index,
    bundle_summary,
    find_scenario_storage,
    load_bundle,
    save_bundle,
)
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle
from trpg_server.json_store import read_json
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
    bundle.edges = [updated if item.id == edge.id else item for item in bundle.edges]
    revision = _commit(storage, bundle, scenario_id=scenario_id)
    return success_response({"edge": updated.to_dict(), "revision": revision}, "Edge updated")


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
