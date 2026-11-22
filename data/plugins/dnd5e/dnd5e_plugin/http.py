"""dnd5e 插件的 HTTP 接口：升级规划、内容检索与规则深库查询。

插件只负责 5e 规则判断；房间访问、角色卡读写与持久卡同步仍由核心提供。
规则深库（kiwee）三个查询端点见文件末尾 `rules_*` 段：全局搜索与状态对
登录用户开放，单条详情对 DM 侧类目（bestiary/掷表/变体规则）要求 DM 身份。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, request, session

from dnd5e_plugin import kiwee, progression
from dnd5e_plugin.content_search import Dnd5eContentSearch
from trpg_server.character_service import CharacterServiceError, apply_room_changes
from trpg_server.json_store import read_json
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import (
    RuleOperationError,
    member_card,
    resolve_member,
    resolve_rule_id,
    save_room_info,
)
from trpg_server.settings import ROOMS_DIR

logger = logging.getLogger(__name__)
bp = Blueprint("dnd5e_plugin", __name__, url_prefix="/api/plugins/dnd5e")
_OWNER_ROLES = {"owner", "admin"}
_CONTENT_SEARCH = Dnd5eContentSearch()

# 深库查询端点的口径：列表正文短一点够 UI 卡片用；详情 raw 超过 64KB 截断。
SEARCH_TEXT_LIMIT = 800
RAW_LIMIT_BYTES = 64 * 1024
SNAPSHOT_HINT = "深库未建，请跑 python -m dnd5e_plugin.kiwee snapshot fetch + snapshot build"
_TRANSLATOR_NOTE = "机翻，未校对"


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


# --- 规则深库查询（kiwee）---------------------------------------------------
#
# 三个端点的分工：
#   /rules/search  —— 全局检索，**任何账号都只看玩家侧类目**（与 KP 视角无关；
#                     深库门控在服务层，这里显式 observer_is_dm=False）。
#   /rules/entry   —— 单条详情，datatag 解析后纯文本 + 结构化字段 + 深链 + raw；
#                     bestiary/掷表/变体规则等 DM 侧类目要求 DM 身份。
#   /rules/status  —— 深库是否可用与统计，给 UI 显示「深库未建」引导。
# 深库缺席一律 200 + ``available: false``（查询端点），绝不 500。


def _login_error():
    """登录校验（与核心路由同款文案与错误码）。"""
    if "user_id" in session:
        return None
    return error_response("Please login first", 401, "Not logged in")


def _kiwee_service():
    """插件服务表里的深库服务；插件未加载/旧核心 → None。"""
    try:
        from trpg_server.plugins.runtime import get_service

        return get_service("dnd5e", kiwee.SERVICE_NAME)
    except Exception:  # noqa: BLE001 - 服务表缺席按无深库降级
        return None


def _unavailable_payload(service: Any, reason: str = "") -> dict[str, Any]:
    """深库不可用时的降级载荷（200 + available:false + 建库指引）。"""
    if not reason and service is not None:
        try:
            status = service.status()
        except Exception:  # noqa: BLE001 - 状态探测失败不拖垮查询端点
            status = {}
        reason = str(status.get("reason") or "")
    return {
        "available": False,
        "reason": reason or "深库未建",
        "hint": SNAPSHOT_HINT,
    }


def _library_available(service: Any) -> bool:
    """库是否真的可用（服务在册但库未建时 status 会如实说 available:false）。"""
    try:
        return bool(service.status().get("available"))
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理，宁可降级不要 500
        return False


def _room_is_dnd5e(info: dict[str, Any]) -> bool:
    try:
        return resolve_rule_id(info, scenarios_dir=current_app.config.get("SCENARIOS_DIR")) == "dnd5e"
    except Exception:  # noqa: BLE001 - 规则集解析失败（未启用等）退到显式字段
        return str(info.get("ruleset_id") or "").strip().lower() == "dnd5e"


def _dm_observer() -> bool:
    """调用者是否具备 DM 身份：站点管理员，或任一 dnd5e 房间的房主/房管。

    最简实现：扫 ``ROOMS_DIR`` 下各房间 ``info.json``，命中
    ``creator_id`` 或成员 ``room_role`` ∈ {owner, admin} 即放行；
    bestiary 这类 DM 侧条目只对主持人开放。
    """
    if is_elevated():
        return True
    user_id = str(session.get("user_id") or "").strip()
    if not user_id:
        return False
    rooms_root = Path(current_app.config.get("ROOMS_DIR") or ROOMS_DIR)
    try:
        room_dirs = sorted(entry for entry in rooms_root.iterdir() if entry.is_dir())
    except OSError:
        return False
    for room_dir in room_dirs:
        info_file = room_dir / "info.json"
        if not info_file.is_file():
            continue
        info = read_json(info_file, default={})
        if not isinstance(info, dict) or not _room_is_dnd5e(info):
            continue
        if str(info.get("creator_id") or "") == user_id:
            return True
        for member in info.get("members") or []:
            if not isinstance(member, dict):
                continue
            if str(member.get("user_id") or "") != user_id:
                continue
            if str(member.get("room_role") or "").strip().lower() in _OWNER_ROLES:
                return True
    return False


def _translator_note(row: dict[str, Any]) -> str:
    return _TRANSLATOR_NOTE if int(row.get("is_machine_translated") or 0) else ""


def _search_item(row: dict[str, Any]) -> dict[str, Any]:
    """列表项：不带 raw（体积），正文截断到 SEARCH_TEXT_LIMIT。"""
    return {
        "category": row.get("category"),
        "uid": row.get("uid"),
        "name": row.get("name"),
        "eng_name": row.get("eng_name"),
        "source": row.get("source"),
        "page_hint": row.get("page_hint"),
        "deep_link": row.get("deep_link"),
        "rules_version": row.get("rules_era"),
        "translator_ok": row.get("translator_ok"),
        "is_machine_translated": row.get("is_machine_translated"),
        "translator_note": _translator_note(row),
        "text": str(row.get("text") or "")[:SEARCH_TEXT_LIMIT],
    }


def _truncate_utf8(text: str, limit: int) -> str:
    """按 UTF-8 字节安全截断（中文不会切出半个字）。"""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _entry_payload(row: dict[str, Any]) -> dict[str, Any]:
    """详情载荷：解析后正文 + 结构化字段 + 深链 + raw（超 64KB 截断标注）。"""
    raw = row.get("raw")
    raw_text = json.dumps(raw, ensure_ascii=False)
    raw_bytes = len(raw_text.encode("utf-8"))
    payload = {
        "available": True,
        "category": row.get("category"),
        "uid": row.get("uid"),
        "name": row.get("name"),
        "eng_name": row.get("eng_name"),
        "source": row.get("source"),
        "page_hint": row.get("page_hint"),
        "deep_link": row.get("deep_link"),
        "site": row.get("site") or kiwee.KIWEE_SITE,
        "rules_version": row.get("rules_era"),
        "translator_ok": row.get("translator_ok"),
        "is_machine_translated": row.get("is_machine_translated"),
        "translator_note": _translator_note(row),
        "license": row.get("license"),
        "license_url": row.get("license_url"),
        "text": row.get("text") or "",
        "structured": row.get("structured") or {},
        "raw_bytes": raw_bytes,
        "raw_truncated": raw_bytes > RAW_LIMIT_BYTES,
        "raw_limit_bytes": RAW_LIMIT_BYTES,
    }
    if payload["raw_truncated"]:
        # 原始 JSON 太大：raw 置空，另有 raw_preview 供 UI 折叠展示。
        payload["raw"] = None
        payload["raw_preview"] = _truncate_utf8(raw_text, RAW_LIMIT_BYTES)
    else:
        payload["raw"] = raw
    return payload


@bp.route("/rules/search", methods=["GET"])
def rules_search():
    """全局检索（玩家视角）：任何账号都不会拿到 DM 侧类目。"""
    login_error = _login_error()
    if login_error:
        return login_error
    query = str(request.args.get("q") or request.args.get("query") or "").strip()
    category = str(request.args.get("category") or "").strip()
    rules_version = str(request.args.get("rules_version") or request.args.get("version") or "").strip()
    try:
        limit = int(request.args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(50, limit))

    base = {
        "query": query,
        "category": category,
        "rules_version": rules_version,
        "limit": limit,
        "results": [],
        "total": 0,
        "categories": [],
    }
    service = _kiwee_service()
    if service is None:
        return success_response({**base, **_unavailable_payload(None)}, "Kiwee rules search")
    base["categories"] = [item for item in service.categories() if not item["dm_only"]]
    # 入参在路由层先归一：服务层的 search 会把坏类目/坏版本按「不可用」降级
    # （KP 回合不能挂），但 UI 需要明确的 400。
    try:
        category_key = kiwee.category_id(category) if category else ""
        era = kiwee.normalize_rules_version(rules_version)
    except ValueError as exc:
        return error_response(str(exc), 400, "dnd5e.invalid_rules_query")
    if not query:
        # 空查询也给准确的可用性（UI 首屏要能显示「深库未建」）。
        if not _library_available(service):
            return success_response({**base, **_unavailable_payload(service)}, "Kiwee rules search")
        return success_response({**base, "available": True, "reason": None}, "Kiwee rules search")
    try:
        rows = service.search(
            category_key or None,
            query,
            rules_version=era,
            observer_is_dm=False,
            limit=limit,
            with_text=True,
        )
    except ValueError as exc:
        return error_response(str(exc), 400, "dnd5e.invalid_rules_query")
    if rows is None:
        return success_response({**base, **_unavailable_payload(service)}, "Kiwee rules search")
    results = [_search_item(row) for row in rows]
    return success_response(
        {**base, "available": True, "reason": None, "results": results, "total": len(results)},
        "Kiwee rules search",
    )


@bp.route("/rules/entry/<category>/<path:uid>", methods=["GET"])
def rules_entry(category: str, uid: str):
    """单条详情；DM 侧类目要求管理员或 dnd5e 房间房主/房管。"""
    login_error = _login_error()
    if login_error:
        return login_error
    service = _kiwee_service()
    if service is None:
        return success_response(
            {"category": category, "uid": uid, **_unavailable_payload(None)},
            "Kiwee rules entry",
        )
    try:
        row = service.entry(category, uid)
    except ValueError as exc:
        return error_response(str(exc), 400, "dnd5e.invalid_rules_category")
    if row is None:
        if not _library_available(service):
            # 库未建：200 + available:false（UI 引导建库），而不是 404 误导。
            return success_response(
                {"category": category, "uid": uid, **_unavailable_payload(service)},
                "Kiwee rules entry",
            )
        return error_response("深库中没有这条记录。", 404, "dnd5e.rules_entry_not_found")
    if str(row.get("category") or "") in kiwee.DM_ONLY_CATEGORIES and not _dm_observer():
        return error_response("这条记录仅对主持人（DM）开放。", 403, "dnd5e.dm_only")
    return success_response(_entry_payload(row), "Kiwee rules entry")


@bp.route("/rules/status", methods=["GET"])
def rules_status():
    """深库状态：给 UI 显示「深库未建，请跑 snapshot fetch+build」。"""
    login_error = _login_error()
    if login_error:
        return login_error
    service = _kiwee_service()
    if service is None:
        return success_response(_unavailable_payload(None), "Kiwee rules status")
    try:
        data = dict(service.status())
    except Exception:  # noqa: BLE001 - 状态探测失败按不可用回报
        logger.warning("dnd5e_kiwee_status_failed", exc_info=True)
        return success_response(_unavailable_payload(None, "深库打开失败"), "Kiwee rules status")
    if not data.get("available"):
        data.setdefault("hint", SNAPSHOT_HINT)
    data["dm_only_categories"] = sorted(kiwee.DM_ONLY_CATEGORIES)
    return success_response(data, "Kiwee rules status")
