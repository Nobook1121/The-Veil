from pathlib import Path

from trpg_server.agents.embedding_provider import HashedTokenEmbedding
from trpg_server.app_factory import create_app


class _Manager:
    def is_session_current(self, user_id, token): return True
    def get_user_by_id(self, user_id): return {"id": user_id, "username": "admin", "role": "ADMIN"}


def test_hashed_embedding_health_reports_loaded_status():
    health = HashedTokenEmbedding(16).health()
    assert health["loaded"] is True
    assert health["backend"] == "hashed"
    assert health["dimensions"] == 16


def test_vector_health_endpoint_exposes_embedding_load_status(tmp_path: Path):
    app = create_app({"TESTING": True, "USER_MANAGER": _Manager(), "CONFIG_DIR": tmp_path / "config", "SCENARIOS_DIR": tmp_path / "scenarios", "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors", "LOCAL_EMBEDDING_MODEL_PATH": tmp_path / "missing-model"})
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="admin", role="ADMIN", session_token="token", csrf_token="csrf")
    response = client.get("/api/vector/health")
    assert response.status_code == 200
    embedding = response.get_json()["data"]["embedding"]
    assert embedding["loaded"] is True
    assert "dimensions" in embedding
