"""玩家投掷：内存态待投掷请求与阻塞等待。

Chat 请求线程在工具内部调用 :func:`wait_for_player_roll` 阻塞等待玩家点击，
前端通过 REST 调用 :func:`resolve_roll` / :func:`complete_roll` 推进状态。
本模块不依赖 Flask，广播函数由 ``socket_events.py`` 注入。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

AUTO_COMPLETE_SECONDS = 5.0
_BROADCASTER: Callable[[str, dict[str, Any]], None] | None = None
_PENDING: dict[str, "PendingRoll"] = {}
_LOCK = threading.Lock()


@dataclass
class PendingRoll:
    request_id: str
    room_id: str
    room_dir: Any
    roller_user_id: str
    roller_name: str
    tool_name: str
    plan: dict[str, Any]
    resolver: Callable[[], dict[str, Any]]
    created_at: float
    expires_at: float
    state: str = "pending"  # pending / rolled / finished / cancelled / timeout / forced
    result: dict[str, Any] | None = None
    rolled_at: float | None = None
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


def register_broadcaster(fn: Callable[[str, dict[str, Any]], None] | None) -> None:
    """注入广播函数：``fn(event, payload)``。"""
    global _BROADCASTER
    _BROADCASTER = fn


def has_broadcaster() -> bool:
    return _BROADCASTER is not None


def _emit(event: str, payload: dict[str, Any]) -> None:
    broadcaster = _BROADCASTER
    if broadcaster is None:
        return
    try:
        broadcaster(event, payload)
    except Exception:  # noqa: BLE001 - 广播失败不能中断投掷流程
        logger.warning("Failed to broadcast dice event %s", event, exc_info=True)


def _log(room_dir: Any, message: str, actor: str | None = None, room_id: str | None = None, **details: Any) -> None:
    if not room_dir:
        return
    try:
        from trpg_server.room_log import log_room_event

        log_room_event(room_dir, message, actor=actor, room_id=room_id, logger_=logger, **details)
    except Exception:  # noqa: BLE001 - 日志失败不影响投掷
        logger.warning("Failed to write dice room log", exc_info=True)


def _result_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """从工具结果里提取前端播放动画所需的字段。"""
    message = result.get("visible_message") if isinstance(result, dict) else None
    metadata = message.get("metadata") if isinstance(message, dict) else None
    dice = metadata.get("dice_roll") if isinstance(metadata, dict) else None
    dice = dice if isinstance(dice, dict) else {}
    return {
        "faces": dice.get("faces") or [],
        "labels": dice.get("labels") or [],
        "roll": dice.get("roll"),
        "rolls": dice.get("rolls") or [],
        "total": dice.get("total"),
        "success": dice.get("success"),
        "success_level": dice.get("success_level"),
        "summary": result.get("summary") if isinstance(result, dict) else "",
        "check_name": dice.get("check_name"),
        "difficulty": dice.get("difficulty"),
        "target": dice.get("target"),
        "roller": dice.get("roller"),
        "mode": dice.get("mode"),
        "expression": dice.get("expression"),
        "count": dice.get("count"),
        "sides": dice.get("sides"),
        "candidates": dice.get("candidates") or [],
    }


def _annotate(result: dict[str, Any], request_id: str, *, auto: bool, player_roll: bool = True) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result
    message = result.get("visible_message")
    if isinstance(message, dict):
        metadata = message.setdefault("metadata", {})
        dice = metadata.get("dice_roll")
        if isinstance(dice, dict):
            dice["request_id"] = request_id
            dice["auto"] = bool(auto)
            dice["player_roll"] = bool(player_roll)
    return result


def _plan_payload(pending: PendingRoll) -> dict[str, Any]:
    return {
        "room_id": pending.room_id,
        "request_id": pending.request_id,
        "tool_name": pending.tool_name,
        "roller": {"user_id": pending.roller_user_id, "username": pending.roller_name},
        "check": pending.plan.get("check"),
        "plan": pending.plan,
        "expires_at": pending.expires_at,
    }


def _state_payload(pending: PendingRoll) -> dict[str, Any]:
    return {
        "request_id": pending.request_id,
        "state": pending.state,
        "roller": {"user_id": pending.roller_user_id, "username": pending.roller_name},
        "plan": pending.plan,
        "tool_name": pending.tool_name,
        "expires_at": pending.expires_at,
        "result": _result_snapshot(pending.result) if pending.result else None,
    }


def wait_timeout_seconds(context: Any) -> int:
    """等待上限：``min(玩家投掷超时, ai_request_timeout - 5)``。"""
    state = getattr(context, "tool_state", None)
    configured = 120
    if isinstance(state, dict) and state.get("player_dice_roll_timeout"):
        try:
            configured = int(state["player_dice_roll_timeout"])
        except (TypeError, ValueError):
            configured = 120
    try:
        from trpg_server.agents.dice_settings import admin_dice_settings

        ai_timeout = int(admin_dice_settings(getattr(context, "config_dir", None)).ai_request_timeout)
    except Exception:  # noqa: BLE001
        ai_timeout = 300
    return max(5, min(configured, max(5, ai_timeout - 5)))


def _existing_pending(room_id: str) -> PendingRoll | None:
    with _LOCK:
        return next((item for item in _PENDING.values() if item.room_id == room_id and item.state == "pending"), None)


def wait_for_player_roll(
    context: Any,
    plan: dict[str, Any],
    resolver: Callable[[], dict[str, Any]],
    *,
    roller_user_id: Any = None,
    roller_name: str = "",
    tool_name: str = "",
) -> dict[str, Any]:
    """创建待投掷请求并阻塞等待玩家；返回解析后的工具结果。"""
    room_id = str(getattr(context, "room_id", "") or "")
    if not room_id or _BROADCASTER is None:
        # 测试 / 控制台 / 无 Socket 环境下立即投掷。
        return resolver()
    if _existing_pending(room_id) is not None:
        # 每房间同时只允许一个等待请求；已有请求时新的检定即时投掷。
        return _annotate(resolver(), "", auto=False, player_roll=False)

    request_id = f"roll-{uuid.uuid4().hex}"
    now = time.time()
    timeout = wait_timeout_seconds(context)
    user_id = str(getattr(context, "user_id", "") or "")
    pending = PendingRoll(
        request_id=request_id,
        room_id=room_id,
        room_dir=getattr(context, "room_dir", None),
        roller_user_id=str(roller_user_id if roller_user_id is not None else user_id),
        roller_name=roller_name or str(roller_name or roller_user_id or ""),
        tool_name=tool_name,
        plan=plan,
        resolver=resolver,
        created_at=now,
        expires_at=now + timeout,
    )
    with _LOCK:
        _PENDING[request_id] = pending
    _emit("player_dice_request", _plan_payload(pending))
    _log(pending.room_dir, f"等待玩家 {pending.roller_name} 投掷骰子", actor=pending.roller_name, room_id=room_id, 工具=tool_name, 请求ID=request_id)
    logger.info("Player roll request %s created in room %s for %s", request_id, room_id, pending.roller_name)

    finished = pending.event.wait(timeout)
    if not finished:
        # 超时兜底：服务端自动投掷。
        with pending.lock:
            if pending.state == "pending":
                pending.state = "timeout"
                result = pending.resolver()
                pending.result = _annotate(result, request_id, auto=True)
                pending.rolled_at = time.time()
        _emit("player_dice_rolled", {"room_id": room_id, "request_id": request_id, "auto": True, "reason": "timeout", **_result_snapshot(pending.result or {})})
        _log(pending.room_dir, "等待玩家投掷超时，系统自动投掷", actor=pending.roller_name, room_id=room_id, 请求ID=request_id)
        _remove(request_id)
        return pending.result or {}

    with pending.lock:
        result = pending.result
    _remove(request_id)
    if pending.state == "cancelled":
        cancel_message = str(plan.get("cancel_message") or "投掷被管理员取消")
        return {"cancelled": True, "summary": cancel_message, "request_id": request_id}
    return result or {}


def _remove(request_id: str) -> None:
    with _LOCK:
        _PENDING.pop(request_id, None)


def _find(room_id: str, request_id: str) -> PendingRoll | None:
    with _LOCK:
        pending = _PENDING.get(request_id)
    if pending is None or pending.room_id != room_id:
        return None
    return pending


def get_pending(room_id: str, request_id: str | None = None) -> dict[str, Any] | None:
    """查询房间内待投掷请求（不传 id 时返回最早的一个）。"""
    with _LOCK:
        candidates = [item for item in _PENDING.values() if item.room_id == room_id]
    if request_id:
        candidates = [item for item in candidates if item.request_id == request_id]
    candidates.sort(key=lambda item: item.created_at)
    if not candidates:
        return None
    pending = candidates[0]
    payload = _state_payload(pending)
    if pending.result:
        payload["result"] = _result_snapshot(pending.result)
    return payload


def list_pending(room_id: str | None = None) -> list[dict[str, Any]]:
    """列出待投掷请求（供控制台与诊断使用），只返回仍处于 pending 的项。"""
    with _LOCK:
        candidates = [
            item
            for item in _PENDING.values()
            if item.state == "pending" and (room_id is None or item.room_id == room_id)
        ]
    candidates.sort(key=lambda item: item.created_at)
    return [_state_payload(item) for item in candidates]


def _auto_complete(request_id: str) -> None:
    pending = None
    with _LOCK:
        candidate = _PENDING.get(request_id)
    pending = candidate
    if pending is None:
        return
    with pending.lock:
        if pending.state != "rolled":
            return
        pending.state = "finished"
    pending.event.set()
    _remove(request_id)
    logger.info("Player roll request %s auto-completed after %.0fs", request_id, AUTO_COMPLETE_SECONDS)


def resolve_roll(room_id: str, request_id: str, user_id: Any, *, force: bool = False, operator: str = "") -> tuple[dict[str, Any] | None, str]:
    """服务端 RNG 结算待投掷请求，返回 ``(result, error)``。"""
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state != "pending":
            return pending.result, "already_rolled"
        if not force and str(user_id) != pending.roller_user_id:
            return None, "forbidden"
        pending.state = "forced" if force else "rolled"
        result = pending.resolver()
        pending.result = _annotate(result, request_id, auto=force)
        pending.rolled_at = time.time()
    snapshot = _result_snapshot(result)
    if force:
        _emit("player_dice_forced", {"room_id": room_id, "request_id": request_id, "operator": operator, "request": _state_payload(pending), "result": snapshot})
        _log(pending.room_dir, f"{operator or '管理员'} 强制结算了待投掷请求", actor=operator, room_id=room_id, 请求ID=request_id)
    else:
        _emit("player_dice_rolled", {"room_id": room_id, "request_id": request_id, "auto": False, "player_roll": True, **snapshot})
    timer = threading.Timer(AUTO_COMPLETE_SECONDS, _auto_complete, args=(request_id,))
    timer.daemon = True
    timer.start()
    return result, ""


def complete_roll(room_id: str, request_id: str, user_id: Any = None) -> tuple[bool, str]:
    """前端动画结束回调；解除工具线程的阻塞。"""
    pending = _find(room_id, request_id)
    if pending is None:
        return False, "not_found"
    with pending.lock:
        if user_id is not None and str(user_id) != pending.roller_user_id:
            return False, "forbidden"
        pending.state = "finished"
    pending.event.set()
    _remove(request_id)
    return True, ""


def cancel_roll(room_id: str, request_id: str, operator: str = "") -> tuple[dict[str, Any] | None, str]:
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state != "pending":
            return None, "already_rolled"
        pending.state = "cancelled"
        pending.rolled_at = time.time()
        message = "投掷被管理员取消"
        pending.result = {"cancelled": True, "summary": message, "request_id": request_id}
    _emit("player_dice_cancelled", {"room_id": room_id, "request_id": request_id, "reason": "cancelled", "operator": operator})
    _log(pending.room_dir, f"{operator or '管理员'} 取消了待投掷请求", actor=operator, room_id=room_id, 请求ID=request_id)
    pending.event.set()
    _remove(request_id)
    return pending.result, ""


def cleanup_room(room_id: str, reason: str = "room_closed") -> int:
    """房间归档/删除时解除所有等待，返回清理数量。"""
    with _LOCK:
        items = [item for item in _PENDING.values() if item.room_id == room_id]
    for pending in items:
        with pending.lock:
            if pending.state == "pending":
                pending.state = "cancelled"
                pending.result = {"cancelled": True, "summary": "房间已关闭，投掷取消", "request_id": pending.request_id}
        _emit("player_dice_cancelled", {"room_id": room_id, "request_id": pending.request_id, "reason": reason, "operator": ""})
        pending.event.set()
        _remove(pending.request_id)
    return len(items)


def reroll_result(room_id: str, request_id: str) -> dict[str, Any] | None:
    """重连恢复用：已投掷但尚未完成时返回结果快照。"""
    pending = _find(room_id, request_id)
    if pending is None or pending.result is None:
        return None
    return _result_snapshot(pending.result)


# 供 REST 路由使用的公开别名。
result_snapshot = _result_snapshot
