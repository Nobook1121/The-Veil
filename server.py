import logging
import os
import subprocess
import time
from pathlib import Path
import socket
import sys
import shutil
import threading
import secrets
import urllib.error
import urllib.request

# Boot timestamp. Recorded before the heavy dependencies are imported so the
# time spent importing Flask/Socket.IO and the embedding model is included.
_BOOT_START = time.perf_counter()

from flask import abort, request
from werkzeug.serving import make_server

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Configure logging and print the startup marker before importing app_factory:
# create_app() loads the embedding model, which takes seconds, so emitting a
# line first lets the user see output immediately instead of waiting in silence.
from trpg_server.logging_config import configure_logging
from trpg_server.settings import CONFIG_DIR, DEFAULT_PORT, LOGS_DIR

configure_logging(LOGS_DIR, CONFIG_DIR)
logger = logging.getLogger(__name__)
logger.info("Server starting")

from trpg_server.app_factory import create_app, socketio
from trpg_server.console_commands import set_shutdown_callback, start_console_command_loop
from trpg_server.network_discovery import (
    find_available_port,
    get_local_ip,
    get_local_ipv4_addresses,
    get_network_config,
    is_port_available,
)

app = create_app()

# Werkzeug 2.3 removed ``werkzeug.server.shutdown`` from the WSGI environ, so
# Flask-SocketIO's ``socketio.stop()`` can no longer stop the dev server.  We
# instead keep a handle to the WSGI server and expose a loopback-only endpoint
# that the console command calls to trigger a graceful stop.
_SHUTDOWN_TOKEN = secrets.token_urlsafe(32)
_shutdown_requested = threading.Event()
_http_server = None
_http_server_lock = threading.Lock()


@app.post("/__internal/shutdown")
def _internal_shutdown():
    if request.remote_addr not in {"127.0.0.1", "::1", "localhost"}:
        abort(404)
    if request.headers.get("X-TRPG-Shutdown-Token") != _SHUTDOWN_TOKEN:
        abort(403)
    if _shutdown_requested.is_set():
        return {"ok": True, "message": "Shutdown already requested"}

    _shutdown_requested.set()
    logger.info("Graceful shutdown accepted from local console")
    # ``BaseServer.shutdown()`` must not be called from the request thread that
    # is currently serving, so hand it off to a short-lived helper thread.
    with _http_server_lock:
        http_server = _http_server
    if http_server is not None:
        threading.Thread(target=http_server.shutdown, name="trpg-server-shutdown", daemon=True).start()
    else:
        logger.warning("Shutdown requested before the HTTP server was ready")
    return {"ok": True}


def _serve_http(port: int) -> None:
    """Run the Socket.IO-wrapped Flask app with a stoppable WSGI server."""

    global _http_server
    http_server = make_server("0.0.0.0", port, app, threaded=True)
    with _http_server_lock:
        _http_server = http_server
    logger.info("Startup complete, elapsed %.1f ms", (time.perf_counter() - _BOOT_START) * 1000)
    try:
        http_server.serve_forever()
    finally:
        http_server.server_close()
        with _http_server_lock:
            if _http_server is http_server:
                _http_server = None


def _request_graceful_shutdown(port: int) -> None:
    """Ask the running server to stop through its loopback-only endpoint."""

    url = f"http://127.0.0.1:{port}/__internal/shutdown"
    request_obj = urllib.request.Request(
        url,
        method="POST",
        headers={"X-TRPG-Shutdown-Token": _SHUTDOWN_TOKEN},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=5) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Unable to contact shutdown endpoint: %s", exc)


def _iter_frontend_source_paths(root: Path):
    # frontend/src/app/generated is a build artifact (build-frontend.mjs rewrites
    # templates.ts on every build). It must not count as a source file: it is
    # written after dist/public/index.html, so its mtime is always newer than the
    # output and every start would be flagged as stale, triggering a rebuild loop.
    generated_dirs = (root / "frontend" / "src" / "app" / "generated",)
    candidates = (
        root / "frontend" / "src",
        root / "frontend" / "src" / "templates",
        root / "frontend" / "src" / "index",
        root / "scripts" / "build-frontend.mjs",
        root / "scripts" / "relocate-tools.mjs",
        root / "package.json",
        root / "tsconfig.frontend.json",
        root / "tsconfig.react.json",
    )
    for path in candidates:
        if path.is_dir():
            for child in path.rglob("*"):
                if not child.is_file():
                    continue
                if any(parent in generated_dirs for parent in child.parents):
                    continue
                yield child
        elif path.is_file():
            yield path


