"""房间控制台 ``rule`` 域：不依赖 AI KP 的检定 / SAN / 物品 / 限频操作。

命令只作用于当前房间；房主 / 房间管理员校验由路由与 ``_require_operator``
双重把关。默认严格对齐项目约定：
- ``rule check`` 只读结算，不改变数值；
- ``rule item`` 与聊天 ``/item`` 使用同一实现；
- ``rule san add|set`` 默认预演，加 ``yes`` 才落盘。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from trpg_server import room_chase, room_clock, room_magic, room_rules, world_override
from trpg_server.console_args import ConsoleArgSpec, parse_console_args
from trpg_server.ruleset.capabilities import attribute_cap

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
    "rest": "--rest",
    "accelerate": "--accelerate",
    "vehicle": "--vehicle",
    "quarry": "--quarry",
    "firearm": "--firearm",
    "maneuver": "--maneuver",
    "first": "--first",
    "pushed": "--pushed",
    "skim": "--skim",
    "study": "--study",
    "catalog": "--catalog",
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
    "skills",
    "era",
    "assets",
    "qty",
    "kind",
    "damage",
    "mov",
    "dex",
    "build",
    "durability",
    "result",
    "loss",
    "outcome",
    "mp",
    "san",
    "pow",
    "weeks",
    "multiplier",
    "rating",
    "cmi",
    "cmf",
    "spells",
    "group",
    "language",
    "max-san",
    "max-mp",
    "limit",
}
# 尾部选项：reason / note 之后的全部内容都算它的值。
RULE_TRAILING_OPTIONS = {"reason", "note"}

RULE_ARG_SPEC = ConsoleArgSpec.of(
    flags=RULE_FLAG_WORDS,
    values=RULE_VALUE_OPTIONS,
    trailing=RULE_TRAILING_OPTIONS,
)

ROOM_RULE_HELP = "\n".join(
    [
        "  rule                        （查看本房间规则集、生效限频与成员 SAN 概览）",
        "  rule status                 （同上，只读）",
        "  rule check <玩家> <技能/属性> [difficulty normal|hard|extreme] [adjustment ±N] [bonus N] [penalty N]（结算一次检定；--skills=A,B --mode=all 走组合检定，--group=luck 走团体幸运）",
        "  rule social <玩家> <技能> [attitude supportive|neutral|opposed|hostile] [leverage] [target-skill N]（房规：社交难度）",
        "  rule combined <玩家> <技能1> <技能2> [...] [mode all|any] [difficulty normal|hard|extreme] [bonus N] [penalty N]（房规：组合检定只掷一次）",
        "  rule groupluck              （房规：由在场最低幸运者代掷团体幸运）",
        "  rule contact <玩家> <技能> [hometown] [remote] [profession] [push] [difficulty normal|hard|extreme]（房规：建立熟人）",
        "  rule economy <玩家> [金额] [era 1920s|modern] [assets N] [apply] [reason 原因]（房规：信用评级与开支；默认只读预览，apply 登记现金写回）",
        "  rule san add <玩家> <±N> [reason 原因] [yes]（调整 SAN；默认预演）",
        "  rule san set <玩家> <N> [reason 原因] [yes]（设为指定 SAN；默认预演）",
        "  rule san bout [玩家]              （房规：查看发作类型、症状与时长：即时 1D10 轮 / 总结 1D10 小时）",
        "  rule san bout end <玩家>          （房规：提前结束当前发作）",
        "  rule san habituation [玩家]        （房规：查看按来源累计的习惯恐惧）",
        "  rule san habituation add <玩家> <来源> <本次损失> [该来源单次最大损失]（记录累计并判断是否免疫）",
        "  rule san habituation tick [玩家]   （房规：幕间习惯恐惧累计 −1；省略玩家则全房间）",
        "  rule san reality <玩家> [成功|失败|大成功|大失败] [来源说明]（房规：用当前 SAN 做现实认知检定，成功获得对幻觉的抗性）",
        "  rule san trait [phobia|mania] [关键词|roll]（房规：按需查询恐惧症 / 躁狂症表）",
        "  rule san treat <玩家> [成功|失败|大失败] [月数] [备注]（心理治疗：成功 +1D3 SAN，大失败 −1D6）",
        "  rule growth [玩家] [技能]（查看成长标记；给玩家+技能则立即结算该技能的一次成长检定）",
        "  rule train <玩家> <技能> [weeks N]（房规：幕间训练，每 4 周一次成长检定）",
        "  rule combat knockout <目标>（同 combat knockout）",
        "  rule combat hit-location [1-20] [major]（同 combat hit-location）",
        "  rule combat poison <目标> <XdY+N> [potency N] [onset N]（同 combat poison）",
        "  rule item <玩家> <物品名> [qty N] [weapon] [persist] [reason 原因]（追加物品/武器；persist 才写回持久卡）",
        "  rule skill <玩家> <技能名> <值|±N> [persist] [reason 原因] [yes]（修改技能；默认预演）",
        "  rule attr <玩家> <属性> <值|±N> [persist] [reason 原因] [yes]（修改 STR/DEX/INT 等基础属性；默认预演）",
        "  rule heal natural <玩家> [天数] [rest]（房规：自然恢复；未给天数时用游戏时钟差，重伤每周 1 点）",
        "  rule heal mp <玩家> [天数]（房规：按天恢复 MP；未给天数时用游戏时钟差）",
        "  rule chase [status]         （房规：查看追逐位置、行动点与障碍）",
        "  rule chase start <追逐者> <逃离者> [更多...]（开始追逐；初始距离 2）",
        "  rule chase add <名称> [mov N] [dex N] [build N] [vehicle] [quarry]（加入 NPC/载具）",
        "  rule chase round           （进入下一追逐轮，重置行动点）",
        "  rule chase move <名称> [N] [accelerate]（移动地点；载具可加速 2-5 个）",
        "  rule chase hazard <名称> <技能> [difficulty ...] [ap N] [damage N]（险境；成员自动掷骰）",
        "  rule chase obstacle <起始位置> <名称> [skill 技能] [difficulty ...] [durability N]（放置障碍）",
        "  rule chase overcome <名称> <起始位置> <技能> [difficulty ...]（翻越障碍；成员自动掷骰）",
        "  rule chase break <起始位置> <damage N>（破坏障碍；返回撞毁反冲）",
        "  rule chase conflict <攻击者> <目标> [firearm] [maneuver] [loss N]（同地点才能近战）",
        "  rule chase end [outcome]   （结束追逐）",
        "  非成员参与者的技能检定用 result regular|hard|extreme|critical|failure 显式传入。",
        "  rule spell <玩家>                   （房规：查看已学法术、典籍与 MP）",
        "  rule spell learn <玩家> <法术> [weeks N]（登记已学法术；耗时由 KP 显式给出）",
        "  rule spell cast <玩家> <法术> [mp N] [san N] [pow N] [first] [pushed] [multiplier N] [result ...] [catalog]",
        "  rule spell grimoire <玩家> <书名> [skim|study] [weeks N] [san N] [cmi N] [cmf N] [rating N] [result ...] [spells a,b] [language 语言] [catalog]",
        "  rule spell find <关键词> [kind spell|grimoire] [group 大类] [language 语言] [max-san N] [max-mp N] [limit N]（房规：按需查询规则书法术 / 典籍）",
        "  rule spell show <名称>             （房规：查看单条法术或典籍的完整条目）",
        "  catalog 由内容表补齐空缺消耗；显式参数优先，骰值类仍由 KP 掷骰。",
        "  首次施法为困难 POW 检定；孤注一掷失败时法术仍生效并额外支付 1D6 倍消耗。",
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
    "heal",
    "chase",
    "spell",
    "growth",
    "train",
    "combat",
    "limits",
}


def _parse(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    """纯词参数解析（共享实现）。

    ``rule item 玩家 手电筒 qty 2 weapon persist`` 就是全部参数写法；
    ``-5`` 这类带符号数值始终按位置参数处理。
    """
    parsed = parse_console_args(args, RULE_ARG_SPEC)
    return parsed.positionals, parsed.flags, parsed.options


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
    room_dir = Path(room_dir)
    info = room_rules.load_room_info(room_dir)

    # `mp` 同时是法术命令的取值选项，不能交给通用解析器；这里先截获
    # `rule heal mp <玩家> [天数]`，避免玩家名被当成 mp 的值吞掉。
    raw_args = [str(item) for item in args]
    if len(raw_args) >= 2 and raw_args[0].lower() == "heal" and raw_args[1].lower() == "mp":
        from trpg_server.ruleset.coc7.options import resolve_options

        if not _require_operator(actor_rank):
            return False, "只有房主 / 房间管理员可以执行该规则命令"
        return _mp_heal_command(
            room_dir,
            info,
            ["mp", *raw_args[2:]],
            coc7_options=resolve_options(info),
            actor_id=actor_id,
            actor_rank=actor_rank,
        )

    positionals, flags, options = _parse(args)
    sub = positionals[0].lower() if positionals else "status"
    rest = positionals[1:]
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
        "heal": coc7_options.healing_natural or coc7_options.game_clock,
        "chase": coc7_options.chase_enabled,
        "spell": coc7_options.magic_enabled or coc7_options.magic_catalog_enabled,
        "train": coc7_options.growth_training,
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
        # 计划 §5 的写法：rule economy spend <玩家> <金额>（等价 rule economy <玩家> <金额> apply）。
        if rest and rest[0].lower() in {"spend", "expense"}:
            rest = rest[1:]
            flags = set(flags) | {"--apply"}
            if not rest:
                return False, "用法：rule economy spend <玩家> <金额> [era 1920s|modern] [reason 原因]"
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
        # 计划 §5 的命令写法：rule check --skills=A,B --mode=all / --group=luck。
        # 等价于 rule combined / rule groupluck，保留旧子命令。
        if options.get("skills"):
            raw = str(options.get("skills") or "")
            names = [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
            if not names:
                return False, "用法：rule check <玩家> --skills=侦查,聆听 [--mode all|any]"
            if not coc7_options.combined_checks:
                return False, "组合检定房规未开启（coc7_combined_checks）"
            try:
                member = room_rules.resolve_member(
                    info,
                    rest[0] if rest else actor_id,
                    actor_id=actor_id,
                    actor_rank=actor_rank,
                )
                result = room_rules.roll_combined_check(
                    room_dir,
                    info,
                    member,
                    names,
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
            return True, str(result.get("summary") or "组合检定完成")

        if str(options.get("group") or "").lower() in {"luck", "groupluck", "group-luck"}:
            if not coc7_options.group_luck:
                return False, "团体幸运房规未开启（coc7_group_luck）"
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
            return True, str(result.get("summary") or "团体幸运检定完成")

        if len(rest) < 2:
            return False, "用法：rule check <玩家> <技能/属性> [difficulty normal|hard|extreme]"
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

    if sub == "heal":
        return _heal_command(
            room_dir,
            info,
            rest,
            flags,
            coc7_options=coc7_options,
            actor_id=actor_id,
            actor_rank=actor_rank,
        )

    if sub == "chase":
        return _chase_command(
            room_dir,
            info,
            rest,
            flags,
            options,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
            config_dir=config_dir,
            room_id=room_id,
        )

    if sub == "spell":
        return _spell_command(
            room_dir,
            info,
            rest,
            flags,
            options,
            actor=actor,
            actor_id=actor_id,
            actor_rank=actor_rank,
            config_dir=config_dir,
            room_id=room_id,
        )

    if sub == "growth":
        from trpg_server import room_interlude

        try:
            if len(rest) >= 2:
                member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
                result = room_interlude.experience_check(
                    room_dir,
                    info,
                    member,
                    skill=" ".join(rest[1:]),
                    actor=actor or ROOM_RULE_ACTOR,
                    actor_id=actor_id,
                )
                return True, str(result.get("summary") or "成长检定完成")
            targets = [room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)] if rest else [
                item for item in (info.get("members") or []) if isinstance(item, dict) and item.get("character_card")
            ]
            lines: list[str] = []
            for member in targets:
                card = room_rules.member_card(member)
                growth = room_rules._growth_capability(card)
                if growth is None:
                    continue
                state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
                marks = growth.read_marks(card, state.get("growth_marks"))
                if not marks:
                    continue
                names = [str(row.get("skill") or row.get("name") or "") for row in marks]
                lines.append(f"{room_rules.member_display_name(member)}：{'、'.join(name for name in names if name)}")
            return True, ("成长标记：\n" + "\n".join(lines)) if lines else "当前没有成长标记。"
        except (room_rules.RuleOperationError, room_interlude.InterludeError) as exc:
            return False, getattr(exc, "message", str(exc))

    if sub == "train":
        from trpg_server import room_interlude

        if len(rest) < 2:
            return False, "用法：rule train <玩家> <技能> [weeks N]"
        weeks = options.get("weeks")
        if weeks is None:
            for index, item in enumerate(rest[2:], start=2):
                if str(item).isdigit():
                    weeks = item
                    rest = [*rest[:index], *rest[index + 1:]]
                    break
        try:
            member = room_rules.resolve_member(info, rest[0], actor_id=actor_id, actor_rank=actor_rank)
            result = room_interlude.train_skill(
                room_dir,
                info,
                member,
                skill=" ".join(rest[1:]),
                weeks=weeks,
                actor=actor or ROOM_RULE_ACTOR,
                actor_id=actor_id,
                reason=str(options.get("reason") or ""),
            )
            record = result.get("record") or {}
            return True, (
                f"{room_rules.member_display_name(member)} 训练「{record.get('skill')}」"
                f"{record.get('weeks')} 周：{record.get('before')}→{record.get('after')}"
            )
        except (room_rules.RuleOperationError, room_interlude.InterludeError) as exc:
            return False, getattr(exc, "message", str(exc))

    if sub == "combat":
        from trpg_server.room_combat_commands import handle_room_combat_command

        if not rest:
            return False, "用法：rule combat <knockout|hit-location|poison> ...（同 combat 子命令）"
        return handle_room_combat_command(
            rest,
            room_id=room_id,
            room_dir=room_dir,
            config_dir=config_dir,
            actor=actor or ROOM_RULE_ACTOR,
            actor_id=actor_id,
            actor_rank=actor_rank,
            confirm=confirm,
        )

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


def _heal_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    *,
    coc7_options: Any,
    actor_id: Any,
    actor_rank: Any,
) -> tuple[bool, str]:
    """房规：自然恢复 / 按天恢复 MP。

    天数显式给出时优先使用；未给出且房间启用游戏时钟时，用「当前游戏时间 −
    上次结算时间」的整天数代替。没有起点时保守返回 0，不凭空补时间。
    """
    mode = str(rest[0]).strip().lower() if rest else ""
    if mode == "mp":
        return _mp_heal_command(
            room_dir,
            info,
            rest,
            coc7_options=coc7_options,
            actor_id=actor_id,
            actor_rank=actor_rank,
        )
    usage = "用法：rule heal natural <玩家> [天数] [rest]（未启用游戏时钟时必须给天数）"
    if len(rest) < 2 or mode not in {"natural", "自然"}:
        return False, usage
    resting = "--rest" in flags
    if coc7_options.healing_require_rest and not resting:
        return False, "本房规要求声明休养：请在命令末尾加 rest"

    try:
        member = room_rules.resolve_member(info, rest[1], actor_id=actor_id, actor_rank=actor_rank)
        room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return False, exc.message
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    days_text = rest[2] if len(rest) > 2 else ""
    explicit = bool(str(days_text).strip())
    clock = room_clock.read_clock(info)
    if explicit:
        try:
            days = int(days_text)
        except (TypeError, ValueError):
            return False, "天数必须是正整数"
        if days <= 0:
            return False, "天数必须是正整数"
    else:
        if not coc7_options.game_clock:
            return False, usage
        marker = state.get("last_natural_heal") or (clock or {}).get("started_at")
        delta = room_clock.clock_days_since(info, marker)
        if delta is None:
            return False, "游戏时钟还没有可用的起点；请显式给出天数。"
        days = delta
    name = room_rules.member_display_name(member)
    if days <= 0:
        return True, f"{name} 距上次休养不足 1 天，未结算；可显式给出天数。"

    source = f"{days} 天" if explicit else f"游戏时钟 {days} 天"
    clock = room_clock.read_clock(info)
    if clock is not None:
        state["last_natural_heal"] = clock["current"]
    try:
        result = room_rules.apply_natural_healing(
            room_dir,
            info,
            member,
            days=days,
            reason=f"自然恢复 {source}" + ("（休养）" if resting else ""),
        )
    except room_rules.RuleOperationError as exc:
        return False, exc.message
    room_rules.save_room_info(room_dir, info)
    if result["after"] == result["before"]:
        return True, (
            f"{name} 休养 {source}，未获得恢复"
            f"（耐久 {result['before']}/{result['max_hp']}）。"
        )
    return (
        True,
        f"{name} 自然恢复（{source}，{'含休养' if resting else '未声明休养'}）："
        f"{result['before']} → {result['after']}/{result['max_hp']}（+{result['amount']}）。",
    )


def _mp_heal_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    *,
    coc7_options: Any,
    actor_id: Any,
    actor_rank: Any,
) -> tuple[bool, str]:
    """房规：按天恢复 MP；未给天数时读游戏时钟差值。"""
    usage = "用法：rule heal mp <玩家> [天数]（未启用游戏时钟时必须给天数）"
    if not coc7_options.game_clock:
        return False, "按天恢复 MP 需要先开启游戏时钟（房规 coc7_game_clock）"
    if len(rest) < 2:
        return False, usage
    try:
        member = room_rules.resolve_member(info, rest[1], actor_id=actor_id, actor_rank=actor_rank)
        room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return False, exc.message
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    days_text = rest[2] if len(rest) > 2 else ""
    explicit = bool(str(days_text).strip())
    clock = room_clock.read_clock(info)
    if explicit:
        try:
            days = int(days_text)
        except (TypeError, ValueError):
            return False, "天数必须是正整数"
        if days <= 0:
            return False, "天数必须是正整数"
    else:
        marker = state.get("last_mp_recovery") or (clock or {}).get("started_at")
        delta = room_clock.clock_days_since(info, marker)
        if delta is None:
            return False, "游戏时钟还没有可用的起点；请显式给出天数。"
        days = delta
    name = room_rules.member_display_name(member)
    if clock is not None:
        state["last_mp_recovery"] = clock["current"]
    if days <= 0:
        room_rules.save_room_info(room_dir, info)
        return True, f"{name} 距上次 MP 恢复不足 1 天，未结算；可显式给出天数。"
    result = room_magic.recover_mp(room_dir, info, member, days=days)
    source = f"{days} 天" if explicit else f"游戏时钟 {days} 天"
    return (
        True,
        f"{name} 按天恢复 MP（{source}）："
        f"{result['before']} → {result['after']}/{result['max_mp']}（+{result['gain']}）。",
    )


def _format_spell_detail(entry: dict[str, Any]) -> str:
    from trpg_server.ruleset.coc7 import magic_content

    head = f"{entry.get('name')}（p{entry.get('page')}）"
    if entry.get("group"):
        head += f" · 所属 {entry['group']}"
    lines = [head]
    if entry.get("cost"):
        lines.append(f"  消耗：{entry['cost']}")
    if entry.get("cost_note"):
        lines.append(f"  说明：{entry['cost_note']}")
    if entry.get("casting_time"):
        lines.append(f"  施法用时：{entry['casting_time']}")
    check = entry.get("check_detail") or {}
    if check:
        bits = []
        if check.get("kinds"):
            bits.append("/".join(check["kinds"]))
        if check.get("skills"):
            bits.append("/".join(check["skills"]))
        lines.append("  检定：" + (" · ".join(bits) or "需要检定"))
    elif entry.get("check"):
        lines.append(f"  检定：{entry['check']}")
    if entry.get("aliases"):
        lines.append("  别名：" + "、".join(entry["aliases"]))
    if entry.get("summary"):
        lines.append(f"  摘要：{entry['summary']}")
    if entry.get("text"):
        lines.append(f"  原文：{entry['text']}")
        if entry.get("text_truncated"):
            lines.append("  （原文过长，已截断；需要更多请查规则书）")
    lines.append(f"  来源：{magic_content.SOURCE_SPELL}")
    return "\n".join(lines)


def _format_grimoire_detail(book: dict[str, Any]) -> str:
    from trpg_server.ruleset.coc7 import magic_content

    lines = [f"《{book.get('title')}》（p{book.get('page')}）"]
    for edition in book.get("editions") or []:
        bits = [str(edition.get("language") or "?")]
        if edition.get("author"):
            bits.append(str(edition["author"]))
        if edition.get("year"):
            bits.append(str(edition["year"]))
        lines.append("  " + " / ".join(bits))
        stats = []
        if edition.get("san_loss"):
            stats.append(f"SAN {edition['san_loss']}")
        if edition.get("cmi") is not None:
            stats.append(f"CMI +{edition['cmi']}")
        if edition.get("cmf") is not None:
            stats.append(f"CMF +{edition['cmf']}")
        if edition.get("rating") is not None:
            stats.append(f"MR {edition['rating']}")
        if edition.get("weeks"):
            stats.append(f"{edition['weeks']} 周")
        if stats:
            lines.append("   " + " · ".join(stats))
        if edition.get("spells"):
            lines.append("   法术：" + "、".join(edition["spells"]))
        if edition.get("occult"):
            lines.append(f"   神秘学：{edition['occult']}")
    if book.get("summary"):
        lines.append(f"  摘要：{book['summary']}")
    if book.get("text"):
        lines.append(f"  原文：{book['text']}")
        if book.get("text_truncated"):
            lines.append("  （原文过长，已截断；需要更多请查规则书）")
    lines.append(f"  来源：{magic_content.SOURCE_GRIMOIRE}")
    return "\n".join(lines)


def _spell_lookup(action: str, args: list[str], options: dict[str, str]) -> tuple[bool, str]:
    """按需查询规则书法术 / 典籍：返回精简列表或单条完整条目。"""
    from trpg_server.ruleset.coc7 import magic_content

    query = " ".join(args).strip()
    if action == "show":
        if not query:
            return False, "用法：rule spell show <法术或典籍名称>"
        spell = magic_content.describe_spell(query)
        if spell:
            return True, _format_spell_detail(spell)
        book = magic_content.describe_grimoire(query)
        if book:
            return True, _format_grimoire_detail(book)
        return False, f"内容表里没有「{query}」。用 rule spell find 关键词 先搜一下。"

    kind = str(options.get("kind") or "any").strip().lower()
    limit = _as_int(options.get("limit"), 6)
    group = options.get("group")
    language = options.get("language")
    max_san = options.get("max-san")
    max_mp = options.get("max-mp")
    lines: list[str] = []
    if kind in {"spell", "法术"}:
        results = magic_content.find_spells(
            query, group=group, max_san=max_san, max_mp=max_mp, limit=limit
        )
        lines.extend(magic_content.format_spell_line(item) for item in results)
    elif kind in {"grimoire", "book", "典籍"}:
        results = magic_content.find_grimoires(
            query, language=language, max_san=max_san, limit=limit
        )
        lines.extend(magic_content.format_grimoire_line(item) for item in results)
    else:
        spells_found = magic_content.find_spells(
            query, group=group, max_san=max_san, max_mp=max_mp, limit=limit
        )
        books_found = magic_content.find_grimoires(
            query, language=language, max_san=max_san, limit=limit
        )
        if spells_found:
            lines.append("【法术】")
            lines.extend("  " + magic_content.format_spell_line(item) for item in spells_found)
        if books_found:
            lines.append("【典籍】")
            lines.extend(
                "  " + magic_content.format_grimoire_line(item) for item in books_found
            )
    if not lines:
        return False, f"内容表里没有匹配「{query}」的条目。"
    lines.append("用 rule spell show <名称> 查看单条完整条目。")
    return True, "\n".join(lines)


def _spell_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    options: dict[str, str],
    *,
    actor: Any,
    actor_id: Any,
    actor_rank: Any,
    config_dir: Any,
    room_id: Any,
) -> tuple[bool, str]:
    """房规：法术与典籍（施法消耗 / 首次施法 / 典籍阅读），规则数学在插件内。"""
    from trpg_server.ruleset.coc7 import magic_content
    from trpg_server.ruleset.coc7.options import resolve_options

    coc7_options = resolve_options(info)
    settlement = coc7_options.magic_enabled
    catalog = coc7_options.magic_catalog_enabled
    verbs = {"status", "list", "learn", "cast", "grimoire", "find", "show"}
    action = "status"
    args = list(rest)
    if args and str(args[0]).strip().lower() in verbs:
        action = str(args[0]).strip().lower()
        args = args[1:]

    # 查表与结算分开受开关控制：只开内容表时仍可 find / show，但不能施法。
    if action in {"find", "show"}:
        if not catalog:
            return False, f"未知的规则命令：rule spell {action}\n{ROOM_RULE_HELP}"
        return _spell_lookup(action, args, options)
    if not settlement:
        return False, f"未知的规则命令：rule spell {action}\n{ROOM_RULE_HELP}"

    if action in {"status", "list"} and not args:
        return False, "用法：rule spell <玩家>"

    try:
        if action in {"status", "list"}:
            member = room_rules.find_member(info, args[0])
            if not member:
                raise room_rules.RuleOperationError(f"找不到玩家 {args[0]}", code="unknown_member")
            return True, room_magic.format_magic(member)

        if action == "learn":
            if len(args) < 2:
                return False, "用法：rule spell learn <玩家> <法术> [weeks N]"
            member = room_rules.find_member(info, args[0])
            if not member:
                raise room_rules.RuleOperationError(f"找不到玩家 {args[0]}", code="unknown_member")
            payload = room_magic.learn_spell(
                room_dir,
                info,
                member,
                " ".join(args[1:]),
                weeks=options.get("weeks") or 0,
                actor=actor,
            )
            return True, (
                f"{args[0]} 学会「{payload['spell']}」（耗时 {payload['weeks']} 周）。\n"
                + room_magic.format_magic(member)
            )

        if action == "cast":
            if len(args) < 2:
                return False, (
                    "用法：rule spell cast <玩家> <法术> [mp N] [san N] [pow N] "
                    "[first] [pushed] [multiplier N] [result ...]"
                )
            member = room_rules.find_member(info, args[0])
            if not member:
                raise room_rules.RuleOperationError(f"找不到玩家 {args[0]}", code="unknown_member")
            spell_name = " ".join(args[1:])
            costs = {
                "mp": _as_int(options.get("mp"), 0),
                "san": _as_int(options.get("san"), 0),
                "pow": _as_int(options.get("pow"), 0),
            }
            note = ""
            if "--catalog" in flags and catalog:
                stats = magic_content.spell_costs(spell_name)
                if stats is None:
                    note = "内容表未收录该法术，按显式参数结算。\n"
                else:
                    pending: list[str] = []
                    for unit in ("mp", "san", "pow"):
                        if costs[unit]:
                            continue
                        value = stats.get(unit)
                        if isinstance(value, int):
                            costs[unit] = value
                        elif (stats.get("units") or {}).get(unit):
                            pending.append(str(stats["units"][unit]))
                    note = f"内容表消耗：{stats.get('raw') or '无'}。"
                    if pending:
                        note += f" 骰值由 KP 掷定：{'、'.join(pending)}。"
                    note += "\n"
            level = str(options.get("result") or "").strip().lower()
            if not level:
                try:
                    check = room_rules.roll_check(
                        room_dir,
                        info,
                        member,
                        name="POW",
                        difficulty="hard",
                        reason="首次施法",
                        actor=actor or ROOM_RULE_ACTOR,
                        actor_id=actor_id,
                        config_dir=config_dir,
                        room_id=room_id,
                    )
                    level = str(check.get("success_level") or "failure")
                except room_rules.RuleOperationError:
                    level = ""
            payload = room_magic.cast_spell(
                room_dir,
                info,
                member,
                name=spell_name,
                mp_cost=costs["mp"],
                san_cost=costs["san"],
                pow_cost=costs["pow"],
                first_cast="--first" in flags,
                success_level=level or None,
                pushed="--pushed" in flags,
                multiplier=options.get("multiplier") or 1,
                actor=actor,
                actor_id=actor_id,
            )
            text = (
                note
                + f"{args[0]} 施放「{payload['spell']}」："
                + ("生效" if payload["effects"] else "没有效果")
                + f"；消耗 MP {payload['mp_spent']}"
                + (f" + 耐久 {payload['hp_spent']}" if payload["hp_spent"] else "")
            )
            if payload["pushed"]:
                text += f"；孤注一掷反噬额外消耗 {payload['extra_cost']}，{payload['side_effect']}"
            return True, text + "\n" + room_magic.format_magic(member)

        if action == "grimoire":
            if len(args) < 2:
                return False, (
                    "用法：rule spell grimoire <玩家> <书名> [skim|study] [weeks N] [san N] "
                    "[cmi N] [cmf N] [rating N] [result ...] [spells a,b] [language 语言] [catalog]"
                )
            member = room_rules.find_member(info, args[0])
            if not member:
                raise room_rules.RuleOperationError(f"找不到玩家 {args[0]}", code="unknown_member")
            mode = "study"
            title = " ".join(args[1:])
            if "--skim" in flags:
                mode = "skim"
            elif "--study" in flags:
                mode = "study"
            elif len(args) > 2 and str(args[2]).lower() in {"skim", "study"}:
                mode = str(args[2]).lower()
                title = args[1]
            spells = [
                item.strip()
                for item in str(options.get("spells") or "").replace("，", ",").split(",")
                if item.strip()
            ]
            cmi = _as_int(options.get("cmi"), 0)
            cmf = _as_int(options.get("cmf"), 0)
            rating = _as_int(options.get("rating"), 0)
            note = ""
            if "--catalog" in flags and catalog:
                stats = magic_content.grimoire_stats(title, options.get("language"))
                if stats is None:
                    note = "内容表未收录该典籍，按显式参数结算。\n"
                else:
                    if not cmi and stats.get("cmi") is not None:
                        cmi = stats["cmi"]
                    if not cmf and stats.get("cmf") is not None:
                        cmf = stats["cmf"]
                    if not rating and stats.get("rating") is not None:
                        rating = stats["rating"]
                    if not spells and stats.get("spells"):
                        spells = list(stats["spells"])
                    note = (
                        f"内容表版本：{stats.get('language') or '?'}；"
                        f"CMI {stats.get('cmi')} / CMF {stats.get('cmf')} / MR {stats.get('rating')}；"
                        f"SAN {stats.get('san_loss') or '-'}；{stats.get('weeks') or '-'} 周。\n"
                    )
            payload = room_magic.read_grimoire(
                room_dir,
                info,
                member,
                title=title,
                mode=mode,
                weeks=(
                    _as_int(options.get("weeks"), 0)
                    if options.get("weeks") not in (None, "")
                    else None
                ),
                san_loss=_as_int(options.get("san"), 0),
                cmi=cmi,
                cmf=cmf,
                rating=rating,
                success_level=options.get("result"),
                spells=spells,
                actor=actor,
                actor_id=actor_id,
            )
            if not payload["success"]:
                return True, note + f"泛读《{payload['book']}》失败，未获得任何收益。"
            return True, (
                note
                + f"《{payload['book']}》{'泛读' if payload['mode'] == 'skim' else '精读'}完成："
                f"克苏鲁神话 +{payload['mythos_gain']}，SAN -{payload['san_loss']}"
                + (f"，累计精读 {payload['reads']} 次" if payload["reads"] else "")
                + "\n"
                + room_magic.format_magic(member)
            )
    except room_rules.RuleOperationError as exc:
        return False, exc.message

    return False, "用法：rule spell <玩家> | learn | cast | grimoire | find | show"


def _chase_check_level(
    room_dir: Path,
    info: dict[str, Any],
    name: str,
    *,
    skill: str,
    difficulty: str,
    bonus: int,
    options: dict[str, str],
    actor: Any,
    actor_id: Any,
    config_dir: Any,
    room_id: Any,
) -> str:
    """成员的追逐技能检定自动掷骰；非成员必须显式给 result。"""
    explicit = str(options.get("result") or "").strip().lower()
    if explicit:
        return explicit
    member = room_rules.find_member(info, name)
    if not member:
        raise room_rules.RuleOperationError(
            "非成员参与者请用 result regular|hard|extreme|critical|failure 传入检定结果",
            code="chase_need_result",
        )
    result = room_rules.roll_check(
        room_dir,
        info,
        member,
        name=skill,
        difficulty=difficulty,
        bonus=bonus,
        reason="追逐",
        actor=actor or ROOM_RULE_ACTOR,
        actor_id=actor_id,
        config_dir=config_dir,
        room_id=room_id,
    )
    return str(result.get("success_level") or "failure")


def _chase_command(
    room_dir: Path,
    info: dict[str, Any],
    rest: list[str],
    flags: set[str],
    options: dict[str, str],
    *,
    actor: Any,
    actor_id: Any,
    actor_rank: Any,
    config_dir: Any,
    room_id: Any,
) -> tuple[bool, str]:
    """房规：追逐会话（地点 / 行动点 / 险境 / 障碍 / 冲突），规则数学在插件内。"""
    action = str(rest[0]).strip().lower() if rest else "status"
    args = rest[1:]
    difficulty = str(options.get("difficulty") or "regular").lower()

    def fail(exc: room_rules.RuleOperationError) -> tuple[bool, str]:
        return False, exc.message

    try:
        if action in {"status", "show"}:
            return True, room_chase.format_chase(room_chase.get_chase(info))

        if action == "start":
            names = [str(item).strip() for item in args if str(item).strip()]
            if len(names) < 2:
                return False, "用法：rule chase start <追逐者> <逃离者> [更多参与者...]"
            participants = []
            for index, name in enumerate(names):
                if len(names) == 2 and index == 1:
                    role = "quarry"
                elif "--quarry" in flags and index > 0:
                    role = "quarry"
                else:
                    role = "pursuer"
                member = room_rules.find_member(info, name)
                if member:
                    participants.append(room_chase.participant_from_member(member, role=role))
                else:
                    participants.append(
                        room_chase.participant_from_spec(
                            {
                                "name": name,
                                "mov": options.get("mov"),
                                "dex": options.get("dex"),
                                "build": options.get("build"),
                                "vehicle": "--vehicle" in flags,
                            },
                            role=role,
                        )
                    )
            chase = room_chase.start_chase(room_dir, info, participants, actor=actor)
            return True, room_chase.format_chase(chase)

        if action == "add":
            if not args:
                return False, "用法：rule chase add <名称> [mov N] [dex N] [build N] [vehicle] [quarry]"
            chase = room_chase.get_chase(info)
            if not chase:
                return False, "当前没有进行中的追逐"
            participant = room_chase.participant_from_spec(
                {
                    "name": args[0],
                    "mov": options.get("mov"),
                    "dex": options.get("dex"),
                    "build": options.get("build"),
                    "vehicle": "--vehicle" in flags,
                },
                role="quarry" if "--quarry" in flags else "pursuer",
            )
            if participant["role"] == "quarry":
                participant["position"] = min(
                    2, len(chase.get("locations") or []) - 1
                )
            chase.setdefault("participants", []).append(participant)
            room_chase.save_chase(room_dir, info, chase)
            return True, room_chase.format_chase(chase)

        if action in {"round", "next"}:
            return True, room_chase.format_chase(
                room_chase.start_round(room_dir, info, actor=actor)
            )

        if action == "move":
            if not args:
                return False, "用法：rule chase move <名称> [地点数] [accelerate]"
            count = _as_int(args[1], 1) if len(args) > 1 else 1
            return True, room_chase.format_chase(
                room_chase.move_participant(
                    room_dir,
                    info,
                    args[0],
                    locations=count,
                    accelerate="--accelerate" in flags,
                    actor=actor,
                )
            )

        if action == "hazard":
            if len(args) < 2:
                return False, "用法：rule chase hazard <名称> <技能> [difficulty ...] [ap N] [damage N]"
            ap_spent = _as_int(options.get("ap"), 0)
            bonus = min(2, max(0, ap_spent))
            level = _chase_check_level(
                room_dir,
                info,
                args[0],
                skill=" ".join(args[1:]),
                difficulty=difficulty,
                bonus=bonus,
                options=options,
                actor=actor,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
            payload = room_chase.attempt_hazard(
                room_dir,
                info,
                args[0],
                success_level=level,
                ap_spent=ap_spent,
                ap_loss=options.get("loss") or 1,
                damage=options.get("damage") or 0,
                note=" ".join(args[1:]),
                actor=actor,
            )
            return True, room_chase.format_chase(room_chase.get_chase(info)) + (
                f"\n险境结算：{payload['name']} {'成功' if payload['success'] else '失败'}"
                f"（奖励骰 {payload['bonus_dice']}，伤害 {payload['damage']}，"
                f"损失行动点 {payload['ap_loss']}）。"
            )

        if action == "obstacle":
            if len(args) < 2:
                return False, "用法：rule chase obstacle <起始位置> <名称> [skill 技能] [difficulty ...] [durability N]"
            obstacle = room_chase.add_obstacle(
                room_dir,
                info,
                start=args[0],
                name=args[1],
                skill=str(options.get("skill") or ""),
                difficulty=difficulty,
                durability=options.get("durability"),
                kind=options.get("kind"),
                actor=actor,
            )
            return True, (
                f"已放置障碍：{obstacle['name']}（耐久 {obstacle['durability']}，"
                f"技能 {obstacle['skill'] or '任意'} / {obstacle['difficulty']}）。"
            )

        if action == "overcome":
            if len(args) < 3:
                return False, "用法：rule chase overcome <名称> <起始位置> <技能> [difficulty ...]"
            level = _chase_check_level(
                room_dir,
                info,
                args[0],
                skill=" ".join(args[2:]),
                difficulty=difficulty,
                bonus=0,
                options=options,
                actor=actor,
                actor_id=actor_id,
                config_dir=config_dir,
                room_id=room_id,
            )
            payload = room_chase.attempt_obstacle(
                room_dir,
                info,
                args[0],
                start=args[1],
                success_level=level,
                damage=options.get("damage") or 0,
                actor=actor,
            )
            return True, (
                f"{payload['obstacle']['name']}：{'已翻越' if payload['success'] else '未能翻越'}"
                f"（位置 {payload['position']}）。\n"
                + room_chase.format_chase(room_chase.get_chase(info))
            )

        if action == "break":
            if len(args) < 2:
                return False, "用法：rule chase break <起始位置> <damage N>"
            payload = room_chase.damage_obstacle(
                room_dir,
                info,
                start=args[0],
                damage=options.get("damage") or args[1],
                actor=actor,
            )
            obstacle = payload["obstacle"]
            return True, (
                f"{obstacle['name']} 剩余耐久 {obstacle['durability']}/{obstacle['max_durability']}"
                + ("（已摧毁）" if obstacle["destroyed"] else "")
                + (f"；撞毁反冲伤害 {payload['recoil']}" if payload["recoil"] else "")
            )

        if action == "conflict":
            if len(args) < 2:
                return False, (
                    "用法：rule chase conflict <攻击者> <目标> [firearm] [maneuver] [loss N]"
                )
            payload = room_chase.resolve_conflict(
                room_dir,
                info,
                args[0],
                args[1],
                has_firearm="--firearm" in flags,
                maneuver_success="--maneuver" in flags,
                ap_loss=options.get("loss") or 1,
                note=str(options.get("note") or ""),
                actor=actor,
            )
            return True, (
                f"{payload['attacker']} → {payload['target']}："
                + ("同地点近战" if payload["same_location"] else "火器跨地点")
                + (f"，目标损失 {payload['target_ap_loss']} 行动点" if payload["target_ap_loss"] else "")
                + "。"
            )

        if action == "end":
            outcome = str(options.get("outcome") or (args[0] if args else "ended"))
            chase = room_chase.end_chase(room_dir, info, outcome=outcome, actor=actor)
            return True, room_chase.format_chase(chase)
    except room_rules.RuleOperationError as exc:
        return fail(exc)

    return False, f"未知的追逐命令：rule chase {action}\n{ROOM_RULE_HELP}"



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
        # 预览与写回共用插件给的技能上限，避免预览显示 100、落盘却是 99。
        skill_cap = room_rules._as_int(room_rules.derive_card(card).get("skillCap"), 100)
        before = max(0, min(skill_cap, room_rules._as_int(row.get("value"), 0)))
        after = max(0, min(skill_cap, before + amount if relative else amount))
    else:
        attribute = room_rules._normalize_attribute_key(target_name, room_rules.resolve_rule_id(info))
        if not attribute:
            return False, "属性名必须是 STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC 或对应的中文名"
        attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
        current_key = next(
            (key for key in attributes if str(key).strip().casefold() == attribute.casefold()),
            attribute,
        )
        before = max(0, room_rules._as_int(attributes.get(current_key), 0))
        # 属性上限由规则集插件提供；这里把上限折回入参，保证预演与实际写回一致。
        attr_cap = attribute_cap(room_rules.derive_card(card))
        if attr_cap is not None:
            if relative and amount > 0:
                amount = min(amount, max(0, attr_cap - before))
            elif not relative:
                amount = min(max(0, amount), attr_cap)
        after = max(0, before + amount if relative else amount)
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
    from trpg_server import room_sanity

    try:
        handled, text = room_sanity.handle_command(
            room_dir,
            info,
            args=[str(item) for item in rest],
            actor=str(actor or ""),
            actor_id=str(actor_id or ""),
        )
    except room_rules.RuleOperationError as exc:
        return True, exc.message
    if handled:
        return True, text
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
