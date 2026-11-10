"""应用更新检查与下载服务。

只做「检查版本 + 下载发布包」，不做自动替换：Windows 下运行中的进程会锁定
自身文件，静默覆盖安装目录风险过高（详见设计文档 §8.3.3）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask

from trpg_server.config import load_settings
from trpg_server.json_store import write_json_atomic
from trpg_server.settings import RUNTIME_DIR
from trpg_server.version import APP_VERSION

logger = logging.getLogger(__name__)

DISABLE_ENV = "AI_TRPG_DISABLE_UPDATE_CHECK"
CHECK_THROTTLE_SECONDS = 60
DOWNLOAD_CHUNK_SIZE = 1 << 15
DOWNLOAD_PROGRESS_STEP = 256 * 1024

_lock = threading.Lock()
_download_lock = threading.Lock()
_last_check_at = 0.0
_last_stars: int | None = None
_download_progress = 0
_started_apps: set[int] = set()


def normalize_repo(value: Any) -> str:
    """接受完整 GitHub URL 或 owner/name，统一成 owner/name。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = re.sub(r"^github\.com/", "", text)
    text = text.split("?", 1)[0].split("#", 1)[0]
    parts = [segment for segment in text.strip("/").split("/") if segment]
    if len(parts) < 2:
        return ""
    owner = re.sub(r"\.git$", "", parts[0])
    name = re.sub(r"\.git$", "", parts[1])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return ""
    return f"{owner}/{name}"


def _parse_version(value: str) -> tuple[tuple[int, ...], int]:
    """把 v1.2.3-beta.1 解析成 (数字段, 稳定标记)；预发布标记为 0，正式版为 1。"""
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    core = text.split("+", 1)[0]
    stable = 1
    if "-" in core:
        core, _suffix = core.split("-", 1)
        stable = 0
    numbers: list[int] = []
    for part in core.split("."):
        match = re.match(r"\d+", part)
        numbers.append(int(match.group(0)) if match else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers), stable


def compare_versions(latest: str, current: str) -> int:
    """latest 比 current 新返回 1，相同返回 0，更旧返回 -1。"""
    latest_numbers, latest_stable = _parse_version(latest)
    current_numbers, current_stable = _parse_version(current)
    if latest_numbers != current_numbers:
        return 1 if latest_numbers > current_numbers else -1
    if latest_stable != current_stable:
        return 1 if latest_stable > current_stable else -1
    return 0


def _update_dir(app: Flask | None) -> Path:
    override = app.config.get("UPDATE_DIR") if app is not None else None
    return Path(override) if override else RUNTIME_DIR / "update"


def _state_path(app: Flask | None) -> Path:
    return _update_dir(app) / "state.json"


def _downloads_dir(app: Flask | None) -> Path:
    return _update_dir(app) / "downloads"


_LEGACY_STATES = {"idle": "unknown", "update_available": "available", "downloaded": "ready"}


def _default_state() -> dict[str, Any]:
    return {
        "state": "unknown",
        "current": APP_VERSION,
        "latest": "",
        "tag": "",
        "notes": "",
        "release_url": "",
        "published_at": "",
        "checked_at": "",
        "message": "",
        "download": {},
    }


def _read_state(app: Flask | None) -> dict[str, Any]:
    path = _state_path(app)
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("update state file is unreadable: %s", path)
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    merged = _default_state()
    merged.update(data)
    # 兼容 1.0.2 之前写入的旧状态名。
    merged["state"] = _LEGACY_STATES.get(merged.get("state"), merged.get("state") or "unknown")
    return merged


def _write_state(app: Flask | None, payload: dict[str, Any]) -> None:
    try:
        write_json_atomic(_state_path(app), payload)
    except OSError:
        logger.warning("failed to persist update state", exc_info=True)


def _update_config(app: Flask) -> dict[str, Any]:
    settings = load_settings(app.config.get("CONFIG_DIR"))
    section = settings.section("update")
    repo = normalize_repo(section.get("repo"))
    channel = str(section.get("channel") or "stable").strip().lower()
    if channel not in ("stable", "prerelease"):
        channel = "stable"
    try:
        timeout = float(section.get("request_timeout_seconds") or 8)
    except (TypeError, ValueError):
        timeout = 8.0
    return {
        "repo": repo,
        "channel": channel,
        "timeout": max(3.0, min(timeout, 15.0)),
        "check_on_startup": bool(section.get("check_update_on_startup", True)),
        "auto_download": bool(section.get("auto_update", False)),
    }


