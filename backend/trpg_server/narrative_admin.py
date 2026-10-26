"""`narrative` 控制台命令（设计文档 §11）。

独立于 Flask 与全局控制台状态；scenarios_dir / confirm 由
`console_commands.execute_console_command` 注入，避免循环依赖。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server.agents.narrative.builder import (
    build_from_scenario,
    rebuild_all,
    rebuild_from_descriptor,
)
from trpg_server.agents.narrative.metrics import COUNTERS, load_metrics
from trpg_server.agents.narrative.model import NarrativeBundle, TOPOLOGIES
from trpg_server.agents.narrative.opening import begin_scenario, plan_scenario
from trpg_server.agents.narrative.runtime_settings import resolve_narrative_settings
from trpg_server.agents.narrative.store import (
    build_index,
    bundle_summary,
    find_scenario_storage,
    invalidate_cache,
    load_bundle,
    save_bundle,
    scenario_dir_for_descriptor,
)
from trpg_server.agents.narrative.topology import detect_topology
from trpg_server.agents.narrative.quality import compute_quality
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle
from trpg_server.agents.narrative.visibility import classify_visibility
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media import image_derivatives, video_derivatives
from trpg_server.scenario_store import load_scenario_by_id, scenario_version_path


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _parse_args(args: list[str]) -> tuple[list[str], set[str]]:
    tokens: list[str] = []
    flags: set[str] = set()
    for arg in args:
        if arg.startswith("--"):
            flags.add(arg[2:].lower())
        elif arg.startswith("-") and len(arg) > 1:
            flags.add(arg[1:].lower())
        else:
            tokens.append(arg)
    return tokens, flags


def _resolve(scenarios_dir: Path | str, scenario_ref: str) -> tuple[Path | None, Path | None]:
    return find_scenario_storage(scenarios_dir, scenario_ref)


def _load_for_ref(scenarios_dir: Path | str, scenario_ref: str) -> tuple[NarrativeBundle | None, Path | None, str]:
    descriptor, storage = _resolve(scenarios_dir, scenario_ref)
    if storage is None:
        return None, None, f"找不到剧本 {scenario_ref}（可用 scenario list 查看）。"
    bundle = load_bundle(storage)
    if bundle is None:
        return None, storage, f"剧本 {scenario_ref} 尚未生成 narrative.json，先运行 narrative build {scenario_ref}。"
    return bundle, storage, ""


def _iter_storage(scenarios_dir: Path | str) -> list[Path]:
    root = Path(scenarios_dir)
    if not root.exists():
        return []
    return sorted({path.parent for path in root.glob("*/scenario.json")} | {path.parent for path in root.glob("*/narrative.json")})


def _status_line(summary: dict[str, Any]) -> str:
    if not summary.get("exists"):
        return "  (未生成 narrative.json)"
    counts = summary.get("counts") or {}
    return (
        f"  拓扑 {summary.get('topology')} "
        f"(置信度 {summary.get('topology_confidence')}, {summary.get('topology_source')}) | "
        f"节点 {counts.get('nodes', 0)} / 边 {counts.get('edges', 0)} | "
        f"入口 {summary.get('start') or '-'} | 哈希 {summary.get('build_hash') or '-'}"
    )


def _status_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    as_json = "json" in flags
    if tokens:
        bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
        if bundle is None:
            return False, error
        summary = bundle_summary(bundle)
        summary["storage"] = str(storage.parent) if storage else None
        if as_json:
            return True, _json(summary)
        lines = [f"剧本 {summary.get('scenario_id') or storage.name} 世界树/图谱状态：", _status_line(summary)]
        if summary.get("unreachable_locations"):
            lines.append(f"  不可达地点: {', '.join(summary['unreachable_locations'])}")
        if summary.get("orphan_assets"):
            lines.append(f"  孤立资产: {', '.join(summary['orphan_assets'])}")
        lines.append(f"  警告: {summary.get('warnings', 0)}")
        return True, "\n".join(lines)

    storages = _iter_storage(scenarios_dir)
    if not storages:
        return True, "没有找到任何剧本目录。"
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for storage in storages:
        bundle = load_bundle(storage)
        summary = bundle_summary(bundle)
        summary["storage"] = str(storage)
        rows.append(summary)
        if not as_json:
            lines.append(f"{storage.name}: {_status_line(summary).strip()}")
    if as_json:
        return True, _json(rows)
    generated = sum(1 for row in rows if row.get("exists"))
    return True, "\n".join(lines + [f"共 {len(rows)} 个剧本，已生成 {generated} 个。" ])


def _format_issue(issue: Any) -> str:
    target = ""
    if issue.node_id:
        target += f" node={issue.node_id}"
    if issue.edge_id:
        target += f" edge={issue.edge_id}"
    line = f"  [{issue.level}] {issue.code}{target}: {issue.message}"
    if issue.suggestion:
        line += f"\n        建议：{issue.suggestion}"
    return line


def _validate_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative validate <剧本id> [--json] [--simulate]"
    bundle, _, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    index = build_index(bundle)
    report = validate_bundle(bundle, index=index)
    simulated = simulate_any_order(bundle) if "simulate" in flags else []
    order_ok = None if "simulate" not in flags else not any(issue.level == "error" for issue in simulated)
    quality = compute_quality(bundle, order_independent=order_ok)
    payload = report.to_dict()
    payload["simulated"] = [issue.__dict__ for issue in simulated]
    payload["quality"] = quality.to_dict()
    if "json" in flags:
        return report.ok and not any(issue.level == "error" for issue in simulated), _json(payload)

    lines = [
        f"校验 {bundle.scenario_id or tokens[0]}："
        f"{'通过' if report.ok else '未通过'}（{report.error_count} error / {report.warning_count} warn）"
    ]
    for issue in report.issues[:50]:
        lines.append(_format_issue(issue))
    if len(report.issues) > 50:
        lines.append(f"  … 其余 {len(report.issues) - 50} 条已省略（--json 查看全部）")
    for issue in simulated[:20]:
        lines.append(_format_issue(issue))
    failed = quality.failed
    lines.append(
        f"  质量指标：{len(quality.metrics) - len(failed)}/{len(quality.metrics)} 达标"
        + (f"；未达标 {'、'.join(item.label for item in failed)}" if failed else "")
    )
    return report.ok and not any(issue.level == "error" for issue in simulated), "\n".join(lines)


def _topology_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative topology <剧本id> [--json]"
    bundle, _, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    explicit = bundle.topology if bundle.topology_source == "author" else None
    decision = detect_topology(bundle, explicit=explicit)
    index = build_index(bundle)
    edge_counts: dict[str, int] = {}
    for edge in bundle.edges:
        edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1
    payload = {
        "scenario_id": bundle.scenario_id,
        "stored": {
            "topology": bundle.topology,
            "confidence": bundle.topology_confidence,
            "source": bundle.topology_source,
        },
        "detected": {
            "topology": decision.topology,
            "confidence": round(decision.confidence, 2),
            "source": decision.source,
            "evidence": decision.evidence,
        },
        "edge_counts": edge_counts,
        "degree": index.degree,
        "reachable": sorted(index.reachable),
        "unreachable_locations": index.unreachable_locations,
    }
    if "json" in flags:
        return decision.topology in TOPOLOGIES, _json(payload)
    lines = [
        f"剧本 {bundle.scenario_id or tokens[0]} 拓扑：{decision.topology} "
        f"(置信度 {decision.confidence:.2f}, {decision.source})",
        f"  已存储: {bundle.topology} / 置信度 {bundle.topology_confidence} / 来源 {bundle.topology_source}",
        "  证据: " + ("; ".join(decision.evidence) or "-"),
        "  边类型: " + (", ".join(f"{key}={value}" for key, value in sorted(edge_counts.items())) or "-"),
        f"  可达地点: {len(index.reachable)} / 不可达: {len(index.unreachable_locations)}",
    ]
    if index.unreachable_locations:
        lines.append("  不可达: " + ", ".join(index.unreachable_locations))
    return True, "\n".join(lines)


def _build_one(scenarios_dir: Path | str, ref: str, *, save: bool) -> tuple[bool, str, dict[str, Any]]:
    descriptor, storage = _resolve(scenarios_dir, ref)
    if storage is None or descriptor is None:
        return False, f"找不到剧本 {ref}。", {}
    result = rebuild_from_descriptor(descriptor, save=save)
    summary = bundle_summary(result.bundle, index=result.index)
    payload = {
        "scenario_id": result.bundle.scenario_id,
        "storage": str(storage),
        "saved": save,
        "summary": summary,
        "errors": result.report.error_count,
        "warnings": result.report.warning_count,
        "issues": [issue.__dict__ for issue in result.report.issues[:50]],
    }
    lines = [
        f"剧本 {result.bundle.scenario_id or ref}：{'已写入' if save else '预演（未写盘）'}",
        _status_line(summary),
        f"  校验: {result.report.error_count} error / {result.report.warning_count} warn",
    ]
    for issue in result.report.issues[:20]:
        lines.append(_format_issue(issue))
    return result.report.error_count == 0, "\n".join(lines), payload


def _build_command(
    args: list[str],
    scenarios_dir: Path | str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    as_json = "json" in flags
    build_all = "all" in flags
    if not tokens and not build_all:
        return False, "用法：narrative build <剧本id>|--all [--yes] [--json]"

    save = "yes" in flags or "y" in flags
    if not save and confirm is not None:
        target = "全部剧本" if build_all else tokens[0]
        save = bool(confirm(f"重新生成 {target} 的 narrative.json？未确认时仅预演。"))

    if build_all:
        rows: list[tuple[Path, Any, str | None]] = rebuild_all(scenarios_dir) if save else []
        if not save:
            # 预演：逐个构建但不落盘。
            rows = []
            for storage in _iter_storage(scenarios_dir):
                descriptor = storage / "scenario.json"
                if not descriptor.exists():
                    rows.append((descriptor, None, "缺少 scenario.json"))
                    continue
                try:
                    rows.append((descriptor, build_from_scenario(
                        read_json(descriptor, default={}),
                        scenario_dir=storage, save=False,
                    ), None))
                except Exception as exc:  # noqa: BLE001
                    rows.append((descriptor, None, str(exc)))
        payload = [
            {
                "descriptor": str(descriptor),
                "ok": error is None,
                "error": error,
                "scenario_id": getattr(result.bundle, "scenario_id", None) if result else None,
                "errors": result.report.error_count if result else None,
                "warnings": result.report.warning_count if result else None,
            }
            for descriptor, result, error in rows
        ]
        if as_json:
            return all(item["ok"] for item in payload), _json(payload)
        lines = [
            f"批量{'重建' if save else '预演'}：{len(payload)} 个剧本"
        ]
        for item in payload:
            if item["ok"]:
                lines.append(
                    f"  {Path(item['descriptor']).parent.name}: ok "
                    f"({item['errors']} error / {item['warnings']} warn)"
                )
            else:
                lines.append(f"  {Path(item['descriptor']).parent.name}: 失败 - {item['error']}")
        return all(item["ok"] for item in payload), "\n".join(lines)

    ok, text, payload = _build_one(scenarios_dir, tokens[0], save=save)
    return ok, _json(payload) if as_json else text


def _room_scenario(rooms_dir: Path | str, room_id: str) -> tuple[Path | None, dict[str, Any]]:
    room_dir = Path(rooms_dir) / room_id
    if not room_dir.exists():
        return None, {}
    info = read_json(room_dir / "info.json", default={})
    return room_dir, info if isinstance(info, dict) else {}


def _opening_command(
    args: list[str],
    scenarios_dir: Path | str,
    rooms_dir: Path | str | None,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative opening <房间id> [--dry-run|--publish|--repair] [--yes] [--json]"
    if rooms_dir is None:
        return False, "控制台未配置房间目录，无法执行 narrative opening。"
    room_id = tokens[0]
    room_dir, info = _room_scenario(rooms_dir, room_id)
    if room_dir is None:
        return False, f"找不到房间 {room_id}。"
    scenario_id = info.get("scenario_id")
    if scenario_id in (None, ""):
        return False, f"房间 {room_id} 没有绑定剧本。"
    descriptor, scenario = load_scenario_by_id(
        Path(scenarios_dir), scenario_id, info.get("scenario_version")
    )
    if not descriptor:
        return False, f"房间绑定的剧本 {scenario_id} 不存在。"
    storage = scenario_dir_for_descriptor(descriptor)
    if not (storage / "narrative.json").exists() and (storage.parent / "narrative.json").exists():
        storage = storage.parent
    bundle = load_bundle(storage)
    if bundle is None:
        return False, "剧本尚未生成 narrative.json，先运行 narrative build。"
    scenario = scenario if isinstance(scenario, dict) else {}
    publish = "publish" in flags or "repair" in flags
    repair = "repair" in flags
    if publish and confirm is not None and "yes" not in flags and "y" not in flags:
        action = "补发" if repair else "重置并发布"
        if not confirm(f"确认对房间 {room_id} {action}开场？"):
            publish = False
    version = str(info.get("scenario_version") or "")
    try:
        plan = plan_scenario(room_dir, bundle, scenario, scenario_version=version)
        if publish and not repair:
            plan = begin_scenario(
                room_dir, bundle, scenario,
                scenario_version=version,
                room_id=room_id,
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"开场生成失败：{exc}"
    messages = plan.messages(force_intro=repair) if publish else []
    if publish and messages:
        messages_path = room_dir / "messages.json"
        existing = read_json(messages_path, default=[])
        existing = existing if isinstance(existing, list) else []
        for item in messages:
            item.setdefault("id", f"opening-{int(time.time() * 1000)}-{len(existing)}")
            item.setdefault("time", time.strftime("%H:%M"))
            item.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            existing.append(item)
        write_json_atomic(messages_path, existing[-200:])
        info["scenario_started_at"] = info.get("scenario_started_at") or time.strftime("%Y-%m-%d %H:%M:%S")
        info["scenario_started_by"] = info.get("scenario_started_by") or "console"
        write_json_atomic(room_dir / "info.json", info)
    payload = plan.to_dict()
    payload["published"] = bool(publish)
    payload["message_count"] = len(messages)
    if "json" in flags:
        return True, _json(payload)
    lines = [
        f"房间 {room_id} 开局预览：{'已发布' if publish else '未写盘'}",
        f"  剧本 {plan.scenario_id} | 起始地点 {plan.location_id or '-'} | 模式 {plan.opening_mode}",
        f"  开场白 {len(plan.pack.get('public_intro') or [])} 段 | 附件 {len(plan.pack.get('attachments') or [])} | "
        f"触发器 {len(plan.triggers_fired)}",
    ]
    if plan.missing_intro:
        lines.append("  警告：剧本缺少玩家可见开场白，已使用最小开场。")
    exits = [
        str(item.get("label") or item.get("title"))
        for item in plan.pack.get("exits") or []
        if isinstance(item, dict) and not item.get("blocked")
    ]
    if exits:
        lines.append("  可见出口：" + "、".join(exits[:12]))
    if plan.warnings:
        lines.append("  警告：" + "；".join(plan.warnings[:10]))
    if publish:
        lines.append(f"  已写入 messages.json {len(messages)} 条。")
    return True, "\n".join(lines)


def _flag_value(args: list[str], name: str) -> str | None:
    """读取 ``--name value`` 形式的参数值（``_parse_args`` 只收集旗标名）。"""
    target = f"--{name}"
    for index, item in enumerate(args):
        if item == target and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(f"{target}="):
            return item.split("=", 1)[1]
    return None


def _rebuild_index_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative rebuild-index <剧本id> [--json]"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    index = build_index(bundle)
    invalidate_cache(storage)
    payload = {"summary": bundle_summary(bundle, index=index), "index": index.to_dict()}
    if "json" in flags:
        return True, _json(payload)
    return True, "\n".join(
        [
            f"剧本 {bundle.scenario_id or tokens[0]} 索引已重建（内存派生，无写盘）：",
            f"  节点 {len(bundle.nodes)} / 边 {len(bundle.edges)} / 邻接 {len(index.adjacency)}",
            f"  可达 {len(index.reachable)} / 不可达 {len(index.unreachable_locations)} / 孤立资产 {len(index.orphan_assets)}",
            "  提示：若正在运行房间，下一次请求会自动加载新索引。",
        ]
    )


def _bundle_for_version(
    scenarios_dir: Path | str, scenario_ref: str, version: str
) -> tuple[NarrativeBundle | None, str]:
    descriptor, scenario = load_scenario_by_id(Path(scenarios_dir), scenario_ref, version)
    if not descriptor:
        return None, f"剧本 {scenario_ref} 不存在版本 {version}。"
    if descriptor.parent.name != "versions":
        return None, f"剧本 {scenario_ref} 不存在版本 {version}。"
    storage = descriptor.parent.parent
    try:
        result = build_from_scenario(scenario, scenario_dir=storage, save=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"版本 {version} 构建失败：{exc}"
    return result.bundle, ""


def _diff_map(bundle: NarrativeBundle) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = {node.id: {"kind": node.kind, "title": node.title, "visibility": node.visibility} for node in bundle.nodes}
    edges = {
        edge.id or f"{edge.from_id}->{edge.to_id}:{edge.type}": {
            "from": edge.from_id,
            "to": edge.to_id,
            "type": edge.type,
            "condition": edge.condition,
        }
        for edge in bundle.edges
    }
    return nodes, edges


def _diff_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if len(tokens) < 3:
        return False, "用法：narrative diff <剧本id> <起始版本> <目标版本> [--json]"
    ref, from_version, to_version = tokens[0], tokens[1], tokens[2]
    old, error = _bundle_for_version(scenarios_dir, ref, from_version)
    if old is None:
        return False, error
    new, error = _bundle_for_version(scenarios_dir, ref, to_version)
    if new is None:
        return False, error
    old_nodes, old_edges = _diff_map(old)
    new_nodes, new_edges = _diff_map(new)
    payload = {
        "scenario_id": new.scenario_id or ref,
        "from_version": from_version,
        "to_version": to_version,
        "nodes": {
            "added": sorted(set(new_nodes) - set(old_nodes)),
            "removed": sorted(set(old_nodes) - set(new_nodes)),
            "changed": sorted(
                key for key in set(old_nodes) & set(new_nodes) if old_nodes[key] != new_nodes[key]
            ),
        },
        "edges": {
            "added": sorted(set(new_edges) - set(old_edges)),
            "removed": sorted(set(old_edges) - set(new_edges)),
            "changed": sorted(
                key for key in set(old_edges) & set(new_edges) if old_edges[key] != new_edges[key]
            ),
        },
    }
    if "json" in flags:
        return True, _json(payload)
    lines = [
        f"剧本 {payload['scenario_id']} 版本差异 {from_version} → {to_version}：",
        f"  节点 +{len(payload['nodes']['added'])} / -{len(payload['nodes']['removed'])} / ~{len(payload['nodes']['changed'])}",
        f"  边   +{len(payload['edges']['added'])} / -{len(payload['edges']['removed'])} / ~{len(payload['edges']['changed'])}",
    ]
    for label, values in (
        ("新增节点", payload["nodes"]["added"]),
        ("移除节点", payload["nodes"]["removed"]),
        ("变更节点", payload["nodes"]["changed"]),
        ("新增边", payload["edges"]["added"]),
        ("移除边", payload["edges"]["removed"]),
        ("变更边", payload["edges"]["changed"]),
    ):
        if values:
            lines.append(f"  {label}: " + "、".join(values[:20]) + (" …" if len(values) > 20 else ""))
    return True, "\n".join(lines)


def _backfill_command(
    scenarios_dir: Path | str, flags: set[str], confirm: Callable[[str], bool] | None
) -> tuple[bool, str]:
    save = "yes" in flags or "y" in flags
    if not save and confirm is not None:
        save = bool(confirm("为缺少 narrative.json 的存量剧本生成只读 bundle？未确认时仅预演。"))
    rows: list[dict[str, Any]] = []
    for storage in _iter_storage(scenarios_dir):
        if (storage / "narrative.json").exists():
            continue
        descriptor = storage / "scenario.json"
        if not descriptor.exists():
            continue
        try:
            result = rebuild_from_descriptor(descriptor, save=save)
            rows.append(
                {
                    "scenario": storage.name,
                    "ok": True,
                    "saved": save,
                    "nodes": len(result.bundle.nodes),
                    "edges": len(result.bundle.edges),
                    "errors": result.report.error_count,
                    "warnings": result.report.warning_count,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"scenario": storage.name, "ok": False, "error": str(exc)})
    if "json" in flags:
        return all(row["ok"] for row in rows), _json(rows)
    if not rows:
        return True, "所有存量剧本都已有 narrative.json，无需回填。"
    lines = [f"回填{'已写入' if save else '预演（未写盘）'}：{len(rows)} 个剧本"]
    for row in rows:
        if row["ok"]:
            lines.append(
                f"  {row['scenario']}: ok（{row['nodes']} 节点 / {row['edges']} 边, "
                f"{row['errors']} error / {row['warnings']} warn）"
            )
        else:
            lines.append(f"  {row['scenario']}: 失败 - {row['error']}")
    return all(row["ok"] for row in rows), "\n".join(lines)


def _local_asset_file(storage: Path, digest: str) -> Path | None:
    if not digest:
        return None
    assets_dir = storage / "assets"
    if not assets_dir.is_dir():
        return None
    for pattern in (f"{digest[:16]}.*", f"{digest}.*"):
        matches = [path for path in assets_dir.glob(pattern) if path.is_file()]
        if matches:
            return sorted(matches)[0]
    return None


def _asset_rows(bundle: NarrativeBundle, storage: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset in bundle.assets:
        if not isinstance(asset, dict):
            continue
        digest = str(asset.get("sha256") or "")
        kind = str(asset.get("kind") or "")
        mime = str(asset.get("mime") or "")
        remote = str(asset.get("url") or "").startswith(("http://", "https://"))
        issues: list[str] = []
        if kind not in {"image", "video", "audio", "richtext", "file"}:
            issues.append("kind 非法")
        if "/" not in mime:
            issues.append("mime 缺失")
        if not remote and not digest:
            issues.append("缺少 sha256")
        if kind == "image" and not asset.get("thumb_url"):
            issues.append("缺少缩略图")
        if kind == "video" and not asset.get("poster_url"):
            issues.append("缺少 poster")
        local = _local_asset_file(storage, digest)
        if digest and not remote and local is None:
            issues.append("本地文件缺失")
        rows.append(
            {
                "id": asset.get("id"),
                "kind": kind,
                "alt": asset.get("alt") or asset.get("name") or "",
                "url": asset.get("url") or "",
                "sha256": digest,
                "remote": remote,
                "local": str(local) if local else None,
                "thumb": bool(asset.get("thumb_url")),
                "poster": bool(asset.get("poster_url")),
                "issues": issues,
            }
        )
    return rows


def _media_command(args: list[str], scenarios_dir: Path | str, flags: set[str]) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative media <剧本id> [--missing] [--json]"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    rows = _asset_rows(bundle, storage) if storage else []
    if "missing" in flags:
        rows = [row for row in rows if row["issues"]]
    if "json" in flags:
        return True, _json(rows)
    if not rows:
        return True, "没有缺失/待补素材。" if "missing" in flags else "该剧本没有登记媒体资产。"
    lines = [f"剧本 {bundle.scenario_id or tokens[0]} 媒体资产 {len(rows)} 项："]
    for row in rows[:80]:
        lines.append(
            f"  {row['id']} [{row['kind']}] {row['alt'] or '-'} | "
            f"{'缺失：' + '、'.join(row['issues']) if row['issues'] else '正常'}"
        )
    if len(rows) > 80:
        lines.append(f"  … 其余 {len(rows) - 80} 项已省略（--json 查看全部）")
    return True, "\n".join(lines)


def _fix_asset_derivatives(asset: dict[str, Any], storage: Path) -> bool:
    local = _local_asset_file(storage, str(asset.get("sha256") or ""))
    if local is None:
        return False
    kind = str(asset.get("kind") or "")
    assets_dir = local.parent
    stem = local.stem
    if kind == "image":
        result = image_derivatives(local, assets_dir, stem)
    elif kind == "video":
        result = video_derivatives(local, assets_dir, stem)
    else:
        return False
    return bool(result.get("thumb_path") or result.get("poster_path"))


def _media_verify_command(
    args: list[str],
    scenarios_dir: Path | str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative media verify <剧本id> [--fix-thumbs] [--yes] [--json]"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    fix = "fix-thumbs" in flags
    if fix and storage is not None and confirm is not None and "yes" not in flags and "y" not in flags:
        fix = bool(confirm("补生成缺失的缩略图 / poster？"))
    rows = _asset_rows(bundle, storage) if storage else []
    fixed = 0
    if fix and storage is not None:
        for asset in bundle.assets:
            if isinstance(asset, dict) and _fix_asset_derivatives(asset, storage):
                fixed += 1
    broken = [row for row in rows if row["issues"]]
    payload = {"assets": rows, "broken": len(broken), "fixed": fixed, "fixed_applied": fix}
    if "json" in flags:
        return len(broken) == 0, _json(payload)
    lines = [
        f"媒体校验 {bundle.scenario_id or tokens[0]}：{len(rows)} 项，异常 {len(broken)} 项。"
    ]
    for row in broken[:40]:
        lines.append(f"  {row['id']}：{'、'.join(row['issues'])}")
    if fix:
        lines.append(f"  已补生成派生文件 {fixed} 项。")
    return len(broken) == 0, "\n".join(lines)


def _visibility_row(kind: str, item: Any, *, is_edge: bool = False) -> dict[str, Any]:
    explicit = str(item.extra.get("visibility_source") or "") == "author"
    decision = classify_visibility(
        kind=kind,
        title=item.label if is_edge else item.title,
        summary="",
        body=str(item.kp_notes or ""),
        kp_notes=str(item.kp_notes or ""),
        keywords=list(item.extra.get("keywords") or []) if isinstance(item.extra, dict) else [],
        reveal_when=item.reveal_when,
        explicit=item.visibility if explicit else None,
    )
    payload = decision.to_dict()
    payload.update(
        {
            "id": item.id or f"{item.from_id}->{item.to_id}",
            "kind": "edge" if is_edge else item.kind,
            "title": item.label if is_edge else item.title,
            "current": item.visibility,
        }
    )
    return payload


def _visibility_command(
    args: list[str],
    scenarios_dir: Path | str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    if not tokens:
        return False, "用法：narrative visibility <剧本id> [--auto] [--review] [--json] [--yes]"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    rows = [_visibility_row(node.kind, node) for node in bundle.nodes]
    rows += [_visibility_row("edge", edge, is_edge=True) for edge in bundle.edges]
    if "review" in flags:
        rows = [row for row in rows if row.get("needs_review")]
    changed = 0
    if "auto" in flags:
        if confirm is not None and "yes" not in flags and "y" not in flags:
            if not confirm("按自动判定写入可见性（只改自动项，不动作者显式设置）？"):
                return True, "已取消，未写入。"
        for node in bundle.nodes:
            row = _visibility_row(node.kind, node)
            if row["source"] != "auto" or row["needs_review"] or row["visibility"] == node.visibility:
                continue
            node.visibility = row["visibility"]
            node.extra["visibility_source"] = "auto"
            node.extra["visibility_reasons"] = row["visibility_reasons"]
            changed += 1
        for edge in bundle.edges:
            row = _visibility_row("edge", edge, is_edge=True)
            if row["source"] != "auto" or row["needs_review"] or row["visibility"] == edge.visibility:
                continue
            edge.visibility = row["visibility"]
            edge.extra["visibility_source"] = "auto"
            edge.extra["visibility_reasons"] = row["visibility_reasons"]
            changed += 1
        if changed and storage is not None:
            save_bundle(storage, bundle)
    payload = {"scenario_id": bundle.scenario_id or tokens[0], "rows": rows, "changed": changed}
    if "json" in flags:
        return True, _json(payload)
    lines = [f"可见性判定 {bundle.scenario_id or tokens[0]}：{len(rows)} 项，需要复核 "
             f"{sum(1 for row in rows if row.get('needs_review'))} 项，本次改写 {changed} 项。"]
    for row in rows[:60]:
        flag = " [复核]" if row.get("needs_review") else ""
        lines.append(
            f"  {row['id']} {row['kind']}：{row['current']} → {row['visibility']} "
            f"({row['visibility_confidence']}, {row['source']}){flag}"
        )
    if len(rows) > 60:
        lines.append(f"  … 其余 {len(rows) - 60} 项已省略（--json 查看全部）")
    return True, "\n".join(lines)


def _room_metrics_rows(rooms_dir: Path | str | None) -> list[dict[str, Any]]:
    """§10.6 / §12.6：按房间运行档位聚合 `metrics.json`，用于灰度对比。"""
    if not rooms_dir:
        return []
    root = Path(rooms_dir)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for room_dir in sorted(root.iterdir()):
        if not room_dir.is_dir():
            continue
        state = read_json(room_dir / "state.json", default={})
        if not isinstance(state, dict) or not state:
            continue
        rules = state.get("house_rules") if isinstance(state.get("house_rules"), dict) else {}
        settings = resolve_narrative_settings({"house_rules": rules})
        metrics = load_metrics(room_dir)
        rows.append(
            {
                "room": room_dir.name,
                "profile": settings.profile,
                "guard": settings.guard,
                "source": settings.source,
                "metrics": {name: metrics.get(name, 0) for name in COUNTERS},
                "avg_latency_ms": metrics.get("avg_latency_ms", 0),
            }
        )
    return rows


def _stats_command(
    scenarios_dir: Path | str,
    flags: set[str],
    rooms_dir: Path | str | None = None,
) -> tuple[bool, str]:
    rows: list[dict[str, Any]] = []
    total_nodes = total_edges = total_warnings = total_errors = 0
    topology_counts: dict[str, int] = {}
    for storage in _iter_storage(scenarios_dir):
        bundle = load_bundle(storage)
        if bundle is None:
            rows.append({"scenario": storage.name, "exists": False})
            continue
        index = build_index(bundle)
        report = validate_bundle(bundle, index=index)
        total_nodes += len(bundle.nodes)
        total_edges += len(bundle.edges)
        total_warnings += len(bundle.warnings)
        total_errors += report.error_count
        topology_counts[bundle.topology] = topology_counts.get(bundle.topology, 0) + 1
        rows.append(
            {
                "scenario": storage.name,
                "exists": True,
                "topology": bundle.topology,
                "nodes": len(bundle.nodes),
                "edges": len(bundle.edges),
                "warnings": len(bundle.warnings),
                "errors": report.error_count,
                "unreachable": index.unreachable_locations,
                "generated_at": bundle.generated_at,
            }
        )
    room_rows = _room_metrics_rows(rooms_dir)
    by_profile: dict[str, dict[str, Any]] = {}
    for row in room_rows:
        bucket = by_profile.setdefault(
            row["profile"],
            {"profile": row["profile"], "rooms": 0, **{name: 0 for name in COUNTERS}},
        )
        bucket["rooms"] += 1
        for name in COUNTERS:
            bucket[name] += int(row["metrics"].get(name, 0) or 0)
    payload = {
        "scenarios": rows,
        "totals": {
            "scenarios": len(rows),
            "built": sum(1 for row in rows if row.get("exists")),
            "nodes": total_nodes,
            "edges": total_edges,
            "warnings": total_warnings,
            "errors": total_errors,
            "topology": topology_counts,
        },
        "rooms": room_rows,
        "by_profile": sorted(by_profile.values(), key=lambda item: item["profile"]),
    }
    if "json" in flags:
        return True, _json(payload)
    totals = payload["totals"]
    lines = [
        f"世界树总览：{totals['built']}/{totals['scenarios']} 已生成，"
        f"{totals['nodes']} 节点 / {totals['edges']} 边，{totals['errors']} error / {totals['warnings']} warn",
        "  拓扑分布：" + ("、".join(f"{key}={value}" for key, value in sorted(topology_counts.items())) or "-"),
    ]
    if payload["by_profile"]:
        lines.append("  房间指标（按档位）：")
        for item in payload["by_profile"]:
            lines.append(
                f"    profile={item['profile']} rooms={item['rooms']} "
                f"turns={item['turn_count']} guard_denied={item['guard_denied']} "
                f"trigger_fired={item['triggers_fired']} opening_fallback={item['opening_fallbacks']}"
            )
    missing = [row["scenario"] for row in rows if not row.get("exists")]
    if missing:
        lines.append("  未生成：" + "、".join(missing))
    return True, "\n".join(lines)


def _topology_set_command(
    args: list[str],
    scenarios_dir: Path | str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    value = _flag_value(args, "set")
    if not tokens or not value:
        return False, f"用法：narrative topology <剧本id> --set <{'|'.join(TOPOLOGIES)}> [--yes]"
    if value not in TOPOLOGIES:
        return False, f"未知拓扑：{value}（可用 {'/'.join(TOPOLOGIES)}）"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    previous = bundle.topology
    if confirm is not None and "yes" not in flags and "y" not in flags:
        if not confirm(f"把 {bundle.scenario_id or tokens[0]} 的拓扑从 {previous} 改为 {value}？"):
            return True, "已取消，未写入。"
    bundle.topology = value
    bundle.topology_source = "author"
    if storage is not None:
        save_bundle(storage, bundle)
    return True, f"拓扑已更新：{previous} → {value}（source=author，已写盘）。"


def _opening_set_command(
    args: list[str],
    scenarios_dir: Path | str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    tokens, _ = _parse_args(args)
    location = _flag_value(args, "location")
    mode = _flag_value(args, "mode") or "fixed"
    if not tokens or not location:
        return False, "用法：narrative opening set <剧本id> --location <id> [--mode fixed|model|hybrid] [--yes]"
    if mode not in {"fixed", "model", "hybrid"}:
        return False, f"未知开场模式：{mode}"
    bundle, storage, error = _load_for_ref(scenarios_dir, tokens[0])
    if bundle is None:
        return False, error
    if bundle.node(location) is None:
        return False, f"剧本中没有地点 {location}。"
    if confirm is not None and "yes" not in flags and "y" not in flags:
        if not confirm(f"把 {bundle.scenario_id or tokens[0]} 的起始地点设为 {location}（{mode}）？"):
            return True, "已取消，未写入。"
    bundle.start = {"location_id": location, "opening_mode": mode}
    if storage is not None:
        save_bundle(storage, bundle)
    return True, f"起始地点已设为 {location}（opening_mode={mode}，已写盘）。"


def handle_narrative_command(
    args: list[str],
    scenarios_dir: Path | str,
    *,
    rooms_dir: Path | str | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> tuple[bool, str]:
    """分发 `narrative <subcommand>`；返回 (ok, 文本)。"""
    if not args:
        return False, "用法：narrative status|validate|topology|build|opening ..."
    subcommand = str(args[0]).strip().lower()
    rest = args[1:]
    tokens, flags = _parse_args(rest)

    if subcommand == "status":
        return _status_command(rest, scenarios_dir, flags)
    if subcommand == "validate":
        return _validate_command(rest, scenarios_dir, flags)
    if subcommand == "topology":
        if _flag_value(rest, "set") is not None:
            return _topology_set_command(rest, scenarios_dir, flags, confirm)
        return _topology_command(rest, scenarios_dir, flags)
    if subcommand == "build":
        return _build_command(rest, scenarios_dir, flags, confirm)
    if subcommand == "opening":
        if rest and str(rest[0]).strip().lower() == "set":
            return _opening_set_command(rest[1:], scenarios_dir, flags, confirm)
        return _opening_command(rest, scenarios_dir, rooms_dir, flags, confirm)
    if subcommand == "rebuild-index":
        return _rebuild_index_command(rest, scenarios_dir, flags)
    if subcommand == "diff":
        return _diff_command(rest, scenarios_dir, flags)
    if subcommand == "backfill":
        return _backfill_command(scenarios_dir, flags, confirm)
    if subcommand == "media":
        if tokens and tokens[0].strip().lower() == "verify":
            return _media_verify_command(rest[1:], scenarios_dir, flags, confirm)
        return _media_command(rest, scenarios_dir, flags)
    if subcommand == "visibility":
        return _visibility_command(rest, scenarios_dir, flags, confirm)
    if subcommand == "stats":
        return _stats_command(scenarios_dir, flags, rooms_dir)
    return False, (
        f"未知 narrative 子命令：{subcommand}（可用 status/validate/topology/build/opening/"
        "rebuild-index/diff/backfill/media/visibility/stats）"
    )


__all__ = ["handle_narrative_command"]
