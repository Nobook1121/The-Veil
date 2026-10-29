from io import BytesIO
from pathlib import Path
import time

from trpg_server.agents.vector_store import EmbeddedVectorStore
from trpg_server.app_factory import create_app
from trpg_server.scenario_import_pipeline import ScenarioImportPipeline
from trpg_server.scenario_import_jobs import ImportJobStore


class FakeEmbedding:
    dimensions = 2

    def embed(self, texts):
        return [[float(len(text)), 1.0] for text in texts]


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


def test_import_route_returns_job_ids_and_publishes(tmp_path):
    """POST /api/scripts/import 必须回传 jobId/scriptId，前端据此轮询与发布。

    回归：前端曾按 `id` 读取该响应，导致轮询 `/api/scripts/import/undefined`
    并报「Import job not found」。
    """
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "KNOWLEDGE_BASES_DIR": tmp_path / "kb",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "SCENARIO_IMPORTS_DIR": tmp_path / "imports",
            "VECTOR_BACKEND": "embedded",
            "VECTOR_BACKEND_EXPLICIT": True,
            "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
            "EMBEDDING_PROVIDER": FakeEmbedding(),
        }
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")

    created = client.post(
        "/api/scripts/import",
        data={
            "file": (BytesIO(("# 场景\n" + "A" * 120).encode("utf-8")), "sample.md"),
            "title": "Sample",
            "author": "Alice",
            "creator": "Bob",
            "playerCount": "3",
            "description": "Sample description",
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert created.status_code == 202
    started = created.get_json()["data"]
    assert started["jobId"]
    assert isinstance(started["scriptId"], int)

    job = None
    for _ in range(300):
        polled = client.get(f"/api/scripts/import/{started['jobId']}")
        assert polled.status_code == 200, polled.get_json()
        job = polled.get_json()["data"]
        if job["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job and job["status"] == "done", job

    published = client.post(
        f"/api/scripts/{started['scriptId']}/publish",
        json={"jobId": started["jobId"]},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert published.status_code == 201, published.get_json()
    published_scenario = published.get_json()["data"]
    assert published_scenario["title"] == "Sample"
    # 直接导入的剧本必须沿用 6 位 public_id 编号规则，而不是回退成时间戳。
    public_id = published_scenario["public_id"]
    assert len(public_id) == 6 and public_id.isalnum(), public_id
    # 用户填写的基础信息要真正落到剧本记录上。
    assert published_scenario["author"] == "Alice"
    assert published_scenario["creator_username"] == "Bob"
    assert published_scenario["playerCount"] == 3
    assert published_scenario["notes"] == "Sample description"
    assert published_scenario["createdAt"] and published_scenario["updatedAt"]


def test_editing_imported_basic_info_keeps_version_and_knowledge(tmp_path, monkeypatch):
    """仅修改导入剧本的基础信息时，版本号不变、知识库向量不被重建/清空。"""
    from trpg_server.agents.knowledge_base import KnowledgeChunk, load_knowledge_index, write_knowledge_index
    from trpg_server.routes import scenarios as scenarios_module
    from trpg_server.scenario_store import save_scenario_record

    scenarios_dir = tmp_path / "scenarios"
    # 剧本读写路由使用模块级 SCENARIOS_DIR，需指向临时目录才能读取测试剧本。
    monkeypatch.setattr(scenarios_module, "SCENARIOS_DIR", scenarios_dir)
    scenario_id = 4200000000001
    descriptor = save_scenario_record(scenarios_dir, {
        "id": scenario_id,
        "scenario_version": "1.0.0",
        "title": "Old title",
        "author": "Old author",
        "playerCount": 1,
        "notes": "Old notes",
        "modules": [],
        "import_mode": "direct",
        "owner_id": 1,
        "public_id": "Ab12Cd",
    })
    write_knowledge_index(descriptor, "1.0.0", [KnowledgeChunk(
        scenario_id=str(scenario_id), scenario_version="1.0.0", scene_id=None, card_type="custom",
        visibility="public", spoiler_level=0, unlock_condition=None, text="doc chunk", chunk_id="chunk-0001",
    )])

    app = create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(),
        "CONFIG_DIR": tmp_path / "config",
        "KNOWLEDGE_BASES_DIR": tmp_path / "kb",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": scenarios_dir,
        "SCENARIO_IMPORTS_DIR": tmp_path / "imports",
        "VECTOR_BACKEND": "embedded",
        "VECTOR_BACKEND_EXPLICIT": True,
        "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
        "EMBEDDING_PROVIDER": FakeEmbedding(),
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")

    updated = client.put(f"/api/scenarios/{scenario_id}", json={
        "title": "New title",
        "author": "New author",
        "playerCount": 4,
        "notes": "New notes",
        "cover": "",
        "modules": [],
    }, headers={"X-CSRF-Token": "csrf"})
    assert updated.status_code == 200, updated.get_json()
    data = updated.get_json()["data"]
    assert data["title"] == "New title"
    assert data["playerCount"] == 4
    # 项目已不再使用默认封面图：未提供封面时 cover 必须保持为空，由前端渲染剧本名文字封面。
    assert data["cover"] == ""
    # 基础信息不属于内容，版本号必须保持原样。
    assert data["scenario_version"] == "1.0.0"
    # 已切分的文档知识块必须原样保留，不能被空 modules 覆盖。
    assert len(load_knowledge_index(descriptor, "1.0.0")) == 1


def test_editing_basic_info_keeps_module_scenario_embeddings(tmp_path, monkeypatch):
    """普通剧本只改基础信息时，版本号不变且已有向量不被无向量内容覆盖。"""
    from trpg_server.agents.knowledge_base import load_knowledge_index
    from trpg_server.routes import scenarios as scenarios_module

    scenarios_dir = tmp_path / "scenarios"
    monkeypatch.setattr(scenarios_module, "SCENARIOS_DIR", scenarios_dir)

    app = create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(),
        "CONFIG_DIR": tmp_path / "config",
        "KNOWLEDGE_BASES_DIR": tmp_path / "kb",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": scenarios_dir,
        "SCENARIO_IMPORTS_DIR": tmp_path / "imports",
        "VECTOR_BACKEND": "embedded",
        "VECTOR_BACKEND_EXPLICIT": True,
        "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
        "EMBEDDING_PROVIDER": FakeEmbedding(),
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")

    created = client.post("/api/scenarios", json={
        "title": f"Basic info {tmp_path.name}",
        "author": "A",
        "playerCount": 2,
        "modules": [
            {"id": "scene", "module_type": "scene", "title": "Room", "summary": "room", "content": "secret room"},
            {"id": "end", "module_type": "ending", "title": "End", "summary": "end", "content": "done"},
        ],
    }, headers={"X-CSRF-Token": "csrf"})
    assert created.status_code == 201, created.get_json()
    scenario = created.get_json()["data"]
    scenario_id = scenario["id"]
    descriptor = scenarios_dir / f"scenario-{scenario_id}" / "scenario.json"
    before = load_knowledge_index(descriptor, "1.0.0")
    assert before and all(chunk.embedding for chunk in before)

    updated = client.put(f"/api/scenarios/{scenario_id}", json={
        "title": f"Basic info {tmp_path.name} v2",
        "author": "B",
        "playerCount": 5,
        "notes": "updated notes",
        "cover": "",
        "modules": scenario["modules"],
    }, headers={"X-CSRF-Token": "csrf"})
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["data"]["scenario_version"] == "1.0.0"
    after = load_knowledge_index(descriptor, "1.0.0")
    assert [chunk.text for chunk in after] == [chunk.text for chunk in before]
    assert all(chunk.embedding for chunk in after)


def _make_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "KNOWLEDGE_BASES_DIR": tmp_path / "kb",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "SCENARIO_IMPORTS_DIR": tmp_path / "imports",
            "VECTOR_BACKEND": "embedded",
            "VECTOR_BACKEND_EXPLICIT": True,
            "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
            "EMBEDDING_PROVIDER": FakeEmbedding(),
        }
    )


def test_bundle_import_route_publishes_scenario_and_narrative(tmp_path):
    """外部 Skill 生成的剧本包可以直接上传，发布后图谱与知识索引都要落盘。"""
    from trpg_server.agents.narrative.store import has_narrative
    from trpg_server.scenario_bundle import BUNDLE_FORMAT

    app = _make_app(tmp_path)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")

    bundle = {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "scenario": {"title": "Bundle Sample", "author": "Alice", "playerCount": 2},
        "modules": [
            {"id": "hall", "title": "大厅", "module_type": "scene", "content": "大厅里有一盏灯。"},
            {"id": "finale", "title": "结局", "module_type": "ending", "content": "玩家离开。"},
        ],
        "narrative": {
            "topology": "linear",
            "start": {"location_id": "hall"},
            "nodes": [
                {"id": "hall", "kind": "location", "title": "大厅"},
                {"id": "finale", "kind": "ending", "title": "结局"},
                {"id": "lamp", "kind": "item", "title": "灯"},
            ],
            "edges": [
                {"from": "hall", "to": "finale", "type": "leads_to"},
                {"from": "hall", "to": "lamp", "type": "contains"},
                {"from": "hall", "to": "missing", "type": "adjacent"},
            ],
        },
        "exclusions": [{"prefix": "作者吐槽", "line_start": 3, "category": "author_note", "reason": "与剧情无关"}],
    }
    imported = client.post(
        "/api/scripts/import/bundle",
        json=bundle,
        headers={"X-CSRF-Token": "csrf"},
    )
    assert imported.status_code == 201, imported.get_json()
    data = imported.get_json()["data"]
    assert data["jobId"] and data["scriptId"]
    preview = data["preview"]
    assert preview["import_mode"] == "ai"
    assert preview["import_exclusions"][0]["prefix"] == "作者吐槽"
    assert preview["narrative_summary"]["nodes"] >= 3
    # 端点不存在的边在归一化阶段就被丢弃，不会带进图谱。
    assert preview["narrative_summary"]["edges"] >= 2

    published = client.post(
        f"/api/scripts/{data['scriptId']}/publish",
        json={"jobId": data["jobId"]},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert published.status_code == 201, published.get_json()
    published_scenario = published.get_json()["data"]
    assert published_scenario["title"] == "Bundle Sample"
    assert published_scenario["import_mode"] == "ai"
    assert has_narrative(tmp_path / "scenarios" / f"scenario-{data['scriptId']}")


def test_ai_import_mode_runs_full_chain(tmp_path, monkeypatch):
    """metadata.mode=ai 时整条 AI 链路（剔除→分模块→世界书→图谱）必须产出可发布预览。"""
    from trpg_server import scenario_import_ai

    monkeypatch.setattr(scenario_import_ai, "build_ai_requester", lambda: _fake_ai_requester)

    store = ImportJobStore(tmp_path / "jobs")
    job = store.create(owner_id="1", filename="ai.md", metadata={"title": "AI Sample", "mode": "ai"})
    source = Path(store.root) / job["id"] / "source" / "ai.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "# 大厅\n大厅里有一盏灯。\n\n# 作者吐槽\n这个本真难写。\n\n# 结局\n玩家离开大厅。",
        encoding="utf-8",
    )

    vector_store = EmbeddedVectorStore(tmp_path / "vectors")
    result = ScenarioImportPipeline(
        store,
        {"OCR_ENABLED": False, "EMBEDDING_PROVIDER": FakeEmbedding(), "VECTOR_STORE": vector_store},
    ).run(job["id"])

    assert result["status"] == "done", result
    assert result["preview"]["import_mode"] == "ai"
    assert result["preview"]["import_exclusions"][0]["prefix"] == "作者吐槽"
    assert result["preview"]["narrative_summary"]["nodes"] >= 3
    assert result["preview"]["narrative_summary"]["error_count"] == 0
    assert result["preview"]["conversion"]["ai"]["stages"]["scan"]
    # 图谱与知识块都要作为中间产物保存，发布时不再重新调用 AI。
    assert store.load_intermediate(job["id"], "narrative", {})
    assert store.load_intermediate(job["id"], "knowledge", [])
    assert vector_store.count({"scenario_id": str(job["script_id"])}) >= 1


def _fake_ai_requester(prompt: str) -> str:
    if "无关的文字" in prompt:
        return '{"exclusions":[{"prefix":"作者吐槽","line_start":4,"line_end":5,"category":"author_note","reason":"作者自述"}]}'
    if "划分成可独立检索的模块" in prompt:
        return (
            '{"modules":['
            '{"title":"大厅","module_type":"scene","start_line":1,"end_line":3,"summary":"大厅有一盏灯。"},'
            '{"title":"结局","module_type":"ending","start_line":4,"end_line":5,"summary":"玩家离开。"}]}'
        )
    if "世界书" in prompt:
        return '{"modules":[{"id":"module_001","keywords":["大厅","灯"]},{"id":"module_002","keywords":["结局"]}]}'
    if "生成世界树图谱" in prompt:
        return (
            '{"topology":"linear","start":"module_001",'
            '"nodes":[{"id":"module_001","kind":"location","title":"大厅"},'
            '{"id":"module_002","kind":"ending","title":"结局"},'
            '{"id":"lamp","kind":"item","title":"灯"}],'
            '"edges":[{"from":"module_001","to":"module_002","type":"leads_to"},'
            '{"from":"module_001","to":"lamp","type":"contains"},'
            '{"from":"module_001","to":"ghost","type":"adjacent"}]}'
        )
    return "{}"


def test_direct_import_chunks_by_heading_without_scene_cards(tmp_path):
    store = ImportJobStore(tmp_path / "jobs")
    job = store.create(owner_id="1", filename="large.txt", metadata={"title": "Chunked"})
    source = Path(store.root) / job["id"] / "source" / "large.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Scene A\n" + ("A" * 1200) + "\n\n# Scene B\n" + ("B" * 1200), encoding="utf-8")

    vector_store = EmbeddedVectorStore(tmp_path / "vectors")
    result = ScenarioImportPipeline(
        store,
        {
            "OCR_ENABLED": False,
            "EMBEDDING_PROVIDER": FakeEmbedding(),
            "VECTOR_STORE": vector_store,
        },
    ).run(job["id"])

    assert result["status"] == "done"
    assert result["preview"]["import_mode"] == "direct"
    # 直接导入按标题切块，不再生成可编辑的场景卡。
    assert result["preview"]["modules"] == []
    assert result["stage_meta"]["totalChunks"] == 2
    assert result["stage_meta"]["processedChunks"] == 2
    assert vector_store.count({"scenario_id": str(job["script_id"])}) == 2
