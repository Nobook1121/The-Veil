"""房间级控制台命令执行器（仅作用于单个房间）。

本模块独立于 Flask 与全局控制台（``console_commands``）：只接受严格的房间域
命令白名单，任何全局命令（``user`` / ``shutdown`` / ``scenario`` / ``backup`` …）
一律拒绝，从而保证房间控制台的影响范围只限于该房间。依赖由调用方注入
（``rooms_dir`` / ``room_dir`` / ``app``），便于在 Web 路由与测试中复用。
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from trpg_server import room_admin
from trpg_server.agents import room_state
from trpg_server.json_store import read_json
from trpg_server.room_log import clear_room_log, log_room_event, read_room_log
from trpg_server.settings import ROOMS_DIR

logger = logging.getLogger(__name__)

ROOM_CONSOLE_ACTOR = "房间控制台"

# 破坏性命令统一支持 --yes：缺省为预演（只打印将发生的变更），交互确认或 --yes 才执行。
YES_FLAGS = {"--yes", "-y"}
DEFAULT_LOG_TAIL = 50

# 严格白名单：domain -> 允许的子命令集合（空集表示该命令不接受子命令）。
ROOM_COMMAND_WHITELIST: dict[str, set[str]] = {
    "help": set(),
    "info": set(),
    "members": {"list", "kick", "promote"},
    "state": set(),
    "event": {"add"},
    "log": {"tail", "clear"},
    "autosave": set(),
    "archive": set(),
    "dice": {"status", "animation", "player-roll", "reroll", "reroll-limit", "pending", "cancel", "level", "player"},
}

# 不带子命令时默认执行的子命令（例如 `members` 等价于 `members list`）。
DEFAULT_SUBS: dict[str, str] = {"members": "list", "dice": "status"}


@dataclass
class RoomCommandResult:
    ok: bool
    message: str


def _split_args(args: list[str]) -> tuple[list[str], set[str]]:
    positionals = [item for item in args if not item.startswith("-")]
    flags = {item.lower() for item in args if item.startswith("-")}
    return positionals, flags


def _assume_yes(flags: set[str]) -> bool:
    return bool(flags & YES_FLAGS)


def _confirm_action(
    *,
    preview: str,
    apply: Callable[[], RoomCommandResult],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    prompt: str,
) -> RoomCommandResult:
    """破坏性命令统一入口：``--yes`` 直接执行，否则预演 + 交互确认。"""
    if assume_yes:
        return apply()
    if confirm is None:
        return RoomCommandResult(True, preview + "\n（预演模式：确认无误后加 --yes 执行）")
    if confirm(f"{prompt} (y/N)"):
        return apply()
    return RoomCommandResult(True, preview + "\n（已取消，未执行）")


def room_command_help_text() -> str:
    return "\n".join(
        [
            "房间控制台命令（仅作用于本房间）：",
            "  help                        （显示本帮助）",
            "  info                        （房间信息：房间码/剧本/成员数）",
            "  members                     （成员及其房间角色）",
            "  members kick <user> [--yes] （将成员移出本房间，破坏性）",
            "  members promote <user> [--yes]（将成员提升为房间管理员，破坏性）",
            "  state                       （房间状态：场景/线索/任务/触发器）",
            "  event add <kind> <content>  （记录一条房间事件）",
            "  log tail [n]                （查看本房间最近 n 条日志，默认 50）",
            "  log clear [--yes]           （清空本房间日志，破坏性）",
            "  autosave                    （立即为本房间保存一次自动存档）",
            "  archive [--yes]             （归档本房间，需已到达结局）",
            "  dice                        （本房间骰子动画档位、玩家投掷、重投配置与待投掷请求）",
            "  dice animation show         （查看本房间生效的动画档位）",
            "  dice animation set <text|simple|3d|inherit> [--yes]（设置动画档位，inherit 跟随管理员默认）",
            "  dice player-roll show       （查看本房间生效的玩家投掷开关）",
            "  dice player-roll <on|off|inherit> [--yes]（设置玩家投掷开关）",
            "  dice reroll show            （查看本房间生效的重投模式与上限）",
            "  dice reroll <off|limited|unlimited|inherit> [--yes]（设置重投模式）",
            "  dice reroll-limit <1-20|inherit> [--yes]（设置指定次数模式的上限）",
            "  dice pending                （列出本房间等待玩家投掷的请求）",
            "  dice cancel [request_id] [--yes]（取消本房间待投掷请求，缺省取消最早的一条）",
            "  说明：破坏性命令缺省为「预演」，加 --yes 或在交互确认时输入 y 才会真正执行。",
            "  说明：成员命令只调整房间内角色（房主/管理员/成员），不会授予网站级权限。",
        ]
    )


def _resolve_rooms_dir(rooms_dir: Any) -> Path:
    return Path(rooms_dir) if rooms_dir else Path(ROOMS_DIR)


def _format_log_line(entry: dict[str, Any]) -> str:
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    detail_text = ""
    if details:
        detail_text = " " + " ".join(f"{key}={value}" for key, value in details.items())
    level = str(entry.get("level") or "info").upper()
    return f"[{entry.get('ts') or '-'}][{level}] {entry.get('message') or ''}{detail_text}"


def execute_room_command(
    command_line: str,
    *,
    room_id: Any,
    room_dir: Any = None,
    rooms_dir: Any = None,
    app: Any = None,
    actor: Any = None,
    actor_id: Any = None,
    confirm: Callable[[str], bool] | None = None,
) -> RoomCommandResult:
    """执行一条房间控制台命令；非白名单命令一律拒绝。"""
    try:
        parts = shlex.split(command_line)
    except ValueError as exc:
        return RoomCommandResult(False, f"命令语法错误: {exc}")

    if not parts:
        return RoomCommandResult(True, "")

    domain = parts[0].lower()
    if domain in {"help", "?"}:
        return RoomCommandResult(True, room_command_help_text())
    if domain not in ROOM_COMMAND_WHITELIST:
        return RoomCommandResult(
            False, f"命令「{parts[0]}」不属于房间范围；房间控制台只允许房间相关命令"
        )

    positionals, flags = _split_args(parts[1:])
    sub = positionals[0].lower() if positionals else DEFAULT_SUBS.get(domain, "")
    rest = positionals[1:]
    allowed_subs = ROOM_COMMAND_WHITELIST[domain]
    if allowed_subs:
        if sub not in allowed_subs:
            return RoomCommandResult(False, f"命令「{domain} {sub or ''}」不属于房间范围")
    elif positionals:
        return RoomCommandResult(False, f"命令「{domain}」不接受参数")

    if room_dir is None:
        return RoomCommandResult(False, "房间目录不可用，无法执行房间命令")
    room_dir = Path(room_dir)
    resolved_rooms_dir = _resolve_rooms_dir(rooms_dir)

    if domain == "info":
        return _info_command(room_dir, room_id, resolved_rooms_dir)
    if domain == "members":
        if sub == "kick":
            return _members_kick_command(room_dir, room_id, rest, flags, confirm)
        if sub == "promote":
            return _members_promote_command(room_dir, room_id, rest, flags, confirm)
        return _members_command(room_id, resolved_rooms_dir)
    if domain == "state":
        return _state_command(room_dir)
    if domain == "event":
        return _event_add_command(room_dir, rest, actor)
    if domain == "log":
        if sub == "tail":
            return _log_tail_command(room_dir, rest)
        return _log_clear_command(room_dir, flags, confirm)
    if domain == "autosave":
        return _autosave_command(room_dir, room_id, app, actor, actor_id)
    if domain == "archive":
        return _archive_command(room_id, resolved_rooms_dir, app, flags, confirm)
    if domain == "dice":
        if sub in {"animation", "level"}:
            return _dice_animation_command(room_dir, room_id, rest, flags, confirm, legacy=sub == "level")
        if sub in {"player-roll", "player"}:
            return _dice_player_roll_command(room_dir, room_id, rest, flags, confirm, legacy=sub == "player")
        if sub == "reroll":
            return _dice_reroll_command(room_dir, room_id, rest, flags, confirm)
        if sub == "reroll-limit":
            return _dice_reroll_limit_command(room_dir, room_id, rest, flags, confirm)
        if sub == "pending":
            return _dice_pending_command(room_id)
        if sub == "cancel":
            return _dice_cancel_command(room_id, rest, flags, confirm)
        return _dice_status_command(room_dir, room_id)
    return RoomCommandResult(False, f"未知命令: {parts[0]}")


def _info_command(room_dir: Path, room_id: Any, rooms_dir: Path) -> RoomCommandResult:
    info = read_json(room_dir / "info.json", default={})
    if not isinstance(info, dict):
        info = {}
    members = info.get("members") or []
    active = [m for m in members if isinstance(m, dict) and m.get("is_active", True) is not False and m.get("status", "active") != "removed"]
    lines = [
        f"房间「{info.get('name') or room_id}」{' [已归档]' if info.get('archived') else ''}",
        f"  房间码: {info.get('room_code') or '-'}",
        f"  剧本: {info.get('scenario_title') or info.get('scenario_id') or '-'}",
        f"  成员: {len(active)} 人（共 {len(members)}）",
        f"  创建时间: {info.get('created_at') or '-'}",
        f"  更新时间: {info.get('updated_at') or '-'}",
    ]
    return RoomCommandResult(True, "\n".join(lines))


def _members_command(room_id: Any, rooms_dir: Path) -> RoomCommandResult:
    try:
        data = room_admin.room_members(rooms_dir, room_id)
    except LookupError as exc:
        return RoomCommandResult(False, str(exc))
    lines = [f"房间「{data['name'] or data['id']}」成员 {len(data['members'])} 人:"]
    for member in data["members"]:
        state = "" if member["active"] else "（已移除）"
        lines.append(
            f"  {member['username'] or member['user_id']} · 账号角色={member['role'] or '-'} · "
            f"房间角色={member['room_role']}{state}"
        )
    return RoomCommandResult(True, "\n".join(lines))


def _load_room_info(room_dir: Path) -> dict[str, Any]:
    info = read_json(room_dir / "info.json", default={})
    return info if isinstance(info, dict) else {}


def _find_room_member(info: dict[str, Any], target: str) -> dict[str, Any] | None:
    """按 user_id 或用户名（忽略大小写）在房间成员中查找目标。"""
    from trpg_server.routes.rooms import _find_member

    text = str(target or "").strip()
    if not text:
        return None
    member = _find_member(info, user_id=text) or _find_member(info, username=text)
    if member is None:
        lowered = text.lower()
        for item in info.get("members") or []:
            if isinstance(item, dict) and str(item.get("username") or "").lower() == lowered:
                return item
    return member


def _members_kick_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    """将普通成员移出本房间（不可移出房间所有者）。"""
    if not rest:
        return RoomCommandResult(False, "Usage: members kick <user_id|username> [--yes]")

    from trpg_server.routes.rooms import (
        ROOM_MEMBER_REMOVED,
        ROOM_ROLE_ADMIN,
        ROOM_ROLE_MEMBER,
        ROOM_ROLE_OWNER,
        _is_active_member,
        _timestamp,
        _write_room,
    )

    info = _load_room_info(room_dir)
    member = _find_room_member(info, rest[0])
    if member is None or not _is_active_member(member):
        return RoomCommandResult(False, f"本房间内未找到成员「{rest[0]}」")
    if member.get("room_role") == ROOM_ROLE_OWNER or str(member.get("user_id")) == str(info.get("creator_id")):
        return RoomCommandResult(False, "不能移出房间所有者")
    name = member.get("username") or member.get("user_id")

    def _apply() -> RoomCommandResult:
        member["status"] = ROOM_MEMBER_REMOVED
        member["is_active"] = False
        member["removed_at"] = _timestamp()
        if member.get("room_role") == ROOM_ROLE_ADMIN:
            member["room_role"] = ROOM_ROLE_MEMBER
        _write_room(room_dir, info)
        log_room_event(
            room_dir,
            f"房间控制台将成员「{name}」移出房间",
            actor=ROOM_CONSOLE_ACTOR,
            source="command",
            room_id=room_id,
        )
        return RoomCommandResult(True, f"已将「{name}」移出本房间")

    return _confirm_action(
        preview=f"将把成员「{name}」移出本房间（保留历史记录，不可自动恢复）。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认移出「{name}」?",
    )


def _members_promote_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    """把成员提升为「房间管理员」。

    只调整房间内角色 ``room_role``，绝不改动账号角色 ``role``，因此不会授予网站级
    ADMIN / OWNER 权限，也不与网站控制台的 ``user promote`` 共用任何实现。
    """
    if not rest:
        return RoomCommandResult(False, "Usage: members promote <user_id|username> [admin] [--yes]")
    desired = (rest[1].lower() if len(rest) > 1 else "admin")
    if desired != "admin":
        return RoomCommandResult(
            False, "只能提升为房间管理员（admin）；不可提升为房间所有者或网站管理员"
        )

    from trpg_server.routes.rooms import (
        ROOM_ROLE_ADMIN,
        ROOM_ROLE_OWNER,
        _is_active_member,
        _write_room,
    )

    info = _load_room_info(room_dir)
    member = _find_room_member(info, rest[0])
    if member is None or not _is_active_member(member):
        return RoomCommandResult(False, f"本房间内未找到成员「{rest[0]}」")
    if member.get("room_role") == ROOM_ROLE_OWNER or str(member.get("user_id")) == str(info.get("creator_id")):
        return RoomCommandResult(False, "该成员已是房间所有者，无需提升")
    if member.get("room_role") == ROOM_ROLE_ADMIN:
        return RoomCommandResult(False, "该成员已是房间管理员")
    name = member.get("username") or member.get("user_id")

    def _apply() -> RoomCommandResult:
        member["room_role"] = ROOM_ROLE_ADMIN
        _write_room(room_dir, info)
        log_room_event(
            room_dir,
            f"房间控制台将成员「{name}」提升为房间管理员",
            actor=ROOM_CONSOLE_ACTOR,
            source="command",
            room_id=room_id,
        )
        return RoomCommandResult(True, f"已将「{name}」提升为房间管理员")

    return _confirm_action(
        preview=f"将把成员「{name}」提升为房间管理员（仅房间内权限，不影响网站角色）。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认提升「{name}」为房间管理员?",
    )


def _state_command(room_dir: Path) -> RoomCommandResult:
    state = room_state.load_room_state(room_dir)
    lines = [
        f"当前场景: {state.get('active_scene_id') or '-'}",
        f"线索 {len(state['clues'])} · 任务 {len(state['quests'])} · 物品 {len(state['items'])} · 时间线 {len(state['timeline'])}",
        f"已触发事件 {len(state['triggered_event_ids'])} · 事件日志 {len(state['event_log'])} 条",
        f"已完结: {bool(state.get('completed') or state.get('ending_reached'))}",
    ]
    summary = str(state.get("rolling_summary") or "").strip()
    if summary:
        lines.append(f"摘要: {summary[:200]}")
    return RoomCommandResult(True, "\n".join(lines))


def _event_add_command(room_dir: Path, rest: list[str], actor: Any) -> RoomCommandResult:
    if len(rest) < 2:
        return RoomCommandResult(False, "Usage: event add <kind> <content>")
    kind = rest[0]
    content = " ".join(rest[1:]).strip()
    if not content:
        return RoomCommandResult(False, "Usage: event add <kind> <content>")
    event = {"kind": kind, "content": content}
    if actor:
        event["actor"] = str(actor)
    item = room_state.append_room_event(room_dir, event)
    return RoomCommandResult(True, f"已记录事件 {item['id']}（{kind}）: {content[:120]}")


def _log_tail_command(room_dir: Path, rest: list[str]) -> RoomCommandResult:
    limit = DEFAULT_LOG_TAIL
    if rest:
        try:
            limit = int(rest[0])
        except (TypeError, ValueError):
            return RoomCommandResult(False, "Usage: log tail [n]")
    data = read_room_log(room_dir, limit=max(1, min(limit, 500)))
    entries = data["entries"]
    if not entries:
        return RoomCommandResult(True, "本房间暂无日志")
    lines = [f"本房间最近 {len(entries)} 条日志:"]
    lines.extend(f"  {_format_log_line(entry)}" for entry in entries)
    return RoomCommandResult(True, "\n".join(lines))


def _dice_status_command(room_dir: Path, room_id: Any) -> RoomCommandResult:
    from trpg_server.agents import player_roll
    from trpg_server.agents.dice_settings import resolve_dice_settings

    info = _load_room_info(room_dir)
    settings = resolve_dice_settings(None, info.get("house_rules"))
    level_source = "房间规则" if settings["level_source"] == "room" else "管理员默认"
    player_source = "房间规则" if settings["player_roll_source"] == "room" else "管理员默认"
    reroll_source = "房间规则" if settings["reroll_source"] == "room" else "管理员默认"
    pending = player_roll.list_pending(str(room_id))
    lines = [
        f"骰子动画: {settings['level']}（{level_source}）",
        f"玩家投掷: {'开' if settings['player_roll'] else '关'}（{player_source}）",
        f"等待超时: {settings['timeout']}s",
        f"重投模式: {settings['reroll_mode']}（{reroll_source}） · 上限: {settings['reroll_limit']}",
        f"待投掷请求: {len(pending)} 个",
    ]
    for item in pending:
        roller = item.get("roller") or {}
        lines.append(f"  {item.get('request_id')} · 玩家={roller.get('username') or roller.get('user_id') or '-'} · 状态={item.get('state')}")
    return RoomCommandResult(True, "\n".join(lines))


def _set_house_rule(room_dir: Path, key: str, value: Any, room_id: Any, summary: str) -> RoomCommandResult:
    from trpg_server.routes.rooms import _write_room

    info = _load_room_info(room_dir)
    rules = info.get("house_rules")
    if not isinstance(rules, dict):
        rules = {}
    rules[key] = value
    info["house_rules"] = rules
    _write_room(room_dir, info)
    log_room_event(room_dir, summary, source="room_console", actor=ROOM_CONSOLE_ACTOR)
    return RoomCommandResult(True, summary)


def _dice_show(room_dir: Path, key: str) -> str:
    from trpg_server.agents.dice_settings import resolve_dice_settings

    info = _load_room_info(room_dir)
    settings = resolve_dice_settings(None, info.get("house_rules"))
    if key == "animation":
        source = "房间规则" if settings["level_source"] == "room" else "管理员默认"
        return f"本房间动画档位: {settings['level']}（{source}）"
    if key == "player-roll":
        source = "房间规则" if settings["player_roll_source"] == "room" else "管理员默认"
        return f"本房间玩家投掷: {'开' if settings['player_roll'] else '关'}（{source}）"
    source = "房间规则" if settings["reroll_source"] == "room" else "管理员默认"
    return f"本房间重投: {settings['reroll_mode']} · 上限 {settings['reroll_limit']}（{source}）"


def _dice_animation_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
    *,
    legacy: bool = False,
) -> RoomCommandResult:
    if legacy:
        value = rest[0].lower() if rest else ""
    elif rest == ["show"]:
        return RoomCommandResult(True, _dice_show(room_dir, "animation"))
    elif len(rest) == 2 and rest[0] == "set":
        value = rest[1].lower()
    else:
        return RoomCommandResult(False, "Usage: dice animation show | dice animation set <text|simple|3d|inherit> [--yes]")
    if value not in {"text", "simple", "3d", "inherit"}:
        return RoomCommandResult(False, "动画档位只能是 text / simple / 3d / inherit")
    target = None if value == "inherit" else value
    label = "跟随管理员默认" if target is None else target

    def _apply() -> RoomCommandResult:
        return _set_house_rule(room_dir, "dice_animation_level", target, room_id, f"房间骰子动画档位已设为 {label}")

    return _confirm_action(
        preview=f"将把本房间骰子动画设为 {label}。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认把本房间骰子动画设为 {label}?",
    )


def _dice_player_roll_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
    *,
    legacy: bool = False,
) -> RoomCommandResult:
    if legacy:
        value = rest[0].lower() if rest else ""
    elif rest == ["show"]:
        return RoomCommandResult(True, _dice_show(room_dir, "player-roll"))
    elif len(rest) == 1:
        value = rest[0].lower()
    else:
        return RoomCommandResult(False, "Usage: dice player-roll show | dice player-roll <on|off|inherit> [--yes]")
    if value not in {"on", "off", "inherit"}:
        return RoomCommandResult(False, "玩家投掷只能是 on / off / inherit")
    target = None if value == "inherit" else value == "on"
    label = "跟随管理员默认" if target is None else ("开" if target else "关")

    def _apply() -> RoomCommandResult:
        return _set_house_rule(room_dir, "player_dice_roll", target, room_id, f"房间玩家投掷已设为 {label}")

    return _confirm_action(
        preview=f"将把本房间玩家投掷设为 {label}。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认把本房间玩家投掷设为 {label}?",
    )


def _dice_reroll_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    from trpg_server.agents.dice_settings import DICE_REROLL_MODES

    if rest == ["show"]:
        return RoomCommandResult(True, _dice_show(room_dir, "reroll"))
    if len(rest) != 1 or rest[0].lower() not in {*DICE_REROLL_MODES, "inherit"}:
        return RoomCommandResult(False, "Usage: dice reroll show | dice reroll <off|limited|unlimited|inherit> [--yes]")
    value = rest[0].lower()
    target = None if value == "inherit" else value
    label = "跟随管理员默认" if target is None else value

    def _apply() -> RoomCommandResult:
        return _set_house_rule(room_dir, "dice_reroll_mode", target, room_id, f"房间重投模式已设为 {label}")

    return _confirm_action(
        preview=f"将把本房间重投模式设为 {label}。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认把本房间重投模式设为 {label}?",
    )


def _dice_reroll_limit_command(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    if len(rest) != 1:
        return RoomCommandResult(False, "Usage: dice reroll-limit <1-20|inherit> [--yes]")
    raw = rest[0].lower()
    if raw == "inherit":
        target: int | None = None
        label = "跟随管理员默认"
    else:
        try:
            target = int(raw)
        except ValueError:
            return RoomCommandResult(False, "重投上限必须是 1-20 的整数或 inherit")
        if not 1 <= target <= 20:
            return RoomCommandResult(False, "重投上限必须是 1-20 的整数或 inherit")
        label = str(target)

    def _apply() -> RoomCommandResult:
        return _set_house_rule(room_dir, "dice_reroll_limit", target, room_id, f"房间重投上限已设为 {label}")

    return _confirm_action(
        preview=f"将把本房间重投上限设为 {label}。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认把本房间重投上限设为 {label}?",
    )


def _dice_pending_command(room_id: Any) -> RoomCommandResult:
    from trpg_server.agents import player_roll

    items = player_roll.list_pending(str(room_id))
    if not items:
        return RoomCommandResult(True, "本房间没有等待玩家投掷的请求")
    lines = [f"本房间待投掷请求 {len(items)} 个:"]
    for item in items:
        roller = item.get("roller") or {}
        plan = item.get("plan") or {}
        lines.append(
            f"  {item.get('request_id')} · 玩家={roller.get('username') or roller.get('user_id') or '-'} · "
            f"表达式={plan.get('expression') or '-'}"
        )
    return RoomCommandResult(True, "\n".join(lines))


def _dice_cancel_command(
    room_id: Any, rest: list[str], flags: set[str], confirm: Callable[[str], bool] | None
) -> RoomCommandResult:
    from trpg_server.agents import player_roll

    pending = player_roll.list_pending(str(room_id))
    if not pending:
        return RoomCommandResult(True, "本房间没有等待玩家投掷的请求")
    request_id = rest[0] if rest else str(pending[0].get("request_id") or "")
    if not request_id:
        return RoomCommandResult(False, "无法确定要取消的请求")

    def _apply() -> RoomCommandResult:
        _, error = player_roll.cancel_roll(str(room_id), request_id, operator=ROOM_CONSOLE_ACTOR)
        if error:
            message = "请求已投掷或不存在" if error in {"not_found", "already_rolled"} else error
            return RoomCommandResult(False, message)
        return RoomCommandResult(True, f"已取消待投掷请求 {request_id}")

    return _confirm_action(
        preview=f"将取消待投掷请求 {request_id}，AI 会收到「已被管理员取消」。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认取消投掷请求 {request_id}?",
    )


def _log_clear_command(
    room_dir: Path, flags: set[str], confirm: Callable[[str], bool] | None
) -> RoomCommandResult:
    assume_yes = _assume_yes(flags)

    def _apply() -> RoomCommandResult:
        count = clear_room_log(room_dir)
        return RoomCommandResult(True, f"已清空本房间日志（{count} 条）")

    return _confirm_action(
        preview="将清空本房间的全部控制台日志（不可恢复）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt="确认清空本房间日志?",
    )


def _autosave_command(
    room_dir: Path, room_id: Any, app: Any, actor: Any, actor_id: Any
) -> RoomCommandResult:
    if app is None:
        return RoomCommandResult(False, "自动存档需要运行中的服务实例（未注入 app）")
    messages = read_json(room_dir / "messages.json", default=[])
    if not isinstance(messages, list):
        messages = []
    from trpg_server.routes.rooms import _write_room_autosave

    try:
        with app.app_context():
            node = _write_room_autosave(
                room_dir, room_id, messages, username=actor, user_id=actor_id
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Room console autosave failed")
        return RoomCommandResult(False, f"自动存档失败: {exc}")
    return RoomCommandResult(True, f"已保存本房间自动存档节点: {node.get('filename')}")


def _archive_command(
    room_id: Any,
    rooms_dir: Path,
    app: Any,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    assume_yes = _assume_yes(flags)

    def _apply() -> RoomCommandResult:
        try:
            result = room_admin.archive_room(rooms_dir, room_id, app=app)
        except LookupError as exc:
            return RoomCommandResult(False, str(exc))
        except ValueError as exc:
            return RoomCommandResult(False, str(exc))
        return RoomCommandResult(True, f"已归档本房间「{result['name'] or result['id']}」")

    return _confirm_action(
        preview="将归档本房间（保留消息并生成不可变归档快照，不可撤销）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt="确认归档本房间?",
    )
