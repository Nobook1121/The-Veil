"""角色卡占用约束：一张卡同一时刻只能绑定一个活跃房间。"""

import json
import threading

from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = "ADMIN" if user_id == 9 else "USER"
        return {"id": user_id, "username": f"user{user_id}", "role": role}


def _card(card_id="card-1", name="调查员甲", player_id=None):
    card = {"id": card_id, "name": name, "maxHp": 10, "currentHp": 10}
    if player_id is not None:
        card["playerId"] = player_id
    return card


def _make_room(rooms_dir, room_id, *, name=None, code=None, ai_kp=True, archived=False):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": name or f"房间 {room_id}",
        "room_code": code or f"CODE-{room_id}",
        "ai_kp_enabled": ai_kp,
        "archived": archived,
        "creator_id": 1,
        "members": [],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir


def _make_app(tmp_path):
    rooms_dir = tmp_path / "rooms"
    rooms_dir.mkdir()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": rooms_dir,
            "CHARACTERS_DIR": tmp_path / "characters",
            "HISTORY_DIR": history_dir,
        }
    )
    return app, rooms_dir


def _client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=f"user{user_id}",
            role="USER",
            session_token="token",
            csrf_token="csrf",
        )
    return client


def _join(client, code, card=None):
    payload = {"room_code": code}
    if card is not None:
        payload["character_card"] = card
    return client.post("/api/rooms/join", json=payload, headers={"X-CSRF-Token": "csrf"})


