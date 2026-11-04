"""实时配图：缓存、预算/配额、外部生图队列与优雅降级。

- 默认 ``procedural``：本地确定性 SVG，零外部依赖、无版权风险；
- 可选 ``platform``：从「生图模型平台表」（``immersive.image_platforms``）解析默认
  平台，调用 OpenAI 兼容 ``/v1/images/generations``；
- 地址/模型/尺寸/密钥只认平台表，旧的单条配置仅用于一次性迁移，运行期不再读取，
  避免「两套真相」；
- 无论哪种来源，任何失败（未配置、超时、上游报错、预算耗尽）都回落到程序化场景图，
  并在回报里带上 ``degraded`` 与原因，绝不阻塞叙事主流程；
- 预算按「提交次数」预留，避免并发下超卖；房间小时上限与自动最小间隔独立计算。
"""
from __future__ import annotations

import base64
import concurrent.futures as futures
import hashlib
import io
import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from trpg_server.agents.http_retry import post_with_retry
from trpg_server.immersive import image_platforms, room_cache, scenario_cache, task_gate
from trpg_server.immersive.config import resolve_room_switches
from trpg_server.immersive.svg_scene import render_portrait_svg, render_scene_svg
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import RUNTIME_DIR, SCENARIOS_DIR

logger = logging.getLogger(__name__)

IMMERSIVE_RUNTIME_DIR = RUNTIME_DIR / "immersive"
IMAGE_CACHE_DIR = IMMERSIVE_RUNTIME_DIR / "images"
USAGE_PATH = IMMERSIVE_RUNTIME_DIR / "usage.json"
ILLUSTRATION_STATE_FILENAME = "immersive_illustration.json"
PUBLIC_IMAGE_PREFIX = "/api/immersive/images/"
MAX_IMAGE_BYTES = 16 * 1024 * 1024
_FILENAME_RE = re.compile(r"^[a-f0-9]{20,32}\.(svg|png|jpg|jpeg|webp)$")

_LOCK = threading.RLock()
_EXECUTORS: dict[int, futures.ThreadPoolExecutor] = {}

_PROFILE_STYLE = {
    "daily": "quiet everyday scene, soft overcast light, muted teal and warm grey, cinematic wide shot",
    "suspense": "tense mystery scene, low key lighting, deep green fog, long shadows, cinematic wide shot",
    "unease": "subtly unsettling scene, ochre and olive tones, distorted perspective, cinematic wide shot",
    "horror": "cosmic horror scene, crimson and black palette, oppressive darkness, barely visible shapes, cinematic wide shot",
    "urgency": "high tension action scene, orange embers and smoke, motion blur, dramatic rim light, cinematic wide shot",
    "sorrow": "melancholic scene, cold slate blue palette, rain and soft haze, lonely atmosphere, cinematic wide shot",
    "awe": "awe inspiring monumental scene, indigo night and golden light, vast scale, sacred geometry, cinematic wide shot",
    "aftermath": "aftermath scene, ash grey and amber palette, ruins and embers, quiet devastation, cinematic wide shot",
}


def _executor(concurrency: int) -> futures.ThreadPoolExecutor:
    workers = max(1, min(4, int(concurrency or 1)))
    with _LOCK:
        if workers not in _EXECUTORS:
            _EXECUTORS[workers] = futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="immersive-img")
        return _EXECUTORS[workers]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _hour_key() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H")


def _read_usage() -> dict[str, Any]:
    data = read_json(USAGE_PATH, default={})
    return data if isinstance(data, dict) else {}


def _write_usage(data: dict[str, Any]) -> None:
    try:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_json_atomic(USAGE_PATH, data)
    except OSError:
        logger.warning("Failed to persist immersive image usage", exc_info=True)


def usage_snapshot() -> dict[str, Any]:
    with _LOCK:
        data = _read_usage()
    if data.get("date") != _today():
        return {"date": _today(), "daily_used": 0, "daily_budget": 0, "rooms": {}}
    return data


