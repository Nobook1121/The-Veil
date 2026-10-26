"""触发器求解：绑定匹配、条件收窄、一次性/冷却与附件可见性（§5.8.2-5.8.3）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trpg_server.agents.narrative.conditions import evaluate_condition
from trpg_server.agents.narrative.model import NarrativeBundle
from trpg_server.agents.narrative.triggers import normalize_trigger_spec


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _result_family(result: str) -> str:
    text = _text(result).lower()
    if text in {"extreme_success", "hard_success", "success", "critical", "大成功", "极难成功", "困难成功", "成功"}:
        return "success"
    if text in {"fail", "failure", "fumble", "失败", "大失败"}:
        return "fail"
    return text


def _clock_value(narrative: Mapping[str, Any], clock_id: str) -> Any:
    clocks = narrative.get("clocks")
    if not isinstance(clocks, Mapping):
        return None
    entry = clocks.get(clock_id)
    if isinstance(entry, Mapping):
        return entry.get("value", entry.get("current"))
    return entry


def _threshold_ok(value: Any, binding: Mapping[str, Any]) -> bool:
    if not isinstance(value, (int, float)):
        return False
    if "gte" in binding:
        return float(value) >= float(binding.get("gte") or 0)
    if "lte" in binding:
        return float(value) <= float(binding.get("lte") or 0)
    if "gt" in binding:
        return float(value) > float(binding.get("gt") or 0)
    if "lt" in binding:
        return float(value) < float(binding.get("lt") or 0)
    if "eq" in binding:
        return float(value) == float(binding.get("eq") or 0)
    return True


def _fire_entry(narrative: Mapping[str, Any], trigger_id: str) -> Mapping[str, Any]:
    fires = narrative.get("trigger_fires")
    if not isinstance(fires, Mapping):
        return {}
    entry = fires.get(trigger_id)
    return entry if isinstance(entry, Mapping) else {}


@dataclass
class TriggerHit:
    spec: dict[str, Any]
    attachments: list[dict[str, Any]]
    check_id: str | None = None

    @property
    def trigger_id(self) -> str:
        return _text(self.spec.get("id"))

    @property
    def group(self) -> str:
        return _text(self.spec.get("group"))

    @property
    def priority(self) -> int:
        return _int(self.spec.get("priority"), 0)


class TriggerResolver:
    def __init__(self, bundle: NarrativeBundle | None, narrative: Mapping[str, Any]):
        self.narrative = narrative if isinstance(narrative, Mapping) else {}
        self.assets = {
            _text(item.get("id")): dict(item)
            for item in (bundle.assets if bundle else [])
            if isinstance(item, Mapping) and _text(item.get("id"))
        }
        self.triggers: list[dict[str, Any]] = []
        for item in (bundle.triggers if bundle else []):
            if not isinstance(item, Mapping):
                continue
            spec = normalize_trigger_spec(item, assets_by_id=self.assets, index=len(self.triggers))
            if spec:
                self.triggers.append(spec)

    @property
    def turn(self) -> int:
        clock = self.narrative.get("clock")
        return _int(clock.get("turn") if isinstance(clock, Mapping) else 0, 0)

    def trigger(self, trigger_id: Any) -> dict[str, Any] | None:
        expected = _text(trigger_id)
        return next((item for item in self.triggers if item["id"] == expected), None)

    def _current_spoiler(self) -> int:
        direct = self.narrative.get("spoiler_level")
        if direct is None:
            progression = self.narrative.get("progression")
            if isinstance(progression, Mapping):
                direct = progression.get("spoiler_level")
        return _int(direct, 5)

    def _binding_matches(self, spec: Mapping[str, Any], kind: str, context: Mapping[str, Any]) -> bool:
        binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else {}
        if _text(binding.get("kind")) != kind:
            return False
        if kind == "interaction":
            for key, actual in (
                ("location_id", context.get("location_id")),
                ("object_id", context.get("object_id")),
                ("interaction_id", context.get("interaction_id")),
                ("skill", context.get("skill")),
            ):
                expected = _text(binding.get(key))
                if expected and expected != _text(actual):
                    return False
            expected_result = _text(binding.get("result") or "any").lower()
            actual_result = _text(context.get("result"))
            if expected_result and expected_result != "any":
                if _result_family(expected_result) != _result_family(actual_result):
                    return False
            return True
        if kind == "enter":
            expected = _text(binding.get("location_id"))
            if expected and expected != _text(context.get("location_id")):
                return False
            if "first_time" in binding and bool(binding.get("first_time")) != bool(context.get("first_time")):
                return False
            return True
        if kind == "clock":
            clock_id = _text(binding.get("clock_id"))
            if clock_id:
                actual_id = _text(context.get("clock_id"))
                if actual_id and actual_id != clock_id:
                    return False
                return _threshold_ok(_clock_value(self.narrative, clock_id), binding)
            if "gte" not in binding and "lte" not in binding and "gt" not in binding and "lt" not in binding and "eq" not in binding:
                return True
            now = _int((self.narrative.get("clock") or {}).get("turn") if isinstance(self.narrative.get("clock"), Mapping) else 0, 0)
            return _threshold_ok(now, binding)
        if kind == "event":
            expected = _text(binding.get("event_id"))
            if expected and expected != _text(context.get("event_id")):
                return False
            return True
        if kind == "clue":
            expected = _text(binding.get("clue_id"))
            return not expected or expected == _text(context.get("clue_id"))
        if kind == "npc":
            expected = _text(binding.get("npc_id"))
            if expected and expected != _text(context.get("npc_id")):
                return False
            expected_event = _text(binding.get("event"))
            actual_event = _text(context.get("event"))
            return not expected_event or not actual_event or expected_event == actual_event
        if kind == "manual":
            expected = _text(binding.get("keyword"))
            action_text = _text(context.get("action_text")).casefold()
            return not expected or expected.casefold() in action_text
        return False

    def _once_denied(self, spec: Mapping[str, Any], *, pc: str | None) -> bool:
        if not spec.get("once"):
            return False
        entry = _fire_entry(self.narrative, spec["id"])
        if not entry:
            return False
        if spec.get("once_scope") == "pc" and pc:
            used = {_text(item) for item in _as_list(entry.get("pcs"))}
            return pc in used
        return _int(entry.get("count"), 0) > 0

    def _cooldown_denied(self, spec: Mapping[str, Any]) -> bool:
        cooldown = _int(spec.get("cooldown"), 0)
        if cooldown <= 0:
            return False
        entry = _fire_entry(self.narrative, spec["id"])
        if not entry:
            return False
        return self.turn - _int(entry.get("last_turn"), 0) < cooldown

    def _visibility_denied(self, spec: Mapping[str, Any], *, audience: str) -> tuple[bool, str]:
        if audience != "kp" and _text(spec.get("visibility")) == "kp_only":
            return True, "visibility_denied"
        if _int(spec.get("spoiler_level"), 0) > self._current_spoiler():
            return True, "spoiler_level_denied"
        return False, ""

    def _attachment_payloads(
        self, spec: Mapping[str, Any], *, audience: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        payloads: list[dict[str, Any]] = []
        missing: list[str] = []
        current_spoiler = self._current_spoiler()
        for position, attachment in enumerate(spec.get("attachments") or []):
            if not isinstance(attachment, Mapping):
                continue
            if audience != "kp" and _text(attachment.get("visibility")) == "kp_only":
                continue
            if _int(attachment.get("spoiler_level"), 0) > current_spoiler:
                continue
            asset = self.assets.get(_text(attachment.get("asset_id"))) or {}
            url = _text(attachment.get("url") or asset.get("url"))
            body = attachment.get("body")
            if not url and body is None:
                if attachment.get("asset_id"):
                    missing.append(_text(attachment.get("asset_id")))
                continue
            payload: dict[str, Any] = {
                "id": _text(attachment.get("id")) or f"att_{position}",
                "kind": _text(attachment.get("kind")) or _text(asset.get("kind")) or "file",
                "order": _int(attachment.get("order"), position),
            }
            if url:
                payload["url"] = url
            for key in ("thumb_url", "mime", "alt", "caption", "poster_url", "format"):
                value = _text(attachment.get(key) or asset.get(key))
                if value:
                    payload[key] = value
            for key in ("width", "height", "duration_ms"):
                value = _int(attachment.get(key) or asset.get(key), 0)
                if value:
                    payload[key] = value
            if body is not None:
                payload["body"] = str(body)
            if not payload.get("alt"):
                payload["alt"] = _text(spec.get("label"))
            payloads.append(payload)
        payloads.sort(key=lambda item: item.get("order", 0))
        return payloads, missing

    def _make_hit(
        self, spec: Mapping[str, Any], *, audience: str, check_id: str | None = None
    ) -> TriggerHit | None:
        attachments, missing = self._attachment_payloads(spec, audience=audience)
        if spec.get("attachments") and not attachments and not _text(spec.get("content")):
            return None
        hit = TriggerHit(spec=dict(spec), attachments=attachments, check_id=check_id or None)
        if missing:
            hit.spec = {**hit.spec, "missing_assets": missing}
        return hit

    def force_hit(
        self, trigger_id: Any, *, audience: str = "player", check_id: str | None = None
    ) -> TriggerHit | None:
        """房主/管理员手动触发：跳过条件、冷却与见证，只保留附件可见性过滤。"""
        spec = self.trigger(trigger_id)
        if spec is None:
            return None
        return self._make_hit(spec, audience=audience, check_id=check_id)

    def evaluate(
        self,
        kind: str,
        context: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
        pc: str | None = None,
        audience: str = "player",
    ) -> tuple[list[TriggerHit], list[dict[str, Any]]]:
        context = dict(context or {})
        hits: list[TriggerHit] = []
        denied: list[dict[str, Any]] = []
        seen: set[str] = set()
        for spec in self.triggers:
            trigger_id = spec["id"]
            if trigger_id in seen:
                continue
            if not self._binding_matches(spec, kind, context):
                continue
            seen.add(trigger_id)
            if not spec.get("enabled", True):
                denied.append({"trigger_id": trigger_id, "reason": "disabled"})
                continue
            condition = spec.get("condition")
            if condition is not None:
                result = evaluate_condition(condition, self.narrative, pc=pc)
                if not result.ok:
                    denied.append({"trigger_id": trigger_id, "reason": "condition_not_met", "missing": result.missing})
                    continue
            if self._once_denied(spec, pc=pc):
                denied.append({"trigger_id": trigger_id, "reason": "once"})
                continue
            if self._cooldown_denied(spec):
                denied.append({"trigger_id": trigger_id, "reason": "cooldown"})
                continue
            blocked, reason = self._visibility_denied(spec, audience=audience)
            if blocked:
                denied.append({"trigger_id": trigger_id, "reason": reason})
                continue
            hit = self._make_hit(spec, audience=audience, check_id=context.get("check_id"))
            if hit is None:
                denied.append({"trigger_id": trigger_id, "reason": "attachment_unavailable"})
                continue
            hits.append(hit)
        return hits, denied

    def request(
        self,
        trigger_id: Any,
        *,
        pc: str | None = None,
        condition_met: bool | None = None,
        check_id: str | None = None,
        action_text: str = "",
        audience: str = "player",
    ) -> tuple[list[TriggerHit], list[dict[str, Any]]]:
        spec = self.trigger(trigger_id)
        if spec is None:
            return [], [{"trigger_id": _text(trigger_id), "reason": "trigger_not_found"}]
        denied: list[dict[str, Any]] = []
        if not spec.get("enabled", True):
            return [], [{"trigger_id": spec["id"], "reason": "disabled"}]
        if condition_met is False:
            return [], [{"trigger_id": spec["id"], "reason": "condition_not_met"}]
        binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else {}
        kind = _text(binding.get("kind"))
        condition = spec.get("condition")
        if kind == "manual" and condition_met is not True:
            if spec.get("needs_review") or (isinstance(condition, Mapping) and condition.get("note")):
                return [], [{"trigger_id": spec["id"], "reason": "condition_not_met"}]
        evidence_ok, evidence_reason = self._request_evidence(
            spec, kind=kind, check_id=check_id, action_text=action_text
        )
        if not evidence_ok:
            return [], [{"trigger_id": spec["id"], "reason": evidence_reason}]
        condition = spec.get("condition")
        if condition is not None:
            result = evaluate_condition(condition, self.narrative, pc=pc)
            if not result.ok:
                denied.append({"trigger_id": spec["id"], "reason": "condition_not_met", "missing": result.missing})
                return [], denied
        if self._once_denied(spec, pc=pc):
            return [], [{"trigger_id": spec["id"], "reason": "once"}]
        if self._cooldown_denied(spec):
            return [], [{"trigger_id": spec["id"], "reason": "cooldown"}]
        blocked, reason = self._visibility_denied(spec, audience=audience)
        if blocked:
            return [], [{"trigger_id": spec["id"], "reason": reason}]
        hit = self._make_hit(spec, audience=audience, check_id=check_id)
        if hit is None:
            return [], [{"trigger_id": spec["id"], "reason": "attachment_unavailable"}]
        return [hit], denied

    def _request_evidence(
        self,
        spec: Mapping[str, Any],
        *,
        kind: str,
        check_id: str | None,
        action_text: str,
    ) -> tuple[bool, str]:
        binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else {}
        if kind == "manual":
            expected = _text(binding.get("keyword"))
            if expected and expected.casefold() not in _text(action_text).casefold():
                return False, "binding_mismatch"
            return True, ""
        if kind == "interaction":
            interaction_id = _text(binding.get("interaction_id"))
            results = self.narrative.get("interaction_results")
            actual = results.get(interaction_id) if isinstance(results, Mapping) and interaction_id else None
            if interaction_id and not actual:
                return False, "binding_mismatch"
            expected_result = _text(binding.get("result") or "any").lower()
            if actual and expected_result != "any" and _result_family(expected_result) != _result_family(_text(actual)):
                return False, "binding_mismatch"
            location = _text(binding.get("location_id"))
            if location and location != _text(self.narrative.get("location")):
                return False, "binding_mismatch"
            if check_id:
                return self._check_evidence(check_id, expected_result, binding)
            return True, ""
        if kind == "enter":
            location = _text(binding.get("location_id"))
            visited = {str(item) for item in _as_list(self.narrative.get("visit_log"))}
            status = self.narrative.get("node_status")
            if isinstance(status, Mapping):
                visited |= {str(key) for key, value in status.items() if value in {"visited", "active", "completed"}}
            if location and location not in visited:
                return False, "binding_mismatch"
            return True, ""
        if kind == "clock":
            clock_id = _text(binding.get("clock_id"))
            if clock_id and not _threshold_ok(_clock_value(self.narrative, clock_id), binding):
                return False, "binding_mismatch"
            return True, ""
        if kind == "event":
            event_id = _text(binding.get("event_id"))
            status = self.narrative.get("node_status")
            active = isinstance(status, Mapping) and _text(status.get(event_id)) in {"visited", "active", "completed"}
            beats = {str(item) for item in _as_list(self.narrative.get("beats_done"))}
            if event_id and not active and event_id not in beats:
                return False, "binding_mismatch"
            return True, ""
        if kind == "clue":
            clue_id = _text(binding.get("clue_id"))
            revealed = {str(item) for item in _as_list(self.narrative.get("revealed_clues"))}
            if clue_id and clue_id not in revealed:
                return False, "binding_mismatch"
            return True, ""
        if kind == "npc":
            npc_id = _text(binding.get("npc_id"))
            npc_state = self.narrative.get("npc_state")
            if npc_id and not (isinstance(npc_state, Mapping) and npc_id in npc_state):
                return False, "binding_mismatch"
            return True, ""
        return False, "binding_mismatch"

    def _check_evidence(
        self, check_id: str, expected_result: str, binding: Mapping[str, Any]
    ) -> tuple[bool, str]:
        checks = self.narrative.get("checks")
        record = None
        for item in checks if isinstance(checks, list) else []:
            if isinstance(item, Mapping) and _text(item.get("check_id") or item.get("id")) == _text(check_id):
                record = item
        if record is None:
            return False, "check_not_found"
        skill = _text(binding.get("skill"))
        if skill and _text(record.get("skill")) != skill:
            return False, "check_mismatch"
        if expected_result != "any":
            actual = _text(record.get("result"))
            if _result_family(expected_result) != _result_family(actual):
                return False, "check_mismatch"
        return True, ""

    def available(self) -> list[dict[str, Any]]:
        """当前地点可触发触发器：只返回 id 与 label，不返回正文/附件（§5.8.3）。"""
        location = _text(self.narrative.get("location"))
        result: list[dict[str, Any]] = []
        for spec in self.triggers:
            if not spec.get("enabled", True):
                continue
            binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else {}
            kind = _text(binding.get("kind"))
            if kind == "manual":
                result.append({"trigger_id": spec["id"], "label": spec["label"], "binding": kind})
                continue
            if kind in {"enter", "interaction"}:
                expected = _text(binding.get("location_id"))
                if expected and location and expected != location:
                    continue
            if kind == "clue":
                clue_id = _text(binding.get("clue_id"))
                revealed = {str(item) for item in _as_list(self.narrative.get("revealed_clues"))}
                if clue_id and clue_id not in revealed:
                    continue
            result.append({"trigger_id": spec["id"], "label": spec["label"], "binding": kind})
        return result


__all__ = ["TriggerHit", "TriggerResolver"]
