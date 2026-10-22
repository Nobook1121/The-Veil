"""剧本相关工具：列表、读取、保存（两段式确认）。"""

from __future__ import annotations

import json
from typing import Any

from trpg_server import scenario_service
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
