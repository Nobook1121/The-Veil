#!/usr/bin/env python3
"""校验 / 修复《帷幕》剧本包（.veil-scenario.json），只依赖 Python 标准库。

用法：
    python validate_bundle.py bundle.json
    python validate_bundle.py bundle.json --fix -o fixed.json

退出码：0 无 ERROR；1 存在 ERROR；2 文件/JSON 读取失败。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FORMAT = "the-veil-scenario-bundle"
VERSION = 1

TREE_KINDS = {"act", "area", "location", "ending"}
ASSET_KINDS = {"npc", "item", "clue", "concept", "flag", "clock"}
NODE_KINDS = TREE_KINDS | ASSET_KINDS
EDGE_TYPES = {
    "adjacent", "portal", "next", "leads_to", "requires", "reveals", "knows",
    "inhabits", "uses", "contains", "triggers", "conflicts", "mentions", "alias",
    "relationship",
}
MODULE_TYPES = {"scene", "ending", "background", "npc", "item", "clue", "handout", "custom"}
SCENE_LIKE = {"scene", "ending"}

KIND_ALIASES = {
    "place": "location", "room": "location", "scene": "location", "map": "location",
    "character": "npc", "person": "npc", "monster": "npc",
    "object": "item", "thing": "item", "prop": "item",
    "evidence": "clue", "handout": "clue", "secret": "clue",
    "cluster": "area", "chapter": "act",
}
EDGE_ALIASES = {
    "connect": "adjacent", "connection": "adjacent", "connected": "adjacent",
    "connected_to": "adjacent", "adjacent_to": "adjacent", "neighbor": "adjacent",
    "neighbour": "adjacent", "door": "adjacent", "exit": "adjacent",
    "leads": "leads_to", "lead_to": "leads_to", "one_way": "leads_to",
    "hidden_passage": "portal", "teleport": "portal",
    "has": "contains", "owns": "contains", "contains_item": "contains",
    "located_in": "contains", "located_at": "contains", "inside": "contains",
    "part_of": "contains", "belongs_to": "contains", "contained_by": "contains",
    "npc_at": "inhabits", "present_at": "inhabits", "lives_in": "inhabits", "stays_at": "inhabits",
    "knows_about": "knows", "aware_of": "knows",
    "use": "uses", "used_at": "uses",
    "hint": "reveals", "clue_for": "reveals",
    "requires_item": "requires", "needs": "requires",
    "related": "relationship", "social": "relationship", "relation": "relationship",
}
REVERSED = {"owned_by", "belongs_to", "located_in", "located_at", "inside", "part_of", "contained_by"}

issues: list[dict[str, str]] = []


def issue(level: str, where: str, message: str) -> None:
    issues.append({"level": level, "where": where, "message": message})


def text(value: Any) -> str:
    return str(value or "").strip()


def slug(value: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in text(value))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or fallback


def unique(base: str, used: set[str], fallback: str) -> str:
    candidate = slug(base, fallback)
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    candidate = f"{candidate}_{index}"
    used.add(candidate)
    return candidate


def normalize_modules(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("modules") if isinstance(payload.get("modules"), list) else []
    worldbook = payload.get("worldbook") if isinstance(payload.get("worldbook"), list) else []
    raw = list(raw) + [{**item, "module_type": item.get("module_type") or "background"} for item in worldbook if isinstance(item, dict)]
    seen: set[str] = set()
    modules: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        content = text(item.get("content") or item.get("text") or item.get("body"))
        summary = text(item.get("summary") or item.get("description"))
        if not content and not summary:
            issue("warn", f"modules[{index}]", "没有正文或摘要，已忽略")
            continue
        module_type = text(item.get("module_type") or item.get("card_type") or item.get("type")).lower()
        if module_type not in MODULE_TYPES:
            issue("warn", f"modules[{index}]", f"module_type={module_type or '空'} 无法识别，按 scene 处理")
            module_type = "scene"
        module_id = unique(text(item.get("id") or item.get("scene_id") or item.get("title")), seen, f"module_{index:03d}")
        keywords = [text(word) for word in item.get("keywords") or [] if text(word)]
        if not keywords:
            issue("warn", f"modules[{index}] {module_id}", "没有 keywords，导入后无法按词触发")
        modules.append(
            {
                **item,
                "id": module_id,
                "scene_id": module_id,
                "title": text(item.get("title") or item.get("name")) or f"模块 {index}",
                "module_type": module_type,
                "content": content or summary,
                "summary": summary,
                "keywords": keywords,
            }
        )
    if not modules:
        issue("error", "modules", "剧本包没有任何可用模块")
    return modules


def normalize_narrative(narrative: Any, modules: list[dict[str, Any]]) -> dict[str, Any]:
    source = narrative if isinstance(narrative, dict) else {}
    raw_nodes = source.get("nodes") if isinstance(source.get("nodes"), list) else []
    seen: set[str] = set()
    id_map: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    kinds: dict[str, str] = {}
    for index, item in enumerate(raw_nodes, 1):
        if not isinstance(item, dict):
            continue
        raw_id = text(item.get("id") or item.get("node_id"))
        kind = text(item.get("kind") or item.get("type")).lower()
        kind = KIND_ALIASES.get(kind, kind)
        if kind not in NODE_KINDS:
            issue("warn", f"nodes[{index}]", f"kind={kind or '空'} 无法识别，已丢弃")
            continue
        node_id = unique(raw_id or text(item.get("title") or item.get("name")), seen, f"node_{index:03d}")
        if raw_id and raw_id not in id_map:
            id_map[raw_id] = node_id
        kinds[node_id] = kind
        nodes.append(
            {
                **item,
                "id": node_id,
                "kind": kind,
                "title": text(item.get("title") or item.get("name")) or node_id,
                "parent": text(item.get("parent")) or None,
            }
        )

    # 场景/结局模块必须有节点；AI 常漏掉，按模块标题认领或补齐。
    for module in modules:
        if text(module.get("module_type")) not in SCENE_LIKE:
            continue
        wanted = "ending" if module["module_type"] == "ending" else "location"
        target = next((node["id"] for node in nodes if node["id"] == module["id"]), "")
        if not target:
            key = re.sub(r"\W+", "", text(module.get("title")).lower())
            target = next(
                (node["id"] for node in nodes if node["kind"] in TREE_KINDS and re.sub(r"\W+", "", text(node.get("title")).lower()) == key),
                "",
            )
        if not target:
            target = unique(module["id"], seen, f"node_{len(nodes) + 1:03d}")
            kinds[target] = wanted
            nodes.append(
                {
                    "id": target,
                    "kind": wanted,
                    "title": module.get("title"),
                    "summary": module.get("summary"),
                    "keywords": list(module.get("keywords") or []),
                    "parent": None,
                    **({"scene_role": "ending"} if wanted == "ending" else {}),
                }
            )
            issue("warn", f"module {module['id']}", "缺少对应图谱节点，已自动补齐")
        else:
            for node in nodes:
                if node["id"] == target:
                    node["summary"] = node.get("summary") or module.get("summary") or ""
                    if wanted == "ending":
                        node["kind"] = "ending"
                        node["scene_role"] = "ending"
                    kinds[target] = node["kind"]
                    break
        module["scene_id"] = target

    for node in nodes:
        if node.get("parent"):
            node["parent"] = id_map.get(node["parent"], node["parent"])
        parent = node.get("parent")
        if parent and (parent not in kinds or kinds.get(parent) not in TREE_KINDS):
            issue("warn", f"node {node['id']}", f"parent={parent} 不是有效地点，已清空")
            node["parent"] = None

    raw_edges = source.get("edges") if isinstance(source.get("edges"), list) else []
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    linked_assets: set[str] = set()
    for index, item in enumerate(raw_edges, 1):
        if not isinstance(item, dict):
            continue
        from_raw = text(item.get("from") or item.get("from_id") or item.get("source"))
        to_raw = text(item.get("to") or item.get("to_id") or item.get("target"))
        if not from_raw or not to_raw:
            continue
        src, dst = id_map.get(from_raw, from_raw), id_map.get(to_raw, to_raw)
        if src not in kinds or dst not in kinds:
            issue("warn", f"edges[{index}]", f"端点不存在（{from_raw} → {to_raw}），已丢弃")
            continue
        if src == dst:
            issue("warn", f"edges[{index}]", "边指向自身，已丢弃")
            continue
        original = text(item.get("type")).lower().replace(" ", "_").replace("-", "_")
        edge_type = EDGE_ALIASES.get(original, original)
        if edge_type not in EDGE_TYPES:
            issue("warn", f"edges[{index}]", f"type={original or '空'} 无法识别，已丢弃")
            continue
        if original in REVERSED:
            src, dst = dst, src
        if edge_type == "contains" and kinds.get(src) in ASSET_KINDS and kinds.get(dst) in TREE_KINDS:
            src, dst = dst, src
        if edge_type == "relationship" and (kinds.get(src) != "npc" or kinds.get(dst) != "npc"):
            issue("warn", f"edges[{index}]", "relationship 必须连接两个 NPC，已丢弃")
            continue
        key = (src, dst, edge_type)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({**item, "from": src, "to": dst, "type": edge_type})
        if edge_type == "contains" and kinds.get(src) in TREE_KINDS and kinds.get(dst) in ASSET_KINDS:
            linked_assets.add(dst)
        if edge_type == "inhabits" and kinds.get(src) in ASSET_KINDS and kinds.get(dst) in TREE_KINDS:
            linked_assets.add(src)

    for node in nodes:
        if node["kind"] in ASSET_KINDS and node["id"] in linked_assets:
            for edge in edges:
                if edge["type"] == "contains" and edge["to"] == node["id"]:
                    node["parent"] = edge["from"]
                elif edge["type"] == "inhabits" and edge["from"] == node["id"]:
                    node["parent"] = edge["to"]

    for node in nodes:
        if node["kind"] not in ASSET_KINDS or not node.get("parent"):
            continue
        if node["id"] in linked_assets:
            continue
        key = (node["parent"], node["id"], "contains")
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({"from": node["parent"], "to": node["id"], "type": "contains", "hard": False, "label": "", "source": "derived"})

    if not any(kind in TREE_KINDS for kind in kinds.values()):
        issue("error", "narrative.nodes", "没有任何地点节点")
    if not any(kind == "ending" for kind in kinds.values()):
        issue("warn", "narrative.nodes", "没有结局节点，剧情可能无法收束")

    start_raw = source.get("start") if isinstance(source.get("start"), dict) else {}
    start_id = text(start_raw.get("location_id") or source.get("start_location"))
    start_id = id_map.get(start_id, start_id)
    if start_id and kinds.get(start_id) not in TREE_KINDS:
        issue("warn", "narrative.start", f"start={start_id} 不是地点节点，已重选")
        start_id = ""
    if not start_id:
        entries = [node["id"] for node in nodes if node["kind"] == "location" and not node.get("parent")]
        entries = entries or [node["id"] for node in nodes if node["kind"] in TREE_KINDS]
        start_id = entries[0] if entries else ""
    start = {"location_id": start_id} if start_id else {}

    topology = text(source.get("topology")).lower()
    if topology not in {"linear", "hub", "sandbox", "hybrid"}:
        topology = "hybrid"
    return {
        "scenario_id": text((source.get("scenario_id"))),
        "scenario_version": text(source.get("scenario_version")) or "1.0.0",
        "schema_version": 2,
        "topology": topology,
        "nodes": nodes,
        "edges": edges,
        "start": start,
        "meta": dict(source.get("meta")) if isinstance(source.get("meta"), dict) else {},
    }


def quality_checks(bundle: dict[str, Any]) -> None:
    narrative = bundle.get("narrative") or {}
    nodes = narrative.get("nodes") or []
    edges = narrative.get("edges") or []
    tree_ids = {node["id"] for node in nodes if node["kind"] in TREE_KINDS}
    asset_ids = {node["id"] for node in nodes if node["kind"] in ASSET_KINDS}

    # 地点孤岛：地点之间没有任何 adjacent/portal/leads_to/next。
    connected: set[str] = set()
    for edge in edges:
        if edge.get("type") in {"adjacent", "portal", "leads_to", "next"} and edge.get("from") in tree_ids and edge.get("to") in tree_ids:
            connected.add(edge["from"])
            connected.add(edge["to"])
    isolated = sorted(tree_ids - connected)
    if len(tree_ids) > 1 and isolated:
        issue("warn", "narrative.edges", f"这些地点没有任何地点间连接：{', '.join(isolated[:8])}")

    # 资产没有所属地点：物品在哪个地点这一点缺失后，KP 无法判断能否取用。
    orphans = sorted(asset_id for asset_id in asset_ids if not any(
        (edge.get("type") == "contains" and edge.get("to") == asset_id)
        or (edge.get("type") == "inhabits" and edge.get("from") == asset_id)
        for edge in edges
    ))
    if orphans:
        issue("warn", "narrative.edges", f"这些物品/NPC 没有所属地点或驻留关系：{', '.join(orphans[:8])}")

    scenes = [module for module in bundle.get("modules") or [] if text(module.get("module_type")).lower() in {"scene", "ending"}]
    story_assets = {node["id"] for node in nodes if node["kind"] in {"npc", "item", "clue"}}
    if scenes and not story_assets:
        issue("warn", "narrative.nodes", "整份剧本没有 npc/item/clue 节点，图谱只剩地点；请补出场景里的 NPC、物品与线索。")

    generic = {"调查", "房间", "地点", "玩家", "游戏", "剧本", "故事", "场景", "之后", "然后", "这里"}
    for module in bundle.get("modules") or []:
        words = [text(word) for word in module.get("keywords") or []]
        hits = [word for word in words if word in generic]
        if hits:
            issue("warn", f"module {module.get('id')}", f"关键词含通用词：{', '.join(hits)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 / 修复剧本包")
    parser.add_argument("bundle", help="剧本包 JSON 路径")
    parser.add_argument("--fix", action="store_true", help="写出归一化后的剧本包")
    parser.add_argument("-o", "--output", help="--fix 的输出路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    args = parser.parse_args()

    path = Path(args.bundle)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("剧本包必须是一个 JSON 对象", file=sys.stderr)
        return 2
    bundle = payload.get("bundle") if isinstance(payload.get("bundle"), dict) else payload
    if text(bundle.get("format")) not in {"", FORMAT}:
        issue("warn", "format", f"format={bundle.get('format')} 不是 {FORMAT}")

    scenario = dict(bundle.get("scenario")) if isinstance(bundle.get("scenario"), dict) else {}
    if not text(scenario.get("title")):
        issue("error", "scenario.title", "缺少剧本标题")
    modules = normalize_modules(bundle)
    narrative = normalize_narrative(bundle.get("narrative"), modules)
    result = {
        "format": FORMAT,
        "format_version": VERSION,
        "scenario": {**scenario, "modules": modules},
        "modules": modules,
        "narrative": narrative,
        "exclusions": [item for item in bundle.get("exclusions") or [] if isinstance(item, dict)],
        "analysis": dict(bundle.get("analysis")) if isinstance(bundle.get("analysis"), dict) else {},
    }
    quality_checks(result)

    errors = [item for item in issues if item["level"] == "error"]
    warns = [item for item in issues if item["level"] == "warn"]
    if args.json:
        print(json.dumps({"errors": errors, "warnings": warns}, ensure_ascii=False, indent=2))
    else:
        print(f"模块 {len(modules)}，节点 {len(narrative['nodes'])}，边 {len(narrative['edges'])}")
        for item in errors + warns:
            print(f"[{item['level'].upper():5}] {item['where']}: {item['message']}")
        print(f"ERROR {len(errors)} / WARN {len(warns)}")

    if args.fix:
        output = Path(args.output) if args.output else path.with_name(f"{path.stem}.fixed.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出：{output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