def is_disabled(app: Flask | None, repo: str) -> bool:
    if os.environ.get(DISABLE_ENV) == "1":
        return True
    if app is not None and app.config.get("TESTING"):
        return True
    return not repo


def get_cached_stars() -> int | None:
    return _last_stars


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _public_state(state: dict[str, Any], enabled: bool) -> dict[str, Any]:
    download = state.get("download") if isinstance(state.get("download"), dict) else {}
    progress = _download_progress if state.get("state") == "downloading" else int(download.get("progress") or 0)
    return {
        "state": state.get("state") or "unknown",
        "current": state.get("current") or APP_VERSION,
        "latest": state.get("latest") or "",
        "notes": state.get("notes") or "",
        "release_url": state.get("release_url") or "",
        "published_at": state.get("published_at") or "",
        "checked_at": state.get("checked_at") or "",
        "message": state.get("message") or "",
        "progress": max(0, min(int(progress), 100)),
        "enabled": enabled,
    }


def get_state(app: Flask | None = None) -> dict[str, Any]:
    state = _read_state(app)
    try:
        repo = _update_config(app).get("repo") if app is not None else ""
    except Exception:
        repo = ""
    return _public_state(state, not is_disabled(app, repo or ""))


def _github_headers() -> dict[str, str]:
    return {
        "User-Agent": f"The-Veil/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_stars(repo: str, timeout: float) -> None:
    global _last_stars
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=_github_headers(),
            timeout=timeout,
        )
        if response.ok:
            payload = response.json()
            value = payload.get("stargazers_count")
            if isinstance(value, int):
                _last_stars = value
    except (requests.RequestException, ValueError):
        logger.debug("failed to fetch repository stars", exc_info=True)


