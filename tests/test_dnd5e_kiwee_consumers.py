"""kiwee 深库三方消费面（**全部离线**）：知识工具 / UI 端点 / 结构化数值。

fixture 建法沿用 ``tests/test_dnd5e_kiwee.py``：用
``tests/fixtures/dnd5e_kiwee/raw/`` 下的手写 5etools 条目建 tiny sqlite，
再把服务挂进插件服务表，模拟真实运行期（服务缺席 → 回落 dnd5e-srd）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e"
FIXTURE_RAW = REPO_ROOT / "tests" / "fixtures" / "dnd5e_kiwee" / "raw"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from dnd5e_plugin import kiwee  # noqa: E402
from dnd5e_plugin import http as dnd5e_http  # noqa: E402
from dnd5e_plugin.kiwee import db as kiwee_db  # noqa: E402
from trpg_server.agents import ruleset_knowledge  # noqa: E402
from trpg_server.agents.tools.knowledge import search_ruleset_knowledge  # noqa: E402
from trpg_server.plugins import runtime as plugin_runtime  # noqa: E402

USAGE_KEYS = {
    "ruleset_chunks",
    "ruleset_sources",
    "ruleset_ids",
    "knowledge_versions",
    "topics",
    "citations",
}


# ---------------------------------------------------------------- fixture 库


@pytest.fixture(scope="module")
def fixture_db_path(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("kiwee-consumers") / "kiwee.sqlite3"
    stats = kiwee_db.build_kiwee_db(FIXTURE_RAW, db_path)
    assert stats["status"] == "ok"
    return db_path


@pytest.fixture()
def kiwee_service(fixture_db_path):
    """把 fixture 深库挂进插件服务表（teardown 摘掉，避免污染其它用例）。"""
    service = kiwee.KiweeDbService(db_path=fixture_db_path)
    plugin_runtime.register_service("dnd5e", kiwee.SERVICE_NAME, service)
    try:
        yield service
    finally:
        plugin_runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)


@pytest.fixture()
def missing_service(tmp_path):
    """服务在册但库文件缺席（深库未建的真实形态）。"""
    service = kiwee.KiweeDbService(db_path=tmp_path / "no-such-kiwee.sqlite3")
    plugin_runtime.register_service("dnd5e", kiwee.SERVICE_NAME, service)
    try:
        yield service
    finally:
        plugin_runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)


# ---------------------------------------------------------------- 检索桩


class _StubAdapter:
    """最小适配器：模拟 dnd5e-srd 内容包的 builtin_search。"""

    def __init__(self, results: list[dict] | None = None) -> None:
        self._results = results if results is not None else [
            {
                "ruleset_id": "dnd5e",
                "knowledge_version": "srd-5.1",
                "chunk_id": "srd:spell:fireball",
                "topic": "spell",
                "title": "Fireball",
                "citation": "SRD 5.1 / Fireball",
                "text": "8d6 fire damage, DC 15 Dexterity save.",
                "source": {"document": "SRD 5.1", "attribution": "Wizards of the Coast"},
                "license": "CC-BY-4.0",
                "license_url": "https://example.test/license",
            }
        ]

    def builtin_search(self, query: str, *, limit: int = 3) -> list[dict]:
        return list(self._results)


class _Registry:
    def __init__(self, adapter) -> None:
        self._adapter = adapter

    def get(self, ruleset_id: str):
        if self._adapter is None:
            raise ValueError(f"unknown ruleset {ruleset_id}")
        return self._adapter


class _StubStore:
    """``search_ruleset`` 的最小鸭子类型替身（房间索引那一层）。"""

    rooms_dir = None

    def __init__(self, stored: list[dict] | None = None, adapter=None) -> None:
        self._stored = stored or []
        self.registry = _Registry(adapter)

    def search(self, ruleset_id, query, version=None, top_k=3):
        return list(self._stored)


class _Context:
    def __init__(self, tool_state: dict) -> None:
        self.tool_state = tool_state


def _tool_state(store: _StubStore, *, default_dm: bool = True) -> dict:
    """把 search_ruleset 包成工具认的回调（接受 observer_is_dm）。"""

    def _search(query, top_k, **options):
        return ruleset_knowledge.search_ruleset(
            "room-1",
            query,
            store=store,
            ruleset_id="dnd5e",
            top_k=top_k,
            observer_is_dm=bool(options.get("observer_is_dm", default_dm)),
        )

    return {"ruleset_search": _search}


# ---------------------------------------------------------------- 知识工具


def test_search_ruleset_prefers_deep_layer(kiwee_service):
    """深库优先：命中即排在最前，带正文/深链/版本，再用既有结果补位。"""
    store = _StubStore(adapter=_StubAdapter())
    results = ruleset_knowledge.search_ruleset(
        "room-1", "火球术", store=store, ruleset_id="dnd5e", top_k=3
    )
    assert results[0]["deep_layer"] == "kiwee"
    assert results[0]["ruleset_id"] == "dnd5e"
    assert "8d6" in results[0]["text"]
    assert results[0]["deep_link"] == "https://5e.kiwee.top/spells.html#phb_火球术"
    assert results[0]["rules_version"] == "2014"
    assert results[0]["translator_ok"] is True
    assert results[0]["translator_note"] == ""
    assert results[0]["license"] == "CC BY-NC-SA 4.0"
    # 深库只给 2 条（PHB/XPHB 火球术）时，第 3 条由既有 SRD 路径补位。
    assert any(item.get("chunk_id") == "srd:spell:fireball" for item in results)


def test_search_ruleset_marks_machine_translation(kiwee_service):
    store = _StubStore(adapter=_StubAdapter())
    results = ruleset_knowledge.search_ruleset(
        "room-1", "闪电束", store=store, ruleset_id="dnd5e", top_k=3
    )
    lightning = next(item for item in results if item.get("name") == "闪电束")
    assert lightning["is_machine_translated"] is True
    assert lightning["translator_note"] == "机翻，未校对"


def test_search_ruleset_zero_regression_without_service():
    """服务缺席：结果与改动前逐位一致（纯既有路径，无深库字段）。"""
    plugin_runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)
    stored = [
        {
            "ruleset_id": "dnd5e",
            "knowledge_version": "3",
            "chunk_id": "stored-1",
            "topic": "core_rules",
            "citation": "房间索引 / 死亡",
            "text": "死亡豁免规则。",
        }
    ]
    store = _StubStore(stored=stored, adapter=_StubAdapter())
    results = ruleset_knowledge.search_ruleset(
        "room-1", "火球术", store=store, ruleset_id="dnd5e", top_k=3
    )
    assert [item["chunk_id"] for item in results] == ["srd:spell:fireball", "stored-1"]
    assert all("deep_layer" not in item for item in results)


def test_search_ruleset_falls_back_when_deep_layer_has_no_hit(kiwee_service):
    """深库建好但没命中：仍走既有路径（不是空结果）。"""
    store = _StubStore(adapter=_StubAdapter())
    results = ruleset_knowledge.search_ruleset(
        "room-1", "完全查不到的词xyz", store=store, ruleset_id="dnd5e", top_k=3
    )
    assert [item["chunk_id"] for item in results] == ["srd:spell:fireball"]


def test_search_ruleset_falls_back_when_database_missing(missing_service):
    store = _StubStore(adapter=_StubAdapter())
    results = ruleset_knowledge.search_ruleset(
        "room-1", "火球术", store=store, ruleset_id="dnd5e", top_k=3
    )
    assert [item["chunk_id"] for item in results] == ["srd:spell:fireball"]


def test_search_ruleset_player_observer_hides_bestiary(kiwee_service):
    store = _StubStore(stored=[], adapter=None)
    dm_view = ruleset_knowledge.search_ruleset(
        "room-1", "地精", store=store, ruleset_id="dnd5e", top_k=3, observer_is_dm=True
    )
    assert any(item.get("category") == "monster" for item in dm_view)
    player_view = ruleset_knowledge.search_ruleset(
        "room-1", "地精", store=store, ruleset_id="dnd5e", top_k=3, observer_is_dm=False
    )
    assert player_view == []


def test_knowledge_tool_forwards_deep_fields(kiwee_service):
    store = _StubStore(adapter=_StubAdapter())
    result = search_ruleset_knowledge({"query": "火球术"}, _Context(_tool_state(store)))
    reference = result["references"][0]
    assert reference["deep_layer"] == "kiwee"
    assert reference["deep_link"].endswith("#phb_火球术")
    assert reference["rules_version"] == "2014"
    assert reference["category"] == "spell"
    assert reference["is_machine_translated"] is False
    assert reference["text"] and len(reference["text"]) <= 1200
    assert reference["license"] == "CC BY-NC-SA 4.0"
    # 返回 schema 向后兼容：knowledge_usage 的键集合一个不少也一个不多。
    assert set(result["knowledge_usage"]) == USAGE_KEYS


def test_knowledge_tool_tolerates_two_arg_search():
    """老的两参数 ruleset_search 回调照旧可用（不因新参数炸掉）。"""
    context = _Context(
        {
            "ruleset_search": lambda query, top_k: [
                {
                    "ruleset_id": "coc7",
                    "knowledge_version": "4",
                    "chunk_id": "chunk-1",
                    "topic": "keeper_guidance",
                    "citation": "Keeper Rulebook p.1",
                    "text": "死亡场景应尊重玩家体验。",
                }
            ]
        }
    )
    result = search_ruleset_knowledge({"query": "死亡"}, context)
    assert result["used"] is True
    assert result["references"][0]["chunk_id"] == "chunk-1"
    assert "deep_layer" not in result["references"][0]


def test_knowledge_tool_player_agent_gets_no_bestiary(kiwee_service):
    """AI 玩家角色（tool_state.ai_player_turn）是玩家视角：拿不到 bestiary。"""
    store = _StubStore(stored=[], adapter=None)
    received: dict = {}

    def _search(query, top_k, **options):
        received.update(options)
        return ruleset_knowledge.search_ruleset(
            "room-1",
            query,
            store=store,
            ruleset_id="dnd5e",
            top_k=top_k,
            observer_is_dm=bool(options.get("observer_is_dm", True)),
        )

    tool_state = {"ruleset_search": _search, "ai_player_turn": True}
    result = search_ruleset_knowledge({"query": "地精"}, _Context(tool_state))
    assert received.get("observer_is_dm") is False
    assert result["used"] is False


# ---------------------------------------------------------------- 结构化数值


def test_spell_mechanics_fireball_anchors(kiwee_service):
    """验收锚点：火球术 8d6 / 敏捷豁免 / 半径 20 尺球状。"""
    fireball = kiwee_service.spell_mechanics("火球术|phb")
    assert fireball["name"] == "火球术"
    assert fireball["eng_name"] == "Fireball"
    assert fireball["level"] == 3
    assert fireball["school"] == "塑能"
    assert fireball["casting_time"] == "1 动作"
    assert fireball["range_ft"] == 150
    assert fireball["components"].startswith("V、S、M")
    assert fireball["duration"] == "立即"
    assert fireball["save_ability"] == "dex"
    assert fireball["save_ability_zh"] == "敏捷"
    assert fireball["damage"] == [{"dice": "8d6", "type": "fire", "type_zh": "火焰"}]
    assert fireball["scaling"]["per_level"] == "1d6"
    assert fireball["scaling"]["slot_range"] == "3-9"
    assert fireball["area"]["shape"] == "sphere"
    assert fireball["area"]["size_ft"] == 20
    assert fireball["deep_link"] == "https://5e.kiwee.top/spells.html#phb_火球术"


def test_spell_mechanics_accepts_name_and_reports_era(kiwee_service):
    by_name = kiwee_service.spell_mechanics("火球术")
    assert by_name["damage"][0]["dice"] == "8d6"
    assert by_name["rules_era"] == "2014"
    by_2024 = kiwee_service.spell_mechanics("火球术|xphb")
    assert by_2024["uid"] == "火球术|xphb"
    assert by_2024["rules_era"] == "2024"


def test_monster_mechanics_goblin_and_missing(kiwee_service):
    goblin = kiwee_service.monster_mechanics("地精|mm")
    assert goblin["name"] == "地精"
    assert goblin["ac"] == 15
    assert goblin["ac_from"] == ["皮甲"]
    assert goblin["hp"] == {"average": 7, "formula": "2d6"}
    assert goblin["cr"] == "1/4"
    assert goblin["cr_numeric"] == 0.25
    assert goblin["abilities"]["dex"] == 14
    assert goblin["alignment"] == "中立邪恶"
    assert "近战武器攻击" in goblin["text"]
    assert kiwee_service.monster_mechanics("不存在的怪物") is None


def test_mechanics_degrade_without_database(missing_service):
    assert missing_service.spell_mechanics("火球术") is None
    assert missing_service.monster_mechanics("地精") is None
    assert missing_service.entry("spell", "火球术|phb") is None


def test_entry_unknown_category_raises(kiwee_service):
    with pytest.raises(ValueError):
        kiwee_service.entry("不存在类目", "x")


# ---------------------------------------------------------------- UI 端点


def _write_room(rooms: Path, room_id: str, ruleset_id: str, members: list[tuple[str, str]]) -> None:
    room_dir = rooms / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": f"{room_id} 房间",
        "room_code": room_id.upper(),
        "ruleset_id": ruleset_id,
        "creator_id": members[0][0] if members else "",
        "members": [
            {
                "user_id": user_id,
                "username": user_id,
                "room_role": room_role,
                "status": "active",
                "is_active": True,
            }
            for user_id, room_role in members
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def client(tmp_path, kiwee_service):
    rooms = tmp_path / "rooms"
    # dnd5e 房间：u-owner 房主、u-player 普通成员。
    _write_room(rooms, "room-5e", "dnd5e", [("u-owner", "owner"), ("u-player", "member")])
    # 非 dnd5e 房间：房主身份不该换来 dnd5e 的 DM 权限。
    _write_room(rooms, "room-coc", "coc7", [("u-coc-owner", "owner")])
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=str(rooms),
        CONFIG_DIR=str(tmp_path / "config"),
        SCENARIOS_DIR=str(tmp_path / "scenarios"),
        TESTING=True,
    )
    app.register_blueprint(dnd5e_http.bp)
    return app.test_client()


def _login(client, user_id: str = "u-player", role: str = "USER") -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=user_id, role=role)


def _data(response) -> dict:
    return response.get_json()["data"]


def test_ui_search_requires_login(client):
    response = client.get("/api/plugins/dnd5e/rules/search?q=火球术")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Not logged in"


def test_ui_search_hits_spell_and_never_exposes_dm_categories(client):
    _login(client)
    response = client.get("/api/plugins/dnd5e/rules/search", query_string={"q": "火球术"})
    assert response.status_code == 200
    data = _data(response)
    assert data["available"] is True
    assert data["total"] >= 1
    fireball = data["results"][0]
    assert fireball["category"] == "spell"
    assert fireball["deep_link"].endswith("#phb_火球术")
    assert "8d6" in fireball["text"]
    assert fireball["rules_version"] == "2014"
    assert fireball["translator_note"] == ""
    assert "raw" not in fireball
    # 类目目录只给玩家侧；DM 侧类目连搜都搜不到。
    assert {item["id"] for item in data["categories"]}.isdisjoint(kiwee.DM_ONLY_CATEGORIES)
    monster = client.get("/api/plugins/dnd5e/rules/search", query_string={"q": "地精"})
    assert _data(monster)["results"] == []


def test_ui_search_category_and_version_filters(client):
    _login(client)
    response = client.get(
        "/api/plugins/dnd5e/rules/search",
        query_string={"q": "火球术", "category": "法术", "rules_version": "2024"},
    )
    data = _data(response)
    assert [item["uid"] for item in data["results"]] == ["火球术|xphb"]
    assert data["results"][0]["rules_version"] == "2024"


def test_ui_search_invalid_category_returns_400(client):
    _login(client)
    response = client.get(
        "/api/plugins/dnd5e/rules/search", query_string={"q": "火球术", "category": "乱写"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "dnd5e.invalid_rules_query"


def test_ui_entry_spell_is_open_to_players(client):
    _login(client)
    response = client.get("/api/plugins/dnd5e/rules/entry/spell/火球术|phb")
    assert response.status_code == 200
    data = _data(response)
    assert data["available"] is True
    assert data["name"] == "火球术"
    assert data["deep_link"] == "https://5e.kiwee.top/spells.html#phb_火球术"
    assert "8d6" in data["text"]
    assert data["structured"]["damage"][0]["dice"] == "8d6"
    assert data["structured"]["area"] == {"shape": "sphere", "size_ft": 20, "damage_types": ["fire"]}
    assert data["raw"]["level"] == 3
    assert data["raw_truncated"] is False
    assert data["license"] == "CC BY-NC-SA 4.0"


def test_ui_entry_bestiary_gated_for_plain_player(client):
    _login(client)
    response = client.get("/api/plugins/dnd5e/rules/entry/monster/地精|mm")
    assert response.status_code == 403
    assert response.get_json()["error"] == "dnd5e.dm_only"
    assert "地精" not in response.get_data(as_text=True)


def test_ui_entry_bestiary_allowed_for_room_owner(client):
    """普通 USER 角色，但它是某个 dnd5e 房间的房主 → 放行。"""
    _login(client, user_id="u-owner")
    response = client.get("/api/plugins/dnd5e/rules/entry/monster/地精|mm")
    assert response.status_code == 200
    data = _data(response)
    assert data["structured"]["ac"] == 15
    assert data["structured"]["cr"] == "1/4"
    assert "近战武器攻击" in data["text"]


def test_ui_entry_bestiary_allowed_for_site_admin(client):
    _login(client, user_id="u-admin", role="ADMIN")
    response = client.get("/api/plugins/dnd5e/rules/entry/monster/地精|mm")
    assert response.status_code == 200
    assert _data(response)["name"] == "地精"


def test_ui_entry_bestiary_denied_for_non_dnd5e_room_owner(client):
    """只有 coc7 房间房主身份不算 dnd5e 的 DM。"""
    _login(client, user_id="u-coc-owner")
    response = client.get("/api/plugins/dnd5e/rules/entry/monster/地精|mm")
    assert response.status_code == 403
    assert response.get_json()["error"] == "dnd5e.dm_only"


def test_ui_entry_not_found_and_bad_category(client):
    _login(client)
    missing = client.get("/api/plugins/dnd5e/rules/entry/spell/不存在的法术")
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "dnd5e.rules_entry_not_found"
    bad = client.get("/api/plugins/dnd5e/rules/entry/乱写/whatever")
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "dnd5e.invalid_rules_category"


def test_ui_entry_raw_is_truncated_and_flagged(client, monkeypatch):
    """raw 超限只给截断预览，并显式标注（64KB 口径可配）。"""
    monkeypatch.setattr(dnd5e_http, "RAW_LIMIT_BYTES", 200)
    _login(client)
    response = client.get("/api/plugins/dnd5e/rules/entry/spell/火球术|phb")
    data = _data(response)
    assert data["raw_truncated"] is True
    assert data["raw"] is None
    assert data["raw_bytes"] > 200
    assert len(data["raw_preview"].encode("utf-8")) <= 200


def test_ui_status_reports_library_stats(client):
    _login(client)
    response = client.get("/api/plugins/dnd5e/rules/status")
    assert response.status_code == 200
    data = _data(response)
    assert data["available"] is True
    assert data["entries"] > 0
    assert data["categories"]["spell"] > 0
    assert data["dm_only_categories"] == sorted(kiwee.DM_ONLY_CATEGORIES)


def test_ui_endpoints_degrade_when_database_missing(client, missing_service):
    """深库未建：查询端点 200 + available:false（不是 500）。"""
    _login(client)
    search = client.get("/api/plugins/dnd5e/rules/search", query_string={"q": "火球术"})
    assert search.status_code == 200
    search_data = _data(search)
    assert search_data["available"] is False
    assert search_data["reason"] == "深库未建"
    assert "snapshot" in search_data["hint"]
    assert search_data["results"] == []

    entry = client.get("/api/plugins/dnd5e/rules/entry/spell/火球术|phb")
    assert entry.status_code == 200
    assert _data(entry)["available"] is False

    # 空查询（UI 首屏）也要如实说不可用。
    blank = client.get("/api/plugins/dnd5e/rules/search")
    assert blank.status_code == 200
    assert _data(blank)["available"] is False

    status = client.get("/api/plugins/dnd5e/rules/status")
    assert status.status_code == 200
    assert _data(status)["available"] is False


def test_ui_endpoints_degrade_without_service(client):
    """插件服务缺席（比如插件未装）：同样降级，不 500。"""
    plugin_runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)
    _login(client)
    for url in (
        "/api/plugins/dnd5e/rules/search?q=火球术",
        "/api/plugins/dnd5e/rules/entry/spell/火球术|phb",
        "/api/plugins/dnd5e/rules/status",
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert _data(response)["available"] is False, url
