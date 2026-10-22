import json
import logging
import socket
import time

from trpg_server.config import load_settings, update_sections
from trpg_server.config.paths import general_config_path
from trpg_server.settings import (
    DEFAULT_PORT,
    DISCOVERY_PORT,
    PORT_RETRY_COUNT,
    PORT_RETRY_INTERVAL,
)

logger = logging.getLogger(__name__)


def is_port_available(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("0.0.0.0", port))
        sock.close()
        return True
    except OSError:
        return False


def find_available_port(start_port, max_attempts=PORT_RETRY_COUNT):
    current_port = start_port
    attempts = 0
    while attempts < max_attempts:
        if is_port_available(current_port):
            return current_port
        attempts += 1
        current_port += 1
        time.sleep(PORT_RETRY_INTERVAL)
    return None


def get_local_ip():
    try:
        # 用 UDP connect 让内核选出默认出口网卡以获取本机对外 IP；UDP 无握手，不会真正发包。
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except OSError:
        logger.exception("Failed to get local IP")
        return "127.0.0.1"


def get_local_ipv4_addresses():
    addresses = ["127.0.0.1"]
    candidates = [get_local_ip()]

    try:
        hostname = socket.gethostname()
        candidates.extend(socket.gethostbyname_ex(hostname)[2])
    except OSError:
        logger.exception("Failed to enumerate local IPv4 addresses")

    for address in candidates:
        if not address:
            continue
        if address.startswith("169.254."):
            continue
        if address not in addresses:
            addresses.append(address)

    return addresses


def get_network_config(config_dir=None):
    """生效网络配置（嵌套形状，与既有 API 契约一致）。

    数据来自统一分层配置（``general.toml`` 的 ``[network]``/``[network.access_control]``，
    兼容期回落到遗留 ``network.json``），代码默认值兜底，因此键始终齐全。
    """
    settings = load_settings(config_dir)
    section = settings.section("network")
    section["access_control"] = settings.section("network.access_control")
    return section


def save_network_config(config_data, config_dir=None):
    """把提交的网络设置写入用户 ``general.toml``（只写提交的键，注释保留）。"""
    data = dict(config_data)
    access_control = data.pop("access_control", None)
    changes: dict[str, dict] = {"network": data}
    if isinstance(access_control, dict):
        changes["network.access_control"] = access_control
    update_sections(general_config_path(config_dir), changes)
    return get_network_config(config_dir)


def get_penetration_config(config_dir=None):
    """生效穿透配置（嵌套形状）；``settings`` 子表来自 ``[network.penetration.settings]``。"""
    settings = load_settings(config_dir)
    section = settings.section("network.penetration")
    section["settings"] = settings.section("network.penetration.settings")
    return section


def save_penetration_config(config_data, config_dir=None):
    data = dict(config_data)
    nested = data.pop("settings", None)
    changes: dict[str, dict] = {"network.penetration": data}
    if isinstance(nested, dict):
        changes["network.penetration.settings"] = nested
    update_sections(general_config_path(config_dir), changes)
    return get_penetration_config(config_dir)


def test_udp_discovery():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(2)
        sock.sendto(json.dumps({"test": "discovery"}).encode("utf-8"), ("<broadcast>", DISCOVERY_PORT))
        sock.close()
        return True, ""
    except OSError as exc:
        return False, str(exc)
