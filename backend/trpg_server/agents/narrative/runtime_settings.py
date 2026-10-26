"""房间级叙事运行档位解析（§12.5）：profile / guard / kill switch。

优先级：房间房规（``house_rules``）> 全局默认（代码常量）。三个键都允许缺省，
缺省表示「跟随默认」，因此老房间无需迁移。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trpg_server.agents.narrative.guards import GuardPolicy
from trpg_server.agents.narrative.logger import log_event, log_warning

logger = logging.getLogger(__name__)

PROFILE_GRAPH = "graph"
PROFILE_DEGRADED = "degraded"
PROFILES = (PROFILE_GRAPH, PROFILE_DEGRADED)

GUARD_ENFORCE = "enforce"
GUARD_SHADOW = "shadow"
GUARD_OFF = "off"
GUARDS = (GUARD_ENFORCE, GUARD_SHADOW, GUARD_OFF)

_BYPASS_LOGGED: set[str] = set()


@dataclass(frozen=True)
class NarrativeSettings:
    """一轮请求实际生效的图谱档位。"""

    enabled: bool = True
    profile: str = PROFILE_GRAPH
    guard: str = GUARD_ENFORCE
    source: str = "default"

    @property
    def graph_enabled(self) -> bool:
        return self.enabled and self.profile == PROFILE_GRAPH

    def guard_policy(self) -> GuardPolicy:
        return GuardPolicy.for_mode(self.guard)


def _normalize(value: Any, allowed: tuple[str, ...]) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else None


def resolve_narrative_settings(room_info: Mapping[str, Any] | None) -> NarrativeSettings:
    """从房间 ``info.json`` 的房规读取档位；非法值按「跟随默认」处理。"""
    info = room_info if isinstance(room_info, Mapping) else {}
    rules = info.get("house_rules")
    rules = rules if isinstance(rules, Mapping) else {}
    enabled_value = rules.get("narrative_enabled")
    enabled = True if enabled_value is None else bool(enabled_value)
    profile = _normalize(rules.get("narrative_profile"), PROFILES) or PROFILE_GRAPH
    guard = _normalize(rules.get("narrative_guard"), GUARDS) or GUARD_ENFORCE
    source = "room" if any(key in rules for key in ("narrative_enabled", "narrative_profile", "narrative_guard")) else "default"
    return NarrativeSettings(enabled=enabled, profile=profile, guard=guard, source=source)


def log_bypass_once(room_id: Any) -> None:
    """kill switch 只在首次生效时记一条日志，避免每轮刷屏。"""
    key = str(room_id or "-")
    if key in _BYPASS_LOGGED:
        return
    _BYPASS_LOGGED.add(key)
    log_warning("narrative.bypass", room_id=key, reason="room_kill_switch")
    log_event("narrative.profile", room_id=key, profile=PROFILE_DEGRADED, guard=GUARD_OFF, source="kill_switch")


def reset_bypass_log() -> None:
    _BYPASS_LOGGED.clear()


__all__ = [
    "GUARDS",
    "GUARD_ENFORCE",
    "GUARD_OFF",
    "GUARD_SHADOW",
    "NarrativeSettings",
    "PROFILES",
    "PROFILE_DEGRADED",
    "PROFILE_GRAPH",
    "log_bypass_once",
    "reset_bypass_log",
    "resolve_narrative_settings",
]
