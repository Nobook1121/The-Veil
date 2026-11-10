"""房间级法术与典籍：卡面扩展字段、MP/SAN/POW 结算与留痕。

规则数学在 :mod:`trpg_server.ruleset.coc7.magic`；本模块只做状态搬运：
- 卡面扩展写在 ``card["extensions"]["magic"]``，旧卡没有该字段时按空结构补上；
- MP/HP/SAN/POW 复用角色卡既有字段与资源槽位，不改 ``sheet`` 原有结构。
- 不内置法术表 / 典籍表：法术名、MP/SAN 消耗、典籍神话等级与收益均由 KP 传入。
"""
from __future__ import annotations

import time
from typing import Any

from trpg_server import room_clock, room_rules
from trpg_server.ruleset.coc7 import magic as magic_rules


def _error(message: str, code: str = "magic_error") -> room_rules.RuleOperationError:
    return room_rules.RuleOperationError(message, code=code)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _magic_block(card: dict[str, Any]) -> dict[str, Any]:
    """取（必要时创建）卡面魔法扩展；缺省为空列表 / 空字典，兼容旧卡。"""
    extensions = card.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        card["extensions"] = extensions
    block = extensions.get("magic")
    if not isinstance(block, dict):
        block = {}
        extensions["magic"] = block
    if not isinstance(block.get("spells"), list):
        block["spells"] = []
    if not isinstance(block.get("grimoires"), dict):
        block["grimoires"] = {}
    return block


def magic_snapshot(member: dict[str, Any]) -> dict[str, Any]:
    card = room_rules.member_card(member)
    block = _magic_block(card)
    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
    power = _as_int(attributes.get("pow"), 0)
    return {
        "spells": [str(item.get("name") if isinstance(item, dict) else item) for item in block["spells"]],
        "grimoires": sorted(block["grimoires"].keys()),
        "pow": power,
        "max_mp": _as_int(state.get("max_mp"), magic_rules.max_mp(power)),
        "current_mp": _as_int(
            state.get("current_mp", card.get("currentMp")), magic_rules.max_mp(power)
        ),
    }


def needs_first_cast(member: dict[str, Any], spell: Any) -> bool:
    """该法术是否还没完成过首次施法检定。"""
    name = " ".join(str(spell or "").split())[:60]
    card = room_rules.member_card(member)
    for item in _magic_block(card)["spells"]:
        if isinstance(item, dict) and item.get("name") == name:
            return not bool(item.get("first_cast_done"))
    return False


def learn_spell(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    name: Any,
    *,
    weeks: Any = 0,
    source: str = "book",
    actor: Any = None,
) -> dict[str, Any]:
    """把法术记入卡面扩展；耗时由调用方显式给出（2D6 周或教师 1D8 天）。"""
    spell = " ".join(str(name or "").split())[:60]
    if not spell:
        raise _error("法术名不能为空", code="invalid_spell")
    card = room_rules.member_card(member)
    block = _magic_block(card)
    existing = next(
        (item for item in block["spells"] if isinstance(item, dict) and item.get("name") == spell),
        None,
    )
    if existing is None:
        existing = {"name": spell, "learned_weeks": max(0, _as_int(weeks)), "source": str(source)}
        block["spells"].append(existing)
    else:
        existing["learned_weeks"] = max(0, _as_int(weeks))
        existing["source"] = str(source)
    existing["first_cast_done"] = bool(existing.get("first_cast_done"))
    room_rules.save_room_info(room_dir, info)
    return {"spell": spell, "weeks": existing["learned_weeks"], "spells": magic_snapshot(member)["spells"]}


def _apply_hp(room_dir: Any, info: dict[str, Any], member: dict[str, Any], delta: Any) -> dict[str, int]:
    card = room_rules.member_card(member)
    state = member.setdefault("character_state", {})
    derived = room_rules.derive_card(card)
    max_hp = _as_int(state.get("max_hp") or derived.get("maxHp") or card.get("maxHp"), 1) or 1
    before = _as_int(state.get("current_hp", card.get("currentHp")), max_hp)
    after = max(0, min(max_hp, before + _as_int(delta)))
    state["max_hp"] = max_hp
    state["current_hp"] = after
    state["pending_persist"] = True
    card["currentHp"] = after
    card["current_hp"] = after
    room_rules.save_room_info(room_dir, info)
    return {"before": before, "after": after, "delta": after - before}


