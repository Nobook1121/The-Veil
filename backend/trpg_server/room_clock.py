"""房间级游戏时钟：只做时间存取与调度，不含任何规则数学。

设计约定（与用户裁决一致）：
- 时钟按**服务器时间**计算，不虚构纪元；``current`` 是带服务器时区偏移的
  ISO 8601 时间串，``elapsed_seconds`` 记录本房间累计推进的秒数。
- 存储位置是房间 ``info.json`` 的 ``game_clock`` 键；开关是房规
  ``coc7_game_clock``（默认关闭）。关闭时本模块的读接口一律返回 ``None``，
  写接口直接报错，**不写盘**；没有该字段的旧房间在开关关闭时也按未启用处理。
- 只允许向前推进：``advance`` / ``set`` 的目标时刻必须晚于当前时刻。
- 并发：模块级 ``RLock`` + 在锁内重新读取 ``info.json``，两个管理员同时推进时
  增量会串行累加，不会互相覆盖；需要强校验时调用方可传 ``expected_current``
  做乐观并发检查（不一致时报错而不是静默覆盖）。
- 首次推进时如果开关已开启但没有时钟字段，才以服务器当前时间惰性初始化；
  纯查询不写盘。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from trpg_server import room_rules

CLOCK_KEY = "game_clock"
# 唯一开关：CoC7 房规里默认关闭的布尔项。核心只读取这个布尔值。
ENABLED_RULE_KEY = "coc7_game_clock"

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY

_LOCK = threading.RLock()


class ClockError(Exception):
    """时钟操作失败；``code`` 供调用方区分未启用 / 并发冲突 / 参数错误。"""

    def __init__(self, message: str, *, code: str = "clock_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _true(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_enabled(info: Mapping[str, Any] | None) -> bool:
    """房间是否启用游戏时钟（房规 ``coc7_game_clock``，默认关闭）。"""
    data = info if isinstance(info, Mapping) else {}
    rules = data.get("house_rules")
    if not isinstance(rules, Mapping):
        return False
    return _true(rules.get(ENABLED_RULE_KEY, False))


def server_timezone() -> Any:
    return datetime.now().astimezone().tzinfo


def server_now() -> datetime:
    return datetime.now().astimezone()


def format_moment(moment: datetime) -> str:
    """统一输出带时区偏移的秒级 ISO 8601。"""
    return moment.isoformat(timespec="seconds")


def parse_moment(value: Any) -> datetime | None:
    """解析时间串；缺失 / 不可解析返回 ``None``（不凭空补时间）。

    不带时区的输入按服务器时区解释，保持与写入格式一致。
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=server_timezone())
    return moment


def format_display(value: Any) -> str:
    moment = parse_moment(value)
    if moment is None:
        return "-"
    offset = moment.strftime("%z")
    label = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')}（{label}）"


