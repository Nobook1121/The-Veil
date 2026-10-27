"""世界树 / 图谱改为手动建图：读取与保存都不建图，由图谱页签的按钮触发。"""
import json

from trpg_server.app_factory import create_app
from trpg_server.routes import scenarios as scenarios_module

CSRF = {"X-CSRF-Token": "csrf"}

SCENARIO_MODULES = [
    {"id": "scene-hall", "module_type": "scene", "title": "大厅", "content": "大厅与书房相连。"},
    {"id": "scene-study", "module_type": "scene", "title": "书房", "content": "书房里有一张旧地图。"},
    {"id": "ending-true", "module_type": "ending", "title": "真相", "content": "调查员找到了旧地图。"},
]


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


def _create(client, modules):
    created = client.post(
        "/api/scenarios",
        json={"title": "手动建图", "author": "A", "playerCount": 2, "modules": modules},
        headers=CSRF,
    )
    assert created.status_code == 201, created.get_json()
    return created.get_json()["data"]["id"]


def test_narrative_read_does_not_build_graph(tmp_path, monkeypatch):
    app = _app(tmp_path)
    scenarios_dir = _isolate(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    scenario_id = _create(client, SCENARIO_MODULES)
    narrative = scenarios_dir / f"scenario-{scenario_id}" / "narrative.json"
    assert not narrative.exists(), "保存剧本不建图，避免与发布硬闸门互相干扰"

    loaded = client.get(f"/api/scenarios/{scenario_id}/narrative")
    assert loaded.status_code == 404
    assert not narrative.exists(), "读取世界树不应再隐式建图"


def test_build_endpoint_creates_graph_and_refuses_to_overwrite(tmp_path, monkeypatch):
    app = _app(tmp_path)
    scenarios_dir = _isolate(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    scenario_id = _create(client, SCENARIO_MODULES)
    narrative = scenarios_dir / f"scenario-{scenario_id}" / "narrative.json"

    built = client.post(f"/api/scenarios/{scenario_id}/narrative/build", headers=CSRF)
    assert built.status_code == 200, built.get_json()
    data = built.get_json()["data"]
    assert data["bundle"]["nodes"]
    assert data["bundle"]["scenario_id"] == str(scenario_id)
    assert narrative.exists()
    assert json.loads(narrative.read_text(encoding="utf-8"))["nodes"]
    before = narrative.read_bytes()

    # 已有图谱时按钮不应覆盖作者可能编辑过的布局 / 可见性 / 连线
    again = client.post(f"/api/scenarios/{scenario_id}/narrative/build", headers=CSRF)
    assert again.status_code == 409
    assert narrative.read_bytes() == before


def test_build_endpoint_rejects_scenario_without_modules(tmp_path, monkeypatch):
    app = _app(tmp_path)
    scenarios_dir = _isolate(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    scenario_id = _create(client, [])

    built = client.post(f"/api/scenarios/{scenario_id}/narrative/build", headers=CSRF)
    assert built.status_code == 400
    assert not (scenarios_dir / f"scenario-{scenario_id}" / "narrative.json").exists()
