"""团录导出任务：内存状态机 + 有界线程池 + 磁盘 PDF（设计稿 §5.5）。

- 状态：``queued`` → ``running`` → ``done`` / ``failed``
- 并发：线程池大小取 ``[room_log] max_concurrent_exports``
- 产物：``<ROOM_LOG_EXPORT_DIR>/<room_id>/<export_id>/export.pdf``
- 元数据：同目录 ``job.json``（含 ``trace_id``），服务重启后据此恢复任务状态
- 保留：超过 ``keep_exports_days`` 的旧产物在启动 / 新建任务时清理
- 超时：单个任务超过 ``ROOM_LOG_JOB_TIMEOUT_SECONDS`` 会被标记为失败

任务状态同时写进内存与磁盘：查询时内存没有记录就从 ``job.json`` 恢复，因此服务
重启后已完成的导出仍可下载；重启时仍在 ``queued`` / ``running`` 的任务会被标记为
``failed``（``log.export_interrupted``），并清理可能残留的半成品 PDF。
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

from trpg_server.json_store import read_json, remove_tree, write_json_atomic
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
STATUS_NAMES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED}

#: 单个导出任务的上限；可在测试里 monkeypatch 后由 ``get_store`` 读取。
ROOM_LOG_JOB_TIMEOUT_SECONDS = 300.0
TIMEOUT_ERROR = "log.timeout"
INTERRUPTED_ERROR = "log.export_interrupted"


class RoomLogExportStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_workers: int = 2,
        timeout_seconds: float | None = None,
    ):
        self.root = Path(root)
        self.max_workers = max(1, int(max_workers or 1))
        if timeout_seconds is None:
            timeout_seconds = ROOM_LOG_JOB_TIMEOUT_SECONDS
        self.timeout_seconds = max(0.0, float(timeout_seconds or 0))
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
            "updated_at": time.time(),
            "trace_id": uuid.uuid4().hex,
            "creator_id": str(creator_id or ""),
            "options": normalize_options(options),
        }
        with self._lock:
            self._jobs[(str(room_id), export_id)] = job
        self._persist(job)
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
        key = (str(room_id), str(export_id))
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                return dict(job)
            # 内存里没有（服务重启/进程外恢复）：尝试从磁盘元数据恢复。
            job = self._restore_unlocked(key)
            return dict(job) if job else None

    def delete(self, room_id: str, export_id: str) -> bool:
        key = (str(room_id), str(export_id))
        with self._lock:
            job = self._jobs.pop(key, None)
        directory = self._job_dir(room_id, export_id)
        removed = job is not None
        if directory is not None:
            if directory.is_dir():
                try:
                    remove_tree(directory)
                    removed = True
                except OSError:
                    logger.warning(
                        "room_log export cleanup failed path=%s", directory, exc_info=True
                    )
            # 兼容旧版本布局：PDF 曾直接放在房间目录下。
            legacy = self._legacy_pdf_path(room_id, export_id)
            if legacy is not None and legacy.is_file():
                try:
                    legacy.unlink()
                except OSError:
                    logger.warning(
                        "room_log export unlink failed path=%s", legacy, exc_info=True
                    )
        return removed

    def pdf_path(self, room_id: str, export_id: str) -> Path | None:
        directory = self._job_dir(room_id, export_id)
        return directory / "export.pdf" if directory is not None else None

    def job_path(self, room_id: str, export_id: str) -> Path | None:
        directory = self._job_dir(room_id, export_id)
        return directory / "job.json" if directory is not None else None

    # ------------------------------------------------------------ 内部执行

    def _run(
        self,
        app,
        key: tuple[str, str],
        room_dir: Path,
        document: Mapping[str, Any],
        font_path: str | None,
    ) -> None:
        with app.app_context():
            self._update(key, status=STATUS_RUNNING, progress=5)
            output = self.pdf_path(*key)
            if output is None:
                self._fail(key, "log.render_failed")
                return
            expired = threading.Event()
            timer: threading.Timer | None = None
            if self.timeout_seconds > 0:
                def _on_timeout() -> None:
                    if self._expire(key):
                        expired.set()

                timer = threading.Timer(self.timeout_seconds, _on_timeout)
                timer.daemon = True
                timer.start()
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
                # 先停看门狗再判定，缩小「渲染刚好成功、超时刚好触发」的竞态窗口。
                if timer is not None:
                    timer.cancel()
                # 超时线程可能已经把状态改成 failed：此时结果直接丢弃，避免覆盖终态。
                if expired.is_set() or self._status(key) == STATUS_FAILED:
                    self._discard_output(output)
                else:
                    self._update(
                        key,
                        status=STATUS_DONE,
                        progress=100,
                        error=None,
                        file={
                            "bytes": result["bytes"],
                            "pages": result["pages"],
                            "name": "export.pdf",
                        },
                    )
            finally:
                if timer is not None:
                    timer.cancel()

    def _update(self, key: tuple[str, str], **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                return
            if job.get("status") in _TERMINAL:
                # 终态不可被迟到的 progress 回调 / 失败分支覆盖（超时优先）。
                return
            job.update(fields)
            job["updated_at"] = time.time()
            snapshot = dict(job)
        self._persist(snapshot)

    def _expire(self, key: tuple[str, str]) -> bool:
        """超时看门狗：把仍在运行的任务标记为失败，返回是否真的改了状态。"""
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job.get("status") in _TERMINAL:
                return False
            job.update(
                status=STATUS_FAILED,
                error=TIMEOUT_ERROR,
                progress=100,
                updated_at=time.time(),
            )
            snapshot = dict(job)
        self._persist(snapshot)
        logger.warning(
            "room_log export timed out export=%s trace_id=%s",
            key[1],
            snapshot.get("trace_id"),
        )
        return True

    def _fail(self, key: tuple[str, str], code: str) -> None:
        self._update(key, status=STATUS_FAILED, error=code, progress=100)

    def _status(self, key: tuple[str, str]) -> str | None:
        with self._lock:
            job = self._jobs.get(key)
            return str(job.get("status")) if job else None

    def _discard_output(self, output: Path) -> None:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            logger.debug("room_log output cleanup failed path=%s", output, exc_info=True)

    def public(self, job: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "export_id": job.get("export_id"),
            "room_id": job.get("room_id"),
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "error": job.get("error"),
            "file": job.get("file"),
            "created_at": job.get("created_at"),
        }
        # 失败响应带上 trace_id，便于用户报障时与日志对齐；成功响应保持原形状。
        if job.get("status") == STATUS_FAILED and job.get("trace_id"):
            payload["trace_id"] = job.get("trace_id")
        return payload

    # ------------------------------------------------------------ 恢复 / 清理

    def _job_dir(self, room_id: str, export_id: str) -> Path | None:
        if not _EXPORT_ID_RE.match(str(export_id or "")):
            return None
        try:
            return safe_join(self.root, str(room_id), str(export_id))
        except (TypeError, ValueError, OSError):
            return None

    def _legacy_pdf_path(self, room_id: str, export_id: str) -> Path | None:
        if not _EXPORT_ID_RE.match(str(export_id or "")):
            return None
        try:
            return safe_join(self.root, str(room_id), f"{export_id}.pdf")
        except (TypeError, ValueError, OSError):
            return None

    def _persist(self, job: Mapping[str, Any]) -> None:
        path = self.job_path(str(job.get("room_id") or ""), str(job.get("export_id") or ""))
        if path is None:
            return
        try:
            write_json_atomic(path, dict(job))
        except OSError:
            logger.warning("room_log job metadata write failed path=%s", path, exc_info=True)

    def _restore_unlocked(self, key: tuple[str, str]) -> dict[str, Any] | None:
        """从 ``job.json`` 恢复一条任务记录（调用方需持锁）。"""
        path = self.job_path(key[0], key[1])
        if path is None:
            return None
        try:
            payload = read_json(path, default=None)
        except OSError:
            logger.warning("room_log job metadata read failed path=%s", path, exc_info=True)
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("export_id") or "") != str(key[1]):
            return None
        if str(payload.get("room_id") or "") != str(key[0]):
            return None
        if payload.get("status") not in STATUS_NAMES:
            return None
        payload.setdefault("trace_id", uuid.uuid4().hex)
        payload.setdefault("updated_at", payload.get("created_at", time.time()))
        payload.setdefault("creator_id", "")
        if payload.get("status") in (STATUS_QUEUED, STATUS_RUNNING):
            # 进程重启后没有线程会继续推进这个任务：标记为中断并清掉半成品。
            payload.update(
                status=STATUS_FAILED,
                error=INTERRUPTED_ERROR,
                progress=100,
                updated_at=time.time(),
            )
            self._jobs[key] = payload
            self._persist(payload)
            self._discard_output(path.parent / "export.pdf")
            logger.warning(
                "room_log export interrupted export=%s trace_id=%s",
                key[1],
                payload.get("trace_id"),
            )
        self._jobs[key] = payload
        return payload

    def recover_interrupted(self) -> int:
        """扫描磁盘元数据，把上次进程遗留的 ``queued`` / ``running`` 任务标为失败。"""
        if not self.root.is_dir():
            return 0
        recovered = 0
        for metadata in self.root.glob("*/*/job.json"):
            directory = metadata.parent
            export_id = directory.name
            room_id = directory.parent.name
            if not _EXPORT_ID_RE.match(export_id):
                continue
            try:
                payload = read_json(metadata, default=None)
            except OSError:
                logger.warning("room_log recover read failed path=%s", metadata, exc_info=True)
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") not in (STATUS_QUEUED, STATUS_RUNNING):
                continue
            # 只信任与本目录一致的文件，避免损坏/伪造元数据被写到别处。
            if str(payload.get("export_id") or export_id) != export_id:
                continue
            if str(payload.get("room_id") or room_id) != room_id:
                continue
            payload["export_id"] = export_id
            payload["room_id"] = room_id
            key = (room_id, export_id)
            with self._lock:
                payload.update(
                    status=STATUS_FAILED,
                    error=INTERRUPTED_ERROR,
                    progress=100,
                    updated_at=time.time(),
                )
                payload.setdefault("trace_id", uuid.uuid4().hex)
                self._jobs[key] = payload
            self._persist(payload)
            self._discard_output(directory / "export.pdf")
            recovered += 1
            logger.warning(
                "room_log export interrupted during restart export=%s trace_id=%s",
                key[1],
                payload.get("trace_id"),
            )
        return recovered

    def cleanup(self, keep_days: int) -> None:
        """清理过期产物与终态任务记录，避免磁盘与内存无限增长。

        ``keep_days <= 0`` 表示关闭自动清理（设计稿未定义 0 的特殊语义，沿用既有
        行为：连内存记录也保留，避免刚创建的任务在轮询期间被误删）。
        """
        keep_days = int(keep_days or 0)
        if keep_days <= 0:
            return
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            stale = [
                key
                for key, job in self._jobs.items()
                if job.get("created_at", 0) < cutoff
                and job.get("status") not in (STATUS_QUEUED, STATUS_RUNNING)
            ]
            for key in stale:
                self._jobs.pop(key, None)
        if not self.root.is_dir():
            return
        root = self.root.resolve()
        # 新布局：每个任务一个目录，含 job.json + export.pdf。
        for metadata in self.root.glob("*/*/job.json"):
            directory = metadata.parent
            try:
                directory.resolve().relative_to(root)
            except ValueError:
                continue
            created_at = self._job_created_at(metadata)
            if created_at is None or created_at >= cutoff:
                continue
            try:
                remove_tree(directory)
            except OSError:
                logger.warning(
                    "room_log cleanup skipped path=%s", directory, exc_info=True
                )
        # 旧布局兼容：PDF 曾直接放在 <root>/<room_id>/<export_id>.pdf。
        for pdf in self.root.glob("*/*.pdf"):
            try:
                pdf.resolve().relative_to(root)
                if pdf.stat().st_mtime < cutoff:
                    pdf.unlink()
            except (OSError, ValueError):
                logger.debug("room_log cleanup skipped path=%s", pdf, exc_info=True)

    def _job_created_at(self, metadata: Path) -> float | None:
        try:
            payload = read_json(metadata, default=None)
        except OSError:
            return None
        if isinstance(payload, dict):
            created_at = payload.get("created_at")
            if isinstance(created_at, (int, float)):
                return float(created_at)
        try:
            return metadata.stat().st_mtime
        except OSError:
            return None


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
            store = RoomLogExportStore(
                root,
                max_workers=int(section.get("max_concurrent_exports") or 2),
                timeout_seconds=ROOM_LOG_JOB_TIMEOUT_SECONDS,
            )
            # 启动时：恢复上次进程遗留的运行中任务，再清掉过期产物。
            try:
                store.recover_interrupted()
                store.cleanup(int(section.get("keep_exports_days") or 0))
            except OSError:
                logger.warning("room_log startup recovery failed", exc_info=True)
            extensions["room_log_export_store"] = store
        return store


def room_log_settings(config_dir: str | Path | None = None) -> dict[str, Any]:
    """读取生效配置的 ``[room_log]`` 段（schema 已保证键齐全）。"""
    from trpg_server.config import load_settings

    return load_settings(config_dir).section("room_log")


__all__ = [
    "INTERRUPTED_ERROR",
    "ROOM_LOG_JOB_TIMEOUT_SECONDS",
    "RoomLogExportStore",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "TIMEOUT_ERROR",
    "get_store",
    "room_log_settings",
]