def _apply_mp(room_dir: Any, info: dict[str, Any], member: dict[str, Any], delta: Any) -> dict[str, int]:
    card = room_rules.member_card(member)
    state = member.setdefault("character_state", {})
    attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
    max_mp = _as_int(state.get("max_mp"), magic_rules.max_mp(attributes.get("pow")))
    before = _as_int(state.get("current_mp", card.get("currentMp")), max_mp)
    after = max(0, min(max_mp, before + _as_int(delta)))
    state["max_mp"] = max_mp
    state["current_mp"] = after
    state["pending_persist"] = True
    card["currentMp"] = after
    card["maxMp"] = max_mp
    room_rules.save_room_info(room_dir, info)
    return {"before": before, "after": after, "delta": after - before}


def recover_mp(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    days: Any,
) -> dict[str, Any]:
    """按天恢复 MP：每小时 1 点（POW 每满 100 额外 +1），不超过上限。"""
    days_value = max(0, _as_int(days))
    card = room_rules.member_card(member)
    state = member.setdefault("character_state", {})
    attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
    max_mp = _as_int(state.get("max_mp"), magic_rules.max_mp(attributes.get("pow")))
    before = _as_int(state.get("current_mp", card.get("currentMp")), max_mp)
    gain = magic_rules.mp_recovery(days_value * 24, attributes.get("pow"))
    after = max(0, min(max_mp, before + gain))
    state["max_mp"] = max_mp
    state["current_mp"] = after
    state["pending_persist"] = True
    card["currentMp"] = after
    card["maxMp"] = max_mp
    room_rules.record_room_changes(
        member,
        [
            {
                "type": "mp",
                "value": after,
                "source": "rule_command",
                "reason": f"按天恢复 MP {days_value} 天",
            }
        ],
    )
    room_rules.save_room_info(room_dir, info)
    return {
        "days": days_value,
        "before": before,
        "after": after,
        "max_mp": max_mp,
        "gain": after - before,
    }


def _apply_pow(room_dir: Any, info: dict[str, Any], member: dict[str, Any], delta: Any) -> dict[str, int]:
    card = room_rules.member_card(member)
    attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
    if not isinstance(card.get("attributes"), dict):
        card["attributes"] = attributes
    before = _as_int(attributes.get("pow"), 0)
    after = max(0, before + _as_int(delta))
    attributes["pow"] = after
    room_rules.save_room_info(room_dir, info)
    return {"before": before, "after": after, "delta": after - before}


def _mark_first_cast(member: dict[str, Any], spell: str) -> None:
    card = room_rules.member_card(member)
    for item in _magic_block(card)["spells"]:
        if isinstance(item, dict) and item.get("name") == spell:
            item["first_cast_done"] = True
            return


