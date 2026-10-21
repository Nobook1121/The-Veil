"""玩家投掷：内存态待投掷请求与阻塞等待。

Chat 请求线程在工具内部调用 :func:`wait_for_player_roll` 阻塞等待玩家点击，
前端通过 REST 调用 :func:`resolve_roll` / :func:`reroll` / :func:`confirm_roll`
/ :func:`complete_roll` 推进状态。本模块不依赖 Flask，广播函数由
``socket_events.py`` 注入。

状态机::

    pending ──roll──> rolled（重投关闭，5 秒后自动完成）
       │                 └─complete──> finished
       └──roll──> awaiting_confirm（重投开启）
                      ├─reroll──> awaiting_confirm
                      ├─confirm─> finished
                      └─expires_at 到达──> timeout（自动确认最后结果）

重投开启时 ``complete`` 不解除阻塞，只有 ``confirm`` 或 ``expires_at`` 到期才
让等待中的工具线程继续，保证 AI 拿到的是玩家最终确认的结果。
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from trpg_server.agents.dice_settings import (
    normalize_reroll_limit,
    normalize_reroll_mode,
    reroll_remaining,
)

logger = logging.getLogger(__name__)

AUTO_COMPLETE_SECONDS = 5.0
ACTIVE_STATES = ("pending", "rolled", "awaiting_confirm")
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
    state: str = "pending"  # pending/rolled/awaiting_confirm/finished/cancelled/timeout/forced
    result: dict[str, Any] | None = None
    rolled_at: float | None = None
    # 0=关闭，-1=无限，N=可重投 N 次。
    reroll_limit: int = 0
    reroll_mode: str = "off"
    rerolls_used: int = 0
    confirmed: bool = False
    # 物理骰面结果：由客户端上报骰子落定后朝上的面，服务端只做结算。
    face_resolver: Callable[[list[int]], dict[str, Any]] | None = None
    # AI 自动投掷：无需玩家点击，所有客户端收到请求后直接播放动画。
    auto_roll: bool = False
    throw_seed: int | None = None
    rolling: bool = False
    # 本次动画是重投产生的，结算时广播 player_dice_rerolled。
    pending_reroll: bool = False
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
        "groups": dice.get("groups") or [],
        "selected": dice.get("selected"),
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


def _annotate_final(result: dict[str, Any], pending: PendingRoll) -> dict[str, Any]:
    """把最终的重投状态写回工具 metadata，供 AI 与历史记录读取。"""
    if not isinstance(result, dict):
        return result
    message = result.get("visible_message")
    if isinstance(message, dict):
        metadata = message.setdefault("metadata", {})
        dice = metadata.get("dice_roll")
        if isinstance(dice, dict):
            dice["reroll"] = _reroll_state(pending)
            dice["confirmed"] = bool(pending.confirmed)
    return result


def _reroll_state(pending: PendingRoll) -> dict[str, Any]:
    return {
        "enabled": pending.reroll_limit != 0,
        "mode": pending.reroll_mode,
        "limit": pending.reroll_limit if pending.reroll_limit > 0 else 0,
        "used": pending.rerolls_used,
        "remaining": reroll_remaining(pending.reroll_mode, pending.reroll_limit, pending.rerolls_used),
    }


def _plan_payload(pending: PendingRoll) -> dict[str, Any]:
    return {
        "room_id": pending.room_id,
        "request_id": pending.request_id,
        "tool_name": pending.tool_name,
        "roller": {"user_id": pending.roller_user_id, "username": pending.roller_name},
        "check": pending.plan.get("check"),
        "plan": pending.plan,
        "reroll": _reroll_state(pending),
        "expires_at": pending.expires_at,
        # 物理结果模式下客户端需要知道是否自动投掷、以及本次投掷的确定性种子。
        "physics": pending.face_resolver is not None,
        "auto": bool(pending.auto_roll),
        "seed": pending.throw_seed,
        "state": pending.state,
    }


def _state_payload(pending: PendingRoll) -> dict[str, Any]:
    return {
        "request_id": pending.request_id,
        "room_id": pending.room_id,
        "state": pending.state,
        "confirmed": pending.confirmed,
        "roller": {"user_id": pending.roller_user_id, "username": pending.roller_name},
        "plan": pending.plan,
        "tool_name": pending.tool_name,
        "reroll": _reroll_state(pending),
        "expires_at": pending.expires_at,
        "physics": pending.face_resolver is not None,
        "auto": bool(pending.auto_roll),
        "seed": pending.throw_seed,
        "result": _result_snapshot(pending.result) if pending.result else None,
    }


def reroll_config_for_context(context: Any) -> tuple[str, int]:
    """从工具上下文读取重投配置，返回 ``(mode, limit)``。

    ``limit`` 采用状态机编码：``0`` 关闭、``-1`` 无限、``N`` 指定次数。
    """
    state = getattr(context, "tool_state", None)
    if isinstance(state, dict):
        mode = normalize_reroll_mode(state.get("dice_reroll_mode"))
        raw = state.get("dice_rerolls")
        if raw is None:
            raw = state.get("dice_reroll_limit")
        try:
            limit = int(raw) if raw is not None else normalize_reroll_limit(None)
        except (TypeError, ValueError):
            limit = normalize_reroll_limit(None)
        if mode == "off":
            return "off", 0
        if mode == "unlimited":
            return "unlimited", -1
        return "limited", max(1, min(20, limit))
    try:
        from trpg_server.agents.dice_settings import resolve_dice_settings

        room_info = context.room_info() if hasattr(context, "room_info") else {}
        rules = room_info.get("house_rules") if isinstance(room_info, dict) else None
        settings = resolve_dice_settings(getattr(context, "config_dir", None), rules)
        return settings["reroll_mode"], int(settings["rerolls"])
    except Exception:  # noqa: BLE001
        return "off", 0


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


def _active_pending(room_id: str) -> PendingRoll | None:
    with _LOCK:
        return next(
            (item for item in _PENDING.values() if item.room_id == room_id and item.state in ACTIVE_STATES),
            None,
        )


def wait_for_player_roll(
    context: Any,
    plan: dict[str, Any],
    resolver: Callable[[], dict[str, Any]],
    *,
    roller_user_id: Any = None,
    roller_name: str = "",
    tool_name: str = "",
    face_resolver: Callable[[list[int]], dict[str, Any]] | None = None,
    auto_roll: bool = False,
) -> dict[str, Any]:
    """创建待投掷请求并阻塞等待玩家；返回解析后的工具结果。"""
    room_id = str(getattr(context, "room_id", "") or "")
    if not room_id or _BROADCASTER is None:
        # 测试 / 控制台 / 无 Socket 环境下立即投掷。
        return resolver()
    existing = _active_pending(room_id)
    if existing is not None:
        # 每房间同时只允许一个等待请求；重复检定直接返回错误，避免出现
        # 「第一次检定无结果、模型换参数再检定一次」的重复投掷。
        _log(
            existing.room_dir,
            f"拒绝重复检定：房间已有等待 {existing.roller_name} 投掷的请求",
            actor=roller_name or "",
            room_id=room_id,
            工具=tool_name,
            已有请求ID=existing.request_id,
            已有工具=existing.tool_name,
        )
        return {
            "error": "dice_request_pending",
            "summary": (
                f"房间中已有等待 {existing.roller_name or '玩家'} 投掷的检定，"
                "请等待该投掷完成后继续，不要重复发起检定。"
            ),
            "pending_request_id": existing.request_id,
        }

    request_id = f"roll-{uuid.uuid4().hex}"
    now = time.time()
    timeout = wait_timeout_seconds(context)
    user_id = str(getattr(context, "user_id", "") or "")
    mode, limit = reroll_config_for_context(context)
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
        reroll_mode=mode,
        reroll_limit=limit,
        face_resolver=face_resolver,
        auto_roll=bool(auto_roll) and face_resolver is not None,
    )
    with _LOCK:
        _PENDING[request_id] = pending
    _emit("player_dice_request", _plan_payload(pending))
    _log(pending.room_dir, f"等待玩家 {pending.roller_name} 投掷骰子", actor=pending.roller_name, room_id=room_id, 工具=tool_name, 请求ID=request_id)
    logger.info("Player roll request %s created in room %s for %s", request_id, room_id, pending.roller_name)

    finished = pending.event.wait(timeout)
    if not finished:
        _expire(request_id)

    with pending.lock:
        result = pending.result
        state = pending.state
    _annotate_final(result or {}, pending)
    _remove(request_id)
    if state == "cancelled":
        cancel_message = str(plan.get("cancel_message") or "投掷被管理员取消")
        return {"cancelled": True, "summary": cancel_message, "request_id": request_id}
    if result is None:
        # 绝不能让工具拿到空结果：空结果会让模型误以为检定失败并换参数重试。
        _log(
            pending.room_dir,
            "投掷请求结束但没有任何结果",
            actor=pending.roller_name,
            room_id=room_id,
            level="warning",
            请求ID=request_id,
            状态=state,
        )
        return {
            "error": "dice_request_closed",
            "summary": "本次投掷没有产生结果，请重新发起检定。",
            "request_id": request_id,
        }
    return result


def _expire(request_id: str) -> None:
    """等待超时：未投掷则自动投掷，已投掷则自动确认最后一次结果。"""
    pending = None
    with _LOCK:
        pending = _PENDING.get(request_id)
    if pending is None:
        return
    rolled = False
    with pending.lock:
        if pending.state == "pending":
            pending.state = "timeout"
            pending.result = _annotate(pending.resolver(), request_id, auto=True)
            pending.rolled_at = time.time()
            pending.confirmed = True
            rolled = True
        elif pending.state in ("rolled", "awaiting_confirm"):
            pending.state = "timeout"
            pending.confirmed = True
    if rolled:
        _emit(
            "player_dice_rolled",
            {
                "room_id": pending.room_id,
                "request_id": request_id,
                "auto": True,
                "fallback": True,
                "reason": "timeout",
                "state": pending.state,
                "reroll": _reroll_state(pending),
                **_result_snapshot(pending.result or {}),
            },
        )
        _log(pending.room_dir, "等待玩家投掷超时，系统自动投掷", actor=pending.roller_name, room_id=pending.room_id, 请求ID=request_id)
    else:
        _emit(
            "player_dice_confirmed",
            {
                "room_id": pending.room_id,
                "request_id": request_id,
                "reason": "timeout",
                "confirmed_by": "",
                "state": pending.state,
                "reroll": _reroll_state(pending),
                "result": _result_snapshot(pending.result or {}),
            },
        )
        _log(pending.room_dir, "等待玩家确认超时，自动确认最后一次结果", actor=pending.roller_name, room_id=pending.room_id, 请求ID=request_id)
    pending.event.set()


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
        candidates = [item for item in _PENDING.values() if item.room_id == room_id and item.state in ACTIVE_STATES]
    if request_id:
        candidates = [item for item in candidates if item.request_id == request_id]
    candidates.sort(key=lambda item: item.created_at)
    if not candidates:
        return None
    return _state_payload(candidates[0])


def list_pending(room_id: str | None = None) -> list[dict[str, Any]]:
    """列出仍在等待/待确认的请求（供控制台与诊断使用）。"""
    with _LOCK:
        candidates = [
            item
            for item in _PENDING.values()
            if item.state in ACTIVE_STATES and (room_id is None or item.room_id == room_id)
        ]
    candidates.sort(key=lambda item: item.created_at)
    return [_state_payload(item) for item in candidates]


def _auto_complete(request_id: str) -> None:
    """重投关闭时的兜底：动画结束回调丢失后 5 秒自动完成。"""
    with _LOCK:
        pending = _PENDING.get(request_id)
    if pending is None:
        return
    with pending.lock:
        if pending.state != "rolled":
            return
        pending.state = "finished"
        pending.confirmed = True
    pending.event.set()
    _remove(request_id)
    logger.info("Player roll request %s auto-completed after %.0fs", request_id, AUTO_COMPLETE_SECONDS)


def _start_auto_complete(request_id: str) -> None:
    timer = threading.Timer(AUTO_COMPLETE_SECONDS, _auto_complete, args=(request_id,))
    timer.daemon = True
    timer.start()


def resolve_roll(room_id: str, request_id: str, user_id: Any, *, force: bool = False, operator: str = "") -> tuple[dict[str, Any] | None, str]:
    """服务端 RNG 结算待投掷请求，返回 ``(result, error)``。"""
    if force:
        return force_roll(room_id, request_id, operator=operator)
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state != "pending":
            return pending.result, "already_rolled"
        if str(user_id) != pending.roller_user_id:
            return None, "forbidden"
        result = pending.resolver()
        pending.result = _annotate(result, request_id, auto=False)
        pending.rolled_at = time.time()
        pending.state = "awaiting_confirm" if pending.reroll_limit != 0 else "rolled"
        state = pending.state
        reroll = _reroll_state(pending)
    _emit(
        "player_dice_rolled",
        {
            "room_id": room_id,
            "request_id": request_id,
            "auto": False,
            "player_roll": True,
            "state": state,
            "reroll": reroll,
            **_result_snapshot(result),
        },
    )
    _log(
        pending.room_dir,
        f"玩家 {pending.roller_name} 投掷了骰子",
        actor=pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
        工具=pending.tool_name,
        结果=result.get("summary") if isinstance(result, dict) else "",
    )
    if state == "rolled":
        _start_auto_complete(request_id)
    return result, ""


def _new_seed() -> int:
    """生成 32 位物理投掷种子；所有客户端用它推导完全一致的初始状态。"""
    return random.SystemRandom().randrange(1, 2**32)


def begin_roll(
    room_id: str,
    request_id: str,
    user_id: Any,
    *,
    reroll: bool = False,
    force: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """开始一次动画投掷：生成确定性种子并广播 ``player_dice_rolling``。

    物理结果模式下，真正的点数由客户端在骰子落定后上报；本函数只负责
    同步开始信号，让所有在线客户端播放同一段动画。
    """
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if reroll:
            if pending.state == "pending":
                return None, "not_rolled"
            if pending.state not in ("rolled", "awaiting_confirm"):
                return None, "already_finished"
            if not force and str(user_id) != pending.roller_user_id:
                return None, "forbidden"
            if pending.reroll_limit == 0:
                return None, "reroll_disabled"
            if pending.reroll_limit > 0 and pending.rerolls_used >= pending.reroll_limit:
                return None, "reroll_exhausted"
            pending.rerolls_used += 1
            pending.pending_reroll = True
            pending.result = None
            pending.confirmed = False
            pending.state = "pending"
        else:
            if pending.state != "pending":
                return None, "already_rolled"
            if not force and str(user_id) != pending.roller_user_id:
                return None, "forbidden"
        pending.throw_seed = _new_seed()
        pending.rolling = True
        payload = {
            **_plan_payload(pending),
            "seed": pending.throw_seed,
            "reroll_index": pending.rerolls_used if pending.pending_reroll else 0,
        }
    _emit("player_dice_rolling", payload)
    _log(
        pending.room_dir,
        f"{pending.roller_name} 开始投掷骰子",
        actor=pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
        工具=pending.tool_name,
        种子=pending.throw_seed,
        重投=bool(reroll),
    )
    return payload, ""


def submit_faces(
    room_id: str,
    request_id: str,
    user_id: Any,
    faces: list[int],
    *,
    fallback: bool = False,
    force: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """接收客户端读取的骰面并结算，返回 ``(result, error)``。

    ``fallback=True`` 时跳过骰面，改用服务端旧随机数生成器（纯文本/兜底）。
    """
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state != "pending":
            # 幂等：重复上报按已投掷处理，避免网络重试把结果再广播一次。
            return pending.result, "already_rolled"
        if not force and str(user_id) != pending.roller_user_id:
            return None, "forbidden"
        resolver = pending.face_resolver
        if fallback or resolver is None:
            result = pending.resolver()
        else:
            try:
                result = resolver(faces)
            except (TypeError, ValueError):
                logger.warning("Invalid dice faces for request %s: %s", request_id, faces)
                return None, "invalid_faces"
        pending.rolled_at = time.time()
        pending.state = "awaiting_confirm" if pending.reroll_limit != 0 else "rolled"
        pending.rolling = False
        rerolled = pending.pending_reroll
        pending.pending_reroll = False
        index = pending.rerolls_used
        state = pending.state
        reroll_state = _reroll_state(pending)
        pending.result = _annotate_final(_annotate(result, request_id, auto=False), pending)
    snapshot = _result_snapshot(result)
    if rerolled:
        _emit(
            "player_dice_rerolled",
            {
                "room_id": room_id,
                "request_id": request_id,
                "player_roll": True,
                "fallback": bool(fallback or resolver is None),
                "reroll_index": index,
                "state": state,
                "reroll": reroll_state,
                "faces": faces if not fallback else [],
                **snapshot,
            },
        )
        _log(
            pending.room_dir,
            f"玩家 {pending.roller_name} 重投第 {index} 次",
            actor=pending.roller_name,
            room_id=room_id,
            请求ID=request_id,
            骰面=faces if not fallback else "服务端随机",
            剩余=reroll_state["remaining"],
        )
        return result, ""
    _emit(
        "player_dice_rolled",
        {
            "room_id": room_id,
            "request_id": request_id,
            "auto": False,
            "player_roll": True,
            "fallback": bool(fallback or resolver is None),
            "state": state,
            "reroll": reroll_state,
            "faces": faces if not fallback else [],
            **snapshot,
        },
    )
    _log(
        pending.room_dir,
        f"玩家 {pending.roller_name} 投掷了骰子",
        actor=pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
        工具=pending.tool_name,
        骰面=faces if not fallback else "服务端随机",
        结果=result.get("summary") if isinstance(result, dict) else "",
    )
    if state == "rolled":
        _start_auto_complete(request_id)
    return result, ""


def reroll(room_id: str, request_id: str, user_id: Any) -> tuple[dict[str, Any] | None, str]:
    """重投：校验身份与剩余次数后重新 RNG，返回 ``(result, error)``。

    物理结果模式请改用 :func:`begin_roll` 的 ``reroll`` 参数，让客户端重新
    播放动画并上报骰面；本函数保留给纯文本与旧生成器兜底。
    """
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state == "pending":
            return None, "not_rolled"
        if pending.state not in ("rolled", "awaiting_confirm"):
            return None, "already_finished"
        if str(user_id) != pending.roller_user_id:
            return None, "forbidden"
        if pending.reroll_limit == 0:
            return None, "reroll_disabled"
        if pending.reroll_limit > 0 and pending.rerolls_used >= pending.reroll_limit:
            return None, "reroll_exhausted"
        pending.rerolls_used += 1
        result = pending.resolver()
        pending.result = _annotate(result, request_id, auto=False)
        pending.rolled_at = time.time()
        pending.state = "awaiting_confirm"
        index = pending.rerolls_used
        reroll_state = _reroll_state(pending)
    _emit(
        "player_dice_rerolled",
        {
            "room_id": room_id,
            "request_id": request_id,
            "player_roll": True,
            "reroll_index": index,
            "state": "awaiting_confirm",
            "reroll": reroll_state,
            **_result_snapshot(result),
        },
    )
    _log(
        pending.room_dir,
        f"玩家 {pending.roller_name} 重投第 {index} 次",
        actor=pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
        剩余=reroll_state["remaining"],
    )
    return result, ""


def confirm_roll(room_id: str, request_id: str, user_id: Any = None, *, reason: str = "player", operator: str = "") -> tuple[dict[str, Any] | None, str]:
    """玩家确认最终结果；立即解除工具线程阻塞。"""
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.result is None:
            return None, "not_rolled"
        if user_id is not None and str(user_id) != pending.roller_user_id:
            return None, "forbidden"
        if pending.confirmed:
            # 重复确认幂等返回当前结果。
            return pending.result, ""
        pending.confirmed = True
        pending.state = "finished"
        result = pending.result
        reroll_state = _reroll_state(pending)
    _emit(
        "player_dice_confirmed",
        {
            "room_id": room_id,
            "request_id": request_id,
            "reason": reason,
            "confirmed_by": operator or pending.roller_name,
            "state": "finished",
            "reroll": reroll_state,
            "result": _result_snapshot(result),
        },
    )
    _log(
        pending.room_dir,
        f"玩家 {pending.roller_name} 确认了最终投掷结果",
        actor=operator or pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
        重投次数=pending.rerolls_used,
    )
    pending.event.set()
    _remove(request_id)
    return result, ""


def force_roll(room_id: str, request_id: str, *, operator: str = "") -> tuple[dict[str, Any] | None, str]:
    """管理员强制：跳过目标玩家校验，自动投掷并立即确认。"""
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state == "pending":
            pending.result = _annotate(pending.resolver(), request_id, auto=True)
            pending.rolled_at = time.time()
        elif pending.result is None:
            return None, "not_rolled"
        pending.confirmed = True
        pending.state = "forced"
        result = pending.result
        reroll_state = _reroll_state(pending)
    snapshot = _result_snapshot(result)
    _emit(
        "player_dice_forced",
        {
            "room_id": room_id,
            "request_id": request_id,
            "operator": operator,
            "fallback": True,
            "state": "forced",
            "reroll": reroll_state,
            "request": _state_payload(pending),
            "result": snapshot,
            **snapshot,
        },
    )
    _log(pending.room_dir, f"{operator or '管理员'} 强制结算了待投掷请求", actor=operator, room_id=room_id, 请求ID=request_id)
    pending.event.set()
    _remove(request_id)
    return result, ""


def complete_roll(room_id: str, request_id: str, user_id: Any = None) -> tuple[bool, str]:
    """前端动画结束回调。

    重投关闭时等价于确认并解除阻塞；重投开启时仅在已确认后幂等成功，
    否则保持等待（必须由 ``confirm`` 或超时结束）。

    尚未投掷（``pending``）时拒绝结束：否则工具线程会拿到空结果并返回空
    摘要，模型会误以为检定失败而重复发起检定。
    """
    pending = _find(room_id, request_id)
    if pending is None:
        return False, "not_found"
    with pending.lock:
        if user_id is not None and str(user_id) != pending.roller_user_id:
            return False, "forbidden"
        if pending.confirmed or pending.state in ("finished", "forced"):
            return True, ""
        if pending.result is None or pending.state == "pending":
            reason = "not_rolled"
        elif pending.reroll_limit != 0:
            # 重投开启：动画结束不解除阻塞，等待玩家确认。
            return True, ""
        else:
            pending.confirmed = True
            pending.state = "finished"
            reason = ""
    if reason:
        _log(
            pending.room_dir,
            "忽略动画结束回调：投掷尚未进行",
            actor=pending.roller_name,
            room_id=room_id,
            level="warning",
            请求ID=request_id,
            状态=pending.state,
        )
        return False, reason
    _log(
        pending.room_dir,
        f"玩家 {pending.roller_name} 的投掷已结束",
        actor=pending.roller_name,
        room_id=room_id,
        请求ID=request_id,
    )
    pending.event.set()
    _remove(request_id)
    return True, ""


def cancel_roll(room_id: str, request_id: str, operator: str = "") -> tuple[dict[str, Any] | None, str]:
    pending = _find(room_id, request_id)
    if pending is None:
        return None, "not_found"
    with pending.lock:
        if pending.state not in ACTIVE_STATES:
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
            if pending.state in ACTIVE_STATES:
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
