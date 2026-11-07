"""KP 显式请求外链曲目的工具。

自动检索是主路径（服务端按剧情触发），本工具只处理「KP 明确想换一首」的场景：

- 同步返回 ``pending``，真正的检索在后台线程执行，绝不让工具调用卡住叙事流；
- 引擎不是 ``external`` 时直接返回 ``disabled``，不偷偷启用外链；
- 工具名对外经 ``to_api_tool_name`` 规整为 ``music_request_track``，
  满足各厂商 ``^[a-zA-Z0-9_-]+$`` 的校验。
"""
from __future__ import annotations

from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.immersive import external_music
from trpg_server.immersive.config import INTENSITIES, PROFILES, load_immersive_settings

MUSIC_REQUEST_TOOL = AgentTool(
    name="music.request_track",
    description=(
        "当剧情进入新的明确氛围、而当前外链曲目不再合适时，请求服务端为其重新检索一首"
        "许可合规的免费曲目。返回 pending 表示已在后台检索，不要等待；"
        "若返回 disabled 说明站点/房间未启用外链引擎，应继续使用程序化配乐。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": list(PROFILES),
                "description": "目标氛围档位；不填则沿用当前配乐档位。",
            },
            "intensity": {
                "type": "string",
                "enum": list(INTENSITIES),
                "description": "目标强度；不填则沿用当前强度。",
            },
            "scene": {
                "type": "string",
                "maxLength": 120,
                "description": "一句话描述当前场景（如「雨夜的废弃教堂」），用于氛围标签映射。",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "可选的额外氛围标签（英文或中文均可）。",
            },
        },
        "required": [],
    },
    handler=lambda arguments, context: _handle_request(arguments, context),
)


def _handle_request(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    room_dir = getattr(context, "room_dir", None)
    room_id = str(getattr(context, "room_id", None) or "")
    config_dir = getattr(context, "config_dir", None)
    if not room_dir or not room_id:
        return {"ok": False, "status": "unavailable", "reason": "no_room_context"}

    settings = load_immersive_settings(config_dir)
    info = context.room_info() if hasattr(context, "room_info") else {}
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    engine, engine_reason = external_music.resolve_engine(settings, rules)
    if engine != external_music.ENGINE_EXTERNAL:
        return {
            "ok": False,
            "status": "disabled",
            "engine": engine,
            "reason": engine_reason,
        }

    profile = str(arguments.get("profile") or "").strip().lower()
    intensity = str(arguments.get("intensity") or "").strip().lower()
    if profile not in PROFILES:
        profile = str(settings.get("music_default_profile") or "daily")
    if intensity not in INTENSITIES:
        intensity = str(settings.get("music_default_intensity") or "medium")
    tags_raw = arguments.get("tags")
    tags = (
        [str(item).strip() for item in tags_raw if str(item).strip()][:8]
        if isinstance(tags_raw, (list, tuple))
        else []
    )
    request_id = external_music.submit(
        room_dir=room_dir,
        room_id=room_id,
        scenario_id=str(info.get("scenario_id") or ""),
        profile=profile,
        intensity=intensity,
        scene=str(arguments.get("scene") or "")[:120],
        extra_tags=tags,
        settings=settings,
        config_dir=config_dir,
        house_rules=rules,
        trigger="kp",
    )
    if request_id is None:
        return {
            "ok": True,
            "status": "pending",
            "reason": "already_running",
            "note": "已有外链检索在进行中，无需重复请求。",
        }
    return {
        "ok": True,
        "status": "pending",
        "request_id": request_id,
        "engine": engine,
        "note": "检索在后台进行；结果会通过房间事件下发，期间保持当前底床。",
    }
