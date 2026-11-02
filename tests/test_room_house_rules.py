import json

from trpg_server.app_factory import create_app


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
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "tester", "role": "ADMIN", "status": "active", "is_active": True, "room_role": "admin"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_room_info(tmp_path, room_id="r-1"):
    return json.loads((tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8"))


def _disable_global_hints(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text("[ai]\nshow_ai_hints = false\n", encoding="utf-8")


def _write_admin_dice_defaults(tmp_path, critical=1, fumble=96):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text(
        f"[ai]\ndice_critical_threshold = {critical}\ndice_fumble_threshold = {fumble}\n",
        encoding="utf-8",
    )


def test_house_rules_default_off_and_exposed_in_room_summary(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["house_rules"]["action_suggestions_enabled"] is False
    assert data["global_hints_enabled"] is True

    detail = client.get("/api/rooms/r-1", headers={"X-CSRF-Token": "csrf"})
    assert detail.status_code == 200
    assert detail.get_json()["data"]["house_rules"]["action_suggestions_enabled"] is False


def test_put_house_rules_enables_and_reads_back(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"action_suggestions_enabled": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["house_rules"]["action_suggestions_enabled"] is True
    assert _read_room_info(tmp_path)["house_rules"]["action_suggestions_enabled"] is True


def test_put_rejects_enabling_when_global_hints_disabled(tmp_path):
    _write_room(tmp_path)
    _disable_global_hints(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"action_suggestions_enabled": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    stored = _read_room_info(tmp_path)
    assert stored.get("house_rules", {}).get("action_suggestions_enabled") is not True


def test_put_rejects_unknown_house_rule_key(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"unknown_rule": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400


def test_house_rules_require_room_access_and_manage_permission(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path, "USER")
    client = app.test_client()
    _login(client, "USER", user_id=99)

    assert client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"}).status_code == 403
    assert client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"action_suggestions_enabled": True}},
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 403


def test_house_rules_dice_thresholds_default_to_admin_values(tmp_path):
    _write_room(tmp_path)
    _write_admin_dice_defaults(tmp_path, 4, 90)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"}).get_json()["data"]

    # 房规未配置时留空，生效值回落到管理员默认值。
    assert data["house_rules"]["dice_critical_threshold"] is None
    assert data["house_rules"]["dice_fumble_threshold"] is None
    assert data["dice_threshold_defaults"] == {"critical": 4, "fumble": 90}
    assert data["dice_thresholds"] == {"critical": 4, "fumble": 90}

    summary = client.get("/api/rooms/r-1", headers={"X-CSRF-Token": "csrf"}).get_json()["data"]
    assert summary["dice_thresholds"] == {"critical": 4, "fumble": 90}


def test_put_house_rules_dice_thresholds_override_defaults(tmp_path):
    _write_room(tmp_path)
    _write_admin_dice_defaults(tmp_path, 4, 90)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dice_critical_threshold": 2, "dice_fumble_threshold": 99}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["dice_thresholds"] == {"critical": 2, "fumble": 99}
    stored = _read_room_info(tmp_path)["house_rules"]
    assert stored["dice_critical_threshold"] == 2
    assert stored["dice_fumble_threshold"] == 99


def test_put_house_rules_blank_dice_threshold_restores_default(tmp_path):
    _write_room(tmp_path)
    _write_admin_dice_defaults(tmp_path, 4, 90)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dice_critical_threshold": 2}},
        headers={"X-CSRF-Token": "csrf"},
    )
    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dice_critical_threshold": ""}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["dice_thresholds"]["critical"] == 4
    assert _read_room_info(tmp_path)["house_rules"]["dice_critical_threshold"] is None


def test_put_rejects_non_integer_dice_threshold(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dice_fumble_threshold": "abc"}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400


def test_put_clamps_out_of_range_dice_threshold(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dice_critical_threshold": 500}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["dice_thresholds"]["critical"] == 100


def test_put_house_rules_bgm_urls_reads_back_and_rejects_invalid(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"bgm_urls": ["https://example.com/bgm.mp3", "/assets/bgm/builtin/01-静谧之海.wav"], "bgm_mode": "shuffle"}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    rules = response.get_json()["data"]["house_rules"]
    assert rules["bgm_urls"] == ["https://example.com/bgm.mp3", "/assets/bgm/builtin/01-静谧之海.wav"]
    assert rules["bgm_mode"] == "shuffle"
    assert _read_room_info(tmp_path)["house_rules"]["bgm_urls"][0] == "https://example.com/bgm.mp3"

    invalid = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"bgm_urls": ["javascript:alert(1)"]}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert invalid.status_code == 400
    assert _read_room_info(tmp_path)["house_rules"]["bgm_urls"][0] == "https://example.com/bgm.mp3"

    invalid_mode = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"bgm_mode": "random"}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert invalid_mode.status_code == 400


def test_put_house_rules_accepts_local_music_library_path(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"bgm_urls": ["/assets/bgm/library/abc__night.wav"]}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert _read_room_info(tmp_path)["house_rules"]["bgm_urls"] == ["/assets/bgm/library/abc__night.wav"]

    invalid = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"bgm_urls": ["/assets/bgm/library/../secret.wav"]}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert invalid.status_code == 400
    assert _read_room_info(tmp_path)["house_rules"]["bgm_urls"] == ["/assets/bgm/library/abc__night.wav"]


def test_room_house_rules_migrates_legacy_bgm_url(tmp_path):
    _write_room(tmp_path, house_rules={"bgm_url": "https://example.com/legacy.mp3"})
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"}).get_json()["data"]

    assert data["house_rules"]["bgm_urls"] == ["https://example.com/legacy.mp3"]
    assert data["house_rules"]["bgm_mode"] == "list"


def test_house_rules_skill_bases_default_to_empty_object(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"}).get_json()["data"]

    assert data["house_rules"]["skill_bases"] == {}


def test_put_house_rules_skill_bases_reads_back_and_drops_invalid_entries(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"skill_bases": {"spotHidden": 40, "fighting.sword": "20", "bad": "abc", "": 5}}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["house_rules"]["skill_bases"] == {"spotHidden": 40, "fighting.sword": 20}
    assert _read_room_info(tmp_path)["house_rules"]["skill_bases"] == {"spotHidden": 40, "fighting.sword": 20}


def test_put_house_rules_skill_bases_clamps_out_of_range(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"skill_bases": {"spotHidden": 500, "listen": -3}}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["house_rules"]["skill_bases"] == {"spotHidden": 99, "listen": 0}


def test_put_rejects_non_object_skill_bases(tmp_path):
    _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"skill_bases": [1, 2]}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    assert "skill_bases" not in _read_room_info(tmp_path).get("house_rules", {})


def test_archive_removes_house_rules(tmp_path):
    _write_room(tmp_path, completed_at="2026-01-01 00:00:00", house_rules={"action_suggestions_enabled": True})
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post("/api/rooms/r-1/archive", headers={"X-CSRF-Token": "csrf"})

    assert response.status_code == 200
    stored = _read_room_info(tmp_path)
    assert stored["archived"] is True
    assert "house_rules" not in stored