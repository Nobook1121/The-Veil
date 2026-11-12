"""AI 起草道具字段（设计稿 §6.4 通道 1）。

只做一件小事：把模板 schema + 少量上下文拼成一次小请求，调当前 KP 平台，
返回经过模板校验的 ``fields`` / ``wear`` / ``style``。不塞整份剧本，输入按
字符上限裁剪；失败重试一次，仍失败抛 :class:`DraftError`（路由层转成可读
错误，前端提示手动填写，编辑器不关闭）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Mapping

from trpg_server.agents.ai_request import build_request_payload
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.ai_platform_resolver import (
    CAPABILITY_CHAT,
    mark_platform_failure,
    mark_platform_success,
    resolve_platform_targets,
)

from . import registry

logger = logging.getLogger(__name__)

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
MAX_CLUES_CHARS = 800
MAX_ERA_CHARS = 80
MAX_PLACE_CHARS = 80
MAX_TONE_CHARS = 60

SYSTEM_PROMPT = (
    "你是 TRPG 道具文案助手。根据模板字段 schema 写出一件可信的年代道具。"
    "只输出一个 JSON 对象，不要解释、不要 Markdown。"
)

RETRY_SUFFIX = (
    "\n\n上一次输出不是合法 JSON。请只输出一个 JSON 对象，字段值必须是字符串，不要附加任何说明。"
)


class DraftError(RuntimeError):
    """AI 起草失败（平台不可用、两次都拿不到合法 JSON）。"""


def extract_json(content: Any) -> dict[str, Any] | None:
    """从可能带 Markdown 围栏/前后缀的正文里取出一个 JSON 对象。"""
    text = str(content or "").strip()
    block = JSON_BLOCK_RE.search(text)
    if block:
        text = block.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _field_schema(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    for field in manifest.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        schema.append(
            {
                "key": field.get("key"),
                "label": field.get("label") or field.get("key"),
                "type": field.get("type") or "text",
                "max": field.get("max"),
                "required": bool(field.get("required")),
            }
        )
    return schema


def build_prompt(template_id: str, context: Mapping[str, Any]) -> str:
    manifest = registry.get_template(template_id)
    payload = {
        "template_id": template_id,
        "template_name": manifest.get("name") or template_id,
        "fields": _field_schema(manifest),
        "era": str(context.get("era") or "").strip()[:MAX_ERA_CHARS],
        "place": str(context.get("place") or "").strip()[:MAX_PLACE_CHARS],
        "known_clues": str(context.get("clues") or "").strip()[:MAX_CLUES_CHARS],
        "tone": str(context.get("tone") or "").strip()[:MAX_TONE_CHARS],
    }
    return (
        "请为下面这件道具起草内容。输入是精简后的上下文，不要编造与已知线索无关的重大设定。\n"
        "输出 JSON："
        '{"title":"道具标题","fields":{"字段key":"文本"},"wear":{"stain":0.2},'
        '"style":{"paper":"ivory","ink":"#2b2b2b","accent":"#7a5c3e"}}\n'
        "规则：\n"
        "1. fields 只能使用 schema 里的 key；required 字段必须非空。\n"
        "2. 文本要符合时代与语气，像真实文件，不要写“这是一封信”之类的说明。\n"
        "3. wear 取值 0–1；style 可省略。\n"
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def _request_via_platform(prompt: str, *, config_dir: Any) -> tuple[str, str]:
    targets = resolve_platform_targets(config_dir, CAPABILITY_CHAT)
    if not targets:
        raise DraftError("没有可用的 KP 平台（需启用且勾选「对话 / KP」用途）。")
    errors: list[str] = []
    for target in targets:
        platform = str(target.get("platform") or "")
        try:
            body = build_request_payload(
                platform,
                str(target.get("model") or ""),
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                config_dir=config_dir,
                params={"temperature": 0.75, "max_tokens": 1200},
            )
            headers = dict(target.get("headers") or {})
            headers.setdefault("Content-Type", "application/json")
            has_auth = any(str(key).lower() == "authorization" for key in headers)
            if target.get("api_key") and not has_auth:
                headers["Authorization"] = f"Bearer {target['api_key']}"
            response = post_with_retry(
                str(target.get("endpoint") or ""),
                headers=headers,
                payload=body,
                timeout=max(20, min(120, int(target.get("timeout") or 45))),
                max_retries=0,
                label="props_ai_draft",
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {str(response.text or '')[:160]}")
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else {}
            content = str((message or {}).get("content") or (message or {}).get("reasoning_content") or "")
            if not content.strip():
                raise RuntimeError("empty response")
            mark_platform_success(platform, CAPABILITY_CHAT)
            return content, platform
        except Exception as exc:  # noqa: BLE001 - 单个平台失败顺延到下一个
            errors.append(f"{platform}: {exc}")
            mark_platform_failure(platform, CAPABILITY_CHAT)
            logger.warning("props_ai_draft platform failed platform=%s error=%s", platform, exc)
    raise DraftError("AI 起草失败，请手动填写。" + ("（" + errors[-1] + "）" if errors else ""))


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_wear(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: _clamp(raw.get(key))
        for key in ("stain", "fold", "burn", "tear")
        if key in raw
    }


def _normalize_style(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in ("paper", "ink", "accent", "font"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value[:40]
    return result


def _clean_payload(
    template_id: str, data: Mapping[str, Any], *, max_field_chars: int | None
) -> dict[str, Any]:
    raw_fields = data.get("fields") if isinstance(data.get("fields"), Mapping) else {}
    fields = registry.validate_fields(template_id, raw_fields, max_field_chars=max_field_chars)
    return {
        "title": str(data.get("title") or "").strip()[:200],
        "fields": fields,
        "wear": _normalize_wear(data.get("wear")),
        "style": _normalize_style(data.get("style")),
    }


def draft_fields(
    template_id: str,
    context: Mapping[str, Any] | None = None,
    *,
    config_dir: Any = None,
    max_field_chars: int | None = None,
    request: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """生成一份可用的道具草稿；失败抛 :class:`DraftError`。"""
    key = str(template_id or "").strip()
    try:
        registry.get_template(key)
    except registry.UnknownTemplate as exc:
        raise DraftError(str(exc)) from exc

    prompt = build_prompt(key, context or {})
    requester = request or (lambda text: _request_via_platform(text, config_dir=config_dir)[0])
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = requester(prompt if attempt == 0 else prompt + RETRY_SUFFIX)
            data = extract_json(raw)
            if data is None:
                raise ValueError("AI 返回内容不是合法 JSON")
            return _clean_payload(key, data, max_field_chars=max_field_chars)
        except (registry.FieldError, ValueError) as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - 网络/平台错误也重试一次
            last_error = exc
    raise DraftError("AI 起草失败，请手动填写。" + (f"（{last_error}）" if last_error else ""))


__all__ = ["DraftError", "build_prompt", "draft_fields", "extract_json"]
