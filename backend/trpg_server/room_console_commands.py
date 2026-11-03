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
from trpg_server.room_narrative_commands import ROOM_NARRATIVE_HELP, handle_room_narrative_command
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
    "narrative": {
        "status", "state", "map", "exits", "ledger", "facts", "clocks", "triggers",
        "trigger", "media", "opening", "trace", "summary", "reset", "unlock",
        "guard", "profile", "rebuild-state",
    },
    "immersive": {"status", "music", "illustration", "history", "director"},
}

# 不带子命令时默认执行的子命令（例如 `members` 等价于 `members list`）。
DEFAULT_SUBS: dict[str, str] = {
    "members": "list",
    "dice": "status",
    "narrative": "status",
    "immersive": "status",
}


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
            *ROOM_NARRATIVE_HELP().splitlines(),
            "  immersive                    （本房间配乐/配图状态与开关）",
            "  immersive music show         （查看本房间配乐档位、强度、段落与种子）",
            "  immersive music set <profile> <intensity> [--yes]（手动指定配乐，档位见站点设置）",
            "  immersive music auto <on|off> [--yes]（本房间自动切歌开关）",
            "  immersive music <on|off> [--yes]（本房间配乐总开关）",
            "  immersive illustration show  （查看本房间配图开关与最近历史）",
            "  immersive illustration <on|off> [--yes]（本房间配图总开关）",
            "  immersive illustration auto <on|off> [--yes]（本房间自动配图开关）",
            "  immersive illustration now [场景描述] [--yes]（立即生成一张图，可能产生费用）",
            "  immersive cache show|stats      （查看本剧本配图缓存文件数与体积）",
            "  immersive cache record|prefer <on|off|inherit> （房间级收紧剧本缓存；无法越过站点开关）",
            "  immersive cache prune [--yes]   （按站点配额 LRU 清理本剧本缓存，默认预演）",
            "  immersive cache clear [--yes]   （删除本剧本全部配图缓存，默认预演）",
            "  immersive history            （查看本房间配乐/配图历史）",
            "  immersive director           （只读查看 AI 音乐导演最近采用的乐谱与段落；站点配置不可在房间内修改）",
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
    if domain == "immersive":
        return _immersive_command(room_dir, room_id, sub, rest, flags, confirm)
    if domain == "narrative":
        ok, message = handle_room_narrative_command(
            parts[1:],
            room_id=room_id,
            room_dir=room_dir,
            rooms_dir=resolved_rooms_dir,
            app=app,
            actor=actor,
            actor_id=actor_id,
            confirm=confirm,
        )
        return RoomCommandResult(ok, message)
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