def _select_release(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft"):
            continue
        if channel == "stable" and release.get("prerelease"):
            continue
        return release
    return None


def _release_asset(release: dict[str, Any], tag: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    assets = [asset for asset in release.get("assets") or [] if isinstance(asset, dict)]
    bundle = next((asset for asset in assets if asset.get("name") == f"the-veil-{tag}.zip"), None)
    if bundle is None:
        bundle = next((asset for asset in assets if str(asset.get("name", "")).endswith(".zip")), None)
    checksum = next((asset for asset in assets if str(asset.get("name", "")).endswith(".zip.sha256")), None)
    return bundle, checksum


def _resolve_current(app: Flask | None) -> dict[str, Any]:
    state = _read_state(app)
    payload = _default_state()
    payload.update(state)
    payload["current"] = APP_VERSION
    return payload


def _disabled_state(app: Flask | None, repo: str, message: str) -> dict[str, Any]:
    state = _resolve_current(app)
    state.update({
        "state": "disabled",
        "enabled": False,
        "message": message,
        "checked_at": _now_iso(),
    })
    _write_state(app, state)
    return _public_state(state, False)


def check_for_update(force: bool = False, app: Flask | None = None) -> dict[str, Any]:
    """检查 GitHub 最新发布；失败时写 error 状态而不抛异常。"""
    global _last_check_at

    if app is None:
        return get_state(None)
    config = _update_config(app)
    repo = config["repo"]
    if is_disabled(app, repo):
        message = "未配置更新仓库" if not repo else "更新检查已停用"
        return _disabled_state(app, repo, message)

    with _lock:
        now = time.monotonic()
        if not force and now - _last_check_at < CHECK_THROTTLE_SECONDS:
            state = _resolve_current(app)
            state["enabled"] = True
            return _public_state(state, True)
        _last_check_at = now

        state = _resolve_current(app)
        timeout = config["timeout"]
        try:
            response = requests.get(
                f"https://api.github.com/repos/{repo}/releases?per_page=10",
                headers=_github_headers(),
                timeout=timeout,
            )
            if response.status_code == 404:
                raise ValueError("仓库不存在或不可访问")
            if not response.ok:
                raise ValueError(f"GitHub 返回 {response.status_code}")
            releases = response.json()
            if not isinstance(releases, list):
                raise ValueError("发布信息格式异常")
            release = _select_release(releases, config["channel"])
            if release is None:
                raise ValueError("没有可用的发布版本")

            tag = str(release.get("tag_name") or release.get("name") or "").strip()
            latest = tag[1:] if tag.lower().startswith("v") else tag
            bundle, checksum = _release_asset(release, tag)
            state.update({
                "state": "up_to_date",
                "latest": latest,
                "tag": tag,
                "notes": str(release.get("body") or "")[:20000],
                "release_url": str(release.get("html_url") or ""),
                "published_at": str(release.get("published_at") or ""),
                "checked_at": _now_iso(),
                "message": "",
                "download": {},
            })
            if compare_versions(latest, APP_VERSION) > 0:
                state["state"] = "available"
                state["message"] = f"发现新版本 {latest}"
                state["download"] = {
                    "file": str((bundle or {}).get("name") or ""),
                    "size": int((bundle or {}).get("size") or 0),
                    "url": str((bundle or {}).get("browser_download_url") or ""),
                    "sha256_url": str((checksum or {}).get("browser_download_url") or ""),
                    "progress": 0,
                }
            _write_state(app, state)
            _fetch_stars(repo, timeout)
            return _public_state(state, True)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("update check failed: %s", exc)
            state.update({
                "state": "error",
                "checked_at": _now_iso(),
                "message": "无法连接 GitHub，请检查网络或代理" if isinstance(exc, requests.RequestException) else str(exc),
            })
            _write_state(app, state)
            return _public_state(state, True)


def _expected_sha256(url: str, timeout: float) -> str:
    if not url:
        return ""
    try:
        response = requests.get(url, timeout=timeout)
        if not response.ok:
            return ""
        match = re.search(r"\b([a-fA-F0-9]{64})\b", response.text)
        return match.group(1).lower() if match else ""
    except requests.RequestException:
        return ""


def download_update(app: Flask | None = None) -> dict[str, Any]:
    """阻塞式下载发布包；成功/失败都写回 state.json。"""
    global _download_progress

    if app is None:
        return get_state(None)
    config = _update_config(app)
    state = _resolve_current(app)
    download = state.get("download") if isinstance(state.get("download"), dict) else {}
    url = str(download.get("url") or "")
    if state.get("state") != "available" or not url:
        return _public_state(state, not is_disabled(app, config["repo"]))
    if not _download_lock.acquire(blocking=False):
        return _public_state(state, True)

    _download_progress = 0
    target = _downloads_dir(app) / str(download.get("file") or f"the-veil-{state.get('tag') or 'latest'}.zip")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        state["state"] = "downloading"
        state["message"] = "正在下载更新包"
        _write_state(app, state)

        expected = _expected_sha256(str(download.get("sha256_url") or ""), config["timeout"])
        digest = hashlib.sha256()
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or download.get("size") or 0)
            written = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if total > 0:
                        progress = int(written * 100 / total)
                        if progress != _download_progress:
                            _download_progress = progress
                    if written % DOWNLOAD_PROGRESS_STEP == 0:
                        logger.debug("update download progress=%s%%", _download_progress)
        _download_progress = 100
        if expected and digest.hexdigest() != expected:
            target.unlink(missing_ok=True)
            state["state"] = "error"
            state["message"] = "更新包校验失败，已删除损坏文件"
        else:
            state["state"] = "ready"
            state["message"] = "更新包已下载，请手动替换程序目录后重启"
            state["download"] = {**download, "progress": 100, "sha256": digest.hexdigest()}
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.warning("update download failed: %s", exc)
        target.unlink(missing_ok=True)
        state["state"] = "error"
        state["message"] = "下载失败，请检查网络后重试"
        _download_progress = 0
    finally:
        _write_state(app, state)
        _download_lock.release()
    return _public_state(state, True)


def start_startup_checker(app: Flask) -> None:
    """启动后延迟 60 秒检查一次；尊重配置与测试开关。"""
    if id(app) in _started_apps:
        return
    if os.environ.get(DISABLE_ENV) == "1" or app.config.get("TESTING"):
        return
    try:
        config = _update_config(app)
    except Exception:
        logger.debug("skip startup update checker: config unavailable", exc_info=True)
        return
    if not config["check_on_startup"] or is_disabled(app, config["repo"]):
        return
    _started_apps.add(id(app))

    def _run() -> None:
        time.sleep(60)
        check_for_update(force=True, app=app)
        if _read_state(app).get("state") == "available" and config["auto_download"]:
            download_update(app)

    threading.Thread(target=_run, name="update-checker", daemon=True).start()
