from __future__ import annotations

from pathlib import Path

from trpg_server.agents.knowledge_base import KnowledgeBaseService, KnowledgeChunk
from trpg_server.agents.narrative.guards import GuardPolicy, apply_output_guard
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.prejudge import (
    INTENT_INVESTIGATE,
    INTENT_META,
    INTENT_MOVE,
    classify_intent,
    clear_cache,
    prepare_turn,
    render_contract,
    retrieval_candidates,
)
from trpg_server.agents.narrative.runtime import NarrativeRuntime
from trpg_server.agents.narrative.runtime_settings import resolve_narrative_settings
from trpg_server.agents.narrative.state_store import load_narrative_state, save_narrative_state
from trpg_server.agents.narrative.store import build_index


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        scenario_id="s1",
        topology="linear",
        nodes=[
            NarrativeNode(id="area_ground", kind="area", title="一层"),
            NarrativeNode(
                id="room_hall",
                kind="location",
                title="门厅",
                parent="area_ground",
                summary="门厅里有一盏摇晃的提灯。",
                keywords=["门厅"],
                objects=[
                    {
                        "id": "obj_lamp",
                        "name": "提灯",
                        "summary": "一盏黄铜提灯。",
                        "visible": True,
                        "visibility": "public",
                        "interactions": [
                            {
                                "id": "int_lamp_investigate",
                                "skill": "侦查",
                                "difficulty": "regular",
                                "repeatable": False,
                                "outcomes": {"success": {"text": "灯罩内侧有新鲜血迹。"}},
                            }
                        ],
                    }
                ],
            ),
            NarrativeNode(id="room_cellar", kind="location", title="地下室", parent="area_ground"),
            NarrativeNode(id="clue_blood", kind="clue", title="灯罩血迹", summary="血迹还没干。"),
            NarrativeNode(id="clue_truth", kind="clue", title="黑幕真相", visibility="kp_only"),
            NarrativeNode(id="npc_butler", kind="npc", title="管家", aliases=["老管家"]),
        ],
        edges=[
            NarrativeEdge(
                id="edge_hall_cellar",
                from_id="room_hall",
                to_id="room_cellar",
                type="adjacent",
                hard=True,
                condition={"clock": "clock_flood", "gte": 10},
            ),
            NarrativeEdge(id="edge_hall_clue", from_id="room_hall", to_id="clue_blood", type="contains"),
            NarrativeEdge(id="edge_hall_npc", from_id="room_hall", to_id="npc_butler", type="knows"),
        ],
    )


def _runtime(tmp_path: Path, *, clock: int = 0) -> NarrativeRuntime:
    bundle = _bundle()
    save_narrative_state(
        tmp_path,
        {
            "location": "room_hall",
            "clock": {"turn": 2},
            "node_status": {"room_hall": "active"},
            "clocks": {"clock_flood": {"value": clock, "threshold": 25}},
        },
    )
    return NarrativeRuntime(
        bundle=bundle,
        narrative=load_narrative_state(tmp_path),
        index=build_index(bundle),
        room_id="room-1",
        room_dir=tmp_path,
    )


def test_classify_intent_routes_branches() -> None:
    assert classify_intent("我走进地下室")["intent"] == INTENT_MOVE
    assert classify_intent("我仔细查看提灯")["intent"] == INTENT_INVESTIGATE
    assert classify_intent("我们先看看技能列表")["intent"] == INTENT_META
    assert classify_intent("你好")["intent"] == "dialogue"


def test_prepare_turn_builds_contract_with_exits_and_denials(tmp_path) -> None:
    clear_cache()
    runtime = _runtime(tmp_path)
    contract = prepare_turn(runtime, "我走进地下室")

    assert contract is not None
    assert contract.intent == INTENT_MOVE
    assert contract.location == "room_hall"
    assert [item["id"] for item in contract.targets] == ["room_cellar"]
    assert any(item["to"] == "room_cellar" and item["blocked"] for item in contract.reachable_exits)
    assert contract.denied and contract.denied[0]["id"] == "room_cellar"
    assert "黑幕真相" in contract.forbidden
    rendered = render_contract(contract)
    assert "【本轮路由】" in rendered and "【禁止】" in rendered


