"""行动校验钩子。

工作流本身不感知地图插件；需要墙体/视线校验的插件在加载时注册一个 guard
回调。工作流入口在创建流程之前统一调用，保证 AI 与真人、工具与 REST 都
走同一条校验链，不能绕过射程、墙体与行动预算。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

ActionGuard = Callable[[Mapping[str, Any]], Iterable[str] | Mapping[str, Any]]

# (guard, critical)：critical 为真表示校验器异常时必须阻止行动（fail-closed），
# 否则只记录降级说明并放行。
_GUARDS: list[tuple[ActionGuard, bool]] = []


def register_action_guard(guard: ActionGuard, *, critical: bool = False) -> None:
    for index, (existing, existing_critical) in enumerate(_GUARDS):
        if existing is guard:
            if critical and not existing_critical:
                _GUARDS[index] = (existing, True)
            return
    _GUARDS.append((guard, bool(critical)))


def clear_action_guards() -> None:
    _GUARDS.clear()


def registered_guards() -> tuple[ActionGuard, ...]:
    """当前已注册的 guard 只读快照（插件激活窗口前后对拍用）。"""
    return tuple(guard for guard, _critical in _GUARDS)


def unregister_action_guard(guard: ActionGuard) -> bool:
    """按身份移除一个 guard（插件运行期回滚用）；返回是否真的移除。

    插件代码在 ``create_tools()`` 等工厂调用里直接调 ``register_action_guard``，
    宿主无法逐调用包装；启停的撤销回执靠「激活前后对拍 + 定点摘除」实现，
    因此这里必须支持按身份定点摘除，而不是只能全清。
    """
    for index, (existing, _critical) in enumerate(_GUARDS):
        if existing is guard:
            _GUARDS.pop(index)
            return True
    return False


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
    for guard, critical in list(_GUARDS):
        try:
            result = guard(payload) or []
        except Exception as exc:  # noqa: BLE001 - 校验器异常不能静默吞掉
            logger.exception(
                "行动校验器异常 kind=%s actor=%s critical=%s",
                payload.get("kind"),
                payload.get("actor"),
                critical,
            )
            if critical:
                errors.append(
                    f"行动校验失败（{type(exc).__name__}），为保证规则正确性已阻止本次行动。"
                )
            else:
                notes.append(
                    f"行动校验异常（{type(exc).__name__}），本次已降级放行，请 KP 复核。"
                )
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
    "registered_guards",
    "unregister_action_guard",
    "validate_action",
    "validate_action_report",
]
