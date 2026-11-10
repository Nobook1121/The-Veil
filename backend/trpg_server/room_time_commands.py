"""房间控制台 ``time`` 域：游戏时钟的查询与推进。

时钟本体在 :mod:`trpg_server.room_clock`（只做存取，不含规则数学）。本模块只负责：
- 解析 ``time show`` / ``time advance`` / ``time set``；
- 破坏性操作沿用房间控制台约定：缺省预演，``yes`` 或交互确认才落盘；
- 推进后触发依赖时间的到期结算（目前只有毒剂；恢复 / 典籍仍由显式命令触发）。
"""
from __future__ import annotations

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
        "  说明：推进 / 设置缺省为「预演」，加 yes 或交互确认才真正落盘；AI 没有推进时间的工具。",
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


def _settle_after_advance(room_dir: Path, room_id: Any, config_dir: Any) -> str:
    """时间推进后结算依赖时钟的到期效果；失败不阻断时间推进。"""
    try:
        from trpg_server.agents.tools import combat as combat_tool

        result = combat_tool.settle_poisons_after_time(
            room_dir, room_id=room_id, config_dir=config_dir
        )
    except Exception:  # noqa: BLE001 - 结算失败不能回滚已经推进的时间
        return ""
    if not result.get("settled"):
        return ""
    return "\n" + "\n".join(result.get("messages") or [])


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
    if sub not in {"show", "status", "advance", "set"}:
        return False, f"未知的时间命令：time {sub}\n{ROOM_TIME_HELP}"

    if sub in {"show", "status"}:
        return True, room_clock.describe(info)

    if not _operator(actor_rank):
        return False, "只有房主 / 房间管理员可以推进游戏时间"
    if not room_clock.is_enabled(info):
        return False, "本房间未启用游戏时钟；请先在房规中开启「游戏时钟」（coc7_game_clock）"

    assume_yes = "--yes" in rest or "yes" in rest
    body = [item for item in rest if item.lower() not in {"yes", "--yes", "-y"}]

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
        return True, f"游戏时间推进完成：{room_clock.format_display(clock['current'])}" + _settle_after_advance(
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
    return True, f"游戏时间设置完成：{room_clock.format_display(clock['current'])}" + _settle_after_advance(
        room_dir, room_id, config_dir
    )
