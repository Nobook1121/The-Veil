"""房间级「幕间」状态与结算。

幕间是显式房间阶段：
- 只有房主 / 房间管理员 / 系统管理员可以把房间切入幕间；AI 与玩家只能提议。
- 幕间期间不推进剧情、不允许回溯存档节点；允许自由发言、复盘与购买物品。
- 进入幕间时一次性结算成长检定、90%+ 技能 SAN 恢复与可选信用评级变动。
- 结束幕间必须二次确认，随后走现有归档链路。

旧房间没有 ``interlude`` 字段时一律按「非幕间」处理，不写回、不改变行为。
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server import room_clock, room_magic, room_rules, room_sanity
from trpg_server.console_args import ConsoleArgSpec, first_positional, parse_console_args
from trpg_server.json_store import read_json
from trpg_server.room_log import load_room_log, log_room_event
from trpg_server.ruleset.coc7 import treatment

INTERLUDE_KEY = "interlude"
MAX_REVIEWS = 20
MAX_MARKERS = 50
MAX_CREDIT_BATCHES = 20

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


def interlude_opening_prompt() -> str:
    """进入幕间后触发 AI KP 开场点评的固定提示词。

    两个入口（聊天命令与控制台命令）共用同一段提示，保证表现一致。内容来源限定为
    已落盘的摘要 / 关键节点 / 房间日志，避免把全量聊天塞进上下文。
    """
    return (
        "（系统）房间已进入幕间，剧情暂停推进。请以当前 KP / 角色口吻，先对玩家这次的表现"
        "做一段轻松自然的点评，再简短回顾这次的剧情：你们解决了什么、发生了什么、留下了"
        "什么后果。只能依据房间摘要、已落盘的关键节点与房间日志，不要编造没有记录的事，"
        "也不要复述全量聊天。语气像平常说话，不要写成报告、条目或格式化清单，不要脱离角色。"
        "说完这段开场白后停下，等待玩家提问；之后只做复盘与回答玩家对剧情的提问，不要推进剧情。"
    )


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
        "startedGameTime": state.get("startedGameTime"),
        "endedGameTime": state.get("endedGameTime"),
        "gameTime": (room_clock.read_stored_clock(info) or {}).get("current"),
        "care": settlement.get("care"),
        "therapy": settlement.get("therapy"),
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


def _credit_event_key(entry: dict[str, Any]) -> str:
    """为单条信用事件生成稳定的幂等键。

    优先使用调用方显式提供的 ``id``；否则用参与结算的字段做规范化哈希，保证同一
    批次重试（例如 HTTP 重试、控制台二次执行）得到相同的键。
    """
    explicit = str(entry.get("id") or "").strip()
    if explicit:
        return f"id:{explicit}"
    payload = {
        "player": str(entry.get("player") or "").strip(),
        "event": str(entry.get("event") or "").strip().lower(),
        "era": entry.get("era"),
        "target_assets": entry.get("target_assets"),
        "has_social_security": bool(entry.get("has_social_security")),
    }
    return "evt:" + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _credit_event_keys(events: list[dict[str, Any]]) -> list[str]:
    """按出现顺序生成事件键；同批次重复事件追加序号，保持可重复计算。"""
    seen: dict[str, int] = {}
    keys: list[str] = []
    for entry in events:
        base = _credit_event_key(entry)
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def _credit_batch_fingerprint(keys: list[str]) -> str:
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def _apply_credit_events(
    room_dir: Any,
    info: dict[str, Any],
    state: dict[str, Any],
    events: list[dict[str, Any]] | None,
    *,
    rng: Callable[[int], int],
    actor: Any,
    actor_id: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """结算一批信用事件，返回 ``(本次结果, 被跳过的重复事件键)``。

    已应用的事件键保存在 ``settlement["applied_credit"]``，历史批次保存在
    ``settlement["credit_batches"]``。同一批次重试只处理尚未应用的事件，全部重复
    时直接跳过，避免重复加钱，同时保留上一次的结算明细。
    """
    from trpg_server.ruleset.coc7 import economy

    entries = [entry for entry in (events or []) if isinstance(entry, dict)]
    keys = _credit_event_keys(entries)
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    state["settlement"] = settlement
    applied = [str(key) for key in (settlement.get("applied_credit") or []) if key]
    applied_set = set(applied)
    pending = [(key, entry) for key, entry in zip(keys, entries) if key not in applied_set]
    skipped = [key for key in keys if key in applied_set]
    if not pending:
        return [], skipped

    results: list[dict[str, Any]] = []
    new_keys: list[str] = []
    for key, entry in pending:
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
                name=str(row.get("name") or "信用评级"),
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
        settlement.setdefault("credit", []).append(result)
        applied.append(key)
        applied_set.add(key)
        new_keys.append(key)

    # 只记录实际应用的键与批次；已应用事件重试时不会再进入上面的循环。
    settlement["applied_credit"] = applied[-500:]
    if new_keys:
        batches = settlement.get("credit_batches")
        if not isinstance(batches, list):
            batches = []
        batches.insert(
            0,
            {
                "id": _new_id("itl-credit"),
                "at": _now(),
                "by": str(actor or ""),
                "fingerprint": _credit_batch_fingerprint(keys),
                "applied": list(new_keys),
                "results": [dict(row) for row in results],
            },
        )
        del batches[MAX_CREDIT_BATCHES:]
        settlement["credit_batches"] = batches
    room_rules.save_room_info(room_dir, info)
    return results, skipped


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
    rng = rng or _default_rng
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
            growth = room_rules._growth_capability(card)
            marks = growth.read_marks(card, member_state.get("growth_marks")) if growth is not None else []
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
                    _clear_growth_marks(card)
                    _append_growth_history(card, rows, player=entry["player"])
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
        _, skipped = _apply_credit_events(
            room_dir,
            info,
            state,
            credit_events,
            rng=rng,
            actor=actor,
            actor_id=actor_id,
        )
        if skipped:
            _log(
                room_dir,
                info,
                f"信用事件批次已结算，跳过 {len(skipped)} 条重复事件",
                actor=actor,
                actor_id=actor_id,
                source="interlude",
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
    clock = room_clock.read_clock(info)
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
            "startedGameTime": clock["current"] if clock else None,
        }
    )
    # 习惯恐惧按幕间衰减一次（规则开关关闭时内部直接跳过）。
    room_sanity.interlude_decay(room_dir, info)
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
            "endedGameTime": (room_clock.read_clock(info) or {}).get("current"),
        }
    )
    info[INTERLUDE_KEY] = state
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, "结束幕间", actor=actor, actor_id=actor_id, source="interlude")
    return snapshot(info)


def apply_monthly_care(
    room_dir: Any,
    info: dict[str, Any],
    *,
    months: int | None = None,
    actor: Any = None,
    actor_id: Any = None,
) -> dict[str, Any]:
    """幕间疗养：按月结算所有活跃成员的 HP 自然恢复与 MP 恢复。

    月份显式给出时优先使用；否则读「当前游戏时间 − 上次疗养/幕间开始时间」的
    整月数，没有起点时按 0 处理（不凭空补时间）。
    """
    if not room_clock.is_enabled(info):
        raise InterludeError(
            "幕间疗养需要先开启游戏时钟（房规 coc7_game_clock）", code="clock_disabled"
        )
    state = _ensure_state(info)
    if not state["active"]:
        raise InterludeError("房间当前不在幕间", code="not_active", status=409)
    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    state["settlement"] = settlement
    clock = room_clock.read_clock(info)
    explicit = months is not None
    if months is None:
        marker = settlement.get("careGameTime") or state.get("startedGameTime")
        days = room_clock.clock_days_since(info, marker)
        months = treatment.months_from_days(days) if days is not None else 0
    months = max(0, int(months))
    if months <= 0:
        if clock is not None:
            settlement["careGameTime"] = clock["current"]
        room_rules.save_room_info(room_dir, info)
        return {"months": 0, "days": 0, "members": []}

    days = treatment.recuperation_days(months)
    rows: list[dict[str, Any]] = []
    for member in _active_members(info):
        name = room_rules.member_display_name(member)
        try:
            hp = room_rules.apply_natural_healing(
                room_dir,
                info,
                member,
                days=days,
                reason=f"幕间疗养 {months} 个月",
                source="interlude",
            )
            mp = room_magic.recover_mp(room_dir, info, member, days=days)
            rows.append(
                {
                    "player": name,
                    "hp": {"before": hp["before"], "after": hp["after"], "max": hp["max_hp"]},
                    "mp": {"before": mp["before"], "after": mp["after"], "max": mp["max_mp"]},
                }
            )
        except room_rules.RuleOperationError as exc:
            rows.append({"player": name, "error": exc.message})
    care = {
        "months": months,
        "days": days,
        "explicit": explicit,
        "game_time": clock["current"] if clock else None,
        "members": rows,
        "applied_at": _now(),
    }
    settlement["care"] = care
    if clock is not None:
        settlement["careGameTime"] = clock["current"]
    room_rules.save_room_info(room_dir, info)
    _log(room_dir, info, f"幕间疗养结算 {months} 个月", actor=actor, actor_id=actor_id, source="interlude")
    return care


def apply_therapy(
    room_dir: Any,
    info: dict[str, Any],
    *,
    player: Any,
    months: int | None = None,
    results: list[Any] | None = None,
    indefinite: bool = False,
    d6: Any = None,
    actor: Any = None,
    actor_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """心理治疗：按月结算 SAN（每月一次精神分析检定结果由 KP 给出）。

    本函数只做时间与结算编排，**不会**清除不定性疯狂：SAN 模块目前没有自动治愈
    不定性疯狂的链路，需要 KP 显式裁定并记录。``indefinite=True`` 时用 1D6 个月
    作为本轮治疗周期。
    """
    if not room_clock.is_enabled(info):
        raise InterludeError(
            "心理治疗需要先开启游戏时钟（房规 coc7_game_clock）", code="clock_disabled"
        )
    state = _ensure_state(info)
    if not state["active"]:
        raise InterludeError("房间当前不在幕间", code="not_active", status=409)
    rng = rng or _default_rng
    try:
        member = room_rules.resolve_member(
            info, str(player or ""), actor_id=actor_id, actor_rank=OPERATOR_RANK
        )
        room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        raise InterludeError(exc.message, code="invalid_member") from exc
    name = room_rules.member_display_name(member)
    member_state = member.setdefault("character_state", {})
    if not isinstance(member_state, dict):
        member_state = {}
        member["character_state"] = member_state
    clock = room_clock.read_clock(info)

    if indefinite:
        months = treatment.indefinite_months(d6 if d6 is not None else rng(6))
    elif months is None:
        days = room_clock.clock_days_since(info, member_state.get("last_therapy"))
        months = treatment.months_from_days(days) if days is not None else None
    if months is None:
        raise InterludeError("游戏时钟还没有该成员的治疗起点；请显式给出月数。", code="no_start")
    months = max(0, int(months))
    levels = [str(item).strip() for item in (results or []) if str(item).strip()]
    if months <= 0:
        return {"player": name, "months": 0, "indefinite": indefinite, "san_delta": 0}
    if len(levels) != months:
        raise InterludeError(
            f"请为 {months} 个月各给出一个精神分析结果（regular/hard/extreme/critical/failure/fumble）。",
            code="invalid_results",
        )

    settlement = state.get("settlement") if isinstance(state.get("settlement"), dict) else {}
    state["settlement"] = settlement
    rows: list[dict[str, Any]] = []
    total = 0
    for index, level in enumerate(levels, start=1):
        delta = treatment.monthly_san_delta(level, d3=rng(3), d6=rng(6))
        try:
            applied = room_rules.apply_san_change(
                room_dir,
                info,
                member,
                delta=delta,
                reason=f"心理治疗第 {index} 月（{level}）",
                actor=actor,
                actor_id=actor_id,
                source="therapy",
            )
        except room_rules.RuleOperationError as exc:
            rows.append({"month": index, "level": level, "error": exc.message})
            continue
        applied_delta = int(applied.get("delta", delta))
        total += applied_delta
        rows.append({"month": index, "level": level, "san_delta": applied_delta})
    if clock is not None:
        member_state["last_therapy"] = clock["current"]
    until = None
    if clock is not None:
        current = room_clock.parse_moment(clock["current"])
        if current is not None:
            from datetime import timedelta

            until = room_clock.format_moment(current + timedelta(days=months * treatment.MONTH_DAYS))
    record = {
        "player": name,
        "months": months,
        "indefinite": indefinite,
        "san_delta": total,
        "months_detail": rows,
        "game_time": clock["current"] if clock else None,
        "until": until,
        "at": _now(),
    }
    therapy = settlement.get("therapy") if isinstance(settlement.get("therapy"), list) else []
    therapy.append(record)
    settlement["therapy"] = therapy[-20:]
    room_rules.save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"心理治疗结算：{name} {months} 个月，SAN {total:+d}",
        actor=actor,
        actor_id=actor_id,
        source="interlude",
    )
    return record


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


def _default_rng(sides: int) -> int:
    """默认骰子函数：接受面数（与注入的 rng(sides) 同签名）。"""
    return random.randint(1, sides)


def _growth_block(card: dict[str, Any]) -> dict[str, Any]:
    """取（并按需创建）角色卡 extensions.growth。"""
    extensions = card.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        card["extensions"] = extensions
    block = extensions.get("growth")
    if not isinstance(block, dict):
        block = {}
        extensions["growth"] = block
    return block


def _clear_growth_marks(card: dict[str, Any]) -> None:
    _growth_block(card)["marks"] = []


def _append_growth_history(
    card: dict[str, Any], rows: list[dict[str, Any]], *, player: str = ""
) -> None:
    """幕间结算后把成长结果追加到 extensions.growth.history（保留最近 20 条）。"""
    if not rows:
        return
    block = _growth_block(card)
    history = block.get("history") if isinstance(block.get("history"), list) else []
    for row in rows:
        history.insert(0, {**row, "player": player, "at": _now(), "source": "interlude"})
    del history[20:]
    block["history"] = history


def _training_block(card: dict[str, Any]) -> dict[str, Any]:
    extensions = card.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        card["extensions"] = extensions
    block = extensions.get("training")
    if not isinstance(block, dict):
        block = {}
        extensions["training"] = block
    return block


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
    # 批次 2：成长标记同时写入角色卡 extensions.growth.marks，归档时随卡保留；
    # 旧位置（成员快照）继续维护，读侧优先新位置、回退旧位置。
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
    if card is not None:
        extensions = card.get("extensions")
        if not isinstance(extensions, dict):
            extensions = {}
            card["extensions"] = extensions
        block = extensions.get("growth")
        if not isinstance(block, dict):
            block = {}
            extensions["growth"] = block
        card_marks = block.get("marks")
        if not isinstance(card_marks, list):
            card_marks = []
        card_marks.insert(0, {**record, "eligible": True, "reason": "check_success"})
        del card_marks[MAX_MARKERS:]
        block["marks"] = card_marks
    room_rules.save_room_info(room_dir, info)
    return record


def train_skill(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    skill: str,
    weeks: Any,
    actor: Any = None,
    actor_id: Any = None,
    reason: str = "",
    rng: Any = None,
) -> dict[str, Any]:
    """幕间训练（可选房规）：KP 批准投入周数，按 WEEKS_PER_GAIN 掷成长检定。

    记录写入 ``extensions.training.records``；技能变化走 ``apply_skill_change``，
    与幕间成长共用同一条写回 / 留痕链路。
    """
    from trpg_server.ruleset.coc7.options import resolve_options

    if not resolve_options(info).growth_training:
        raise InterludeError(
            "幕间训练房规未开启（coc7_growth_training）", code="training_disabled", status=403
        )
    state = _ensure_state(info)
    if not state["active"]:
        raise InterludeError("房间尚未进入幕间", code="not_active", status=409)
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
    if card is None:
        raise InterludeError("该成员还没有绑定角色卡", code="no_card", status=409)
    training = room_rules._training_capability(card)
    if training is None:
        raise InterludeError("该角色卡的规则集不支持幕间训练", code="unsupported", status=409)
    name = str(skill or "").strip()
    row = room_rules._find_skill_row(card, name)
    if row is None:
        raise InterludeError(f"角色卡上没有技能：{name}", code="unknown_skill", status=404)
    rng = rng or _default_rng
    result = training.train_skill(row.get("value"), weeks=weeks, rng=rng)
    record = {
        "id": _new_id("itl-train"),
        "skill": str(row.get("name") or name),
        "weeks": result["weeks"],
        "unit": getattr(training, "TIME_UNIT", "week"),
        "before": result["before"],
        "after": result["after"],
        "gain": result["gain"],
        "checks": result["checks"],
        "at": _now(),
        "actor": str(actor or ""),
        "reason": str(reason or ""),
    }
    if result["gain"]:
        room_rules.apply_skill_change(
            room_dir,
            info,
            member,
            name=str(row.get("name") or name),
            value=result["after"],
            reason=reason or f"幕间训练 {result['weeks']} 周",
            actor=actor,
            actor_id=actor_id,
            source="interlude",
        )
    block = _training_block(card)
    records = block.get("records") if isinstance(block.get("records"), list) else []
    records.insert(0, record)
    del records[max(1, int(getattr(training, "MAX_RECORDS", 12))):]
    block["records"] = records
    room_rules.save_room_info(room_dir, info)
    return {"record": record, "result": result}


def experience_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    skill: str,
    actor: Any = None,
    actor_id: Any = None,
    rng: Any = None,
) -> dict[str, Any]:
    """对单个已打勾技能立即结算成长检定（批次 2 薄入口）。

    正常流程由 :func:`settle_interlude` 一次性结算全部标记；本函数供 KP 手动逐项确认，
    跳过 ``settle`` 的批量口径，但复用同一 ``growth.growth_check`` 与写回链路。
    """
    if not is_active(info):
        raise InterludeError(
            "剧情进行中不结算成长检定，请进入幕间后再试", code="not_active", status=409
        )
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else None
    if card is None:
        raise InterludeError("该成员还没有绑定角色卡", code="no_card", status=409)
    growth = room_rules._growth_capability(card)
    if growth is None:
        raise InterludeError("该角色卡的规则集不支持成长检定", code="unsupported", status=409)
    name = str(skill or "").strip()
    if not name:
        raise InterludeError("请提供技能名", code="bad_arguments", status=400)
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    marks = growth.read_marks(card, state.get("growth_marks"))
    key = name.casefold()
    mark = next(
        (
            row
            for row in marks
            if isinstance(row, dict)
            and str(row.get("skill") or row.get("name") or "").strip().casefold() == key
        ),
        None,
    )
    if mark is None:
        raise InterludeError(f"技能「{name}」没有成长标记", code="no_mark", status=404)
    row = room_rules._find_skill_row(card, name)
    if row is None:
        raise InterludeError(f"角色卡上没有技能：{name}", code="unknown_skill", status=404)
    rng = rng or _default_rng
    outcome = growth.growth_check(row.get("value"), roll=rng(100), gain_roll=rng(10))
    result = {
        "skill": str(row.get("name") or name),
        "value": row.get("value"),
        **outcome,
        "mark_id": mark.get("id"),
    }
    if outcome["gain"]:
        room_rules.apply_skill_change(
            room_dir,
            info,
            member,
            name=result["skill"],
            value=result["after"],
            reason="幕间成长检定",
            actor=actor,
            actor_id=actor_id,
            source="interlude",
        )
    remaining = [row for row in marks if row is not mark and row.get("id") != mark.get("id")]
    _growth_block(card)["marks"] = remaining
    state["growth_marks"] = [
        row
        for row in (state.get("growth_marks") or [])
        if isinstance(row, dict) and row.get("id") != mark.get("id")
    ]
    _append_growth_history(card, [result], player=room_rules.member_display_name(member))
    room_rules.save_room_info(room_dir, info)
    summary = (
        f"{room_rules.member_display_name(member)} 成长检定「{result['skill']}」："
        f"{result['value']}→{result['after']}"
    )
    return {"record": result, "mark": mark, "summary": summary}


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
    clock = room_clock.read_clock(info)
    if clock is not None:
        lines.append(f"游戏时间：{room_clock.format_display(clock['current'])}")
        if settlement.get("careGameTime"):
            lines.append(f"上次疗养：{room_clock.format_display(settlement['careGameTime'])}")
    care = settlement.get("care") if isinstance(settlement.get("care"), dict) else None
    if care:
        lines.append(f"最近疗养：{care.get('months')} 个月（{care.get('days')} 天）")
    therapy = settlement.get("therapy") if isinstance(settlement.get("therapy"), list) else []
    if therapy:
        last = therapy[-1] if isinstance(therapy[-1], dict) else {}
        lines.append(
            f"最近心理治疗：{last.get('player') or '-'} {last.get('months')} 个月，"
            f"SAN {int(last.get('san_delta') or 0):+d}"
        )
    return "\n".join(lines)


INTERLUDE_ARG_SPECS = {
    "start": ConsoleArgSpec.of(values={"credit"}),
    "settle": ConsoleArgSpec.of(values={"credit"}),
    "review": ConsoleArgSpec.of(values={"text"}, trailing={"topic"}),
    "care": ConsoleArgSpec.of(values={"months"}),
    "therapy": ConsoleArgSpec.of(values={"months", "results", "d6"}),
    "end": ConsoleArgSpec.of(flags={"yes"}),
}


def _parse_flags(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    spec = INTERLUDE_ARG_SPECS.get(first_positional(args).lower(), ConsoleArgSpec.of())
    parsed = parse_console_args(args, spec)
    return parsed.positionals, parsed.flags, parsed.options


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
    if sub not in {"request", "start", "settle", "review", "care", "therapy", "end"}:
        return False, "用法：interlude [status|request|start|settle|review|care|therapy|end]"
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
            "已进入幕间并完成结算，KP 正在准备点评与结语。\n"
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

    if sub == "care":
        raw = options.get("months") or (positionals[1] if len(positionals) > 1 else "")
        months: int | None = None
        if str(raw).strip():
            try:
                months = int(raw)
            except (TypeError, ValueError):
                return False, "月数必须是正整数"
        try:
            care = apply_monthly_care(
                room_dir, info, months=months, actor=actor, actor_id=actor_id
            )
        except InterludeError as exc:
            return False, exc.message
        if not care["months"]:
            return True, "游戏时间距上次疗养不足 1 个月，未结算；可显式给出月数。"
        lines = [f"幕间疗养结算：{care['months']} 个月（{care['days']} 天）"]
        for row in care["members"]:
            if row.get("error"):
                lines.append(f"  {row['player']}：{row['error']}")
                continue
            lines.append(
                f"  {row['player']}：HP {row['hp']['before']}→{row['hp']['after']}/{row['hp']['max']}，"
                f"MP {row['mp']['before']}→{row['mp']['after']}/{row['mp']['max']}"
            )
        return True, "\n".join(lines)

    if sub == "therapy":
        if len(positionals) < 3:
            return False, (
                "用法：interlude therapy <玩家> <月数|indefinite> <每月精神分析结果...> "
                "（结果用逗号或空格分隔，例如 regular,failure,fumble）"
            )
        months_text = str(options.get("months") or positionals[2]).strip()
        indefinite = months_text.lower() in {"indefinite", "1d6", "不定性", "不定性疯狂"}
        months = None
        if months_text and not indefinite:
            try:
                months = int(months_text)
            except (TypeError, ValueError):
                return False, "月数必须是正整数，或写 indefinite 让系统掷 1D6 个月"
        raw_results = options.get("results") or " ".join(positionals[3:])
        levels = [item for item in raw_results.replace(",", " ").split() if item]
        d6_value: Any = options.get("d6")
        if d6_value not in (None, ""):
            try:
                d6_value = int(d6_value)
            except (TypeError, ValueError):
                return False, "d6 必须是 1-6 的整数"
        try:
            record = apply_therapy(
                room_dir,
                info,
                player=positionals[1],
                months=months,
                results=levels,
                indefinite=indefinite,
                d6=d6_value,
                actor=actor,
                actor_id=actor_id,
                rng=rng,
            )
        except InterludeError as exc:
            return False, exc.message
        detail = "（不定性周期）" if record.get("indefinite") else ""
        message = (
            f"心理治疗结算{detail}：{record['player']} {record['months']} 个月，"
            f"SAN {int(record.get('san_delta') or 0):+d}"
        )
        if record.get("until"):
            message += f"；本轮周期至 {room_clock.format_display(record['until'])}"
        return True, message

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
            return True, "结束幕间会立即归档房间。确认后请输入：interlude end yes"
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
