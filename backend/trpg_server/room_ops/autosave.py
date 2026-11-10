"""自动存档与后台调度：关键帧、节点写入、定时存档、非活跃房间回收。"""
from flask import current_app
from pathlib import Path
from trpg_server.json_store import read_json
from trpg_server.json_store import write_json_atomic
from trpg_server.logging_config import user_action_text
from trpg_server.room_card_occupancy import card_binding_lock
from trpg_server.room_log import log_room_event
from trpg_server.room_ops.access import _room_has_online_member
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.storage import (
    _autosave_node_limit,
    _autosave_settings,
    _get_rooms_dir,
    _iter_room_dirs,
    _read_messages,
    _read_room,
    _room_cleanup_days,
    _timestamp,
)
from trpg_server.settings import HISTORY_DIR
import time

def _keyframe_for_autosave(room_dir):
    """自动存档前写入关键帧（§6.4.2）；失败只记日志，不阻断存档。"""
    try:
        from trpg_server.agents.narrative.state_store import write_keyframe

        state = read_json(Path(room_dir) / "state.json", default={})
        if isinstance(state, dict) and isinstance(state.get("narrative"), dict) and state["narrative"]:
            write_keyframe(room_dir, reason="autosave")
    except Exception:  # noqa: BLE001 - 关键帧失败不影响自动存档
        logger.warning("narrative.keyframe.autosave_failed room=%s", getattr(room_dir, "name", room_dir), exc_info=True)

def _write_room_autosave(room_dir, room_id, messages, *, username=None, user_id=None):
    """写入自动存档节点并裁剪旧节点。

    ``username`` 为空表示由服务端后台调度触发（记录为系统动作）；传入时保留
    玩家手动触发路径的操作日志。
    """
    timestamp = int(time.time() * 1000)
    autosave = {
        "filename": f"autosave-{timestamp}.json",
        "updated_at": _timestamp(),
        "created_at": _timestamp(),
        "automatic": True,
        "messages": messages,
    }
    _keyframe_for_autosave(room_dir)
    nodes_dir = room_dir / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(nodes_dir / autosave["filename"], autosave)
    write_json_atomic(room_dir / "autosave.json", autosave)
    automatic_nodes = []
    for node_file in nodes_dir.glob("autosave-*.json"):
        node = read_json(node_file, default={})
        if node.get("automatic"):
            automatic_nodes.append((str(node.get("created_at") or ""), node_file))
    automatic_nodes.sort(key=lambda item: item[0], reverse=True)
    for _, old_file in automatic_nodes[_autosave_node_limit():]:
        old_file.unlink(missing_ok=True)
    if username:
        log_room_event(
            room_dir,
            user_action_text(username, "保存了房间自动存档"),
            actor=username,
            room_id=room_id,
            logger_=logger,
            level="debug",
            用户ID=user_id,
            消息数=len(messages),
        )
    else:
        log_room_event(
            room_dir,
            "系统保存了房间自动存档",
            room_id=room_id,
            logger_=logger,
            level="debug",
            消息数=len(messages),
        )
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_autosave_created", {"room_id": room_id, "filename": autosave["filename"]}, room=room_id)
    return autosave

def run_scheduled_autosaves():
    """后台调度入口：为开启自动存档且到期的房间写入自动存档节点。

    由 app_factory 启动的守护线程周期调用，需要处于 Flask 应用上下文中。
    """
    settings = _autosave_settings()
    if not settings["enabled"]:
        return
    interval = settings["interval"]
    now = time.time()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if not info or info.get("archived"):
            continue
        # 仅当房间内确实有「已进入该房间且在线」的玩家时才自动存档：
        # 玩家在线但没进入这个房间，不应对该房间产生自动存档。
        if not _room_has_online_member(info):
            continue
        autosave_path = room_dir / "autosave.json"
        try:
            if autosave_path.exists() and now - autosave_path.stat().st_mtime < interval:
                continue
        except OSError:
            continue
        messages = _read_messages(room_dir)
        if not messages:
            continue
        room_id = info.get("id") or room_dir.name
        try:
            _write_room_autosave(room_dir, room_id, messages)
        except OSError:
            logger.debug("Scheduled autosave failed for room %s", room_id, exc_info=True)

def run_inactive_room_cleanup():
    """后台调度入口：按 [rooms] cleanup_inactive_days 回收未激活房间。

    由 app_factory 启动的守护线程在服务启动后先跑一次，之后按天轮询；
    需要处于 Flask 应用上下文中。与加入/换卡共用占用锁，避免在玩家加入的
    瞬间把房间删掉。
    """
    days = _room_cleanup_days()
    if days <= 0:
        return []
    from trpg_server import room_admin

    history_dir = current_app.config.get("HISTORY_DIR", HISTORY_DIR)
    with card_binding_lock():
        return room_admin.cleanup_inactive_rooms(_get_rooms_dir(), days, history_dir=history_dir)

def save_all_rooms_on_shutdown():
    """服务正常关闭（stop/exit 等）前为每个房间写一个自动存档节点。

    与后台调度不同：忽略 ``[autosave] enabled`` 与间隔限制，确保退出前最后
    一段对话一定落盘；内容与最近一次自动存档完全相同的房间跳过，避免产生
    无意义的重复节点。需要处于 Flask 应用上下文中。
    """

    saved = 0
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if not info or info.get("archived"):
            continue
        messages = _read_messages(room_dir)
        if not messages:
            continue
        existing = read_json(room_dir / "autosave.json", default={})
        if isinstance(existing, dict) and existing.get("messages") == messages:
            continue
        room_id = info.get("id") or room_dir.name
        try:
            _write_room_autosave(room_dir, room_id, messages)
            saved += 1
        except Exception:  # noqa: BLE001 - 单个房间失败不能阻断关闭流程
            logger.warning("Shutdown autosave failed for room %s", room_id, exc_info=True)
    if saved:
        logger.info("Saved shutdown autosaves for %d room(s)", saved)
    return saved
