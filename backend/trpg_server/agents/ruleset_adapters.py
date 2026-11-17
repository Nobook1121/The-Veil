"""Extensible adapters for external tabletop ruleset knowledge."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from trpg_server.scenario_importer import extract_script_text


@dataclass(frozen=True)
class RulesetChunk:
    ruleset_id: str
    source_id: str
    knowledge_version: str
    chunk_id: str
    topic: str
    rule_scope: str
    locale: str
    title: str
    visibility: str
    priority: int
    text: str
    citation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RulesetAdapter(Protocol):
    ruleset_id: str
    display_name: str
    supported_locales: tuple[str, ...]

    def extract(self, raw: bytes, filename: str, ocr_provider: Any = None) -> str: ...
    def chunk(self, text: str, source: dict[str, Any]) -> list[RulesetChunk]: ...
    def classify_query(self, query: str) -> str | None: ...


class COC7Adapter:
    ruleset_id = "coc7"
    display_name = "Call of Cthulhu 7th Edition"
    supported_locales = ("zh-CN", "en-US")
    _topics = {
        "skill": ("skill", "技能", "专长"), "check": ("check", "检定", "成功", "困难"),
        "opposed_check": ("opposed", "对抗"), "combat": ("combat", "战斗"),
        "damage": ("damage", "伤害"), "weapon": ("weapon", "武器"),
        "sanity": ("sanity", "理智", "san"), "madness": ("madness", "疯狂"),
        "chase": ("chase", "追逐"), "investigation": ("investigation", "调查"),
        "character": ("character", "角色", "属性"), "example": ("example", "示例", "例"),
        "faq": ("faq", "常见问题"),
    }

    def extract(self, raw: bytes, filename: str, ocr_provider: Any = None) -> str:
        return extract_script_text(raw, filename, ocr_provider=ocr_provider)

    def classify_query(self, query: str) -> str | None:
        value = str(query or "").casefold()
        for topic, words in self._topics.items():
            if any(word.casefold() in value for word in words):
                return topic
        return None

    def chunk(self, text: str, source: dict[str, Any]) -> list[RulesetChunk]:
        source_id = str(source.get("source_id") or "source")
        version = str(source.get("knowledge_version") or "1")
        locale = str(source.get("locale") or "zh-CN")
        sections: list[tuple[str, list[str]]] = []
        title, body = "Core rules", []
        for line in str(text or "").replace("\r", "").split("\n"):
            match = re.match(r"^\s{0,3}(?:#{1,6}|(?:Chapter|第)\s*[0-9一二三四五六七八九十]+)\s*(.*)$", line, re.I)
            if match and match.group(1).strip():
                if body:
                    sections.append((title, body))
                title, body = match.group(1).strip(), []
            else:
                body.append(line)
        if body:
            sections.append((title, body))
        chunks: list[RulesetChunk] = []
        for index, (heading, lines) in enumerate(sections, 1):
            meaningful = [line.rstrip() for line in lines if line.strip()]
            if not meaningful:
                continue
            # Keep formulas, table rows, exceptions, and examples in one semantic block.
            block = "\n".join(meaningful).strip()
            topic = self.classify_query(heading) or "core_rules"
            if re.search(r"例|example", block, re.I):
                topic = "example" if topic == "core_rules" else topic
            chunks.append(RulesetChunk(
                ruleset_id=self.ruleset_id, source_id=source_id, knowledge_version=version,
                chunk_id=f"{source_id}-{index:04d}", topic=topic, rule_scope="coc7-7e",
                locale=locale, title=heading, visibility="global", priority=10,
                text=block, citation=f"{source_id} / {heading}",
            ))
        return chunks


class Dnd5eAdapter:
    """D&D 5e（SRD 5.1）规则检索适配器。

    5e 没有单独的规则文本库，规则条目直接来自 ``dnd5e-srd`` 内容包：法术、
    怪物、装备、状态、职业与技能条目自带说明文本。返回结构与上传规则书
    检索统一，并附带数据包声明的来源与许可。
    """

    ruleset_id = "dnd5e"
    display_name = "Dungeons & Dragons 5e (SRD 5.1)"
    supported_locales = ("zh-CN", "en-US")
    builtin_types = ("spell", "monster", "equipment", "condition", "class", "skill", "ability")
    _topics = {
        "spell": ("spell", "法术", "戏法", "环阶"),
        "combat": ("combat", "战斗", "攻击", "伤害", "先攻"),
        "check": ("check", "检定", "豁免", "优势", "劣势"),
        "rest": ("rest", "休息", "长休", "短休", "生命骰"),
        "equipment": ("equipment", "装备", "护甲", "武器", "同调"),
        "condition": ("condition", "状态", "中毒", "恐慌", "力竭"),
        "monster": ("monster", "怪物", "生物", "cr"),
    }

    def extract(self, raw: bytes, filename: str, ocr_provider: Any = None) -> str:
        return extract_script_text(raw, filename, ocr_provider=ocr_provider)

    def classify_query(self, query: str) -> str | None:
        value = str(query or "").casefold()
        for topic, words in self._topics.items():
            if any(word.casefold() in value for word in words):
                return topic
        return None

    def chunk(self, text: str, source: dict[str, Any]) -> list[RulesetChunk]:
        source_id = str(source.get("source_id") or "source")
        version = str(source.get("knowledge_version") or "1")
        locale = str(source.get("locale") or "zh-CN")
        sections: list[tuple[str, list[str]]] = []
        title, body = "Core rules", []
        for line in str(text or "").replace("\r", "").split("\n"):
            match = re.match(r"^\s{0,3}(?:#{1,6}|(?:Chapter|第)\s*[0-9一二三四五六七八九十]+)\s*(.*)$", line, re.I)
            if match and match.group(1).strip():
                if body:
                    sections.append((title, body))
                title, body = match.group(1).strip(), []
            else:
                body.append(line)
        if body:
            sections.append((title, body))
        chunks: list[RulesetChunk] = []
        for index, (heading, lines) in enumerate(sections, 1):
            meaningful = [line.rstrip() for line in lines if line.strip()]
            if not meaningful:
                continue
            block = "\n".join(meaningful).strip()
            topic = self.classify_query(heading) or "core_rules"
            chunks.append(
                RulesetChunk(
                    ruleset_id=self.ruleset_id,
                    source_id=source_id,
                    knowledge_version=version,
                    chunk_id=f"{source_id}-{index:04d}",
                    topic=topic,
                    rule_scope="dnd5e-srd",
                    locale=locale,
                    title=heading,
                    visibility="global",
                    priority=10,
                    text=block,
                    citation=f"{source_id} / {heading}",
                )
            )
        return chunks

    def builtin_search(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """按 SRD 内容包检索规则条目；返回统一结构并带来源与许可。"""
        from trpg_server.agents.knowledge_base import lexical_match_score
        from trpg_server.plugins import runtime

        text = str(query or "").strip()
        if not text:
            return []
        library = runtime.content_packs_for(self.ruleset_id)
        topic = self.classify_query(text)
        found: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
        for type_name in self.builtin_types:
            for pack in library.packs_for(self.ruleset_id):
                for entry in pack.entries(type_name):
                    blob = f"{entry.get('id', '')} {entry.get('name', '')} {_entry_text(entry)}"
                    score = lexical_match_score(blob, text)
                    if score <= 0 and topic and type_name in {topic, topic + "s"}:
                        score = 1
                    if score <= 0:
                        continue
                    found.append((score, entry, type_name, pack.meta()))
        found.sort(
            key=lambda item: (
                -item[0],
                item[2],
                str(item[1].get("name") or "").casefold(),
            )
        )
        results: list[dict[str, Any]] = []
        for score, entry, type_name, meta in found[: max(1, min(int(limit), 20))]:
            pack_source = meta.get("source") or {}
            name = str(entry.get("name") or entry.get("id") or "")
            results.append(
                {
                    "ruleset_id": self.ruleset_id,
                    "knowledge_version": meta.get("content_version") or "",
                    "chunk_id": f"{meta.get('pack_id')}:{type_name}:{entry.get('id')}",
                    "topic": topic or type_name,
                    "title": name,
                    "citation": f"{pack_source.get('document') or meta.get('pack_id')} / {name}",
                    "text": _entry_text(entry),
                    "score": score,
                    "source": {
                        "system": pack_source.get("system") or "",
                        "document": pack_source.get("document") or "",
                        "edition": pack_source.get("edition") or "",
                        "attribution": pack_source.get("attribution") or "",
                    },
                    "license": pack_source.get("license") or "",
                    "license_url": pack_source.get("license_url") or "",
                }
            )
        return results


def _entry_text(entry: dict[str, Any]) -> str:
    """把内容条目压成可读规则文本；优先官方说明字段。"""
    for key in ("text", "description", "desc"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1200]
    parts: list[str] = []
    for key in ("traits", "actions", "reactions", "features"):
        values = entry.get(key)
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                body = str(item.get("text") or item.get("description") or "").strip()
                if name or body:
                    parts.append(f"{name}: {body}".strip(": "))
    facts = []
    for key in ("level", "school", "damage", "ac", "hp", "cr", "weight_lb", "category"):
        value = entry.get(key)
        if value not in (None, "", [], {}):
            facts.append(f"{key}={value}")
    if parts:
        return "\n".join(parts)[:1200]
    if facts:
        return f"{entry.get('name', '')}（{'，'.join(facts)}）"
    return str(entry.get("name") or entry.get("id") or "")


class RulesetRegistry:
    def __init__(self, adapters: list[RulesetAdapter] | None = None):
        self._adapters = {
            adapter.ruleset_id: adapter for adapter in (adapters or [COC7Adapter(), Dnd5eAdapter()])
        }

    def get(self, ruleset_id: str) -> RulesetAdapter:
        try:
            return self._adapters[str(ruleset_id)]
        except KeyError as exc:
            raise ValueError(f"Unknown ruleset: {ruleset_id}") from exc

    def list(self) -> list[dict[str, Any]]:
        return [{"ruleset_id": a.ruleset_id, "display_name": a.display_name, "supported_locales": list(a.supported_locales)} for a in self._adapters.values()]
