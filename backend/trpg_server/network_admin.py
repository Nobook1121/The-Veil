"""网络状态与连通性测试（控制台专用）。

复用 :mod:`trpg_server.network_discovery` 的配置读取，不依赖 Flask 请求上下文。
"""
from __future__ import annotations

import logging
import socket
from typing import Any

from trpg_server.network_discovery import (
    DEFAULT_PORT,
    get_local_ip,
    get_network_config,
    get_penetration_config,
    test_udp_discovery,
)

logger = logging.getLogger(__name__)


def _can_bind(ip: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((ip, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def network_status(config_dir: Any = None) -> dict[str, Any]:
    """返回本地 IP、端口占用、局域网发现与穿透开关。"""

    config = get_network_config(config_dir)
    penetration = get_penetration_config(config_dir)
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except socket.gaierror:
        local_ip = "127.0.0.1"
    port = int(config.get("port", DEFAULT_PORT))
    return {
        "local_ip": local_ip,
        "port": port,
        "port_available": _can_bind(local_ip, port),
        "discovery_enabled": bool(config.get("discovery_enabled", True)),
        "penetration_enabled": bool(penetration.get("enabled", False)),
        "penetration_type": penetration.get("type", "ngrok"),
    }


def network_test(config_dir: Any = None) -> dict[str, Any]:
    """本地端口绑定测试 + UDP 发现测试，用于判断网络是否可用。"""

    config = get_network_config(config_dir)
    local_ip = get_local_ip()
    bind_ok = _can_bind(local_ip, 0)
    try:
        discovery_ok, discovery_error = test_udp_discovery()
    except Exception as exc:  # noqa: BLE001
        logger.exception("UDP discovery test failed")
        discovery_ok, discovery_error = False, str(exc)
    return {
        "local_ip": local_ip,
        "port": int(config.get("port", DEFAULT_PORT)),
        "connection_success": bind_ok,
        "discovery_success": discovery_ok,
        "discovery_error": discovery_error or "",
    }