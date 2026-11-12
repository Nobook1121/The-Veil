"""房间消息、触发与节点路由：消息读写、剧本触发、角色记录、存档节点。"""
from flask import request
from flask import session
from trpg_server import room_interlude
from trpg_server.agents.trigger_system import find_trigger_definition
from trpg_server.agents.trigger_system import record_trigger
from trpg_server.agents.trigger_system import validate_trigger
from trpg_server.json_store import read_json
from trpg_server.json_store import write_json_atomic
from trpg_server.logging_config import user_action_text
from trpg_server.media_defaults import normalize_avatar
from trpg_server.responses import error_response
from trpg_server.responses import success_response
from trpg_server.room_log import log_room_event
from trpg_server.room_ops.access import (
    _can_access,
    _can_manage,
    _can_manage_members,
    _denied,
    _find_member,
    _is_elevated,
    _require_login,
)
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.cards import (
    _bounded_int,
    _ensure_character_state,
    _recalculate_character_state,
)
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _configured_role,
    _find_room,
    _find_room_by_name,
    _get_scenarios_dir,
    _read_messages,
    _timestamp,
    _write_messages,
    _write_room,
)
from trpg_server.room_ops.views import _room_summary
from trpg_server.room_ops.visibility import filter_payload_messages, visible_messages
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.security import safe_join
from uuid import uuid4
import time

@bp.route("/api/rooms/<room_id>/messages", methods=["GET"])
def get_room_messages(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    is_kp = _is_elevated() or str((member or {}).get("room_role") or "") in {"owner", "admin"}
    messages = visible_messages(room_dir, info, session.get("user_id"), is_kp=is_kp)
    return success_response(messages, "Room messages loaded successfully")

@bp.route("/api/rooms/<room_id>/messages", methods=["POST"])
def create_room_message(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return error_response("Please provide message content", 400, "No message content")

    member = _find_member(info, user_id=session["user_id"], active_only=True)
    if not member:
        return _denied(room_id)
    message = {
        "id": uuid4().hex,
        "type": data.get("type", "player"),
        "sender_id": member["user_id"],
        "sender_name": member["username"],
        "avatar": member["avatar"],
        "content": content,
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": data.get("metadata", {}),
    }
    if message["type"] == "kp":
        role = _configured_role((message.get("metadata") or {}).get("roleId") or data.get("role_id") or "kp")
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or role.get("name") or "KP"
        message["avatar"] = normalize_avatar(data.get("avatar") or role.get("avatar"))
    elif message["type"] == "dice":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "骰娘")
        message["avatar"] = ""
    elif message["type"] == "system":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "系统")
        message["avatar"] = ""
    elif message["type"] == "trigger":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or "触发器"
        message["avatar"] = normalize_avatar(data.get("avatar"))

    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "发送了房间消息"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        类型=message["type"],
        内容长度=len(content),
    )
    return success_response(message, "Room message saved successfully", 201)

