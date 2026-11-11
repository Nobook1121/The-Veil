from flask import Blueprint, current_app, session
from trpg_server.responses import error_response
bp = Blueprint("vector_health", __name__)
@bp.get("/api/vector/health")
def health():
    if not session.get("user_id"):
        return error_response("Authentication required", 401, "Authentication required")
    vector = current_app.extensions.get("vector_store")
    embedding = current_app.extensions.get("embedding_provider")
    ocr = current_app.extensions.get("ocr_provider")
    vector_health = vector.health() if vector else {"available": False, "last_indexed_at": None}
    embedding_health = dict(embedding.health()) if embedding and hasattr(embedding, "health") else {"configured": bool(embedding and embedding.configured), "loaded": False}
    backend = str(embedding_health.get("backend") or "")
    loaded = bool(embedding_health.get("loaded"))
    # 本地模型加载失败时 embed() 会退回哈希向量，所以实际是否兜底不能只看 backend。
    fallback = backend == "hashed" or (backend == "sentence-transformers" and not loaded)
    # ready 表示「现在有可用的向量生成路径」，兜底也算可用；云端后端的 loaded 只代表已配置，
    # 可达性要等首次调用才知道，因此用 verified 单独标记，不混进 ready。
    embedding_health["fallback"] = fallback
    embedding_health["verified"] = backend != "openai-compatible"
    embedding_health["ready"] = bool(backend) and (fallback or (loaded and not embedding_health.get("error")))
    return {"success": True, "data": {"vector": vector_health, "embedding": embedding_health, "ocr": {"enabled": bool(ocr), "available": bool(ocr and ocr.available)}}}
