"""房间级游戏时钟：只做时间存取与调度，不含任何规则数学。

设计约定（与用户裁决一致）：
- 时钟按**服务器时间**计算，不虚构纪元；``current`` 是带服务器时区偏移的
  ISO 8601 时间串，``elapsed_seconds`` 记录本房间累计推进的秒数。
- 存储位置是房间 ``info.json`` 的 ``game_clock`` 键；开关是房规
  ``coc7_game_clock``（默认关闭）。关闭时本模块的读接口一律返回 ``None``，
  写接口直接报错，**不写盘**；没有该字段的旧房间在开关关闭时也按未启用处理。
- 只允许向前推进：``advance`` / ``set`` 的目标时刻必须晚于当前时刻。
- 并发：``room_lock`` 是**房间级**统一锁（模块级 ``RLock`` + ``info.json.lock``
  文件锁，同线程可重入），覆盖同进程多线程与多 worker / 多进程。时钟、毒剂队列
  和所有 ``info.json`` 写入共用它；写入方在锁内重读磁盘，按时钟 ``revision``
  合并回更新的 ``game_clock``，因此“先读整份 info、后整份写回”的路径不会把
  时间推进覆盖掉。需要强校验时调用方可传 ``expected_current`` 做乐观并发检查。
- ``correct`` 是唯一的回拨入口（仅房主），保留 before/after、操作者与原因，
  并写房间日志；``advance`` / ``set`` 仍然只允许向前。
- 首次推进时如果开关已开启但没有时钟字段，才以服务器当前时间惰性初始化；
  纯查询不写盘。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from trpg_server import room_rules
from trpg_server.json_store import read_json

# 固定本地时区：用显式 tzinfo 做换算，避免 Windows 上 ``astimezone()`` /
# ``localtime`` 处理 1970 年前的游戏时间时抛 OSError。
_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

CLOCK_KEY = "game_clock"
# 唯一开关：CoC7 房规里默认关闭的布尔项。核心只读取这个布尔值。
ENABLED_RULE_KEY = "coc7_game_clock"

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY

# 校正记录只保留最近若干条，避免房间 info.json 无限增长。
MAX_CORRECTIONS = 20

# 跨进程锁参数：锁文件是 ``info.json.lock``，用 O_EXCL 创建实现互斥。
# 它是**房间级**状态锁：时钟、毒剂队列以及所有 info.json 写入共用同一把锁。
LOCK_FILENAME = "info.json.lock"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05

# 进程内锁：同一 worker 的线程先串行，再竞争跨进程文件锁。
_LOCK = threading.RLock()
# 当前线程已持有的房间锁（规范化路径 -> 重入计数）。
_HELD = threading.local()


class ClockError(Exception):
    """时钟操作失败；``code`` 供调用方区分未启用 / 并发冲突 / 参数错误。"""

    def __init__(self, message: str, *, code: str = "clock_error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# 跨进程文件锁
# ---------------------------------------------------------------------------
def _lock_path(room_dir: Any) -> Path:
    return Path(room_dir) / LOCK_FILENAME


def _lock_age(path: Path) -> float | None:
    """锁文件存在时长（秒）；文件不存在或不可读返回 ``None``。"""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _read_lock_token(path: Path) -> str | None:
    """读取锁文件里的持有者 token；文件不存在 / 不可读返回 ``None``。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _remove_lock_if_owner(path: Path, token: str) -> bool:
    """仅当锁文件内容仍是 ``token`` 时删除，避免误删后来者新建的锁（ABA）。"""
    if _read_lock_token(path) != token:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Windows 上锁文件可能正被杀毒软件短暂占用；保留它，等超时 / 陈旧清理。
        return False


