"""批次 3：房间理智状态层（状态存取与调度，规则数学在 ruleset/coc7）。

这里不做规则计算，只负责：
- 把插件给出的结构化精神状态写入 ``character_state["mental"]`` 与
  ``character_card["extensions"]["mental"]``，并登记 ``extensions`` 持久化变更；
- 提供控制台 / AI 工具共用的查询、发作结束、习惯恐惧、现实认知与查表入口；
- 给房间时钟留幕间接口 :func:`interlude_decay`（心理治疗另由幕间治疗命令编排）。

计时口径：发作 / 临时性疯狂的开始、到期与免疫判断统一走
:func:`room_clock.san_now`（房规游戏时钟优先，未启用时显式回退墙钟），
不要在这些函数里单独取 ``time.time()``。

所有新行为都受房规开关控制，默认关闭时这些函数直接拒绝执行。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable

from trpg_server import room_clock, room_rules
from trpg_server.ruleset import get_plugin
from trpg_server.ruleset.capabilities import checks_of, sanity_of
from trpg_server.ruleset.coc7 import sanity_state, traits, treatment
from trpg_server.ruleset.coc7.options import resolve_options
from trpg_server.ruleset.contracts import CheckRequest

SAN_COMMAND_SUBS = {"bout", "habituation", "habits", "reality", "trait", "traits", "treat"}
FEATURE_LABELS = {
    "sanity_state": "理智结构化",
    "sanity_habituation": "习惯恐惧",
    "sanity_reality": "现实认知",
    "sanity_bout": "发作计时",
    "sanity_traits": "恐惧 / 躁狂表",
    "sanity_mythos": "目击神话",
}


def options_for(info: dict[str, Any] | None) -> Any:
    return resolve_options(info)


def _sanity_for(member: dict[str, Any]) -> Any:
    card = room_rules.member_card(member)
    plugin = None
    try:
        from trpg_server.ruleset import get_plugin

        plugin = get_plugin(card.get("rulesetId"))
    except Exception:  # noqa: BLE001 - 插件缺失按无能力处理
        plugin = None
    return sanity_of(plugin)


def _require(member: dict[str, Any]) -> Any:
    sanity = _sanity_for(member)
    if sanity is None:
        raise room_rules.RuleOperationError("当前规则集不支持理智状态", code="mental_unsupported")
    return sanity


def _state(member: dict[str, Any]) -> dict[str, Any]:
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    return state


def _disabled(feature: str) -> "room_rules.RuleOperationError":
    return room_rules.RuleOperationError(
        f"房间未启用「{FEATURE_LABELS.get(feature, feature)}」房规（开关：coc7_{feature}）",
        code="rule_disabled",
    )


def _pending(block: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "type": "extensions",
        "value": {"mental": dict(block)},
        "source": "rule",
        "reason": reason,
    }


def mental_block(member: dict[str, Any]) -> dict[str, Any]:
    """取（必要时创建）结构化精神状态；不检查开关，供已判定开启的调用方使用。"""
    sanity = _require(member)
    return sanity.mental_block(room_rules.member_card(member), _state(member))


def mental_snapshot(member: dict[str, Any], info: dict[str, Any] | None = None) -> dict[str, Any]:
    sanity = _require(member)
    return sanity.mental_snapshot(room_rules.member_card(member), _state(member), info)


def describe(member: dict[str, Any], info: dict[str, Any] | None = None) -> str:
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    now_ts, _, _ = room_clock.san_now(info)
    return sanity_state.describe(
        mental_snapshot(member, info),
        timestamp=now_ts,
        safety_minutes=options.sanity_bout_safety_minutes,
    )


def _member_label(member: dict[str, Any]) -> str:
    return room_rules.member_display_name(member)


def _save(room_dir: Any, info: dict[str, Any]) -> None:
    room_rules.save_room_info(room_dir, info)


# ---------------------------------------------------------------- 发作


def end_bout(room_dir: Any, info: dict[str, Any], member: dict[str, Any], *, actor: str = "", actor_id: str = "") -> dict[str, Any]:
    """提前结束当前发作（治疗、KP 裁定或时间到）。"""
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    block = mental_block(member)
    bout = sanity_state.clear_bout(block)
    if not bout:
        return {"player_name": _member_label(member), "bout": {}, "summary": "当前没有进行中的发作。"}
    state = _state(member)
    room_rules._add_pending_change(state, _pending(block, "结束发作"))
    _save(room_dir, info)
    summary = f"{_member_label(member)} 的发作「{bout.get('name')}」已结束（{bout.get('ended_at')}）。"
    return {"player_name": _member_label(member), "bout": bout, "mental": block, "summary": summary}


def active_bout_text(member: dict[str, Any], info: dict[str, Any] | None = None) -> str:
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    block = mental_snapshot(member, info)
    bout = block.get("bout") if isinstance(block.get("bout"), dict) else {}
    if not bout:
        return f"{_member_label(member)}：无疯狂发作记录。"
    # 插件快照已按房规算出 activeBout（含安全阀为 0 时关闭兜底的口径）。
    status = "进行中" if block.get("activeBout") else "已结束"
    return f"{_member_label(member)}：{status}；{sanity_state.bout_summary(bout)}"


# ---------------------------------------------------------------- 习惯恐惧


def record_habituation(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    source: str,
    loss: int,
    cap: int | None = None,
    actor: str = "",
    actor_id: str = "",
) -> dict[str, Any]:
    """记录一次来源造成的理智损失；返回值含实际损失与是否免疫。

    ``cap`` 是该神话存在单次遭遇可能造成的最大损失（如 1D6 → 6）；
    缺省时沿用该来源已记录的上限。
    """
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    if not options.sanity_habituation:
        raise _disabled("sanity_habituation")
    key = " ".join(str(source or "").split())
    if not key:
        raise room_rules.RuleOperationError("请提供恐惧来源名称", code="bad_arguments")
    block = mental_block(member)
    result = sanity_state.note_habituation(block, key, int(loss or 0), max_loss=cap)
    room_rules._add_pending_change(_state(member), _pending(block, "习惯恐惧"))
    _save(room_dir, info)
    total = result["previous_total"] + result["applied"]
    if result["immune"]:
        summary = (
            f"{_member_label(member)} 对「{key}」的累计理智损失已达上限 "
            f"{result['previous_total']}/{result['cap']}，本次不再损失。"
        )
    elif result["applied"] != result["loss"]:
        summary = (
            f"{_member_label(member)} 对「{key}」累计 {result['previous_total']}/{result['cap']}，"
            f"本次损失 {result['loss']} 点中只计入 {result['applied']} 点。"
        )
    else:
        summary = f"{_member_label(member)} 对「{key}」累计理智损失 {total}/{result['cap'] or '?'}。"
    return {"player_name": _member_label(member), "habituation": result, "mental": block, "summary": summary}


def treatment_session(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    level: str = "regular",
    months: int = 1,
    d3: int | None = None,
    d6: int | None = None,
    note: str = "",
    actor: str = "",
    actor_id: str = "",
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """心理治疗（批次 3）：按月掷精神分析结果，SAN 变化走房间写回链路。

    ``level`` 是本月精神分析检定的结果（regular/hard/extreme/failure/fumble）；
    成功 +1D3、大失败 −1D6、其余 0（规则书 p.129）。记录写
    ``extensions.mental.treatment``（保留最近 20 条）。
    """
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    level_text = str(level or "").strip().lower()
    month_count = max(1, min(12, int(months or 1)))
    rng = rng or (lambda sides: random.randint(1, sides))
    block = mental_block(member)
    total = 0
    for _ in range(month_count):
        total += treatment.monthly_san_delta(
            level_text,
            d3=d3 if d3 is not None else rng(3),
            d6=d6 if d6 is not None else rng(6),
        )
    record = sanity_state.record_treatment(
        block,
        level=level_text,
        months=month_count,
        delta=total,
        note=note,
        source="rule",
    )
    applied = None
    if total:
        applied = room_rules.apply_san_change(
            room_dir,
            info,
            member,
            delta=total,
            reason=f"心理治疗（{level_text}，{month_count} 个月）",
            actor=actor,
            actor_id=actor_id,
            source="interlude",
        )
    room_rules._add_pending_change(_state(member), _pending(block, "心理治疗"))
    _save(room_dir, info)
    if total > 0:
        summary = f"{_member_label(member)} 心理治疗（{level_text}，{month_count} 个月）：SAN +{total}。"
    elif total < 0:
        summary = f"{_member_label(member)} 心理治疗失败（{level_text}）：SAN {total}。"
    else:
        summary = f"{_member_label(member)} 心理治疗（{level_text}，{month_count} 个月）：SAN 无变化。"
    return {"player_name": _member_label(member), "treatment": record, "applied": applied, "mental": block, "summary": summary}


def habituation_rows(member: dict[str, Any], info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    block = mental_snapshot(member, info)
    rows = []
    for key, row in (block.get("habituations") or {}).items():
        if isinstance(row, dict):
            rows.append(
                {
                    "source": row.get("name") or key,
                    "total_loss": row.get("total_loss"),
                    "max_loss": row.get("max_loss"),
                    "updated_at": row.get("updated_at"),
                }
            )
    return rows


def interlude_decay(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any] | None = None,
    *,
    steps: int = 1,
    reason: str = "幕间",
) -> dict[str, Any]:
    """幕间习惯恐惧 −1（房间时钟在幕间开始时调用一次）。

    ``member`` 为空时处理房间内所有带角色卡的成员。心理治疗（月度精神分析、
    1D6 月不定性疯狂）不在本函数内，由幕间心理治疗命令单独结算；当前版本
    没有自动治愈不定性疯狂的链路，需要 KP / 治疗流程显式处理。
    """
    options = options_for(info)
    if not options.sanity_state or not options.sanity_habituation:
        return {"members": [], "summary": "未启用习惯恐惧房规，跳过幕间衰减。"}
    targets: Iterable[dict[str, Any]] = [member] if member else [
        item for item in (info.get("members") or []) if isinstance(item, dict)
    ]
    summaries: list[str] = []
    changed = False
    for target in targets:
        try:
            block = mental_block(target)
        except room_rules.RuleOperationError:
            continue
        before = {
            key: int((row or {}).get("total_loss") or 0) if isinstance(row, dict) else 0
            for key, row in (block.get("habituations") or {}).items()
        }
        if not before:
            continue
        removed = sanity_state.decay_habituations(block, steps)
        after = {
            key: int((row or {}).get("total_loss") or 0) if isinstance(row, dict) else 0
            for key, row in (block.get("habituations") or {}).items()
        }
        if after == before:
            continue
        room_rules._add_pending_change(_state(target), _pending(block, reason))
        changed = True
        detail = "、".join(f"{name}→{int(row.get('total_loss') or 0)}" for name, row in block["habituations"].items())
        if removed:
            detail += ("；" if detail else "") + "已消除：" + "、".join(removed)
        summaries.append(f"{_member_label(target)}：{detail or '无有效记录'}")
    if changed:
        _save(room_dir, info)
    return {
        "members": summaries,
        "summary": ("幕间习惯恐惧衰减：" + "；".join(summaries)) if summaries else "没有需要衰减的习惯恐惧记录。",
    }


# ---------------------------------------------------------------- 到期收敛


def expire_bouts(
    room_dir: Any,
    info: dict[str, Any] | None = None,
    *,
    timestamp: float | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """房间时钟调用的收敛入口：到点结束发作与临时性疯狂。幂等。

    - 总结症状的发作（1D10 小时）超过 ``expires_at`` → 收尾并清空 ``bout``；
    - 临时性疯狂（1D10 小时）超过 ``endsAt`` → 清空 ``insanity`` 并移除
      ``temporaryInsanity`` 标记；
    - 即时症状默认由 KP / ``rule san bout end`` 显式结束；只有房规安全阀
      （``coc7_sanity_bout_safety_minutes``，默认 60 分钟）开启时才在超时后兜底收尾，
      设为 0 表示不做超时处理。

    ``now`` 是房间时钟传入的当前时刻（datetime / 数字 / 时间串均可）。所有记录
    先按 :func:`room_clock.san_now` 的同一口径折算（旧墙钟数据会被锚定到当前
    时钟），再做到期判断；``now`` 缺省时使用该口径的当前值，不再单独取墙钟。
    未启用作息或没有到期项时不做任何写入，可被时钟反复调用；返回的 ``messages``
    已整理成可直接写入房间日志的行。
    """
    if info is None:
        info = room_rules.load_room_info(room_dir) or {}
    options = options_for(info)
    if not options.sanity_state:
        return {"expired": [], "insanity_ended": [], "messages": [], "summary": "未启用理智结构化房规，跳过到期检查。"}
    game_now, _, clock_kind = room_clock.san_now(info)
    moment = now if now is not None else timestamp
    if moment is None:
        current = game_now
    elif isinstance(moment, (int, float)) and not isinstance(moment, bool):
        current = float(moment)
    elif hasattr(moment, "timestamp"):
        current = float(moment.timestamp())
    else:
        parsed = sanity_state.parse_ts(moment)
        current = game_now if parsed is None else parsed
    expired: list[dict[str, Any]] = []
    insanity_ended: list[dict[str, Any]] = []
    changed = False
    for member in info.get("members") or []:
        if not isinstance(member, dict) or not member.get("character_card"):
            continue
        try:
            block = mental_block(member)
        except room_rules.RuleOperationError:
            continue
        # 旧数据（没有 clock 标记）按墙钟记录，先折算到当前口径，
        # 避免墙钟开始时间与游戏时钟当前值混用导致永不跳期 / 立刻跳期。
        rebased = sanity_state.rebase_timers(
            block,
            now=current,
            target_clock=clock_kind,
            safety_minutes=options.sanity_bout_safety_minutes,
        )
        bout = sanity_state.expire_bout(
            block, current, safety_minutes=options.sanity_bout_safety_minutes
        )
        insanity = sanity_state.expire_insanity(block, current)
        if not rebased and not bout and not insanity:
            continue
        changed = True
        card = room_rules.member_card(member)
        if insanity:
            insanity_ended.append({"player_name": _member_label(member), "insanity": insanity})
            for target in (card, _state(member)):
                flags = target.get("mental_status")
                if isinstance(flags, dict):
                    flags["temporaryInsanity"] = False
                    flags["temporary_insanity"] = False
                    target["status"] = flags
        if bout:
            expired.append({"player_name": _member_label(member), "bout": bout})
        room_rules._add_pending_change(_state(member), _pending(block, "发作到期"))
    if changed:
        _save(room_dir, info)
    messages = [f"{item['player_name']} 的发作「{item['bout'].get('name')}」到期结束" for item in expired]
    messages += [f"{item['player_name']} 的临时性疯狂已恢复" for item in insanity_ended]
    return {
        "expired": expired,
        "insanity_ended": insanity_ended,
        "messages": messages,
        "summary": ("；".join(messages) + "。") if messages else "没有到期的发作或临时性疯狂。",
    }


def reanchor_bouts(room_dir: Any, info: dict[str, Any] | None, delta_seconds: float) -> dict[str, Any]:
    """游戏时钟被 ``time correct`` 改写后平移活动计时，保留剩余时长。

    只平移 ``clock="game"`` 的发作 / 临时性疯狂：回拨到历史日期后它们仍会按
    新时间线继续走完剩余时长，而不是因为开始时间在未来永远不到期。本函数
    不做到期结算，``time correct`` 的现有语义保持不变。
    """
    if info is None:
        info = room_rules.load_room_info(room_dir) or {}
    options = options_for(info)
    if not options.sanity_state:
        return {"shifted": [], "summary": "未启用理智结构化房规，跳过计时平移。"}
    try:
        delta = int(round(float(delta_seconds or 0)))
    except (TypeError, ValueError):
        delta = 0
    if not delta:
        return {"shifted": [], "summary": "时间没有变化，无需平移计时。"}
    shifted: list[str] = []
    changed = False
    for member in info.get("members") or []:
        if not isinstance(member, dict) or not member.get("character_card"):
            continue
        try:
            block = mental_block(member)
        except room_rules.RuleOperationError:
            continue
        if sanity_state.shift_timers(block, delta):
            room_rules._add_pending_change(_state(member), _pending(block, "时间校正"))
            shifted.append(_member_label(member))
            changed = True
    if changed:
        _save(room_dir, info)
    return {
        "shifted": shifted,
        "summary": (
            f"已平移 {len(shifted)} 名成员的发作计时（{delta:+d} 秒）：" + "、".join(shifted)
            if shifted
            else "没有需要平移的发作计时。"
        ),
    }


# ---------------------------------------------------------------- 现实认知

def _normalize_result(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "critical": "critical",
        "大成功": "critical",
        "extreme": "extreme",
        "极难成功": "extreme",
        "hard": "hard",
        "困难成功": "hard",
        "regular": "regular",
        "普通成功": "regular",
        "success": "regular",
        "成功": "regular",
        "failure": "failure",
        "失败": "failure",
        "fumble": "fumble",
        "大失败": "fumble",
    }
    return mapping.get(text, "")


def reality_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    result: Any = None,
    source: str = "",
    actor: str = "",
    actor_id: str = "",
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """现实认知：用当前 SAN 做理智检定，成功看穿幻象并获得对幻觉的抗性。

    规则书第八章「疯狂的副作用2：幻想与现实的界限」（p.135）：
    “玩家需要投一个理智来进行现实认知检定：失败：失去１点理智值。如果调查员
     当时正处于潜在疯狂的阶段，这会立刻引起调查员的疯狂发作症状……成功：
     调查员将会看穿任何的自身产生的幻觉……并暂时获得对幻觉的抗性，直到调查员
     下一次失去理智值为止。（这也是为了防止守秘人会不停的给予调查员新的幻觉
     来抵消现实认知检定的效果。）”
    """
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    if not options.sanity_reality:
        raise _disabled("sanity_reality")

    level = _normalize_result(result)
    check_result: dict[str, Any] = {}
    if level:
        success = level not in {"failure", "fumble"}
    else:
        # 原文是“投一个理智”，不是 POW；目标值取当前 SAN（上限 100）。
        checks = checks_of(get_plugin(room_rules.resolve_rule_id(info)))
        if checks is None:
            raise room_rules.RuleOperationError("当前规则集不支持检定", code="check_unavailable")
        target = max(1, min(100, int(room_rules.san_snapshot(member)["current"])))
        resolution = checks.roll(
            room_rules.member_card(member),
            CheckRequest(kind="resource", name="SAN", target=target, difficulty="regular"),
            rng or (lambda sides: random.randint(1, sides)),
        )
        level = str(resolution.level or "")
        success = bool(resolution.success)
        check_result = {
            "name": "SAN",
            "roll": int(resolution.roll),
            "target": int(resolution.target),
            "success_level": level,
            "success": success,
            "summary": checks.format_room_check_summary("理智", "regular", resolution.roll, resolution.target, success),
        }

    block = mental_block(member)
    state = _state(member)
    entry = {
        "ts": sanity_state.now_text(),
        "source": str(source or "").strip()[:120],
        "success": bool(success),
        "result": level,
    }
    checks = block.get("realityChecks") if isinstance(block.get("realityChecks"), list) else []
    checks.append(entry)
    block["realityChecks"] = checks[-sanity_state.REALITY_CHECK_LIMIT:]
    san_loss = 0
    bout: dict[str, Any] = {}
    if success:
        # 原文 p.135：成功后“暂时获得对幻觉的抗性，直到调查员下一次失去理智值”。
        # 抗性只由真实的 SAN 损失消耗；被它抵消的幻觉损失不算失去理智值，
        # 因此不能在这里“用一次就清”，否则守秘人可反复用新幻觉抵消（见原文括号）。
        block["realityImmunity"] = True
        summary = (
            f"{_member_label(member)} 现实认知成功（{level or '成功'}），看穿幻象并暂时获得对幻觉的抗性，"
            "直到下一次失去理智值。"
        )
    else:
        block["realityImmunity"] = False
        room_rules.apply_san_change(
            room_dir,
            info,
            member,
            delta=-1,
            reason="现实认知失败",
            actor=actor,
            actor_id=actor_id,
            source="rule",
        )
        san_loss = 1
        # 规则书：现实认知失败后立即进入一次疯狂发作。
        bout = _require(member).begin_bout(
            card=room_rules.member_card(member),
            state=state,
            room_info=info,
            source=str(source or "现实认知失败"),
        )
        summary = f"{_member_label(member)} 现实认知失败（{level or '失败'}），理智 -1，确信眼前的一切都是真的。"
        if bout:
            block = bout.get("mental") or block
            for change in bout.get("pending_changes") or []:
                room_rules._add_pending_change(state, change)
            summary += " " + str(bout.get("summary") or "")
        else:
            summary += " 房间未启用发作计时房规，未记录发作细节。"
    if not bout:
        room_rules._add_pending_change(state, _pending(block, "现实认知"))
    _save(room_dir, info)
    return {
        "player_name": _member_label(member),
        "success": bool(success),
        "result": level,
        "check": check_result,
        "san_loss": san_loss,
        "reality_immunity": bool(block.get("realityImmunity")),
        "bout": bout.get("bout") or {},
        "mental": block,
        "summary": summary,
    }


# ---------------------------------------------------------------- 表查询


def trait_query(kind: Any = None, keyword: Any = None, *, limit: int = 10, roll: bool = False, rng: Any = None) -> dict[str, Any]:
    """按需查询恐惧症 / 躁狂症表；``roll=True`` 时随机抽一条。"""
    if roll:
        picker = rng or (lambda sides: int(time.time() * 1000) % sides + 1)
        kinds = [str(kind).strip().lower()] if kind else ["phobia", "mania"]
        picked = []
        for item in kinds:
            entry = traits.random_entry(item, picker)
            picked.append({"kind": item, "label": traits.KIND_LABELS.get(item, item), "entry": entry, "text": traits.format_entry(entry)})
        return {"rolled": True, "picks": picked}
    return traits.query(kind, keyword, limit=limit)


# ---------------------------------------------------------------- 控制台


def _resolve_member(info: dict[str, Any], name: str) -> dict[str, Any]:
    member = room_rules.find_member(info, name)
    if member is None:
        raise room_rules.RuleOperationError(f"找不到角色：{name}", code="member_not_found")
    return member


def handle_command(
    room_dir: Any,
    info: dict[str, Any],
    *,
    args: list[str],
    actor: str = "",
    actor_id: str = "",
) -> tuple[bool, str]:
    """``rule san <子命令>`` 的批次 3 扩展；返回 (是否处理, 文本)。"""
    if not args:
        return False, ""
    sub = str(args[0] or "").strip().lower()
    if sub not in SAN_COMMAND_SUBS:
        return False, ""
    # 开关关闭时这些子命令等同于不存在，交回旧的 ``rule san add|set`` 分支。
    options = options_for(info)
    required = {
        "bout": options.sanity_state,
        "habituation": options.sanity_state and options.sanity_habituation,
        "habits": options.sanity_state and options.sanity_habituation,
        "reality": options.sanity_state and options.sanity_reality,
        "trait": options.sanity_state and options.sanity_traits,
        "traits": options.sanity_state and options.sanity_traits,
        "treat": options.sanity_state,
    }
    if not required.get(sub, False):
        return False, ""
    if sub == "treat":
        if len(args) < 2:
            return True, "用法：rule san treat <玩家> [成功|失败|大失败] [月数] [备注]"
        member = _resolve_member(info, args[1])
        level = args[2] if len(args) > 2 else "regular"
        months = 1
        if len(args) > 3:
            try:
                months = max(1, min(12, int(args[3])))
            except ValueError:
                months = 1
        note = " ".join(args[4:]) if len(args) > 4 else ""
        outcome = treatment_session(
            room_dir,
            info,
            member,
            level=level,
            months=months,
            note=note,
            actor=actor,
            actor_id=actor_id,
        )
        return True, outcome["summary"]
    if sub in {"trait", "traits"}:
        payload = trait_query(
            args[1] if len(args) > 1 else None,
            " ".join(args[2:]) if len(args) > 2 else None,
            limit=10,
            roll=len(args) > 1 and str(args[1]).lower() in {"roll", "random", "随机"},
        )
        if payload.get("rolled"):
            return True, "随机抽取：" + "；".join(f"[{item['label']}] {item['text']}" for item in payload["picks"])
        lines = []
        for group in payload.get("results") or []:
            lines.append(f"{group['label']}（共 {group['total']} 条，显示 {len(group['entries'])} 条）：")
            lines.extend(f"  #{row['id']} {traits.format_entry(row)}" for row in group["entries"])
        return True, "\n".join(lines) if lines else "没有匹配的条目。"

    if sub in {"reality"}:
        if len(args) < 2:
            return True, "用法：rule san reality <玩家> [成功|失败|大成功|大失败] [来源说明]"
        member = _resolve_member(info, args[1])
        options = options_for(info)
        if not options.sanity_state or not options.sanity_reality:
            return True, "房间未启用现实认知房规（coc7_sanity_reality）。"
        outcome = reality_check(
            room_dir,
            info,
            member,
            result=args[2] if len(args) > 2 else None,
            source=" ".join(args[3:]) if len(args) > 3 else "",
            actor=actor,
            actor_id=actor_id,
        )
        return True, outcome["summary"]

    if sub in {"habituation", "habits"}:
        action = str(args[1] or "").strip().lower() if len(args) > 1 else ""
        if action == "add":
            if len(args) < 5:
                return True, "用法：rule san habituation add <玩家> <来源> <本次损失> [该来源单次最大损失]"
            member = _resolve_member(info, args[2])
            cap = None
            if len(args) > 5:
                try:
                    cap = int(args[5])
                except ValueError:
                    cap = None
            outcome = record_habituation(
                room_dir,
                info,
                member,
                source=args[3],
                loss=int(args[4]),
                cap=cap,
                actor=actor,
                actor_id=actor_id,
            )
            return True, outcome["summary"]
        if action in {"tick", "interlude", "decay"}:
            member = _resolve_member(info, args[2]) if len(args) > 2 else None
            outcome = interlude_decay(room_dir, info, member, reason="幕间（控制台）")
            return True, outcome["summary"]
        target = args[1] if len(args) > 1 else ""
        if target:
            member = _resolve_member(info, target)
            rows = habituation_rows(member, info)
            if not rows:
                return True, f"{_member_label(member)} 暂无习惯恐惧记录。"
            return True, f"{_member_label(member)} 的习惯恐惧：" + "；".join(
                f"{row['source']}（累计 {row['total_loss']}/{row['max_loss'] or '?'}，{row['updated_at'] or '—'}）"
                for row in rows
            )
        rows = []
        for item in info.get("members") or []:
            if not isinstance(item, dict) or not item.get("character_card"):
                continue
            try:
                member_rows = habituation_rows(item, info)
            except room_rules.RuleOperationError:
                continue
            if member_rows:
                rows.append(
                    f"{_member_label(item)}："
                    + "、".join(f"{row['source']}({row['total_loss']}/{row['max_loss'] or '?'})" for row in member_rows)
                )
        return True, "\n".join(rows) if rows else "房间内暂无习惯恐惧记录。"

    # bout
    action = str(args[1] or "").strip().lower() if len(args) > 1 else ""
    if action == "end":
        if len(args) < 3:
            return True, "用法：rule san bout end <玩家>"
        member = _resolve_member(info, args[2])
        return True, end_bout(room_dir, info, member, actor=actor, actor_id=actor_id)["summary"]
    target = args[1] if len(args) > 1 else ""
    if target:
        return True, active_bout_text(_resolve_member(info, target), info)
    lines = []
    for item in info.get("members") or []:
        if not isinstance(item, dict) or not item.get("character_card"):
            continue
        try:
            text = active_bout_text(item, info)
        except room_rules.RuleOperationError:
            continue
        bout = mental_snapshot(item, info).get("bout") or {}
        if isinstance(bout, dict) and bout.get("active"):
            lines.append(text)
    return True, "\n".join(lines) if lines else "房间内暂无进行中的疯狂发作。"
