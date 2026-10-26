"""`narrative` 控制台命令（设计文档 §11）。

独立于 Flask 与全局控制台状态；scenarios_dir / confirm 由
`console_commands.execute_console_command` 注入，避免循环依赖。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server.agents.narrative.builder import build_from_scenario, rebuild_all, rebuild_from_descriptor
from trpg_server.agents.narrative.model import NarrativeBundle, TOPOLOGIES
from trpg_server.agents.narrative.opening import begin_scenario, plan_scenario
from trpg_server.agents.narrative.store import (
    build_index,
    bundle_summary,
    find_scenario_storage,
    load_bundle,
    scenario_dir_for_descriptor,
)
from trpg_server.agents.narrative.topology import detect_topology
from trpg_server.agents.narrative.validate import simulate_any_order, validate_bundle
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.scenario_store import load_scenario_by_id


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
    payload = report.to_dict()
    payload["simulated"] = [issue.__dict__ for issue in simulated]
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
        return _topology_command(rest, scenarios_dir, flags)
    if subcommand == "build":
        return _build_command(rest, scenarios_dir, flags, confirm)
    if subcommand == "opening":
        return _opening_command(rest, scenarios_dir, rooms_dir, flags, confirm)
    return False, f"未知 narrative 子命令：{subcommand}（可用 status/validate/topology/build/opening）"


__all__ = ["handle_narrative_command"]