def cast_spell(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: Any,
    mp_cost: Any = 0,
    san_cost: Any = 0,
    pow_cost: Any = 0,
    first_cast: bool = False,
    success_level: str | None = None,
    pushed: bool = False,
    multiplier: Any = 1,
    side_effect_roll: Any = 1,
    actor: Any = None,
    actor_id: Any = None,
) -> dict[str, Any]:
    """结算一次施法：支付 MP/SAN，首次施法做困难 POW 检定，孤注一掷失败加倍。"""
    spell = " ".join(str(name or "").split())[:60]
    if not spell:
        raise _error("法术名不能为空", code="invalid_spell")
    card = room_rules.member_card(member)
    block = _magic_block(card)
    known = next(
        (item for item in block["spells"] if isinstance(item, dict) and item.get("name") == spell),
        None,
    )
    if known is None:
        raise _error(f"角色尚未学会「{spell}」，请先用 rule spell learn 登记", code="spell_not_known")

    need_check = bool(first_cast or not known.get("first_cast_done"))
    level = str(success_level or "").lower()
    if need_check and not level:
        raise _error("首次施法需要困难 POW 检定结果", code="magic_need_result")
    success = magic_rules.is_success(level) if need_check else True
    effects = success or bool(pushed)

    mp_details = {"mp": 0, "hp": 0}
    if effects:
        state = member.setdefault("character_state", {})
        attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
        current_mp = _as_int(state.get("current_mp", card.get("currentMp")), 0)
        current_hp = _as_int(state.get("current_hp", card.get("currentHp")), 1)
        split = magic_rules.mp_cost_split(mp_cost, current_mp, current_hp)
        mp_details = {"mp": split["mp"], "hp": split["hp"]}
        if split["mp"]:
            _apply_mp(room_dir, info, member, -split["mp"])
        if split["hp"]:
            _apply_hp(room_dir, info, member, -split["hp"])
        if _as_int(san_cost):
            room_rules.apply_san_change(
                room_dir,
                info,
                member,
                delta=-abs(_as_int(san_cost)),
                reason=f"施放「{spell}」",
                actor=actor,
                actor_id=actor_id,
                source="magic",
            )
        if _as_int(pow_cost):
            _apply_pow(room_dir, info, member, -abs(_as_int(pow_cost)))

    extra_cost = 0
    side_effect = ""
    if need_check and not success and pushed:
        multiplied = magic_rules.push_multiplier(multiplier)
        extra_mp = magic_rules.pushed_cost(mp_cost, multiplied)
        extra_san = magic_rules.pushed_cost(san_cost, multiplied)
        extra_pow = magic_rules.pushed_cost(pow_cost, multiplied)
        extra_cost = extra_mp + extra_san + extra_pow
        if extra_mp:
            state = member.setdefault("character_state", {})
            current_mp = _as_int(state.get("current_mp", card.get("currentMp")), 0)
            current_hp = _as_int(state.get("current_hp", card.get("currentHp")), 1)
            split = magic_rules.mp_cost_split(extra_mp, current_mp, current_hp)
            if split["mp"]:
                _apply_mp(room_dir, info, member, -split["mp"])
            if split["hp"]:
                _apply_hp(room_dir, info, member, -split["hp"])
        if extra_san:
            room_rules.apply_san_change(
                room_dir,
                info,
                member,
                delta=-abs(extra_san),
                reason=f"孤注一掷施放「{spell}」的反噬",
                actor=actor,
                actor_id=actor_id,
                source="magic",
            )
        if extra_pow:
            _apply_pow(room_dir, info, member, -abs(extra_pow))
        side_effect = magic_rules.push_side_effect(side_effect_roll)

    if effects:
        _mark_first_cast(member, spell)
        card["extensions"]["magic"]["last_cast"] = {
            "spell": spell,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        room_rules.save_room_info(room_dir, info)

    return {
        "spell": spell,
        "success": success,
        "effects": effects,
        "first_cast_check": need_check,
        "success_level": level,
        "pushed": bool(need_check and not success and pushed),
        "mp_spent": mp_details["mp"],
        "hp_spent": mp_details["hp"],
        "extra_cost": extra_cost,
        "side_effect": side_effect,
        "magic": magic_snapshot(member),
    }


def read_grimoire(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    title: Any,
    mode: str = "study",
    weeks: Any = None,
    san_loss: Any = 0,
    cmi: Any = 0,
    cmf: Any = 0,
    rating: Any = 0,
    success_level: str | None = None,
    spells: list[Any] | None = None,
    actor: Any = None,
    actor_id: Any = None,
) -> dict[str, Any]:
    """泛读 / 精读一本典籍。

    ``weeks`` 显式给出时优先使用；未给出且房间已启用游戏时钟时，用
    「当前游戏时间 − 上次阅读时间」的整周数代替，不凭空补时间。
    """
    book = " ".join(str(title or "").split())[:80]
    if not book:
        raise _error("典籍名不能为空", code="invalid_grimoire")
    mode_value = str(mode or "study").strip().lower()
    if mode_value not in {"skim", "study", "泛读", "精读"}:
        raise _error("阅读方式只能是 skim / study", code="invalid_mode")
    skim = mode_value in {"skim", "泛读"}

    card = room_rules.member_card(member)
    block = _magic_block(card)
    record = block["grimoires"].get(book)
    if not isinstance(record, dict):
        record = {"reads": 0, "weeks": 0, "mythos_gain": 0, "san_loss": 0}
        block["grimoires"][book] = record

    if skim:
        if not magic_rules.is_success(success_level):
            return {
                "book": book,
                "mode": "skim",
                "success": False,
                "mythos_gain": 0,
                "san_loss": 0,
                "spells": [],
            }
        gain = max(0, _as_int(cmi))
        loss = max(0, _as_int(san_loss))
        record["skimmed"] = True
    else:
        gain = magic_rules.grimoire_mythos_gain(
            mythos=_mythos_value(member), rating=rating, cmi=cmi, cmf=cmf
        )
        loss = max(0, _as_int(san_loss))
        record["reads"] = _as_int(record.get("reads")) + 1
        auto_weeks = weeks is None
        weeks_value = (
            room_clock.clock_weeks_since(info, record.get("last_read_at"))
            if auto_weeks
            else _as_int(weeks)
        )
        record["weeks"] = max(0, _as_int(weeks_value))
        clock = room_clock.read_clock(info)
        if clock is not None:
            record["last_read_at"] = clock["current"]

    if gain:
        _add_mythos(room_dir, info, member, gain, book=book, actor=actor, actor_id=actor_id)
    if loss:
        room_rules.apply_san_change(
            room_dir,
            info,
            member,
            delta=-loss,
            reason=f"阅读典籍《{book}》",
            actor=actor,
            actor_id=actor_id,
            source="magic",
        )
    record["mythos_gain"] = _as_int(record.get("mythos_gain")) + gain
    record["san_loss"] = _as_int(record.get("san_loss")) + loss
    record["last_mode"] = "skim" if skim else "study"
    learned = [str(item) for item in (spells or []) if str(item).strip()]
    if learned:
        known = {item.get("name") for item in block["spells"] if isinstance(item, dict)}
        for spell in learned:
            if spell not in known:
                block["spells"].append(
                    {"name": spell, "learned_weeks": 0, "source": f"典籍《{book}》", "first_cast_done": False}
                )
    room_rules.save_room_info(room_dir, info)
    return {
        "book": book,
        "mode": "skim" if skim else "study",
        "success": True,
        "mythos_gain": gain,
        "san_loss": loss,
        "weeks": _as_int(record.get("weeks")),
        "reads": _as_int(record.get("reads")),
        "spells": magic_snapshot(member)["spells"],
    }


def _mythos_value(member: dict[str, Any]) -> int:
    card = room_rules.member_card(member)
    for row in card.get("skills") or []:
        if isinstance(row, dict) and str(row.get("name") or "").strip() == "克苏鲁神话":
            return _as_int(row.get("value"))
    return 0


def _add_mythos(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    gain: int,
    *,
    book: str,
    actor: Any = None,
    actor_id: Any = None,
) -> None:
    card = room_rules.member_card(member)
    if not any(
        isinstance(row, dict) and str(row.get("name") or "").strip() == "克苏鲁神话"
        for row in card.get("skills") or []
    ):
        card.setdefault("skills", []).append({"name": "克苏鲁神话", "value": 0, "base": 0})
    room_rules.apply_skill_change(
        room_dir,
        info,
        member,
        name="克苏鲁神话",
        delta=gain,
        reason=f"阅读典籍《{book}》",
        actor=actor,
        actor_id=actor_id,
        source="magic",
    )


def format_magic(member: dict[str, Any]) -> str:
    snapshot = magic_snapshot(member)
    lines = [
        f"法术：{'、'.join(snapshot['spells']) if snapshot['spells'] else '（无）'}",
        f"典籍：{'、'.join(snapshot['grimoires']) if snapshot['grimoires'] else '（无）'}",
        f"MP {snapshot['current_mp']}/{snapshot['max_mp']} · POW {snapshot['pow']}",
    ]
    return "\n".join(lines)
