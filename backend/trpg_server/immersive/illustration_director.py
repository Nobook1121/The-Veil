"""AI KP 配图决策：判断本回合是否需要配图，并产出场景/NPC 提示词。

设计约束（与既有音乐导演一致）：

1. **绝不阻塞叙事**。由 :func:`submit` 提交到后台线程，叙事响应先返回；
   失败/非法结构只会回退到规则结果或跳过，不影响主流程。
2. **走既有 AI 链路**。平台解析、请求构造、重试与超时复用
   ``ai_platform_config`` / ``build_request_payload`` / ``post_with_retry``，
   并单独记录每日预算与用量。
3. **严格校验**。只接受合法 JSON；``should_illustrate`` 必须是布尔值，
   提示词必须非空；NPC 名称去重、限长，非法项丢弃而不是整份作废。
4. **同一房间同时只跑一个决策**，避免重复计费。
5. **不依赖 Flask**，可在后台线程、房间控制台与测试中复用。
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server.agents.ai_request import build_request_payload
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.immersive.director import recent_context, resolve_ai_target
from trpg_server.immersive.illustration import build_prompt_for_scene
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

KP_USAGE_PATH = RUNTIME_DIR / "immersive" / "illustration_kp_usage.json"
MAX_NPCS = 3

_LOCK = threading.RLock()
_INFLIGHT: set[str] = set()
_EXECUTOR = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="immersive-kp")

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)

SYSTEM_PROMPT = (
    "你是 TRPG 的「视觉 KP」。只输出一个 JSON 对象，不要输出解释、Markdown 或代码块。\n"
    "任务：判断本轮叙事是否值得生成一张配图；如果需要，写出可直接交给文生图模型的提示词。\n"
    "JSON schema：\n"
    "{\n"
    '  "should_illustrate": true|false,\n'
    '  "reason": "不超过 120 字的判断理由",\n'
    '  "scene_prompt": "场景图提示词，不超过 800 字；不需要配图时留空",\n'
    '  "npcs": [{"name": "NPC 名称，不超过 32 字", "prompt": "头像提示词，不超过 500 字"}]\n'
    "}\n"
    "规则：\n"
    "- 只在场景切换、关键发现、战斗高潮、情绪转折等值得定格的时刻配图；日常寒暄不要配图。\n"
    "- 场景提示词只描述环境、光线、构图与氛围，不写对白，不写文字/水印/logo/UI。\n"
    "- NPC 头像提示词描述头肩构图、神态、服饰与光线，背景简洁，避免与场景图混淆。\n"
    "- 仅列出本轮真正出场且值得画像的 NPC；没有就返回空数组。\n"
    "- 提示词必须忠于叙事，不添加未出现的角色或现代元素。"
)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_snapshot(*, usage_path: Path | None = None) -> dict[str, Any]:
    data = read_json(Path(usage_path) if usage_path else KP_USAGE_PATH, default={})
    if not isinstance(data, dict) or data.get("date") != _today():
        return {"date": _today(), "calls": 0}
    return {"date": _today(), "calls": int(data.get("calls") or 0)}


def reserve_call(*, budget: int, usage_path: Path | None = None) -> tuple[bool, str, int]:
    path = Path(usage_path) if usage_path else KP_USAGE_PATH
    with _LOCK:
        data = read_json(path, default={})
        if not isinstance(data, dict) or data.get("date") != _today():
            data = {"date": _today(), "calls": 0}
        used = int(data.get("calls") or 0)
        if budget > 0 and used >= budget:
            return False, "illustration_kp_budget_exhausted", used
        data["calls"] = used + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, data)
        except OSError:
            logger.warning("Failed to persist illustration KP usage", exc_info=True)
        return True, "", used + 1


def _extract_json(content: str) -> dict[str, Any] | None:
    text = str(content or "").strip()
    block = _JSON_BLOCK_RE.search(text)
    if block:
        text = block.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def validate_decision(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """严格校验 KP 输出；返回（归一化结果，错误列表）。"""
    if not isinstance(raw, dict):
        return None, ["not_an_object"]
    should = raw.get("should_illustrate")
    if not isinstance(should, bool):
        return None, ["should_illustrate_not_bool"]
    scene_prompt = re.sub(r"\s+", " ", str(raw.get("scene_prompt") or "")).strip()[:800]
    if should and not scene_prompt:
        return None, ["missing_scene_prompt"]
    npcs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("npcs") if isinstance(raw.get("npcs"), list) else []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:32]
        if not name:
            continue
        normalized = name.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        prompt = re.sub(r"\s+", " ", str(item.get("prompt") or "")).strip()[:500]
        npcs.append({"name": name, "prompt": prompt})
        if len(npcs) >= MAX_NPCS:
            break
    return (
        {
            "should_illustrate": should,
            "reason": re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()[:120],
            "scene_prompt": scene_prompt,
            "npcs": npcs,
        },
        [],
    )


def fallback_decision(
    *,
    trigger: str,
    analysis: dict[str, Any],
    important_signal: bool = False,
) -> dict[str, Any]:
    """规则降级：重要档只在关键场景出图，其余到点即出；无法识别 NPC。"""
    should = trigger in {"frequent", "turn_interval"} or (
        trigger == "important" and important_signal
    )
    scene = str(analysis.get("scene") or "未知场景")
    return {
        "should_illustrate": should,
        "reason": "rule_fallback" if should else "rule_fallback_not_important",
        "scene_prompt": build_prompt_for_scene(
            scene,
            str(analysis.get("profile") or "daily"),
            str(analysis.get("intensity") or "medium"),
        )
        if should
        else "",
        "npcs": [],
    }


def request_decision(
    *,
    config_dir: Any,
    context: str,
    analysis: dict[str, Any],
    structured: dict[str, Any] | None,
    settings: dict[str, Any],
    secret_dir_override: Any = None,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """真实调用一次 KP；失败抛异常，由 :func:`run` 统一回退。"""
    # 平台/模型统一取「模型设置」中按顺序第一个启用的 AI 平台。
    target = resolve_ai_target(config_dir, secret_dir_override=secret_dir_override)
    if target is None:
        raise RuntimeError("no_enabled_ai_platform")
    payload_context = {
        "scene": str(analysis.get("scene") or "")[:120],
        "profile": analysis.get("profile"),
        "intensity": analysis.get("intensity"),
        "confidence": analysis.get("confidence"),
        "structured": structured if isinstance(structured, dict) else {},
        "recent_context": context,
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "下面是本回合的叙事上下文与场景分析（JSON）。只输出决策 JSON。\n"
                + json.dumps(payload_context, ensure_ascii=False)
            ),
        },
    ]
    body = build_request_payload(
        target["platform"],
        target["model"],
        messages,
        config_dir=config_dir,
        params={"temperature": 0.4},
    )
    headers = dict(target["headers"])
    headers.setdefault("Content-Type", "application/json")
    if target["api_key"]:
        headers["Authorization"] = f"Bearer {target['api_key']}"
    timeout = max(10, min(300, int(settings.get("illustration_kp_timeout") or target["timeout"] or 45)))
    started = time.monotonic()
    response = post_with_retry(
        target["endpoint"],
        headers=headers,
        payload=body,
        timeout=timeout,
        max_retries=1,
        post=post,
        label="immersive_illustration_kp",
    )
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if response.status_code >= 400:
        raise RuntimeError(f"upstream_http_{response.status_code}:{response.text[:160]}")
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    content = str(message.get("content") or "")
    if not content.strip():
        content = str(message.get("reasoning_content") or "")
    if not content.strip():
        raise RuntimeError("empty_kp_content")
    return {
        "raw": _extract_json(content),
        "model": target["model"],
        "platform": target["platform"],
        "elapsed_ms": elapsed_ms,
    }


def run(
    *,
    room_id: str,
    room_dir: Path | None,
    text: str,
    analysis: dict[str, Any],
    structured: dict[str, Any] | None,
    settings: dict[str, Any],
    trigger: str,
    important_signal: bool = False,
    config_dir: Any = None,
    secret_dir_override: Any = None,
    post: Callable[..., Any] | None = None,
    usage_path: Path | None = None,
    request_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """获取一次 KP 决策；永不抛错，失败时返回规则降级结果。"""
    result: dict[str, Any] = {
        "accepted": False,
        "decision": None,
        "trigger": trigger,
        "platform": "",
        "model": "",
        "elapsed_ms": 0,
        "validation": "skipped",
        "fallback_reason": "",
        "fallback": False,
    }
    try:
        allowed, reason, _used = reserve_call(
            budget=int(settings.get("illustration_kp_daily_budget") or 0),
            usage_path=usage_path,
        )
        if not allowed:
            result["fallback_reason"] = reason
            result["fallback"] = True
            result["decision"] = fallback_decision(
                trigger=trigger, analysis=analysis, important_signal=important_signal
            )
            return result

        context = recent_context(
            room_dir,
            text,
            int(settings.get("illustration_kp_max_context_chars") or 1600),
        )
        if request_fn is not None:
            call = request_fn(
                config_dir=config_dir,
                context=context,
                analysis=analysis,
                structured=structured,
                settings=settings,
                secret_dir_override=secret_dir_override,
            )
        else:
            call = request_decision(
                config_dir=config_dir,
                context=context,
                analysis=analysis,
                structured=structured,
                settings=settings,
                secret_dir_override=secret_dir_override,
                post=post,
            )
        result["model"] = str(call.get("model") or "")
        result["platform"] = str(call.get("platform") or "")
        result["elapsed_ms"] = float(call.get("elapsed_ms") or 0)
        decision, errors = validate_decision(call.get("raw"))
        if decision is None:
            result["validation"] = "invalid"
            result["fallback_reason"] = "; ".join(errors)[:180]
            result["fallback"] = True
            result["decision"] = fallback_decision(
                trigger=trigger, analysis=analysis, important_signal=important_signal
            )
            return result
        result.update({"accepted": True, "decision": decision, "validation": "ok"})
        return result
    except Exception as exc:  # noqa: BLE001 - KP 失败必须降级，不能影响叙事
        logger.warning("immersive illustration KP failed room=%s: %s", room_id, exc)
        result["fallback_reason"] = str(exc)[:180]
        result["validation"] = "error"
        result["fallback"] = True
        result["decision"] = fallback_decision(
            trigger=trigger, analysis=analysis, important_signal=important_signal
        )
        return result


def submit(**kwargs: Any) -> futures.Future | None:
    """后台提交一次 KP 决策；同一房间已有在途请求时返回 ``None``。"""
    room_id = str(kwargs.get("room_id") or "")
    with _LOCK:
        if room_id in _INFLIGHT:
            return None
        _INFLIGHT.add(room_id)
    future = _EXECUTOR.submit(run, **kwargs)

    def _release(_future):
        with _LOCK:
            _INFLIGHT.discard(room_id)

    future.add_done_callback(_release)
    return future


def in_flight(room_id: str = "") -> bool:
    with _LOCK:
        return bool(_INFLIGHT) if not room_id else room_id in _INFLIGHT
