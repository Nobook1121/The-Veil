"""剧本导入后的道具包预生成（设计稿 §6.4 通道 2）。

由 ``auto_prepare_on_import`` 控制，默认关闭。只处理 ``module_type == "clue"``
的模块：把线索与模板清单交给 AI，映射出 PropSpec 后写入
``scenario-<id>/prop-packs/<pack_id>.json``。任何失败都只记 warning，不影响
导入与发布主流程。
"""
from __future__ import annotations

import json
import logging
import random
import string
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.json_store import write_json_atomic
from trpg_server.security import safe_join

from . import props_settings, registry, store
from .draft import extract_json

logger = logging.getLogger(__name__)

MAX_CLUES = 12
MAX_CLUE_CHARS = 500
MAX_PACK_PROPS = 20
_ALPHABET = string.ascii_lowercase + string.digits

SYSTEM_PROMPT = (
    "你是 TRPG 备团助手。把剧本线索映射到纸质道具模板，输出可直接渲染的道具 JSON。"
)

PROMPT_HEADER = (
    "任务：把下面的剧本线索映射成可交给玩家阅读的纸质道具。只输出 JSON：\n"
    '{"props":[{"clue_id":"线索 id","template_id":"模板 id","title":"道具标题",'
    '"fields":{"字段key":"文本"},"wear":{"stain":0.2},"style":{"paper":"aged"},'
    '"redaction":[{"field":"body","start":0,"end":4,"mode":"burn"}]}]}\n'
    "规则：\n"
    "1. template_id 只能从模板清单里选；fields 只能使用该模板的 key，required 必须非空。\n"
    "2. 一条线索最多生成一件道具；不确定时宁可不生成，不要编造关键真相。\n"
    "3. 需要隐藏的部分用 redaction 标出（start/end 是字段文本的下标），不要写进正文再要求忽略。\n"
    "4. 不要输出解释或 Markdown。\n"
)


class PrepareError(RuntimeError):
    """备道具失败（AI 不可用、输出无法解析）。"""


