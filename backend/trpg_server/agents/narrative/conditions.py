"""条件 DSL 的确定性求值（设计文档 §4.2.1）。

只支持结构化叶子，不做自然语言求值。求值失败只返回「缺什么」，
不返回正确答案，避免把剧透写进工具结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

_MISSING = object()


@dataclass
class ConditionResult:
    ok: bool
    missing: list[dict[str, Any]] = field(default_factory=list)

    def add(self, atom: Mapping[str, Any]) -> None:
        self.missing.append(dict(atom))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": list(self.missing)}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lookup(state: Mapping[str, Any], key: str) -> Any:
    if key in state:
        return state[key]
    narrative = state.get("narrative")
    if isinstance(narrative, Mapping) and key in narrative:
        return narrative[key]
    return _MISSING


def _known_clues(state: Mapping[str, Any], pc: str | None) -> set[str]:
    clues = set(str(item) for item in _as_list(_lookup(state, "revealed_clues")) if item)
    pc_clues = _lookup(state, "pc_known_clues")
    if isinstance(pc_clues, Mapping):
        if pc:
            clues |= {str(item) for item in _as_list(pc_clues.get(pc)) if item}
        else:
            for values in pc_clues.values():
                clues |= {str(item) for item in _as_list(values) if item}
    return clues


def _visited_locations(state: Mapping[str, Any]) -> set[str]:
    visited = _lookup(state, "visit_log")
    result = {str(item) for item in _as_list(visited) if item}
    status = _lookup(state, "node_status")
    if isinstance(status, Mapping):
        result |= {str(key) for key, value in status.items() if value in {"visited", "active", "completed"}}
    location = _lookup(state, "location")
    if isinstance(location, str) and location:
        result.add(location)
    return result


def _check_result(state: Mapping[str, Any], skill: str) -> str | None:
    checks = _lookup(state, "checks")
    if isinstance(checks, list):
        for item in reversed(checks):
            if isinstance(item, Mapping) and str(item.get("skill") or "") == skill:
                return str(item.get("result") or "") or None
    results = _lookup(state, "check_results")
    if isinstance(results, Mapping):
        value = results.get(skill)
        if isinstance(value, Mapping):
            return str(value.get("result") or "") or None
        if value:
            return str(value)
    return None


def evaluate_condition(condition: Any, state: Mapping[str, Any], *, pc: str | None = None) -> ConditionResult:
    """返回是否满足以及缺失的叶子列表。空条件视为满足。"""
    if condition in (None, "", {}, []):
        return ConditionResult(True)
    if not isinstance(condition, Mapping):
        return ConditionResult(False, [{"error": "invalid_condition"}])

    result = ConditionResult(True)

    if "all" in condition:
        for item in _as_list(condition.get("all")):
            child = evaluate_condition(item, state, pc=pc)
            if not child.ok:
                result.ok = False
                result.missing.extend(child.missing)
        # all 失败时仍继续收集其它缺失项，便于一次提示完整。

    if "any" in condition:
        children = _as_list(condition.get("any"))
        any_ok = False
        missing_groups: list[list[dict[str, Any]]] = []
        for item in children:
            child = evaluate_condition(item, state, pc=pc)
            if child.ok:
                any_ok = True
                break
            missing_groups.append(child.missing)
        if not any_ok:
            result.ok = False
            if missing_groups:
                shortest = min(missing_groups, key=len)
                result.missing.extend(shortest)

    if "not" in condition:
        child = evaluate_condition(condition.get("not"), state, pc=pc)
        if child.ok:
            result.ok = False
            result.add({"not": condition.get("not")})

    if "clue" in condition:
        clue = str(condition.get("clue") or "")
        if clue and clue not in _known_clues(state, pc):
            result.ok = False
            result.add({"clue": clue})

    if "flag" in condition:
        flag = str(condition.get("flag") or "")
        flags = _lookup(state, "flags")
        current = flags.get(flag) if isinstance(flags, Mapping) else None
        expected = condition.get("value", True)
        if bool(current) != bool(expected):
            result.ok = False
            result.add({"flag": flag, "value": bool(expected)})

    if "item" in condition:
        item = str(condition.get("item") or "")
        inventory = _lookup(state, "items")
        if not inventory or item not in {str(value) for value in _as_list(inventory)}:
            result.ok = False
            result.add({"item": item})

    if "visited" in condition:
        target = str(condition.get("visited") or "")
        if target and target not in _visited_locations(state):
            result.ok = False
            result.add({"visited": target})

    if "visited_object" in condition:
        target = str(condition.get("visited_object") or "")
        visited_objects = {str(value) for value in _as_list(_lookup(state, "visited_objects"))}
        if target and target not in visited_objects:
            result.ok = False
            result.add({"visited_object": target})

    if "check" in condition:
        spec = condition.get("check")
        if isinstance(spec, Mapping):
            skill = str(spec.get("skill") or "")
            expected = str(spec.get("result") or "success")
            actual = _check_result(state, skill)
            if actual != expected:
                result.ok = False
                result.add({"check": {"skill": skill, "result": expected}})

    if "clock" in condition:
        clock_id = str(condition.get("clock") or "")
        clocks = _lookup(state, "clocks")
        entry = clocks.get(clock_id) if isinstance(clocks, Mapping) else None
        value = None
        if isinstance(entry, Mapping):
            value = entry.get("value", entry.get("current"))
        elif entry is not None:
            value = entry
        ok = False
        if isinstance(value, (int, float)):
            if "gte" in condition:
                ok = float(value) >= float(condition.get("gte") or 0)
            elif "lte" in condition:
                ok = float(value) <= float(condition.get("lte") or 0)
            elif "gt" in condition:
                ok = float(value) > float(condition.get("gt") or 0)
            elif "lt" in condition:
                ok = float(value) < float(condition.get("lt") or 0)
            elif "eq" in condition:
                ok = float(value) == float(condition.get("eq") or 0)
        if not ok:
            result.ok = False
            result.add({key: condition[key] for key in ("clock", "gte", "lte", "gt", "lt", "eq") if key in condition})

    if "attitude" in condition:
        spec = condition.get("attitude")
        if isinstance(spec, Mapping):
            npc = str(spec.get("npc") or "")
            attitudes = _lookup(state, "npc_attitudes")
            value = None
            if isinstance(attitudes, Mapping):
                raw = attitudes.get(npc)
                value = raw.get("attitude") if isinstance(raw, Mapping) else raw
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if ok and "gte" in spec:
                ok = _as_float(value) >= _as_float(spec.get("gte"))
            elif ok and "lte" in spec:
                ok = _as_float(value) <= _as_float(spec.get("lte"))
            if not ok:
                result.ok = False
                result.add({"attitude": dict(spec)})

    # 触发器扩展原子（§4.5.1）：type=location / interaction_result / skill
    atom_type = str(condition.get("type") or "").strip().lower()
    if atom_type:
        if atom_type == "location":
            target = str(condition.get("id") or "")
            if target and target not in _visited_locations(state):
                result.ok = False
                result.add({"type": "location", "id": target})
        elif atom_type == "interaction_result":
            interaction_id = str(condition.get("interaction_id") or "")
            outcome = str(condition.get("outcome") or "success")
            results = _lookup(state, "interaction_results")
            actual = results.get(interaction_id) if isinstance(results, Mapping) else None
            if actual != outcome:
                result.ok = False
                result.add({"type": "interaction_result", "interaction_id": interaction_id, "outcome": outcome})
        elif atom_type == "skill":
            name = str(condition.get("name") or "")
            actual = _check_result(state, name)
            if actual is None:
                result.ok = False
                result.add({"type": "skill", "name": name})

    return result


def condition_atoms(condition: Any) -> Iterable[dict[str, Any]]:
    """展开条件树里的叶子，供校验器检查解锁来源。"""
    if not isinstance(condition, Mapping):
        return
    for key in ("all", "any"):
        for item in _as_list(condition.get(key)):
            yield from condition_atoms(item)
    if "not" in condition:
        yield from condition_atoms(condition.get("not"))
    leaf_keys = {"clue", "flag", "item", "visited", "visited_object", "check", "clock", "attitude", "type"}
    if leaf_keys & set(condition.keys()):
        yield dict(condition)
