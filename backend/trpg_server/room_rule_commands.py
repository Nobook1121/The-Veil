"""房间控制台 ``rule`` 域：不依赖 AI KP 的检定 / SAN / 物品 / 限频操作。

命令只作用于当前房间；房主 / 房间管理员校验由路由与 ``_require_operator``
双重把关。默认严格对齐项目约定：
- ``rule check`` 只读结算，不改变数值；
- ``rule item`` 与聊天 ``/item`` 使用同一实现；
- ``rule san add|set`` 默认预演，加 ``yes`` 才落盘（旧 ``--yes`` 仍兼容）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from trpg_server import room_rules, world_override

ROOM_RULE_ACTOR = "房间控制台"

# 纯词开关：写成 leverage / weapon / persist / yes，不再要求 -- 前缀。
RULE_FLAG_WORDS = {
    "leverage": "--leverage",
    "hometown": "--hometown",
    "remote": "--remote",
    "profession": "--profession",
    "push": "--push",
    "weapon": "--weapon",
    "persist": "--persist",
    "apply": "--apply",
    "yes": "--yes",
}
# 带值选项：写成 key value。
RULE_VALUE_OPTIONS = {
    "difficulty",
    "adjustment",
    "bonus",
    "penalty",
    "attitude",
    "target-skill",
    "mode",
    "era",
    "assets",
    "qty",
    "kind",
    "damage",
}
# 尾部选项：reason / note 之后的全部内容都算它的值。
RULE_TRAILING_OPTIONS = {"reason", "note"}

ROOM_RULE_HELP = "\n".join(
    [
        "  rule                        （查看本房间规则集、生效限频与成员 SAN 概览）",
        "  rule status                 （同上，只读）",
        "  rule check <玩家> <技能/属性> [difficulty normal|hard|extreme] [adjustment ±N] [bonus N] [penalty N]（结算一次检定）",
        "  rule social <玩家> <技能> [attitude supportive|neutral|opposed|hostile] [leverage] [target-skill N]（房规：社交难度）",
        "  rule combined <玩家> <技能1> <技能2> [...] [mode all|any] [difficulty normal|hard|extreme] [bonus N] [penalty N]（房规：组合检定只掷一次）",
        "  rule groupluck              （房规：由在场最低幸运者代掷团体幸运）",
        "  rule contact <玩家> <技能> [hometown] [remote] [profession] [push] [difficulty normal|hard|extreme]（房规：建立熟人）",
        "  rule economy <玩家> [金额] [era 1920s|modern] [assets N] [apply] [reason 原因]（房规：信用评级与开支；默认只读预览，apply 登记现金写回）",
        "  rule san add <玩家> <±N> [reason 原因] [yes]（调整 SAN；默认预演）",
        "  rule san set <玩家> <N> [reason 原因] [yes]（设为指定 SAN；默认预演）",
        "  rule item <玩家> <物品名> [qty N] [weapon] [persist] [reason 原因]（追加物品/武器；persist 才写回持久卡）",
        "  rule skill <玩家> <技能名> <值|±N> [persist] [reason 原因] [yes]（修改技能；默认预演）",
        "  rule attr <玩家> <属性> <值|±N> [persist] [reason 原因] [yes]（修改 STR/DEX/INT 等基础属性；默认预演）",
        "  rule limits                 （查看世界意志的生效频率限制）",
        "  rule limits cooldown <秒|inherit>（房间只能比站点更严格）",
        "  rule limits hourly <次数|inherit>（房间只能比站点更严格）",
        "  参数用空格分隔的纯词；旧的 --xxx / --key=value 写法仍然可用。",
    ]
)

RULE_SUBS = {
    "status",
    "check",
    "social",
    "combined",
    "groupluck",
    "contact",
    "economy",
    "san",
    "item",
    "skill",
    "attr",
    "limits",
}


def _parse(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    """纯词参数解析。

    ``rule item 玩家 手电筒 qty 2 weapon persist`` 与旧的
    ``rule item 玩家 手电筒 --qty=2 --weapon --persist`` 等价；
    ``-5`` 这类带符号数值始终按位置参数处理。
    """
    positionals: list[str] = []
    flags: set[str] = set()
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        lowered = token.lower()
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            options[key[2:].lower()] = value
        elif token.startswith("-") and not re.fullmatch(r"-\d+(?:\.\d+)?", token):
            flags.add(token.lower())
        elif lowered in RULE_FLAG_WORDS:
            flags.add(RULE_FLAG_WORDS[lowered])
        elif lowered in RULE_VALUE_OPTIONS:
            if index + 1 < len(args):
                options[lowered] = args[index + 1]
                index += 1
        elif lowered in RULE_TRAILING_OPTIONS:
            options[lowered] = " ".join(args[index + 1:])
            break
        else:
            positionals.append(token)
        index += 1
    return positionals, flags, options


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_number(value: Any) -> float | None:
    """解析金额；空值或非法值返回 ``None``。"""
    raw = str(value or "").strip().replace("$", "").replace("¥", "").replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _signed(text: Any) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw[0] not in "+-" and not raw.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _require_operator(actor_rank: Any) -> bool:
    return room_rules._actor_rank_value(actor_rank) >= 2


def _assume_yes(flags: set[str]) -> bool:
    return bool(flags & {"--yes", "-y"})


def _settings_policy(config_dir: Any) -> dict[str, int]:
    return world_override.resolve_policy(config_dir)


def _format_status(info: dict[str, Any], *, config_dir: Any) -> str:
    policy = world_override.resolve_policy(config_dir, info.get("house_rules"))
    lines = [f"规则集：{room_rules.room_rule_label(info)}"]
    lines.append(
        "世界意志限频：冷却 "
        f"{policy['cooldown_seconds']} 秒 / 每小时 {policy['hourly_limit'] or '不限'} 次"
    )
    members = [m for m in (info.get("members") or []) if isinstance(m, dict)]
    if not members:
        lines.append("本房间还没有成员。")
        return "\n".join(lines)
    lines.append("成员 SAN / 物品：")
    for member in members:
        if member.get("status") == "removed" or member.get("is_active", True) is False:
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
        if not card:
            lines.append(f"  {member.get('username') or '?'}：未绑定角色卡")
            continue
        snapshot = room_rules.san_snapshot(member)
        equipment = card.get("equipment") if isinstance(card.get("equipment"), list) else []
        weapons = card.get("weapons") if isinstance(card.get("weapons"), list) else []
        lines.append(
            f"  {room_rules.member_display_name(member)}（{member.get('username') or '-'}）："
            f"SAN {snapshot['current']}/{snapshot['max']}，物品 {len(equipment)} 件，武器 {len(weapons)} 件"
        )
    return "\n".join(lines)


def handle_room_rule_command(
    args: list[str],
    *,
    room_id: Any,
    room_dir: Any,
    actor: Any = None,
    actor_id: Any = None,
    actor_rank: Any = None,
    confirm: Callable[[str], bool] | None = None,
    config_dir: Any = None,
    app: Any = None,
) -> tuple[bool, str]:
    positionals, flags, options = _parse(args)
    sub = positionals[0].lower() if positionals else "status"
    rest = positionals[1:]
    room_dir = Path(room_dir)
    info = room_rules.load_room_info(room_dir)
    if config_dir is None and app is not None:
        config_dir = app.config.get("CONFIG_DIR")

    if sub not in RULE_SUBS:
        return False, f"未知的规则命令：rule {sub}\n{ROOM_RULE_HELP}"

    # 批次 1 房规：开关关闭时这些入口等同于不存在，行为与旧版本一致。
    from trpg_server.ruleset.coc7.options import resolve_options

    coc7_options = resolve_options(info)
    optional_subs = {
        "social": coc7_options.social_difficulty,
        "combined": coc7_options.combined_checks,
        "groupluck": coc7_options.group_luck,
        "contact": coc7_options.contacts,
        "economy": coc7_options.economy,
    }
    if sub in optional_subs and not optional_subs[sub]:
        return False, f"未知的规则命令：rule {sub}\n{ROOM_RULE_HELP}"

    if sub == "status":
        return True, _format_status(info, config_dir=config_dir)

    if not _require_operator(actor_rank):
        return False, "只有房主 / 房间管理员可以执行该规则命令"

    if sub == "social":
        if len(rest) < 2:
            return False, (
                "用法：rule social <玩家> <技能> "
                "[attitude supportive|neutral|opposed|hostile] [leverage] [target-skill N]"
            )
        raw_target = options.get("target-skill")
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_rules.roll_social_check(
                room_dir,
                info,
                member,
                name=" ".join(rest[1:]),
                target_skill=_as_int(raw_target, 0) if raw_target is not None else None,
                attitude=str(options.get("attitude") or "neutral"),
                leverage="--leverage" in flags,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, str(result.get("summary") or "检定完成")

    if sub == "combined":
        if len(rest) < 3:
            return False, (
                "用法：rule combined <玩家> <技能1> <技能2> [...] "
                "[mode all|any] [difficulty normal|hard|extreme] [bonus N] [penalty N]"
            )
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_rules.roll_combined_check(
                room_dir,
                info,
                member,
                rest[1:],
                mode=str(options.get("mode") or "all"),
                difficulty=str(options.get("difficulty") or "regular"),
                bonus=_as_int(options.get("bonus"), 0),
                penalty=_as_int(options.get("penalty"), 0),
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        lines = [str(result.get("summary") or "检定完成")]
        for row in result.get("skills") or []:
            lines.append(
                f"  {row.get('display_name') or row.get('name')}："
                f"{row.get('value')} / {row.get('threshold')}（{row.get('level')}）"
            )
        return True, "\n".join(lines)

    if sub == "groupluck":
        if rest:
            return False, "用法：rule groupluck"
        try:
            result = room_rules.roll_group_luck(
                room_dir,
                info,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, str(result.get("summary") or "检定完成")

    if sub == "contact":
        if len(rest) < 2:
            return False, (
                "用法：rule contact <玩家> <技能> [hometown] [remote] [profession] "
                "[push] [difficulty normal|hard|extreme]"
            )
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_rules.roll_contact_check(
                room_dir,
                info,
                member,
                name=" ".join(rest[1:]),
                in_hometown="--hometown" in flags,
                remote="--remote" in flags,
                same_profession="--profession" in flags,
                base_difficulty=str(options.get("difficulty") or "regular"),
                pushed="--push" in flags,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, str(result.get("summary") or "检定完成")

    if sub == "economy":
        if not rest:
            return False, (
                "用法：rule economy <玩家> [金额] [era 1920s|modern] [assets N] "
                "[apply] [reason 原因]"
            )
        amount = _as_number(rest[1]) if len(rest) > 1 else None
        if len(rest) > 1 and amount is None:
            return False, "金额必须是数字，例如 25 或 $12.5"
        assets = _as_number(options.get("assets")) if options.get("assets") is not None else None
        if options.get("assets") is not None and assets is None:
            return False, "assets 必须是数字"
        apply_write = "--apply" in flags or "--yes" in flags
        if apply_write and amount is None:
            return False, "写回现金需要同时提供金额，例如：rule economy 玩家 25 apply"
        if apply_write and assets is not None:
            return False, "其他资产字段为自由文本，写回只覆盖现金；assets 仅用于预览"
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            if apply_write:
                result = room_rules.apply_economy_expense(
                    room_dir,
                    info,
                    member,
                    amount=amount,
                    era=str(options.get("era") or "1920s"),
                    reason=str(options.get("reason") or ""),
                    actor=actor or ROOM_RULE_ACTOR,
                    actor_id=actor_id,
                    config_dir=config_dir,
                    room_id=room_id,
                )
            else:
                result = room_rules.economy_summary(
                    member,
                    amount=amount,
                    era=str(options.get("era") or "1920s"),
                    assets=assets,
                    info=info,
                )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, str(result.get("summary") or "计算完成")

    if sub == "check":
        if len(rest) < 2:
            return False, "用法：rule check <玩家> <技能/属性> [difficulty hard|extreme]"
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_rules.roll_check(
                room_dir,
                info,
                member,
                name=" ".join(rest[1:]),
                difficulty=str(options.get("difficulty") or "regular"),
                adjustment=options.get("adjustment"),
                bonus=_as_int(options.get("bonus"), 0),
                penalty=_as_int(options.get("penalty"), 0),
                reason=str(options.get("reason") or ""),
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, str(result.get("summary") or "检定完成")

    if sub == "san":
        return _san_command(
            room_dir,
            info,
            rest,
            flags,
            options,
            confirm,
            room_id=room_id,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
        )

    if sub == "item":
        if len(rest) < 2:
            return False, "用法：rule item <玩家> <物品名> [qty N] [weapon] [persist]"
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_rules.add_item(
                room_dir,
                info,
                member,
                name=" ".join(rest[1:]),
                kind="weapon" if "--weapon" in flags else str(options.get("kind") or "item"),
                quantity=_as_int(options.get("qty"), 1),
                note=str(options.get("note") or ""),
                damage=str(options.get("damage") or ""),
                skill=str(options.get("skill") or ""),
                reason=str(options.get("reason") or ""),
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                source="rule_command",
            )
            message = (
                f"{result['player_name']} 获得{'武器' if result['kind'] == 'weapon' else '物品'}"
                f"「{result['item'].get('name')}」×{result['quantity']}（仅本房间快照）"
            )
            if "--persist" in flags:
                message += _persist_member(
                    member,
                    room_dir=room_dir,
                    info=info,
                    item_record_id=(result.get("record") or {}).get("id"),
                )
        except room_rules.RuleOperationError as exc:
            return False, exc.message
        return True, message

    if sub in {"skill", "attr"}:
        return _stat_command(
            room_dir,
            info,
            rest,
            flags,
            options,
            confirm,
            kind=sub,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
        )

    return _limits_command(room_dir, info, rest, flags, options, config_dir=config_dir)


def _stat_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    options: dict[str, str],
    confirm: Callable[[str], bool] | None,
    *,
    kind: str,
    actor: Any,
    actor_id: Any,
    actor_rank: Any,
) -> tuple[bool, str]:
    label = "技能" if kind == "skill" else "属性"
    usage = (
        f"用法：rule {kind} <玩家> <{label}名> <值|±N> [persist] [reason 原因] [yes]（默认预演）"
    )
    if len(rest) < 3:
        return False, usage
    amount = _signed(rest[-1])
    if amount is None:
        return False, f"{label}变更值必须是整数，例如 65 / +5 / -5"
    raw_value = str(rest[-1]).strip()
    relative = raw_value[0] in "+-"
    target_name = " ".join(rest[1:-1]).strip()
    if not target_name:
        return False, usage
    try:
        member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
        card = room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return False, exc.message

    name = room_rules.member_display_name(member)
    if kind == "skill":
        row = room_rules._find_skill_row(card, target_name)
        if row is None:
            return False, f"角色卡上没有技能「{target_name}」"
        before = max(0, min(100, room_rules._as_int(row.get("value"), 0)))
        after = max(0, min(100, before + amount if relative else amount))
    else:
        attribute = room_rules._normalize_attribute_key(target_name, room_rules.resolve_rule_id(info))
        if not attribute:
            return False, "属性名必须是 STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC 或对应的中文名"
        attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
        current_key = next(
            (key for key in attributes if str(key).strip().casefold() == attribute.casefold()),
            attribute,
        )
        before = max(0, min(999, room_rules._as_int(attributes.get(current_key), 0)))
        after = max(0, min(999, before + amount if relative else amount))
        target_name = attribute
    preview = f"将把 {name} 的{label}「{target_name}」从 {before} 调整为 {after}（{after - before:+d}）。"
    reason = str(options.get("reason") or "").strip()

    def _apply() -> tuple[bool, str]:
        if kind == "skill":
            result = room_rules.apply_skill_change(
                room_dir,
                info,
                member,
                name=target_name,
                value=None if relative else amount,
                delta=amount if relative else None,
                reason=reason,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                source="rule_command",
            )
        else:
            result = room_rules.apply_attribute_change(
                room_dir,
                info,
                member,
                key=target_name,
                value=None if relative else amount,
                delta=amount if relative else None,
                reason=reason,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                source="rule_command",
            )
        message = f"{name} 的{label}「{target_name}」：{result['before']} → {result['after']}（{result['delta']:+d}）"
        if "--persist" in flags:
            message += _persist_member(member, room_dir=room_dir, info=info)
        return True, message

    if _assume_yes(flags):
        return _apply()
    if confirm is None:
        return True, preview + "\n（预演模式：确认无误后加 yes 执行）"
    if confirm(f"确认调整 {name} 的{label}「{target_name}」?"):
        return _apply()
    return True, preview + "\n（已取消，未执行）"


def _persist_member(
    member: dict[str, Any],
    *,
    room_dir: Path,
    info: dict[str, Any],
    item_record_id: str | None = None,
) -> str:
    from trpg_server.routes.characters import character_service_actor

    try:
        room_rules.persist_member_card(
            member,
            actor=character_service_actor(),
            item_record_id=item_record_id,
        )
    except room_rules.RuleOperationError as exc:
        return f"\n写回持久角色卡失败：{exc.message}（房间快照已更新）"
    # 已写回的变更记录需要落盘，避免归档时重复写入。
    room_rules.save_room_info(room_dir, info)
    return "\n已写回持久角色卡"


def _san_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    options: dict[str, str],
    confirm: Callable[[str], bool] | None,
    *,
    room_id: Any,
    actor: Any,
    actor_id: Any,
    actor_rank: Any,
) -> tuple[bool, str]:
    if len(rest) < 3 or rest[0].lower() not in {"add", "set"}:
        return False, "用法：rule san add <玩家> <±N> | rule san set <玩家> <N>（默认预演，yes 落盘）"
    mode = rest[0].lower()
    try:
        member = room_rules.resolve_member(info, rest[1], actor_id=actor_id, actor_rank=actor_rank)
        room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return False, exc.message
    amount = _signed(rest[2])
    if amount is None:
        return False, "SAN 变更值必须是整数，例如 +3 / -1d6 请改用 /sc"
    snapshot = room_rules.san_snapshot(member)
    target = amount if mode == "set" else snapshot["current"] + amount
    after = max(0, min(snapshot["max"], target))
    name = room_rules.member_display_name(member)
    preview = f"将把 {name} 的 SAN 从 {snapshot['current']} 调整为 {after}（{after - snapshot['current']:+d}）。"
    reason = str(options.get("reason") or "").strip()

    def _apply() -> tuple[bool, str]:
        result = room_rules.apply_san_change(
            room_dir,
            info,
            member,
            value=amount if mode == "set" else None,
            delta=amount if mode == "add" else None,
            reason=reason,
            actor=actor or ROOM_RULE_ACTOR,
            actor_id=actor_id,
            source="rule_command",
        )
        return True, f"{name} 的 SAN：{result['san_before']} → {result['san_after']}（{result['delta']:+d}）"

    if _assume_yes(flags):
        return _apply()
    if confirm is None:
        return True, preview + "\n（预演模式：确认无误后加 yes 执行）"
    if confirm(f"确认调整 {name} 的 SAN?"):
        return _apply()
    return True, preview + "\n（已取消，未执行）"


def _limits_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    options: dict[str, str],
    *,
    config_dir: Any,
) -> tuple[bool, str]:
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    policy = world_override.resolve_policy(config_dir, info.get("house_rules"))
    if not rest:
        site = _settings_policy(config_dir)
        return True, "\n".join(
            [
                f"站点默认：冷却 {site['cooldown_seconds']} 秒 / 每小时 {site['hourly_limit'] or '不限'} 次",
                f"本房间生效：冷却 {policy['cooldown_seconds']} 秒 / 每小时 {policy['hourly_limit'] or '不限'} 次",
                "房间只能比站点更严格（更长的冷却 / 更少的每小时次数）。",
                "用法：rule limits cooldown <秒|inherit>；rule limits hourly <次数|inherit>",
            ]
        )
    if len(rest) < 2 or rest[0].lower() not in {"cooldown", "hourly"}:
        return False, "用法：rule limits cooldown <秒|inherit>；rule limits hourly <次数|inherit>"
    key = "world_override_cooldown_seconds" if rest[0].lower() == "cooldown" else "world_override_hourly_limit"
    raw = str(rest[1]).strip().lower()
    if raw == "inherit":
        rules.pop(key, None)
        value_text = "inherit"
    else:
        value = _as_int(raw, -1)
        if value < 0:
            return False, "数值必须是非负整数，或 inherit"
        rules[key] = value
        value_text = str(value)
    info["house_rules"] = rules
    effective = world_override.resolve_policy(config_dir, rules)
    if raw != "inherit":
        # 只能收紧：把请求值夹到站点允许的范围后再保存，避免「存了 5 却生效 20」。
        field = "cooldown_seconds" if key.endswith("cooldown_seconds") else "hourly_limit"
        rules[key] = effective[field]
        info["house_rules"] = rules
    room_rules.save_room_info(room_dir, info)
    from trpg_server.room_log import log_room_event

    log_room_event(
        room_dir,
        f"更新世界意志限频：{rest[0].lower()}={value_text}",
        source="rule",
        room_id=info.get("id"),
        actor="房间控制台",
    )
    warning = ""
    if raw != "inherit" and str(effective["cooldown_seconds" if rest[0].lower() == "cooldown" else "hourly_limit"]) != raw:
        warning = "（站点限制更严格，已按站点值生效）"
    return True, (
        f"世界意志限频已更新：冷却 {effective['cooldown_seconds']} 秒 / "
        f"每小时 {effective['hourly_limit'] or '不限'} 次{warning}"
    )
