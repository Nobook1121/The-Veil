"""配置字段规格与类型转换器。

每个配置键由一条 ``FieldSpec`` 声明：所属 section、可用键名（含历史别名）、转换器。
转换器同时接受 TOML 原生类型（bool/int）与字符串（历史文件、环境变量），
非法值一律回退默认值，保证任意一层配置损坏都不会让启动失败。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def as_bool(raw: Any, default: bool) -> bool:
    """TOML bool 直取；字符串沿用历史语义：只有字面量 ``true``（忽略大小写）为真。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return default


def as_text(raw: Any, default: str) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if raw is None:
        return default
    return str(raw).strip()


def clamped_int(*, minimum: int, maximum: int | None = None) -> Callable[[Any, int], int]:
    """生成整数转换器：非法值回退默认值，再收敛到 ``[minimum, maximum]``。"""

    def convert(raw: Any, default: int) -> int:
        value: int
        if isinstance(raw, bool):
            value = default
        elif isinstance(raw, int):
            value = raw
        else:
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                value = default
        if value < minimum:
            return minimum
        if maximum is not None and value > maximum:
            return maximum
        return value

    return convert


def as_enum(field_name: str, allowed: frozenset[str]) -> Callable[[Any, str], str]:
    """生成枚举转换器：非法值告警并回退默认值。"""

    def convert(raw: Any, default: str) -> str:
        candidate = str(raw).strip().lower()
        if candidate in allowed:
            return candidate
        logger.warning("Invalid %s %r in general.toml; falling back to %s", field_name, raw, default)
        return default

    return convert


def as_str_list(raw: Any, default: list) -> list:
    """字符串数组（如 IP 白名单）：TOML 数组直取；字符串按 JSON 数组或逗号分隔解析。"""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return list(default)
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return list(default)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            return list(default)
        return [part.strip() for part in text.split(",") if part.strip()]
    return list(default)


def as_list(raw: Any, default: list) -> list:
    """任意数组整体替换（如端口映射）：只接受 TOML 数组或 JSON 数组字符串。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw.strip())
        except ValueError:
            return list(default)
        if isinstance(parsed, list):
            return parsed
    return list(default)


@dataclass(frozen=True)
class FieldSpec:
    """一条配置项接线：``general.toml`` 的 section/键 → 规范字段名 + 默认值 + 转换方式。"""

    name: str
    section: str
    keys: tuple[str, ...]
    convert: Callable[[Any, Any], Any]
    default: Any
