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
_CREDITS_SCHEMA_VERSION = 1
_credits_lock = threading.Lock()
_credits_cache: tuple[float, dict[str, Any]] | None = None

# 每类条目的已知字段。归一化时补齐为空字符串，旧文件缺字段也不会让前端拿到
# undefined；条目里额外的自定义字段原样保留，不因归一化丢失。
_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "contributors": ("name", "role", "url", "note"),
    "special_thanks": ("name", "note"),
    "dependencies": ("name", "version", "license", "url", "usage"),
    "sponsor_channels": ("id", "label", "url", "note"),
    "sponsors": ("name", "tier", "since", "url", "message"),
}

_EMPTY_CREDITS: dict[str, Any] = {
    "schema_version": _CREDITS_SCHEMA_VERSION,
    "updated_at": "",
    "contributors": [],
    "special_thanks": [],
    "dependencies": [],
    "sponsor_channels": [],
    "sponsors": [],
}


def _empty_credits() -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, list) else value) for key, value in _EMPTY_CREDITS.items()}


def _normalize_list(value: Any) -> list[Any]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _normalize_entries(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _normalize_list(value):
        entry = dict(item)
        for field in fields:
            raw = item.get(field)
            entry[field] = str(raw).strip() if raw is not None else ""
        entries.append(entry)
    return entries


def _normalize_sponsor_channels(value: Any) -> list[dict[str, Any]]:
    """赞助渠道必须是可点击的真实地址：缺 id/url 或仍是占位文本的条目不进 release。"""
    channels: list[dict[str, Any]] = []
    for entry in _normalize_entries(value, _ENTRY_FIELDS["sponsor_channels"]):
        if not entry["id"] or not entry["url"]:
            continue
        if any("CHANGE_ME" in item for item in entry.values()):
            logger.warning("credits.json contains a placeholder sponsor channel; entry skipped")
            continue
        channels.append(entry)
    return channels


def _normalize_schema_version(value: Any) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError):
        return _CREDITS_SCHEMA_VERSION
    return version if version > 0 else _CREDITS_SCHEMA_VERSION


def load_credits() -> dict[str, Any]:
    """读取并缓存 resources/credits.json；文件变更时按 mtime 失效。"""
    global _credits_cache
    try:
        mtime = _CREDITS_PATH.stat().st_mtime
    except OSError:
        return _empty_credits()
    with _credits_lock:
        if _credits_cache and _credits_cache[0] == mtime:
            return _credits_cache[1]
    try:
        raw = json.loads(_CREDITS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("credits must be an object")
        credits = {
            "schema_version": _normalize_schema_version(raw.get("schema_version")),
            "updated_at": str(raw.get("updated_at") or "").strip(),
            "contributors": _normalize_entries(raw.get("contributors"), _ENTRY_FIELDS["contributors"]),
            "special_thanks": _normalize_entries(raw.get("special_thanks"), _ENTRY_FIELDS["special_thanks"]),
            "dependencies": _normalize_entries(raw.get("dependencies"), _ENTRY_FIELDS["dependencies"]),
            "sponsor_channels": _normalize_sponsor_channels(raw.get("sponsor_channels")),
            "sponsors": _normalize_entries(raw.get("sponsors"), _ENTRY_FIELDS["sponsors"]),
        }
    except (OSError, ValueError):
        logger.warning("credits.json is unreadable", exc_info=True)
        credits = _empty_credits()
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
            # platform 保留「系统-内核版本」完整字段（旧前端契约），system 是只含系统名的
            # 简化字段；两者都不包含主机名/用户名等本机信息（计划 §8.8）。
            "platform": f"{platform_module.system()}-{platform_module.release()}",
            "system": platform_module.system(),
        },
        "credits": credits,
    }
