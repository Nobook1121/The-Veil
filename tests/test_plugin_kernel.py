"""mini 内核（``trpg_server.plugins.kernel``）单测。

覆盖：effect 注册与 LIFO 撤销、fn 抛异常不残留、异常 disposer 不阻断兄弟
撤销、provide/get 与多提供方遮蔽、反应式 inject 往返、fiber 状态机与非法
流转拒绝、内核零依赖红线（机械守卫）。

并发场景不做（启停是单线程管理路径）——内核各模块头已注明该前提，
这里同样只验证单线程语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.plugins.kernel import (
    FIBER_ACTIVE,
    FIBER_FAILED,
    FIBER_WAITING,
    Fiber,
    InvalidFiberTransition,
    PluginContext,
    ServiceRegistry,
)
from trpg_server.plugins.kernel import context as kernel_context


# ---------------------------------------------------------------- 工具


def _reg(events: list[str], name: str):
    """返回一个注册函数：记一条 register，带回记 dispose 的回执。"""

    def register():
        events.append(f"register:{name}")
        return lambda: events.append(f"dispose:{name}")

    return register


# ---------------------------------------------------------------- context


def test_effect_lifo_dispose_order() -> None:
    ctx = PluginContext("p-lifo")
    events: list[str] = []
    for name in ("a", "b", "c"):
        ctx.effect(_reg(events, name))
    assert events == ["register:a", "register:b", "register:c"]
    assert ctx.effect_count == 3
    ctx.dispose()
    assert events == [
        "register:a",
        "register:b",
        "register:c",
        "dispose:c",
        "dispose:b",
        "dispose:a",
    ]
    assert ctx.effect_count == 0
    # 无回执（返回值不是可调用对象）：允许，但不算可撤销注册。
    assert ctx.effect(lambda: None) is None
    assert ctx.effect_count == 0


def test_effect_fn_exception_leaves_stack_unchanged() -> None:
    ctx = PluginContext("p-boom")
    events: list[str] = []
    ctx.effect(_reg(events, "good"))

    def bad():
        events.append("bad:ran")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        ctx.effect(bad)
    assert ctx.effect_count == 1
    assert events == ["register:good", "bad:ran"]
    ctx.dispose()
    # bad 没留下任何注册，撤销时只有 good 的回执。
    assert events == ["register:good", "bad:ran", "dispose:good"]


def test_dispose_survives_disposer_exception() -> None:
    ctx = PluginContext("p-chaos")
    order: list[str] = []
    ctx.effect(_reg(order, "first"))

    def boom_register():
        def dispose():
            order.append("dispose:boom")
            raise ValueError("undo crash")

        return dispose

    ctx.effect(boom_register)
    ctx.effect(_reg(order, "last"))
    ctx.dispose()
    # 单个 disposer 抛异常只记日志，不中断兄弟撤销，栈清空。
    assert order == [
        "register:first",
        "register:last",
        "dispose:last",
        "dispose:boom",
        "dispose:first",
    ]
    assert ctx.effect_count == 0


# ---------------------------------------------------------------- services


def test_provide_get_and_shadowing() -> None:
    reg = ServiceRegistry()
    assert reg.get("nothing") is None
    provider_a = PluginContext("provider-a")
    reg.provide("map", "obj-a", provider_a)
    assert reg.get("map") == "obj-a"
    provider_b = PluginContext("provider-b")
    reg.provide("map", "obj-b", provider_b)
    assert reg.get("map") == "obj-b"  # 后者覆盖前者
    assert reg.provider_count("map") == 2  # 前者记为被遮蔽，仍留在栈里
    provider_b.dispose()
    assert reg.get("map") == "obj-a"  # 顶层撤销，被遮蔽方浮回
    assert reg.provider_count("map") == 1
    provider_a.dispose()
    assert reg.get("map") is None
    assert reg.provider_count("map") == 0


def test_inject_reactive_roundtrip_three_times() -> None:
    reg = ServiceRegistry()
    events: list[str] = []
    consumer = PluginContext("consumer")

    def callback(ctx: PluginContext) -> None:
        events.append("activate")
        ctx.effect(_reg(events, "svc"))

    fiber = reg.inject(["map"], consumer, callback)
    # 服务就绪才激活：没有提供方时回调一次都不跑。
    assert fiber.state == FIBER_WAITING
    assert fiber.waiting_for == ["map"]
    assert events == []

    for index in (1, 2, 3):
        provider = PluginContext(f"provider-{index}")
        reg.provide("map", f"obj-{index}", provider)
        assert fiber.state == FIBER_ACTIVE
        assert fiber.waiting_for == []
        assert consumer.effect_count == 2  # 订阅声明 + 回调注册的效果
        provider.dispose()
        # 注入方被自动回滚（执行它自己的 dispose），重新落 WAITING。
        assert fiber.state == FIBER_WAITING
        assert fiber.waiting_for == ["map"]
        assert consumer.effect_count == 1  # 只剩重新挂回的订阅声明

    assert events == ["activate", "register:svc", "dispose:svc"] * 3
    assert reg.get("map") is None


def test_shadowed_provider_resurfaces_to_consumers() -> None:
    reg = ServiceRegistry()
    provider_a = PluginContext("provider-a")
    provider_b = PluginContext("provider-b")
    reg.provide("map", "obj-a", provider_a)
    seen: list[str] = []
    consumer = PluginContext("consumer")

    def callback(ctx: PluginContext) -> None:
        seen.append(str(reg.get("map")))

    fiber = reg.inject(["map"], consumer, callback)
    assert fiber.state == FIBER_ACTIVE
    assert seen == ["obj-a"]
    # 遮蔽：不重绑已激活注入方（它绑定的还是旧对象）。
    reg.provide("map", "obj-b", provider_b)
    assert reg.get("map") == "obj-b"
    assert seen == ["obj-a"]
    # 顶层消失 → 注入方回滚 → 被遮蔽方浮回 → 立即重激活。
    provider_b.dispose()
    assert fiber.state == FIBER_ACTIVE
    assert seen == ["obj-a", "obj-a"]
    assert reg.get("map") == "obj-a"


def test_inject_waits_for_every_key() -> None:
    reg = ServiceRegistry()
    events: list[str] = []
    consumer = PluginContext("consumer")
    fiber = reg.inject(["a", "b"], consumer, lambda ctx: events.append("go"))
    assert fiber.state == FIBER_WAITING
    assert fiber.waiting_for == ["a", "b"]
    provider_a = PluginContext("provider-a")
    reg.provide("a", 1, provider_a)
    assert fiber.state == FIBER_WAITING
    assert fiber.waiting_for == ["b"]  # 部分就绪仍等待
    provider_b = PluginContext("provider-b")
    reg.provide("b", 2, provider_b)
    assert fiber.state == FIBER_ACTIVE
    assert events == ["go"]
    provider_b.dispose()
    assert fiber.state == FIBER_WAITING
    assert fiber.waiting_for == ["b"]
    assert reg.get("a") == 1


def test_inject_activation_failure_is_terminal() -> None:
    reg = ServiceRegistry()
    events: list[str] = []
    consumer = PluginContext("consumer-bad")

    def bad_callback(ctx: PluginContext) -> None:
        ctx.effect(_reg(events, "halfway"))
        raise RuntimeError("callback boom")

    provider_1 = PluginContext("provider-1")
    fiber = reg.inject(["map"], consumer, bad_callback)
    assert fiber.state == FIBER_WAITING
    reg.provide("map", "obj-1", provider_1)  # 激活 → 回调抛异常
    assert fiber.state == FIBER_FAILED
    assert "callback boom" in fiber.reason
    assert events == ["register:halfway", "dispose:halfway"]  # 半途注册被清
    assert consumer.effect_count == 0
    provider_1.dispose()
    provider_2 = PluginContext("provider-2")
    reg.provide("map", "obj-2", provider_2)  # FAILED 是终态，不复活
    assert fiber.state == FIBER_FAILED
    assert events.count("register:halfway") == 1


def test_consumer_dispose_removes_declaration_no_resurrection() -> None:
    reg = ServiceRegistry()
    provider = PluginContext("provider")
    reg.provide("map", "obj", provider)
    events: list[str] = []
    consumer = PluginContext("gone")
    fiber = reg.inject(["map"], consumer, lambda ctx: events.append("activate"))
    assert fiber.state == FIBER_ACTIVE
    consumer.dispose()  # 宿主禁用：声明随 ctx 离场
    provider.dispose()
    again = PluginContext("provider-again")
    reg.provide("map", "obj-again", again)
    assert events == ["activate"]  # 已禁用的注入方不复活


def test_lifecycle_hooks_observe_provide_and_injection() -> None:
    reg = ServiceRegistry()
    provide_events: list[tuple[str, object]] = []
    injection_events: list[tuple[str, str, tuple[str, ...]]] = []
    reg.add_provide_hook(lambda key, obj: provide_events.append((key, obj)))
    reg.add_injection_hook(
        lambda plugin_id, state, waiting: injection_events.append((plugin_id, state, tuple(waiting)))
    )

    def noisy_hook(*args: object) -> None:
        raise ValueError("observer boom")

    reg.add_injection_hook(noisy_hook)  # 观测回调抛异常不能拖垮内核

    provider = PluginContext("provider")
    reg.provide("map", "obj", provider)
    consumer = PluginContext("consumer")
    fiber = reg.inject(["map"], consumer, lambda ctx: None)
    assert fiber.state == FIBER_ACTIVE
    assert provide_events == [("map", "obj")]
    assert ("consumer", FIBER_ACTIVE, ()) in injection_events
    provider.dispose()
    assert provide_events[-1] == ("map", None)
    assert ("consumer", FIBER_WAITING, ("map",)) in injection_events
    assert fiber.state == FIBER_WAITING


# ---------------------------------------------------------------- fiber


def test_fiber_transitions_and_rejections() -> None:
    fiber = Fiber("p-fiber")
    assert fiber.state == FIBER_WAITING
    fiber.mark_waiting(["dep-a", "dep-b"])
    assert fiber.waiting_for == ["dep-a", "dep-b"]
    fiber.mark_waiting(["dep-a"])  # WAITING → WAITING：更新在等谁，合法
    assert fiber.waiting_for == ["dep-a"]
    fiber.activate()
    assert fiber.active is True
    assert fiber.state == FIBER_ACTIVE
    assert fiber.waiting_for == []
    with pytest.raises(InvalidFiberTransition):
        fiber.activate()  # ACTIVE → ACTIVE 非法
    fiber.mark_waiting(["dep-a"])  # ACTIVE → WAITING 合法（反应式回滚）
    assert fiber.state == FIBER_WAITING
    fiber.fail("加载失败")
    assert fiber.state == FIBER_FAILED
    assert fiber.reason == "加载失败"
    assert fiber.waiting_for == []
    assert fiber.to_dict()["state"] == FIBER_FAILED
    # FAILED 是终态，任何流转都被拒绝。
    with pytest.raises(InvalidFiberTransition):
        fiber.activate()
    with pytest.raises(InvalidFiberTransition):
        fiber.mark_waiting([])
    with pytest.raises(InvalidFiberTransition):
        fiber.fail("再失败一次")


# ---------------------------------------------------------------- 红线守卫


def test_kernel_stays_dependency_free() -> None:
    """红线：内核不 import Flask、不 import 业务模块（机械守卫，防回潮）。"""
    kernel_dir = Path(kernel_context.__file__).resolve().parent
    for name in ("__init__.py", "context.py", "fiber.py", "services.py"):
        for line in (kernel_dir / name).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            assert "flask" not in stripped.lower(), (name, stripped)
            assert "trpg_server" not in stripped, (name, stripped)
