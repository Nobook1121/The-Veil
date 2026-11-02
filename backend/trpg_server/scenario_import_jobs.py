"""Persistent import job state and a small in-process executor."""
from __future__ import annotations
import json, logging, secrets, shutil, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

IMPORT_STAGES = ("parsing", "chunking", "extracting", "merging", "carding", "summarizing", "embedding")
TERMINAL = {"done", "failed", "cancelled", "published"}

class ImportJobStore:
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True); self._lock = threading.RLock()
    def _path(self, job_id: str) -> Path: return self.root / str(job_id) / "job.json"
    def create(self, *, owner_id: str, filename: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        now = time.time(); job_id = secrets.token_urlsafe(18)
        job = {"id": job_id, "owner_id": str(owner_id), "script_id": int(now * 1000), "target_version": "1.0.0", "source_filename": filename, "metadata": dict(metadata), "status": "pending", "progress": 0, "current_stage": "pending", "stage_progress": 0, "stage_meta": {}, "intermediate_results": {}, "cancel_requested": False, "retry_count": 0, "created_at": now, "updated_at": now}
        self.update(job_id, **job); return job
    def get(self, job_id: str) -> dict[str, Any] | None: return read_json(self._path(job_id), default=None)
    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            current = self.get(job_id) or {"id": job_id}
            current.update(changes); current["updated_at"] = time.time(); write_json_atomic(self._path(job_id), current); return current
    def save_intermediate(self, job_id: str, name: str, value: Any) -> Path:
        path = self.root / str(job_id) / "intermediate" / f"{name}.json"; write_json_atomic(path, value); self.update(job_id, intermediate_results={**(self.get(job_id) or {}).get("intermediate_results", {}), name: str(path.relative_to(self.root / str(job_id)))}); return path
    def load_intermediate(self, job_id: str, name: str, default: Any = None) -> Any:
        job = self.get(job_id) or {}; rel = (job.get("intermediate_results") or {}).get(name, f"intermediate/{name}.json"); return read_json(self.root / str(job_id) / rel, default=default)
    def request_cancel(self, job_id: str) -> dict[str, Any]: return self.update(job_id, cancel_requested=True)
    def discard(self, job_id: str) -> bool:
        """彻底移除任务目录（源文件、检查点、预览等），用于「放弃导入」。"""
        path = self.root / str(job_id)
        with self._lock:
            if not path.exists():
                return False
            shutil.rmtree(path, ignore_errors=True)
        return True
    def list_by_owner(self, owner_id: str) -> list[dict[str, Any]]:
        """列出该用户最近创建的导入任务（按创建时间倒序）。"""
        jobs: list[dict[str, Any]] = []
        for path in self.root.glob("*/job.json"):
            job = read_json(path, default={})
            if str(job.get("owner_id")) != str(owner_id):
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda item: float(item.get("created_at") or 0), reverse=True)
    def active_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        """返回该用户当前仍在跑的导入任务；用于「每人同时只能导入一个剧本」守卫。"""
        for job in self.list_by_owner(owner_id):
            if job.get("status") not in TERMINAL:
                return job
        return None
    def recover_interrupted(self) -> int:
        count = 0
        for path in self.root.glob("*/job.json"):
            job = read_json(path, default={})
            # 进程重启后不存在仍在排队的执行器线程，pending 与 running 都视为中断；
            # 否则残留的 pending 会一直挡住该用户后续的新导入。
            if job.get("status") not in TERMINAL:
                self.update(job["id"], status="failed", error="worker interrupted", failed_stage=job.get("current_stage"))
                count += 1
        return count

def public_job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(job); value.pop("embedding", None); value.pop("intermediate_results", None); return value

def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def submit_import_job(app, job_id: str, start_stage: str | None = None):
    executor = app.extensions["scenario_import_executor"]
    store = app.extensions["scenario_import_store"]

    def runner():
        with app.app_context():
            from trpg_server.scenario_import_pipeline import ScenarioImportPipeline
            ScenarioImportPipeline(store, app.config).run(job_id, start_stage=start_stage)
            job = store.get(job_id) or {}
            metadata = job.get("metadata") or {}
            # 「导入前可选」：用户勾选自动发布时，后台任务完成即落盘；失败只记录提示，
            # 任务本身仍保持 done，用户依旧可以回到页面手动审核并发布。
            if job.get("status") == "done" and _truthy(metadata.get("autoPublish")):
                try:
                    from trpg_server.scenario_import_publish import publish_import_job
                    publish_import_job(
                        store,
                        app.config,
                        job,
                        username=str(metadata.get("creator") or ""),
                        user_id=job.get("owner_id"),
                    )
                except Exception as exc:  # noqa: BLE001 - 自动发布失败不应把导入标成失败
                    logger.exception("scenario_import.auto_publish_failed job=%s", job_id)
                    store.update(
                        job_id,
                        stage_meta={
                            **dict(job.get("stage_meta") or {}),
                            "auto_publish_error": f"自动发布失败，请手动发布：{exc}",
                        },
                    )
    return executor.submit(runner)
