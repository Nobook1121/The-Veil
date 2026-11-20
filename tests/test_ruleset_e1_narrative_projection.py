"""批次 E1（Slice 1 收口）：叙述词表投影的逐位对拍与中性降级锚定。

`ruleset/narrative.py` + `ruleset/coc7/narrative.py` 已把四个消费点
（建图抽取 / 触发器 / 交互结算 / Agent 工具 schema）的词面切到投影
读取。本文件锁三件事：

1. **coc7 逐位对拍**：投影持有与迁移前硬编码常量完全一致的快照——
   27 技能词、十值工具枚举、结果行交替键序、别名 / 回退链。快照写死
   在本文件（迁移前的原值已物理删除，这里保有一份独立锚；投影侧任何
   「顺手整理」都会在此露馅）。
2. **双层级族不可合并**：resolver 层的原样词面族（含 ``critical`` /
   ``failure`` 与中文词面）与 runtime 层的枚举键族对同一个词归属
   不同——合并会让 ``critical`` 在结算层凭空变成成功族。
3. **dnd5e / 缺席中性**：真插件在场但无 ``narrative`` 声明、注册表
   整体缺席、``projection_of`` 的三种 stub 形态全部退中性投影；
   八条中性语义逐条经真实消费点锚定，含前任留的 SC 空噪声读法
   （``token not in ∅`` 恒真 → SC 行一律按通用 2 级标题接受）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.narrative.extract import (
    _document_from_text,
    _heading_info,
    _interaction_from_line,
    _outcome_key,
    _outcome_line_re,
    extract_narrative,
)
from trpg_server.agents.narrative.resolver import _result_family as resolver_family
from trpg_server.agents.narrative.runtime import (
    _normalize_result,
    _pick_outcome,
    _result_family as runtime_family,
)
from trpg_server.agents.tools.narrative import (
    NARRATIVE_TOOL_NAMES,
    _resolve_interaction_tool,
    narrative_tools,
)
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.narrative import (
    NEUTRAL_NARRATIVE_PROJECTION,
    NarrativeProjection,
    projection_for_ruleset,
    projection_of,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"

# ------------------------------------------------------------- 迁移前快照
# 这些值在迁移前分别硬编码于 extract._SKILL_WORDS / tools/narrative.py 的
# result enum / extract._OUTCOME_RE 与 runtime._normalize_result ——快照锚定
# 投影搬家零漂移，改投影必须连带改这里并在变更说明里写明。

COC7_SKILL_NAMES_SNAPSHOT = (
    "侦查", "图书馆使用", "聆听", "心理学", "潜行", "追踪", "急救", "医学", "神秘学",
    "历史", "话术", "魅惑", "恐吓", "闪避", "格斗", "射击", "攀爬", "跳跃", "游泳",
    "锁匠", "机械维修", "电气维修", "会计", "法律", "博物学", "导航", "驾驶",
)
COC7_TOOL_ENUM_SNAPSHOT = (
    "extreme_success", "hard_success", "success", "fail", "fumble",
    "大成功", "困难成功", "成功", "失败", "大失败",
)
NARRATIVE_TOOL_NAMES_SNAPSHOT = (
    "narrative.get_current_location",
    "narrative.lookup_location",
    "narrative.list_exits",
    "narrative.move_to_location",
    "narrative.get_area",
    "narrative.get_poi",
    "narrative.travel_to",
    "narrative.inspect_object",
    "narrative.resolve_interaction",
    "narrative.reveal_clue",
    "narrative.update_npc_state",
    "narrative.write_fact",
)
# 结果行词面的键序即正则交替序：「大成功」必须排在「成功」之前，
# 重排会让「大成功」被更短前缀「成功」吞掉。
COC7_OUTCOME_LABEL_KEYS_SNAPSHOT = ("大成功", "成功", "失败", "大失败")
COC7_RESULT_ALIASES_SNAPSHOT = {
    "大成功": "extreme_success",
    "极难成功": "extreme_success",
    "困难成功": "hard_success",
    "成功": "success",
    "失败": "fail",
    "大失败": "fumble",
}

# 「大成功」样本词的提取锚：物件 + 技能交互 + 四条结果行。
# （幕标题避开 location 词——「书房」会把它吃成地点节点。）
EXTREME_SAMPLE = "\n".join(
    [
        "第一章 夜幕降临",
        "SC 1 玄关",
        "门锁着。",
        "- 书架：积灰的书架。",
        "- 侦查：翻找书架上的记事本。",
        "大成功：找到写满咒文的日记。",
        "成功：翻出几页手稿。",
        "失败：扬起一阵灰尘。",
        "大失败：书架轰然倒塌。",
    ]
)


@pytest.fixture
def coc7_projection() -> NarrativeProjection:
    return projection_for_ruleset("coc7")


@pytest.fixture
def dnd5e_plugin_process():
    """真插件装配（data/plugins 下的 dnd5e），照 D1/B1 同源手法。

    dnd5e 是「在场但未声明 narrative 能力」的形态——中性投影必须由
    ``projection_of`` 的缺省路径给出，而不是靠规则集缺席碰巧发生。
    """
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    reset_builtin_registry()
    assert "dnd5e" in builtin_registry().ids()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture
def coc7_absent(monkeypatch):
    """注册表缺席（P0 手法）：插件模块清单置空 + 清第三方插件运行时。"""
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    yield builtin_registry()
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


# ------------------------------------------------------------- coc7 逐位对拍


def test_coc7_skill_names_match_pre_migration_snapshot(coc7_projection):
    assert coc7_projection.skill_names == COC7_SKILL_NAMES_SNAPSHOT
    assert len(coc7_projection.skill_names) == 27


def test_coc7_tool_enum_and_tool_names_align(coc7_projection):
    """十值枚举 + 工具名 12 项同名同序（枚举经 narrative_tools() 构建期解析）。"""
    assert coc7_projection.tool_result_enum == COC7_TOOL_ENUM_SNAPSHOT
    assert NARRATIVE_TOOL_NAMES == list(NARRATIVE_TOOL_NAMES_SNAPSHOT)
    assert [tool.name for tool in narrative_tools()] == list(NARRATIVE_TOOL_NAMES_SNAPSHOT)
    resolve_tool = next(tool for tool in narrative_tools() if tool.name == "narrative.resolve_interaction")
    assert resolve_tool.parameters["properties"]["result"]["enum"] == list(COC7_TOOL_ENUM_SNAPSHOT)


def test_coc7_outcome_label_order_is_regex_alternation_order(coc7_projection):
    """键序即 ``_outcome_line_re`` 的交替序：大成功 必须排在 成功 之前。"""
    assert tuple(coc7_projection.outcome_labels) == COC7_OUTCOME_LABEL_KEYS_SNAPSHOT
    assert dict(coc7_projection.outcome_labels) == {
        "大成功": "extreme_success",
        "成功": "success",
        "失败": "fail",
        "大失败": "fumble",
    }
    pattern = _outcome_line_re(coc7_projection).pattern
    assert pattern.index("大成功") < pattern.index("成功")


def test_coc7_result_aliases_and_chains_match_pre_migration(coc7_projection):
    assert dict(coc7_projection.result_aliases) == COC7_RESULT_ALIASES_SNAPSHOT
    assert coc7_projection.outcome_chains == {
        "extreme_success": ("extreme_success", "hard_success", "success", "any"),
        "hard_success": ("hard_success", "success", "any"),
        "success": ("success", "any"),
        "fumble": ("fumble", "fail", "any"),
        "fail": ("fail", "any"),
    }


def test_coc7_families_are_two_layers_not_one(coc7_projection):
    """双层级族：resolver 词面族含 critical / failure / 中文；runtime 仅枚举键。

    两层对 ``critical`` 的归属不同——这正是族集合不可合并的机械证据。
    """
    # resolver 层：原样词面族。
    assert "critical" in coc7_projection.success_family_terms
    assert "failure" in coc7_projection.fail_family_terms
    assert {"大成功", "极难成功", "困难成功", "成功"} <= coc7_projection.success_family_terms
    assert {"fail", "fumble", "失败", "大失败"} <= coc7_projection.fail_family_terms
    # runtime 层：仅归一后枚举键。
    assert coc7_projection.success_family_keys == frozenset({"extreme_success", "hard_success", "success"})
    assert coc7_projection.fail_family_keys == frozenset({"fail", "fumble"})
    # 同一个词在两层归属不同：critical 在词面层是成功族，在结算层原样透传。
    assert resolver_family("critical", coc7_projection) == "success"
    assert runtime_family("critical", coc7_projection) == "critical"
    # 中文词面在结算层先经 aliases 归一再入族（殊途同归），在词面层直接命中。
    assert resolver_family("大成功", coc7_projection) == "success"
    assert runtime_family("大成功", coc7_projection) == "success"


def test_coc7_extreme_success_extraction_is_unchanged(coc7_projection):
    """「大成功」样本词经 extract_narrative 提取与迁移前一致。"""
    assert _outcome_key("大成功", coc7_projection) == "extreme_success"
    extracted = extract_narrative(EXTREME_SAMPLE, scenario_id="e1", ruleset_id="coc7")
    location = next(node for node in extracted.nodes if node.kind == "location")
    bookshelf = next(item for item in location.objects if item["name"] == "书架")
    assert bookshelf["interactions"], "coc7 投影下技能词命中必须产出检定交互"
    interaction = bookshelf["interactions"][0]
    assert interaction["skill"] == "侦查"
    assert set(interaction["outcomes"]) == {"extreme_success", "success", "fail", "fumble"}
    assert "日记" in interaction["outcomes"]["extreme_success"]["text"]
    assert "倒塌" in interaction["outcomes"]["fumble"]["text"]
    assert "any" not in interaction["outcomes"]


def test_coc7_pick_outcome_uses_declared_chain(coc7_projection):
    """coc7 链：extreme_success 缺失时逐级回退 hard_success → success → any。"""
    outcomes = {"success": {"text": "S"}, "any": {"text": "A"}}
    assert _pick_outcome(outcomes, "extreme_success", coc7_projection) == {"text": "S"}
    assert _pick_outcome(outcomes, "fumble", coc7_projection) == {"text": "A"}


# ------------------------------------------------------- dnd5e / stub / 缺席


def test_dnd5e_plugin_without_narrative_is_neutral(dnd5e_plugin_process):
    """真插件在场但未声明 ``narrative`` 能力 → 中性投影（不是缺席碰巧）。"""
    projection = projection_for_ruleset("dnd5e")
    assert projection == NEUTRAL_NARRATIVE_PROJECTION
    plugin = builtin_registry().resolve("dnd5e")
    assert getattr(plugin, "narrative", None) is None
    assert projection_of(plugin) == NEUTRAL_NARRATIVE_PROJECTION


def test_projection_of_stub_shapes():
    """仿 role_prompt 的 getattr 取用：三种 stub 形态全部退中性。"""
    class _NoNarrative:
        options = object()  # 有其他能力属性，唯独没有 narrative。

    class _MalformedNarrative:
        narrative = object()  # 声明了槽但没挂 NARRATIVE_PROJECTION。

    class _ValidNarrative:
        class narrative:  # noqa: N801 - 模块属性的形态仿真
            NARRATIVE_PROJECTION = NarrativeProjection(skill_names=("侦查",))

    valid = _ValidNarrative().narrative.NARRATIVE_PROJECTION
    assert projection_of(_NoNarrative()) == NEUTRAL_NARRATIVE_PROJECTION
    assert projection_of(_MalformedNarrative()) == NEUTRAL_NARRATIVE_PROJECTION
    assert projection_of(_ValidNarrative()) is valid


def test_absent_registry_falls_back_to_neutral_everywhere(coc7_absent):
    """注册表缺席：任何 id（含默认 / coc7 / dnd5e）都拿中性投影，叙述链不炸。"""
    for ruleset_id in ("", "coc7", "dnd5e", "anything-else"):
        assert projection_for_ruleset(ruleset_id) == NEUTRAL_NARRATIVE_PROJECTION
    # 工具 schema 构建期取默认规则集 → 空注册表下 resolve_interaction 退二元枚举。
    resolve_tool = next(tool for tool in narrative_tools() if tool.name == "narrative.resolve_interaction")
    assert resolve_tool.parameters["properties"]["result"]["enum"] == ["success", "fail"]


# ------------------------------------------------- 八条中性语义逐条锚定


def test_neutral_semantics_extract_layer(coc7_projection):
    neutral = NEUTRAL_NARRATIVE_PROJECTION
    # (1) skill_names 空 → 不提取检定交互。
    assert _interaction_from_line("侦查：翻找记事本", "obj_1", neutral) is None
    # (2) difficulty_labels 空 → 难度恒 regular（技能词与难度词是两个独立降级）。
    only_skills = NarrativeProjection(skill_names=("侦查",))
    interaction = _interaction_from_line("侦查 极难：翻找记事本", "obj_1", only_skills)
    assert interaction is not None and interaction["difficulty"] == "regular"
    # (3) outcome_labels 空 → 结果行不分桶（不生成正则）。
    assert _outcome_line_re(neutral) is None
    # (4) SC 空噪声读法锚定：token not in ∅ 恒真 → SC 行一律按通用 2 级标题
    #     接受；coc7 投影下同一行是噪声、不当作标题。
    assert _heading_info("SC 5 检定：意志", neutral) == (2, "检定：意志")
    assert _heading_info("SC 5 检定：意志", coc7_projection) is None


def test_neutral_semantics_extraction_end_to_end(dnd5e_plugin_process):
    """dnd5e 投影下「大成功」样本：物件照建、检定交互不提取、结果行不分桶。"""
    extracted = extract_narrative(EXTREME_SAMPLE, scenario_id="e1", ruleset_id="dnd5e")
    location = next(node for node in extracted.nodes if node.kind == "location")
    bookshelf = next(item for item in location.objects if item["name"] == "书架")
    assert bookshelf["interactions"] == []
    for item in location.objects:
        for interaction in item["interactions"]:
            assert "extreme_success" not in interaction["outcomes"]


def test_neutral_semantics_runtime_and_tools():
    neutral = NEUTRAL_NARRATIVE_PROJECTION
    # (5) result_aliases 空 → 结果原样透传；空文本回 success 是守卫不是词表。
    assert _normalize_result("大成功", neutral) == "大成功"
    assert _normalize_result("critical", neutral) == "critical"
    assert _normalize_result("", neutral) == "success"
    # (6) 族字段空 → 字面相等匹配（枚举键不再归族）。
    assert runtime_family("extreme_success", neutral) == "extreme_success"
    assert runtime_family("fumble", neutral) == "fumble"
    assert resolver_family("critical", neutral) == "critical"
    # (7) outcome_chains 缺结果键 → 退 (result, "any")。
    outcomes = {"success": {"text": "S"}, "any": {"text": "A"}}
    assert _pick_outcome(outcomes, "extreme_success", neutral) == {"text": "A"}
    assert _pick_outcome({}, "fail", neutral) is None
    # (8) tool_result_enum 空 → 退二元枚举。
    assert neutral.tool_result_enum == ("success", "fail")
    resolve_tool = _resolve_interaction_tool(neutral)
    assert resolve_tool.parameters["properties"]["result"]["enum"] == ["success", "fail"]


def test_absent_registry_extraction_degrades_neutrally(coc7_absent):
    """缺席注册表下建图链路不炸：样本按中性语义降级（与 dnd5e 在场同形）。"""
    extracted = extract_narrative(EXTREME_SAMPLE, scenario_id="e1", ruleset_id="coc7")
    location = next(node for node in extracted.nodes if node.kind == "location")
    bookshelf = next(item for item in location.objects if item["name"] == "书架")
    assert bookshelf["interactions"] == []
