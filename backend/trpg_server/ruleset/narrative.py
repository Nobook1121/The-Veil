"""叙述层词表投影：规则集声明建图/触发/结算所用的词面。

核心叙述链（建图抽取 / 触发器 / 交互结算 / Agent 工具 schema）不再硬编码
任何规则集词表：每个消费点按本模块返回的 :class:`NarrativeProjection`
读取词面。规则集插件把声明挂成 ``plugin.narrative`` 模块属性（coc7 的
挂法与 ``options`` 等既有惯例一致，见 ``ruleset/coc7/narrative.py``）。

这里只做两件事：定义投影形态与按规则集解析投影。**没有任何默认词表**——
未声明 ``narrative`` 的规则集拿到全空的中性投影，各消费点在中性语义下
自然降级（不提取检定交互、结果原样透传、工具 enum 退二元），不炸叙述链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple


@dataclass(frozen=True)
class NarrativeProjection:
    """一个规则集在叙述链上用到的全部词面。

    消费点与字段的对应（四族词表 → 三层消费）：

    - 建图抽取（``agents/narrative/extract.py``）：
      ``sc_noise_tokens``（SC 标题噪声）/ ``skill_names``（技能词）/
      ``difficulty_labels``（难度词 → 难度级）/ ``outcome_labels``
      （结果行词面 → 结果键；**键序即正则交替序，消费方必须原样保留**）。
    - 触发器匹配（``agents/narrative/resolver.py``）：
      ``success_family_terms`` / ``fail_family_terms``——原样词面族
      （含 ``critical`` / ``failure`` 与中文词面），与运行时族**分两个
      层级字段**，合并会让 coc7 判定漂移。
    - 交互结算（``agents/narrative/runtime.py``）：
      ``result_aliases``（结果归一键）/ ``outcome_chains``（结果键回退链）/
      ``success_family_keys`` / ``fail_family_keys``（仅归一后枚举键的族）。
    - Agent 工具 schema（``agents/tools/narrative.py``）：
      ``tool_result_enum``（``narrative.resolve_interaction`` 的 result 枚举）。

    中性语义（所有字段为空时的消费方行为，测试锚定）：

    - ``skill_names`` 空 → 不提取检定交互；
    - ``difficulty_labels`` 空 → 难度恒 ``regular``；
    - ``outcome_labels`` 空 → 结果行不分桶，落 any/beats；
    - ``sc_noise_tokens`` 空 → SC 标题不做规则集噪声过滤，按通用标题处理；
    - ``result_aliases`` 空 → 结果原样透传；
    - 族字段空 → 字面相等匹配；
    - ``outcome_chains`` 缺结果键 → 退 ``(result, "any")``；
    - ``tool_result_enum`` 空 → 退 ``("success", "fail")``。
    """

    sc_noise_tokens: frozenset[str] = field(default_factory=frozenset)
    skill_names: tuple[str, ...] = ()
    difficulty_labels: Mapping[str, str] = field(default_factory=dict)
    outcome_labels: Mapping[str, str] = field(default_factory=dict)
    result_aliases: Mapping[str, str] = field(default_factory=dict)
    success_family_terms: frozenset[str] = field(default_factory=frozenset)
    fail_family_terms: frozenset[str] = field(default_factory=frozenset)
    success_family_keys: frozenset[str] = field(default_factory=frozenset)
    fail_family_keys: frozenset[str] = field(default_factory=frozenset)
    outcome_chains: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    tool_result_enum: tuple[str, ...] = ("success", "fail")


NEUTRAL_NARRATIVE_PROJECTION = NarrativeProjection()


def projection_of(plugin: Any) -> NarrativeProjection:
    """读取插件声明的叙述词表；未声明或形态不对时返回中性投影。

    仿 ``ruleset.prompts.role_prompt`` 的 getattr 式取用：核心不 import
    任何插件包内部实现，插件把词表模块挂成 ``narrative`` 属性即可。
    """
    provider = getattr(plugin, "narrative", None)
    projection = getattr(provider, "NARRATIVE_PROJECTION", None)
    if isinstance(projection, NarrativeProjection):
        return projection
    return NEUTRAL_NARRATIVE_PROJECTION


def projection_for_ruleset(ruleset_id: str | None) -> NarrativeProjection:
    """按规则集 id 解析投影；空 id 走默认规则集，解析失败回中性。

    叙述链的兜底语义是「不炸」：未知/不可用规则集拿到中性投影后按中性
    语义降级，而不是抛 ``RulesetSelectionError`` 中断建图或结算。
    """
    try:
        from trpg_server.ruleset import builtin_registry

        plugin = builtin_registry().resolve(ruleset_id)
    except Exception:  # noqa: BLE001 - 注册表不可用/规则集缺席时叙述链降级
        return NEUTRAL_NARRATIVE_PROJECTION
    return projection_of(plugin)


__all__ = [
    "NEUTRAL_NARRATIVE_PROJECTION",
    "NarrativeProjection",
    "projection_for_ruleset",
    "projection_of",
]
