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

COC7_EXTRA_TOOLS = (
    SOCIAL_CHECK_TOOL,
    COMBINED_CHECK_TOOL,
    GROUP_LUCK_TOOL,
    CONTACT_CHECK_TOOL,
    ECONOMY_TOOL,
)

COC7_EXTRA_TOOL_NAMES = tuple(tool.name for tool in COC7_EXTRA_TOOLS)
