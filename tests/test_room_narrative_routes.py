"""房间侧图谱运行态接口（§8.5）回归测试。"""
from __future__ import annotations

import json

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeEdge, NarrativeNode
from trpg_server.agents.narrative.store import save_bundle
from trpg_server.app_factory import create_app


class FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
        "NARRATIVE_JOBS_DIR": tmp_path / "narrative-jobs",
    })


def _login(client, user_id=1, room_role="owner"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role="USER", session_token="token", csrf_token="csrf")


def _write_room(tmp_path, *, scenario_id="s1", room_role="owner", user_id=1, creator_id=1):
    room_dir = tmp_path / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": "r-1",
        "name": "Test Room",
        "room_code": "ABCD1234",
        "creator_id": creator_id,
        "scenario_id": scenario_id,
        "members": [
            {
                "user_id": user_id,
                "username": "tester",
                "role": "USER",
                "status": "active",
                "is_active": True,
                "room_role": room_role,
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _write_scenario(tmp_path):
    storage = tmp_path / "scenarios" / "scenario-s1"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "scenario.json").write_text(
        json.dumps({"id": "s1", "title": "测试剧本", "version": "1.0.0", "modules": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    bundle = NarrativeBundle(
        scenario_id="s1",
        scenario_version="1.0.0",
        nodes=[
            NarrativeNode(id="room_hall", kind="location", title="门厅", summary="门厅"),
            NarrativeNode(id="room_cellar", kind="location", title="地下室"),
        ],
        edges=[NarrativeEdge(id="edge_hall_cellar", from_id="room_hall", to_id="room_cellar", type="adjacent")],
        triggers=[
            {
                "id": "trg_manual",
                "label": "敲门",
                "binding": {"kind": "manual", "keyword": "敲门"},
                "content": "门后传来回应。",
            },
            {
                "id": "trg_kp_only",
                "label": "KP 备忘",
                "binding": {"kind": "manual", "keyword": "备忘"},
                "content": "仅 KP 可见的内容。",
                "visibility": "kp_only",
            },
        ],
        start={"location_id": "room_hall"},
    )
    save_bundle(storage, bundle)
    return storage


def test_room_narrative_reports_location_party_and_status(tmp_path):
    _write_room(tmp_path)
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/narrative")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["location"]["id"] == "room_hall"
    assert data["location"]["title"] == "门厅"
    assert data["status"]["topology"]
    assert data["party"]["split"] is False
    assert data["permissions"]["can_manage"] is True


def test_room_triggers_list_hides_attachment_bodies(tmp_path):
    _write_room(tmp_path)
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/triggers")

    assert response.status_code == 200
    data = response.get_json()["data"]
    triggers = data.get("triggers") or []
    assert {item["trigger_id"] for item in triggers} >= {"trg_manual"}
    assert all("content" not in item for item in triggers)


def test_room_trigger_manual_fire_writes_state(tmp_path):
    _write_room(tmp_path)
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    fired = client.post("/api/rooms/r-1/triggers/trg_manual/fire", headers={"X-CSRF-Token": "csrf"})

    assert fired.status_code == 200
    assert fired.get_json()["data"]["triggered"] is True
    status = client.get("/api/rooms/r-1/narrative").get_json()["data"]["status"]
    assert status["trigger_fires"] == 1


def test_room_trigger_fire_requires_manager(tmp_path):
    _write_room(tmp_path, room_role="player", user_id=2, creator_id=1)
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client, user_id=2)

    response = client.post("/api/rooms/r-1/triggers/trg_manual/fire", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 403
