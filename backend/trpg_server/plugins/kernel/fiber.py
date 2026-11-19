"""插件 fiber：三态生命周期状态机（WAITING / ACTIVE / FAILED）。

Cordis 的 Fiber 在 The Veil 只保留三个状态（计划 §4 阶段 1 明确裁掉
PENDING / LOADING / UNLOADING / DISPOSED 全套）：

- ``WAITING``：依赖未就绪，``waiting_for`` 记录在等的 service key；
- ``ACTIVE``：激活成功；
- ``FAILED``：终态，``reason`` 记录失败原因，不自动重试。

供后续阶段挂进 ``PluginRecord``、透出到 ``/api/plugins`` 摘要——
「静默 WAITING 是头号调试疑难」，waiting_for 必须可观测。

流转表（非法组合抛 :class:`InvalidFiberTransition`）：

    WAITING → ACTIVE / WAITING / FAILED
    ACTIVE  → WAITING / FAILED
    FAILED  → 终态，任何流转都被拒绝

并发前提：启停是单线程管理路径，本模块不加锁。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

FIBER_WAITING = "waiting"
FIBER_ACTIVE = "active"
FIBER_FAILED = "failed"

_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        (FIBER_WAITING, FIBER_ACTIVE),
        (FIBER_WAITING, FIBER_WAITING),
        (FIBER_WAITING, FIBER_FAILED),
        (FIBER_ACTIVE, FIBER_WAITING),
        (FIBER_ACTIVE, FIBER_FAILED),
    }
)


class InvalidFiberTransition(ValueError):
    """fiber 收到了流转表不允许的状态变更请求。"""


class Fiber:
    """一个插件（或一条 inject 声明）的可观测生命周期。"""

    __slots__ = ("_plugin_id", "_state", "_waiting_for", "_reason")

    def __init__(self, plugin_id: str = "") -> None:
        self._plugin_id = str(plugin_id or "")
        self._state = FIBER_WAITING
        self._waiting_for: tuple[str, ...] = ()
        self._reason = ""

    # -- 只读视图 ---------------------------------------------------------
    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def waiting_for(self) -> list[str]:
        return list(self._waiting_for)

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def active(self) -> bool:
        return self._state == FIBER_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        """可序列化视图（阶段 5 透出 API 摘要用）。"""
        return {
            "plugin_id": self._plugin_id,
            "state": self._state,
            "waiting_for": list(self._waiting_for),
            "reason": self._reason,
        }

    # -- 流转（非法组合抛 InvalidFiberTransition）-------------------------
    def activate(self) -> None:
        self._move(FIBER_ACTIVE)
        self._waiting_for = ()

    def mark_waiting(self, missing: list[str] | tuple[str, ...]) -> None:
        self._move(FIBER_WAITING)
        self._waiting_for = tuple(dict.fromkeys(str(item) for item in missing))

    def fail(self, reason: str) -> None:
        self._move(FIBER_FAILED)
        self._reason = str(reason or "unknown")
        self._waiting_for = ()

    def _move(self, target: str) -> None:
        if (self._state, target) not in _ALLOWED:
            raise InvalidFiberTransition(
                f"fiber={self._plugin_id or '<anonymous>'} 不允许的流转：{self._state} → {target}"
            )
        if target != self._state:
            logger.debug("fiber_transition plugin=%s %s → %s", self._plugin_id, self._state, target)
        self._state = target


__all__ = [
    "FIBER_ACTIVE",
    "FIBER_FAILED",
    "FIBER_WAITING",
    "Fiber",
    "InvalidFiberTransition",
]
