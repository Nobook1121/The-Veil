"""道具工坊（设计稿 ``docs/plans/prop-workshop-design.md`` §7）。

模块划分：

- :mod:`trpg_server.props.registry` 模板扫描与字段校验
- :mod:`trpg_server.props.store` 房间级 Props 存储（index + 单条 spec）
- :mod:`trpg_server.props.policy` 可见性与 ``apply_redaction``（AI / 玩家共用）
- :mod:`trpg_server.props.render` PyMuPDF Story 渲染与缓存

这里只放配置读取与少量再导出，刻意不在包初始化时 import pymupdf：
控制台与测试只需要 ``props_settings`` 时不应该承担渲染依赖的启动成本。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_props_per_room": 200,
    "max_field_chars": 4000,
    "image_daily_budget": 20,
    "render_cache_days": 7,
    "ai_draft_enabled": True,
    "auto_prepare_on_import": False,
}


def props_settings(config_dir: str | Path | None = None) -> dict[str, Any]:
    """读取生效配置的 ``[props]`` 段；缺键回落设计稿默认值。"""
    from trpg_server.config import load_settings

    section: dict[str, Any] = {}
    try:
        loaded = load_settings(config_dir).section("props")
        if isinstance(loaded, dict):
            section = dict(loaded)
    except (KeyError, AttributeError):  # pragma: no cover - 老配置目录没有该段
        section = {}
    return {**DEFAULTS, **section}


def __getattr__(name: str):
    """惰性再导出，避免 ``import trpg_server.props`` 时加载渲染栈。"""
    lazy = {
        "RenderError": (".render", "RenderError"),
        "render_prop": (".render", "render_prop"),
        "render_cached": (".render", "render_cached"),
        "apply_redaction": (".policy", "apply_redaction"),
    }
    if name in lazy:
        module_name, attribute = lazy[name]
        from importlib import import_module

        return getattr(import_module(module_name, __name__), attribute)
    raise AttributeError(name)


__all__ = ["DEFAULTS", "props_settings"]