@bp.route("/api/rooms/<room_id>/triggers", methods=["POST"])
def trigger_room_scenario(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    trigger_id = data.get("trigger_id")
    if trigger_id in (None, ""):
        return error_response("Please provide trigger id", 400, "No trigger id")

    scenarios_dir = _get_scenarios_dir()
    from trpg_server.scenario_store import build_trigger_message, load_scenario_by_id

    _, scenario = load_scenario_by_id(scenarios_dir, info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    if not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")

    room_state = read_json(room_dir / "state.json", default={})
    new_trigger = find_trigger_definition(scenario, str(trigger_id))
    if new_trigger:
        validation = validate_trigger(str(trigger_id), {**(room_state if isinstance(room_state, dict) else {}), "scenario_version": info.get("scenario_version")}, scenario, audience="kp")
        if not validation.get("ok"):
            logger.info("trigger_rejected room_id=%s trigger_id=%s reason=%s", room_id, trigger_id, validation.get("reason"))
            return error_response("Trigger condition is not satisfied", 409, validation.get("reason", "Trigger rejected"))
        attachments = validation["trigger"].get("attachments", [])
        contents = []
        for attachment in attachments:
            resource = attachment.get("resourceRef") or {}
            if resource.get("content"):
                contents.append(str(resource["content"]))
            elif resource.get("url"):
                contents.append(f"[{resource.get('alt')}]({resource.get('url')})")
        trigger_message = {"sender_name": validation["trigger"].get("text") or f"触发器{trigger_id}", "content": "\n\n".join(contents), "metadata": {"trigger_id": trigger_id, "content_mode": "attachment"}}
    else:
        trigger_message = build_trigger_message(scenario, trigger_id)
    if not trigger_message:
        return error_response("Trigger not found", 404, "Trigger not found")

    message = {
        "id": uuid4().hex,
        "type": "trigger",
        "sender_id": None,
        "sender_name": trigger_message.get("sender_name", "触发器"),
        "avatar": normalize_avatar(trigger_message.get("avatar")),
        "content": trigger_message.get("content", ""),
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": trigger_message.get("metadata", {}),
    }
    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)
    if new_trigger:
        record_trigger(room_dir, str(trigger_id), reason=str(data.get("reason") or "manual trigger"))
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "触发了场景触发器"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        触发器ID=trigger_id,
    )
    return success_response(message, "Trigger executed successfully", 201)

