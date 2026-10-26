"""剧本正文 → 章节 / 地点 / 物件 / 线索 / NPC / 结局（设计文档 §7.2-§7.4）。

只做确定性的结构抽取，不做自然语言推断、不调用模型。抽取结果用于建图；
无法确定的信息写进 `warnings`，留给作者复核。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from trpg_server.agents.narrative.model import (
    NarrativeEdge,
    NarrativeNode,
    VISIBILITY_HIDDEN,
    VISIBILITY_KP_ONLY,
    slugify_id,
)
from trpg_server.agents.narrative.visibility import classify_visibility, normalize_visibility
from trpg_server.scenario_documents import DocumentBlock, ParsedDocument

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_BRACKET_HEADING_RE = re.compile(r"^\s*[【《<](.{1,40})[》】>]\s*$")
_CN_HEADING_RE = re.compile(
    r"^\s*第\s*([0-9一二三四五六七八九十百]+)\s*([章节幕])\s*[、：:.\-—]?\s*(.*?)\s*$"
)
_AREA_HEADING_RE = re.compile(r"^\s*第\s*([0-9一二三四五六七八九十百]+)\s*(层|区域|甲板|楼层|街区)\s*[、：:.\-—]?\s*(.*?)\s*$")
_SC_HEADING_RE = re.compile(r"^\s*SC\s*[.\-]?\s*(\d+)\s*[、：:.\-—]?\s*(.*?)\s*$", re.I)
_SC_NOISE = {"检定", "理智", "成功", "失败", "大成功", "大失败", "san", "check", "roll"}

_ENDING_WORDS = ("结局", "ending", "true end", "bad end", "crazy end", "happy end", "normal end")
_ACT_WORDS = ("幕", "章", "chapter", "act")
_AREA_WORDS = ("层", "区域", "甲板", "楼层", "街区", "区", "area", "floor", "deck")
_LOCATION_WORDS = (
    "场景", "地点", "房间", "大厅", "房间", "馆", "室", "车厢", "驾驶室", "玄关",
    "走廊", "楼梯", "甲板", "仓库", "书房", "卧室", "厨房", "地下室", "教堂", "庭院",
    "location", "room", "scene", "hall", "area",
)
_BACKGROUND_WORDS = ("背景", "概要", "导入", "前言", "准备", "游戏准备", "公开信息", "时间线", "幕后黑手")
_MODULE_KIND = {
    "scene": "location",
    "ending": "ending",
    "background": "background",
    "opening": "background",
    "public_info": "public_info",
    "preparation": "background",
    "timeline": "background",
    "monster": "background",
    "npc": "background",
    "custom": "background",
}

_SKILL_WORDS = (
    "侦查", "图书馆使用", "聆听", "心理学", "潜行", "追踪", "急救", "医学", "神秘学",
    "历史", "话术", "魅惑", "恐吓", "闪避", "格斗", "射击", "攀爬", "跳跃", "游泳",
    "锁匠", "机械维修", "电气维修", "会计", "法律", "博物学", "导航", "驾驶",
)
_DIFFICULTY_WORDS = {"极难": "extreme", "困难": "hard", "普通": "regular"}
_OBJECT_WORDS = (
    "书架", "雕像", "尸体", "门", "窗", "钟", "桌子", "书桌", "抽屉", "柜子", "壁柜",
    "画", "画像", "镜子", "楼梯", "光幕", "箱子", "地毯", "壁炉", "电脑", "文件",
    "血迹", "祭坛", "石碑", "保险箱", "电话", "收音机", "电视", "床", "衣柜", "柜",
    "门把手", "灯", "烛台", "水槽", "冰箱", "墓碑", "井", "船桨", "舵", "控制台",
)
_OBJECT_HINT_RE = re.compile("|".join(_OBJECT_WORDS))
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.、)]|[（(]\d+[）)])\s*")
_OUTCOME_RE = re.compile(r"^\s*(大成功|成功|失败|大失败)\s*[：:]?\s*(.*)$")
_CLUE_RE = re.compile(r"线索\s*[：:]\s*([^\n。；;]{1,40})")
_CLUE_DISCOVER_RE = re.compile(r"(?:获得|找到|发现|拾取)[了]?[「“]([^」”]{1,40})[」”]")
_NPC_RE = re.compile(r"(?:NPC|npc|人物|角色)\s*[：:]\s*([^\n，,。；;（(]{1,20})")
_NPC_TAG_RE = re.compile(r"([\u4e00-\u9fff]{2,6})[（(]\s*NPC\s*[)）]", re.I)
_ALIAS_RE = re.compile(r"[（(]([^（）()]{1,20})[)）]")
_NEGATIVE_RE = re.compile(r"(?:不是|并非|而非|而不是)\s*([\u4e00-\u9fffA-Za-z0-9]{1,12})")
_TRUTH_RE = re.compile(r"实际上|事实上|真相是|他其实是|她其实是|伪装成|冒充|并不知道|误以为|真实目的是")

_GENERIC_TITLES = {"简介", "说明", "正文", "内容"}


@dataclass
class Section:
    id: str
    title: str
    kind: str
    level: int
    content: str
    order: int
    chapter_path: list[str] = field(default_factory=list)
    module_type: str = ""
    visibility: str | None = None


@dataclass
class ExtractedNarrative:
    sections: list[Section]
    nodes: list[NarrativeNode]
    edges: list[NarrativeEdge]
    warnings: list[dict[str, Any]]


def _document_from_text(text: str, filename: str = "script.txt") -> ParsedDocument:
    blocks: list[DocumentBlock] = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        info = _heading_info(line)
        blocks.append(
            DocumentBlock(
                text=info[1] if info else line.strip(),
                heading_level=info[0] if info else None,
            )
        )
    markdown = "\n\n".join(block.text for block in blocks)
    return ParsedDocument(filename=filename, markdown=markdown, blocks=blocks, page_count=None)


def _heading_info(text: str) -> tuple[int, str] | None:
    """返回 (标题层级, 标题文本)；纯正文返回 None。"""
    stripped = str(text or "").strip()
    if not stripped:
        return None
    match = _HEADING_RE.match(stripped)
    if match:
        return len(match.group(1)), match.group(2).strip()
    match = _CN_HEADING_RE.match(stripped)
    if match:
        title = f"第{match.group(1)}{match.group(2)} {match.group(3)}".strip()
        return 0, title
    match = _AREA_HEADING_RE.match(stripped)
    if match:
        title = f"第{match.group(1)}{match.group(2)} {match.group(3)}".strip()
        return 1, title
    match = _SC_HEADING_RE.match(stripped)
    if match and match.group(2):
        rest = match.group(2).strip()
        token = re.split(r"[：:\s]", rest, maxsplit=1)[0].strip().lower()
        if token and token not in _SC_NOISE:
            return 2, rest
    match = _BRACKET_HEADING_RE.match(stripped)
    if match:
        return 2, match.group(1).strip()
    # 短行 + 场景/区域词也视为标题（无 Markdown 样式的旧剧本）。
    if len(stripped) <= 24 and not re.search(r"[。！？!?]$", stripped):
        lowered = stripped.casefold()
        if any(word in lowered for word in _ENDING_WORDS):
            return 2, stripped
        if any(word in stripped for word in _AREA_WORDS) and ("第" in stripped or "层" in stripped):
            return 1, stripped
        if any(word in lowered for word in _BACKGROUND_WORDS):
            return 0, stripped
        if any(word in lowered for word in _LOCATION_WORDS) and "：" not in stripped:
            return 2, stripped
    return None


def _kind_for_title(title: str) -> str:
    lowered = str(title or "").casefold()
    if any(word in lowered for word in _ENDING_WORDS):
        return "ending"
    if any(word in lowered for word in _BACKGROUND_WORDS):
        return "background"
    if re.search(r"第\s*[0-9一二三四五六七八九十百]+\s*[章幕]", title) or any(
        word in lowered for word in _ACT_WORDS
    ):
        if not any(word in lowered for word in _LOCATION_WORDS):
            return "act"
    if re.search(r"第\s*[0-9一二三四五六七八九十百]+\s*(层|区域|甲板|楼层|街区)", title):
        return "area"
    if any(word in lowered for word in _AREA_WORDS) and not any(
        word in lowered for word in ("房间", "大厅", "室")
    ):
        return "area"
    if any(word in lowered for word in _LOCATION_WORDS):
        return "location"
    return "unknown"


def _level_for_kind(kind: str) -> int:
    return {"act": 0, "area": 1, "location": 2, "ending": 2}.get(kind, 2)


def detect_sections(document: ParsedDocument) -> list[Section]:
    """按标题把文档切成章节，并维护 act → area → location 的 chapter_path。"""
    sections: list[Section] = []
    current: Section | None = None
    stack: list[tuple[int, Section]] = []
    order = 0

    def _close() -> None:
        nonlocal current
        if current is not None:
            current.content = current.content.strip()
            sections.append(current)
            current = None

    for block in document.blocks:
        text = str(block.text or "").strip()
        if not text:
            continue
        if block.heading_level:
            markdown = _HEADING_RE.match(text)
            info = (block.heading_level, markdown.group(2).strip() if markdown else text)
        else:
            info = _heading_info(text)
        if info and info[1]:
            _close()
            title = str(info[1]).strip()
            kind = _kind_for_title(title)
            if kind == "unknown" and info[0] == 2:
                kind = "location"
            level = _level_for_kind(kind)
            while stack and stack[-1][0] >= level:
                stack.pop()
            chapter_path = [item[1].title for item in stack]
            current = Section(
                id=slugify_id(f"sec_{order}_{title}", prefix=f"sec_{order}"),
                title=title or "未命名章节",
                kind=kind,
                level=level,
                content="",
                order=order,
                chapter_path=chapter_path,
            )
            order += 1
            stack.append((level, current))
        elif current is not None:
            current.content = f"{current.content}\n\n{text}".strip()
        else:
            current = Section(
                id="sec_preamble", title="前言", kind="background", level=0,
                content=text, order=-1, chapter_path=[],
            )
    _close()
    return sections


def _unique_id(used: set[str], base: str) -> str:
    candidate = slugify_id(base)
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    candidate = f"{candidate}_{index}"
    used.add(candidate)
    return candidate


def _summary(content: str, limit: int = 120) -> str:
    for line in str(content or "").split("\n"):
        text = line.strip().lstrip("#").strip()
        if text:
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return ""


def _keywords(title: str, content: str, limit: int = 12) -> list[str]:
    result: list[str] = []
    title_text = str(title or "").strip()
    if title_text:
        result.append(title_text)
        cleaned = re.sub(r"第\s*[0-9一二三四五六七八九十百]+\s*[章节幕层区域甲板楼层街区]", "", title_text).strip()
        if cleaned and cleaned != title_text:
            result.append(cleaned)
    for name in re.findall(r"[「“]([^」”]{2,16})[」”]", content or ""):
        result.append(name)
    for word in _OBJECT_WORDS:
        if word in (content or "") and word not in result:
            result.append(word)
    unique: list[str] = []
    for item in result:
        if item and item not in unique:
            unique.append(item)
    return unique[:limit]


def _aliases(title: str) -> list[str]:
    values = [match.strip() for match in _ALIAS_RE.findall(title or "") if match.strip()]
    return list(dict.fromkeys(values))


def _negative_keywords(content: str) -> list[str]:
    return list(dict.fromkeys(match.strip() for match in _NEGATIVE_RE.findall(content or "") if match.strip()))


def _interaction_from_line(line: str, object_id: str) -> dict[str, Any] | None:
    skill = next((word for word in _SKILL_WORDS if word in line), None)
    if not skill:
        return None
    difficulty = "regular"
    for word, level in _DIFFICULTY_WORDS.items():
        if word in line:
            difficulty = level
            break
    return {
        "id": slugify_id(f"int_{object_id}_{skill}"),
        "skill": skill,
        "difficulty": difficulty,
        "repeatable": True,
        "outcomes": {"any": {"text": line.strip()}},
    }


def _parse_objects(section: Section) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (objects, beats)。成功/失败文本按标签分桶，绝不混写。"""
    objects: list[dict[str, Any]] = []
    beats: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    lines = [line.strip() for line in str(section.content or "").split("\n") if line.strip()]

    current_object: dict[str, Any] | None = None
    outcome_key: str | None = None
    for line in lines:
        bullet = bool(_BULLET_RE.match(line))
        body = _BULLET_RE.sub("", line).strip()
        outcome_match = _OUTCOME_RE.match(body)
        if outcome_match and current_object is not None:
            outcome_key = str(outcome_match.group(1) or "")
            text = outcome_match.group(2).strip()
            outcomes = current_object["interactions"][0]["outcomes"] if current_object["interactions"] else None
            if outcomes is not None:
                outcomes.pop("any", None)
                outcomes[_outcome_key(outcome_key)] = {"text": text}
            continue
        if outcome_match and current_object is None:
            continue

        # 已进入某个物件时，带技能的行优先当作该物件的交互，而不是新物件。
        if current_object is not None:
            head = re.split(r"[：:]", body, maxsplit=1)[0]
            if _interaction_from_line(body, current_object["id"]) and not _OBJECT_HINT_RE.search(head):
                current_object["interactions"].append(_interaction_from_line(body, current_object["id"]))
                outcome_key = None
                continue

        name = ""
        description = body
        colon = re.split(r"[：:]", body, maxsplit=1)
        if len(colon) == 2 and _OBJECT_HINT_RE.search(colon[0]):
            # 只有「物件名：描述」才建物件；正文里的物名不算。
            name, description = colon[0].strip(), colon[1].strip()
        elif bullet and _OBJECT_HINT_RE.search(body):
            match = _OBJECT_HINT_RE.search(body)
            if match:
                name = match.group(0)
        if name:
            object_id = _unique_id(used_ids, f"obj_{name}")
            current_object = {
                "id": object_id,
                "name": name,
                "visible": True,
                "summary": description if len(description) <= 120 else description[:119] + "…",
                "interactions": [],
                "visibility": "public",
            }
            interaction = _interaction_from_line(body, object_id)
            if interaction:
                current_object["interactions"].append(interaction)
            objects.append(current_object)
            outcome_key = None
            continue
        if current_object is not None:
            interactions = current_object.get("interactions") or []
            if interactions:
                interaction = _interaction_from_line(body, current_object["id"])
                if interaction:
                    interactions.append(interaction)
                    outcome_key = None
                    continue
                if outcome_key:
                    key = _outcome_key(outcome_key)
                    outcomes = interactions[0]["outcomes"]
                    outcomes.setdefault(key, {"text": ""})
                    outcomes[key]["text"] = f"{outcomes[key]['text']}\n{body}".strip()
                    continue
        if bullet:
            beat_id = _unique_id(used_ids, f"beat_{section.id}_{len(beats) + 1}")
            beats.append({"id": beat_id, "text": body, "required": False, "repeatable": False})
    return objects, beats


