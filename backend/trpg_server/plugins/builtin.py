"""系统内置插件视图：把随包发布的规则集适配成插件清单条目。

内置规则集由 :mod:`trpg_server.ruleset.bootstrap` 以模块形式登记，不从
``data/plugins`` 扫描，也不受 ``enabled.json`` 控制。本模块只做**只读适配**，
供 ``GET /api/plugins`` 的 ``system_plugins`` 使用：

- 条目结构与第三方插件记录同构，前端复用同一张卡片；
- ``system=True`` 与 ``source="system"`` 明确区分来源，不混进第三方列表；
- 不支持启用/禁用/卸载，因此不写 ``enabled.json``，也不出现在 ``counts`` 里。
"""

from __future__ import annotations

import logging
from typing import Any

from trpg_server.plugins.contracts import (
    PLUGIN_SOURCE_SYSTEM,
    STATE_FAILED,
    STATE_LOADED,
)
from trpg_server.ruleset.registry import STATUS_AVAILABLE, STATUS_DEVELOPMENT, builtin_registry

logger = logging.getLogger(__name__)


def _to_plugin_record(entry: dict[str, Any]) -> dict[str, Any]:
    """``RulesetRegistry.describe()`` 的单项 → 插件清单条目。"""
    loaded = entry.get("state") == STATUS_AVAILABLE
    ruleset_id = str(entry.get("id") or "")
    return {
        "id": ruleset_id,
        "system": True,
        "source": PLUGIN_SOURCE_SYSTEM,
        "state": STATE_LOADED if loaded else STATE_FAILED,
        "enabled": loaded,
        "reason": str(entry.get("reason") or ""),
        "reason_code": str(entry.get("reason_code") or ""),
        "dir": "",
        "capabilities": [str(name) for name in entry.get("capabilities") or []],
        "pending_restart": False,
        "manifest": {
            "id": ruleset_id,
            "name": str(entry.get("display_name") or ruleset_id),
            "version": str(entry.get("ruleset_version") or ""),
            "description": str(entry.get("description") or ""),
            # 内置插件随包发布，没有外部署名作者；界面显示占位符。
            "author": "",
            "has_code": True,
        },
    }


def system_plugin_entries() -> list[dict[str, Any]]:
    """已注册或已判定不兼容的内置规则集；「开发中」占位不算插件。"""
    try:
        described = builtin_registry().describe()
    except Exception:  # noqa: BLE001 - 内置视图失败不能拖垮第三方插件清单
        logger.warning("system_plugin_view_failed", exc_info=True)
        return []
    return [
        _to_plugin_record(entry)
        for entry in described
        if entry.get("state") != STATUS_DEVELOPMENT
    ]


__all__ = ["system_plugin_entries"]
