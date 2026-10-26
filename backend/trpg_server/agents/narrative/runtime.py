"""图谱模式的运行时裁决：位置、出口、交互、线索与状态写入（§5.4-§5.6）。

工具层只做参数校验与展示，所有「能不能做」都集中在这里：可见性、硬条件、
一次性、结局条件与原子状态/账本写入。这样 degraded 档可以复用同一套编排，
也避免模型用自然语言绕开引擎。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from trpg_server.agents.narrative.conditions import ConditionResult, evaluate_condition
from trpg_server.agents.narrative.ledger import append_ledger_event
from trpg_server.agents.narrative.model import (
    VISIBILITY_KP_ONLY,
    VISIBILITY_PUBLIC,
    NarrativeBundle,
    NarrativeEdge,
    NarrativeNode,
)
from trpg_server.agents.narrative.publisher import publish_hits
from trpg_server.agents.narrative.resolver import TriggerResolver
from trpg_server.agents.narrative.state_store import apply_event, load_narrative_state
from trpg_server.agents.narrative.store import (
    MOVEMENT_EDGE_TYPES,
    NarrativeIndex,
    build_index,
    has_narrative,
    load_bundle,
    scenario_dir_for_descriptor,
)
from trpg_server.scenario_store import load_scenario_by_id

logger = logging.getLogger(__name__)


class NarrativeRuntimeError(RuntimeError):
    """运行态不可用或动作被引擎拒绝；消息可以直接回给模型。"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item or "").strip()))


def _is_movement(edge: NarrativeEdge) -> bool:
    return edge.type in MOVEMENT_EDGE_TYPES


def _edge_visible(edge: NarrativeEdge, revealed: set[str], narrative: Mapping[str, Any]) -> bool:
    state = _text(edge.visibility) or VISIBILITY_PUBLIC
    if state == VISIBILITY_KP_ONLY:
        return False
    if state == VISIBILITY_PUBLIC:
        return True
    if edge.id in revealed:
        return True
    if edge.reveal_when:
        return evaluate_condition(edge.reveal_when, narrative).ok
    return False


def _node_visible(node: NarrativeNode, revealed: set[str], narrative: Mapping[str, Any]) -> bool:
    if node.visibility == VISIBILITY_KP_ONLY:
        return False
    if node.visibility == VISIBILITY_PUBLIC:
        return True
    if node.id in revealed:
        return True
    if node.reveal_when:
        return evaluate_condition(node.reveal_when, narrative).ok
    return False


def _object_visible(obj: Mapping[str, Any], revealed: set[str], narrative: Mapping[str, Any]) -> bool:
    if not obj.get("visible", True):
        return False
    state = _text(obj.get("visibility")) or VISIBILITY_PUBLIC
    if state == VISIBILITY_KP_ONLY:
        return False
    if state == VISIBILITY_PUBLIC:
        return True
    object_id = _text(obj.get("id"))
    if object_id in revealed:
        return True
    reveal_when = obj.get("reveal_when")
    return bool(reveal_when) and evaluate_condition(reveal_when, narrative).ok


