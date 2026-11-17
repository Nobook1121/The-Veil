"""房间级非战斗法术与效果：状态搬运与显式时间推进。

规则数学仍由规则集插件的 ``spells`` / ``effects`` 能力负责；本模块只做：
- 把房间成员的卡面与 ``character_state`` 整理成插件可读的施法者/目标；
- 把法术位、HP、效果实例与专注写回成员状态并保存 ``info.json``；
- 非战斗没有回合驱动时，用「分钟」作为显式推进单位，由 ``scene.advance``
  工具或房间时钟结算调用 ``tick_holder``。

CoC7 插件没有 ``spells`` / ``effects`` 能力，因此这些路径对它完全不可见。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server import room_rules
from trpg_server.combat.registry import get_ruleset
from trpg_server.combat.settings import resolve_settings
from trpg_server.ruleset import builtin_registry
from trpg_server.ruleset.effects import remove_effect, tick_holder


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def spells_available(ruleset: Any) -> bool:
    return getattr(ruleset, "spells", None) is not None


def effects_available(ruleset: Any) -> bool:
    return getattr(ruleset, "effects", None) is not None


def ruleset_for(info: Mapping[str, Any]) -> tuple[Any, Any]:
    """取规则集插件对象（含 ``combat`` / ``spells`` / ``effects`` 能力）。

    优先返回插件条目：只有它同时带有战斗实现与法术/效果语义；插件不可用时
    退回战斗实现，让调用方的能力检查给出统一错误。
    """
    settings = resolve_settings(dict(info))
    ruleset_id = str(settings.ruleset_id or "")
    try:
        plugin = builtin_registry().resolve(ruleset_id)
    except Exception:  # noqa: BLE001 - 未注册的规则集交给战斗注册表报错
        plugin = None
    if plugin is not None:
        return plugin, settings
    return get_ruleset(ruleset_id), settings


def member_names(member: Mapping[str, Any]) -> set[str]:
    card = member.get("character_card") if isinstance(member.get("character_card"), Mapping) else {}
    names = {
        _as_text(member.get("username")),
        _as_text(member.get("nickname")),
        _as_text(card.get("name")),
        _as_text(member.get("user_id")),
    }
    return {name.casefold() for name in names if name}


def find_member(info: Mapping[str, Any], identifier: Any) -> dict[str, Any] | None:
    key = _as_text(identifier).casefold()
    if not key:
        return None
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        if key in member_names(member):
            return member
    return None


def resolve_member(
    info: Mapping[str, Any],
    identifier: Any,
    context: Any = None,
    *,
    role: str = "施法者",
) -> tuple[dict[str, Any] | None, str]:
    """解析房间成员；显式标识找不到时直接报错，不做“第一个有卡成员”的猜测。

    只有完全未指定标识、且 ``context.user_id`` 能唯一对应当前成员时才回落到
    「调用者本人」。这样 KP 或写错名字的 AI 工具不会把行动转嫁到其他玩家。
    """
    text = _as_text(identifier)
    if text:
        found = find_member(info, text)
        if found is None:
            return None, f"找不到{role}“{text}”（需要房间成员名或用户名）"
        return found, ""
    user_id = getattr(context, "user_id", None)
    if user_id is not None:
        found = find_member(info, user_id)
        if found is not None:
            return found, ""
        return None, f"当前用户不是房间成员，无法确定{role}；请显式指定"
    return None, f"找不到{role}；请显式指定（房间成员名）"


def _prepare_holder(
    member: dict[str, Any],
    ruleset: Any,
    settings: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    card = room_rules.member_card(member)
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    effects = state.get("effects")
    holder: dict[str, Any] = {
        "name": _as_text(card.get("name")) or _as_text(member.get("username")) or "冒险者",
        "aliases": sorted(member_names(member)),
        "user_id": member.get("user_id"),
        "is_player": True,
        "side": "player",
        "level": _as_int(card.get("level"), 1),
        "ruleset": getattr(ruleset, "ruleset_id", ""),
        "effects": effects if isinstance(effects, list) else [],
    }
    if isinstance(state.get("concentration"), Mapping):
        holder["concentration"] = dict(state["concentration"])
    prepare = getattr(getattr(ruleset, "combat", None), "prepare_combatant", None)
    if callable(prepare):
        prepare(holder, card=card, settings=settings)
    # 非战斗的当前 HP 以 character_state 为准，卡面只提供上限。
    if state.get("current_hp") is not None:
        holder["hp"] = max(0, _as_int(state.get("current_hp"), 0))
    if state.get("max_hp") is not None:
        holder["max_hp"] = max(1, _as_int(state.get("max_hp"), 1))
    holder.setdefault("effects", [])
    return holder, card, state


def _sync_holder(member: dict[str, Any], holder: Mapping[str, Any], ruleset: Any) -> None:
    card = room_rules.member_card(member)
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    persist = getattr(getattr(ruleset, "combat", None), "persist_combatant", None)
    if callable(persist):
        persist(holder, card)
    if holder.get("hp") is not None:
        state["current_hp"] = max(0, _as_int(holder.get("hp"), 0))
    if holder.get("max_hp") is not None:
        state["max_hp"] = max(1, _as_int(holder.get("max_hp"), 1))
    state["effects"] = list(holder.get("effects") or [])
    if isinstance(holder.get("concentration"), Mapping):
        state["concentration"] = dict(holder["concentration"])
    else:
        state.pop("concentration", None)
    state["pending_persist"] = True


def _holder_map(
    info: Mapping[str, Any], ruleset: Any, settings: Any
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    holders: dict[str, dict[str, Any]] = {}
    members: dict[str, dict[str, Any]] = {}
    for member in info.get("members") or []:
        if not isinstance(member, dict) or not isinstance(member.get("character_card"), Mapping):
            continue
        holder, _card, _state = _prepare_holder(member, ruleset, settings)
        key = _as_text(holder.get("name"))
        if not key or key in holders:
            continue
        holders[key] = holder
        members[key] = member
    return holders, members


def _sync_all(members: Mapping[str, Mapping[str, Any]], holders: Mapping[str, Any], ruleset: Any) -> None:
    for key, member in members.items():
        holder = holders.get(key)
        if isinstance(holder, Mapping):
            _sync_holder(member, holder, ruleset)  # type: ignore[arg-type]


def cast(
    room_dir: Any,
    info: dict[str, Any],
    *,
    caster: Any = None,
    spell: Any = "",
    targets: Any = None,
    slot_level: Any = None,
    ritual: bool = False,
    context: Any = None,
    rng: Any = None,
    request_key: str = "",
) -> dict[str, Any]:
    """非战斗施法：解析成员、委托插件结算、写回法术位/HP/效果/专注。

    与战斗施法共用服务端工作流入口；``request_key`` 相同的重复提交只返回
    上一次的结果，不会二次消耗法术位或重复治疗/伤害。
    """
    ruleset, settings = ruleset_for(info)
    spells = getattr(ruleset, "spells", None)
    if spells is None:
        return {"error": "当前规则集没有法术能力"}
    if request_key:
        # 幂等重试先于成员解析与目标校验，返回第一次施法的结果。
        from trpg_server.workflows.actions import STEP_RESOLVE, prior_flow

        existing = prior_flow(room_dir, request_key, STEP_RESOLVE)
        if existing is not None:
            flow_existing, cached = existing
            if isinstance(cached, dict):
                return {
                    **cached,
                    "workflow": {
                        "id": flow_existing.get("id"),
                        "status": flow_existing.get("status"),
                        "duplicate": True,
                    },
                }
    caster_member, caster_error = resolve_member(info, caster, context, role="施法者")
    if caster_member is None:
        return {"error": caster_error}

    holders, members = _holder_map(info, ruleset, settings)
    caster_holder = next(
        (holder for name, holder in holders.items() if caster_member in (members.get(name),)),
        None,
    )
    if caster_holder is None:
        # 成员没有绑定角色卡时也允许给出明确错误。
        return {"error": "施法者没有绑定角色卡"}

    mode = "target"
    target_mode = getattr(spells, "target_mode", None)
    if callable(target_mode):
        try:
            mode = str(target_mode(spell) or "target")
        except Exception:  # noqa: BLE001 - 目标模式失败时按需要目标处理
            mode = "target"
    raw_targets = targets if isinstance(targets, list) else ([targets] if targets else [])
    target_members: list[dict[str, Any]] = []
    for name in raw_targets:
        text = _as_text(name)
        if text.casefold() in {"self", "自己", "自身"}:
            target_members.append(caster_member)
            continue
        member = find_member(info, text)
        if member is None:
            return {"error": f"房间里找不到目标“{text}”"}
        target_members.append(member)
    if not target_members:
        if mode == "self":
            target_members = [caster_member]
        elif mode == "target":
            return {"error": "该法术需要指定 targets（房间成员名）"}
    target_holders = []
    for member in target_members:
        found = next(
            (holder for name, holder in holders.items() if members.get(name) is member), None
        )
        if found is not None and found not in target_holders:
            target_holders.append(found)

    from trpg_server.workflows.guards import validate_action_report

    blocked, guard_notes = validate_action_report(
        room_dir,
        kind="cast",
        actor=str(caster_holder.get("name") or ""),
        targets=[str(holder.get("name") or "") for holder in target_holders],
    )
    if blocked:
        return {"error": "；".join(blocked), "code": "workflow.blocked"}

    combat = {"combatants": holders, "round": 0}

    def resolve(flow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        result = spells.cast(
            caster_holder,
            room_rules.member_card(caster_member),
            spell=spell,
            targets=target_holders,
            slot_level=_as_int(slot_level, 0) or None,
            target_cards={
                _as_text(holder.get("name")): room_rules.member_card(members[_as_text(holder.get("name"))])
                for holder in target_holders
            },
            rng=rng,
            combat=combat,
            settings=settings,
            noncombat=True,
            ritual=bool(ritual),
        )
        if result.get("error"):
            return result
        _sync_all(members, holders, ruleset)
        room_rules.save_room_info(room_dir, info)
        from trpg_server.workflows import areas as area_store
        from trpg_server.workflows import concentration

        record = result.get("concentration") if isinstance(result.get("concentration"), Mapping) else {}
        profile = caster_holder.get("spellcasting") if isinstance(caster_holder.get("spellcasting"), Mapping) else {}
        dc = _as_int(record.get("dc") if record else profile.get("spellSaveDC"), 0)
        origin = area_store.lookup_position(
            room_dir,
            _as_text((target_holders[0] if target_holders else caster_holder).get("name")),
        )
        spec = area_store.area_spec_from_cast(
            result,
            caster=_as_text(caster_holder.get("name")),
            dc=dc,
            origin=origin,
            grid_ft=int((origin or {}).get("grid_ft") or 5),
            targets=[_as_text(holder.get("name")) for holder in target_holders],
        )
        if spec:
            created = area_store.create_area(room_dir, spec)
            if created.get("area"):
                result["area"] = created["area"]
        concentration.sync(
            room_dir, concentration.holders_from_members(info.get("members"))
        )
        result["noncombat"] = True
        result["caster"] = caster_holder.get("name")
        result["targets"] = [holder.get("name") for holder in target_holders]
        if guard_notes:
            result["guard_notes"] = list(guard_notes)
        return result

    from trpg_server.workflows.actions import (
        STEP_RESOLVE,
        action_steps,
        executor_type_for,
        flow_result,
        run_flow_action,
    )

    initiator_type = executor_type_for(
        is_player=True,
        agent_id=str(getattr(context, "agent_id", "") or ""),
    )
    outcome = run_flow_action(
        room_dir,
        kind="cast",
        room_id=str(info.get("id") or ""),
        initiator=str(caster_holder.get("name") or ""),
        initiator_type=initiator_type,
        steps=action_steps("cast", executor_type=initiator_type),
        handlers={STEP_RESOLVE: resolve},
        request_key=str(request_key or ""),
        metadata={"spell": _as_text(spell), "noncombat": True},
    )
    if outcome.get("error"):
        return {"error": outcome["error"], "code": outcome.get("code")}
    flow = outcome.get("flow") or {}
    result = flow_result(flow, STEP_RESOLVE) or {}
    if not isinstance(result, dict):
        return {"error": "法术结算返回了无法识别的结果"}
    if result.get("error"):
        return result
    return {
        **result,
        "workflow": {
            "id": flow.get("id"),
            "status": flow.get("status"),
            "duplicate": bool(outcome.get("duplicate")),
        },
    }


def list_spells(
    info: Mapping[str, Any],
    *,
    caster: Any = None,
    context: Any = None,
) -> dict[str, Any]:
    ruleset, settings = ruleset_for(info)
    spells = getattr(ruleset, "spells", None)
    if spells is None:
        return {"error": "当前规则集没有法术能力"}
    member, member_error = resolve_member(info, caster, context, role="施法者")
    if member is None:
        return {"error": member_error}
    holder, card, _state = _prepare_holder(member, ruleset, settings)
    return {
        "noncombat": True,
        "caster": holder.get("name"),
        "spells": spells.list_spells(holder, card),
    }


def advance_minutes(
    room_dir: Any,
    info: dict[str, Any],
    *,
    minutes: int,
) -> dict[str, Any]:
    """按分钟显式推进所有成员的非战斗效果；返回到期与持续提示。"""
    ruleset, _settings = ruleset_for(info)
    if not effects_available(ruleset):
        return {"error": "当前规则集没有状态与效果能力"}
    step = max(1, int(minutes))
    notes: list[str] = []
    changed = False
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        state = member.get("character_state")
        if not isinstance(state, dict) or not state.get("effects"):
            continue
        member_notes, expired = tick_holder(
            state,
            phase="minute",
            timing="time_advance",
            amount=step,
            plugin=ruleset,
            rng=None,
        )
        member_changed = bool(member_notes or expired)
        if member_changed:
            changed = True
            state["pending_persist"] = True
            notes.extend(str(note) for note in member_notes if str(note or "").strip())
    if changed:
        room_rules.save_room_info(room_dir, info)
    return {"minutes": step, "notes": notes}


def concentration(
    room_dir: Any,
    info: dict[str, Any],
    *,
    member: Any = None,
    action: str = "status",
    damage: Any = 0,
    reason: str = "",
    context: Any = None,
    rng: Any = None,
) -> dict[str, Any]:
    """查看/打断非战斗专注；damage > 0 时先做专注检定。"""
    ruleset, settings = ruleset_for(info)
    spells = getattr(ruleset, "spells", None)
    if spells is None:
        return {"error": "当前规则集没有法术能力"}
    target_member, member_error = resolve_member(info, member, context, role="角色")
    if target_member is None:
        return {"error": member_error}
    holders, members = _holder_map(info, ruleset, settings)
    holder = next(
        (value for name, value in holders.items() if members.get(name) is target_member), None
    )
    if holder is None:
        return {"error": "角色没有绑定角色卡"}
    normalized = _as_text(action).casefold() or "status"
    if normalized not in {"status", "break", "damage", "打断", "查看"}:
        return {"error": "action 只能是 status / break / damage"}
    value = _as_int(damage, 0)
    record = holder.get("concentration")
    # 只查看：没有伤害、也没有主动打断时保持原状。
    if normalized in {"status", "查看"} and value <= 0:
        return {"member": holder.get("name"), "concentration": record or None}
    if normalized == "damage" and value <= 0:
        return {"error": "action=damage 需要传入正整数 damage"}
    notes: list[str] = []
    combat = {"combatants": holders, "round": 0}
    if value > 0:
        check = getattr(spells, "concentration_check", None)
        if callable(check):
            notes.extend(check(holder, value, combat=combat, rng=rng))
        else:
            holder.pop("concentration", None)
    else:
        end = getattr(spells, "end_concentration", None)
        if callable(end):
            notes.extend(end(holder, combat=combat, reason=reason or "主动结束"))
        else:
            holder.pop("concentration", None)
    # 兜底：插件没有返回时，直接按效果引用清理目标状态。
    if isinstance(record, Mapping) and not notes:
        for ref in record.get("effects") or []:
            if not isinstance(ref, Mapping):
                continue
            target = holders.get(_as_text(ref.get("target")))
            if isinstance(target, dict):
                remove_effect(target, effect_id=_as_text(ref.get("id")))
    _sync_all(members, holders, ruleset)
    room_rules.save_room_info(room_dir, info)
    return {
        "member": holder.get("name"),
        "broken": not isinstance(holder.get("concentration"), Mapping),
        "concentration": holder.get("concentration") or None,
        "notes": notes,
    }


__all__ = [
    "advance_minutes",
    "cast",
    "concentration",
    "effects_available",
    "find_member",
    "list_spells",
    "member_names",
    "resolve_member",
    "ruleset_for",
    "spells_available",
]
