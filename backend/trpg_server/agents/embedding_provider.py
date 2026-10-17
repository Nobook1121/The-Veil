"""Embedding providers with deterministic offline fallback."""
from __future__ import annotations
import hashlib, math
from importlib.util import find_spec
from typing import Sequence
from pathlib import Path

class EmbeddingError(RuntimeError): pass

class HashedTokenEmbedding:
    configured = True

    def __init__(self, dimensions: int = 256): self.dimensions = max(8, int(dimensions))
    def health(self) -> dict:
        return {"backend": "hashed", "configured": True, "loaded": True, "dimensions": self.dimensions}
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors=[]
        for text in texts:
            values=[0.0]*self.dimensions; normalized=str(text or "").casefold(); tokens=list(normalized)
            tokens += [normalized[i:i+2] for i in range(max(0,len(normalized)-1))]
            for token in tokens:
                digest=hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(); index=int.from_bytes(digest[:4],"big")%self.dimensions; sign=1.0 if digest[4]&1 else -1.0; values[index]+=sign
            norm=math.sqrt(sum(v*v for v in values)) or 1.0; vectors.append([v/norm for v in values])
        return vectors

class OpenAICompatibleEmbeddingProvider:
    def __init__(self, base_url: str|None, api_key: str|None, model: str|None, dimensions: int = 256, timeout: float = 30):
        self.base_url, self.api_key, self.model, self.dimensions, self.timeout = base_url, api_key, model, int(dimensions), timeout; self.fallback=HashedTokenEmbedding(self.dimensions)
    @property
    def configured(self): return bool(self.base_url and self.api_key and self.model)
    def health(self) -> dict:
        return {
            "backend": "openai-compatible",
            "configured": self.configured,
            "loaded": self.configured,
            "model": self.model,
            "dimensions": self.dimensions,
        }
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.configured: return self.fallback.embed(texts)
        try:
            import requests
            response=requests.post(self.base_url, headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"}, json={"model":self.model,"input":list(texts)}, timeout=self.timeout)
            response.raise_for_status(); values=[item.get("embedding") for item in response.json().get("data",[])]
            if len(values)!=len(texts) or any(not isinstance(v,list) or len(v)!=self.dimensions for v in values): raise EmbeddingError("embedding dimension mismatch")
            return values
        except Exception:
            return self.fallback.embed(texts)

class LocalSentenceTransformerEmbedding:
    configured = True

    def __init__(self, model_path: str | Path, fallback_dimensions: int = 256):
        self.model_path=Path(model_path); self._model=None; self.dimensions=None; self.fallback=HashedTokenEmbedding(fallback_dimensions)
    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model=SentenceTransformer(str(self.model_path), local_files_only=True)
            get_dimensions = getattr(self._model, "get_embedding_dimension", None) or getattr(self._model, "get_sentence_embedding_dimension", None)
            if callable(get_dimensions):
                self.dimensions = get_dimensions()
        return self._model
    def health(self) -> dict:
        try:
            self._load()
            return {"backend": "sentence-transformers", "configured": True, "loaded": True, "model": str(self.model_path), "dimensions": self.dimensions}
        except Exception as exc:
            return {"backend": "sentence-transformers", "configured": True, "loaded": False, "model": str(self.model_path), "dimensions": self.dimensions, "error": str(exc)}
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            vectors = self._load().encode(list(texts), normalize_embeddings=True).tolist()
        except Exception:
            return self.fallback.embed(texts)
        if vectors and self.dimensions is None:
            self.dimensions = len(vectors[0])
        return vectors

def _local_backend_available(model_path: str|Path|None) -> bool:
    """本地模型后端仅在「模型目录存在 且 sentence-transformers 可导入」时才算可用。

    只判断目录是否存在是不够的：目录在、依赖没装时会选中一个必然加载失败的本地后端，
    启动时报 embedding_model_load_failed，检索时再静默退回哈希向量。这里显式跳过，
    让 select_embedding_provider 继续往下选云端接口或哈希兜底。
    """
    if not model_path or not Path(model_path).is_dir():
        return False
    try:
        return find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):
        return False

def select_embedding_provider(*, local_model_path: str|Path|None=None, base_url: str|None=None, api_key: str|None=None, model: str|None=None, dimensions: int=256):
    if _local_backend_available(local_model_path): return LocalSentenceTransformerEmbedding(local_model_path, dimensions)
    if base_url and api_key and model: return OpenAICompatibleEmbeddingProvider(base_url, api_key, model, dimensions)
    return HashedTokenEmbedding(dimensions)
