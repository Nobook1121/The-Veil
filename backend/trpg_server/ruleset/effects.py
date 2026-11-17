"""通用效果容器与 tick/阈值接线（E5/E6 核心壳）。

核心只做容器与通用机制：效果实例的增删查、持续时间推进、结束时点、作用域、
资源阈值跨越事件的转发。效果叫什么、怎么判定、什么时候结束，全部由插件在
``effects`` 能力对象里决定；本模块不出现任何具体规则的条件名或豁免规则。

CoC7 插件没有 ``effects`` 能力，因此调度器与工具层调用到这里时保持空操作，
原有 SAN / 状态 / 战斗路径不变。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from uuid import uuid4

from trpg_server.ruleset.resources import (
    ResourceChange,
    ResourceDelta,
    ThresholdEvent,
    apply_delta,
    normalize_effect,
    tick_effects,
)

EFFECT_TICK_PHASES = ("turn_start", "turn_end", "round_end", "scene_end")


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _effect_rules(plugin: Any) -> Any:
    return getattr(plugin, "effects", None)


def effects_of(
    holder: Mapping[str, Any] | None,
    *,
    kind: str | None = None,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """返回持有者上的效果实例（浅拷贝）；``kind`` / ``scope`` 为空时不过滤。"""
    raw = holder.get("effects") if isinstance(holder, Mapping) else None
    if not isinstance(raw, list):
        return []
    wanted_kind = str(kind or "").strip().lower()
    wanted_scope = str(scope or "").strip().lower()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        effect = dict(item)
        if wanted_kind and str(effect.get("kind") or "").strip().lower() != wanted_kind:
            continue
        if wanted_scope and str(effect.get("scope") or "").strip().lower() != wanted_scope:
            continue
        result.append(effect)
    return result


def _effects_bucket(holder: dict[str, Any]) -> list[dict[str, Any]]:
    raw = holder.get("effects")
    if not isinstance(raw, list):
        raw = []
        holder["effects"] = raw
    return raw


def _identity(effect: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(effect.get("kind") or "").strip().lower(),
        str(effect.get("source") or "").strip(),
        str(effect.get("scope") or "").strip().lower(),
    )


def add_effect(holder: dict[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """归一后写入效果列表：同 ``id`` 覆盖，同 ``kind+source`` 叠加 ``stacks``。"""
    effect = normalize_effect(raw)
    data = effect.to_dict()
    if not data.get("id"):
        data["id"] = f"{data.get('kind') or 'effect'}:{uuid4().hex[:8]}"
    bucket = _effects_bucket(holder)
    for index, item in enumerate(bucket):
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == data["id"]:
            bucket[index] = data
            return data
        if _identity(item) == _identity(data) and data.get("kind"):
            stacks = max(1, _as_int(item.get("stacks"), 1)) + max(1, _as_int(data.get("stacks"), 1))
            merged = dict(item)
            merged["stacks"] = stacks
            merged["duration"] = _longer_duration(item.get("duration"), data.get("duration"))
            merged["detail"] = {**dict(item.get("detail") or {}), **dict(data.get("detail") or {})}
            bucket[index] = merged
            return merged
    bucket.append(data)
    return data


def _longer_duration(left: Any, right: Any) -> dict[str, Any]:
    first = dict(left) if isinstance(left, Mapping) else {}
    second = dict(right) if isinstance(right, Mapping) else {}
    if not first:
        return second
    if not second:
        return first
    if _as_int(first.get("remaining"), 0) >= _as_int(second.get("remaining"), 0):
        return first
    return second


def remove_effect(
    holder: dict[str, Any],
    *,
    effect_id: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """按 id / kind / source / scope 组合移除效果，返回被移除的实例。"""
    bucket = _effects_bucket(holder)
    wanted_id = str(effect_id or "").strip()
    wanted_kind = str(kind or "").strip().lower()
    wanted_source = str(source or "").strip()
    wanted_scope = str(scope or "").strip().lower()
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for item in bucket:
        if not isinstance(item, dict):
            continue
        matched = True
        if wanted_id:
            matched = matched and str(item.get("id") or "") == wanted_id
        if wanted_kind:
            matched = matched and str(item.get("kind") or "").strip().lower() == wanted_kind
        if wanted_source:
            matched = matched and str(item.get("source") or "").strip() == wanted_source
        if wanted_scope:
            matched = matched and str(item.get("scope") or "").strip().lower() == wanted_scope
        if matched and (wanted_id or wanted_kind or wanted_source or wanted_scope):
            removed.append(item)
        else:
            kept.append(item)
    if removed:
        holder["effects"] = kept
    return removed


def _slot_of(plugin: Any, kind: str) -> dict[str, Any] | None:
    if plugin is None:
        return None
    from trpg_server.ruleset.capabilities import resource_slot

    return resource_slot(plugin, kind)


def _holder_key(holder: Mapping[str, Any], slot: Mapping[str, Any]) -> str:
    for key in (slot.get("state"), slot.get("mirror"), slot.get("kind")):
        name = str(key or "").strip()
        if name and name in holder:
            return name
    return str(slot.get("state") or slot.get("kind") or "")


def _call_threshold_hook(
    rules: Any,
    holder: dict[str, Any],
    event: ThresholdEvent,
    *,
    combat: Mapping[str, Any] | None,
    rng: Any,
) -> list[str]:
    hook = getattr(rules, "on_threshold", None)
    if not callable(hook):
        return []
    try:
        result = hook(holder, event.to_dict(), combat=combat, rng=rng)
    except TypeError:
        try:
            result = hook(holder, event.to_dict())
        except Exception:  # noqa: BLE001 - 单个阈值钩子不能击穿主流程
            return []
    except Exception:  # noqa: BLE001
        return []
    return _notes_of(result, holder)


def _notes_of(result: Any, holder: dict[str, Any]) -> list[str]:
    if result is None:
        return []
    if isinstance(result, str):
        return [result] if result.strip() else []
    if isinstance(result, (list, tuple)):
        return [str(item) for item in result if str(item or "").strip()]
    if isinstance(result, Mapping):
        for effect in result.get("effects") or []:
            if isinstance(effect, Mapping):
                add_effect(holder, effect)
        notes = result.get("notes")
        if isinstance(notes, str):
            return [notes] if notes.strip() else []
        return [str(item) for item in notes or [] if str(item or "").strip()]
    return []


def apply_resource_delta(
    holder: dict[str, Any],
    plugin: Any,
    kind: str,
    request: ResourceDelta,
    *,
    combat: Mapping[str, Any] | None = None,
    rng: Any = None,
) -> tuple[ResourceChange | None, list[str]]:
    """按插件声明的资源槽位结算一次增减，并把阈值事件转给插件。

    键解析顺序 ``slot.state → slot.mirror → slot.kind``：角色快照用
    ``current_hp``，战斗者用 ``hp``，核心不写死任何一侧。
    """
    slot = _slot_of(plugin, kind)
    if not slot:
        return None, []
    key = _holder_key(holder, slot)
    if not key:
        return None, []
    effective = dict(slot)
    if effective.get("cap_max") is None:
        cap_key = str(slot.get("cap_state") or "")
        if cap_key and holder.get(cap_key) is not None:
            effective["cap_max"] = _as_int(holder.get(cap_key), 0)
    change = apply_delta(effective, holder.get(key, slot.get("cap_min", 0)), request)
    holder[key] = change.after
    rules = _effect_rules(plugin)
    notes: list[str] = []
    for event in change.events:
        notes.extend(_call_threshold_hook(rules, holder, event, combat=combat, rng=rng))
    return change, notes


def tick_holder(
    holder: dict[str, Any],
    *,
    phase: str,
    timing: str | None = None,
    amount: int = 1,
    plugin: Any = None,
    combat: Mapping[str, Any] | None = None,
    rng: Any = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """推进持有者身上的效果：持续时间 → 插件 tick → 过期/移除回调。

    返回 ``(notes, expired)``；没有 ``effects`` 列表时是空操作。
    ``amount`` 用于非战斗按分钟显式推进。
    """
    current = effects_of(holder)
    if not current:
        return [], []
    remaining, expired = tick_effects(current, phase=phase, timing=timing, amount=amount)
    rules = _effect_rules(plugin)
    notes: list[str] = []
    kept: list[dict[str, Any]] = []
    pending: list[Mapping[str, Any]] = []
    resource_requests: list[Mapping[str, Any]] = []
    remove = getattr(rules, "tick", None)
    for effect in remaining:
        decision: Any = None
        if callable(remove):
            try:
                decision = remove(
                    holder,
                    effect,
                    phase=phase,
                    timing=timing,
                    combat=combat,
                    rng=rng,
                )
            except TypeError:
                try:
                    decision = remove(holder, effect, phase=phase, combat=combat, rng=rng)
                except Exception:  # noqa: BLE001 - 插件 tick 失败不影响持续时间推进
                    decision = None
            except Exception:  # noqa: BLE001
                decision = None
        if isinstance(decision, Mapping):
            notes.extend(str(item) for item in decision.get("notes") or [] if str(item or "").strip())
            pending.extend(
                item for item in decision.get("effects") or [] if isinstance(item, Mapping)
            )
            resource = decision.get("resource")
            if isinstance(resource, Mapping):
                resource_requests.append(resource)
            elif isinstance(resource, (list, tuple)):
                resource_requests.extend(item for item in resource if isinstance(item, Mapping))
            if decision.get("remove"):
                expired.append(effect)
                continue
        else:
            notes.extend(_notes_of(decision, holder))
        kept.append(effect)

    holder["effects"] = kept
    expired_notes = _expire(rules, holder, expired)
    notes.extend(expired_notes)
    for effect in pending:
        add_effect(holder, effect)

    for request in resource_requests:
        kind = str(request.get("kind") or "").strip()
        if not kind:
            continue
        delta = ResourceDelta(
            kind=kind,
            delta=int(request["delta"]) if request.get("delta") is not None else None,
            value=int(request["value"]) if request.get("value") is not None else None,
            reason=str(request.get("reason") or "effect"),
            source=str(request.get("source") or ""),
        )
        _change, threshold_notes = apply_resource_delta(
            holder, plugin, kind, delta, combat=combat, rng=rng
        )
        notes.extend(threshold_notes)
    return notes, expired


def _expire(rules: Any, holder: dict[str, Any], expired: Iterable[Mapping[str, Any]]) -> list[str]:
    hook = getattr(rules, "on_expired", None)
    if not callable(hook):
        return []
    notes: list[str] = []
    for effect in expired:
        try:
            result = hook(holder, dict(effect))
        except Exception:  # noqa: BLE001 - 过期回调失败不影响主流程
            continue
        notes.extend(_notes_of(result, holder))
    return notes


def clear_effects(
    holder: dict[str, Any],
    *,
    plugin: Any = None,
    scope: str | None = None,
) -> list[str]:
    """清除可移除效果（``persistence=removable``），保留休息/不可逆效果。"""
    bucket = _effects_bucket(holder)
    wanted_scope = str(scope or "").strip().lower()
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in bucket:
        if not isinstance(item, dict):
            continue
        in_scope = not wanted_scope or str(item.get("scope") or "").strip().lower() == wanted_scope
        if in_scope and str(item.get("persistence") or "removable") == "removable":
            removed.append(item)
        else:
            kept.append(item)
    if removed:
        holder["effects"] = kept
    return _expire(_effect_rules(plugin), holder, removed)


def effect_summary(holder: Mapping[str, Any]) -> list[dict[str, Any]]:
    """效果的精简视图，供战斗快照与工具返回使用。"""
    rows: list[dict[str, Any]] = []
    for effect in effects_of(holder):
        rows.append(
            {
                "id": effect.get("id"),
                "kind": effect.get("kind"),
                "scope": effect.get("scope"),
                "source": effect.get("source"),
                "stacks": _as_int(effect.get("stacks"), 1),
                "value": effect.get("value"),
                "duration": dict(effect.get("duration") or {}),
                "persistence": effect.get("persistence"),
                "detail": dict(effect.get("detail") or {}),
            }
        )
    return rows


__all__ = [
    "EFFECT_TICK_PHASES",
    "add_effect",
    "apply_resource_delta",
    "clear_effects",
    "effect_summary",
    "effects_of",
    "remove_effect",
    "tick_holder",
]
