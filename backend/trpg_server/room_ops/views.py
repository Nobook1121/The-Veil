"""房间只读视图与归档：房间摘要、归档快照、角色卡模组记录。"""
from flask import current_app
from flask import session
from pathlib import Path
from trpg_server import room_clock
from trpg_server import room_interlude
from trpg_server.json_store import read_json
from trpg_server.json_store import write_json_atomic
from trpg_server.room_log import ARCHIVE_LOG_LIMIT
from trpg_server.room_log import load_room_log
from trpg_server.room_mode import has_bound_scenario
from trpg_server.room_mode import normalize_room_mode
from trpg_server.room_ops.access import (
    _can_manage,
    _is_active_member,
    _is_elevated,
    _is_member_online,
    _live_member_role,
    _normalize_members,
    _room_permission_label,
)
from trpg_server.room_ops.constants import ROOM_VISIBILITIES, ROOM_VISIBILITY_PRIVATE
from trpg_server.room_ops.house_rules import (
    _room_dice_animation,
    _room_dice_thresholds,
    _room_house_rules,
)
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _get_archives_dir,
    _get_scenarios_dir,
    _read_messages,
    _timestamp,
)
from trpg_server.room_usage import load_room_usage
from trpg_server.ruleset import builtin_registry
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.settings import CHARACTERS_DIR
from trpg_server.settings import HISTORY_DIR
from trpg_server.world_override import evidence_entries
import json

def _room_usage_for(info, room_dir):
    """只有房主/房间管理员（或站点管理员）能看到房间 token 与花费。"""
    if not _can_manage(info):
        return None
    return load_room_usage(room_dir, str(info.get("id") or ""))

def _archive_visible(archive):
    if _is_elevated():
        return True
    user_id = str(session.get("user_id"))
    return any(str(member.get("user_id")) == user_id for member in archive.get("members", []) if isinstance(member, dict))

