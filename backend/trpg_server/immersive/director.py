"""LLM 音乐导演：读取叙事上下文与当前音频状态，输出结构化乐谱参数。

设计原则（对应「绝不能让叙事流或播放卡住」）：

1. **规则先行**。叙事回合里先由 :mod:`trpg_server.immersive.music` 按既有规则分析
   并广播配乐，LLM 只作为「精修层」在后台运行；后台失败/超时不会影响已播放的音乐。
2. **缓存优先**。上下文指纹（叙事文本 + 场景/档位 + 当前音频状态）命中缓存时直接复用
   乐谱，同步生效；未命中才发起调用。
3. **严格校验**。模型输出必须通过 :mod:`trpg_server.immersive.director_schema` 校验；
   非法、低置信或调用失败一律回退，回退原因写入日志与结果。
4. **预算与并发**。每日调用预算、单房间同一时间只允许一个在途请求，避免重复计费。
5. **不依赖 Flask**。AI 平台从 ``config_dir/aiplatform`` 直接解析，可在后台线程、
   控制台与测试中复用。
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server.agents.ai_request import build_request_payload
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.ai_platform_resolver import (
    CAPABILITY_MUSIC_DIRECTOR,
    PlatformUpstreamError,
    mark_platform_failure,
    mark_platform_success,
    resolve_platform_target,
    resolve_platform_targets,
)
from trpg_server.immersive.director_schema import validate_score
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

DIRECTOR_RUNTIME_DIR = RUNTIME_DIR / "immersive"
CACHE_PATH = DIRECTOR_RUNTIME_DIR / "director_cache.json"
USAGE_PATH = DIRECTOR_RUNTIME_DIR / "director_usage.json"
MAX_CONTEXT_MESSAGES = 8
MAX_CACHE_ENTRIES = 200

_LOCK = threading.RLock()
_INFLIGHT: set[str] = set()
_EXECUTOR = futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="immersive-director")

SYSTEM_PROMPT = (
    "你是 TRPG 的「音乐导演」。只输出一个 JSON 对象，不要输出任何解释、Markdown 或代码块。\n"
    "JSON 必须严格符合以下 schema（未知字段会被丢弃，类型错误会导致整份输出作废）：\n"
    "{\n"
    '  "scene": "不超过 80 字的场景概括",\n'
    '  "mood": "daily|suspense|unease|horror|urgency|sorrow|awe|aftermath",\n'
    '  "mode": "major|minor|dorian|phrygian|lydian|mixolydian|harmonic_minor|whole_tone|chromatic",\n'
    '  "tempo": 30-200 的整数,\n'
    '  "chords": [{"root": 0-11 的整数, "quality": "major|minor|sus2|sus4|dim|aug|power"}]，2-8 个,\n'
    '  "instrumentation": ["pad|bass|arp|pulse|bells|drone|strings|choir|noise|organ|timpani|ostinato|brass|pluck|rumble"]，1-6 个,\n'
    '  "motif": [-12~24 的整数]，2-8 个，表示主题音级序列,\n'
    '  "sections": [{"label": "段落名", "dynamic": "low|medium|high", "bars": 2-16}]，1-4 个,\n'
    '  "dynamics": [0~1 的数值]，2-8 个，逐段响度曲线,\n'
    '  "pedal": "可选，0-11 的整数，持续低音的音高类；想制造 pedal point 压迫感时填写",\n'
    '  "motif_variants": "可选，1-4 个 {degrees: [-12~24 整数], rhythm: [0.25~4 数值], role: statement|answer|climax|fragment}",\n'
    '  "confidence": 0-100 的整数，表示这份乐谱与叙事的贴合程度\n'
    "}\n"
    "要求：音乐必须服务叙事，不要突然改变情绪；恐怖/紧迫场景可以用不协和音程与更快 tempo，"
    "但整体仍要可听。段落之间请用持续低音或共同和弦衔接；高潮段再让铜管/定音鼓进入，不要一开头就满编。"
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _root(config_dir: Any) -> Path:
    if config_dir:
        return Path(config_dir)
    from trpg_server.config.paths import resolve_config_dir

    return Path(resolve_config_dir(None))


def _cache_file(config_dir: Any = None, cache_path: Path | None = None) -> Path:
    return Path(cache_path) if cache_path else CACHE_PATH


def _usage_file(config_dir: Any = None, usage_path: Path | None = None) -> Path:
    return Path(usage_path) if usage_path else USAGE_PATH


def _normalize_text(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def context_fingerprint(text: str, analysis: dict[str, Any], audio_state: dict[str, Any]) -> str:
    """上下文指纹：叙事文本 + 规则分析结果 + 当前音频状态。

    指纹只取稳定字段，避免把时间戳/随机数带进来导致缓存永远不命中。
    """
    payload = {
        "text": _normalize_text(text, 2400),
        "scene": str(analysis.get("scene") or "")[:120],
        "profile": str(analysis.get("profile") or ""),
        "intensity": str(analysis.get("intensity") or ""),
        "audio": {
            "profile": str(audio_state.get("profile") or ""),
            "intensity": str(audio_state.get("intensity") or ""),
            "section": int(audio_state.get("section") or 0),
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def recent_context(room_dir: Path | None, text: str, max_chars: int) -> str:
    """取最近几条房间消息 + 当前叙事，拼成导演可读的上下文文本。"""
    messages: list[dict[str, Any]] = []
    if room_dir is not None:
        data = read_json(Path(room_dir) / "messages.json", default=[])
        if isinstance(data, list):
            messages = [item for item in data if isinstance(item, dict)][-MAX_CONTEXT_MESSAGES:]
    lines: list[str] = []
    for item in messages:
        content = _normalize_text(str(item.get("content") or ""), 500)
        if not content:
            continue
        who = str(item.get("sender_name") or item.get("type") or "").strip() or "未署名"
        lines.append(f"{who}: {content}")
    current = _normalize_text(text, max_chars)
    if current and (not lines or current not in lines[-1]):
        lines.append(f"当前回合: {current}")
    joined = "\n".join(lines)
    return joined[-max(200, int(max_chars)):]


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


def _entry_valid(entry: dict[str, Any], ttl: int) -> bool:
    if ttl <= 0:
        return False
    try:
        age = time.time() - float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= ttl


def get_cached(
    fingerprint: str,
    *,
    ttl: int,
    cache_path: Path | None = None,
) -> dict[str, Any] | None:
    with _LOCK:
        data = read_json(_cache_file(cache_path=cache_path), default={})
    entries = data.get("entries") if isinstance(data, dict) else {}
    entry = entries.get(fingerprint) if isinstance(entries, dict) else None
    if not isinstance(entry, dict) or not _entry_valid(entry, ttl):
        return None
    score = entry.get("score")
    if not isinstance(score, dict):
        return None
    return entry


def put_cache(
    fingerprint: str,
    score: dict[str, Any],
    *,
    model: str,
    platform: str,
    confidence: int,
    cache_path: Path | None = None,
) -> None:
    path = _cache_file(cache_path=cache_path)
    with _LOCK:
        data = read_json(path, default={})
        entries = data.get("entries") if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            entries = {}
        entries[fingerprint] = {
            "ts": time.time(),
            "score": score,
            "model": model,
            "platform": platform,
            "confidence": confidence,
        }
        if len(entries) > MAX_CACHE_ENTRIES:
            ordered = sorted(entries.items(), key=lambda item: float(item[1].get("ts") or 0), reverse=True)
            entries = dict(ordered[:MAX_CACHE_ENTRIES])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, {"entries": entries})
        except OSError:
            logger.warning("Failed to persist director cache", exc_info=True)


def clear_cache(*, cache_path: Path | None = None) -> int:
    path = _cache_file(cache_path=cache_path)
    with _LOCK:
        data = read_json(path, default={})
        entries = data.get("entries") if isinstance(data, dict) else {}
        count = len(entries) if isinstance(entries, dict) else 0
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to clear director cache", exc_info=True)
    return count


def cache_stats(*, cache_path: Path | None = None, ttl: int = 0) -> dict[str, Any]:
    with _LOCK:
        data = read_json(_cache_file(cache_path=cache_path), default={})
    entries = data.get("entries") if isinstance(data, dict) else {}
    if not isinstance(entries, dict):
        entries = {}
    valid = sum(1 for entry in entries.values() if isinstance(entry, dict) and _entry_valid(entry, ttl))
    return {"entries": len(entries), "valid": valid, "ttl": ttl}


def usage_snapshot(*, usage_path: Path | None = None) -> dict[str, Any]:
    data = read_json(_usage_file(usage_path=usage_path), default={})
    if not isinstance(data, dict) or data.get("date") != _today():
        return {"date": _today(), "calls": 0}
    return {"date": _today(), "calls": int(data.get("calls") or 0)}


def reserve_call(
    *,
    budget: int,
    usage_path: Path | None = None,
) -> tuple[bool, str, int]:
    """预留一次导演调用；返回（是否允许，拒绝原因，今日已用次数）。"""
    path = _usage_file(usage_path=usage_path)
    with _LOCK:
        data = read_json(path, default={})
        if not isinstance(data, dict) or data.get("date") != _today():
            data = {"date": _today(), "calls": 0}
        used = int(data.get("calls") or 0)
        if budget > 0 and used >= budget:
            return False, "director_daily_budget_exhausted", used
        data["calls"] = used + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, data)
        except OSError:
            logger.warning("Failed to persist director usage", exc_info=True)
        return True, "", used + 1


def should_trigger(
    plan: dict[str, Any],
    *,
    settings: dict[str, Any],
    switches: dict[str, bool] | None = None,
) -> tuple[bool, str]:
    """判断本回合是否调用导演：事件驱动（档位变化）或每 N 回合采样。"""
    if not bool(settings.get("music_director_enabled")):
        return False, "disabled"
    if switches is not None and not switches.get("music", True):
        return False, "music_disabled"
    interval = max(1, int(settings.get("music_director_interval_turns") or 2))
    if plan.get("changed"):
        return True, f"event:{plan.get('reason') or 'changed'}"
    turns = int(plan.get("turns") or 0)
    if turns > 0 and turns % interval == 0:
        return True, f"interval:{interval}"
    return False, "not_due"


def resolve_ai_target(
    config_dir: Any,
    *,
    platform_id: str = "",
    model_id: str = "",
    secret_dir_override: Any = None,
) -> dict[str, Any] | None:
    """从 ``config_dir/aiplatform`` 解析第一个可用且声明「音乐导演」用途的平台。"""
    return resolve_platform_target(
        _root(config_dir),
        CAPABILITY_MUSIC_DIRECTOR,
        platform_id=platform_id,
        model_id=model_id,
        secret_dir_override=secret_dir_override,
    )


def request_score(
    *,
    config_dir: Any,
    context: str,
    analysis: dict[str, Any],
    audio_state: dict[str, Any],
    settings: dict[str, Any],
    secret_dir_override: Any = None,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """按平台顺序调用 LLM；单个平台失败会顺延到下一个，全部失败才抛异常。"""
    targets = resolve_platform_targets(
        _root(config_dir),
        CAPABILITY_MUSIC_DIRECTOR,
        secret_dir_override=secret_dir_override,
    )
    if not targets:
        raise RuntimeError("no_enabled_ai_platform")
    payload_context = {
        "scene": str(analysis.get("scene") or "")[:120],
        "rule_profile": analysis.get("profile"),
        "rule_intensity": analysis.get("intensity"),
        "audio_state": {
            "profile": audio_state.get("profile"),
            "intensity": audio_state.get("intensity"),
            "section": audio_state.get("section"),
        },
        "recent_context": context,
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "下面是本回合的叙事上下文与音频状态（JSON）。只输出乐谱 JSON。\n"
                + json.dumps(payload_context, ensure_ascii=False)
            ),
        },
    ]
    last_error: Exception | None = None
    for target in targets:
        try:
            body = build_request_payload(
                target["platform"],
                target["model"],
                messages,
                config_dir=config_dir,
                params={"temperature": 0.55},
            )
            headers = dict(target["headers"])
            headers.setdefault("Content-Type", "application/json")
            if target["api_key"]:
                headers["Authorization"] = f"Bearer {target['api_key']}"
            timeout = max(20, min(300, int(settings.get("music_director_timeout") or target["timeout"] or 45)))
            started = time.monotonic()
            response = post_with_retry(
                target["endpoint"],
                headers=headers,
                payload=body,
                timeout=timeout,
                max_retries=1,
                post=post,
                label="immersive_music_director",
            )
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            if response.status_code >= 400:
                raise PlatformUpstreamError(
                    f"upstream_http_{response.status_code}:{response.text[:160]}",
                    status_code=response.status_code,
                )
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            content = str(message.get("content") or "")
            if not content.strip():
                # 部分推理模型把正文放在 reasoning_content；与项目其他调用一致，回退读取。
                content = str(message.get("reasoning_content") or "")
            if not content.strip():
                raise RuntimeError("empty_director_content")
            raw = _extract_json(content)
            mark_platform_success(target["platform"], CAPABILITY_MUSIC_DIRECTOR)
            return {
                "raw": raw,
                "model": target["model"],
                "platform": target["platform"],
                "elapsed_ms": elapsed_ms,
            }
        except Exception as exc:  # noqa: BLE001 - 单个平台失败必须顺延到下一个
            last_error = exc
            ttl = mark_platform_failure(
                target["platform"],
                CAPABILITY_MUSIC_DIRECTOR,
                status_code=getattr(exc, "status_code", None),
            )
            logger.warning(
                "音乐导演平台 %s 调用失败，%s 秒内跳过：%s",
                target["platform"],
                int(ttl),
                exc,
            )
    raise last_error or RuntimeError("no_enabled_ai_platform")


def run(
    *,
    room_dir: Path | None,
    room_id: str,
    text: str,
    analysis: dict[str, Any],
    plan: dict[str, Any],
    audio_state: dict[str, Any],
    settings: dict[str, Any],
    config_dir: Any = None,
    trigger: str = "",
    secret_dir_override: Any = None,
    post: Callable[..., Any] | None = None,
    cache_path: Path | None = None,
    usage_path: Path | None = None,
    request_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """获取导演乐谱：缓存优先 → 预算 → 调用 → 严格校验 → 回退描述。

    本函数**不抛错**；任何失败都返回 ``accepted=False`` 与 ``fallback_reason``。
    """
    ttl = int(settings.get("music_director_cache_ttl") or 0)
    fingerprint = context_fingerprint(text, analysis, audio_state)
    result: dict[str, Any] = {
        "accepted": False,
        "fingerprint": fingerprint,
        "cached": False,
        "trigger": trigger,
        "score": None,
        "platform": "",
        "model": "",
        "elapsed_ms": 0,
        "confidence": 0,
        "validation": "skipped",
        "fallback_reason": "",
    }
    try:
        cached = get_cached(fingerprint, ttl=ttl, cache_path=cache_path)
        if cached:
            score = cached.get("score")
            if isinstance(score, dict):
                result.update(
                    {
                        "accepted": True,
                        "cached": True,
                        "score": score,
                        "platform": cached.get("platform") or "",
                        "model": cached.get("model") or "",
                        "confidence": int(cached.get("confidence") or 0),
                        "validation": "cache_hit",
                    }
                )
                return result

        allowed, reason, _used = reserve_call(
            budget=int(settings.get("music_director_daily_budget") or 0),
            usage_path=usage_path,
        )
        if not allowed:
            result["fallback_reason"] = reason
            return result

        context = recent_context(
            room_dir,
            text,
            int(settings.get("music_director_max_context_chars") or 1200),
        )
        if request_fn is not None:
            call = request_fn(
                config_dir=config_dir,
                context=context,
                analysis=analysis,
                audio_state=audio_state,
                settings=settings,
                secret_dir_override=secret_dir_override,
            )
        else:
            call = request_score(
                config_dir=config_dir,
                context=context,
                analysis=analysis,
                audio_state=audio_state,
                settings=settings,
                secret_dir_override=secret_dir_override,
                post=post,
            )
        result["model"] = str(call.get("model") or "")
        result["platform"] = str(call.get("platform") or "")
        result["elapsed_ms"] = float(call.get("elapsed_ms") or 0)
        score, errors, confidence = validate_score(
            call.get("raw"),
            min_confidence=int(settings.get("music_director_min_confidence") or 0),
        )
        result["confidence"] = confidence
        if score is None:
            result["validation"] = "invalid" if errors != ["low_confidence"] else "low_confidence"
            result["fallback_reason"] = "; ".join(errors)[:180]
            return result
        result.update(
            {
                "accepted": True,
                "score": score,
                "validation": "ok",
                "fallback_reason": "",
            }
        )
        put_cache(
            fingerprint,
            score,
            model=result["model"],
            platform=result["platform"],
            confidence=confidence,
            cache_path=cache_path,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - 导演失败必须回退，不能影响播放
        logger.warning("immersive director failed room=%s: %s", room_id, exc)
        result["fallback_reason"] = str(exc)[:180]
        result["validation"] = "error"
        return result


def submit(**kwargs: Any) -> futures.Future | None:
    """后台提交一次导演调用；同一房间已有在途请求时返回 ``None``。"""
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
