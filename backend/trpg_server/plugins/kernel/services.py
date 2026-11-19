"""ServiceRegistry：按名服务提供 + 声明式 inject + 反应式依赖跟踪。

Cordis「Reactive Coeffects」在本平台的最小落地，四条规则：

1. ``provide(key, obj, ctx)`` 本身是一个 effect：提供记录随提供方 ctx 的
   dispose 自动出栈。同一 key 允许多提供方，后进者为顶层、先进者记为被
   遮蔽（记 WARNING）；顶层撤销后被遮蔽方自动浮回。
2. ``inject(keys, ctx, callback)`` 把声明挂到 ctx 上（声明本身也是 effect）：
   keys 全部就绪 → 执行 ``callback(ctx)``；任一**顶层**提供方消失 →
   注入方执行自己的 dispose 全量回滚并落 WAITING（waiting_for 列出缺失
   key，绝不静默）；服务恢复 → 自动重新执行 callback。被遮蔽方的撤销
   不影响已激活注入方（它绑定的还是旧对象）。
3. 同一 ctx 的多条注入共命运：整 ctx 回滚后未受影响的声明立即重估，
   在用服务都还在 → 立即重激活。
4. 激活回调抛异常 → 注入方回滚到空栈并落 FAILED（终态，不自动重试）；
   同一 ctx 的其余声明一并终结，避免留下假 WAITING 的静默状态。

观测 hook 只是简单回调列表，不是事件总线；回调抛异常只记日志。
并发前提：启停是单线程管理路径，本模块不加锁。同一 ctx 既提供又注入
同一 key 的自环不被支持（context.dispose 的防重入标记会把它截断）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from .context import PluginContext
from .fiber import FIBER_ACTIVE, FIBER_FAILED, FIBER_WAITING, Fiber

logger = logging.getLogger(__name__)

InjectCallback = Callable[[PluginContext], None]


class _Provider:
    __slots__ = ("key", "obj", "ctx")

    def __init__(self, key: str, obj: Any, ctx: PluginContext) -> None:
        self.key = key
        self.obj = obj
        self.ctx = ctx


class _Injection:
    __slots__ = ("keys", "ctx", "callback", "fiber")

    def __init__(self, keys: tuple[str, ...], ctx: PluginContext, callback: InjectCallback) -> None:
        self.keys = keys
        self.ctx = ctx
        self.callback = callback
        self.fiber = Fiber(ctx.plugin_id)


class ServiceRegistry:
    """进程级服务注册表；声明随 ctx 撤销自动离场，回滚后由内核重新挂回。"""

    def __init__(self) -> None:
        # key → 提供方栈（后进为顶层；顶层之前的都是被遮蔽的历史提供方）。
        self._providers: dict[str, list[_Provider]] = {}
        # 当前存活的 inject 声明。
        self._injections: list[_Injection] = []
        self._provide_hooks: list[Callable[[str, Any], None]] = []
        self._injection_hooks: list[Callable[[str, str, list[str]], None]] = []

    # -- 观测 hook（简单回调列表，不是事件总线）-------------------------
    def add_provide_hook(self, hook: Callable[[str, Any], None]) -> None:
        """顶层变化 → ``hook(key, obj)``；顶层撤销 → ``hook(key, 浮回对象或 None)``。"""
        self._provide_hooks.append(hook)

    def add_injection_hook(self, hook: Callable[[str, str, list[str]], None]) -> None:
        """注入方状态变化 → ``hook(plugin_id, state, waiting_for)``。"""
        self._injection_hooks.append(hook)

    def _fire(self, hooks: list, *args: Any) -> None:
        for hook in list(hooks):
            try:
                hook(*args)
            except Exception:  # noqa: BLE001 - 观测回调不能拖垮内核
                logger.warning("kernel_hook_failed args=%s", args, exc_info=True)

    # -- 查找 -------------------------------------------------------------
    def get(self, key: str) -> Any | None:
        """按名查找顶层服务；找不到返回 None。"""
        stack = self._providers.get(str(key))
        return stack[-1].obj if stack else None

    def provider_count(self, key: str) -> int:
        """该 key 的提供方数（含被遮蔽方），供遮蔽场景观测。"""
        return len(self._providers.get(str(key)) or ())

    # -- 提供 -------------------------------------------------------------
    def provide(self, key: str, obj: Any, ctx: PluginContext) -> None:
        """注册服务提供方（本身是一个 effect，随 ctx dispose 自动出栈）。"""
        key = str(key)
        record = _Provider(key, obj, ctx)

        def _register() -> Callable[[], None]:
            stack = self._providers.setdefault(key, [])
            if stack:
                logger.warning(
                    "service_key_shadowed key=%s provider=%s shadowed=%s",
                    key,
                    ctx.plugin_id,
                    stack[-1].ctx.plugin_id,
                )
            stack.append(record)
            self._fire(self._provide_hooks, key, obj)
            self._evaluate_waiters(key)
            return lambda: self._remove_provider(record)

        ctx.effect(_register)

    def _remove_provider(self, record: _Provider) -> None:
        stack = self._providers.get(record.key)
        if not stack or record not in stack:
            return
        was_top = stack[-1] is record
        stack.remove(record)
        if not stack:
            self._providers.pop(record.key, None)
        if not was_top:
            return  # 撤的是被遮蔽方，顶层没变，注入方无感。
        self._fire(self._provide_hooks, record.key, self.get(record.key))
        self._rollback_consumers(record.key)

    # -- 注入 -------------------------------------------------------------
    def inject(self, keys: Iterable[str], ctx: PluginContext, callback: InjectCallback) -> Fiber:
        """声明依赖；keys 全部就绪时执行 ``callback(ctx)``。返回 fiber 供观测。"""
        normalized = tuple(dict.fromkeys(str(item) for item in keys))
        injection = _Injection(normalized, ctx, callback)
        self._subscribe(injection)
        missing = self._missing_keys(normalized)
        if missing:
            self._set_waiting(injection, missing)
        else:
            self._activate(injection)
        return injection.fiber

    def _subscribe(self, injection: _Injection) -> None:
        """声明挂到 ctx 上：ctx 撤销时声明离场（反应式回滚后由内核重新挂回）。"""

        def _bind() -> Callable[[], None]:
            self._injections.append(injection)
            return lambda: self._remove_injection(injection)

        injection.ctx.effect(_bind)

    def _remove_injection(self, injection: _Injection) -> None:
        try:
            self._injections.remove(injection)
        except ValueError:
            pass

    def _missing_keys(self, keys: tuple[str, ...]) -> list[str]:
        return [key for key in keys if not self._providers.get(key)]

    # -- 反应式 -----------------------------------------------------------
    def _evaluate_waiters(self, key: str) -> None:
        """key 出现（新）提供方：重估等待它的声明。"""
        for injection in [item for item in self._injections if key in item.keys]:
            if injection.fiber.state != FIBER_WAITING:
                continue
            missing = self._missing_keys(injection.keys)
            if missing:
                self._set_waiting(injection, missing)
            else:
                self._activate(injection)

    def _rollback_consumers(self, key: str) -> None:
        """顶层提供方消失：整 ctx 回滚所有依赖该 key 的激活注入方。"""
        done: set[int] = set()
        while True:
            target = None
            for item in self._injections:
                if item.fiber.state == FIBER_ACTIVE and key in item.keys and id(item.ctx) not in done:
                    target = item.ctx
                    break
            if target is None:
                return
            done.add(id(target))
            self._rollback_ctx(target)

    def _rollback_ctx(self, ctx: PluginContext) -> None:
        if ctx.disposing:
            return  # 该 ctx 正在全量撤销（宿主 disable 或级联），订阅随栈一起离场。
        held = [item for item in self._injections if item.ctx is ctx]
        if not held:
            return
        logger.info("injections_rolled_back plugin=%s", ctx.plugin_id)
        ctx.dispose()  # 注入方自己的 dispose：全部效果 LIFO 撤销，订阅一并消耗
        for injection in held:
            self._subscribe(injection)  # 声明比激活长寿：重新挂回，等服务恢复
            missing = self._missing_keys(injection.keys)
            if missing:
                self._set_waiting(injection, missing)
            else:
                self._activate(injection)  # 有被遮蔽提供方浮回顶层：立即重激活

    def _set_waiting(self, injection: _Injection, missing: list[str]) -> None:
        injection.fiber.mark_waiting(missing)
        self._fire(self._injection_hooks, injection.ctx.plugin_id, FIBER_WAITING, list(missing))

    def _activate(self, injection: _Injection) -> None:
        ctx = injection.ctx
        try:
            injection.callback(ctx)
        except Exception as exc:  # noqa: BLE001 - 激活失败只终结该 ctx 的声明
            siblings = [item for item in self._injections if item.ctx is ctx]
            if not ctx.disposing:
                ctx.dispose()  # 半途注册全部撤销，订阅随栈消耗
            injection.fiber.fail(f"激活回调抛异常：{exc}")
            self._fire(self._injection_hooks, ctx.plugin_id, FIBER_FAILED, [])
            for item in siblings:  # 同 ctx 其余声明共命运，不留假 WAITING
                if item is injection or item.fiber.state == FIBER_FAILED:
                    continue
                item.fiber.fail("同 ctx 的兄弟注入激活失败，整体终结")
                self._fire(self._injection_hooks, item.ctx.plugin_id, FIBER_FAILED, [])
            logger.warning(
                "injection_activate_failed plugin=%s keys=%s",
                ctx.plugin_id,
                list(injection.keys),
                exc_info=True,
            )
            return
        injection.fiber.activate()
        self._fire(self._injection_hooks, ctx.plugin_id, FIBER_ACTIVE, [])


__all__ = ["ServiceRegistry"]
