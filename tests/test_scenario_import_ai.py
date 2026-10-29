from __future__ import annotations

import time

import pytest

from trpg_server.scenario_import_ai import (
    AnalysisReport,
    AIBudgetExceeded,
    EmptyAIResponse,
    GraphGenerationError,
    MAX_GRAPH_DETAIL_MODULES,
    _detail_batches,
    _request_json,
    _scan_bounds,
    apply_noise_removal,
    analyze_script,
    build_graph,
    locate_exclusion,
    response_content,
    salvage_partial_json,
)

SCRIPT = "\n".join(
    [
        "# 旧宅",
        "调查员在雨夜抵达旧宅。",
        "",
        "# 作者吐槽",
        "这个本真难写，我边写边想睡觉。",
        "",
        "# 地下室",
        "地下室有一扇锁着的铁门，钥匙在管家身上。",
        "",
        "## 结局",
        "玩家打开铁门逃出旧宅。",
    ]
)


def _fake_requester(prompt: str) -> str:
    """按阶段返回固定 JSON；用于验证「AI 出内容、程序做归一化」的链路。"""
    if "无关的文字" in prompt:
        return (
            '{"exclusions":[{"prefix":"作者吐槽","line_start":4,"line_end":5,'
            '"category":"author_note","reason":"作者自述，与剧情无关"}]}'
        )
    if "划分成可独立检索的模块" in prompt:
        # 剔除 4-5 行后原文剩 8 行，模块行号基于清洗后的文档。
        return (
            '{"modules":['
            '{"title":"旧宅","module_type":"scene","start_line":1,"end_line":3,"summary":"雨夜抵达旧宅。"},'
            '{"title":"地下室","module_type":"scene","start_line":4,"end_line":6,"summary":"锁着的铁门与管家。"},'
            '{"title":"结局","module_type":"ending","start_line":7,"end_line":8,"summary":"逃出旧宅。"}]}'
        )
    if "世界书" in prompt:
        return (
            '{"modules":['
            '{"id":"module_001","keywords":["旧宅","雨夜"],"is_constant":false,"tier":"background"},'
            '{"id":"module_002","keywords":["地下室","铁门"],"is_constant":false,"tier":"core"},'
            '{"id":"module_003","keywords":["结局","逃出旧宅"],"is_constant":false,"tier":"core"}]}'
        )
    if "生成世界树图谱" in prompt:
        return (
            '{"topology":"hub","start":"module_001",'
            '"nodes":[{"id":"module_001","kind":"location","title":"旧宅","summary":"雨夜的旧宅。"},'
            '{"id":"butler","kind":"npc","title":"管家","summary":"身上带着钥匙。"},'
            '{"id":"key","kind":"item","title":"铜钥匙"}],'
            '"edges":[{"from":"module_001","to":"module_002","type":"adjacent","label":"楼梯"},'
            '{"from":"module_001","to":"nowhere","type":"portal"},'
            '{"from":"butler","to":"key","type":"uses"},'
            '{"from":"butler","to":"module_001","type":"inhabits"}]}'
        )
    return "{}"


def test_analyze_script_runs_full_chain_and_reports_exclusions():
    bundle = analyze_script(SCRIPT, title="旧宅", request=_fake_requester, scenario_id="77")

    assert bundle["format"] == "the-veil-scenario-bundle"
    assert len(bundle["modules"]) == 3
    assert bundle["modules"][0]["id"] == "module_001"
    assert bundle["modules"][0]["keywords"] == ["旧宅", "雨夜"]
    # 剔除内容与理由必须回传到剧本包，便于导入后审计。
    assert bundle["exclusions"][0]["prefix"] == "作者吐槽"
    assert bundle["exclusions"][0]["category"] == "author_note"
    assert bundle["exclusions"][0]["reason"] == "作者自述，与剧情无关"
    assert bundle["analysis"]["ai_calls"] >= 4
    # AI 只给了第一个地点节点，另外两个由 normalize_bundle 补齐。
    assert {"module_001", "butler", "key"} <= {node["id"] for node in bundle["narrative"]["nodes"]}


