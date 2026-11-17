"""房间级休息与调谐入口（规则集中立）。

核心只负责：挑选成员、调用插件的休息/调谐能力、把结果写回成员状态并落盘。
具体数值（生命骰、恢复比例、同调上限、是否需要休息）全部由插件声明。
没有 ``rest`` 能力的规则集（如 CoC7）调用这里会得到明确错误，且不会下发入口。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server import room_rules
from trpg_server.ruleset.rest import RestMemberResult, rest_rules_of


def rest_available(info: Mapping[str, Any]) -> bool:
    from trpg_server.room_spells import ruleset_for

    plugin, _settings = ruleset_for(info)
    return rest_rules_of(plugin) is not None


def _target_members(info: dict[str, Any], names: Any) -> list[dict[str, Any]]:
    wanted = (
        {str(item).strip().casefold() for item in names if str(item).strip()}
        if isinstance(names, (list, tuple, set))
        else set()
    )
    members: list[dict[str, Any]] = []
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        if not isinstance(member.get("character_card"), Mapping):
            continue
        if wanted:
            card = member.get("character_card") or {}
            candidates = {
                str(member.get("username") or "").strip().casefold(),
                str(card.get("name") or "").strip().casefold(),
            }
            if not (candidates & wanted):
                continue
        members.append(member)
    return members


def take_rest(
    room_dir: Any,
    info: dict[str, Any],
    *,
    kind: str = "short",
    hit_dice: int = 1,
    members: Any = None,
    rng: Any = None,
    actor: Any = None,
    actor_id: Any = None,
) -> dict[str, Any]:
    """结算一次短休或长休；返回每个成员的结果。"""
    from trpg_server import room_spells

    plugin, settings = room_spells.ruleset_for(info)
    rest = rest_rules_of(plugin)
    if rest is None:
        raise room_rules.RuleOperationError(
            "当前规则集未提供休息能力", code="no_rest"
        )
    normalized = str(kind or "").strip().lower()
    if normalized not in {"short", "long"}:
        raise room_rules.RuleOperationError(
            f"未知的休息类型“{kind}”，只支持 short / long", code="rest_kind_invalid"
        )
    targets = _target_members(info, members)
    if not targets:
        raise room_rules.RuleOperationError("房间里没有可结算休息的成员", code="no_members")

    rows: list[dict[str, Any]] = []
    for member in targets:
        card = room_rules.member_card(member)
        holder, _card, _state = room_spells._prepare_holder(member, plugin, settings)
        result = RestMemberResult(name=str(holder.get("name") or ""), kind=normalized)
        try:
            if normalized == "short":
                outcome = rest.short_rest(
                    holder,
                    card,
                    hit_dice=max(0, int(hit_dice)),
                    rng=rng,
                    settings=settings,
                )
            else:
                outcome = rest.long_rest(holder, card, rng=rng, settings=settings)
        except ValueError as exc:
            result.error = str(exc)
            rows.append(result.to_dict())
            continue
        except Exception as exc:  # noqa: BLE001 - 单个成员失败不阻断其它成员
            result.error = str(exc)
            rows.append(result.to_dict())
            continue
        room_spells._sync_holder(member, holder, plugin)
        if isinstance(holder.get("hit_dice"), Mapping):
            sheet = card.get("sheet")
            if not isinstance(sheet, dict):
                sheet = {}
                card["sheet"] = sheet
            sheet["hit_dice"] = dict(holder["hit_dice"])
        result.hp_before = int(outcome.get("hp_before", 0))
        result.hp_after = int(outcome.get("hp_after", 0))
        result.healed = int(outcome.get("healed", 0))
        result.hit_dice_spent = int(outcome.get("hit_dice_spent", 0))
        result.hit_dice_regained = int(outcome.get("hit_dice_regained", 0))
        result.slots_restored = bool(outcome.get("slots_restored"))
        result.exhaustion_level = int(outcome.get("exhaustion_level", 0))
        result.notes = [str(item) for item in outcome.get("notes") or []]
        rows.append(result.to_dict())

    room_rules.save_room_info(room_dir, info)
    try:
        room_rules._log(
            room_dir,
            info,
            f"{'长休' if normalized == 'long' else '短休'}结算",
            actor=actor,
            actor_id=actor_id,
            source="rest",
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响结算
        pass
    return {"kind": normalized, "profile": dict(rest.profile(settings)), "members": rows}


def _equipment_entries(sheet: Mapping[str, Any]) -> list[Any]:
    raw = sheet.get("equipment")
    if isinstance(raw, Mapping):
        raw = raw.get("items")
    return list(raw) if isinstance(raw, list) else []


def _item_id(sheet: Mapping[str, Any], item: Any) -> str:
    key = str(item or "").strip().casefold()
    if not key:
        return ""
    for entry in _equipment_entries(sheet):
        if isinstance(entry, str):
            if entry.strip().casefold() == key:
                return entry.strip()
        elif isinstance(entry, Mapping):
            identifier = str(entry.get("id") or entry.get("item_id") or entry.get("name") or "").strip()
            if identifier.casefold() == key:
                return identifier
    return ""


def _attuned(sheet: Mapping[str, Any]) -> list[str]:
    raw = sheet.get("attunement")
    if isinstance(raw, Mapping):
        raw = raw.get("items")
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _save_attunement(room_dir: Any, info: dict[str, Any], sheet: dict[str, Any], items: list[str]) -> None:
    sheet["attunement"] = list(items)
    room_rules.save_room_info(room_dir, info)


def attune(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    item: Any,
    *,
    during_rest: bool = False,
) -> dict[str, Any]:
    """建立同调；未在休息期间调用时按插件声明拒绝。"""
    from trpg_server.room_spells import ruleset_for

    plugin, settings = ruleset_for(info)
    rest = rest_rules_of(plugin)
    if rest is None:
        raise room_rules.RuleOperationError("当前规则集未提供同调能力", code="no_attunement")
    card = room_rules.member_card(member)
    sheet = card.get("sheet")
    if not isinstance(sheet, dict):
        sheet = {}
        card["sheet"] = sheet
    item_id = _item_id(sheet, item)
    if not item_id:
        raise room_rules.RuleOperationError(f"找不到装备“{item}”", code="item_not_found")
    if not rest.requires_attunement(sheet, item_id):
        raise room_rules.RuleOperationError(
            f"“{item_id}”不需要同调", code="attunement_not_required"
        )
    current = _attuned(sheet)
    if item_id in current:
        return {"item": item_id, "attuned": current, "already": True}
    limit = max(0, int(rest.attunement_limit(settings)))
    if limit and len(current) >= limit:
        raise room_rules.RuleOperationError(
            f"同调数量已达上限 {limit}", code="attunement_limit"
        )
    if rest.attunement_requires_rest() and not during_rest:
        raise room_rules.RuleOperationError(
            "建立同调需要一次短休或长休", code="attunement_requires_rest"
        )
    current.append(item_id)
    _save_attunement(room_dir, info, sheet, current)
    return {"item": item_id, "attuned": list(current), "already": False}


def unattune(room_dir: Any, info: dict[str, Any], member: dict[str, Any], item: Any) -> dict[str, Any]:
    """解除同调；立即生效。"""
    card = room_rules.member_card(member)
    sheet = card.get("sheet")
    if not isinstance(sheet, dict):
        sheet = {}
        card["sheet"] = sheet
    item_id = _item_id(sheet, item)
    current = _attuned(sheet)
    for name in list(current):
        if name.casefold() == str(item or "").strip().casefold() or name == item_id:
            current.remove(name)
    _save_attunement(room_dir, info, sheet, current)
    return {"item": item_id or str(item or ""), "attuned": list(current)}


__all__ = ["attune", "rest_available", "take_rest", "unattune"]