def _immersive_command(
    room_dir: Path,
    room_id: Any,
    sub: str,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    """房间内沉浸命令；严格限定在 ``room_dir`` 作用域，绝不触碰站点配置。"""
    from trpg_server.immersive.config import INTENSITIES, PROFILES, load_immersive_settings, resolve_room_switches
    from trpg_server.immersive.illustration import history_for_room
    from trpg_server.immersive.music import load_state as load_music_state
    from trpg_server.immersive.service import generate_illustration_now, set_music_directive

    settings = load_immersive_settings(None)
    info = _load_room_info(room_dir)
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    switches = resolve_room_switches(settings, rules)
    music_state = load_music_state(room_dir)

    if sub == "status":
        return _immersive_status_text(room_id, switches, music_state, room_dir)

    if sub == "music":
        action = rest[0].lower() if rest else "show"
        if action == "show":
            return _immersive_status_text(room_id, switches, music_state, room_dir)
        if action in {"on", "off"}:
            target = action == "on"

            def _apply_music_toggle() -> RoomCommandResult:
                return _set_house_rule(
                    room_dir, "immersive_music_enabled", target, room_id,
                    f"房间沉浸配乐已{'开启' if target else '关闭'}",
                )

            return _confirm_action(
                preview=f"将把本房间沉浸配乐设为『{'开' if target else '关'}』（无法越过站点总开关）。",
                apply=_apply_music_toggle,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改本房间配乐开关?",
            )
        if action == "auto" and len(rest) > 1:
            target = rest[1].lower() in {"on", "enable", "true", "1", "开", "开启"}
            return _confirm_action(
                preview=f"将把本房间自动配乐切换设为『{'开' if target else '关'}』。",
                apply=lambda: _set_house_rule(
                    room_dir, "immersive_music_auto_switch", target, room_id,
                    f"房间自动配乐切换已{'开启' if target else '关闭'}",
                ),
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改自动配乐?",
            )
        if action == "set":
            profile = rest[1].lower() if len(rest) > 1 else ""
            intensity = rest[2].lower() if len(rest) > 2 else ""
            if profile not in PROFILES or intensity not in INTENSITIES:
                return RoomCommandResult(
                    False,
                    f"Usage: immersive music set <profile> <intensity>\n档位: {', '.join(PROFILES)}\n强度: {', '.join(INTENSITIES)}",
                )

            def _apply_directive() -> RoomCommandResult:
                plan = set_music_directive(
                    room_id=str(room_id),
                    room_dir=room_dir,
                    settings=settings,
                    profile=profile,
                    intensity=intensity,
                    scene="房间控制台",
                    scenario_id=str(info.get("scenario_id") or ""),
                )
                return RoomCommandResult(True, f"已切换本房间配乐为 {plan['profile']} / {plan['intensity']}（第 {plan['section']} 段）")

            return _confirm_action(
                preview=f"将把本房间配乐切到 {profile} / {intensity}。",
                apply=_apply_directive,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认切换本房间配乐?",
            )
        return RoomCommandResult(False, "Usage: immersive music show|on|off|auto <on|off>|set <profile> <intensity>")

    if sub == "illustration":
        action = rest[0].lower() if rest else "show"
        if action == "show":
            return _immersive_status_text(room_id, switches, music_state, room_dir)
        if action in {"on", "off"}:
            target = action == "on"
            return _confirm_action(
                preview=f"将把本房间沉浸配图设为『{'开' if target else '关'}』（无法越过站点总开关）。",
                apply=lambda: _set_house_rule(
                    room_dir, "immersive_illustration_enabled", target, room_id,
                    f"房间沉浸配图已{'开启' if target else '关闭'}",
                ),
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改本房间配图开关?",
            )
        if action == "auto" and len(rest) > 1:
            target = rest[1].lower() in {"on", "enable", "true", "1", "开", "开启"}
            return _confirm_action(
                preview=f"将把本房间自动配图设为『{'开' if target else '关'}』。",
                apply=lambda: _set_house_rule(
                    room_dir, "immersive_illustration_auto", target, room_id,
                    f"房间自动配图已{'开启' if target else '关闭'}",
                ),
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改自动配图?",
            )
        if action == "now":
            if not switches["illustration"]:
                return RoomCommandResult(False, "本房间配图未开启（或站点总开关关闭），请先开启后再生成")
            scene = " ".join(rest[1:]).strip()[:120] or "房间控制台手动场景"
            profile = music_state.get("profile") if music_state.get("profile") in PROFILES else settings.get("music_default_profile")
            intensity = music_state.get("intensity") if music_state.get("intensity") in INTENSITIES else settings.get("music_default_intensity")

            def _apply_now() -> RoomCommandResult:
                payload = generate_illustration_now(
                    room_id=str(room_id),
                    room_dir=room_dir,
                    settings=settings,
                    scene=scene,
                    profile=profile or "daily",
                    intensity=intensity or "medium",
                    turn_index=int(music_state.get("turns") or 0),
                    force=True,
                    scenario_id=str(info.get("scenario_id") or ""),
                    house_rules=rules,
                )
                return RoomCommandResult(
                    True,
                    f"配图完成：{payload.get('scene')}（{payload.get('provider')}）"
                    + ("· 已降级" if payload.get("degraded") else "")
                    + f"\n  url={payload.get('url')}",
                )

            return _confirm_action(
                preview=f"将立即为本房间生成配图：{scene}（外部服务可能产生费用）。",
                apply=_apply_now,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认立即生成本房间配图?",
            )
        return RoomCommandResult(False, "Usage: immersive illustration show|on|off|auto <on|off>|now [场景]")

    if sub == "cache":
        from trpg_server.immersive import scenario_cache
        from trpg_server.settings import SCENARIOS_DIR

        scenario_id = str(info.get("scenario_id") or "")
        action = rest[0].lower() if rest else "show"
        if action in {"show", "stats"}:
            if not scenario_id:
                return RoomCommandResult(False, "本房间未关联剧本，无法统计剧本配图缓存")
            data = scenario_cache.stats(scenario_id, scenarios_dir=SCENARIOS_DIR)
            rows = data.get("scenarios") or []
            if not rows:
                return RoomCommandResult(True, f"剧本 {scenario_id} 暂无配图缓存。")
            row = rows[0]
            return RoomCommandResult(
                True,
                f"剧本配图缓存（{scenario_id}）：\n"
                f"  文件 {row.get('files')} 个 / {_human_bytes(int(row.get('bytes') or 0))}"
                f"（索引内 {row.get('tracked_files')}，未登记 {row.get('untracked_files')}）\n"
                f"  目录: {row.get('directory')}\n"
                f"  站点配置: 记录新图 {'开' if switches.get('scenario_record') else '关'} · "
                f"优先复用 {'开' if switches.get('scenario_prefer') else '关'}",
            )
        if action == "clear":
            if not scenario_id:
                return RoomCommandResult(False, "本房间未关联剧本，无法清理剧本配图缓存")

            def _apply_room_clear() -> RoomCommandResult:
                files, size = scenario_cache.clear(scenario_id, scenarios_dir=SCENARIOS_DIR)
                return RoomCommandResult(True, f"已清理剧本 {scenario_id} 的配图缓存：{files} 个文件 / {_human_bytes(size)}")

            return _confirm_action(
                preview=f"将删除剧本 {scenario_id} 的全部配图缓存（不可恢复，相关房间历史链接可能失效）。",
                apply=_apply_room_clear,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认清理本剧本配图缓存?",
            )
        if action == "prune":
            if not scenario_id:
                return RoomCommandResult(False, "本房间未关联剧本，无法清理剧本配图缓存")

            def _apply_room_prune() -> RoomCommandResult:
                files, size = scenario_cache.prune(
                    scenario_id=scenario_id, settings=settings, scenarios_dir=SCENARIOS_DIR
                )
                return RoomCommandResult(True, f"已按配额清理剧本 {scenario_id}：{files} 个文件 / {_human_bytes(size)}")

            return _confirm_action(
                preview=f"将按站点配额对剧本 {scenario_id} 做 LRU 清理（删除最旧配图，默认预演）。",
                apply=_apply_room_prune,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认执行剧本缓存 LRU 清理?",
            )
        if action in {"record", "prefer"} and len(rest) > 1:
            raw = rest[1].lower()
            if raw in {"inherit", "follow", "跟随", "默认"}:
                target = None
            elif raw in {"on", "enable", "true", "1", "开", "开启"}:
                target = True
            elif raw in {"off", "disable", "false", "0", "关", "关闭"}:
                target = False
            else:
                return RoomCommandResult(False, "Usage: immersive cache record|prefer <on|off|inherit>")
            rule_key = "immersive_scenario_record" if action == "record" else "immersive_scenario_prefer"
            return _confirm_action(
                preview=f"将把本房间「{'记录到剧本' if action == 'record' else '优先复用剧本图'}」设为『{raw}』（无法越过站点开关）。",
                apply=lambda: _set_house_rule(
                    room_dir, rule_key, target, room_id,
                    f"房间剧本配图{'记录' if action == 'record' else '复用'}设置已更新为 {raw}",
                ),
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改房间剧本配图缓存房规?",
            )
        return RoomCommandResult(
            False,
            "Usage: immersive cache show|stats|clear|prune|record <on|off|inherit>|prefer <on|off|inherit>",
        )

    if sub == "director":
        # 只读：站点级导演配置与预算只能在站点控制台/设置页修改，房间侧不可越权。
        return _immersive_director_text(settings, music_state)

    if sub == "history":
        return _immersive_history_text(room_dir, music_state, history_for_room(room_dir))

    return RoomCommandResult(False, "Usage: immersive status|music|illustration|history|director|cache")


def _human_bytes(value: int) -> str:
    number = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    return f"{number:.1f}GB"


def _immersive_director_text(settings: dict[str, Any], music_state: dict[str, Any]) -> RoomCommandResult:
    director_state = music_state.get("director") if isinstance(music_state.get("director"), dict) else {}
    score = director_state.get("score") if isinstance(director_state.get("score"), dict) else {}
    if not settings.get("music_director_enabled"):
        return RoomCommandResult(True, "AI 音乐导演未启用（站点设置可开启）；当前使用规则分析器。")
    if not director_state:
        return RoomCommandResult(True, "AI 音乐导演已启用，但本房间尚未产出乐谱；当前使用规则分析器。")
    sections = score.get("sections") if isinstance(score.get("sections"), list) else []
    section_index = int(director_state.get("section_index") or 0)
    current = sections[section_index] if 0 <= section_index < len(sections) else {}
    return RoomCommandResult(
        True,
        "房间 AI 音乐导演（只读）：\n"
        f"  来源: {director_state.get('platform') or '-'} / {director_state.get('model') or '-'}"
        f" · 置信 {director_state.get('confidence') or 0} · {'缓存命中' if director_state.get('cached') else '实时生成'}\n"
        f"  指纹: {director_state.get('fingerprint') or '-'} · 更新于 {director_state.get('at') or '-'}\n"
        f"  场景: {score.get('scene') or '-'} · 情绪 {score.get('mood') or '-'} · 调式 {score.get('mode') or '-'}"
        f" · 速度 {score.get('tempo') or '-'} BPM\n"
        f"  段落: {section_index + 1}/{max(1, len(sections))}"
        + (f" · {current.get('label') or '-'} · 动态 {current.get('dynamic') or '-'} · {current.get('bars') or '-'} 小节"
           if current else "")
        + "\n  提示：站点级开关/预算/缓存请在站点控制台 `immersive director` 操作。",
    )


def _immersive_status_text(
    room_id: Any,
    switches: dict[str, bool],
    music_state: dict[str, Any],
    room_dir: Path,
) -> RoomCommandResult:
    from trpg_server.immersive.illustration import history_for_room

    history = history_for_room(room_dir, limit=3)
    lines = [
        f"房间 {room_id} 沉浸状态：",
        f"  配乐: {'开' if switches.get('music') else '关'} · "
        f"{music_state.get('profile') or '-'} / {music_state.get('intensity') or '-'} · 第 {music_state.get('section') or 0} 段 · "
        f"回合 {music_state.get('turns') or 0}",
        f"  配图: {'开' if switches.get('illustration') else '关'} · 自动 {'开' if switches.get('illustration_auto') else '关'} · "
        f"历史 {len(history)} 条（显示最近 3 条）",
    ]
    for item in history:
        lines.append(
            f"    {item.get('created_at') or '-'} · {item.get('scene') or '-'} · "
            f"{item.get('provider') or '-'} · degraded={bool(item.get('degraded'))}"
        )
    return RoomCommandResult(True, "\n".join(lines))


def _immersive_history_text(
    room_dir: Path,
    music_state: dict[str, Any],
    illustration_history: list[dict[str, Any]],
) -> RoomCommandResult:
    lines = ["本房间配乐历史："]
    music_history = music_state.get("history") if isinstance(music_state.get("history"), list) else []
    if not music_history:
        lines.append("  （暂无记录）")
    for item in music_history[-8:]:
        if isinstance(item, dict):
            lines.append(
                f"  {item.get('at') or '-'} · {item.get('profile') or '-'} / {item.get('intensity') or '-'} "
                f"· 第 {item.get('section') or 0} 段 · {item.get('reason') or '-'}"
            )
    lines.append("本房间配图历史：")
    if not illustration_history:
        lines.append("  （暂无记录）")
    for item in illustration_history[-8:]:
        lines.append(
            f"  {item.get('created_at') or '-'} · {item.get('scene') or '-'} · "
            f"{item.get('provider') or '-'} · turn={item.get('turn')} · degraded={bool(item.get('degraded'))}"
        )
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
