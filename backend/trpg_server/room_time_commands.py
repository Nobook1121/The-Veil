"""房间控制台 ``time`` 域：游戏时钟的查询、推进与校正。

时钟本体在 :mod:`trpg_server.room_clock`（只做存取，不含规则数学）。本模块只负责：
- 解析 ``time show`` / ``time advance`` / ``time set`` / ``time correct``；
- 破坏性操作沿用房间控制台约定：缺省预演，``yes`` 或交互确认才落盘；
  ``time correct`` 只确认一次（命令末尾的 ``yes`` 或一次交互确认），不再连问两遍；
- 参数一律空格分隔的纯词，不接受 ``-`` / ``--`` 前缀；
- 推进后在同一处触发依赖时间的到期结算：毒剂（战斗内外）与不定性疯狂到期；
  校正只改时间，不触发结算；但会把已存在的发作 / 临时性疯狂计时平移同样
  的差值以保留剩余时长，避免回拨后永远不到期、超前时立刻到期。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Callable

from trpg_server import room_clock, room_rules

ROOM_TIME_ACTOR = "房间控制台"

ROOM_TIME_HELP = "\n".join(
    [
        "  time                        （查看本房间游戏时钟；需房规 coc7_game_clock）",
        "  time show                   （同上，只读）",
        "  time advance <N>d|<N>h [yes]（推进游戏时间；支持 3d / 12h / 3 days / 12 hours）",
        "  time set <YYYY-MM-DD[ HH:MM]> [yes]（把游戏时间设到指定时刻，只能向前）",
        "  time correct <YYYY-MM-DD[ HH:MM]> reason <原因> [yes]（房主校正，可回拨，写房间日志）",
        "  说明：推进 / 设置缺省为「预演」，加 yes 或交互确认才真正落盘；AI 没有推进时间的工具。",
        "  说明：参数一律空格分隔的纯词，开关写 yes，不用 - / -- 前缀。",
        "  说明：校正必须给出原因，只需确认一次，且不会触发毒剂 / 疯狂到期结算（发作计时会随校正平移）。",
    ]
)

_AMOUNT_RE = re.compile(r"^(?P<value>\d+)\s*(?P<unit>d|day|days|h|hour|hours)?$", re.IGNORECASE)
_UNIT_WORDS = {
    "d": "days",
    "day": "days",
    "days": "days",
    "h": "hours",
    "hour": "hours",
    "hours": "hours",
}


def _operator(actor_rank: Any) -> bool:
    return room_rules._actor_rank_value(actor_rank) >= 2


def _parse_amount(tokens: list[str]) -> tuple[int, int] | None:
    """把 ``3d`` / ``12h`` / ``3 days`` 解析成 (days, hours)；失败返回 ``None``。"""
    if not tokens:
        return None
    head = str(tokens[0]).strip()
    matched = _AMOUNT_RE.match(head)
    unit_text = ""
    value_text = ""
    if matched and matched.group("unit"):
        value_text = matched.group("value")
        unit_text = matched.group("unit").lower()
    elif head.isdigit():
        value_text = head
        if len(tokens) > 1:
            unit_text = str(tokens[1]).strip().lower()
    else:
        return None
    unit = _UNIT_WORDS.get(unit_text)
    if unit is None:
        return None
    value = int(value_text)
    if value <= 0:
        return None
    return (value, 0) if unit == "days" else (0, value)


def _preview_advance(info: dict[str, Any], days: int, hours: int) -> str:
    clock = room_clock.read_clock(info)
    if clock is None:
        return "游戏时钟尚未初始化；执行后将以服务器当前时间建立，并推进相应时长。"
    from datetime import timedelta

    current = room_clock.parse_moment(clock["current"])
    target = current + timedelta(days=days, hours=hours) if current else None
    target_text = room_clock.format_display(target) if target else "-"
    return (
        f"将把游戏时间从 {room_clock.format_display(clock['current'])} "
        f"推进到 {target_text}（+{days} 天 {hours} 小时）。"
    )


def _expire_bouts(room_dir: Path, room_id: Any, config_dir: Any) -> list[str]:
    """调用 SAN 侧的不定性疯狂到期收敛钩子；函数缺失或签名不符时静默跳过。"""
    try:
        from trpg_server import room_sanity
    except Exception:  # noqa: BLE001 - SAN 模块尚未落地时不影响时间推进
        return []
    expire = getattr(room_sanity, "expire_bouts", None)
    if not callable(expire):
        return []

    info = room_rules.load_room_info(room_dir)
    clock = room_clock.read_clock(info)
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(expire).parameters
    except (TypeError, ValueError):
        parameters = {}
    for name in parameters:
        if name in {"info", "room_info"}:
            kwargs[name] = info
        elif name == "room_dir":
            kwargs[name] = room_dir
        elif name == "room_id":
            kwargs[name] = room_id
        elif name == "config_dir":
            kwargs[name] = config_dir
        elif name == "now":
            kwargs[name] = room_clock.parse_moment((clock or {}).get("current"))
    try:
        result = expire(**kwargs)
    except Exception:  # noqa: BLE001 - 钩子内部异常不得阻断时间推进
        return []
    return _bout_messages(result)


def _bout_messages(result: Any) -> list[str]:
    """把钩子返回值收敛成日志行；未识别的类型按空处理。"""
    if result is None:
        return []
    if isinstance(result, str):
        return [result] if result.strip() else []
    if isinstance(result, (list, tuple)):
        return [str(item) for item in result if str(item).strip()]
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, str):
            return [messages] if messages.strip() else []
        if isinstance(messages, (list, tuple)):
            return [str(item) for item in messages if str(item).strip()]
        expired = result.get("expired")
        if isinstance(expired, (list, tuple)) and expired:
            return [f"不定性疯狂到期：{'、'.join(str(item) for item in expired)}"]
    return []


def _settle_after_time_change(room_dir: Path, room_id: Any, config_dir: Any) -> str:
    """时间向前推进后在同一处结算到期效果；失败不阻断时间推进。"""
    messages: list[str] = []
    try:
        from trpg_server.agents.tools import combat as combat_tool

        result = combat_tool.settle_poisons_after_time(
            room_dir, room_id=room_id, config_dir=config_dir
        )
        messages.extend(result.get("messages") or [])
    except Exception:  # noqa: BLE001 - 结算失败不能回滚已经推进的时间
        pass
    messages.extend(_expire_bouts(room_dir, room_id, config_dir))
    if not messages:
        return ""
    return "\n" + "\n".join(messages)


def handle_room_time_command(
    args: list[str],
    *,
    room_id: Any = None,
    room_dir: Any,
    actor: Any = None,
    actor_id: Any = None,
    actor_rank: Any = None,
    confirm: Callable[[str], bool] | None = None,
    config_dir: Any = None,
) -> tuple[bool, str]:
    positionals = [str(item) for item in args]
    sub = positionals[0].lower() if positionals else "show"
    rest = positionals[1:]
    room_dir = Path(room_dir)
    info = room_rules.load_room_info(room_dir)

    if sub in {"help", "?"}:
        return True, ROOM_TIME_HELP
    if sub not in {"show", "status", "advance", "set", "correct"}:
        return False, f"未知的时间命令：time {sub}\n{ROOM_TIME_HELP}"

    if sub in {"show", "status"}:
        return True, room_clock.describe(info)

    if sub == "correct":
        return _handle_correct(
            rest,
            room_dir=room_dir,
            info=info,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
            confirm=confirm,
        )

    if not _operator(actor_rank):
        return False, "只有房主 / 房间管理员可以推进游戏时间"
    if not room_clock.is_enabled(info):
        return False, "本房间未启用游戏时钟；请先在房规中开启「游戏时钟」（coc7_game_clock）"
    if any(item.startswith("-") for item in rest):
        return False, "参数请用空格分隔的纯词（例如 time advance 3d yes），不支持 - / -- 前缀"

    assume_yes = any(item.lower() == "yes" for item in rest)
    body = [item for item in rest if item.lower() != "yes"]

    if sub == "advance":
        parsed = _parse_amount(body)
        if parsed is None:
            return False, "用法：time advance <N>d 或 <N>h（例如 3d / 12h / 3 days）"
        days, hours = parsed
        preview = _preview_advance(info, days, hours)
        if not assume_yes and confirm is not None:
            if not confirm("推进游戏时间？(y/N)"):
                return True, preview + "\n（已取消，未推进）"
            assume_yes = True
        if not assume_yes:
            return True, preview + "\n（预演模式：确认无误后加 yes 执行）"
        try:
            clock = room_clock.advance_clock(
                room_dir, days=days, hours=hours, actor=actor
            )
        except room_clock.ClockError as exc:
            return False, exc.message
        return True, f"游戏时间推进完成：{room_clock.format_display(clock['current'])}" + _settle_after_time_change(
            room_dir, room_id, config_dir
        )

    # time set
    moment_text = " ".join(body).strip()
    if not moment_text:
        return False, "用法：time set <YYYY-MM-DD> 或 time set <YYYY-MM-DD HH:MM>"
    target = room_clock.parse_moment(moment_text)
    if target is None:
        return False, "时间格式无法解析，请用 2026-11-10 或 2026-11-10 15:30"
    preview = f"将把游戏时间设为 {room_clock.format_display(target)}（只能向前）。"
    if not assume_yes and confirm is not None:
        if not confirm("设置游戏时间？(y/N)"):
            return True, preview + "\n（已取消，未设置）"
        assume_yes = True
    if not assume_yes:
        return True, preview + "\n（预演模式：确认无误后加 yes 执行）"
    try:
        clock = room_clock.set_clock(room_dir, target, actor=actor)
    except room_clock.ClockError as exc:
        return False, exc.message
    return True, f"游戏时间设置完成：{room_clock.format_display(clock['current'])}" + _settle_after_time_change(
        room_dir, room_id, config_dir
    )


def _split_correct(rest: list[str]) -> tuple[str, str, bool]:
    """把 ``time correct`` 的参数拆成 (时刻文本, 原因, 是否自带 yes)。"""
    assume_yes = any(item.lower() == "yes" for item in rest)
    tokens = [item for item in rest if item.lower() != "yes"]
    marker = next(
        (index for index, item in enumerate(tokens) if item.lower() == "reason"), None
    )
    if marker is None:
        return " ".join(tokens).strip(), "", assume_yes
    return (
        " ".join(tokens[:marker]).strip(),
        " ".join(tokens[marker + 1 :]).strip(),
        assume_yes,
    )


def _handle_correct(
    rest: list[str],
    *,
    room_dir: Path,
    info: dict[str, Any],
    actor: Any,
    actor_id: Any,
    actor_rank: Any,
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    """``time correct``：仅房主，必须给原因，写房间日志，且不触发到期结算。"""
    if room_rules._actor_rank_value(actor_rank) < 3:
        return False, "只有房主可以校正游戏时间"
    if any(item.startswith("-") for item in rest):
        return False, (
            "参数请用空格分隔的纯词"
            "（例如 time correct 2026-11-10 reason 记错时间 yes），不支持 - / -- 前缀"
        )

    moment_text, reason, assume_yes = _split_correct(rest)
    if not moment_text or not reason:
        return False, (
            "用法：time correct <YYYY-MM-DD[ HH:MM]> reason <原因> [yes]\n"
            "（校正可回拨，必须记录原因；请先预演再确认）"
        )
    target = room_clock.parse_moment(moment_text)
    if target is None:
        return False, "时间格式无法解析，请用 2026-11-10 或 2026-11-10 15:30"
    if not room_clock.is_enabled(info):
        return False, "本房间未启用游戏时钟；请先在房规中开启「游戏时钟」（coc7_game_clock）"

    clock = room_clock.read_clock(info)
    current_text = clock["current"] if clock else None
    before = room_clock.parse_moment(current_text) if current_text else None
    delta_seconds = (target - before).total_seconds() if before is not None else 0.0
    preview = (
        f"将把游戏时间从 {room_clock.format_display(current_text)} "
        f"校正为 {room_clock.format_display(target)}；原因：{reason}\n"
        "该校正允许回拨，会写入房间日志，且不会触发毒剂 / 疯狂到期结算；"
        "已存在的发作与临时性疯狂会按差值平移计时，保留剩余时长。"
    )

    if not assume_yes:
        if confirm is None:
            return True, preview + "\n（预演模式：确认无误后加 yes 执行）"
        if not confirm("校正游戏时间？(y/N)"):
            return True, preview + "\n（已取消，未校正）"

    try:
        updated = room_clock.correct_clock(
            room_dir,
            target,
            actor=actor,
            actor_id=actor_id,
            reason=reason,
        )
    except room_clock.ClockError as exc:
        return False, exc.message
    extra = ""
    try:
        from trpg_server import room_sanity

        result = room_sanity.reanchor_bouts(room_dir, None, delta_seconds)
        if result.get("shifted"):
            extra = "\n" + str(result.get("summary") or "")
    except Exception:  # noqa: BLE001 - 计时平移失败不能回滚已校正的时间
        extra = ""
    return True, (
        f"游戏时间已校正：{room_clock.format_display(updated['current'])}"
        f"（原因：{reason}）{extra}"
    )
