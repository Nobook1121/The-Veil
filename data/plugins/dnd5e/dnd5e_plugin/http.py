"""dnd5e 插件的 HTTP 接口：升级规划与内容检索。

插件只负责 5e 规则判断；房间访问、角色卡读写与持久卡同步仍由核心提供。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, request, session

from dnd5e_plugin import progression
from dnd5e_plugin.content_search import Dnd5eContentSearch
from trpg_server.character_service import CharacterServiceError, apply_room_changes
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import (
    RuleOperationError,
    member_card,
    resolve_member,
    resolve_rule_id,
    save_room_info,
)

logger = logging.getLogger(__name__)
bp = Blueprint("dnd5e_plugin", __name__, url_prefix="/api/plugins/dnd5e")
_OWNER_ROLES = {"owner", "admin"}
_CONTENT_SEARCH = Dnd5eContentSearch()


def _rank(info: dict[str, Any]) -> int:
    user_id = str(session.get("user_id") or "")
    if is_elevated():
        return 2
    if user_id and user_id == str(info.get("creator_id") or ""):
        return 2
    for member in info.get("members") or []:
        if str(member.get("user_id") or "") != user_id:
            continue
        if str(member.get("room_role") or "").strip().lower() in _OWNER_ROLES:
            return 2
    return 0


def _room_or_error(room_id: str):
    room_dir, info = find_room(room_id)
    if not info:
        return None, None, error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return None, None, error_response("你不在这个房间里。", 403, "room.access_denied")
    ruleset_id = resolve_rule_id(info, scenarios_dir=current_app.config.get("SCENARIOS_DIR"))
    if ruleset_id != "dnd5e":
        return None, None, error_response(
            "当前房间的规则集不是 D&D 5e。", 400, "dnd5e.ruleset_mismatch"
        )
    return room_dir, info, None


def _member_for(info: dict[str, Any], target: Any) -> dict[str, Any]:
    try:
        return resolve_member(
            info,
            target,
            actor_id=session.get("user_id"),
            actor_rank=_rank(info),
            fallback_to_actor=not str(target or "").strip(),
        )
    except RuleOperationError as exc:
        raise _HttpError(exc.message, exc.status, exc.code) from exc


def _card_for(info: dict[str, Any], target: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    member = _member_for(info, target)
    try:
        card = member_card(member)
    except RuleOperationError as exc:
        raise _HttpError(exc.message, exc.status, exc.code) from exc
    return member, card


class _HttpError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "dnd5e.error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@bp.route("/rooms/<room_id>/content/search", methods=["GET"])
def content_search(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    type_name = str(request.args.get("type") or "").strip()
    query = str(request.args.get("q") or request.args.get("query") or "").strip()
    try:
        limit = int(request.args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(20, limit))
    if not type_name:
        return success_response(
            {
                "status": "none",
                "query": query,
                "type": "",
                "types": _CONTENT_SEARCH.types(),
                "matches": [],
                "total": 0,
            },
            "Content types",
        )
    try:
        result = _CONTENT_SEARCH.search(type_name, query, limit=limit)
    except ValueError as exc:
        return error_response(str(exc), 400, "dnd5e.invalid_content_type")
    return success_response(result, "Content search")


@bp.route("/rooms/<room_id>/rules/recommendation", methods=["GET"])
def rules_recommendation(room_id: str):
    """按房间剧本形态推荐 dnd5e 房规档案（M5 第二段）。

    只读优先：推荐 + 理由 + 当前档案三态。自动应用仅在「房主从未手动
    设置过 dnd5e 房规」的窗口内、且调用方是房主/管理员时发生（普通玩家
    请求绝不触发房间写入）；站点配置可整体关闭。
    """
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    from dnd5e_plugin import house_rule_profiles

    payload = house_rule_profiles.recommendation_payload(
        room_dir,
        info,
        can_apply=_rank(info) >= 2,
        config_dir=current_app.config.get("CONFIG_DIR"),
    )
    return success_response(payload, "D&D 5e house-rule profile recommendation")


@bp.route("/rooms/<room_id>/progression/plan", methods=["POST"])
def progression_plan(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "dnd5e.invalid_payload")
    try:
        member, card = _card_for(info, payload.get("user_id") or "")
        target_user_id = str(member.get("user_id") or "")
        plan = progression.create_plan(
            card,
            payload.get("target_level"),
            owner_key=f"{room_id}:{target_user_id}",
            target_user_id=target_user_id,
            class_id=str(payload.get("class") or ""),
        )
    except _HttpError as exc:
        return error_response(exc.message, exc.status, exc.code)
    except progression.ProgressionError as exc:
        return error_response(exc.message, exc.status, exc.code)
    plan["member_user_id"] = target_user_id
    return success_response(plan, "Progression plan created")


@bp.route("/rooms/<room_id>/progression/apply", methods=["POST"])
def progression_apply(room_id: str):
    room_dir, info, denied = _room_or_error(room_id)
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("请求体必须是 JSON 对象。", 400, "dnd5e.invalid_payload")
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        return error_response("缺少 plan_id。", 400, "progression.plan_missing")
    try:
        target = payload.get("user_id") or progression.plan_target(plan_id)
        member, card = _card_for(info, target)
        result = progression.apply_plan(plan_id, payload.get("choices"), card)
    except _HttpError as exc:
        return error_response(exc.message, exc.status, exc.code)
    except progression.ProgressionError as exc:
        return error_response(exc.message, exc.status, exc.code)

    try:
        _persist_sheet(room_dir, info, member, card, result["sheet"])
    except (CharacterServiceError, RuleOperationError, _HttpError) as exc:
        progression.abort_plan(plan_id)
        message = getattr(exc, "message", None) or str(exc) or "角色卡写入失败。"
        status = getattr(exc, "status", 400) or 400
        code = getattr(exc, "code", "progression.persist_failed") or "progression.persist_failed"
        return error_response(message, status, code)
    except Exception:  # noqa: BLE001 - 持久卡写入失败必须回退计划占用
        progression.abort_plan(plan_id)
        logger.exception("dnd5e_progression_persist_failed room=%s plan=%s", room_id, plan_id)
        return error_response("角色卡写入失败，请稍后重试。", 500, "progression.persist_failed")
    progression.commit_plan(plan_id)
    return success_response(
        {
            "plan_id": result["plan_id"],
            "before": result["before"],
            "after": result["after"],
            "changes": result["changes"],
            "sheet": result["sheet"],
        },
        "Progression applied",
    )


def _persist_sheet(
    room_dir,
    info: dict[str, Any],
    member: dict[str, Any],
    card: dict[str, Any],
    sheet: dict[str, Any],
) -> None:
    """一次性写入角色卡：持久卡走 apply_room_changes，房间卡直接更新快照。"""
    character_id = str(card.get("id") or "").strip()
    if character_id:
        from trpg_server.site_agent_auth import build_actor

        actor = build_actor()
        apply_room_changes(
            actor,
            character_id,
            [{"type": "sheet", "value": dict(sheet)}],
        )
        return
    card["sheet"] = dict(sheet)
    card["level"] = sheet.get("level", card.get("level"))
    card["xp"] = sheet.get("xp", card.get("xp"))
    member["character_card"] = card
    save_room_info(room_dir, info)
