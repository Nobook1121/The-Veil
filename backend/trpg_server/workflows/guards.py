"""行动校验钩子。

工作流本身不感知地图插件；需要墙体/视线校验的插件在加载时注册一个 guard
回调。工作流入口在创建流程之前统一调用，保证 AI 与真人、工具与 REST 都
走同一条校验链，不能绕过射程、墙体与行动预算。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

ActionGuard = Callable[[Mapping[str, Any]], Iterable[str]]

_GUARDS: list[ActionGuard] = []


def register_action_guard(guard: ActionGuard) -> None:
    if guard in _GUARDS:
        return
    _GUARDS.append(guard)


def clear_action_guards() -> None:
    _GUARDS.clear()


def validate_action(
    room_dir: Any,
    *,
    kind: str,
    actor: str,
    targets: Iterable[str] = (),
    range_ft: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> list[str]:
    """运行所有已注册 guard；返回中文错误列表（空列表表示通过）。"""
    payload = {
        "room_dir": room_dir,
        "kind": str(kind or ""),
        "actor": str(actor or ""),
        "targets": [str(name) for name in targets if str(name or "").strip()],
        "range_ft": range_ft,
        **dict(extra or {}),
    }
    errors: list[str] = []
    for guard in list(_GUARDS):
        try:
            result = guard(payload) or []
        except Exception:  # noqa: BLE001 - 地图数据损坏不能阻断战斗结算
            continue
        for item in result:
            text = str(item or "").strip()
            if text and text not in errors:
                errors.append(text)
    return errors


__all__ = ["ActionGuard", "clear_action_guards", "register_action_guard", "validate_action"]
