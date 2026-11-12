"""房间内规则操作（不依赖 AI KP）。

聊天斜杠命令（``/sc``、``/item``）、房间控制台 ``rule`` 域与 KP 工具共用本模块，
保证同一次检定 / SAN 变更 / 物品写入只有一套数值与留痕逻辑。

边界（便于后续整体插件化）：
- 规则判定复用 ``ruleset`` 插件提供的规则集标识与 ``agents/tools/dice`` 的兼容桥；
  本模块不新增规则公式，只负责成员定位、权限边界、落盘与留痕。
- 所有函数显式接收 ``room_dir`` / ``info``，不依赖 Flask 请求上下文，便于测试与
  其他规则集接入时替换实现。
"""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.room_log import log_room_event

logger = logging.getLogger(__name__)

# 留痕记录上限，避免房间快照无限增长。
MAX_RECORDS = 100
ITEM_KINDS = {"item", "weapon"}
ITEM_KIND_ALIASES = {
    "item": "item",
    "物品": "item",
    "道具": "item",
    "equipment": "item",
    "weapon": "weapon",
    "武器": "weapon",
}
OPERATOR_ROLES = {"owner", "admin", "sysadmin", "system"}

# 统一的待写回队列：SAN / 技能 / 属性 / 物品都追加到这里，归档时只判断这一处。
PENDING_CHANGES_KEY = "pending_changes"


