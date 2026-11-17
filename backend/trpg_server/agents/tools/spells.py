"""法术工具（规则中立命名）。

``spell.list`` / ``spell.cast`` 只负责解析角色、校验行动预算、把请求转给当前
规则集的 ``spells`` 能力，并写回战斗或房间状态；法术位、升环、攻击/豁免、伤害
与专注全部在插件侧。没有 ``spells`` 能力的规则集会在下发阶段被过滤掉这些工具。
没有战斗时，施法与专注走 :mod:`trpg_server.room_spells` 的房间级结算。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from trpg_server import room_spells
from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.dice import _dice_roll_metadata, _result_message
from trpg_server.agents.tools.combat import (
    _append_log,
    _as_int,
    _as_text,
    _combat_ruleset,
    _combat_settings,
    _combat_summary_state,
    _load_combat,
    _player_card,
    _resolve_combatant,
    _roller,
    _save_combat,
    _scheduler,
)
from trpg_server.ruleset.contracts import CheckRequest
from trpg_server.workflows.actions import (
    STEP_RESOLVE,
    action_steps,
    executor_type_for,
    flow_result,
    prior_flow,
    run_flow_action,
)


def _current_actor(combat: dict[str, Any]) -> dict[str, Any] | None:
    order = combat.get("order") or []
    index = _as_int(combat.get("turn"), 0)
    if 0 <= index < len(order):
        return _resolve_combatant(combat, order[index])
    return None


def _require_spells(combat: dict[str, Any], context: Any) -> tuple[Any, Any, dict[str, Any] | None]:
    ruleset = _combat_ruleset(combat, _combat_settings(context))
    spells = getattr(ruleset, "spells", None)
    if spells is None:
        return ruleset, None, {"error": "当前规则集没有法术能力"}
    return ruleset, spells, None


def _resolve_targets(
    combat: dict[str, Any],
    caster: dict[str, Any],
    arguments: dict[str, Any],
    context: Any,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    raw = arguments.get("targets")
    if isinstance(raw, str):
        names = [raw]
    elif isinstance(raw, list):
        names = [str(item) for item in raw if str(item or "").strip()]
    else:
        names = []
    if not names and _as_text(arguments.get("range")).casefold() == "self":
        names = [str(caster.get("name") or "")]
    targets: list[dict[str, Any]] = []
    cards: dict[str, dict[str, Any]] = {}
    for name in names:
        target = _resolve_combatant(combat, name)
        if target is None:
            return [], {}, {"error": f"战斗中找不到目标“{name}”"}
        targets.append(target)
        card = _player_card(target, context)
        if card:
            cards[str(target.get("name") or "")] = card
    return targets, cards, None


def _outcome_dice_metadata(
    source: Any,
    *,
    check_name: str,
    target: Any = None,
    roller: str | None = None,
) -> dict[str, Any] | None:
    """把插件给出的 d20 CheckOutcome 转成骰桌元数据；非 d20 一律不接管。"""
    if not isinstance(source, dict):
        return None
    presentation = source.get("presentation") if isinstance(source.get("presentation"), dict) else {}
    if str(presentation.get("mode") or "") != "d20":
        return None
    detail = source.get("detail") if isinstance(source.get("detail"), dict) else {}
    dice = [int(item) for item in (source.get("dice") or detail.get("dice") or [])]
    if not dice:
        return None
    kept = detail.get("kept")
    try:
        natural = int(kept if kept is not None else dice[0])
    except (TypeError, ValueError):
        natural = dice[0]
    payload = {
        "expression": "1d20",
        "rolls": dice,
        "roll": natural,
        "natural": natural,
        "total": source.get("total"),
        "success": source.get("success"),
        "success_level": source.get("degree"),
        "advantage": bool(detail.get("advantage")),
        "disadvantage": bool(detail.get("disadvantage")),
    }
    return _dice_roll_metadata(
        payload,
        mode="d20",
        check_name=check_name,
        target=int(target) if target is not None else source.get("target"),
        roller=roller,
    )


def _attach_spell_dice_message(result: dict[str, Any]) -> None:
    """法术攻击与单目标豁免追加骰桌消息；多目标豁免交给摘要，避免错配。"""
    if not isinstance(result, dict) or result.get("error"):
        return
    spell = result.get("spell") if isinstance(result.get("spell"), dict) else {}
    label = str(spell.get("name") or "法术")
    caster_name = str(result.get("caster") or "")
    messages: list[dict[str, Any]] = []

    attack = result.get("attack")
    if isinstance(attack, dict):
        meta = _outcome_dice_metadata(
            attack,
            check_name=f"{label}攻击",
            target=attack.get("target"),
            roller=caster_name or None,
        )
        summary = str(
            attack.get("summary")
            or (attack.get("presentation") or {}).get("summary")
            or ""
        )
        if meta and summary:
            messages.append(
                _result_message(
                    summary,
                    "spell.cast",
                    check_type="spell_attack",
                    extra_metadata={"dice_roll": meta},
                )
            )

    saves = result.get("saves") if isinstance(result.get("saves"), list) else []
    if len(saves) == 1 and isinstance(saves[0], dict):
        save = saves[0]
        target_name = str(save.get("target") or "")
        meta = _outcome_dice_metadata(
            save,
            check_name=f"{label}豁免（{target_name}）" if target_name else f"{label}豁免",
            target=save.get("dc"),
            roller=target_name or None,
        )
        summary = str(save.get("summary") or "")
        if meta and summary:
            messages.append(
                _result_message(
                    summary,
                    "spell.cast",
                    check_type="spell_save",
                    extra_metadata={"dice_roll": meta},
                )
            )

    if messages:
        result["direct_messages"] = [*(result.get("direct_messages") or []), *messages]


def list_spells(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """列出施法者已知/准备的法术、法术位与可施放状态。"""
    combat = _load_combat(context)
    if not combat:
        return room_spells.list_spells(
            context.room_info(),
            caster=arguments.get("caster"),
            context=context,
        )
    _ruleset, spells, error = _require_spells(combat, context)
    if error:
        return error
    caster = _resolve_combatant(combat, arguments.get("caster")) or _current_actor(combat)
    if caster is None:
        return {"error": "当前没有可施法的战斗者"}
    listing = spells.list_spells(caster, _player_card(caster, context))
    return {
        **_combat_summary_state(combat),
        "caster": caster.get("name"),
        "spells": listing,
    }


def _counter_candidates(
    combat: Mapping[str, Any],
    caster: Mapping[str, Any],
    spell: str,
) -> list[dict[str, Any]]:
    """找出能反制这次施法的候选者（有反应额度、认识反制法术、还有三环位）。"""
    if str(spell or "").strip().lower() in {"counter-spell", "counterspell"}:
        return []
    rows: list[dict[str, Any]] = []
    for name, combatant in (combat.get("combatants") or {}).items():
        if not isinstance(combatant, Mapping) or combatant is caster:
            continue
        if combatant.get("dead") or combatant.get("unconscious"):
            continue
        budget = combatant.get("budget") if isinstance(combatant.get("budget"), Mapping) else {}
        if _as_int(budget.get("reaction"), 0) <= 0:
            continue
        known = {
            str(item).strip().lower()
            for key in ("spells_known", "spells_prepared", "known_spells")
            for item in (combatant.get(key) or [])
        }
        if "counter-spell" not in known and "counterspell" not in known:
            continue
        slots = combatant.get("spell_slots") if isinstance(combatant.get("spell_slots"), Mapping) else {}
        row = slots.get("3") if isinstance(slots.get("3"), Mapping) else {}
        if _as_int(row.get("max"), 0) - _as_int(row.get("used"), 0) <= 0:
            continue
        rows.append({"name": str(name), "is_player": bool(combatant.get("is_player"))})
    return rows


_SAVE_SUCCESS_WORDS = frozenset(
    {"success", "succeeded", "pass", "passed", "make", "made", "ok", "saved"}
)
_SAVE_FAILURE_WORDS = frozenset({"fail", "failed", "failure", "miss", "missed"})


def _save_candidates(
    spells: Any,
    spell: str,
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """法术需要豁免时返回每个目标的候选项；不需要豁免返回空列表。"""
    ability_of = getattr(spells, "save_ability", None)
    if not callable(ability_of) or not str(ability_of(spell) or "").strip():
        return []
    rows: list[dict[str, Any]] = []
    for target in targets:
        name = str(target.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "is_player": bool(target.get("is_player")),
                "agent_id": str(target.get("agent_id") or ""),
            }
        )
    return rows


def _parse_save_decision(value: Any) -> dict[str, Any] | None:
    """把玩家/KP 提交的豁免决定归一化成 ``{success, total, dice, ...}``。"""
    if isinstance(value, bool):
        return {"success": value}
    if not isinstance(value, Mapping):
        return None
    success = value.get("success")
    if not isinstance(success, bool):
        raw = str(value.get("decision") or value.get("result") or "").strip().lower()
        if raw in _SAVE_SUCCESS_WORDS:
            success = True
        elif raw in _SAVE_FAILURE_WORDS:
            success = False
        else:
            return None
    payload: dict[str, Any] = {"success": success}
    for key in ("total", "summary", "degree"):
        if value.get(key) not in (None, ""):
            payload[key] = value.get(key)
    dice = value.get("dice")
    if isinstance(dice, list):
        payload["dice"] = dice
    for key in ("detail", "presentation"):
        if isinstance(value.get(key), Mapping):
            payload[key] = dict(value[key])
    return payload


def _save_results(flow: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """收集已决的 ``save:<目标>`` 步骤，交给规则集消费。"""
    from trpg_server.workflows import reactions

    results: dict[str, dict[str, Any]] = {}
    for step in flow.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        step_id = str(step.get("id") or "")
        if not step_id.startswith(reactions.SAVE_PREFIX):
            continue
        if str(step.get("status")) != "completed":
            continue
        parsed = _parse_save_decision(step.get("result"))
        if parsed is not None:
            results[reactions.save_target_of(step_id)] = parsed
    return results


def _apply_combat_cast(
    context: Any,
    combat: dict[str, Any],
    caster: dict[str, Any],
    spell: str,
    targets: list[dict[str, Any]],
    target_cards: Mapping[str, Mapping[str, Any]],
    slot_level: int | None,
    save_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行一次战斗内施法；流程 handler 与反应续跑器共用。

    ``save_results`` 是策略为 ask 时由玩家/KP 决定的豁免结果；缺省时规则集
    仍按原逻辑自动掷骰（auto）。
    """
    _ruleset, spells, error = _require_spells(combat, context)
    if error:
        return error
    slot = spells.required_slot(spell)
    scheduler = _scheduler(combat, context)
    budget = scheduler.budget_of(caster)
    if budget and slot in budget and _as_int(budget.get(slot), 0) <= 0:
        return {"error": f"{caster.get('name')} 的 {slot} 额度不足，无法施放 {spell}"}
    result = spells.cast(
        caster,
        _player_card(caster, context),
        spell=spell,
        targets=targets,
        slot_level=slot_level,
        target_cards=target_cards,
        rng=_roller,
        combat=combat,
        settings=_combat_settings(context),
        save_results=save_results,
    )
    if result.get("error"):
        return result
    caster_name = str(caster.get("name") or "")
    result["caster"] = caster_name
    _attach_spell_dice_message(result)
    from trpg_server.workflows import areas as area_store
    from trpg_server.workflows import concentration

    concentration_record = (
        result.get("concentration") if isinstance(result.get("concentration"), Mapping) else {}
    )
    profile = caster.get("spellcasting") if isinstance(caster.get("spellcasting"), Mapping) else {}
    dc = _as_int(
        concentration_record.get("dc") if concentration_record else profile.get("spellSaveDC"),
        0,
    )
    origin = area_store.lookup_position(
        context.room_dir,
        str((targets[0] if targets else caster).get("name") or ""),
    )
    spec = area_store.area_spec_from_cast(
        result,
        caster=caster_name,
        dc=dc,
        scene_id=str(getattr(context, "scene_id", "") or ""),
        origin=origin,
        grid_ft=int((origin or {}).get("grid_ft") or 5),
        targets=[str(target.get("name") or "") for target in targets],
    )
    if spec:
        created = area_store.create_area(context.room_dir, spec)
        if created.get("area"):
            result["area"] = created["area"]
    concentration.sync(context.room_dir, concentration.holders_from_combat(combat))
    if budget and slot in budget:
        scheduler.spend_budget(caster, slot, 1)
    _append_log(combat, str(result.get("summary") or f"{caster_name} 施放 {spell}。"), context)
    _save_combat(context, combat)
    return result


