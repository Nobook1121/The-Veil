from pathlib import Path

from trpg_server.agents.vector_store import EmbeddedVectorStore, QdrantVectorStore, create_vector_store
from trpg_server.app_factory import create_app


def test_factory_defaults_to_embedded_store(tmp_path: Path):
    store = create_vector_store(path=tmp_path / "embedded")

    assert isinstance(store, EmbeddedVectorStore)
    assert store.db_path == tmp_path / "embedded" / "vectors.sqlite3"


def test_factory_uses_qdrant_for_explicit_backend_without_importing_service(tmp_path: Path):
    store = create_vector_store(backend="qdrant", path=tmp_path / "embedded")

    assert isinstance(store, QdrantVectorStore)
    assert store.path.endswith("qdrant")


def test_app_factory_exposes_embedded_backend_config(tmp_path: Path):
    app = create_app({"TESTING": True, "VECTOR_BACKEND": "embedded", "VECTOR_DB_PATH": tmp_path / "embedded"})

    assert app.config["VECTOR_BACKEND"] == "embedded"
    assert isinstance(app.extensions["vector_store"], EmbeddedVectorStore)


def test_app_factory_url_selects_qdrant_when_backend_was_not_explicit(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "VECTOR_DB_URL": "http://127.0.0.1:6333",
            "VECTOR_DB_PATH": tmp_path / "qdrant",
            "EMBEDDED_VECTOR_DB_PATH": tmp_path / "embedded",
            "LOCAL_EMBEDDING_MODEL_PATH": tmp_path / "missing",
        }
    )

    assert isinstance(app.extensions["vector_store"], QdrantVectorStore)
