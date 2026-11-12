"""CoC7 批次 1 房规的 AI 工具：社交 / 组合 / 团体幸运 / 熟人 / 经济。

五个工具都只做参数搬运：真正的判定统一走 :mod:`trpg_server.check_service`，
规则数学留在 ``ruleset/coc7`` 插件内。房规开关关闭时，这些工具会在
``routes/chat.py`` 组装 profile 时被剔除；处理器内再做一次防御性检查。
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_ai_changes, room_rules
from trpg_server.agents.tools.base import AgentTool


def _options(context: Any):
    from trpg_server.ruleset.coc7.options import resolve_options

    return resolve_options(context.room_info())


def _common(context: Any) -> dict[str, Any]:
    return {
        "actor": getattr(context, "username", None) or "AI KP",
        "actor_id": getattr(context, "user_id", None),
        "config_dir": getattr(context, "config_dir", None),
        "room_id": getattr(context, "room_id", None),
    }


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _member(info: dict[str, Any], arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return room_rules.resolve_member(
        info,
        arguments.get("player_name") or arguments.get("playerName"),
        actor_id=getattr(context, "user_id", None),
        actor_rank=3,
    )


def _check_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": True,
        "player_name": result.get("player_name"),
        "character_name": result.get("character_name"),
        "success": result.get("success"),
        "success_level": result.get("success_level"),
        "roll": result.get("roll"),
        "summary": result.get("summary"),
    }
    for key in ("social", "contact", "skills", "mode", "expense", "profile", "credit"):
        if key in result:
            payload[key] = result[key]
    return payload


def execute_social_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    if not _options(context).social_difficulty:
        return {"ok": False, "error": "coc7 social difficulty house rule is disabled"}
    target_skill = arguments.get("target_skill")
    try:
        member = _member(info, arguments, context)
        result = room_rules.roll_social_check(
            context.room_dir,
            info,
            member,
            name=str(arguments.get("name") or ""),
            target_skill=int(target_skill) if target_skill not in (None, "") else None,
            attitude=str(arguments.get("attitude") or "neutral"),
            leverage=bool(arguments.get("leverage")),
            **_common(context),
        )
    except (room_rules.RuleOperationError, TypeError, ValueError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return _check_result(result)


def execute_combined_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    if not _options(context).combined_checks:
        return {"ok": False, "error": "coc7 combined check house rule is disabled"}
    names = arguments.get("skills")
    if isinstance(names, str):
        names = [names]
    try:
        member = _member(info, arguments, context)
        result = room_rules.roll_combined_check(
            context.room_dir,
            info,
            member,
            [str(name) for name in (names or [])],
            mode=str(arguments.get("mode") or "all"),
            difficulty=str(arguments.get("difficulty") or "regular"),
            bonus=int(arguments.get("bonus") or 0),
            penalty=int(arguments.get("penalty") or 0),
            **_common(context),
        )
    except (room_rules.RuleOperationError, TypeError, ValueError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return _check_result(result)


def execute_group_luck(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    if not _options(context).group_luck:
        return {"ok": False, "error": "coc7 group luck house rule is disabled"}
    try:
        result = room_rules.roll_group_luck(context.room_dir, info, **_common(context))
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return _check_result(result)


def execute_contact_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    if not _options(context).contacts:
        return {"ok": False, "error": "coc7 contacts house rule is disabled"}
    try:
        member = _member(info, arguments, context)
        result = room_rules.roll_contact_check(
            context.room_dir,
            info,
            member,
            name=str(arguments.get("name") or ""),
            in_hometown=bool(arguments.get("in_hometown")),
            remote=bool(arguments.get("remote")),
            same_profession=bool(arguments.get("same_profession")),
            base_difficulty=str(arguments.get("base_difficulty") or "regular"),
            pushed=bool(arguments.get("pushed")),
            **_common(context),
        )
    except (room_rules.RuleOperationError, TypeError, ValueError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return _check_result(result)


def execute_economy(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    info = context.room_info()
    if not _options(context).economy:
        return {"ok": False, "error": "coc7 economy house rule is disabled"}
    try:
        member = _member(info, arguments, context)
        if bool(arguments.get("apply")):
            amount = _number(arguments.get("amount"))
            card = room_rules.member_card(member)
            assets = card.get("assets") if isinstance(card.get("assets"), dict) else {}
            current_cash = room_rules._as_int(assets.get("cash"), 0)
            predicted_after = max(0, current_cash - int(amount or 0))
            conflict = room_ai_changes.rollback_conflict(
                info, member, field="cash", name="现金", value=predicted_after
            )
            if conflict is not None:
                return {
                    "ok": True,
                    "skipped": True,
                    "rolled_back": True,
                    "current": current_cash,
                    "summary": (
                        f"房主已回滚该笔现金改动，当前现金为 {current_cash}，"
                        "以当前角色卡为准，不要重试。"
                    ),
                }
            result = room_rules.apply_economy_expense(
                context.room_dir,
                info,
                member,
                amount=amount,
                era=str(arguments.get("era") or "1920s"),
                reason=str(arguments.get("reason") or ""),
                actor=getattr(context, "username", None) or "AI KP",
                actor_id=getattr(context, "user_id", None),
            )
            if result.get("applied"):
                record = result.get("record") if isinstance(result.get("record"), dict) else {}
                room_ai_changes.record_change(
                    context.room_dir,
                    info,
                    member,
                    tool="coc7.economy",
                    field="cash",
                    name="现金",
                    before=result.get("before_cash"),
                    after=result.get("after_cash"),
                    record_id=str(record.get("id") or ""),
                    reason=str(arguments.get("reason") or ""),
                    actor=getattr(context, "username", None) or "AI KP",
                    actor_id=getattr(context, "user_id", None),
                )
        else:
            result = room_rules.economy_summary(
                member,
                amount=_number(arguments.get("amount")),
                era=str(arguments.get("era") or "1920s"),
                assets=_number(arguments.get("assets")),
                info=info,
            )
    except room_rules.RuleOperationError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, **result}


SOCIAL_CHECK_TOOL = AgentTool(
    name="coc7.social_check",
    description=(
        "Resolve a CoC7 social skill check (charm/fast talk/intimidate/persuade) against an NPC. "
        "The difficulty is derived from the NPC's best social or psychology skill, or from "
        "target_skill when the NPC has no card; attitude and concrete leverage shift it. "
        "Use only when the social difficulty house rule is enabled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "name": {"type": "string", "description": "Skill to check on the player's card, e.g. 说服 / 取悦 / 恐吓 / 话术."},
            "attitude": {
                "type": "string",
                "enum": ["supportive", "neutral", "opposed", "hostile"],
                "description": "NPC attitude toward the request.",
            },
            "leverage": {"type": "boolean", "description": "Whether the player offers a concrete bargaining chip or threat."},
            "target_skill": {
                "type": "integer",
                "description": "NPC's social/psychology skill when no NPC card exists; omit to assume 0.",
            },
        },
        "required": ["player_name", "name"],
    },
    handler=execute_social_check,
)

COMBINED_CHECK_TOOL = AgentTool(
    name="coc7.combined_check",
    description=(
        "Resolve a CoC7 combined skill check: roll once and compare the same roll against several "
        "skills. mode=all requires every skill to pass, mode=any requires one. Never roll separately "
        "per skill. Use only when the combined check house rule is enabled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skills on the character card to compare against one roll.",
            },
            "mode": {"type": "string", "enum": ["all", "any"], "description": "Whether all or any skill must pass."},
            "difficulty": {
                "type": "string",
                "enum": ["regular", "hard", "extreme"],
                "description": "Difficulty applied to every skill.",
            },
            "bonus": {"type": "integer", "description": "Bonus dice (0-2)."},
            "penalty": {"type": "integer", "description": "Penalty dice (0-2)."},
        },
        "required": ["player_name", "skills"],
    },
    handler=execute_combined_check,
)

GROUP_LUCK_TOOL = AgentTool(
    name="coc7.group_luck",
    description=(
        "Resolve a CoC7 group luck check. The active investigator with the lowest Luck rolls once on "
        "behalf of the whole group. Use only when the group luck house rule is enabled."
    ),
    parameters={"type": "object", "properties": {}},
    handler=execute_group_luck,
)

CONTACT_CHECK_TOOL = AgentTool(
    name="coc7.contact_check",
    description=(
        "Resolve a CoC7 contact-making check. The player picks a skill; being away from home or in a "
        "remote place raises difficulty, while a same-profession contact in the hometown lowers it. "
        "pushed=true marks a pushed failure, which may yield a hostile contact. Use only when the "
        "contacts house rule is enabled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "name": {"type": "string", "description": "Skill or attribute used to find the contact."},
            "in_hometown": {"type": "boolean", "description": "The search happens in the investigator's hometown."},
            "remote": {"type": "boolean", "description": "The search happens far from home or in a remote place."},
            "same_profession": {"type": "boolean", "description": "Looking for a contact in the same profession."},
            "base_difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"]},
            "pushed": {"type": "boolean", "description": "Whether this is a pushed retry after a failure."},
        },
        "required": ["player_name", "name"],
    },
    handler=execute_contact_check,
)

ECONOMY_TOOL = AgentTool(
    name="coc7.economy",
    description=(
        "Read a CoC7 investigator's credit rating, cash, other assets and daily spending level, and "
        "optionally preview one expense (cash first, then other assets). Set apply=true only when the "
        "purchase actually happens: the tool then deducts cash and queues a pending asset change that "
        "is merged into the persisted character card on room archive. Other assets are free text and "
        "are not deducted numerically. Use only when the economy house rule is enabled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "amount": {"type": "number", "description": "Expense amount. Required when apply=true."},
            "era": {"type": "string", "enum": ["1920s", "modern"], "description": "Price table era."},
            "assets": {"type": "number", "description": "Override for other assets in the read-only expense preview."},
            "apply": {
                "type": "boolean",
                "description": "When true, actually settle the expense and queue the cash change for write-back.",
            },
            "reason": {"type": "string", "description": "Short reason recorded with the pending cash change."},
        },
        "required": ["player_name"],
    },
    handler=execute_economy,
)

def execute_spend(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """``character.spend``：结算一笔开销，等价于 ``coc7.economy`` 的 apply 模式。"""
    return execute_economy({**arguments, "apply": True}, context)


def execute_experience_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """``character.experience_check``：对单个已打勾技能立即结算成长。"""
    from trpg_server.room_interlude import InterludeError, experience_check

    info = context.room_info()
    try:
        member = _member(info, arguments, context)
        result = experience_check(
            context.room_dir,
            info,
            member,
            skill=str(arguments.get("skill") or ""),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
        )
    except (room_rules.RuleOperationError, InterludeError, ValueError, TypeError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return {"ok": True, **result}


def execute_train(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """``character.train``：幕间花周数训练技能（可选房规）。"""
    from trpg_server.room_interlude import InterludeError, train_skill

    info = context.room_info()
    try:
        member = _member(info, arguments, context)
        result = train_skill(
            context.room_dir,
            info,
            member,
            skill=str(arguments.get("skill") or ""),
            weeks=arguments.get("weeks"),
            reason=str(arguments.get("reason") or ""),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
        )
    except (room_rules.RuleOperationError, InterludeError, ValueError, TypeError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return {"ok": True, **result}


def execute_san_treat(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """``san.treat``：月度心理治疗的薄入口，数字委托 ``ruleset/coc7/treatment.py``。"""
    from trpg_server import room_sanity

    info = context.room_info()
    try:
        member = _member(info, arguments, context)
        result = room_sanity.treatment_session(
            context.room_dir,
            info,
            member,
            level=str(arguments.get("level") or "regular"),
            months=int(arguments.get("months") or 1),
            note=str(arguments.get("note") or ""),
            actor=getattr(context, "username", None) or "AI KP",
            actor_id=getattr(context, "user_id", None),
        )
    except (room_rules.RuleOperationError, ValueError, TypeError) as exc:
        return {"ok": False, "error": getattr(exc, "message", str(exc))}
    return {"ok": True, **result}


CHECK_COMBINED_TOOL = AgentTool(
    name="check.combined",
    description=(
        "Roadmap alias of coc7.combined_check: one roll compared against several skills. "
        "mode=all requires every skill to pass, mode=any requires one. Requires the combined "
        "check house rule."
    ),
    parameters=COMBINED_CHECK_TOOL.parameters,
    handler=execute_combined_check,
)

CHECK_GROUP_TOOL = AgentTool(
    name="check.group",
    description=(
        "Roadmap alias of coc7.group_luck: the active investigator with the lowest Luck rolls "
        "once for the whole group. Requires the group luck house rule."
    ),
    parameters=GROUP_LUCK_TOOL.parameters,
    handler=execute_group_luck,
)

CHARACTER_CONTACTS_TOOL = AgentTool(
    name="character.contacts",
    description=(
        "Roadmap alias of coc7.contact_check: resolve a contact-making check, with hometown / "
        "remote / same profession modifiers and pushed failures. Requires the contacts house rule."
    ),
    parameters=CONTACT_CHECK_TOOL.parameters,
    handler=execute_contact_check,
)

CHARACTER_SPEND_TOOL = AgentTool(
    name="character.spend",
    description=(
        "Settle one expense: cash is deducted immediately and the cash change is queued for "
        "write-back on archive. Other assets stay free text. Requires the economy house rule."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "amount": {"type": "number", "description": "Expense amount."},
            "era": {"type": "string", "enum": ["1920s", "modern"], "description": "Price table era."},
            "reason": {"type": "string", "description": "Short reason recorded with the pending cash change."},
        },
        "required": ["player_name", "amount"],
    },
    handler=execute_spend,
)

CHARACTER_EXPERIENCE_CHECK_TOOL = AgentTool(
    name="character.experience_check",
    description=(
        "Roll one growth check for a single marked skill during the interlude. Only skills that "
        "already have a growth mark can be checked; the normal batch settlement is interlude settle."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "skill": {"type": "string", "description": "Skill that already has a growth mark."},
        },
        "required": ["player_name", "skill"],
    },
    handler=execute_experience_check,
)

CHARACTER_TRAIN_TOOL = AgentTool(
    name="character.train",
    description=(
        "Spend interlude weeks training one skill with Keeper approval: every 4 weeks grants one "
        "growth check. Requires the optional training house rule (coc7_growth_training)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "skill": {"type": "string", "description": "Skill to train."},
            "weeks": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Weeks spent; every 4 weeks = 1 check."},
            "reason": {"type": "string", "description": "Short note recorded with the training."},
        },
        "required": ["player_name", "skill", "weeks"],
    },
    handler=execute_train,
)

SAN_TREAT_TOOL = AgentTool(
    name="san.treat",
    description=(
        "Resolve monthly psychotherapy: a successful Psychoanalysis check restores +1D3 SAN, a "
        "fumble costs 1D6 SAN, other results change nothing. Requires structured sanity state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "player_name": {"type": "string", "description": "Room member's login username or character card name."},
            "level": {
                "type": "string",
                "enum": ["regular", "hard", "extreme", "critical", "success", "failure", "fumble"],
                "description": "This month's Psychoanalysis result.",
            },
            "months": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Number of months treated."},
            "note": {"type": "string", "description": "Optional note stored with the treatment record."},
        },
        "required": ["player_name"],
    },
    handler=execute_san_treat,
)

COC7_EXTRA_TOOLS = (
    SOCIAL_CHECK_TOOL,
    COMBINED_CHECK_TOOL,
    GROUP_LUCK_TOOL,
    CONTACT_CHECK_TOOL,
    ECONOMY_TOOL,
    CHECK_COMBINED_TOOL,
    CHECK_GROUP_TOOL,
    CHARACTER_CONTACTS_TOOL,
    CHARACTER_SPEND_TOOL,
    CHARACTER_EXPERIENCE_CHECK_TOOL,
    CHARACTER_TRAIN_TOOL,
    SAN_TREAT_TOOL,
)

COC7_EXTRA_TOOL_NAMES = tuple(tool.name for tool in COC7_EXTRA_TOOLS)