class _FlowContext:
    """续跑时重建一个最小的 room 上下文，避免依赖原请求对象。"""

    def __init__(self, room_dir: Any, room_id: str, info: dict[str, Any]):
        self.room_dir = room_dir
        self.room_id = room_id
        self.config_dir = Path(room_dir) / "config"
        self.scenarios_dir = Path(room_dir) / "scenarios"
        self.user_id = 0
        self.agent_id = ""
        self._info = info

    def room_info(self) -> dict[str, Any]:
        return self._info


def _finish_cast_steps(
    flow: dict[str, Any],
    result: Mapping[str, Any],
    *,
    countered: bool = False,
) -> None:
    from trpg_server.workflows.model import stamp

    if countered:
        # 法术被反制后豁免不再发生，待决步骤按跳过收尾。
        for step in flow.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if not str(step.get("id") or "").startswith("save:"):
                continue
            if str(step.get("status")) in {"pending", "waiting", "running"}:
                step["status"] = "skipped"
                step["finished_at"] = stamp()
    for step in flow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("id") not in {"resolve", "effect", "concentration"}:
            continue
        if str(step.get("status")) not in {"pending", "running"}:
            continue
        step["status"] = "completed"
        step["finished_at"] = stamp()
        if step.get("id") == "resolve":
            step["result"] = dict(result)
        else:
            step["result"] = {"skipped": countered, "countered": countered}


