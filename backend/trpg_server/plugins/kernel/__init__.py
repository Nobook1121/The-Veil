"""插件 mini 内核（cordis-paradigm-adoption-plan.md 阶段 1）。

三个新概念，刻意不引入第四个：

- :class:`PluginContext` —— 副作用的插座：注册即副作用，dispose LIFO 撤销；
- :class:`ServiceRegistry` —— 按名服务 + 声明式 inject + 反应式依赖跟踪；
- :class:`Fiber` —— WAITING / ACTIVE / FAILED 三态生命周期（挂进
  ``PluginRecord`` 是阶段 2 的事）。

红线：内核不 import Flask、不 import 任何业务模块，零依赖纯逻辑；
启停是单线程管理路径，内核不加锁、不做并发。本包与既有 ``registry.py``
并行共存，现阶段互不引用、互不影响。
"""

from __future__ import annotations

from .context import PluginContext
from .fiber import FIBER_ACTIVE, FIBER_FAILED, FIBER_WAITING, Fiber, InvalidFiberTransition
from .services import ServiceRegistry

__all__ = [
    "FIBER_ACTIVE",
    "FIBER_FAILED",
    "FIBER_WAITING",
    "Fiber",
    "InvalidFiberTransition",
    "PluginContext",
    "ServiceRegistry",
]
