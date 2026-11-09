"""房间长期记忆巩固（防止长剧本后期遗忘早期剧情）。

设计要点：

- **只读已有落盘数据**：从房间事件日志（``state.json``）、AI 显式记忆
  （``agent_memory.json``）、图谱关键节点（``story_ledger.jsonl`` 的 key 事件）
  以及对话归档中抽取条目；不修改任何既有房间文件。
- **可追溯**：新条目写入独立的追加账本 ``memory_ledger.jsonl``，每条都带
  ``source``（来源类型 + 引用）与时间；注入时也声明来源，模型只能据此回忆，
  没有记录时必须明确说"没有记录"。
- **按相关性注入**：每轮只挑与当前输入/最近几轮相关的少量条目，受字符预算约束，
  不注入全量历史。
- **对话不丢**：历史文件会被裁剪为最近 20 条；在裁剪或压缩前先把原文归档到
  记忆账本，保证早期对话仍可被检索，而不是仅依赖模型生成的摘要。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from trpg_server.json_store import read_json

logger = logging.getLogger(__name__)

MEMORY_LEDGER_FILENAME = "memory_ledger.jsonl"
MAX_RECORD_TEXT = 300
MAX_LOAD_RECORDS = 2000
MAX_ARCHIVE_ITEMS = 200
MAX_ARCHIVE_TEXT = 240

_KIND_LABELS = {
    "promise": "承诺",
    "decision": "决策",
    "clue": "线索",
    "item": "物品",
    "npc": "NPC",
    "location": "地点",
    "event": "事件",
    "dialogue": "对话",
    "summary": "摘要",
}

_SOURCE_LABELS = {
    "event_log": "房间事件日志",
    "agent_memory": "AI 记忆",
    "story_ledger": "关键节点",
    "dialogue": "对话归档",
    "compact_summary": "历史摘要",
}

# 「关键事实」条目在滚动摘要里的中文标签 → 记忆类型。
_COMPACT_LABEL_KINDS = {
    "承诺": "promise",
    "决策": "decision",
    "线索": "clue",
    "物品": "item",
    "地点": "location",
    "NPC": "npc",
}

_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_COMPACT_FACT_RE = re.compile(r"^\s*-\s*\[(?P<label>[^\]]+)\]\s*(?P<text>.+?)\s*$")
_APPEND_LOCK = threading.Lock()


def ledger_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / MEMORY_LEDGER_FILENAME


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _clip(text: str, limit: int = MAX_RECORD_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def load_memory_records(room_dir: Path | str, limit: int = MAX_LOAD_RECORDS) -> list[dict[str, Any]]:
    """读取记忆账本；文件不存在或单行损坏时返回可用的部分。"""
    path = ledger_path(room_dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and str(payload.get("text") or "").strip():
                    records.append(payload)
    except OSError:
        logger.warning("memory.load_failed room=%s", Path(room_dir).name, exc_info=True)
        return []
    if limit and len(records) > limit:
        records = records[-limit:]
    return records


def _append_records(room_dir: Path | str, records: Iterable[Mapping[str, Any]]) -> int:
    items = [dict(item) for item in records if str(item.get("text") or "").strip()]
    if not items:
        return 0
    directory = Path(room_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        for item in items
    ]
    with _APPEND_LOCK:
        with ledger_path(directory).open("a", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
    return len(items)


def _make_record(
    *,
    text: str,
    kind: str,
    source_type: str,
    source_ref: str = "",
    importance: int = 2,
    turn: int = 0,
    created_at: str = "",
) -> dict[str, Any]:
    return {
        "id": f"mem_{uuid4().hex[:12]}",
        "kind": str(kind or "event")[:32],
        "text": _clip(text),
        "importance": max(1, min(5, int(importance or 1))),
        "turn": max(0, int(turn or 0)),
        "source": {"type": str(source_type or "event_log")[:32], "ref": str(source_ref or "")[:120]},
        "created_at": str(created_at or _now_text()),
    }


def _existing_texts(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {_normalize(str(item.get("text") or "")) for item in records}


def _append_new(room_dir: Path | str, candidates: list[dict[str, Any]]) -> int:
    existing = _existing_texts(load_memory_records(room_dir))
    fresh: list[dict[str, Any]] = []
    for item in candidates:
        key = _normalize(str(item.get("text") or ""))
        if not key or key in existing:
            continue
        existing.add(key)
        fresh.append(item)
        if len(fresh) >= MAX_ARCHIVE_ITEMS:
            break
    return _append_records(room_dir, fresh)


def _state_turn(state: Mapping[str, Any] | None) -> int:
    data = state if isinstance(state, Mapping) else {}
    clock = data.get("clock") if isinstance(data.get("clock"), Mapping) else {}
    try:
        return int(clock.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _record_from_event_log(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    data = state if isinstance(state, Mapping) else {}
    events = data.get("event_log")
    records: list[dict[str, Any]] = []
    if not isinstance(events, list):
        return records
    for index, event in enumerate(events[-MAX_ARCHIVE_ITEMS:]):
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("kind") or "event")
        content = _clip(str(event.get("content") or ""))
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        text = content
        if kind == "state_update" and metadata:
            parts: list[str] = []
            if metadata.get("active_scene_id"):
                parts.append(f"场景推进到 {metadata.get('active_scene_id')}")
            for key, label in (("clues", "获得线索"), ("items", "获得物品"), ("quests", "更新任务")):
                values = metadata.get(key)
                if isinstance(values, list) and values:
                    parts.append(f"{label} {'、'.join(str(item) for item in values[:5])}")
            if metadata.get("completed") or metadata.get("ending_reached"):
                parts.append("剧情到达结局")
            if parts:
                text = "；".join(parts)
        if not text:
            continue
        memory_kind = "decision" if kind == "player_decision" else "event"
        records.append(
            _make_record(
                text=text,
                kind=memory_kind,
                source_type="event_log",
                source_ref=str(event.get("id") or f"event:{index}"),
                importance=3 if kind == "player_decision" else 2,
                turn=_state_turn(data),
                created_at=str(event.get("created_at") or ""),
            )
        )
    return records


def _record_from_agent_memory(room_dir: Path | str) -> list[dict[str, Any]]:
    payload = read_json(Path(room_dir) / "agent_memory.json", default=[])
    if not isinstance(payload, list):
        return []
    records: list[dict[str, Any]] = []
    for item in payload[-MAX_ARCHIVE_ITEMS:]:
        if not isinstance(item, Mapping):
            continue
        text = _clip(str(item.get("content") or ""))
        if not text:
            continue
        kind = str(item.get("kind") or "event")
        if kind in {"player_decision"}:
            kind = "decision"
        elif kind in {"npc_state", "npc_status"}:
            kind = "npc"
        records.append(
            _make_record(
                text=text,
                kind=kind,
                source_type="agent_memory",
                source_ref=str(item.get("id") or ""),
                importance=int(item.get("importance") or 2),
                created_at=str(item.get("created_at") or ""),
            )
        )
    return records


def _record_from_story_ledger(room_dir: Path | str) -> list[dict[str, Any]]:
    """图谱房间的关键节点（``key=true``）也并入长期记忆。"""
    try:
        from trpg_server.agents.narrative.ledger import read_ledger_events
    except Exception:  # noqa: BLE001 - 图谱模块不可用时不影响普通房间
        return []
    try:
        events = read_ledger_events(room_dir, key_only=True, limit=MAX_ARCHIVE_ITEMS)
    except (OSError, ValueError):
        logger.warning("memory.ledger_read_failed room=%s", Path(room_dir).name, exc_info=True)
        return []
    records: list[dict[str, Any]] = []
    for event in events:
        text = _clip(str(event.get("summary") or ""))
        if not text:
            continue
        records.append(
            _make_record(
                text=text,
                kind="event",
                source_type="story_ledger",
                source_ref=str(event.get("id") or ""),
                importance=3,
                turn=int(event.get("turn") or 0),
                created_at=str(event.get("created_at") or ""),
            )
        )
    return records


def harvest_room_memory(room_dir: Path | str, state: Mapping[str, Any] | None = None) -> int:
    """从既有记录抽取长期记忆；幂等（相同文本不会重复写入）。"""
    if not room_dir:
        return 0
    event_records = _record_from_event_log(state)
    memory_records = _record_from_agent_memory(room_dir)
    ledger_records = _record_from_story_ledger(room_dir)
    added = _append_new(room_dir, [*event_records, *memory_records, *ledger_records])
    if added:
        logger.info(
            "memory.harvest room=%s added=%d event_log=%d agent_memory=%d ledger=%d",
            Path(room_dir).name,
            added,
            len(event_records),
            len(memory_records),
            len(ledger_records),
        )
    return added


def archive_history_for_memory(
    room_dir: Path | str,
    history: list[dict[str, Any]] | None,
    *,
    max_items: int = 20,
    exclude_recent: int = 0,
) -> int:
    """把即将被压缩/裁剪的历史原文归档，保证早期对话仍可检索。

    ``exclude_recent`` 用于跳过仍在当前上下文里的最近几条，避免同一内容
    既出现在历史里又被当作长期记忆重复注入。
    """
    if not room_dir or not isinstance(history, list):
        return 0
    window = history[-MAX_ARCHIVE_ITEMS:]
    if exclude_recent > 0:
        window = window[:-exclude_recent]
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(window):
        if not isinstance(item, Mapping) or item.get("compact"):
            continue
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant", "system"}:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        prefix = {"user": "玩家", "assistant": "KP", "system": "系统"}[role]
        candidates.append(
            _make_record(
                text=f"{prefix}：{content[:MAX_ARCHIVE_TEXT]}",
                kind="dialogue",
                source_type="dialogue",
                source_ref=f"history:{index}",
                importance=2 if role == "user" else 1,
            )
        )
    # 只归档最近 max_items 条，避免每次请求重复扫描整段历史。
    added = _append_new(room_dir, candidates[-max(1, max_items):])
    if added:
        logger.info("memory.archive room=%s added=%d", Path(room_dir).name, added)
    return added


def remember_compact_facts(room_dir: Path | str, compact_entries: list[dict[str, Any]] | None) -> int:
    """把滚动摘要里的结构化「关键事实」落进记忆账本（保留来源=历史摘要）。"""
    if not room_dir or not isinstance(compact_entries, list):
        return 0
    candidates: list[dict[str, Any]] = []
    for entry in compact_entries:
        if not isinstance(entry, Mapping) or not entry.get("compact"):
            continue
        lines = str(entry.get("content") or "").splitlines()
        for line in lines:
            match = _COMPACT_FACT_RE.match(line)
            if not match:
                continue
            label = match.group("label").strip()
            text = match.group("text").strip()
            if not text:
                continue
            candidates.append(
                _make_record(
                    text=text,
                    kind=_COMPACT_LABEL_KINDS.get(label, "summary"),
                    source_type="compact_summary",
                    source_ref="compact",
                    importance=3 if label in {"承诺", "决策"} else 2,
                )
            )
    added = _append_new(room_dir, candidates)
    if added:
        logger.info("memory.compact_facts room=%s added=%d", Path(room_dir).name, added)
    return added


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").casefold()
    tokens = set(_WORD_RE.findall(lowered))
    cjk = "".join(_CJK_RE.findall(lowered))
    tokens.update(cjk[i : i + 2] for i in range(max(0, len(cjk) - 1)))
    return tokens


def _score_record(record: Mapping[str, Any], query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    record_tokens = _tokens(str(record.get("text") or ""))
    if not record_tokens:
        return 0.0
    overlap = len(query_tokens & record_tokens)
    if not overlap:
        return 0.0
    # 对话归档只靠两个汉字的偶然重合很容易变成噪声，至少命中两个双字词才召回。
    if str(record.get("kind") or "") == "dialogue" and overlap < 2:
        return 0.0
    importance = max(1, min(5, int(record.get("importance") or 1)))
    kind_bonus = {"promise": 1.5, "decision": 1.0, "clue": 0.8, "npc": 0.5}.get(str(record.get("kind") or ""), 0.0)
    return overlap * (1.0 + importance * 0.25) + kind_bonus


def _fallback_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """没有相关命中时，只补最近两条承诺/决策，维持基本连续性。"""
    structured = [item for item in records if str(item.get("kind") or "") in {"promise", "decision"}]
    return structured[-2:]


def _format_record(record: Mapping[str, Any]) -> str:
    kind = _KIND_LABELS.get(str(record.get("kind") or ""), "记录")
    source = _SOURCE_LABELS.get(str((record.get("source") or {}).get("type") or ""), "房间记录")
    turn = int(record.get("turn") or 0)
    stamp = str(record.get("created_at") or "").strip()
    meta = [kind, source]
    if turn > 0:
        meta.insert(0, f"第 {turn} 轮")
    elif stamp:
        meta.insert(0, stamp)
    return f"- [{' · '.join(meta)}] {str(record.get('text') or '').strip()}"


def build_memory_context(
    room_dir: Path | str,
    query: str,
    *,
    budget_chars: int = 800,
    max_items: int = 6,
) -> str:
    """按相关性选取少量长期记忆，渲染为可直接注入的系统消息。

    返回空串表示本房间还没有可召回的记忆，调用方应跳过注入。
    """
    if not room_dir:
        return ""
    try:
        budget = int(budget_chars)
    except (TypeError, ValueError):
        budget = 800
    if budget <= 0:
        return ""
    try:
        limit = max(1, int(max_items))
    except (TypeError, ValueError):
        limit = 6
    records = load_memory_records(room_dir)
    if not records:
        return ""
    query_tokens = _tokens(query)
    scored = [(_score_record(item, query_tokens), index, item) for index, item in enumerate(records)]
    hits = [(score, index, item) for score, index, item in scored if score > 0]
    hits.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    selected = [item for _, _, item in hits[:limit]]
    if not selected:
        selected = _fallback_records(records)
    if not selected:
        return ""

    header = (
        "【长期记忆·回顾参考】以下条目来自本房间已记录的历史，按与当前情境的相关度选取，"
        "不是本轮新发生的事。只能据此回忆玩家早期做过什么、答应过谁什么；"
        "若某件事在这里和历史对话里都没有记录，必须明确说明“没有找到相关记录”，不得编造。"
    )
    lines = [header]
    used = len(header)
    injected = 0
    injected_ids: list[str] = []
    for record in selected:
        line = _format_record(record)
        if used + len(line) + 1 > budget:
            continue
        lines.append(line)
        used += len(line) + 1
        injected += 1
        injected_ids.append(str(record.get("id") or "-"))
    if injected == 0:
        return ""
    logger.info(
        "memory.inject room=%s items=%d chars=%d candidates=%d query_chars=%d ids=%s",
        Path(room_dir).name,
        injected,
        used,
        len(hits),
        len(str(query or "")),
        ",".join(injected_ids),
    )
    return "\n".join(lines)


def memory_recall(room_dir: Path | str, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """给房间工具使用：按相关度返回长期记忆（无查询时按重要性/时间倒序）。"""
    if not room_dir:
        return []
    records = load_memory_records(room_dir)
    if not records:
        return []
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    query_tokens = _tokens(query)
    if query_tokens:
        scored = [(_score_record(item, query_tokens), index, item) for index, item in enumerate(records)]
        hits = [(score, index, item) for score, index, item in scored if score > 0]
        hits.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        selected = [item for _, _, item in hits[:limit]]
        if selected:
            return selected
    ordered = sorted(
        records,
        key=lambda item: (int(item.get("importance") or 1), str(item.get("created_at") or "")),
        reverse=True,
    )
    return ordered[:limit]


__all__ = [
    "MEMORY_LEDGER_FILENAME",
    "archive_history_for_memory",
    "build_memory_context",
    "harvest_room_memory",
    "ledger_path",
    "load_memory_records",
    "memory_recall",
    "remember_compact_facts",
]
