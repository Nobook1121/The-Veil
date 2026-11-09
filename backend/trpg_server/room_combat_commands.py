"""战斗轮的房间控制台命令。

把 ``agents.tools.combat`` 的能力暴露给房间控制台，供房主/房间管理员在不依赖
Agent 的情况下开局、推进和结算。命令只作用于当前房间；权限由调用方
（``execute_role``）与本模块的房主/管理员检查双重把关。

日志分流：
- 战斗事件由 combat 工具写入房间日志（source=combat），玩家可见；
- 命令回显与失败原因由房间控制台路由记录（成功 INFO、失败 WARNING）；
- 本模块不重复写 logger，避免同一失败既 warn 又 error。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trpg_server.agents.tools import combat
from trpg_server.console_args import ConsoleArgSpec, first_positional, parse_console_args
from trpg_server.json_store import read_json

ROOM_COMBAT_ACTOR = "房间控制台"

COMBAT_DESTRUCTIVE = {
    "start", "join", "next", "end-turn", "endturn",
    "delay", "disengage", "reload", "ready", "cast",
    "damage", "heal", "treat", "end",
}

_DICE_PATTERN = re.compile(r"^(?P<count>\d{1,2})[dD](?P<size>\d{1,3})(?P<mod>[+-]\d+)?$")


ROOM_COMBAT_HELP = "\n".join(
    [
        "  combat                       （查看当前战斗：轮次、顺序、耐久与状态）",
        "  combat status                （同上，只读）",
        "  combat start [成员...] [module id] [initiative-check] [yes]（开战；缺省取所有已绑定角色卡的成员）",
        "  combat join <成员> [initiative-check] [yes]（战斗中追加先攻；也可写 combat join module <id>）",
        "  combat next                  （推进到下一位行动者/下一轮）",
        "  combat end-turn              （同 combat next）",
        "  combat delay [成员]          （把当前行动者延迟到本轮末尾；已是最后一人则放弃行动）",
        "  combat disengage [成员]      （脱离近战，消耗本次行动）",
        "  combat reload [成员] [武器]  （装填/换弹，消耗本次行动）",
        "  combat ready [成员]          （准备武器；耗时由房规 combat_ready_weapon_cost 决定）",
        "  combat cast <法术> [caster 名字] [time instant|one_round|two_rounds] [mp n] [san n]（战斗内施法）",
        "  combat damage <目标> <数值|XdY+N> [ignore-armor] [yes]（手动伤害）",
        "  combat heal <目标> <数值> [yes]（手动恢复耐久）",
        "  combat treat <目标> [first-aid|medicine] [healer 名字] [yes]（急救/医学检定）",
        "  combat end [reason 原因] [yes]（结束战斗，破坏性）",
        "  参数用空格分隔的纯词；旧的 --xxx / --key=value 写法仍然可用。",
    ]
)

# 各子命令认识的纯词参数。
COMBAT_ARG_SPECS = {
    "start": ConsoleArgSpec.of(flags={"initiative-check", "yes"}, values={"module"}),
    "join": ConsoleArgSpec.of(flags={"initiative-check", "yes"}, values={"module"}),
    "cast": ConsoleArgSpec.of(values={"caster", "time", "mp", "san"}),
    "damage": ConsoleArgSpec.of(flags={"ignore-armor", "ignore-armour", "yes"}),
    "heal": ConsoleArgSpec.of(flags={"yes"}),
    "treat": ConsoleArgSpec.of(flags={"yes"}, values={"healer"}),
    "end": ConsoleArgSpec.of(flags={"yes"}, trailing={"reason"}),
}


@dataclass
class _RoomCombatContext:
    room_dir: Path
    room_id: str
    scenarios_dir: Path | None
    config_dir: Path | None
    info: dict[str, Any]

    def room_info(self) -> dict[str, Any]:
        return self.info


def _build_context(
    room_dir: Path,
    room_id: Any,
    scenarios_dir: Any,
    config_dir: Any,
) -> _RoomCombatContext:
    info = read_json(room_dir / "info.json", default={}) or {}
    return _RoomCombatContext(
        room_dir=Path(room_dir),
        room_id=str(room_id or ""),
        scenarios_dir=Path(scenarios_dir) if scenarios_dir else None,
        config_dir=Path(config_dir) if config_dir else None,
        info=info if isinstance(info, dict) else {},
    )


def _actor_rank(room_dir: Path, actor_id: Any, actor_rank: Any) -> int:
    if actor_rank is not None:
        try:
            return int(actor_rank)
        except (TypeError, ValueError):
            return 0
    if actor_id is None:
        return 0
    from trpg_server.routes.rooms import _find_member, _room_permission

    info = read_json(room_dir / "info.json", default={}) or {}
    member = _find_member(info, user_id=str(actor_id), active_only=True)
    role = _room_permission(member, info)
    return {"owner": 3, "admin": 2, "member": 1}.get(str(role or ""), 0)


def _parse(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    spec = COMBAT_ARG_SPECS.get(first_positional(args).lower(), ConsoleArgSpec.of())
    parsed = parse_console_args(args, spec)
    return parsed.positionals, parsed.flags, parsed.options


def _roll_amount(text: str) -> tuple[int, str]:
    """把 ``2D6+1`` 之类的表达式转成总伤害；非法输入返回 (0, 原因)。"""
    raw = str(text or "").strip()
    if raw.isdigit():
        return max(0, int(raw)), ""
    matched = _DICE_PATTERN.match(raw)
    if not matched:
        return 0, f"伤害「{raw}」不是数字或 XdY+N 形式"
    count = max(1, min(20, int(matched.group("count"))))
    size = max(2, min(100, int(matched.group("size"))))
    modifier = int(matched.group("mod") or 0)
    rolls = [random.randint(1, size) for _ in range(count)]
    total = max(0, sum(rolls) + modifier)
    detail = f"{raw}={total}（{'+'.join(str(item) for item in rolls)}" + (f"{modifier:+d}" if modifier else "") + "）"
    return total, detail


def _current_name(combat: dict[str, Any]) -> str:
    order = combat.get("order") or []
    turn = int(combat.get("turn") or 0)
    return str(order[turn]) if 0 <= turn < len(order) else ""


def _as_option_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _format_state(state: dict[str, Any]) -> str:
    if not state.get("active"):
        return "当前没有进行中的战斗。使用 combat start 开战。"
    lines = [f"第 {state.get('round')} 轮，当前行动者：{state.get('current') or '-'}"]
    for index, item in enumerate(state.get("order") or []):
        marker = "▶" if item.get("name") == state.get("current") else " "
        status = item.get("status") or "active"
        extra = []
        if item.get("major_wound"):
            extra.append("重伤")
        if item.get("dying"):
            extra.append("濒死")
        if item.get("disengaged"):
            extra.append("脱离")
        if item.get("ready_weapon"):
            extra.append("已备武器")
        suffix = f" [{'/'.join(extra)}]" if extra else ""
        lines.append(
            f"{marker} {index + 1}. {item.get('name')}  DEX {item.get('dex')}  先攻 {item.get('initiative')}  "
            f"HP {item.get('hp')}/{item.get('max_hp')}  {status}{suffix}"
        )
    return "\n".join(lines)


def handle_room_combat_command(
    args: list[str],
    *,
    room_id: Any,
    room_dir: Any,
    rooms_dir: Any = None,
    scenarios_dir: Any = None,
    config_dir: Any = None,
    actor: Any = None,
    actor_id: Any = None,
    actor_rank: Any = None,
    confirm: Any = None,
) -> tuple[bool, str]:
    """执行 ``combat <subcommand>``；返回 (ok, 文本)。"""
    positionals, flags, options = _parse(args)
    room_dir = Path(room_dir)
    context = _build_context(room_dir, room_id, scenarios_dir, config_dir)
    sub = positionals[0].lower() if positionals else "status"
    rest = positionals[1:]

    if sub in {"status", "state", "order"}:
        return True, _format_state(combat.combat_state({}, context))
    if sub not in COMBAT_DESTRUCTIVE:
        return False, f"未知的战斗命令：combat {sub}\n{ROOM_COMBAT_HELP}"

    # 破坏性/改变战斗状态的动作只允许房主/房间管理员，即使路由权限被放宽也能兜底。
    if _actor_rank(room_dir, actor_id, actor_rank) < 2:
        return False, "只有房主/房间管理员可以执行该战斗命令"

    active = combat._load_combat(context)
    if sub in {"next", "end-turn", "endturn"}:
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        result = combat.next_turn({}, context)
        if result.get("error"):
            return False, result["error"]
        return True, _format_state(result)
    if sub == "delay":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        current = _current_name(active)
        if rest and rest[0] != current:
            return False, f"只能延迟当前行动者（{current}）"
        result = combat.delay_combatant({}, context)
    elif sub in {"disengage", "reload", "ready"}:
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        payload: dict[str, Any] = {}
        if rest:
            payload["actor"] = rest[0]
        if sub == "reload" and len(rest) > 1:
            payload["weapon"] = rest[1]
        handler = {
            "disengage": combat.disengage_combatant,
            "reload": combat.reload_combatant,
            "ready": combat.ready_combatant,
        }[sub]
        result = handler(payload, context)
    elif sub == "cast":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        spell = rest[0] if rest else options.get("spell", "")
        if not spell:
            return False, "用法：combat cast <法术> [caster 名字] [time instant|one_round|two_rounds] [mp n] [san n]"
        payload = {
            "spell": spell,
            "cast_time": options.get("time") or options.get("cast_time") or "instant",
            "mp_cost": _as_option_int(options.get("mp")),
            "san_cost": _as_option_int(options.get("san")),
        }
        if options.get("caster"):
            payload["caster"] = options["caster"]
        result = combat.cast_spell(payload, context)
    elif sub == "start":
        if active:
            return False, "当前已有进行中的战斗；先 combat end 结束，或继续用 combat next 推进"
        participants = rest
        module_ids = [options["module"]] if options.get("module") else []
        result = combat.start_combat(
            {
                "participants": participants,
                "module_ids": module_ids,
                "initiative_check": bool(flags & {"--initiative-check", "--initiative"}),
            },
            context,
        )
    elif sub == "join":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        raw_target = rest[0] if rest else options.get("module", "")
        if not raw_target:
            return False, "用法：combat join <成员> 或 combat join module <id>"
        payload = {"initiative_check": bool(flags & {"--initiative-check", "--initiative"})}
        if options.get("module"):
            payload["module_id"] = options["module"]
        else:
            payload["participant"] = raw_target
        result = combat.join_combat(payload, context)
    elif sub == "damage":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        if len(rest) < 2:
            return False, "用法：combat damage <目标> <数值|XdY+N> [ignore-armor]"
        amount, detail = _roll_amount(rest[1])
        if amount <= 0 and detail:
            return False, detail
        result = combat.damage_combatant(
            {
                "target": rest[0],
                "amount": amount,
                "ignore_armor": bool(flags & {"--ignore-armor", "--ignore-armour"}),
            },
            context,
        )
        if not result.get("error"):
            result["summary"] = f"{result.get('summary') or ''} {detail}".strip()
    elif sub == "heal":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        if len(rest) < 2 or not rest[1].isdigit():
            return False, "用法：combat heal <目标> <正整数>"
        result = combat.heal_combatant({"target": rest[0], "amount": int(rest[1])}, context)
    elif sub == "treat":
        if not active:
            return False, "当前没有进行中的战斗，请先 combat start"
        if not rest:
            return False, "用法：combat treat <目标> [first-aid|medicine] [healer 名字]"
        mode = rest[1].lower() if len(rest) > 1 else "first-aid"
        mode = "first_aid" if mode in {"first-aid", "firstaid", "急救"} else "medicine"
        payload = {"target": rest[0], "method": mode}
        if options.get("healer"):
            payload["healer"] = options["healer"]
        result = combat.treat_combatant(payload, context)
    else:  # end
        if not active:
            return False, "当前没有进行中的战斗"
        if not (flags & {"--yes", "-y"}):
            if confirm is None:
                return True, "将结束当前战斗（保留角色最终耐久）。确认无误后加 yes 执行（旧 --yes 仍兼容）。"
            if not confirm("结束当前战斗？(y/N)"):
                return True, "已取消，未结束战斗。"
        result = combat.end_combat({"reason": options.get("reason", "")}, context)

    if result.get("error"):
        return False, str(result["error"])
    if sub == "end":
        return True, "战斗已结束。"
    if sub == "start":
        return True, "战斗开始。\n" + _format_state(combat.combat_state({}, context))
    return True, result.get("summary") or "已执行。"
