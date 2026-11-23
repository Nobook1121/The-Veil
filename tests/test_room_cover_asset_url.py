"""房间卡封面 URL 解析：剧本 cover 已是完整公开 URL 时不得重复拼接前缀。

回归背景：fvtt 导入器把 ``cover`` 写成完整公开 URL
（``/assets/scenarios/scenario-x/assets/y.png``），房间卡封面若再次加前缀会得到
``/assets/scenarios/assets/scenarios/...`` → 404 → 前端显示破图图标。
"""

import json

from trpg_server.app_factory import create_app
from trpg_server.room_ops.views import _room_cover, _room_summary
from trpg_server.scenario_store import scenario_public_asset_url

ABSOLUTE_COVER = "/assets/scenarios/scenario-1/assets/cover.png"


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": f"user{user_id}", "role": "ADMIN"}


def _make_app(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": rooms_dir,
            "SCENARIOS_DIR": scenarios_dir,
            "CHARACTERS_DIR": tmp_path / "characters",
            "HISTORY_DIR": tmp_path / "history",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        }
    )
    return app, scenarios_dir, rooms_dir


def _write_scenario(scenarios_dir, scenario_id="1", cover=""):
    payload = {
        "id": int(scenario_id),
        "title": "封面剧本",
        "scenario_version": "1.0.0",
        "cover": cover,
        "modules": [{"id": "s1", "module_type": "scene", "title": "开场"}],
    }
    (scenarios_dir / f"{scenario_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_room(rooms_dir, room_id="r-1", scenario_id="1"):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "封面房间",
        "room_code": f"CODE-{room_id}",
        "creator_id": 1,
        "scenario_id": scenario_id,
        "scenario_title": "封面剧本",
        "members": [
            {
                "user_id": 1,
                "username": "user1",
                "role": "ADMIN",
                "status": "active",
                "is_active": True,
                "room_role": "owner",
            }
        ],
    }
    (room_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    return info


def _client(app, user_id=1):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=f"user{user_id}",
            role="ADMIN",
            session_token="token",
            csrf_token="csrf",
        )
    return client


# --- scenario_public_asset_url：幂等单元测试 ---


def test_relative_asset_path_gets_public_prefix():
    assert (
        scenario_public_asset_url("scenario-1/assets/cover.png")
        == "/assets/scenarios/scenario-1/assets/cover.png"
    )


def test_root_relative_asset_path_is_treated_as_url():
    """以 / 开头即视为根路径地址，原样返回（不再包一层 /assets/scenarios）。"""
    assert (
        scenario_public_asset_url("/scenario-1/assets/cover.png")
        == "/scenario-1/assets/cover.png"
    )


def test_root_path_asset_url_is_returned_unchanged():
    """已是根路径的公开 URL 原样返回，避免出现双重前缀。"""
    assert scenario_public_asset_url(ABSOLUTE_COVER) == ABSOLUTE_COVER


def test_absolute_http_asset_url_is_returned_unchanged():
    for url in (
        "https://cdn.example.com/cover.png",
        "http://cdn.example.com/cover.png",
        "//cdn.example.com/cover.png",
    ):
        assert scenario_public_asset_url(url) == url


def test_empty_asset_path_yields_empty_url():
    """空封面不返回前缀目录，前端据此不渲染 <img>。"""
    assert scenario_public_asset_url("") == ""
    assert scenario_public_asset_url(None) == ""  # type: ignore[arg-type]
    assert scenario_public_asset_url("   ") == ""


# --- _room_cover：两种 cover 输入 ---


def test_room_cover_keeps_prefixed_scenario_cover(tmp_path):
    app, scenarios_dir, _ = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover=ABSOLUTE_COVER)
    with app.app_context():
        assert _room_cover({"scenario_id": "1"}) == ABSOLUTE_COVER


def test_room_cover_prefixes_relative_scenario_cover(tmp_path):
    app, scenarios_dir, _ = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover="scenario-1/assets/cover.png")
    with app.app_context():
        assert (
            _room_cover({"scenario_id": "1"})
            == "/assets/scenarios/scenario-1/assets/cover.png"
        )


def test_room_cover_without_scenario_or_cover_is_empty(tmp_path):
    app, scenarios_dir, _ = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover="")
    with app.app_context():
        assert _room_cover({"scenario_id": "1"}) == ""
        assert _room_cover({"scenario_id": ""}) == ""
        assert _room_cover({"scenario_id": "missing"}) == ""


# --- 房间摘要 cover 字段：接口级断言 ---


def test_room_summary_cover_keeps_absolute_url(tmp_path):
    app, scenarios_dir, rooms_dir = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover=ABSOLUTE_COVER)
    info = _write_room(rooms_dir)

    response = _client(app).get("/api/rooms")

    assert response.status_code == 200
    rooms = response.get_json()["data"]
    summary = next(item for item in rooms if item["id"] == info["id"])
    assert summary["cover"] == ABSOLUTE_COVER


def test_room_summary_cover_prefixes_relative_path(tmp_path):
    app, scenarios_dir, rooms_dir = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover="scenario-1/assets/cover.png")
    info = _write_room(rooms_dir)

    response = _client(app).get("/api/rooms")

    assert response.status_code == 200
    rooms = response.get_json()["data"]
    summary = next(item for item in rooms if item["id"] == info["id"])
    assert summary["cover"] == "/assets/scenarios/scenario-1/assets/cover.png"


def test_room_summary_cover_empty_when_scenario_has_none(tmp_path):
    app, scenarios_dir, rooms_dir = _make_app(tmp_path)
    _write_scenario(scenarios_dir, cover="")
    info = _write_room(rooms_dir)

    response = _client(app).get("/api/rooms")

    assert response.status_code == 200
    rooms = response.get_json()["data"]
    summary = next(item for item in rooms if item["id"] == info["id"])
    assert summary["cover"] == ""
    with app.app_context():
        assert _room_summary(info)["cover"] == ""
