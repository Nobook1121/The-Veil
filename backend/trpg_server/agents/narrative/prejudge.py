"""JEV 预判断：意图分类、实体链接、候选收窄与回合契约（§5.1 / §5.3 / §5.4）。

纯规则实现（``jev_mode=deterministic``）：正则/词典 + 图遍历 + 状态查表，
不调用任何模型；任何异常都回退为「无契约」，绝不阻塞主请求。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from trpg_server.agents.narrative.logger import log_event, log_warning
from trpg_server.agents.narrative.model import NarrativeNode
from trpg_server.agents.narrative.visibility import forbidden_names

logger = logging.getLogger(__name__)

INTENT_DIALOGUE = "dialogue"
INTENT_INVESTIGATE = "investigate"
INTENT_MOVE = "move"
INTENT_COMBAT = "combat"
INTENT_META = "meta"
INTENTS = (INTENT_DIALOGUE, INTENT_INVESTIGATE, INTENT_MOVE, INTENT_COMBAT, INTENT_META)

# 词典顺序不作为优先级；打分只看命中词数量与词长。
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    INTENT_MOVE: (
        "前往", "走到", "走向", "走进", "进入", "进去", "离开", "退出", "回到", "返回",
        "下楼", "上楼", "爬下", "爬上", "穿过", "跑向", "移动到", "移步", "动身", "去",
    ),
    INTENT_COMBAT: (
        "攻击", "开枪", "射击", "搏斗", "格斗", "闪避", "追逐", "逃跑", "战斗", "反击",
        "砍", "刺", "殴打", "擒抱", "掩护", "瞄准",
    ),
    INTENT_INVESTIGATE: (
        "调查", "搜查", "检查", "查看", "观察", "研究", "阅读", "翻找", "搜索", "聆听",
        "倾听", "闻", "触摸", "打开", "仔细看", "看看", "摸索", "检视", "翻看", "推",
    ),
    INTENT_DIALOGUE: (
        "说", "问", "告诉", "交谈", "喊", "回答", "询问", "对话", "打招呼", "聊",
        "叫道", "低语", "搭话", "回应",
    ),
    INTENT_META: (
        "规则", "系统", "怎么判定", "如何检定", "怎么玩", "技能列表", "角色卡规则",
        "掷骰规则", "换场景", "存档", "读档",
    ),
}

# 拓扑预设只调权重，不改代码（§5.3）。
TOPOLOGY_WEIGHTS: dict[str, dict[str, float]] = {
    "linear": {"keyword": 0.30, "adjacent": 0.35, "entity": 0.20, "intent": 0.15, "hook": 0.10},
    "hub": {"keyword": 0.30, "adjacent": 0.20, "entity": 0.20, "intent": 0.15, "hook": 0.25},
    "sandbox": {"keyword": 0.30, "adjacent": 0.20, "entity": 0.20, "intent": 0.15, "hook": 0.25},
    "hybrid": {"keyword": 0.30, "adjacent": 0.25, "entity": 0.20, "intent": 0.15, "hook": 0.10},
}

_GENERIC_NAMES = {"他", "她", "它", "他们", "她们", "玩家", "调查员", "守秘人", "你们", "你"}
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 256
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, str, str], tuple[float, "TurnContract"]] = {}
_FALLBACK_LOGGED: set[str] = set()


def _turn(runtime: Any) -> int:
    narrative = getattr(runtime, "narrative", None)
    if not isinstance(narrative, Mapping):
        return 0
    clock = narrative.get("clock")
    if not isinstance(clock, Mapping):
        return 0
    return _as_int(clock.get("turn"), 0)


def _log_fallback(room_id: str, reason: str) -> None:
    """§10.2：同房间同原因的降级只记一次，避免逐轮刷屏。"""
    marker = f"{room_id}:{reason}"
    with _CACHE_LOCK:
        if marker in _FALLBACK_LOGGED:
            return
        _FALLBACK_LOGGED.add(marker)
    log_warning("jev.fallback", room_id=room_id, reason=reason)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 元请求优先级更高：玩家说「看看规则」时不应被「看看」带进调查分支。
INTENT_WEIGHTS: dict[str, float] = {
    INTENT_META: 1.4,
    INTENT_MOVE: 1.1,
    INTENT_COMBAT: 1.05,
}


def classify_intent(message: str) -> dict[str, Any]:
    """词典打分选分支；无任何命中时按对话处理（不跳检索，也不额外拦动作）。"""
    text = _text(message)
    scores = {intent: 0.0 for intent in INTENTS}
    if text:
        for intent, keywords in INTENT_KEYWORDS.items():
            score = 0.0
            for keyword in keywords:
                if keyword in text:
                    score += 1.0 + 0.1 * len(keyword)
            scores[intent] = round(score * INTENT_WEIGHTS.get(intent, 1.0), 2)
    best = max(scores, key=lambda item: scores[item])
    if scores[best] <= 0:
        return {"intent": INTENT_DIALOGUE, "confidence": 0.3, "scores": scores}
    total = sum(scores.values()) or 1.0
    confidence = round(min(0.95, scores[best] / total), 2)
    return {"intent": best, "confidence": confidence, "scores": scores}


def _visible_name_index(runtime: Any) -> list[tuple[str, str, str, float]]:
    """(名称, 节点/物件 id, kind, 置信度)，按名称长度降序以取最长匹配。"""
    entries: list[tuple[str, str, str, float]] = []

    def _add(name: Any, object_id: str, kind: str, confidence: float) -> None:
        value = _text(name)
        if len(value) < 2 or value in _GENERIC_NAMES or len(value) > 40:
            return
        entry = (value, object_id, kind, confidence)
        if entry not in entries:
            entries.append(entry)

    for node in runtime.bundle.nodes:
        if not runtime.node_visible(node.id):
            continue
        _add(node.title, node.id, node.kind, 0.9)
        for alias in node.aliases:
            _add(alias, node.id, node.kind, 0.85)
    card = runtime.location_card()
    for obj in card.get("objects") or []:
        _add(obj.get("name"), _text(obj.get("id")), "object", 0.8)
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries


def link_targets(runtime: Any, message: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """别名/标题的最长匹配；匹配不到就保留原文，不强行链接（§5.3）。"""
    text = _text(message)
    if not text:
        return []
    folded = text.casefold()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, object_id, kind, confidence in _visible_name_index(runtime):
        if object_id in seen:
            continue
        if name.casefold() not in folded:
            continue
        seen.add(object_id)
        log_event(
            "jev.entity_link",
            room_id=_text(getattr(runtime, "room_id", None)) or "",
            turn=_turn(runtime),
            surface_hash=hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:8],
            entity_id=object_id,
            confidence=confidence,
        )
        result.append(
            {
                "id": object_id,
                "surface": name,
                "kind": kind,
                "confidence": confidence,
            }
        )
        if len(result) >= limit:
            break
    return result


def _name_matches(name: str, folded_text: str, mode: str) -> bool:
    """整词模式对拉丁词做边界判断；中文没有词边界，仍按子串处理（§8.2）。"""
    if mode == "substring" or not name.isascii():
        return name in folded_text
    pattern = re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])")
    return bool(pattern.search(folded_text))


def _node_keyword_hit(node: NarrativeNode, folded_text: str) -> float:
    weight = max(0.0, min(2.0, float(node.keyword_weight or 1.0)))
    mode = "substring" if node.keyword_match == "substring" else "word"
    for value in (node.title, *node.aliases, *node.keywords):
        name = _text(value)
        if len(name) >= 2 and _name_matches(name.casefold(), folded_text, mode):
            return weight
    return 0.0


def _unresolved_hook(runtime: Any, node: NarrativeNode) -> bool:
    """房间仍有未完成对象/线索，而非“没去过”（§5.3 打分项）。"""
    visited_objects = {str(item) for item in runtime.narrative.get("visited_objects") or []}
    for obj in node.objects:
        if not isinstance(obj, Mapping) or not obj.get("visible", True):
            continue
        object_id = _text(obj.get("id"))
        if object_id and object_id in visited_objects:
            continue
        for interaction in obj.get("interactions") or []:
            if isinstance(interaction, Mapping) and bool(interaction.get("repeatable", True)):
                return True
    return False


def candidate_locations(
    runtime: Any,
    message: str,
    targets: list[dict[str, Any]],
    intent: str,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """只允许当前地点、可达目标、已访问地点与关键词命中地点（§5.3）。"""
    current = runtime.location_id
    exits = runtime.exits(include_blocked=True, include_hidden=False)
    reachable = {
        item["target_id"] for item in exits if item.get("visible") and not item.get("blocked")
    }
    blocked = {item["target_id"] for item in exits if item.get("blocked")}
    folded = _text(message).casefold()
    target_ids = {str(item.get("id")) for item in targets}
    weights = TOPOLOGY_WEIGHTS.get(runtime.bundle.topology, TOPOLOGY_WEIGHTS["hybrid"])
    ranked: list[dict[str, Any]] = []
    for node in runtime.bundle.location_nodes:
        if node.visibility != "public" and not runtime.node_visible(node.id):
            continue
        keyword_hit = 1.0 if _node_keyword_hit(node, folded) else 0.0
        if node.id != current and node.id not in reachable and node.id not in blocked and not keyword_hit:
            continue
        adjacent = 1.0 if node.id in reachable else 0.0
        entity = 1.0 if node.id in target_ids else 0.0
        if intent == INTENT_MOVE:
            intent_fit = 1.0 if adjacent else 0.3
        elif intent == INTENT_INVESTIGATE:
            intent_fit = 0.6 if node.id == current else 0.3
        else:
            intent_fit = 0.5
        hook = 1.0 if node.id == current and _unresolved_hook(runtime, node) else 0.0
        score = (
            weights["keyword"] * keyword_hit
            + weights["adjacent"] * adjacent
            + weights["entity"] * entity
            + weights["intent"] * intent_fit
            + weights["hook"] * hook
        )
        ranked.append(
            {
                "id": node.id,
                "title": node.title or node.id,
                "score": round(score, 3),
                "reachable": adjacent > 0,
                "blocked": node.id in blocked,
                "visited": runtime.is_visited(node.id),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def _candidate_ids(runtime: Any, contract: "TurnContract", folded: str) -> tuple[set[str], set[str]]:
    """§5.4 候选集：先图过滤，再只在子集内检索。"""
    ids: set[str] = set()
    titles: set[str] = set()

    def _add(node_id: Any) -> None:
        node = runtime.node(node_id)
        if node is None:
            return
        ids.add(node.id)
        if node.title:
            titles.add(node.title)

    if contract.location:
        _add(contract.location)
    for item in contract.candidate_locations:
        _add(item.get("id"))
    for item in contract.reachable_exits:
        _add(item.get("to"))
    for target in contract.targets:
        _add(target.get("id"))
    for node in runtime.bundle.nodes:
        if runtime.node_visible(node.id) and _node_keyword_hit(node, folded):
            _add(node.id)
    # 实体 1 跳邻域与当前地点的一跳 lore。
    anchors = {
        str(item.get("id")) for item in contract.targets if str(item.get("id") or "").strip()
    }
    if contract.location:
        anchors.add(contract.location)
    lore_types = {"contains", "mentions", "reveals", "knows", "uses"}
    for edge in runtime.bundle.edges:
        linked = (
            edge.from_id in anchors
            or (edge.bidirectional and edge.to_id in anchors)
        )
        if not linked:
            continue
        if edge.type in lore_types or edge.from_id in anchors:
            _add(edge.to_id if edge.from_id in anchors else edge.from_id)
    card = runtime.location_card()
    for obj in card.get("objects") or []:
        name = _text(obj.get("name"))
        if name:
            titles.add(name)
    return ids, titles


def _verify(runtime: Any, contract: "TurnContract") -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """确定性校验：可达出口、可见对象、KP-only 专名（§5.3 Verify）。"""
    allowed: list[dict[str, Any]] = []
    forbidden: list[str] = []
    denied: list[dict[str, Any]] = []
    card = runtime.location_card()
    for obj in card.get("objects") or []:
        object_id = _text(obj.get("id"))
        if not object_id:
            continue
        allowed.append({"action": "inspect_object", "id": object_id})
        for interaction in obj.get("interactions") or []:
            if not isinstance(interaction, Mapping):
                continue
            interaction_id = _text(interaction.get("id"))
            if interaction_id:
                allowed.append(
                    {"action": "resolve_interaction", "id": interaction_id, "object": object_id}
                )
    exits = runtime.exits(include_blocked=True, include_hidden=False)
    for item in exits:
        if item.get("blocked"):
            forbidden.append(f"enter:{item['target_id']}")
            continue
        allowed.append({"action": "move_to_location", "id": item["target_id"]})
    if contract.intent == INTENT_MOVE:
        reachable = {item["target_id"] for item in exits if not item.get("blocked")}
        location_targets = [
            item for item in contract.targets if item.get("kind") in {"location", "ending"}
        ]
        for item in location_targets:
            target_id = _text(item.get("id"))
            if target_id and target_id not in reachable:
                denied.append(
                    {
                        "action": "move_to_location",
                        "id": target_id,
                        "reason": "not_reachable",
                        "reachable_exits": sorted(reachable),
                    }
                )
    for name in forbidden_names(runtime.bundle)[:12]:
        if name not in forbidden:
            forbidden.append(name)
    return allowed, forbidden, denied


def _evidence(runtime: Any, contract: "TurnContract") -> dict[str, Any]:
    """紧凑证据包：只给对象名与技能，不给任何 outcome 正文（§5.3）。"""
    card = runtime.location_card()
    clocks = []
    for clock_id, value in (runtime.narrative.get("clocks") or {}).items():
        entry = value if isinstance(value, Mapping) else {"value": value}
        clocks.append(
            {
                "id": clock_id,
                "value": _as_int(entry.get("value"), 0),
                "threshold": _as_int(entry.get("threshold"), 0),
            }
        )
    return {
        "location": card.get("location_id"),
        "area": (runtime.node(contract.location).parent if contract.location and runtime.node(contract.location) else None),
        "exits": [
            {key: item[key] for key in ("to", "type", "blocked", "visited") if key in item}
            for item in contract.reachable_exits
        ],
        "objects": [
            {"id": obj.get("id"), "name": obj.get("name"), "skills": [i.get("skill") for i in obj.get("interactions") or [] if isinstance(i, Mapping)]}
            for obj in card.get("objects") or []
        ],
        "clues": list(runtime.narrative.get("revealed_clues") or [])[:8],
        "clocks": clocks[:6],
        "recent": list(runtime.narrative.get("visit_log") or [])[-3:],
    }


@dataclass(frozen=True)
class TurnContract:
    """主模型只看到契约与证据包，不看到 JEV 的内部打分（§5.3）。"""

    intent: str
    confidence: float
    location: str | None
    targets: tuple[dict[str, Any], ...] = ()
    candidate_locations: tuple[dict[str, Any], ...] = ()
    reachable_exits: tuple[dict[str, Any], ...] = ()
    allowed: tuple[dict[str, Any], ...] = ()
    forbidden: tuple[str, ...] = ()
    denied: tuple[dict[str, Any], ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def skip_retrieval(self) -> bool:
        """meta/combat 不注入剧情检索（§5.1 分支收益）。"""
        return self.intent in {INTENT_META, INTENT_COMBAT}

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "location": self.location,
            "targets": [dict(item) for item in self.targets],
            "candidate_locations": [dict(item) for item in self.candidate_locations],
            "reachable_exits": [dict(item) for item in self.reachable_exits],
            "allowed": [dict(item) for item in self.allowed],
            "forbidden": list(self.forbidden),
            "denied": [dict(item) for item in self.denied],
            "evidence": dict(self.evidence),
        }


def _state_hash(narrative: Mapping[str, Any]) -> str:
    payload = {
        "location": narrative.get("location"),
        "flags": narrative.get("flags") or {},
        "clues": narrative.get("revealed_clues") or [],
        "clocks": narrative.get("clocks") or {},
        "revealed": [
            item.get("id") for item in narrative.get("revealed") or [] if isinstance(item, Mapping)
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _cache_lookup(key: tuple[str, str, str, str]) -> TurnContract | None:
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        stamp, contract = entry
        if now - stamp > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return contract


def _cache_store(key: tuple[str, str, str, str], contract: TurnContract) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest = sorted(_CACHE.items(), key=lambda item: item[1][0])[: _CACHE_MAX_ENTRIES // 4]
            for stale_key, _ in oldest:
                _CACHE.pop(stale_key, None)
        _CACHE[key] = (time.time(), contract)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def prepare_turn(runtime: Any, message: str) -> TurnContract | None:
    """每轮入口：意图 → 实体 → 候选地点 → 证据 → 校验 → 检索候选。

    任何异常都返回 ``None``（调用方按「无 JEV」继续），保证预判永不阻塞主请求。
    """
    text = _text(message)
    if not text:
        return None
    room_id = _text(getattr(runtime, "room_id", None))
    location = _text(runtime.location_id)
    key = (room_id, location, text.casefold(), _state_hash(runtime.narrative))
    cached = _cache_lookup(key)
    if cached is not None:
        log_event("jev.cache_hit", room_id=room_id or "", location=location or "", intent=cached.intent)
        return cached
    try:
        classification = classify_intent(text)
        intent = classification["intent"]
        log_event(
            "jev.judge",
            room_id=room_id or "",
            turn=_turn(runtime),
            intent=intent,
            branch=intent,
            confidence=classification["confidence"],
            source="rule",
        )
        targets = link_targets(runtime, text)
        locations = candidate_locations(runtime, text, targets, intent)
        exits = [
            {
                "to": item["target_id"],
                "title": item.get("title") or item["target_id"],
                "type": item.get("type"),
                "free": not item.get("blocked"),
                "blocked": bool(item.get("blocked")),
                "label": item.get("label") or "",
                "missing": list(item.get("missing") or [])[:4],
                "visited": bool(item.get("visited")),
            }
            for item in runtime.exits(include_blocked=True, include_hidden=False)
        ]
        contract = TurnContract(
            intent=intent,
            confidence=float(classification["confidence"]),
            location=location or None,
            targets=tuple(targets),
            candidate_locations=tuple(locations),
            reachable_exits=tuple(exits),
            evidence={},
        )
        allowed, forbidden, denied = _verify(runtime, contract)
        contract = TurnContract(
            intent=contract.intent,
            confidence=contract.confidence,
            location=contract.location,
            targets=contract.targets,
            candidate_locations=contract.candidate_locations,
            reachable_exits=contract.reachable_exits,
            allowed=tuple(allowed),
            forbidden=tuple(forbidden),
            denied=tuple(denied),
            evidence=_evidence(runtime, contract),
        )
        _cache_store(key, contract)
        log_event(
            "jev.verify",
            room_id=room_id or "",
            location=location or "",
            intent=contract.intent,
            confidence=contract.confidence,
            targets=len(contract.targets),
            candidates=len(contract.candidate_locations),
            allowed=len(contract.allowed),
            forbidden=len(contract.forbidden),
            denied=len(contract.denied),
        )
        return contract
    except Exception:  # noqa: BLE001 - 预判失败必须降级，不影响主流程
        logger.warning("jev.prepare_failed room=%s", room_id or "-", exc_info=True)
        _log_fallback(room_id, "prepare_failed")
        return None


def retrieval_candidates(runtime: Any, contract: TurnContract | None, message: str) -> tuple[set[str], set[str]]:
    """图谱约束检索的候选 (ids, titles)；无契约时返回空集合。"""
    if contract is None:
        return set(), set()
    try:
        ids, titles = _candidate_ids(runtime, contract, _text(message).casefold())
        log_event(
            "retrieval.graph",
            room_id=_text(getattr(runtime, "room_id", None)) or "",
            turn=_turn(runtime),
            intent=contract.intent,
            ids=len(ids),
            titles=len(titles),
        )
        return ids, titles
    except Exception:  # noqa: BLE001
        logger.warning("jev.candidates_failed", exc_info=True)
        _log_fallback(_text(getattr(runtime, "room_id", None)), "candidates_failed")
        return set(), set()


def _format_exit(item: Mapping[str, Any]) -> str:
    label = _text(item.get("title") or item.get("to"))
    if item.get("blocked"):
        missing = "、".join(_text(part) for part in item.get("missing") or [])
        return f"{label}（封闭：{missing or '条件未满足'}）"
    if item.get("visited"):
        return f"{label}（自由，已访问）"
    return f"{label}（自由）"


def _format_allowed(item: Mapping[str, Any]) -> str:
    action = _text(item.get("action"))
    if action == "inspect_object":
        return f"查看 {item.get('id')}"
    if action == "resolve_interaction":
        return f"结算 {item.get('id')}"
    if action == "move_to_location":
        return f"前往 {item.get('id')}"
    return f"{action} {item.get('id') or ''}".strip()


def render_contract(contract: TurnContract | None, *, limit: int = 8) -> str:
    """把契约压成一小段注入文本（不含 JEV 内部推理）。"""
    if contract is None:
        return ""
    lines = [f"【本轮路由】意图={contract.intent}｜置信度={contract.confidence}｜地点={contract.location or '-'}"]
    if contract.targets:
        lines.append(
            "【本轮目标】"
            + "；".join(f"{item.get('surface')}（{item.get('id')}）" for item in contract.targets[:6])
        )
    if contract.reachable_exits:
        lines.append(
            "【可达出口】" + "；".join(_format_exit(item) for item in contract.reachable_exits[:6])
        )
    if contract.allowed:
        lines.append(
            "【本轮允许】" + "；".join(_format_allowed(item) for item in contract.allowed[:limit])
        )
    if contract.forbidden:
        lines.append("【禁止】" + "；".join(contract.forbidden[:limit]))
    if contract.denied:
        lines.append(
            "【已拒绝】"
            + "；".join(
                f"{item.get('action')} {item.get('id')}（{item.get('reason')}）"
                for item in contract.denied[:4]
            )
        )
    objects = contract.evidence.get("objects") if isinstance(contract.evidence, dict) else []
    skills = [obj for obj in objects or [] if obj.get("skills")]
    if skills:
        lines.append(
            "【可用检定】"
            + "；".join(
                f"{obj.get('name') or obj.get('id')}：{'、'.join(_text(s) for s in obj['skills'] if _text(s))}"
                for obj in skills[:5]
            )
        )
    lines.append("说明：只能在与上述出口/对象一致时推进剧情；成功/失败文本必须由交互结算返回。")
    return "\n".join(lines)


__all__ = [
    "INTENTS",
    "INTENT_COMBAT",
    "INTENT_DIALOGUE",
    "INTENT_INVESTIGATE",
    "INTENT_META",
    "INTENT_MOVE",
    "TOPOLOGY_WEIGHTS",
    "TurnContract",
    "candidate_locations",
    "classify_intent",
    "clear_cache",
    "link_targets",
    "prepare_turn",
    "render_contract",
    "retrieval_candidates",
]
