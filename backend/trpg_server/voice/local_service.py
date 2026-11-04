"""本地 TTS 适配层：子进程生命周期、健康检查、并发闸门、缓存与降级。

降级链固定为 ``local → system → 仅文字``：
- 子服务未安装/未启动 → ``local_unavailable``；
- 单次请求超时 → ``local_timeout``；
- 子服务返回错误或崩溃 → ``local_error``；
以上情况都只记录并向上层返回 reason，由导演决定回落到 ``system`` 或仅文字，绝不阻塞叙事。
"""
from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from trpg_server.immersive import task_gate
from trpg_server.settings import RUNTIME_DIR
from trpg_server.voice import events, local_cache, local_models

logger = logging.getLogger(__name__)

REASON_LOCAL_DISABLED = "local_disabled"
REASON_LOCAL_MODEL_MISSING = "local_model_missing"
REASON_LOCAL_UNAVAILABLE = "local_unavailable"
REASON_LOCAL_TIMEOUT = "local_timeout"
REASON_LOCAL_ERROR = "local_error"

_STATE_LOCK = threading.RLock()
_GATE = threading.Condition()
_ACTIVE = 0
_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "data": None}
_WORKER: dict[str, Any] = {
    "proc": None,
    "base_url": "",
    "model": "",
    "started_at": 0.0,
    "last_error": "",
    "log_handle": None,
}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _worker_file() -> Path:
    return RUNTIME_DIR / "voice" / "local_worker.log"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_base_url(settings: dict[str, Any]) -> str:
    return str(settings.get("local_base_url") or "").strip().rstrip("/")


def _backend_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _read_status_line(proc: subprocess.Popen) -> None:
    try:
        if proc.stdout is None:
            return
        line = proc.stdout.readline()
        if line:
            logger.info("voice local worker ready: %s", line.strip())
    except Exception:  # noqa: BLE001
        pass


