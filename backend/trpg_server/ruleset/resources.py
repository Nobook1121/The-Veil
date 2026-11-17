"""通用资源与效果壳（E0 资源/效果契约）。

核心只做四件事：命名空间化的键值、加减与夹紧、快照、阈值事件广播。
资源的语义（HP / SAN / 法术位 / Chaos / 奖励点……）由插件声明。

``ResourceSlot`` 保留旧槽位字段（``state/mirror/cap_state/cap_derived``），
因此现有 CoC7 读写路径可以原样使用；新增字段都有中性默认值：

- ``scope``：character / party / room / gm
- ``shape``：gauge / counter / pool / flag / rating
- ``thresholds``：越过阈值时由核心发出事件，插件决定怎么处理
- ``visibility``：public / owner / gm

``EffectInstance`` 只定义存储与 tick 契约，V1 不把它接进房间业务流。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

RESOURCE_SLOT_VERSION = 2
RESOURCE_DELTA_VERSION = 1
EFFECT_INSTANCE_VERSION = 1

VALID_SCOPES = frozenset({"character", "party", "room", "gm"})
VALID_SHAPES = frozenset({"gauge", "counter", "pool", "flag", "rating"})
VALID_VISIBILITIES = frozenset({"public", "owner", "gm"})
VALID_THRESHOLD_DIRECTIONS = frozenset({"below", "above", "cross"})
VALID_DURATION_UNITS = frozenset({"turn", "round", "minute", "scene", "rest", "permanent"})
VALID_PERSISTENCE = frozenset({"removable", "rest", "irreversible"})


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bounded(value: Any, minimum: int, maximum: int | None, fallback: int) -> int:
    parsed = _as_int(value, fallback)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return max(minimum, parsed)


def _normalize_thresholds(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        event = str(item.get("event") or "").strip()
        if not event:
            continue
        direction = str(item.get("when") or item.get("direction") or "below").strip().lower()
        if direction not in VALID_THRESHOLD_DIRECTIONS:
            direction = "below"
        result.append(
            {
                "at": _as_int(item.get("at"), 0),
                "when": direction,
                "event": event,
                "detail": dict(item.get("detail") or {}),
            }
        )
    return tuple(result)


def normalize_slot(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把旧/新槽位声明归一成同一形状；未知字段原样保留。"""
    slot = dict(raw or {})
    kind = str(slot.get("kind") or "").strip().lower()
    slot["kind"] = kind
    scope = str(slot.get("scope") or "character").strip().lower()
    slot["scope"] = scope if scope in VALID_SCOPES else "character"
    shape = str(slot.get("shape") or "gauge").strip().lower()
    slot["shape"] = shape if shape in VALID_SHAPES else "gauge"
    visibility = str(slot.get("visibility") or "public").strip().lower()
    slot["visibility"] = visibility if visibility in VALID_VISIBILITIES else "public"
    slot["state"] = str(slot.get("state") or f"current_{kind}")
    slot["mirror"] = str(slot.get("mirror") or "")
    slot["cap_state"] = str(slot.get("cap_state") or f"max_{kind}")
    slot["cap_derived"] = str(slot.get("cap_derived") or "")
    slot["initial_state"] = str(slot.get("initial_state") or "")
    slot["initial_mirror"] = str(slot.get("initial_mirror") or "")
    slot["cap_min"] = _as_int(slot.get("cap_min"), 0)
    if slot.get("cap_max") is not None:
        slot["cap_max"] = _as_int(slot.get("cap_max"), slot["cap_min"])
    slot["mirror_aliases"] = tuple(str(item) for item in (slot.get("mirror_aliases") or ()))
    slot["thresholds"] = _normalize_thresholds(slot.get("thresholds"))
    slot.setdefault("label_i18n", "")
    slot["slot_version"] = RESOURCE_SLOT_VERSION
    return slot


def normalize_slots(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(normalize_slot(slot) for slot in raw if isinstance(slot, Mapping))


@dataclass(frozen=True)
class ThresholdEvent:
    """一次资源变化跨过插件声明阈值时产生的事件；核心不解释 ``event``。"""

    kind: str
    event: str
    threshold: int
    direction: str
    before: int
    after: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "event": self.event,
            "threshold": self.threshold,
            "direction": self.direction,
            "before": self.before,
            "after": self.after,
            "detail": dict(self.detail),
        }


def threshold_events(
    slot: Mapping[str, Any],
    before: int,
    after: int,
) -> tuple[ThresholdEvent, ...]:
    """返回从 ``before`` 到 ``after`` 跨过的阈值事件（只报跨越，不报停留）。"""
    kind = str(slot.get("kind") or "")
    events: list[ThresholdEvent] = []
    seen: set[tuple[str, int]] = set()
    for item in _normalize_thresholds(slot.get("thresholds")):
        at = int(item["at"])
        direction = str(item["when"])
        crossed = (
            (direction == "below" and after <= at < before)
            or (direction == "above" and before < at <= after)
            or (direction == "cross" and ((before < at <= after) or (after <= at < before)))
        )
        key = (str(item["event"]), at)
        if not crossed or key in seen:
            continue
        seen.add(key)
        events.append(
            ThresholdEvent(
                kind=kind,
                event=str(item["event"]),
                threshold=at,
                direction=direction,
                before=before,
                after=after,
                detail=dict(item.get("detail") or {}),
            )
        )
    return tuple(events)


