from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic


def _platform_config_copy(config: dict[str, Any] | None) -> dict[str, Any]:
    return deepcopy(config) if isinstance(config, dict) else {}


def platform_load_order(config: dict[str, Any] | None) -> float | None:
    """读取平台的显式加载顺序（``order``）；缺失或非法时返回 ``None``。"""
    if not isinstance(config, dict):
        return None
    order = config.get("order")
    if isinstance(order, bool) or not isinstance(order, (int, float)):
        return None
    return float(order)


def ordered_platform_paths(platform_dir: Path) -> list[Path]:
    """返回平台配置文件列表，按显式 ``order`` 升序；无 ``order`` 的排在其后并按文件名。

    ``order`` 由管理员在前端拖动排序后写入各平台配置，决定「已启用平台」的加载顺序：
    先加载 order 最小的启用平台，请求失败时再按序回退到下一个。
    ``default-request.json`` 是模型请求模板而非平台配置，始终排除。
    """
    if not platform_dir.exists():
        return []

    paths = [path for path in platform_dir.glob("*.json") if path.name != "default-request.json"]

    def _sort_key(path: Path):
        order = platform_load_order(read_json(path, default={}))
        return (0, order, path.name) if order is not None else (1, 0.0, path.name)

    return sorted(paths, key=_sort_key)


def split_platform_config(config: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    public_config = _platform_config_copy(config)
    config_section = public_config.get("config")
    if not isinstance(config_section, dict):
        return public_config, None

    api_key = config_section.pop("api_key", None)
    if api_key in (None, ""):
        return public_config, None

    return public_config, {"api_key": str(api_key)}


def merge_platform_config(
    public_config: dict[str, Any] | None,
    secret_config: dict[str, Any] | None,
) -> dict[str, Any]:
    merged_config = _platform_config_copy(public_config)
    if not isinstance(secret_config, dict):
        return merged_config

    api_key = secret_config.get("api_key")
    if api_key in (None, ""):
        return merged_config

    config_section = merged_config.setdefault("config", {})
    if isinstance(config_section, dict):
        config_section["api_key"] = str(api_key)
    return merged_config


def load_public_platform_config(public_path: Path, secret_path: Path | None = None) -> dict[str, Any]:
    raw_config = read_json(public_path, default={})
    public_config, secret_config = split_platform_config(raw_config)

    if secret_path and secret_config:
        write_json_atomic(secret_path, secret_config)
        write_json_atomic(public_path, public_config)
    elif raw_config != public_config:
        write_json_atomic(public_path, public_config)

    return public_config


def load_platform_config(public_path: Path, secret_path: Path | None = None) -> dict[str, Any]:
    public_config = load_public_platform_config(public_path, secret_path)
    secret_config = read_json(secret_path, default={}) if secret_path and secret_path.exists() else {}
    return merge_platform_config(public_config, secret_config)


def save_platform_config(
    public_path: Path,
    secret_path: Path | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    public_config, secret_config = split_platform_config(config)
    write_json_atomic(public_path, public_config)
    if secret_path:
        existing_secret = read_json(secret_path, default={}) if secret_path.exists() else {}
        if secret_config is None and isinstance(existing_secret, dict):
            existing_api_key = existing_secret.get("api_key")
            if existing_api_key not in (None, ""):
                secret_config = {"api_key": str(existing_api_key)}
        if secret_config is not None:
            write_json_atomic(secret_path, secret_config)
    return public_config
