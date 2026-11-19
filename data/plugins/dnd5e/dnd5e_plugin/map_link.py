"""dnd5e 战斗与 veil-map 棋子同步的插件侧胶水。

核心只按规则集调用 ``on_combat_saved``；地图插件是否加载、当前房间是否有
地图都由这里探测。地图不可用时返回可读状态，不阻断战斗。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _map_service() -> Any | None:
    try:
        from trpg_server.plugins.runtime import get_service

        return get_service("veil-map", "map_service")
    except Exception:  # noqa: BLE001 - 核心未安装插件运行期时静默降级
        return None


def sync_combat_tokens(context: Any, combat: Any) -> dict[str, Any]:
    """把战斗者状态同步到当前地图棋子；失败只记日志，不影响战斗。"""
    if not isinstance(combat, dict) or not combat.get("id"):
        return {"ok": True, "status": "skipped", "reason": "no_combat"}
    room_dir = getattr(context, "room_dir", None)
    if not room_dir:
        return {"ok": True, "status": "skipped", "reason": "no_room"}
    service = _map_service()
    if service is None or not callable(getattr(service, "sync_combat_tokens", None)):
        return {"ok": True, "status": "skipped", "reason": "map_plugin_missing"}
    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else room_info
    try:
        return service.sync_combat_tokens(room_dir, info if isinstance(info, dict) else {}, combat)
    except Exception:  # noqa: BLE001 - 棋子同步失败不能影响战斗成立
        logger.warning("dnd5e_map_token_sync_failed", exc_info=True)
        return {"ok": False, "status": "error", "error": "地图棋子同步失败。"}


__all__ = ["sync_combat_tokens"]
