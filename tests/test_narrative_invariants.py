"""narrative 运行时不变量：move / trigger / ending 的组合语义（加固计划 C 阶段）。

这些用例只依赖内存构造的小图与临时房间目录，不调用 LLM、不访问网络。
断言都针对具体返回值与落盘状态，失败信息带 `node_id` 便于定位。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.narrative.ledger import read_ledger_events
from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError
from trpg_server.agents.narrative.state_store import load_narrative_state, save_narrative_state
from trpg_server.agents.narrative.store import build_index

START = "room_a"
CORRIDOR = "room_b"
SECRET_ROOM = "room_c"
ISOLATED = "room_isolated"
ENDING = "ending_escape"


def _bundle() -> NarrativeBundle:
    """6 节点小图：门厅 / 走廊 / 密室 / 孤岛 / 结局 / 线索。"""
    return NarrativeBundle(
        scenario_id="invariants",
        nodes=[
            NarrativeNode(id="area_ground", kind="area", title="一层"),
            NarrativeNode(
                id=START, kind="location", title="门厅", parent="area_ground",
                summary="门厅里有一盏摇晃的提灯。", body="门厅正文。",
                objects=[
                    {
                        "id": "obj_lamp", "name": "提灯", "summary": "一盏黄铜提灯。",
                        "visible": True, "visibility": "public",
                    },
                    {
                        "id": "obj_secret", "name": "暗格", "summary": "墙板后似乎有空间。",
                        "visible": True, "visibility": "hidden",
                        "reveal_when": {"flag": "secret_known", "value": True},
                    },
                ],
            ),
            NarrativeNode(id=CORRIDOR, kind="location", title="走廊", parent="area_ground"),
            NarrativeNode(
                id=SECRET_ROOM, kind="location", title="密室", parent="area_ground",
                visibility="hidden", reveal_when={"flag": "secret_known", "value": True},
            ),
            NarrativeNode(id=ISOLATED, kind="location", title="孤岛", parent="area_ground"),
            NarrativeNode(
                id=ENDING, kind="ending", title="逃出宅邸", visibility="kp_only",
                completion={"flag": "door_open", "value": True},
            ),
            NarrativeNode(id="clue_key", kind="clue", title="黄铜钥匙"),
        ],
        edges=[
            NarrativeEdge(
                id="edge_a_b", from_id=START, to_id=CORRIDOR, type="adjacent", hard=True,
                bidirectional=True, condition={"flag": "door_unlocked", "value": True},
            ),
            NarrativeEdge(
                id="edge_b_c", from_id=CORRIDOR, to_id=SECRET_ROOM, type="next", hard=True,
                bidirectional=False,
            ),
            NarrativeEdge(
                id="edge_a_ending", from_id=START, to_id=ENDING, type="leads_to", hard=True,
                bidirectional=False,
            ),
            # 故意给结局补一条回头路：终点语义应由运行时保证，而不是靠图恰好没有出边。
            NarrativeEdge(
                id="edge_ending_back", from_id=ENDING, to_id=START, type="adjacent", hard=True,
                bidirectional=True,
            ),
        ],
        triggers=[
            {
                "id": "trg_enter_b_first", "label": "初入走廊",
                "binding": {"kind": "enter", "location_id": CORRIDOR, "first_time": True},
                "content": "你第一次走进走廊。",
            },
            {
                "id": "trg_enter_b_gate", "label": "条件未满足的进入触发",
                "binding": {"kind": "enter", "location_id": CORRIDOR},
                "condition": {"flag": "impossible_door", "value": True},
                "content": "不应出现的进入文本。",
            },
            {
                "id": "trg_manual_knock", "label": "敲门",
                "binding": {"kind": "manual", "keyword": "敲门"},
                "content": "门后传来回应。",
            },
            {
                "id": "trg_manual_gate", "label": "条件未满足的手动触发",
                "binding": {"kind": "manual", "keyword": "敲窗"},
                "condition": {"flag": "impossible_window", "value": True},
                "content": "不应出现的窗后文本。",
            },
        ],
    )


def _runtime(tmp_path: Path) -> NarrativeRuntime:
    bundle = _bundle()
    save_narrative_state(
        tmp_path,
        {"location": START, "clock": {"turn": 1}, "node_status": {START: "active"}},
    )
    return NarrativeRuntime(
        bundle=bundle,
        narrative=load_narrative_state(tmp_path),
        index=build_index(bundle),
        room_id="room-invariants",
        room_dir=tmp_path,
    )


def _event_kinds(room_dir: Path) -> list[str]:
    """账本 meta 会把未知 kind 归一成 action，真实 kind 在 payload 里。"""
    kinds: list[str] = []
    for event in read_ledger_events(room_dir):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        kinds.append(str(payload.get("kind") or event.get("kind") or ""))
    return kinds


def _unlock_corridor(runtime: NarrativeRuntime) -> None:
    runtime.record({"kind": "flag", "name": "door_unlocked", "value": True}, summary="解锁走廊")


# ---------------------------------------------------------------------------
# 1. 不可达节点 move 必须拒绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target_ref", "expected"),
    [
        (ISOLATED, "无法直接到达"),          # 图里存在、但与当前位置无移动边
        ("孤岛", "无法直接到达"),            # 按标题同样走邻接校验
        ("不存在的房间", "找不到地点"),      # 完全未知的目标
    ],
)
def test_unreachable_move_is_rejected(tmp_path, target_ref, expected) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(NarrativeRuntimeError) as excinfo:
        runtime.move(target_ref)

    assert expected in str(excinfo.value), f"node_id={target_ref} 的拒绝原因不符合预期"
    assert load_narrative_state(tmp_path)["location"] == START, f"node_id={target_ref} 被拒绝后位置不应变化"
    move_events = [
        event for event in read_ledger_events(tmp_path)
        if isinstance(event.get("payload"), dict) and event["payload"].get("kind") == "move"
    ]
    assert move_events == [], f"node_id={target_ref} 被拒绝后不应写入 move 账本"


# ---------------------------------------------------------------------------
# 2. 条件不满足的 trigger 不触发且无副作用
# ---------------------------------------------------------------------------
def test_auto_trigger_with_unmet_condition_has_no_side_effect(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    _unlock_corridor(runtime)

    result = runtime.move(CORRIDOR)

    assert result["moved"] is True
    assert result["triggers_fired"] == ["trg_enter_b_first"], f"node_id={CORRIDOR} 只应触发首次进入触发器"
    denied = {item["trigger_id"]: item for item in result["trigger_denied"]}
    assert denied["trg_enter_b_gate"]["reason"] == "condition_not_met", f"node_id={CORRIDOR} 的 gated 触发器应被拒"
    assert {"flag": "impossible_door", "value": True} in denied["trg_enter_b_gate"]["missing"]
    assert all("不应出现" not in message.get("content", "") for message in result["direct_messages"]), \
        f"node_id={CORRIDOR} 条件未满足时不应产生消息"

    state = load_narrative_state(tmp_path)
    assert set(state["trigger_fires"]) == {"trg_enter_b_first"}, "条件未满足的触发器不得写入 trigger_fires"
    assert "trg_enter_b_gate" not in state["trigger_fires"]
    fired = [
        event["payload"]["trigger_id"]
        for event in read_ledger_events(tmp_path)
        if isinstance(event.get("payload"), dict) and event["payload"].get("kind") == "trigger_fire"
    ]
    assert fired == ["trg_enter_b_first"], "条件未满足的触发器不得写入 trigger_fire 账本"


def test_manual_trigger_with_unmet_condition_writes_nothing(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    before = load_narrative_state(tmp_path)

    result = runtime.request_trigger("trg_manual_gate", action_text="敲窗")

    assert result["triggered"] is False
    assert result["denied"][0]["trigger_id"] == "trg_manual_gate"
    assert result["denied"][0]["reason"] == "condition_not_met"
    assert "direct_messages" not in result, "被拒的手动触发不应产生消息"
    assert read_ledger_events(tmp_path) == [], "被拒的手动触发不应写账本"
    assert load_narrative_state(tmp_path) == before, "被拒的手动触发不应改动运行态"


# ---------------------------------------------------------------------------
# 3. 进入 ending 后所有 move / trigger 被拒
# ---------------------------------------------------------------------------
def test_ending_rejects_move_even_with_return_edge(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.record({"kind": "flag", "name": "door_open", "value": True}, summary="打开大门")

    entered = runtime.move("逃出宅邸")
    assert entered["moved"] is True, f"node_id={ENDING} 满足 completion 后应能进入"
    assert runtime.location_node().kind == "ending"

    with pytest.raises(NarrativeRuntimeError) as excinfo:
        runtime.move("门厅")
    assert "无法直接到达" in str(excinfo.value) or "结局" in str(excinfo.value), \
        f"node_id={ENDING} 结局后移动应被运行时拒绝"
    assert runtime.exits() == [], f"node_id={ENDING} 进入结局后不应再暴露任何出口"


def test_ending_rejects_manual_trigger(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.record({"kind": "flag", "name": "door_open", "value": True}, summary="打开大门")
    runtime.move("逃出宅邸")

    result = runtime.request_trigger("trg_manual_knock", action_text="敲门")

    assert result["triggered"] is False, f"node_id={ENDING} 结局后不应再接受触发器请求"
    assert "direct_messages" not in result, f"node_id={ENDING} 结局后不应再产生触发消息"


# ---------------------------------------------------------------------------
# 4. 同参数重复 move 幂等
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("second_ref", [CORRIDOR, "走廊"])
def test_repeated_move_is_idempotent(tmp_path, second_ref) -> None:
    runtime = _runtime(tmp_path)
    _unlock_corridor(runtime)

    first = runtime.move(CORRIDOR)
    assert first["moved"] is True, f"node_id={CORRIDOR} 解锁后应能移动"
    second = runtime.move(second_ref)

    assert second == {
        "moved": False,
        "already_here": True,
        "location_id": CORRIDOR,
        "title": "走廊",
    }, f"node_id={CORRIDOR} 重复移动应直接返回 already_here"

    state = load_narrative_state(tmp_path)
    assert state["visit_log"].count(CORRIDOR) == 1, f"node_id={CORRIDOR} 不应因重复移动重复进入访问日志"
    assert state["trigger_fires"]["trg_enter_b_first"]["count"] == 1, "重复移动不应重复触发 enter"
    move_events = _event_kinds(tmp_path).count("move")
    assert move_events == 1, f"node_id={CORRIDOR} 重复移动不应重复写 move 账本"


# ---------------------------------------------------------------------------
# 5. record 后 autosave round-trip 状态一致
# ---------------------------------------------------------------------------
def test_record_persists_and_round_trips(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    event = runtime.record(
        {"kind": "clock", "clock_id": "clock_doom", "delta": 2, "threshold": 5},
        summary="厄运推进",
    )

    persisted = load_narrative_state(tmp_path)
    assert persisted == runtime.narrative, "record 后磁盘状态应与内存运行态完全一致"
    assert persisted["clocks"]["clock_doom"] == {"value": 2, "threshold": 5}
    assert event["id"] in persisted["applied_event_ids"]

    # 同一事件 id 重放不得改变状态（账本幂等）。
    runtime.record(
        {"id": event["id"], "kind": "clock", "clock_id": "clock_doom", "delta": 2, "threshold": 5}
    )
    assert load_narrative_state(tmp_path) == persisted, "同一事件 id 重放不应改变运行态"


# ---------------------------------------------------------------------------
# 6. 可见性：未解锁节点不出现在 location_card() / exits()
# ---------------------------------------------------------------------------
def test_hidden_location_absent_from_exits_until_unlocked(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    _unlock_corridor(runtime)
    runtime.move(CORRIDOR)

    card = runtime.location_card()
    assert SECRET_ROOM not in {item["target_id"] for item in card["exits"]}, \
        f"node_id={SECRET_ROOM} 未解锁时不应出现在 location_card().exits"
    hidden = {item["target_id"]: item for item in runtime.exits(include_hidden=True)}
    assert hidden[SECRET_ROOM]["visible"] is False, f"node_id={SECRET_ROOM} 的 include_hidden 投影应标记未可见"

    runtime.record({"kind": "flag", "name": "secret_known", "value": True}, summary="得知密室")
    unlocked = {item["target_id"]: item for item in runtime.location_card()["exits"]}
    assert unlocked[SECRET_ROOM]["visible"] is True, f"node_id={SECRET_ROOM} 解锁后应出现在出口列表"


def test_hidden_object_absent_from_location_card_until_unlocked(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    assert [item["id"] for item in runtime.location_card()["objects"]] == ["obj_lamp"], \
        f"node_id={START} 未解锁时不应暴露隐藏物件"
    with pytest.raises(NarrativeRuntimeError):
        runtime.inspect_object("暗格")

    runtime.record({"kind": "flag", "name": "secret_known", "value": True}, summary="得知暗格")
    assert [item["id"] for item in runtime.location_card()["objects"]] == ["obj_lamp", "obj_secret"], \
        f"node_id={START} 解锁后应暴露隐藏物件"
    assert runtime.inspect_object("暗格")["revealed_now"] is True
