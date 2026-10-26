"""房间记忆账本、事实与摘要（设计文档 §4.4、§6.1-§6.3）。

账本是追加写、不可修改的 `story_ledger.jsonl`；事实在 `facts.json`，
摘要按位置/区域/总览分层存放在 `summaries.json`。所有写入都在落盘前
检查 KP-only 专名，防止真相通过摘要或账本泄漏给玩家侧。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from trpg_server.agents.narrative.model import NarrativeBundle
from trpg_server.agents.narrative.visibility import assert_no_kp_only
from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

STORY_LEDGER_FILENAME = "story_ledger.jsonl"
FACTS_FILENAME = "facts.json"
SUMMARIES_FILENAME = "summaries.json"
MAX_LEDGER_SUMMARY = 500
MAX_KEYFRAME_EVENTS = 2000

EVENT_KINDS = (
    "action", "dialogue", "discovery", "state_change", "check",
    "interaction", "move", "clock", "trigger",
)
EVENT_SOURCES = ("player", "kp", "system", "script")
FACT_STATUSES = ("canon", "learned", "rumor", "provisional", "retracted")
SUMMARY_SCOPES = ("location", "area", "scenario")


class LedgerVisibilityError(ValueError):
    """账本/摘要试图写入 KP-only 专名。"""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _guard_text(text: str, bundle: NarrativeBundle | None, *, scope: str, room_dir: Path | str) -> str:
    if bundle is None:
        return text
    hits = assert_no_kp_only(text, bundle)
    if hits:
        logger.warning(
            "narrative.visibility_violation room=%s scope=%s hits=%d",
            Path(room_dir).name, scope, len(hits),
        )
        raise LedgerVisibilityError("content contains kp-only names")
    return text


def ledger_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / STORY_LEDGER_FILENAME


def append_ledger_event(
    room_dir: Path | str,
    event: Mapping[str, Any],
    *,
    bundle: NarrativeBundle | None = None,
) -> dict[str, Any]:
    """追加一条账本；按 `id` 幂等，落盘后 fsync。"""
    directory = Path(room_dir)
    directory.mkdir(parents=True, exist_ok=True)
    event_id = str(event.get("id") or "").strip() or f"evt_{uuid4().hex[:12]}"
    existing = read_ledger_events(directory, limit=MAX_KEYFRAME_EVENTS)
    if any(str(item.get("id") or "") == event_id for item in existing):
        for item in reversed(existing):
            if str(item.get("id") or "") == event_id:
                return item

    summary = str(event.get("summary") or "").strip()
    if summary:
        summary = _guard_text(summary, bundle, scope="ledger", room_dir=directory)
    kind = str(event.get("kind") or "action").strip().lower()
    if kind not in EVENT_KINDS:
        kind = "action"
    source = str(event.get("source") or "kp").strip().lower()
    if source not in EVENT_SOURCES:
        source = "kp"
    item = {
        "id": event_id,
        "turn": _as_int(event.get("turn"), 0),
        "location_id": str(event.get("location_id") or "").strip() or None,
        "kind": kind,
        "summary": summary[:MAX_LEDGER_SUMMARY],
        "entities": _str_list(event.get("entities")),
        "facts": _str_list(event.get("facts")),
        "source": source,
        "key": bool(event.get("key", False)),
        "created_at": str(event.get("created_at") or _now_text()),
    }
    for extra in ("check_id", "object_id", "interaction_id", "trigger_id"):
        if event.get(extra):
            item[extra] = str(event.get(extra))

    line = json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
    with ledger_path(directory).open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    logger.info(
        "narrative.ledger.append room=%s turn=%s event=%s kind=%s key=%s",
        directory.name, item["turn"], event_id, kind, item["key"],
    )
    return item


def _iter_raw_events(room_dir: Path | str) -> Iterable[tuple[int, dict[str, Any]]]:
    path = ledger_path(room_dir)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                yield index, payload


def read_ledger_events(
    room_dir: Path | str,
    *,
    limit: int | None = None,
    key_only: bool = False,
    search: str | None = None,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    """按文件顺序读取；`after_seq` 是 1-based 行号，用于关键帧重放。"""
    query = str(search or "").strip().casefold()
    events: list[dict[str, Any]] = []
    for seq, payload in _iter_raw_events(room_dir):
        if seq <= after_seq:
            continue
        if key_only and not payload.get("key"):
            continue
        if query and query not in json.dumps(payload, ensure_ascii=False).casefold():
            continue
        events.append({**payload, "_seq": seq})
    if limit is not None and limit > 0:
        events = events[-limit:]
    return events


def ledger_size(room_dir: Path | str) -> int:
    path = ledger_path(room_dir)
    return path.stat().st_size if path.exists() else 0


def facts_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / FACTS_FILENAME


def load_facts(room_dir: Path | str) -> list[dict[str, Any]]:
    payload = read_json(facts_path(room_dir), default=[])
    return [dict(item) for item in payload if isinstance(item, Mapping)] if isinstance(payload, list) else []


def append_fact(
    room_dir: Path | str,
    fact: Mapping[str, Any],
    *,
    bundle: NarrativeBundle | None = None,
) -> dict[str, Any]:
    """新增事实三元组；同 id 幂等，默认 `provisional`（§4.4）。"""
    directory = Path(room_dir)
    facts = load_facts(directory)
    fact_id = str(fact.get("id") or "").strip() or f"fact_{uuid4().hex[:10]}"
    for existing in facts:
        if str(existing.get("id") or "") == fact_id:
            return existing
    subject = str(fact.get("subject") or "").strip()
    predicate = str(fact.get("predicate") or "").strip()
    object_value = str(fact.get("object") or "").strip()
    if not subject or not predicate or not object_value:
        raise ValueError("subject, predicate and object are required")
    status = str(fact.get("status") or "provisional").strip().lower()
    if status not in FACT_STATUSES:
        status = "provisional"
    if status == "canon" and any(
        str(item.get("subject")) == subject
        and str(item.get("predicate")) == predicate
        and str(item.get("object")) == object_value
        and item.get("status") == "canon"
        for item in facts
    ):
        return next(
            item for item in facts
            if str(item.get("subject")) == subject and str(item.get("predicate")) == predicate
            and str(item.get("object")) == object_value and item.get("status") == "canon"
        )
    item = {
        "id": fact_id,
        "subject": subject,
        "predicate": predicate,
        "object": object_value,
        "status": status,
        "confidence": round(_as_float(fact.get("confidence"), 0.5), 3),
        "evidence": _str_list(fact.get("evidence")),
        "turn": _as_int(fact.get("turn"), 0),
        "visibility": str(fact.get("visibility") or "kp_only"),
        "created_at": str(fact.get("created_at") or _now_text()),
    }
    facts.append(item)
    write_json_atomic(facts_path(directory), facts)
    logger.info(
        "narrative.fact.append room=%s turn=%s fact=%s status=%s",
        directory.name, item["turn"], fact_id, status,
    )
    return item


def set_fact_status(room_dir: Path | str, fact_id: str, status: str) -> dict[str, Any] | None:
    normalized = str(status or "").strip().lower()
    if normalized not in FACT_STATUSES:
        raise ValueError(f"invalid fact status: {status}")
    directory = Path(room_dir)
    facts = load_facts(directory)
    for item in facts:
        if str(item.get("id") or "") == str(fact_id):
            item["status"] = normalized
            write_json_atomic(facts_path(directory), facts)
            return item
    return None


def summaries_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / SUMMARIES_FILENAME


def load_summaries(room_dir: Path | str) -> dict[str, Any]:
    payload = read_json(summaries_path(room_dir), default={})
    if not isinstance(payload, Mapping):
        return {scope: {} for scope in SUMMARY_SCOPES}
    return {
        scope: dict(payload.get(scope) or {}) if isinstance(payload.get(scope), Mapping) else {}
        for scope in SUMMARY_SCOPES
    }


def save_summary(
    room_dir: Path | str,
    scope: str,
    scope_id: str,
    text: str,
    *,
    turn: int = 0,
    bundle: NarrativeBundle | None = None,
) -> dict[str, Any]:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in SUMMARY_SCOPES:
        raise ValueError(f"invalid summary scope: {scope}")
    key = str(scope_id or "").strip() or normalized_scope
    content = _guard_text(str(text or "").strip(), bundle, scope=f"summary:{normalized_scope}", room_dir=room_dir)
    summaries = load_summaries(room_dir)
    entry = {
        "text": content[:1200],
        "turn": _as_int(turn, 0),
        "updated_at": _now_text(),
    }
    summaries[normalized_scope][key] = entry
    write_json_atomic(summaries_path(room_dir), summaries)
    return entry


__all__ = [
    "EVENT_KINDS",
    "EVENT_SOURCES",
    "FACT_STATUSES",
    "FACTS_FILENAME",
    "LedgerVisibilityError",
    "STORY_LEDGER_FILENAME",
    "SUMMARIES_FILENAME",
    "append_fact",
    "append_ledger_event",
    "facts_path",
    "ledger_path",
    "ledger_size",
    "load_facts",
    "load_summaries",
    "read_ledger_events",
    "save_summary",
    "set_fact_status",
    "summaries_path",
]
