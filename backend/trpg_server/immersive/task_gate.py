"""沉浸系统的重任务协调闸门。

语音合成与实时配图共用同一台机器的 CPU / IO：语音请求在生图任务运行期间排队等待，
而不是与生图抢占。这里只维护一个计数器，避免两个子系统互相 import。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_COND = threading.Condition()
_HEAVY_ACTIVE = 0


@contextmanager
def heavy_task():
    """标记一段高成本任务（当前由实时配图调用）。"""
    global _HEAVY_ACTIVE
    with _COND:
        _HEAVY_ACTIVE += 1
        _COND.notify_all()
    try:
        yield
    finally:
        with _COND:
            _HEAVY_ACTIVE = max(0, _HEAVY_ACTIVE - 1)
            _COND.notify_all()


def heavy_active() -> int:
    with _COND:
        return _HEAVY_ACTIVE


def wait_until_idle(timeout: float = 5.0) -> bool:
    with _COND:
        if _HEAVY_ACTIVE == 0:
            return True
        _COND.wait_for(lambda: _HEAVY_ACTIVE == 0, timeout=max(0.1, timeout))
        return _HEAVY_ACTIVE == 0
