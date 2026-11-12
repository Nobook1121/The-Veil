"""关于页数据、致谢文件与更新接口的契约。"""

from __future__ import annotations

import json
import platform as platform_module

from trpg_server import app_info, update_service
from trpg_server.app_factory import create_app
from trpg_server.app_info import build_app_info, load_credits
from trpg_server.version import APP_VERSION


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(role),
        "CONFIG_DIR": tmp_path / "config",
        "KNOWLEDGE_BASES_DIR": tmp_path / "knowledge-bases",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        # 更新状态与构建清单都写到临时目录，避免污染仓库 data/ 与真实 runtime。
        "UPDATE_DIR": tmp_path / "update",
        "RUNTIME_DIR": tmp_path / "runtime",
    })


def _login(client, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role=role, session_token="token", csrf_token="csrf")


def _use_credits_file(monkeypatch, path):
    monkeypatch.setattr(app_info, "_CREDITS_PATH", path)
    monkeypatch.setattr(app_info, "_credits_cache", None)


def test_compare_versions_orders_releases():
    assert update_service.compare_versions("1.0.0", "0.9.9") == 1
    assert update_service.compare_versions("v1.0.0", "1.0.0") == 0
    assert update_service.compare_versions("1.0.0", "1.0.1") == -1
    assert update_service.compare_versions("1.0.0-beta.1", "1.0.0") == -1
    assert update_service.compare_versions("1.0.0", "1.0.0-beta.1") == 1


def test_normalize_repo_accepts_url_and_slug():
    assert update_service.normalize_repo("Nobook1121/The-Veil") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("https://github.com/Nobook1121/The-Veil.git") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("https://github.com/Nobook1121/The-Veil/releases") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("") == ""
    assert update_service.normalize_repo("not-a-repo") == ""


def test_build_app_info_has_stable_shape():
    info = build_app_info()
    assert info["name"] == "帷幕 The Veil"
    assert info["version"] == APP_VERSION
    assert info["build"]["mode"] in {"dev", "release"}
    assert info["license"]["spdx"] == "Apache-2.0"
    assert info["repo"]["github"] == "Nobook1121/The-Veil"
    assert isinstance(info["runtime"]["python"], str)
    assert info["runtime"]["platform"] == f"{platform_module.system()}-{platform_module.release()}"
    assert info["runtime"]["system"] == platform_module.system()

    credits = info["credits"]
    assert credits["schema_version"] == 1
    assert isinstance(credits["updated_at"], str)
    for key in ("contributors", "special_thanks", "dependencies", "sponsor_channels", "sponsors"):
        assert isinstance(credits[key], list)


def test_shipped_credits_file_is_release_ready():
    credits = load_credits()
    assert credits["schema_version"] == 1
    assert credits["updated_at"]
    assert credits["contributors"], "至少保留项目发起者"

    dependencies = credits["dependencies"]
    assert 8 <= len(dependencies) <= 12
    for dependency in dependencies:
        # 每条都必须能支撑「关于页 → 开源许可」列表的展示与跳转。
        assert dependency["name"]
        assert dependency["version"]
        assert dependency["license"]
        assert dependency["url"].startswith("https://")
        assert dependency["usage"]

    for contributor in credits["contributors"]:
        assert contributor["name"]
        assert contributor["role"]

    for channel in credits["sponsor_channels"]:
        assert channel["id"] and channel["label"] and channel["url"]

    # 计划 §10：占位文本绝不能进入 release 包。
    raw = app_info._CREDITS_PATH.read_text(encoding="utf-8")
    assert "CHANGE_ME" not in raw


def test_load_credits_missing_file_degrades(tmp_path, monkeypatch):
    _use_credits_file(monkeypatch, tmp_path / "missing" / "credits.json")
    credits = load_credits()
    assert credits["schema_version"] == 1
    assert credits["updated_at"] == ""
    for key in ("contributors", "special_thanks", "dependencies", "sponsor_channels", "sponsors"):
        assert credits[key] == []
    # build_app_info 也不能因为 credits 缺失而抛异常。
    assert build_app_info()["credits"] == credits