def test_scan_noise_is_not_applied_when_prefix_cannot_be_located():
    lines = SCRIPT.split("\n")
    located = locate_exclusion(lines, {"prefix": "不存在的段落", "line_start": 3}, window_start=1, window_end=len(lines))
    assert located is None


def test_apply_noise_removal_merges_overlapping_ranges_and_keeps_other_lines():
    cleaned = apply_noise_removal(
        "a\nb\nc\nd\ne",
        [
            {"line_start": 2, "line_end": 2},
            {"line_start": 3, "line_end": 4},
        ],
    )
    assert cleaned.split("\n") == ["a", "e"]


def test_build_graph_always_runs_asset_pass_and_merges_same_npc():
    """短剧本也必须补资产；补全批次用不同 id 的同一 NPC 要并入已有节点。"""
    modules = [
        {"id": "car4", "title": "四号车厢", "module_type": "scene", "content": "乘务员在四号车厢。"},
        {"id": "car3", "title": "三号车厢", "module_type": "scene", "content": "三号车厢有钥匙和怪物。"},
    ]

    def request(prompt: str) -> str:
        if "补全资产节点" in prompt:
            return (
                '{"nodes":['
                '{"id":"steward","kind":"npc","title":"乘务员"},'
                '{"id":"key","kind":"item","title":"钥匙"},'
                '{"id":"monster","kind":"npc","title":"怪物"}],'
                '"edges":['
                '{"from":"steward","to":"car4","type":"inhabits"},'
                '{"from":"car3","to":"key","type":"contains"},'
                '{"from":"monster","to":"car3","type":"inhabits"}]}'
            )
        return (
            '{"topology":"linear","start":"car4",'
            '"nodes":['
            '{"id":"car4","kind":"location","title":"四号车厢"},'
            '{"id":"car3","kind":"location","title":"三号车厢"},'
            '{"id":"npc1","kind":"npc","title":"乘务员"}],'
            '"edges":[{"from":"car4","to":"car3","type":"adjacent"}]}'
        )

    graph = build_graph("常暗之厢", modules, "乘务员在四号车厢。三号车厢有钥匙和怪物。", request, AnalysisReport())

    ids = {node["id"] for node in graph["nodes"]}
    assert {"car4", "car3", "npc1", "key", "monster"} == ids  # steward 合并进 npc1，没有重复
    keys = {(edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]}
    assert ("npc1", "car4", "inhabits") in keys
    assert ("car3", "key", "contains") in keys
    assert ("monster", "car3", "inhabits") in keys


def test_build_graph_rejects_location_only_result():
    """AI 只返回地点时不能静默发布骨架图，必须中止并说明原因。"""
    modules = [{"id": "car4", "title": "四号车厢", "module_type": "scene", "content": "四号车厢。"}]

    def request(_prompt: str) -> str:
        return '{"topology":"linear","start":"car4","nodes":[{"id":"car4","kind":"location","title":"四号车厢"}],"edges":[]}'

    with pytest.raises(GraphGenerationError):
        build_graph("常暗之厢", modules, "四号车厢。", request, AnalysisReport())


def test_locate_exclusion_tolerates_ellipsis_and_script_variants():
    lines = ["# 标题", "模组来源：原作者：8版，改动说明如下。", "正文"]
    located = locate_exclusion(
        lines,
        {"prefix": "模組來源: 原作者: 8…", "line_start": 2, "line_end": 2, "category": "meta", "reason": "来源说明"},
        window_start=1,
        window_end=3,
    )
    assert located is not None
    assert (located["line_start"], located["line_end"]) == (2, 2)


