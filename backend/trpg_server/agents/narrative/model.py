"""世界树 / 图谱的单一数据模型（`narrative.json`）。

设计文档：docs/kp-worldtree-graph-design.md §4.1、§4.2。

节点、边、触发器统一存放在 `NarrativeBundle` 中；定位树与图谱只是同一份
节点数据的不同投影。这里只做「结构归一化」，不参与任何检索或运行时裁决。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1

VISIBILITY_PUBLIC = "public"
VISIBILITY_HIDDEN = "hidden"
VISIBILITY_KP_ONLY = "kp_only"
VISIBILITY_STATES = (VISIBILITY_PUBLIC, VISIBILITY_HIDDEN, VISIBILITY_KP_ONLY)

# 旧剧本字段映射：`secret` 属于隐藏层，`kp/gm/keeper` 属于 KP 专属。
# 未识别的非空值一律收紧为 `kp_only`，避免未知值被当成 public 放行。
_LEGACY_VISIBILITY = {
    "public": VISIBILITY_PUBLIC,
    "player_visible": VISIBILITY_PUBLIC,
    "player": VISIBILITY_PUBLIC,
    "visible": VISIBILITY_PUBLIC,
    "hidden": VISIBILITY_HIDDEN,
    "secret": VISIBILITY_HIDDEN,
    "kp": VISIBILITY_KP_ONLY,
    "kp_only": VISIBILITY_KP_ONLY,
    "gm": VISIBILITY_KP_ONLY,
    "keeper": VISIBILITY_KP_ONLY,
}


def normalize_visibility(value: Any, default: str = VISIBILITY_PUBLIC) -> str:
    """旧值 `public/kp/player_visible` → 三态；未知非空值按 fail-closed 收紧。"""
    text = str(value or "").strip().lower()
    if not text:
        return default
    mapped = _LEGACY_VISIBILITY.get(text)
    if mapped:
        return mapped
    return VISIBILITY_KP_ONLY


# 节点类型：前四种属于定位树，其余是资产节点。
TREE_KINDS = ("act", "area", "location", "ending")
ASSET_KINDS = ("npc", "item", "clue", "concept", "interactable", "flag", "clock")
NODE_KINDS = TREE_KINDS + ASSET_KINDS

EDGE_TYPES = (
    "adjacent",
    "portal",
    "next",
    "leads_to",
    "requires",
    "reveals",
    "knows",
    "inhabits",
    "uses",
    "contains",
    "triggers",
    "conflicts",
    "mentions",
    "alias",
)
BIDIRECTIONAL_DEFAULT = {"adjacent", "contains", "alias", "conflicts"}

TOPOLOGIES = ("linear", "hub", "sandbox", "hybrid")
SCENE_ROLES = ("stage", "hub", "room", "event", "ending")
ACCESS_MODES = ("free", "gated", "one_way")

# 开场叙事模式（§5.9）：fixed 服务端固定文本，model 交给模型，hybrid 两者兼有。
OPENING_MODES = ("fixed", "model", "hybrid")
DEFAULT_OPENING_MODE = "hybrid"

_ID_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+")


def slugify_id(value: Any, prefix: str = "node") -> str:
    """生成稳定的节点/边 id；保留中文，避免出现空 id。"""
    text = str(value or "").strip()
    text = _ID_RE.sub("_", text).strip("_")
    return text or prefix


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def normalize_start(value: Any) -> dict[str, Any]:
    """归一化剧本开场配置（§5.9）。兼容 ``"start": "hall"`` 旧写法。"""
    if isinstance(value, str):
        value = {"location_id": value}
    data = dict(value) if isinstance(value, Mapping) else {}
    mode = str(data.get("opening_mode") or "").strip().lower()
    if mode not in OPENING_MODES:
        if data.get("fixed_opening") is not None:
            mode = "fixed" if _as_bool(data.get("fixed_opening")) else "hybrid"
        else:
            mode = DEFAULT_OPENING_MODE
    raw_intro = data.get("public_intro")
    if isinstance(raw_intro, str):
        intro = [
            part.strip()
            for part in re.split(r"\n\s*\n|\n", raw_intro)
            if part.strip()
        ]
    else:
        intro = _str_list(raw_intro)
    known = {
        "location_id", "start_location", "opening_module_id", "opening_mode",
        "fixed_opening", "public_intro", "opening_attachments", "attachments",
        "allow_enter_triggers", "allow_model_continuation", "kp_truth",
    }
    normalized = {
        "location_id": str(data.get("location_id") or data.get("start_location") or "").strip() or None,
        "opening_module_id": str(data.get("opening_module_id") or "").strip() or None,
        "opening_mode": mode,
        "public_intro": intro,
        "opening_attachments": _str_list(data.get("opening_attachments") or data.get("attachments")),
        "allow_enter_triggers": _as_bool(data.get("allow_enter_triggers"), True),
        "allow_model_continuation": _as_bool(data.get("allow_model_continuation"), True),
        "kp_truth": str(data.get("kp_truth") or "").strip(),
    }
    normalized.update({key: item for key, item in data.items() if key not in known})
    return normalized


@dataclass
class NarrativeNode:
    """§4.1 节点。定位树节点与资产节点共用同一结构。"""

    id: str
    kind: str
    title: str = ""
    parent: str | None = None
    order: int = 0
    summary: str = ""
    body: str = ""
    body_ref: dict[str, Any] = field(default_factory=dict)
    scene_role: str | None = None
    access: str = "free"
    repeatable: bool = True
    beats: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    completion: dict[str, Any] | None = None
    on_enter: list[dict[str, Any]] = field(default_factory=list)
    on_reenter: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    scope: str = "local"
    constant: bool = False
    keyword_weight: float = 1.0
    keyword_match: str = "word"
    visibility: str = VISIBILITY_PUBLIC
    kp_notes: str | None = None
    reveal_when: dict[str, Any] | None = None
    spoiler_level: int = 0
    token_cost: int = 0
    layout: dict[str, Any] = field(default_factory=dict)
    source: str = "import"
    content_hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "parent": self.parent,
            "order": self.order,
            "summary": self.summary,
            "body": self.body,
            "body_ref": dict(self.body_ref),
            "scene_role": self.scene_role,
            "access": self.access,
            "repeatable": self.repeatable,
            "beats": [dict(item) for item in self.beats],
            "objects": [dict(item) for item in self.objects],
            "completion": self.completion,
            "on_enter": [dict(item) for item in self.on_enter],
            "on_reenter": [dict(item) for item in self.on_reenter],
            "keywords": list(self.keywords),
            "aliases": list(self.aliases),
            "negative_keywords": list(self.negative_keywords),
            "scope": self.scope,
            "constant": self.constant,
            "keyword_weight": self.keyword_weight,
            "keyword_match": self.keyword_match,
            "visibility": self.visibility,
            "kp_notes": self.kp_notes,
            "reveal_when": self.reveal_when,
            "spoiler_level": self.spoiler_level,
            "token_cost": self.token_cost,
            "layout": dict(self.layout),
            "source": self.source,
            "content_hash": self.content_hash,
        }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NarrativeNode":
        known = {
            "id", "kind", "title", "parent", "order", "summary", "body", "body_ref",
            "scene_role", "access", "repeatable", "beats", "objects", "completion",
            "on_enter", "on_reenter", "keywords", "aliases", "negative_keywords",
            "scope", "constant", "visibility", "kp_notes", "reveal_when",
            "keyword_weight", "keyword_match",
            "spoiler_level", "token_cost", "layout", "source", "content_hash",
        }
        node_id = slugify_id(data.get("id") or data.get("node_id") or data.get("title"))
        kind = str(data.get("kind") or data.get("type") or "location").strip().lower()
        if kind not in NODE_KINDS:
            kind = "location"
        visibility = normalize_visibility(data.get("visibility"), VISIBILITY_PUBLIC)
        scene_role = data.get("scene_role")
        if kind == "ending":
            scene_role = "ending"
        elif scene_role not in SCENE_ROLES and scene_role is not None:
            scene_role = None
        access = str(data.get("access") or "free").strip().lower()
        if access not in ACCESS_MODES:
            access = "free"
        completion = data.get("completion")
        reveal_when = data.get("reveal_when")
        return cls(
            id=node_id,
            kind=kind,
            title=str(data.get("title") or data.get("name") or "").strip(),
            parent=str(data["parent"]).strip() if data.get("parent") not in (None, "") else None,
            order=_as_int(data.get("order"), 0),
            summary=str(data.get("summary") or data.get("description") or "").strip(),
            body=str(data.get("body") or data.get("content") or "").strip(),
            body_ref=dict(data.get("body_ref")) if isinstance(data.get("body_ref"), Mapping) else {},
            scene_role=scene_role,
            access=access,
            repeatable=_as_bool(data.get("repeatable"), True),
            beats=_dict_list(data.get("beats")),
            objects=_dict_list(data.get("objects")),
            completion=dict(completion) if isinstance(completion, Mapping) else None,
            on_enter=_dict_list(data.get("on_enter")),
            on_reenter=_dict_list(data.get("on_reenter")),
            keywords=_str_list(data.get("keywords")),
            aliases=_str_list(data.get("aliases")),
            negative_keywords=_str_list(data.get("negative_keywords")),
            scope=str(data.get("scope") or "local").strip().lower(),
            constant=_as_bool(data.get("constant")),
            keyword_weight=max(0.0, min(2.0, _as_float(data.get("keyword_weight"), 1.0))),
            keyword_match=(
                "substring"
                if str(data.get("keyword_match") or "word").strip().lower() == "substring"
                else "word"
            ),
            visibility=visibility,
            kp_notes=str(data.get("kp_notes") or "").strip() or None,
            reveal_when=dict(reveal_when) if isinstance(reveal_when, Mapping) else None,
            spoiler_level=max(0, _as_int(data.get("spoiler_level"), 0)),
            token_cost=max(0, _as_int(data.get("token_cost"), 0)),
            layout=dict(data.get("layout")) if isinstance(data.get("layout"), Mapping) else {},
            source=str(data.get("source") or "import"),
            content_hash=str(data.get("content_hash") or "") or None,
            extra={key: value for key, value in data.items() if key not in known},
        )


@dataclass
class NarrativeEdge:
    """§4.2 边。hard 边才在运行时拦人，soft 边只参与排序与提示。"""

    id: str
    from_id: str
    to_id: str
    type: str = "adjacent"
    hard: bool = False
    bidirectional: bool | None = None
    condition: dict[str, Any] | None = None
    label: str = ""
    visibility: str = VISIBILITY_PUBLIC
    kp_notes: str | None = None
    reveal_when: dict[str, Any] | None = None
    weight: float = 1.0
    evidence: dict[str, Any] | None = None
    confidence: float = 1.0
    source: str = "inferred"
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bidirectional is None:
            self.bidirectional = self.type in BIDIRECTIONAL_DEFAULT
        if not self.id:
            self.id = f"edge_{self.from_id}__{self.to_id}__{self.type}"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "from": self.from_id,
            "to": self.to_id,
            "type": self.type,
            "hard": self.hard,
            "bidirectional": bool(self.bidirectional),
            "condition": self.condition,
            "label": self.label,
            "visibility": self.visibility,
            "kp_notes": self.kp_notes,
            "reveal_when": self.reveal_when,
            "weight": self.weight,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "source": self.source,
        }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NarrativeEdge | None":
        from_id = slugify_id(data.get("from") or data.get("from_id"))
        to_id = slugify_id(data.get("to") or data.get("to_id"))
        if not from_id or not to_id:
            return None
        edge_type = str(data.get("type") or "adjacent").strip().lower()
        if edge_type not in EDGE_TYPES:
            edge_type = "mentions"
        visibility = normalize_visibility(data.get("visibility"), VISIBILITY_PUBLIC)
        condition = data.get("condition")
        reveal_when = data.get("reveal_when")
        evidence = data.get("evidence")
        known = {
            "id", "from", "to", "from_id", "to_id", "type", "hard", "bidirectional",
            "condition", "label", "visibility", "kp_notes", "reveal_when", "weight",
            "evidence", "confidence", "source",
        }
        synchronous = data.get("bidirectional")
        return cls(
            id=str(data.get("id") or "").strip(),
            from_id=from_id,
            to_id=to_id,
            type=edge_type,
            hard=_as_bool(data.get("hard"), str(data.get("source") or "") == "explicit"),
            bidirectional=_as_bool(synchronous) if synchronous is not None else None,
            condition=dict(condition) if isinstance(condition, Mapping) else None,
            label=str(data.get("label") or "").strip(),
            visibility=visibility,
            kp_notes=str(data.get("kp_notes") or "").strip() or None,
            reveal_when=dict(reveal_when) if isinstance(reveal_when, Mapping) else None,
            weight=_as_float(data.get("weight"), 1.0),
            evidence=dict(evidence) if isinstance(evidence, Mapping) else None,
            confidence=_as_float(data.get("confidence"), 1.0),
            source=str(data.get("source") or "inferred"),
            extra={key: value for key, value in data.items() if key not in known},
        )


@dataclass
class NarrativeBundle:
    """`narrative.json` 的完整内容。"""

    scenario_id: str = ""
    scenario_version: str = "1.0.0"
    schema_version: int = SCHEMA_VERSION
    topology: str = "hybrid"
    topology_confidence: float = 0.0
    topology_source: str = "auto"
    nodes: list[NarrativeNode] = field(default_factory=list)
    edges: list[NarrativeEdge] = field(default_factory=list)
    triggers: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)
    clocks: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    build_hash: str = ""
    generated_at: str = ""
    source_document_hash: str = ""
    start: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.topology not in TOPOLOGIES:
            self.topology = "hybrid"

    @property
    def node_map(self) -> dict[str, NarrativeNode]:
        return {node.id: node for node in self.nodes}

    def node(self, node_id: str | None) -> NarrativeNode | None:
        if not node_id:
            return None
        return self.node_map.get(str(node_id))

    def nodes_of(self, *kinds: str) -> list[NarrativeNode]:
        allow = set(kinds)
        return [node for node in self.nodes if node.kind in allow]

    @property
    def location_nodes(self) -> list[NarrativeNode]:
        return self.nodes_of("location")

    @property
    def ending_nodes(self) -> list[NarrativeNode]:
        return self.nodes_of("ending")

    def edges_of(self, *types: str) -> list[NarrativeEdge]:
        allow = set(types)
        return [edge for edge in self.edges if edge.type in allow]

    def out_edges(self, node_id: str) -> list[NarrativeEdge]:
        return [edge for edge in self.edges if edge.from_id == node_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "topology": self.topology,
            "topology_confidence": self.topology_confidence,
            "topology_source": self.topology_source,
            "build_hash": self.build_hash,
            "generated_at": self.generated_at,
            "source_document_hash": self.source_document_hash,
            "start": dict(self.start),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "triggers": [dict(item) for item in self.triggers],
            "flags": [dict(item) for item in self.flags],
            "clocks": [dict(item) for item in self.clocks],
            "assets": [dict(item) for item in self.assets],
            "warnings": [dict(item) for item in self.warnings],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NarrativeBundle":
        nodes = [NarrativeNode.from_dict(item) for item in _dict_list(data.get("nodes"))]
        node_ids = {node.id for node in nodes}
        edges: list[NarrativeEdge] = []
        for item in _dict_list(data.get("edges")):
            edge = NarrativeEdge.from_dict(item)
            if edge is None or edge.from_id not in node_ids or edge.to_id not in node_ids:
                continue
            edges.append(edge)
        topology = str(data.get("topology") or "hybrid").strip().lower()
        if topology not in TOPOLOGIES:
            topology = "hybrid"
        return cls(
            scenario_id=str(data.get("scenario_id") or "").strip(),
            scenario_version=str(data.get("scenario_version") or "1.0.0").strip(),
            schema_version=_as_int(data.get("schema_version"), SCHEMA_VERSION),
            topology=topology,
            topology_confidence=_as_float(data.get("topology_confidence"), 0.0),
            topology_source=str(data.get("topology_source") or "auto"),
            nodes=nodes,
            edges=edges,
            triggers=_dict_list(data.get("triggers")),
            flags=_dict_list(data.get("flags")),
            clocks=_dict_list(data.get("clocks")),
            assets=_dict_list(data.get("assets")),
            warnings=_dict_list(data.get("warnings")),
            build_hash=str(data.get("build_hash") or ""),
            generated_at=str(data.get("generated_at") or ""),
            source_document_hash=str(data.get("source_document_hash") or ""),
            start=normalize_start(
                data.get("start")
                if data.get("start") is not None
                else (data.get("meta") or {}).get("start") if isinstance(data.get("meta"), Mapping) else None
            ),
            meta=dict(data.get("meta")) if isinstance(data.get("meta"), Mapping) else {},
        )


def content_fingerprint(bundle: NarrativeBundle) -> str:
    """图谱内容的稳定指纹：忽略时间戳、局部布局与警告。"""
    payload = bundle.to_dict()
    for key in ("generated_at", "build_hash", "warnings"):
        payload.pop(key, None)
    for node in payload.get("nodes", []):
        if isinstance(node, dict):
            node.pop("layout", None)
    for edge in payload.get("edges", []):
        if isinstance(edge, dict):
            edge.pop("weight", None)
            edge.pop("confidence", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def collect_worldbook_entries(bundle: NarrativeBundle) -> list[dict[str, Any]]:
    """把节点上的关键词/可见性整理为世界书条目（供知识库与导出使用）。"""
    entries: list[dict[str, Any]] = []
    for node in bundle.nodes:
        if not (node.keywords or node.aliases or node.body or node.summary):
            continue
        entries.append(
            {
                "node_id": node.id,
                "title": node.title or node.id,
                "keywords": list(node.keywords),
                "aliases": list(node.aliases),
                "negative_keywords": list(node.negative_keywords),
                "scope": node.scope,
                "constant": node.constant,
                "keyword_weight": node.keyword_weight,
                "keyword_match": node.keyword_match,
                "visibility": node.visibility,
                "kp_notes": node.kp_notes,
                "reveal_when": node.reveal_when,
                "spoiler_level": node.spoiler_level,
                "kind": node.kind,
            }
        )
    return entries


def iter_node_ids(nodes: Iterable[NarrativeNode]) -> list[str]:
    return [node.id for node in nodes]