def _scenario_finished(info, state=None):
    if info.get("completed_at") or info.get("ended_at"):
        return True
    scene_id = str(info.get("active_scene_id") or ((state or {}).get("active_scene_id") if isinstance(state, dict) else "") or "")
    if isinstance(state, dict) and (state.get("completed_at") or state.get("completed") or state.get("ending_reached")):
        return True
    if not scene_id:
        return False
    _, scenario = load_scenario_by_id(_get_scenarios_dir(), info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    for module in (scenario or {}).get("modules", []):
        if isinstance(module, dict) and str(module.get("scene_id") or module.get("id")) == scene_id:
            return str(module.get("module_type") or module.get("type") or "").lower() == "ending"
    return False

def _character_delta(member):
    """Return ``(san_change_text, other_changes_text)`` for a room member.

    ``other_changes_text`` aggregates HP/injury/skill/item changes so it can be
    rendered as the third line of a "参与过的模组" record.
    """
    state = member.get("character_state") if isinstance(member, dict) else {}
    state = state if isinstance(state, dict) else {}
    max_san = int(state.get("max_san") or 0)
    max_hp = int(state.get("max_hp") or 0)
    current_hp = int(state.get("current_hp") if state.get("current_hp") is not None else max_hp)
    san_base = int(state.get("initial_san") if state.get("initial_san") is not None else max_san)
    current_san = int(state.get("current_san") if state.get("current_san") is not None else san_base)
    san_change = current_san - san_base
    other = []
    if current_hp != max_hp:
        other.append(f"HP {max_hp}->{current_hp}")
    for record in (state.get("injury_records") or [])[:20]:
        if isinstance(record, dict) and record.get("reason"):
            other.append(str(record["reason"])[:120])
    for record in (state.get("skill_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    for record in (state.get("item_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    san_text = f"SAN {san_change:+d}" if san_change else "SAN 无变化"
    return san_text, "；".join(dict.fromkeys(other))

def _change_record_text(record):
    """Format a structured skill/item change record into a short line."""
    if not isinstance(record, dict):
        return ""
    change_type = str(record.get("type") or "")
    target = str(record.get("target") or record.get("name") or record.get("item") or "").strip()
    if not target:
        return ""
    verb = record.get("change") or record.get("operation") or record.get("action") or "变化"
    amount = record.get("amount")
    if amount is None and record.get("value") is not None:
        amount = record["value"]
    reason = str(record.get("reason") or "").strip()
    prefix = "技能" if change_type == "skill" else "物品" if change_type == "item" else "变更"
    if amount is not None:
        try:
            amount_signed = f"{int(amount):+d}"
        except (TypeError, ValueError):
            amount_signed = str(amount)
        line = f"{prefix} {target} {verb} {amount_signed}".strip() + (f"（{reason}）" if reason else "")
    else:
        line = f"{prefix} {target} {verb}".strip() + (f"（{reason}）" if reason else "")
    return line[:160]

def _append_character_archive_records(info):
    """Copy the finished room outcome to each participating character card.

    Each entry is stored with ``name`` (module/scenario name), ``san_change``
    and ``other_changes`` so UIs can render the required three-line format:
    参与过的模组名 / SAN 值变化 / 其他变化.
    """
    scenario_name = str(info.get("scenario_title") or info.get("name") or "未命名剧本").strip()
    for member in info.get("members", []):
        card = member.get("character_card") if isinstance(member, dict) else None
        card_id = str((card or {}).get("id") or "").strip()
        if not card_id:
            continue
        path = Path(current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR)) / f"{card_id}.json"
        if not path.exists():
            continue
        stored = read_json(path, default={})
        if not isinstance(stored, dict):
            continue
        san_change, other_changes = _character_delta(member)
        entry = {
            "name": scenario_name,
            "san_change": san_change,
            "other_changes": other_changes,
            "experience": f"{san_change}" + (f"；{other_changes}" if other_changes else ""),
        }
        existing = stored.get("experiencedModules")
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (TypeError, ValueError):
                existing = []
        if not isinstance(existing, list):
            # Runtime-shaped character cards use experiencedScenarios.
            existing = stored.get("experiencedScenarios") if isinstance(stored.get("experiencedScenarios"), list) else []
        # One immutable outcome entry per archived room/scenario.
        if not any(isinstance(item, dict) and item.get("name") == scenario_name and item.get("experience") == entry["experience"] for item in existing):
            existing.append(entry)
        if "experiencedModules" in stored or "experiencedScenarios" not in stored:
            stored["experiencedModules"] = json.dumps(existing[-100:], ensure_ascii=False)
        else:
            stored["experiencedScenarios"] = existing[-100:]
        write_json_atomic(path, stored)

def _build_room_archive(room_dir, info):
    """Build the room archive snapshot. Rooms are archived in place (kept usable
    for dice and history) rather than deleted/moved; the archive JSON is retained
    as an immutable history copy and participating players' character cards are
    updated with a three-line "参与过的模组" record."""
    mode = normalize_room_mode(info)
    archive = {
        "id": info.get("id"),
        "name": info.get("name"),
        "room_mode": mode["room_mode"],
        "ai_kp_enabled": mode["ai_kp_enabled"],
        "scenario_binding": mode["scenario_binding"],
        "created_at": info.get("created_at"),
        "completed_at": info.get("completed_at") or _timestamp(),
        "archived_at": info.get("archived_at") or _timestamp(),
        "archived": True,
        "creator_id": info.get("creator_id"),
        "members": info.get("members", []),
        "messages": _read_messages(room_dir),
        "console_log": load_room_log(room_dir, limit=ARCHIVE_LOG_LIMIT),
    }
    # 归档时把游戏时钟一并快照，便于复盘「这一局过了多久」。
    # 归档链路会先移除 house_rules，因此这里按字段读取而不是看开关。
    clock = room_clock.read_stored_clock(info)
    if clock is not None:
        archive["game_clock"] = clock
    interlude_state = room_interlude.interlude_state(info)
    if interlude_state.get("startedAt") or interlude_state.get("reviews") or interlude_state.get("settlement"):
        archive["interlude"] = room_interlude.snapshot(info)
        archive["interlude_settlement"] = interlude_state.get("settlement") or {}
        archive["interlude_reviews"] = interlude_state.get("reviews") or []
    # 世界意志留痕：把已拍定的既定事实随归档快照一并保存，便于事后核对。
    state = read_json(room_dir / "state.json", default={})
    facts = evidence_entries(state) if isinstance(state, dict) else []
    if facts:
        archive["world_override_facts"] = facts
    # 无剧本房间（freeform / 未绑定资料的 manual）跳过剧本字段与角色卡模组记录，
    # 避免生成一条空白的「参与过的模组」。
    if has_bound_scenario(info):
        archive.update(
            {
                "scenario_id": info.get("scenario_id"),
                "scenario_title": info.get("scenario_title"),
                "scenario_version": info.get("scenario_version"),
            }
        )
    history_dir = current_app.config.get("HISTORY_DIR", HISTORY_DIR)
    if history_dir:
        history_file = Path(history_dir) / f"room-{info.get('id')}-kp.json"
        archive["ai_history"] = read_json(history_file, default=[])
    archive_dir = _get_archives_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{info.get('id')}.json"
    write_json_atomic(archive_path, archive)
    if has_bound_scenario(info):
        _append_character_archive_records(info)
    return archive

def _room_visibility(info):
    value = str((info or {}).get("visibility") or ROOM_VISIBILITY_PRIVATE).strip().lower()
    return value if value in ROOM_VISIBILITIES else ROOM_VISIBILITY_PRIVATE

def _room_ruleset_id(info):
    """解析房间规则集：显式字段优先，旧房间按剧本推断，最后回退 coc7。"""
    data = info or {}
    explicit = str(data.get("ruleset_id") or "").strip()
    if explicit:
        return builtin_registry().resolve(explicit).id
    scenario_id = str(data.get("scenario_id") or "").strip()
    if scenario_id:
        try:
            _, scenario = load_scenario_by_id(
                _get_scenarios_dir(),
                scenario_id,
                scenario_version=data.get("scenario_version"),
            )
            inferred = str((scenario or {}).get("ruleset_id") or "").strip()
            if inferred:
                return builtin_registry().resolve(inferred).id
        except Exception:  # pragma: no cover - 剧本缺失时按默认规则集处理
            logger.debug("Failed to infer ruleset for room %s", data.get("id"), exc_info=True)
    return builtin_registry().resolve("").id

def _room_summary(info):
    _normalize_members(info)
    mode = normalize_room_mode(info)

    def _member_view(member):
        # 角色以账号数据库为准，避免降级后成员仍显示为管理员配色/标签。
        role = _live_member_role(member)
        view = {**member, "role": role}
        view["is_active"] = _is_active_member(member)
        view["is_online"] = _is_member_online(member, info.get("id"))
        view["permission_label"] = _room_permission_label(view, info)
        return view

    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "room_code": info.get("room_code"),
        "scenario_id": info.get("scenario_id"),
        "scenario_version": info.get("scenario_version"),
        "scenario_title": info.get("scenario_title"),
        # 房间级唯一规则集：剧本侧已持久化，旧房间在读取时按剧本推断。
        "ruleset_id": _room_ruleset_id(info),
        # 房间模式的两个正交能力位：AI KP 是否参与、剧本绑定强度。
        # 旧房间在读取时补默认值，不写回 info.json。
        "room_mode": mode["room_mode"],
        "ai_kp_enabled": mode["ai_kp_enabled"],
        "scenario_binding": mode["scenario_binding"],
        "visibility": _room_visibility(info),
        "scenario_started_at": info.get("scenario_started_at"),
        "scenario_started_by": info.get("scenario_started_by"),
        "archived": bool(info.get("archived")),
        "archived_at": info.get("archived_at"),
        "completed_at": info.get("completed_at"),
        "interlude": room_interlude.snapshot(info),
        "creator_id": info.get("creator_id"),
        "creator_name": info.get("creator_name"),
        "members": [_member_view(member) for member in info.get("members", [])],
        "house_rules": _room_house_rules(info),
        # 全房间统一的骰娘大成功/大失败阈值（房规优先，其次管理员默认值）。
        "dice_thresholds": _room_dice_thresholds(info),
        "dice_animation": _room_dice_animation(info),
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
    }

def _invisible_room_view(room_dir, info):
    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "messages": _read_messages(room_dir),
        "invisible_view": True,
    }
