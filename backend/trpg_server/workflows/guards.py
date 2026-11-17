"""行动校验钩子。

工作流本身不感知地图插件；需要墙体/视线校验的插件在加载时注册一个 guard
回调。工作流入口在创建流程之前统一调用，保证 AI 与真人、工具与 REST 都
走同一条校验链，不能绕过射程、墙体与行动预算。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

ActionGuard = Callable[[Mapping[str, Any]], Iterable[str] | Mapping[str, Any]]

_GUARDS: list[ActionGuard] = []


def register_action_guard(guard: ActionGuard) -> None:
    if guard in _GUARDS:
        return
    _GUARDS.append(guard)


def clear_action_guards() -> None:
    _GUARDS.clear()


def validate_action_report(
    room_dir: Any,
    *,
    kind: str,
    actor: str,
    targets: Iterable[str] = (),
    range_ft: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """运行所有已注册 guard；返回 ``(错误, 说明)``。

    guard 可以返回错误字符串列表（旧契约），也可以返回
    ``{"errors": [...], "notes": [...]}``。``notes`` 用于标注本次校验的退化，
    例如没有棋子时无法校验视线与墙体，不阻断结算但要让 KP 看见。
    """
    payload = {
        "room_dir": room_dir,
        "kind": str(kind or ""),
        "actor": str(actor or ""),
        "targets": [str(name) for name in targets if str(name or "").strip()],
        "range_ft": range_ft,
        **dict(extra or {}),
    }
    errors: list[str] = []
    notes: list[str] = []
    for guard in list(_GUARDS):
        try:
            result = guard(payload) or []
        except Exception:  # noqa: BLE001 - 地图数据损坏不能阻断战斗结算
            continue
        if isinstance(result, Mapping):
            rows = result.get("errors") or []
            row_notes = result.get("notes") or []
        else:
            rows = result
            row_notes = []
        for item in rows:
            text = str(item or "").strip()
            if text and text not in errors:
                errors.append(text)
        for item in row_notes:
            text = str(item or "").strip()
            if text and text not in notes:
                notes.append(text)
    return errors, notes


def validate_action(
    room_dir: Any,
    *,
    kind: str,
    actor: str,
    targets: Iterable[str] = (),
    range_ft: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> list[str]:
    """只返回错误列表的兼容入口；需要退化说明时用 :func:`validate_action_report`。"""
    errors, _notes = validate_action_report(
        room_dir,
        kind=kind,
        actor=actor,
        targets=targets,
        range_ft=range_ft,
        extra=extra,
    )
    return errors


__all__ = [
    "ActionGuard",
    "clear_action_guards",
    "register_action_guard",
    "validate_action",
    "validate_action_report",
]
