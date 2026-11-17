"""工作流服务端鉴权：决定、取消、超时与可见范围的回归用例。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

from trpg_server.workflows import access as flow_access
from trpg_server.workflows import store as flow_store
from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import POLICY_ASK, make_step, now


def _write_room(tmp_path: Path) -> tuple[Path, dict]:
    room_dir = tmp_path / "room1"
    room_dir.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "鉴权测试",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "is_active": True,
                "status": "active",
                "character_card": {"name": "Alice"},
            },
            {
                "user_id": "u2",
                "username": "bob",
                "room_role": "member",
                "is_active": True,
                "status": "active",
                "character_card": {"name": "Bob"},
            },
            {
                "user_id": "u3",
                "username": "ai-mira",
                "room_role": "member",
                "is_active": True,
                "status": "active",
                "is_ai_player": True,
                "control_mode": "ai",
                "character_card": {"name": "Mira"},
            },
            {
                "user_id": "u4",
                "username": "admin",
                "room_role": "admin",
                "is_active": True,
                "status": "active",
                "character_card": {"name": "Dana"},
            },
        ],
    }
    (room_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    return room_dir, info


def _waiting_flow(engine: WorkflowEngine, *, initiator: str = "Alice") -> str:
    flow, error, created = engine.open(
        kind="cast",
        initiator=initiator,
        initiator_type="player",
        steps=[
            make_step(
                f"save:{initiator}",
                f"{initiator} 豁免",
                policy=POLICY_ASK,
                executor="player",
            )
        ],
        ttl=60,
    )
    assert error is None and created
    engine.run_auto(flow["id"], {})
    return flow["id"]


def _age_flow(room_dir: Path, flow_id: str, seconds: float = 5) -> None:
    with flow_store.room_write_lock(room_dir):
        data = flow_store.load(room_dir)
        data["flows"][flow_id]["expires_at"] = now() - seconds
        flow_store.save(room_dir, data)


def test_member_cannot_decide_for_another_character(tmp_path):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)

    denied = engine.submit(
        flow_id,
        "save:Alice",
        decision={"decision": "fail"},
        actor_user_id="u2",
    )
    assert denied["code"] == "workflow.forbidden"
    assert "Alice" in denied["error"]
    flow = engine.get(flow_id, expire=False)
    step = next(item for item in flow["steps"] if item["id"] == "save:Alice")
    assert step["status"] == "waiting"

    allowed = engine.submit(
        flow_id,
        "save:Alice",
        decision={"decision": "success"},
        idempotency_key="alice-1",
        actor_user_id="u1",
    )
    assert allowed.get("duplicate") is False
    assert allowed["flow"]["status"] == "completed"


def test_kp_and_manager_can_decide_for_player_character(tmp_path):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)

    kp = engine.submit(
        flow_id,
        "save:Alice",
        decision={"decision": "fail"},
        idempotency_key="kp-1",
        actor_user_id="u4",
        is_manager=True,
    )
    assert kp.get("duplicate") is False
    assert kp["flow"]["status"] == "completed"


def test_ai_controlled_character_only_accepts_ai_executor(tmp_path):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow, error, _created = engine.open(
        kind="attack",
        initiator="Alice",
        initiator_type="player",
        steps=[
            make_step("reaction:Mira", "Mira 是否反应", policy=POLICY_ASK, executor="ai")
        ],
        ttl=60,
    )
    assert error is None
    engine.run_auto(flow["id"], {})

    human = engine.submit(
        flow["id"],
        "reaction:Mira",
        decision={"decision": "use"},
        actor_user_id="u2",
    )
    assert human["code"] == "workflow.forbidden"
    assert "AI" in human["error"]

    ai = engine.submit(
        flow["id"],
        "reaction:Mira",
        decision={"decision": "use"},
        actor_user_id="u3",
        agent_id="player",
    )
    assert ai.get("duplicate") is False
    assert ai["flow"]["status"] == "completed"


def test_cancel_and_force_expire_limited_to_initiator_or_manager(tmp_path):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)

    denied = engine.cancel(flow_id, actor_user_id="u2")
    assert denied["code"] == "workflow.forbidden"
    assert engine.get(flow_id, expire=False)["status"] == "waiting"

    expired_denied = engine.expire(flow_id, actor_user_id="u2")
    assert expired_denied["code"] == "workflow.forbidden"

    by_manager = engine.expire(flow_id, actor_user_id="u4", is_manager=True)
    assert by_manager["expired"] is True

    second_id = _waiting_flow(engine)
    by_initiator = engine.cancel(second_id, actor_user_id="u1")
    assert by_initiator["cancelled"] is True


def test_flow_visibility_filters_unrelated_members(tmp_path):
    room_dir, info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)
    flow = engine.get(flow_id, expire=False)

    assert flow_access.flow_visible_to(flow, user_id="u1", info=info)
    assert not flow_access.flow_visible_to(flow, user_id="u2", info=info)
    assert flow_access.flow_visible_to(flow, user_id="u4", manager=True, info=info)
    assert flow_access.flow_visible_to(flow, user_id="u9", agent_id="kp", info=info)


def test_expired_flow_is_settled_before_new_lock_acquisition(tmp_path):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)
    _age_flow(room_dir, flow_id)

    # 旧流程的 TTL 已过：open 必须先收尾旧流程、释放角色锁，再允许新流程抢入。
    new_flow, error, created = engine.open(
        kind="cast",
        initiator="Alice",
        initiator_type="player",
        steps=[make_step("resolve", "结算")],
        ttl=60,
        request_key="after-expire",
    )
    assert error is None and created
    old = engine.get(flow_id, expire=False)
    assert old["status"] in {"expired", "completed"}
    assert old["steps"][0]["result"]["timeout"] is True

    # 旧流程不能再被提交，避免收尾后又被补一次决定造成重复结算。
    denied = engine.submit(
        flow_id,
        "save:Alice",
        decision={"decision": "success"},
        actor_user_id="u1",
    )
    assert denied["code"] in {"workflow.expired", "workflow.closed"}


def test_route_ignores_body_actor_and_rejects_other_members(tmp_path, monkeypatch):
    room_dir, _info = _write_room(tmp_path)
    engine = WorkflowEngine(room_dir, room_id="room1")
    flow_id = _waiting_flow(engine)

    # 该用例只验证鉴权接线，不加载规则集插件：直接固定规则集解析结果。
    from types import SimpleNamespace

    from trpg_server.routes import workflows as workflow_routes

    monkeypatch.setattr(
        workflow_routes, "resolve_settings", lambda _info: SimpleNamespace(ruleset_id="dnd5e")
    )

    from trpg_server.routes.workflows import bp

    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(tmp_path), CONFIG_DIR=str(tmp_path / "config"))
    app.register_blueprint(bp)

    def _session(client, user_id, username):
        with client.session_transaction() as session:
            session.update(user_id=user_id, username=username, role="USER")

    bob = app.test_client()
    _session(bob, "u2", "bob")
    response = bob.post(
        f"/api/rooms/room1/workflows/{flow_id}/decide",
        json={
            "step_id": "save:Alice",
            "decision": {"decision": "fail"},
            # 伪造 actor 不应改变服务端判定。
            "actor": "alice",
        },
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"] == flow_access.FORBIDDEN
    assert engine.get(flow_id, expire=False)["status"] == "waiting"

    alice = app.test_client()
    _session(alice, "u1", "alice")
    allowed = alice.post(
        f"/api/rooms/room1/workflows/{flow_id}/decide",
        json={"step_id": "save:Alice", "decision": {"decision": "success"}},
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["flow"]["status"] == "completed"
