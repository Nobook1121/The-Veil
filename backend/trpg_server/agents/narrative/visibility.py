"""三态可见性与自动分类（设计文档 §4.6、§7.5.1、§7.5.2）。

原则：
- 显式作者值优先，但自动判定只能把内容「收紧」，不得把 `hidden/kp_only` 放宽；
- `hidden` 需要可解析的 `reveal_when`，否则进入人工复核；
- `kp_only` 永不因检定转为可见。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from trpg_server.agents.narrative.model import (
    VISIBILITY_HIDDEN,
    VISIBILITY_KP_ONLY,
    VISIBILITY_PUBLIC,
    VISIBILITY_STATES,
    NarrativeBundle,
)

_LEGACY_MAP = {
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
_VISIBILITY_RANK = {VISIBILITY_PUBLIC: 0, VISIBILITY_HIDDEN: 1, VISIBILITY_KP_ONLY: 2}

_KP_MODULE_TYPES = {"background", "opening", "preparation", "timeline", "monster", "ending"}
_TITLE_KP_RE = re.compile(
    r"真相|幕后|秘密|隐情|动机|真实身份|暗线|剧情梗概|守秘人|KP|主持人|不可告知|结局条件|时间线|怪物数据",
    re.I,
)
_FORBID_RE = re.compile(r"不要告诉玩家|玩家不知道|调查员不可知|仅守秘人|不可告知玩家", re.I)
_TRUTH_RE = re.compile(
    r"实际上|事实上|真相是|他其实是|她其实是|伪装成|冒充|并不知道|误以为|真实目的是", re.I
)
_DATA_BLOCK_RE = re.compile(r"\b(?:STR|CON|DEX|POW|APP|INT|SIZ|EDU|HP|SAN|MP)\b|伤害|格斗|闪避|技能[:：]", re.I)
_PERCEPTIBLE_RE = re.compile(r"你们看到|你注意到|映入眼帘|空气中弥漫|你能看到|耳边响起", re.I)
_SPACE_SECRET_RE = re.compile(r"暗门|密道|夹层|暗格|地下室|阁楼|墙缝|密室", re.I)
_REVEAL_VERB_RE = re.compile(r"推开|搬开|撬开|找到钥匙|拉开帷幕|水位到|血池满|转动机关|念出咒语", re.I)
_KEYWORD_SPOILER_RE = re.compile(r"真名|化名|秘密组织|真相|幕后", re.I)
_REVEALABLE_RE = re.compile(r"技能|检定|线索|时钟|旗标|条件|需要|必须|达到|成功", re.I)
_QUOTED_RE = re.compile(r"[「『“\"]([^「」『』”\"\n]{2,24})[」』”\"]")

_GENERIC_NAMES = {"他", "她", "它", "他们", "她们", "玩家", "调查员", "守秘人", "真相", "秘密", "结局", "房间"}


def normalize_visibility(value: Any, default: str = VISIBILITY_PUBLIC) -> str:
    """旧值 `public/kp/player_visible` → 三态；未知值按 fail-closed 收紧。"""
    text = str(value or "").strip().lower()
    if not text:
        return default
    mapped = _LEGACY_MAP.get(text)
    if mapped:
        return mapped
    # 未识别的可见性字段不能默认放行。
    return VISIBILITY_KP_ONLY


def visibility_rank(value: Any) -> int:
    return _VISIBILITY_RANK.get(normalize_visibility(value), 2)


def tighten(current: str, candidate: str) -> str:
    """只升不降：返回更严格的一侧。"""
    return current if visibility_rank(current) >= visibility_rank(candidate) else candidate


def is_visible_to_player(visibility: str, *, revealed: bool = False) -> bool:
    state = normalize_visibility(visibility)
    if state == VISIBILITY_PUBLIC:
        return True
    if state == VISIBILITY_HIDDEN:
        return bool(revealed)
    return False


def is_visible_to_audience(visibility: str, audience: str, *, revealed: bool = False) -> bool:
    if str(audience).strip().lower() in {"kp", "keeper", "gm", "admin"}:
        return True
    return is_visible_to_player(visibility, revealed=revealed)


def redact_kp_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """移除只对 KP 可见的字段；用于所有玩家侧 payload。"""
    return {key: value for key, value in payload.items() if key not in {"kp_notes", "kp_only"}}


@dataclass
class VisibilityDecision:
    visibility: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    source: str = "auto"
    needs_review: bool = False
    keywords_visibility: str = VISIBILITY_PUBLIC

    def to_dict(self) -> dict[str, Any]:
        return {
            "visibility": self.visibility,
            "visibility_confidence": round(self.confidence, 2),
            "visibility_reasons": list(self.reasons),
            "visibility_source": self.source,
            "needs_review": self.needs_review,
            "keywords_visibility": self.keywords_visibility,
        }


def classify_visibility(
    *,
    kind: str = "location",
    module_type: str = "",
    title: str = "",
    summary: str = "",
    body: str = "",
    kp_notes: str = "",
    keywords: Iterable[str] | None = None,
    reveal_when: Mapping[str, Any] | None = None,
    explicit: str | None = None,
) -> VisibilityDecision:
    """确定性规则打分（§7.5.1），不调用任何模型。"""
    text = "\n".join(part for part in (title, summary, body) if part)
    module = str(module_type or "").strip().lower()
    score = 0.0
    hidden_score = 0.0
    reasons: list[str] = []

    if module in _KP_MODULE_TYPES:
        score = max(score, 1.0)
        reasons.append(f"module:{module}")
    if title and _TITLE_KP_RE.search(title):
        score = max(score, 0.8)
        reasons.append("title_keyword")
    combined = "\n".join(part for part in (title, summary, body, kp_notes) if part)
    if _FORBID_RE.search(combined):
        score = max(score, 1.0)
        reasons.append("explicit_no_disclose")
    truth_hits = len(_TRUTH_RE.findall(text))
    if truth_hits:
        score = max(score, min(0.9, 0.3 * truth_hits))
        reasons.append(f"truth_phrase:{truth_hits}")
    if _DATA_BLOCK_RE.search(text):
        score = max(score, 0.7)
        reasons.append("stat_block")
    if str(kind).strip().lower() == "concept" and _TRUTH_RE.search(text):
        score = max(score, 0.8)

    if _SPACE_SECRET_RE.search(text):
        hidden_score = max(hidden_score, 0.4)
        reasons.append("spatial_secret")
    if _REVEAL_VERB_RE.search(text) and _REVEALABLE_RE.search(text):
        hidden_score = max(hidden_score, 0.5)
        reasons.append("reveal_verb")
    if str(kind).strip().lower() == "clue":
        hidden_score = max(hidden_score, 0.4)
        reasons.append("clue_default_hidden")
    if str(kind).strip().lower() == "ending":
        score = max(score, 1.0)
        reasons.append("ending_default_kp_only")

    keyword_list = [str(item).strip() for item in (keywords or []) if str(item).strip()]
    keyword_spoiler = any(_KEYWORD_SPOILER_RE.search(item) for item in keyword_list)
    if keyword_spoiler:
        score = max(score, 0.6)
        reasons.append("keyword_spoiler")

    has_reveal = bool(reveal_when)
    needs_review = False
    if score >= 0.8:
        auto = VISIBILITY_KP_ONLY
        confidence = min(0.99, score)
    elif hidden_score >= 0.4 or score >= 0.4:
        auto = VISIBILITY_HIDDEN
        confidence = min(0.95, max(score, hidden_score))
        if not has_reveal:
            needs_review = True
            reasons.append("hidden_without_reveal_when")
    else:
        auto = VISIBILITY_PUBLIC
        confidence = 0.6 if (_PERCEPTIBLE_RE.search(text) or keyword_list) else 0.4
        if _PERCEPTIBLE_RE.search(text):
            reasons.append("perceptible")

    decision = VisibilityDecision(visibility=auto, confidence=confidence, reasons=reasons, needs_review=needs_review)

    explicit_state = normalize_visibility(explicit) if explicit not in (None, "") else None
    if explicit_state:
        merged = tighten(explicit_state, auto)
        decision.visibility = merged
        decision.source = "author" if merged == explicit_state else "auto+author"
        decision.confidence = 1.0 if merged == explicit_state else max(confidence, 0.9)
        decision.reasons.append(f"author:{explicit_state}")
        if merged == VISIBILITY_HIDDEN and not has_reveal:
            decision.needs_review = True

    if decision.visibility == VISIBILITY_KP_ONLY:
        decision.keywords_visibility = VISIBILITY_KP_ONLY
    elif keyword_spoiler:
        decision.keywords_visibility = VISIBILITY_KP_ONLY
    return decision


def forbidden_names(bundle: NarrativeBundle) -> list[str]:
    """`kp_only` 与未揭示 `hidden` 的专名，供输出侧闸门使用。"""
    names: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in _GENERIC_NAMES or len(text) > 40:
            return
        if text not in names:
            names.append(text)

    for node in bundle.nodes:
        if node.visibility != VISIBILITY_KP_ONLY:
            continue
        for value in (node.title, *node.aliases, *node.keywords):
            _add(value)
        for quoted in _QUOTED_RE.findall(node.kp_notes or ""):
            _add(quoted)
    for edge in bundle.edges:
        if edge.visibility == VISIBILITY_KP_ONLY:
            _add(edge.label)
    return names


def assert_no_kp_only(content: str, bundle: NarrativeBundle) -> list[str]:
    """返回在文本中出现的 KP-only 专名；空列表表示通过。"""
    text = str(content or "")
    return [name for name in forbidden_names(bundle) if name and name in text]
