"""剧本相关工具：列表、读取、保存（两段式确认）。"""

from __future__ import annotations

import json
from typing import Any

from trpg_server import scenario_service
from trpg_server.agents.narrative.model import NarrativeBundle
from trpg_server.agents.narrative.repair import repair_bundle
from trpg_server.agents.narrative.store import has_narrative, load_bundle, save_bundle
from trpg_server.agents.narrative.validate import validate_bundle
from trpg_server.scenario_bundle import BundleError, build_narrative, normalize_bundle
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

_MAX_SCENARIO_JSON = 200000


def _payload(arguments: dict[str, Any]) -> dict[str, Any]:
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), dict) else {}
    payload = dict(fields)
    if arguments.get("title"):
        payload["title"] = str(arguments["title"]).strip()
    return payload


def list_scenarios(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    items = scenario_service.list_for_actor(
        actor,
        query=str(arguments.get("query") or ""),
        limit=int(arguments.get("limit") or 20),
    )
    return ToolResult.success({"items": items, "count": len(items)}, summary=f"找到 {len(items)} 个可编辑剧本")


def get_scenario(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    scenario = scenario_service.get_for_actor(actor, arguments.get("scenario_id"))
    return ToolResult.success(scenario, summary=f"读取剧本「{scenario.get('title')}」")


def preview_save(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    payload = _payload(arguments)
    scenario_id = arguments.get("scenario_id")
    try:
        if scenario_id in (None, "", 0):
            preview = scenario_service.preview_create(actor, payload)
            preview["apply_args"] = {"scenario_id": None, "payload": payload}
        else:
            preview = scenario_service.preview_update(actor, scenario_id, payload)
            preview["apply_args"] = {"scenario_id": scenario_id, "payload": payload}
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    return ToolResult.success(preview, summary=str(preview.get("summary") or "保存剧本"))


def apply_save(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
    scenario_id = arguments.get("scenario_id")
    try:
        if scenario_id in (None, "", 0):
            scenario = scenario_service.apply_create(actor, payload)
        else:
            scenario = scenario_service.apply_update(actor, scenario_id, payload)
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    return ToolResult.success(
        {"id": scenario.get("id"), "title": scenario.get("title"), "version": scenario.get("scenario_version")},
        summary=f"已保存剧本「{scenario.get('title')}」",
    )


def validate_scenario(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """读取图谱并跑确定性校验，附带「一键修复」的预览结果（不写盘）。"""
    try:
        descriptor = scenario_service.descriptor_path_for_actor(actor, arguments.get("scenario_id"))
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    scenario_dir = descriptor.parent
    if not has_narrative(scenario_dir):
        return ToolResult.success(
            {"has_narrative": False, "issues": [], "repair_preview": []},
            summary="该剧本还没有生成图谱，先在剧本编辑器的「世界树 / 图谱」里生成",
        )
    bundle = load_bundle(scenario_dir)
    if bundle is None:
        return ToolResult.success({"has_narrative": False, "issues": []}, summary="图谱文件为空，需要重新生成")
    report = validate_bundle(bundle)
    preview = repair_bundle(NarrativeBundle.from_dict(bundle.to_dict()))
    data = report.to_dict()
    data["has_narrative"] = True
    data["node_count"] = len(bundle.nodes)
    data["edge_count"] = len(bundle.edges)
    data["repair_preview"] = preview.to_dict()["applied"]
    return ToolResult.success(
        data,
        summary=f"剧本校验：{data.get('error_count', 0)} 个错误、{data.get('warning_count', 0)} 个警告",
    )


def _import_bundle(arguments: dict[str, Any]) -> Any:
    """返回 (BundleResult, narrative_bundle, repair_report, validation)。"""
    bundle = arguments.get("bundle") if isinstance(arguments.get("bundle"), dict) else None
    if bundle is None:
        content = str(arguments.get("content") or "").strip()
        if not content:
            raise BundleError("请提供剧本原文（content）或已生成的剧本包（bundle）")
        from trpg_server.scenario_import_ai import analyze_script, build_ai_requester

        bundle = analyze_script(
            content,
            title=str(arguments.get("title") or "").strip(),
            request=build_ai_requester(),
        )
    result = normalize_bundle(bundle, fallback_title=str(arguments.get("title") or "").strip())
    if result.errors:
        raise BundleError("；".join(str(item.get("message")) for item in result.errors[:5]))
    narrative, repair, validation = build_narrative(result)
    return result, narrative, repair, validation


def preview_import_script(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """AI 全链路导入剧本：预览剔除项、模块与图谱统计，确认后才创建。"""
    try:
        result, narrative, repair, validation = _import_bundle(arguments)
    except BundleError as exc:
        return ToolResult.failure(str(exc))
    except RuntimeError as exc:
        return ToolResult.failure(f"AI 分析失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - 导入阶段失败要返回可读原因
        return ToolResult.failure(f"剧本分析失败：{exc}")
    scenario = dict(result.data["scenario"])
    scenario.pop("id", None)
    scenario.pop("scenario_version", None)
    scenario["import_mode"] = "ai"
    scenario["import_exclusions"] = result.data.get("exclusions") or []
    scenario["conversion"] = {
        "version": 5,
        "source_preserved": True,
        "document_import": True,
        "ai": {"status": "ok", "stages": (result.data.get("analysis") or {}).get("stages") or {}},
        "warnings": result.warnings,
        "stages": ["review", "segment", "worldbook", "graph", "normalize"],
    }
    if arguments.get("author"):
        scenario["author"] = str(arguments["author"]).strip()
    if arguments.get("player_count") is not None:
        scenario["playerCount"] = int(arguments["player_count"])
    try:
        preview = scenario_service.preview_create(actor, scenario)
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    data = {
        "summary": preview.get("summary"),
        "modules": len(scenario.get("modules", [])),
        "nodes": len(narrative.nodes),
        "edges": len(narrative.edges),
        "endings": len(narrative.ending_nodes),
        "exclusions": result.data.get("exclusions") or [],
        "warnings": result.warnings[:20],
        "repair_count": len(repair.applied),
        "validation": validation.to_dict(),
        "module_outline": [
            {"id": item.get("id"), "title": item.get("title"), "module_type": item.get("module_type"), "summary": item.get("summary")}
            for item in scenario.get("modules", [])[:40]
        ],
        "apply_args": {
            "scenario_id": None,
            "payload": scenario,
            "bundle": result.data,
        },
    }
    return ToolResult.success(
        data,
        summary=f"待确认导入：{len(scenario.get('modules', []))} 个模块、{len(narrative.nodes)} 个节点、{len(narrative.edges)} 条边",
    )


def apply_import_script(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
    bundle = arguments.get("bundle") if isinstance(arguments.get("bundle"), dict) else None
    if not payload or not isinstance(bundle, dict):
        return ToolResult.failure("导入参数缺失，请重新预览")
    try:
        scenario = scenario_service.apply_create(actor, payload)
    except scenario_service.ScenarioServiceError as exc:
        return ToolResult.failure(exc.message)
    narrative_data = bundle.get("narrative") if isinstance(bundle.get("narrative"), dict) else {}
    if narrative_data.get("nodes"):
        try:
            descriptor = scenario_service.descriptor_path_for_actor(actor, scenario.get("id"))
            narrative = NarrativeBundle.from_dict(narrative_data)
            narrative.scenario_id = str(scenario.get("id"))
            narrative.scenario_version = str(scenario.get("scenario_version") or "1.0.0")
            repair = repair_bundle(narrative)
            narrative.meta = {
                **dict(narrative.meta or {}),
                "generator": "agent_import",
                "repair": repair.to_dict(),
            }
            save_bundle(descriptor.parent, narrative)
        except (scenario_service.ScenarioServiceError, ValueError) as exc:
            return ToolResult.failure(f"剧本已创建，但图谱写入失败：{exc}")
    return ToolResult.success(
        {
            "id": scenario.get("id"),
            "title": scenario.get("title"),
            "version": scenario.get("scenario_version"),
            "nodes": len(narrative_data.get("nodes") or []),
        },
        summary=f"已导入剧本「{scenario.get('title')}」并写入世界树",
    )


def _check_import_script(actor: Any, arguments: dict[str, Any]) -> str | None:
    if not str(arguments.get("content") or "").strip() and not isinstance(arguments.get("bundle"), dict):
        return "请提供剧本原文（content）或剧本包（bundle）"
    if isinstance(arguments.get("bundle"), dict) and not str(arguments.get("title") or "").strip():
        scenario_title = (arguments["bundle"].get("scenario") or {}).get("title") if isinstance(arguments["bundle"].get("scenario"), dict) else ""
        if not str(scenario_title or "").strip() and not str(arguments.get("title") or "").strip():
            return "剧本包缺少标题，请补 title"
    return None


def _check_save(actor: Any, arguments: dict[str, Any]) -> str | None:
    payload = _payload(arguments)
    if arguments.get("scenario_id") in (None, "", 0) and not payload.get("title"):
        return "创建剧本时 title 不能为空"
    try:
        if len(json.dumps(payload, ensure_ascii=False)) > _MAX_SCENARIO_JSON:
            return "剧本数据过大，请拆分或精简后重试"
    except (TypeError, ValueError):
        return "剧本数据无法序列化"
    return None


SCENARIO_TOOLS = [
    ToolSpec(
        name="scenarios.list",
        description="列出当前用户可编辑的剧本摘要（不含他人且无管理权限的剧本）。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 80},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        required_permission="scenarios.preview",
        handler=list_scenarios,
    ),
    ToolSpec(
        name="scenarios.get",
        description="读取一个可编辑剧本的完整数据（仅限本人创建或具备 scenarios.manage_all）。",
        parameters=schema({"scenario_id": {"type": "integer"}}, ["scenario_id"]),
        risk="read",
        required_permission="scenarios.preview",
        handler=get_scenario,
    ),
    ToolSpec(
        name="scenarios.import_script",
        description=(
            "把整份剧本原文或外部 Skill 生成的剧本包导入为新剧本，并同时创建世界树图谱。"
            "传 content 时由 AI 通读、剔除无关内容、按语义分模块、生成世界书与图谱；传 bundle 时只做校验修复。"
            "调用后先生成待确认预览（含剔除项、模块、节点/边统计），用户确认后才创建。"
        ),
        parameters=schema(
            {
                "content": {"type": "string", "maxLength": 400000},
                "bundle": {"type": "object"},
                "title": {"type": "string", "maxLength": 120},
                "author": {"type": "string", "maxLength": 80},
                "player_count": {"type": "integer", "minimum": 1, "maximum": 20},
            }
        ),
        risk="write",
        required_permission="scenarios.create",
        preview=preview_import_script,
        apply=apply_import_script,
        check=_check_import_script,
    ),
    ToolSpec(
        name="scenarios.validate",
        description=(
            "对剧本图谱跑一次确定性校验，返回问题清单与「一键修复」可自动处理的结构问题预览。"
            "只读，不改动剧本；用于解释报错原因并给出修复步骤。"
        ),
        parameters=schema({"scenario_id": {"type": "integer"}}, ["scenario_id"]),
        risk="read",
        required_permission="scenarios.preview",
        handler=validate_scenario,
    ),
    ToolSpec(
        name="scenarios.save",
        description=(
            "创建或更新剧本。不传 scenario_id 表示创建；字段通过 fields 传入。"
            "调用后只会生成待确认操作，用户确认后才写入并更新版本与知识库。"
        ),
        parameters=schema(
            {
                "scenario_id": {"type": "integer"},
                "title": {"type": "string", "maxLength": 120},
                "fields": {"type": "object"},
            }
        ),
        risk="write",
        required_permission="scenarios.create",
        preview=preview_save,
        apply=apply_save,
        check=_check_save,
    ),
]
