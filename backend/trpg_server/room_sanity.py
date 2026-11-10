"""批次 3：房间理智状态层（状态存取与调度，规则数学在 ruleset/coc7）。

这里不做规则计算，只负责：
- 把插件给出的结构化精神状态写入 ``character_state["mental"]`` 与
  ``character_card["extensions"]["mental"]``，并登记 ``extensions`` 持久化变更；
- 提供控制台 / AI 工具共用的查询、发作结束、习惯恐惧、现实认知与查表入口；
- 给房间时钟留幕间接口 :func:`interlude_decay`（心理治疗由时钟会话实现）。

所有新行为都受房规开关控制，默认关闭时这些函数直接拒绝执行。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from trpg_server import room_rules
from trpg_server.ruleset.capabilities import sanity_of
from trpg_server.ruleset.coc7 import sanity_state, traits
from trpg_server.ruleset.coc7.options import resolve_options

SAN_COMMAND_SUBS = {"bout", "habituation", "habits", "reality", "trait", "traits"}
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
    return sanity_state.describe(mental_snapshot(member, info))


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
    status = "进行中" if bout.get("active") and sanity_state.has_active_bout(block) else "已结束"
    return f"{_member_label(member)}：{status}；{sanity_state.bout_summary(bout)}"


# ---------------------------------------------------------------- 习惯恐惧


def record_habituation(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    source: str,
    loss: int,
    actor: str = "",
    actor_id: str = "",
) -> dict[str, Any]:
    """记录一次来源造成的理智损失；返回值含是否免疫。"""
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    if not options.sanity_habituation:
        raise _disabled("sanity_habituation")
    key = " ".join(str(source or "").split())
    if not key:
        raise room_rules.RuleOperationError("请提供恐惧来源名称", code="bad_arguments")
    block = mental_block(member)
    result = sanity_state.note_habituation(block, key, int(loss or 0))
    room_rules._add_pending_change(_state(member), _pending(block, "习惯恐惧"))
    _save(room_dir, info)
    if result["immune"]:
        summary = f"{_member_label(member)} 已习惯「{key}」：本次损失 {result['loss']} ≤ 记录值 {result['previous']}，不再损失理智。"
    else:
        summary = f"{_member_label(member)} 对「{key}」的记录最大损失更新为 {result['loss']}（原 {result['previous']}）。"
    return {"player_name": _member_label(member), "habituation": result, "mental": block, "summary": summary}


def habituation_rows(member: dict[str, Any], info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    options = options_for(info)
    if not options.sanity_state:
        raise _disabled("sanity_state")
    block = mental_snapshot(member, info)
    rows = []
    for key, row in (block.get("habituations") or {}).items():
        if isinstance(row, dict):
            rows.append({"source": row.get("name") or key, "max_loss": row.get("max_loss"), "updated_at": row.get("updated_at")})
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
    1D6 月不定性疯狂）不在本函数内，由时钟会话在其后自行实现。
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
            key: int((row or {}).get("max_loss") or 0) if isinstance(row, dict) else 0
            for key, row in (block.get("habituations") or {}).items()
        }
        if not before:
            continue
        removed = sanity_state.decay_habituations(block, steps)
        after = {
            key: int((row or {}).get("max_loss") or 0) if isinstance(row, dict) else 0
            for key, row in (block.get("habituations") or {}).items()
        }
        if after == before:
            continue
        room_rules._add_pending_change(_state(target), _pending(block, reason))
        changed = True
        detail = "、".join(f"{name}→{int(row.get('max_loss') or 0)}" for name, row in block["habituations"].items())
        if removed:
            detail += ("；" if detail else "") + "已消除：" + "、".join(removed)
        summaries.append(f"{_member_label(target)}：{detail or '无有效记录'}")
    if changed:
        _save(room_dir, info)
    return {
        "members": summaries,
        "summary": ("幕间习惯恐惧衰减：" + "；".join(summaries)) if summaries else "没有需要衰减的习惯恐惧记录。",
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
) -> dict[str, Any]:
    """现实认知：POW 检定成功则看穿幻象并获得免疫，失败损失 1 点理智。"""
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
        check_result = room_rules.roll_check(
            room_dir,
            info,
            member,
            name="POW",
            difficulty="regular",
            reason="现实认知",
            actor=actor,
            actor_id=actor_id,
        )
        level = str(check_result.get("success_level") or "")
        success = bool(check_result.get("success"))

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
        block["realityImmunity"] = True
        summary = f"{_member_label(member)} 现实认知成功（{level or '成功'}），看穿当前幻象并免疫下一次理智损失。"
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
        # 规则书：现实认知失败后立即进入疯狂发作（1D10 小时）。
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
    }
    if not required.get(sub, False):
        return False, ""
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
                return True, "用法：rule san habituation add <玩家> <来源> <本次损失>"
            member = _resolve_member(info, args[2])
            outcome = record_habituation(
                room_dir,
                info,
                member,
                source=args[3],
                loss=int(args[4]),
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
                f"{row['source']}（{row['max_loss']}，{row['updated_at'] or '—'}）" for row in rows
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
                rows.append(f"{_member_label(item)}：" + "、".join(f"{row['source']}({row['max_loss']})" for row in member_rows))
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
