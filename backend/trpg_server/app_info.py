"""关于页数据：版本、构建、仓库、运行时与致谢。

所有读取失败都降级为空结构，绝不因为缺失文件或网络问题返回 500。
"""

from __future__ import annotations

import json
import logging
import platform as platform_module
import threading
from pathlib import Path
from typing import Any

from flask import Flask

from trpg_server import update_service
from trpg_server.config import load_settings
from trpg_server.settings import RUNTIME_DIR
from trpg_server.version import (
    APP_DEFAULT_REPO,
    APP_DISPLAY_NAME,
    APP_LICENSE_NAME,
    APP_LICENSE_SPDX,
    APP_LICENSE_URL,
    APP_VERSION,
)

logger = logging.getLogger(__name__)

_CREDITS_PATH = Path(__file__).resolve().parent / "resources" / "credits.json"
_credits_lock = threading.Lock()
_credits_cache: tuple[float, dict[str, Any]] | None = None

_EMPTY_CREDITS: dict[str, Any] = {
    "contributors": [],
    "special_thanks": [],
    "dependencies": [],
    "sponsor_channels": [],
    "sponsors": [],
}


def _normalize_list(value: Any) -> list[Any]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def load_credits() -> dict[str, Any]:
    """读取并缓存 resources/credits.json；文件变更时按 mtime 失效。"""
    global _credits_cache
    try:
        mtime = _CREDITS_PATH.stat().st_mtime
    except OSError:
        return dict(_EMPTY_CREDITS)
    with _credits_lock:
        if _credits_cache and _credits_cache[0] == mtime:
            return _credits_cache[1]
    try:
        raw = json.loads(_CREDITS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("credits must be an object")
        credits = {
            "contributors": _normalize_list(raw.get("contributors")),
            "special_thanks": _normalize_list(raw.get("special_thanks")),
            "dependencies": _normalize_list(raw.get("dependencies")),
            "sponsor_channels": _normalize_list(raw.get("sponsor_channels")),
            "sponsors": _normalize_list(raw.get("sponsors")),
        }
    except (OSError, ValueError):
        logger.warning("credits.json is unreadable", exc_info=True)
        credits = dict(_EMPTY_CREDITS)
    with _credits_lock:
        _credits_cache = (mtime, credits)
    return credits


_BUILD_MANIFEST = Path(__file__).resolve().parent / "build_manifest.json"


def _read_manifest_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("build manifest is unreadable: %s", path)
        return None
    return raw if isinstance(raw, dict) else None


def _read_build_manifest(app: Flask | None) -> dict[str, Any]:
    """优先读运行时 build.json，其次读发布包内的 build_manifest.json。"""
    runtime_dir = Path(app.config.get("RUNTIME_DIR") or RUNTIME_DIR) if app is not None else RUNTIME_DIR
    for path in (runtime_dir / "build.json", _BUILD_MANIFEST):
        raw = _read_manifest_file(path)
        if raw is not None:
            # 发布包内清单没有 mode 字段：存在清单即视为正式构建。
            raw.setdefault("mode", "release")
            return raw
    return {"mode": "dev", "sha": "local"}


def _repo_config(app: Flask | None) -> str:
    if app is None:
        return APP_DEFAULT_REPO
    try:
        section = load_settings(app.config.get("CONFIG_DIR")).section("update")
    except Exception:
        logger.debug("failed to read update config", exc_info=True)
        return APP_DEFAULT_REPO
    return update_service.normalize_repo(section.get("repo")) or APP_DEFAULT_REPO


def _repo_payload(repo: str) -> dict[str, Any] | None:
    if not repo:
        return None
    base = f"https://github.com/{repo}"
    return {
        "url": base,
        "github": repo,
        "issues": f"{base}/issues",
        "discussions": f"{base}/discussions",
        "wiki": f"{base}/wiki",
        "releases": f"{base}/releases",
        "license": f"{base}/blob/main/LICENSE",
        "third_party": f"{base}/blob/main/THIRD-PARTY-LICENSES.md",
        "stars": update_service.get_cached_stars(),
    }


def build_app_info(app: Flask | None = None) -> dict[str, Any]:
    manifest = _read_build_manifest(app)
    repo = _repo_config(app)
    config_channel = "stable"
    if app is not None:
        try:
            config_channel = str(load_settings(app.config.get("CONFIG_DIR")).section("update").get("channel") or "stable")
        except Exception:
            config_channel = "stable"
    credits = load_credits()
    return {
        "name": APP_DISPLAY_NAME,
        "version": APP_VERSION,
        "build": {
            "mode": str(manifest.get("mode") or "dev"),
            "sha": str(manifest.get("sha") or "local"),
            "built_at": manifest.get("built_at"),
            "channel": str(manifest.get("channel") or config_channel),
        },
        "license": {
            "spdx": APP_LICENSE_SPDX,
            "name": APP_LICENSE_NAME,
            "url": APP_LICENSE_URL,
        },
        "repo": _repo_payload(repo),
        "runtime": {
            "python": platform_module.python_version(),
            "platform": f"{platform_module.system()}-{platform_module.release()}",
        },
        "credits": credits,
    }
