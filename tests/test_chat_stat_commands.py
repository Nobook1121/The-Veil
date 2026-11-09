"""聊天斜杠命令 /skill、/attr、/item 的服务端接口测试。

这三条命令与控制台 ``rule skill`` / ``rule attr`` / ``rule item`` 共用
``room_rules`` 服务入口，因此测试重点在：默认写房间快照、``persist`` 才写持久卡、
本人可操作自己的卡、操作他人需要房主 / 管理员权限。
"""
import json

from trpg_server.app_factory import create_app


class FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester" if user_id == 1 else "player", "role": self.role}


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
    })


def _login(client, user_id=2, username="player", role="USER"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username=username, role=role, session_token="token", csrf_token="csrf")


def _card(card_id: str, name: str) -> dict:
    return {
        "id": card_id,
        "name": name,
        "rulesetId": "coc7",
        "attributes": {"STR": 50, "DEX": 50, "CON": 50, "APP": 50, "POW": 60, "SIZ": 50, "EDU": 60, "INT": 60, "LUC": 50},
        "maxHp": 10,
        "currentHp": 10,
        "maxSan": 60,
        "initialSan": 60,
        "currentSan": 60,
        "maxMp": 12,
        "currentMp": 12,
        "mov": 8,
        "build": 0,
        "damageBonus": "0",
        "armor": 0,
        "skills": [
            {"name": "侦察", "value": 60},
            {"name": "克苏鲁神话", "value": 0},
        ],
        "equipment": [{"name": "手电筒", "quantity": 2}],
        "weapons": [{"name": "匕首", "damage": "1D4"}],
        "status": {},
    }


def _room(tmp_path, room_id="r-1"):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "house_rules": {},
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": _card("card-1", "Alice"),
                "character_state": {"max_hp": 10, "current_hp": 10, "max_san": 60, "current_san": 60, "max_mp": 12, "current_mp": 12},
            },
            {
                "user_id": 2,
                "username": "player",
                "role": "USER",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": _card("card-2", "Bob"),
                "character_state": {"max_hp": 10, "current_hp": 10, "max_san": 60, "current_san": 60, "max_mp": 12, "current_mp": 12},
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _stored_card(tmp_path, card_id="card-2", name="Bob"):
    card_dir = tmp_path / "characters" / card_id
    card_dir.mkdir(parents=True, exist_ok=True)
    stored = {
        "id": card_id,
        "name": name,
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": {"STR": 50, "DEX": 50, "CON": 50, "APP": 50, "POW": 60, "SIZ": 50, "EDU": 60, "INT": 60, "LUC": 50},
        "deriveAttributes": {
            "sanity": {"start": 60, "current": 60, "max": 60},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 12, "max": 12},
        },
        "skills": [{"name": "侦察", "value": 60}, {"name": "克苏鲁神话", "value": 0}],
        "equipment": [{"name": "手电筒", "quantity": 2}],
        "weapons": [{"name": "匕首", "damage": "1D4"}],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
    }
    path = card_dir / "character.json"
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return path


def _read_room(room_dir) -> dict:
    return json.loads((room_dir / "info.json").read_text(encoding="utf-8"))


def _member(room_dir, username: str) -> dict:
    return next(m for m in _read_room(room_dir)["members"] if m["username"] == username)


def _headers():
    return {"X-CSRF-Token": "csrf"}


