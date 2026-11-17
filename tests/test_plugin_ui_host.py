"""插件 UI 宿主接口：房间声明裁剪与前端资源路由。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.app_factory import create_app
from trpg_server.plugins.registry import reset_plugin_registry


class _FakeManager:
    def is_session_current(self, _user_id, _token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "USER"}


def _write_plugin(root: Path, plugin_id: str, *, rulesets: list[str]) -> None:
    directory = root / plugin_id
    (directory / "ui").mkdir(parents=True, exist_ok=True)
    (directory / "ui" / "stage.js").write_text("window.__stage = true;", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "id": plugin_id,
        "name": plugin_id,
        "version": "1.0.0",
        "api_version": 1,
        "capabilities": {
            "frontend_assets": [{"path": "ui/stage.js", "type": "script"}],
            "ui": {
                "api_version": 1,
                "slots": [
                    {
                        "id": f"{plugin_id}-stage",
                        "slot": "room.stage.primary",
                        "rulesets": rulesets,
                        "priority": 10,
                        "mount": "mountStage",
                        "permissions": ["room.read", "token.read"],
                    }
                ],
            },
        },
    }
    (directory / "plugin.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_room(rooms: Path, room_id: str, ruleset_id: str, *, include_member: bool = True) -> None:
    directory = rooms / room_id
    directory.mkdir(parents=True)
    members = [
        {"user_id": "u1", "username": "alice", "room_role": "owner", "status": "active", "is_active": True},
    ]
    if include_member:
        members.append(
            {"user_id": "u2", "username": "bob", "room_role": "member", "status": "active", "is_active": True}
        )
    info = {
        "id": room_id,
        "name": "测试房间",
        "room_code": room_id.upper(),
        "creator_id": "u1",
        "ruleset_id": ruleset_id,
        "members": members,
    }
    (directory / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_plugins():
    yield
    reset_plugin_registry()


def _app(tmp_path: Path):
    plugins = tmp_path / "plugins"
    # 测试环境只加载内置 coc7；dnd5e 房间用来验证“禁用规则集显式失败”。
    _write_plugin(plugins, "stage-ui", rulesets=["coc7"])
    _write_plugin(plugins, "disabled-ui", rulesets=["coc7"])
    (plugins / "enabled.json").write_text(
        json.dumps({"version": 1, "enabled": ["stage-ui"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    rooms = tmp_path / "rooms"
    _write_room(rooms, "room-dnd", "dnd5e")
    _write_room(rooms, "room-coc", "coc7")
    app = create_app(
        {
            "TESTING": True,
            "PLUGIN_DIR": str(plugins),
            "USER_MANAGER": _FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": str(rooms),
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
            "ROOM_LOG_EXPORT_DIR": tmp_path / "exports",
        }
    )
    return app, plugins


def _login(client, user_id: str = "u2") -> None:
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=user_id, role="USER", session_token="t", csrf_token="c")


def test_room_ui_filters_by_ruleset_and_rejects_disabled(tmp_path):
    app, _plugins = _app(tmp_path)
    client = app.test_client()
    _login(client, "u2")

    response = client.get("/api/rooms/room-coc/ui")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "coc7"
    assert [slot["plugin_id"] for slot in data["slots"]] == ["stage-ui"]
    assert data["slots"][0]["assets"][0]["url"] == "/api/plugins/stage-ui/assets/ui/stage.js"
    assert "token.move.dm" not in data["permissions"]

    # 房间声明 dnd5e，但服务端未启用该规则集：显式 409，不能静默按 coc7 返回槽位。
    disabled = client.get("/api/rooms/room-dnd/ui")
    assert disabled.status_code == 409
    assert "dnd5e" in str(disabled.get_json())

    owner = app.test_client()
    _login(owner, "u1")
    owner_data = owner.get("/api/rooms/room-coc/ui").get_json()["data"]
    assert "token.move.dm" in owner_data["permissions"]

    outsider = app.test_client()
    _login(outsider, "u9")
    assert outsider.get("/api/rooms/room-coc/ui").status_code == 403


def test_plugin_asset_only_serves_declared_files(tmp_path):
    app, _plugins = _app(tmp_path)
    client = app.test_client()
    _login(client)

    asset = client.get("/api/plugins/stage-ui/assets/ui/stage.js")
    assert asset.status_code == 200
    assert b"__stage" in asset.data
    assert asset.headers["X-Content-Type-Options"] == "nosniff"

    assert client.get("/api/plugins/stage-ui/assets/plugin.json").status_code == 404
    assert client.get("/api/plugins/disabled-ui/assets/ui/stage.js").status_code == 404
    assert client.get("/api/plugins/missing/assets/ui/stage.js").status_code == 404

    anonymous = app.test_client()
    assert anonymous.get("/api/plugins/stage-ui/assets/ui/stage.js").status_code == 401