@dataclass
class NarrativeRuntime:
    bundle: NarrativeBundle
    narrative: dict[str, Any]
    index: NarrativeIndex
    room_id: str | None = None
    room_dir: Path | None = None
    scenario: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, context: Any) -> "NarrativeRuntime":
        room_dir = getattr(context, "room_dir", None)
        scenarios_dir = getattr(context, "scenarios_dir", None)
        if not room_dir:
            raise NarrativeRuntimeError("当前请求没有绑定房间，图谱运行态不可用")
        if not scenarios_dir or not Path(scenarios_dir).exists():
            raise NarrativeRuntimeError("剧本目录未配置，图谱运行态不可用")
        room_info = context.room_info() if callable(getattr(context, "room_info", None)) else {}
        room_info = room_info if isinstance(room_info, Mapping) else {}
        scenario_id = room_info.get("scenario_id")
        if scenario_id in (None, ""):
            raise NarrativeRuntimeError("当前房间没有绑定剧本")
        descriptor, scenario = load_scenario_by_id(
            Path(scenarios_dir), scenario_id, room_info.get("scenario_version")
        )
        if not descriptor:
            raise NarrativeRuntimeError(f"剧本 {scenario_id} 不存在")
        scenario_dir = scenario_dir_for_descriptor(descriptor)
        bundle = load_bundle(scenario_dir)
        if bundle is None:
            raise NarrativeRuntimeError("剧本尚未生成 narrative.json，图谱模式不可用")
        narrative = load_narrative_state(Path(room_dir))
        return cls(
            bundle=bundle,
            narrative=narrative,
            index=build_index(bundle),
            room_id=getattr(context, "room_id", None),
            room_dir=Path(room_dir),
            scenario=scenario if isinstance(scenario, Mapping) else {},
        )

    # ---- 基础查询 ---------------------------------------------------------
    @property
    def location_id(self) -> str | None:
        current = _text(self.narrative.get("location"))
        if current and self.bundle.node(current):
            return current
        return self.index.start or (self.bundle.location_nodes[0].id if self.bundle.location_nodes else None)

    def node(self, node_id: Any) -> NarrativeNode | None:
        return self.bundle.node(_text(node_id))

    def title(self, node_id: Any) -> str:
        node = self.node(node_id)
        return node.title if node and node.title else _text(node_id)

    def revealed_ids(self) -> set[str]:
        result: set[str] = set()
        revealed = self.narrative.get("revealed")
        if isinstance(revealed, list):
            for item in revealed:
                if isinstance(item, Mapping) and _text(item.get("id")):
                    result.add(_text(item.get("id")))
        return result

    def node_visible(self, node_id: Any) -> bool:
        node = self.node(node_id)
        return bool(node) and _node_visible(node, self.revealed_ids(), self.narrative)

    def location_node(self) -> NarrativeNode | None:
        return self.node(self.location_id)

    def is_visited(self, node_id: Any) -> bool:
        key = _text(node_id)
        status = self.narrative.get("node_status") or {}
        if isinstance(status, Mapping) and str(status.get(key) or "") in {"visited", "active", "completed"}:
            return True
        return key in {str(item) for item in self.narrative.get("visit_log") or []}

    def resolve_node(self, reference: Any, *, kinds: tuple[str, ...] | None = None) -> NarrativeNode | None:
        ref = _text(reference)
        if not ref:
            return None
        direct = self.node(ref)
        if direct and (kinds is None or direct.kind in kinds):
            return direct
        folded = ref.casefold()
        for node in self.bundle.nodes:
            if kinds is not None and node.kind not in kinds:
                continue
            names = [node.title, *node.aliases]
            if any(_text(name).casefold() == folded for name in names):
                return node
        return None

    def location_candidates(self, query: str) -> list[NarrativeNode]:
        needle = _text(query).casefold()
        if not needle:
            return []
        result: list[NarrativeNode] = []
        for node in self.bundle.location_nodes:
            names = [node.id, node.title, *node.aliases]
            if any(needle in _text(name).casefold() for name in names):
                result.append(node)
        return result

    # ---- 出口与地点卡 -----------------------------------------------------
    def _movement_edges_from(self, location: str) -> list[tuple[NarrativeEdge, str]]:
        result: list[tuple[NarrativeEdge, str]] = []
        for edge in self.bundle.edges:
            if not _is_movement(edge):
                continue
            if edge.from_id == location:
                result.append((edge, edge.to_id))
            elif edge.to_id == location and edge.bidirectional:
                result.append((edge, edge.from_id))
        return result

    @staticmethod
    def _edge_condition(edge: NarrativeEdge, narrative: Mapping[str, Any]) -> ConditionResult:
        if edge.hard and edge.condition:
            return evaluate_condition(edge.condition, narrative)
        return ConditionResult(True)

    def exits(self, *, include_blocked: bool = True, include_hidden: bool = False) -> list[dict[str, Any]]:
        location = self.location_id
        if not location:
            return []
        revealed = self.revealed_ids()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge, target_id in self._movement_edges_from(location):
            if target_id in seen:
                continue
            target = self.node(target_id)
            if target is None:
                continue
            visible = _edge_visible(edge, revealed, self.narrative) and _node_visible(
                target, revealed, self.narrative
            )
            if not visible and not include_hidden:
                continue
            condition = self._edge_condition(edge, self.narrative)
            if condition.ok is False and not include_blocked:
                continue
            result.append({
                "edge_id": edge.id,
                "target_id": target.id,
                "title": target.title or target.id,
                "label": edge.label or target.title or target.id,
                "type": edge.type,
                "visible": visible,
                "blocked": not condition.ok,
                "missing": condition.missing,
                "visited": self.is_visited(target.id),
                "one_way": not bool(edge.bidirectional),
            })
            seen.add(target_id)
        result.sort(key=lambda item: (item["blocked"], not item["visited"]))
        return result

    def location_card(self) -> dict[str, Any]:
        node = self.location_node()
        if node is None:
            return {"error": "当前地点不在图谱中", "location_id": self.location_id}
        revealed = self.revealed_ids()
        visible = _node_visible(node, revealed, self.narrative)
        objects = []
        for obj in node.objects:
            if not _object_visible(obj, revealed, self.narrative):
                continue
            interactions = [
                {
                    "id": item.get("id"),
                    "skill": item.get("skill"),
                    "difficulty": item.get("difficulty"),
                    "repeatable": bool(item.get("repeatable", True)),
                }
                for item in (obj.get("interactions") or [])
                if isinstance(item, Mapping)
            ]
            objects.append({
                "id": obj.get("id"),
                "name": obj.get("name"),
                "summary": obj.get("summary"),
                "interactions": interactions,
            })
        return {
            "location_id": node.id,
            "title": node.title or node.id,
            "kind": node.kind,
            "scene_role": node.scene_role,
            "visible": visible,
            "summary": node.summary,
            "body": node.body if visible else "",
            "objects": objects,
            "exits": self.exits(),
            "visited": self.is_visited(node.id),
            "beats_done": list(self.narrative.get("beats_done") or []),
            "kp_notes": node.kp_notes,
            "kp_notes_do_not_narrate": bool(node.kp_notes),
        }

    # ---- 写入 -------------------------------------------------------------
    def _event(self, payload: Mapping[str, Any], *, summary: str = "") -> dict[str, Any]:
        event = dict(payload)
        event.setdefault("id", f"evt_{uuid4().hex[:12]}")
        event.setdefault("turn", _as_int((self.narrative.get("clock") or {}).get("turn"), 0))
        event.setdefault("location_id", self.location_id)
        event.setdefault("summary", summary)
        event.setdefault("source", "kp")
        return event

    def record(self, payload: Mapping[str, Any], *, summary: str = "") -> dict[str, Any]:
        event = self._event(payload, summary=summary)
        if self.room_dir is None:
            raise NarrativeRuntimeError("房间目录不可用，无法写入状态")
        append_ledger_event(self.room_dir, event, bundle=self.bundle)
        self.narrative = apply_event(self.room_dir, event)
        return event

    def record_batch(self, payloads: list[Mapping[str, Any]], *, summary: str = "") -> list[dict[str, Any]]:
        """一次原子状态写入 + 一条账本摘要；用于交互结果的多效果。"""
        from trpg_server.agents.narrative.state_store import apply_to_narrative, save_narrative_state

        if self.room_dir is None:
            raise NarrativeRuntimeError("房间目录不可用，无法写入状态")
        events = [self._event(payload) for payload in payloads]
        narrative = self.narrative
        for event in events:
            narrative = apply_to_narrative(narrative, event)
        save_narrative_state(self.room_dir, narrative)
        self.narrative = narrative
        if summary:
            append_ledger_event(
                self.room_dir,
                {
                    "id": f"evt_{uuid4().hex[:12]}",
                    "turn": _as_int((self.narrative.get("clock") or {}).get("turn"), 0),
                    "location_id": self.location_id,
                    "kind": "state_change",
                    "summary": summary,
                    "source": "kp",
                },
                bundle=self.bundle,
            )
        return events

    # ---- 触发器 -----------------------------------------------------------
    def _auto_triggers(
        self,
        kind: str,
        context: Mapping[str, Any] | None = None,
        *,
        pc: str | None = None,
        source: str | None = None,
        audience: str = "player",
    ) -> dict[str, Any]:
        """状态变更后由引擎自动求值；返回值并入工具结果，前端统一持久化。"""
        context = dict(context or {})
        resolver = TriggerResolver(self.bundle, self.narrative)
        hits, denied = resolver.evaluate(kind, context, pc=pc, audience=audience)
        if not hits:
            return {"triggers_fired": [], "trigger_denied": denied}
        messages = publish_hits(hits, binding=context, source=source or kind, audience=audience)
        payloads = [
            {
                "kind": "trigger_fire",
                "trigger_id": hit.trigger_id,
                "pc": _text(pc) or None,
                "check_id": hit.check_id,
                "group": hit.group,
                "source": source or kind,
            }
            for hit in hits
        ]
        self.record_batch(payloads, summary="触发 " + "、".join(hit.trigger_id for hit in hits))
        return {
            "triggers_fired": [hit.trigger_id for hit in hits],
            "direct_messages": messages,
            "trigger_denied": denied,
        }

    def request_trigger(
        self,
        trigger_id: Any,
        *,
        condition_met: bool | None = None,
        check_id: str | None = None,
        pc: str | None = None,
        action_text: str = "",
        audience: str = "player",
    ) -> dict[str, Any]:
        resolver = TriggerResolver(self.bundle, self.narrative)
        hits, denied = resolver.request(
            trigger_id,
            pc=pc,
            condition_met=condition_met,
            check_id=check_id,
            action_text=action_text,
            audience=audience,
        )
        if not hits:
            return {
                "triggered": False,
                "trigger_id": _text(trigger_id),
                "denied": denied,
            }
        messages = publish_hits(hits, binding={"check_id": check_id}, source="manual", audience=audience)
        self.record_batch(
            [
                {
                    "kind": "trigger_fire",
                    "trigger_id": hit.trigger_id,
                    "pc": _text(pc) or None,
                    "check_id": check_id or hit.check_id,
                    "group": hit.group,
                    "source": "manual",
                }
                for hit in hits
            ],
            summary="手动触发 " + "、".join(hit.trigger_id for hit in hits),
        )
        return {
            "triggered": True,
            "trigger_id": hits[0].trigger_id,
            "direct_messages": messages,
            "denied": denied,
        }

    def list_available_triggers(self) -> dict[str, Any]:
        resolver = TriggerResolver(self.bundle, self.narrative)
        return {
            "location_id": self.location_id,
            "triggers": resolver.available(),
        }

    def move(self, target_ref: Any, *, actor: str | None = None) -> dict[str, Any]:
        target = self.resolve_node(target_ref, kinds=("location", "ending"))
        if target is None:
            raise NarrativeRuntimeError(f"找不到地点：{target_ref}")
        if target.visibility == VISIBILITY_KP_ONLY or not self.node_visible(target.id):
            raise NarrativeRuntimeError("目标地点尚未向玩家揭示，不能移动")
        if target.id == self.location_id:
            return {"moved": False, "already_here": True, "location_id": target.id, "title": self.title(target.id)}

        edges = [
            (edge, condition)
            for edge, condition in ((edge, self._edge_condition(edge, self.narrative)) for edge, target_id in self._movement_edges_from(self.location_id or "") if target_id == target.id)
        ]
        if not edges:
            raise NarrativeRuntimeError(f"从当前位置无法直接到达 {target.title or target.id}")
        usable = [edge for edge, condition in edges if condition.ok]
        if not usable:
            missing = edges[0][1].missing
            raise NarrativeRuntimeError(
                f"前往 {target.title or target.id} 的条件未满足，缺少："
                + "；".join(str(item) for item in missing)
            )
        if target.kind == "ending" and target.completion:
            gate = evaluate_condition(target.completion, self.narrative)
            if not gate.ok:
                raise NarrativeRuntimeError(
                    "结局条件未满足，不能进入结局；缺少："
                    + "；".join(str(item) for item in gate.missing)
                )

        visited_before = self.is_visited(target.id)
        event = self.record(
            {"kind": "move", "to": target.id, "from": self.location_id, "actor": _text(actor) or None},
            summary=f"移动到 {target.title or target.id}",
        )
        trigger_result = self._auto_triggers(
            "enter",
            {"location_id": target.id, "first_time": not visited_before},
            pc=actor,
            source="enter",
        )
        return {
            "moved": True,
            "location_id": target.id,
            "title": target.title or target.id,
            "visited_before": visited_before,
            "event_id": event["id"],
            **trigger_result,
        }

    def _find_object(self, reference: Any) -> tuple[NarrativeNode, dict[str, Any], str, bool]:
        """返回 (地点, 物件, 可见性, 是否本次首次揭示)；隐藏物必须 reveal_when 成立。"""
        ref = _text(reference)
        location = self.location_node()
        if location is None:
            raise NarrativeRuntimeError("当前地点不在图谱中")
        for obj in location.objects:
            if not isinstance(obj, Mapping):
                continue
            if ref in {_text(obj.get("id")), _text(obj.get("name"))}:
                if not obj.get("visible", True):
                    raise NarrativeRuntimeError(f"物件 {ref} 当前不可见")
                state = _text(obj.get("visibility")) or VISIBILITY_PUBLIC
                if state == VISIBILITY_KP_ONLY:
                    raise NarrativeRuntimeError(f"物件 {ref} 为 KP-only，不可检查")
                object_id = _text(obj.get("id"))
                newly_revealed = state != VISIBILITY_PUBLIC and object_id not in self.revealed_ids()
                if newly_revealed:
                    reveal_when = obj.get("reveal_when")
                    if not reveal_when or not evaluate_condition(reveal_when, self.narrative).ok:
                        raise NarrativeRuntimeError("物件隐藏层尚未解锁")
                return location, dict(obj), state, newly_revealed
        raise NarrativeRuntimeError(f"当前地点没有物件：{ref}")

    def inspect_object(self, reference: Any) -> dict[str, Any]:
        location, obj, state, newly_revealed = self._find_object(reference)
        object_id = _text(obj.get("id"))
        if newly_revealed:
            self.record(
                {"kind": "reveal", "object_id": object_id, "object_kind": "object", "reason": "inspect"},
                summary=f"揭示物件 {obj.get('name') or object_id}",
            )
        interactions = [
            {"id": item.get("id"), "skill": item.get("skill"), "difficulty": item.get("difficulty")}
            for item in (obj.get("interactions") or [])
            if isinstance(item, Mapping)
        ]
        return {
            "object_id": object_id,
            "name": obj.get("name"),
            "location_id": location.id,
            "summary": obj.get("summary"),
            "body": str(obj.get("summary") or ""),
            "revealed_now": newly_revealed,
            "interactions": interactions,
        }

    def resolve_interaction(
        self,
        object_ref: Any,
        interaction_ref: Any,
        result: str,
        *,
        skill: str | None = None,
        total: int | None = None,
        roll: int | None = None,
        pc: str | None = None,
        check_id: str | None = None,
    ) -> dict[str, Any]:
        _, obj, _, newly_revealed = self._find_object(object_ref)
        interactions = [item for item in (obj.get("interactions") or []) if isinstance(item, Mapping)]
        interaction = None
        for item in interactions:
            if _text(item.get("id")) == _text(interaction_ref):
                interaction = item
                break
        if interaction is None and len(interactions) == 1:
            interaction = interactions[0]
        if interaction is None:
            raise NarrativeRuntimeError(f"物件 {object_ref} 没有交互：{interaction_ref}")

        interaction_id = _text(interaction.get("id"))
        normalized_result = _normalize_result(result)
        check_record = self._check_record(check_id) if check_id else None
        if check_id and check_record is None:
            raise NarrativeRuntimeError(f"检定记录 {check_id} 不存在或不属于本房间")
        if check_record is not None:
            recorded_skill = _text(check_record.get("skill"))
            expected_skill = _text(interaction.get("skill") or skill)
            if recorded_skill and expected_skill and recorded_skill.casefold() != expected_skill.casefold():
                raise NarrativeRuntimeError("检定技能与交互要求不一致")
            recorded_result = _normalize_result(check_record.get("result"))
            if _result_family(recorded_result) != _result_family(normalized_result):
                raise NarrativeRuntimeError("检定结果与结算结果不一致")
        repeatable = bool(interaction.get("repeatable", True))
        results = self.narrative.get("interaction_results") or {}
        if not repeatable and isinstance(results, Mapping) and interaction_id in results:
            return {
                "resolved": False,
                "already_resolved": True,
                "interaction_id": interaction_id,
                "previous_result": results[interaction_id],
                "summary": "这个交互已经发生过，不再重复给予线索或推进时钟。",
            }

        outcomes = interaction.get("outcomes") if isinstance(interaction.get("outcomes"), Mapping) else {}
        outcome = outcomes.get(normalized_result) or outcomes.get("any")
        if not isinstance(outcome, Mapping):
            raise NarrativeRuntimeError(f"交互 {interaction_id} 没有可用的结果文本")
        outcome_text = _text(outcome.get("text"))
        effects = outcome.get("effects") if isinstance(outcome.get("effects"), Mapping) else outcome

        payloads: list[dict[str, Any]] = [{
            "kind": "interaction",
            "interaction_id": interaction_id,
            "result": normalized_result,
            "check": {
                "check_id": _text(check_id) or None,
                "skill": _text(skill or interaction.get("skill")),
                "difficulty": _text(interaction.get("difficulty")) or None,
                "result": normalized_result,
                "total": total,
                "roll": roll,
                "pc": _text(pc) or None,
            },
        }]
        if newly_revealed:
            payloads.append({
                "kind": "reveal", "object_id": _text(obj.get("id")),
                "object_kind": "object", "reason": "interaction",
            })
        applied = _effect_payloads(effects)
        payloads.extend(applied)
        self.record_batch(
            payloads,
            summary=f"交互 {obj.get('name') or object_ref} / {normalized_result}",
        )
        trigger_result = self._auto_triggers(
            "interaction",
            {
                "location_id": self.location_id,
                "object_id": _text(obj.get("id")),
                "interaction_id": interaction_id,
                "result": normalized_result,
                "skill": _text(skill or interaction.get("skill")),
                "difficulty": _text(interaction.get("difficulty")) or None,
                "check_id": _text(check_id) or None,
                "success": _result_family(normalized_result) == "success",
            },
            pc=pc,
            source="interaction",
        )
        clock_result = self._auto_triggers("clock", {}, pc=pc, source="clock")
        return {
            "resolved": True,
            "interaction_id": interaction_id,
            "result": normalized_result,
            "outcome_text": outcome_text,
            "effects_applied": applied,
            "repeatable": repeatable,
            **_merge_trigger_results(trigger_result, clock_result),
        }

    def reveal_clue(self, reference: Any, *, pc: str | None = None) -> dict[str, Any]:
        clue = self.resolve_node(reference, kinds=("clue",))
        if clue is None:
            raise NarrativeRuntimeError(f"找不到线索：{reference}")
        if clue.visibility == VISIBILITY_KP_ONLY:
            raise NarrativeRuntimeError("该线索为 KP-only，永远不会向玩家揭示")
        if clue.id in (self.narrative.get("revealed_clues") or []):
            return {"revealed": False, "already_revealed": True, "clue_id": clue.id, "title": clue.title}
        if clue.reveal_when:
            gate = evaluate_condition(clue.reveal_when, self.narrative)
            if not gate.ok:
                raise NarrativeRuntimeError(
                    "线索揭示条件未满足，缺少：" + "；".join(str(item) for item in gate.missing)
                )
        self.record(
            {"kind": "clue", "clue_id": clue.id, "pc": _text(pc) or None},
            summary=f"揭示线索 {clue.title or clue.id}",
        )
        trigger_result = self._auto_triggers(
            "clue", {"clue_id": clue.id}, pc=pc, source="clue"
        )
        return {
            "revealed": True,
            "clue_id": clue.id,
            "title": clue.title or clue.id,
            "summary": clue.summary,
            "body": clue.body,
            **trigger_result,
        }

    def update_npc_state(self, reference: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
        npc = self.resolve_node(reference, kinds=("npc",))
        if npc is None:
            raise NarrativeRuntimeError(f"找不到 NPC：{reference}")
        payload: dict[str, Any] = {"kind": "attitude", "npc_id": npc.id}
        if "value" in arguments:
            payload["value"] = _as_int(arguments.get("value"), 0)
        elif "delta" in arguments:
            payload["delta"] = _as_int(arguments.get("delta"), 0)
        if arguments.get("location") not in (None, ""):
            target = self.resolve_node(arguments.get("location"), kinds=("location",))
            if target is None:
                raise NarrativeRuntimeError(f"找不到 NPC 目标位置：{arguments.get('location')}")
            payload["location"] = target.id
        if "visible" in arguments:
            payload["visible"] = bool(arguments.get("visible"))
        if "known_clues" in arguments:
            payload["known_clues"] = _str_list(arguments.get("known_clues"))
        self.record(payload, summary=f"更新 NPC {npc.title or npc.id}")
        entry = (self.narrative.get("npc_state") or {}).get(npc.id) or {}
        change = "attitude" if ("value" in arguments or "delta" in arguments) else "state"
        trigger_result = self._auto_triggers(
            "npc",
            {"npc_id": npc.id, "event": change},
            pc=_text(arguments.get("pc")) or None,
            source="npc",
        )
        return {
            "updated": True,
            "npc_id": npc.id,
            "title": npc.title or npc.id,
            "state": dict(entry),
            **trigger_result,
        }

    def _check_record(self, check_id: Any) -> Mapping[str, Any] | None:
        expected = _text(check_id)
        checks = self.narrative.get("checks")
        for item in checks if isinstance(checks, list) else []:
            if isinstance(item, Mapping) and _text(item.get("check_id") or item.get("id")) == expected:
                return item
        return None

    def lookup_location(self, query: str) -> dict[str, Any]:
        matches = self.location_candidates(query)
        items = []
        for node in matches[:5]:
            items.append({
                "location_id": node.id,
                "title": node.title or node.id,
                "summary": node.summary[:120],
                "visited": self.is_visited(node.id),
                "reachable": node.id in self.index.reachable,
                "visible": self.node_visible(node.id),
            })
        return {"query": _text(query), "matches": items}


def _normalize_result(value: Any) -> str:
    text = _text(value).lower()
    mapping = {
        "大成功": "extreme_success",
        "极难成功": "extreme_success",
        "困难成功": "hard_success",
        "成功": "success",
        "失败": "fail",
        "大失败": "fumble",
    }
    return mapping.get(text, text or "success")


def _result_family(value: Any) -> str:
    normalized = _normalize_result(value)
    if normalized in {"extreme_success", "hard_success", "success"}:
        return "success"
    if normalized in {"fail", "fumble"}:
        return "fail"
    return normalized


def _merge_trigger_results(*results: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "triggers_fired": [],
        "trigger_denied": [],
        "direct_messages": [],
    }
    for result in results:
        merged["triggers_fired"].extend(result.get("triggers_fired") or [])
        merged["trigger_denied"].extend(result.get("trigger_denied") or [])
        merged["direct_messages"].extend(result.get("direct_messages") or [])
    return merged


def _effect_payloads(effects: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把结果里的结构化效果展开为状态事件；未知字段一律忽略。"""
    payloads: list[dict[str, Any]] = []
    for clue_id in _str_list(effects.get("clue") or effects.get("clues")):
        payloads.append({"kind": "clue", "clue_id": clue_id})
    flags = effects.get("flags")
    if isinstance(flags, Mapping):
        for name, value in flags.items():
            payloads.append({"kind": "flag", "name": str(name), "value": value})
    clocks = effects.get("clocks")
    if isinstance(clocks, Mapping):
        for clock_id, value in clocks.items():
            if isinstance(value, Mapping):
                payloads.append({"kind": "clock", "clock_id": str(clock_id), **dict(value)})
            else:
                payloads.append({"kind": "clock", "clock_id": str(clock_id), "value": value})
    items = effects.get("items")
    if isinstance(items, Mapping):
        for item_id, action in items.items():
            payloads.append({"kind": "item", "item_id": str(item_id), "action": str(action)})
    for npc_id, value in (effects.get("attitudes") or {}).items() if isinstance(effects.get("attitudes"), Mapping) else []:
        payloads.append({"kind": "attitude", "npc_id": str(npc_id), "value": value})
    for object_id in _str_list(effects.get("reveals")):
        payloads.append({"kind": "reveal", "object_id": object_id, "reason": "interaction"})
    return payloads


def narrative_available(context: Any) -> bool:
    """轻量探测：不加载完整 bundle，用于决定是否启用图谱工具集。"""
    room_dir = getattr(context, "room_dir", None)
    scenarios_dir = getattr(context, "scenarios_dir", None)
    if not room_dir or not scenarios_dir or not Path(scenarios_dir).exists():
        return False
    room_info = context.room_info() if callable(getattr(context, "room_info", None)) else {}
    scenario_id = room_info.get("scenario_id") if isinstance(room_info, Mapping) else None
    if scenario_id in (None, ""):
        return False
    descriptor, _ = load_scenario_by_id(Path(scenarios_dir), scenario_id, room_info.get("scenario_version"))
    return bool(descriptor) and has_narrative(scenario_dir_for_descriptor(descriptor))


__all__ = [
    "NarrativeRuntime",
    "NarrativeRuntimeError",
    "narrative_available",
]