def _normalize(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    current = parse_moment(raw.get("current"))
    if current is None:
        return None
    started = parse_moment(raw.get("started_at")) or current
    try:
        elapsed = max(0, int(raw.get("elapsed_seconds") or 0))
    except (TypeError, ValueError):
        elapsed = 0
    return {
        "started_at": format_moment(started),
        "current": format_moment(current),
        "elapsed_seconds": elapsed,
        "updated_at": str(raw.get("updated_at") or "") or None,
        "updated_by": str(raw.get("updated_by") or "") or None,
    }


def read_clock(info: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """只读查询；未启用 / 字段缺失 / 不可解析都返回 ``None``，绝不写盘。"""
    if not is_enabled(info):
        return None
    data = info if isinstance(info, Mapping) else {}
    return _normalize(data.get(CLOCK_KEY))


def read_stored_clock(info: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """不管开关、只按字段读取（归档时房规已被移除，仍需快照游戏时间）。"""
    data = info if isinstance(info, Mapping) else {}
    return _normalize(data.get(CLOCK_KEY))


def current_seconds(info: Mapping[str, Any] | None) -> int | None:
    clock = read_clock(info)
    if clock is None:
        return None
    moment = parse_moment(clock["current"])
    return int(moment.timestamp()) if moment is not None else None


def elapsed_units(
    info: Mapping[str, Any] | None,
    since: Any,
    *,
    unit_seconds: int = SECONDS_PER_DAY,
) -> int | None:
    """从 ``since`` 到当前游戏时间的整单位数（向下取整）。

    时钟未启用、``since`` 缺失或不可解析时返回 ``None``，由调用方决定
    “不凭空补时间”的保守行为；未来时间按 0 处理。
    """
    start = parse_moment(since)
    now_seconds = current_seconds(info)
    if start is None or now_seconds is None or unit_seconds <= 0:
        return None
    return max(0, (now_seconds - int(start.timestamp())) // unit_seconds)


def clock_days_since(info: Mapping[str, Any] | None, since: Any) -> int | None:
    return elapsed_units(info, since, unit_seconds=SECONDS_PER_DAY)


def clock_weeks_since(info: Mapping[str, Any] | None, since: Any) -> int | None:
    return elapsed_units(info, since, unit_seconds=SECONDS_PER_WEEK)


def _new_clock(now: datetime, actor: Any) -> dict[str, Any]:
    moment = format_moment(now)
    return {
        "started_at": moment,
        "current": moment,
        "elapsed_seconds": 0,
        "updated_at": moment,
        "updated_by": str(actor or "") or None,
    }


def _load_clock_for_mutation(
    info: dict[str, Any],
    *,
    now: datetime,
    actor: Any,
    expected_current: Any = None,
) -> dict[str, Any]:
    raw = info.get(CLOCK_KEY)
    if not isinstance(raw, dict) or parse_moment(raw.get("current")) is None:
        # 开关已开启但旧房间没有时钟：此时才以服务器当前时间惰性初始化。
        return _new_clock(now, actor)
    clock = _normalize(raw) or _new_clock(now, actor)
    if expected_current is not None and str(clock["current"]) != str(expected_current):
        raise ClockError(
            "游戏时间已被其他操作推进，请刷新后重试（期望 "
            f"{format_display(expected_current)}，实际 {format_display(clock['current'])}）",
            code="clock_conflict",
        )
    return clock


def _store(room_dir: Any, info: dict[str, Any], clock: dict[str, Any]) -> dict[str, Any]:
    info[CLOCK_KEY] = clock
    room_rules.save_room_info(Path(room_dir), info)
    return clock


def advance_clock(
    room_dir: Any,
    *,
    days: int = 0,
    hours: int = 0,
    seconds: int = 0,
    actor: Any = None,
    expected_current: Any = None,
) -> dict[str, Any]:
    """把游戏时间向前推进给定的天数 / 小时 / 秒数；只允许正增量。"""
    delta_seconds = (
        int(days) * SECONDS_PER_DAY + int(hours) * SECONDS_PER_HOUR + int(seconds)
    )
    if delta_seconds <= 0:
        raise ClockError("推进量必须是正的（用法：time advance <N>d 或 <N>h）", code="invalid_delta")

    with _LOCK:
        info = room_rules.load_room_info(room_dir)
        if not is_enabled(info):
            raise ClockError("本房间未启用游戏时钟（房规 coc7_game_clock）", code="clock_disabled")
        now = server_now()
        clock = _load_clock_for_mutation(
            info, now=now, actor=actor, expected_current=expected_current
        )
        current = parse_moment(clock["current"]) or now
        clock["current"] = format_moment(current + timedelta(seconds=delta_seconds))
        clock["elapsed_seconds"] = int(clock.get("elapsed_seconds") or 0) + delta_seconds
        clock["updated_at"] = format_moment(now)
        clock["updated_by"] = str(actor or "") or None
        return _store(room_dir, info, clock)


def set_clock(
    room_dir: Any,
    moment: Any,
    *,
    actor: Any = None,
    expected_current: Any = None,
) -> dict[str, Any]:
    """把游戏时间设到某个绝对时刻；目标必须晚于当前时间。"""
    target = parse_moment(moment)
    if target is None:
        raise ClockError(
            "时间格式无法解析，请用 2026-11-10 或 2026-11-10 15:30", code="invalid_moment"
        )

    with _LOCK:
        info = room_rules.load_room_info(room_dir)
        if not is_enabled(info):
            raise ClockError("本房间未启用游戏时钟（房规 coc7_game_clock）", code="clock_disabled")
        now = server_now()
        clock = _load_clock_for_mutation(
            info, now=now, actor=actor, expected_current=expected_current
        )
        current = parse_moment(clock["current"]) or now
        delta = int((target - current).total_seconds())
        if delta <= 0:
            raise ClockError(
                f"游戏时间只能向前推进：当前 {format_display(clock['current'])}",
                code="clock_backwards",
            )
        clock["current"] = format_moment(target)
        clock["elapsed_seconds"] = int(clock.get("elapsed_seconds") or 0) + delta
        clock["updated_at"] = format_moment(now)
        clock["updated_by"] = str(actor or "") or None
        return _store(room_dir, info, clock)


def describe(info: Mapping[str, Any] | None) -> str:
    """给控制台 / AI 工具用的简短描述（只读）。"""
    if not is_enabled(info):
        return "本房间未启用游戏时钟（房规 coc7_game_clock）。"
    clock = read_clock(info)
    if clock is None:
        return "游戏时钟已启用，但尚未初始化；首次 time advance / time set 时会以服务器当前时间建立。"
    elapsed = int(clock["elapsed_seconds"])
    days, rest = divmod(elapsed, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    spent = f"{days} 天 {hours} 小时 {minutes} 分"
    return (
        f"当前游戏时间：{format_display(clock['current'])}\n"
        f"起始时间：{format_display(clock['started_at'])}（累计推进 {spent}）\n"
        f"最近更新：{clock.get('updated_at') or '-'}"
        + (f"（{clock['updated_by']}）" if clock.get("updated_by") else "")
    )