def _append_flow_event(
    flow: dict[str, Any],
    event_type: str,
    *,
    actor: str = "",
    payload: Mapping[str, Any] | None = None,
) -> None:
    from trpg_server.workflows.model import stamp

    events = flow.setdefault("events", [])
    events.append(
        {
            "seq": len(events) + 1,
            "at": stamp(),
            "type": str(event_type),
            "actor": str(actor or ""),
            "payload": dict(payload or {}),
        }
    )
    if len(events) > 200:
        del events[:-200]


def _counter_check(
    ruleset: Any,
    spells: Any,
    reactor: Mapping[str, Any] | None,
    reactor_name: str,
    context: Any,
    *,
    cast_level: int,
) -> tuple[bool, dict[str, Any]]:
    """高环反制的施法属性检定：DC = 10 + 被反制法术等级。"""
    checks = getattr(spells, "checks", None)
    if checks is None:
        checks = getattr(ruleset, "checks", None)
    if checks is None or not callable(getattr(checks, "roll_outcome", None)):
        raise RuntimeError("当前规则集没有判定能力，无法结算高环反制")
    card = _player_card(reactor, context) if isinstance(reactor, Mapping) else None
    ability = ""
    ability_of = getattr(spells, "casting_ability", None)
    if callable(ability_of) and isinstance(reactor, Mapping):
        ability = str(ability_of(reactor, card) or "").strip()
    ability = ability or "int"
    dc = 10 + int(cast_level)
    target_card = card if isinstance(card, Mapping) else (reactor or {})
    outcome = checks.roll_outcome(
        target_card,
        CheckRequest(
            kind="check",
            name=f"{ability}检定",
            target=dc,
            extra={"label": "反制法术检定"},
        ),
        _roller,
    )
    presentation = outcome.presentation if isinstance(outcome.presentation, Mapping) else {}
    record = {
        "reactor": reactor_name,
        "ability": ability,
        "dc": dc,
        "spell_level": int(cast_level),
        "success": bool(outcome.success),
        "total": outcome.total,
        "dice": list(outcome.dice),
        "degree": outcome.degree,
        "summary": str(presentation.get("summary") or ""),
    }
    return bool(outcome.success), record


