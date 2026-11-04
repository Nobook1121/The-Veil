"""本地 TTS 子服务（独立进程，仅监听 127.0.0.1）。

主进程（``voice/local_service.py``）以子进程方式启动本模块，通过回环 HTTP 调用：
- ``GET  /health``     健康检查；
- ``GET  /voices``     列出音色；
- ``POST /synthesize`` 返回 WAV（16-bit PCM mono）；
- ``POST /shutdown``   主动退出（主进程重启或关闭时使用）。

本模块只依赖 ``sherpa-onnx`` + 标准库，不导入 Flask / 项目配置，避免与主进程耦合。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 240
MAX_BODY_BYTES = 8 * 1024
LOGGER = logging.getLogger("voice.local_worker")


class LocalTtsEngine:
    """封装 sherpa-onnx 离线 TTS，进程内串行生成（底层不支持并发调用）。"""

    def __init__(self, model_dir: Path, num_threads: int = 2) -> None:
        import sherpa_onnx  # 延迟导入，便于 /health 在缺依赖时仍可返回结构化错误

        self.model_dir = Path(model_dir)
        self._lock = threading.Lock()
        lexicon = ",".join(
            str(self.model_dir / name)
            for name in ("lexicon-us-en.txt", "lexicon-zh.txt")
            if (self.model_dir / name).is_file()
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(self.model_dir / "model.int8.onnx"),
                    voices=str(self.model_dir / "voices.bin"),
                    tokens=str(self.model_dir / "tokens.txt"),
                    lexicon=lexicon,
                    data_dir=str(self.model_dir / "espeak-ng-data"),
                ),
                num_threads=max(1, int(num_threads)),
                provider="cpu",
                debug=False,
            ),
            max_num_sentences=1,
            silence_scale=0.2,
        )
        if not config.validate():
            raise ValueError("sherpa-onnx 模型配置校验失败")
        started = time.perf_counter()
        self.tts = sherpa_onnx.OfflineTts(config)
        self.load_ms = (time.perf_counter() - started) * 1000
        self.sample_rate = int(getattr(self.tts, "sample_rate", 24000) or 24000)
        self.num_speakers = int(getattr(self.tts, "num_speakers", 0) or 0)
        LOGGER.info(
            "voice.local_worker loaded model_dir=%s sample_rate=%s speakers=%s load_ms=%.0f",
            self.model_dir,
            self.sample_rate,
            self.num_speakers,
            self.load_ms,
        )

    def synthesize(self, text: str, sid: int, speed: float) -> tuple[bytes, dict[str, Any]]:
        started = time.perf_counter()
        with self._lock:
            audio = self.tts.generate(text, sid=int(sid), speed=float(speed))
        samples = audio.samples
        audio_ms = 0.0
        if len(samples) and self.sample_rate > 0:
            audio_ms = len(samples) / self.sample_rate * 1000.0
        wav = _wav_bytes(samples, self.sample_rate)
        elapsed_ms = (time.perf_counter() - started) * 1000
        rtf = (elapsed_ms / audio_ms) if audio_ms > 0 else 0.0
        return wav, {
            "latency_ms": round(elapsed_ms, 1),
            "audio_ms": round(audio_ms, 1),
            "rtf": round(rtf, 3),
            "sample_rate": self.sample_rate,
            "sid": int(sid),
            "chars": len(text),
        }


def _wav_bytes(samples: Any, sample_rate: int) -> bytes:
    try:
        import numpy as np

        pcm = np.clip(np.asarray(samples, dtype=np.float32) * 32767.0, -32768, 32767)
        payload = pcm.astype("<i2").tobytes()
    except Exception:  # noqa: BLE001 - numpy 缺失时退回纯 Python 打包
        import array
        import math

        clipped = array.array("h")
        for value in samples:
            scaled = math.floor(float(value) * 32767.0)
            bounded = max(-32768, min(32767, scaled))
            clipped.append(int(bounded))
        payload = clipped.tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(payload)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "TrpgVoiceLocal/1.0"
    protocol_version = "HTTP/1.1"
    engine: LocalTtsEngine | None = None
    started_at = time.time()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOGGER.debug("voice.local_worker http %s", fmt % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/health", "/"}:
            if self.engine is None:
                self._json(503, {"ok": False, "reason": "model_not_loaded"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "model": "kokoro-int8-multi-lang-v1_1",
                    "sample_rate": self.engine.sample_rate,
                    "speakers": self.engine.num_speakers,
                    "pid": self.server.server_address[1],
                    "uptime_s": round(time.time() - self.started_at, 1),
                },
            )
            return
        if self.path.split("?", 1)[0] == "/voices":
            self._json(
                200,
                {
                    "ok": True,
                    "count": 103,
                    "gender": {"female": [3, 57], "male": [58, 102]},
                },
            )
            return
        self._json(404, {"ok": False, "reason": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path != "/synthesize":
            self._json(404, {"ok": False, "reason": "not_found"})
            return
        if self.engine is None:
            self._json(503, {"ok": False, "reason": "model_not_loaded"})
            return
        payload = self._read_json()
        if not payload:
            self._json(400, {"ok": False, "reason": "invalid_json"})
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            self._json(400, {"ok": False, "reason": "empty_text"})
            return
        if len(text) > MAX_TEXT_CHARS:
            self._json(413, {"ok": False, "reason": "text_too_long"})
            return
        try:
            sid = int(payload.get("sid") or 0)
            speed = float(payload.get("speed") or 1.0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "reason": "invalid_params"})
            return
        sid = max(0, min(102, sid))
        speed = max(0.5, min(2.0, speed))
        try:
            audio, meta = self.engine.synthesize(text, sid, speed)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("voice.local_worker synthesize failed")
            self._json(500, {"ok": False, "reason": "synthesis_failed", "error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        for header, key in (
            ("X-Audio-Ms", "audio_ms"),
            ("X-Latency-Ms", "latency_ms"),
            ("X-Rtf", "rtf"),
            ("X-Sample-Rate", "sample_rate"),
            ("X-Voice-Id", "sid"),
        ):
            self.send_header(header, str(meta.get(key, "")))
        self.end_headers()
        self.wfile.write(audio)
        LOGGER.info(
            "voice.local_worker synth chars=%s sid=%s speed=%.2f latency_ms=%s rtf=%s",
            meta.get("chars"),
            sid,
            speed,
            meta.get("latency_ms"),
            meta.get("rtf"),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TRPG local TTS worker")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        engine = LocalTtsEngine(Path(args.model_dir), args.threads)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "error", "error": str(exc)}), flush=True)
        LOGGER.exception("voice.local_worker init failed")
        return 1
    Handler.engine = engine
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    print(
        json.dumps(
            {
                "event": "ready",
                "port": port,
                "sample_rate": engine.sample_rate,
                "load_ms": round(engine.load_ms, 1),
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
