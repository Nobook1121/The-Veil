from pathlib import Path

from trpg_server.agents.narrative.generate_ai import fill_nodes
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        nodes=[
            NarrativeNode(id="loc_lobby", kind="location", title="大厅", summary="", body="（待补充）"),
            NarrativeNode(id="loc_room", kind="location", title="客房", summary="已写摘要", body="已有正文"),
            NarrativeNode(id="act_1", kind="act", title="第一幕", summary="", body=""),
        ]
    )


def test_fill_nodes_only_fills_blank_location_nodes(tmp_path: Path):
    prompts: list[str] = []

    def request(prompt: str) -> str:
        prompts.append(prompt)
        return (
            '{"nodes": ['
            '{"id": "loc_lobby", "summary": "入口大厅", "body": "门厅里堆着旧箱子。"},'
            '{"id": "loc_room", "summary": "不应覆盖", "body": "不应覆盖"},'
            '{"id": "made_up", "summary": "幻觉", "body": "幻觉"}'
            ']}'
        )

    bundle = _bundle()
    result = fill_nodes(bundle, "剧本正文", request=request, storage=tmp_path, scenario_title="测试剧本")

    assert result["filled"] == 1
    assert result["calls"] == 1
    assert len(prompts) == 1
    lobby = bundle.node("loc_lobby")
    room = bundle.node("loc_room")
    act = bundle.node("act_1")
    assert lobby is not None and lobby.summary == "入口大厅"
    assert lobby.extra.get("ai_filled") is True
    assert room is not None and room.summary == "已写摘要" and room.body == "已有正文"
    assert act is not None and act.summary == "" and act.body == ""
    assert (tmp_path / "llm_cache.json").is_file()


def test_fill_nodes_reuses_cache(tmp_path: Path):
    calls = 0

    def request(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"nodes": [{"id": "loc_lobby", "summary": "入口大厅", "body": "门厅里堆着旧箱子。"}]}'

    fill_nodes(_bundle(), "剧本正文", request=request, storage=tmp_path)
    second = fill_nodes(_bundle(), "剧本正文", request=request, storage=tmp_path)

    assert calls == 1
    assert second["filled"] == 1


def test_fill_nodes_keeps_deterministic_result_on_failure(tmp_path: Path):
    def request(prompt: str) -> str:
        raise RuntimeError("平台超时")

    bundle = _bundle()
    result = fill_nodes(bundle, "剧本正文", request=request, storage=tmp_path)

    assert result["filled"] == 0
    assert any(item["code"] == "generate.ai_fill_failed" for item in result["warnings"])
    lobby = bundle.node("loc_lobby")
    assert lobby is not None and lobby.body == "（待补充）"


def test_fill_nodes_reports_invalid_json(tmp_path: Path):
    def request(prompt: str) -> str:
        return "这不是 JSON"

    result = fill_nodes(_bundle(), "剧本正文", request=request, storage=tmp_path)

    assert result["filled"] == 0
    assert any(item["code"] == "generate.ai_fill_invalid" for item in result["warnings"])