def test_prepare_turn_allows_visible_object_inspection(tmp_path) -> None:
    clear_cache()
    runtime = _runtime(tmp_path)
    contract = prepare_turn(runtime, "我查看提灯")

    assert contract is not None
    assert contract.intent == INTENT_INVESTIGATE
    assert not contract.denied
    allowed = {(item["action"], item.get("id")) for item in contract.allowed}
    assert ("inspect_object", "obj_lamp") in allowed


def test_prepare_turn_marks_meta_as_retrieval_skip(tmp_path) -> None:
    clear_cache()
    runtime = _runtime(tmp_path)
    contract = prepare_turn(runtime, "我们看看房间规则")

    assert contract is not None
    assert contract.intent == INTENT_META
    assert contract.skip_retrieval is True


def test_prepare_turn_is_cached_for_same_state(tmp_path) -> None:
    clear_cache()
    runtime = _runtime(tmp_path)
    first = prepare_turn(runtime, "我查看提灯")
    second = prepare_turn(runtime, "我查看提灯")
    assert first is second


def test_retrieval_candidates_stay_within_graph(tmp_path) -> None:
    clear_cache()
    runtime = _runtime(tmp_path)
    contract = prepare_turn(runtime, "我查看提灯")
    ids, titles = retrieval_candidates(runtime, contract, "我查看提灯")

    assert {"room_hall", "clue_blood"}.issubset(ids)
    assert "灯罩血迹" in titles
    assert "门厅" in titles


def test_knowledge_filter_keeps_constants_and_matching_chunks() -> None:
    def chunk(chunk_id: str, title: str, *, constant: bool = False) -> KnowledgeChunk:
        return KnowledgeChunk(
            scenario_id="s1",
            scenario_version="1",
            scene_id=None,
            card_type="scene",
            visibility="public",
            spoiler_level=0,
            unlock_condition=None,
            text=title,
            chunk_id=chunk_id,
            metadata={"title": title},
            is_constant=constant,
        )

    chunks = [chunk("c1", "门厅"), chunk("c2", "地下室"), chunk("c3", "背景", constant=True)]
    kept = KnowledgeBaseService._filter_by_candidates(chunks, {"c1"}, {"提灯"})
    assert [item.chunk_id for item in kept] == ["c1", "c3"]

    kept_by_title = KnowledgeBaseService._filter_by_candidates(chunks, None, {"地下室"})
    assert [item.chunk_id for item in kept_by_title] == ["c2", "c3"]

    assert KnowledgeBaseService._filter_by_candidates(chunks, None, None) == chunks


def test_resolve_narrative_settings_reads_room_overrides() -> None:
    default = resolve_narrative_settings({})
    assert default.graph_enabled and default.guard == "enforce" and default.source == "default"

    disabled = resolve_narrative_settings({"house_rules": {"narrative_enabled": False}})
    assert disabled.enabled is False
    assert disabled.graph_enabled is False
    assert disabled.source == "room"

    shadow = resolve_narrative_settings(
        {"house_rules": {"narrative_profile": "degraded", "narrative_guard": "shadow"}}
    )
    assert shadow.profile == "degraded" and shadow.guard == "shadow"
    assert shadow.guard_policy().shadow is True

    invalid = resolve_narrative_settings({"house_rules": {"narrative_guard": "loud"}})
    assert invalid.guard == "enforce"


def test_guard_policy_shadow_logs_but_keeps_text() -> None:
    bundle = _bundle()
    text = "黑幕真相。门厅很安静。"
    enforced = apply_output_guard(text, bundle, {}, GuardPolicy.for_mode("enforce"))
    assert "黑幕真相" not in enforced
    shadowed = apply_output_guard(text, bundle, {}, GuardPolicy.for_mode("shadow"))
    assert shadowed == text
    assert apply_output_guard(text, bundle, {}, GuardPolicy.for_mode("off")) == text
