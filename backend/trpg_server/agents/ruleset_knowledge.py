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


def _legacy_ruleset_search(
    room_id: str, query: str, *, store: RulesetKnowledgeStore, ruleset_id: str, top_k: int
) -> list[dict[str, Any]]:
    """既有检索路径（房间索引 + 规则集内置内容包），保持逐位行为不变。"""
    version = None
    if store.rooms_dir:
        info = read_json(store.rooms_dir / str(room_id) / "info.json", default={})
        version = (info.get("rulesets") or {}).get(ruleset_id)
    stored = store.search(ruleset_id, query, version=version, top_k=top_k)
    # 规则集自带检索源（如 5e 的 SRD 内容包）时合并；没有 builtin_search 的
    # 适配器（CoC7）保持原有返回，一条不变。
    try:
        adapter = store.registry.get(ruleset_id)
    except ValueError:
        return stored
    builtin = getattr(adapter, "builtin_search", None)
    if not callable(builtin):
        return stored
    merged = list(builtin(query, limit=top_k)) + list(stored)
    return merged[: max(1, min(int(top_k), 20))]


# 深库服务的约定名（插件注册在 ``get_service(ruleset_id, name)``）。核心只认
# 「名字 + search() 鸭子类型」，不 import 任何插件包；插件缺席/未建库时下面
# 一路探测都会落空，检索退回既有路径，零回归。
DEEP_SEARCH_SERVICES = ("kiwee_db",)
DEEP_TEXT_LIMIT = 1200


def deep_ruleset_search(
    ruleset_id: str, query: str, *, limit: int = 3, observer_is_dm: bool = True
) -> list[dict[str, Any]]:
    """深库优先检索：先问插件的深库服务，服务缺席/无命中 → ``[]``。

    返回统一 chunk dict（与 :meth:`RulesetKnowledgeStore.search` 同构的
    ruleset_id/chunk_id/topic/citation/text/source/license + 深库附加的
    deep_link / rules_version / 机翻标记），失败一律降级为空列表，
    绝不把 KP 回合拖挂。
    """
    text = str(query or "").strip()
    ruleset = str(ruleset_id or "").strip()
    if not text or not ruleset:
        return []
    try:
        from trpg_server.plugins.runtime import get_service
    except Exception:  # noqa: BLE001 - 旧核心没有服务表时按无深库处理
        return []
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 3
    for layer in DEEP_SEARCH_SERVICES:
        service = get_service(ruleset, layer)
        search = getattr(service, "search", None)
        if not callable(search):
            continue
        try:
            rows = search(None, text, observer_is_dm=observer_is_dm, limit=limit, with_text=True)
        except TypeError:
            # 第三方深库服务没有 with_text 扩展：退回基础契约。
            try:
                rows = search(None, text, observer_is_dm=observer_is_dm, limit=limit)
            except Exception:  # noqa: BLE001 - 深库失败不阻断规则检索
                logger.warning("Deep ruleset search failed (layer=%s)", layer, exc_info=True)
                continue
        except Exception:  # noqa: BLE001 - 深库失败不阻断规则检索
            logger.warning("Deep ruleset search failed (layer=%s)", layer, exc_info=True)
            continue
        if not rows:
            continue
        version = ""
        getter = getattr(service, "version", None)
        if callable(getter):
            try:
                version = str(getter() or "")
            except Exception:  # noqa: BLE001 - 版本只是标签，取不到留空
                version = ""
        return [
            _deep_chunk(ruleset, layer, row, version)
            for row in rows
            if isinstance(row, dict) and str(row.get("text") or "").strip()
        ]
    return []


def _deep_chunk(ruleset_id: str, layer: str, row: dict[str, Any], version: str) -> dict[str, Any]:
    """深库结果 → 统一 chunk（下游工具/提示词只认这套字段）。"""
    category = str(row.get("category") or "")
    uid = str(row.get("uid") or "")
    name = str(row.get("name") or "").strip()
    # 服务名带 _db 后缀是注册约定，对外的层名只要 "kiwee" 这种短标签。
    layer_label = layer[: -len("_db")] if layer.endswith("_db") else layer
    eng_name = str(row.get("eng_name") or "").strip()
    page_hint = str(row.get("page_hint") or "").strip()
    machine = bool(row.get("is_machine_translated"))
    return {
        "ruleset_id": ruleset_id,
        "knowledge_version": version,
        "chunk_id": f"{layer}:{category}:{uid}",
        "topic": category,
        "title": f"{name}（{eng_name}）" if eng_name else name,
        "citation": f"{page_hint} / {name}" if page_hint else name,
        "text": str(row.get("text") or "").strip()[:DEEP_TEXT_LIMIT],
        "score": 1.0,
        "source": {
            "system": ruleset_id,
            "document": page_hint or str(row.get("source") or ""),
            "edition": str(row.get("rules_era") or ""),
            "attribution": str(row.get("site") or ""),
        },
        "license": str(row.get("license") or ""),
        "license_url": str(row.get("license_url") or ""),
        "deep_layer": layer_label,
        "deep_link": str(row.get("deep_link") or ""),
        "category": category,
        "uid": uid,
        "name": name,
        "eng_name": eng_name,
        "rules_version": str(row.get("rules_era") or ""),
        "is_machine_translated": machine,
        "translator_ok": bool(row.get("translator_ok")),
        # 直接可用的质量提示：UI 与提示词都不需要再判一次。
        "translator_note": "机翻，未校对" if machine else "",
    }


def search_ruleset(
    room_id: str,
    query: str,
    *,
    store: RulesetKnowledgeStore,
    ruleset_id: str = "",
    top_k: int = 3,
    observer_is_dm: bool = True,
) -> list[dict[str, Any]]:
    """规则检索总入口：**深库优先**，深库缺席/无命中时逐位回落到既有路径。

    ``observer_is_dm=False``（AI 玩家角色侧）时深库不返回 bestiary / 掷表 /
    变体规则等 DM 侧类目；KP 侧保持默认 True。深库不可用时 legacy 路径
    的结果与改动前完全一致。
    """
    if not ruleset_id:
        try:
            from trpg_server.ruleset import default_ruleset_id

            ruleset_id = default_ruleset_id()
        except RuntimeError:
            ruleset_id = ""
    legacy = _legacy_ruleset_search(room_id, query, store=store, ruleset_id=ruleset_id, top_k=top_k)
    deep = deep_ruleset_search(ruleset_id, query, limit=top_k, observer_is_dm=observer_is_dm)
    if not deep:
        return legacy
    seen = {(str(item.get("category") or ""), str(item.get("uid") or "")) for item in deep}
    rest = [
        item
        for item in legacy
        if (str(item.get("category") or ""), str(item.get("uid") or "")) not in seen
    ]
    merged = deep + rest
    try:
        limit = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        limit = 3
    return merged[:limit]
