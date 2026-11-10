"""Versioned storage and deterministic retrieval for external rulesets."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from trpg_server.agents.knowledge_base import lexical_match_score
from trpg_server.agents.ruleset_adapters import RulesetChunk, RulesetRegistry
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.scenario_importer import extract_script_text


class RulesetKnowledgeStore:
    def __init__(self, root: Path, registry: RulesetRegistry | None = None, rooms_dir: Path | None = None, ocr_provider: Any = None):
        self.root = Path(root)
        self.registry = registry or RulesetRegistry()
        self.rooms_dir = Path(rooms_dir) if rooms_dir else None
        self.ocr_provider = ocr_provider
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "rulesets.json"

    def _registry(self) -> dict[str, Any]:
        data = read_json(self.registry_path, default={})
        return data if isinstance(data, dict) else {}

    def _save_registry(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.registry_path, data)

    def _meta(self, ruleset_id: str) -> dict[str, Any]:
        data = self._registry(); meta = data.get(str(ruleset_id))
        if not isinstance(meta, dict):
            meta = {"ruleset_id": str(ruleset_id), "active_version": None, "enabled": True, "versions": [], "sources": []}
            data[str(ruleset_id)] = meta; self._save_registry(data)
        return meta

    def _base(self, ruleset_id: str) -> Path:
        base = self.root / str(ruleset_id)
        for name in ("sources", "versions", "indexes"):
            (base / name).mkdir(parents=True, exist_ok=True)
        return base

    def list_rulesets(self) -> list[dict[str, Any]]:
        data = self._registry(); result = []
        for item in self.registry.list():
            meta = data.get(item["ruleset_id"], {})
            result.append({**item, **{key: meta.get(key) for key in ("active_version", "enabled", "versions", "sources")}})
        return result

    def upload_source(self, ruleset_id: str, filename: str, raw: bytes, locale: str = "zh-CN") -> dict[str, Any]:
        adapter = self.registry.get(ruleset_id); source_id = hashlib.sha256(raw).hexdigest()[:16]
        safe_name = Path(filename).name or "source.txt"; base = self._base(ruleset_id)
        source_dir = base / "sources" / source_id; source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / safe_name).write_bytes(raw)
        source = {"source_id": source_id, "filename": safe_name, "locale": locale, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "uploaded_at": time.time()}
        write_json_atomic(source_dir / "source.json", source)
        meta = self._meta(ruleset_id); meta.setdefault("sources", [])
        meta["sources"] = [item for item in meta["sources"] if item.get("source_id") != source_id] + [source]
        data = self._registry(); data[str(ruleset_id)] = meta; self._save_registry(data)
        return source

    def reindex(self, ruleset_id: str) -> dict[str, Any]:
        adapter = self.registry.get(ruleset_id); meta = self._meta(ruleset_id)
        next_version = str(max([int(v) for v in meta.get("versions", []) if str(v).isdigit()] or [0]) + 1)
        chunks: list[RulesetChunk] = []
        for source in meta.get("sources", []):
            source_dir = self._base(ruleset_id) / "sources" / str(source["source_id"])
            files = [path for path in source_dir.iterdir() if path.name != "source.json"] if source_dir.exists() else []
            if not files: continue
            try:
                text = adapter.extract(files[0].read_bytes(), files[0].name, self.ocr_provider)
            except TypeError:
                # Preserve compatibility with third-party adapters implementing
                # the original two-argument extraction contract.
                text = adapter.extract(files[0].read_bytes(), files[0].name)
            chunks.extend(adapter.chunk(text, {**source, "knowledge_version": next_version}))
        index = [chunk.to_dict() for chunk in chunks]
        base = self._base(ruleset_id); write_json_atomic(base / "indexes" / f"{next_version}.json", index)
        write_json_atomic(base / "versions" / f"{next_version}.json", {"knowledge_version": next_version, "chunk_count": len(index), "created_at": time.time()})
        meta["versions"] = [*meta.get("versions", []), next_version]; meta["active_version"] = next_version
        data = self._registry(); data[str(ruleset_id)] = meta; self._save_registry(data)
        return {"ruleset_id": ruleset_id, "knowledge_version": next_version, "chunk_count": len(index), "active_version": next_version}

    def search(self, ruleset_id: str, query: str, version: str | None = None, top_k: int = 3, topic: str | None = None) -> list[dict[str, Any]]:
        meta = self._meta(ruleset_id); selected = str(version or meta.get("active_version") or "")
        if not selected or meta.get("enabled") is False: return []
        values = read_json(self._base(ruleset_id) / "indexes" / f"{selected}.json", default=[])
        result = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict) or (topic and item.get("topic") != topic):
                continue
            score = lexical_match_score(f"{item.get('title','')} {item.get('text','')} {item.get('topic','')}", query)
            if score:
                result.append((score, item))
        result.sort(key=lambda pair: (-pair[0], -int(pair[1].get("priority", 0)), pair[1].get("chunk_id", "")))
        return [{**item, "score": score} for score, item in result[:max(1, min(int(top_k), 20))]]

    def bind_room(self, room_id: str, ruleset_id: str, version: str | None = None) -> dict[str, Any]:
        if not self.rooms_dir: raise ValueError("rooms directory is not configured")
        meta = self._meta(ruleset_id); selected = str(version or meta.get("active_version") or "")
        if not selected or selected not in {str(v) for v in meta.get("versions", [])}: raise ValueError("Unknown knowledge version")
        room = self.rooms_dir / str(room_id); room.mkdir(parents=True, exist_ok=True); path = room / "info.json"; info = read_json(path, default={})
        bindings = info.get("rulesets", {}) if isinstance(info.get("rulesets"), dict) else {}; bindings[str(ruleset_id)] = selected; info["rulesets"] = bindings
        from trpg_server import room_rules
        room_rules.save_room_info(room, info)
        return bindings

    def archive(self, ruleset_id: str) -> dict[str, Any]:
        meta = self._meta(ruleset_id); meta["enabled"] = False; data = self._registry(); data[str(ruleset_id)] = meta; self._save_registry(data); return meta


def search_ruleset(room_id: str, query: str, *, store: RulesetKnowledgeStore, ruleset_id: str = "", top_k: int = 3) -> list[dict[str, Any]]:
    if not ruleset_id:
        try:
            from trpg_server.ruleset import default_ruleset_id

            ruleset_id = default_ruleset_id()
        except RuntimeError:
            ruleset_id = ""
    version = None
    if store.rooms_dir:
        info = read_json(store.rooms_dir / str(room_id) / "info.json", default={})
        version = (info.get("rulesets") or {}).get(ruleset_id)
    return store.search(ruleset_id, query, version=version, top_k=top_k)
