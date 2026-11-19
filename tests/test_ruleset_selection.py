"""规则集选择入口：开发中占位、后端拒绝与剧本绑定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server import scenario_service
from trpg_server.app_factory import create_app
from trpg_server.ruleset import RulesetSelectionError, builtin_registry, reset_builtin_registry
from trpg_server.ruleset.registry import DeclaredRuleset, RulesetRegistry

PLACEHOLDER = DeclaredRuleset(id="coming-soon", display_name="Coming Soon")


@pytest.fixture
def placeholder():
    from trpg_server.ruleset import bootstrap

    original = bootstrap.DEFAULT_DECLARED_RULESETS
    bootstrap.DEFAULT_DECLARED_RULESETS = (PLACEHOLDER,)
    reset_builtin_registry()
    try:
        yield PLACEHOLDER
    finally:
        bootstrap.DEFAULT_DECLARED_RULESETS = original
        reset_builtin_registry()


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
    registry = RulesetRegistry([builtin_registry().resolve("coc7")], declared=[PLACEHOLDER])
    items = {item["id"]: item for item in registry.describe()}
    assert items["coc7"]["state"] == "available"
    assert items["coc7"]["selectable"] is True
    placeholder = items["coming-soon"]
    assert placeholder["state"] == "development"
    assert placeholder["selectable"] is False
    assert placeholder["reason_code"] == "ruleset_in_development"


def test_selecting_development_ruleset_is_rejected_with_reason():
    registry = RulesetRegistry([builtin_registry().resolve("coc7")], declared=[PLACEHOLDER])
    with pytest.raises(RulesetSelectionError) as exc_info:
        registry.assert_selectable("coming-soon")
    assert exc_info.value.reason_code == "ruleset_in_development"
    assert "开发中" in exc_info.value.message


def test_rulesets_endpoint_requires_login_and_exposes_development_state(tmp_path: Path, placeholder):
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
    assert items[placeholder.id]["state"] == "development"
    assert items[placeholder.id]["selectable"] is False
    assert items[placeholder.id]["reason"]


def test_create_scenario_defaults_to_coc7_and_rejects_development_ruleset(tmp_path: Path, placeholder):
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
                    "ruleset_id": placeholder.id,
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


def _login(client) -> None:
    with client.session_transaction() as sess:
        sess.update(
            user_id=1,
            username="tester",
            role="ADMIN",
            session_token="token",
            csrf_token="csrf",
        )


def test_scenario_import_accepts_explicit_ruleset(tmp_path: Path):
    client = _make_app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/import",
        json={"text": "# 场景\n洞穴深处传来低吼。", "title": "5e 剧本", "ruleset_id": "dnd5e"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["ruleset_id"] == "dnd5e"

    # 未指定规则集时保持旧输出：不写 ruleset_id，由创建接口补默认值（coc7）。
    default_response = client.post(
        "/api/scenarios/import",
        json={"text": "# 场景\n洞穴深处传来低吼。", "title": "默认规则剧本"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert default_response.status_code == 200, default_response.get_json()
    assert "ruleset_id" not in default_response.get_json()["data"]


def test_scenario_import_rejects_unknown_ruleset(tmp_path: Path):
    client = _make_app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/import",
        json={"text": "# 场景\n洞穴深处传来低吼。", "ruleset_id": "not-a-ruleset"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "ruleset_unknown"
    assert "not-a-ruleset" in body["message"]
