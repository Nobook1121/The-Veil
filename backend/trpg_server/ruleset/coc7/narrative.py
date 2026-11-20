"""CoC7 的叙述层词表。

这些词面原先是 ``agents/narrative/extract.py`` / ``resolver.py`` /
``runtime.py`` / ``agents/tools/narrative.py`` 里的硬编码常量；批次 E1
把它们原样搬进规则集声明。**现值逐位不变**：``outcome_labels`` 的键序
同时是结果行正则的交替序（大成功 必须排在 成功 之前），不得重排。
"""

from trpg_server.ruleset.narrative import NarrativeProjection

# SC 标题噪声：SC n 后面跟这些词的行是指令不是地点标题（原 extract._SC_NOISE）。
COC7_SC_NOISE_TOKENS = frozenset(
    {"检定", "理智", "成功", "失败", "大成功", "大失败", "san", "check", "roll"}
)

# 技能词：物产行带这些词即提取为检定交互（原 extract._SKILL_WORDS）。
COC7_SKILL_NAMES = (
    "侦查", "图书馆使用", "聆听", "心理学", "潜行", "追踪", "急救", "医学", "神秘学",
    "历史", "话术", "魅惑", "恐吓", "闪避", "格斗", "射击", "攀爬", "跳跃", "游泳",
    "锁匠", "机械维修", "电气维修", "会计", "法律", "博物学", "导航", "驾驶",
)

# 难度词 → 难度级（原 extract._DIFFICULTY_WORDS）。
COC7_DIFFICULTY_LABELS = {"极难": "extreme", "困难": "hard", "普通": "regular"}

# 结果行词面 → 结果键。键序即正则交替序（原 extract._OUTCOME_RE 的
# 大成功|成功|失败|大失败），必须原样保留，重排会让「大成功」被「成功」吞掉。
COC7_OUTCOME_LABELS = {
    "大成功": "extreme_success",
    "成功": "success",
    "失败": "fail",
    "大失败": "fumble",
}

# 结果归一别名（原 runtime._normalize_result 的 mapping）：中文词面 → 枚举键。
COC7_RESULT_ALIASES = {
    "大成功": "extreme_success",
    "极难成功": "extreme_success",
    "困难成功": "hard_success",
    "成功": "success",
    "失败": "fail",
    "大失败": "fumble",
}

# 触发器族：原样词面（含 critical/failure 与中文词面），仅 resolver 层消费
# （原 resolver._result_family）。与 runtime 的枚举键族是两个层级，不得合并。
COC7_SUCCESS_FAMILY_TERMS = frozenset(
    {"extreme_success", "hard_success", "success", "critical", "大成功", "极难成功", "困难成功", "成功"}
)
COC7_FAIL_FAMILY_TERMS = frozenset({"fail", "failure", "fumble", "失败", "大失败"})

# 结算族：仅归一后枚举键，仅 runtime 层消费（原 runtime._result_family）。
COC7_SUCCESS_FAMILY_KEYS = frozenset({"extreme_success", "hard_success", "success"})
COC7_FAIL_FAMILY_KEYS = frozenset({"fail", "fumble"})

# 结果键回退链（原 runtime._pick_outcome 的 chain 表）。
COC7_OUTCOME_CHAINS = {
    "extreme_success": ("extreme_success", "hard_success", "success", "any"),
    "hard_success": ("hard_success", "success", "any"),
    "success": ("success", "any"),
    "fumble": ("fumble", "fail", "any"),
    "fail": ("fail", "any"),
}

# resolve_interaction 工具的 result 枚举（十值，原 tools/narrative.py）。
COC7_TOOL_RESULT_ENUM = (
    "extreme_success", "hard_success", "success", "fail", "fumble",
    "大成功", "困难成功", "成功", "失败", "大失败",
)

NARRATIVE_PROJECTION = NarrativeProjection(
    sc_noise_tokens=COC7_SC_NOISE_TOKENS,
    skill_names=COC7_SKILL_NAMES,
    difficulty_labels=COC7_DIFFICULTY_LABELS,
    outcome_labels=COC7_OUTCOME_LABELS,
    result_aliases=COC7_RESULT_ALIASES,
    success_family_terms=COC7_SUCCESS_FAMILY_TERMS,
    fail_family_terms=COC7_FAIL_FAMILY_TERMS,
    success_family_keys=COC7_SUCCESS_FAMILY_KEYS,
    fail_family_keys=COC7_FAIL_FAMILY_KEYS,
    outcome_chains=COC7_OUTCOME_CHAINS,
    tool_result_enum=COC7_TOOL_RESULT_ENUM,
)


__all__ = [
    "NARRATIVE_PROJECTION",
    "COC7_SC_NOISE_TOKENS",
    "COC7_SKILL_NAMES",
    "COC7_DIFFICULTY_LABELS",
    "COC7_OUTCOME_LABELS",
    "COC7_RESULT_ALIASES",
    "COC7_SUCCESS_FAMILY_TERMS",
    "COC7_FAIL_FAMILY_TERMS",
    "COC7_SUCCESS_FAMILY_KEYS",
    "COC7_FAIL_FAMILY_KEYS",
    "COC7_OUTCOME_CHAINS",
    "COC7_TOOL_RESULT_ENUM",
]
