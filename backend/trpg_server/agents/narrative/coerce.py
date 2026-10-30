"""narrative 子模块共用的类型归一化。

各模块此前各写一份 ``_as_bool`` / ``_str_list``，语义逐渐漂移；统一到这里，
新增字段只改一处，也便于测试覆盖。
"""

from __future__ import annotations

from typing import Any


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_str_list(value: Any) -> list[str]:
    """把 list/tuple/set 转成去重后的非空字符串列表；其他类型返回空列表。"""
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def as_str_list_or_scalar(value: Any) -> list[str]:
    """同 :func:`as_str_list`，但额外接受单个标量（trigger 兼容旧格式）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return as_str_list(value)
    text = str(value or "").strip()
    return [text] if text else []
