"""剧本世界树 / 图谱编辑 API 的确定性测试（设计文档 §8.5）。"""
from __future__ import annotations

import json

from trpg_server.app_factory import create_app

CSRF = {"X-CSRF-Token": "csrf"}


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


def _login(client, user_id=1, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _bundle(revision=2):
    return {
        "scenario_id": "7",
        "scenario_version": "1.0.0",
        "topology": "hub",
        "nodes": [
            {"id": "hall", "kind": "location", "title": "大厅", "access": "free", "scene_role": "hub"},
            {"id": "room", "kind": "location", "title": "房间"},
        ],
        "edges": [
            {"id": "edge_hall_room", "from": "hall", "to": "room", "type": "adjacent"},
        ],
        "meta": {"revision": revision},
    }


def _write_scenario(tmp_path, *, import_mode="edit", owner_id=1, bundle=None):
    storage = tmp_path / "scenarios" / "scenario-7"
    storage.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "id": 7,
        "title": "测试剧本",
        "owner_id": owner_id,
        "import_mode": import_mode,
        "modules": [],
    }
    (storage / "scenario.json").write_text(json.dumps(descriptor, ensure_ascii=False), encoding="utf-8")
    if bundle is not None:
        (storage / "narrative.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return storage


def _get(client):
    return client.get("/api/scenarios/7/narrative")


def _patch_node(client, node_id="room", **body):
    payload = {"base_revision": 2}
    payload.update(body)
    return client.patch(f"/api/scenarios/7/narrative/nodes/{node_id}", json=payload, headers=CSRF)


def test_get_narrative_returns_bundle_and_permissions(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _get(client)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert [node["id"] for node in data["bundle"]["nodes"]] == ["hall", "room"]
    assert data["revision"] == 2
    assert data["permissions"] == {"can_view": True, "can_edit": True, "readonly": False}
    assert data["summary"]["counts"]["nodes"] == 2


def test_get_narrative_requires_generated_bundle(tmp_path):
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = _get(client)

    assert response.status_code == 404


def test_patch_node_updates_fields_and_bumps_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _patch_node(client, title="密室", visibility="kp_only", kp_notes="仅 KP")

    assert response.status_code == 200
    assert response.get_json()["data"]["revision"] == 3
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in saved["nodes"] if item["id"] == "room")
    assert node["title"] == "密室"
    assert node["visibility"] == "kp_only"
    assert node["kp_notes"] == "仅 KP"
    assert saved["meta"]["revision"] == 3


def test_patch_node_rejects_stale_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/nodes/room",
        json={"base_revision": 1, "title": "新"},
        headers=CSRF,
    )

    assert response.status_code == 409
    assert "revision 2" in response.get_data(as_text=True)


def test_patch_requires_base_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch("/api/scenarios/7/narrative/nodes/room", json={"title": "新"}, headers=CSRF)

    assert response.status_code == 409


def test_patch_edge_updates_type(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/edges/edge_hall_room",
        json={"base_revision": 2, "hard": True, "label": "门"},
        headers=CSRF,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["edge"]["hard"] is True


def test_put_narrative_replaces_bundle(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)
    bundle = _bundle()
    bundle["nodes"].append({"id": "ending_a", "kind": "ending", "title": "结局 A"})

    response = client.put(
        "/api/scenarios/7/narrative",
        json={"base_revision": 2, "bundle": bundle},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["revision"] == 3
    assert any(node["id"] == "ending_a" for node in data["bundle"]["nodes"])
    assert data["bundle"]["scenario_id"] == "7"


def test_put_narrative_rejects_empty_nodes(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.put(
        "/api/scenarios/7/narrative",
        json={"base_revision": 2, "bundle": {"nodes": [], "edges": []}},
        headers=CSRF,
    )

    assert response.status_code == 400


def test_validate_returns_report_and_order_simulation(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/narrative/validate",
        json={"all_orders": True},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["topology"] == "hub"
    assert isinstance(data["issues"], list)
    assert data["order_simulation"]["ok"] is True


def test_topology_override_marks_author_source(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/topology",
        json={"base_revision": 2, "topology": "linear"},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["topology"] == "linear"
    assert data["source"] == "author"


def test_topology_override_rejects_unknown_value(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/topology",
        json={"base_revision": 2, "topology": "free"},
        headers=CSRF,
    )

    assert response.status_code == 400


def test_direct_import_is_read_only(tmp_path):
    _write_scenario(tmp_path, import_mode="direct", bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    assert _get(client).get_json()["data"]["permissions"]["readonly"] is True
    response = _patch_node(client, title="新")
    assert response.status_code == 403


def test_non_owner_cannot_edit(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path, role="USER").test_client()
    _login(client, user_id=2, role="USER")

    response = _patch_node(client, title="新")

    assert response.status_code == 403
