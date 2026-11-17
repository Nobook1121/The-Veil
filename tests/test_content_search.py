"""内容检索：唯一命中 / 多候选 / 无命中，以及插件区分信息。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from trpg_server.agents.tools.content import CONTENT_SEARCH_TOOL
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
if str(PLUGIN_ROOT / "dnd5e") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "dnd5e"))

from dnd5e_plugin.content_search import Dnd5eContentSearch  # noqa: E402
from dnd5e_plugin.http import bp as dnd5e_bp  # noqa: E402


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture()
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture()
def library(plugins):
    return runtime.content_packs_for("dnd5e")


def _room(tmp_path: Path, ruleset: str = "dnd5e") -> Path:
    rooms = tmp_path / "rooms"
    room = rooms / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "检索测试",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": ruleset,
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "艾拉"},
            }
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return rooms


@pytest.fixture()
def client(tmp_path, plugins):
    rooms = _room(tmp_path)
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=str(rooms),
        CONFIG_DIR=str(tmp_path / "config"),
        SCENARIOS_DIR=str(tmp_path / "scenarios"),
        TESTING=True,
    )
    app.register_blueprint(dnd5e_bp)
    return app.test_client(), app


def _login(client) -> None:
    with client.session_transaction() as session:
        session.update(user_id="u1", username="u1", role="USER")


# ---------------------------------------------------------------- 核心通用层


def test_lookup_exact_id_name_and_none(library):
    by_id = library.lookup("dnd5e", "spell", "fireball")
    assert by_id["status"] == "match"
    assert by_id["matches"][0]["id"] == "fireball"
    assert by_id["matches"][0]["pack"]["content_version"]

    by_name = library.lookup("dnd5e", "spell", "Fireball")
    assert by_name["status"] == "match"
    assert by_name["matches"][0]["matched_by"] in {"id", "name"}

    missing = library.lookup("dnd5e", "spell", "完全不存在")
    assert missing["status"] == "none"
    assert missing["matches"] == []


def test_lookup_chinese_and_approximate(library):
    chinese = library.lookup("dnd5e", "spell", "火球术")
    assert chinese["status"] == "match"
    assert chinese["matches"][0]["id"] == "fireball"

    approximate = library.lookup("dnd5e", "spell", "fireb")
    assert approximate["status"] == "match"
    assert approximate["matches"][0]["id"] == "fireball"

    equipment = library.lookup("dnd5e", "equipment", "短棒")
    assert equipment["status"] == "match"
    assert equipment["matches"][0]["id"] == "club"


def test_lookup_ambiguous_returns_candidates(library):
    result = library.lookup("dnd5e", "spell", "治疗")
    assert result["status"] == "ambiguous"
    assert result["total"] == 2
    assert {item["id"] for item in result["matches"]} == {"cure-wounds", "healing-word"}
    for item in result["matches"]:
        assert item["pack"]["edition"]
        assert item["matched_by"] in {"name_prefix", "name_part"}


# ---------------------------------------------------------------- 插件区分信息


def test_provider_adds_distinguishing_fields(plugins):
    provider = Dnd5eContentSearch()

    spell = provider.search("spell", "fireball")
    assert spell["status"] == "match"
    assert spell["matches"][0]["label"] == "Fireball（火球术）"
    assert spell["matches"][0]["distinguish"]["环阶"] == "3 环"
    assert spell["matches"][0]["distinguish"]["学派"] == "塑能"

    monster = provider.search("怪物", "地精")
    assert monster["status"] == "match"
    detail = monster["matches"][0]["distinguish"]
    assert detail["CR"] == "1/4"
    assert detail["体型"] == "小型"

    ambiguous = provider.search("法术", "治疗")
    assert ambiguous["status"] == "ambiguous"
    assert all(item["distinguish"].get("环阶") for item in ambiguous["matches"])


def test_provider_rejects_unknown_type(plugins):
    with pytest.raises(ValueError):
        Dnd5eContentSearch().search("trap", "fireball")


# ---------------------------------------------------------------- HTTP 与 Agent 工具


def test_http_content_search_statuses(client):
    manager, _app = client
    _login(manager)

    unique = manager.get(
        "/api/plugins/dnd5e/rooms/room1/content/search?type=spell&q=火球术"
    )
    assert unique.status_code == 200
    data = unique.get_json()["data"]
    assert data["status"] == "match"
    assert data["matches"][0]["id"] == "fireball"
    assert data["matches"][0]["distinguish"]["环阶"] == "3 环"

    ambiguous = manager.get(
        "/api/plugins/dnd5e/rooms/room1/content/search?type=spell&q=治疗"
    )
    assert ambiguous.get_json()["data"]["status"] == "ambiguous"

    none = manager.get(
        "/api/plugins/dnd5e/rooms/room1/content/search?type=spell&q=完全不存在"
    )
    assert none.get_json()["data"]["status"] == "none"

    unknown = manager.get(
        "/api/plugins/dnd5e/rooms/room1/content/search?type=trap&q=fireball"
    )
    assert unknown.status_code == 400


def test_agent_tool_dispatches_by_ruleset(library):
    context = SimpleNamespace(
        room_info=lambda: {"ruleset_id": "dnd5e"},
        scenarios_dir=None,
    )
    result = CONTENT_SEARCH_TOOL.handler(
        {"type": "spell", "query": "治疗"}, context
    )
    assert result["ok"] is True
    assert result["status"] == "ambiguous"
    assert len(result["matches"]) == 2
    assert "spell" in result["types"]

    types_only = CONTENT_SEARCH_TOOL.handler({}, context)
    assert types_only["ok"] is True
    assert set(types_only["types"]) == {"spell", "monster", "equipment"}

    coc7 = SimpleNamespace(room_info=lambda: {"ruleset_id": "coc7"}, scenarios_dir=None)
    unsupported = CONTENT_SEARCH_TOOL.handler(
        {"type": "spell", "query": "fireball"}, coc7
    )
    assert unsupported["ok"] is False
    assert "没有提供内容检索能力" in unsupported["error"]
