"""各配置层的读取与扁平化。

TOML 读取统一走 ``tomlkit``（往返保留注释，写入侧复用同一解析器），读出后
``unwrap`` 为普通 dict 并把嵌套表扁平化为点号 section（``[ai.prompt]`` → ``"ai.prompt"``），
与前端 ``{section: {key: value}}`` 的对象形状一致。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

logger = logging.getLogger(__name__)

# 环境变量命名空间：AI_TRPG_CFG_<SECTION>_<KEY>（section 点号转下划线，全大写）。
ENV_PREFIX = "AI_TRPG_CFG_"

# 兼容期遗留文件：(文件名, 映射到的 base section)。嵌套 dict 按 TOML 相同语义压平
# （network.json 的 access_control → network.access_control；penetration.json 的
# settings → network.penetration.settings）。迁移完成后本表随兼容代码一起删除。
LEGACY_JSON_FILES: tuple[tuple[str, str], ...] = (
    ("site.json", "site"),
    ("network.json", "network"),
    ("penetration.json", "network.penetration"),
)


def flatten_sections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把嵌套表压平成点号 section。叶子键的值若仍是 dict（内联表），按 JSON 不支持处理：

    当前 general.toml 没有内联表值，嵌套 dict 一律视为子 section。
    """
    flat: dict[str, dict[str, Any]] = {}

    def visit(section: str, table: dict[str, Any]) -> None:
        target = flat.setdefault(section, {})
        for key, value in table.items():
            if isinstance(value, dict):
                visit(f"{section}.{key}" if section else key, value)
            else:
                target[key] = value

    for key, value in data.items():
        if isinstance(value, dict):
            visit(key, value)
        else:
            flat.setdefault("", {})[key] = value
    return flat


def read_toml_file(path: Path) -> dict[str, dict[str, Any]]:
    """读取 TOML 文件为扁平 section dict；文件缺失或损坏时返回空并记录错误。"""
    if not path.exists():
        return {}
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, TOMLKitError) as exc:
        logger.error("Failed to parse %s: %s; ignoring this layer", path, exc)
        return {}
    return flatten_sections(doc.unwrap())


def legacy_json_stamps(config_dir: Path) -> tuple:
    """遗留 JSON 文件的 (文件名, mtime_ns, size) 指纹，用于加载缓存键。"""
    stamps: list[tuple[str, int, int]] = []
    for filename, _base_section in LEGACY_JSON_FILES:
        try:
            stat = (config_dir / filename).stat()
            stamps.append((filename, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append((filename, 0, 0))
    return tuple(stamps)


def read_legacy_json_layer(config_dir: Path) -> dict[str, dict[str, Any]]:
    """兼容期：把遗留 site/network/penetration JSON 读成扁平 section 层。

    该层合并优先级低于用户 ``general.toml``，因此 TOML 里已有的键天然胜出，
    只有 TOML 缺失的键才回落到遗留 JSON 的值。文件损坏只跳过该文件并告警。
    """
    layer: dict[str, dict[str, Any]] = {}
    for filename, base_section in LEGACY_JSON_FILES:
        path = config_dir / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("Failed to parse legacy %s: %s; ignoring this file", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        for section, values in flatten_sections({base_section: data}).items():
            layer.setdefault(section, {}).update(values)
    return layer


def env_key_name(section: str, key: str) -> str:
    """派生环境变量名：``AI_TRPG_CFG_`` + section 去点转下划线 + ``_`` + key，全大写。"""
    return f"{ENV_PREFIX}{section.replace('.', '_')}_{key}".upper()


def collect_env_layer(
    env: dict[str, str],
    known_env_names: dict[str, tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    """从环境变量收集覆盖层。

    ``known_env_names`` 由 schema 派生（env 名 → (section, key)）。返回
    ``(layer, locked, warnings)``：layer 为扁平 section dict（值为字符串，由转换器
    统一解析），locked 记录 ``section.key → "env"``，warnings 为未知变量告警。
    """
    layer: dict[str, dict[str, Any]] = {}
    locked: dict[str, str] = {}
    warnings: list[str] = []
    for name, value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        target = known_env_names.get(name)
        if target is None:
            warnings.append(name)
            continue
        section, key = target
        layer.setdefault(section, {})[key] = value
        locked[f"{section}.{key}"] = "env"
    return layer, locked, warnings
