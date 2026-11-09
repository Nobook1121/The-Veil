"""规则集选择入口：开发中占位、后端拒绝与剧本绑定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server import scenario_service
from trpg_server.app_factory import create_app
from trpg_server.ruleset import RulesetSelectionError, builtin_registry


class _ScenarioActor:
    user_id = 11
    username = "owner"

    def has(self, *_args, **_kwargs) -> bool:
        return True


class _FakeSessionUserManager:
    def is_session_current(self, user_id, token) -> bool:
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "owner", "role": "USER"}


def _make_app(tmp_path: Path) -> object:
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOMS_DIR": tmp_path / "rooms",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "WEAPONS_DIR": tmp_path / "weapons",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
            "USER_MANAGER": _FakeSessionUserManager(),
        }
    )


def test_registry_describes_development_placeholder_without_hardcoding():
    items = {item["id"]: item for item in builtin_registry().describe()}
    assert items["coc7"]["state"] == "available"
    assert items["coc7"]["selectable"] is True
    placeholder = items["dnd5e"]
    assert placeholder["state"] == "development"
    assert placeholder["selectable"] is False
    assert placeholder["reason_code"] == "ruleset_in_development"


def test_selecting_development_ruleset_is_rejected_with_reason():
    with pytest.raises(RulesetSelectionError) as exc_info:
        builtin_registry().assert_selectable("dnd5e")
    assert exc_info.value.reason_code == "ruleset_in_development"
    assert "开发中" in exc_info.value.message


def test_rulesets_endpoint_requires_login_and_exposes_development_state(tmp_path: Path):
    """前端只依赖这个接口渲染灰卡，因此接口层也要固定住契约。"""
    app = _make_app(tmp_path)
    client = app.test_client()
    assert client.get("/api/rulesets").status_code == 401

    with client.session_transaction() as sess:
        sess["user_id"] = "7"
        sess["role"] = "USER"
        sess["username"] = "owner"
        sess["session_token"] = "token"
        sess["csrf_token"] = "csrf"
    response = client.get("/api/rulesets")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.get_json()["data"]}
    assert items["coc7"]["selectable"] is True
    assert items["dnd5e"]["state"] == "development"
    assert items["dnd5e"]["selectable"] is False
    assert items["dnd5e"]["reason"]


def test_create_scenario_defaults_to_coc7_and_rejects_development_ruleset(tmp_path: Path):
    app = _make_app(tmp_path)
    with app.app_context():
        created = scenario_service.apply_create(
            _ScenarioActor(),
            {"title": "默认规则剧本", "author": "作者", "scenario_version": "1.0.0"},
        )
        assert created["ruleset_id"] == "coc7"

        with pytest.raises(scenario_service.ScenarioServiceError) as exc_info:
            scenario_service.apply_create(
                _ScenarioActor(),
                {
                    "title": "开发中规则剧本",
                    "author": "作者",
                    "scenario_version": "1.0.0",
                    "ruleset_id": "dnd5e",
                },
            )
        assert exc_info.value.status == 400
        assert exc_info.value.code == "ruleset_in_development"
        assert "开发中" in exc_info.value.message

        updated = scenario_service.apply_update(
            _ScenarioActor(),
            created["id"],
            {"ruleset_id": "coc7"},
        )
        assert updated["ruleset_id"] == "coc7"