def _find_named_row(node, name: str):
    """在持久卡 JSON 里递归查找指定名称的行（技能可能落在 skillGroups 里）。"""
    if isinstance(node, dict):
        if node.get("name") == name and ("value" in node or "base" in node):
            return node
        for value in node.values():
            found = _find_named_row(value, name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_named_row(item, name)
            if found is not None:
                return found
    return None


def test_skill_view_self(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/rules/skills", headers=_headers())

    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["player_name"] == "Bob"
    assert {"name": "侦察", "value": 60} in body["data"]["skills"]


def test_skill_view_single(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/rules/skills?name=侦察", headers=_headers())

    assert response.status_code == 200
    assert response.get_json()["data"]["skill"] == {"name": "侦察", "value": 60}


def test_player_sets_own_skill_in_room_snapshot_only(tmp_path):
    room_dir, _ = _room(tmp_path)
    stored = _stored_card(tmp_path)
    original = json.loads(stored.read_text(encoding="utf-8"))
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"name": "侦察", "value": 75},
        headers=_headers(),
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert (data["before"], data["after"], data["delta"]) == (60, 75, 15)
    assert data["persisted"] is False
    assert _member(room_dir, "player")["character_card"]["skills"][0]["value"] == 75
    # 未带 --persist 时持久卡保持不变
    assert json.loads(stored.read_text(encoding="utf-8"))["skills"][0]["value"] == original["skills"][0]["value"]


def test_player_skill_delta_is_capped_at_99(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"name": "侦察", "delta": 50},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["after"] == 99
    assert _member(room_dir, "player")["character_card"]["skills"][0]["value"] == 99


def test_skill_persist_writes_back_card_and_clears_queue(tmp_path):
    room_dir, _ = _room(tmp_path)
    stored = _stored_card(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"name": "侦察", "delta": 5, "persist": True, "reason": "成长"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["persisted"] is True
    saved = json.loads(stored.read_text(encoding="utf-8"))
    match = _find_named_row(saved, "侦察")
    assert match is not None and match.get("value", match.get("base")) == 65
    assert "pending_changes" not in _member(room_dir, "player")["character_state"]


def test_skill_change_on_other_player_requires_privilege(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"player_name": "tester", "name": "侦察", "value": 70},
        headers=_headers(),
    )

    assert response.status_code == 403


def test_owner_can_change_other_player(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client, user_id=1, username="tester", role="ADMIN")

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"player_name": "player", "name": "侦察", "value": 70},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert _member(room_dir, "player")["character_card"]["skills"][0]["value"] == 70


def test_unknown_skill_returns_error(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/skills",
        json={"name": "不存在的技能", "value": 10},
        headers=_headers(),
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_attribute_view_and_change_recomputes_derived(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    view = client.get("/api/rooms/r-1/rules/attributes", headers=_headers())
    assert view.status_code == 200
    assert view.get_json()["data"]["attributes"]["POW"] == 60

    response = client.post(
        "/api/rooms/r-1/rules/attributes",
        json={"key": "CON", "value": 80},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["after"] == 80
    member = _member(room_dir, "player")
    # CON/SIZ 变化后 HP 上限按派生值重算，当前值只做上限夹紧
    assert member["character_state"]["max_hp"] == 13
    assert member["character_state"]["current_hp"] == 10
    assert member["character_card"]["maxHp"] == 13


def test_attribute_change_updates_san_cap_when_pow_changes(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/attributes",
        json={"key": "POW", "value": 40},
        headers=_headers(),
    )

    assert response.status_code == 200
    member = _member(room_dir, "player")
    # 初始理智 = POW，上限 = 99 − 克苏鲁神话（当前值只受上限夹紧，不受初始值回压）
    assert member["character_card"]["initialSan"] == 40
    assert member["character_card"]["maxSan"] == 99
    assert member["character_state"]["max_san"] == 99
    assert member["character_state"]["current_san"] == 60


def test_attribute_change_rejects_unknown_key(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/attributes",
        json={"key": "CHA", "value": 10},
        headers=_headers(),
    )

    assert response.status_code == 400


def test_item_list_returns_equipment_and_weapons(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/rooms/r-1/rules/items", headers=_headers())

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["equipment"][0] == {"name": "手电筒", "quantity": 2, "note": ""}
    assert data["weapons"][0]["name"] == "匕首"


def test_item_remove_decrements_quantity(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"name": "手电筒", "quantity": 1},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["quantity"] == 1
    equipment = _member(room_dir, "player")["character_card"]["equipment"]
    assert equipment == [{"name": "手电筒", "quantity": 1}]


def test_item_remove_last_unit_deletes_row(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"name": "手电筒", "quantity": 2},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert _member(room_dir, "player")["character_card"]["equipment"] == []


def test_item_remove_weapon(tmp_path):
    room_dir, _ = _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"name": "匕首", "kind": "weapon"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["kind"] == "weapon"
    assert _member(room_dir, "player")["character_card"]["weapons"] == []


def test_item_remove_persist_writes_back_card(tmp_path):
    room_dir, _ = _room(tmp_path)
    stored = _stored_card(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"name": "匕首", "kind": "weapon", "persist": True},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["persisted"] is True
    saved = json.loads(stored.read_text(encoding="utf-8"))
    assert saved["weapons"] == []
    assert "pending_changes" not in _member(room_dir, "player")["character_state"]


def test_item_remove_missing_name_returns_error(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"name": "不存在的物品"},
        headers=_headers(),
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_item_remove_other_player_requires_privilege(tmp_path):
    _room(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/rooms/r-1/rules/items",
        json={"player_name": "tester", "name": "手电筒"},
        headers=_headers(),
    )

    assert response.status_code == 403
