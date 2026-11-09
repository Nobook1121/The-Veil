"""规则无关的检定调度。

REST 路由、房间控制台与 AI 工具都通过本模块结算检定；具体判定由所选规则集插件
的 ``checks`` 能力提供，本模块只处理输入校验、难度 / 房规阈值与结果格式。
这样换规则集时只需要换插件，不需要再改路由或骰子工具。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from trpg_server.ruleset import CheckRequest, get_plugin


class CheckServiceError(ValueError):
    """检定输入或规则执行失败；消息可直接返回给调用方。"""


def _dice_module():
    from trpg_server.agents.tools import dice

    return dice


def _lookup_target(checks: Any, card: Mapping[str, Any], name: str) -> int | None:
    """优先走插件的目标值查询；旧插件没有该方法时退回骰子工具的兼容实现。"""
    lookup = getattr(checks, "lookup_target", None)
    if callable(lookup):
        return lookup(card, name)
    dice = _dice_module()
    return dice._lookup_check_value(dict(card or {}), name)


def run_room_check(
    member: Mapping[str, Any],
    *,
    context: Any = None,
    player_name: str | None = None,
    name: str,
    difficulty: str = "regular",
    adjustment: Any = None,
    bonus: int = 0,
    penalty: int = 0,
    critical_threshold: Any = None,
    fumble_threshold: Any = None,
    ruleset_id: str | None = None,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """结算一次房间检定，返回与旧骰子工具兼容的结果字典。"""
    dice = _dice_module()
    if not isinstance(member, Mapping):
        raise CheckServiceError("player was not found in current room")

    resolved_name = str(player_name or "").strip()
    if not resolved_name:
        card_hint = member.get("character_card")
        card_hint = card_hint if isinstance(card_hint, Mapping) else {}
        resolved_name = str(
            member.get("username") or member.get("character_name") or card_hint.get("name") or ""
        ).strip()
    if not resolved_name:
        raise CheckServiceError("player_name is required")

    check_name = str(name or "").strip()
    if not check_name:
        raise CheckServiceError("name is required")

    difficulty_key = dice._normalize_difficulty(difficulty)
    if not difficulty_key:
        raise CheckServiceError("difficulty must be regular, hard, extreme, 普通, 困难, or 极难")

    card = member.get("character_card")
    if not isinstance(card, Mapping):
        raise CheckServiceError(f"player {resolved_name} has no bound character card")

    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持检定")

    base_target = _lookup_target(checks, card, check_name)
    if base_target is None or base_target < 0:
        if check_name.casefold() in dice._member_names(dict(member)):
            raise CheckServiceError(
                f"“{check_name}”是玩家/角色名，不是技能名；name 参数应填写要检定的技能或属性"
                "（如 力量/敏捷/侦察/聆听/心理学/理智）。"
            )
        if not dice._is_known_check_name(check_name):
            raise CheckServiceError(
                f"“{check_name}”不是 COC7 标准属性或技能目录中的技能，已拒绝该检定；"
                "请改用标准检定名（如 侦察/聆听/心理学/力量/敏捷）或角色卡上已有的技能。"
            )
        raise CheckServiceError(f"{check_name} was not found on player {resolved_name}'s character card")
    if base_target > 100:
        raise CheckServiceError(f"{check_name} value must be between 0 and 100")

    bonus_dice = max(0, min(10, int(bonus or 0)))
    penalty_dice = max(0, min(10, int(penalty or 0)))
    # 奖励与惩罚同时存在时互相抵消，只有净额生效（上限 2 颗）。
    bonus_dice, penalty_dice = dice._normalize_bonus_penalty(bonus_dice, penalty_dice)

    if context is not None and hasattr(dice, "_context_thresholds"):
        default_critical, default_fumble = dice._context_thresholds(context)
    else:
        default_critical, default_fumble = dice.DEFAULT_CRITICAL_THRESHOLD, dice.DEFAULT_FUMBLE_THRESHOLD
    critical_value = dice._threshold_arg(
        {"critical_threshold": critical_threshold}, "critical_threshold", default_critical
    )
    fumble_value = dice._threshold_arg(
        {"fumble_threshold": fumble_threshold}, "fumble_threshold", default_fumble
    )

    request = CheckRequest(
        kind="skill",
        name=check_name,
        difficulty=difficulty_key,
        bonus=bonus_dice,
        penalty=penalty_dice,
        extra={
            "adjustment": adjustment,
            "faces": faces,
            "critical_threshold": critical_value,
            "fumble_threshold": fumble_value,
        },
    )
    try:
        resolution = checks.roll(card, request, rng or (lambda sides: dice.random.randint(1, sides)))
    except ValueError as exc:
        raise CheckServiceError(str(exc)) from exc

    detail = resolution.detail if isinstance(resolution.detail, dict) else {}
    threshold = int(resolution.target)
    level = str(resolution.level or "failure")
    success = bool(resolution.success)
    display_name = str(detail.get("display_name") or dice._resolve_check_display_name(dict(card), check_name))
    candidates = [int(value) for value in (detail.get("candidates") or [])]

    summary = (
        dice._format_room_check_summary(display_name, difficulty_key, int(resolution.roll), threshold, success)
        + dice._level_suffix(level)
    )
    modifier = str(adjustment or "").strip()
    if candidates:
        summary += f" {'奖励骰' if bonus_dice else '惩罚骰'}（{', '.join(map(str, candidates))}），取{int(resolution.roll)}。"

    result: dict[str, Any] = {
        "player_name": resolved_name,
        "character_name": card.get("name"),
        "name": display_name,
        "roll": int(resolution.roll),
        "rolls": candidates or [int(resolution.roll)],
        "base_target": int(detail.get("base_target", base_target)),
        "target": threshold,
        "difficulty": difficulty_key,
        "difficulty_label": dice.DIFFICULTY_LABELS[difficulty_key],
        "adjustment": modifier,
        "bonus_dice": bonus_dice,
        "penalty_dice": penalty_dice,
        "success": success,
        "success_level": level if success else "failure",
        "raw_success_level": level,
        "critical": level == "critical",
        "fumble": level == "fumble",
        "critical_threshold": critical_value,
        "fumble_threshold": fumble_value,
        "summary": summary,
    }
    result["visible_message"] = dice._result_message(
        summary,
        "check.roll_room_check",
        check_type="room",
        player_name=resolved_name,
        name=display_name,
        extra_metadata={
            "dice_roll": dice._dice_roll_metadata(
                result,
                mode="percentile",
                check_name=display_name,
                difficulty=difficulty_key,
                target=threshold,
                roller=resolved_name,
            )
        },
    )
    return result


def _resolve_member(member: Mapping[str, Any], player_name: str | None) -> tuple[Mapping[str, Any], str]:
    """校验成员与其角色卡，返回 ``(card, 展示名)``。"""
    if not isinstance(member, Mapping):
        raise CheckServiceError("player was not found in current room")
    card = member.get("character_card")
    if not isinstance(card, Mapping):
        raise CheckServiceError("player has no bound character card")
    name = str(player_name or "").strip()
    if not name:
        name = str(member.get("username") or member.get("character_name") or card.get("name") or "").strip()
    if not name:
        raise CheckServiceError("player_name is required")
    return card, name


def _capability(checks: Any, method: str, plugin: Any) -> Callable[..., Any]:
    fn = getattr(checks, method, None)
    if not callable(fn):
        raise CheckServiceError(f"规则集 {getattr(plugin, 'id', '')} 不支持 {method}")
    return fn


def _rewrite_visible(result: Mapping[str, Any], summary: str) -> None:
    message = result.get("visible_message")
    if isinstance(message, dict):
        message["content"] = summary


def run_room_social_check(
    member: Mapping[str, Any],
    *,
    context: Any = None,
    player_name: str | None = None,
    name: str,
    target_skill: Any = None,
    target_card: Mapping[str, Any] | None = None,
    attitude: str = "neutral",
    leverage: bool = False,
    ruleset_id: str | None = None,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """5.10 社交检定：先由插件给出难度，再走统一检定入口。"""
    dice = _dice_module()
    card, resolved_name = _resolve_member(member, player_name)
    check_name = str(name or "").strip()
    if not check_name:
        raise CheckServiceError("name is required")

    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持检定")
    planner = _capability(checks, "social_difficulty", plugin)
    try:
        plan = planner(
            card,
            target_skill=target_skill,
            target_card=target_card,
            attitude=attitude,
            leverage=leverage,
        )
    except (TypeError, ValueError) as exc:
        raise CheckServiceError(str(exc)) from exc

    if plan.get("auto_success"):
        summary = f"社交检定：{plan.get('reason') or '目标倾向支持，无需检定'}。"
        result: dict[str, Any] = {
            "player_name": resolved_name,
            "character_name": card.get("name"),
            "name": check_name,
            "auto_success": True,
            "success": True,
            "success_level": "success",
            "raw_success_level": "success",
            "roll": None,
            "social": plan,
            "summary": summary,
        }
        result["visible_message"] = dice._result_message(
            summary,
            "coc7.social_check",
            check_type="social",
            player_name=resolved_name,
            name=check_name,
        )
        return result

    result = run_room_check(
        member,
        context=context,
        player_name=resolved_name,
        name=check_name,
        difficulty=plan["difficulty"],
        ruleset_id=ruleset_id,
        rng=rng,
        faces=faces,
    )
    result["social"] = plan
    summary = f"社交检定（目标技能 {plan['target_skill']}）：{result['summary']}"
    result["summary"] = summary
    _rewrite_visible(result, summary)
    return result


def run_room_combined_check(
    member: Mapping[str, Any],
    *,
    player_name: str | None = None,
    names: list[str] | tuple[str, ...] | None,
    mode: str = "all",
    difficulty: str = "regular",
    bonus: int = 0,
    penalty: int = 0,
    ruleset_id: str | None = None,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """5.9 组合检定：只掷一次，插件负责与多个技能比较。"""
    dice = _dice_module()
    card, resolved_name = _resolve_member(member, player_name)
    skill_names = [str(item or "").strip() for item in (names or []) if str(item or "").strip()]
    if not skill_names:
        raise CheckServiceError("at least one skill is required")
    difficulty_key = dice._normalize_difficulty(difficulty)
    if not difficulty_key:
        raise CheckServiceError("difficulty must be regular, hard, extreme, 普通, 困难, or 极难")

    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持检定")
    resolver = _capability(checks, "combined_check", plugin)
    try:
        resolution = resolver(
            card,
            skill_names,
            mode=mode,
            difficulty=difficulty_key,
            bonus=bonus,
            penalty=penalty,
            rng=rng,
            faces=faces,
        )
    except (TypeError, ValueError) as exc:
        raise CheckServiceError(str(exc)) from exc

    rows = []
    for row in resolution.get("skills") or []:
        display = dice._resolve_check_display_name(dict(card), str(row.get("name") or ""))
        rows.append({**row, "display_name": display})
    labels = "、".join(
        f"{row['display_name']}{'成功' if row.get('success') else '失败'}" for row in rows
    )
    mode_label = "全部成功" if resolution.get("mode") == "all" else "任一成功"
    summary = f"组合检定（{mode_label}）：掷出 {int(resolution['roll'])}；{labels}。"
    success = bool(resolution.get("success"))
    result: dict[str, Any] = {
        "player_name": resolved_name,
        "character_name": card.get("name"),
        "name": "组合检定",
        "roll": int(resolution["roll"]),
        "rolls": [int(value) for value in (resolution.get("candidates") or [int(resolution["roll"])])],
        "target": None,
        "difficulty": difficulty_key,
        "difficulty_label": dice.DIFFICULTY_LABELS[difficulty_key],
        "bonus_dice": int(resolution.get("bonus_dice") or 0),
        "penalty_dice": int(resolution.get("penalty_dice") or 0),
        "success": success,
        "success_level": "success" if success else "failure",
        "raw_success_level": "success" if success else "failure",
        "critical": False,
        "fumble": False,
        "mode": resolution.get("mode"),
        "skills": rows,
        "summary": summary,
    }
    result["visible_message"] = dice._result_message(
        summary,
        "coc7.combined_check",
        check_type="combined",
        player_name=resolved_name,
        name="组合检定",
        extra_metadata={
            "dice_roll": dice._dice_roll_metadata(
                result,
                mode="percentile",
                check_name="组合检定",
                difficulty=difficulty_key,
                roller=resolved_name,
            )
        },
    )
    return result


def run_room_group_luck(
    members: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    ruleset_id: str | None = None,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """5.6 团体幸运：由在场最低幸运者代掷一次。"""
    dice = _dice_module()
    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持检定")
    resolver = _capability(checks, "group_luck", plugin)
    try:
        resolution = resolver(list(members or []), rng=rng, faces=faces)
    except (TypeError, ValueError) as exc:
        raise CheckServiceError(str(exc)) from exc

    luck = int(resolution.get("luck") or 0)
    roll = int(resolution["roll"])
    level = str(resolution.get("level") or "failure")
    success = bool(resolution.get("success"))
    summary = (
        f"团体幸运检定：由 {resolution.get('player_name') or '未知成员'}（幸运 {luck}）代掷 {roll}，"
        f"{'成功' if success else '失败'}{dice._level_suffix(level)}。"
    )
    result: dict[str, Any] = {
        "player_name": str(resolution.get("player_name") or ""),
        "character_name": resolution.get("character_name"),
        "name": "团体幸运",
        "roll": roll,
        "rolls": [roll],
        "target": luck,
        "success": success,
        "success_level": level if success else "failure",
        "raw_success_level": level,
        "critical": level == "critical",
        "fumble": level == "fumble",
        "luck": luck,
        "summary": summary,
    }
    result["visible_message"] = dice._result_message(
        summary,
        "coc7.group_luck",
        check_type="group_luck",
        player_name=result["player_name"],
        name="团体幸运",
        extra_metadata={
            "dice_roll": dice._dice_roll_metadata(
                result,
                mode="percentile",
                check_name="团体幸运",
                target=luck,
                roller=result["player_name"],
            )
        },
    )
    return result


def run_room_contact_check(
    member: Mapping[str, Any],
    *,
    context: Any = None,
    player_name: str | None = None,
    name: str,
    in_hometown: bool = False,
    remote: bool = False,
    same_profession: bool = False,
    base_difficulty: str = "regular",
    pushed: bool = False,
    ruleset_id: str | None = None,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """5.13 建立熟人：插件给出难度，统一检定入口掷骰，再映射后果提示。"""
    card, resolved_name = _resolve_member(member, player_name)
    check_name = str(name or "").strip()
    if not check_name:
        raise CheckServiceError("name is required")

    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持检定")
    planner = _capability(checks, "contact_plan", plugin)
    try:
        plan = planner(
            card,
            check_name,
            in_hometown=in_hometown,
            remote=remote,
            same_profession=same_profession,
            base_difficulty=base_difficulty,
        )
    except (TypeError, ValueError) as exc:
        raise CheckServiceError(str(exc)) from exc

    result = run_room_check(
        member,
        context=context,
        player_name=resolved_name,
        name=check_name,
        difficulty=plan["difficulty"],
        ruleset_id=ruleset_id,
        rng=rng,
        faces=faces,
    )
    outcome_fn = getattr(checks, "contact_outcome", None)
    outcome = outcome_fn(bool(result["success"]), pushed=pushed) if callable(outcome_fn) else {}
    result["contact"] = {**plan, **outcome}
    hint = str(outcome.get("hint") or "").strip()
    summary = f"建立熟人检定：{result['summary']}{(' ' + hint) if hint else ''}"
    result["summary"] = summary
    _rewrite_visible(result, summary)
    return result


def run_room_economy(
    member: Mapping[str, Any],
    *,
    player_name: str | None = None,
    amount: Any = None,
    era: str = "1920s",
    assets: Any = None,
    ruleset_id: str | None = None,
) -> dict[str, Any]:
    """5.12 信用评级与开支计算（只读，不写回角色卡）。"""
    card, resolved_name = _resolve_member(member, player_name)

    plugin = get_plugin(ruleset_id)
    checks = getattr(plugin, "checks", None)
    if checks is None:
        raise CheckServiceError(f"规则集 {plugin.id} 不支持经济规则")
    profile_fn = _capability(checks, "economy_profile", plugin)

    credit = _lookup_target(checks, card, "信用评级")
    if credit is None:
        credit = card.get("creditRating")
    if credit is None:
        card_assets = card.get("assets")
        credit = card_assets.get("spendingLevel") if isinstance(card_assets, Mapping) else None
    try:
        credit = max(0, min(99, int(credit or 0)))
        profile = profile_fn(credit, era)
    except (TypeError, ValueError) as exc:
        raise CheckServiceError(str(exc)) from exc

    result: dict[str, Any] = {
        "player_name": resolved_name,
        "character_name": card.get("name"),
        "credit": credit,
        "era": profile.get("era", era),
        "profile": profile,
        "summary": (
            f"{card.get('name') or resolved_name} 信用评级 {credit}（{profile.get('label')}）："
            f"现金 {profile.get('cash')} / 其他资产 {profile.get('assets')} / 每日消费水平 {profile.get('spending')}。"
        ),
    }
    if amount is not None:
        expense_fn = _capability(checks, "economy_expense", plugin)
        card_assets = card.get("assets") if isinstance(card.get("assets"), Mapping) else {}
        cash = card_assets.get("cash") or 0
        asset_value = assets if assets is not None else 0
        try:
            expense = expense_fn(
                cash=cash,
                assets=asset_value,
                amount=amount,
                cr=credit,
                era=profile.get("era", era),
            )
        except (TypeError, ValueError) as exc:
            raise CheckServiceError(str(exc)) from exc
        result["expense"] = expense
        if expense.get("within_standard"):
            result["summary"] += f" 本次开支 {amount} 在消费水平内，不动用现金。"
        else:
            result["summary"] += (
                f" 本次开支 {amount}：现金 → {expense.get('cash')}，"
                f"其他资产 → {expense.get('assets')}。"
            )
            if float(expense.get("uncovered") or 0) > 0:
                result["summary"] += f" 仍有 {expense['uncovered']} 未覆盖，需借款或变卖。"
    return result

