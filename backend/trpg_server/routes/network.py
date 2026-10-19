import logging
import socket
import time

from flask import Blueprint, current_app, request, session

from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.network_discovery import (
    DEFAULT_PORT,
    get_local_ip,
    get_local_ipv4_addresses,
    get_network_config,
    get_penetration_config,
    save_network_config,
    save_penetration_config,
    test_udp_discovery,
)
from trpg_server.security import require_permission_node
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.settings import NETWORK_CONFIG_FILE, PENETRATION_CONFIG_FILE

bp = Blueprint("network", __name__)
logger = logging.getLogger(__name__)


def _network_config_file():
    return current_app.config.get("NETWORK_CONFIG_FILE", NETWORK_CONFIG_FILE)


def _penetration_config_file():
    return current_app.config.get("PENETRATION_CONFIG_FILE", PENETRATION_CONFIG_FILE)


@bp.route("/api/network/config", methods=["GET"])
def get_network_config_api():
    try:
        config = get_network_config(_network_config_file())
        return success_response(config, "Network config loaded successfully")
    except Exception as exc:
        logger.exception("Failed to get network config")
        return server_error("Failed to get network config")


@bp.route("/api/network/config", methods=["POST"])
@require_permission_node("settings.network")
def update_network_config_api():
    try:
        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Please provide network config data", 400, "No data")

        validation_error = _validate_network_config(config_data)
        if validation_error:
            return validation_error

        saved_config = save_network_config(config_data, _network_config_file())
        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了网络设置"),
            用户ID=session.get("user_id"),
            端口=saved_config.get("port"),
            局域网发现=saved_config.get("discovery_enabled"),
        )
        return success_response(config_data, "Network config updated successfully")
    except Exception as exc:
        logger.exception("Failed to update network config")
        return server_error("Failed to update network config")


@bp.route("/api/network/addresses", methods=["GET"])
def get_share_addresses():
    """返回可用于生成房间邀请链接的候选地址（本机多网卡 IPv4 探测）。

    当主机拥有多块网卡（有线/无线/虚拟网卡）时会有多个可用地址，管理员需要在
    设置页里挑一个作为对外地址。此接口只暴露本机 IP 与当前配置端口，不含凭据，
    因此沿用与 ``/api/network/config`` GET 一致的开放读取策略。
    """
    try:
        config = get_network_config(_network_config_file())
        port = int(config.get("port", DEFAULT_PORT))
        addresses = [f"http://{ip}:{port}" for ip in get_local_ipv4_addresses()]
        return success_response(
            {
                "addresses": addresses,
                "share_base_url": config.get("share_base_url", ""),
                "port": port,
            },
            "Share addresses loaded successfully",
        )
    except Exception as exc:
        logger.exception("Failed to get share addresses")
        return server_error("Failed to get share addresses")


@bp.route("/api/network/status", methods=["GET"])
def get_network_status():
    try:
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            local_ip = "127.0.0.1"

        config = get_network_config(_network_config_file())
        port = config.get("port", DEFAULT_PORT)
        port_available = _can_bind(local_ip, port)
        status = {
            "local_ip": local_ip,
            "port": port,
            "port_available": port_available,
            "discovery_enabled": config.get("discovery_enabled", True),
            "access_control": config.get(
                "access_control",
                {"enabled": False, "allowed_ips": []},
            ),
            "timestamp": int(time.time()),
        }
        return success_response(status, "Network status loaded successfully")
    except Exception as exc:
        logger.exception("Failed to get network status")
        return server_error("Failed to get network status")


@bp.route("/api/network/test", methods=["POST"])
def test_network_connection():
    try:
        config = get_network_config(_network_config_file())
        local_ip = get_local_ip()
        target_port = config.get("port", DEFAULT_PORT)
        connection_success, error_message = _test_local_bind(local_ip)
        discovery_success, discovery_error = test_udp_discovery()

        return success_response(
            {
                "local_ip": local_ip,
                "port": target_port,
                "connection_success": connection_success,
                "error_message": error_message,
                "discovery_success": discovery_success,
                "discovery_error": discovery_error,
            },
            "Network connection test completed",
        )
    except Exception as exc:
        logger.exception("Failed to test network connection")
        return server_error("Failed to test network connection")


@bp.route("/api/network/penetration/config", methods=["GET"])
@require_permission_node("settings.network")
def get_penetration_config_api():
    try:
        config = get_penetration_config(_penetration_config_file())
        return success_response(config, "Penetration config loaded successfully")
    except Exception as exc:
        logger.exception("Failed to get penetration config")
        return server_error("Failed to get penetration config")


@bp.route("/api/network/penetration/config", methods=["POST"])
@require_permission_node("settings.network")
def update_penetration_config_api():
    try:
        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Please provide penetration config data", 400, "No data")

        save_penetration_config(config_data, _penetration_config_file())
        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了穿透设置"),
            用户ID=session.get("user_id"),
            类型=config_data.get("type"),
            启用=config_data.get("enabled"),
        )
        return success_response(config_data, "Penetration config updated successfully")
    except Exception as exc:
        logger.exception("Failed to update penetration config")
        return server_error("Failed to update penetration config")


@bp.route("/api/network/penetration/status", methods=["GET"])
def get_penetration_status():
    try:
        config = get_penetration_config(_penetration_config_file())
        status = {
            "enabled": config.get("enabled", False),
            "type": config.get("type", "ngrok"),
            "settings": config.get("settings", {}),
            "port_mappings": config.get("port_mappings", []),
            "timestamp": int(time.time()),
        }
        return success_response(status, "Penetration status loaded successfully")
    except Exception as exc:
        logger.exception("Failed to get penetration status")
        return server_error("Failed to get penetration status")


def _validate_network_config(config_data):
    port = config_data.get("port")
    if port:
        try:
            port = int(port)
            if port < 1 or port > 65535:
                return error_response("Port must be between 1 and 65535", 400, "Invalid port")
        except (TypeError, ValueError):
            return error_response("Port must be an integer", 400, "Invalid port")

    # 分享地址（邀请链接/二维码的对外基础地址）：允许留空表示跟随当前访问地址。
    share_base_url = config_data.get("share_base_url")
    if share_base_url not in (None, ""):
        share_text = str(share_base_url).strip()
        if not share_text.startswith(("http://", "https://")):
            return error_response(
                "share_base_url must start with http:// or https://",
                400,
                "Invalid share_base_url",
            )

    access_control = config_data.get("access_control", {})
    if access_control and isinstance(access_control, dict):
        allowed_ips = access_control.get("allowed_ips", [])
        if not isinstance(allowed_ips, list):
            return error_response("allowed_ips must be a list", 400, "Invalid config")

    return None


def _can_bind(local_ip, port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((local_ip, port))
        sock.close()
        return True
    except OSError:
        return False


def _test_local_bind(local_ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.bind((local_ip, 0))
        sock.close()
        return True, ""
    except socket.timeout:
        return False, "Connection timeout"
    except socket.gaierror:
        return False, "DNS resolution failed"
    except OSError as exc:
        return False, f"Network error: {exc}"
