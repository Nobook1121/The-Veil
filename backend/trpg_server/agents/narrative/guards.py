"""输出侧护栏：KP-only / 未揭示专名不得进入玩家叙事（§5.5）。

门槛在模型输出之后、落库之前执行。当前实现是确定性删句：含禁词的句子
整句移除，全删空时给中性占位；不做模型二次改写，避免额外请求与不确定性。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from trpg_server.agents.narrative.conditions import evaluate_condition
from trpg_server.agents.narrative.model import (
    VISIBILITY_HIDDEN,
    VISIBILITY_KP_ONLY,
    VISIBILITY_PUBLIC,
    NarrativeBundle,
)

logger = logging.getLogger(__name__)

GENERIC_NAMES = {"", "kp", "KP", "守秘人", "玩家", "调查员"}
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])|(?<=\n)")


@dataclass(frozen=True)
class GuardPolicy:
    mode: str = "enforce"
    move_gate: bool = True
    spoiler_gate: bool = True
    result_gate: bool = True
    once_gate: bool = True
    progression_gate: bool = True
    declaration_check: bool = True
    # shadow：照常扫描并记违规，但不改正文（§12.5 灰度观测）。
    shadow: bool = False

    @classmethod
    def for_mode(cls, mode: str) -> "GuardPolicy":
        normalized = str(mode or "").strip().lower()
        if normalized == "off":
            return cls(
                mode="off",
                move_gate=False,
                spoiler_gate=False,
                result_gate=False,
                once_gate=False,
                progression_gate=False,
                declaration_check=False,
            )
        if normalized == "shadow":
            return cls(mode="shadow", shadow=True)
        if normalized in {"degraded", "legacy"}:
            return cls(
                mode="degraded",
                move_gate=False,
                spoiler_gate=True,
                result_gate=False,
                once_gate=False,
                progression_gate=False,
                declaration_check=True,
            )
        return cls(mode="enforce")


@dataclass
class GuardReport:
    text: str
    violations: list[dict[str, Any]] = field(default_factory=list)
    removed_sentences: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def changed(self) -> bool:
        return self.removed_sentences > 0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _revealed_ids(narrative: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in narrative.get("revealed") or []:
        if isinstance(item, Mapping) and _text(item.get("id")):
            result.add(_text(item.get("id")))
    return set(str(item) for item in narrative.get("revealed_clues") or []) | result


def _is_auto_revealed(item: Any, narrative: Mapping[str, Any]) -> bool:
    if isinstance(item, Mapping):
        reveal_when = item.get("reveal_when")
    else:
        reveal_when = getattr(item, "reveal_when", None)
    return bool(reveal_when) and evaluate_condition(reveal_when, narrative).ok


def forbidden_entries(bundle: NarrativeBundle, narrative: Mapping[str, Any]) -> list[dict[str, Any]]:
    """返回 (name, node_id, visibility) 列表；已揭示或 reveal_when 成立的除外。"""
    revealed = _revealed_ids(narrative)
    entries: list[dict[str, Any]] = []

    def _add(values: Any, object_id: str, visibility: str) -> None:
        for value in values:
            name = _text(value)
            if not name or name in GENERIC_NAMES or len(name) > 40:
                continue
            entry = {"name": name, "object_id": object_id, "visibility": visibility}
            if entry not in entries:
                entries.append(entry)

    for node in bundle.nodes:
        if node.visibility == VISIBILITY_KP_ONLY:
            _add((node.title, *node.aliases, *node.keywords), node.id, VISIBILITY_KP_ONLY)
        elif node.visibility == VISIBILITY_HIDDEN and node.id not in revealed and not _is_auto_revealed(node, narrative):
            _add((node.title, *node.aliases, *node.keywords), node.id, VISIBILITY_HIDDEN)

        # 公开地点里的隐藏物件同样要拦；物件自己的 reveal_when 成立后放行。
        for obj in node.objects:
            if not obj.get("visible", True):
                continue
            state = _text(obj.get("visibility")) or VISIBILITY_PUBLIC
            object_id = _text(obj.get("id"))
            if state == VISIBILITY_PUBLIC or object_id in revealed:
                continue
            if _is_auto_revealed(obj, narrative):
                continue
            _add((obj.get("name"),), object_id, state)

    for edge in bundle.edges:
        if edge.visibility == VISIBILITY_PUBLIC:
            continue
        if edge.visibility == VISIBILITY_KP_ONLY:
            _add((edge.label,), edge.id, VISIBILITY_KP_ONLY)
            continue
        if edge.id in revealed or _is_auto_revealed(edge, narrative):
            continue
        _add((edge.label,), edge.id, VISIBILITY_HIDDEN)
    return entries


def scan_output(text: str, bundle: NarrativeBundle, narrative: Mapping[str, Any]) -> GuardReport:
    original = str(text or "")
    if not original.strip():
        return GuardReport(text=original)
    entries = forbidden_entries(bundle, narrative)
    if not entries:
        return GuardReport(text=original)

    violations: list[dict[str, Any]] = []
    removed = 0
    kept: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(original):
        if not sentence:
            continue
        hits = [entry for entry in entries if entry["name"] in sentence]
        if not hits:
            kept.append(sentence)
            continue
        removed += 1
        for entry in hits:
            marker = {"object_id": entry["object_id"], "visibility": entry["visibility"]}
            if marker not in violations:
                violations.append(marker)
    sanitized = "".join(kept).strip()
    if removed and not sanitized:
        sanitized = "（这段描述涉及尚未解锁的内容，暂时无法呈现。）"
    return GuardReport(text=sanitized if removed else original, violations=violations, removed_sentences=removed)


def apply_output_guard_report(
    text: str,
    bundle: NarrativeBundle | None,
    narrative: Mapping[str, Any] | None,
    policy: GuardPolicy | None = None,
    *,
    room_id: str | None = None,
) -> GuardReport:
    """与 ``apply_output_guard`` 相同，但返回完整报告供指标与轨迹记录。"""
    policy = policy or GuardPolicy.for_mode("enforce")
    if bundle is None or not policy.spoiler_gate:
        return GuardReport(text=text)
    report = scan_output(text, bundle, narrative or {})
    if report.changed:
        logger.warning(
            "narrative.guard.%s room=%s sentences=%s objects=%s",
            "shadow" if policy.shadow else "output_removed",
            room_id, report.removed_sentences,
            ",".join(item["object_id"] for item in report.violations[:5]),
        )
    report.text = text if policy.shadow else report.text
    return report


def apply_output_guard(
    text: str,
    bundle: NarrativeBundle | None,
    narrative: Mapping[str, Any] | None,
    policy: GuardPolicy | None = None,
    *,
    room_id: str | None = None,
) -> str:
    return apply_output_guard_report(text, bundle, narrative, policy, room_id=room_id).text


__all__ = [
    "GuardPolicy",
    "GuardReport",
    "apply_output_guard",
    "forbidden_entries",
    "scan_output",
]
