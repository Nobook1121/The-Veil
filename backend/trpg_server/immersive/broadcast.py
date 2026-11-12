"""沉浸系统实时广播注入。

与 ``agents/player_roll.py``、``agents/tools/combat.py`` 保持一致：模块只保存
一个可选的广播函数，由 ``socket_events.register_socket_events`` 在启动时注入；
广播失败绝不能影响叙事主流程。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

_BROADCASTER: Callable[[str, dict[str, Any], Iterable[int] | None], None] | None = None


def register_broadcaster(
    fn: Callable[[str, dict[str, Any], Iterable[int] | None], None] | None,
) -> None:
    global _BROADCASTER
    _BROADCASTER = fn


def emit(
    event: str,
    payload: dict[str, Any],
    *,
    audience: Iterable[int] | None = None,
) -> None:
    """广播事件；``audience`` 非空时只发给指定用户（用于按收件人投递的道具）。"""
    if _BROADCASTER is None:
        return
    try:
        _BROADCASTER(event, payload, audience)
    except Exception:  # noqa: BLE001 - 广播是增强能力，失败只记日志
        logger.warning("Immersive broadcast failed: %s", event, exc_info=True)
