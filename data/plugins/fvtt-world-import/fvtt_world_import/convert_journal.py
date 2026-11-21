"""FVTT journal / folders → 剧本场景文本条目 + 世界树图谱数据（纯函数）。

第四刀（M1 收官）：``data/journal``（JournalEntry + pages）与 ``data/folders``
（章节层级）→ 两份产物：

- ``modules``：剧本模块数组（``scenario.json`` 的 modules 形态，
  ``module_type=scene`` 的场景文本条目；FVTT 玩家可读页 → ``visibility``
  ``public``，其余 → ``kp``，玩家侧拿不到 KP 材料）；
- ``bundle``：``narrative.json`` 形态的图谱数据（nodes/edges 普通字典，由
  端点层经 ``NarrativeBundle.from_dict`` 归一化后落盘——可见性三态与悬空
  边校验都吃现有模型，不另写一套）。

确定性规则（不依赖任何 AI）：

- ``folders(type=JournalEntry)`` 层级 → ``act``（根）/``area``（嵌套）章节节点；
- journal 条目 → ``area`` 节点；页 → 内容节点（检定散文 → ``clue``，其余
  ``concept``）；场景 → ``location`` 锚点节点（有 journal 产物才建）；
- ownership（§5.3 接 §4.6 可见性模型）：页显式 ``ownership.default>=0`` 用
  页面级，否则继承条目级；FVTT OWNER/OBSERVER（≥1，可读）→ ``public``、
  LIMITED（0，只见标题）→ ``hidden``、NONE（-1，玩家侧不存在）→ ``kp_only``；
- 拓扑：章节线性（sort 序 ``next`` 链）+ ``contains`` 包含边 + 场景挂点
  （页文本/标题提到场景名 → ``mentions`` 边）。

AI 辅助语义切块（§5.3 已拍板，可选注入、三选一可关）：

- ``chunk_page(page)``：一页 → 场景/线索/遭遇/DM 私注四类块；**块文本必须
  逐字保留原文**（子串校验不过 → 整页降级为确定性单块，DC 检定散文绝不
  因 AI 改写而丢失）；
- ``summarize(title, text)``：节点摘要 marker；
- ``infer_edges(node_rows)``：边推断（端点白名单校验在回调侧，这里再兜一层）。

任何回调缺席 / 抛错 / 返回非法 → 逐项确定性降级 + 稳定 code 告警，
导入永不因 AI 失败而中断。本模块不落任何数据、不 import 核心或规则集
插件（图谱落盘在端点层走 ``narrative.store``）。

落盘契约备注：``narrative.json`` 的节点/边把非模型字段平铺在条目顶层
（``NarrativeNode.to_dict`` 的 ``payload.update(extra)`` 语义），所以
``fvtt_*`` 溯源标记直接写在节点顶层，不要包 ``extra`` 子对象。
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Callable, Mapping

# -- 上限（防解析型滥用；超限截断并告警，不拒收整包） ------------------------

#: 单页纯文本上限：超长截断 + 告警（DC 检定散文按原文保留到上限为止）。
MAX_PAGE_CHARS = 20000
#: 节点 body 上限（对齐 §4.1 的运行时注入预算）。
MAX_BODY_CHARS = 4000
#: 模块 content 上限（scenario.json 的场景文本条目）。
MAX_CONTENT_CHARS = 12000
#: 摘要 marker 上限（对齐生成向导的 80 字口径）。
MAX_SUMMARY_CHARS = 80
#: AI 摘要调用预算（切块节点 + 模块 marker 合计；超出走确定性摘要）。
MAX_SUMMARY_CALLS = 40
#: 单页切块数上限：AI 切块超限截断。
MAX_CHUNKS_PER_PAGE = 8
#: 参与转换的页数上限。
MAX_PAGES = 300
#: AI 边推断的节点行数上限（超出跳过推断，只保留确定性边）。
MAX_EDGE_INFERENCE_NODES = 40
#: 参与的场景锚点数上限。
MAX_SCENE_ANCHORS = 30
#: AI 切块覆盖下限（占页正文字符比例，不足 → 整页确定性降级）。
MIN_CHUNK_COVERAGE = 0.6

#: 与 ``agents/narrative/model.EDGE_TYPES`` 逐位对齐（插件不 import 核心，
#: 改那边时同步这里）。
EDGE_TYPES = (
    "adjacent", "portal", "next", "leads_to", "requires", "reveals",
    "knows", "inhabits", "uses", "contains", "triggers", "conflicts",
    "mentions", "alias", "relationship",
)

#: 可见性三态（``narrative.model`` 的口径；字符串与核心常量一致）。
VISIBILITY_PUBLIC = "public"
VISIBILITY_HIDDEN = "hidden"
VISIBILITY_KP_ONLY = "kp_only"
_VISIBILITY_RANK = {VISIBILITY_PUBLIC: 0, VISIBILITY_HIDDEN: 1, VISIBILITY_KP_ONLY: 2}

#: FVTT ownership.default：-1 NONE / 0 LIMITED / 1 OBSERVER / 2 OWNER。
_OWNERSHIP_LEVELS = (-1, 0, 1, 2, 3)

#: 检定散文标记（确定性节点分类：命中 → clue 节点）。
_CHECK_RE = re.compile(r"DC\s*\d+|检定|豁免")

_BLOCK_TAGS = re.compile(
    r"</?\s*(p|div|section|article|h[1-6]|li|ul|ol|table|tr|blockquote|br)\s*/?>",
    re.IGNORECASE,
)
#: 引用块哨兵：blockquote 开合先替换成哨兵行，块内行统一加「> 」前缀
#: （剧本旁白/谜语朗读段保持可读），避免标签被剥掉后语义丢失。哨兵行
#: 自身从产物中剔除；不能用空白类控制字符当哨兵——``str.strip()`` 会剥掉。
_QUOTE_OPEN_RE = re.compile(r"<\s*blockquote[^>]*>", re.IGNORECASE)
_QUOTE_CLOSE_RE = re.compile(r"<\s*/\s*blockquote\s*>", re.IGNORECASE)
#: 列表项同理：``<li>`` 的符号在标签里，开标签先替换成字面圆点行，
#: 行循环里统一转成 ``- `` 前缀。
_LI_OPEN_RE = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_QUOTE_MARK = "jrn-quote-open"
_ENDQUOTE_MARK = "jrn-quote-close"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+")
_LIST_ITEM_RE = re.compile(r"^[-•·]\s*")

#: 本刀产物节点 id 前缀（端点层幂等合并旧 bundle 时据此剔除上一轮产物）。
JOURNAL_NODE_PREFIXES = ("fvtt-folder-", "fvtt-journal-", "fvtt-scene-")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ownership_level(value: Any) -> int | None:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return level if level in _OWNERSHIP_LEVELS else None


def _slug(value: Any, prefix: str = "node") -> str:
    text = _ID_SAFE_RE.sub("_", str(value or "").strip()).strip("_")
    return text or prefix


def _sort_value(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("sort") or 0)
    except (TypeError, ValueError):
        return 0.0


def is_journal_scoped(node_row: Mapping[str, Any]) -> bool:
    """端点层合并旧 bundle 用：判断一个 narrative 节点是否本刀产物。

    按 id 前缀（确定性幂等 id）或 ``fvtt_*`` 溯源标记识别——重导时旧产物
    整体替换，非 journal 节点（作者手写 / 其他导入刀）原样保留。
    """
    node_id = str(node_row.get("id") or "")
    if node_id.startswith(JOURNAL_NODE_PREFIXES):
        return True
    return any(
        str(node_row.get(key) or "").strip()
        for key in ("fvtt_page_id", "fvtt_scene_id", "fvtt_folder_id", "fvtt_journal_entry_id")
    )


def page_visibility(entry: Mapping[str, Any], page: Mapping[str, Any]) -> tuple[str, int]:
    """FVTT ownership → 世界树三态；返回 (visibility, effective_level)。

    页显式配置了 ``ownership.default``（数值且 ≥0）用页面级；未配置（缺省
    -1 / 非数值）继承条目级——实测两包页级都是 -1 + 仅 GM 用户授权，条目级
    default（0 vs 2）才是作者意图的判别位。条目级缺省按 FVTT 新建 journal
    的默认 LIMITED(0) 处理（fail 向「内容封锁」一侧，不会把 KP 材料放行）。
    """
    entry_level = _ownership_level(_mapping(entry.get("ownership")).get("default"))
    if entry_level is None:
        entry_level = 0
    page_level = _ownership_level(_mapping(page.get("ownership")).get("default"))
    level = page_level if (page_level is not None and page_level >= 0) else entry_level
    if level >= 1:
        return VISIBILITY_PUBLIC, level
    if level == 0:
        return VISIBILITY_HIDDEN, level
    return VISIBILITY_KP_ONLY, level


def page_plain_text(page: Mapping[str, Any]) -> str:
    """JournalEntryPage 的 ``text.content``（HTML）→ 可读纯文本。

    只做结构保真转换（块级标签 → 换行、引用块 → ``> `` 前缀、行内标签剔除、
    实体反转义），**不改写任何正文**——``DC 14``、检定话术等散文逐字保留
    （§5.2 的检定线索要求）。非文本页（image/pdf/video）的正文按空处理。
    """
    if str(page.get("type") or "text").strip().lower() != "text":
        return ""
    raw = str(_mapping(page.get("text")).get("content") or "")
    if not raw.strip():
        return ""
    text = _QUOTE_OPEN_RE.sub(f"\n{_QUOTE_MARK}\n", raw)
    text = _QUOTE_CLOSE_RE.sub(f"\n{_ENDQUOTE_MARK}\n", text)
    text = _LI_OPEN_RE.sub("\n· ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    cleaned: list[str] = []
    in_quote = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == _QUOTE_MARK:
            in_quote = True
            continue
        if line == _ENDQUOTE_MARK:
            in_quote = False
            continue
        if in_quote:
            cleaned.append("> " + line)
        elif _LIST_ITEM_RE.match(line):
            cleaned.append("- " + _LIST_ITEM_RE.sub("", line))
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def _marker(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """确定性摘要 marker：压平空白后按句读边界截断。"""
    flat = _WS_RE.sub(" ", text or "").strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in ("。", "！", "？", "；", ".", "!", "?", ";"):
        pos = cut.rfind(sep)
        if pos > 12:
            return cut[: pos + 1]
    return cut


def _dedupe_edges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (from, to, type) 去重：确定性边优先（AI 边不覆盖结构边）。"""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("from") or ""), str(row.get("to") or ""), str(row.get("type") or ""))
        if key in seen or not all(key):
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _edge_visibility(nodes: list[dict[str, Any]], from_id: str, to_id: str) -> str:
    """边可见性取两端节点中更紧的一侧（fail-closed，防隐性泄露）。"""
    rank = 0
    for node in nodes:
        if node.get("id") in (from_id, to_id):
            rank = max(rank, _VISIBILITY_RANK.get(str(node.get("visibility") or VISIBILITY_PUBLIC), 2))
    if rank == 0:
        return VISIBILITY_PUBLIC
    return VISIBILITY_HIDDEN if rank == 1 else VISIBILITY_KP_ONLY


