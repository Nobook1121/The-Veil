"""房间控制台的 `narrative` 命令实现（设计文档 §11.2）。

只操作当前房间：读取房间绑定的 `narrative.json` 运行态、账本、事实与轨迹，
破坏性命令默认预演，需 `--yes` 或交互确认。模块不依赖 Flask，依赖由
``execute_room_command`` 注入，避免与 ``room_console_commands`` 形成循环导入。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.agents.context import build_agent_context
from trpg_server.agents.narrative.ledger import load_facts, load_summaries, read_ledger_events, save_summary
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError
from trpg_server.agents.narrative.runtime_settings import resolve_narrative_settings
from trpg_server.agents.narrative.state_store import load_narrative_state, rebuild_narrative_state, save_narrative_state
from trpg_server.agents.narrative.trace import read_trace
from trpg_server.json_store import read_json
from trpg_server.room_log import log_room_event

logger = logging.getLogger(__name__)

ROOM_NARRATIVE_ACTOR = "房间控制台"
VALUE_OPTIONS = {"depth", "limit", "search", "turn", "entity", "status", "location", "object", "mode"}


def _parse(args: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    positionals: list[str] = []
    flags: set[str] = set()
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        item = str(args[index])
        if item.startswith("-"):
            name = item.lstrip("-").lower()
            if "=" in name:
                key, value = name.split("=", 1)
                options[key] = value
                flags.add(key)
                index += 1
                continue
            if name in VALUE_OPTIONS and index + 1 < len(args) and not str(args[index + 1]).startswith("-"):
                options[name] = str(args[index + 1])
                flags.add(name)
                index += 2
                continue
            flags.add(name)
            index += 1
            continue
        positionals.append(item)
        index += 1
    return positionals, flags, options


def _int_option(options: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(options.get(name, default))
    except (TypeError, ValueError):
        return default


def _info(room_dir: Path) -> dict[str, Any]:
    payload = read_json(room_dir / "info.json", default={})
    return payload if isinstance(payload, dict) else {}


def _runtime(room_id: Any, room_dir: Path, rooms_dir: Any, scenarios_dir: Any) -> tuple[NarrativeRuntime | None, str]:
    if not scenarios_dir or not Path(scenarios_dir).exists():
        return None, "剧本目录未配置，无法读取世界树。"
    context = build_agent_context(room_id, rooms_dir or room_dir.parent, scenarios_dir)
    try:
        return NarrativeRuntime.load(context), ""
    except NarrativeRuntimeError as exc:
        return None, str(exc)


def _confirm(
    *,
    preview: str,
    apply: Callable[[], tuple[bool, str]],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
    prompt: str,
) -> tuple[bool, str]:
    if flags & {"yes", "y"}:
        return apply()
    if confirm is None:
        return True, preview + "\n（预演模式：确认无误后加 --yes 执行）"
    if confirm(f"{prompt} (y/N)"):
        return apply()
    return True, preview + "\n（已取消，未执行）"


def _save_state(room_dir: Path, narrative: dict[str, Any]) -> None:
    save_narrative_state(room_dir, narrative)


def _set_house_rule(room_dir: Path, key: str, value: Any, room_id: Any, summary: str) -> tuple[bool, str]:
    from trpg_server.routes.rooms import _write_room

    info = _info(room_dir)
    rules = info.get("house_rules")
    if not isinstance(rules, dict):
        rules = {}
    rules[key] = value
    info["house_rules"] = rules
    _write_room(room_dir, info)
    log_room_event(room_dir, summary, actor=ROOM_NARRATIVE_ACTOR, source="room_console", room_id=room_id)
    return True, summary


def _status(room_dir: Path, room_id: Any, rooms_dir: Any, scenarios_dir: Any) -> tuple[bool, str]:
    info = _info(room_dir)
    settings = resolve_narrative_settings(info)
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    lines = [
        f"世界树: {'开' if settings.enabled else '关（kill switch）'} · 能力档 {settings.profile} · 护栏 {settings.guard}",
    ]
    if runtime is None:
        lines.append(f"  运行态不可用：{error}")
        return True, "\n".join(lines)
    narrative = runtime.narrative
    clock = narrative.get("clock") if isinstance(narrative.get("clock"), dict) else {}
    location = runtime.location_id
    lines.extend(
        [
            f"  拓扑 {runtime.bundle.topology} · 剧本 {runtime.bundle.scenario_id or '-'} "
            f"· 版本 {narrative.get('scenario_version') or runtime.bundle.scenario_version or '-'}",
            f"  当前地点 {runtime.title(location) if location else '-'}"
            f"（{location or '-'}）· 回合 {clock.get('turn', 0)}",
            f"  已访问 {len(narrative.get('visit_log') or [])} · 已触发 {len(narrative.get('trigger_fires') or {})} "
            f"· 已揭示线索 {len(narrative.get('revealed_clues') or [])}",
        ]
    )
    metrics = read_json(room_dir / "metrics.json", default={})
    if isinstance(metrics, dict) and metrics:
        keys = ("turn_count", "guard_denied", "guard_violation", "spoiler_denied", "triggers_fired", "media_missing")
        parts = [f"{key}={metrics[key]}" for key in keys if key in metrics]
        if parts:
            lines.append("  指标：" + " ".join(parts))
    return True, "\n".join(lines)


def _state(room_dir: Path, room_id: Any, rooms_dir: Any, scenarios_dir: Any, flags: set[str]) -> tuple[bool, str]:
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    narrative = runtime.narrative
    clock = narrative.get("clock") if isinstance(narrative.get("clock"), dict) else {}
    payload = {
        "location": runtime.location_id,
        "title": runtime.title(runtime.location_id),
        "turn": clock.get("turn", 0),
        "flags": narrative.get("flags") or {},
        "clocks": narrative.get("clocks") or {},
        "node_status": narrative.get("node_status") or {},
        "revealed_clues": narrative.get("revealed_clues") or [],
        "trigger_fires": narrative.get("trigger_fires") or {},
        "visit_log": narrative.get("visit_log") or [],
        "party_split": bool(narrative.get("party_split")),
        "party_locations": narrative.get("party_locations") or {},
    }
    if "json" in flags:
        import json

        return True, json.dumps(payload, ensure_ascii=False, indent=2)
    lines = [
        f"地点 {payload['title'] or '-'}（{payload['location'] or '-'}）· 回合 {payload['turn']}",
        f"旗标 {len(payload['flags'])} · 时钟 {len(payload['clocks'])} · 地点状态 {len(payload['node_status'])}",
        f"线索 {len(payload['revealed_clues'])} · 触发记录 {len(payload['trigger_fires'])} · 访问 {len(payload['visit_log'])}",
    ]
    if payload["flags"]:
        lines.append("  旗标：" + "、".join(sorted(payload["flags"])[:20]))
    if payload["clocks"]:
        clocks = [
            f"{key}={item.get('value', 0)}/{item.get('threshold', '-')}"
            for key, item in payload["clocks"].items()
            if isinstance(item, dict)
        ]
        if clocks:
            lines.append("  时钟：" + "、".join(clocks[:20]))
    if payload["party_split"]:
        lines.append("  队伍已分头行动：" + "、".join(
            f"{pc}@{runtime.title(loc)}" for pc, loc in payload["party_locations"].items()
        ))
    return True, "\n".join(lines)


def _map(
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any,
    scenarios_dir: Any,
    flags: set[str],
    options: Mapping[str, str],
) -> tuple[bool, str]:
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    start = runtime.location_id
    if not start:
        return True, "当前房间没有可用的起始地点。"
    depth = max(1, min(_int_option(options, "depth", 1), 5))
    edges: dict[str, list[tuple[Any, str]]] = {}
    node_ids = {node.id for node in runtime.bundle.nodes}
    for edge in runtime.bundle.edges:
        if edge.from_id not in node_ids or edge.to_id not in node_ids:
            continue
        edges.setdefault(edge.from_id, []).append((edge, edge.to_id))
        if edge.bidirectional:
            edges.setdefault(edge.to_id, []).append((edge, edge.from_id))

    lines = [f"当前地点 {runtime.title(start)}（{start}）· 深度 {depth}"]
    seen = {start}
    frontier = [(start, 0)]
    while frontier:
        current, level = frontier.pop(0)
        if level >= depth:
            continue
        for edge, target in edges.get(current, []):
            if target in seen:
                continue
            seen.add(target)
            condition = runtime.edge_condition(edge)
            marks = []
            if runtime.is_visited(target):
                marks.append("已访问")
            if not runtime.node_visible(target):
                marks.append("未揭示")
            if not condition.ok:
                marks.append("门控")
            frontier.append((target, level + 1))
            indent = "  " * (level + 2)
            suffix = f" [{'/'.join(marks)}]" if marks else ""
            lines.append(f"{indent}{runtime.title(target)}（{target}）{suffix}")
    if "json" in flags:
        import json

        return True, json.dumps(
            {
                "location": start,
                "depth": depth,
                "visited": [item for item in seen if runtime.is_visited(item)],
                "nodes": sorted(seen),
                "unreachable": runtime.index.unreachable_locations,
            },
            ensure_ascii=False,
            indent=2,
        )
    return True, "\n".join(lines)


def _exits(room_id: Any, room_dir: Path, rooms_dir: Any, scenarios_dir: Any) -> tuple[bool, str]:
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    items = runtime.exits(include_blocked=True, include_hidden=False)
    if not items:
        return True, "当前地点没有玩家可见出口。"
    lines = [f"当前地点 {runtime.title(runtime.location_id)} 的出口 {len(items)} 个："]
    for item in items:
        status = "可通行"
        if item["blocked"]:
            missing = item.get("missing") or []
            status = "条件未满足：" + "；".join(str(part) for part in missing[:3]) if missing else "条件未满足"
        elif item["visited"]:
            status = "已访问"
        lines.append(f"  [{'x' if item['blocked'] else 'o'}] {item['label']} → {item['title']}（{status}）")
    return True, "\n".join(lines)


def _ledger(room_dir: Path, flags: set[str], options: Mapping[str, str]) -> tuple[bool, str]:
    events = read_ledger_events(
        room_dir,
        limit=max(1, min(_int_option(options, "limit", 20), 200)),
        key_only="key-only" in flags,
        search=options.get("search") or "",
    )
    if not events:
        return True, "本房间暂无账本事件。"
    lines = [f"账本最近 {len(events)} 条："]
    for event in events:
        marker = "*" if event.get("key") else " "
        turn = (event.get("turn") if isinstance(event.get("turn"), int) else None) or "-"
        lines.append(
            f"  {marker} #{event.get('_seq')} T{turn} [{event.get('kind')}] "
            f"{event.get('summary') or event.get('pc') or ''}"
        )
    return True, "\n".join(lines)


def _facts(room_dir: Path, options: Mapping[str, str]) -> tuple[bool, str]:
    entity = str(options.get("entity") or "").strip().casefold()
    status = str(options.get("status") or "").strip().lower()
    items = load_facts(room_dir)
    if entity:
        items = [
            item for item in items
            if entity in str(item.get("entity_id") or "").casefold()
            or entity in str(item.get("entity") or "").casefold()
        ]
    if status:
        items = [item for item in items if str(item.get("status") or "").lower() == status]
    if not items:
        return True, "没有匹配的事实三元组。"
    lines = [f"事实 {len(items)} 条："]
    for item in items[:60]:
        lines.append(
            f"  {item.get('id') or '-'} [{item.get('status') or '-'}] "
            f"{item.get('entity') or item.get('entity_id') or '-'} · {item.get('relation') or item.get('predicate') or '-'}"
            f" · {item.get('value') or item.get('object') or '-'}"
        )
    return True, "\n".join(lines)


def _clocks(room_id: Any, room_dir: Path, rooms_dir: Any, scenarios_dir: Any) -> tuple[bool, str]:
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    clocks = runtime.narrative.get("clocks")
    if not isinstance(clocks, dict) or not clocks:
        return True, "本房间没有全局时钟。"
    lines = ["全局时钟："]
    for key, item in clocks.items():
        if not isinstance(item, dict):
            continue
        lines.append(
            f"  {item.get('title') or key}：{item.get('value', 0)}/{item.get('threshold', '-')}"
            f"{'（已到达）' if item.get('reached') else ''}"
        )
    return True, "\n".join(lines)


def _triggers(
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any,
    scenarios_dir: Any,
    flags: set[str],
    options: Mapping[str, str],
) -> tuple[bool, str]:
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    fires = runtime.narrative.get("trigger_fires") if isinstance(runtime.narrative.get("trigger_fires"), dict) else {}
    location = str(options.get("location") or "").strip()
    obj = str(options.get("object") or "").strip()
    rows = []
    for spec in runtime.bundle.triggers:
        if not isinstance(spec, dict):
            continue
        binding = spec.get("binding") if isinstance(spec.get("binding"), dict) else {}
        if location and str(binding.get("location_id") or "") != location:
            continue
        if obj and str(binding.get("object_id") or "") != obj:
            continue
        count = 0
        fire = fires.get(str(spec.get("id") or ""))
        if isinstance(fire, dict):
            count = int(fire.get("count") or 0)
        elif isinstance(fire, int):
            count = fire
        if "fired" in flags and count <= 0:
            continue
        rows.append(
            {
                "id": spec.get("id"),
                "label": spec.get("label") or spec.get("id"),
                "binding": binding.get("kind") or "-",
                "enabled": bool(spec.get("enabled", True)),
                "count": count,
            }
        )
    if not rows:
        return True, "没有匹配的触发器。"
    lines = [f"触发器 {len(rows)} 个："]
    for row in rows:
        lines.append(
            f"  {row['id']} · {row['label']} · 绑定={row['binding']} · "
            f"{'已触发 {0} 次'.format(row['count']) if row['count'] else '未触发'}"
            f"{'' if row['enabled'] else ' · 已停用'}"
        )
    return True, "\n".join(lines)


def _trigger_fire(
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any,
    scenarios_dir: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
    actor_id: Any,
) -> tuple[bool, str]:
    if not rest:
        return False, "用法：narrative trigger fire <trigger_id> [--yes]"
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    trigger_id = rest[0]

    def _apply() -> tuple[bool, str]:
        result = runtime.force_trigger(trigger_id, audience="player", actor_id=str(actor_id or ""))
        if not result.get("triggered"):
            reason = str(result.get("reason") or "trigger_not_found")
            return False, f"触发器未执行：{reason}"
        log_room_event(
            room_dir,
            f"手动触发触发器 {trigger_id}",
            actor=ROOM_NARRATIVE_ACTOR,
            source="room_console",
            room_id=room_id,
        )
        count = len(result.get("direct_messages") or [])
        return True, f"已触发 {trigger_id}（推送消息 {count} 条）"

    return _confirm(
        preview=f"将立即触发 {trigger_id}，附件与正文会推送给玩家。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt=f"确认触发 {trigger_id}?",
    )


def _media(room_dir: Path, flags: set[str]) -> tuple[bool, str]:
    messages = read_json(room_dir / "messages.json", default=[])
    rows: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            url = str(attachment.get("url") or "")
            rows.append(
                {
                    "message_id": message.get("id"),
                    "kind": attachment.get("kind") or "-",
                    "name": attachment.get("name") or attachment.get("alt") or "-",
                    "url": url,
                    "missing": not url,
                }
            )
    if "missing" in flags:
        rows = [row for row in rows if row["missing"]]
    if not rows:
        return True, "本房间没有缺失素材。" if "missing" in flags else "本房间还没有已发送的媒体附件。"
    lines = [f"本房间媒体附件 {len(rows)} 项："]
    for row in rows[:60]:
        lines.append(
            f"  {row['kind']} {row['name']} → {row['url'] or '（缺失）'}"
        )
    return True, "\n".join(lines)


def _summary(
    room_id: Any,
    room_dir: Path,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    summaries = load_summaries(room_dir)
    if "rebuild" not in flags:
        lines = []
        for scope, items in summaries.items():
            if not items:
                continue
            lines.append(f"[{scope}]")
            for key, entry in items.items():
                text = str(entry.get("text") or "") if isinstance(entry, dict) else str(entry)
                if text:
                    lines.append(f"  {key}（T{entry.get('turn', 0) if isinstance(entry, dict) else 0}）：{text[:160]}")
        return True, "\n".join(lines) if lines else "本房间还没有摘要。"

    runtime_events = read_ledger_events(room_dir, limit=40, key_only=True)
    text = "；".join(
        str(event.get("summary") or "").strip()
        for event in runtime_events
        if str(event.get("summary") or "").strip()
    )

    def _apply() -> tuple[bool, str]:
        turn = 0
        try:
            from trpg_server.agents.narrative.state_store import load_narrative_state as _load

            clock = _load(room_dir).get("clock") or {}
            turn = int(clock.get("turn") or 0)
        except Exception:  # noqa: BLE001 - 摘要重建不能阻断控制台
            turn = 0
        entry = save_summary(room_dir, "scenario", "scenario", text or "（暂无可压缩的关键事件）", turn=turn)
        log_room_event(
            room_dir,
            "重建房间摘要",
            actor=ROOM_NARRATIVE_ACTOR,
            source="room_console",
            room_id=room_id,
        )
        return True, f"已重建剧本摘要（{len(entry.get('text') or '')} 字，回合 {turn}）"

    return _confirm(
        preview=f"将用最近 {len(runtime_events)} 条关键账本事件重写剧本级摘要。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt="确认重建摘要?",
    )


def _reset(
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any,
    scenarios_dir: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    if not rest:
        return False, "用法：narrative reset <location_id> [--yes]"
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    target = runtime.node(rest[0])
    if target is None or target.kind not in {"location", "ending", "area", "act"}:
        return False, f"找不到地点 {rest[0]}。"
    narrative = load_narrative_state(room_dir)

    def _apply() -> tuple[bool, str]:
        narrative["location"] = target.id
        status = narrative.get("node_status") if isinstance(narrative.get("node_status"), dict) else {}
        status[target.id] = "active"
        narrative["node_status"] = status
        _save_state(room_dir, narrative)
        log_room_event(
            room_dir,
            f"位置已重置到 {target.title or target.id}",
            actor=ROOM_NARRATIVE_ACTOR,
            source="room_console",
            room_id=room_id,
        )
        return True, f"已把队伍位置重置到 {target.title or target.id}（{target.id}）"

    return _confirm(
        preview=f"将把队伍位置重置到 {target.title or target.id}（{target.id}）。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt="确认重置位置?",
    )


def _unlock(
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any,
    scenarios_dir: Any,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    if not rest:
        return False, "用法：narrative unlock <edge_id|location_id> [--yes]"
    runtime, error = _runtime(room_id, room_dir, rooms_dir, scenarios_dir)
    if runtime is None:
        return False, error
    key = rest[0]
    edge = next((item for item in runtime.bundle.edges if item.id == key), None)
    node = runtime.node(key)
    if edge is None and node is None:
        return False, f"找不到通道或地点 {key}。"
    label = (edge.label if edge and edge.label else None) or (node.title if node else key)
    narrative = load_narrative_state(room_dir)

    def _apply() -> tuple[bool, str]:
        unlocked = narrative.get("unlocked") if isinstance(narrative.get("unlocked"), list) else []
        if key not in unlocked:
            unlocked.append(key)
        narrative["unlocked"] = unlocked
        _save_state(room_dir, narrative)
        log_room_event(
            room_dir,
            f"已手动解锁 {label}（{key}）",
            actor=ROOM_NARRATIVE_ACTOR,
            source="room_console",
            room_id=room_id,
        )
        return True, f"已解锁 {label}（{key}），此后该通道/地点不再受条件门控。"

    return _confirm(
        preview=f"将手动解锁 {label}（{key}），跳过其条件门控。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt="确认解锁?",
    )


def _guard(
    room_id: Any,
    room_dir: Path,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    if not rest:
        current = resolve_narrative_settings(_info(room_dir))
        return True, f"本房间护栏模式：{current.guard}（可用 off / shadow / enforce）"
    value = rest[0].lower()
    if value not in {"off", "shadow", "enforce"}:
        return False, "护栏模式只能是 off / shadow / enforce"

    def _apply() -> tuple[bool, str]:
        return _set_house_rule(room_dir, "narrative_guard", value, room_id, f"房间护栏模式已设为 {value}")

    return _confirm(
        preview=f"将把本房间护栏模式设为 {value}（下次请求生效）。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt=f"确认把护栏设为 {value}?",
    )


def _profile(
    room_id: Any,
    room_dir: Path,
    rest: list[str],
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    if not rest:
        current = resolve_narrative_settings(_info(room_dir))
        return True, f"本房间能力档：{current.profile}（可用 graph / degraded）"
    value = rest[0].lower()
    if value not in {"graph", "degraded"}:
        return False, "能力档只能是 graph / degraded"

    def _apply() -> tuple[bool, str]:
        return _set_house_rule(room_dir, "narrative_profile", value, room_id, f"房间能力档已设为 {value}")

    return _confirm(
        preview=f"将把本房间能力档设为 {value}（下次请求生效）。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt=f"确认把能力档设为 {value}?",
    )


def _rebuild_state(
    room_id: Any,
    room_dir: Path,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    def _apply() -> tuple[bool, str]:
        narrative = rebuild_narrative_state(room_dir)
        log_room_event(
            room_dir,
            "已从关键帧与账本重建运行态",
            actor=ROOM_NARRATIVE_ACTOR,
            source="room_console",
            room_id=room_id,
        )
        return True, f"已重建运行态：地点 {narrative.get('location') or '-'}，回合 {(narrative.get('clock') or {}).get('turn', 0)}"

    return _confirm(
        preview="将从最近关键帧 + 账本重放并覆盖当前运行态（不可撤销）。",
        apply=_apply,
        flags=flags,
        confirm=confirm,
        prompt="确认重建运行态?",
    )


def handle_room_narrative_command(
    args: list[str],
    *,
    room_id: Any,
    room_dir: Path,
    rooms_dir: Any = None,
    scenarios_dir: Any = None,
    app: Any = None,
    actor: Any = None,
    actor_id: Any = None,
    confirm: Callable[[str], bool] | None = None,
) -> tuple[bool, str]:
    """执行 `narrative <subcommand>` 房间命令；返回 (ok, 文本)。"""
    positionals, flags, options = _parse(args)
    room_dir = Path(room_dir)
    scenarios_dir = scenarios_dir or (app.config.get("SCENARIOS_DIR") if app is not None else None)
    if not positionals:
        return _status(room_dir, room_id, rooms_dir, scenarios_dir)
    sub = positionals[0].lower()
    rest = positionals[1:]

    if sub == "status":
        return _status(room_dir, room_id, rooms_dir, scenarios_dir)
    if sub == "state":
        return _state(room_dir, room_id, rooms_dir, scenarios_dir, flags)
    if sub == "map":
        return _map(room_id, room_dir, rooms_dir, scenarios_dir, flags, options)
    if sub == "exits":
        return _exits(room_id, room_dir, rooms_dir, scenarios_dir)
    if sub == "ledger":
        return _ledger(room_dir, flags, options)
    if sub == "facts":
        return _facts(room_dir, options)
    if sub == "clocks":
        return _clocks(room_id, room_dir, rooms_dir, scenarios_dir)
    if sub == "triggers":
        return _triggers(room_id, room_dir, rooms_dir, scenarios_dir, flags, options)
    if sub == "trigger":
        if not rest or rest[0].lower() != "fire":
            return False, "用法：narrative trigger fire <trigger_id> [--yes]"
        return _trigger_fire(room_id, room_dir, rooms_dir, scenarios_dir, rest[1:], flags, confirm, actor_id)
    if sub == "media":
        return _media(room_dir, flags)
    if sub == "opening":
        if not scenarios_dir:
            return False, "剧本目录未配置，无法读取开场配置。"
        from trpg_server.narrative_admin import handle_narrative_command

        passthrough = list(args[1:])
        return handle_narrative_command(
            ["opening", str(room_id), *passthrough],
            scenarios_dir,
            rooms_dir=rooms_dir,
            confirm=confirm,
        )
    if sub == "trace":
        turn = _int_option(options, "turn", -1)
        rows = read_trace(room_dir, turn=None if turn < 0 else turn, limit=max(1, min(_int_option(options, "limit", 20), 200)))
        if not rows:
            return True, "本房间暂无轨迹记录。"
        import json

        if "json" in flags:
            return True, json.dumps(rows, ensure_ascii=False, indent=2)
        lines = [f"轨迹最近 {len(rows)} 条："]
        for row in rows:
            guard = row.get("guard") if isinstance(row.get("guard"), dict) else {}
            contract = row.get("contract") if isinstance(row.get("contract"), dict) else {}
            lines.append(
                f"  T{row.get('turn', '-')} · {row.get('branch') or '-'} @ {row.get('location') or '-'} "
                f"· guard={guard.get('decision') or '-'}"
                f" · targets={len(contract.get('targets') or [])}"
            )
        return True, "\n".join(lines)
    if sub == "summary":
        return _summary(room_id, room_dir, flags, confirm)
    if sub == "reset":
        return _reset(room_id, room_dir, rooms_dir, scenarios_dir, rest, flags, confirm)
    if sub == "unlock":
        return _unlock(room_id, room_dir, rooms_dir, scenarios_dir, rest, flags, confirm)
    if sub == "guard":
        return _guard(room_id, room_dir, rest, flags, confirm)
    if sub == "profile":
        return _profile(room_id, room_dir, rest, flags, confirm)
    if sub == "rebuild-state":
        return _rebuild_state(room_id, room_dir, flags, confirm)
    return False, f"未知 narrative 子命令：{sub}"


def _help_text() -> str:
    return "\n".join(
        [
            "房间世界树命令（仅作用于本房间）：",
            "  narrative status                     （能力档、拓扑、位置、版本与护栏状态）",
            "  narrative state [--json]             （位置、旗标、时钟、线索与触发记录）",
            "  narrative map [--depth N]            （当前可达地图：已访问/未揭示/门控）",
            "  narrative exits                      （当前地点出口与缺失条件）",
            "  narrative ledger [--limit N] [--key-only] [--search <text>]",
            "                                       （查看账本，默认最近 20 条）",
            "  narrative facts [--entity <id>] [--status <s>]（事实三元组）",
            "  narrative clocks                     （全局时钟与阈值）",
            "  narrative triggers [--location <id>] [--object <id>] [--fired]",
            "                                       （触发器与触发次数，只显示名称）",
            "  narrative trigger fire <id> [--yes]  （手动触发，房主/管理员）",
            "  narrative media [--missing]          （本房间已发媒体与缺失素材）",
            "  narrative opening [--publish|--repair] [--yes]（预览/补发开场）",
            "  narrative trace [--turn N] [--limit N]（JEV/检索/护栏轨迹）",
            "  narrative summary [--rebuild] [--yes]（查看或重建摘要）",
            "  narrative reset <location_id> [--yes]（重置位置，救场）",
            "  narrative unlock <edge|location> [--yes]（手动解锁通道/地点）",
            "  narrative guard <off|shadow|enforce> [--yes]（切换护栏模式）",
            "  narrative profile <graph|degraded> [--yes]（切换能力档）",
            "  narrative rebuild-state [--yes]      （从关键帧+账本重放恢复状态）",
            "  说明：破坏性命令缺省为「预演」，加 --yes 或在交互确认时输入 y 才会执行。",
        ]
    )


ROOM_NARRATIVE_HELP = _help_text

__all__ = ["handle_room_narrative_command", "ROOM_NARRATIVE_HELP"]
