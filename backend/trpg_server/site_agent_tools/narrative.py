"""网站 Agent 的剧本世界树节点工具（设计文档 §8.4）。

粒度从「整份剧本」降到「节点 / 边 / 关键词」：读工具返回裁剪后的上下文，
写工具全部走现有 preview → confirm → apply 两段式，并在应用前校验版本。
Agent 不能发布剧本，也不能把人工标注的 `kp_only` 放开为 `public`。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from trpg_server import scenario_service
from trpg_server.agents.narrative.builder import rebuild_from_descriptor
from trpg_server.agents.narrative.generate import GenerateOptions, generate_preview
from trpg_server.agents.narrative.keywords import suggest_node_keywords
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode, VISIBILITY_STATES
from trpg_server.agents.narrative.quality import compute_quality
from trpg_server.agents.narrative.store import build_index, bundle_summary, save_bundle
from trpg_server.agents.narrative.validate import validate_bundle
from trpg_server.agents.narrative.visibility import classify_visibility, normalize_visibility, tighten
from trpg_server.scenario_store import find_scenario_descriptor_by_id
from trpg_server.settings import SCENARIOS_DIR
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

_MAX_BODY = 1600
_MAX_SUMMARY = 120
_NODE_FIELDS = {
    "id", "kind", "title", "summary", "body", "visibility", "kp_notes", "reveal_when",
    "keywords", "aliases", "negative_keywords", "scope", "constant", "tags", "aliases",
    "objects", "interactions", "extra", "scene_role", "access", "layout",
}
_EDGE_TYPES = {
    "adjacent", "portal", "next", "leads_to", "requires", "reveals", "knows",
    "inhabits", "uses", "triggers", "conflicts", "mentions", "alias", "contains",
}
_OPERATIONS = {
    "add_node", "update_node", "delete_node", "add_edge", "update_edge", "delete_edge",
    "set_keywords", "bind_chunks", "set_visibility",
}


def _scenario(actor: Any, scenario_id: Any) -> dict[str, Any]:
    return scenario_service.get_for_actor(actor, scenario_id)


def _scenario_ref(arguments: dict[str, Any], context: ToolContext) -> Any:
    """工具参数缺省时回退到本轮注入的剧本上下文（§8.4.1）。"""
    value = arguments.get("scenario_id")
    if value not in (None, ""):
        return value
    data = context.scenario_context or {}
    return data.get("scenario_id")


def _storage(scenario_id: Any) -> tuple[Path | None, Path | None]:
    descriptor = find_scenario_descriptor_by_id(Path(SCENARIOS_DIR), str(scenario_id))
    if descriptor is None:
        return None, None
    return descriptor, descriptor.parent


def _bundle(actor: Any, scenario_id: Any) -> tuple[dict[str, Any], NarrativeBundle, Path]:
    scenario = _scenario(actor, scenario_id)
    descriptor, storage = _storage(scenario_id)
    if descriptor is None or storage is None:
        raise scenario_service.ScenarioServiceError("找不到该剧本的存储目录")
    bundle = rebuild_from_descriptor(descriptor, save=False).bundle
    return scenario, bundle, storage


def _revision(bundle: NarrativeBundle) -> int:
    meta = bundle.meta if isinstance(bundle.meta, dict) else {}
    try:
        return int(meta.get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def _writable(scenario: dict[str, Any]) -> str | None:
    if str(scenario.get("import_mode") or "") == "direct":
        return "该剧本为只读导入，请先「另存为可编辑剧本」后再使用世界树编辑。"
    return None


# --------------------------------------------------------------------------- 读


def get_outline(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        scenario, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    index = build_index(bundle)
    items = []
    for node in bundle.nodes:
        if node.kind not in {"area", "location", "ending"}:
            continue
        items.append(
            {
                "id": node.id,
                "kind": node.kind,
                "title": node.title,
                "visibility": normalize_visibility(node.visibility),
                "summary": str(node.summary or node.body or "")[:_MAX_SUMMARY],
                "exits": index.exits.get(node.id, []),
            }
        )
        if len(items) >= 60:
            break
    return ToolResult.success(
        {
            "scenario_id": scenario.get("id"),
            "title": scenario.get("title"),
            "topology": bundle.topology,
            "revision": _revision(bundle),
            "counts": index.counts,
            "items": items,
        },
        summary=f"返回 {len(items)} 个地点摘要（拓扑 {bundle.topology}）",
    )


def get_node(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        _, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    node = bundle.node(str(arguments.get("node_id") or ""))
    if node is None:
        return ToolResult.failure("找不到该节点")
    edges = [
        edge.to_dict() for edge in bundle.edges
        if edge.from_id == node.id or edge.to_id == node.id
    ]
    payload = node.to_dict()
    payload["body"] = str(payload.get("body") or "")[:_MAX_BODY]
    payload["edges"] = edges
    return ToolResult.success(payload, summary=f"读取节点「{node.title or node.id}」")


def get_object(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        _, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    node = bundle.node(str(arguments.get("location_id") or ""))
    if node is None:
        return ToolResult.failure("找不到该地点")
    object_id = str(arguments.get("object_id") or "")
    obj = next(
        (item for item in (node.objects or []) if isinstance(item, dict) and str(item.get("id")) == object_id),
        None,
    )
    if obj is None:
        return ToolResult.failure("找不到该物件")
    payload = dict(obj)
    payload["interactions"] = list(payload.get("interactions") or [])[:8]
    return ToolResult.success(payload, summary=f"读取物件「{payload.get('name') or object_id}」")


def suggest_keywords_tool(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        _, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    node_id = str(arguments.get("node_id") or "")
    if bundle.node(node_id) is None:
        return ToolResult.failure("找不到该节点")
    result = suggest_node_keywords(bundle.nodes, node_ids=[node_id])
    return ToolResult.success(result, summary="关键词候选")


def validate_tool(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        _, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    report = validate_bundle(bundle)
    quality = compute_quality(bundle).to_dict()
    return ToolResult.success(
        {"validation": report.to_dict(), "quality": quality},
        summary=f"校验完成：{report.error_count} error / {report.warning_count} warn",
    )


# --------------------------------------------------------------------------- 写


def _apply_operations(bundle: NarrativeBundle, operations: list[Any]) -> tuple[NarrativeBundle, list[str]]:
    patched = copy.deepcopy(bundle)
    ignored: list[str] = []

    for raw in operations:
        if not isinstance(raw, dict):
            ignored.append("忽略非对象操作")
            continue
        op = str(raw.get("op") or "").strip()
        if op not in _OPERATIONS:
            ignored.append(f"未知操作：{op}")
            continue
        if op in {"add_node", "update_node"}:
            payload = raw.get("node") if isinstance(raw.get("node"), dict) else {}
            fields = {key: value for key, value in payload.items() if key in _NODE_FIELDS}
            node_id = str(raw.get("node_id") or fields.get("id") or "").strip()
            if not node_id:
                ignored.append("节点缺少 id")
                continue
            fields["id"] = node_id
            existing = patched.node(node_id)
            if op == "add_node":
                if existing is not None:
                    ignored.append(f"节点已存在：{node_id}")
                    continue
                if not str(fields.get("kind") or "").strip():
                    ignored.append(f"节点缺少 kind：{node_id}")
                    continue
                node = NarrativeNode.from_dict(fields)
            else:
                if existing is None:
                    ignored.append(f"节点不存在：{node_id}")
                    continue
                merged = existing.to_dict()
                merged.update(fields)
                node = NarrativeNode.from_dict(merged)
            decision = classify_visibility(
                kind=node.kind,
                title=node.title,
                summary=node.summary,
                body=node.body,
                kp_notes=node.kp_notes,
                keywords=node.keywords,
                reveal_when=node.reveal_when,
                explicit=fields.get("visibility"),
            )
            node.visibility = tighten(normalize_visibility(node.visibility), decision.visibility)
            if not isinstance(node.extra, dict):
                node.extra = {}
            node.extra["needs_review"] = bool(node.extra.get("needs_review") or decision.needs_review)
            patched.nodes = [item for item in patched.nodes if item.id != node_id] + [node]
        elif op == "delete_node":
            node_id = str(raw.get("node_id") or "").strip()
            if patched.node(node_id) is None:
                ignored.append(f"节点不存在：{node_id}")
                continue
            patched.nodes = [item for item in patched.nodes if item.id != node_id]
            patched.edges = [
                edge for edge in patched.edges if edge.from_id != node_id and edge.to_id != node_id
            ]
        elif op in {"add_edge", "update_edge"}:
            payload = raw.get("edge") if isinstance(raw.get("edge"), dict) else {}
            edge_id = str(raw.get("edge_id") or payload.get("id") or "").strip()
            if not edge_id:
                ignored.append("边缺少 id")
                continue
            payload = {**payload, "id": edge_id}
            if str(payload.get("type") or "") not in _EDGE_TYPES:
                ignored.append(f"边类型不合法：{edge_id}")
                continue
            if patched.node(str(payload.get("from_id") or "")) is None or patched.node(str(payload.get("to_id") or "")) is None:
                ignored.append(f"边端点不存在：{edge_id}")
                continue
            if op == "update_edge":
                existing = next((item for item in patched.edges if item.id == edge_id), None)
                if existing is None:
                    ignored.append(f"边不存在：{edge_id}")
                    continue
                payload = {**existing.to_dict(), **payload}
            edge = type(patched.edges[0]).from_dict(payload) if patched.edges else None
            if edge is None:
                from trpg_server.agents.narrative.model import NarrativeEdge

                edge = NarrativeEdge.from_dict(payload)
            if edge is None:
                ignored.append(f"边数据不合法：{edge_id}")
                continue
            patched.edges = [item for item in patched.edges if item.id != edge_id] + [edge]
        elif op == "delete_edge":
            edge_id = str(raw.get("edge_id") or "").strip()
            if not any(item.id == edge_id for item in patched.edges):
                ignored.append(f"边不存在：{edge_id}")
                continue
            patched.edges = [item for item in patched.edges if item.id != edge_id]
        elif op == "set_keywords":
            node_id = str(raw.get("node_id") or "").strip()
            node = patched.node(node_id)
            if node is None:
                ignored.append(f"节点不存在：{node_id}")
                continue
            node.keywords = [str(item).strip() for item in (raw.get("keywords") or []) if str(item).strip()][:20]
            node.aliases = [str(item).strip() for item in (raw.get("aliases") or []) if str(item).strip()][:20]
        elif op == "bind_chunks":
            node_id = str(raw.get("node_id") or "").strip()
            node = patched.node(node_id)
            if node is None:
                ignored.append(f"节点不存在：{node_id}")
                continue
            if not isinstance(node.extra, dict):
                node.extra = {}
            node.extra["bound_chunks"] = [
                str(item).strip() for item in (raw.get("chunk_ids") or []) if str(item).strip()
            ][:40]
        elif op == "set_visibility":
            node_id = str(raw.get("node_id") or "").strip()
            node = patched.node(node_id)
            if node is None:
                ignored.append(f"节点不存在：{node_id}")
                continue
            target = str(raw.get("visibility") or "").strip()
            if target not in VISIBILITY_STATES:
                ignored.append(f"可见性不合法：{target}")
                continue
            if not str(raw.get("reason") or "").strip():
                ignored.append("set_visibility 缺少 reason")
                continue
            current = normalize_visibility(node.visibility)
            merged = tighten(current, target)
            if merged != target:
                ignored.append(f"{node_id} 不能把 {current} 放开为 {target}")
            node.visibility = merged
            if not isinstance(node.extra, dict):
                node.extra = {}
            node.extra["visibility_source"] = "author"
            node.extra["visibility_reason"] = str(raw.get("reason") or "")[:200]
    return patched, ignored


def _diff(before: NarrativeBundle, after: NarrativeBundle) -> dict[str, Any]:
    def _map(bundle: NarrativeBundle, attr: str) -> dict[str, dict[str, Any]]:
        return {item.id: item.to_dict() for item in getattr(bundle, attr)}

    def _side(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        return {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "modified": sorted(key for key in set(old) & set(new) if old[key] != new[key]),
        }

    return {
        "nodes": _side(_map(before, "nodes"), _map(after, "nodes")),
        "edges": _side(_map(before, "edges"), _map(after, "edges")),
    }


def _operations(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    operations = arguments.get("operations")
    return [item for item in operations if isinstance(item, dict)] if isinstance(operations, list) else []


def preview_patch(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        scenario, bundle, _ = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    denied = _writable(scenario)
    if denied:
        return ToolResult.failure(denied)
    operations = _operations(arguments)
    base_version = arguments.get("base_version")
    if base_version is not None and int(base_version) != _revision(bundle):
        return ToolResult.failure(f"剧本已更新（当前版本 {_revision(bundle)}），请重新读取后再试")
    if not operations:
        return ToolResult.failure("operations 不能为空")
    patched, ignored = _apply_operations(bundle, operations)
    diff = _diff(bundle, patched)
    return ToolResult.success(
        {
            "diff": diff,
            "ignored": ignored[:20],
            "base_version": _revision(bundle),
            "apply_args": {
                "scenario_id": scenario.get("id"),
                "base_version": _revision(bundle),
                "operations": operations,
            },
        },
        summary=(
            f"将修改 {len(diff['nodes']['modified'])} 个节点、新增 {len(diff['nodes']['added'])} 个节点、"
            f"修改 {len(diff['edges']['modified'])} 条边"
        ),
    )


def apply_patch(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        scenario, bundle, storage = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    denied = _writable(scenario)
    if denied:
        return ToolResult.failure(denied)
    base_version = arguments.get("base_version")
    if base_version is not None and int(base_version) != _revision(bundle):
        return ToolResult.failure(f"剧本已更新（当前版本 {_revision(bundle)}），请重新读取后再试")
    operations = _operations(arguments)
    if not operations:
        return ToolResult.failure("operations 不能为空")
    patched, ignored = _apply_operations(bundle, operations)
    if not isinstance(patched.meta, dict):
        patched.meta = {}
    patched.meta["revision"] = _revision(bundle) + 1
    patched.meta["updated_by"] = "agent"
    save_bundle(storage, patched)
    return ToolResult.success(
        {
            "revision": patched.meta["revision"],
            "ignored": ignored[:20],
            "summary": bundle_summary(patched, index=build_index(patched)),
        },
        summary=f"已应用补丁，世界树版本 {patched.meta['revision']}",
    )


def preview_generate(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        scenario, bundle, storage = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    denied = _writable(scenario)
    if denied:
        return ToolResult.failure(denied)
    options = GenerateOptions(
        source="premise",
        premise=str(arguments.get("premise") or "").strip(),
        topology=str(arguments.get("topology") or "auto"),
        free_order=bool(arguments.get("free_order", False)),
        acts=int(arguments.get("areas") or 2),
        scenes_per_act=int(arguments.get("rooms") or 3),
    )
    preview = generate_preview(
        scenario, storage, options, current=bundle,
    )
    apply_args = {"scenario_id": scenario.get("id"), "bundle": preview["bundle"]}
    return ToolResult.success(
        {
            "summary": preview.get("summary"),
            "topology": preview.get("topology"),
            "diff": preview.get("diff"),
            "warnings": preview.get("warnings"),
            "apply_args": apply_args,
        },
        summary="已从梗概生成世界树骨架（待确认）",
    )


def apply_generate(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        scenario, _, storage = _bundle(actor, _scenario_ref(arguments, context))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    denied = _writable(scenario)
    if denied:
        return ToolResult.failure(denied)
    payload = arguments.get("bundle")
    if not isinstance(payload, dict):
        return ToolResult.failure("缺少生成的 bundle")
    bundle = NarrativeBundle.from_dict(payload)
    if not isinstance(bundle.meta, dict):
        bundle.meta = {}
    bundle.meta["revision"] = _revision(bundle) + 1
    save_bundle(storage, bundle)
    return ToolResult.success(
        {"revision": bundle.meta["revision"]},
        summary=f"已应用生成骨架，世界树版本 {bundle.meta['revision']}",
    )


NARRATIVE_TOOLS = [
    ToolSpec(
        name="narrative.get_outline",
        description="返回剧本地点/区域的一行摘要、拓扑与计数（≤1200 token），每行带 visibility。",
        parameters=schema({"scenario_id": {"type": "integer"}}, ["scenario_id"]),
        risk="read",
        required_permission="scenarios.preview",
        handler=get_outline,
    ),
    ToolSpec(
        name="narrative.get_node",
        description="读取单个地点/资产的完整内容（正文≤1600 字），含 visibility / kp_notes / reveal_when 与相邻边。",
        parameters=schema(
            {"scenario_id": {"type": "integer"}, "node_id": {"type": "string", "maxLength": 80}},
            ["scenario_id", "node_id"],
        ),
        risk="read",
        required_permission="scenarios.preview",
        handler=get_node,
    ),
    ToolSpec(
        name="narrative.get_object",
        description="读取地点内某个物件的 interactions/outcomes、visibility 与 kp_notes。",
        parameters=schema(
            {
                "scenario_id": {"type": "integer"},
                "location_id": {"type": "string", "maxLength": 80},
                "object_id": {"type": "string", "maxLength": 80},
            },
            ["scenario_id", "location_id", "object_id"],
        ),
        risk="read",
        required_permission="scenarios.preview",
        handler=get_object,
    ),
    ToolSpec(
        name="narrative.suggest_keywords",
        description="给出节点关键词候选、来源与建议的 keywords_visibility。",
        parameters=schema(
            {"scenario_id": {"type": "integer"}, "node_id": {"type": "string", "maxLength": 80}},
            ["scenario_id", "node_id"],
        ),
        risk="read",
        required_permission="scenarios.preview",
        handler=suggest_keywords_tool,
    ),
    ToolSpec(
        name="narrative.validate",
        description="返回世界树校验报告与 §7.9 质量指标。",
        parameters=schema({"scenario_id": {"type": "integer"}}, ["scenario_id"]),
        risk="read",
        required_permission="scenarios.preview",
        handler=validate_tool,
    ),
    ToolSpec(
        name="narrative.propose_patch",
        description=(
            "对节点/边/关键词生成补丁 diff，不落盘。operations 支持 "
            "add_node/update_node/delete_node/add_edge/update_edge/delete_edge/set_keywords/bind_chunks/set_visibility；"
            "set_visibility 只能收紧且必须带 reason。"
        ),
        parameters=schema(
            {
                "scenario_id": {"type": "integer"},
                "base_version": {"type": "integer", "minimum": 0},
                "operations": {"type": "array", "maxItems": 30},
            },
            ["scenario_id", "operations"],
        ),
        risk="write",
        required_permission="scenarios.create",
        preview=preview_patch,
        apply=apply_patch,
    ),
    ToolSpec(
        name="narrative.generate_outline",
        description="从一句梗概生成世界树骨架（章节/区域/房间/结局与分支边），只返回 diff 与预览。",
        parameters=schema(
            {
                "scenario_id": {"type": "integer"},
                "premise": {"type": "string", "maxLength": 2000},
                "topology": {"type": "string", "enum": ["auto", "linear", "hub", "sandbox", "hybrid"]},
                "free_order": {"type": "boolean"},
                "areas": {"type": "integer", "minimum": 1, "maximum": 8},
                "rooms": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            ["scenario_id", "premise"],
        ),
        risk="write",
        required_permission="scenarios.create",
        preview=preview_generate,
        apply=apply_generate,
    ),
]

__all__ = ["NARRATIVE_TOOLS"]