def reserve_external(room_id: str, settings: dict[str, Any]) -> tuple[bool, str]:
    """为一次外部生图预留预算；返回（是否允许，拒绝原因）。"""
    with _LOCK:
        data = _read_usage()
        if data.get("date") != _today():
            data = {"date": _today(), "daily_used": 0, "rooms": {}}
        budget = int(settings.get("illustration_daily_budget") or 0)
        used = int(data.get("daily_used") or 0)
        if budget > 0 and used >= budget:
            return False, "daily_budget_exhausted"
        rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        entry = rooms.get(room_id) if isinstance(rooms.get(room_id), dict) else {}
        cap = int(settings.get("illustration_room_hourly_cap") or 0)
        hour_used = int(entry.get("hour_used") or 0) if entry.get("hour") == _hour_key() else 0
        if cap > 0 and hour_used >= cap:
            return False, "room_hourly_cap"
        rooms[room_id] = {"hour": _hour_key(), "hour_used": hour_used + 1}
        data["rooms"] = rooms
        data["daily_used"] = used + 1
        _write_usage(data)
        return True, ""


def _room_state_path(room_dir: Path | None) -> Path | None:
    return room_dir / ILLUSTRATION_STATE_FILENAME if room_dir else None


def load_room_state(room_dir: Path | None) -> dict[str, Any]:
    path = _room_state_path(room_dir)
    if path is None:
        return {}
    data = read_json(path, default={})
    return data if isinstance(data, dict) else {}


def _save_room_state(room_dir: Path | None, state: dict[str, Any]) -> None:
    path = _room_state_path(room_dir)
    if path is None:
        return
    try:
        write_json_atomic(path, state)
    except OSError:
        logger.warning("Failed to persist immersive illustration state", exc_info=True)


def _record_history(room_dir: Path | None, entry: dict[str, Any], limit: int = 12) -> None:
    state = load_room_state(room_dir)
    history = state.get("history") if isinstance(state.get("history"), list) else []
    history = [item for item in history if isinstance(item, dict)]
    history.append(entry)
    state["history"] = history[-limit:]
    state["last_turn"] = entry.get("turn")
    state["updated_at"] = entry.get("created_at")
    _save_room_state(room_dir, state)


def next_turn_index(room_dir: Path | None) -> int:
    """房间叙事回合计数器：与配乐开关无关，保证配图冷却独立推进。"""
    state = load_room_state(room_dir)
    turn = int(state.get("turn_counter") or 0) + 1
    state["turn_counter"] = turn
    _save_room_state(room_dir, state)
    return turn


def should_auto_generate(room_dir: Path | None, turn_index: int, min_turns: int) -> tuple[bool, str]:
    state = load_room_state(room_dir)
    last_turn = state.get("last_turn")
    if last_turn is None:
        return True, ""
    try:
        gap = int(turn_index) - int(last_turn)
    except (TypeError, ValueError):
        return True, ""
    if gap < max(1, int(min_turns)):
        return False, f"cooldown:{gap}/{min_turns}"
    return True, ""


def _cache_key(provider: str, model: str, size: str, prompt: str) -> str:
    raw = f"{provider}|{model}|{size}|{prompt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _room_cache_key(provider: str, model: str, size: str, category: str, fingerprint: str) -> str:
    raw = f"{provider}|{model}|{size}|{category}|{fingerprint}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def room_cache_lookup(
    room_dir: Path | None,
    key: str,
    *,
    fingerprint: str = "",
    platform: str = "",
    model: str = "",
    size: str = "",
    category: str = "",
) -> dict[str, Any] | None:
    """房间级缓存查询：图片与索引都在房间目录内，命中不扣生图预算。"""
    return room_cache.lookup(
        room_dir,
        key,
        fingerprint=fingerprint,
        platform=platform,
        model=model,
        size=size,
        category=category,
    )


