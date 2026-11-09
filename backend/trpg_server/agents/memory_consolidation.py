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

性能约定（长剧本关键）：

- 账本按「文件指纹（mtime_ns + size）」做内存索引缓存。同一轮里
  ``harvest_room_memory`` / ``archive_history_for_memory`` / ``build_memory_context``
  共用同一份索引，只有文件真正变化时才重新解析，且只解析新增的尾部字节。
- 每条记录的检索分词结果按记录 id 缓存，跨轮未变更时不重复分词。
- 账本超过阈值后轮转为 ``memory_ledger.<n>.jsonl`` 分片（格式不变，旧文件仍可读），
  去重集合覆盖所有已加载分片，不再只检查最近 2000 条。
"""
from __future__ import annotations

import json
import logging
import os
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

# 分片轮转：单个账本超过条数或体积阈值后改名为 memory_ledger.<n>.jsonl，
# 新建空账本继续追加。最旧的若干分片不再载入索引（文件保留在磁盘上）。
MEMORY_LEDGER_MAX_SEGMENT_RECORDS = 4000
MEMORY_LEDGER_MAX_SEGMENT_BYTES = 4 * 1024 * 1024
MEMORY_LEDGER_MAX_SEGMENTS = 12

_ROOM_CACHE_LIMIT = 64
_TOKEN_CACHE_LIMIT = 60000

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
_LOCK = threading.RLock()

# 可观测计数：测试与排查用，不参与业务逻辑。
_STATS: dict[str, int] = {
    "segment_reads": 0,       # 真正打开文件解析的次数
    "records_parsed": 0,      # 解析出的记录条数
    "tokenizations": 0,       # 记录级分词次数
    "index_builds": 0,        # 索引重组次数
    "index_hits": 0,          # 指纹命中、直接复用次数
}


def memory_cache_stats() -> dict[str, int]:
    with _LOCK:
        return dict(_STATS)


def reset_memory_cache_stats() -> None:
    with _LOCK:
        for key in _STATS:
            _STATS[key] = 0


def _bump(key: str, amount: int = 1) -> None:
    _STATS[key] = _STATS.get(key, 0) + amount


def ledger_path(room_dir: Path | str) -> Path:
    return Path(room_dir) / MEMORY_LEDGER_FILENAME


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _clip(text: str, limit: int = MAX_RECORD_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


# ---------------------------------------------------------------------------
# 账本索引（文件指纹 + 内存缓存）
# ---------------------------------------------------------------------------


class _Segment:
    """单个账本文件的解析结果；追加时原地扩展，指纹随之更新。"""

    __slots__ = ("path", "fingerprint", "size", "records", "text_keys")

    def __init__(self, path: Path, fingerprint: tuple[int, int], size: int) -> None:
        self.path = path
        self.fingerprint = fingerprint
        self.size = size
        self.records: list[dict[str, Any]] = []
        self.text_keys: set[str] = set()

    def add_records(self, items: list[dict[str, Any]]) -> None:
        self.records.extend(items)
        for item in items:
            self.text_keys.add(_normalize(str(item.get("text") or "")))


class _RoomCache:
    """房间级索引：分片列表 + 拼接视图 + 分词缓存。"""

    __slots__ = ("room_dir", "segments", "records", "key", "token_cache", "last_access")

    def __init__(self, room_dir: Path) -> None:
        self.room_dir = room_dir
        self.segments: dict[str, _Segment] = {}
        self.records: list[dict[str, Any]] | None = None
        self.key: tuple[tuple[str, tuple[int, int]], ...] | None = None
        self.token_cache: dict[str, frozenset[str]] = {}
        self.last_access = time.monotonic()

    def exists(self, text_key: str) -> bool:
        with _LOCK:
            return any(text_key in segment.text_keys for segment in self.segments.values())

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with _LOCK:
            records = self.records or []
            if limit and len(records) > limit:
                return records[-limit:]
            return list(records)

    def remember_tokens(self, record: Mapping[str, Any]) -> frozenset[str]:
        key = _token_key(record)
        cached = self.token_cache.get(key)
        if cached is not None:
            return cached
        tokens = frozenset(_tokens(str(record.get("text") or "")))
        if len(self.token_cache) >= _TOKEN_CACHE_LIMIT:
            self.token_cache.clear()
        self.token_cache[key] = tokens
        _bump("tokenizations")
        return tokens


_ROOM_CACHE: dict[str, _RoomCache] = {}


def _room_key(room_dir: Path | str) -> str:
    return str(Path(room_dir).resolve())


def _segment_paths(room_dir: Path) -> list[Path]:
    """按「最旧分片 → 最新分片 → 活动账本」返回需要读取的文件。"""
    rotated: list[tuple[int, Path]] = []
    if room_dir.is_dir():
        for path in room_dir.glob("memory_ledger.*.jsonl"):
            suffix = path.name[len("memory_ledger.") : -len(".jsonl")]
            if suffix.isdigit():
                rotated.append((int(suffix), path))
    rotated.sort()
    if MEMORY_LEDGER_MAX_SEGMENTS > 0:
        rotated = rotated[-MEMORY_LEDGER_MAX_SEGMENTS:]
    paths = [path for _, path in rotated]
    active = ledger_path(room_dir)
    if active.exists():
        paths.append(active)
    return paths


def _parse_segment(path: Path, cached: _Segment | None) -> _Segment | None:
    """读取分片；文件未变直接复用，仅追加时从旧长度处继续解析。"""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    if cached is not None and cached.fingerprint == fingerprint:
        return cached

    segment = cached
    start = 0
    if segment is None:
        segment = _Segment(path, fingerprint, 0)
    elif stat.st_size > segment.size:
        # 只追加过：从上次读到的字节位置继续，避免整文件重解析。
        start = segment.size
    else:
        # 文件被截断/重写（例如手工修复）：整份重读。
        segment = _Segment(path, fingerprint, 0)

    try:
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
            chunk = handle.read()
    except OSError:
        logger.warning("memory.load_failed room=%s", path.parent.name, exc_info=True)
        return segment

    consumed = len(chunk)
    if chunk and not chunk.endswith(b"\n"):
        # 最后一行可能仍在上游写入中：不解析、也不推进已读长度。
        cut = chunk.rfind(b"\n")
        if cut < 0:
            chunk = b""
            consumed = 0
        else:
            chunk = chunk[: cut + 1]
            consumed = cut + 1
    if chunk:
        _bump("segment_reads")
    parsed: list[dict[str, Any]] = []
    for raw in chunk.splitlines():
        text = raw.strip()
        if not text:
            continue
        try:
            payload = json.loads(text.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict) and str(payload.get("text") or "").strip():
            parsed.append(payload)
    if parsed:
        _bump("records_parsed", len(parsed))
        segment.add_records(parsed)
    segment.size = start + consumed
    segment.fingerprint = fingerprint
    return segment


def _refresh(cache: _RoomCache) -> _RoomCache:
    with _LOCK:
        paths = _segment_paths(cache.room_dir)
        segments: list[_Segment] = []
        key_parts: list[tuple[str, tuple[int, int]]] = []
        for path in paths:
            segment = _parse_segment(path, cache.segments.get(str(path)))
            if segment is None:
                continue
            segments.append(segment)
            key_parts.append((str(path), segment.fingerprint))
        key = tuple(key_parts)
        if cache.records is not None and cache.key == key:
            _bump("index_hits")
            return cache
        records: list[dict[str, Any]] = []
        for segment in segments:
            records.extend(segment.records)
        cache.segments = {str(segment.path): segment for segment in segments}
        cache.records = records
        cache.key = key
        _bump("index_builds")
        return cache


def _index(room_dir: Path | str) -> _RoomCache:
    key = _room_key(room_dir)
    with _LOCK:
        cache = _ROOM_CACHE.get(key)
        if cache is None:
            cache = _RoomCache(Path(room_dir))
            _ROOM_CACHE[key] = cache
            if len(_ROOM_CACHE) > _ROOM_CACHE_LIMIT:
                oldest = min(_ROOM_CACHE, key=lambda item: _ROOM_CACHE[item].last_access)
                if oldest != key:
                    _ROOM_CACHE.pop(oldest, None)
        cache.last_access = time.monotonic()
    return _refresh(cache)


def invalidate_memory_cache(room_dir: Path | str | None = None) -> None:
    """测试或外部改文件后手动失效；正常追加由写入方就地更新。"""
    with _LOCK:
        if room_dir is None:
            _ROOM_CACHE.clear()
        else:
            _ROOM_CACHE.pop(_room_key(room_dir), None)


def load_memory_records(room_dir: Path | str, limit: int = MAX_LOAD_RECORDS) -> list[dict[str, Any]]:
    """返回最近 ``limit`` 条记录（旧的在前）；文件未变时走内存索引。"""
    if not room_dir:
        return []
    return _index(room_dir).recent(limit)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _update_cache_after_append(
    room_dir: Path,
    active: Path,
    items: list[dict[str, Any]],
    written_bytes: int,
) -> None:
    """把自己刚写入的条目直接并入内存索引，避免同轮再次解析这些行。"""
    cache = _ROOM_CACHE.get(_room_key(room_dir))
    if cache is None or cache.records is None:
        return
    try:
        stat = active.stat()
    except OSError:
        cache.key = None
        return
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    segment = cache.segments.get(str(active))
    if segment is None:
        if stat.st_size != written_bytes:
            # 文件里已有别人的内容：交给下次读取从头解析。
            cache.key = None
            return
        # 轮转后新建的活动分片，内容恰好就是本次写入的行。
        segment = _Segment(active, fingerprint, written_bytes)
        cache.segments[str(active)] = segment
    segment.add_records(items)
    cache.records.extend(items)
    segment.size = stat.st_size
    segment.fingerprint = fingerprint
    # 指纹变化需要重组拼接视图；分片对象已更新，重组不会重新打开文件。
    cache.key = None


def _rotate_if_needed(room_dir: Path, active: Path) -> None:
    cache = _ROOM_CACHE.get(_room_key(room_dir))
    segment = cache.segments.get(str(active)) if cache else None
    try:
        stat = active.stat()
    except OSError:
        return
    too_many = segment is not None and len(segment.records) > MEMORY_LEDGER_MAX_SEGMENT_RECORDS
    too_big = stat.st_size > MEMORY_LEDGER_MAX_SEGMENT_BYTES
    if not (too_many or too_big):
        return
    existing = [
        int(path.name[len("memory_ledger.") : -len(".jsonl")])
        for path in room_dir.glob("memory_ledger.*.jsonl")
        if path.name[len("memory_ledger.") : -len(".jsonl")].isdigit()
    ]
    target = room_dir / f"memory_ledger.{max(existing, default=0) + 1}.jsonl"
    try:
        os.replace(active, target)
    except OSError:
        logger.warning("memory.rotate_failed room=%s", room_dir.name, exc_info=True)
        return
    logger.info("memory.rotate room=%s segment=%s", room_dir.name, target.name)
    if cache is None:
        return
    if segment is not None:
        cache.segments.pop(str(active), None)
        segment.path = target
        cache.segments[str(target)] = segment
    cache.key = None


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
    active = ledger_path(directory)
    written_bytes = sum(len(line.encode("utf-8")) for line in lines)
    with _LOCK:
        with active.open("a", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
        _update_cache_after_append(directory, active, items, written_bytes)
        _rotate_if_needed(directory, active)
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


def _append_new(room_dir: Path | str, candidates: list[dict[str, Any]]) -> int:
    """去重写入：去重集合覆盖全部已加载分片，不再只看最近 N 条尾部队列。"""
    if not candidates:
        return 0
    cache = _index(room_dir)
    seen: set[str] = set()
    fresh: list[dict[str, Any]] = []
    for item in candidates:
        key = _normalize(str(item.get("text") or ""))
        if not key or key in seen or cache.exists(key):
            continue
        seen.add(key)
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


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").casefold()
    tokens = set(_WORD_RE.findall(lowered))
    cjk = "".join(_CJK_RE.findall(lowered))
    tokens.update(cjk[i : i + 2] for i in range(max(0, len(cjk) - 1)))
    return tokens


def _token_key(record: Mapping[str, Any]) -> str:
    record_id = str(record.get("id") or "").strip()
    if record_id:
        return record_id
    return "text:" + _normalize(str(record.get("text") or ""))[:120]


def _score_record(record: Mapping[str, Any], query_tokens: set[str], token_cache: _RoomCache | None) -> float:
    if not query_tokens:
        return 0.0
    record_tokens = (
        token_cache.remember_tokens(record)
        if token_cache is not None
        else frozenset(_tokens(str(record.get("text") or "")))
    )
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
    cache = _index(room_dir)
    records = cache.recent(MAX_LOAD_RECORDS)
    if not records:
        return ""
    query_tokens = _tokens(query)
    scored = [(_score_record(item, query_tokens, cache), index, item) for index, item in enumerate(records)]
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
    cache = _index(room_dir)
    records = cache.recent(MAX_LOAD_RECORDS)
    if not records:
        return []
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    query_tokens = _tokens(query)
    if query_tokens:
        scored = [(_score_record(item, query_tokens, cache), index, item) for index, item in enumerate(records)]
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
    "invalidate_memory_cache",
    "ledger_path",
    "load_memory_records",
    "memory_cache_stats",
    "memory_recall",
    "remember_compact_facts",
    "reset_memory_cache_stats",
]
