"""世界树生成向导的任务状态（设计文档 §8.5）。

预览结果单独存放在 `result.json`，任务元数据只有进度与参数；这样轮询接口
可以保持轻量，作者点「应用到草稿」时再一次性读取完整 bundle。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from trpg_server.json_store import read_json, write_json_atomic

TERMINAL = {"done", "failed", "cancelled"}

logger = logging.getLogger(__name__)


class NarrativeJobStore:
    """单进程内的生成任务仓库；每个任务一个目录：job.json + result.json。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _dir(self, job_id: str) -> Path:
        return self.root / str(job_id)

    def _path(self, job_id: str) -> Path:
        return self._dir(job_id) / "job.json"

    def result_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "result.json"

    def create(self, *, owner_id: str, scenario_id: int, options: Mapping[str, Any]) -> dict[str, Any]:
        now = time.time()
        job_id = secrets.token_urlsafe(18)
        job = {
            "id": job_id,
            "owner_id": str(owner_id),
            "scenario_id": int(scenario_id),
            "options": dict(options),
            "status": "pending",
            "progress": 0,
            "stage": "pending",
            "error": None,
            "result_summary": None,
            "warnings": [],
            "created_at": now,
            "updated_at": now,
        }
        write_json_atomic(self._path(job_id), job)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        payload = read_json(self._path(job_id), default=None)
        return payload if isinstance(payload, Mapping) else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            current = dict(self.get(job_id) or {"id": job_id})
            current.update(changes)
            current["updated_at"] = time.time()
            write_json_atomic(self._path(job_id), current)
            return current

    def save_result(self, job_id: str, result: Mapping[str, Any]) -> Path:
        path = self.result_path(job_id)
        write_json_atomic(path, dict(result))
        return path

    def load_result(self, job_id: str, default: Any = None) -> Any:
        return read_json(self.result_path(job_id), default=default)

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        return self.update(job_id, status="cancelled", stage="cancelled", progress=100, error="cancelled by user")

    def recover_interrupted(self) -> int:
        """服务重启后把未完成任务标记为失败，避免前端无限轮询。"""
        count = 0
        for path in self.root.glob("*/job.json"):
            job = read_json(path, default={})
            if not isinstance(job, Mapping):
                continue
            if job.get("status") in TERMINAL:
                continue
            self.update(
                job.get("id") or path.parent.name,
                status="failed",
                stage="failed",
                progress=100,
                error="worker interrupted",
            )
            count += 1
        return count


def public_job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    """去掉内部字段，返回可直接给前端的视图。"""
    value = {
        "id": job.get("id"),
        "scenario_id": job.get("scenario_id"),
        "options": job.get("options") or {},
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "stage": job.get("stage"),
        "error": job.get("error"),
        "result_summary": job.get("result_summary"),
        "warnings": job.get("warnings") or [],
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }
    return value


def submit_generate_job(app, job_id: str):
    """提交到世界树生成专用线程池；在应用上下文内运行。"""
    executor: ThreadPoolExecutor = app.extensions["narrative_generate_executor"]

    def runner() -> None:
        with app.app_context():
            from trpg_server.agents.narrative.generate import run_generate_job

            run_generate_job(app, job_id)

    return executor.submit(runner)


__all__ = [
    "NarrativeJobStore",
    "public_job_payload",
    "submit_generate_job",
]