class RuleOperationError(Exception):
    """规则操作失败（消息可直接展示给玩家）。"""

    def __init__(self, message: str, *, code: str = "rule_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class RoomRuleContext:
    """``agents/tools/dice`` 需要的房间上下文适配器（内存 info + 落盘目录）。"""

    def __init__(
        self,
        room_dir: Path | None,
        info: dict[str, Any],
        *,
        room_id: Any = None,
        config_dir: Any = None,
        user_id: Any = None,
        username: Any = None,
        turn: int = 0,
    ):
        self.room_dir = Path(room_dir) if room_dir else None
        self.room_id = room_id
        self.config_dir = config_dir
        self.user_id = user_id
        self.username = username
        self.turn = turn
        self._info = info

    def room_info(self) -> dict[str, Any]:
        return self._info

    def save(self) -> None:
        if self.room_dir is not None:
            save_room_info(self.room_dir, self._info)


def load_room_info(room_dir: Any) -> dict[str, Any]:
    info = read_json(Path(room_dir) / "info.json", default={})
    return info if isinstance(info, dict) else {}


def save_room_info(room_dir: Any, info: dict[str, Any]) -> None:
    """房间 ``info.json`` 的唯一整份写入口。

    写入方通常先读后改再整份写回；这里在房间锁内重读磁盘，把期间被时钟推进过的
    ``game_clock`` 合并回快照再落盘，避免旧快照把游戏时间覆盖回去。
    """
    from trpg_server import room_clock

    with room_clock.room_lock(room_dir):
        room_clock.preserve_stored_clock(room_dir, info)
        info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json_atomic(Path(room_dir) / "info.json", info)


def resolve_rule_id(info: dict[str, Any] | None, *, scenarios_dir: Any = None) -> str:
    """解析房间使用的规则集标识。

    优先级：房间显式 ``ruleset_id`` > 剧本 ``ruleset_id`` > 默认规则集。
    旧房间没有该字段时按剧本推断，仍推断不出则回退 coc7。
    """
    from trpg_server.ruleset import get_plugin

    data = info or {}
    explicit = str(data.get("ruleset_id") or "").strip()
    if explicit:
        return get_plugin(explicit).id
    scenario_id = str(data.get("scenario_id") or "").strip()
    if scenario_id and scenarios_dir:
        try:
            from trpg_server.scenario_store import load_scenario_by_id

            _, scenario = load_scenario_by_id(
                scenarios_dir,
                scenario_id,
                scenario_version=data.get("scenario_version"),
            )
            inferred = str((scenario or {}).get("ruleset_id") or "").strip()
            if inferred:
                return get_plugin(inferred).id
        except Exception:  # pragma: no cover - 剧本缺失时按默认规则集处理
            logger.debug("Failed to infer ruleset from scenario %s", scenario_id, exc_info=True)
    return get_plugin(None).id


def room_rule_label(info: dict[str, Any] | None, *, scenarios_dir: Any = None) -> str:
    from trpg_server.ruleset import get_plugin

    plugin = get_plugin(resolve_rule_id(info, scenarios_dir=scenarios_dir))
    return f"{plugin.display_name}（{plugin.id}）"


def _actor_rank_value(rank: Any) -> int:
    if isinstance(rank, (int, float)) and not isinstance(rank, bool):
        return int(rank)
    text = str(rank or "").strip().lower()
    if text in OPERATOR_ROLES:
        return 3
    if text in {"member", "player", "user"}:
        return 1
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def actor_can_operate(actor_id: Any, actor_rank: Any, member: dict[str, Any]) -> bool:
    """本人可操作自己的卡；指定他人须房主 / 房间管理员 / 系统管理员。"""
    if _actor_rank_value(actor_rank) >= 2:
        return True
    return str(member.get("user_id") or "") == str(actor_id or "")


def member_card(member: dict[str, Any]) -> dict[str, Any]:
    card = member.get("character_card")
    if not isinstance(card, dict):
        raise RuleOperationError("该成员没有绑定角色卡，无法执行规则操作", code="no_card")
    return card


def member_display_name(member: dict[str, Any]) -> str:
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    return str(card.get("name") or member.get("character_name") or member.get("username") or "未命名角色")


def apply_natural_healing(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    days: int,
    reason: str = "自然恢复",
    source: str = "rule_command",
) -> dict[str, int]:
    """按休养天数结算自然恢复；恢复量由规则集插件提供，本函数只搬状态。"""
    from trpg_server.ruleset import get_plugin
    from trpg_server.ruleset.capabilities import combat_rules_of

    rules = combat_rules_of(get_plugin(resolve_rule_id(info)))
    if rules is None:
        raise RuleOperationError("当前规则集未提供自然恢复能力", code="no_natural_healing")
    card = member_card(member)
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    max_hp = (
        _as_int(state.get("max_hp"), 0)
        or _as_int(card.get("maxHp"), 0)
        or _as_int(card.get("max_hp"), 0)
        or _as_int(card.get("hp"), 1)
    )
    max_hp = max(1, max_hp)
    current = _as_int(state.get("current_hp"), _as_int(card.get("currentHp"), max_hp))
    before = max(0, min(max_hp, current))
    status = card.get("status") if isinstance(card.get("status"), dict) else {}
    major_wound = bool(state.get("major_wound") or status.get("majorWound"))
    amount = rules.natural_healing(days, major_wound=major_wound)
    after = min(max_hp, before + amount)
    if after != before:
        state["max_hp"] = max_hp
        state["current_hp"] = after
        card["currentHp"] = after
        card["current_hp"] = after
        if after > 0:
            state["dying"] = False
            state["stable"] = True
        record_room_changes(
            member,
            [
                {
                    "type": "hp",
                    "value": after,
                    "source": source,
                    "reason": reason,
                }
            ],
        )
        save_room_info(room_dir, info)
    return {"before": before, "after": after, "max_hp": max_hp, "amount": after - before}


def member_ref(member: dict[str, Any]) -> str:
    """给 dice 工具使用的稳定引用（登录名优先，其次角色名）。"""
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    return str(member.get("username") or member.get("character_name") or card.get("name") or "")


def _match_member(member: dict[str, Any], needle: str) -> bool:
    if not isinstance(member, dict):
        return False
    if member.get("is_active", True) is False and str(member.get("status") or "active") == "removed":
        return False
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    values = {
        str(member.get("user_id") or ""),
        str(member.get("username") or ""),
        str(member.get("character_name") or ""),
        str(member.get("display_name") or ""),
        str(card.get("name") or ""),
    }
    return any(value and value.casefold() == needle for value in values)


def find_member(info: dict[str, Any], target: str) -> dict[str, Any] | None:
    needle = str(target or "").strip().casefold()
    if not needle:
        return None
    members = info.get("members") if isinstance(info.get("members"), list) else []
    for member in members:
        if _match_member(member, needle):
            return member
    return None


def resolve_member(
    info: dict[str, Any],
    target: Any,
    *,
    actor_id: Any = None,
    actor_rank: Any = None,
    fallback_to_actor: bool = False,
) -> dict[str, Any]:
    """按名字 / ID 定位房间成员，并做「本人或管理员」权限校验。"""
    text = str(target or "").strip()
    if fallback_to_actor or not text or text.casefold() in {"self", "me", "自己", "我"}:
        if actor_id in (None, ""):
            raise RuleOperationError("缺少操作目标：请指定玩家名", code="missing_target")
        member = find_member(info, str(actor_id))
        if member is None:
            raise RuleOperationError("你不在本房间的成员列表中", code="not_in_room", status=403)
    else:
        member = find_member(info, text)
        if member is None:
            raise RuleOperationError(f"房间里找不到玩家「{text}」", code="member_not_found", status=404)
    if not actor_can_operate(actor_id, actor_rank, member):
        raise RuleOperationError("只能操作自己的角色卡；操作他人需要房主 / 房间管理员权限", code="permission_denied", status=403)
    return member


def _plugin_for(card: dict[str, Any]):
    try:
        from trpg_server.ruleset import get_plugin

        return get_plugin((card or {}).get("rulesetId"))
    except Exception:  # noqa: BLE001 - 插件异常按无扩展能力处理
        return None


def derive_card(card: dict[str, Any]) -> dict[str, Any]:
    """按卡片所属规则集重算派生值；通用层只做调度，公式在插件内。"""
    if not isinstance(card, dict):
        return {}
    from trpg_server.ruleset.capabilities import derive_card as derive_with_plugin

    plugin = _plugin_for(card)
    if plugin is None:
        return dict(card)
    try:
        return derive_with_plugin(plugin, card)
    except Exception:  # noqa: BLE001 - 规则插件异常不应阻断读路径
        logger.debug("Failed to derive character card", exc_info=True)
        return dict(card)


def sync_derived_stats(
    member: dict[str, Any],
    *,
    source: str = "rule",
    record_id: str = "",
) -> dict[str, Any]:
    """属性 / 神话值变化后重算派生值，并同步房间快照与待写回队列。

    HP / MP / SAN 的当前值只做上限夹紧；实际被压回时才登记写回，避免无意义覆盖。
    """
    card = member_card(member)
    state = member["character_state"]
    derived = derive_card(card)
    plugin = _plugin_for(card)
    from trpg_server.ruleset.capabilities import derived_card_keys, resource_slots

    # 派生键与资源槽位由规则集声明；未声明时用核心默认槽位，保持旧卡行为不变。
    for key in derived_card_keys(plugin):
        if key in derived:
            card[key] = derived[key]

    slots = resource_slots(plugin)
    for slot in slots:
        cap_key = str(slot["cap_state"])
        cap_min = _as_int(slot.get("cap_min"), 0)
        previous_cap = _as_int(state.get(cap_key), cap_min)
        state[cap_key] = max(cap_min, _as_int(derived.get(str(slot["cap_derived"])), previous_cap))

    pressed: dict[str, int] = {}
    for slot in slots:
        kind = str(slot["kind"])
        state_key = str(slot["state"])
        card_key = str(slot["mirror"])
        cap = state[str(slot["cap_state"])]
        value = _as_int(state.get(state_key), _as_int(card.get(card_key), cap))
        value = max(0, min(cap, value))
        previous = state.get(state_key)
        if previous is None:
            # 首次初始化只写快照，不产生待写回记录（持久卡本身已有自己的当前值）。
            state[state_key] = value
            card[card_key] = value
            continue
        if _as_int(previous, -1) != value:
            state[state_key] = value
            card[card_key] = value
            _add_pending_change(
                state,
                {
                    "type": kind,
                    "value": value,
                    "source": source,
                    "reason": "派生值重算夹紧",
                    "record_id": record_id,
                },
            )
            pressed[kind] = value
    result: dict[str, Any] = {
        "mov": card.get("mov"),
        "build": card.get("build"),
        "damage_bonus": card.get("damageBonus"),
        "pressed": pressed,
    }
    for slot in slots:
        result[str(slot["cap_state"])] = state[str(slot["cap_state"])]
    return result


def _sanity_or_none(card: dict[str, Any]):
    from trpg_server.ruleset.capabilities import sanity_of

    return sanity_of(_plugin_for(card))


def _resource_slot(card: dict[str, Any], kind: str) -> dict[str, Any]:
    """取卡所属规则集声明的资源槽位；未声明时抛「不支持」而不是写死字段名。"""
    from trpg_server.ruleset.capabilities import resource_slot

    slot = resource_slot(_plugin_for(card), kind)
    if slot is None:
        raise RuleOperationError(f"当前规则集不支持{kind}", code=f"{kind}_unsupported")
    return slot


def san_snapshot(member: dict[str, Any]) -> dict[str, int]:
    """SAN 快照；上下限与镜像由规则集声明，本函数只负责调度。"""
    card = member_card(member)
    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    sanity = _sanity_or_none(card)
    if sanity is None:
        raise RuleOperationError("当前规则集不支持理智值", code="sanity_unsupported")
    return sanity.snapshot(card, derive_card(card), state, write=True)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _append_record(state: dict[str, Any], key: str, record: dict[str, Any]) -> None:
    records = state.get(key)
    if not isinstance(records, list):
        records = []
        state[key] = records
    records.insert(0, record)
    del records[MAX_RECORDS:]


def _add_pending_change(state: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """把一次房间改动追加到统一待写回队列。

    ``pending_persist`` 保留为粗粒度兼容标记；字段级信息在 ``pending_changes``，
    旧房间没有该队列时归档仍按 ``pending_persist`` / ``item_changes`` 兼容处理。
    同一字段的绝对值改动只保留最新一条，避免归档时重复计数。
    """
    records = state.get(PENDING_CHANGES_KEY)
    if not isinstance(records, list):
        records = []
        state[PENDING_CHANGES_KEY] = records
    change = _dedupe_pending_change(records, change)
    record = {
        "id": f"chg-{int(time.time() * 1000)}-{len(records)}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        **change,
    }
    records.insert(0, record)
    del records[MAX_RECORDS:]
    state["pending_persist"] = True
    return record


def pending_changes(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = state.get(PENDING_CHANGES_KEY) if isinstance(state, dict) else None
    return [row for row in records if isinstance(row, dict)] if isinstance(records, list) else []


def record_room_changes(member: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    """供 AI 工具等直接修改房间快照的路径登记待写回字段。"""
    state = member.get("character_state")
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    for change in changes or []:
        if isinstance(change, dict) and change.get("type"):
            _add_pending_change(state, change)


def _growth_capability(card: dict[str, Any]) -> Any:
    """按卡片规则集取成长能力；通用层不写死任何规则公式。"""
    try:
        from trpg_server.ruleset import get_plugin

        return getattr(get_plugin(card.get("rulesetId")), "growth", None)
    except Exception:  # noqa: BLE001 - 插件异常按无成长能力处理
        return None


def _training_capability(card: dict[str, Any]) -> Any:
    """按卡片规则集取幕间训练能力（纯计算模块）；无此能力按不可用处理。"""
    try:
        from trpg_server.ruleset import get_plugin

        return getattr(get_plugin(card.get("rulesetId")), "training", None)
    except Exception:  # noqa: BLE001 - 插件异常按无训练能力处理
        return None


def record_growth_mark_for_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """技能检定成功后立即打成长标记；属性 / 幕间内 / 奖励骰等不打勾。"""
    interlude = info.get("interlude") if isinstance(info.get("interlude"), dict) else {}
    if interlude.get("active"):
        return None
    # 调用方可能传入从另一份快照解析出的 member，先对齐到 info 内的同一对象，
    # 否则写入只会落在临时 dict 上，保存时丢失。
    members = info.get("members") if isinstance(info.get("members"), list) else []
    target = next((candidate for candidate in members if candidate is member), None)
    if target is None:
        target = next(
            (
                candidate
                for candidate in members
                if isinstance(candidate, dict)
                and str(candidate.get("user_id") or "") == str(member.get("user_id") or "")
                and str(candidate.get("username") or "") == str(member.get("username") or "")
            ),
            member,
        )
    card = target.get("character_card") if isinstance(target.get("character_card"), dict) else None
    if not card:
        return None
    row = _find_skill_row(card, str(result.get("name") or ""))
    if row is None:
        return None
    growth = _growth_capability(card)
    if growth is None:
        return None
    name = str(row.get("name") or result.get("name") or "").strip()
    if not growth.should_mark(
        name,
        row.get("value"),
        success=bool(result.get("success")),
        bonus_dice=_as_int(result.get("bonus_dice"), 0),
        luck_spent=bool(result.get("luck_spent")),
        opposed_failed=bool(result.get("opposed_failed")),
    ):
        return None
    from trpg_server import room_interlude

    return room_interlude.record_growth_mark(
        room_dir,
        info,
        target,
        skill=name,
        value=_as_int(row.get("value"), 0),
        success_level=str(result.get("success_level") or result.get("level") or ""),
        source="check",
    )


def apply_san_change(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    delta: int | None = None,
    value: int | None = None,
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "rule",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """直接修改 SAN（房间控制台专用）：夹紧范围、写镜像、留结构化记录。"""
    card = member_card(member)
    slot = _resource_slot(card, "san")
    snapshot = san_snapshot(member)
    before, maximum = snapshot["current"], snapshot["max"]
    target = value if value is not None else before + int(delta or 0)
    after = max(0, min(maximum, int(target)))
    applied = after - before
    state = member["character_state"]

    # 快照键与卡面镜像键均来自插件声明；旧卡同时保留同名字段，避免读取断档。
    state[str(slot["state"])] = after
    state["pending_persist"] = True
    card[str(slot["mirror"])] = after
    card[str(slot["state"])] = after
    loss = max(0, -applied)
    sanity = _sanity_or_none(card)
    if sanity is None:
        raise RuleOperationError("当前规则集不支持理智值", code="sanity_unsupported")
    day = str(info.get("game_date") or info.get("game_day") or "").strip() or time.strftime("%Y-%m-%d")
    outcome = sanity.resolve_loss(
        card=card,
        state=state,
        before=before,
        new_san=after,
        maximum=maximum,
        loss=loss,
        day=day,
        record_last=False,
        write_card=False,
    )
    daily = outcome.get("daily") or {}
    if outcome.get("indefinite"):
        card["indefinite_insanity"] = True
    if outcome.get("permanent"):
        flags = state.get("mental_status")
        if isinstance(flags, dict):
            card["status"] = flags
        card["permanent_insanity"] = True

    record = {
        "id": f"san-{int(time.time() * 1000)}-{len(state.get('sanity_records') or [])}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "san",
        "source": source,
        "value": max(0, -applied),
        "delta": applied,
        "san_before": before,
        "san_after": after,
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
    }
    if daily:
        record["daily_san_loss"] = daily.get("loss")
        record["daily_san_base"] = daily.get("base")
        record["daily_san_date"] = daily.get("date")
    if extra:
        record.update(extra)
    _append_record(state, "sanity_records", record)
    _add_pending_change(
        state,
        {
            "type": "san",
            "value": after,
            "source": source,
            "reason": record["reason"],
            "record_id": record["id"],
        },
    )
    if after <= 0 and isinstance(state.get("mental_status"), dict):
        _add_pending_change(
            state,
            {
                "type": "status",
                "value": dict(state["mental_status"]),
                "source": source,
                "reason": record["reason"],
                "record_id": record["id"],
            },
        )

    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"SAN {before} → {after}（{applied:+d}）",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=record["reason"],
    )
    return {
        "player_name": member_display_name(member),
        "san_before": before,
        "san_after": after,
        "max_san": maximum,
        "delta": applied,
        "record": record,
        "daily": daily,
    }


def _normalize_attribute_key(value: Any, ruleset_id: str = "") -> str:
    """属性名 → 规范键；属性集合与别名由规则集插件声明。"""
    from trpg_server.ruleset import get_plugin

    try:
        checks = getattr(get_plugin(ruleset_id or None), "checks", None)
    except Exception:  # noqa: BLE001 - 插件异常按无法识别处理
        return ""
    normalize = getattr(checks, "attribute_key", None)
    return normalize(value) if callable(normalize) else ""


def _skill_rows(card: dict[str, Any]) -> list[Any]:
    skills = card.get("skills")
    if not isinstance(skills, list):
        skills = []
        card["skills"] = skills
    return skills


def _find_skill_row(card: dict[str, Any], name: str) -> dict[str, Any] | None:
    target = str(name or "").strip().casefold()
    if not target:
        return None
    for row in _skill_rows(card):
        if isinstance(row, dict) and str(row.get("name") or "").strip().casefold() == target:
            return row
    return None


def apply_skill_change(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    value: int | None = None,
    delta: int | None = None,
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "rule",
) -> dict[str, Any]:
    """修改房间快照里的技能值，登记到统一待写回队列。"""
    card = member_card(member)
    skill_name = " ".join(str(name or "").split())[:60]
    if not skill_name:
        raise RuleOperationError("技能名称不能为空", code="invalid_skill")
    row = _find_skill_row(card, skill_name)
    if row is None:
        raise RuleOperationError(f"角色卡上没有技能「{skill_name}」", code="skill_not_found")
    skill_cap = _as_int(derive_card(card).get("skillCap"), 100)
    before = max(0, min(skill_cap, _as_int(row.get("value"), 0)))
    target = value if value is not None else before + int(delta or 0)
    after = max(0, min(skill_cap, int(target)))
    row["value"] = after
    state = member["character_state"]
    record = {
        "id": f"skill-{int(time.time() * 1000)}-{len(state.get('skill_changes') or [])}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "skill",
        "skill": str(row.get("name") or skill_name),
        "before": before,
        "after": after,
        "delta": after - before,
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "source": source,
    }
    _append_record(state, "skill_changes", record)
    _add_pending_change(
        state,
        {
            "type": "skill",
            "name": record["skill"],
            "value": after,
            "source": source,
            "reason": record["reason"],
            "record_id": record["id"],
        },
    )
    # 克苏鲁神话上升会压低理智上限：立即重算派生值并把超出的现有 SAN 压回。
    sync_derived_stats(member, source=source, record_id=record["id"])
    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"技能「{record['skill']}」{before} → {after}",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=record["reason"],
    )
    return {
        "player_name": member_display_name(member),
        "skill": record["skill"],
        "before": before,
        "after": after,
        "delta": after - before,
        "record": record,
    }


def apply_attribute_change(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    key: str,
    value: int | None = None,
    delta: int | None = None,
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "rule",
) -> dict[str, Any]:
    """修改房间快照里的基础属性，登记到统一待写回队列。"""
    card = member_card(member)
    attribute = _normalize_attribute_key(key, resolve_rule_id(info))
    if not attribute:
        raise RuleOperationError(
            "属性名必须是 STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC 或对应的中文名",
            code="invalid_attribute",
        )
    attributes = card.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
        card["attributes"] = attributes
    existing_key = next(
        (item for item in attributes if str(item).strip().casefold() == attribute.casefold()),
        attribute,
    )
    before = max(0, min(999, _as_int(attributes.get(existing_key), 0)))
    target = value if value is not None else before + int(delta or 0)
    after = max(0, min(999, int(target)))
    attributes[existing_key] = after
    state = member["character_state"]
    record = {
        "id": f"attr-{int(time.time() * 1000)}-{len(state.get('attr_changes') or [])}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "attr",
        "key": attribute,
        "before": before,
        "after": after,
        "delta": after - before,
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "source": source,
    }
    _append_record(state, "attr_changes", record)
    _add_pending_change(
        state,
        {
            "type": "attr",
            "key": attribute,
            "value": after,
            "source": source,
            "reason": record["reason"],
            "record_id": record["id"],
        },
    )
    derived = sync_derived_stats(member, source=source, record_id=record["id"])
    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"属性 {attribute} {before} → {after}",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=record["reason"],
    )
    return {
        "player_name": member_display_name(member),
        "key": attribute,
        "before": before,
        "after": after,
        "delta": after - before,
        "record": record,
        "derived": derived,
    }


def roll_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    difficulty: str = "regular",
    adjustment: Any = None,
    bonus: int = 0,
    penalty: int = 0,
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """房间成员检定（不等待玩家物理投掷，命令触发即结算）。

    REST 与房间控制台共用 ``check_service`` 入口，具体判定由房间规则集插件完成。
    """
    from trpg_server import check_service
    from trpg_server.agents.tools import dice

    member_card(member)
    context = RoomRuleContext(
        room_dir,
        info,
        room_id=room_id,
        config_dir=config_dir,
        user_id=actor_id,
        username=actor,
    )
    try:
        result = check_service.run_room_check(
            member,
            context=context,
            player_name=member_ref(member),
            name=name,
            difficulty=difficulty,
            adjustment=adjustment,
            bonus=bonus,
            penalty=penalty,
            ruleset_id=resolve_rule_id(info),
            rng=rng,
        )
    except check_service.CheckServiceError as exc:
        message = str(exc)
        if "was not found" in message:
            message = "角色卡上没有这个技能 / 属性（请检查名称）"
        raise RuleOperationError(message, code="check_failed") from exc
    result["reason"] = str(reason or "").strip()[:200]
    result["created_by"] = str(actor or "")
    record_growth_mark_for_check(room_dir, info, member, result)
    dice._log_roll_result(result, context, tool_name="rule.check", actor=actor or "房间规则")
    context.save()
    return result


def _normalize_san_change(value: Any, card: dict[str, Any]) -> str:
    """玩家命令默认把「1d4/1d10」视为损失；显式带 ``+`` 才表示恢复。"""
    sanity = _sanity_or_none(card)
    if sanity is None:
        raise RuleOperationError("当前规则集不支持理智值", code="sanity_unsupported")
    try:
        return sanity.normalize_change(value)
    except ValueError as exc:
        raise RuleOperationError(str(exc), code="invalid_san_change") from exc


def roll_sanity(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    san_change: str,
    reason: str = "",
    auto_madness: bool = True,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """理智检定 + SAN 结算；一次损失 ≥5 时自动接 INT 检定与疯狂表现。"""
    from trpg_server.agents.tools import dice

    card = member_card(member)
    context = RoomRuleContext(
        room_dir,
        info,
        room_id=room_id,
        config_dir=config_dir,
        user_id=actor_id,
        username=actor,
    )
    before_snapshot = san_snapshot(member)
    result = dice.roll_sanity_check(
        {"player_name": member_ref(member), "san_change": _normalize_san_change(san_change, card)},
        context,
        rng=rng,
    )
    if result.get("error"):
        raise RuleOperationError(str(result["error"]), code="sanity_failed")

    madness: dict[str, Any] | None = None
    if auto_madness and result.get("madness_check_required"):
        madness = dice.roll_madness(
            {
                "player_name": member_ref(member),
                "cause": reason or "理智检定失败",
            },
            context,
            rng=rng,
        )
        if madness.get("error"):
            madness = None

    sanity_record = {
        "id": f"sc-{int(time.time() * 1000)}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "san",
        "source": "sanity_check",
        "value": max(0, before_snapshot["current"] - _as_int(result.get("current_san"), before_snapshot["current"])),
        "delta": _as_int(result.get("current_san"), before_snapshot["current"]) - before_snapshot["current"],
        "san_before": before_snapshot["current"],
        "san_after": _as_int(result.get("current_san"), before_snapshot["current"]),
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "expression": result.get("change_expression") or "",
        "roll": result.get("roll"),
        "success": bool(result.get("success")),
    }
    if madness:
        bout = madness.get("bout") if isinstance(madness.get("bout"), dict) else {}
        sanity_record["madness"] = {
            "bout": str(bout.get("bout_name") or ""),
            "temporary": bool(madness.get("temporary_insanity")),
            "indefinite": bool(madness.get("indefinite_insanity")),
            "permanent": bool(madness.get("permanent_insanity")),
            "summary": madness.get("summary") or "",
        }
        mstate = member.setdefault("character_state", {})
        if isinstance(mstate, dict):
            _append_record(
                mstate,
                "madness_records",
                {
                    "id": f"mad-{int(time.time() * 1000)}",
                    "ts": sanity_record["ts"],
                    "loss": _as_int(madness.get("loss"), 0),
                    "bout": sanity_record["madness"]["bout"],
                    "summary": madness.get("summary") or "",
                    "created_by": str(actor or ""),
                    "created_by_id": str(actor_id or ""),
                },
            )
    _append_record(member.setdefault("character_state", {}), "sanity_records", sanity_record)

    dice._log_roll_result(result, context, tool_name="rule.sanity_check", actor=actor or "房间规则")
    if madness:
        dice._log_roll_result(madness, context, tool_name="rule.madness", actor=actor or "房间规则")

    summary = str(result.get("summary") or "")
    if madness and madness.get("summary"):
        summary = f"{summary}\n{madness['summary']}"
    context.save()
    return {
        **result,
        "summary": summary,
        "madness": madness,
        "san_before": sanity_record["san_before"],
        "san_after": sanity_record["san_after"],
        "delta": sanity_record["delta"],
        "reason": sanity_record["reason"],
        "created_by": str(actor or ""),
        "record": sanity_record,
    }


def add_item(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    kind: str = "item",
    quantity: int = 1,
    note: str = "",
    damage: str = "",
    skill: str = "",
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "item",
) -> dict[str, Any]:
    """向房间快照中的角色卡追加物品 / 武器，并留下变更记录。"""
    from trpg_server import weapon_catalog

    card = member_card(member)
    item_name = " ".join(str(name or "").split())[:60]
    if not item_name:
        raise RuleOperationError("物品名称不能为空", code="invalid_item")
    normalized_kind = ITEM_KIND_ALIASES.get(str(kind or "item").strip().lower())
    if normalized_kind not in ITEM_KINDS:
        raise RuleOperationError("物品类型只能是物品（item）或武器（weapon）", code="invalid_item")
    amount = max(1, min(999, _as_int(quantity, 1)))
    note_text = str(note or "").strip()[:200]
    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state

    changed = True
    item: dict[str, Any]
    if normalized_kind == "weapon":
        row = {
            "name": item_name,
            "damage": str(damage or "").strip()[:40],
            "skill": str(skill or "").strip()[:40],
            "note": note_text,
        }
        weapons = card.get("weapons") if isinstance(card.get("weapons"), list) else []
        sanitized = weapon_catalog.sanitize_character_weapons([row])[0]
        existing = next(
            (row for row in weapons if isinstance(row, dict) and str(row.get("name") or "").casefold() == item_name.casefold()),
            None,
        )
        before_row = copy.deepcopy(existing) if isinstance(existing, dict) else None
        if existing is None:
            weapons.append(sanitized)
        else:
            existing.update({key: value for key, value in sanitized.items() if value not in (None, "", 0)})
            changed = False
        card["weapons"] = weapons
        item = sanitized if existing is None else existing
    else:
        equipment = card.get("equipment") if isinstance(card.get("equipment"), list) else []
        existing = next(
            (row for row in equipment if isinstance(row, dict) and str(row.get("name") or "").casefold() == item_name.casefold()),
            None,
        )
        before_row = copy.deepcopy(existing) if isinstance(existing, dict) else None
        if existing is None:
            item = {
                "name": item_name,
                "quantity": amount,
                "weight": "",
                "volume": "",
                "notes": note_text,
            }
            equipment.append(item)
        else:
            item = existing
            item["quantity"] = max(1, min(999, _as_int(item.get("quantity"), 1) + amount))
            if note_text and not str(item.get("notes") or "").strip():
                item["notes"] = note_text
            changed = False
        card["equipment"] = equipment

    record = {
        "id": f"item-{uuid4().hex}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "item",
        "target": item_name,
        "kind": normalized_kind,
        "change": "获得" if changed else "补充",
        "amount": amount,
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "source": source,
        "persisted": False,
        "before_row": before_row,
    }
    _append_record(state, "item_changes", record)
    _add_pending_change(
        state,
        {
            "type": "weapon" if normalized_kind == "weapon" else "item",
            "name": item_name,
            "amount": amount,
            "fields": dict(item),
            "note": note_text,
            "source": source,
            "reason": record["reason"],
            "record_id": record["id"],
        },
    )

    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"获得{'武器' if normalized_kind == 'weapon' else '物品'}「{item_name}」×{amount}",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=record["reason"],
    )
    return {
        "player_name": member_display_name(member),
        "item": item,
        "kind": normalized_kind,
        "quantity": amount,
        "changed": changed,
        "record": record,
    }


def item_snapshot(member: dict[str, Any]) -> dict[str, Any]:
    """房间快照里的物品 / 武器清单（只读，聊天 ``/item`` 与控制台共用）。"""
    card = member_card(member)
    equipment = [
        {
            "name": str(row.get("name") or ""),
            "quantity": max(1, _as_int(row.get("quantity"), 1)),
            "note": str(row.get("notes") or row.get("note") or ""),
        }
        for row in (card.get("equipment") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    weapons = [
        {
            "name": str(row.get("name") or ""),
            "damage": str(row.get("damage") or ""),
            "skill": str(row.get("skill") or ""),
            "note": str(row.get("note") or ""),
        }
        for row in (card.get("weapons") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    return {
        "player_name": member_display_name(member),
        "equipment": equipment,
        "weapons": weapons,
    }


def remove_item(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    kind: str = "",
    quantity: int = 1,
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "item",
) -> dict[str, Any]:
    """从房间快照移除物品 / 武器，与 :func:`add_item` 共用留痕与写回队列。"""
    card = member_card(member)
    item_name = " ".join(str(name or "").split())[:60]
    if not item_name:
        raise RuleOperationError("物品名称不能为空", code="invalid_item")
    kind_hint = ITEM_KIND_ALIASES.get(str(kind or "").strip().lower(), "")

    def _match(rows: list[Any]) -> tuple[int, dict[str, Any]] | None:
        for index, row in enumerate(rows):
            if isinstance(row, dict) and str(row.get("name") or "").strip().casefold() == item_name.casefold():
                return index, row
        return None

    equipment = card.get("equipment") if isinstance(card.get("equipment"), list) else []
    weapons = card.get("weapons") if isinstance(card.get("weapons"), list) else []
    found: tuple[str, int, dict[str, Any]] | None = None
    for candidate_kind, rows in (("weapon", weapons), ("item", equipment)):
        if kind_hint and kind_hint != candidate_kind:
            continue
        hit = _match(rows)
        if hit:
            found = (candidate_kind, hit[0], hit[1])
            break
    if found is None:
        raise RuleOperationError(f"角色卡上没有物品「{item_name}」", code="item_not_found")

    normalized_kind, index, row = found
    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    removed = 1
    if normalized_kind == "weapon":
        weapons.pop(index)
        card["weapons"] = weapons
        item = dict(row)
    else:
        current = max(1, _as_int(row.get("quantity"), 1))
        amount = max(1, min(999, _as_int(quantity, 1)))
        removed = min(current, amount)
        if current - removed > 0:
            row["quantity"] = current - removed
        else:
            equipment.pop(index)
            removed = current
        card["equipment"] = equipment
        item = dict(row)

    record = {
        "id": f"item-{uuid4().hex}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "item",
        "target": item_name,
        "kind": normalized_kind,
        "change": "移除",
        "amount": removed,
        "reason": str(reason or "").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "source": source,
        "persisted": False,
    }
    _append_record(state, "item_changes", record)
    _add_pending_change(
        state,
        {
            "type": "weapon_remove" if normalized_kind == "weapon" else "item_remove",
            "name": item_name,
            "amount": removed,
            "source": source,
            "reason": record["reason"],
            "record_id": record["id"],
        },
    )

    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"移除{'武器' if normalized_kind == 'weapon' else '物品'}「{item_name}」×{removed}",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=record["reason"],
    )
    return {
        "player_name": member_display_name(member),
        "item": item,
        "kind": normalized_kind,
        "quantity": removed,
        "removed": True,
        "record": record,
    }


def _pending_write_changes(member: dict[str, Any], card: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """汇总本次需要写回持久卡的所有字段变更。

    新数据统一从 ``pending_changes`` 读取；旧数据没有该队列时，回退读取
    ``item_changes`` / ``pending_persist``，并转换成等价的字段级变更。
    """
    queued = pending_changes(state)
    changes = list(queued)
    covered = {str(change.get("record_id")) for change in queued if change.get("record_id")}
    for record in state.get("item_changes") or []:
        if not isinstance(record, dict) or record.get("persisted"):
            continue
        if str(record.get("id")) in covered:
            # 已经通过统一队列登记过，避免同一物品被写回两次。
            continue
        item_name = str(record.get("target") or "").strip()
        if not item_name:
            continue
        kind = str(record.get("kind") or "item").lower()
        if kind == "weapon":
            row = next(
                (
                    item
                    for item in (card.get("weapons") or [])
                    if isinstance(item, dict) and str(item.get("name") or "").casefold() == item_name.casefold()
                ),
                None,
            )
            changes.append(
                {
                    "type": "weapon",
                    "name": item_name,
                    "fields": dict(row or {"name": item_name}),
                    "record_id": record.get("id"),
                }
            )
        else:
            changes.append(
                {
                    "type": "item",
                    "name": item_name,
                    "amount": _as_int(record.get("amount"), 1),
                    "record_id": record.get("id"),
                }
            )
    if not queued and state.get("pending_persist"):
        snapshot = san_snapshot(member)
        changes.append({"type": "san", "value": snapshot["current"]})
        if isinstance(card.get("status"), dict):
            changes.append({"type": "status", "value": dict(card["status"])})
        if isinstance(card.get("background"), dict):
            changes.append({"type": "background", "value": dict(card["background"])})
    return changes


def persist_member_card(
    member: dict[str, Any],
    *,
    actor: Any,
    item_record_id: str | None = None,
) -> dict[str, Any]:
    """把房间快照里登记过的变更写回持久角色卡。

    新数据通过 ``pending_changes`` 只写实际变更的技能 / 属性 / SAN / 物品等字段，
    避免覆盖玩家在卡面编辑器里对其它字段的修改；旧数据走等价转换后的字段级写回。
    写回成功后清除统一队列与 ``pending_persist`` 标记。
    """
    from trpg_server import character_service

    card = member_card(member)
    character_id = str(card.get("id") or "").strip()
    if not character_id:
        raise RuleOperationError("角色卡缺少 ID，无法写回持久卡", code="persist_unavailable")
    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    changes = _pending_write_changes(member, card, state)
    try:
        if changes:
            character_service.apply_room_changes(actor, character_id, changes)
        else:
            character_service.apply_save(actor, character_id, {"id": character_id})
    except character_service.CharacterServiceError as exc:
        raise RuleOperationError(f"写回角色卡失败：{exc.message}", code=exc.code, status=exc.status)

    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    # 本次写回已经把房间快照里的变更同步到持久卡，清掉统一队列与兼容标记。
    state.pop(PENDING_CHANGES_KEY, None)
    state.pop("pending_persist", None)
    for key in ("item_changes", "sanity_records", "skill_changes", "attr_changes"):
        for record in state.get(key) or []:
            if isinstance(record, dict):
                record["persisted"] = True
    if item_record_id:
        for record in state.get("item_changes") or []:
            if isinstance(record, dict) and record.get("id") == item_record_id:
                record["persisted"] = True
                break
    return {"character_id": character_id, "item_record_id": item_record_id, "changes": len(changes)}


def persist_room_items_on_archive(
    room_dir: Any,
    info: dict[str, Any],
    *,
    app: Any = None,
) -> dict[str, Any]:
    """房间归档时按房规把本房间的全部字段变更写回持久角色卡。

    ``rule_persist_on_archive`` 默认为开启。统一判断 ``pending_changes`` 队列；
    旧房间没有该队列时兼容读取 ``pending_persist`` 与未写回的 ``item_changes``，
    避免物品与 SAN 两套判断互相漏掉。写回成功后统一清除待写回标记。
    """
    rules = info.get("house_rules") if isinstance(info.get("house_rules"), dict) else {}
    if rules.get("rule_persist_on_archive", True) is False:
        return {"persisted": 0, "skipped": "disabled"}
    members = [
        member
        for member in (info.get("members") or [])
        if isinstance(member, dict)
        and member.get("is_active", True) is not False
        and str(member.get("status") or "active") != "removed"
    ]
    targets = []
    for member in members:
        state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
        legacy_items = [
            record
            for record in (state.get("item_changes") or [])
            if isinstance(record, dict) and not record.get("persisted")
        ]
        if pending_changes(state) or legacy_items or state.get("pending_persist"):
            targets.append(member)

    def _run() -> dict[str, Any]:
        actor = system_actor()
        persisted = 0
        for member in targets:
            try:
                persist_member_card(member, actor=actor)
                persisted += 1
            except Exception:  # noqa: BLE001 - 单张卡失败不应阻断归档
                logger.warning("归档写回角色卡失败: %s", member_display_name(member), exc_info=True)
        return {"persisted": persisted, "skipped": ""}

    if not targets:
        return {"persisted": 0, "skipped": "no_changes"}
    try:
        from flask import has_app_context

        if has_app_context():
            return _run()
        if app is not None:
            with app.app_context():
                return _run()
    except Exception:  # noqa: BLE001 - 应用上下文不可用时保持归档可完成
        logger.warning("归档写回角色卡缺少应用上下文", exc_info=True)
    return {"persisted": 0, "skipped": "no_app_context"}


def system_actor():
    """归档等系统动作使用的角色卡写入身份（可写入任意角色卡）。"""
    from trpg_server.site_agent_auth import Actor

    return Actor(
        user_id=0,
        username="房间归档",
        role="SYSTEM",
        permissions=frozenset({"characters.manage_all"}),
        room_ids=frozenset(),
    )


def _log(
    room_dir: Any,
    info: dict[str, Any],
    message: str,
    *,
    actor: Any = None,
    actor_id: Any = None,
    source: str = "rule",
    target: Any = None,
    reason: Any = None,
) -> None:
    details: dict[str, Any] = {"操作者": actor or "未知", "操作者ID": actor_id or ""}
    if target:
        details["目标玩家"] = target
    if reason:
        details["原因"] = reason
    try:
        log_room_event(
            Path(room_dir),
            message,
            source=source,
            room_id=info.get("id"),
            level="info",
            actor=actor or "房间规则",
            **details,
        )
    except Exception:  # noqa: BLE001 - 日志失败不应影响规则结算
        logger.warning("写入房间日志失败: %s", message, exc_info=True)


def _check_error(exc: Exception) -> RuleOperationError:
    message = str(exc)
    if "was not found" in message:
        message = "角色卡上没有这个技能 / 属性（请检查名称）"
    return RuleOperationError(message, code="check_failed")


def _rule_context(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
) -> RoomRuleContext:
    return RoomRuleContext(
        room_dir,
        info,
        room_id=room_id,
        config_dir=config_dir,
        user_id=actor_id,
        username=actor,
    )


def roll_social_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    target_skill: Any = None,
    attitude: str = "neutral",
    leverage: bool = False,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """5.10 社交检定；难度由规则插件按目标技能 / 态度 / 筹码给出。"""
    from trpg_server import check_service
    from trpg_server.agents.tools import dice

    member_card(member)
    context = _rule_context(
        room_dir, info, actor=actor, actor_id=actor_id, config_dir=config_dir, room_id=room_id
    )
    try:
        result = check_service.run_room_social_check(
            member,
            context=context,
            player_name=member_ref(member),
            name=name,
            target_skill=target_skill,
            attitude=attitude,
            leverage=leverage,
            ruleset_id=resolve_rule_id(info),
            rng=rng,
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc
    result["created_by"] = str(actor or "")
    if not result.get("auto_success"):
        dice._log_roll_result(result, context, tool_name="rule.social", actor=actor or "房间规则")
    context.save()
    return result


def roll_combined_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    names: list[str],
    *,
    mode: str = "all",
    difficulty: str = "regular",
    bonus: int = 0,
    penalty: int = 0,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """5.9 组合技能检定；只掷一次，同一结果与所有技能比较。"""
    from trpg_server import check_service
    from trpg_server.agents.tools import dice

    member_card(member)
    context = _rule_context(
        room_dir, info, actor=actor, actor_id=actor_id, config_dir=config_dir, room_id=room_id
    )
    try:
        result = check_service.run_room_combined_check(
            member,
            player_name=member_ref(member),
            names=names,
            mode=mode,
            difficulty=difficulty,
            bonus=bonus,
            penalty=penalty,
            ruleset_id=resolve_rule_id(info),
            rng=rng,
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc
    result["created_by"] = str(actor or "")
    dice._log_roll_result(result, context, tool_name="rule.combined", actor=actor or "房间规则")
    context.save()
    return result


def roll_group_luck(
    room_dir: Any,
    info: dict[str, Any],
    *,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """5.6 团体幸运检定；由在场最低幸运成员代掷。"""
    from trpg_server import check_service
    from trpg_server.agents.tools import dice

    context = _rule_context(
        room_dir, info, actor=actor, actor_id=actor_id, config_dir=config_dir, room_id=room_id
    )
    try:
        result = check_service.run_room_group_luck(
            info.get("members") or [],
            ruleset_id=resolve_rule_id(info),
            rng=rng,
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc
    result["created_by"] = str(actor or "")
    dice._log_roll_result(result, context, tool_name="rule.groupluck", actor=actor or "房间规则")
    context.save()
    return result


def roll_contact_check(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    name: str,
    in_hometown: bool = False,
    remote: bool = False,
    same_profession: bool = False,
    base_difficulty: str = "regular",
    pushed: bool = False,
    actor: Any = None,
    actor_id: Any = None,
    config_dir: Any = None,
    room_id: Any = None,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """5.13 建立熟人检定；地点与关系调整难度。"""
    from trpg_server import check_service
    from trpg_server.agents.tools import dice

    member_card(member)
    context = _rule_context(
        room_dir, info, actor=actor, actor_id=actor_id, config_dir=config_dir, room_id=room_id
    )
    try:
        result = check_service.run_room_contact_check(
            member,
            context=context,
            player_name=member_ref(member),
            name=name,
            in_hometown=in_hometown,
            remote=remote,
            same_profession=same_profession,
            base_difficulty=base_difficulty,
            pushed=pushed,
            ruleset_id=resolve_rule_id(info),
            rng=rng,
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc
    result["created_by"] = str(actor or "")
    dice._log_roll_result(result, context, tool_name="rule.contact", actor=actor or "房间规则")
    context.save()
    return result


def economy_summary(
    member: dict[str, Any],
    *,
    amount: Any = None,
    era: str = "1920s",
    assets: Any = None,
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """5.12 信用评级与开支计算；只读，不写回角色卡。"""
    from trpg_server import check_service

    member_card(member)
    try:
        return check_service.run_room_economy(
            member,
            player_name=member_ref(member),
            amount=amount,
            era=era,
            assets=assets,
            ruleset_id=resolve_rule_id(info or {}),
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc


def apply_economy_expense(
    room_dir: Any,
    info: dict[str, Any],
    member: dict[str, Any],
    *,
    amount: Any,
    era: str = "1920s",
    reason: str = "",
    actor: Any = None,
    actor_id: Any = None,
    source: str = "rule",
) -> dict[str, Any]:
    """结算一次信用评级消费，并把现金变化登记到统一待写回队列。

    角色卡没有数值化的「其他资产」字段（``assetsText`` 是自由文本），因此写回只
    覆盖 ``assets.cash``；其他资产仍由 KP 在叙事与卡面文本里处理。金额未超出
    每日消费水平时不动现金，也不产生待写回项。
    """
    from trpg_server import check_service

    card = member_card(member)
    try:
        result = check_service.run_room_economy(
            member,
            player_name=member_ref(member),
            amount=amount,
            era=era,
            assets=0,
            ruleset_id=resolve_rule_id(info),
        )
    except check_service.CheckServiceError as exc:
        raise _check_error(exc) from exc

    expense = result.get("expense")
    if not isinstance(expense, dict):
        raise RuleOperationError("缺少金额，无法结算消费", code="missing_amount")

    current_assets = card.get("assets") if isinstance(card.get("assets"), dict) else {}
    before = max(0, _as_int(current_assets.get("cash"), 0))
    after = max(0, _as_int(expense.get("cash"), before))
    result["applied"] = False
    result["before_cash"] = before
    result["after_cash"] = after
    if after == before:
        result["summary"] = f"{result.get('summary') or ''} 现金未发生变化，未登记写回。"
        return result

    assets = dict(current_assets)
    assets["cash"] = after
    card["assets"] = assets
    state = member.setdefault("character_state", {})
    record = {
        "id": f"cash-{uuid4().hex}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "cash",
        "before": before,
        "after": after,
        "reason": str(reason or "信用评级消费").strip()[:200],
        "created_by": str(actor or ""),
        "created_by_id": str(actor_id or ""),
        "source": source,
    }
    _append_record(state, "economy_changes", record)
    record_room_changes(
        member,
        [
            {
                "type": "assets",
                "value": {"cash": after},
                "source": source,
                "reason": record["reason"],
                "record_id": record["id"],
            }
        ],
    )
    save_room_info(room_dir, info)
    _log(
        room_dir,
        info,
        f"现金 {before} → {after}（信用评级消费）",
        actor=actor,
        actor_id=actor_id,
        source=source,
        target=member_display_name(member),
        reason=reason,
    )
    result["applied"] = True
    result["record"] = record
    result["summary"] = (
        f"{result.get('summary') or ''} 已登记现金写回：{before} → {after}"
        "（归档时合并进角色卡）。"
    )
    return result


# 绝对值类改动重复登记时只保留最新一条：归档合并写入的是最终值，重复记录没有意义。
_ABSOLUTE_CHANGE_TYPES = {"san", "hp", "mp", "armor", "credit"}


def _pending_change_identity(change: dict[str, Any]) -> tuple[str, str] | None:
    """返回可去重的字段身份；相对量（物品增删）返回 ``None``，避免误合并。"""
    kind = str(change.get("type") or "").strip().lower()
    if kind in _ABSOLUTE_CHANGE_TYPES:
        return (kind, "")
    if kind == "skill":
        return (kind, str(change.get("name") or "").strip().casefold())
    if kind == "attr":
        return (kind, str(change.get("key") or "").strip().casefold())
    if kind == "assets":
        return (kind, "")
    return None


def _dedupe_pending_change(records: list[Any], change: dict[str, Any]) -> dict[str, Any]:
    """去掉同一字段的旧记录；资产变更合并为新旧键的并集。

    物品 / 武器属于相对增量，只按 ``record_id`` 去重，保证多次合法购买仍会累加。
    """
    record_id = str(change.get("record_id") or "")
    kind = str(change.get("type") or "").strip().lower()
    if record_id:
        # ``record_id`` 指向来源审计记录，同一条技能记录也可能同时产生 SAN 夹紧
        # 变更；只在同一类型内去重，避免把不同类型的变更误删。
        for index, row in enumerate(records):
            if (
                isinstance(row, dict)
                and str(row.get("record_id") or "") == record_id
                and str(row.get("type") or "").strip().lower() == kind
            ):
                records.pop(index)
                break
    identity = _pending_change_identity(change)
    if identity is None:
        return change
    for index, row in enumerate(records):
        if not isinstance(row, dict) or _pending_change_identity(row) != identity:
            continue
        if identity[0] == "assets":
            old_value = row.get("value")
            new_value = change.get("value")
            if isinstance(old_value, dict) and isinstance(new_value, dict):
                merged = dict(old_value)
                merged.update(new_value)
                change = {**change, "value": merged}
        records.pop(index)
        break
    return change
