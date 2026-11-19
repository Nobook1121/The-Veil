"""PluginContext：一个插件的「副作用插座」。

约定（cordis-paradigm-adoption-plan.md §3 核心四件套之一）：

- 插件的一切注册都经 :meth:`effect`：执行注册函数 ``fn``，``fn`` 返回可调用
  对象则作为 disposer（撤销回执）入栈；
- ``fn`` 抛异常时**不留下任何注册**（效果栈不变）；fn 半途污染了外部状态
  由 fn 自己负责清理——本平台注册点全是同步内存操作，常规写法天然满足；
- :meth:`dispose` 按 LIFO 逆序执行全部 disposer；单个 disposer 抛异常只记
  日志、绝不中断兄弟撤销；
- 不支持异步 disposer：本平台注册点全是同步内存操作，内核不做 async；
- dispose 后的 ctx 可复用（效果栈已清空），这是「反应式回滚 → 服务恢复后
  重新激活」的基础。

并发前提：启停是单线程管理路径，本模块不加锁；若未来引入并发启停，
需在外层串行化后再复用这里的语义。
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

Disposer = Callable[[], None]
EffectFn = Callable[[], "Disposer | None"]


class PluginContext:
    """每插件一个实例；只持 id 与效果栈，内核不 import Flask / 业务模块。"""

    __slots__ = ("plugin_id", "_disposers", "_disposing")

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = str(plugin_id or "")
        self._disposers: list[Disposer] = []
        self._disposing = False

    # -- 只读视图 ---------------------------------------------------------
    @property
    def effect_count(self) -> int:
        """当前挂着的 disposer 数（启停往返对拍「零残留」用）。"""
        return len(self._disposers)

    @property
    def disposing(self) -> bool:
        """是否正在执行 dispose（内核防重入标记，见 :meth:`dispose`）。"""
        return self._disposing

    # -- 注册即副作用 -----------------------------------------------------
    def effect(self, fn: EffectFn) -> Disposer | None:
        """执行 ``fn``；返回可调用对象则作为 disposer 入栈并原样返回。

        ``fn`` 抛异常时效果栈不变，异常原样向外抛（由调用方决定该插件的
        命运）。返回值不是可调用对象：不视为可撤销注册，记 DEBUG 提示。
        """
        disposer = fn()
        if callable(disposer):
            self._disposers.append(disposer)
            return disposer
        logger.debug("effect_without_disposer plugin=%s", self.plugin_id)
        return None

    def dispose(self) -> None:
        """LIFO 逆序执行全部 disposer。

        重入（同一 ctx 嵌套 dispose，反应式级联时可能出现）直接返回，
        由最外层那一遍统一清空；dispose 后栈为空，ctx 可复用。
        """
        if self._disposing:
            return
        self._disposing = True
        try:
            while self._disposers:
                disposer = self._disposers.pop()
                try:
                    disposer()
                except Exception:  # noqa: BLE001 - 单个撤销失败不能中断兄弟撤销
                    logger.warning(
                        "disposer_failed plugin=%s remaining=%s",
                        self.plugin_id,
                        len(self._disposers),
                        exc_info=True,
                    )
        finally:
            self._disposing = False


__all__ = ["PluginContext"]