def _cast_after_reaction(room_dir: Any, flow: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """反应窗口收束后继续施法；被反制则只扣法术位，不产生效果。"""
    from trpg_server.room_rules import load_room_info
    from trpg_server.workflows import reactions

    metadata = flow.get("metadata") if isinstance(flow.get("metadata"), Mapping) else {}
    info = load_room_info(room_dir)
    context = _FlowContext(room_dir, str(flow.get("room_id") or ""), info)
    combat = _load_combat(context)
    if not combat:
        raise RuntimeError("反应结束后找不到战斗状态")
    caster = _resolve_combatant(combat, metadata.get("caster"))
    if caster is None:
        raise RuntimeError("反应结束后找不到施法者")
    spell = _as_text(metadata.get("spell"))
    slot_level = _as_int(metadata.get("slot_level"), 0) or None
    target_names = [str(name) for name in metadata.get("targets") or []]
    targets = [item for item in (_resolve_combatant(combat, name) for name in target_names) if item]
    caster_name = str(caster.get("name") or "")
    countered_by = reactions.counter_used(flow)
    if countered_by:
        ruleset, spells, error = _require_spells(combat, context)
        if error:
            raise RuntimeError(error.get("error") or "缺少法术能力")
        scheduler = _scheduler(combat, context)
        reactor = _resolve_combatant(combat, countered_by)
        # 反制法术一旦施放，反应与 3 环位都要扣除；检定失败也不退还。
        if reactor is not None:
            reactor_slots = reactor.setdefault("spell_slots", {})
            third = reactor_slots.get("3")
            if isinstance(third, dict):
                third["used"] = _as_int(third.get("used"), 0) + 1
            if scheduler.budget_of(reactor):
                scheduler.spend_budget(reactor, "reaction", 1)
        cast_level = max(0, _as_int(slot_level, 0))
        counter_works = True
        counter_record: dict[str, Any] | None = None
        if cast_level >= 4:
            # 4 环及以上：自动结算施法属性检定，DC = 10 + 法术等级。
            counter_works, counter_record = _counter_check(
                ruleset,
                spells,
                reactor,
                countered_by,
                context,
                cast_level=cast_level,
            )
            _append_flow_event(
                flow,
                "counter.check",
                actor=countered_by,
                payload=counter_record,
            )
        if counter_works:
            use_level = slot_level or 1
            slots = caster.setdefault("spell_slots", {})
            row = slots.get(str(use_level))
            if not isinstance(row, dict):
                row = {"max": 1, "used": 0}
                slots[str(use_level)] = row
            row["used"] = _as_int(row.get("used"), 0) + 1
            scheduler.spend_budget(caster, spells.required_slot(spell), 1) if scheduler.budget_of(caster) else None
            notes = [f"{caster_name} 的 {spell} 被 {countered_by} 反制，法术位不退还但效果未产生。"]
            result = {
                "countered": True,
                "countered_by": countered_by,
                "caster": caster_name,
                "spell": spell,
                "summary": notes[0],
                "counter_spell": counter_record,
            }
            _append_log(combat, notes[0], context)
            _save_combat(context, combat)
            from trpg_server.workflows import concentration

            concentration.sync(room_dir, concentration.holders_from_combat(combat))
            _finish_cast_steps(flow, result, countered=True)
            return result
        # 反制失败：原法术照常结算。把检定结果留在流程元数据里，供最终结果与复盘。
        _append_log(
            combat,
            f"{countered_by} 的反制检定失败（DC {counter_record.get('dc')}），{caster_name} 的 {spell} 照常生效。",
            context,
        )
        flow["metadata"] = {**metadata, "counter_spell": counter_record}

    # 反制窗口结束后才轮到豁免：把待决的 save:<目标> 步骤挂起，等当事人决定。
    pending_saves = [
        step
        for step in flow.get("steps") or []
        if isinstance(step, dict)
        and str(step.get("id") or "").startswith(reactions.SAVE_PREFIX)
        and str(step.get("status")) == "pending"
    ]
    if pending_saves:
        from trpg_server.workflows.model import stamp

        events = flow.setdefault("events", [])
        for step in pending_saves:
            step["status"] = "waiting"
            step["started_at"] = step.get("started_at") or stamp()
            events.append(
                {
                    "seq": len(events) + 1,
                    "at": stamp(),
                    "type": "step.waiting",
                    "actor": caster_name,
                    "payload": {"step": step.get("id"), "policy": step.get("policy")},
                }
            )
        return {
            "status": "awaiting_save",
            "awaiting": [reactions.save_target_of(str(step.get("id"))) for step in pending_saves],
            "counter_spell": (flow.get("metadata") or {}).get("counter_spell"),
        }

    target_cards = {
        str(target.get("name") or ""): _player_card(target, context) for target in targets
    }
    result = _apply_combat_cast(
        context,
        combat,
        caster,
        spell,
        targets,
        target_cards,
        slot_level,
        save_results=_save_results(flow),
    )
    if result.get("error"):
        raise RuntimeError(str(result.get("error")))
    counter_record = (flow.get("metadata") or {}).get("counter_spell")
    if countered_by:
        # 反制者出手但检定失败：原法术成功结算，结果里保留检定明细。
        result["countered"] = False
        result["countered_by"] = countered_by
        result["counter_spell"] = counter_record
    _finish_cast_steps(flow, result)
    return result


from trpg_server.workflows import continuations

continuations.register("dnd5e.cast_after_reaction", _cast_after_reaction)


def cast_spell(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """施放法术：战斗中校验预算与法术位；战斗外走房间级结算。

    真正的状态变更由服务端工作流执行：同一次施法带流程 id、步骤与 TTL，
    重复提交（同一个 ``request_id``）直接返回上次结果，不会二次扣法术位或伤害。
    """
    combat = _load_combat(context)
    request_key = _as_text(
        arguments.get("request_id")
        or arguments.get("idempotency_key")
        or arguments.get("requestId")
    )
    if not combat:
        result = room_spells.cast(
            context.room_dir,
            context.room_info(),
            caster=arguments.get("caster"),
            spell=arguments.get("spell") or arguments.get("spell_id"),
            targets=arguments.get("targets"),
            slot_level=arguments.get("slot_level"),
            ritual=bool(arguments.get("ritual")),
            context=context,
            rng=_roller,
            request_key=request_key,
        )
        _attach_spell_dice_message(result)
        return result
    _ruleset, spells, error = _require_spells(combat, context)
    if error:
        return error
    if arguments.get("ritual"):
        return {"error": "仪式施法只能在战斗外使用"}
    # 幂等重试先于预算/法术位校验：第一次施法已经花掉额度，重试应返回原结果。
    existing = prior_flow(getattr(context, "room_dir", None), request_key, STEP_RESOLVE)
    if existing is not None:
        flow_existing, cached = existing
        if isinstance(cached, dict):
            return {
                **_combat_summary_state(combat),
                **cached,
                "workflow": {
                    "id": flow_existing.get("id"),
                    "status": flow_existing.get("status"),
                    "duplicate": True,
                },
            }
        awaiting = [
            str(step.get("id")).split(":", 1)[1]
            for step in flow_existing.get("steps") or []
            if str(step.get("id") or "").startswith(("reaction:", "save:"))
            and str(step.get("status")) == "waiting"
        ]
        return {
            **_combat_summary_state(combat),
            "status": str(flow_existing.get("status") or ""),
            "awaiting": awaiting,
            "summary": "该施法正在等待反应决定，重复提交不会再次结算。",
            "workflow": {
                "id": flow_existing.get("id"),
                "status": flow_existing.get("status"),
                "duplicate": True,
            },
        }
    caster = _resolve_combatant(combat, arguments.get("caster")) or _current_actor(combat)
    if caster is None:
        return {"error": "当前没有可施法的战斗者"}
    spell = _as_text(arguments.get("spell") or arguments.get("spell_id"))
    if not spell:
        return {"error": "缺少 spell"}
    slot = spells.required_slot(spell)
    scheduler = _scheduler(combat, context)
    budget = scheduler.budget_of(caster)
    if budget and slot in budget and _as_int(budget.get(slot), 0) <= 0:
        return {"error": f"{caster.get('name')} 的 {slot} 额度不足，无法施放 {spell}"}
    # 射程、目标限制与法术位都必须在创建流程之前校验；
    # 插件内部还会在扣法术位前再校验一次，防止并发期间状态变化。
    targets, target_cards, target_error = _resolve_targets(combat, caster, arguments, context)
    if target_error:
        return target_error
    from trpg_server.workflows.guards import validate_action

    blocked = validate_action(
        getattr(context, "room_dir", None),
        kind="cast",
        actor=str(caster.get("name") or ""),
        targets=[str(target.get("name") or "") for target in targets],
    )
    if blocked:
        return {"error": "；".join(blocked), "code": "workflow.blocked"}
    caster_name = str(caster.get("name") or "")

    def resolve(flow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        return _apply_combat_cast(
            context,
            combat,
            caster,
            spell,
            targets,
            target_cards,
            _as_int(arguments.get("slot_level"), 0) or None,
        )

    # 有反制候选者或需要玩家决定的豁免时，把它作为流程中的待决步骤，
    # 先不产生任何效果；两者共用同一套 waiting/TTL/幂等收尾。
    from trpg_server.workflows import reactions
    from trpg_server.workflows.engine import WorkflowEngine

    reactor_rows = _counter_candidates(combat, caster, spell)
    save_candidates = _save_candidates(spells, spell, targets)
    save_rows = reactions.save_steps(
        save_candidates,
        policy_overrides=WorkflowEngine(context.room_dir).policy(),
    )
    if reactor_rows or save_rows:
        window = reactions.open_cast_window(
            context.room_dir,
            room_id=str(getattr(context, "room_id", "") or ""),
            caster=caster_name,
            caster_type=executor_type_for(
                is_player=bool(caster.get("is_player")),
                agent_id=str(getattr(context, "agent_id", "") or ""),
            ),
            spell=spell,
            slot_level=_as_int(arguments.get("slot_level"), 0) or 1,
            targets=[str(target.get("name") or "") for target in targets],
            reactors=reactor_rows,
            save_steps=save_rows,
            ttl=60,
            request_key=request_key,
        )
        if window.get("error"):
            return {"error": window["error"], "code": window.get("code")}
        waiting = window.get("flow") or {}
        awaiting_reactions = [
            reactions.candidate_of(str(step.get("id")))
            for step in waiting.get("steps") or []
            if isinstance(step, dict)
            and str(step.get("id") or "").startswith(reactions.REACTION_PREFIX)
            and str(step.get("status")) == "waiting"
        ]
        awaiting_saves = [
            reactions.save_target_of(str(step.get("id")))
            for step in waiting.get("steps") or []
            if isinstance(step, dict)
            and str(step.get("id") or "").startswith(reactions.SAVE_PREFIX)
            and str(step.get("status")) == "waiting"
        ]
        parts: list[str] = []
        if awaiting_reactions:
            parts.append("、".join(awaiting_reactions) + " 的反应")
        if awaiting_saves:
            parts.append("、".join(awaiting_saves) + " 的豁免")
        return {
            **_combat_summary_state(combat),
            "status": "awaiting_reaction" if awaiting_reactions else "awaiting_save",
            "summary": f"{caster_name} 施放 {spell}，等待 " + " 与 ".join(parts) + "。",
            "awaiting": [*awaiting_reactions, *awaiting_saves],
            "awaiting_saves": awaiting_saves,
            "workflow": {
                "id": waiting.get("id"),
                "status": waiting.get("status"),
                "duplicate": False,
            },
        }

    outcome = run_flow_action(
        context.room_dir,
        kind="cast",
        room_id=str(getattr(context, "room_id", "") or ""),
        initiator=caster_name,
        initiator_type=executor_type_for(
            is_player=bool(caster.get("is_player")),
            agent_id=str(getattr(context, "agent_id", "") or ""),
        ),
        steps=action_steps(
            "cast",
            executor_type=str(caster.get("is_player") and "player" or "kp"),
        ),
        handlers={STEP_RESOLVE: resolve},
        request_key=request_key,
        metadata={"spell": spell, "slot_level": arguments.get("slot_level")},
    )
    if outcome.get("error"):
        return {"error": outcome["error"], "code": outcome.get("code")}
    flow = outcome.get("flow") or {}
    result = flow_result(flow, STEP_RESOLVE) or {}
    if not isinstance(result, dict):
        result = {"result": result}
    if result.get("error"):
        return result
    refreshed = _load_combat(context) or combat
    return {
        **_combat_summary_state(refreshed),
        **result,
        "workflow": {
            "id": flow.get("id"),
            "status": flow.get("status"),
            "duplicate": bool(outcome.get("duplicate")),
        },
    }


def concentration(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """查看或打断非战斗专注；damage > 0 时按插件规则做专注检定。"""
    if _load_combat(context):
        return {"error": "战斗中专注由伤害结算自动处理，无需手动调用"}
    return room_spells.concentration(
        context.room_dir,
        context.room_info(),
        member=arguments.get("member") or arguments.get("caster"),
        action=arguments.get("action") or "status",
        damage=arguments.get("damage"),
        reason=arguments.get("reason"),
        context=context,
        rng=_roller,
    )


SPELL_LIST_TOOL = AgentTool(
    name="spell.list",
    description=(
        "列出当前施法者已知/准备的法术、剩余法术位与可施放状态。"
        "仅在有法术能力的规则集（如 dnd5e）中可用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "caster": {"type": "string", "description": "省略则使用当前行动者"},
        },
    },
    handler=list_spells,
)

SPELL_CAST_TOOL = AgentTool(
    name="spell.cast",
    description=(
        "施法：战斗中校验行动预算与法术位；没有战斗时按房间成员结算。"
        "升环用 slot_level，仪式施法 ritual 仅战斗外且不消耗法术位。"
        "法术攻击/豁免、伤害或治疗、条件与专注均由规则集插件处理。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "caster": {"type": "string", "description": "省略则使用当前行动者"},
            "spell": {"type": "string", "description": "法术 id 或名称（含中文名）"},
            "slot_level": {"type": "integer", "minimum": 0, "description": "消耗的法术位环阶；戏法为 0"},
            "ritual": {"type": "boolean", "description": "以仪式施法（仅战斗外，不消耗法术位）"},
            "request_id": {
                "type": "string",
                "description": "幂等键；同一 request_id 重复提交只结算一次",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标战斗者/房间成员名称；自身法术可省略",
            },
        },
        "required": ["spell"],
    },
    handler=cast_spell,
)

SPELL_CONCENTRATION_TOOL = AgentTool(
    name="spell.concentration",
    description=(
        "查看或打断非战斗专注。action=status 查看；action=break 直接结束；"
        "传入 damage 时按规则集插件做专注检定（DC 由插件计算），检定失败会连带动效移除。"
        "只传 damage 不传 action 时按受伤处理。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "member": {"type": "string", "description": "角色名；省略则取默认成员"},
            "action": {
                "type": "string",
                "enum": ["status", "break", "damage"],
                "description": "默认 status；传 damage 或只给 damage 时做专注检定",
            },
            "damage": {"type": "integer", "minimum": 0, "description": "非战斗受到的伤害值"},
            "reason": {"type": "string", "description": "打断原因"},
        },
    },
    handler=concentration,
)

SPELL_TOOLS = [SPELL_LIST_TOOL, SPELL_CAST_TOOL, SPELL_CONCENTRATION_TOOL]
SPELL_TOOL_NAMES = [tool.name for tool in SPELL_TOOLS]
