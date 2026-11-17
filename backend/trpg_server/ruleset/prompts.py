"""按房间规则集组装角色提示词。

核心提示词文件只保留规则集中立内容；具体主持口径（属性体系、检定方式、
专属工具）由规则集插件通过 ``tools.role_prompt(role_id)`` 提供。
"""

from __future__ import annotations

from typing import Any

from trpg_server.ruleset import builtin_registry


def role_prompt(ruleset_id: str | None, role: str = "kp") -> str:
    """读取规则集声明的角色提示词片段；规则集不可用时显式失败。"""
    plugin: Any = builtin_registry().resolve(ruleset_id)
    provider = getattr(plugin, "tools", None)
    getter = getattr(provider, "role_prompt", None)
    if not callable(getter):
        return ""
    return str(getter(str(role or "kp")) or "").strip()


def assemble_role_prompt(
    base_prompt: str | None,
    ruleset_id: str | None,
    role: str = "kp",
) -> str:
    """把基础提示词与当前规则集片段拼成最终提示词。"""
    base = str(base_prompt or "").strip()
    extra = role_prompt(ruleset_id, role)
    if not extra:
        return base
    return f"{base}\n\n{extra}" if base else extra


__all__ = ["assemble_role_prompt", "role_prompt"]