def room_cache_store(
    room_dir: Path | None,
    key: str,
    *,
    scene: str,
    npc_name: str,
    category: str,
    fingerprint: str,
    platform: str,
    model: str,
    size: str,
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> None:
    """把刚生成的图片复制进房间缓存目录；降级图不写，避免固化失败结果。"""
    if not key or not room_dir or payload.get("degraded"):
        return
    filename = str(payload.get("filename") or "")
    if not _FILENAME_RE.match(filename):
        return
    source = IMAGE_CACHE_DIR / filename
    try:
        content = source.read_bytes()
    except OSError:
        logger.warning("immersive_room_cache write skipped key=%s reason=read_failed", key)
        return
    room_cache.store(
        room_dir,
        key,
        fingerprint=fingerprint,
        platform=platform,
        model=model,
        size=size,
        category=category,
        content=content,
        suffix=source.suffix.lstrip("."),
        scene=scene,
        npc_name=npc_name,
        provider=str(payload.get("provider") or ""),
        platform_id=str(payload.get("platform_id") or ""),
        width=int(payload.get("width") or 0),
        height=int(payload.get("height") or 0),
        settings=settings,
    )


def _file_for_key(key: str, suffix: str) -> Path:
    return IMAGE_CACHE_DIR / f"{key}.{suffix}"


def _public_url(path: Path) -> str:
    return f"{PUBLIC_IMAGE_PREFIX}{path.name}"


def _materialize_scenario_hit(hit: dict[str, Any]) -> Path | None:
    """把剧本缓存图片复制到运行期缓存，返回可公开访问的路径。"""
    source = Path(hit.get("path") or "")
    key = str(hit.get("key") or "")
    suffix = source.suffix.lower().lstrip(".")
    if not key or suffix not in {"svg", "png", "jpg", "jpeg", "webp"}:
        return None
    target = _file_for_key(key, suffix)
    try:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            shutil.copyfile(source, target)
    except OSError:
        logger.warning("scenario cache materialize failed source=%s", source, exc_info=True)
        return None
    return target


def _materialize_room_hit(hit: dict[str, Any], room_id: str) -> Path | None:
    """把房间目录里的缓存图片复制到运行期目录，返回可公开访问的路径。

    运行期文件名由「房间 ID + 缓存 key」再哈希，避免不同房间同 key 互相覆盖。
    """
    source = Path(hit.get("path") or "")
    key = str(hit.get("key") or "")
    suffix = source.suffix.lower().lstrip(".")
    if not key or suffix not in {"svg", "png", "jpg", "jpeg", "webp"}:
        return None
    filename_key = hashlib.sha256(f"{room_id}|{key}".encode("utf-8")).hexdigest()[:24]
    target = _file_for_key(filename_key, suffix)
    try:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            shutil.copyfile(source, target)
    except OSError:
        logger.warning("room cache materialize failed source=%s", source, exc_info=True)
        return None
    return target


def resolve_image_path(filename: str) -> Path | None:
    """仅允许访问缓存目录内的规范文件名，阻断路径穿越。"""
    name = str(filename or "").strip()
    if not _FILENAME_RE.match(name):
        return None
    path = IMAGE_CACHE_DIR / name
    try:
        if not path.resolve().is_file():
            return None
    except OSError:
        return None
    return path


def _build_prompt(scene: str, profile: str, intensity: str) -> str:
    style = _PROFILE_STYLE.get(profile, _PROFILE_STYLE["daily"])
    intensity_word = {"low": "subtle", "medium": "moderate", "high": "intense"}.get(intensity, "moderate")
    return (
        f"{scene}. {style}. Mood intensity: {intensity_word}. "
        "Painterly cinematic lighting, rich atmosphere, detailed environment, no text, no watermark, "
        "no logo, no visible UI, no modern anachronism."
    )[:900]


def build_prompt_for_scene(scene: str, profile: str, intensity: str) -> str:
    """供 AI KP 降级链路复用的场景提示词构造器。"""
    return _build_prompt(scene, profile, intensity)


def room_cache_stats(room_dir: Path | None) -> dict[str, Any]:
    return room_cache.stats(room_dir)


def clear_room_cache(room_dir: Path | None, *, delete_files: bool = True) -> tuple[int, int]:
    """清空本房间缓存目录（图片 + 索引），不触碰剧本目录。"""
    return room_cache.clear(room_dir)


def scenario_accepts_room_images(
    scenario_id: str, scenarios_dir: Path | str | None = None
) -> bool:
    """读取剧本作者开关：是否接受来自房间的生成图片进入跨房间复用池。"""
    safe_id = scenario_cache.safe_scenario_id(scenario_id)
    if not safe_id:
        return False
    base = Path(scenarios_dir) if scenarios_dir else Path(SCENARIOS_DIR)
    descriptor = base / f"scenario-{safe_id}" / "scenario.json"
    try:
        data = read_json(descriptor, default={})
    except OSError:
        return False
    return bool(isinstance(data, dict) and data.get("accept_room_images"))


def _write_svg(
    room_dir: Path | None,
    scene: str,
    profile: str,
    intensity: str,
    seed: str,
    category: str = "scene",
    size_override: str = "",
) -> dict[str, Any]:
    if str(category).strip().lower() == "portrait":
        side = 768
        match = re.match(r"(\d{2,4})x(\d{2,4})", str(size_override or ""))
        if match:
            side = max(384, min(1536, int(match.group(1))))
        key = _cache_key("procedural", "svg-portrait-v1", f"{side}x{side}", f"{scene}|{profile}|{intensity}|{seed}")
        path = _file_for_key(key, "svg")
        if not path.is_file():
            IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            svg = render_portrait_svg(profile, intensity, scene, seed, size=side)
            path.write_text(svg, encoding="utf-8")
        return {
            "provider": "procedural",
            "url": _public_url(path),
            "filename": path.name,
            "width": side,
            "height": side,
            "degraded": False,
            "error": "",
        }
    key = _cache_key("procedural", "svg-v1", "1280x720", f"{scene}|{profile}|{intensity}|{seed}")
    path = _file_for_key(key, "svg")
    if not path.is_file():
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        svg = render_scene_svg(profile, intensity, scene, seed)
        path.write_text(svg, encoding="utf-8")
    return {
        "provider": "procedural",
        "url": _public_url(path),
        "filename": path.name,
        "width": 1280,
        "height": 720,
        "degraded": False,
        "error": "",
    }


def _save_external_image(content: bytes, content_type: str, key: str) -> Path:
    mime = (content_type or "").lower()
    if "png" in mime:
        suffix = "png"
    elif "webp" in mime:
        suffix = "webp"
    else:
        suffix = "jpg"
    path = _file_for_key(key, suffix)
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("image_too_large")
    path.write_bytes(content)
    return path


def _download_image(url: str) -> tuple[bytes, str]:
    response = requests.get(url, timeout=(10, 40), stream=True)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    buffer = io.BytesIO()
    total = 0
    for chunk in response.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ValueError("image_too_large")
        buffer.write(chunk)
    return buffer.getvalue(), content_type


def _generate_external(
    prompt: str,
    platform: dict[str, Any],
    *,
    category: str = "scene",
    size_override: str = "",
) -> dict[str, Any]:
    """按平台表配置真实生成一张图。

    ``platform`` 来自 :mod:`trpg_server.immersive.image_platforms`，包含解析后的
    ``base_url`` / ``model`` / ``size`` / ``timeout`` / ``api_key``。函数只负责调用
    OpenAI 兼容 ``/v1/images/generations`` 并把结果落到本地缓存。
    """
    protocol = str(platform.get("protocol") or "openai_images")
    base_url = str(platform.get("base_url") or "").strip().rstrip("/")
    api_key = str(platform.get("api_key") or "").strip()
    model = str(platform.get("model") or "")
    size = str(size_override or platform.get("size") or "1024x1024")
    platform_id = str(platform.get("id") or "")
    platform_name = str(platform.get("name") or platform_id)
    if not base_url or not api_key:
        raise RuntimeError("not_configured")
    key = _cache_key(protocol, model, size, f"{category}|{prompt}" if category else prompt)
    for candidate in ("png", "webp", "jpg"):
        existing = _file_for_key(key, candidate)
        if existing.is_file():
            return {
                "provider": "platform",
                "platform_id": platform_id,
                "platform_name": platform_name,
                "url": _public_url(existing),
                "filename": existing.name,
                "width": 0,
                "height": 0,
                "degraded": False,
                "error": "",
                "cached": True,
            }

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }
    timeout = int(platform.get("timeout") or 120)
    started = time.monotonic()
    response = post_with_retry(
        f"{base_url}/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload=payload,
        timeout=timeout,
        max_retries=1,
        label="immersive_illustration",
    )
    if response.status_code == 400 and "response_format" in payload:
        # 部分新模型不支持 response_format，去掉后重试一次（仍受同一总预算约束）。
        payload.pop("response_format", None)
        response = post_with_retry(
            f"{base_url}/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload=payload,
            timeout=timeout,
            max_retries=1,
            label="immersive_illustration_retry",
        )
    if response.status_code >= 400:
        raise RuntimeError(f"upstream_http_{response.status_code}:{response.text[:160]}")

    data = response.json()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError("empty_image_response")
    item = items[0] if isinstance(items[0], dict) else {}
    b64_value = item.get("b64_json") or item.get("b64")
    if b64_value:
        content = base64.b64decode(str(b64_value))
        path = _save_external_image(content, "image/png", key)
        suffix = path.suffix.lstrip(".")
    else:
        url = str(item.get("url") or "").strip()
        if not url:
            raise RuntimeError("empty_image_url")
        if urlparse(url).scheme not in {"http", "https"}:
            raise RuntimeError("invalid_image_url")
        content, content_type = _download_image(url)
        path = _save_external_image(content, content_type, key)
        suffix = path.suffix.lstrip(".")
    logger.info(
        "immersive_illustration generated platform=%s model=%s size=%s elapsed_ms=%.0f bytes=%s",
        platform_id, model, size, (time.monotonic() - started) * 1000, path.stat().st_size,
    )
    return {
        "provider": "platform",
        "platform_id": platform_id,
        "platform_name": platform_name,
        "url": _public_url(path),
        "filename": path.name,
        "width": 0,
        "height": 0,
        "degraded": False,
        "error": "",
        "cached": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "suffix": suffix,
    }