def test_load_credits_corrupt_file_degrades(tmp_path, monkeypatch):
    path = tmp_path / "credits.json"
    path.write_text("{ this is not json", encoding="utf-8")
    _use_credits_file(monkeypatch, path)
    credits = load_credits()
    assert credits["schema_version"] == 1
    assert credits["dependencies"] == []
    assert credits["contributors"] == []


def test_load_credits_normalizes_legacy_and_filters_placeholders(tmp_path, monkeypatch):
    path = tmp_path / "credits.json"
    path.write_text(
        json.dumps(
            {
                "contributors": [{"name": "旧贡献者", "role": "test"}],
                "dependencies": [{"name": "Flask", "license": "BSD-3-Clause"}],
                "sponsor_channels": [
                    {"id": "afdian", "label": "爱发电", "url": "https://afdian.com/a/veil", "note": "赞助"},
                    {"id": "kofi", "label": "Ko-fi", "note": "缺少 url，应被丢弃"},
                    {"id": "bad", "label": "占位", "url": "https://example.test/CHANGE_ME", "note": ""},
                ],
                "sponsors": [{"name": "某人", "tier": "bronze"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _use_credits_file(monkeypatch, path)
    credits = load_credits()

    # 旧文件没有 schema_version/updated_at 或条目字段不齐时补默认值而不是报错。
    assert credits["schema_version"] == 1
    assert credits["updated_at"] == ""
    assert credits["contributors"][0]["note"] == ""
    assert credits["contributors"][0]["url"] == ""
    assert credits["dependencies"][0]["version"] == ""
    assert credits["dependencies"][0]["usage"] == ""
    assert credits["sponsors"][0]["since"] == ""
    assert [channel["id"] for channel in credits["sponsor_channels"]] == ["afdian"]


def test_get_app_info_route_returns_full_payload(tmp_path):
    client = _app(tmp_path).test_client()
    response = client.get("/api/app/info")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True

    data = payload["data"]
    assert data["name"] == "帷幕 The Veil"
    assert data["version"] == APP_VERSION
    assert set(data["build"]) >= {"mode", "sha", "built_at", "channel"}
    assert data["license"]["spdx"] == "Apache-2.0"
    assert data["repo"]["github"] == "Nobook1121/The-Veil"
    assert data["repo"]["third_party"].endswith("THIRD-PARTY-LICENSES.md")
    assert set(data["runtime"]) == {"python", "platform", "system"}
    assert data["credits"]["schema_version"] == 1


def test_get_app_info_is_public_without_session(tmp_path):
    client = _app(tmp_path).test_client()
    # 关于页是登录前也能看的公开接口，只允许暴露系统名等非敏感信息。
    assert client.get("/api/app/info").status_code == 200


def test_update_state_is_public_and_has_public_shape(tmp_path):
    client = _app(tmp_path).test_client()
    response = client.get("/api/app/update")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert set(data) >= {"state", "current", "enabled", "progress"}
    assert data["current"] == APP_VERSION
    # GET 只读上一次检查的落盘状态（从未检查过是 unknown），不联网；
    # TESTING/conftest 的 DISABLE=1 只体现在 enabled=False。
    assert data["state"] == "unknown"
    assert data["enabled"] is False


def test_update_check_requires_permission(tmp_path):
    anonymous = _app(tmp_path).test_client()
    unauthenticated = anonymous.post("/api/app/update/check")
    assert unauthenticated.status_code == 401
    assert unauthenticated.get_json()["success"] is False

    user_client = _app(tmp_path, "USER").test_client()
    _login(user_client, "USER")
    forbidden = user_client.post("/api/app/update/check", headers={"X-CSRF-Token": "csrf"})
    assert forbidden.status_code == 403
    assert forbidden.get_json()["success"] is False

    admin_client = _app(tmp_path, "ADMIN").test_client()
    _login(admin_client, "ADMIN")
    allowed = admin_client.post("/api/app/update/check", headers={"X-CSRF-Token": "csrf"})
    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["state"] == "disabled"


def test_update_download_requires_permission(tmp_path):
    user_client = _app(tmp_path, "USER").test_client()
    _login(user_client, "USER")
    response = user_client.post("/api/app/update/download", headers={"X-CSRF-Token": "csrf"})
    assert response.status_code == 403
