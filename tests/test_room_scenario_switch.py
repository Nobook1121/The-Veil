import json

from trpg_server.app_factory import create_app


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(role),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_scenario(tmp_path, scenario_id, title, scene_id, scenario_version="1.0.0", ruleset_id=None):
    """写入一个最小的剧本描述文件；场景 id 会被归一化为整数（与真实剧本一致）。"""
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": scenario_id,
        "title": title,
        "scenario_version": scenario_version,
        "modules": [{"id": scene_id, "module_type": "scene", "title": f"{title} 开场"}],
    }
    if ruleset_id:
        payload["ruleset_id"] = ruleset_id
    (scenarios_dir / f"{scenario_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "tester", "role": "ADMIN", "status": "active", "is_active": True, "room_role": "owner"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_room_info(tmp_path, room_id="r-1"):
    return json.loads((tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8"))


def test_switch_room_scenario_rebinds_and_resets_progress(tmp_path):
    """强制换绑到另一个剧本：绑定/场景指针更新，剧情进度与开场标记重置。"""
    room_dir, _ = _write_room(
        tmp_path,
        scenario_id=1,
        scenario_title="旧剧本",
        scenario_version="1.0.0",
        active_scene_id="old-scene",
        active_scene_title="旧场景",
        scenario_started_at="2026-01-01 00:00:00",
        scenario_started_by=1,
    )
    # 预置旧剧本的剧情进度与检索游标，验证换绑后被清空。
    (room_dir / "state.json").write_text(
        json.dumps({"active_scene_id": "old-scene", "clues": ["c1"], "items": ["i1"], "quests": ["q1"], "triggered_event_ids": ["e1"]}),
        encoding="utf-8",
    )
    (room_dir / "knowledge_cursor.json").write_text(json.dumps({"scenario": "1@1.0.0", "rounds": ["chunk-0001"], "timed": {"chunk-0001": {}}}), encoding="utf-8")
    _write_scenario(tmp_path, 2, "新剧本", 202)

    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 2, "scenario_title": "新剧本"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["scenario_id"] == 2
    assert data["scenario_title"] == "新剧本"
    stored = _read_room_info(tmp_path)
    assert stored["scenario_id"] == 2
    assert stored["scenario_title"] == "新剧本"
    assert stored["scenario_version"] == "1.0.0"
    # 指向新剧本的第一个场景，且旧剧本的「已开始」标记被清除，需重新开场。
    assert stored["active_scene_id"] == "202"
    assert stored.get("scenario_started_at") is None
    assert stored.get("scenario_started_by") is None

    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert state["active_scene_id"] == "202"
    assert state["clues"] == [] and state["items"] == [] and state["quests"] == []
    assert state["triggered_event_ids"] == []
    # 检索游标按剧本绑定，换绑后应被删除。
    assert not (room_dir / "knowledge_cursor.json").exists()


def test_switch_room_scenario_archives_combat_and_cancels_pending_state(tmp_path):
    """切换规则集不再 409：归档战斗、取消等待流程、结束区域，然后完成换绑。"""
    from trpg_server.workflows import areas as area_store
    from trpg_server.workflows.engine import WorkflowEngine
    from trpg_server.workflows.model import FLOW_CANCELLED, make_step

    room_dir, _ = _write_room(
        tmp_path,
        ruleset_id="dnd5e",
        scenario_id=1,
        scenario_title="旧剧本",
        scenario_version="1.0.0",
    )
    (room_dir / "state.json").write_text(
        json.dumps({"combat": {"active": True, "round": 2, "combatants": {"Aria": {"hp": 9}}}}),
        encoding="utf-8",
    )
    flow, _error, _created = WorkflowEngine(room_dir).open(
        kind="cast",
        initiator="Aria",
        initiator_type="player",
        steps=[make_step("resolve", "法术结算", policy="manual", executor="player")],
        ttl=600,
    )
    area_store.create_area(
        room_dir,
        {
            "name": "Moonbeam",
            "shape": "cylinder",
            "size_ft": 10,
            "origin": {"cell_x": 2, "cell_y": 2},
            "duration": {"unit": "round", "rounds": 10},
        },
    )
    _write_scenario(tmp_path, 2, "新剧本", 202, ruleset_id="coc7")

    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 2, "scenario_title": "新剧本"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "coc7"
    assert data["ruleset_switch"] == {
        "combat_archived": True,
        "cancelled_workflows": 1,
        "ended_areas": 1,
    }
    archive = json.loads((room_dir / "combat_archive.json").read_text(encoding="utf-8"))
    assert archive["archived"][0]["reason"] == "ruleset_switch"
    assert WorkflowEngine(room_dir).get(flow["id"], expire=False)["status"] == FLOW_CANCELLED
    assert area_store.list_areas(room_dir, scene_id="", active_only=True) == []


def test_switch_room_scenario_preserve_progress_keeps_state(tmp_path):
    """继承进度：切换到同一剧本的最新版本时，保留剧情进度与开场标记。"""
    room_dir, _ = _write_room(
        tmp_path,
        scenario_id=1,
        scenario_title="剧本",
        scenario_version="1.0.0",
        active_scene_id="101",
        active_scene_title="开场",
        scenario_started_at="2026-01-01 00:00:00",
        scenario_started_by=1,
    )
    (room_dir / "state.json").write_text(
        json.dumps({"active_scene_id": "101", "clues": ["c1"], "items": ["i1"], "quests": ["q1"], "triggered_event_ids": ["e1"]}),
        encoding="utf-8",
    )
    (room_dir / "knowledge_cursor.json").write_text(
        json.dumps({"scenario": "1@1.0.0", "rounds": ["chunk-0001"], "timed": {"chunk-0001": {}}}),
        encoding="utf-8",
    )
    # 同一剧本 id 的新版本，且旧场景在新版本中仍存在。
    _write_scenario(tmp_path, 1, "剧本", 101, scenario_version="2.0.0")

    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 1, "scenario_title": "剧本", "preserve_progress": True},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    stored = _read_room_info(tmp_path)
    # 版本已更新到最新，但剧情进度与开场标记都被继承。
    assert stored["scenario_version"] == "2.0.0"
    assert stored["active_scene_id"] == "101"
    assert stored["scenario_started_at"] == "2026-01-01 00:00:00"
    assert stored["scenario_started_by"] == 1

    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert state["active_scene_id"] == "101"
    assert state["clues"] == ["c1"]
    assert state["items"] == ["i1"]
    assert state["quests"] == ["q1"]
    assert state["triggered_event_ids"] == ["e1"]
    # 继承模式下不删除检索游标。
    assert (room_dir / "knowledge_cursor.json").exists()


def test_switch_room_scenario_preserve_progress_falls_back_when_scene_missing(tmp_path):
    """继承进度但旧场景在新版本中已不存在时，仅回退场景指针，其余进度保留。"""
    room_dir, _ = _write_room(
        tmp_path,
        scenario_id=1,
        scenario_title="剧本",
        scenario_version="1.0.0",
        active_scene_id="999",
        scenario_started_at="2026-01-01 00:00:00",
        scenario_started_by=1,
    )
    (room_dir / "state.json").write_text(
        json.dumps({"active_scene_id": "999", "clues": ["c1"]}),
        encoding="utf-8",
    )
    _write_scenario(tmp_path, 1, "剧本", 101, scenario_version="2.0.0")

    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 1, "scenario_title": "剧本", "preserve_progress": True},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.get_json()
    stored = _read_room_info(tmp_path)
    # 旧场景不存在 → 指针回退到新剧本的开场，但进度与开场标记保留。
    assert stored["active_scene_id"] == "101"
    assert stored["scenario_started_at"] == "2026-01-01 00:00:00"
    state = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    assert state["active_scene_id"] == "101"
    assert state["clues"] == ["c1"]


def test_switch_room_scenario_requires_manage_permission(tmp_path):
    """普通成员不能强制切换剧本。"""
    room_dir, _ = _write_room(
        tmp_path,
        creator_id=99,
        scenario_id=1,
        scenario_title="旧剧本",
        members=[{"user_id": 1, "username": "tester", "role": "USER", "status": "active", "is_active": True, "room_role": "member"}],
    )
    _write_scenario(tmp_path, 2, "新剧本", 202)

    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER")

    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 2},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 403
    assert _read_room_info(tmp_path)["scenario_id"] == 1


def test_switch_room_scenario_rejects_archived_and_unknown_target(tmp_path):
    _write_room(tmp_path, scenario_id=1, scenario_title="旧剧本", archived=True)
    _write_scenario(tmp_path, 2, "新剧本", 202)

    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    archived = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 2},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert archived.status_code == 409

    # 未归档的房间也会拒绝不存在的目标剧本。
    _write_room(tmp_path, scenario_id=1, scenario_title="旧剧本")
    unknown = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 999},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert unknown.status_code == 404
    assert _read_room_info(tmp_path)["scenario_id"] == 1