def generate_now(
    *,
    room_dir: Path | None,
    room_id: str,
    scene: str,
    profile: str,
    intensity: str,
    seed: str,
    turn_index: int,
    settings: dict[str, Any],
    force: bool = False,
    config_dir: Path | str | None = None,
    scenario_id: str = "",
    scenarios_dir: Path | str | None = None,
    house_rules: dict[str, Any] | None = None,
    prompt_override: str = "",
    category: str = "scene",
    fingerprint: str = "",
    allow_cross_room: bool = False,
    npc_name: str = "",
    size_override: str = "",
) -> dict[str, Any]:
    """同步生成一张图（供手动触发/控制台使用），失败自动回落程序化图。

    外部生图只认「生图模型平台表」：``illustration_provider`` 为 ``platform``
    （或旧别名 ``openai_compatible``）时解析默认平台，解析不到即视为未配置并降级。

    剧本缓存（可选）：若开启「优先复用剧本图」且 key 命中，直接复制剧本目录里的
    图片到运行期缓存，**不占外部生图预算**；``force=True`` 的手动重绘会跳过复用。
    """
    switches = resolve_room_switches(settings, house_rules)
    provider = str(settings.get("illustration_provider") or "procedural")
    wants_external = provider in {"platform", "openai_compatible"}
    platform = image_platforms.resolve_platform(config_dir, settings) if wants_external else None
    external_allowed = bool(platform) and bool(switches.get("illustration"))
    degradation = ""
    payload: dict[str, Any] | None = None
    category_norm = "portrait" if str(category).strip().lower() == "portrait" else "scene"
    if category_norm == "portrait" and not size_override:
        size_override = str(settings.get("illustration_npc_size") or "768x768")
    scenario_sid = scenario_cache.safe_scenario_id(scenario_id)
    scenario_fp = str(fingerprint or "").strip() or scenario_cache.scene_fingerprint(
        scene, profile, intensity
    )
    reuse_allowed = (bool(switches.get("scenario_prefer")) or bool(allow_cross_room)) and not force
    # 房间生成的图片进入跨房间复用池，必须同时满足「站点允许写入剧本」与「剧本作者接受」，
    # 默认两者都关闭；不满足时只写本房间目录，其他房间不会命中。
    record_allowed = bool(switches.get("scenario_record")) and bool(allow_cross_room)
    room_key = ""

    # 命中 key 需要平台/模型/尺寸；外部平台未配置时无法确定 key，直接走降级逻辑。
    if wants_external and platform is not None:
        target_platform = str(platform.get("id") or platform.get("protocol") or "platform")
        target_model = str(platform.get("model") or "")
        target_size = str(size_override or platform.get("size") or "")
        target_platform_name = str(platform.get("name") or target_platform)
    elif not wants_external:
        target_platform = "procedural"
        target_model = "svg-portrait-v1" if category_norm == "portrait" else "svg-v1"
        target_size = str(size_override or "") if category_norm == "portrait" else "1280x720"
        target_platform_name = "程序化头像" if category_norm == "portrait" else "程序化场景"
    else:
        target_platform = target_model = target_size = target_platform_name = ""

    scenario_key = ""
    if scenario_sid and (reuse_allowed or record_allowed):
        scenario_key = scenario_cache.cache_key(
            scenario_sid, scenario_fp, target_platform, target_model, target_size, category_norm
        )
    if target_platform and target_model and target_size:
        room_key = _room_cache_key(
            target_platform, target_model, target_size, category_norm, scenario_fp
        )

    if reuse_allowed and scenario_sid and target_platform:
        hit = scenario_cache.lookup(
            scenario_sid,
            scenario_fp,
            target_platform,
            target_model,
            target_size,
            scenarios_dir=scenarios_dir,
            category=category_norm,
        )
        if hit:
            entry_meta = hit.get("entry") if isinstance(hit.get("entry"), dict) else {}
            entry_origin = str(entry_meta.get("origin") or "scenario").strip().lower()
            source_room = str(entry_meta.get("source_room") or "")
            if source_room and source_room != room_id and not allow_cross_room:
                logger.info(
                    "immersive_scenario_cache cross-room blocked room=%s source=%s scenario=%s key=%s reason=author_not_accepted",
                    room_id, source_room, scenario_sid, hit.get("key"),
                )
                hit = None
        if hit:
            path = _materialize_scenario_hit(hit)
            if path is not None:
                payload = {
                    "provider": "procedural" if target_platform == "procedural" else "platform",
                    "platform_id": "" if target_platform == "procedural" else target_platform,
                    "platform_name": target_platform_name,
                    "url": _public_url(path),
                    "filename": path.name,
                    "width": int(entry_meta.get("width") or 0),
                    "height": int(entry_meta.get("height") or 0),
                    "degraded": False,
                    "error": "",
                    "cached": True,
                    "cache_hit": True,
                    "cache_level": (
                        "cross-room" if source_room and source_room != room_id else "scenario"
                    ),
                    "reuse_source": "scenario",
                    "scenario_key": hit.get("key"),
                    "source_room": source_room,
                }
                logger.info(
                    "immersive_scenario_cache reuse room=%s scenario=%s key=%s level=%s origin=%s source=%s",
                    room_id, scenario_sid, hit.get("key"),
                    "cross-room" if source_room and source_room != room_id else "scenario",
                    entry_origin, source_room or "-",
                )
            else:
                logger.warning(
                    "immersive_scenario_cache reuse failed room=%s scenario=%s key=%s reason=materialize_failed",
                    room_id, scenario_sid, hit.get("key"),
                )
    elif force and scenario_sid and switches.get("scenario_prefer"):
        logger.info(
            "immersive_scenario_cache bypass room=%s scenario=%s key=%s reason=force",
            room_id, scenario_sid, scenario_key,
        )
    elif not reuse_allowed and scenario_sid:
        logger.info(
            "immersive_scenario_cache miss room=%s scenario=%s key=%s reason=%s",
            room_id, scenario_sid, scenario_key,
            "prefer_disabled" if not switches.get("scenario_prefer") else "force",
        )

    # 剧本级未命中（或未开启）时，再看房间级缓存：场景/地点来回切换直接复用。
    if payload is None and room_key and not force:
        room_hit = room_cache_lookup(
            room_dir,
            room_key,
            fingerprint=scenario_fp,
            platform=target_platform,
            model=target_model,
            size=target_size,
            category=category_norm,
        )
        if room_hit is not None:
            path = _materialize_room_hit(room_hit, room_id)
            room_entry = room_hit.get("entry") if isinstance(room_hit.get("entry"), dict) else {}
            if path is not None:
                payload = {
                    "provider": room_entry.get("provider") or "platform",
                    "platform_id": room_entry.get("platform_id") or "",
                    "platform_name": target_platform_name,
                    "url": _public_url(path),
                    "filename": path.name,
                    "width": int(room_entry.get("width") or 0),
                    "height": int(room_entry.get("height") or 0),
                    "degraded": False,
                    "error": "",
                    "cached": True,
                    "cache_hit": True,
                    "cache_level": "room",
                    "reuse_source": "room",
                    "room_key": room_key,
                    "source_room": room_id,
                }
                logger.info(
                    "immersive_room_cache reuse room=%s key=%s category=%s scene=%s npc=%s",
                    room_id, room_key, category_norm, scene, npc_name or "-",
                )
            else:
                logger.warning(
                    "immersive_room_cache reuse failed room=%s key=%s reason=materialize_failed",
                    room_id, room_key,
                )

    if payload is None:
        if wants_external and platform is None:
            # 选择了外部生图但平台表里没有可用条目：明确降级，而不是静默使用旧字段。
            degradation = "platform_not_configured"
        elif external_allowed:
            allowed, reason = reserve_external(room_id, settings)
            if not allowed:
                degradation = reason
            else:
                prompt = str(prompt_override or "").strip() or _build_prompt(scene, profile, intensity)
                try:
                    payload = _generate_external(
                        prompt, platform, category=category_norm, size_override=size_override
                    )
                except Exception as exc:  # noqa: BLE001 - 外部服务不可控
                    logger.warning("immersive_illustration external failed: %s", exc)
                    degradation = str(exc)[:180]
        if payload is None:
            # 程序化图是默认的「可用方案」，只有外部服务失败/未配置才算降级，
            # 避免默认配置下每张图都被标记成异常。
            if not degradation and external_allowed:
                degradation = "external_failed"
            payload = _write_svg(
                room_dir, scene, profile, intensity, seed,
                category=category_norm, size_override=size_override,
            )
            payload["degraded"] = bool(degradation)
            payload["error"] = degradation
            payload["fallback"] = bool(degradation)

    # 新生成的图按配置写入剧本目录；降级回落的程序化图由 scenario_cache 自行跳过。
    if payload and not payload.get("cache_hit"):
        payload["reuse_source"] = ""
        if not record_allowed:
            logger.info(
                "immersive_scenario_cache write skipped scenario=%s key=%s reason=record_disabled",
                scenario_sid or "-", scenario_key or "-",
            )
        elif not scenario_sid:
            logger.info("immersive_scenario_cache write skipped reason=missing_scenario")
        else:
            source = IMAGE_CACHE_DIR / str(payload.get("filename") or "")
            suffix = source.suffix.lower().lstrip(".")
            try:
                content = source.read_bytes()
            except OSError:
                content = b""
            recorded, reason, stored = scenario_cache.store(
                scenario_sid,
                fingerprint=scenario_fp,
                platform=target_platform,
                model=target_model,
                size=target_size,
                content=content,
                suffix=suffix,
                scene=scene,
                profile=profile,
                intensity=intensity,
                settings=settings,
                room_id=room_id,
                platform_name=target_platform_name,
                width=int(payload.get("width") or 0),
                height=int(payload.get("height") or 0),
                degraded=bool(payload.get("degraded")),
                scenarios_dir=scenarios_dir,
                category=category_norm,
                origin="room" if allow_cross_room else "scenario",
                source_room=room_id,
            )
            payload["scenario_key"] = (stored or {}).get("key") or scenario_key
            logger.info(
                "immersive_scenario_cache write room=%s scenario=%s key=%s category=%s recorded=%s reason=%s",
                room_id, scenario_sid, payload["scenario_key"], category_norm, recorded, reason,
            )
        payload.setdefault("cache_level", "none")
        room_cache_store(
            room_dir,
            room_key,
            scene=scene,
            npc_name=npc_name,
            category=category_norm,
            fingerprint=scenario_fp,
            platform=target_platform,
            model=target_model,
            size=target_size,
            payload=payload,
            settings=settings,
        )
        if room_key:
            logger.info(
                "immersive_room_cache write room=%s key=%s category=%s file=%s",
                room_id, room_key, category_norm, payload.get("filename"),
            )
    entry = {
        "id": f"{int(time.time() * 1000)}-{seed[-6:]}",
        "url": payload.get("url"),
        "filename": payload.get("filename"),
        "scene": scene,
        "profile": profile,
        "intensity": intensity,
        "provider": payload.get("provider"),
        "platform_id": payload.get("platform_id") or "",
        "cache_hit": bool(payload.get("cache_hit")),
        "cache_level": payload.get("cache_level") or ("none" if not payload.get("cache_hit") else ""),
        "category": category_norm,
        "npc_name": npc_name,
        "fingerprint": scenario_fp,
        "reuse_source": payload.get("reuse_source") or "",
        "source_room": payload.get("source_room") or "",
        "scenario_key": payload.get("scenario_key") or "",
        "degraded": payload.get("degraded", False),
        "error": payload.get("error", ""),
        "turn": turn_index,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed": seed,
    }
    _record_history(room_dir, entry)
    logger.info(
        "immersive_illustration room=%s provider=%s degraded=%s covered_turn=%s error=%s",
        room_id, payload.get("provider"), payload.get("degraded"), turn_index, payload.get("error") or "-",
    )
    payload["room_id"] = room_id
    payload["scene"] = scene
    payload["profile"] = profile
    payload["intensity"] = intensity
    payload["turn"] = turn_index
    payload["seed"] = seed
    return payload


def _generate_with_gate(**kwargs: Any) -> Any:
    # 生图期间标记为重任务，语音合成会排队让路，避免争抢 CPU。
    with task_gate.heavy_task():
        return generate_now(**kwargs)


def submit_generation(**kwargs: Any) -> futures.Future:
    """异步提交，调用方立即返回；结果通过 Socket.IO 广播。"""
    concurrency = int(kwargs.get("settings", {}).get("illustration_concurrency") or 1)
    return _executor(concurrency).submit(_generate_with_gate, **kwargs)


def history_for_room(room_dir: Path | None, limit: int = 12) -> list[dict[str, Any]]:
    state = load_room_state(room_dir)
    history = state.get("history") if isinstance(state.get("history"), list) else []
    return [item for item in history if isinstance(item, dict)][-limit:]