def _outcome_key(label: str) -> str:
    return {"大成功": "extreme_success", "成功": "success", "失败": "fail", "大失败": "fumble"}.get(label, "any")


def _build_node(
    section: Section,
    *,
    scenario_id: str,
    used_ids: set[str],
    parent: str | None,
    module_type: str = "",
    explicit_visibility: str | None = None,
) -> NarrativeNode:
    kind = section.kind
    node_id = _unique_id(used_ids, f"{kind}_{section.title}")
    keywords = _keywords(section.title, section.content)
    reveal_when = _extract_reveal_when(section.content)
    decision = classify_visibility(
        kind=kind,
        module_type=module_type or ("ending" if kind == "ending" else ""),
        title=section.title,
        summary=_summary(section.content),
        body=section.content,
        keywords=keywords,
        reveal_when=reveal_when,
        explicit=explicit_visibility,
    )
    objects, beats = _parse_objects(section) if kind == "location" else ([], [])
    scene_role = None
    if kind == "location":
        scene_role = "hub" if any(word in section.title for word in ("大厅", "枢纽", "前厅")) else "room"
    elif kind == "ending":
        scene_role = "ending"
    access = "free"
    if kind == "location" and re.search(r"需要.{0,12}(?:才能|才可|方可)|只有.{0,12}才能", section.content or ""):
        access = "gated"
    return NarrativeNode(
        id=node_id,
        kind=kind,
        title=section.title,
        parent=parent,
        order=section.order,
        summary=_summary(section.content),
        body=section.content[:4000],
        scene_role=scene_role,
        access=access,
        repeatable=True,
        beats=beats,
        objects=objects,
        keywords=keywords,
        aliases=_aliases(section.title),
        negative_keywords=_negative_keywords(section.content),
        visibility=decision.visibility,
        kp_notes=_kp_notes(section.content),
        reveal_when=reveal_when,
        spoiler_level=2 if decision.visibility == VISIBILITY_KP_ONLY else (1 if decision.visibility == VISIBILITY_HIDDEN else 0),
        source="import",
        extra={"visibility_confidence": round(decision.confidence, 2), "visibility_reasons": decision.reasons},
    )