def test_salvage_partial_json_keeps_complete_nodes():
    truncated = (
        '{"topology":"linear","start":"car4","nodes":['
        '{"id":"car4","kind":"location","title":"四号车厢"},'
        '{"id":"car3","kind":"location","title":"三号车厢"},'
        '{"id":"cut","kind":"location","tit'
    )
    saved = salvage_partial_json(truncated)
    assert saved is not None
    assert [node["id"] for node in saved["nodes"]] == ["car4", "car3"]
    assert saved["topology"] == "linear"
    assert saved["start"] == "car4"


def test_request_json_retries_when_output_is_incomplete():
    replies = iter(
        [
            '{"nodes":[{"id":"car4","kind":"location","tit',  # 截断到没有完整对象，无法 salvage
            '{"nodes":[{"id":"car4","kind":"location","title":"四号车厢"}],"edges":[]}',
        ]
    )
    report = AnalysisReport()
    data = _request_json(lambda _prompt: next(replies), "prompt", report=report, stage="graph", attempts=2)
    assert data is not None
    assert data["nodes"][0]["id"] == "car4"
    assert report.ai_calls == 2
    assert any("重试" in note.message for note in report.notes)


def test_analyze_script_reports_progress_for_every_ai_stage():
    """前端靠这些回调显示「正在通读/正在补全」；阶段顺序与文案都必须齐全。"""
    events: list[tuple[str, int, int, str]] = []

    analyze_script(
        SCRIPT,
        title="旧宅",
        request=_fake_requester,
        scenario_id="77",
        progress=lambda stage, done, total, message: events.append((stage, done, total, message)),
    )

    stages = {event[0] for event in events}
    assert {"scan", "clean", "segment", "worldbook", "graph", "graph_detail"} <= stages
    assert all(message for _, _, _, message in events)
    # 进度必须是单调完成的（done/total 从 0 到 total），否则前端进度条会倒退。
    assert any(event[1] == event[2] and event[2] > 0 for event in events)


def test_request_json_aborts_when_budget_is_exhausted():
    report = AnalysisReport(deadline=time.monotonic() - 1)
    with pytest.raises(AIBudgetExceeded):
        _request_json(lambda _prompt: "{}", "prompt", report=report, stage="scan")
    assert report.ai_calls == 0


def test_scan_bounds_single_pass_for_short_script_and_windows_for_long_one():
    short = SCRIPT.split("\n")
    assert _scan_bounds(short) == [(1, len(short))]
    long_lines = ["台词" * 200] * 200  # 远超单次提示词上限，必须切窗
    bounds = _scan_bounds(long_lines)
    assert len(bounds) > 1
    assert bounds[0][0] == 1 and bounds[-1][1] == len(long_lines)


def test_response_content_falls_back_to_reasoning_json_when_content_is_empty():
    """推理模型把输出预算花在思考上时，content 为空、reasoning_content 里有 JSON。"""
    payload = {
        "choices": [
            {
                "message": {"content": "", "reasoning_content": '分析完成，结果：{"nodes":[],"edges":[]}'},
                "finish_reason": "length",
            }
        ],
        "usage": {"completion_tokens": 8192},
    }
    content, finish_reason, usage = response_content(payload)
    assert '"nodes"' in content
    assert finish_reason == "length"
    assert usage["completion_tokens"] == 8192


def test_request_json_retries_empty_response_with_direct_output_hint():
    prompts: list[str] = []

    def request(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            raise EmptyAIResponse("AI 平台未返回正文")
        return '{"nodes":[{"id":"key","kind":"item","title":"钥匙"}],"edges":[]}'

    report = AnalysisReport()
    data = _request_json(request, "补全资产节点", report=report, stage="graph_detail", attempts=2)
    assert data is not None
    assert data["nodes"][0]["id"] == "key"
    assert "不要输出思考过程" in prompts[1]  # 第二轮改用「直接输出」提示，而不是重复原请求


def test_detail_batches_cap_modules_so_graph_detail_output_stays_small():
    modules = [{"id": f"m{index}", "content": "正文" * 100} for index in range(12)]
    batches = _detail_batches(modules)
    assert all(len(batch) <= MAX_GRAPH_DETAIL_MODULES for batch in batches)
    assert len(batches) >= 3
