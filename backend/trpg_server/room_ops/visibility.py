"""消息可见性：把道具收件人过滤下沉到读取层。

``messages.json`` 保存的是全量消息（含只发给部分玩家的 ``type=prop`` 卡片）。
所有把消息交给客户端的出口都必须经过这里，避免只修了 ``/messages`` 而
房间详情、回档节点、自动存档、团录导出等入口各漏一遍（B2）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from flask import session

from trpg_server.props import policy as props_policy
from trpg_server.room_ops.access import _find_member, _is_elevated, _room_permission
from trpg_server.room_ops.constants import ROOM_ROLE_ADMIN, ROOM_ROLE_OWNER
from trpg_server.room_ops.storage import _read_messages


def viewer_is_kp(info: Mapping[str, Any] | None, viewer_id: Any = None) -> bool:
    """站点管理员，或该房间的房主/房管，视为 KP 视角。"""
    if _is_elevated():
        return True
    uid = session.get("user_id") if viewer_id is None else viewer_id
    member = _find_member(info or {}, user_id=uid, active_only=True)
    return _room_permission(member, info or {}) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def visible_messages(
    room_dir: str | Path,
    info: Mapping[str, Any] | None = None,
    viewer_id: Any = None,
    *,
    is_kp: bool | None = None,
) -> list[Mapping[str, Any]]:
    """读取房间消息并按查看者过滤；``viewer_id`` 缺省取当前会话。"""
    uid = session.get("user_id") if viewer_id is None else viewer_id
    if is_kp is None:
        is_kp = viewer_is_kp(info, uid)
    return props_policy.filter_messages(_read_messages(room_dir), uid, is_kp=is_kp)


def filter_payload_messages(
    payload: Any,
    info: Mapping[str, Any] | None = None,
    viewer_id: Any = None,
    *,
    is_kp: bool | None = None,
) -> Any:
    """过滤节点/自动存档这类 ``{"messages": [...]}`` 结构，返回浅拷贝。

    非映射或不含消息列表时原样返回，调用方无需额外判断。
    """
    if not isinstance(payload, Mapping):
        return payload
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return dict(payload)
    uid = session.get("user_id") if viewer_id is None else viewer_id
    if is_kp is None:
        is_kp = viewer_is_kp(info, uid)
    result = dict(payload)
    result["messages"] = props_policy.filter_messages(messages, uid, is_kp=is_kp)
    return result


__all__ = ["filter_payload_messages", "viewer_is_kp", "visible_messages"]
