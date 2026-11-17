"""工作流授权：谁可以查看、决定、取消一个流程。

引擎本身只负责编排，不信任调用方传入的 ``actor`` 字符串；所有写入口在拿到
会话用户后都会经过这里的判定：

- ``save:<角色>`` / ``reaction:<角色>`` 步骤只能由该角色的控制者本人提交；
  AI 控制的角色由对应的 AI 执行者提交，KP / 房管可以代管；
- 普通步骤只能由流程发起者本人或 KP / 房管提交；
- 取消与强制超时同样限发起者本人、KP / 房管；
- 读接口按 ``flow_visible_to`` 裁剪，隐藏 NPC 的流程不会泄露给无关成员。

这里只做纯数据判定，不依赖 Flask，便于引擎、REST 与 AI 工具共用同一套规则。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from trpg_server.json_store import read_json

MANAGER_ROLES = frozenset({"owner", "admin"})
REACTION_PREFIX = "reaction:"
SAVE_PREFIX = "save:"
TARGET_PREFIXES = (REACTION_PREFIX, SAVE_PREFIX)

FORBIDDEN = "workflow.forbidden"


def load_room_info(room_dir: Any) -> dict[str, Any]:
    """读取房间 ``info.json``；目录缺失或损坏时返回空字典。"""
    if room_dir is None:
        return {}
    try:
        path = Path(room_dir) / "info.json"
        if not path.exists():
            return {}
        info = read_json(path, default={})
    except Exception:  # noqa: BLE001 - 授权读取失败按无成员处理，一律拒绝
        return {}
    return info if isinstance(info, dict) else {}


def active_members(info: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for member in (info or {}).get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        rows.append(member)
    return rows


def _member_names(member: Mapping[str, Any]) -> set[str]:
    card = member.get("character_card") if isinstance(member.get("character_card"), Mapping) else {}
    names = {
        card.get("name"),
        member.get("character_name"),
        member.get("username"),
    }
    return {str(name).strip().casefold() for name in names if str(name or "").strip()}


def member_for_character(
    info: Mapping[str, Any] | None, character: Any
) -> dict[str, Any] | None:
    """按角色名找控制者；找不到返回 ``None``（NPC / 隐藏单位）。"""
    key = str(character or "").strip().casefold()
    if not key:
        return None
    for member in active_members(info):
        if key in _member_names(member):
            return member
    return None


def is_manager(info: Mapping[str, Any] | None, user_id: Any) -> bool:
    target = str(user_id or "").strip()
    if not target or not info:
        return False
    if target == str(info.get("creator_id") or ""):
        return True
    for member in active_members(info):
        if str(member.get("user_id") or "") != target:
            continue
        return str(member.get("room_role") or "").strip().lower() in MANAGER_ROLES
    return False


def is_ai_controlled(member: Mapping[str, Any] | None) -> bool:
    if not isinstance(member, Mapping):
        return False
    if member.get("is_ai_player"):
        return True
    return str(member.get("control_mode") or "").strip().lower() == "ai"


def step_target_name(step: Mapping[str, Any] | None) -> str:
    """从 ``save:<角色>`` / ``reaction:<角色>`` 解析目标角色名。"""
    step_id = str((step or {}).get("id") or "")
    for prefix in TARGET_PREFIXES:
        if step_id.startswith(prefix):
            return step_id[len(prefix):].strip()
    return ""


def _character_controlled_by(info: Mapping[str, Any] | None, character: Any, user_id: str) -> bool:
    member = member_for_character(info, character)
    if member is None:
        return False
    return str(member.get("user_id") or "") == user_id


def _identity_ok(*, user_id: Any, agent_id: Any, manager: bool) -> bool:
    """KP（工具侧 ``agent_id == "kp"``）与管理者视为代管身份。"""
    if manager:
        return True
    return str(agent_id or "").strip().lower() == "kp"


def authorize_step(
    flow: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    user_id: Any = "",
    agent_id: Any = "",
    manager: bool = False,
    info: Mapping[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """判断 ``user_id`` 能否提交该步骤的决定；返回 ``(allowed, code, reason)``。"""
    uid = str(user_id or "").strip()
    if _identity_ok(user_id=uid, agent_id=agent_id, manager=manager):
        return True, "", ""
    if not uid:
        return False, FORBIDDEN, "无法确认操作者身份，已拒绝提交。"

    target = step_target_name(step)
    if not target:
        initiator = str(flow.get("initiator") or "").strip()
        if initiator and _character_controlled_by(info, initiator, uid):
            if not is_ai_controlled(member_for_character(info, initiator)):
                return True, "", ""
        return False, FORBIDDEN, "只有流程发起者本人或 KP 可以提交这一步的决定。"

    controller = member_for_character(info, target)
    if controller is None:
        return (
            False,
            FORBIDDEN,
            f"「{target}」不由玩家控制，只有 KP 可以代为决定。",
        )
    if is_ai_controlled(controller):
        if uid == str(controller.get("user_id") or "") and str(agent_id or "").strip():
            return True, "", ""
        return (
            False,
            FORBIDDEN,
            f"「{target}」由 AI 控制，只有该 AI 执行者或 KP 可以决定。",
        )
    if uid == str(controller.get("user_id") or ""):
        return True, "", ""
    return (
        False,
        FORBIDDEN,
        f"这一步属于「{target}」，只有该角色本人或 KP 可以提交决定。",
    )


def authorize_control(
    flow: Mapping[str, Any],
    *,
    user_id: Any = "",
    agent_id: Any = "",
    manager: bool = False,
    info: Mapping[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """取消 / 强制超时：只允许发起者本人、KP 或房管。"""
    uid = str(user_id or "").strip()
    if _identity_ok(user_id=uid, agent_id=agent_id, manager=manager):
        return True, "", ""
    if not uid:
        return False, FORBIDDEN, "无法确认操作者身份，已拒绝。"
    initiator = str(flow.get("initiator") or "").strip()
    if initiator and _character_controlled_by(info, initiator, uid):
        return True, "", ""
    return (
        False,
        FORBIDDEN,
        "只有流程发起者本人或 KP / 房管可以取消或强制收尾该流程。",
    )


def step_belongs_to(
    flow: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    user_id: Any = "",
    info: Mapping[str, Any] | None = None,
) -> bool:
    """该步骤的可见性：目标角色归属本人，或流程由本人角色发起。"""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    target = step_target_name(step)
    if target:
        return _character_controlled_by(info, target, uid)
    return _character_controlled_by(info, flow.get("initiator"), uid)


def flow_visible_to(
    flow: Mapping[str, Any],
    *,
    user_id: Any = "",
    agent_id: Any = "",
    manager: bool = False,
    info: Mapping[str, Any] | None = None,
) -> bool:
    """流程是否对调用者可见；KP / 房管可见全部。"""
    if _identity_ok(user_id=user_id, agent_id=agent_id, manager=manager):
        return True
    uid = str(user_id or "").strip()
    if not uid:
        return False
    if _character_controlled_by(info, flow.get("initiator"), uid):
        return True
    for step in flow.get("steps") or []:
        if isinstance(step, Mapping) and step_belongs_to(flow, step, user_id=uid, info=info):
            return True
    return False


__all__ = [
    "FORBIDDEN",
    "MANAGER_ROLES",
    "REACTION_PREFIX",
    "SAVE_PREFIX",
    "active_members",
    "authorize_control",
    "authorize_step",
    "flow_visible_to",
    "is_ai_controlled",
    "is_manager",
    "load_room_info",
    "member_for_character",
    "step_belongs_to",
    "step_target_name",
]