@contextlib.contextmanager
def _file_lock(room_dir: Any) -> Iterator[None]:
    """房间级跨进程互斥锁。

    用 ``O_CREAT | O_EXCL`` 创建 ``info.json.lock`` 实现；锁文件内容写入
    ``pid + uuid`` 作为唯一 token。获取不到时轮询等待，超过
    ``LOCK_TIMEOUT_SECONDS`` 抛 ``clock_busy``。锁文件 mtime 超过
    ``LOCK_STALE_SECONDS`` 视为进程异常退出残留，仅当 token 未变化时才清理。
    """
    path = _lock_path(room_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClockError(f"无法创建房间目录以加锁：{exc}", code="clock_lock_failed") from exc

    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    token = f"{os.getpid()} {uuid4().hex}"
    fd: int | None = None
    acquired = False
    try:
        while fd is None:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ClockError(
                        "游戏时钟正在被其他操作占用，请稍后重试", code="clock_busy"
                    )
                age = _lock_age(path)
                if age is None:
                    # stat 失败通常是刚被其他进程释放；同样受 deadline 约束地轮询，
                    # 否则会空转占满 CPU。
                    time.sleep(LOCK_POLL_SECONDS)
                    continue
                if age > LOCK_STALE_SECONDS:
                    stale_token = _read_lock_token(path)
                    if stale_token is not None and _remove_lock_if_owner(path, stale_token):
                        continue
                time.sleep(LOCK_POLL_SECONDS)
            except OSError as exc:
                raise ClockError(f"无法创建时钟文件锁：{exc}", code="clock_lock_failed") from exc
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
            fd = None
        acquired = True
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if acquired:
            _remove_lock_if_owner(path, token)


def _held_paths() -> dict[str, int]:
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = {}
        _HELD.paths = held
    return held


def _room_lock_key(room_dir: Any) -> str:
    return os.path.normcase(os.path.abspath(str(Path(room_dir))))


@contextlib.contextmanager
def room_lock(room_dir: Any) -> Iterator[None]:
    """统一的房间状态锁（进程内重入 + 跨进程文件锁）。

    ``info.json``、游戏时钟与毒剂队列的所有读改写都必须走这里。同一线程可重入
    （例如时钟在锁内调用 ``save_room_info``），不同线程 / 进程按房间串行。
    调用约定：持有本锁时不要再获取角色卡锁；需要同时持有时必须先角色卡锁、
    后房间锁，以保证全局顺序一致、单进程内不死锁。
    """
    key = _room_lock_key(room_dir)
    with _LOCK:
        held = _held_paths()
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
                if held[key] <= 0:
                    held.pop(key, None)
            return
        with _file_lock(room_dir):
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)


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
    try:
        revision = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    corrections = raw.get("corrections")
    history = (
        [dict(item) for item in corrections if isinstance(item, Mapping)]
        if isinstance(corrections, list)
        else []
    )
    return {
        "started_at": format_moment(started),
        "current": format_moment(current),
        "elapsed_seconds": elapsed,
        "revision": revision,
        "updated_at": str(raw.get("updated_at") or "") or None,
        "updated_by": str(raw.get("updated_by") or "") or None,
        "corrections": history[-MAX_CORRECTIONS:],
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


def san_now(info: Mapping[str, Any] | None) -> tuple[float, str, str]:
    """SAN 计时的统一 now 口径，返回 ``(epoch 秒, 记录文本, "game"|"wall")``。

    房规 ``coc7_game_clock`` 开启且时钟已初始化时使用游戏时间；否则显式回退
    服务器时间。发作开始、到期判断、免疫判断与 ``expire_bouts`` 必须共用这个
    结果，避免把墙钟写入的 ``started_at`` 和游戏时钟的当前值混在一起比较。
    """
    clock = read_clock(info)
    if clock is not None:
        moment = parse_moment(clock.get("current"))
        if moment is not None:
            # 文本统一换算到服务器本地时区，和 `sanity_state.parse_ts` 的解释口径一致；
            # 时钟记录带别的时区偏移时也不会把时间写歪。
            return moment.timestamp(), moment.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"), "game"
    now = server_now()
    return now.timestamp(), now.strftime("%Y-%m-%d %H:%M:%S"), "wall"


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
        "revision": 0,
        "updated_at": moment,
        "updated_by": str(actor or "") or None,
        "corrections": [],
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


def _clock_revision(raw: Any) -> int:
    """时钟写回版本号；缺失 / 非法按 0 处理，非时钟值按 -1 处理。"""
    if not isinstance(raw, Mapping):
        return -1
    try:
        return max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _bump_revision(clock: dict[str, Any]) -> None:
    clock["revision"] = _clock_revision(clock) + 1


def preserve_stored_clock(room_dir: Any, info: dict[str, Any]) -> bool:
    """锁内把磁盘上更新的 ``game_clock`` 合并回待写快照。

    写入方（加入房间、写角色卡、保存房规等）通常先读后写整份 ``info.json``；
    如果期间时钟已被推进，直接用旧快照落盘会把时间覆盖回去。这里在房间锁内
    重读磁盘，按 ``revision`` 判断哪份时钟更新；磁盘更新则覆盖快照中的时钟。
    返回是否发生了覆盖。调用方必须已持有 :func:`room_lock`。
    """
    stored = read_json(Path(room_dir) / "info.json", default={})
    stored_clock = stored.get(CLOCK_KEY) if isinstance(stored, Mapping) else None
    if _clock_revision(stored_clock) > _clock_revision(info.get(CLOCK_KEY)):
        info[CLOCK_KEY] = dict(stored_clock)
        return True
    return False


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

    with room_lock(room_dir):
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
        _bump_revision(clock)
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

    with room_lock(room_dir):
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
        _bump_revision(clock)
        return _store(room_dir, info, clock)


def correct_clock(
    room_dir: Any,
    moment: Any,
    *,
    actor: Any = None,
    actor_id: Any = None,
    reason: Any = None,
    expected_current: Any = None,
) -> dict[str, Any]:
    """房主校正：把游戏时间改到任意时刻（含回拨），保留审计记录并写房间日志。

    与 ``advance`` / ``set`` 不同，这里允许目标早于当前时刻；``elapsed_seconds``
    按差值累加（回拨后不低于 0，仅用于“这一局过了多久”的累计口径）。
    """
    from trpg_server.room_log import log_room_event

    text_reason = str(reason or "").strip()
    if not text_reason:
        raise ClockError(
            "time correct 必须提供原因（用法：time correct <时刻> reason <原因>）",
            code="reason_required",
        )
    target = parse_moment(moment)
    if target is None:
        raise ClockError(
            "时间格式无法解析，请用 2026-11-10 或 2026-11-10 15:30", code="invalid_moment"
        )

    with room_lock(room_dir):
        info = room_rules.load_room_info(room_dir)
        if not is_enabled(info):
            raise ClockError("本房间未启用游戏时钟（房规 coc7_game_clock）", code="clock_disabled")
        now = server_now()
        clock = _load_clock_for_mutation(
            info, now=now, actor=actor, expected_current=expected_current
        )
        current = parse_moment(clock["current"]) or now
        delta = int((target - current).total_seconds())
        clock["current"] = format_moment(target)
        clock["elapsed_seconds"] = max(
            0, int(clock.get("elapsed_seconds") or 0) + delta
        )
        clock["updated_at"] = format_moment(now)
        clock["updated_by"] = str(actor or "") or None
        history = clock.get("corrections")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "at": format_moment(now),
                "before": format_moment(current),
                "after": format_moment(target),
                "delta_seconds": delta,
                "actor": str(actor or "") or None,
                "actor_id": str(actor_id or "") or None,
                "reason": text_reason[:200],
            }
        )
        clock["corrections"] = history[-MAX_CORRECTIONS:]
        _bump_revision(clock)
        stored = _store(room_dir, info, clock)

    log_room_event(
        Path(room_dir),
        f"游戏时间校正：{format_display(current)} → {format_display(target)}"
        f"（{text_reason}）",
        level="warning",
        source="time",
        actor=str(actor or "") or None,
        before=format_moment(current),
        after=format_moment(target),
        delta_seconds=delta,
        actor_id=str(actor_id or "") or None,
        reason=text_reason[:200],
    )
    return stored


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
