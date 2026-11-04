"""叙事台词标记解析。

协议（同时写进 ``config/templates/roles/kp.md``）：

- ``【NPC:名字】`` 起一段 NPC 台词，全角/半角冒号都接受，名字可含空格；
- ``【旁白】`` 起一段旁白；
- 同一行内切换说话人必须重新标记；
- 没有标记的正文默认归旁白。

解析结果分两部分返回：
- ``clean_text``：剥离全部标记后的正文，唯一允许持久化/回灌模型的内容；
- ``segments``：按说话人切分并受单段/单回合上限约束的配音片段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from trpg_server.voice.profiles import normalize_speaker_name

_MARKER = re.compile(
    r"【\s*(?P<kind>NPC|旁白)\s*(?:(?P<colon>[：:])\s*(?P<name>[^】\n]*?))?\s*】",
    re.IGNORECASE,
)
# 未闭合标记：只认「行首 + 有冒号 + 本行无右括号」的情况，避免误伤正常文案。
_UNCLOSED = re.compile(
    r"^【\s*(?P<kind>NPC|旁白)\s*[：:]\s*(?P<name>[^】\n]{1,40})(?=\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
# 启发式兜底只接受「名字 + 可选说/道 + 冒号 + 引号台词」，避免把普通旁白误判成对白。
_HEURISTIC = re.compile(
    r"^\s*(?P<name>[^\s：:，。“”\"「」『』]{1,20})"
    r"\s*(?P<verb>说|说道|道|问|问道|喊|喊道|答|答道|低声道|沉声道|笑道|怒道)?"
    r"\s*[：:]\s*(?P<open>[“\"「『])(?P<text>.+?)(?P<close>[”\"」』])\s*$"
)
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;…\n])")
_SPACES = re.compile(r"\s+")

SOURCE_MARKUP = "markup"
SOURCE_HEURISTIC = "heuristic"
SOURCE_NARRATOR = "narrator"


@dataclass
class NpcEntry:
    name: str
    aliases: tuple[str, ...] = ()
    gender: str | None = None
    age_group: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized(self) -> str:
        return normalize_speaker_name(self.name)


@dataclass
class ParsedNarration:
    clean_text: str
    segments: list[dict[str, Any]]
    raw_segment_count: int
    had_markup: bool
    truncated: bool


def _as_entry(item: Any) -> NpcEntry | None:
    if isinstance(item, str):
        name = item.strip()
        return NpcEntry(name=name, data={"name": name}) if name else None
    if not isinstance(item, dict):
        return None
    name = str(
        item.get("name")
        or item.get("title")
        or item.get("npc_name")
        or item.get("label")
        or ""
    ).strip()
    if not name:
        return None
    aliases_raw = item.get("aliases") or item.get("alias") or item.get("aka") or []
    if isinstance(aliases_raw, str):
        aliases = tuple(part.strip() for part in re.split(r"[,，、/|]", aliases_raw) if part.strip())
    elif isinstance(aliases_raw, (list, tuple)):
        aliases = tuple(str(part).strip() for part in aliases_raw if str(part).strip())
    else:
        aliases = ()
    return NpcEntry(name=name, aliases=aliases, data=dict(item))


def build_npc_roster(scenario: dict[str, Any] | None, scene: dict[str, Any] | None = None) -> list[NpcEntry]:
    """从房间剧本快照里尽量宽容地收集 NPC 名字与别名。

    只读取 ``scenario.npcs/key_npcs``、``scene.npcs`` 与 NPC 类型模块；字段缺失时跳过，
    不猜测、不创造剧本以外的角色。
    """
    entries: list[NpcEntry] = []
    seen: set[str] = set()

    def add(item: Any) -> None:
        entry = _as_entry(item)
        if entry is None:
            return
        key = entry.normalized
        if not key or key in seen:
            return
        seen.add(key)
        entries.append(entry)

    data = scenario if isinstance(scenario, dict) else {}
    for key in ("npcs", "key_npcs", "main_npcs"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                add(item)
    modules = data.get("modules")
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            module_type = str(module.get("card_type") or module.get("module_type") or "").casefold()
            if module_type in {"npc", "npcs", "character"}:
                add(module)
    if isinstance(scene, dict):
        scene_npcs = scene.get("npcs")
        if isinstance(scene_npcs, list):
            for item in scene_npcs:
                add(item)
    return entries


def _alias_index(roster: Iterable[NpcEntry]) -> dict[str, NpcEntry]:
    index: dict[str, NpcEntry] = {}
    for entry in roster:
        for name in (entry.name, *entry.aliases):
            normalized = normalize_speaker_name(name)
            if normalized and normalized not in index:
                index[normalized] = entry
    return index


def _speaker_of(kind: str, name: str, source: str, roster_index: dict[str, NpcEntry]) -> dict[str, Any]:
    if kind != "npc":
        return {
            "speaker_id": "narrator",
            "display_name": "旁白",
            "kind": "narrator",
            "source": source,
        }
    # 显示名保留标记里的写法（含空格），身份用归一后的 speaker_id 保证别名一致。
    display = _SPACES.sub(" ", str(name or "")).strip()
    if not display:
        display = "NPC"
    return {
        "speaker_id": "npc:" + (normalize_speaker_name(display) or "unknown"),
        "display_name": display,
        "kind": "npc",
        "source": source,
    }


def _close_unclosed_markers(text: str) -> str:
    """把行首未闭合标记补成完整标记，既避免正文残留 ``【NPC:名字``，也不误伤正文。"""
    return _UNCLOSED.sub(lambda m: f"【{m.group('kind')}：{m.group('name').strip()}】", text)


def _chunk_text(text: str, limit: int) -> list[str]:
    """按句末标点/换行切块，单块不超过 ``limit``；超长单句再硬切。"""
    cleaned = str(text or "").replace("\r\n", "\n").strip()
    if not cleaned:
        return []
    pieces = [part for part in _SENTENCE_END.split(cleaned) if part.strip()]
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if len(piece) > limit:
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            for start in range(0, len(piece), limit):
                chunks.append(piece[start:start + limit].strip())
            continue
        if len(buffer) + len(piece) <= limit:
            buffer += piece
        else:
            chunks.append(buffer.strip())
            buffer = piece
    if buffer.strip():
        chunks.append(buffer.strip())
    return [chunk for chunk in chunks if chunk]


def _explicit_segments(
    text: str, roster_index: dict[str, NpcEntry]
) -> tuple[str, list[tuple[dict[str, Any], str]], bool]:
    markers = list(_MARKER.finditer(text))
    if not markers:
        return text, [], False
    logical: list[tuple[dict[str, Any], str]] = []
    current = _speaker_of("narrator", "", SOURCE_MARKUP, roster_index)
    cursor = 0
    clean_parts: list[str] = []
    for marker in markers:
        chunk = text[cursor:marker.start()]
        if chunk:
            clean_parts.append(chunk)
            if chunk.strip():
                logical.append((current, chunk))
        kind = str(marker.group("kind") or "旁白").upper()
        name = str(marker.group("name") or "")
        current = _speaker_of("npc" if kind == "NPC" else "narrator", name, SOURCE_MARKUP, roster_index)
        cursor = marker.end()
    tail = text[cursor:]
    if tail:
        clean_parts.append(tail)
        if tail.strip():
            logical.append((current, tail))
    return "".join(clean_parts), logical, True


def _heuristic_segments(
    text: str, roster_index: dict[str, NpcEntry]
) -> tuple[list[tuple[dict[str, Any], str]], int]:
    logical: list[tuple[dict[str, Any], str]] = []
    matched = 0
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        raw = line.strip()
        if not raw:
            continue
        match = _HEURISTIC.match(raw)
        entry = roster_index.get(normalize_speaker_name(match.group("name"))) if match else None
        if match and entry is not None:
            matched += 1
            speaker = _speaker_of("npc", entry.name, SOURCE_HEURISTIC, roster_index)
            logical.append((speaker, match.group("text").strip()))
        else:
            logical.append((_speaker_of("narrator", "", SOURCE_NARRATOR, roster_index), raw))
    return logical, matched


def parse_narration(
    raw_text: str,
    *,
    roster: Iterable[NpcEntry] | None = None,
    max_segment_chars: int = 40,
    max_turn_chars: int = 800,
    max_segments: int = 24,
    allow_heuristic: bool = True,
) -> ParsedNarration:
    """解析一段 AI 叙事，剥离标记并产出受配额的配音片段。"""
    source_text = str(raw_text or "")
    roster_index = _alias_index(roster or ())
    normalized_text = _close_unclosed_markers(source_text)
    explicit_text, explicit_logical, had_markup = _explicit_segments(normalized_text, roster_index)

    if had_markup:
        clean_text = explicit_text
        logical = explicit_logical
        heuristic_matched = 0
    else:
        clean_text = normalized_text
        if allow_heuristic:
            logical, heuristic_matched = _heuristic_segments(clean_text, roster_index)
        else:
            logical = []
            heuristic_matched = 0
        if not logical or heuristic_matched == 0:
            # 没有任何可用对白：整段作为旁白，保留原文换行。
            logical = [(_speaker_of("narrator", "", SOURCE_NARRATOR, roster_index), clean_text.strip())]

    segments: list[dict[str, Any]] = []
    total_chars = 0
    truncated = False
    for speaker, text in logical:
        if not str(text or "").strip():
            continue
        if len(segments) >= max_segments or total_chars >= max_turn_chars:
            truncated = True
            break
        for chunk in _chunk_text(text, max_segment_chars):
            if len(segments) >= max_segments or total_chars >= max_turn_chars:
                truncated = True
                break
            remaining = max_turn_chars - total_chars
            if len(chunk) > remaining:
                chunk = chunk[:remaining].strip()
                truncated = True
            if not chunk:
                continue
            segments.append(
                {
                    "speaker_id": speaker["speaker_id"],
                    "display_name": speaker["display_name"],
                    "kind": speaker["kind"],
                    "text": chunk,
                    "source": speaker["source"],
                }
            )
            total_chars += len(chunk)
            if remaining <= len(chunk):
                truncated = truncated or total_chars >= max_turn_chars
                if total_chars >= max_turn_chars:
                    break

    return ParsedNarration(
        clean_text=clean_text.strip(),
        segments=segments,
        raw_segment_count=len(logical),
        had_markup=had_markup,
        truncated=truncated,
    )
