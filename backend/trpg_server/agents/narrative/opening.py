"""开局、自动导入与预注入（设计文档 §5.9）。

本模块只做确定性编排：

1. `begin_scenario()` 把房间运行态重置到起始地点并写关键帧；
2. `build_opening_pack()` 生成玩家可见开场包与 KP 专属真相；
3. `opening_messages()` 把开场包变成可持久化的 KP 消息；
4. `fire_opening_triggers()` 由 runtime 调用，只求值起始地点首次进入的触发器。

模型仍然负责叙事，但开场事实不再由模型临场猜测；缺失开场文本时回退到
确定性最小开场，并记录 `opening.missing_intro`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from trpg_server.agents.narrative.coerce import as_str_list as _str_list
from trpg_server.agents.narrative.guards import forbidden_entries
from trpg_server.agents.narrative.logger import log_event, log_warning
from trpg_server.agents.narrative.model import (
    DEFAULT_OPENING_MODE,
    VISIBILITY_KP_ONLY,
    NarrativeBundle,
    normalize_start,
)
from trpg_server.agents.narrative.runtime import NarrativeRuntime
from trpg_server.agents.narrative.state_store import (
    default_narrative_state,
    load_narrative_state,
    reset_narrative_state,
    write_keyframe,
)
from trpg_server.agents.narrative.store import NarrativeIndex, build_index, resolve_start


class OpeningError(RuntimeError):
    """开场无法建立（缺起始地点、缺剧本等）。"""


DEFAULT_MINIMAL_INTRO = "故事从这里开始。"
_KP_LINE_RE = re.compile(r"^\s*(?:KP|GM|守秘人|仅\s*KP)\s*[：:]", re.I)
_TRUTH_RE = re.compile(r"实际上|事实上|真相是|其实是|真实目的|伪装成|冒充|并不知道|误以为")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _module_type(module: Mapping[str, Any]) -> str:
    return _text(module.get("module_type") or module.get("type")).lower()


def find_opening_module(
    scenario: Mapping[str, Any] | None,
    module_id: str | None = None,
) -> dict[str, Any] | None:
    """按 id 或 module_type=opening 找开场模块。"""
    modules = scenario.get("modules") if isinstance(scenario, Mapping) else None
    if not isinstance(modules, list):
        return None
    candidates = [dict(item) for item in modules if isinstance(item, Mapping)]
    wanted = _text(module_id)
    if wanted:
        for module in candidates:
            if _text(module.get("id")) == wanted:
                return module
        return None
    return next((module for module in candidates if _module_type(module) == "opening"), None)


def _split_module_content(content: Any) -> tuple[list[str], list[str]]:
    """把开场模块正文拆成玩家可见段落与 KP 专属段落。"""
    text = _text(content)
    if not text:
        return [], []
    public: list[str] = []
    truth: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            paragraph = "\n".join(buffer).strip()
            if paragraph:
                public.append(paragraph)
            buffer.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if _KP_LINE_RE.match(line):
            flush()
            truth.append(re.sub(r"^\s*(?:KP|GM|守秘人|仅\s*KP)\s*[：:]\s*", "", line, flags=re.I))
            continue
        if _TRUTH_RE.search(line):
            flush()
            truth.append(line)
            continue
        buffer.append(line)
    flush()
    return public, truth


def resolve_start_config(
    bundle: NarrativeBundle,
    scenario: Mapping[str, Any] | None = None,
    *,
    index: NarrativeIndex | None = None,
) -> dict[str, Any]:
    """合并 bundle.start、meta.start 与开场模块，返回可用开场配置。"""
    start = normalize_start(bundle.start)
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    if not start.get("location_id"):
        meta_start = meta.get("start") or meta.get("start_location")
        if isinstance(meta_start, str) and meta_start.strip():
            start["location_id"] = meta_start.strip()
    if not start.get("location_id"):
        start["location_id"] = (index or build_index(bundle)).start or resolve_start(bundle)
    module = find_opening_module(scenario, start.get("opening_module_id"))
    if module is not None:
        if not start.get("opening_module_id"):
            start["opening_module_id"] = _text(module.get("id")) or None
        if not start.get("public_intro"):
            public, _ = _split_module_content(module.get("content"))
            start["public_intro"] = public
    return start


def _initial_clocks(bundle: NarrativeBundle) -> dict[str, dict[str, Any]]:
    clocks: dict[str, dict[str, Any]] = {}
    for item in bundle.clocks:
        if not isinstance(item, Mapping):
            continue
        clock_id = _text(item.get("id") or item.get("name"))
        if not clock_id:
            continue
        entry: dict[str, Any] = {"value": 0}
        threshold = _as_int(item.get("threshold") or item.get("max"), 0)
        if threshold:
            entry["threshold"] = threshold
        visibility = _text(item.get("visibility"))
        if visibility:
            entry["visibility"] = visibility
        clocks[clock_id] = entry
    for node in bundle.nodes:
        if node.kind == "clock" and node.id not in clocks:
            clocks[node.id] = {"value": 0}
    return clocks


def _fresh_state(bundle: NarrativeBundle, *, scenario_version: str, location_id: str) -> dict[str, Any]:
    state = default_narrative_state()
    state["mode"] = "graph"
    state["topology"] = bundle.topology or "hybrid"
    state["scenario_version"] = _text(scenario_version or bundle.scenario_version or "1")
    state["location"] = location_id
    state["visit_log"] = [location_id]
    state["node_status"] = {location_id: "active"}
    state["clock"] = {"turn": 0, "in_game_time": "", "scene_enter_turn": 0}
    state["clocks"] = _initial_clocks(bundle)
    return state


def _attachment_payload(asset: Mapping[str, Any], visibility: str = "public", caption: str = "") -> dict[str, Any]:
    return {
        "id": _text(asset.get("id")),
        "kind": _text(asset.get("kind")) or "file",
        "url": _text(asset.get("url")),
        "mime": _text(asset.get("mime")),
        "alt": _text(asset.get("alt")),
        "caption": caption,
        "body": _text(asset.get("body")),
        "name": _text(asset.get("name")),
        "visibility": visibility,
        "spoiler_level": 0,
    }


def _opening_attachments(
    bundle: NarrativeBundle,
    start: Mapping[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    if not start.get("opening_attachments"):
        return []
    assets = {
        _text(asset.get("id")): asset
        for asset in bundle.assets
        if isinstance(asset, Mapping) and _text(asset.get("id"))
    }
    trigger_attachments: dict[str, tuple[dict[str, Any], str]] = {}
    for trigger in bundle.triggers:
        if not isinstance(trigger, Mapping):
            continue
        for attachment in trigger.get("attachments") or []:
            if not isinstance(attachment, Mapping):
                continue
            asset_id = _text(attachment.get("asset_id") or attachment.get("id"))
            if asset_id:
                trigger_attachments.setdefault(
                    asset_id,
                    (dict(attachment), _text(attachment.get("visibility")) or "public"),
                )
    result: list[dict[str, Any]] = []
    for reference in start.get("opening_attachments") or []:
        asset_id = _text(reference)
        if not asset_id:
            continue
        asset = assets.get(asset_id)
        visibility = "public"
        caption = ""
        if asset is None:
            fallback = trigger_attachments.get(asset_id)
            if fallback is not None:
                asset, visibility = fallback
        if asset is None:
            warnings.append(f"opening.missing_asset:{asset_id}")
            continue
        visibility = _text(asset.get("visibility")) or visibility
        caption = _text(asset.get("caption"))
        if visibility == VISIBILITY_KP_ONLY:
            continue
        result.append(_attachment_payload(asset, visibility=visibility, caption=caption))
    return result


def _location_snapshot(runtime: NarrativeRuntime) -> dict[str, Any]:
    card = runtime.location_card()
    if card.get("error"):
        return {}
    return {
        "location_id": card.get("location_id"),
        "title": card.get("title"),
        "summary": card.get("summary"),
        "body": card.get("body"),
        "objects": [
            {"id": item.get("id"), "name": item.get("name"), "summary": item.get("summary")}
            for item in card.get("objects") or []
            if isinstance(item, Mapping)
        ],
    }


def build_opening_pack(
    bundle: NarrativeBundle,
    narrative: Mapping[str, Any],
    scenario: Mapping[str, Any] | None = None,
    *,
    start: Mapping[str, Any] | None = None,
    index: NarrativeIndex | None = None,
) -> dict[str, Any]:
    """生成开场包；纯读，不写盘，供 dry-run 与修复复用。"""
    index = index or build_index(bundle)
    start_config = dict(start) if isinstance(start, Mapping) else resolve_start_config(bundle, scenario, index=index)
    location_id = _text(start_config.get("location_id")) or index.start
    if not location_id:
        raise OpeningError("剧本没有起始地点，无法生成开场包")
    view = dict(narrative)
    if not view.get("location"):
        view["location"] = location_id
    runtime = NarrativeRuntime(
        bundle=bundle,
        narrative=view,
        index=index,
        scenario=dict(scenario) if isinstance(scenario, Mapping) else {},
    )
    warnings: list[str] = []
    location = _location_snapshot(runtime)
    module = find_opening_module(scenario, start_config.get("opening_module_id"))
    module_public, module_truth = _split_module_content((module or {}).get("content"))
    public_intro = _str_list(start_config.get("public_intro")) or module_public
    missing_intro = not public_intro
    if missing_intro:
        title = _text(location.get("title")) or location_id
        public_intro = [f"你们抵达了{title}。"] if title else [DEFAULT_MINIMAL_INTRO]
        log_warning("opening.missing_intro", scenario_id=bundle.scenario_id, location_id=location_id)
    truth_parts = _str_list([start_config.get("kp_truth")])
    truth_parts.extend(module_truth)
    start_node = bundle.node(location_id)
    if start_node is not None and _text(start_node.kp_notes):
        truth_parts.append(_text(start_node.kp_notes))
    forbidden = [
        entry.get("name")
        for entry in forbidden_entries(bundle, view)
        if isinstance(entry, Mapping) and _text(entry.get("name"))
    ]
    pack = {
        "scenario_id": bundle.scenario_id,
        "location_id": location_id,
        "opening_mode": _text(start_config.get("opening_mode")) or DEFAULT_OPENING_MODE,
        "public_intro": public_intro,
        "location": location,
        "exits": [
            {
                "target_id": item.get("target_id"),
                "title": item.get("title"),
                "label": item.get("label"),
                "blocked": bool(item.get("blocked")),
                "visited": bool(item.get("visited")),
            }
            for item in runtime.exits()
            if isinstance(item, Mapping)
        ],
        "attachments": _opening_attachments(bundle, start_config, warnings),
        "kp_truth": "\n\n".join(truth_parts),
        "forbidden_names": forbidden,
        "allow_enter_triggers": bool(start_config.get("allow_enter_triggers", True)),
        "missing_intro": missing_intro,
        "warnings": warnings,
    }
    log_event(
        "opening.build",
        scenario_id=bundle.scenario_id,
        location_id=location_id,
        mode=pack["opening_mode"],
        intro_paragraphs=len(public_intro),
        attachments=len(pack["attachments"]),
        missing_intro=missing_intro,
    )
    return pack


def render_opening_prompt(pack: Mapping[str, Any], *, published: bool) -> str:
    """把开场包渲染成模型提示段。KP 真相单独标注，禁止转述给玩家。"""
    lines = ["【开场包（脚本既定事实，不得改写）】"]
    intro = _str_list(pack.get("public_intro"))
    if intro:
        status = "已由系统发布，不要逐字复述" if published else "请据此撰写开场"
        lines.append(f"开场白（{status}）：" + "\n".join(intro))
    location = pack.get("location") if isinstance(pack.get("location"), Mapping) else {}
    if location:
        summary = _text(location.get("summary"))
        lines.append(
            f"起始地点：{_text(location.get('title')) or _text(pack.get('location_id'))}"
            + (f"（{summary}）" if summary else "")
        )
    exits = [
        _text(item.get("label") or item.get("title"))
        for item in pack.get("exits") or []
        if isinstance(item, Mapping) and not item.get("blocked")
    ]
    exits = [item for item in exits if item]
    if exits:
        lines.append("玩家可见出口：" + "、".join(exits[:12]))
    attachments = [
        _text(item.get("alt") or item.get("caption") or item.get("kind"))
        for item in pack.get("attachments") or []
        if isinstance(item, Mapping)
    ]
    attachments = [item for item in attachments if item]
    if attachments:
        lines.append("开场附件：" + "、".join(attachments[:12]))
    truth = _text(pack.get("kp_truth"))
    if truth:
        lines.append("【KP 专属真相（禁止向玩家讲述或暗示，等待解锁）】" + truth)
    forbidden = _str_list(pack.get("forbidden_names"))
    if forbidden:
        lines.append("【禁词（揭示前不要写出）】" + "、".join(forbidden[:30]))
    lines.append("【开场约束】不要声称玩家说了‘开始’，不要替玩家决定行动，不要在结尾列出选项。")
    return "\n".join(lines)


def opening_message(pack: Mapping[str, Any], *, include_intro: bool = True) -> dict[str, Any]:
    """服务端发布的开场消息；`type=kp`，附件走 metadata。"""
    intro = _str_list(pack.get("public_intro")) if include_intro else []
    attachments = [
        dict(item)
        for item in pack.get("attachments") or []
        if isinstance(item, Mapping)
    ]
    return {
        "type": "kp",
        "sender_id": None,
        "sender_name": "KP",
        "avatar": "",
        "content": "\n\n".join(intro),
        "metadata": {
            "message_kind": "opening",
            "reveal_source": "opening",
            "location_id": pack.get("location_id"),
            "opening_mode": pack.get("opening_mode"),
            "attachments": attachments,
            "audience": "player",
        },
    }


@dataclass
class OpeningPlan:
    scenario_id: str = ""
    location_id: str | None = None
    opening_mode: str = "hybrid"
    pack: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_intro: bool = False
    triggers_fired: list[str] = field(default_factory=list)
    trigger_messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def published_intro(self) -> bool:
        return self.opening_mode in {"fixed", "hybrid"}

    def prompt_text(self) -> str:
        return render_opening_prompt(self.pack, published=self.published_intro)

    def messages(self, *, force_intro: bool = False) -> list[dict[str, Any]]:
        if not self.pack:
            return []
        include = force_intro or self.published_intro
        message = opening_message(self.pack, include_intro=include)
        result: list[dict[str, Any]] = []
        if message["content"] or message["metadata"]["attachments"]:
            result.append(message)
        result.extend(dict(item) for item in self.trigger_messages)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "location_id": self.location_id,
            "opening_mode": self.opening_mode,
            "missing_intro": self.missing_intro,
            "warnings": list(self.warnings),
            "triggers_fired": list(self.triggers_fired),
            "pack": dict(self.pack),
        }


def begin_scenario(
    room_dir: Path | str,
    bundle: NarrativeBundle,
    scenario: Mapping[str, Any] | None = None,
    *,
    scenario_version: str = "",
    room_id: str | None = None,
    index: NarrativeIndex | None = None,
) -> OpeningPlan:
    """确定性初始化开局运行态；重复调用会重新开局（路由层用 409 保护）。"""
    if not room_dir:
        raise OpeningError("房间目录不可用，无法开局")
    index = index or build_index(bundle)
    start = resolve_start_config(bundle, scenario, index=index)
    location_id = _text(start.get("location_id"))
    if not location_id:
        raise OpeningError("剧本没有起始地点，无法开局")
    state = _fresh_state(bundle, scenario_version=scenario_version, location_id=location_id)
    reset_narrative_state(room_dir, state)
    # 账本已在 reset 中清空，起始关键帧从 seq=0 开始，重放不会跨局。
    write_keyframe(room_dir, state, turn=0, reason="start", ledger_seq=0)
    log_event(
        "scenario.begin",
        room_id=room_id,
        scenario_id=bundle.scenario_id,
        scenario_version=state["scenario_version"],
        location_id=location_id,
        topology=bundle.topology,
    )
    pack = build_opening_pack(bundle, state, scenario, start=start, index=index)
    runtime = NarrativeRuntime(
        bundle=bundle,
        narrative=state,
        index=index,
        room_id=room_id,
        room_dir=Path(room_dir),
        scenario=dict(scenario) if isinstance(scenario, Mapping) else {},
    )
    trigger_result = runtime.fire_opening_triggers(allow_enter=bool(pack.get("allow_enter_triggers", True)))
    plan = OpeningPlan(
        scenario_id=bundle.scenario_id,
        location_id=location_id,
        opening_mode=_text(pack.get("opening_mode")) or DEFAULT_OPENING_MODE,
        pack=pack,
        state=runtime.narrative,
        warnings=list(pack.get("warnings") or []),
        missing_intro=bool(pack.get("missing_intro")),
        triggers_fired=list(trigger_result.get("triggers_fired") or []),
        trigger_messages=list(trigger_result.get("direct_messages") or []),
    )
    log_event(
        "opening.publish",
        room_id=room_id,
        scenario_id=bundle.scenario_id,
        location_id=location_id,
        mode=plan.opening_mode,
        messages=len(plan.messages()),
        triggers=len(plan.triggers_fired),
    )
    return plan


def plan_scenario(
    room_dir: Path | str,
    bundle: NarrativeBundle,
    scenario: Mapping[str, Any] | None = None,
    *,
    scenario_version: str = "",
    index: NarrativeIndex | None = None,
) -> OpeningPlan:
    """只读预览：用现有运行态（或空态）生成开场包，不写盘。"""
    index = index or build_index(bundle)
    narrative = load_narrative_state(room_dir) if room_dir else default_narrative_state()
    start = resolve_start_config(bundle, scenario, index=index)
    location_id = _text(start.get("location_id")) or _text(narrative.get("location"))
    pack = build_opening_pack(bundle, narrative, scenario, start=start, index=index)
    return OpeningPlan(
        scenario_id=bundle.scenario_id,
        location_id=location_id,
        opening_mode=_text(pack.get("opening_mode")) or DEFAULT_OPENING_MODE,
        pack=pack,
        state=dict(narrative),
        warnings=list(pack.get("warnings") or []),
        missing_intro=bool(pack.get("missing_intro")),
    )


__all__ = [
    "DEFAULT_MINIMAL_INTRO",
    "OpeningError",
    "OpeningPlan",
    "begin_scenario",
    "build_opening_pack",
    "find_opening_module",
    "opening_message",
    "plan_scenario",
    "render_opening_prompt",
    "resolve_start_config",
]