def _extract_reveal_when(text: str) -> dict[str, Any] | None:
    """只从原文明确条件提取；提取不到就返回 None，不猜。"""
    content = str(text or "")
    clock = re.search(r"(?:累计|达到|满)\s*(\d+)\s*(?:点|层|格|分钟|小时)", content)
    if clock:
        return {"clock": "clock_progress", "gte": int(clock.group(1))}
    clue = re.search(r"(?:需要|必须|只有|找到|持有)\s*[「“]?([^」”，。,、；;\s]{2,20})[」”]?", content)
    if clue and any(word in content for word in ("钥匙", "线索", "密码", "证件", "徽章")):
        return {"clue": slugify_id(f"clue_{clue.group(1)}"), "label": clue.group(1)}
    return None


def _kp_notes(text: str) -> str | None:
    for line in str(text or "").split("\n"):
        if _TRUTH_RE.search(line):
            return line.strip()
    return None


def _section_module_type(section: Section) -> str:
    if section.kind == "ending":
        return "ending"
    if section.kind == "background":
        return "background"
    return ""


def _module_sections(modules: list[dict[str, Any]], used_titles: set[str]) -> list[Section]:
    sections: list[Section] = []
    order = 10000
    for module in modules:
        if not isinstance(module, dict):
            continue
        title = str(module.get("title") or module.get("name") or "").strip()
        content = str(module.get("content") or module.get("text") or module.get("summary") or "").strip()
        if not title or not content:
            continue
        module_type = str(module.get("module_type") or module.get("card_type") or "custom").strip().lower()
        kind = _MODULE_KIND.get(module_type, "background")
        if kind in {"location", "ending"} and title in used_titles:
            continue
        sections.append(
            Section(
                id=slugify_id(f"mod_{module.get('id') or title}", prefix=f"mod_{order}"),
                title=title,
                kind=kind,
                level=_level_for_kind(kind),
                content=content,
                order=order,
                chapter_path=[],
                module_type=module_type,
                visibility=normalize_visibility(module.get("visibility")) if module.get("visibility") else None,
            )
        )
        used_titles.add(title)
        order += 1
    return sections


