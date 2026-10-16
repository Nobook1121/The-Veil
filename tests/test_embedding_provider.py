from trpg_server.agents import embedding_provider
from trpg_server.agents.embedding_provider import HashedTokenEmbedding, select_embedding_provider


def _stub_find_spec(available: bool):
    """模拟 sentence-transformers 是否已安装，让选择逻辑的测试与真实环境解耦。"""
    def fake(name, *args, **kwargs):
        return object() if available and name == "sentence_transformers" else None
    return fake


def test_embedding_provider_uses_hashed_fallback_without_local_or_remote_config(tmp_path, monkeypatch):
    monkeypatch.setattr(embedding_provider, "find_spec", _stub_find_spec(False))
    provider = select_embedding_provider(local_model_path=tmp_path / "missing", dimensions=32)
    assert isinstance(provider, HashedTokenEmbedding)
    assert len(provider.embed(["灯塔"])[0]) == 32


def test_embedding_provider_prefers_existing_local_model_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(embedding_provider, "find_spec", _stub_find_spec(True))
    model = tmp_path / "bge-small-zh-v1.5"
    model.mkdir()
    provider = select_embedding_provider(local_model_path=model, dimensions=512)
    assert provider.__class__.__name__ == "LocalSentenceTransformerEmbedding"


def test_embedding_provider_skips_local_model_when_stack_missing(tmp_path, monkeypatch):
    """本地目录存在但 sentence-transformers 未安装时，不应选中它——旧行为会选中一个必然加载失败的后端。"""
    monkeypatch.setattr(embedding_provider, "find_spec", _stub_find_spec(False))
    model = tmp_path / "bge-small-zh-v1.5"
    model.mkdir()
    provider = select_embedding_provider(
        local_model_path=model, base_url="https://example/v1", api_key="k", model="text-embedding-v3", dimensions=1024
    )
    assert provider.__class__.__name__ == "OpenAICompatibleEmbeddingProvider"
    assert provider.health()["backend"] == "openai-compatible"


def test_embedding_provider_falls_back_to_hashed_when_local_stack_missing_and_cloud_off(tmp_path, monkeypatch):
    monkeypatch.setattr(embedding_provider, "find_spec", _stub_find_spec(False))
    model = tmp_path / "bge-small-zh-v1.5"
    model.mkdir()
    provider = select_embedding_provider(local_model_path=model, dimensions=256)
    assert isinstance(provider, HashedTokenEmbedding)
    assert provider.health()["backend"] == "hashed"


def test_embedding_providers_expose_configuration_and_dimensions(tmp_path, monkeypatch):
    fallback = HashedTokenEmbedding(32)
    assert fallback.configured is True
    assert fallback.dimensions == 32

    monkeypatch.setattr(embedding_provider, "find_spec", _stub_find_spec(True))
    model = tmp_path / "model"
    model.mkdir()
    local = select_embedding_provider(local_model_path=model, dimensions=512)
    assert local.configured is True
    assert local.dimensions is None
