"""剧本包契约：AI 生成、外部 Skill 生成、站内导入共用同一种可导入格式。

外部工具（Skill）与站内 AI 全链路导入都产出一个 ``scenario-bundle`` JSON；本模块
负责把它归一化成站点可直接落盘的 ``scenario`` + ``narrative``，并保证：

- 节点 id、parent、边的端点全部指向真实节点（AI 常见的「边定义错误」在这里被修正/丢弃）；
- 每个场景/结局模块都有对应的图谱地点节点（AI 漏节点时由程序补齐）；
- 地点的连接、物品/NPC 的所属关系以 ``contains`` / ``inhabits`` 边落盘，供 KP 判断
  「A 地的物品能否拿到 B 地使用」；
- 结构性错误交给 ``repair_bundle`` 修复，剩余问题交给 ``validate_bundle`` 报告。

格式示例见仓库 ``skills/scenario-import/references/bundle-schema.md``。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.model import (
    ASSET_KINDS,
    EDGE_TYPES,
    NODE_KINDS,
    TREE_KINDS,
    NarrativeBundle,
    normalize_visibility,
    slugify_id,
)
from trpg_server.agents.narrative.repair import RepairReport, repair_bundle
from trpg_server.agents.narrative.validate import ValidationReport, validate_bundle

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "the-veil-scenario-bundle"
BUNDLE_VERSION = 1

_TREE = set(TREE_KINDS)
_ASSET = set(ASSET_KINDS)
_MODULE_TYPES = {
    "scene": "scene",
    "location": "scene",
    "room": "scene",
    "area": "scene",
    "ending": "ending",
    "end": "ending",
    "background": "background",
    "lore": "background",
    "setting": "background",
    "npc": "npc",
    "item": "item",
    "clue": "clue",
    "handout": "handout",
    "custom": "custom",
}
_SCENE_LIKE = {"scene", "ending"}
_TIERS = {"core", "background", "archived"}
_KEYWORD_LOGIC = {"and_any", "and_all", "not_any", "not_all"}

_EDGE_ALIASES = {
    "connect": "adjacent",
    "connection": "adjacent",
    "connected": "adjacent",
    "connected_to": "adjacent",
    "adjacent_to": "adjacent",
    "neighbor": "adjacent",
    "neighbour": "adjacent",
    "door": "adjacent",
    "exit": "adjacent",
    "leads": "leads_to",
    "lead_to": "leads_to",
    "one_way": "leads_to",
    "hidden_passage": "portal",
    "teleport": "portal",
    "has": "contains",
    "owns": "contains",
    "owned_by": "contains",
    "belongs_to": "contains",
    "located_in": "contains",
    "located_at": "contains",
    "inside": "contains",
    "part_of": "contains",
    "contains_item": "contains",
    "npc_at": "inhabits",
    "present_at": "inhabits",
    "lives_in": "inhabits",
    "stays_at": "inhabits",
    "knows_about": "knows",
    "aware_of": "knows",
    "use": "uses",
    "used_at": "uses",
    "hint": "reveals",
    "clue_for": "reveals",
    "requires_item": "requires",
    "needs": "requires",
}
# 这些别名表达的是「子 → 父」，归一化时需要把方向掉转成「地点 contains 资产」。
_REVERSED_ALIASES = {"owned_by", "belongs_to", "located_in", "located_at", "inside", "part_of", "contained_by"}


_KIND_ALIASES = {
    "place": "location",
    "room": "location",
    "scene": "location",
    "map": "location",
    "character": "npc",
    "person": "npc",
    "monster": "npc",
    "object": "item",
    "thing": "item",
    "prop": "item",
    "evidence": "clue",
    "handout": "clue",
    "secret": "clue",
    "cluster": "area",
    "chapter": "act",
}


class BundleError(ValueError):
    """剧本包无法归一化（缺字段或格式完全不符）。"""

    def __init__(self, message: str, issues: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.issues = list(issues or [])


@dataclass
class BundleResult:
    """归一化结果：可直接写入磁盘的剧本包与诊断信息。"""

    data: dict[str, Any]
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def payload(self) -> dict[str, Any]:
        result = dict(self.data)
        result["errors"] = list(self.errors)
        result["warnings"] = list(self.warnings)
        return result


def _issue(code: str, message: str, *, level: str = "warn", target: str = "") -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, "target": target}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_title(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _text_list(value: Any, *, limit: int = 40, max_length: int = 60) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)[:max_length]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique_id(base: str, used: set[str], *, prefix: str) -> str:
    candidate = slugify_id(base, prefix=prefix)
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    candidate = f"{candidate}_{index}"
    used.add(candidate)
    return candidate


def _normalize_module(raw: Mapping[str, Any], index: int, used_ids: set[str], errors: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, Any] | None:
    title = _text(raw.get("title") or raw.get("name"))
    content = _text(raw.get("content") or raw.get("text") or raw.get("body"))
    summary = _text(raw.get("summary") or raw.get("description"))
    if not content and not summary:
        warnings.append(_issue("module.empty", f"第 {index} 个模块没有正文或摘要，已忽略。"))
        return None
    if not content:
        content = summary
    module_type = _MODULE_TYPES.get(_text(raw.get("module_type") or raw.get("card_type") or raw.get("type")).lower(), "scene")
    module_id = _unique_id(_text(raw.get("id") or raw.get("scene_id") or title), used_ids, prefix=f"module_{index:03d}")
    visibility = normalize_visibility(raw.get("visibility"))
    keywords = _text_list(raw.get("keywords"))
    secondary = _text_list(raw.get("secondary_keywords"))
    logic = _text(raw.get("secondary_logic")).lower()
    tier = _text(raw.get("tier")).lower()
    if tier not in _TIERS:
        tier = "background"
    return {
        "id": module_id,
        "scene_id": module_id,
        "title": title or f"模块 {index}",
        "module_type": module_type,
        "card_type": module_type,
        "content": content,
        "summary": summary,
        "visibility": visibility,
        "keywords": keywords,
        "secondary_keywords": secondary,
        "secondary_logic": logic if logic in _KEYWORD_LOGIC else "and_any",
        "is_constant": _bool(raw.get("is_constant")),
        "tier": tier,
        "probability": max(0, min(100, _int(raw.get("probability"), 100))),
        "group": _text(raw.get("group"))[:60],
        "trigger_chunks": _bool(raw.get("trigger_chunks")) if raw.get("trigger_chunks") is not None else bool(keywords),
        "spoiler_level": max(0, _int(raw.get("spoiler_level"), 0)),
        "source_ref": dict(raw.get("source_ref")) if isinstance(raw.get("source_ref"), Mapping) else {},
        "metadata": dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else {},
    }


def _normalize_edge_type(raw_type: Any, *, from_id: str, to_id: str, node_kinds: Mapping[str, str]) -> tuple[str, str, str] | None:
    text = _text(raw_type).lower().replace(" ", "_").replace("-", "_")
    original = text
    edge_type = _EDGE_ALIASES.get(text, text)
    if edge_type not in EDGE_TYPES:
        return None
    if original in _REVERSED_ALIASES:
        from_id, to_id = to_id, from_id
    # 只允许资产属于树节点：若方向写反（树节点 → 子节点的 contains 写成子 → 父），自动掉转。
    if edge_type == "contains":
        from_kind = node_kinds.get(from_id, "")
        to_kind = node_kinds.get(to_id, "")
        if from_kind in _ASSET and to_kind in _TREE:
            from_id, to_id = to_id, from_id
    return edge_type, from_id, to_id


def _normalize_exclusions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        prefix = _text(item.get("prefix") or item.get("text") or item.get("excerpt"))
        if not prefix:
            continue
        start = _int(item.get("line_start") or item.get("line"), 0)
        end = _int(item.get("line_end") or item.get("end_line"), start)
        result.append(
            {
                "prefix": prefix[:80],
                "line_start": max(0, start),
                "line_end": max(0, max(end, start)),
                "category": _text(item.get("category") or item.get("type"))[:40],
                "reason": _text(item.get("reason"))[:300],
            }
        )
    return result


def normalize_bundle(
    payload: Mapping[str, Any],
    *,
    fallback_title: str = "",
    default_scenario_id: str = "",
    default_version: str = "1.0.0",
) -> BundleResult:
    """校验并归一化剧本包；结构性错误尽量修复并记录，无法修复的进 ``errors``。"""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        raise BundleError("剧本包必须是 JSON 对象")
    fmt = _text(payload.get("format"))
    if fmt and fmt != BUNDLE_FORMAT:
        raise BundleError(f"不支持的剧本包格式：{fmt}")
    version = _int(payload.get("format_version") or payload.get("version"), BUNDLE_VERSION)
    if version > BUNDLE_VERSION:
        warnings.append(_issue("format.version", f"剧本包版本 {version} 高于当前支持版本 {BUNDLE_VERSION}，按兼容模式读取。"))

    scenario_raw = payload.get("scenario") if isinstance(payload.get("scenario"), Mapping) else {}
    title = _text(scenario_raw.get("title") or payload.get("title")) or _text(fallback_title)
    if not title:
        errors.append(_issue("scenario.title", "缺少剧本标题。", level="error"))

    modules_raw = payload.get("modules") if isinstance(payload.get("modules"), list) else []
    if not modules_raw and isinstance(scenario_raw.get("modules"), list):
        modules_raw = list(scenario_raw["modules"])
    worldbook_raw = payload.get("worldbook") if isinstance(payload.get("worldbook"), list) else []
    modules_raw = list(modules_raw) + [
        {**dict(item), "module_type": item.get("module_type") or "background"}
        for item in worldbook_raw
        if isinstance(item, Mapping)
    ]

    modules: list[dict[str, Any]] = []
    used_module_ids: set[str] = set()
    for index, item in enumerate(modules_raw, 1):
        if not isinstance(item, Mapping):
            continue
        module = _normalize_module(item, index, used_module_ids, errors, warnings)
        if module:
            modules.append(module)
    if not modules:
        errors.append(_issue("modules.empty", "剧本包没有任何可用模块。", level="error"))
    if not any(str(item.get("module_type")) in _SCENE_LIKE for item in modules):
        warnings.append(_issue("modules.scene_missing", "没有场景/结局模块，知识库仍可检索，但无法生成地点节点。"))

    narrative_raw = payload.get("narrative") if isinstance(payload.get("narrative"), Mapping) else {}
    nodes_raw = narrative_raw.get("nodes") if isinstance(narrative_raw.get("nodes"), list) else []
    if not nodes_raw and payload.get("nodes") is not None:
        nodes_raw = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []

    used_node_ids: set[str] = set()
    id_map: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    node_kinds: dict[str, str] = {}
    for index, item in enumerate(nodes_raw, 1):
        if not isinstance(item, Mapping):
            continue
        raw_id = _text(item.get("id") or item.get("node_id"))
        kind = _text(item.get("kind") or item.get("type")).lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in NODE_KINDS:
            warnings.append(_issue("node.kind", f"节点 {raw_id or index} 的类型 {kind or '空'} 无法识别，已丢弃。", target=raw_id))
            continue
        title_text = _text(item.get("title") or item.get("name")) or (raw_id or f"节点 {index}")
        node_id = _unique_id(raw_id or title_text, used_node_ids, prefix=f"node_{index:03d}")
        if raw_id and raw_id not in id_map:
            id_map[raw_id] = node_id
        node_kinds[node_id] = kind
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "title": title_text,
                "parent": _text(item.get("parent")) or None,
                "summary": _text(item.get("summary") or item.get("description")),
                "body": _text(item.get("body") or item.get("content")),
                "visibility": normalize_visibility(item.get("visibility")),
                "keywords": _text_list(item.get("keywords")),
                "aliases": _text_list(item.get("aliases")),
                "scene_role": _text(item.get("scene_role")) or None,
                "access": _text(item.get("access")) or "free",
                "repeatable": _bool(item.get("repeatable"), True),
                "spoiler_level": max(0, _int(item.get("spoiler_level"), 0)),
                "source": _text(item.get("source")) or "ai_import",
            }
        )

    # 场景/结局模块必须在地图上存在；AI 漏节点时按模块补齐，避免「缺少节点」。
    module_node_ids: set[str] = set()
    for module in modules:
        if str(module.get("module_type")) not in _SCENE_LIKE:
            continue
        module_id = str(module["id"])
        wanted_kind = "ending" if module.get("module_type") == "ending" else "location"
        target_id = next((node["id"] for node in nodes if node["id"] == module_id), "")
        if not target_id:
            # AI 有时会给地点起自己的 id（hall/living_room），但标题与模块一致；先按标题认领，
            # 否则同一地点会凭空多出一个重复节点。
            title_key = _normalized_title(str(module.get("title") or ""))
            target_id = next(
                (
                    node["id"]
                    for node in nodes
                    if node["kind"] in _TREE and _normalized_title(str(node.get("title") or "")) == title_key
                ),
                "",
            )
            if target_id:
                node_kinds[target_id] = wanted_kind if wanted_kind == "ending" else node_kinds.get(target_id, wanted_kind)
        if not target_id:
            target_id = _unique_id(module_id, used_node_ids, prefix=f"node_{len(nodes) + 1:03d}")
            node_kinds[target_id] = wanted_kind
            nodes.append(
                {
                    "id": target_id,
                    "kind": wanted_kind,
                    "title": str(module.get("title") or module_id),
                    "parent": None,
                    "summary": str(module.get("summary") or ""),
                    "body": "",
                    "visibility": normalize_visibility(module.get("visibility")),
                    "keywords": list(module.get("keywords") or []),
                    "aliases": [],
                    "scene_role": "ending" if wanted_kind == "ending" else None,
                    "access": "free",
                    "repeatable": True,
                    "spoiler_level": int(module.get("spoiler_level") or 0),
                    "source": "module",
                }
            )
            warnings.append(_issue("node.module_missing", f"模块「{module.get('title')}」缺少对应图谱节点，已补齐。", target=module_id))
        for node in nodes:
            if node["id"] != target_id:
                continue
            node["summary"] = node.get("summary") or str(module.get("summary") or "")
            node["keywords"] = node.get("keywords") or list(module.get("keywords") or [])
            if wanted_kind == "ending":
                node["kind"] = "ending"
                node["scene_role"] = "ending"
            break
        module_node_ids.add(target_id)
        module["scene_id"] = target_id

    # parent 先按 id_map 重写，再校验引用是否真实存在、方向是否合法。
    for node in nodes:
        parent_raw = node.get("parent")
        if parent_raw:
            node["parent"] = id_map.get(parent_raw, parent_raw)
        parent = node.get("parent")
        if parent and (parent not in node_kinds or node_kinds.get(parent) not in _TREE):
            warnings.append(_issue("node.parent_invalid", f"节点 {node['id']} 的 parent={parent} 不是有效地点节点，已清空。", target=node["id"]))
            node["parent"] = None

    edges_raw = narrative_raw.get("edges") if isinstance(narrative_raw.get("edges"), list) else []
    if not edges_raw and payload.get("edges") is not None:
        edges_raw = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    frm_to_parent: dict[str, str] = {}
    for index, item in enumerate(edges_raw, 1):
        if not isinstance(item, Mapping):
            continue
        from_raw = _text(item.get("from") or item.get("from_id") or item.get("source"))
        to_raw = _text(item.get("to") or item.get("to_id") or item.get("target"))
        if not from_raw or not to_raw:
            continue
        from_id = id_map.get(from_raw, from_raw)
        to_id = id_map.get(to_raw, to_raw)
        if from_id not in node_kinds or to_id not in node_kinds:
            warnings.append(_issue("edge.endpoint", f"第 {index} 条边的端点不存在（{from_raw} → {to_raw}），已丢弃。", target=f"{from_raw}->{to_raw}"))
            continue
        if from_id == to_id:
            warnings.append(_issue("edge.self", f"第 {index} 条边指向自身（{from_id}），已丢弃。", target=from_id))
            continue
        normalized = _normalize_edge_type(item.get("type"), from_id=from_id, to_id=to_id, node_kinds=node_kinds)
        if normalized is None:
            warnings.append(_issue("edge.type", f"第 {index} 条边的类型 {_text(item.get('type')) or '空'} 无法识别，已丢弃。", target=f"{from_id}->{to_id}"))
            continue
        edge_type, from_id, to_id = normalized
        if edge_type == "relationship" and (
            node_kinds.get(from_id) != "npc" or node_kinds.get(to_id) != "npc"
        ):
            warnings.append(_issue("edge.relationship", f"人物关系边必须连接两个 NPC（{from_id} → {to_id}），已丢弃。", target=f"{from_id}->{to_id}"))
            continue
        key = (from_id, to_id, edge_type)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        hard_default = edge_type in {"portal", "next", "leads_to"}
        edge = {
            "id": _text(item.get("id")) or f"edge_{from_id}__{to_id}__{edge_type}",
            "from": from_id,
            "to": to_id,
            "type": edge_type,
            "hard": _bool(item.get("hard"), hard_default),
            "condition": dict(item["condition"]) if isinstance(item.get("condition"), Mapping) else None,
            "label": _text(item.get("label"))[:80],
            "visibility": normalize_visibility(item.get("visibility")),
            "evidence": dict(item["evidence"]) if isinstance(item.get("evidence"), Mapping) else None,
            "source": _text(item.get("source")) or "ai_import",
        }
        edges.append(edge)
        if edge_type in {"contains", "inhabits"}:
            # 树节点 → 资产：资产挂到地点下；资产 → 树节点（inhabits 的写法）同样挂载。
            source_kind = node_kinds.get(from_id, "")
            target_kind = node_kinds.get(to_id, "")
            if source_kind in _TREE and target_kind in _ASSET:
                frm_to_parent[to_id] = from_id
            elif edge_type == "inhabits" and source_kind in _ASSET and target_kind in _TREE:
                frm_to_parent[from_id] = to_id

    # 资产 parent 缺省由 contains/inhabits 推导；已有合法 parent 保留。
    node_map = {node["id"]: node for node in nodes}
    for node in nodes:
        if node["kind"] not in _ASSET:
            continue
        if frm_to_parent.get(node["id"]):
            node["parent"] = frm_to_parent[node["id"]]

    # 资产有 parent 却没有 contains 边时补一条，确保「物品属于哪个地点」这个客观事实可被检索。
    for node in nodes:
        if node["kind"] not in _ASSET or not node.get("parent"):
            continue
        if node["id"] in frm_to_parent:
            # 已经有 contains/inhabits 边表达所属关系，无需补重复的 contains。
            continue
        parent_id = node["parent"]
        if (parent_id, node["id"], "contains") in seen_edges:
            continue
        seen_edges.add((parent_id, node["id"], "contains"))
        edges.append(
            {
                "id": f"edge_{parent_id}__{node['id']}__contains",
                "from": parent_id,
                "to": node["id"],
                "type": "contains",
                "hard": False,
                "condition": None,
                "label": "",
                "visibility": "public",
                "evidence": {"source": "parent"},
                "source": "derived",
            }
        )

    if not any(node["kind"] in _TREE for node in nodes):
        errors.append(_issue("graph.no_location", "图谱没有任何地点节点。", level="error"))
    if not any(node["kind"] == "ending" for node in nodes):
        warnings.append(_issue("graph.no_ending", "图谱没有结局节点，KP 可能无法收束剧情。"))

    start_raw = narrative_raw.get("start") if isinstance(narrative_raw.get("start"), Mapping) else {}
    start_id_raw = _text(start_raw.get("location_id") or start_raw.get("start_location") or narrative_raw.get("start_location"))
    start_id = id_map.get(start_id_raw, start_id_raw)
    if start_id and node_kinds.get(start_id) not in _TREE:
        warnings.append(_issue("start.invalid", f"开场地点 {start_id} 不是地点节点，已清除。", target=start_id))
        start_id = ""
    if not start_id:
        entries = [node["id"] for node in nodes if node["kind"] == "location" and not node.get("parent")]
        entries = entries or [node["id"] for node in nodes if node["kind"] in _TREE]
        start_id = entries[0] if entries else ""
    start: dict[str, Any] = {"location_id": start_id} if start_id else {}
    for key in ("opening_mode", "opening_text", "opening_assets"):
        if start_raw.get(key):
            start[key] = start_raw[key]

    topology = _text(narrative_raw.get("topology")).lower()
    if topology not in {"linear", "hub", "sandbox", "hybrid"}:
        topology = "hybrid"

    narrative = {
        "scenario_id": _text(scenario_raw.get("id") or payload.get("scenario_id")) or default_scenario_id,
        "scenario_version": _text(scenario_raw.get("scenario_version") or payload.get("scenario_version")) or default_version,
        "schema_version": 2,
        "topology": topology,
        "nodes": nodes,
        "edges": edges,
        "start": start,
        "warnings": [issue for issue in warnings if issue.get("code", "").startswith(("graph.", "node.", "edge."))],
        "meta": {
            "generator": "scenario_bundle",
            "bundle_format": BUNDLE_FORMAT,
            "bundle_version": version,
        },
    }
    if isinstance(narrative_raw.get("meta"), Mapping):
        narrative["meta"].update(dict(narrative_raw["meta"]))

    scenario = {
        "id": narrative["scenario_id"],
        "scenario_version": narrative["scenario_version"],
        "title": title,
        "author": _text(scenario_raw.get("author") or payload.get("author")) or "Imported",
        "creator_username": _text(scenario_raw.get("creator_username") or scenario_raw.get("creator")),
        "playerCount": max(1, _int(scenario_raw.get("playerCount") or scenario_raw.get("players"), 1)),
        "notes": _text(scenario_raw.get("notes") or scenario_raw.get("description")),
        "global_summary": _text(scenario_raw.get("global_summary") or scenario_raw.get("summary"))[:2000],
        "modules": modules,
        "import_mode": "ai",
    }

    data = {
        "format": BUNDLE_FORMAT,
        "format_version": version,
        "scenario": scenario,
        "modules": modules,
        "narrative": narrative,
        "exclusions": _normalize_exclusions(payload.get("exclusions")),
        "analysis": dict(payload.get("analysis")) if isinstance(payload.get("analysis"), Mapping) else {},
    }
    for item in data["exclusions"]:
        if not item.get("line_start"):
            warnings.append(_issue("exclusion.position", f"剔除项「{item['prefix'][:12]}…」没有行号，仅作说明。", target=item["prefix"]))
    return BundleResult(data, errors=errors, warnings=warnings)


def scenario_from_result(result: BundleResult) -> dict[str, Any]:
    """取出可直接写 ``scenario.json`` 的剧本记录。"""
    return dict(result.data["scenario"])


def build_narrative(result: BundleResult, *, repair: bool = True) -> tuple[NarrativeBundle, RepairReport, ValidationReport]:
    """把归一化后的 narrative 转成 ``NarrativeBundle``，并做结构修复与校验。"""
    data = dict(result.data["narrative"])
    bundle = NarrativeBundle.from_dict(data)
    if repair:
        report = repair_bundle(bundle)
    else:
        report = RepairReport()
    validation = validate_bundle(bundle)
    if report.applied:
        logger.info("scenario_bundle.repaired count=%d", len(report.applied))
    return bundle, report, validation


def bundle_payload_for_export(
    scenario: Mapping[str, Any],
    narrative: NarrativeBundle | Mapping[str, Any] | None = None,
    *,
    exclusions: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """把站内剧本与图谱导出成剧本包，供 Skill/外部编辑后再导入。"""
    narrative_data: dict[str, Any] = {}
    if isinstance(narrative, NarrativeBundle):
        narrative_data = narrative.to_dict()
    elif isinstance(narrative, Mapping):
        narrative_data = dict(narrative)
    return {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_VERSION,
        "scenario": {
            key: scenario.get(key)
            for key in (
                "title",
                "author",
                "creator_username",
                "playerCount",
                "notes",
                "global_summary",
                "scenario_version",
            )
            if scenario.get(key) not in (None, "")
        },
        "modules": list(scenario.get("modules") or []),
        "narrative": narrative_data,
        "exclusions": [dict(item) for item in (exclusions or []) if isinstance(item, Mapping)],
    }


def document_hash(markdown: str) -> str:
    return hashlib.sha256(str(markdown or "").encode("utf-8")).hexdigest()
