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
from trpg_server.room_combat_commands import ROOM_COMBAT_HELP, handle_room_combat_command
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
        "guard", "profile", "rebuild-state", "insert", "say",
    },
    "immersive": {"status", "music", "illustration", "history", "director"},
    "voice": {"status", "on", "off", "map", "mute", "usage", "cache"},
    "combat": {
        "status", "state", "order", "start", "join", "next", "end-turn", "endturn",
        "delay", "disengage", "reload", "ready", "cast",
        "damage", "heal", "treat", "end",
    },
}

# 不带子命令时默认执行的子命令（例如 `members` 等价于 `members list`）。
DEFAULT_SUBS: dict[str, str] = {
    "members": "list",
    "dice": "status",
    "narrative": "status",
    "immersive": "status",
    "voice": "status",
    "combat": "status",
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
            "  immersive music show         （查看配乐引擎、档位、当前外链曲目与底床状态）",
            "  immersive music set <profile> <intensity> [--yes]（手动指定配乐，档位见站点设置）",
            "  immersive music auto <on|off> [--yes]（本房间自动切歌开关）",
            "  immersive music <on|off> [--yes]（本房间配乐总开关；房间级，与个人本地开关无关）",
            "  immersive music search [场景] [--tags=a,b] [--profile=x] [--intensity=y]（重新检索外链 BGM）",
            "  immersive music switch [序号|track_key|标题]（在最近检索结果中切换曲目；省略参数=换一首）",
            "  immersive illustration show  （查看本房间配图开关与最近历史）",
            "  immersive illustration <on|off> [--yes]（本房间配图总开关）",
            "  immersive illustration auto <on|off> [--yes]（本房间自动配图开关）",
            "  immersive illustration trigger show（查看生效配图触发档位与来源）",
            "  immersive illustration trigger <inherit|important|off> [--yes]（房间收紧配图档位，只能更紧）",
            "  immersive illustration now [场景描述] [--yes]（立即生成一张图，可能产生费用）",
            "  immersive cache room show         （查看本房间缓存条数/体积/命中次数）",
            "  immersive cache room clear [--yes]（清空本房间目录缓存，默认预演）",
            "  immersive cache show|stats      （只读：本房间缓存 + 本剧本缓存统计）",
            "  immersive cache record|prefer <on|off|inherit> （房间级收紧剧本缓存；无法越过站点开关）",
            "  immersive cache prune [--yes]   （按单房间配额 LRU 清理本房间缓存，默认预演）",
            "  immersive cache clear [--yes]   （删除本房间目录缓存，默认预演；剧本缓存需站点控制台）",
            "  immersive history            （查看本房间配乐/配图历史）",
            "  immersive director           （只读查看 AI 音乐导演最近采用的乐谱与段落；站点配置不可在房间内修改）",
            "  voice status                 （本房间语音配音生效值、映射、静音名单与小时用量）",
            "  voice on|off [--yes]         （本房间语音开关；无法越过站点总开关）",
            "  voice map set <speaker> <profile> [--yes]（绑定 NPC 音色，只能使用内置 NPC 人设）",
            "  voice mute <speaker>|list|clear [--yes]（本房间 NPC 静音名单）",
            "  voice usage                  （本房间小时用量与最近语音事件）",
            "  voice cache stats            （本房间语音缓存条数/体积/命中次数，只读）",
            "  voice cache clear [--yes]    （删除本房间语音缓存，默认预演）",
            "  voice cache prune [--yes]    （按站点/房间配额 LRU 清理本房间缓存，默认预演）",
            *ROOM_COMBAT_HELP.splitlines(),
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
    actor_rank: Any = None,
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
    if domain == "voice":
        return _voice_command(room_dir, room_id, sub, rest, flags, confirm)
    if domain == "combat":
        ok, message = handle_room_combat_command(
            parts[1:],
            room_id=room_id,
            room_dir=room_dir,
            rooms_dir=resolved_rooms_dir,
            scenarios_dir=app.config.get("SCENARIOS_DIR") if app is not None else None,
            config_dir=app.config.get("CONFIG_DIR") if app is not None else None,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
            confirm=confirm,
        )
        return RoomCommandResult(ok, message)
    if domain == "narrative":
        ok, message = handle_room_narrative_command(
            parts[1:],
            room_id=room_id,
            room_dir=room_dir,
            rooms_dir=resolved_rooms_dir,
            app=app,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
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
        return _immersive_status_text(room_id, switches, music_state, room_dir, settings, rules)

    if sub == "music":
        action = rest[0].lower() if rest else "show"
        if action == "show":
            return _immersive_status_text(room_id, switches, music_state, room_dir, settings, rules)
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
        if action in {"search", "rescan"}:
            return _music_external_search(
                room_dir, room_id, rest[1:], flags, info, settings, rules, music_state
            )
        if action in {"switch", "next"}:
            return _music_external_switch(
                room_dir, room_id, rest, action, settings
            )
        return RoomCommandResult(
            False,
            "Usage: immersive music show|on|off|auto <on|off>|set <profile> <intensity>|"
            "search [场景] [--tags=a,b] [--profile=x] [--intensity=y]|switch [序号|track_key|标题]",
        )

    if sub == "illustration":
        action = rest[0].lower() if rest else "show"
        if action == "show":
            return _immersive_status_text(room_id, switches, music_state, room_dir, settings, rules)
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
        if action == "trigger":
            from trpg_server.immersive.config import resolve_illustration_trigger

            if not rest[1:]:
                room_rule = rules.get("immersive_illustration_trigger")
                effective = resolve_illustration_trigger(settings, rules)
                return RoomCommandResult(
                    True,
                    "本房间配图触发档位（只读提示；修改用 immersive illustration trigger <inherit|important|off>）：\n"
                    f"  房间收紧: {room_rule if room_rule in ('important', 'off') else '跟随站点'}\n"
                    f"  站点默认: {settings.get('illustration_trigger')}\n"
                    f"  生效档位: {effective}",
                )
            raw = rest[1].lower()
            if raw in {"show"}:
                return _immersive_command(room_dir, room_id, "illustration", ["trigger"], flags, confirm)
            if raw in {"inherit", "follow", "跟随", "默认"}:
                target: Any = None
            elif raw in {"important", "off"}:
                target = raw
            else:
                return RoomCommandResult(
                    False, "Usage: immersive illustration trigger <inherit|important|off> [--yes]"
                )

            def _apply_trigger() -> RoomCommandResult:
                return _set_house_rule(
                    room_dir, "immersive_illustration_trigger", target, room_id,
                    f"房间配图触发档位已设为 {('跟随站点' if target is None else target)}（房间只能收紧）",
                )

            return _confirm_action(
                preview=f"将把本房间配图触发档位设为『{'跟随站点' if target is None else target}』（只能比站点/个人设置更收紧）。",
                apply=_apply_trigger,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改本房间配图触发档位?",
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
        return RoomCommandResult(
            False,
            "Usage: immersive illustration show|on|off|auto <on|off>|trigger <inherit|important|off>|now [场景]",
        )

    if sub == "cache":
        from trpg_server.immersive import room_cache, scenario_cache
        from trpg_server.immersive.illustration import clear_room_cache, room_cache_stats
        from trpg_server.settings import SCENARIOS_DIR

        scenario_id = str(info.get("scenario_id") or "")
        action = rest[0].lower() if rest else "show"
        if action == "room":
            room_action = rest[1].lower() if len(rest) > 1 else "show"
            if room_action in {"show", "stats"}:
                stats = room_cache_stats(room_dir)
                return RoomCommandResult(
                    True,
                    f"本房间配图缓存：{stats['entries']} 条索引 / {stats['files']} 个文件 / "
                    f"{_human_bytes(int(stats['bytes']))}\n"
                    f"  累计命中 {stats.get('hits') or 0} 次 · 最近命中 {stats.get('last_hit_at') or '-'}\n"
                    f"  目录: {stats.get('directory') or '-'}\n"
                    "  说明：房间缓存落在本房间目录内，随归档保留；只在本房间复用，不跨房间。",
                )
            if room_action == "clear":
                preview = "将删除本房间目录内的配图缓存（图片与索引），不影响剧本缓存。"

                def _apply_room_cache_clear() -> RoomCommandResult:
                    files, removed_bytes = clear_room_cache(room_dir)
                    log_room_event(
                        room_dir, "清空房间配图缓存", source="room_console", actor=ROOM_CONSOLE_ACTOR
                    )
                    return RoomCommandResult(
                        True,
                        f"已清空本房间配图缓存：{files} 个文件 / {_human_bytes(removed_bytes)}。",
                    )

                return _confirm_action(
                    preview=preview,
                    apply=_apply_room_cache_clear,
                    assume_yes=_assume_yes(flags),
                    confirm=confirm,
                    prompt="确认清空本房间配图缓存?",
                )
            return RoomCommandResult(False, "Usage: immersive cache room show|clear [--yes]")
        if action in {"show", "stats"}:
            room_stats = room_cache_stats(room_dir)
            room_text = (
                f"房间缓存：{room_stats['entries']} 条 / {room_stats['files']} 个文件 / "
                f"{_human_bytes(int(room_stats['bytes']))} · 命中 {room_stats.get('hits') or 0} 次"
            )
            if not scenario_id:
                return RoomCommandResult(True, room_text + "\n本房间未关联剧本，仅显示房间缓存。")
            data = scenario_cache.stats(scenario_id, scenarios_dir=SCENARIOS_DIR)
            rows = data.get("scenarios") or []
            if not rows:
                return RoomCommandResult(True, room_text + f"\n剧本 {scenario_id} 暂无配图缓存。")
            row = rows[0]
            return RoomCommandResult(
                True,
                room_text + "\n"
                f"剧本配图缓存（{scenario_id}）：\n"
                f"  文件 {row.get('files')} 个 / {_human_bytes(int(row.get('bytes') or 0))}"
                f"（索引内 {row.get('tracked_files')}，未登记 {row.get('untracked_files')}）\n"
                f"  目录: {row.get('directory')}\n"
                f"  站点配置: 记录新图 {'开' if switches.get('scenario_record') else '关'} · "
                f"优先复用 {'开' if switches.get('scenario_prefer') else '关'}",
            )
        if action == "clear":
            def _apply_room_clear() -> RoomCommandResult:
                files, size = clear_room_cache(room_dir)
                log_room_event(
                    room_dir, "清空房间配图缓存", source="room_console", actor=ROOM_CONSOLE_ACTOR
                )
                return RoomCommandResult(
                    True, f"已清空本房间配图缓存：{files} 个文件 / {_human_bytes(size)}。"
                )

            return _confirm_action(
                preview="将删除本房间目录内的全部配图缓存（不可恢复）；剧本缓存需在站点控制台清理。",
                apply=_apply_room_clear,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认清空本房间配图缓存?",
            )
        if action == "prune":
            def _apply_room_prune() -> RoomCommandResult:
                files, size = room_cache.prune(room_dir, settings)
                return RoomCommandResult(
                    True, f"已按单房间配额清理：{files} 个文件 / {_human_bytes(size)}。"
                )

            return _confirm_action(
                preview="将按单房间容量上限做 LRU 清理（删除最久未命中的房间缓存，默认预演）。",
                apply=_apply_room_prune,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认执行房间缓存 LRU 清理?",
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
            "Usage: immersive cache show|stats|room show|room clear|clear|prune|record <on|off|inherit>|prefer <on|off|inherit>",
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


def _flag_value(flags: set[str], name: str) -> str:
    prefix = f"--{name}="
    for item in flags:
        if item.startswith(prefix):
            return item[len(prefix):].strip()
    return ""


def _music_external_search(
    room_dir: Path,
    room_id: Any,
    args: list[str],
    flags: set[str],
    info: dict[str, Any],
    settings: dict[str, Any],
    rules: dict[str, Any],
    music_state: dict[str, Any],
) -> RoomCommandResult:
    """控制台手动重新检索外链 BGM（force 绕过查询缓存）。"""
    from trpg_server.immersive.config import INTENSITIES, PROFILES, resolve_room_switches
    from trpg_server.immersive.external_music import service as external_music

    if not resolve_room_switches(settings, rules).get("music"):
        return RoomCommandResult(False, "本房间配乐已关闭，先执行 immersive music on")
    engine, engine_reason = external_music.resolve_engine(settings, rules)
    if engine != external_music.ENGINE_EXTERNAL:
        return RoomCommandResult(
            False,
            f"当前引擎为 {engine}（{engine_reason}），无法检索外链曲库；"
            "需站点开启 external 且房间未收紧为 off/procedural。程序化底床仍可正常播放。",
        )

    profile = (_flag_value(flags, "profile") or music_state.get("profile") or settings.get("music_default_profile") or "daily").lower()
    intensity = (_flag_value(flags, "intensity") or music_state.get("intensity") or settings.get("music_default_intensity") or "medium").lower()
    if profile not in PROFILES:
        return RoomCommandResult(False, f"未知档位 {profile}；可选：{', '.join(PROFILES)}")
    if intensity not in INTENSITIES:
        return RoomCommandResult(False, f"未知强度 {intensity}；可选：{', '.join(INTENSITIES)}")
    tags = [item.strip() for item in _flag_value(flags, "tags").replace("，", ",").split(",") if item.strip()][:8]
    scene = " ".join(args).strip()[:120]
    force = "--cache" not in flags

    result = external_music.search_and_select(
        room_dir=room_dir,
        room_id=str(info.get("id") or room_id),
        scenario_id=str(info.get("scenario_id") or ""),
        profile=profile,
        intensity=intensity,
        scene=scene,
        extra_tags=tags,
        settings=settings,
        house_rules=rules,
        trigger="room_console",
        force=force,
    )
    if result.get("status") != "selected":
        reason = result.get("reason") or result.get("status")
        fallback = str(result.get("fallback") or "procedural")
        note = "已降级到程序化底床" if fallback == "procedural" else "外链配乐已停止"
        return RoomCommandResult(
            False,
            f"外链检索未命中（{reason}），{note}；可换场景/标签重试："
            "immersive music search 雨夜街道 --tags=dark,ambient",
        )
    external_music.publish_selection(
        result, room_dir=room_dir, room_id=str(info.get("id") or room_id), settings=settings
    )
    track = result.get("track") or {}
    alternatives = result.get("alternatives") or []
    return RoomCommandResult(
        True,
        f"已重新检索并切曲：{track.get('title') or track.get('track_key')} — {track.get('artist') or '未知作者'}\n"
        f"  许可: {track.get('license') or '-'} · 署名: {track.get('attribution') or '-'} · 来源: {track.get('provider') or '-'}\n"
        f"  备选: {len(alternatives)} 首（用 immersive music switch 换一首）",
    )


def _music_external_switch(
    room_dir: Path,
    room_id: Any,
    rest: list[str],
    action: str,
    settings: dict[str, Any],
) -> RoomCommandResult:
    """在最近一次检索结果中切换曲目；无参数时换下一首。"""
    from trpg_server.immersive.external_music import service as external_music

    state = external_music.load_selection(room_dir)
    track = state.get("track") if isinstance(state.get("track"), dict) else {}
    if not track:
        return RoomCommandResult(False, "尚无外链检索结果；先执行 immersive music search [场景]")
    candidates = [track] + [item for item in (state.get("alternatives") or []) if isinstance(item, dict)]
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.get("track_key") or "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    if action == "next" or len(rest) <= 1:
        if len(deduped) < 2:
            return RoomCommandResult(False, "当前检索结果只有 1 首；先执行 immersive music search 获取更多备选")
        chosen = deduped[1]
    else:
        target = rest[1].strip()
        if target.isdigit():
            index = int(target) - 1
            if index < 0 or index >= len(deduped):
                return RoomCommandResult(False, f"序号超出范围（1-{len(deduped)}）")
            chosen = deduped[index]
        else:
            needle = target.casefold()
            chosen = next(
                (
                    item for item in deduped
                    if needle in str(item.get("track_key") or "").casefold()
                    or needle in str(item.get("title") or "").casefold()
                    or needle in str(item.get("artist") or "").casefold()
                ),
                {},
            )
            if not chosen:
                listing = "；".join(
                    f"{index + 1}. {item.get('title') or item.get('track_key')} — {item.get('artist') or '未知作者'}"
                    for index, item in enumerate(deduped)
                )
                return RoomCommandResult(False, f"未找到匹配曲目。可选：{listing}")

    alternatives = [item for item in deduped if item.get("track_key") != chosen.get("track_key")][:3]
    payload = {
        "ok": True,
        "status": "selected",
        "engine": external_music.ENGINE_EXTERNAL,
        "engine_reason": "room_console",
        "fallback": external_music.ENGINE_PROCEDURAL,
        "track": chosen,
        "alternatives": alternatives,
        "request_id": "",
        "trigger": "room_console",
        "cached": bool(state.get("trigger") != "room_console"),
        "reason": "console_switch",
        "elapsed_ms": 0,
    }
    external_music.publish_selection(
        payload, room_dir=room_dir, room_id=room_id, settings=settings
    )
    return RoomCommandResult(
        True,
        f"已切换外链曲目：{chosen.get('title') or chosen.get('track_key')} — {chosen.get('artist') or '未知作者'}\n"
        f"  许可: {chosen.get('license') or '-'} · 署名: {chosen.get('attribution') or '-'}",
    )


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
    settings: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
) -> RoomCommandResult:
    from trpg_server.immersive.config import (
        load_immersive_settings,
        resolve_illustration_trigger,
        resolve_music_source,
    )
    from trpg_server.immersive.illustration import history_for_room

    if settings is None:
        settings = load_immersive_settings(None)
    if rules is None:
        info = _load_room_info(room_dir)
        rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    effective_source = resolve_music_source(settings, rules)
    effective_trigger = resolve_illustration_trigger(settings, rules)
    history = history_for_room(room_dir, limit=3)
    from trpg_server.immersive.external_music import service as external_music

    engine, engine_reason = external_music.resolve_engine(settings, rules)
    selection = external_music.load_selection(room_dir)
    track = selection.get("track") if isinstance(selection.get("track"), dict) else {}
    if track:
        track_line = (
            f"{track.get('title') or track.get('track_key')} — {track.get('artist') or '未知作者'}"
            f"（{track.get('provider') or '-'} · 许可 {track.get('license') or '-'} · {selection.get('updated_at') or '-'}）"
        )
    else:
        track_line = "尚未检索（外链曲目会叠在程序化底床上）"
    bed_state = {
        "off": "已静音（引擎 off）",
        "external": "运行中（外链播放时自动压低）",
        "procedural": "运行中（程序化生成）",
    }.get(engine, f"未知（{engine}）")
    try:
        from trpg_server.immersive.config import resolve_audio_blocks

        blocks = resolve_audio_blocks(settings, rules)
        block_names = blocks.get("blocks") or []
        if block_names:
            bed_state = "运行中" if "procedural" in block_names else "关闭（音频块未选中 procedural）"
    except Exception:  # noqa: BLE001 - 兼容尚未启用音频块的版本
        pass
    lines = [
        f"房间 {room_id} 沉浸状态：",
        f"  配乐: {'开' if switches.get('music') else '关'} · 来源 {effective_source}（站点 {settings.get('music_source')}）· "
        f"{music_state.get('profile') or '-'} / {music_state.get('intensity') or '-'} · 第 {music_state.get('section') or 0} 段 · "
        f"回合 {music_state.get('turns') or 0}",
        f"  引擎: {engine}（{engine_reason}）· 底床: {bed_state}",
        f"  当前外链曲目: {track_line}",
        f"  配图: {'开' if switches.get('illustration') else '关'} · 自动 {'开' if switches.get('illustration_auto') else '关'} · "
        f"触发档位 {effective_trigger}（站点 {settings.get('illustration_trigger')}）· "
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


def _voice_command(
    room_dir: Path,
    room_id: Any,
    sub: str,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> RoomCommandResult:
    """房间级语音命令；只读写本房间目录，无法越过站点配置。"""
    from trpg_server.voice import events
    from trpg_server.voice.config import (
        load_room_voice,
        load_voice_settings,
        resolve_room_voice,
        room_usage_snapshot,
        save_room_voice,
    )
    from trpg_server.voice.engine import engine_health
    from trpg_server.voice.profiles import PROFILE_MAP, PROFILE_IDS

    settings = load_voice_settings(None)
    info = _load_room_info(room_dir)
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    effective = resolve_room_voice(settings, rules)
    data = load_room_voice(room_dir)
    usage = room_usage_snapshot(data)

    if sub == "status":
        health = engine_health(settings)
        lines = [
            f"房间 {room_id} 语音状态：",
            f"  生效: {'开' if effective['enabled'] else '关'}（站点 {'开' if settings['enabled'] else '关'} · "
            f"房间规则 {'跟随' if rules.get('voice_enabled') is None else ('开' if rules.get('voice_enabled') else '关')}）",
            f"  引擎: {effective['engine']} · {'可用' if health.get('ok') else '不可用（' + str(health.get('reason')) + '）'}",
            f"  上限: 单回合 {effective['max_turn_chars']} 字 · 单段 {effective['max_segment_chars']} 字 · "
            f"最多 {effective['max_segments']} 段 · 每小时 {effective['room_hourly_cap'] or '不限'} 回合",
            f"  ducking: {'开' if effective['ducking'] else '关'} · 级别 {effective['ducking_level']}",
            f"  本小时用量: {usage['count']}（{usage['hour'] or '-'}）",
            f"  NPC 映射: {len(data['npc_profiles'])} 条 · 静音: {', '.join(data['muted']) or '无'}",
        ]
        for name, profile_id in sorted(data["npc_profiles"].items()):
            label = PROFILE_MAP.get(profile_id).label if profile_id in PROFILE_MAP else "未知"
            lines.append(f"    {name} -> {profile_id}（{label}）")
        return RoomCommandResult(True, "\n".join(lines))

    if sub in {"on", "off"}:
        target = sub == "on"
        if target and not settings["enabled"]:
            return RoomCommandResult(False, "站点语音总开关关闭；房间只能收紧，无法单独开启。请先由管理员执行 voice on。")

        def _apply_toggle() -> RoomCommandResult:
            return _set_house_rule(
                room_dir, "voice_enabled", target, room_id,
                f"房间语音已{'开启' if target else '关闭'}",
            )

        return _confirm_action(
            preview=f"将把本房间语音设为『{'开' if target else '关'}』（无法越过站点总开关）。",
            apply=_apply_toggle,
            assume_yes=_assume_yes(flags),
            confirm=confirm,
            prompt="确认修改本房间语音开关?",
        )

    if sub == "map":
        action = rest[0].lower() if rest else "list"
        if action == "list":
            return _voice_command(room_dir, room_id, "status", [], flags, confirm)
        if action == "set":
            if len(rest) < 3:
                return RoomCommandResult(False, "Usage: voice map set <speaker> <profile> [--yes]")
            speaker = rest[1]
            profile_id = rest[2].lower()
            if profile_id not in PROFILE_MAP or PROFILE_MAP[profile_id].kind != "npc":
                return RoomCommandResult(
                    False,
                    f"未知 NPC 人设: {profile_id}；可选: {', '.join(PROFILE_IDS[1:])}",
                )

            def _apply_map() -> RoomCommandResult:
                fresh = load_room_voice(room_dir)
                fresh["npc_profiles"][speaker] = profile_id
                if not save_room_voice(room_dir, fresh):
                    return RoomCommandResult(False, "写入房间 voice.json 失败。")
                return RoomCommandResult(True, f"已将「{speaker}」映射到 {profile_id}（{PROFILE_MAP[profile_id].label}）")

            return _confirm_action(
                preview=f"将把本房间「{speaker}」映射到 {profile_id}（{PROFILE_MAP[profile_id].label}）。",
                apply=_apply_map,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认修改 NPC 音色映射?",
            )
        return RoomCommandResult(False, "Usage: voice map list|set <speaker> <profile>")

    if sub == "mute":
        action = rest[0].lower() if rest else "list"
        if action == "list" or not rest:
            return RoomCommandResult(True, "本房间静音名单: " + (", ".join(data["muted"]) or "无"))
        if action == "clear":
            def _apply_clear() -> RoomCommandResult:
                fresh = load_room_voice(room_dir)
                fresh["muted"] = []
                save_room_voice(room_dir, fresh)
                return RoomCommandResult(True, "已清空本房间静音名单。")

            return _confirm_action(
                preview="将清空本房间 NPC 静音名单。",
                apply=_apply_clear,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认清空静音名单?",
            )
        target = rest[0]
        remove = False
        if target.lower() in {"off", "remove", "unmute"} and len(rest) > 1:
            remove = True
            target = rest[1]

        def _apply_mute() -> RoomCommandResult:
            fresh = load_room_voice(room_dir)
            muted = [str(item) for item in fresh.get("muted") or []]
            lowered = {item.lower() for item in muted}
            if remove:
                muted = [item for item in muted if item.lower() != target.lower()]
                message = f"已从静音名单移除「{target}」。"
            elif target.lower() in lowered:
                muted = [item for item in muted if item.lower() != target.lower()]
                message = f"「{target}」已在静音名单中，现已取消静音。"
            else:
                muted.append(target)
                message = f"已将「{target}」加入静音名单。"
            fresh["muted"] = muted
            save_room_voice(room_dir, fresh)
            return RoomCommandResult(True, message)

        return _confirm_action(
            preview=f"将{'取消' if remove else '切换'}本房间 NPC「{target}」的静音状态。",
            apply=_apply_mute,
            assume_yes=_assume_yes(flags),
            confirm=confirm,
            prompt="确认修改静音名单?",
        )

    if sub == "cache":
        from trpg_server.voice import local_cache

        action = rest[0].lower() if rest else "stats"

        def _mb(value: int | float) -> str:
            return f"{float(value or 0) / 1024 / 1024:.1f} MB"

        if action in {"stats", "show"}:
            info = local_cache.stats(room_dir)
            lines = [
                f"房间 {room_id} 语音缓存：",
                f"  文件: {info['files']} 条 / 上限 {effective.get('cache_max_entries') or '不限'} 条",
                f"  体积: {_mb(info['bytes'])} / 上限 {int(effective.get('cache_quota_mb') or 0) or '不限'} MB",
                f"  命中次数: {info['hits']}，最近命中: {info['last_hit_at'] or '-'}",
                f"  开关: {'开' if effective.get('cache_enabled') else '关'}，超限策略: {effective.get('cache_overflow') or 'lru'}",
                f"  目录: {info['directory'] or '-'}",
            ]
            return RoomCommandResult(True, "\n".join(lines))

        if action == "clear":
            info = local_cache.stats(room_dir)
            preview = (
                f"将删除本房间语音缓存：{info['files']} 个文件，{_mb(info['bytes'])}。"
                "（不影响模型权重与其他房间缓存）"
            )

            def _apply_clear() -> RoomCommandResult:
                files, size = local_cache.clear(room_dir)
                return RoomCommandResult(True, f"已清理本房间语音缓存：{files} 个文件，{_mb(size)}。")

            return _confirm_action(
                preview=preview,
                apply=_apply_clear,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认清空本房间语音缓存?",
            )

        if action == "prune":
            preview = (
                f"将按 LRU 清理本房间语音缓存，使总条数不超过 {effective.get('cache_max_entries') or '不限'}、"
                f"总体积不超过 {int(effective.get('cache_quota_mb') or 0) or '不限'} MB。"
            )

            def _apply_prune() -> RoomCommandResult:
                removed, freed = local_cache.prune(room_dir, effective)
                return RoomCommandResult(True, f"已按 LRU 清理本房间语音缓存：{removed} 个文件，释放 {_mb(freed)}。")

            return _confirm_action(
                preview=preview,
                apply=_apply_prune,
                assume_yes=_assume_yes(flags),
                confirm=confirm,
                prompt="确认清理本房间语音缓存?",
            )

        return RoomCommandResult(False, "Usage: voice cache stats|clear|prune [--yes]")

    if sub == "usage":
        recent = [item for item in events.recent(50) if str(item.get("room_id") or "") == str(room_id)]
        lines = [
            f"房间 {room_id} 语音用量：",
            f"  本小时: {usage['count']} 回合（{usage['hour'] or '-'}，上限 {effective['room_hourly_cap'] or '不限'}）",
            f"  最近事件: {len(recent)} 条",
        ]
        for item in recent[:10]:
            lines.append(
                f"    {item.get('ts') or '-'} {item.get('event') or '-'} "
                f"speaker={item.get('speaker') or '-'} profile={item.get('profile_id') or '-'} "
                f"reason={item.get('reason') or '-'}"
            )
        return RoomCommandResult(True, "\n".join(lines))

    return RoomCommandResult(False, "Usage: voice status|on|off|map <list|set>|mute <speaker|list|clear>|usage")
