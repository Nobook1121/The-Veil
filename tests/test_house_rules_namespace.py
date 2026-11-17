"""E2 房规命名空间：落库、旧扁平兼容读、切换隔离与绑定暂停。"""
import json

from trpg_server.app_factory import create_app
from trpg_server.house_rules_store import (
    BY_RULESET_SCOPE,
    CORE_SCOPE,
    LEGACY_KEY,
    switch_namespaces,
)
from trpg_server.room_ops.cards import pause_incompatible_bindings


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(role),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
        }
    )


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username="tester",
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "status": "active",
                "is_active": True,
                "room_role": "admin",
            }
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_info(tmp_path, room_id="r-1"):
    return json.loads((tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8"))


def _namespaced_request(client, room_id="r-1"):
    response = client.get(f"/api/rooms/{room_id}/house-rules", headers={"X-CSRF-Token": "csrf"})
    assert response.status_code == 200
    return response.get_json()["data"]


def test_legacy_flat_room_reads_through_compat_view_without_writing(tmp_path):
    legacy = {
        "bgm_mode": "single",
        "coc7_social_difficulty": True,
        "skill_bases": {"spotHidden": 40},
        "future_key": 1,
    }
    _write_room(tmp_path, house_rules=legacy)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = _namespaced_request(client)

    assert data["ruleset_id"] == "coc7"
    assert data["schema_version"] == 2
    assert data["house_rules"]["bgm_mode"] == "single"
    assert data["house_rules"]["coc7_social_difficulty"] is True
    assert data["house_rules"]["skill_bases"] == {"spotHidden": 40}
    # 未知键不进入扁平视图，但仍保留在磁盘上（读请求不写盘）。
    assert "future_key" not in data["house_rules"]
    assert _read_info(tmp_path)["house_rules"] == legacy


def test_put_migrates_legacy_flat_room_to_namespaced_storage(tmp_path):
    _write_room(
        tmp_path,
        house_rules={
            "bgm_mode": "single",
            "coc7_social_difficulty": True,
            "skill_bases": {"spotHidden": 40},
            "future_key": 1,
        },
    )
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"coc7_group_luck": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    stored = _read_info(tmp_path)["house_rules"]
    assert stored["schema_version"] == 2
    assert stored[CORE_SCOPE]["bgm_mode"] == "single"
    coc7 = stored[BY_RULESET_SCOPE]["coc7"]
    assert coc7["coc7_social_difficulty"] is True
    assert coc7["coc7_group_luck"] is True
    assert coc7["skill_bases"] == {"spotHidden": 40}
    # schema 不认识的旧键进入 _legacy，不丢失也不冒充新字段。
    assert coc7[LEGACY_KEY]["future_key"] == 1
    assert "future_key" not in response.get_json()["data"]["house_rules"]


def test_namespaced_room_hides_other_rulesets(tmp_path):
    _write_room(
        tmp_path,
        house_rules={
            "schema_version": 2,
            "core": {"dice_critical_threshold": 3},
            "by_ruleset": {
                "coc7": {"skill_bases": {"listen": 55}},
                "dnd5e": {"house_rule_a": True},
            },
        },
    )
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = _namespaced_request(client)

    assert data["house_rules"]["dice_critical_threshold"] == 3
    assert data["house_rules"]["skill_bases"] == {"listen": 55}
    assert "house_rule_a" not in data["house_rules"]


def test_put_rejects_key_outside_active_ruleset_schema(tmp_path):
    _write_room(tmp_path, ruleset_id="dnd5e")
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"coc7_group_luck": True}},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    assert "house_rules" not in _read_info(tmp_path)


def test_switch_namespaces_snapshots_old_and_restores_on_switch_back():
    info = {
        "ruleset_id": "coc7",
        "house_rules": {
            "bgm_mode": "single",
            "coc7_social_difficulty": True,
            "skill_bases": {"spotHidden": 40},
        },
    }

    old_id, new_id = switch_namespaces(info, "dnd5e")

    assert (old_id, new_id) == ("coc7", "dnd5e")
    assert info["ruleset_id"] == "dnd5e"
    # core 设置跨规则集保留；旧规则集字段不串到新视图。
    assert info["house_rules"]["bgm_mode"] == "single"
    assert "coc7_social_difficulty" not in info["house_rules"]
    snapshot = info["_house_rules_ns"][BY_RULESET_SCOPE]["coc7"]
    assert snapshot["skill_bases"] == {"spotHidden": 40}

    old_id, new_id = switch_namespaces(info, "coc7")

    assert (old_id, new_id) == ("dnd5e", "coc7")
    assert info["ruleset_id"] == "coc7"
    # 切回时按旧 schema 重新校验，旧值恢复。
    assert info["house_rules"]["coc7_social_difficulty"] is True
    assert info["house_rules"]["skill_bases"] == {"spotHidden": 40}
    assert info["house_rules"]["bgm_mode"] == "single"


def test_switch_pauses_only_incompatible_bindings():
    info = {
        "ruleset_id": "coc7",
        "members": [
            {"user_id": 1, "character_card": {"id": "card-coc", "rulesetId": "coc7"}},
            {"user_id": 2, "character_card": {"id": "card-dnd", "rulesetId": "dnd5e"}},
            {"user_id": 3},
        ],
    }

    switch_namespaces(info, "dnd5e")
    paused = pause_incompatible_bindings(info, "dnd5e")

    assert [item["user_id"] for item in paused] == ["1"]
    assert info["members"][0]["character_card_paused"] is True
    assert "character_card_paused" not in info["members"][1]
    assert "character_card_paused" not in info["members"][2]

    switch_namespaces(info, "coc7")
    pause_incompatible_bindings(info, "coc7")

    # 切回后恢复兼容绑定，只暂停新的不匹配卡。
    assert "character_card_paused" not in info["members"][0]
    assert info["members"][1]["character_card_paused"] is True