def _clue_modules(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    modules = scenario.get("modules") if isinstance(scenario.get("modules"), list) else []
    result: list[dict[str, Any]] = []
    for index, module in enumerate(modules):
        if not isinstance(module, Mapping):
            continue
        if str(module.get("module_type") or "").strip().lower() != "clue":
            continue
        result.append(
            {
                "clue_id": str(module.get("id") or f"clue-{index + 1}"),
                "title": str(module.get("title") or f"线索 {index + 1}"),
                "summary": str(module.get("summary") or "")[:200],
                "content": str(module.get("content") or "")[:MAX_CLUE_CHARS],
            }
        )
        if len(result) >= MAX_CLUES:
            break
    return result


def _template_brief() -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    for manifest in registry.list_templates():
        brief.append(
            {
                "id": manifest.get("id"),
                "name": manifest.get("name") or manifest.get("id"),
                "fields": [
                    {
                        "key": field.get("key"),
                        "label": field.get("label") or field.get("key"),
                        "type": field.get("type") or "text",
                        "max": field.get("max"),
                        "required": bool(field.get("required")),
                    }
                    for field in manifest.get("fields") or []
                    if isinstance(field, Mapping)
                ],
            }
        )
    return brief


def build_prompt(clues: list[dict[str, Any]]) -> str:
    payload = {"templates": _template_brief(), "clues": clues}
    return PROMPT_HEADER + json.dumps(payload, ensure_ascii=False)[:12000]


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_wear(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    return {key: _clamp(raw.get(key)) for key in ("stain", "fold", "burn", "tear") if key in raw}


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
        items.append(
            {
                "field": field,
                "start": start,
                "end": end,
                "mode": mode if mode in {"burn", "ink"} else "burn",
            }
        )
    return items


def _build_specs(
    payload: Any,
    *,
    scenario_id: str,
    max_field_chars: int | None,
) -> tuple[list[dict[str, Any]], int]:
    raw_items = payload.get("props") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_items, list):
        raise PrepareError("AI 返回内容缺少 props 数组")
    specs: list[dict[str, Any]] = []
    skipped = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for item in raw_items:
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        template_id = str(item.get("template_id") or "").strip()
        try:
            manifest = registry.get_template(template_id)
            raw_fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
            fields = registry.validate_fields(template_id, raw_fields, max_field_chars=max_field_chars)
        except registry.TemplateError as exc:
            logger.info("props prepare skipped clue=%s template=%s reason=%s", item.get("clue_id"), template_id, exc)
            skipped += 1
            continue
        specs.append(
            {
                "schema_version": 1,
                "id": store.new_prop_id(),
                "room_id": "",
                "template_id": template_id,
                "title": str(item.get("title") or "").strip()[:200] or str(manifest.get("name") or template_id),
                "fields": fields,
                "style": _normalize_style(item.get("style")),
                "wear": _normalize_wear(item.get("wear")),
                "redaction": _normalize_redaction(item.get("redaction"), fields),
                "visibility": {"mode": "hidden", "revealed_to": []},
                "source": {
                    "kind": "imported",
                    "clue_id": item.get("clue_id"),
                    "scenario_id": scenario_id,
                    "ai_request_id": None,
                },
                "assets": {},
                "created_by": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        if len(specs) >= MAX_PACK_PROPS:
            break
    return specs, skipped


def new_pack_id(now: float | None = None) -> str:
    stamp = int((time.time() if now is None else now) * 1000)
    suffix = "".join(random.SystemRandom().choice(_ALPHABET) for _ in range(6))
    return f"pack-{stamp}-{suffix}"


def prepare_scenario_props(
    scenario: Mapping[str, Any],
    *,
    scenario_dir: str | Path,
    config_dir: Any = None,
    settings: Mapping[str, Any] | None = None,
    request: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """把 clue 模块映射成 PropSpec 并写入道具包；失败只返回错误信息。"""
    settings = settings or props_settings(config_dir)
    if not settings.get("enabled", True) or not settings.get("auto_prepare_on_import"):
        return {"ok": True, "count": 0, "reason": "disabled"}

    clues = _clue_modules(scenario)
    if not clues:
        return {"ok": True, "count": 0, "reason": "no_clues"}

    requester = request
    if requester is None:
        try:
            from trpg_server.scenario_import_ai import build_ai_requester

            requester = build_ai_requester()
        except Exception as exc:  # noqa: BLE001 - 无平台/无 Flask 上下文都只降级
            logger.warning("props prepare requester unavailable: %s", exc)
            return {"ok": False, "count": 0, "reason": str(exc)[:160]}

    try:
        raw = requester(SYSTEM_PROMPT + "\n\n" + build_prompt(clues))
        payload = extract_json(raw)
        if payload is None:
            raise PrepareError("AI 返回内容不是合法 JSON")
        scenario_id = str(scenario.get("id") or "")
        specs, skipped = _build_specs(
            payload,
            scenario_id=scenario_id,
            max_field_chars=int(settings.get("max_field_chars") or 0) or None,
        )
        if not specs:
            return {"ok": True, "count": 0, "skipped": skipped, "reason": "no_valid_props"}
        pack_id = new_pack_id()
        path = safe_join(Path(scenario_dir), "prop-packs", f"{pack_id}.json")
        write_json_atomic(
            path,
            {
                "schema_version": 1,
                "pack_id": pack_id,
                "scenario_id": scenario_id,
                "source": "scenario_import",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "props": specs,
            },
        )
        logger.info("props prepare pack written scenario=%s pack=%s count=%s", scenario_id, pack_id, len(specs))
        return {"ok": True, "count": len(specs), "skipped": skipped, "pack_id": pack_id, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - 后置步骤绝不影响导入主流程
        logger.warning("props prepare failed scenario=%s error=%s", scenario.get("id"), exc, exc_info=True)
        return {"ok": False, "count": 0, "reason": str(exc)[:160]}


__all__ = [
    "MAX_CLUES",
    "PrepareError",
    "build_prompt",
    "new_pack_id",
    "prepare_scenario_props",
]