def _frontend_build_is_stale(root: Path) -> bool:
    outputs = [
        root / "dist" / "public" / "index.html",
        root / "dist" / "public" / "js" / "react" / "main.js",
        root / "dist" / "public" / "js" / "react" / "main.css",
    ]
    if any(not path.exists() for path in outputs):
        return True

    latest_source_mtime = max(path.stat().st_mtime for path in _iter_frontend_source_paths(root))
    oldest_output_mtime = min(path.stat().st_mtime for path in outputs)
    return latest_source_mtime > oldest_output_mtime


def ensure_frontend_build():
    """开发侧可选：源码比构建产物新时自动重建前端。

    仅当设置 ``AI_TRPG_DEV_BUILD=1`` 时启用。发布包默认关闭该开关，直接使用随包
    附带的 ``dist/`` 构建产物，避免“每次改动代码就自动构建”，也避免在没有 npm
    的部署环境里启动失败。
    """
    if os.environ.get("AI_TRPG_DEV_BUILD", "").strip().lower() not in {"1", "true", "yes"}:
        return
    root = Path(__file__).resolve().parent
    if not _frontend_build_is_stale(root):
        return
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise FileNotFoundError("npm is not available on PATH")
    subprocess.run([npm, "run", "build:frontend"], cwd=root, check=True)


def _log_listening_addresses(local_ip, port):
    for address in get_local_ipv4_addresses():
        logger.info("listening on http://%s:%s", address, port)


def _select_port(argv):
    config = get_network_config()
    port = config.get("port", DEFAULT_PORT)

    if len(argv) > 1:
        try:
            port = int(argv[1])
            logger.debug("Using command line port: %s", port)
        except ValueError:
            logger.warning("Invalid command line port; using configured port")

    if is_port_available(port):
        return port

    logger.warning("Port %s is in use; searching for an available port", port)
    available_port = find_available_port(port)
    if available_port:
        logger.debug("Found available port: %s", available_port)
        return available_port

    logger.error("No available port found; falling back to default port %s", DEFAULT_PORT)
    return DEFAULT_PORT


def run_server(argv=None):
    argv = argv or sys.argv
    _shutdown_requested.clear()
    port = _select_port(argv)
    local_ip = get_local_ip()
    ensure_frontend_build()
    logger.debug("Starting server on port %s", port)
    logger.debug("LAN URL: http://%s:%s", local_ip, port)
    _log_listening_addresses(local_ip, port)
    # Register the callback after the final port is chosen so the console
    # command always targets the actual listening socket.
    set_shutdown_callback(lambda: _request_graceful_shutdown(port))
    # 控制台必须复用 Web 应用正在使用的 user_manager 实例，否则控制台
    # 对用户组的修改可能落到另一份数据源上而“不生效”。剧本命令同理需要
    # 复用同一份 scenarios/rooms 路径与向量库实例。
    start_console_command_loop(
        user_manager=app.config.get("USER_MANAGER"),
        scenarios_dir=app.config.get("SCENARIOS_DIR"),
        rooms_dir=app.config.get("ROOMS_DIR"),
        vector_store=app.extensions.get("vector_store"),
        embedding_provider=app.extensions.get("embedding_provider"),
        ocr_provider=app.extensions.get("ocr_provider"),
        app=app,
        log_dir=app.config.get("LOGS_DIR"),
        ai_platform_dir=app.config.get("AI_PLATFORM_DIR"),
        ai_platform_secret_dir=app.config.get("AI_PLATFORM_SECRET_DIR"),
    )

    try:
        _serve_http(port)
    except socket.error:
        logger.exception("Server failed to start on port %s", port)
        available_port = find_available_port(port)
        if not available_port:
            raise

        logger.debug("Retrying server on port %s", available_port)
        _log_listening_addresses(local_ip, available_port)
        set_shutdown_callback(lambda: _request_graceful_shutdown(available_port))
        _serve_http(available_port)


if __name__ == "__main__":
    run_server()