def _worker_start(settings: dict[str, Any]) -> tuple[bool, str]:
    """启动（或复用）本地子服务；返回是否可用与具体原因。"""
    base_url = _resolve_base_url(settings)
    model = str(settings.get("local_model") or local_models.MODEL_ID)
    with _STATE_LOCK:
        if base_url:
            # 管理员自行托管的外部回环服务：不拉子进程、不管理其生命周期。
            _WORKER.update({"base_url": base_url, "model": model, "proc": None})
            return True, ""
        proc = _WORKER.get("proc")
        if proc is not None and proc.poll() is None and _WORKER.get("base_url"):
            if _WORKER.get("model") == model:
                return True, ""
            _stop_worker_locked()
        status = local_models.installed_status(model)
        if not status["installed"]:
            return False, REASON_LOCAL_MODEL_MISSING
        port = _free_port()
        log_path = _worker_file()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
        except OSError:
            log_handle = None
        env = os.environ.copy()
        backend = _backend_root()
        env["PYTHONPATH"] = backend + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [
            sys.executable,
            "-m",
            "trpg_server.voice.local_worker",
            "--model-dir",
            str(Path(status["path"])),
            "--port",
            str(port),
            # 线程数独立于并发闸门：单次合成本身需要多线程才有实时率。
            "--threads",
            str(min(8, max(4, (os.cpu_count() or 4) // 2))),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=backend,
                env=env,
                stdout=subprocess.PIPE,
                stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            if log_handle is not None:
                log_handle.close()
            logger.error("voice local worker spawn failed: %s", exc)
            return False, REASON_LOCAL_UNAVAILABLE
        _WORKER.update(
            {
                "proc": proc,
                "base_url": f"http://127.0.0.1:{port}",
                "model": model,
                "started_at": time.time(),
                "last_error": "",
                "log_handle": log_handle,
            }
        )
        threading.Thread(target=_read_status_line, args=(proc,), daemon=True).start()
        # 权重加载需要数秒，必须等 /health 真正可连再返回，否则首句会被连接拒绝。
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _stop_worker_locked()
                return False, REASON_LOCAL_UNAVAILABLE
            ok, _, _ = _probe(f"http://127.0.0.1:{port}", timeout=1.0)
            if ok:
                return True, ""
            time.sleep(0.2)
        _stop_worker_locked()
        return False, REASON_LOCAL_TIMEOUT
    return True, ""


def _stop_worker_locked() -> None:
    proc = _WORKER.get("proc")
    base_url = _WORKER.get("base_url")
    if proc is not None and proc.poll() is None:
        try:
            if base_url:
                requests.post(f"{base_url}/shutdown", timeout=1.5)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    handle = _WORKER.get("log_handle")
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
    _WORKER.update({"proc": None, "base_url": "", "log_handle": None})


def stop_service() -> None:
    with _STATE_LOCK:
        _stop_worker_locked()


atexit.register(stop_service)


def _probe(base_url: str, timeout: float = 2.0) -> tuple[bool, str, dict[str, Any]]:
    try:
        response = requests.get(f"{base_url}/health", timeout=timeout)
        if response.status_code != 200:
            return False, REASON_LOCAL_UNAVAILABLE, {}
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return False, REASON_LOCAL_UNAVAILABLE, data if isinstance(data, dict) else {}
        return True, "", data
    except requests.Timeout:
        return False, REASON_LOCAL_TIMEOUT, {}
    except Exception:  # noqa: BLE001
        return False, REASON_LOCAL_UNAVAILABLE, {}


def health(settings: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    engine_settings = settings if isinstance(settings, dict) else {}
    key = "|".join(
        (
            str(engine_settings.get("local_enabled")),
            _resolve_base_url(engine_settings),
            str(engine_settings.get("local_model") or local_models.MODEL_ID),
        )
    )
    now = time.monotonic()
    with _STATE_LOCK:
        cached = _HEALTH_CACHE.get("data")
        if not force and cached and _HEALTH_CACHE.get("key") == key and now - float(_HEALTH_CACHE.get("ts") or 0) < 5:
            return dict(cached)
    result: dict[str, Any]
    if not engine_settings.get("local_enabled"):
        result = {
            "engine": "local",
            "ok": False,
            "mode": "server",
            "reason": REASON_LOCAL_DISABLED,
            "model": str(engine_settings.get("local_model") or local_models.MODEL_ID),
        }
    else:
        started_ok, start_reason = _worker_start(engine_settings)
        if not started_ok:
            result = {
                "engine": "local",
                "ok": False,
                "mode": "server",
                "reason": start_reason,
                "model": str(engine_settings.get("local_model") or local_models.MODEL_ID),
            }
        else:
            base_url = str(_WORKER.get("base_url") or "")
            ok, reason, data = _probe(base_url, timeout=3.0)
            if not ok and reason == REASON_LOCAL_UNAVAILABLE:
                # 首次启动可能仍在加载权重，给一次短暂等待。
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    proc = _WORKER.get("proc")
                    if proc is not None and proc.poll() is not None:
                        break
                    time.sleep(0.5)
                    ok, reason, data = _probe(base_url, timeout=2.0)
                    if ok:
                        break
            result = {
                "engine": "local",
                "ok": bool(ok),
                "mode": "server",
                "reason": "" if ok else (reason or REASON_LOCAL_UNAVAILABLE),
                "model": str(engine_settings.get("local_model") or local_models.MODEL_ID),
                "base_url": base_url,
                "sample_rate": data.get("sample_rate") if ok else None,
                "speakers": data.get("speakers") if ok else None,
            }
    with _STATE_LOCK:
        _HEALTH_CACHE.update({"ts": now, "key": key, "data": dict(result)})
    return result


def _acquire(limit: int, timeout: float) -> bool:
    global _ACTIVE
    deadline = time.monotonic() + max(0.1, timeout)
    with _GATE:
        while True:
            if _ACTIVE < limit and task_gate.heavy_active() == 0:
                _ACTIVE += 1
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _GATE.wait(min(remaining, 0.1))


def _release() -> None:
    global _ACTIVE
    with _GATE:
        _ACTIVE = max(0, _ACTIVE - 1)
        _GATE.notify_all()


def active_requests() -> int:
    with _GATE:
        return _ACTIVE


def speaker_binding(
    profile_id: str,
    settings: dict[str, Any],
    room_voice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析 profile → 真实说话人，来源优先级：房间 > 站点 > 默认。"""
    room_map = (room_voice or {}).get("local_speakers")
    room_map = room_map if isinstance(room_map, dict) else {}
    site_map = settings.get("local_speakers")
    site_map = site_map if isinstance(site_map, dict) else {}
    default = local_models.DEFAULT_SPEAKERS.get(profile_id, 24)
    source = "default"
    sid = default
    for candidate, label in ((site_map, "site"), (room_map, "room")):
        value = candidate.get(profile_id) if isinstance(candidate, dict) else None
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= parsed <= local_models.SPEAKER_COUNT - 1:
            sid = parsed
            source = label
    return {
        "sid": sid,
        "voice_id": local_models.speaker_name(sid),
        "gender": local_models.speaker_gender(sid),
        "source": source,
        "model": str(settings.get("local_model") or local_models.MODEL_ID),
    }


def synthesize(
    room_dir: Path | str | None,
    *,
    text: str,
    voice_id: int | str,
    speed: float,
    settings: dict[str, Any],
    room_id: str = "",
    message_id: str = "",
    turn: int = 0,
    profile_id: str = "",
    speaker: str = "",
) -> dict[str, Any]:
    """合成单句；返回 ``{ok, reason, audio, ...}``，永不抛错。"""
    cfg = settings if isinstance(settings, dict) else {}
    model = str(cfg.get("local_model") or local_models.MODEL_ID)
    sid = _as_int(voice_id, 0, 0, local_models.SPEAKER_COUNT - 1)
    speed_value = max(0.5, min(2.0, float(speed or 1.0)))
    text_value = str(text or "").strip()
    if not text_value:
        return {"ok": False, "reason": REASON_LOCAL_ERROR, "audio": b""}
    cache_enabled = bool(cfg.get("cache_enabled"))
    key = (
        local_cache.cache_key(text_value, sid, speed_value, model=model, fmt="wav")
        if cache_enabled
        else ""
    )
    base_fields = {
        "room_id": room_id,
        "message_id": message_id,
        "turn": turn,
        "profile_id": profile_id,
        "speaker": speaker,
        "engine": "local",
        "model": model,
        "voice_id": str(sid),
        "chars": len(text_value),
    }
    if cache_enabled and key:
        hit = local_cache.lookup(room_dir, key)
        if hit:
            entry = hit.get("entry") if isinstance(hit.get("entry"), dict) else {}
            events.emit(
                "cache_hit",
                **base_fields,
                cache_key=key,
                text_chars=len(text_value),
                latency_ms=0,
                rtf=0,
                audio_ms=float(entry.get("audio_ms") or 0),
                degraded=False,
                reason="",
            )
            try:
                audio = Path(hit["path"]).read_bytes()
            except OSError:
                audio = b""
            if audio:
                return {
                    "ok": True,
                    "reason": "",
                    "audio": audio,
                    "suffix": "wav",
                    "cache_hit": True,
                    "cache_key": key,
                    "latency_ms": 0.0,
                    "rtf": 0.0,
                    "audio_ms": float(entry.get("audio_ms") or 0),
                }
        events.emit("cache_miss", **base_fields, cache_key=key, degraded=False, reason="")

    concurrency = _as_int(cfg.get("local_concurrency"), 1, 1, 8)
    timeout = float(_as_int(cfg.get("local_timeout"), 30, 5, 180))
    if not _acquire(concurrency, timeout * 0.5):
        events.emit(
            "local_error", **base_fields, degraded=True, reason=REASON_LOCAL_TIMEOUT, latency_ms=0
        )
        return {"ok": False, "reason": REASON_LOCAL_TIMEOUT, "audio": b""}
    try:
        ok, start_reason = _worker_start(cfg)
        if not ok:
            events.emit(
                "local_error", **base_fields, degraded=True, reason=start_reason, latency_ms=0
            )
            return {"ok": False, "reason": start_reason, "audio": b""}
        base_url = str(_WORKER.get("base_url") or "")
        started = time.monotonic()
        events.emit("local_start", **base_fields, degraded=False, reason="")
        try:
            response = requests.post(
                f"{base_url}/synthesize",
                json={"text": text_value, "sid": sid, "speed": speed_value},
                timeout=(3, timeout),
            )
        except requests.Timeout:
            latency_ms = (time.monotonic() - started) * 1000
            events.emit(
                "local_error", **base_fields, degraded=True, reason=REASON_LOCAL_TIMEOUT, latency_ms=latency_ms
            )
            return {"ok": False, "reason": REASON_LOCAL_TIMEOUT, "audio": b""}
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - started) * 1000
            logger.warning("voice local request failed: %s", exc)
            events.emit(
                "local_error", **base_fields, degraded=True, reason=REASON_LOCAL_ERROR, latency_ms=latency_ms
            )
            return {"ok": False, "reason": REASON_LOCAL_ERROR, "audio": b""}
        if response.status_code != 200:
            latency_ms = (time.monotonic() - started) * 1000
            events.emit(
                "local_error", **base_fields, degraded=True, reason=REASON_LOCAL_ERROR, latency_ms=latency_ms
            )
            return {"ok": False, "reason": REASON_LOCAL_ERROR, "audio": b""}
        audio = response.content or b""
        if not audio:
            events.emit(
                "local_error", **base_fields, degraded=True, reason=REASON_LOCAL_ERROR, latency_ms=0
            )
            return {"ok": False, "reason": REASON_LOCAL_ERROR, "audio": b""}
        latency_ms = (time.monotonic() - started) * 1000
        try:
            rtf = float(response.headers.get("X-Rtf") or 0)
        except ValueError:
            rtf = 0.0
        try:
            audio_ms = float(response.headers.get("X-Audio-Ms") or 0)
        except ValueError:
            audio_ms = 0.0
        events.emit(
            "local_success",
            **base_fields,
            latency_ms=latency_ms,
            rtf=rtf,
            audio_ms=audio_ms,
            degraded=False,
            reason="",
        )
        if cache_enabled and key:
            stored, store_reason, _ = local_cache.store(
                room_dir,
                key,
                content=audio,
                voice_id=str(sid),
                model=model,
                suffix="wav",
                latency_ms=latency_ms,
                audio_ms=audio_ms,
                text_chars=len(text_value),
                settings=cfg,
            )
            if not stored and store_reason != "quota_exceeded":
                logger.warning("voice cache store failed reason=%s", store_reason)
        return {
            "ok": True,
            "reason": "",
            "audio": audio,
            "suffix": "wav",
            "cache_hit": False,
            "cache_key": key,
            "latency_ms": latency_ms,
            "rtf": rtf,
            "audio_ms": audio_ms,
        }
    finally:
        _release()


def service_status(settings: dict[str, Any]) -> dict[str, Any]:
    model = str(settings.get("local_model") or local_models.MODEL_ID)
    status = local_models.installed_status(model)
    health_info = health(settings, force=True)
    return {
        "enabled": bool(settings.get("local_enabled")),
        "base_url": _resolve_base_url(settings) or str(_WORKER.get("base_url") or ""),
        "managed": not bool(_resolve_base_url(settings)),
        "model": status,
        "health": health_info,
        "concurrency": _as_int(settings.get("local_concurrency"), 1, 1, 8),
        "active": active_requests(),
        "cache": {
            "enabled": bool(settings.get("cache_enabled")),
            "quota_mb": _as_int(settings.get("cache_quota_mb"), 64, 0, 1024),
            "max_entries": _as_int(settings.get("cache_max_entries"), 300, 0, 10000),
            "overflow": str(settings.get("cache_overflow") or "lru"),
        },
        "speakers": local_models.speaker_catalog(),
    }
