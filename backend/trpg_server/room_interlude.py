"""房间级「幕间」状态与结算。

幕间是显式房间阶段：
- 只有房主 / 房间管理员 / 系统管理员可以把房间切入幕间；AI 与玩家只能提议。
- 幕间期间不推进剧情、不允许回溯存档节点；允许自由发言、复盘与购买物品。
- 进入幕间时一次性结算成长检定、90%+ 技能 SAN 恢复与可选信用评级变动。
- 结束幕间必须二次确认，随后走现有归档链路。

旧房间没有 ``interlude`` 字段时一律按「非幕间」处理，不写回、不改变行为。
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server import room_rules
from trpg_server.json_store import read_json
from trpg_server.room_log import load_room_log, log_room_event

INTERLUDE_KEY = "interlude"
MAX_REVIEWS = 20
MAX_MARKERS = 50

OPERATOR_RANK = 2

# 幕间期间禁止下发给 AI 的工具：所有推进剧情、触发、战斗与直接改数值的入口。
INTERLUDE_BLOCKED_TOOLS = {
    "room.get_scenario_context",
    "room.get_scenario_module",
    "room.activate_scenario_scene",
    "room.remember_fact",
    "room.request_interlude",
    "trigger.reveal_scenario_trigger",
    "trigger.list_available",
    "trigger.request",
    "narrative.get_current_location",
    "narrative.lookup_location",
    "narrative.list_exits",
    "narrative.move_to_location",
    "narrative.inspect_object",
    "narrative.resolve_interaction",
    "narrative.reveal_clue",
    "narrative.update_npc_state",
    "narrative.write_fact",
    "combat.start",
    "combat.join",
    "combat.state",
    "combat.next",
    "combat.delay",
    "combat.disengage",
    "combat.reload",
    "combat.ready",
    "combat.cast",
    "combat.attack",
    "combat.shoot",
    "combat.damage",
    "combat.heal",
    "combat.treat",
    "combat.end",
    "character.set_skill",
    "character.set_attribute",
}


class InterludeError(Exception):
    """幕间操作失败；消息可直接展示。"""

    def __init__(self, message: str, *, code: str = "interlude_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(100, 999)}"


def interlude_state(info: dict[str, Any] | None) -> dict[str, Any]:
    """返回归一化后的幕间状态；缺省房间按非幕间处理。

    生命周期字段（``interlude``）只由房主 / 管理员写入；玩家提议与复盘分别落在
    ``interlude_request`` 与 ``interlude_reviews``，避免玩家操作改写房间阶段。
    """
    data = info or {}
    raw = data.get(INTERLUDE_KEY)
    state = dict(raw) if isinstance(raw, dict) else {}
    state["active"] = bool(state.get("active"))
    state.setdefault("startedAt", None)
    state.setdefault("startedBy", "")
    state.setdefault("completedAt", None)
    state["request"] = data.get("interlude_request") if isinstance(data.get("interlude_request"), dict) else None
    reviews = data.get("interlude_reviews")
    state["reviews"] = reviews if isinstance(reviews, list) else []
    state.setdefault("settlement", {})
    return state


def is_active(info: dict[str, Any] | None) -> bool:
    return bool(interlude_state(info).get("active"))


def snapshot(info: dict[str, Any] | None) -> dict[str, Any]:
    """给房间摘要 / 前端快照使用的只读副本。"""
    state = interlude_state(info)
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    return {
        "active": state["active"],
        "startedAt": state.get("startedAt"),
        "startedBy": state.get("startedBy"),
        "completedAt": state.get("completedAt"),
        "request": state.get("request"),
        "settledAt": settlement.get("settled_at"),
        "reviewCount": len(state.get("reviews") or []),
    }


def _ensure_state(info: dict[str, Any]) -> dict[str, Any]:
    state = interlude_state(info)
    state.pop("request", None)
    state.pop("reviews", None)
    info[INTERLUDE_KEY] = state
    return state


def _require_not_archived(info: dict[str, Any]) -> None:
    if info.get("archived"):
        raise InterludeError("房间已归档，幕间操作已关闭", code="room_archived", status=409)


def request_interlude(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
    source: str = "player",
    reason: str = "",
) -> dict[str, Any]:
    """记录一条「结束剧情 / 进入幕间」的提议，不改变房间阶段。"""
    _require_not_archived(info)
    state = interlude_state(info)
    if state["active"]:
        raise InterludeError("房间已经处于幕间", code="already_active", status=409)
    request = {
        "id": _new_id("itl-req"),
        "at": _now(),
        "by": str(actor or ""),
        "byId": str(actor_id or ""),
        "source": str(source or "player")[:20],
        "reason": str(reason or "").strip()[:300],
    }
    info["interlude_request"] = request
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "提议进入幕间", actor=actor, actor_id=actor_id, source=source, reason=request["reason"])
    return request


def _credit_row(card: dict[str, Any]) -> dict[str, Any] | None:
    targets = {"信用评级", "creditrating", "credit rating", "credit_rating"}
    for row in card.get("skills") or []:
        if not isinstance(row, dict):
            continue
        keys = {str(row.get("name") or "").strip().casefold(), str(row.get("skillKey") or "").strip().casefold()}
        if keys & targets:
            return row
    return None


def _credit_value(card: dict[str, Any]) -> int:
    row = _credit_row(card)
    if row is not None:
        return max(0, min(99, room_rules._as_int(row.get("value"), 0)))
    return max(0, min(99, room_rules._as_int(card.get("creditRating"), 0)))


def _active_members(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        member
        for member in (info.get("members") or [])
        if isinstance(member, dict)
        and member.get("is_active", True) is not False
        and str(member.get("status") or "active") != "removed"
        and isinstance(member.get("character_card"), dict)
    ]


def _apply_credit_events(
    room_dir: Any,
    info: dict[str, Any],
    state: dict[str, Any],
    events: list[dict[str, Any]] | None,
    *,
    rng: Callable[[int], int],
    actor: Any,
    actor_id: Any,
) -> list[dict[str, Any]]:
    from trpg_server.ruleset.coc7 import economy

    results: list[dict[str, Any]] = []
    for entry in events or []:
        if not isinstance(entry, dict):
            continue
        player = str(entry.get("player") or "").strip()
        event = str(entry.get("event") or "").strip().lower()
        if not player or not event:
            continue
        member = room_rules.find_member(info, player)
        if member is None:
            results.append({"player": player, "event": event, "error": "成员不存在"})
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
        if not card:
            results.append({"player": player, "event": event, "error": "未绑定角色卡"})
            continue
        before = _credit_value(card)
        try:
            outcome = economy.apply_credit_event(
                before,
                event,
                rng=rng,
                era=str(info.get("era") or entry.get("era") or economy.ERA_1920S),
                target_assets=entry.get("target_assets"),
                has_social_security=bool(entry.get("has_social_security")),
            )
        except ValueError as exc:
            results.append({"player": player, "event": event, "error": str(exc)})
            continue
        after = int(outcome["to"])
        row = _credit_row(card)
        if row is not None:
            room_rules.apply_skill_change(
                room_dir,
                info,
                member,
                value=after,
                reason=f"幕间信用评级：{event}",
                actor=actor,
                actor_id=actor_id,
                source="interlude",
            )
        card["creditRating"] = after
        refresh = economy.refresh_economy(
            after,
            era=str(info.get("era") or economy.ERA_1920S),
            current_assets=(card.get("assets") or {}).get("assets") if isinstance(card.get("assets"), dict) else None,
        )
        assets = card.get("assets") if isinstance(card.get("assets"), dict) else {}
        if not isinstance(assets, dict):
            assets = {}
            card["assets"] = assets
        assets["cash"] = refresh["cash"]
        assets["spendingLevel"] = refresh["spending"]
        room_rules.record_room_changes(
            member,
            [
                {"type": "credit", "value": after, "source": "interlude", "reason": f"幕间信用评级：{event}"},
                {
                    "type": "assets",
                    "value": {"cash": refresh["cash"], "spendingLevel": refresh["spending"]},
                    "source": "interlude",
                    "reason": f"幕间现金刷新：{event}",
                },
            ],
        )
        result = {
            "player": room_rules.member_display_name(member),
            "event": event,
            "from": before,
            "to": after,
            "rolls": outcome.get("rolls"),
            "cash": refresh["cash"],
            "spending": refresh["spending"],
        }
        results.append(result)
        state.setdefault("settlement", {})
        settlement = state["settlement"]
        settlement.setdefault("credit", []).append(result)
    room_rules.save_room_info(room_dir, info)
    return results


def settle_interlude(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
    rng: Callable[[int], int] | None = None,
    credit_events: list[dict[str, Any]] | None = None,
    include_growth: bool = True,
) -> dict[str, Any]:
    """一次性结算幕间成长、90%+ SAN 恢复与信用评级变动。"""
    _require_not_archived(info)
    state = _ensure_state(info)
    if not state["active"]:
        raise InterludeError("房间尚未进入幕间", code="not_active", status=409)
    rng = rng or random.randint
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    state["settlement"] = settlement

    if include_growth and not settlement.get("growth_done"):
        member_results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for member in _active_members(info):
            card = member["character_card"]
            member_state = member.get("character_state")
            if not isinstance(member_state, dict):
                member_state = {}
                member["character_state"] = member_state
            marks = member_state.get("growth_marks") if isinstance(member_state.get("growth_marks"), list) else []
            growth = room_rules._growth_capability(card)
            entry: dict[str, Any] = {
                "player": room_rules.member_display_name(member),
                "growth": [],
                "san_recovery": [],
            }
            if growth is not None:
                if marks:
                    try:
                        rows = growth.resolve_member_growth(card, marks, rng)
                    except Exception as exc:  # noqa: BLE001 - 单个成员失败不阻断其他人
                        errors.append({"player": entry["player"], "error": str(exc)})
                        rows = []
                    for row in rows:
                        if row.get("gain"):
                            try:
                                room_rules.apply_skill_change(
                                    room_dir,
                                    info,
                                    member,
                                    name=row["skill"],
                                    value=row["after"],
                                    reason="幕间成长检定",
                                    actor=actor,
                                    actor_id=actor_id,
                                    source="interlude",
                                )
                            except room_rules.RuleOperationError as exc:
                                errors.append({"player": entry["player"], "skill": row["skill"], "error": exc.message})
                                continue
                        entry["growth"].append(row)
                    member_state["growth_marks"] = []
                # 成长后再结算 90%+ 技能恢复，避免刚跨过 90 的技能漏发。
                try:
                    awards = growth.high_skill_san_awards(card, rng)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"player": entry["player"], "error": str(exc)})
                    awards = []
                for award in awards:
                    try:
                        applied = room_rules.apply_san_change(
                            room_dir,
                            info,
                            member,
                            delta=award["san"],
                            reason=f"幕间恢复：技能「{award['skill']}」达到90%+",
                            actor=actor,
                            actor_id=actor_id,
                            source="interlude",
                        )
                    except room_rules.RuleOperationError as exc:
                        errors.append({"player": entry["player"], "skill": award["skill"], "error": exc.message})
                        continue
                    entry["san_recovery"].append({**award, "applied": applied.get("delta", 0)})
            if entry["growth"] or entry["san_recovery"]:
                member_results.append(entry)
        settlement.update(
            {
                "growth_done": True,
                "settled_at": _now(),
                "settled_by": str(actor or ""),
                "members": member_results,
                "errors": errors,
            }
        )

    if credit_events:
        _apply_credit_events(
            room_dir,
            info,
            state,
            credit_events,
            rng=rng,
            actor=actor,
            actor_id=actor_id,
        )

    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "幕间结算完成", actor=actor, actor_id=actor_id, source="interlude")
    return settlement


def start_interlude(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
    rng: Callable[[int], int] | None = None,
    credit_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """进入幕间并立即结算一次。调用方必须先完成权限校验。"""
    _require_not_archived(info)
    state = _ensure_state(info)
    if state["active"]:
        raise InterludeError("房间已经处于幕间", code="already_active", status=409)
    now = _now()
    # 幕间意味着剧情结束：补齐结束时间，结束后才能走现有归档链路。
    if not info.get("completed_at"):
        info["completed_at"] = now
    state.update(
        {
            "active": True,
            "startedAt": now,
            "startedBy": str(actor or ""),
            "startedById": str(actor_id or ""),
            "completedAt": None,
            "settlement": {},
        }
    )
    info.pop("interlude_request", None)
    info[INTERLUDE_KEY] = state
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "进入幕间", actor=actor, actor_id=actor_id, source="interlude")
    settlement = settle_interlude(
        room_dir,
        info,
        actor=actor,
        actor_id=actor_id,
        rng=rng,
        credit_events=credit_events,
        include_growth=True,
    )
    return {"state": snapshot(info), "settlement": settlement}


def end_interlude(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
) -> dict[str, Any]:
    """结束幕间；调用方负责随后的自动归档与二次确认。"""
    _require_not_archived(info)
    state = _ensure_state(info)
    if not state["active"]:
        raise InterludeError("房间当前不在幕间", code="not_active", status=409)
    state.update(
        {
            "active": False,
            "completedAt": _now(),
            "endedBy": str(actor or ""),
            "endedById": str(actor_id or ""),
        }
    )
    info[INTERLUDE_KEY] = state
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "结束幕间", actor=actor, actor_id=actor_id, source="interlude")
    return snapshot(info)


def add_review(
    room_dir: Any,
    info: dict[str, Any],
    *,
    content: str,
    topic: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "player",
) -> dict[str, Any]:
    """保存一条幕间复盘记录。"""
    _require_not_archived(info)
    text = str(content or "").strip()
    if not text:
        raise InterludeError("复盘内容不能为空", code="empty_review")
    record = {
        "id": _new_id("itl-review"),
        "at": _now(),
        "by": str(actor or ""),
        "byId": str(actor_id or ""),
        "source": str(source or "player")[:20],
        "topic": str(topic or "").strip()[:80],
        "content": text[:4000],
    }
    reviews = info.get("interlude_reviews") if isinstance(info.get("interlude_reviews"), list) else []
    reviews.insert(0, record)
    del reviews[MAX_REVIEWS:]
    info["interlude_reviews"] = reviews
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "保存幕间复盘", actor=actor, actor_id=actor_id, source=source, reason=record["topic"])
    return record


def record_growth_mark(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    skill: str,
    value: int,
    success_level: str = "",
    source: str = "check",
) -> dict[str, Any] | None:
    """剧情中技能检定成功后立即打勾；同一技能只保留一个标记。"""
    if is_active(info):
        return None
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    marks = state.get("growth_marks") if isinstance(state.get("growth_marks"), list) else []
    key = str(skill or "").strip().casefold()
    if any(isinstance(row, dict) and str(row.get("skill") or "").strip().casefold() == key for row in marks):
        return None
    record = {
        "id": _new_id("itl-mark"),
        "skill": str(skill or "").strip(),
        "value": int(value),
        "at": _now(),
        "success_level": str(success_level or ""),
        "source": str(source or "check"),
    }
    marks.insert(0, record)
    del marks[MAX_MARKERS:]
    state["growth_marks"] = marks
    room_rules.save_room_info(room_dir, info)
    return record


def review_context(room_dir: Any, info: dict[str, Any]) -> dict[str, Any]:
    """构建给 AI 的复盘材料：只用已落盘摘要 / 关键帧 / 日志，不读全量聊天。"""
    directory = Path(room_dir)
    state = interlude_state(info)
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    summaries: list[dict[str, Any]] = []
    try:
        from trpg_server.agents.narrative.ledger import load_summaries

        payload = load_summaries(directory)
        for scope, entries in payload.items():
            for key, entry in (entries or {}).items():
                if isinstance(entry, dict) and str(entry.get("text") or "").strip():
                    summaries.append(
                        {
                            "scope": scope,
                            "key": key,
                            "turn": entry.get("turn"),
                            "text": str(entry.get("text") or "")[:600],
                        }
                    )
    except Exception:  # noqa: BLE001 - 摘要缺失时用日志兜底
        summaries = []
    summaries.sort(key=lambda item: int(item.get("turn") or 0))
    keyframes: list[dict[str, Any]] = []
    try:
        from trpg_server.agents.narrative.state_store import load_keyframes

        for frame in load_keyframes(directory)[-3:]:
            if isinstance(frame, dict):
                keyframes.append(
                    {
                        "turn": frame.get("turn"),
                        "location": frame.get("location_id"),
                        "reason": frame.get("reason"),
                        "created_at": frame.get("created_at"),
                    }
                )
    except Exception:  # noqa: BLE001
        keyframes = []
    events: list[dict[str, Any]] = []
    try:
        for entry in load_room_log(directory, limit=15):
            if isinstance(entry, dict):
                events.append(
                    {
                        "ts": entry.get("ts"),
                        "message": str(entry.get("message") or "")[:200],
                        "target": entry.get("target"),
                    }
                )
    except Exception:  # noqa: BLE001
        events = []
    return {
        "active": state["active"],
        "startedAt": state.get("startedAt"),
        "startedBy": state.get("startedBy"),
        "settlement": {
            "settled_at": settlement.get("settled_at"),
            "members": settlement.get("members") or [],
            "credit": settlement.get("credit") or [],
        },
        "summaries": summaries[-6:],
        "keyframes": keyframes,
        "recent_events": events,
        "reviews": [row for row in (state.get("reviews") or [])[-3:] if isinstance(row, dict)],
    }


def system_directive() -> str:
    """幕间模式下追加到动态系统指令的硬约束。"""
    return (
        "【幕间模式·最高优先级】当前房间已进入幕间，剧情已经结束。"
        "禁止推进剧情、禁止调用场景/触发器/战斗/剧情工具、禁止回溯存档节点；"
        "不得创造《幕间复盘材料》中未出现的剧情事实，也不要重新阅读全量聊天记录。"
        "只做复盘、点评、结语与规则性交流；玩家购买物品等非剧情行为可以正常处理。"
        "如给出一段可留存的复盘，调用 room.save_interlude_review 保存。"
    )


def _log(
    room_dir: Any,
    info: dict[str, Any],
    message: str,
    *,
    actor: Any = None,
    actor_id: Any = None,
    source: str = "interlude",
    reason: str = "",
) -> None:
    try:
        log_room_event(
            room_dir,
            f"【幕间】{message}",
            actor=actor,
            room_id=info.get("id"),
            logger_=None,
            source=source,
            user_id=actor_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不应阻断幕间操作
        pass


def _format_state(info: dict[str, Any]) -> str:
    state = interlude_state(info)
    lines = [
        f"幕间：{'进行中' if state['active'] else '未进行'}",
        f"开始时间：{state.get('startedAt') or '-'}",
        f"发起人：{state.get('startedBy') or '-'}",
    ]
    request = state.get("request")
    if isinstance(request, dict):
        lines.append(f"待确认提议：{request.get('by') or '-'}（{request.get('source') or '-'}）：{request.get('reason') or '无说明'}")
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    if settlement:
        members = settlement.get("members") if isinstance(settlement.get("members"), list) else []
        lines.append(f"结算时间：{settlement.get('settled_at') or '-'}，成长结算成员 {len(members)} 人")
        for entry in members:
            growth = entry.get("growth") if isinstance(entry.get("growth"), list) else []
            gains = [row for row in growth if isinstance(row, dict) and row.get("gain")]
            san = entry.get("san_recovery") if isinstance(entry.get("san_recovery"), list) else []
            parts = []
            if gains:
                parts.append("成长：" + "、".join(f"{row.get('skill')}+{row.get('gain')}" for row in gains))
            if san:
                parts.append("SAN恢复：" + "、".join(f"{row.get('skill')}+{row.get('applied', row.get('san'))}" for row in san))
            lines.append(f"  {entry.get('player') or '-'}：" + ("；".join(parts) if parts else "无变化"))
    lines.append(f"复盘记录：{len(state.get('reviews') or [])} 条")
    return "\n".join(lines)


def _parse_flags(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    positionals: list[str] = []
    flags: set[str] = set()
    options: dict[str, str] = {}
    for token in args:
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            options[key[2:].lower()] = value
        elif token.startswith("-"):
            flags.add(token.lower())
        else:
            positionals.append(token)
    return positionals, flags, options


def _parse_credit_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for part in str(raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        player, _, event = item.partition(":")
        if player and event:
            events.append({"player": player.strip(), "event": event.strip()})
    return events


def handle_console_command(
    args: list[str],
    *,
    room_id: Any,
    room_dir: Any,
    rooms_dir: Any = None,
    app: Any = None,
    actor: Any = None,
    actor_id: Any = None,
    actor_rank: Any = None,
    confirm: Callable[[str], bool] | None = None,
    rng: Callable[[int], int] | None = None,
) -> tuple[bool, str]:
    """房间控制台 ``interlude`` 域。"""
    positionals, flags, options = _parse_flags(args)
    sub = (positionals[0] if positionals else "status").lower()
    info = room_rules.load_room_info(room_dir)
    if sub == "status":
        return True, _format_state(info)
    if sub not in {"request", "start", "settle", "review", "end"}:
        return False, "用法：interlude [status|request|start|settle|review|end]"
    if room_rules._actor_rank_value(actor_rank) < OPERATOR_RANK:
        return False, "只有房主 / 房间管理员可以执行该幕间命令"

    if sub == "request":
        try:
            request = request_interlude(
                room_dir,
                info,
                actor=actor,
                actor_id=actor_id,
                source="console",
                reason=" ".join(positionals[1:]),
            )
        except InterludeError as exc:
            return False, exc.message
        return True, f"已登记幕间提议：{request.get('by') or '-'}（等待房主 / 管理员确认）"

    if sub == "start":
        credit_events = _parse_credit_events(options.get("credit", ""))
        try:
            outcome = start_interlude(
                room_dir,
                info,
                actor=actor,
                actor_id=actor_id,
                rng=rng,
                credit_events=credit_events,
            )
        except InterludeError as exc:
            return False, exc.message
        return True, (
            "已进入幕间并完成结算。\n"
            "在聊天框发送任意一条消息，KP 会给出点评与结语（控制台无法直接触发 AI 回合）。\n"
            + _format_state(info)
        )

    if sub == "settle":
        credit_events = _parse_credit_events(options.get("credit", ""))
        try:
            settle_interlude(
                room_dir,
                info,
                actor=actor,
                actor_id=actor_id,
                rng=rng,
                credit_events=credit_events,
                include_growth=True,
            )
        except InterludeError as exc:
            return False, exc.message
        return True, "幕间结算完成。\n" + _format_state(info)

    if sub == "review":
        content = options.get("text") or " ".join(positionals[1:])
        try:
            record = add_review(
                room_dir,
                info,
                content=content,
                topic=str(options.get("topic") or ""),
                actor=actor,
                actor_id=actor_id,
                source="console",
            )
        except InterludeError as exc:
            return False, exc.message
        return True, f"已保存复盘记录（{record['id']}）。"

    # end：必须二次确认，随后自动归档。
    if not (flags & {"--yes", "-y"}):
        if confirm is not None and confirm("结束幕间并自动归档？(y/N)"):
            pass
        else:
            return True, "结束幕间会立即归档房间。确认后请输入：interlude end --yes"
    try:
        end_interlude(room_dir, info, actor=actor, actor_id=actor_id)
    except InterludeError as exc:
        return False, exc.message
    if rooms_dir is None:
        return True, "幕间已结束；未提供房间目录，归档需在网页端完成。"
    try:
        from trpg_server import room_admin

        archive = room_admin.archive_room(rooms_dir, room_id, app=app)
    except Exception as exc:  # noqa: BLE001 - 幕间状态已结束，归档失败需明确报告
        return False, f"幕间已结束，但自动归档失败：{exc}"
    return True, f"幕间已结束并完成归档（归档时间 {archive.get('archived_at') or '-'}）。"


__all__ = [
    "INTERLUDE_BLOCKED_TOOLS",
    "INTERLUDE_KEY",
    "InterludeError",
    "add_review",
    "end_interlude",
    "handle_console_command",
    "interlude_state",
    "is_active",
    "record_growth_mark",
    "request_interlude",
    "review_context",
    "settle_interlude",
    "snapshot",
    "start_interlude",
    "system_directive",
]