def test_card_occupied_by_another_active_room_is_rejected(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", name="迷雾镇", code="CODE-A")
    _make_room(rooms_dir, "r-b", name="旧宅", code="CODE-B")

    assert _join(_client(app, 2), "CODE-A", _card()).status_code == 200

    response = _join(_client(app, 3), "CODE-B", _card())

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"] == "character_card_in_use"
    assert "迷雾镇" in payload["message"]
    assert payload["conflict"]["room_id"] == "r-a"
    assert payload["conflict"]["room_name"] == "迷雾镇"
    assert payload["conflict"]["card_id"] == "card-1"


def test_same_card_rejoin_same_room_is_idempotent(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")

    client = _client(app, 2)
    assert _join(client, "CODE-A", _card()).status_code == 200
    assert _join(client, "CODE-A", _card()).status_code == 200

    info = json.loads((rooms_dir / "r-a" / "info.json").read_text(encoding="utf-8"))
    assert len(info["members"]) == 1


def test_removed_member_releases_card(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    _make_room(rooms_dir, "r-b", code="CODE-B")

    owner = _client(app, 1)
    assert _join(owner, "CODE-A", _card("card-owner", "房主卡")).status_code == 200

    player = _client(app, 2)
    assert _join(player, "CODE-A", _card()).status_code == 200
    assert _join(player, "CODE-B", _card()).status_code == 409

    removed = owner.delete("/api/rooms/r-a/members/2", headers={"X-CSRF-Token": "csrf"})
    assert removed.status_code == 200

    assert _join(player, "CODE-B", _card()).status_code == 200


def test_archived_room_does_not_occupy_card(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(
        rooms_dir,
        "r-old",
        name="已归档",
        code="CODE-OLD",
        archived=True,
    )
    (rooms_dir / "r-old" / "info.json").write_text(
        json.dumps(
            {
                "id": "r-old",
                "name": "已归档",
                "room_code": "CODE-OLD",
                "ai_kp_enabled": True,
                "archived": True,
                "members": [
                    {"user_id": 9, "username": "user9", "role": "USER", "status": "active", "is_active": True, "character_card": _card()}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _make_room(rooms_dir, "r-new", code="CODE-NEW")

    response = _join(_client(app, 2), "CODE-NEW", _card())

    assert response.status_code == 200


def test_cardless_join_is_not_blocked(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    _make_room(rooms_dir, "r-b", code="CODE-B", ai_kp=False)

    assert _join(_client(app, 2), "CODE-A", _card()).status_code == 200
    assert _join(_client(app, 3), "CODE-B").status_code == 200


def test_binding_card_held_by_another_room_is_rejected(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A", ai_kp=False)
    _make_room(rooms_dir, "r-b", code="CODE-B", ai_kp=False)

    assert _join(_client(app, 2), "CODE-A", _card()).status_code == 200
    # 无卡进入 r-b，再尝试绑定同一张卡
    player = _client(app, 3)
    assert _join(player, "CODE-B").status_code == 200

    response = player.put(
        "/api/rooms/r-b/members/3/character",
        json={"character_card": _card(player_id=3)},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "character_card_in_use"


def test_create_room_with_held_card_is_rejected(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", name="迷雾镇", code="CODE-A")
    assert _join(_client(app, 2), "CODE-A", _card()).status_code == 200

    response = _client(app, 3).post(
        "/api/rooms",
        json={"name": "新房间", "room_mode": "manual", "character_card": _card()},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "character_card_in_use"


def test_concurrent_joins_only_one_succeeds(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    _make_room(rooms_dir, "r-b", code="CODE-B")

    barrier = threading.Barrier(2)
    results = {}

    def _attempt(user_id, code):
        client = _client(app, user_id)
        barrier.wait(timeout=5)
        results[code] = _join(client, code, _card()).status_code

    threads = [
        threading.Thread(target=_attempt, args=(2, "CODE-A")),
        threading.Thread(target=_attempt, args=(3, "CODE-B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results.values()) == [200, 409]


def _write_persistent_card(tmp_path, card, card_id=None, user_id=2):
    card_id = card_id or card["id"]
    directory = tmp_path / "characters" / card_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = dict(card)
    payload.setdefault("id", card_id)
    payload.setdefault("playerId", str(user_id))
    (directory / "character.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _snapshot_card():
    return {
        "id": "card-1",
        "name": "调查员",
        "attributes": {"STR": 60, "CON": 50, "SIZ": 50},
        "skills": [{"name": "侦查", "value": 60}],
        "assets": {"cash": 42},
        "currentHp": 8,
        "maxHp": 10,
        "currentSan": 40,
        "maxSan": 50,
        "currentMp": 10,
        "maxMp": 10,
        "equipment": [{"name": "手电筒", "quantity": 3, "weight": 0}],
        "weapons": [{"name": "撬棍"}],
    }


def _persistent_card():
    return {
        "id": "card-1",
        "name": "调查员",
        "attributes": {"STR": 50, "CON": 50, "SIZ": 50},
        "skills": [{"name": "侦查", "value": 50}],
        "assets": {"cash": 82},
        "currentHp": 10,
        "maxHp": 10,
        "currentSan": 50,
        "maxSan": 50,
        "currentMp": 10,
        "maxMp": 10,
        "equipment": [{"name": "手电筒", "quantity": 1, "weight": 0}],
        "weapons": [],
    }


def test_room_character_diff_reports_only_changed_fields(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    client = _client(app, 2)
    assert _join(client, "CODE-A", _snapshot_card()).status_code == 200
    # 加入房间时不会复制资产字段；经济写回会在快照卡上补出 assets.cash。
    room_info = json.loads((rooms_dir / "r-a" / "info.json").read_text(encoding="utf-8"))
    room_info["members"][0]["character_card"]["assets"] = {"cash": 42}
    (rooms_dir / "r-a" / "info.json").write_text(
        json.dumps(room_info, ensure_ascii=False), encoding="utf-8"
    )
    _write_persistent_card(tmp_path, _persistent_card())

    response = client.get(
        "/api/rooms/r-a/members/2/character-diff", headers={"X-CSRF-Token": "csrf"}
    )

    assert response.status_code == 200
    diff = response.get_json()["data"]["diff"]
    assert diff["attributes"] == {"STR": {"current": 60, "saved": 50}}
    assert diff["skills"] == {"侦查": {"current": 60, "saved": 50}}
    assert diff["vitals"]["hp"] == {"current": 8, "saved": 10}
    assert diff["vitals"]["san"] == {"current": 40, "saved": 50}
    assert "mp" not in diff["vitals"]
    assert diff["cash"] == {"current": 42, "saved": 82}
    assert diff["items"] == {"手电筒": {"current": 3, "saved": 1}}
    assert diff["weapons"] == {"撬棍": {"current": 1, "saved": 0}}


def test_room_character_diff_without_persistent_card_is_empty(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    client = _client(app, 2)
    assert _join(client, "CODE-A", _snapshot_card()).status_code == 200

    response = client.get(
        "/api/rooms/r-a/members/2/character-diff", headers={"X-CSRF-Token": "csrf"}
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["diff"] == {}


def test_room_character_diff_requires_membership(tmp_path):
    app, rooms_dir = _make_app(tmp_path)
    _make_room(rooms_dir, "r-a", code="CODE-A")
    assert _join(_client(app, 2), "CODE-A", _snapshot_card()).status_code == 200
    _write_persistent_card(tmp_path, _persistent_card())

    outsider = _client(app, 3)
    denied = outsider.get(
        "/api/rooms/r-a/members/2/character-diff", headers={"X-CSRF-Token": "csrf"}
    )
    assert denied.status_code == 403

    admin = app.test_client()
    with admin.session_transaction() as session:
        session.update(
            user_id=9,
            username="admin",
            role="ADMIN",
            session_token="token",
            csrf_token="csrf",
        )
    allowed = admin.get(
        "/api/rooms/r-a/members/2/character-diff", headers={"X-CSRF-Token": "csrf"}
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["diff"]["skills"]["侦查"]["saved"] == 50
