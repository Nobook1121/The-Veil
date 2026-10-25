"""角色卡相关工具：列表、读取、武器目录、保存（两段式确认）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server import character_service, weapon_catalog
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema
from trpg_server.settings import WEAPONS_DIR

_MAX_CARD_JSON = 40000


def _payload(arguments: dict[str, Any]) -> dict[str, Any]:
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), dict) else {}
    payload = dict(fields)
    payload["name"] = str(arguments.get("name") or payload.get("name") or "").strip()
    if arguments.get("player_id"):
        payload["playerId"] = str(arguments["player_id"])
    return payload


def _resolve_id(actor: Any, arguments: dict[str, Any]) -> str:
    character_id = str(arguments.get("character_id") or "").strip()
    return character_id or character_service.new_character_id(actor)


def list_characters(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    items = character_service.list_for_actor(
        actor,
        query=str(arguments.get("query") or ""),
        limit=int(arguments.get("limit") or 20),
    )
    return ToolResult.success({"items": items, "count": len(items)}, summary=f"找到 {len(items)} 张角色卡")


def get_character(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    character = character_service.get_for_actor(actor, str(arguments.get("character_id") or ""))
    return ToolResult.success(character, summary=f"读取角色卡「{character.get('name')}」")


def list_weapons(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    weapons_dir = Path(current_app.config.get("WEAPONS_DIR", WEAPONS_DIR))
    weapons = weapon_catalog.load_preset_weapons(weapons_dir)
    categories: list[dict[str, Any]] = []
    for weapon in weapons:
        category = str(weapon.get("category") or "")
        if category and not any(item["name"] == category for item in categories):
            categories.append({"name": category, "order": weapon.get("categoryOrder")})
    result: dict[str, Any] = {"categories": categories, "weapons": weapons, "count": len(weapons)}
    character_id = str(arguments.get("character_id") or "").strip()
    if character_id:
        try:
            result["customWeapons"] = character_service.get_custom_weapons(actor, character_id)
        except character_service.CharacterServiceError as exc:
            return ToolResult.failure(exc.message)
    return ToolResult.success(result, summary=f"读取到 {len(weapons)} 把预设武器")


def preview_save(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    character_id = _resolve_id(actor, arguments)
    payload = _payload(arguments)
    try:
        preview = character_service.preview_save(actor, character_id, payload)
    except character_service.CharacterServiceError as exc:
        return ToolResult.failure(exc.message)
    preview["apply_args"] = {"character_id": character_id, "payload": payload}
    return ToolResult.success(preview, summary=str(preview.get("summary") or "保存角色卡"))


def apply_save(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    character_id = str(arguments.get("character_id") or "")
    payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
    try:
        character = character_service.apply_save(actor, character_id, payload)
    except character_service.CharacterServiceError as exc:
        return ToolResult.failure(exc.message)
    return ToolResult.success(
        {"id": character.get("id"), "name": character.get("name")},
        summary=f"已保存角色卡「{character.get('name')}」",
    )


def _check_save(actor: Any, arguments: dict[str, Any]) -> str | None:
    payload = _payload(arguments)
    if not payload.get("name"):
        return "角色卡名称不能为空"
    if arguments.get("player_id") and str(arguments["player_id"]) != str(actor.user_id):
        if not actor.has("characters.manage_all"):
            return "只有具备 characters.manage_all 的管理员才能把角色卡指派给他人"
    try:
        if len(json.dumps(payload, ensure_ascii=False)) > _MAX_CARD_JSON:
            return "角色卡数据过大，请精简后重试"
    except (TypeError, ValueError):
        return "角色卡数据无法序列化"
    return None


CHARACTER_TOOLS = [
    ToolSpec(
        name="characters.list",
        description="列出当前用户可访问的角色卡，可按名称或 ID 过滤，最多返回 50 条摘要。",
        parameters=schema(
            {
                "query": {"type": "string", "maxLength": 80},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        required_permission="characters.manage_own",
        handler=list_characters,
    ),
    ToolSpec(
        name="characters.get",
        description="读取一张可访问角色卡的完整内容（不含他人私有卡）。",
        parameters=schema({"character_id": {"type": "string", "maxLength": 80}}, ["character_id"]),
        risk="read",
        required_permission="characters.manage_own",
        handler=get_character,
    ),
    ToolSpec(
        name="weapons.list",
        description=(
            "读取预设武器目录（含分类、id、名称、技能、伤害、射程等），可选附带某张角色卡的自定义武器库。"
            "为角色卡选武器时，把返回武器的 id 填到 characters.save 的 fields.weapons[].presetId。"
            "自定义武器则把完整字段放入 fields.customWeapons（会存入该角色卡自己的武器库）。"
        ),
        parameters=schema(
            {
                "character_id": {"type": "string", "maxLength": 80},
            }
        ),
        risk="read",
        required_permission="characters.manage_own",
        handler=list_weapons,
    ),
    ToolSpec(
        name="characters.save",
        description=(
            "创建或更新角色卡。character_id 为空时由服务端生成新 ID。"
            "调用后只会生成待确认操作，用户点击确认才会落盘。"
            "fields.weapons 每项可直接给完整武器字段，或用 presetId 引用 weapons.list 中的预设武器"
            "（可用 damage/range 等字段做覆盖）；fields.customWeapons 接受自定义武器数组，"
            "会写入角色卡目录内的 weapons.json。"
        ),
        parameters=schema(
            {
                "character_id": {"type": "string", "maxLength": 80},
                "name": {"type": "string", "maxLength": 80},
                "player_id": {"type": "string", "maxLength": 32},
                "fields": {"type": "object"},
            },
            ["name"],
        ),
        risk="write",
        required_permission="characters.create",
        preview=preview_save,
        apply=apply_save,
        check=_check_save,
    ),
]
