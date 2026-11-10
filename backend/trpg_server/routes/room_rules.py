"""房间规则操作接口（不依赖 AI KP）。

聊天斜杠命令走这里：``/sc`` 结算理智检定、``/item`` 向角色卡追加物品 / 武器。
房间控制台 ``rule`` 域与 KP 工具直接调用 :mod:`trpg_server.room_rules`，三者
共用同一套权限与留痕逻辑。
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, request, session

from trpg_server import room_rules
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_log import log_room_event
from trpg_server.ruleset.capabilities import clamp_attribute_change

bp = Blueprint("room_rules", __name__)
logger = logging.getLogger(__name__)

ROLE_RANKS = {"owner": 3, "admin": 2, "member": 1}


def _room_permission(info):
    user_id = session.get("user_id")
    member = room_rules.find_member(info, str(user_id)) if user_id is not None else None
    if member is None:
        return 0
    if is_elevated() or str(member.get("user_id")) == str(info.get("creator_id")):
        return 3
    return ROLE_RANKS.get(str(member.get("room_role") or "member"), 1)


def _load_room(room_id: str):
    room_dir, info = find_room(room_id)
    if room_dir is None or not isinstance(info, dict):
        return None, None, error_response("房间不存在", 404, "room_not_found")
    if not can_access(info, session.get("user_id")):
        return None, None, error_response("Permission denied", 403, "permission_denied")
    return room_dir, info, None


def _rule_error(exc: room_rules.RuleOperationError):
    return error_response(exc.message, exc.status, exc.code)


def _notify_room(room_id):
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_members_changed", {"room_id": room_id}, room=room_id)


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _target_member(info, data):
    """解析操作目标：未指定玩家时默认操作自己，其余仍走统一的权限校验。"""
    name = data.get("player_name") or data.get("playerName")
    return room_rules.resolve_member(
        info,
        name,
        actor_id=session.get("user_id"),
        actor_rank=_room_permission(info),
        fallback_to_actor=not str(name or "").strip(),
    )


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise room_rules.RuleOperationError("数值必须是整数，例如 65 / +5 / -5", code="invalid_argument")


def _persist_if_requested(room_dir, info, member, data, result) -> bool:
    """``persist`` 为真时立即写回持久角色卡，失败只回传原因不阻断快照变更。"""
    if not data.get("persist"):
        return False
    from trpg_server.routes.characters import character_service_actor

    try:
        record = result.get("record") or {}
        item_record_id = record.get("id") if record.get("type") == "item" else None
        room_rules.persist_member_card(
            member,
            actor=character_service_actor(),
            item_record_id=item_record_id,
        )
        room_rules.save_room_info(room_dir, info)
        return True
    except room_rules.RuleOperationError as exc:
        result["persist_error"] = exc.message
        log_room_event(
            room_dir,
            f"写回持久角色卡失败：{exc.message}",
            source="rule",
            room_id=info.get("id"),
            level="warning",
            actor=session.get("username"),
        )
        return False


@bp.get("/api/rooms/<room_id>/rules/skills")
def list_skills(room_id: str):
    """查看房间快照里的技能值（默认本人）。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    try:
        member = _target_member(info, request.args)
        card = room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    rows = [
        {"name": str(row.get("name") or ""), "value": room_rules._as_int(row.get("value"), 0)}
        for row in (card.get("skills") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    row = None
    wanted = str(request.args.get("name") or "").strip()
    if wanted:
        row = next((item for item in rows if item["name"].casefold() == wanted.casefold()), None)
        if row is None:
            return error_response(f"角色卡上没有技能「{wanted}」", 404, "skill_not_found")
    return success_response(
        {"player_name": room_rules.member_display_name(member), "skills": rows, "skill": row}
    )


@bp.post("/api/rooms/<room_id>/rules/skills")
def change_skill(room_id: str):
    """设置或增减技能值；默认只写房间快照，``persist`` 才写回持久卡。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = _target_member(info, data)
        value = _optional_int(data.get("value"))
        delta = _optional_int(data.get("delta"))
        if value is None and delta is None:
            raise room_rules.RuleOperationError("需要提供 value（设为）或 delta（增减）", code="invalid_argument")
        result = room_rules.apply_skill_change(
            room_dir,
            info,
            member,
            name=str(data.get("name") or ""),
            value=value,
            delta=delta,
            reason=str(data.get("reason") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source="skill_command",
        )
        result["persisted"] = _persist_if_requested(room_dir, info, member, data, result)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    _notify_room(room_id)
    return success_response(result)


@bp.get("/api/rooms/<room_id>/rules/attributes")
def list_attributes(room_id: str):
    """查看房间快照里的基础属性（默认本人）。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    try:
        member = _target_member(info, request.args)
        card = room_rules.member_card(member)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    return success_response(
        {
            "player_name": room_rules.member_display_name(member),
            "attributes": dict(card.get("attributes") or {}),
        }
    )


@bp.post("/api/rooms/<room_id>/rules/attributes")
def change_attribute(room_id: str):
    """设置或增减基础属性；属性变更后派生值按规则集自动重算。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = _target_member(info, data)
        value = _optional_int(data.get("value"))
        delta = _optional_int(data.get("delta"))
        if value is None and delta is None:
            raise room_rules.RuleOperationError("需要提供 value（设为）或 delta（增减）", code="invalid_argument")
        key = str(data.get("key") or data.get("attribute") or data.get("name") or "")
        # 属性上调按规则集插件声明的上限钳制，核心不写死上限数值。
        card = room_rules.member_card(member)
        value, delta = clamp_attribute_change(
            room_rules.derive_card(card), card, key, value=value, delta=delta
        )
        result = room_rules.apply_attribute_change(
            room_dir,
            info,
            member,
            key=key,
            value=value,
            delta=delta,
            reason=str(data.get("reason") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source="attr_command",
        )
        result["persisted"] = _persist_if_requested(room_dir, info, member, data, result)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    _notify_room(room_id)
    return success_response(result)


@bp.get("/api/rooms/<room_id>/rules/items")
def list_items(room_id: str):
    """查看房间快照里的物品与武器（默认本人）。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    try:
        member = _target_member(info, request.args)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    return success_response(room_rules.item_snapshot(member))


@bp.delete("/api/rooms/<room_id>/rules/items")
def remove_item(room_id: str):
    """移除物品 / 武器；``persist`` 才写回持久卡。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = _target_member(info, data)
        result = room_rules.remove_item(
            room_dir,
            info,
            member,
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or ""),
            quantity=data.get("quantity", 1),
            reason=str(data.get("reason") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source="item_command",
        )
        result["persisted"] = _persist_if_requested(room_dir, info, member, data, result)
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    _notify_room(room_id)
    return success_response(result)


@bp.post("/api/rooms/<room_id>/rules/sanity-check")
def sanity_check(room_id: str):
    """玩家自跑房间：结算一次理智检定（可选自动疯狂判定）。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = room_rules.resolve_member(
            info,
            data.get("player_name") or data.get("playerName"),
            actor_id=session.get("user_id"),
            actor_rank=_room_permission(info),
        )
        result = room_rules.roll_sanity(
            room_dir,
            info,
            member,
            san_change=str(data.get("san_change") or data.get("change") or ""),
            reason=str(data.get("reason") or ""),
            auto_madness=data.get("auto_madness", True) is not False,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            config_dir=current_app.config.get("CONFIG_DIR"),
            room_id=room_id,
        )
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    _notify_room(room_id)
    return success_response(result)


@bp.post("/api/rooms/<room_id>/rules/items")
def add_item(room_id: str):
    """给房间快照中的角色卡追加物品 / 武器；``persist`` 才写回持久卡。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = room_rules.resolve_member(
            info,
            data.get("player_name") or data.get("playerName"),
            actor_id=session.get("user_id"),
            actor_rank=_room_permission(info),
        )
        result = room_rules.add_item(
            room_dir,
            info,
            member,
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or "item"),
            quantity=data.get("quantity", 1),
            note=str(data.get("note") or ""),
            damage=str(data.get("damage") or ""),
            skill=str(data.get("skill") or ""),
            reason=str(data.get("reason") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source="item_command",
        )
        persisted = False
        if data.get("persist"):
            from trpg_server.routes.characters import character_service_actor

            try:
                room_rules.persist_member_card(
                    member,
                    actor=character_service_actor(),
                    item_record_id=(result.get("record") or {}).get("id"),
                )
                persisted = True
                # persist_member_card 会把对应变更记录标记为已写回，这里补一次落盘。
                room_rules.save_room_info(room_dir, info)
            except room_rules.RuleOperationError as exc:
                result["persist_error"] = exc.message
                log_room_event(
                    room_dir,
                    f"物品写回持久角色卡失败：{exc.message}",
                    source="rule",
                    room_id=room_id,
                    level="warning",
                    actor=session.get("username"),
                )
    except room_rules.RuleOperationError as exc:
        return _rule_error(exc)
    result["persisted"] = persisted
    _notify_room(room_id)
    return success_response(result)


@bp.post("/api/rooms/<room_id>/rules/check")
def roll_check(room_id: str):
    """按名字/属性做一次房间检定（控制台 ``rule check`` 与外部调用共用）。"""
    room_dir, info, failure = _load_room(room_id)
    if failure:
        return failure
    data = _payload()
    try:
        member = room_rules.resolve_member(
            info,
            data.get("player_name") or data.get("playerName"),
            actor_id=session.get("user_id"),
            actor_rank=_room_permission(info),
        )
        result = room_rules.roll_check(
            room_dir,
            info,
            member,
            name=str(data.get("name") or ""),
            difficulty=str(data.get("difficulty") or "regular"),
            adjustment=data.get("adjustment"),
            bonus=int(data.get("bonus") or 0),
            penalty=int(data.get("penalty") or 0),
            reason=str(data.get("reason") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            config_dir=current_app.config.get("CONFIG_DIR"),
            room_id=room_id,
        )
    except (room_rules.RuleOperationError, ValueError) as exc:
        if isinstance(exc, room_rules.RuleOperationError):
            return _rule_error(exc)
        return error_response(str(exc), 400, "invalid_argument")
    return success_response(result)
