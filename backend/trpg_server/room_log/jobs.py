"""团录导出任务：内存状态机 + 有界线程池 + 磁盘 PDF（设计稿 §5.5）。

- 状态：``queued`` → ``running`` → ``done`` / ``failed``
- 并发：线程池大小取 ``[room_log] max_concurrent_exports``
- 产物：``<ROOM_LOG_EXPORT_DIR>/<room_id>/<export_id>.pdf``
- 保留：超过 ``keep_exports_days`` 的旧产物在每次新建任务时清理

任务状态只存在内存里：服务重启后旧任务不可查询，但已生成的 PDF 文件仍按保留
策略存在磁盘上。这样既避免把大 JSON 状态写进房间目录，也符合「导出是短任务」
的产品定位。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from trpg_server.security import safe_join

from .fonts import FontMissing
from .model import normalize_options
from .render import RenderError, render_pdf

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
_EXPORT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_TERMINAL = {STATUS_DONE, STATUS_FAILED}


class RoomLogExportStore:
    def __init__(self, root: str | Path, *, max_workers: int = 2):
        self.root = Path(root)
        self.max_workers = max(1, int(max_workers or 1))
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------ 生命周期

    def create(
        self,
        room_id: str,
        options: Mapping[str, Any] | None,
        creator_id: Any,
    ) -> dict[str, Any]:
        export_id = uuid.uuid4().hex
        job = {
            "export_id": export_id,
            "room_id": str(room_id),
            "status": STATUS_QUEUED,
            "progress": 0,
            "error": None,
            "file": None,
            "created_at": time.time(),
            "creator_id": str(creator_id or ""),
            "options": normalize_options(options),
        }
        with self._lock:
            self._jobs[(str(room_id), export_id)] = job
        return self.public(job)

    def start(
        self,
        app,
        room_dir: str | Path,
        document: Mapping[str, Any],
        job: Mapping[str, Any],
        *,
        font_path: str | None = None,
    ) -> None:
        """把已构建的文档交给后台线程渲染。``app`` 用于在线程内推上下文。"""
        with self._lock:
            executor = self._executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="room-log-export"
                )
                self._executor = executor
        key = (str(job["room_id"]), str(job["export_id"]))
        executor.submit(self._run, app, key, Path(room_dir), dict(document), font_path)

    def get(self, room_id: str, export_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get((str(room_id), str(export_id)))
            return dict(job) if job else None

    def delete(self, room_id: str, export_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop((str(room_id), str(export_id)), None)
        if job and job.get("status") == STATUS_DONE:
            path = self.pdf_path(room_id, export_id)
            if path and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    logger.warning("room_log export unlink failed path=%s", path, exc_info=True)
        return job is not None

    def pdf_path(self, room_id: str, export_id: str) -> Path | None:
        if not _EXPORT_ID_RE.match(str(export_id or "")):
            return None
        try:
            return safe_join(self.root, str(room_id), f"{export_id}.pdf")
        except (TypeError, ValueError, OSError):
            return None

    # ------------------------------------------------------------ 内部执行

    def _run(self, app, key: tuple[str, str], room_dir: Path, document: Mapping[str, Any], font_path: str | None) -> None:
        with app.app_context():
            self._update(key, status=STATUS_RUNNING, progress=5)
            output = self.pdf_path(key[0], key[1])
            if output is None:
                self._fail(key, "log.render_failed")
                return
            try:
                from trpg_server.settings import DATA_DIR

                options = document.get("options") or {}
                result = render_pdf(
                    document,
                    output,
                    font_path=font_path,
                    template=str(options.get("template") or "archive"),
                    room_dir=room_dir,
                    data_dir=DATA_DIR,
                    progress=lambda value: self._update(key, progress=value),
                )
            except FontMissing:
                logger.warning("room_log export failed: font missing room=%s", key[0])
                self._fail(key, "log.font_missing")
            except RenderError as exc:
                code = str(exc) or "log.render_failed"
                if not code.startswith("log."):
                    code = "log.render_failed"
                self._fail(key, code)
            except Exception:  # noqa: BLE001 - 任务线程必须吞掉异常并回写状态
                logger.exception("room_log export crashed room=%s export=%s", key[0], key[1])
                self._fail(key, "log.render_failed")
            else:
                self._update(
                    key,
                    status=STATUS_DONE,
                    progress=100,
                    error=None,
                    file={
                        "bytes": result["bytes"],
                        "pages": result["pages"],
                        "name": f"{key[1]}.pdf",
                    },
                )

    def _update(self, key: tuple[str, str], **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                return
            job.update(fields)

    def _fail(self, key: tuple[str, str], code: str) -> None:
        self._update(key, status=STATUS_FAILED, error=code, progress=100)

    def public(self, job: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "export_id": job.get("export_id"),
            "room_id": job.get("room_id"),
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "error": job.get("error"),
            "file": job.get("file"),
            "created_at": job.get("created_at"),
        }

    def cleanup(self, keep_days: int) -> None:
        """清理过期 PDF 与终态任务记录，避免磁盘与内存无限增长。

        ``keep_days <= 0`` 表示关闭自动清理（连内存记录也保留，避免刚创建的任务
        在轮询期间被误删）。
        """
        keep_days = int(keep_days or 0)
        if keep_days <= 0:
            return
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            stale = [key for key, job in self._jobs.items() if job.get("created_at", 0) < cutoff]
            for key in stale:
                self._jobs.pop(key, None)
        if not self.root.is_dir():
            return
        for pdf in self.root.glob("*/*.pdf"):
            try:
                if pdf.stat().st_mtime < cutoff:
                    pdf.unlink()
            except OSError:
                logger.debug("room_log cleanup skipped path=%s", pdf, exc_info=True)


_store_lock = threading.Lock()


def get_store() -> RoomLogExportStore:
    """按 Flask app 缓存一个任务存储；根目录与并发数来自配置。"""
    from flask import current_app

    from trpg_server.config import load_settings
    from trpg_server.settings import DATA_DIR

    extensions = current_app.extensions
    store = extensions.get("room_log_export_store")
    if store is not None:
        return store
    with _store_lock:
        store = extensions.get("room_log_export_store")
        if store is None:
            config_dir = current_app.config.get("CONFIG_DIR")
            section = load_settings(config_dir).section("room_log")
            root = current_app.config.get("ROOM_LOG_EXPORT_DIR") or (
                Path(DATA_DIR) / "runtime" / "logs" / "exports"
            )
            store = RoomLogExportStore(root, max_workers=int(section.get("max_concurrent_exports") or 2))
            extensions["room_log_export_store"] = store
        return store


def room_log_settings(config_dir: str | Path | None = None) -> dict[str, Any]:
    """读取生效配置的 ``[room_log]`` 段（schema 已保证键齐全）。"""
    from trpg_server.config import load_settings

    return load_settings(config_dir).section("room_log")


__all__ = [
    "RoomLogExportStore",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "get_store",
    "room_log_settings",
]
