"""Scenario knowledge cards and safe room-scoped retrieval.

The default implementation intentionally uses a deterministic lexical scorer so the
server remains dependency-free. A vector backend can implement the same card
contract later without changing callers.
"""

from __future__ import annotations

import logging
import re
import math
import random
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.agents.narrative.visibility import is_visible_to_player, normalize_visibility

logger = logging.getLogger(__name__)

# 检索降级（embedding 失败 / 向量库查询失败）属于持续状态，
# 只在首次出现时告警，避免每条消息、每轮工具调用都重复刷同一条日志。
_logged_degradations: set[str] = set()


def _log_retrieval_degradation(kind: str, error: Exception) -> None:
    if kind in _logged_degradations:
        return
    _logged_degradations.add(kind)
    logger.warning(
        "knowledge retrieval degraded (%s); suppressing further identical warnings: %s",
        kind,
        error,
        exc_info=True,
    )


@dataclass(frozen=True)
class KnowledgeChunk:
    scenario_id: str
    scenario_version: str
    scene_id: str | None
    card_type: str
    visibility: str
    spoiler_level: int
    unlock_condition: str | None
    text: str
    chunk_id: str
    embedding: list[float] | None = None
    source_ref: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    attachments: list[dict[str, Any]] | None = None
    # 世界书（lorebook）字段：关键词通道直接激活条目，无需依赖语义相似度。
    # ``keywords`` 为主触发词，命中即激活；``secondary_keywords`` 是「与主键同时
    # 成立」的门槛，按 ``secondary_logic``（and_any/and_all/not_any/not_all）判定。
    keywords: list[str] | None = None
    secondary_keywords: list[str] | None = None
    secondary_logic: str = "and_any"
    # 常驻条目（世界观/背景）每轮都注入，且不受冷却/概率/分组影响。
    is_constant: bool = False
    # core 永远排在 background 之前；archived 在硬过滤阶段直接排除。
    tier: str = "background"
    # 概率激活（0-100），仅对本轮非强制条目生效。
    probability: int = 100
    # 同组条目竞争，每个分组只保留一条；group_weight 高者胜出。
    group: str = ""
    group_weight: int = 100
    priority: int = 100
    order: int = 100
    # 是否允许用本条正文去递归触发其它条目（triggers_recursive）。
    trigger_chunks: bool = False
    # 条目自身的粘滞/冷却/延迟轮数（0 表示沿用管理员全局默认）。
    sticky_rounds: int = 0
    cooldown_rounds: int = 0
    delay_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version(data: Mapping[str, Any], *keys: str, default: str = "1") -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iter_modules(scenario: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    modules = scenario.get("modules")
    if isinstance(modules, list) and modules:
        yield from (item for item in modules if isinstance(item, dict))
        return
    for collection, card_type in (("scenes", "scene"), ("endings", "ending")):
        values = scenario.get(collection)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    yield {**item, "module_type": item.get("module_type") or card_type}


def build_knowledge_chunks(scenario: Mapping[str, Any] | None) -> list[KnowledgeChunk]:
    """Convert a normalized scenario into versioned, filterable cards."""
    if not isinstance(scenario, Mapping):
        return []
    scenario_id = str(scenario.get("id") or scenario.get("scenario_id") or "unknown")
    scenario_version = _version(scenario, "scenario_version", "version", "version_id")
    chunks: list[KnowledgeChunk] = []
    for index, module in enumerate(_iter_modules(scenario), 1):
        text = str(module.get("content") or module.get("text") or module.get("summary") or "").strip()
        if not text:
            continue
        card_type = str(module.get("card_type") or module.get("module_type") or "custom").strip().lower()
        scene_id = module.get("scene_id")
        if scene_id in (None, "") and card_type in {"scene", "ending"}:
            scene_id = module.get("id")
        chunk_id = str(module.get("chunk_id") or module.get("id") or f"{card_type}-{index}")
        chunks.append(
            KnowledgeChunk(
                scenario_id=scenario_id,
                scenario_version=_version(module, "scenario_version", default=scenario_version),
                scene_id=str(scene_id) if scene_id not in (None, "") else None,
                card_type=card_type,
                visibility=normalize_visibility(module.get("visibility")),
                spoiler_level=max(0, _int(module.get("spoiler_level"), 0)),
                unlock_condition=(str(module.get("unlock_condition")) if module.get("unlock_condition") else None),
                text=text,
                chunk_id=chunk_id,
                embedding=module.get("embedding") if isinstance(module.get("embedding"), list) else None,
                source_ref=module.get("source_ref") if isinstance(module.get("source_ref"), dict) else None,
                metadata=module.get("metadata") if isinstance(module.get("metadata"), dict) else None,
                attachments=module.get("attachments") if isinstance(module.get("attachments"), list) else None,
                # 世界书字段可由模块/知识索引携带；缺失时用 dataclass 默认值。
                keywords=module.get("keywords") if isinstance(module.get("keywords"), list) else None,
                secondary_keywords=(
                    module.get("secondary_keywords") if isinstance(module.get("secondary_keywords"), list) else None
                ),
                secondary_logic=str(module.get("secondary_logic") or "and_any"),
                is_constant=bool(module.get("is_constant")),
                tier=str(module.get("tier") or "background"),
                probability=_int(module.get("probability"), 100),
                group=str(module.get("group") or ""),
                group_weight=_int(module.get("group_weight"), 100),
                priority=_int(module.get("priority"), 100),
                order=_int(module.get("order"), 100),
                trigger_chunks=bool(module.get("trigger_chunks")),
                sticky_rounds=_int(module.get("sticky_rounds"), 0),
                cooldown_rounds=_int(module.get("cooldown_rounds"), 0),
                delay_rounds=_int(module.get("delay_rounds"), 0),
            )
        )
    return chunks


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[\w\u4e00-\u9fff]+", value.casefold()) if token]


def lexical_match_score(haystack: str, query: str) -> float:
    """Deterministic lexical score shared by scenario and ruleset retrieval.

    Latin tokens are matched as whole words, but CJK text cannot be split on
    whitespace: tokenising ``走进图书馆`` as a single run means an exact token
    comparison never matches a query such as ``图书馆``. Overlapping character
    bigrams let a CJK query match an occurrence inside a longer run while
    keeping the scorer dependency-free.
    """
    hay = str(haystack or "").casefold()
    query_tokens = _tokens(query)
    if not query_tokens:
        return 1.0
    score = 0.0
    for token in query_tokens:
        if token.isascii() or len(token) < 2:
            score += hay.count(token)
            continue
        score += sum(hay.count(token[index : index + 2]) for index in range(len(token) - 1))
    return score


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")

# 规则类查询更依赖精确词形（技能名/属性名/规则术语），叙事类查询更依赖语义向量。
_RULE_QUERY_HINTS = (
    "检定", "规则", "技能", "伤害", "骰", "属性", "成功", "失败",
    "check", "rule", "skill", "damage", "dice",
)


def _bm25_tokens(value: str) -> list[str]:
    """BM25 分词：拉丁词整词，CJK 拆成二元组（中文无法用空格切分）。"""
    tokens: list[str] = []
    for token in _TOKEN_PATTERN.findall(str(value or "").casefold()):
        if token[0].isascii() or len(token) == 1:
            tokens.append(token)
        else:
            tokens.extend(token[index:index + 2] for index in range(len(token) - 1))
    return tokens


def _bm25_scores(texts: list[str], query_tokens: list[str]) -> list[float]:
    """在当前语料（硬过滤后的知识块）上计算 DF 得到 IDF，返回每个文档的词法分。

    IDF 在子集上计算，冷门词（专有人名/地名）能拿到更高权重；泛化输入与所有
    文档都无交集时整体趋近 0，从而自然落到兜底路径。
    """
    corpus = [_bm25_tokens(text) for text in texts]
    total = len(corpus)
    document_frequency: dict[str, int] = {}
    for tokens in corpus:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    average_length = sum(len(tokens) for tokens in corpus) / max(1, total)
    k1, b = 1.5, 0.75
    scores: list[float] = []
    for tokens in corpus:
        length = len(tokens) or 1
        frequency: dict[str, int] = {}
        for token in tokens:
            frequency[token] = frequency.get(token, 0) + 1
        score = 0.0
        for query_token in set(query_tokens):
            term_frequency = frequency.get(query_token, 0)
            if not term_frequency:
                continue
            idf = math.log(1 + (total + 1) / (document_frequency.get(query_token, 0) + 1))
            score += idf * (term_frequency * (k1 + 1)) / (
                term_frequency + k1 * (1 - b + b * length / average_length)
            )
        scores.append(score)
    return scores


def _normalize_scores(scores: list[float]) -> list[float]:
    """把一组分数线性归一化到 [0, 1]（全 0 时保持全 0）。"""
    top = max(scores) if scores else 0.0
    if top <= 0:
        return [0.0 for _ in scores]
    return [score / top for score in scores]


def _score_weights(query: str) -> tuple[float, float]:
    """按查询类型给出 (词法权重, 向量权重)：规则类问题偏词法，叙事类偏向量。"""
    text = str(query or "").casefold()
    if any(hint in text for hint in _RULE_QUERY_HINTS):
        return 1.0, 0.6
    return 0.75, 1.0


def _section_key(chunk: KnowledgeChunk) -> str:
    """章节级去重键：同一章节的多个子块取值相同，无章节信息时退化为 chunk_id。"""
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    section = str(metadata.get("section") or "").strip()
    return f"section:{section}" if section else chunk.chunk_id


def _merge_section_text(
    chunks: list[KnowledgeChunk], hit: KnowledgeChunk, parent_max_chars: int
) -> tuple[str, list[str]]:
    """从命中子块向两侧回填同章节的相邻块，拼成完整章节（受父块上限约束）。"""
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    try:
        index = chunk_ids.index(hit.chunk_id)
    except ValueError:
        return hit.text, [hit.chunk_id]
    pieces = [chunks[index].text]
    total = len(pieces[0])
    left = right = index
    while True:
        extended = False
        if right + 1 < len(chunks) and total + len(chunks[right + 1].text) <= parent_max_chars:
            right += 1
            pieces.append(chunks[right].text)
            total += len(chunks[right].text)
            extended = True
        if left - 1 >= 0 and total + len(chunks[left - 1].text) <= parent_max_chars:
            left -= 1
            pieces.insert(0, chunks[left].text)
            total += len(chunks[left].text)
            extended = True
        if not extended:
            break
    metadata = hit.metadata if isinstance(hit.metadata, dict) else {}
    marker = str(metadata.get("section") or "").strip()
    cleaned: list[str] = []
    for offset, piece in enumerate(pieces):
        # 续块带「【章节】」前缀，合并时只保留一次，避免重复噪声。
        if offset and marker:
            prefix = f"【{marker}】"
            if piece.startswith(prefix):
                piece = piece[len(prefix):].lstrip("\n")
        cleaned.append(piece)
    return "\n".join(cleaned).strip(), chunk_ids[left:right + 1]


def _grouped_result(
    chunk: KnowledgeChunk, merged_text: str, chunk_ids: list[str], score: float, lexical: float, vector: float
) -> dict[str, Any]:
    result = chunk.to_dict()
    result["text"] = merged_text
    result["chunk_ids"] = chunk_ids
    result["score"] = score
    result["score_components"] = {"lexical": lexical, "vector": vector}
    return result


@dataclass(frozen=True)
class _Candidate:
    """一个候选章节：``score`` 为排序分，``lexical``/``vector`` 保留原始分量。

    ``channel`` 记录激活通道（keyword/score/fallback/sticky），``forced`` 表示常驻
    或粘滞条目——它们跳过概率与分组竞争，但仍计入末尾的 token 预算。
    """

    chunk: KnowledgeChunk
    score: float
    lexical: float
    vector: float
    channel: str = "score"
    forced: bool = False


# 递归扫描的硬上限：深度/步数/激活条目三重封顶，避免大剧本下无限扩散。
_MAX_RECURSION_STEPS = 2000
_MAX_ACTIVATED_ENTRIES = 400
_TIER_RANK = {"core": 0, "background": 1}


def _group_by_section(chunks: list[KnowledgeChunk]) -> dict[str, list[KnowledgeChunk]]:
    """按章节归组，保持块在剧本文档中的原始顺序（回填时依赖该顺序）。"""
    sections: dict[str, list[KnowledgeChunk]] = {}
    for chunk in chunks:
        sections.setdefault(_section_key(chunk), []).append(chunk)
    return sections


def _cooldown_keys(recent_rounds: list[list[str]], cooldown_rounds: int) -> set[str]:
    """最近若干轮召回过的章节 key：兜底路径据此向前推进，避免反复返回开头几段。"""
    cooldown: set[str] = set()
    if cooldown_rounds > 0:
        for round_keys in recent_rounds[-cooldown_rounds:]:
            cooldown.update(round_keys)
    return cooldown


def _keyword_in(key: str, query: str) -> bool:
    """触发词命中判定：拉丁词按整词匹配，CJK 按子串匹配（中文无法用空格切分）。"""
    if not key:
        return False
    if key.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", query) is not None
    return key in query


def _keyword_hit(chunk: KnowledgeChunk, query: str) -> tuple[bool, float]:
    """关键词通道：主键命中即激活，副键按 ``secondary_logic`` 进一步筛选。

    返回 ``(是否激活, 命中分)``。命中分用于同分排序：主键每次命中计 1 分，
    副键计 0.5 分（与 diceframe 的 ``matched_key_score`` 同构）。
    """
    text = str(query or "").casefold()
    primary = [str(key).casefold().strip() for key in (chunk.keywords or []) if str(key).strip()]
    if not text or not primary:
        return False, 0.0
    primary_hits = [key for key in primary if _keyword_in(key, text)]
    if not primary_hits:
        return False, 0.0
    score = float(len(primary_hits))
    secondary = [str(key).casefold().strip() for key in (chunk.secondary_keywords or []) if str(key).strip()]
    if secondary:
        secondary_hits = [key for key in secondary if _keyword_in(key, text)]
        logic = str(chunk.secondary_logic or "and_any").lower()
        if logic == "and_all" and len(secondary_hits) != len(secondary):
            return False, 0.0
        if logic == "and_any" and not secondary_hits:
            return False, 0.0
        if logic == "not_any" and secondary_hits:
            return False, 0.0
        if logic == "not_all" and len(secondary_hits) == len(secondary):
            return False, 0.0
        score += 0.5 * len(secondary_hits)
    return True, score


def _tier_rank(chunk: KnowledgeChunk) -> int:
    """``core`` 章节永远排在 ``background`` 之前；``archived`` 已在硬过滤阶段剔除。"""
    return _TIER_RANK.get(str(chunk.tier or "").lower(), 1)


def _candidate_sort_key(item: _Candidate) -> tuple[Any, ...]:
    """强制条目（常驻/粘滞）优先保住，其余按 tier → priority → 得分 → order 排序。

    末尾不设 key，依赖 Python 稳定排序保留文档原始顺序（决定同分时的先后）。
    """
    return (
        0 if item.forced else 1,
        _tier_rank(item.chunk),
        -int(item.chunk.priority),
        -item.score,
        int(item.chunk.order),
    )


def _dedupe_by_section(items: list[_Candidate]) -> list[_Candidate]:
    """同一章节只保留最优候选，避免同一章内容重复注入。"""
    best: dict[str, _Candidate] = {}
    for item in items:
        key = _section_key(item.chunk)
        current = best.get(key)
        if current is None or item.score > current.score:
            best[key] = item
    return list(best.values())


def _advance_timed(timed: dict[str, dict[str, int]]) -> None:
    """推进一轮生命周期：sticky 递减到 0 时才武装冷却，随后冷却递减。"""
    for state in timed.values():
        sticky = int(state.get("sticky_remaining", 0))
        if sticky > 0:
            sticky -= 1
            state["sticky_remaining"] = sticky
            if sticky == 0:
                state["cooldown_remaining"] = int(state.get("pending_cooldown", 0))
                state["pending_cooldown"] = 0
        elif int(state.get("cooldown_remaining", 0)) > 0:
            state["cooldown_remaining"] = int(state["cooldown_remaining"]) - 1


def _timed_flags(key: str, timed: Mapping[str, Mapping[str, int]]) -> tuple[bool, bool]:
    """返回 ``(sticky_active, on_cooldown)``：粘滞期恒为真，冷却期被硬门控挡下。"""
    state = timed.get(key) or {}
    return int(state.get("sticky_remaining", 0)) > 0, int(state.get("cooldown_remaining", 0)) > 0


def _apply_probability(items: list[_Candidate], rng: Any) -> list[_Candidate]:
    """按 ``probability`` 丢弃条目；强制条目与 100% 条目不受影响。"""
    kept: list[_Candidate] = []
    for item in items:
        if item.forced:
            kept.append(item)
            continue
        chance = max(0, min(100, int(item.chunk.probability)))
        if chance >= 100 or (chance > 0 and rng.random() * 100 < chance):
            kept.append(item)
    return kept


def _resolve_groups(items: list[_Candidate]) -> list[_Candidate]:
    """同组条目竞争：每个分组只保留 group_weight 最高（并列则得分最高）的一条。"""
    free: list[_Candidate] = []
    winners: dict[str, _Candidate] = {}
    for item in items:
        group = str(item.chunk.group or "").strip()
        if not group:
            free.append(item)
            continue
        current = winners.get(group)
        if current is None or (item.chunk.group_weight, item.score) > (current.chunk.group_weight, current.score):
            winners[group] = item
    return free + list(winners.values())


def _expand_recursive(
    seeds: list[_Candidate],
    pool: list[KnowledgeChunk],
    *,
    max_depth: int,
    max_steps: int,
    max_activated: int,
) -> list[_Candidate]:
    """递归扫描：用带 ``trigger_chunks`` 的条目正文去找其它条目的触发词。

    深度、步数、激活条目数三重封顶；``seen`` 同时充当环检测。
    """
    result = list(seeds)
    seen = {item.chunk.chunk_id for item in seeds}
    frontier = [item for item in seeds if item.chunk.trigger_chunks]
    steps = 0
    depth = 0
    while frontier and depth < max(0, max_depth) and steps < max_steps and len(result) < max_activated:
        nxt: list[_Candidate] = []
        for item in frontier:
            text = str(item.chunk.text or "")
            for candidate in pool:
                if steps >= max_steps or len(result) >= max_activated:
                    break
                steps += 1
                if candidate.chunk_id in seen:
                    continue
                hit, score = _keyword_hit(candidate, text)
                if not hit:
                    continue
                seen.add(candidate.chunk_id)
                activated = _Candidate(chunk=candidate, score=score, lexical=0.0, vector=0.0, channel="keyword")
                result.append(activated)
                if candidate.trigger_chunks:
                    nxt.append(activated)
        frontier = nxt
        depth += 1
    return result


def _estimate_tokens(text: str) -> int:
    """粗略的 token 估算：CJK 约 1 token/字，其余约 4 字符/token。"""
    value = str(text or "")
    cjk = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return cjk + max(0, len(value) - cjk) // 4 + 1


def _apply_token_budget(results: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """按最终排序裁剪注入量：丢弃放不下的低优先级条目，但绝不截断正文。

    ``budget <= 0``（默认）表示不限制，保证既有行为与「不靠截断降 token」的约束。
    """
    if budget <= 0 or not results:
        return results
    kept: list[dict[str, Any]] = []
    used = 0
    for item in results:
        cost = _estimate_tokens(str(item.get("text") or ""))
        if kept and used + cost > budget:
            continue
        kept.append(item)
        used += cost
    return kept


def _fallback_ranked(
    chunks: list[KnowledgeChunk], cooldown_keys: set[str], limit: int
) -> list[_Candidate]:
    """未命中时的兜底排序：优先返回冷却之外的章节，使召回随剧情推进而前进。

    直接导入的剧本没有场景模块，内容仅在知识块中，返回空会让模型「忘记」剧本，
    因此词法与向量均未命中时仍必须给出内容。
    """
    ordered = [chunk for chunk in chunks if _section_key(chunk) not in cooldown_keys] or chunks
    picked: dict[str, KnowledgeChunk] = {}
    for chunk in ordered:
        picked.setdefault(_section_key(chunk), chunk)
        if len(picked) >= limit:
            break
    return [
        _Candidate(chunk=chunk, score=0.0, lexical=0.0, vector=0.0, channel="fallback")
        for chunk in picked.values()
    ]


def _render_section(hit: _Candidate, sections: dict[str, list[KnowledgeChunk]], parent_max_chars: int) -> dict[str, Any]:
    """把命中的子块回填成一条完整章节结果。"""
    chunk = hit.chunk
    merged_text, chunk_ids = _merge_section_text(sections.get(_section_key(chunk), [chunk]), chunk, parent_max_chars)
    return _grouped_result(chunk, merged_text, chunk_ids, hit.score, hit.lexical, hit.vector)


SECONDARY_LOGIC_VALUES = ("and_any", "and_all", "not_any", "not_all")
TIER_VALUES = ("core", "background", "archived")
# 编辑界面可改写的世界书字段及取值范围（(最小值, 最大值)），键名与 dataclass 一致。
LOREBOOK_INT_FIELDS: dict[str, tuple[int, int]] = {
    "probability": (0, 100),
    "group_weight": (0, 1000),
    "priority": (0, 1000),
    "order": (0, 1000),
    "sticky_rounds": (0, 10),
    "cooldown_rounds": (0, 20),
    "delay_rounds": (0, 20),
}
LOREBOOK_BOOL_FIELDS = ("is_constant", "trigger_chunks")
LOREBOOK_LIST_FIELDS = ("keywords", "secondary_keywords")


def _keyword_list(value: Any) -> list[str] | None:
    """把逗号/换行/顿号分隔的字符串或列表规范化为去重后的触发词列表。"""
    if isinstance(value, str):
        value = re.split(r"[,，、;；\n\r]+", value)
    if not isinstance(value, (list, tuple, set)):
        return None
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _clamp_int(value: Any, bounds: tuple[int, int], default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    low, high = bounds
    return max(low, min(high, number))


def apply_lorebook_update(chunk: KnowledgeChunk, values: Mapping[str, Any]) -> KnowledgeChunk:
    """按编辑界面提交的值改写单个知识块的世界书字段（未提交的字段保持不变）。"""
    updates: dict[str, Any] = {}
    for field in LOREBOOK_LIST_FIELDS:
        if field in values:
            parsed = _keyword_list(values[field])
            if parsed is not None:
                updates[field] = parsed or None
    if "secondary_logic" in values:
        logic = str(values["secondary_logic"] or "").strip().lower()
        updates["secondary_logic"] = logic if logic in SECONDARY_LOGIC_VALUES else "and_any"
    if "tier" in values:
        tier = str(values["tier"] or "").strip().lower()
        updates["tier"] = tier if tier in TIER_VALUES else "background"
    if "group" in values:
        updates["group"] = str(values["group"] or "").strip()
    for field in LOREBOOK_BOOL_FIELDS:
        if field in values:
            updates[field] = bool(values[field])
    for field, bounds in LOREBOOK_INT_FIELDS.items():
        if field in values:
            updates[field] = _clamp_int(values[field], bounds, getattr(chunk, field))
    return replace(chunk, **updates) if updates else chunk


def list_knowledge_sections(chunks: Iterable[KnowledgeChunk]) -> list[dict[str, Any]]:
    """按章节汇总知识块，供管理端查看/编辑世界书字段（触发词、常驻、分层等）。"""
    sections: list[dict[str, Any]] = []
    for key, items in _group_by_section(list(chunks)).items():
        head = items[0]
        metadata = head.metadata if isinstance(head.metadata, dict) else {}
        sections.append(
            {
                "section_key": key,
                "title": str(metadata.get("title") or metadata.get("section") or head.chunk_id),
                "chunk_ids": [item.chunk_id for item in items],
                "preview": (head.text or "")[:160],
                "keywords": list(head.keywords or []),
                "secondary_keywords": list(head.secondary_keywords or []),
                "secondary_logic": head.secondary_logic,
                "is_constant": bool(head.is_constant),
                "tier": head.tier,
                "probability": int(head.probability),
                "group": head.group,
                "group_weight": int(head.group_weight),
                "priority": int(head.priority),
                "order": int(head.order),
                "trigger_chunks": bool(head.trigger_chunks),
                "sticky_rounds": int(head.sticky_rounds),
                "cooldown_rounds": int(head.cooldown_rounds),
                "delay_rounds": int(head.delay_rounds),
            }
        )
    return sections


def update_knowledge_section(
    chunks: list[KnowledgeChunk], section_key: str, values: Mapping[str, Any]
) -> tuple[list[KnowledgeChunk], int]:
    """把世界书字段写入某章节的全部子块，返回新列表与命中的块数。"""
    updated: list[KnowledgeChunk] = []
    count = 0
    for chunk in chunks:
        if _section_key(chunk) == section_key:
            updated.append(apply_lorebook_update(chunk, values))
            count += 1
        else:
            updated.append(chunk)
    return updated, count


class KnowledgeBaseService:
    """Room-aware retrieval facade; callers never access the index directly."""

    def __init__(
        self,
        rooms_dir: Path | None = None,
        scenarios_dir: Path | None = None,
        scenarios: Mapping[str, Mapping[str, Any]] | None = None,
        vector_store: Any = None,
        embedding_provider: Any = None,
    ):
        self.rooms_dir = Path(rooms_dir) if rooms_dir else None
        self.scenarios_dir = Path(scenarios_dir) if scenarios_dir else None
        self.scenarios = {str(key): dict(value) for key, value in (scenarios or {}).items()}
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self._chunks: dict[tuple[str, str], list[KnowledgeChunk]] = {}

    def _room_info(self, room_id: str) -> dict[str, Any]:
        if not self.rooms_dir:
            return {}
        room_dir = self.rooms_dir / str(room_id)
        info = read_json(room_dir / "info.json", default={})
        state = read_json(room_dir / "state.json", default={})
        if isinstance(info, dict) and isinstance(state, dict) and state.get("active_scene_id") not in (None, ""):
            info = {**info, "active_scene_id": state["active_scene_id"]}
        return info

    def _cursor_path(self, room_id: str) -> Path | None:
        if not self.rooms_dir:
            return None
        return self.rooms_dir / str(room_id) / "knowledge_cursor.json"

    @staticmethod
    def _cursor_key(scenario_id: Any, scenario_version: Any) -> str:
        return f"{scenario_id}@{scenario_version}"

    def _cursor(self, room_id: str, scenario_key: str) -> dict[str, Any]:
        """房间检索游标：``rounds`` 驱动兜底推进，``timed`` 记录章节生命周期状态。

        游标绑定剧本身份：房间换绑/升级剧本后 chunk_id 会重新从 ``chunk-0001``
        开始，旧游标若不失效，新剧本的块会被误判为「已消费」。
        """
        empty: dict[str, Any] = {"scenario": scenario_key, "rounds": [], "timed": {}}
        path = self._cursor_path(room_id)
        if path is None:
            return empty
        data = read_json(path, default={})
        if not isinstance(data, dict) or str(data.get("scenario") or "") != scenario_key:
            return empty
        rounds = data.get("rounds") if isinstance(data.get("rounds"), list) else []
        timed = data.get("timed") if isinstance(data.get("timed"), dict) else {}
        return {"scenario": scenario_key, "rounds": rounds, "timed": timed}

    def _save_cursor(self, room_id: str, cursor: Mapping[str, Any]) -> None:
        path = self._cursor_path(room_id)
        if path is None:
            return
        write_json_atomic(path, dict(cursor))

    @staticmethod
    def _normalize_rounds(history: Any) -> list[list[str]]:
        rounds: list[list[str]] = []
        if isinstance(history, list):
            for item in history:
                if isinstance(item, list):
                    rounds.append([str(value) for value in item if str(value).strip()])
        return rounds

    def _scenario(self, scenario_id: Any, scenario_version: Any = None) -> dict[str, Any] | None:
        if scenario_id in (None, ""):
            return None
        scenario = self.scenarios.get(str(scenario_id))
        if scenario is not None:
            return scenario
        if self.scenarios_dir:
            path, scenario = load_scenario_by_id(self.scenarios_dir, scenario_id, scenario_version=scenario_version)
            if scenario and path:
                scenario = dict(scenario)
                scenario["__descriptor_path"] = str(path)
            return scenario
        return None

    def _get_chunks(self, scenario: Mapping[str, Any]) -> list[KnowledgeChunk]:
        key = (str(scenario.get("id")), _version(scenario, "scenario_version", "version", "version_id"))
        if key not in self._chunks:
            source = scenario.get("__descriptor_path")
            loaded = load_knowledge_index(Path(str(source)), key[1]) if source else []
            self._chunks[key] = loaded or build_knowledge_chunks(scenario)
        return self._chunks[key]

    def _eligible_chunks(
        self,
        scenario: Mapping[str, Any],
        scenario_id: str,
        scenario_version: str,
        current_scene: str | None,
        spoiler_level: int,
        audience: str,
    ) -> list[KnowledgeChunk]:
        """筛出本房间当前可检索的块：版本匹配、场景未切出、未剧透、未归档、可见性允许。"""
        eligible: list[KnowledgeChunk] = []
        for chunk in self._get_chunks(scenario):
            if chunk.scenario_id != scenario_id or chunk.scenario_version != scenario_version:
                continue
            if str(chunk.tier or "").lower() == "archived":
                continue
            if chunk.scene_id not in (None, current_scene):
                continue
            if chunk.spoiler_level > spoiler_level:
                continue
            if audience != "kp" and not is_visible_to_player(chunk.visibility):
                continue
            eligible.append(chunk)
        return eligible

    def _vector_score_map(
        self, query: str, scenario_id: str, scenario_version: str, chunks: list[KnowledgeChunk], top_k: int
    ) -> dict[str, float]:
        """chunk_id → 向量相似度；向量库不可用时退回块自带 embedding 的本地余弦。"""
        store_scores: dict[str, float] = {}
        query_vector: list[float] | None = None
        if query and self.embedding_provider is not None:
            try:
                embedded = self.embedding_provider.embed([str(query)])
                query_vector = embedded[0] if embedded else None
            except Exception as error:
                _log_retrieval_degradation("embedding", error)
                query_vector = None
        if query_vector and self.vector_store is not None:
            try:
                collection = f"scenario_{scenario_id}_{scenario_version}"
                query_store = getattr(self.vector_store, "query", None)
                if callable(query_store):
                    try:
                        vector_results = query_store(
                            query_vector,
                            {
                                "collection": collection,
                                "scenario_id": scenario_id,
                                "scenario_version": scenario_version,
                            },
                            max(20, int(top_k) * 4),
                        )
                    except NotImplementedError:
                        vector_results = self.vector_store.search(collection, query_vector, limit=max(20, int(top_k) * 4))
                else:
                    vector_results = self.vector_store.search(collection, query_vector, limit=max(20, int(top_k) * 4))
                allowed_ids = {chunk.chunk_id for chunk in chunks}
                for item in vector_results:
                    payload = item.get("payload") if isinstance(item, dict) else {}
                    payload = payload if isinstance(payload, dict) else {}
                    chunk_id = str(payload.get("chunk_id") or payload.get("module_id") or item.get("id") or "")
                    if chunk_id in allowed_ids:
                        store_scores[chunk_id] = max(store_scores.get(chunk_id, 0.0), float(item.get("score") or 0.0))
            except Exception as error:
                _log_retrieval_degradation("vector_store", error)
                store_scores = {}

        query_tokens = _tokens(str(query or ""))
        scores: dict[str, float] = {}
        for chunk in chunks:
            score = store_scores.get(chunk.chunk_id, 0.0)
            if not score and chunk.embedding:
                qv = query_vector if query_vector and len(query_vector) == len(chunk.embedding) else [0.0] * len(chunk.embedding)
                if query_vector is None:
                    for token in query_tokens:
                        qv[hash(token) % len(qv)] += 1.0
                norm = math.sqrt(sum(value * value for value in qv)) or 1.0
                cnorm = math.sqrt(sum(value * value for value in chunk.embedding)) or 1.0
                score = sum(a * b for a, b in zip(qv, chunk.embedding)) / (norm * cnorm)
            scores[chunk.chunk_id] = score
        return scores

    def _score_chunks(
        self, chunks: list[KnowledgeChunk], query: str, scenario_id: str, scenario_version: str, top_k: int
    ) -> list[_Candidate]:
        """词法 BM25 分与向量分各自归一化后按查询类型加权，得到候选块打分。

        ``_Candidate.lexical``/``vector`` 保留原始分（供调用方观察），``score``
        是归一化加权后的排序分。
        """
        vector_scores = self._vector_score_map(query, scenario_id, scenario_version, chunks, top_k)
        lexical_values = _bm25_scores(
            [f"{chunk.text} {chunk.card_type} {chunk.unlock_condition or ''}" for chunk in chunks],
            _bm25_tokens(str(query or "")),
        )
        lexical_weight, vector_weight = _score_weights(query)
        lexical_norm = _normalize_scores(list(lexical_values))
        vector_norm = _normalize_scores([vector_scores.get(chunk.chunk_id, 0.0) for chunk in chunks])
        scored: list[_Candidate] = []
        for index, chunk in enumerate(chunks):
            score = lexical_weight * lexical_norm[index] + vector_weight * vector_norm[index]
            if score <= 0:
                continue
            scored.append(
                _Candidate(
                    chunk=chunk,
                    score=score,
                    lexical=lexical_values[index],
                    vector=vector_scores.get(chunk.chunk_id, 0.0),
                    channel="score",
                )
            )
        return scored

    def catalog(self, room_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """返回房间绑定剧本的章节目录（标题/类型/ID）。

        直接导入的剧本没有 scene_manifest，模型缺少整体结构索引；这里按知识块
        顺序给出目录，让模型像普通剧本一样"知道"有哪些章节，再靠每轮注入的
        检索结果读取原文。
        """
        info = self._room_info(room_id)
        scenario = self._scenario(info.get("scenario_id"), info.get("scenario_version"))
        if not scenario:
            return []
        scenario_id = str(scenario.get("id") or info.get("scenario_id"))
        scenario_version = str(info.get("scenario_version") or _version(scenario, "scenario_version", "version", "version_id"))
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in self._get_chunks(scenario):
            if chunk.scenario_id != scenario_id or chunk.scenario_version != scenario_version:
                continue
            # 同一章节可能被拆成多个子块，目录按章节去重，避免重复标题撑大系统提示。
            key = _section_key(chunk)
            if key in seen:
                continue
            seen.add(key)
            metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
            title = str(metadata.get("title") or "").strip() or chunk.chunk_id
            entries.append({"chunk_id": chunk.chunk_id, "title": title, "card_type": chunk.card_type})
            if len(entries) >= limit:
                break
        return entries

    def _keyword_candidates(self, chunks: list[KnowledgeChunk], query: str, enabled: bool) -> list[_Candidate]:
        """关键词通道候选：主键命中即激活（副键按 ``secondary_logic`` 过滤）。"""
        if not enabled:
            return []
        candidates: list[_Candidate] = []
        for chunk in chunks:
            if chunk.is_constant:
                continue
            hit, score = _keyword_hit(chunk, query)
            if hit:
                candidates.append(
                    _Candidate(chunk=chunk, score=score, lexical=0.0, vector=0.0, channel="keyword")
                )
        return candidates

    @staticmethod
    def _apply_timed_gate(
        candidates: list[_Candidate], filtered: list[KnowledgeChunk], timed: Mapping[str, Mapping[str, int]]
    ) -> list[_Candidate]:
        """硬状态机门控：冷却中的条目剔除，粘滞中的条目强制注入。

        粘滞条目即便本轮没有关键词命中也会被重新注入（sticky 语义），保证上下文
        连贯；冷却只挡新激活，粘滞期自身不受自己的冷却影响。
        """
        existing = {_section_key(item.chunk) for item in candidates}
        for chunk in filtered:
            key = _section_key(chunk)
            if key in existing or chunk.is_constant:
                continue
            sticky_active, _cooldown = _timed_flags(key, timed)
            if sticky_active:
                candidates.append(
                    _Candidate(chunk=chunk, score=0.0, lexical=0.0, vector=0.0, channel="sticky", forced=True)
                )
                existing.add(key)
        kept: list[_Candidate] = []
        for item in candidates:
            if item.chunk.is_constant:
                kept.append(replace(item, forced=True))
                continue
            sticky_active, on_cooldown = _timed_flags(_section_key(item.chunk), timed)
            if sticky_active:
                kept.append(replace(item, forced=True))
            elif on_cooldown:
                continue
            else:
                kept.append(item)
        return kept

    @staticmethod
    def _record_timed(
        candidates: list[_Candidate],
        timed: dict[str, dict[str, int]],
        sticky_rounds: int,
        cooldown_rounds: int,
    ) -> None:
        """只对「关键词通道」且非常驻的条目写粘滞/冷却，纯语义命中不写状态。"""
        for item in candidates:
            if item.channel != "keyword" or item.chunk.is_constant:
                continue
            entry = timed.setdefault(_section_key(item.chunk), {})
            if int(entry.get("sticky_remaining", 0)) or int(entry.get("cooldown_remaining", 0)):
                continue
            entry["sticky_remaining"] = max(0, int(item.chunk.sticky_rounds) or int(sticky_rounds))
            entry["cooldown_remaining"] = 0
            entry["pending_cooldown"] = max(0, int(item.chunk.cooldown_rounds) or int(cooldown_rounds))

    def search(
        self,
        room_id: str,
        query: str,
        top_k: int = 5,
        audience: str = "kp",
        sticky_rounds: int = 1,
        cooldown_rounds: int = 3,
        parent_max_chars: int = 2400,
        keyword_channel: bool = True,
        recursive_scanning: bool = True,
        max_recursion_depth: int = 3,
        token_budget: int = 0,
        rng: Any = None,
    ) -> list[dict[str, Any]]:
        info = self._room_info(room_id)
        scenario = self._scenario(info.get("scenario_id"), info.get("scenario_version"))
        if not scenario:
            return []
        scenario_id = str(scenario.get("id") or info.get("scenario_id"))
        scenario_version = str(info.get("scenario_version") or _version(scenario, "scenario_version", "version", "version_id"))
        current_scene = str(info.get("active_scene_id") or info.get("current_scene_id") or "") or None
        spoiler_level = max(0, _int(info.get("spoiler_level") or info.get("current_spoiler_level"), 0))
        filtered = self._eligible_chunks(
            scenario, scenario_id, scenario_version, current_scene, spoiler_level, audience
        )
        if not filtered:
            return []
        sections = _group_by_section(filtered)
        limit = max(1, min(int(top_k), 20))
        scenario_key = self._cursor_key(scenario_id, scenario_version)
        cursor = self._cursor(room_id, scenario_key)
        timed = cursor["timed"]
        rounds = self._normalize_rounds(cursor["rounds"])

        constants = [
            _Candidate(chunk=chunk, score=0.0, lexical=0.0, vector=0.0, channel="constant", forced=True)
            for chunk in filtered
            if chunk.is_constant
        ]
        # 双通道：关键词命中就直接激活（不再依赖语义相似度）；无命中才走 BM25+向量。
        hits = self._keyword_candidates(filtered, str(query or ""), keyword_channel)
        if hits:
            candidates = constants + hits
        else:
            scored = self._score_chunks(filtered, str(query or ""), scenario_id, scenario_version, limit)
            if scored:
                candidates = constants + scored
            else:
                candidates = constants + _fallback_ranked(filtered, _cooldown_keys(rounds, cooldown_rounds), limit)

        # 延迟门：第 delay_rounds 回合前不激活（常驻条目除外）。
        round_index = len(rounds)
        candidates = [
            item for item in candidates
            if item.chunk.is_constant or int(item.chunk.delay_rounds) <= round_index
        ]
        candidates = self._apply_timed_gate(candidates, filtered, timed)
        if not candidates:
            # 硬门控可能把候选全部剔除；导入剧本只有知识块这一条剧情来源，
            # 返回空会让模型声称剧本缺失，因此仍需兜底给出内容（并避开冷却中的章节）。
            candidates = _fallback_ranked(filtered, _cooldown_keys(rounds, cooldown_rounds), limit)
        candidates = _apply_probability(candidates, rng or random)
        if recursive_scanning:
            candidates = _expand_recursive(
                candidates,
                filtered,
                max_depth=max_recursion_depth,
                max_steps=_MAX_RECURSION_STEPS,
                max_activated=_MAX_ACTIVATED_ENTRIES,
            )
        candidates = _resolve_groups(_dedupe_by_section(candidates))
        candidates.sort(key=_candidate_sort_key)

        # 一轮结束：先推进既有生命周期，再登记本轮关键词激活（不覆盖正在计时的条目）。
        _advance_timed(timed)
        self._record_timed(candidates, timed, sticky_rounds, cooldown_rounds)

        ranked = candidates[:limit]
        results = [_render_section(item, sections, parent_max_chars) for item in ranked]
        results = _apply_token_budget(results, int(token_budget))

        rounds.append([_section_key(item.chunk) for item in ranked])
        cursor["rounds"] = rounds[-max(1, int(sticky_rounds), int(cooldown_rounds)):]
        cursor["timed"] = timed
        self._save_cursor(room_id, cursor)
        return results


def search(
    room_id: str,
    query: str,
    *,
    rooms_dir: Path | None = None,
    scenarios_dir: Path | None = None,
    top_k: int = 5,
    audience: str = "kp",
) -> list[dict[str, Any]]:
    """Functional facade kept for API callers that do not need a long-lived service."""
    return KnowledgeBaseService(rooms_dir=rooms_dir, scenarios_dir=scenarios_dir).search(
        room_id, query, top_k=top_k, audience=audience
    )


def knowledge_index_path(descriptor_path: Path, version: Any) -> Path:
    base = descriptor_path.parent.parent if descriptor_path.parent.name == "versions" else descriptor_path.parent
    return base / "knowledge-index" / f"{version}.json"


def index_knowledge_chunks(
    chunks: Iterable[KnowledgeChunk],
    *,
    vector_store: Any = None,
    embedding_provider: Any = None,
) -> list[KnowledgeChunk]:
    """Fill missing embeddings and idempotently write chunks to a vector store."""
    prepared = list(chunks)
    missing = [index for index, chunk in enumerate(prepared) if not chunk.embedding]
    if missing and embedding_provider is not None:
        try:
            vectors = embedding_provider.embed([prepared[index].text for index in missing])
        except Exception:
            vectors = []
        for index, vector in zip(missing, vectors):
            if isinstance(vector, list) and vector:
                prepared[index] = replace(prepared[index], embedding=[float(value) for value in vector])
    if vector_store is not None:
        records = []
        for chunk in prepared:
            if not chunk.embedding:
                continue
            record = chunk.to_dict()
            record["collection"] = f"scenario_{chunk.scenario_id}_{chunk.scenario_version}"
            records.append(record)
        if records:
            vector_store.upsert(records)
    return prepared


def persist_knowledge_index(
    descriptor_path: Path,
    scenario: Mapping[str, Any],
    *,
    vector_store: Any = None,
    embedding_provider: Any = None,
) -> Path:
    version = _version(scenario, "scenario_version", "version", "version_id")
    path = knowledge_index_path(descriptor_path, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = index_knowledge_chunks(
        build_knowledge_chunks(scenario),
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )
    write_json_atomic(path, [chunk.to_dict() for chunk in chunks])
    return path


def write_knowledge_index(descriptor_path: Path, version: Any, chunks: Iterable[KnowledgeChunk]) -> Path:
    """直接写入已有的知识块（用于「直接导入」文档时跳过场景卡的情形）。

    ``persist_knowledge_index`` 只能从场景的 ``modules`` 构建分块；直接导入的
    剧本没有场景卡，因此需要把切分并嵌入后的知识块直接落盘。
    """
    path = knowledge_index_path(descriptor_path, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = [chunk.to_dict() if hasattr(chunk, "to_dict") else dict(chunk) for chunk in chunks]
    write_json_atomic(path, values)
    return path


def load_knowledge_index(descriptor_path: Path, version: Any) -> list[KnowledgeChunk]:
    path = knowledge_index_path(descriptor_path, version)
    if not path.exists() and str(version).isdigit():
        from trpg_server.agents.versioning import normalize_semver
        path = knowledge_index_path(descriptor_path, normalize_semver(version))
    values = read_json(path, default=[])
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            result.append(KnowledgeChunk(**item))
        except TypeError:
            continue
    return result
