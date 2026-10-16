"""Backend-neutral vector storage with an embedded SQLite default.

The embedded implementation deliberately uses only Python's standard library. The
Qdrant adapter remains optional and is selected explicitly by configuration.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping


class VectorStore:
    """Small contract shared by embedded and remote vector backends."""

    def upsert(self, chunks: Iterable[Any], points: Iterable[dict[str, Any]] | None = None) -> bool:
        raise NotImplementedError

    def delete(self, ids: Iterable[str]) -> int:
        raise NotImplementedError

    def delete_by_filter(self, filter: Mapping[str, Any]) -> int:
        raise NotImplementedError

    def deleteByFilter(self, filter: Mapping[str, Any]) -> int:  # noqa: N802 - compatibility alias
        return self.delete_by_filter(filter)

    def query(self, vector: list[float], filter: Mapping[str, Any] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count(self, filter: Mapping[str, Any] | None = None) -> int:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"available": False}

    def scenario_ids(self) -> set[str] | None:
        """枚举当前库中出现过的 scenario_id；后端不支持时返回 ``None``。"""
        return None

    def search(self, collection: str, vector: list[float], limit: int = 5) -> list[dict[str, Any]]:
        """Compatibility shim for the former search(collection, vector, limit) API."""
        return self.query(vector, {"collection": collection}, limit)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        return dict(converted) if isinstance(converted, Mapping) else {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _normalise_chunk(value: Any) -> tuple[str, list[float], dict[str, Any]] | None:
    chunk = _as_dict(value)
    chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "").strip()
    vector = chunk.get("embedding")
    if not isinstance(vector, list):
        vector = chunk.get("vector")
    if not chunk_id or not isinstance(vector, list) or not vector:
        return None
    try:
        values = [float(item) for item in vector]
    except (TypeError, ValueError):
        return None
    payload = dict(chunk)
    payload["chunk_id"] = chunk_id
    payload.pop("vector", None)
    payload.pop("embedding", None)
    return chunk_id, values, payload


def _matches(payload: Mapping[str, Any], filter: Mapping[str, Any] | None) -> bool:
    if not filter:
        return True
    for key, expected in filter.items():
        actual = payload.get(key)
        if isinstance(expected, Mapping):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                return False
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            continue
        if actual != expected and str(actual) != str(expected):
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


class EmbeddedVectorStore(VectorStore):
    """Persistent SQLite vector store requiring no native extension or service."""

    _lock = threading.RLock()

    def __init__(self, path: str | Path = "data/runtime/vector-db/embedded"):
        raw = Path(path)
        self.db_path = raw if raw.suffix in {".sqlite", ".sqlite3", ".db"} else raw / "vectors.sqlite3"
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialise(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    id TEXT PRIMARY KEY,
                    vector TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    scenario_id TEXT,
                    scenario_version TEXT,
                    scene_id TEXT,
                    card_type TEXT,
                    visibility TEXT,
                    spoiler_level INTEGER,
                    collection TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vectors_scenario ON vectors(scenario_id, scenario_version)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vectors_scene ON vectors(scene_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vectors_spoiler ON vectors(spoiler_level)")

    def _iter_input(self, chunks: Iterable[Any], points: Iterable[dict[str, Any]] | None) -> list[tuple[str, list[float], dict[str, Any]]]:
        if points is not None:
            collection = str(chunks)
            values = []
            for point in points:
                item = dict(point)
                payload = dict(item.get("payload") or {})
                payload.setdefault("collection", collection)
                item["id"] = payload.get("chunk_id") or payload.get("module_id") or item.get("id")
                item["embedding"] = item.get("vector")
                values.append(item)
            chunks = values
        normalised = []
        for chunk in chunks:
            item = _normalise_chunk(chunk)
            if item:
                normalised.append(item)
        return normalised

    @staticmethod
    def _storage_id(chunk_id: str, payload: Mapping[str, Any]) -> str:
        namespace = payload.get("collection")
        if not namespace and payload.get("scenario_id") is not None:
            namespace = f"scenario_{payload.get('scenario_id')}_{payload.get('scenario_version', '1')}"
        return f"{namespace}:{chunk_id}" if namespace else chunk_id

    def upsert(self, chunks: Iterable[Any], points: Iterable[dict[str, Any]] | None = None) -> bool:
        rows = self._iter_input(chunks, points)
        if not rows:
            return True
        with self._lock, self._session() as connection:
            connection.executemany(
                """
                INSERT INTO vectors(id, vector, payload, scenario_id, scenario_version, scene_id,
                                    card_type, visibility, spoiler_level, collection, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    vector=excluded.vector, payload=excluded.payload,
                    scenario_id=excluded.scenario_id, scenario_version=excluded.scenario_version,
                    scene_id=excluded.scene_id, card_type=excluded.card_type,
                    visibility=excluded.visibility, spoiler_level=excluded.spoiler_level,
                    collection=excluded.collection, updated_at=CURRENT_TIMESTAMP
                """,
                [
                    (
                        self._storage_id(chunk_id, payload),
                        json.dumps(vector, separators=(",", ":")),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        payload.get("scenario_id"),
                        payload.get("scenario_version"),
                        payload.get("scene_id"),
                        payload.get("card_type"),
                        payload.get("visibility"),
                        int(payload.get("spoiler_level", 0) or 0),
                        payload.get("collection"),
                    )
                    for chunk_id, vector, payload in rows
                ],
            )
        return True

    def _rows(self, filter: Mapping[str, Any] | None = None) -> list[tuple[str, list[float], dict[str, Any]]]:
        with self._lock, self._session() as connection:
            values = connection.execute("SELECT id, vector, payload FROM vectors").fetchall()
        result = []
        for row in values:
            try:
                payload = json.loads(row[2])
                vector = [float(item) for item in json.loads(row[1])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and _matches(payload, filter):
                result.append((str(row[0]), vector, payload))
        return result

    def query(self, vector: list[float], filter: Mapping[str, Any] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        try:
            query_vector = [float(item) for item in vector]
        except (TypeError, ValueError):
            return []
        scored = [(_cosine(query_vector, values), chunk_id, payload) for chunk_id, values, payload in self._rows(filter)]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"id": str(payload.get("chunk_id") or chunk_id), "score": score, "payload": payload}
            for score, chunk_id, payload in scored[: max(0, int(top_k))]
        ]

    def search(self, collection: str, vector: list[float], limit: int = 5) -> list[dict[str, Any]]:
        return self.query(vector, {"collection": collection}, limit)

    def delete(self, ids: Iterable[str]) -> int:
        values = {str(value) for value in ids}
        if not values:
            return 0
        storage_ids = [
            storage_id
            for storage_id, _vector, payload in self._rows()
            if storage_id in values or str(payload.get("chunk_id") or "") in values
        ]
        with self._lock, self._session() as connection:
            cursor = connection.executemany("DELETE FROM vectors WHERE id = ?", [(value,) for value in storage_ids])
            return int(cursor.rowcount or 0)

    def delete_by_filter(self, filter: Mapping[str, Any]) -> int:
        ids = [item[0] for item in self._rows(filter)]
        return self.delete(ids)

    def count(self, filter: Mapping[str, Any] | None = None) -> int:
        return len(self._rows(filter))

    def health(self) -> dict[str, Any]:
        try:
            self._initialise()
            return {"available": True, "backend": "embedded", "path": str(self.db_path)}
        except Exception:
            return {"available": False, "backend": "embedded"}

    def scenario_ids(self) -> set[str] | None:
        with self._lock, self._session() as connection:
            rows = connection.execute(
                "SELECT DISTINCT scenario_id FROM vectors "
                "WHERE scenario_id IS NOT NULL AND scenario_id <> ''"
            ).fetchall()
        return {str(row[0]) for row in rows}


class QdrantVectorStore(VectorStore):
    """Optional Qdrant adapter. Importing this class never requires qdrant-client."""

    def __init__(self, url: str | None = None, path: str | None = None, api_key: str | None = None, dimensions: int | None = 256):
        self.url, self.path, self.api_key = url, path, api_key
        self.dimensions = int(dimensions) if dimensions else None
        self.client = None
        self._last_collection: str | None = None
        try:
            from qdrant_client import QdrantClient

            self.client = QdrantClient(url=url, path=path, api_key=api_key) if url else QdrantClient(path=path or "data/runtime/vector-db/qdrant")
        except Exception:
            self.client = None

    def health(self):
        if not self.client:
            return {"available": False, "backend": "qdrant"}
        try:
            self.client.get_collections()
            return {"available": True, "backend": "qdrant"}
        except Exception:
            return {"available": False, "backend": "qdrant"}

    def upsert(self, chunks: Iterable[Any], points: Iterable[dict[str, Any]] | None = None) -> bool:
        if not self.client:
            return False
        collection = str(chunks) if points is not None else None
        values = list(points) if points is not None else list(chunks)
        normalised = [_normalise_chunk(item) for item in values]
        normalised = [item for item in normalised if item]
        if not normalised:
            return True
        try:
            from qdrant_client.models import Distance, PointStruct, VectorParams

            if not collection:
                first_payload = normalised[0][2]
                collection = str(first_payload.get("collection") or f"scenario_{first_payload.get('scenario_id', 'default')}_{first_payload.get('scenario_version', '1')}")
            vector_size = len(normalised[0][1])
            if not self.client.collection_exists(collection):
                self.dimensions = vector_size
                self.client.create_collection(collection_name=collection, vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE))
            elif self.dimensions and vector_size != self.dimensions:
                return False
            self.client.upsert(
                collection_name=collection,
                points=[
                    PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection}:{chunk_id}")),
                        vector=vector,
                        payload=payload,
                    )
                    for chunk_id, vector, payload in normalised
                ],
            )
            self._last_collection = collection
            return True
        except Exception:
            return False

    def query(self, vector: list[float], filter: Mapping[str, Any] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        if not self.client:
            return []
        try:
            collection = str((filter or {}).get("collection") or "")
            if not collection:
                return []
            self._last_collection = collection
            items = self.client.search(collection_name=collection, query_vector=vector, limit=int(top_k))
            return [{"id": str(item.id), "score": float(item.score), "payload": item.payload or {}} for item in items]
        except Exception:
            return []

    def search(self, collection: str, vector: list[float], limit: int = 5) -> list[dict[str, Any]]:
        return self.query(vector, {"collection": collection}, limit)

    def delete(self, ids: Iterable[str]) -> int:
        if not self.client or not self._last_collection:
            return 0
        values = [str(value) for value in ids]
        if not values:
            return 0
        try:
            from qdrant_client.models import PointIdsList

            point_ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self._last_collection}:{value}")) for value in values]
            self.client.delete(collection_name=self._last_collection, points_selector=PointIdsList(points=point_ids))
            return len(values)
        except Exception:
            return 0

    def delete_by_filter(self, filter: Mapping[str, Any]) -> int:
        if not self.client:
            return 0
        collection = str(filter.get("collection") or self._last_collection or "")
        if not collection:
            return 0
        try:
            from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

            conditions = []
            for key, value in filter.items():
                if key == "collection" or isinstance(value, Mapping):
                    continue
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
            selector = FilterSelector(filter=Filter(must=conditions))
            result = self.client.delete(collection_name=collection, points_selector=selector)
            return int(getattr(result, "operation_id", 0) or 0)
        except Exception:
            return 0

    def count(self, filter: Mapping[str, Any] | None = None) -> int:
        if not self.client:
            return 0
        collection = str((filter or {}).get("collection") or self._last_collection or "")
        if not collection:
            return 0
        try:
            result = self.client.count(collection_name=collection, exact=True)
            return int(getattr(result, "count", 0) or 0)
        except Exception:
            return 0


def create_vector_store(*, backend: str | None = None, url: str | None = None, path: str | Path = "data/runtime/vector-db/embedded", api_key: str | None = None, dimensions: int = 256) -> VectorStore:
    selected = (backend or "").strip().lower()
    if selected == "qdrant" or (not selected and url):
        raw_path = Path(path)
        qdrant_path = raw_path.parent / "qdrant" if raw_path.name == "embedded" else raw_path
        return QdrantVectorStore(url=url, path=str(qdrant_path), api_key=api_key, dimensions=dimensions)
    return EmbeddedVectorStore(path)