def convert_world_journal(
    index: Any,
    *,
    world_title: str = "",
    chunk_page: Callable[[Mapping[str, Any]], list[dict[str, Any]]] | None = None,
    summarize: Callable[[str, str], str] | None = None,
    infer_edges: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """整包 journal/folders → ``{modules, bundle, warnings, degradations, totals}``。

    ``index`` 是 :class:`fvtt_world_import.world_package.JournalIndex`
    （条目 + 复合键页 + JournalEntry 文件夹 + 场景名）。空库（sixian 形态）
    返回零长产物 + 空库警告，调用方如实汇报「0 条」，不视为错误。
    """
    entries: dict[str, dict[str, Any]] = dict(getattr(index, "entries") or {})
    pages_by_entry: dict[str, list[dict[str, Any]]] = {
        str(key): list(value) for key, value in (getattr(index, "pages") or {}).items()
    }
    folder_docs: dict[str, dict[str, Any]] = dict(getattr(index, "folders") or {})
    scene_names: dict[str, str] = dict(getattr(index, "scene_names") or {})
    scene_active: set[str] = set(getattr(index, "scene_active") or [])

    warnings: list[dict[str, str]] = []
    degradations: dict[str, int] = {}

    def warn(code: str, message: str, level: str = "warning") -> None:
        warnings.append({"code": code, "level": level, "message": message})

    def degrade(code: str, message: str) -> None:
        degradations[code] = degradations.get(code, 0) + 1
        warn(code, message)

    # -- AI 摘要预算（有界：切块节点 + 模块 marker 合计）---------------------

    summary_calls = [0]
    summary_budget_warned = [False]

    def node_summary(title: str, text: str) -> str:
        """marker 摘要：AI 回调优先，缺席/失败/超预算 → 确定性截断。"""
        if summarize is None:
            return _marker(text)
        if summary_calls[0] >= MAX_SUMMARY_CALLS:
            if not summary_budget_warned[0]:
                summary_budget_warned[0] = True
                warn(
                    "journal.ai_summary_budget",
                    f"AI 摘要调用超过 {MAX_SUMMARY_CALLS} 次预算，其余节点用确定性摘要。",
                )
            return _marker(text)
        summary_calls[0] += 1
        try:
            value = summarize(title, text[:2000])
        except Exception:  # noqa: BLE001 - 回调失败 → 确定性降级，绝不阻塞
            degrade("journal.ai_summary_failed", f"节点「{title}」AI 摘要失败，已用确定性摘要。")
            return _marker(text)
        marker = _marker(str(value or ""))
        return marker or _marker(text)

    # -- 章节（folders）层级 → act / area 节点 -------------------------------

    def folder_depth(folder_id: str) -> int:
        """向根数层数（环 / 悬空 parent 按 0 层处理）。"""
        depth = 0
        seen = {folder_id}
        current = folder_id
        while True:
            parent = str(folder_docs.get(current, {}).get("folder") or "")
            if not parent or parent not in folder_docs or parent in seen:
                return depth
            depth += 1
            seen.add(parent)
            current = parent

    def folder_chain(folder_id: str) -> list[str]:
        """folder → 根链（含自身），根在前、直接父在后。"""
        chain: list[str] = []
        seen = {folder_id}
        current = folder_id
        while True:
            chain.append(current)
            parent = str(folder_docs.get(current, {}).get("folder") or "")
            if not parent or parent not in folder_docs or parent in seen:
                return chain
            seen.add(parent)
            current = parent

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    folder_node_ids: dict[str, str] = {}

    def folder_node_for(folder_id: str, order: int) -> str:
        node_id = folder_node_ids.get(folder_id)
        if node_id:
            return node_id
        doc = folder_docs[folder_id]
        name = str(doc.get("name") or folder_id).strip()
        node_id = f"fvtt-folder-{_slug(folder_id, 'folder')}"
        depth = folder_depth(folder_id)
        parent_id = folder_node_for(str(doc.get("folder") or ""), order) if depth > 0 else None
        folder_node_ids[folder_id] = node_id
        nodes.append(
            {
                "id": node_id,
                "kind": "act" if depth == 0 else "area",
                "title": name,
                "parent": parent_id,
                "order": order,
                "summary": _marker(name, 40),
                "body": "",
                "visibility": VISIBILITY_PUBLIC,
                "keywords": [name] if name else [],
                "source": "import",
                "fvtt_folder_id": folder_id,
                "fvtt_kind": "folder",
            }
        )
        return node_id

    def folder_sort_key(folder_id: str) -> tuple[float, str]:
        return (_sort_value(folder_docs[folder_id]), str(folder_docs[folder_id].get("name") or folder_id))

    root_folders = sorted(
        (fid for fid in folder_docs if folder_depth(fid) == 0),
        key=folder_sort_key,
    )
    for order, folder_id in enumerate(root_folders):
        folder_node_for(folder_id, order)

    # -- 条目按 folder 分组；排序 (sort, name) ---------------------------------

    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for entry_id, entry in entries.items():
        grouped.setdefault(str(entry.get("folder") or ""), []).append((entry_id, dict(entry)))
    for bucket in grouped.values():
        bucket.sort(key=lambda pair: (_sort_value(pair[1]), str(pair[1].get("name") or pair[0])))

    # 无 folder 的条目挂隐式根章节（世界名兜底）——只在没有任何真实 JournalEntry
    # 文件夹时才建，避免凭空多一层。
    implicit_root_id = ""
    if grouped.get("") and not any(
        str(entry.get("folder") or "") in folder_docs for entry in entries.values()
    ):
        implicit_title = str(world_title or "Journal 章节").strip()
        implicit_root_id = f"fvtt-folder-{_slug(implicit_title, 'root')}"
        nodes.append(
            {
                "id": implicit_root_id,
                "kind": "act",
                "title": implicit_title,
                "parent": None,
                "order": len(root_folders),
                "summary": _marker(implicit_title, 40),
                "body": "",
                "visibility": VISIBILITY_PUBLIC,
                "keywords": [implicit_title] if implicit_title else [],
                "source": "import",
                "fvtt_kind": "implicit_root",
            }
        )

    def chapter_for(folder_key: str) -> list[str]:
        """条目的章节链：[根 act, ..., 直接父]；无章节 → 空链（条目裸挂）。"""
        if folder_key and folder_key in folder_docs:
            chain = list(reversed(folder_chain(folder_key)))
            return [folder_node_for(fid, 0) for fid in chain]
        if implicit_root_id:
            return [implicit_root_id]
        return []

    # -- 场景锚点：有 journal 产物才建（§5.3 的确定性挂点）-------------------

    page_total = sum(len(rows) for rows in pages_by_entry.values())
    anchor_ids: dict[str, str] = {}
    if page_total and scene_names:
        for scene_id, name in sorted(scene_names.items(), key=lambda pair: pair[1]):
            if len(anchor_ids) >= MAX_SCENE_ANCHORS:
                degrade(
                    "journal.scene_anchors_truncated",
                    f"场景锚点超过 {MAX_SCENE_ANCHORS} 个上限，多余场景不建图谱节点。",
                )
                break
            node_id = f"fvtt-scene-{_slug(scene_id, 'scene')}"
            anchor_ids[scene_id] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "kind": "location",
                    "title": name,
                    "parent": None,
                    "order": len(nodes),
                    "summary": _marker(name, 40),
                    "body": "",
                    "visibility": VISIBILITY_PUBLIC,
                    "keywords": [name],
                    "source": "import",
                    "node_role": "site",
                    "fvtt_scene_id": scene_id,
                    "fvtt_active": scene_id in scene_active,
                    "fvtt_kind": "scene_anchor",
                }
            )

    # -- 切块（AI 可选；缺席/失败 → 整页确定性单块）--------------------------

    def chunk_rows(entry_name: str, page_id: str, plain: str) -> list[dict[str, Any]]:
        fallback = [
            {
                "kind": "clue" if _CHECK_RE.search(plain) else "scene",
                "title": "",
                "text": plain,
            }
        ]
        if chunk_page is None:
            return fallback
        payload = {
            "id": page_id,
            "name": str(page_names.get(page_id) or ""),
            "entry": entry_name,
            "text": plain[:MAX_PAGE_CHARS],
        }
        try:
            rows = chunk_page(payload)
        except Exception:  # noqa: BLE001 - 回调失败 → 确定性降级，绝不阻塞
            degrade(
                "journal.ai_chunk_failed",
                f"journal 页「{payload['name']}」AI 切块失败，已按整页确定性切块。",
            )
            return fallback
        if not isinstance(rows, list) or not rows:
            return fallback
        collapsed = _WS_RE.sub("", plain)
        kept: list[dict[str, Any]] = []
        for row in rows[:MAX_CHUNKS_PER_PAGE]:
            if not isinstance(row, Mapping):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            # 逐字保留校验：块文本必须是页原文的子串（压平空白比较）。
            if _WS_RE.sub("", text) not in collapsed:
                degrade(
                    "journal.ai_chunk_not_verbatim",
                    f"journal 页「{payload['name']}」有切块文本不是原文子串，已丢弃该块。",
                )
                continue
            kind = str(row.get("kind") or "scene").strip().lower()
            if kind not in {"scene", "clue", "encounter", "kp_note"}:
                kind = "scene"
            kept.append(
                {
                    "kind": kind,
                    "title": str(row.get("title") or "").strip(),
                    "text": text,
                }
            )
        if not kept:
            return fallback
        covered = sum(len(_WS_RE.sub("", row["text"])) for row in kept)
        if collapsed and covered < len(collapsed) * MIN_CHUNK_COVERAGE:
            degrade(
                "journal.ai_chunk_coverage",
                f"journal 页「{payload['name']}」AI 切块覆盖不足 {int(MIN_CHUNK_COVERAGE * 100)}%，已按整页确定性切块。",
            )
            return fallback
        if len(rows) > MAX_CHUNKS_PER_PAGE:
            degrade(
                "journal.ai_chunk_truncated",
                f"journal 页「{payload['name']}」切块超过 {MAX_CHUNKS_PER_PAGE} 块上限，已截断。",
            )
        return kept

    # -- 页 → 节点 / 模块 ----------------------------------------------------

    modules: list[dict[str, Any]] = []
    page_names: dict[str, str] = {}
    for rows in pages_by_entry.values():
        for row in rows:
            page_names[str(row.get("_id") or "")] = str(row.get("name") or "")

    processed_pages = 0
    chunk_nodes = 0
    for folder_key in sorted(grouped.keys()):
        bucket = grouped.get(folder_key) or []
        if not bucket:
            continue
        chapter_ids = chapter_for(folder_key)
        for chain_from, chain_to in zip(chapter_ids, chapter_ids[1:]):
            edges.append({"from": chain_from, "to": chain_to, "type": "contains", "label": "章节层级"})

        previous_area = ""
        for entry_id, entry in bucket:
            entry_name = str(entry.get("name") or entry_id).strip()
            area_id = f"fvtt-journal-{_slug(entry_id, 'entry')}"
            nodes.append(
                {
                    "id": area_id,
                    "kind": "area",
                    "title": entry_name,
                    "parent": chapter_ids[-1] if chapter_ids else None,
                    "order": len(nodes),
                    "summary": _marker(entry_name, 40),
                    "body": "",
                    "visibility": VISIBILITY_PUBLIC,
                    "keywords": [entry_name] if entry_name else [],
                    "source": "import",
                    "fvtt_journal_entry_id": entry_id,
                    "fvtt_kind": "journal_entry",
                }
            )
            if chapter_ids:
                edges.append(
                    {"from": chapter_ids[-1], "to": area_id, "type": "contains", "label": "章节包含条目"}
                )
            if previous_area:
                edges.append({"from": previous_area, "to": area_id, "type": "next", "label": "同章节条目顺序"})
            previous_area = area_id

            rows = pages_by_entry.get(entry_id) or []
            if not rows:
                continue
            rows = sorted(rows, key=lambda row: (_sort_value(row), str(row.get("name") or "")))
            previous_page = ""
            for row in rows:
                processed_pages += 1
                if processed_pages > MAX_PAGES:
                    degrade(
                        "journal.pages_truncated",
                        f"journal 页超过 {MAX_PAGES} 个上限，其余页未导入。",
                    )
                    break
                raw_plain = page_plain_text(row)
                plain = raw_plain[:MAX_PAGE_CHARS]
                if len(raw_plain) > MAX_PAGE_CHARS:
                    degrade(
                        "journal.page_truncated",
                        f"journal 页「{row.get('name') or row.get('_id')}」正文超 {MAX_PAGE_CHARS} 字上限，已截断。",
                    )
                if not plain:
                    degrade(
                        "journal.page_empty",
                        f"journal 页「{row.get('name') or row.get('_id')}」没有正文，未产出场景文本。",
                    )
                    continue
                page_id = str(row.get("_id") or "")
                page_title = str(row.get("name") or "").strip() or entry_name
                visibility, level = page_visibility(entry, row)
                chunks = chunk_rows(entry_name, page_id, plain)

                previous_chunk = ""
                single_summary = ""
                for chunk_index, chunk in enumerate(chunks, start=1):
                    chunk_kind = str(chunk.get("kind") or "scene")
                    chunk_text = str(chunk.get("text") or "").strip()
                    chunk_title = str(chunk.get("title") or "").strip()
                    base_title = chunk_title or page_title
                    if len(chunks) == 1:
                        node_id = f"fvtt-journal-{_slug(page_id, 'page')}"
                        node_title = base_title
                    else:
                        node_id = f"fvtt-journal-{_slug(page_id, 'page')}-c{chunk_index}"
                        node_title = f"{base_title}（{chunk_index}）"
                    if chunk_kind == "kp_note":
                        # DM 私注：正文进 kp_notes（玩家侧永不出现），body 留空。
                        chunk_visibility = VISIBILITY_KP_ONLY
                        node_body = ""
                        node_kp_notes = chunk_text[:MAX_BODY_CHARS]
                        node_kind = "concept"
                    else:
                        chunk_visibility = visibility
                        node_body = chunk_text[:MAX_BODY_CHARS]
                        node_kp_notes = (
                            f"FVTT journal 条目「{entry_name}」（ownership.default={level}）导入门注。"
                            if visibility != VISIBILITY_PUBLIC
                            else None
                        )
                        node_kind = "clue" if chunk_kind == "clue" else "concept"
                    summary = node_summary(node_title, chunk_text)
                    if len(chunks) == 1:
                        single_summary = summary
                    chunk_nodes += 1
                    nodes.append(
                        {
                            "id": node_id,
                            "kind": node_kind,
                            "title": node_title,
                            "parent": area_id,
                            "order": chunk_index,
                            "summary": summary,
                            "body": node_body,
                            "visibility": chunk_visibility,
                            "kp_notes": node_kp_notes,
                            "keywords": [base_title] if base_title else [],
                            "source": "import",
                            "fvtt_page_id": page_id,
                            "fvtt_journal_entry": entry_name,
                            "chunk_kind": chunk_kind,
                            "fvtt_kind": "journal_page",
                        }
                    )
                    edges.append(
                        {"from": area_id, "to": node_id, "type": "contains", "label": "journal 页归属"}
                    )
                    if previous_chunk:
                        edges.append(
                            {"from": previous_chunk, "to": node_id, "type": "next", "label": "页内块顺序"}
                        )
                    previous_chunk = node_id

                    # 场景挂点：页正文/标题提到场景名 → mentions 边。
                    for scene_id, scene_name in scene_names.items():
                        if scene_id not in anchor_ids or not scene_name:
                            continue
                        if scene_name in chunk_text or scene_name in base_title:
                            edges.append(
                                {
                                    "from": node_id,
                                    "to": anchor_ids[scene_id],
                                    "type": "mentions",
                                    "label": f"文本提到场景「{scene_name}」",
                                    "visibility": chunk_visibility,
                                }
                            )
                if previous_page and previous_chunk:
                    edges.append(
                        {"from": previous_page, "to": previous_chunk, "type": "next", "label": "条目内页顺序"}
                    )
                previous_page = previous_chunk or previous_page

                # 场景文本条目（模块）：整页原文，DC 检定散文逐字保留。
                modules.append(
                    {
                        "id": f"fvtt-journal-{_slug(page_id, 'page')}",
                        "module_type": "scene",
                        "title": page_title,
                        "summary": (single_summary or node_summary(page_title, plain)),
                        "content": plain[:MAX_CONTENT_CHARS],
                        "visibility": "public" if visibility == VISIBILITY_PUBLIC else "kp",
                        "send_to_ai": True,
                        "fvtt_page_id": page_id,
                        "fvtt_journal_entry": entry_name,
                        "notes": (
                            f"FVTT journal「{entry_name}」页 {page_id}；"
                            f"ownership.default={level} → {visibility}。"
                        ),
                    }
                )

    # -- AI 边推断（可选；端点白名单校验在回调侧，这里再兜一层） --------------

    ai_edge_count = 0
    if infer_edges is not None and 0 < len(nodes) <= MAX_EDGE_INFERENCE_NODES:
        node_rows = [
            {
                "id": node["id"],
                "kind": node["kind"],
                "title": node["title"],
                "summary": node["summary"],
                "visibility": node["visibility"],
            }
            for node in nodes
        ]
        try:
            raw_edges = infer_edges(node_rows)
        except Exception:  # noqa: BLE001
            degrade("journal.ai_edges_failed", "AI 边推断失败，已只保留确定性结构边。")
            raw_edges = None
        if isinstance(raw_edges, list):
            node_ids = {node["id"] for node in nodes}
            structural = {
                (str(row.get("from") or ""), str(row.get("to") or ""), str(row.get("type") or ""))
                for row in edges
            }
            for row in raw_edges:
                if not isinstance(row, Mapping):
                    continue
                from_id = str(row.get("from") or row.get("from_id") or "").strip()
                to_id = str(row.get("to") or row.get("to_id") or "").strip()
                edge_type = str(row.get("type") or "mentions").strip().lower()
                if from_id not in node_ids or to_id not in node_ids or from_id == to_id:
                    continue
                if edge_type not in EDGE_TYPES:
                    edge_type = "mentions"
                if (from_id, to_id, edge_type) in structural:
                    continue
                edges.append(
                    {
                        "from": from_id,
                        "to": to_id,
                        "type": edge_type,
                        "label": str(row.get("label") or "").strip(),
                        "visibility": _edge_visibility(nodes, from_id, to_id),
                        "source": "ai",
                        "confidence": 0.6,
                    }
                )
                ai_edge_count += 1
        elif raw_edges is not None:
            degrade("journal.ai_edges_invalid", "AI 边推断返回非法结构，已只保留确定性结构边。")

    edges = _dedupe_edges(edges)

    if not entries:
        warn("journal.empty", "journal 集合为空库，没有可转换的条目（sixianyuchanmian 形态）。", "info")
    orphan_pages = int(getattr(index, "orphan_pages", 0) or 0)
    if orphan_pages:
        warn("journal.orphan_pages", f"{orphan_pages} 个 journal 页引用了不存在的条目，已跳过。")

    bundle = {
        "schema_version": 1,
        "scenario_id": "",
        "scenario_version": "",
        "topology": "linear",
        "topology_confidence": 0.5,
        "topology_source": "fvtt_journal_import",
        "nodes": nodes,
        "edges": edges,
        "triggers": [],
        "flags": [],
        "clocks": [],
        "assets": [],
        "warnings": warnings[:200],
        "meta": {
            "generator": "fvtt_journal.v1",
            "world_title": str(world_title or ""),
            "ai_chunked": bool(chunk_page),
            "ai_summarized": bool(summarize),
            "ai_edges": bool(ai_edge_count),
        },
        "start": {},
    }

    totals = {
        "journals": len(entries),
        "pages": processed_pages,
        "modules": len(modules),
        "nodes": len(nodes),
        "edges": len(edges),
        "chunks": chunk_nodes,
        "scene_anchors": len(anchor_ids),
        "warnings": len(warnings),
    }
    return {
        "modules": modules,
        "bundle": bundle,
        "warnings": [item["message"] for item in warnings],
        "degradations": degradations,
        "totals": totals,
    }


__all__ = [
    "EDGE_TYPES",
    "JOURNAL_NODE_PREFIXES",
    "MAX_BODY_CHARS",
    "MAX_CHUNKS_PER_PAGE",
    "MAX_CONTENT_CHARS",
    "MAX_EDGE_INFERENCE_NODES",
    "MAX_PAGE_CHARS",
    "MAX_PAGES",
    "MAX_SCENE_ANCHORS",
    "MAX_SUMMARY_CALLS",
    "MAX_SUMMARY_CHARS",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_KP_ONLY",
    "VISIBILITY_PUBLIC",
    "convert_world_journal",
    "is_journal_scoped",
    "page_plain_text",
    "page_visibility",
]
