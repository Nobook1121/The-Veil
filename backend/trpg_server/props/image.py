"""照片道具的生图通道（设计稿 §6.5）。

复用 ``immersive`` 的外部生图平台表与调用链路（重试、超时、平台缓存），
叠加道具自己的 ``[props] image_daily_budget`` 预算与 ``props/<id>/photo.*``
资产落盘。任何失败都只降级为「空白照片框 + 背面题字」，不抛给渲染主流程。
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.immersive import image_platforms, task_gate
from trpg_server.immersive.illustration import generate_external_asset
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import RUNTIME_DIR

from . import store

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_PHOTO_SEMAPHORE = threading.Semaphore(2)
_SUFFIXES = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp"}
_PROMPT_SUFFIX = (
    "vintage 1920s photograph, silver gelatin print, visible grain, slightly faded, "
    "soft natural light, no text, no watermark, no modern objects"
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def usage_path() -> Path:
    return RUNTIME_DIR / "props" / "image_usage.json"


def photo_prompt(spec: Mapping[str, Any]) -> str:
    """按道具字段拼出生图提示词；没有画面描述时返回空串（不花预算）。"""
    fields = spec.get("fields") if isinstance(spec.get("fields"), Mapping) else {}
    scene = str(fields.get("scene") or "").strip()
    if not scene:
        parts = [
            str(fields.get("caption") or "").strip(),
            str(fields.get("back_text") or "").strip(),
            str(spec.get("title") or "").strip(),
        ]
        scene = "，".join(part for part in parts if part)
    if not scene:
        return ""
    return f"{scene}. {_PROMPT_SUFFIX}"[:900]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()[:24]


def reserve_budget(settings: Mapping[str, Any], *, path: str | Path | None = None) -> tuple[bool, str]:
    """按天预占一次生图预算；``budget <= 0`` 表示不限制。"""
    budget = int(settings.get("image_daily_budget") or 0)
    if budget <= 0:
        return True, ""
    target = Path(path) if path else usage_path()
    with _LOCK:
        data = read_json(target, default={})
        data = data if isinstance(data, dict) else {}
        if data.get("date") != _today():
            data = {"date": _today(), "used": 0}
        used = int(data.get("used") or 0)
        if used >= budget:
            return False, "daily_budget_exhausted"
        data["used"] = used + 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(target, data)
        except OSError:
            logger.warning("props image usage persist failed path=%s", target, exc_info=True)
        return True, ""


def _asset_file(room_dir: str | Path, prop_id: str, suffix: str) -> Path:
    return store.assets_dir(room_dir, prop_id) / f"photo.{_SUFFIXES.get(suffix, 'png')}"


def _existing_asset(room_dir: str | Path, prop_id: str) -> Path | None:
    directory = store.assets_dir(room_dir, prop_id)
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("photo.*")):
        if path.is_file():
            return path
    return None


def _clear_asset_ref(room_dir: str | Path, prop_id: str) -> None:
    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        assets = dict(current.get("assets") or {})
        assets.pop("photo", None)
        assets.pop("photo_hash", None)
        current["assets"] = assets
        return current

    try:
        store.update_prop(room_dir, prop_id, mutate)
    except Exception:  # noqa: BLE001 - 清引用失败不影响渲染降级
        logger.warning("props photo asset ref clear failed id=%s", prop_id, exc_info=True)


def _resolve_platform(config_dir: Any) -> dict[str, Any] | None:
    try:
        return image_platforms.resolve_platform(config_dir)
    except Exception:  # noqa: BLE001 - 平台配置损坏按未配置处理
        logger.warning("props photo platform resolve failed", exc_info=True)
        return None


def _default_generate(prompt: str, platform: Mapping[str, Any]) -> tuple[bytes, str]:
    with task_gate.heavy_task():
        return generate_external_asset(prompt, dict(platform), category="prop_photo", size_override="1024x1024")


def ensure_photo(
    room_dir: str | Path,
    spec: Mapping[str, Any],
    *,
    config_dir: Any = None,
    settings: Mapping[str, Any] | None = None,
    force: bool = False,
    usage_file: str | Path | None = None,
    platform: Mapping[str, Any] | None = None,
    generate: Callable[[str, Mapping[str, Any]], tuple[bytes, str]] | None = None,
) -> dict[str, Any]:
    """确保照片资产存在；失败返回 ``degraded`` 而不是抛异常。"""
    settings = settings or {}
    prop_id = str(spec.get("id") or "")
    if not store.is_valid_prop_id(prop_id):
        return {"ok": False, "degraded": True, "reason": "invalid_prop"}

    prompt = photo_prompt(spec)
    if not prompt:
        return {"ok": False, "degraded": True, "reason": "no_prompt"}

    digest = prompt_hash(prompt)
    assets = spec.get("assets") if isinstance(spec.get("assets"), Mapping) else {}
    existing = _existing_asset(room_dir, prop_id)
    if not force and existing is not None and str(assets.get("photo_hash") or "") == digest:
        return {
            "ok": True,
            "cached": True,
            "path": str(existing),
            "reason": "",
            "degraded": False,
        }

    allowed, reason = reserve_budget(settings, path=usage_file)
    if not allowed:
        _clear_asset_ref(room_dir, prop_id)
        logger.info("props photo budget exhausted room=%s id=%s", room_dir, prop_id)
        return {"ok": False, "degraded": True, "reason": reason}

    target_platform = dict(platform) if platform else _resolve_platform(config_dir)
    runner = generate or _default_generate
    if target_platform is None:
        _clear_asset_ref(room_dir, prop_id)
        return {"ok": False, "degraded": True, "reason": "platform_not_configured"}

    try:
        with _PHOTO_SEMAPHORE:
            content, suffix = runner(prompt, target_platform)
        if not content:
            raise ValueError("empty image")
        path = _asset_file(room_dir, prop_id, str(suffix or "png"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except Exception as exc:  # noqa: BLE001 - 外部服务不可控，统一降级
        logger.warning("props photo generation failed room=%s id=%s error=%s", room_dir, prop_id, exc)
        _clear_asset_ref(room_dir, prop_id)
        return {"ok": False, "degraded": True, "reason": str(exc)[:160]}

    try:
        store.update_prop(
            room_dir,
            prop_id,
            lambda current: {
                **current,
                "assets": {
                    **dict(current.get("assets") or {}),
                    "photo": f"props/{prop_id}/{path.name}",
                    "photo_hash": digest,
                    "photo_provider": str(target_platform.get("id") or target_platform.get("name") or "platform"),
                },
            },
        )
    except Exception:  # noqa: BLE001 - 资产已落盘，spec 更新失败仍可读文件
        logger.warning("props photo asset save failed id=%s", prop_id, exc_info=True)

    return {
        "ok": True,
        "cached": False,
        "path": str(path),
        "filename": path.name,
        "reason": "",
        "degraded": False,
        "prompt_hash": digest,
    }


__all__ = ["ensure_photo", "photo_prompt", "prompt_hash", "reserve_budget", "usage_path"]