def extract_narrative(
    document_or_text: ParsedDocument | str,
    *,
    scenario_id: str = "",
    source: str = "import",
    modules: list[dict[str, Any]] | None = None,
) -> ExtractedNarrative:
    document = (
        document_or_text
        if isinstance(document_or_text, ParsedDocument)
        else _document_from_text(str(document_or_text or ""))
    )
    sections = detect_sections(document)

    used_titles = {section.title for section in sections if section.kind in {"location", "ending"}}
    if modules:
        sections.extend(_module_sections(modules, used_titles))
    sections.sort(key=lambda item: (item.order, item.id))

    used_ids: set[str] = set()
    nodes: list[NarrativeNode] = []
    warnings: list[dict[str, Any]] = []
    section_node: dict[str, str] = {}
    stack: list[tuple[int, str]] = []

    for section in sections:
        level = _level_for_kind(section.kind)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        if section.kind in {"act", "area", "location", "ending"}:
            node = _build_node(
                section,
                scenario_id=scenario_id,
                used_ids=used_ids,
                parent=parent,
                module_type=section.module_type,
                explicit_visibility=section.visibility,
            )
            nodes.append(node)
            section_node[section.id] = node.id
            stack.append((level, node.id))
            for reason in node.extra.get("visibility_reasons", []):
                if reason == "hidden_without_reveal_when":
                    warnings.append(
                        {"code": "visibility.needs_review", "node_id": node.id, "reason": "hidden_without_reveal_when"}
                    )

    # 资产节点：线索 / NPC / 结局补充。
    location_nodes = [node for node in nodes if node.kind == "location"]
    for section in sections:
        owner_id = section_node.get(section.id)
        if section.kind not in {"location", "ending", "unknown", "background"}:
            owner_id = owner_id or (location_nodes[0].id if location_nodes else None)
        if section.kind in {"act", "area"}:
            continue
        parent = owner_id if owner_id in used_ids else (location_nodes[0].id if location_nodes else None)
        for name in _extract_clues(section.content):
            if name in _clue_ids(nodes):
                continue
            clue_id = _unique_id(used_ids, f"clue_{name}")
            decision = classify_visibility(
                kind="clue", title=name, summary=name, body=name,
                reveal_when=_extract_reveal_when(section.content), explicit=VISIBILITY_HIDDEN,
            )
            nodes.append(
                NarrativeNode(
                    id=clue_id, kind="clue", title=name, parent=parent, summary=name,
                    keywords=[name], visibility=decision.visibility, reveal_when=_extract_reveal_when(section.content),
                    spoiler_level=1, source=source,
                )
            )
            warnings.append({"code": "visibility.needs_review", "node_id": clue_id, "reason": "clue_reveal_when_unresolved"})
        for name in _extract_npcs(section.content):
            npc_id = _unique_id(used_ids, f"npc_{name}")
            decision = classify_visibility(kind="npc", title=name, summary=name, body=section.content)
            nodes.append(
                NarrativeNode(
                    id=npc_id, kind="npc", title=name, parent=parent, summary=name, keywords=[name],
                    visibility=decision.visibility, kp_notes=_kp_notes(section.content), source=source,
                )
            )

    return ExtractedNarrative(sections=sections, nodes=nodes, edges=[], warnings=warnings)


def _extract_clues(content: str) -> list[str]:
    names: list[str] = []
    for pattern in (_CLUE_RE, _CLUE_DISCOVER_RE):
        for match in pattern.findall(content or ""):
            name = str(match).strip().strip("「」“”")
            if name and name not in names:
                names.append(name)
    return names


def _clue_ids(nodes: list[NarrativeNode]) -> set[str]:
    return {node.title for node in nodes if node.kind == "clue"}


def _extract_npcs(content: str) -> list[str]:
    names: list[str] = []
    for match in _NPC_RE.findall(content or ""):
        name = str(match).strip()
        if name and name not in names:
            names.append(name)
    for match in _NPC_TAG_RE.findall(content or ""):
        name = str(match).strip()
        if name and name not in names:
            names.append(name)
    return names
