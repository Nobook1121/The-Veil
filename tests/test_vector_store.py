from pathlib import Path

from trpg_server.agents.vector_store import EmbeddedVectorStore


def _chunk(chunk_id: str, vector: list[float], **metadata):
    return {
        "chunk_id": chunk_id,
        "text": f"text-{chunk_id}",
        "embedding": vector,
        **metadata,
    }


def test_embedded_store_upserts_and_queries_by_cosine_score(tmp_path: Path):
    store = EmbeddedVectorStore(tmp_path / "vectors")
    store.upsert(
        [
            _chunk("library", [1.0, 0.0], scenario_id="case-1", scenario_version="1", scene_id="scene-1"),
            _chunk("garden", [0.0, 1.0], scenario_id="case-1", scenario_version="1", scene_id="scene-1"),
        ]
    )

    results = store.query([1.0, 0.0], {"scenario_id": "case-1", "scenario_version": "1"}, 2)

    assert [item["id"] for item in results] == ["library", "garden"]
    assert results[0]["score"] == 1.0
    assert results[0]["payload"]["scene_id"] == "scene-1"
    assert store.count({"scenario_id": "case-1"}) == 2


def test_embedded_store_filters_before_query_and_deletes_by_filter(tmp_path: Path):
    store = EmbeddedVectorStore(tmp_path / "vectors")
    store.upsert(
        [
            _chunk("current", [1.0, 0.0], scenario_id="case-1", scenario_version="2", scene_id="scene-1", spoiler_level=0),
            _chunk("old", [1.0, 0.0], scenario_id="case-1", scenario_version="1", scene_id="scene-1", spoiler_level=0),
            _chunk("other", [1.0, 0.0], scenario_id="case-2", scenario_version="2", scene_id="scene-1", spoiler_level=0),
        ]
    )

    assert [item["id"] for item in store.query([1.0, 0.0], {"scenario_id": "case-1", "scenario_version": "2"}, 5)] == ["current"]
    assert store.delete_by_filter({"scenario_id": "case-1", "scenario_version": "1"}) == 1
    assert store.delete(["other"]) == 1
    assert store.count() == 1


def test_embedded_store_initializes_lazily_and_reports_health(tmp_path: Path):
    store = EmbeddedVectorStore(tmp_path / "nested" / "vectors")

    assert store.health()["available"] is True
    assert store.health()["backend"] == "embedded"
    assert (tmp_path / "nested" / "vectors" / "vectors.sqlite3").exists()


def test_embedded_store_health_returns_error_instead_of_raising(tmp_path: Path):
    store = EmbeddedVectorStore(tmp_path / "vectors")
    # 把库文件换成目录，SQLite 无法打开；探活应回传 error，而不是抛异常或长时间等待。
    store.db_path.unlink()
    store.db_path.mkdir()

    health = store.health()

    assert health["available"] is False
    assert health["backend"] == "embedded"
    assert health["error"]
