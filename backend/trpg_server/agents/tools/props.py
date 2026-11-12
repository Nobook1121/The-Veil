"""KP 道具工具：props.create / props.reveal / props.update（设计稿 §6.1）。

工具只做「校验 + 调 props 包」，不抛异常：所有可预期失败都返回可读文本，
让模型自行改用可用模板或换一种做法。创建默认 hidden，必须显式 reveal。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Mapping

from trpg_server.agents.tools.base import AgentTool
from trpg_server.props import policy, registry, store
from trpg_server.props import props_settings
from trpg_server.room_ops.storage import _read_messages, _timestamp, _write_messages

logger = logging.getLogger(__name__)

TOOL_CREATE = "props.create"
TOOL_REVEAL = "props.reveal"
TOOL_UPDATE = "props.update"
PROPS_TOOL_NAMES = {TOOL_CREATE, TOOL_REVEAL, TOOL_UPDATE}

_WEAR_KEYS = ("stain", "fold", "burn", "tear")
_MISSING_PROP_TEXT = "该道具不存在或不可见。"


def _settings(context: Any) -> dict[str, Any]:
    return props_settings(getattr(context, "config_dir", None))


def _room_dir(context: Any):
    room_dir = getattr(context, "room_dir", None)
    if room_dir is None:
        raise ValueError("当前不在房间内")
    return room_dir


def _templates_hint() -> str:
    items = [
        f"{item.get('id')}({item.get('name') or item.get('id')})"
        for item in registry.list_templates()
    ]
    return "模板不存在，可用：" + (" / ".join(items) if items else "（无）")


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_wear(raw: Any, previous: Mapping[str, Any] | None = None) -> dict[str, float]:
    result = {key: _clamp((previous or {}).get(key)) for key in _WEAR_KEYS}
    if isinstance(raw, Mapping):
        for key in _WEAR_KEYS:
            if key in raw:
                result[key] = _clamp(raw.get(key))
    return result


def _normalize_style(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in ("paper", "ink", "accent", "font"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value[:40]
    return result


def _normalize_redaction(raw: Any, fields: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "").strip()
        if field not in fields:
            continue
        try:
            start = int(entry.get("start"))
            end = int(entry.get("end"))
        except (TypeError, ValueError):
            continue
        length = len(str(fields.get(field) or ""))
        start = max(0, min(start, length))
        end = max(0, min(end, length))
        if end <= start:
            continue
        mode = str(entry.get("mode") or "burn").strip().lower()
        items.append({"field": field, "start": start, "end": end, "mode": mode if mode in {"burn", "ink"} else "burn"})
    return items


def _truncation_notice(
    template_id: str, raw_fields: Mapping[str, Any], final_fields: Mapping[str, Any]
) -> tuple[dict[str, dict[str, int]], str]:
    manifest = registry.get_template(template_id)
    raw_map = raw_fields if isinstance(raw_fields, Mapping) else {}
    details: dict[str, dict[str, int]] = {}
    for field in manifest.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        if not key:
            continue
        original = len(str(raw_map.get(key) or ""))
        kept = len(str(final_fields.get(key) or ""))
        if original > kept:
            details[key] = {"limit": kept, "original": original}
    if not details:
        return {}, ""
    parts = [f"{key}({item['original']}→{item['limit']})" for key, item in details.items()]
    return details, "以下字段超长，已按模板上限截断：" + "、".join(parts)


def _member_ids(context: Any) -> set[str]:
    info = context.room_info() if hasattr(context, "room_info") else {}
    members = info.get("members") if isinstance(info, Mapping) else []
    result: set[str] = set()
    for member in members or []:
        if not isinstance(member, Mapping):
            continue
        if member.get("is_active") is False or member.get("status") == "removed":
            continue
        if member.get("user_id") is not None:
            result.add(str(member.get("user_id")))
    return result


def _parse_recipients(raw: Any) -> tuple[str, list[int]] | None:
    """返回 ``(mode, ids)``；``mode`` 为 ``all`` 或 ``partial``。"""
    if raw == "all" or (isinstance(raw, str) and raw.strip().lower() == "all"):
        return ("all", [])
    if isinstance(raw, (list, tuple)):
        if any(str(item).strip().lower() == "all" for item in raw):
            return ("all", [])
        ids: list[int] = []
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value not in ids:
                ids.append(value)
        return ("partial", ids) if ids else None
    return None


def _deliver(
    context: Any,
    spec: Mapping[str, Any],
    mode: str,
    recipients: list[int],
    message: str = "",
) -> dict[str, Any]:
    room_dir = _room_dir(context)
    room_id = str(getattr(context, "room_id", "") or "")
    visibility = {
        "mode": "revealed" if mode == "all" else "partial",
        "revealed_to": [] if mode == "all" else recipients,
    }
    updated = store.update_prop(
        room_dir,
        str(spec.get("id")),
        lambda current: {**current, "visibility": visibility, "updated_at": _timestamp()},
    )
    if updated is None:
        return {"ok": False, "error": _MISSING_PROP_TEXT}

    content = str(message or "").strip() or f"递来一件道具：{updated.get('title') or ''}"
    payload = {
        "id": uuid.uuid4().hex,
        "type": "prop",
        "sender_id": None,
        "sender_name": "KP",
        "avatar": "/assets/avatars/default_kp.jpg",
        "content": content[:500],
        "time": time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": {
            "prop_id": str(updated.get("id")),
            "preview": str(updated.get("template_id") or ""),
            "revealed_to": recipients,
        },
    }
    messages = _read_messages(room_dir)
    messages.append(payload)
    _write_messages(room_dir, messages)
    try:
        from trpg_server.immersive import broadcast

        broadcast.emit("new_message", {**payload, "room_id": room_id})
    except Exception:  # noqa: BLE001 - 广播失败不回滚已落库的消息
        logger.warning("props tool reveal broadcast failed room=%s", room_id, exc_info=True)
    return {"ok": True, "message": payload, "visibility": visibility}


def execute_create_prop(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = _settings(context)
    if not settings.get("enabled", True):
        return {"ok": False, "error": "道具工坊未启用，无法创建道具。"}

    created = int(getattr(context, "tool_state", {}).get("props_created", 0) or 0)
    if created >= 1:
        return {"ok": False, "error": "本回合已创建过 1 件道具；请先完成叙事，需要修改请用 props.update。"}

    room_dir = _room_dir(context)
    limit = int(settings.get("max_props_per_room") or 200)
    if len(store.list_index(room_dir)) >= limit:
        return {
            "ok": False,
            "error": f"本房间道具已达上限（{limit} 件），无法再创建；请修改或复用现有道具。",
        }

    template_id = str(arguments.get("template_id") or "").strip()
    try:
        manifest = registry.get_template(template_id)
    except registry.UnknownTemplate:
        return {"ok": False, "error": _templates_hint()}

    raw_fields = arguments.get("fields") if isinstance(arguments.get("fields"), Mapping) else {}
    max_chars = int(settings.get("max_field_chars") or 0) or None
    try:
        fields = registry.validate_fields(template_id, raw_fields, max_field_chars=max_chars)
    except registry.FieldError as exc:
        return {"ok": False, "error": f"字段不合法：{exc}。请按模板 {template_id} 的字段重新填写。"}

    now = _timestamp()
    spec = {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": str(getattr(context, "room_id", "") or ""),
        "template_id": template_id,
        "title": str(arguments.get("title") or "").strip()[:200] or str(manifest.get("name") or template_id),
        "fields": fields,
        "style": _normalize_style(arguments.get("style")),
        "wear": _normalize_wear(arguments.get("wear")),
        "redaction": _normalize_redaction(arguments.get("redaction"), fields),
        "visibility": {"mode": "hidden", "revealed_to": []},
        "source": {
            "kind": "ai",
            "clue_id": None,
            "scenario_id": None,
            "ai_request_id": None,
        },
        "assets": {},
        "created_by": getattr(context, "user_id", None),
        "created_at": now,
        "updated_at": now,
    }
    try:
        store.write_prop(room_dir, spec)
    except Exception as exc:  # noqa: BLE001 - 存储失败也只回告模型
        logger.warning("props.create write failed", exc_info=True)
        return {"ok": False, "error": f"道具写入失败：{exc}"}

    context.tool_state["props_created"] = created + 1
    truncated, notice = _truncation_notice(template_id, raw_fields, fields)

    delivered = 0
    warn = ""
    reveal_error = ""
    parsed = _parse_recipients(arguments.get("reveal_to"))
    if parsed is not None:
        mode, recipients = parsed
        if mode == "partial":
            allowed = _member_ids(context)
            recipients = [value for value in recipients if str(value) in allowed]
            if not recipients:
                reveal_error = "reveal_to 中没有房间内的玩家，已按仅 KP 可见保存。"
        if not reveal_error:
            result = _deliver(context, spec, mode, recipients, str(arguments.get("message") or ""))
            if result.get("ok"):
                delivered = 1
                spec = store.read_prop(room_dir, str(spec["id"])) or spec
            else:
                reveal_error = str(result.get("error") or "投递失败")

    if delivered:
        target = "全体成员" if parsed and parsed[0] == "all" else "指定玩家"
        summary = f"已创建《{spec['title']}》{spec['id']}（模板 {template_id}），已投递给{target}。"
    else:
        summary = (
            f"已创建《{spec['title']}》{spec['id']}（模板 {template_id}，仅 KP 可见）。"
            "调用 props.reveal 可发给玩家。"
        )
        if reveal_error:
            summary += "\n" + reveal_error
    if notice:
        summary += "\n" + notice

    return {
        "ok": True,
        "prop_id": spec["id"],
        "template_id": template_id,
        "title": spec["title"],
        "visibility": (spec.get("visibility") or {}).get("mode", "hidden"),
        "truncated": truncated,
        "summary": summary,
    }


def execute_reveal_prop(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = _settings(context)
    if not settings.get("enabled", True):
        return {"ok": False, "error": "道具工坊未启用，无法投递道具。"}
    room_dir = _room_dir(context)
    prop_id = str(arguments.get("prop_id") or "").strip()
    spec = store.read_prop(room_dir, prop_id) if store.is_valid_prop_id(prop_id) else None
    if spec is None:
        return {"ok": False, "error": _MISSING_PROP_TEXT}

    parsed = _parse_recipients(arguments.get("to"))
    if parsed is None:
        return {"ok": False, "error": "to 需要是玩家 id 数组或字符串 all。"}
    mode, recipients = parsed
    if mode == "partial":
        allowed = _member_ids(context)
        recipients = [value for value in recipients if str(value) in allowed]
        if not recipients:
            return {"ok": False, "error": "收件人不在房间内；请先查看房间成员再投递。"}

    result = _deliver(context, spec, mode, recipients, str(arguments.get("message") or ""))
    if not result.get("ok"):
        return result
    target = "全体成员" if mode == "all" else "玩家 " + "、".join(str(value) for value in recipients)
    return {
        "ok": True,
        "prop_id": prop_id,
        "visibility": result["visibility"],
        "summary": f"已将《{spec.get('title') or prop_id}》{prop_id} 发给{target}。",
    }


def execute_update_prop(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = _settings(context)
    if not settings.get("enabled", True):
        return {"ok": False, "error": "道具工坊未启用，无法修改道具。"}
    room_dir = _room_dir(context)
    prop_id = str(arguments.get("prop_id") or "").strip()
    spec = store.read_prop(room_dir, prop_id) if store.is_valid_prop_id(prop_id) else None
    if spec is None:
        return {"ok": False, "error": _MISSING_PROP_TEXT}

    if not any(key in arguments for key in ("fields", "wear", "add_redaction")):
        return {"ok": False, "error": "没有提供可更新的内容；请给 fields、wear 或 add_redaction。"}

    failure: dict[str, str] = {}
    raw_fields = arguments.get("fields") if isinstance(arguments.get("fields"), Mapping) else {}
    truncated: dict[str, dict[str, int]] = {}
    notice = ""
    added = 0

    def mutate(current: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal truncated, notice, added
        try:
            if raw_fields:
                merged = dict(current.get("fields") or {})
                merged.update(raw_fields)
                current["fields"] = registry.validate_fields(
                    str(current.get("template_id") or ""),
                    merged,
                    max_field_chars=int(settings.get("max_field_chars") or 0) or None,
                )
                truncated, notice = _truncation_notice(
                    str(current.get("template_id") or ""), merged, current["fields"]
                )
            if "wear" in arguments:
                current["wear"] = _normalize_wear(arguments.get("wear"), current.get("wear"))
            if "add_redaction" in arguments:
                merged_redactions = list(current.get("redaction") or [])
                merged_redactions.extend(
                    _normalize_redaction(arguments.get("add_redaction"), current.get("fields") or {})
                )
                current["redaction"] = _clamp_redaction(current, merged_redactions)
                added = max(0, len(current["redaction"]) - len(spec.get("redaction") or []))
            current["updated_at"] = _timestamp()
            return current
        except registry.TemplateError as exc:
            failure["error"] = f"字段不合法：{exc}"
            return None

    updated = store.update_prop(room_dir, prop_id, mutate)
    if failure:
        return {"ok": False, "error": failure["error"]}
    if updated is None:
        return {"ok": False, "error": _MISSING_PROP_TEXT}

    summary = f"已更新《{updated.get('title') or prop_id}》{prop_id}。"
    if added:
        summary += f"已追加 {added} 段脱敏。"
    if notice:
        summary += "\n" + notice
    return {
        "ok": True,
        "prop_id": prop_id,
        "truncated": truncated,
        "redaction_count": len(updated.get("redaction") or []),
        "summary": summary,
    }


def _clamp_redaction(spec: Mapping[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = spec.get("fields") if isinstance(spec.get("fields"), Mapping) else {}
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or "")
        if field not in fields:
            continue
        length = len(str(fields.get(field) or ""))
        start = max(0, min(int(item.get("start") or 0), length))
        end = max(0, min(int(item.get("end") or 0), length))
        if end <= start:
            continue
        result.append({**dict(item), "start": start, "end": end})
    return result


CREATE_PROP_TOOL = AgentTool(
    name=TOOL_CREATE,
    description=(
        "Create a TRPG prop from a template (letter/newspaper/etc). The prop is hidden to players "
        "until props.reveal is called. Use template_id from the 【道具】 template list, and fill "
        "fields with the template's field keys."
    ),
    parameters={
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "Template id from the prop template list."},
            "title": {"type": "string", "description": "Display title shown to the KP."},
            "fields": {"type": "object", "description": "Template field keys mapped to text."},
            "style": {
                "type": "object",
                "properties": {
                    "paper": {"type": "string"},
                    "ink": {"type": "string"},
                    "accent": {"type": "string"},
                },
            },
            "wear": {
                "type": "object",
                "properties": {
                    "stain": {"type": "number", "minimum": 0, "maximum": 1},
                    "fold": {"type": "number", "minimum": 0, "maximum": 1},
                    "burn": {"type": "number", "minimum": 0, "maximum": 1},
                    "tear": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
            "redaction": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "mode": {"type": "string", "enum": ["burn", "ink"]},
                    },
                    "required": ["field", "start", "end"],
                },
            },
            "reveal_to": {
                "description": "Optional: array of player user ids, or the string 'all'.",
                "oneOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "string", "enum": ["all"]},
                ],
            },
            "message": {"type": "string", "description": "Optional line said while handing it over."},
        },
        "required": ["template_id", "title", "fields"],
    },
    handler=execute_create_prop,
)

REVEAL_PROP_TOOL = AgentTool(
    name=TOOL_REVEAL,
    description=(
        "Reveal a hidden prop to players. Use to='all' for the whole room, or an array of player "
        "user ids. The prop card is posted into the room chat."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prop_id": {"type": "string"},
            "to": {
                "oneOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "string", "enum": ["all"]},
                ]
            },
            "message": {"type": "string", "description": "Optional line said while handing it over."},
        },
        "required": ["prop_id", "to"],
    },
    handler=execute_reveal_prop,
)

UPDATE_PROP_TOOL = AgentTool(
    name=TOOL_UPDATE,
    description=(
        "Update an existing prop: merge fields, adjust wear, or append redaction ranges. "
        "Use add_redaction to burn/ink out text instead of writing secrets in plain sight."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prop_id": {"type": "string"},
            "fields": {"type": "object"},
            "wear": {"type": "object"},
            "add_redaction": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "mode": {"type": "string", "enum": ["burn", "ink"]},
                    },
                    "required": ["field", "start", "end"],
                },
            },
        },
        "required": ["prop_id"],
    },
    handler=execute_update_prop,
)

PROPS_TOOLS = [CREATE_PROP_TOOL, REVEAL_PROP_TOOL, UPDATE_PROP_TOOL]


__all__ = [
    "CREATE_PROP_TOOL",
    "PROPS_TOOL_NAMES",
    "PROPS_TOOLS",
    "REVEAL_PROP_TOOL",
    "UPDATE_PROP_TOOL",
    "execute_create_prop",
    "execute_reveal_prop",
    "execute_update_prop",
]
