"""世界树 / 图谱的按需补建：导入保存后的剧本首次打开世界树页签即可看到图谱。"""
import json

from trpg_server.app_factory import create_app
from trpg_server.routes import scenarios as scenarios_module

CSRF = {"X-CSRF-Token": "csrf"}


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager("ADMIN"),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
    })


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")


def _isolate(tmp_path, monkeypatch):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scenarios_module, "SCENARIOS_DIR", scenarios_dir)
    monkeypatch.setattr(scenarios_module, "SCENARIO_COVERS_DIR", tmp_path / "covers")
    return scenarios_dir


def test_first_narrative_read_builds_graph_for_imported_scenario(tmp_path, monkeypatch):
    app = _app(tmp_path)
    scenarios_dir = _isolate(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)

    created = client.post(
        "/api/scenarios",
        json={
            "title": "自动建图",
            "author": "A",
            "playerCount": 2,
            "modules": [
                {"id": "scene-hall", "module_type": "scene", "title": "大厅", "content": "大厅与书房相连。"},
                {"id": "scene-study", "module_type": "scene", "title": "书房", "content": "书房里有一张旧地图。"},
                {"id": "ending-true", "module_type": "ending", "title": "真相", "content": "调查员找到了旧地图。"},
            ],
        },
        headers=CSRF,
    )
    assert created.status_code == 201, created.get_json()
    scenario_id = created.get_json()["data"]["id"]
    narrative = scenarios_dir / f"scenario-{scenario_id}" / "narrative.json"
    assert not narrative.exists(), "保存剧本本身不建图，避免与发布硬闸门互相干扰"

    loaded = client.get(f"/api/scenarios/{scenario_id}/narrative")
    assert loaded.status_code == 200, loaded.get_json()
    payload = loaded.get_json()["data"]["bundle"]
    assert payload["scenario_id"] == str(scenario_id)
    assert payload["nodes"]

    assert narrative.exists(), "首次读取世界树时应自动补建 narrative.json"
    assert json.loads(narrative.read_text(encoding="utf-8"))["nodes"]


def test_narrative_read_without_modules_keeps_empty_state(tmp_path, monkeypatch):
    app = _app(tmp_path)
    scenarios_dir = _isolate(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)

    created = client.post(
        "/api/scenarios",
        json={"title": "直接导入", "author": "A", "playerCount": 2, "modules": []},
        headers=CSRF,
    )
    assert created.status_code == 201, created.get_json()
    scenario_id = created.get_json()["data"]["id"]

    loaded = client.get(f"/api/scenarios/{scenario_id}/narrative")
    assert loaded.status_code == 404
    assert not (scenarios_dir / f"scenario-{scenario_id}" / "narrative.json").exists()
