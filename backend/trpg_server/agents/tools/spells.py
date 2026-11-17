"""法术工具（规则中立命名）。

``spell.list`` / ``spell.cast`` 只负责解析角色、校验行动预算、把请求转给当前
规则集的 ``spells`` 能力，并写回战斗或房间状态；法术位、升环、攻击/豁免、伤害
与专注全部在插件侧。没有 ``spells`` 能力的规则集会在下发阶段被过滤掉这些工具。
没有战斗时，施法与专注走 :mod:`trpg_server.room_spells` 的房间级结算。
"""

from __future__ import annotations

from typing import Any

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


def cast_spell(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """施放法术：战斗中校验预算与法术位；战斗外走房间级结算。"""
    combat = _load_combat(context)
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
        )
        _attach_spell_dice_message(result)
        return result
    _ruleset, spells, error = _require_spells(combat, context)
    if error:
        return error
    if arguments.get("ritual"):
        return {"error": "仪式施法只能在战斗外使用"}
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
    targets, target_cards, target_error = _resolve_targets(combat, caster, arguments, context)
    if target_error:
        return target_error
    result = spells.cast(
        caster,
        _player_card(caster, context),
        spell=spell,
        targets=targets,
        slot_level=_as_int(arguments.get("slot_level"), 0) or None,
        target_cards=target_cards,
        rng=_roller,
        combat=combat,
        settings=_combat_settings(context),
    )
    if result.get("error"):
        return result
    result["caster"] = caster.get("name")
    _attach_spell_dice_message(result)
    if budget and slot in budget:
        scheduler.spend_budget(caster, slot, 1)
    _append_log(combat, str(result.get("summary") or f"{caster.get('name')} 施放 {spell}。"), context)
    _save_combat(context, combat)
    return {**_combat_summary_state(combat), **result}


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
