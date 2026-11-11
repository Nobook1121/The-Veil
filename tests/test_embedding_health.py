from pathlib import Path

from trpg_server.agents.embedding_provider import HashedTokenEmbedding
from trpg_server.app_factory import create_app


class _Manager:
    def is_session_current(self, user_id, token): return True
    def get_user_by_id(self, user_id): return {"id": user_id, "username": "admin", "role": "ADMIN"}


class _StubEmbedding:
    def __init__(self, health: dict):
        self._health = health

    def health(self) -> dict:
        return dict(self._health)


def _health_client(tmp_path: Path, name: str, embedding_health: dict | None = None):
    app = create_app({"TESTING": True, "USER_MANAGER": _Manager(), "CONFIG_DIR": tmp_path / name / "config", "SCENARIOS_DIR": tmp_path / name / "scenarios", "EMBEDDED_VECTOR_DB_PATH": tmp_path / name / "vectors", "LOCAL_EMBEDDING_MODEL_PATH": tmp_path / name / "missing-model"})
    if embedding_health is not None:
        app.extensions["embedding_provider"] = _StubEmbedding(embedding_health)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="admin", role="ADMIN", session_token="token", csrf_token="csrf")
    return client


def _embedding_payload(client) -> dict:
    response = client.get("/api/vector/health")
    assert response.status_code == 200
    return response.get_json()["data"]["embedding"]


def test_hashed_embedding_health_reports_loaded_status():
    health = HashedTokenEmbedding(16).health()
    assert health["loaded"] is True
    assert health["backend"] == "hashed"
    assert health["dimensions"] == 16


def test_vector_health_endpoint_exposes_embedding_load_status(tmp_path: Path):
    payload = _health_client(tmp_path, "hashed").get("/api/vector/health").get_json()["data"]
    embedding = payload["embedding"]
    assert embedding["loaded"] is True
    assert embedding["ready"] is True
    assert embedding["fallback"] is True
    assert embedding["verified"] is True
    assert "dimensions" in embedding
    assert "last_indexed_at" in payload["vector"]
    assert "error" not in payload["vector"]


def test_vector_health_treats_failed_local_model_as_hash_fallback(tmp_path: Path):
    client = _health_client(tmp_path, "local-failed", {"backend": "sentence-transformers", "configured": True, "loaded": False, "model": "missing", "dimensions": None, "error": "boom"})

    embedding = _embedding_payload(client)

    # 本地模型加载失败时运行时退回哈希向量：可用但降级，不能报成「不可用」。
    assert embedding["ready"] is True
    assert embedding["fallback"] is True
    assert embedding["verified"] is True
    assert embedding["error"] == "boom"


def test_vector_health_marks_cloud_embedding_as_unverified(tmp_path: Path):
    client = _health_client(tmp_path, "cloud", {"backend": "openai-compatible", "configured": True, "loaded": True, "model": "text-embedding-3-small", "dimensions": 256})

    embedding = _embedding_payload(client)

    # 云端后端只能确认已配置，可达性未验证，因此 ready 不再等同于「已验证」。
    assert embedding["ready"] is True
    assert embedding["fallback"] is False
    assert embedding["verified"] is False
