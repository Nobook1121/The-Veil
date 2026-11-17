"""跨请求的流程续跑器。

等待玩家反应/豁免的流程在决定到达或超时后，需要继续执行剩余步骤。续跑器
按名字注册；流程的 ``metadata.continuation`` 指向它。引擎在流程收束时调用，
续跑失败会把流程标记为 failed，而不是留下半截状态。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

Continuation = Callable[[Any, dict[str, Any], dict[str, Any]], Any]

_CONTINUATIONS: dict[str, Continuation] = {}


def register(name: str, handler: Continuation) -> None:
    key = str(name or "").strip()
    if key and callable(handler):
        _CONTINUATIONS[key] = handler


def unregister(name: str) -> None:
    _CONTINUATIONS.pop(str(name or "").strip(), None)


def clear() -> None:
    _CONTINUATIONS.clear()


def has(name: str) -> bool:
    return str(name or "").strip() in _CONTINUATIONS


def run(
    name: str,
    room_dir: Any,
    flow: dict[str, Any],
    data: dict[str, Any],
) -> Any:
    handler = _CONTINUATIONS.get(str(name or "").strip())
    if handler is None:
        raise KeyError(f"未注册的流程续跑器：{name}")
    return handler(room_dir, flow, data)


def decisions(flow: Mapping[str, Any]) -> dict[str, Any]:
    """收集所有已决步骤的结果，键为步骤 id。"""
    return {
        str(step.get("id")): step.get("result")
        for step in flow.get("steps") or []
        if isinstance(step, Mapping) and str(step.get("status")) == "completed"
    }


__all__ = ["Continuation", "clear", "decisions", "has", "register", "run", "unregister"]
