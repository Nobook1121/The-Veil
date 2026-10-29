"""Deterministic, chunk-oriented scenario import pipeline.

「直接导入」不会把整篇文档交给 LLM，也不会生成可编辑的场景卡。它先按标题把
文档切分为一个个场景/章节块，再逐块嵌入写入向量库；审核模式（AI 转换）仍然
走既有的场景卡生成流程。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from trpg_server.agents.config import AIRuntimeConfig, load_ai_runtime_config
from trpg_server.scenario_documents import chunk_parsed_document, parse_scenario_document
from trpg_server.scenario_importer import _summary

logger = logging.getLogger(__name__)

# AI 全链路阶段 → 总进度区间（progress 8%~45%）。区间留白给 normalize/embed/graph，
# 前端因此能持续看到「扫描第几段 / 补全第几批」而不是一直停在 8%。
AI_STAGE_RANGES: dict[str, tuple[float, float]] = {
    "scan": (8, 20),
    "clean": (20, 22),
    "segment": (22, 28),
    "worldbook": (28, 34),
    "graph": (34, 40),
    "graph_detail": (40, 45),
}
# 整条 AI 链路的默认总预算（秒）。超过则明确失败，避免前端无限等待。
DEFAULT_AI_BUDGET_SECONDS = 1800

# 关键词派生：优先取引号包裹的专有名词，其次标题词、大写拉丁词、正文高频短名。
# 「」《》【】“” 是中文剧本里包裹专名的常见引号，直接取内容；
# （）() 里的内容多为整句注释（如「（详见下文）」「（不管破门是否成功）」），
# 仅当内容是纯拉丁人名（如「（Iriuma Zousu）」）时才采纳，避免整句垃圾词。
_QUOTED_TERM = re.compile(r"[「《【“]([^」》】”]{2,12})[」》】”]")
_LATIN_PAREN_TERM = re.compile(r"[（(]([A-Za-z][A-Za-z .'\-]{1,19})[）)]")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_CAP = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")
# 含数字的候选几乎都是年龄/页码/骰式（34岁、SC 0/1、1d5+DB、PC用资料2），无专名价值。
_DIGIT_NOISE = re.compile(r"[0-9０-９]")
# PDF/Word 目录页的点线标题（孩子与绣花鞋.............）不是可用触发词。
_TOC_DOTS = re.compile(r"\.{3,}")
# 标题首行即正文时，标题切分会得到整句（徐天在发现同伴A与B皆出现意外的情况下），
# 超过该长度的一律丢弃；中文人名/地名极少超过 8 字。
_TITLE_MAX_TERM_CHARS = 8
_KEYWORD_STOPWORDS = {
    "然后", "如果", "但是", "因为", "所以", "我们", "你们", "他们", "可以", "这个",
    "那个", "什么", "已经", "现在", "时候", "一个", "没有", "起来", "知道", "看到",
    "说道", "而且", "就是", "还是", "这样", "那些", "这些", "这里", "那里", "之后",
    "之前", "自己", "出来", "进来", "一下", "一样", "有些", "为了", "不过", "于是",
    # 规则书用语与泛化动词/名词：出现在几乎每一章里，作为触发词只会造成误激活。
    "调查员", "调查员们", "探索者", "探索者们", "调查", "探索", "模组", "玩家", "主持人", "检定", "成功",
    "失败", "发现", "需要", "进行", "使用", "获得", "声音", "此时", "随后", "开始",
    "结束", "以及", "并且", "通过", "关于", "根据", "按照", "情况", "内容", "部分",
    "详情", "下文", "详见", "随意", "注意", "提示", "建议", "方式", "方法",
}
# 大写拉丁词的首词常是英文句首（The/You/When…），并非专名，需剔除。
_LATIN_STOPWORDS = {
    "the", "this", "that", "these", "those", "you", "your", "and", "but", "for",
    "with", "when", "what", "which", "there", "then", "they", "their", "from",
    "have", "will", "one", "two", "all", "any", "not", "are", "was", "were",
    "can", "may", "his", "her", "him", "she", "it", "its",
}
# 标题含这些词视为「常驻」条目：世界观/背景每轮都应注入，避免模型丢失设定。
_CONSTANT_TITLE_HINTS = ("简介", "背景", "前言", "世界观", "序幕", "导入", "序章", "设定", "概述")
# 标题含这些词视为核心章节（core），排序永远优先于普通章节。
_CORE_TITLE_HINTS = ("主线", "核心", "关键", "真相", "结局", "终局", "finale", "climax")

_CHAPTER_NUMBER = re.compile(r"^第.{0,4}[章节幕回篇部]")

_TOPIC_MAX_KEYWORDS = 8


def _ngram_fragments(counts: dict[str, int]) -> set[str]:
    """找出所有「n-gram 碎片」：其在正文中的每次出现都落在某个更长 n-gram 内部。

    判定依据是出现次数——子串出现次数必然不少于包含它的长词；一旦两者相等，说明子串
    没有独立出现（查员 ⊂ 调查员、冯季 ⊂ 冯季阳、钥匙 ⊂ 铜钥匙），因此不应作为触发词。
    一次性预计算，避免逐个候选扫描 counts。
    """
    fragments: set[str] = set()
    for gram, gram_count in counts.items():
        if len(gram) < 3:
            continue
        for size in range(2, len(gram)):
            for start in range(len(gram) - size + 1):
                sub = gram[start:start + size]
                if counts.get(sub, 0) <= gram_count:
                    fragments.add(sub)
    return fragments


def derive_keywords(text: str, title: str = "", limit: int = _TOPIC_MAX_KEYWORDS) -> list[str]:
    """从章节正文与标题派生触发词（纯函数，便于单测）。

    派生顺序即优先级：引号专名 → 括号拉丁名 → 标题词 → 大写拉丁词 → 正文高频短名。
    去重后截断，保证条目数量有界，避免把整章变成一堆无意义关键词。
    """
    body = str(text or "")
    quoted = _QUOTED_TERM.findall(body)
    latin_paren = _LATIN_PAREN_TERM.findall(body)
    title_terms = [
        token
        for token in re.split(r"[\s，。、：:；;！!？?（）()\[\]【】「」《》]+", str(title or ""))
        if 2 <= len(token) <= _TITLE_MAX_TERM_CHARS
    ]
    latin = _LATIN_CAP.findall(body)
    # 中文没有词边界，短专名（人名/地名）常嵌在长句里，因此对每个汉字连续段
    # 统计 2-4 字 n-gram 的出现次数，取重复出现者作为候选触发词。
    counts: dict[str, int] = {}
    for run in _CJK_RUN.findall(body):
        for size in (2, 3, 4):
            for start in range(len(run) - size + 1):
                gram = run[start:start + size]
                counts[gram] = counts.get(gram, 0) + 1
    repeated = sorted(
        (term for term, count in counts.items() if count >= 2),
        key=lambda term: (-counts[term], -len(term), term),
    )
    fragments = _ngram_fragments(counts)
    candidates = [
        *((term, False) for term in quoted),
        *((term, False) for term in latin_paren),
        *((term, False) for term in title_terms),
        *((term, False) for term in latin),
        *((term, True) for term in repeated),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for term, from_repeated in candidates:
        value = str(term).strip()
        key = value.casefold()
        if len(value) < 2 or key in seen:
            continue
        if value in _KEYWORD_STOPWORDS or key in _LATIN_STOPWORDS:
            continue
        if _CHAPTER_NUMBER.match(value) or value.startswith("#"):
            continue
        # 数字/页码/骰式、目录点线标题都不是可用触发词。
        if _DIGIT_NOISE.search(value) or _TOC_DOTS.search(value):
            continue
        # 只对正文 n-gram 做碎片判定：引号/标题/拉丁词是显式专名，直接信任。
        if from_repeated and value in fragments:
            continue
        # n-gram 里若嵌着规则书泛词（如果调查、果调查员），说明它跨了词边界，不是专名。
        if from_repeated and any(word in value for word in _KEYWORD_STOPWORDS):
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def is_constant_section(title: str) -> bool:
    """世界观/背景类章节应每轮注入，不参与冷却与概率。"""
    lowered = str(title or "").casefold()
    return any(hint in lowered for hint in _CONSTANT_TITLE_HINTS)


def derive_tier(title: str) -> str:
    """按标题把章节归入 core/background（archived 由管理员手动设置）。"""
    lowered = str(title or "").casefold()
    return "core" if any(hint in lowered for hint in _CORE_TITLE_HINTS) else "background"


def _chunk_child_max(config: Any) -> int:
    """读取管理员设置的导入剧本子块字符上限。"""
    config_dir = config.get("CONFIG_DIR") if hasattr(config, "get") else None
    if not config_dir:
        return AIRuntimeConfig().chunk_child_max_chars
    return load_ai_runtime_config(config_dir).chunk_child_max_chars


def _chunk_title(text: str, index: int) -> str:
    for line in str(text or "").splitlines():
        value = line.strip().lstrip("#").strip().strip("【】").strip()
        if value:
            return value[:120]
    return f"Imported section {index}"


def _local_metadata(chunk: Any, index: int) -> dict[str, Any]:
    title = _chunk_title(getattr(chunk, "text", ""), index)
    chapter_path = getattr(chunk, "chapter_path", None)
    # section 是「章节归属」去重/回填键：同一章节被拆成多个子块时取值相同，
    # 检索命中后据此把相邻子块回填成完整章节。
    section = ""
    if isinstance(chapter_path, list) and chapter_path:
        section = str(chapter_path[0]).strip()
    lowered = title.casefold()
    if any(word in lowered for word in ("ending", "结局", "true end", "bad end", "crazy end")):
        card_type = "ending"
    elif any(word in lowered for word in ("npc", "人物", "角色", "怪物", "monster")):
        card_type = "npc"
    elif any(word in lowered for word in ("scene", "场景", "地点", "房间", "车厢")):
        card_type = "scene"
    else:
        card_type = "custom"
    return {
        "card_type": card_type,
        "summary": _summary(str(getattr(chunk, "text", "")), 180),
        "title": title,
        "section": section,
    }


class ScenarioImportPipeline:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config or {}

    def _cancelled(self, job_id: str) -> bool:
        current = self.store.get(job_id) or {}
        return bool(current.get("cancel_requested"))

    def _update_chunk_progress(self, job_id: str, stage: str, index: int, total: int, start: float, span: float, **extra: Any) -> None:
        ratio = index / max(1, total)
        self.store.update(
            job_id,
            status=stage,
            current_stage=stage,
            progress=round(start + span * ratio, 2),
            stage_progress=round(ratio * 100, 2),
            stage_meta={
                "totalChunks": total,
                "processedChunks": index,
                "message": f"正在生成知识库向量（{index}/{total} 块）",
                **extra,
            },
        )

    def _ai_progress(self, job_id: str, stage: str, done: int, total: int, message: str, started_at: float) -> None:
        """把 AI 阶段回调映射成任务进度；done<0 表示只更新文案、不动进度条。"""
        current = self.store.get(job_id) or {}
        start, end = AI_STAGE_RANGES.get(stage, (8, 45))
        if done >= 0 and total > 0:
            ratio = max(0.0, min(1.0, done / total))
            # 扫描窗口可能并发完成，确保任何一次回调都不会让进度条倒退。
            progress = max(round(start + (end - start) * ratio, 2), float(current.get("progress") or 0))
            stage_progress = round(ratio * 100, 2)
        else:
            progress = float(current.get("progress") or 8)
            stage_progress = float(current.get("stage_progress") or 0)
        self.store.update(
            job_id,
            status="reviewing",
            current_stage=stage,
            progress=progress,
            stage_progress=stage_progress,
            stage_meta={
                "mode": "ai",
                "message": message,
                "ai_stage": stage,
                "ai_done": done,
                "ai_total": total,
                "elapsed_s": round(time.monotonic() - started_at, 1),
            },
        )
        logger.info(
            "scenario_import.ai_progress job=%s stage=%s done=%s/%s message=%s",
            job_id,
            stage,
            done,
            total,
            message,
        )

    def _start_ai_heartbeat(self, job_id: str, started_at: float) -> tuple[threading.Event, threading.Thread]:
        """AI 请求可能连续几分钟没有结果；每 10 秒写一次心跳，前端据此判断「还在跑」。"""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(10):
                job = self.store.get(job_id) or {}
                if job.get("status") in {"done", "failed", "cancelled", "published"}:
                    return
                meta = dict(job.get("stage_meta") or {})
                meta["elapsed_s"] = round(time.monotonic() - started_at, 1)
                meta["heartbeat_at"] = time.time()
                self.store.update(job_id, stage_meta=meta)

        thread = threading.Thread(target=beat, daemon=True, name=f"import-heartbeat-{job_id}")
        thread.start()
        return stop, thread

    def _run_ai(self, job_id: str, job: dict[str, Any], parsed):
        """AI 全链路：通读剔除噪声 → 语义分模块 → 世界书 → 图谱 → 校验修复。"""
        from trpg_server.agents.embedding_provider import HashedTokenEmbedding
        from trpg_server.agents.knowledge_base import build_knowledge_chunks, index_knowledge_chunks
        from trpg_server.scenario_bundle import BundleError, build_narrative, normalize_bundle
        from trpg_server.scenario_import_ai import analyze_script, build_ai_requester

        metadata = job.get("metadata") or {}
        scenario_id = str(job.get("script_id"))
        scenario_version = str(job.get("target_version") or "1.0.0")
        title = str(metadata.get("title") or Path(job["source_filename"]).stem)

        started_at = time.monotonic()
        try:
            budget = float(self.config.get("SCENARIO_IMPORT_AI_BUDGET_SECONDS", DEFAULT_AI_BUDGET_SECONDS))
        except (TypeError, ValueError):
            budget = float(DEFAULT_AI_BUDGET_SECONDS)
        self.store.update(
            job_id,
            status="reviewing",
            current_stage="scan",
            progress=8,
            stage_progress=0,
            stage_meta={"mode": "ai", "message": "正在读取剧本，准备通读"},
        )
        request = build_ai_requester()
        heartbeat_stop, heartbeat = self._start_ai_heartbeat(job_id, started_at)
        try:
            bundle = analyze_script(
                parsed.markdown,
                title=title,
                request=request,
                scenario_id=scenario_id,
                scenario_version=scenario_version,
                budget_seconds=budget,
                progress=lambda stage, done, total, message: self._ai_progress(
                    job_id, stage, done, total, message, started_at
                ),
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        if self._cancelled(job_id):
            return self.store.update(job_id, status="cancelled", current_stage="scan", error="Import cancelled")
        self.store.save_intermediate(job_id, "ai_bundle", bundle)
        self.store.update(
            job_id,
            status="normalizing",
            current_stage="normalizing",
            progress=45,
            stage_progress=100,
            stage_meta={"mode": "ai", "message": "正在校验并修复剧本模块与图谱"},
        )

        result = normalize_bundle(
            bundle,
            fallback_title=title,
            default_scenario_id=scenario_id,
            default_version=scenario_version,
        )
        if result.errors:
            raise BundleError(
                "AI 生成的剧本包结构不完整：" + "；".join(str(item.get("message")) for item in result.errors[:5]),
                result.errors,
            )
        scenario = result.data["scenario"]
        try:
            player_count = int(str(metadata.get("playerCount") or "").strip() or "1")
        except ValueError:
            player_count = 1
        scenario.update(
            {
                "id": job.get("script_id"),
                "scenario_version": scenario_version,
                "title": scenario.get("title") or title,
                "author": scenario.get("author") or metadata.get("author") or "Imported",
                "creator_username": metadata.get("creator") or "",
                "playerCount": max(1, player_count),
                "notes": metadata.get("description") or scenario.get("notes") or "",
                "import_mode": "ai",
                "import_exclusions": result.data.get("exclusions") or [],
                "conversion": {
                    "version": 5,
                    "source_preserved": True,
                    "document_import": True,
                    "ai": {
                        "status": "ok",
                        "stages": (result.data.get("analysis") or {}).get("stages") or {},
                        "notes": (result.data.get("analysis") or {}).get("notes") or [],
                        "ai_calls": (result.data.get("analysis") or {}).get("ai_calls") or 0,
                    },
                    "warnings": result.warnings,
                    "stages": ["review", "segment", "worldbook", "graph", "normalize", "embed"],
                },
            }
        )
        self.store.update(
            job_id,
            status="embedding",
            current_stage="embedding",
            progress=55,
            stage_progress=0,
            stage_meta={"mode": "ai", "message": "正在生成知识库向量"},
        )

        # 知识库直接复用 AI 分好的模块与世界书字段，避免二次规则切分引入偏差。
        chunks = build_knowledge_chunks(scenario)
        provider = self.config.get("EMBEDDING_PROVIDER") or HashedTokenEmbedding()
        chunks = index_knowledge_chunks(
            chunks,
            vector_store=self.config.get("VECTOR_STORE"),
            embedding_provider=provider,
        )
        self.store.save_intermediate(job_id, "knowledge", [chunk.to_dict() for chunk in chunks])
        self.store.update(
            job_id,
            status="graphing",
            current_stage="graphing",
            progress=82,
            stage_progress=0,
            stage_meta={"mode": "ai", "message": "正在校验并修复图谱关系"},
        )

        narrative, repair, validation = build_narrative(result)
        if not narrative.nodes:
            raise BundleError("AI 生成的图谱没有任何节点")
        assets = [node for node in narrative.nodes if node.kind in {"npc", "item", "clue"}]
        has_scene = any(
            str(module.get("module_type")) in {"scene", "ending"}
            for module in scenario.get("modules") or []
        )
        if not assets and has_scene:
            raise BundleError("AI 图谱没有提取到任何 NPC/物品/线索，拒绝发布只含地点的骨架图；请重试或检查 AI 平台。")
        logger.info(
            "scenario_import.ai_done job=%s nodes=%s edges=%s assets=%s stages=%s",
            job_id,
            len(narrative.nodes),
            len(narrative.edges),
            len(assets),
            (result.data.get("analysis") or {}).get("stages"),
        )
        narrative.scenario_id = str(job.get("script_id"))
        narrative.scenario_version = scenario_version
        narrative.meta = {
            **dict(narrative.meta or {}),
            "generator": "ai_import",
            "source_document": parsed.filename,
            "exclusions": result.data.get("exclusions") or [],
            "repair": repair.to_dict(),
            "validation": validation.to_dict(),
        }
        self.store.save_intermediate(job_id, "bundle", result.payload())
        self.store.save_intermediate(job_id, "narrative", narrative.to_dict())
        scenario["narrative_summary"] = {
            "nodes": len(narrative.nodes),
            "edges": len(narrative.edges),
            "locations": len(narrative.location_nodes),
            "endings": len(narrative.ending_nodes),
            "assets": len(assets),
            "ai_calls": (result.data.get("analysis") or {}).get("ai_calls") or 0,
            "repair_count": len(repair.applied),
            "error_count": validation.to_dict().get("error_count", 0),
            "warning_count": validation.to_dict().get("warning_count", 0),
        }
        scenario["global_summary"] = scenario.get("global_summary") or " ".join(
            str(item.get("summary") or "") for item in scenario.get("modules", [])[:6]
        )[:1000]
        self.store.update(
            job_id,
            status="done",
            current_stage="done",
            progress=100,
            stage_progress=100,
            stage_meta={"mode": "ai", "message": "导入完成，请审核后发布"},
        )
        self.store.save_intermediate(job_id, "preview", scenario)
        logger.info(
            "scenario_import.ai_complete job=%s modules=%d chunks=%d nodes=%d edges=%d",
            job_id,
            len(scenario.get("modules", [])),
            len(chunks),
            len(narrative.nodes),
            len(narrative.edges),
        )
        return self.store.update(job_id, preview=scenario)

    def run(self, job_id: str, start_stage: str | None = None):
        job = self.store.get(job_id)
        if not job:
            raise ValueError("import job not found")
        source = Path(self.store.root) / job_id / "source" / job["source_filename"]
        try:
            raw = source.read_bytes()
            ocr = self.config.get("OCR_PROVIDER") if self.config.get("OCR_ENABLED") else None
            self.store.update(
                job_id,
                status="parsing",
                current_stage="parsing",
                progress=5,
                stage_progress=0,
                stage_meta={"message": "正在读取文档并提取文本"},
            )
            parsed = parse_scenario_document(raw, job["source_filename"], ocr_provider=ocr)
            self.store.save_intermediate(job_id, "parsed", {"filename": parsed.filename, "markdown": parsed.markdown, "page_count": parsed.page_count})
            mode = str((job.get("metadata") or {}).get("mode") or "direct").strip().lower()
            if mode == "ai":
                return self._run_ai(job_id, job, parsed)

            # 直接导入：按标题（Word/PDF 标题、Markdown #、第X章）切分为语义块，
            # 每个标题对应原文中的一个场景/章节，不再生成场景卡。
            sections = chunk_parsed_document(parsed, target_max=_chunk_child_max(self.config))
            total = len(sections)
            self.store.update(
                job_id,
                status="chunking",
                current_stage="chunking",
                progress=15,
                stage_progress=100,
                stage_meta={"totalChunks": total, "processedChunks": total, "message": f"已按标题切分为 {total} 个知识块"},
            )
            self.store.save_intermediate(job_id, "chunks", [c.__dict__ for c in sections])

            from trpg_server.agents.embedding_provider import HashedTokenEmbedding
            from trpg_server.agents.knowledge_base import KnowledgeChunk, index_knowledge_chunks

            provider = self.config.get("EMBEDDING_PROVIDER") or HashedTokenEmbedding()
            scenario_id = str(job.get("script_id"))
            scenario_version = str(job.get("target_version") or "1")

            knowledge_chunks: list[KnowledgeChunk] = []
            for index, section in enumerate(sections, 1):
                if self._cancelled(job_id):
                    return self.store.update(job_id, status="cancelled", current_stage="embedding", error="Import cancelled")
                metadata = _local_metadata(section, index)
                title = str(metadata.get("title") or _chunk_title(section.text, index))
                # 派生触发词后，检索可以走「关键词直命中」通道，无需依赖语义相似度；
                # 关键词足够多的条目同时被标记为可递归触发其它条目。
                keywords = derive_keywords(section.text, title)
                chunk = KnowledgeChunk(
                    scenario_id=scenario_id,
                    scenario_version=scenario_version,
                    # 直接导入没有场景卡，块不绑定场景，检索时不被当前场景过滤掉。
                    scene_id=None,
                    card_type=str(metadata.get("card_type") or "custom"),
                    visibility="public",
                    spoiler_level=0,
                    unlock_condition=None,
                    text=section.text,
                    chunk_id=f"chunk-{index:04d}",
                    source_ref={"page": section.page, "page_end": section.page_end, "chapter": section.chapter_path or []},
                    metadata={"title": title, "section": str(metadata.get("section") or "")},
                    keywords=keywords,
                    is_constant=is_constant_section(title),
                    tier=derive_tier(title),
                    trigger_chunks=len(keywords) >= 2,
                )
                vectors = provider.embed([chunk.text])
                if not vectors:
                    raise ValueError(f"Embedding returned no vector for chunk {index}")
                knowledge_chunks.append(replace(chunk, embedding=vectors[0]))
                self._update_chunk_progress(job_id, "embedding", index, total, 15, 80)

            index_knowledge_chunks(
                knowledge_chunks,
                vector_store=self.config.get("VECTOR_STORE"),
                embedding_provider=provider,
            )
            self.store.save_intermediate(job_id, "knowledge", [chunk.to_dict() for chunk in knowledge_chunks])

            metadata = job.get("metadata") or {}
            try:
                player_count = int(str(metadata.get("playerCount") or "").strip() or "1")
            except ValueError:
                player_count = 1
            scenario: dict[str, Any] = {
                "id": job.get("script_id"),
                "scenario_version": scenario_version,
                "title": metadata.get("title") or Path(job["source_filename"]).stem,
                "author": metadata.get("author") or "Imported",
                "creator_username": metadata.get("creator") or "",
                "playerCount": max(1, player_count),
                "notes": metadata.get("description") or "",
                # 直接导入不生成可编辑场景卡，知识以向量块形式保存。
                "modules": [],
                "import_mode": "direct",
                "conversion": {"version": 4, "source_preserved": True, "document_import": True, "chunk_count": len(knowledge_chunks), "stages": ["parse", "chunk", "embed"]},
            }
            self.store.update(
                job_id,
                status="summarizing",
                current_stage="summarizing",
                progress=95,
                stage_progress=100,
                stage_meta={"totalChunks": total, "processedChunks": total, "message": "正在整理剧本摘要"},
            )
            summary = " ".join(str(chunk.text) for chunk in knowledge_chunks)[:1000]
            scenario["global_summary"] = summary

            # 直接导入也在导入过程中生成地点骨架图谱，用户不必再进图谱页手动点「自动建图」。
            # 该骨架只含标题级地点/结局；NPC、物品与地点之间的关系由「AI 全链路导入」补齐。
            self.store.update(
                job_id,
                status="graphing",
                current_stage="graphing",
                progress=96,
                stage_progress=0,
                stage_meta={"totalChunks": total, "processedChunks": total, "message": "正在生成地点骨架图谱"},
            )
            try:
                from trpg_server.agents.narrative.builder import build_narrative as build_document_narrative
                from trpg_server.agents.narrative.repair import repair_bundle

                built = build_document_narrative(
                    parsed,
                    scenario=scenario,
                    scenario_id=scenario_id,
                    scenario_version=scenario_version,
                )
                narrative = built.bundle
                repair_bundle(narrative)
                narrative.scenario_id = scenario_id
                narrative.scenario_version = scenario_version
                narrative.meta = {
                    **dict(narrative.meta or {}),
                    "generator": "direct_import",
                    "source_document": parsed.filename,
                }
                self.store.save_intermediate(job_id, "narrative", narrative.to_dict())
                scenario["narrative_summary"] = {
                    "nodes": len(narrative.nodes),
                    "edges": len(narrative.edges),
                    "locations": len(narrative.location_nodes),
                    "endings": len(narrative.ending_nodes),
                    "assets": len([node for node in narrative.nodes if node.kind in {"npc", "item", "clue"}]),
                    "error_count": getattr(built.report, "error_count", 0),
                    "warning_count": getattr(built.report, "warning_count", 0),
                }
                scenario["conversion"]["stages"].append("graph")
            except Exception as exc:  # noqa: BLE001 - 图谱失败不应阻断知识库导入
                logger.warning("scenario_import.direct_graph_failed job=%s error=%s", job_id, exc)
                scenario["conversion"]["graph_error"] = str(exc)

            self.store.save_intermediate(job_id, "preview", scenario)
            logger.info("scenario_import.direct_complete job=%s chunks=%d vectors=%d", job_id, total, len(knowledge_chunks))
            return self.store.update(
                job_id,
                status="done",
                current_stage="done",
                progress=100,
                stage_progress=100,
                stage_meta={"totalChunks": total, "processedChunks": total, "message": "导入完成，请审核后发布"},
                preview=scenario,
            )
        except Exception as exc:
            logger.exception("scenario_import.pipeline_failed job=%s", job_id)
            return self.store.update(job_id, status="failed", error=str(exc), failed_stage=(self.store.get(job_id) or {}).get("current_stage") or "parsing")