@dataclass(frozen=True)
class ResourceDelta:
    """规则无关的资源变更请求；``value`` 优先于 ``delta``。"""

    kind: str
    delta: int | None = None
    value: int | None = None
    scope: str = "character"
    reason: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_version": RESOURCE_DELTA_VERSION,
            "kind": self.kind,
            "delta": self.delta,
            "value": self.value,
            "scope": self.scope,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResourceChange:
    """一次资源变更的结算结果，供房间日志与插件阈值回调使用。"""

    kind: str
    before: int
    after: int
    requested: int
    applied: int
    clamped: bool
    events: tuple[ThresholdEvent, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "requested": self.requested,
            "applied": self.applied,
            "clamped": self.clamped,
            "events": [event.to_dict() for event in self.events],
        }


def apply_delta(
    slot: Mapping[str, Any],
    current: Any,
    request: ResourceDelta,
) -> ResourceChange:
    """按槽位上限夹紧并返回变更结果；不写任何存储。"""
    cap_min = _as_int(slot.get("cap_min"), 0)
    cap_max = slot.get("cap_max")
    maximum = _as_int(cap_max, 10**9) if cap_max is not None else None
    before = _bounded(current, cap_min, maximum, cap_min)
    if request.value is not None:
        target = _as_int(request.value, before)
    else:
        target = before + _as_int(request.delta, 0)
    after = _bounded(target, cap_min, maximum, before)
    return ResourceChange(
        kind=str(slot.get("kind") or request.kind),
        before=before,
        after=after,
        requested=target,
        applied=after - before,
        clamped=after != target,
        events=threshold_events(slot, before, after),
    )


@dataclass(frozen=True)
class EffectInstance:
    """一个带作用域、持续时间与持久性的效果实例（不绑定具体规则语义）。"""

    id: str
    kind: str
    scope: str = "character"
    target: str = ""
    source: str = ""
    stacks: int = 1
    value: int | None = None
    duration: dict[str, Any] = field(default_factory=dict)
    persistence: str = "removable"
    modifiers: tuple[dict[str, Any], ...] = ()
    visibility: str = "public"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def irreversible(self) -> bool:
        return self.persistence == "irreversible"

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_version": EFFECT_INSTANCE_VERSION,
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "target": self.target,
            "source": self.source,
            "stacks": self.stacks,
            "value": self.value,
            "duration": dict(self.duration),
            "persistence": self.persistence,
            "modifiers": [dict(item) for item in self.modifiers],
            "visibility": self.visibility,
            "detail": dict(self.detail),
        }


def normalize_effect(raw: Mapping[str, Any]) -> EffectInstance:
    scope = str(raw.get("scope") or "character").strip().lower()
    visibility = str(raw.get("visibility") or "public").strip().lower()
    persistence = str(raw.get("persistence") or "removable").strip().lower()
    duration = raw.get("duration")
    duration = dict(duration) if isinstance(duration, Mapping) else {}
    unit = str(duration.get("unit") or "permanent").strip().lower()
    duration["unit"] = unit if unit in VALID_DURATION_UNITS else "permanent"
    modifiers = raw.get("modifiers")
    return EffectInstance(
        id=str(raw.get("id") or ""),
        kind=str(raw.get("kind") or ""),
        scope=scope if scope in VALID_SCOPES else "character",
        target=str(raw.get("target") or ""),
        source=str(raw.get("source") or ""),
        stacks=_as_int(raw.get("stacks"), 1),
        value=_optional_int(raw.get("value")),
        duration=duration,
        persistence=persistence if persistence in VALID_PERSISTENCE else "removable",
        modifiers=tuple(dict(item) for item in modifiers if isinstance(item, Mapping))
        if isinstance(modifiers, (list, tuple))
        else (),
        visibility=visibility if visibility in VALID_VISIBILITIES else "public",
        detail=dict(raw.get("detail") or {}),
    )


def tick_effects(
    effects: Iterable[Mapping[str, Any]],
    *,
    phase: str = "turn",
    timing: str | None = None,
    amount: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按回合/轮/场景/分钟推进效果持续时间，返回 ``(remaining, expired)``。

    ``persistence=irreversible`` 或 ``duration.unit=permanent`` 的效果永不自动过期。
    ``timing`` 非空时只推进「结束时点」匹配的效果（``turn_start`` / ``turn_end`` /
    ``round_end`` / ``scene_end``），未声明时点的效果对任意时点生效。
    ``amount`` 是一次推进的单位数（非战斗按分钟显式推进时用）。
    纯函数，不写存储；业务流通过 :mod:`trpg_server.ruleset.effects` 调用。
    """
    step = max(1, int(amount or 1))
    remaining: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for raw in effects:
        if not isinstance(raw, Mapping):
            continue
        effect = dict(raw)
        duration = dict(effect.get("duration") or {})
        unit = str(duration.get("unit") or "permanent")
        if effect.get("persistence") == "irreversible" or unit == "permanent" or unit != phase:
            remaining.append(effect)
            continue
        if timing:
            declared_timing = str(duration.get("timing") or "")
            if declared_timing and declared_timing != timing:
                remaining.append(effect)
                continue
        left = _as_int(duration.get("remaining"), 1) - step
        duration["remaining"] = max(0, left)
        effect["duration"] = duration
        if left <= 0:
            expired.append(effect)
        else:
            remaining.append(effect)
    return remaining, expired


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "EFFECT_INSTANCE_VERSION",
    "RESOURCE_DELTA_VERSION",
    "RESOURCE_SLOT_VERSION",
    "VALID_DURATION_UNITS",
    "VALID_PERSISTENCE",
    "VALID_SCOPES",
    "VALID_SHAPES",
    "VALID_THRESHOLD_DIRECTIONS",
    "VALID_VISIBILITIES",
    "EffectInstance",
    "ResourceChange",
    "ResourceDelta",
    "ThresholdEvent",
    "apply_delta",
    "normalize_effect",
    "normalize_slot",
    "normalize_slots",
    "threshold_events",
    "tick_effects",
]