@bp.route("/api/rooms/<room_id>/character-records", methods=["POST"])
def create_character_record(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    return _create_character_record_for_room(room_dir, info)

def _create_character_record_for_room(room_dir, info):
    data = request.get_json(silent=True) or {}
    record_type = str(data.get("type", "")).strip().lower()
    if record_type not in {"damage", "san", "skill", "item"}:
        return error_response("Invalid record type", 400, "Invalid record type")

    target = _find_member(info, user_id=data.get("user_id"), username=data.get("username"), active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if not target.get("character_card"):
        return error_response("Player has no bound character card", 400, "No bound character card")

    value = _bounded_int(data.get("value"), 1, 999, 1)
    reason = str(data.get("reason") or "未知").strip() or "未知"
    state = _ensure_character_state(target)
    record = {
        "id": uuid4().hex,
        "type": record_type,
        "value": value,
        "reason": reason[:200],
        "created_at": _timestamp(),
        "created_by": session.get("user_id"),
        "created_by_name": session.get("username", "user"),
    }

    if record_type == "damage":
        max_hp = max(1, _bounded_int(state.get("max_hp"), 1, 999, 1))
        before = _bounded_int(state.get("current_hp"), 0, max_hp, max_hp)
        major_wound = value >= max_hp / 2
        if value > max_hp:
            state["current_hp"] = 0
            state["dead"] = True
            state["dying"] = False
            state["unconscious"] = True
        else:
            state["current_hp"] = max(0, before - value)
        if major_wound:
            state["major_wound"] = True
        if state["current_hp"] <= 0 and not state.get("dead"):
            if state.get("major_wound"):
                state["dying"] = True
            else:
                state["unconscious"] = True
        if state.get("dead") or state.get("dying"):
            state["unconscious"] = True
        record["hp_before"] = before
        record["hp_after"] = state["current_hp"]
        record["major_wound"] = bool(state.get("major_wound"))
        record["dying"] = bool(state.get("dying"))
        record["dead"] = bool(state.get("dead"))
        state["injury_records"].insert(0, record)
        card = target.get("character_card")
        if isinstance(card, dict):
            card["currentHp"] = state["current_hp"]
            card["current_hp"] = state["current_hp"]
            status = card.get("status") if isinstance(card.get("status"), dict) else {}
            status["majorWound"] = bool(state.get("major_wound"))
            status["unconscious"] = bool(state.get("unconscious"))
            status["dead"] = bool(state.get("dead"))
            card["status"] = status
    elif record_type == "san":
        state["current_san"] = max(0, state["current_san"] - value)
        if isinstance(target.get("character_card"), dict):
            target["character_card"]["currentSan"] = state["current_san"]
            target["character_card"]["current_san"] = state["current_san"]
        record["san_after"] = state["current_san"]
        state["sanity_records"].insert(0, record)
    else:
        # skill / item change records (e.g. 技能增减、获得物品) feed the
        # "其他变化" line of the participating-module record on the player card.
        record["target"] = str(data.get("target") or record.get("name") or "").strip()[:80]
        record["change"] = str(data.get("change") or data.get("operation") or data.get("action") or "").strip()[:40] or "变化"
        record["amount"] = data.get("amount")
        collection = "skill_changes" if record_type == "skill" else "item_changes"
        state.setdefault(collection, []).insert(0, record)

    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "记录了角色状态变化"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        类型=record_type,
        目标玩家=target.get("username"),
        数值=value,
        原因=reason,
    )
    return success_response({"member": target, "record": record}, "Character record created", 201)

@bp.route("/api/rooms/by-name/<path:room_name>/character-records", methods=["POST"])
def create_character_record_by_room_name(room_name):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room_by_name(room_name)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return _create_character_record_for_room(room_dir, info)

@bp.route("/api/rooms/<room_id>/character-records/<record_id>", methods=["DELETE"])
def delete_character_record(room_id, record_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _is_elevated():
        return _denied(room_id)

    for member in info.get("members", []):
        state = _ensure_character_state(member)
        for collection_name in ("injury_records", "sanity_records", "skill_changes", "item_changes"):
            records = state.get(collection_name, [])
            next_records = [record for record in records if record.get("id") != record_id]
            if len(next_records) != len(records):
                state[collection_name] = next_records
                _recalculate_character_state(state)
                _write_room(room_dir, info)
                log_room_event(
                    room_dir,
                    user_action_text(session.get("username"), "删除了角色状态记录"),
                    actor=session.get("username"),
                    room_id=room_id,
                    logger_=logger,
                    用户ID=session.get("user_id"),
                    记录ID=record_id,
                )
                return success_response({"member": member}, "Character record deleted")

    return error_response("Record not found", 404, "Record not found")

@bp.route("/api/rooms/<room_id>/nodes", methods=["GET"])
def list_room_nodes(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    nodes_dir = room_dir / "nodes"
    nodes = []
    for node_file in nodes_dir.glob("*.json") if nodes_dir.exists() else []:
        if node_file.name == "autosave.json":
            continue
        node = read_json(node_file, default={})
        nodes.append(
            {
                "filename": node_file.name,
                "created_at": node.get("created_at", ""),
                "message_count": len(node.get("messages", [])),
                "automatic": bool(node.get("automatic")),
            }
        )
    nodes.sort(key=lambda item: item["created_at"], reverse=True)
    return success_response({"info": _room_summary(info), "nodes": nodes}, "Room nodes loaded successfully")

@bp.route("/api/rooms/<room_id>/nodes", methods=["POST"])
def create_room_node(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    timestamp = int(time.time() * 1000)
    node = {
        "filename": f"{timestamp}.json",
        "created_at": _timestamp(),
        "automatic": False,
        # 节点文件保留全量消息（供房主/管理员回溯），但响应只回当前查看者
        # 可见的部分，避免玩家用回档接口拿到别人的定向道具卡（B2）。
        "messages": _read_messages(room_dir),
    }
    write_json_atomic(room_dir / "nodes" / node["filename"], node)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "创建了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node["filename"],
    )
    return success_response(
        filter_payload_messages(node, info), "Room node created successfully", 201
    )

@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["GET"])
def get_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")
    node = read_json(node_file, default={})
    return success_response(
        filter_payload_messages(node, info), "Room node loaded successfully"
    )

@bp.route("/api/rooms/<room_id>/nodes/<node_filename>/restore", methods=["POST"])
def restore_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if room_interlude.is_active(info):
        return error_response("Interlude is active", 409, "幕间期间不能回溯存档节点")

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")

    node = read_json(node_file, default={"messages": []})
    _write_messages(room_dir, node.get("messages", []))
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "恢复了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node_filename,
    )
    return success_response(
        filter_payload_messages(node, info), "Room node restored successfully"
    )

@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["DELETE"])
def delete_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if node_file.exists():
        node_file.unlink()
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "删除了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node_filename,
    )
    return success_response(message="Room node deleted successfully")
