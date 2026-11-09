import json

from trpg_server import room_interlude, room_rules
from trpg_server.app_factory import create_app
from trpg_server.json_store import read_json
from trpg_server.ruleset.coc7 import growth


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
        }
    )


def _login(client):
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="token", csrf_token="csrf")


def _write_room(tmp_path):
    room_dir = tmp_path / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    card = {
        "name": "调查员",
        "rulesetId": "coc7",
        "attributes": {"STR": 50, "DEX": 50, "CON": 50, "APP": 50, "POW": 60, "SIZ": 50, "EDU": 60, "INT": 60},
        "skills": [
            {"name": "侦查", "value": 50},
            {"name": "聆听", "value": 92},
            {"name": "信用评级", "value": 30},
            {"name": "克苏鲁神话", "value": 10},
        ],
        "currentSan": 50,
        "maxSan": 60,
    }
    info = {
        "id": "r-1",
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "room_role": "admin",
                "status": "active",
                "is_active": True,
                "character_card": card,
                "character_state": {
                    "current_san": 50,
                    "max_san": 60,
                    "current_hp": 10,
                    "max_hp": 10,
                    "growth_marks": [{"id": "m-1", "skill": "侦查", "value": 50}],
                },
            },
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _queue_rng(values):
    queue = list(values)

    def rng(sides):
        value = queue.pop(0)
        assert 1 <= value <= sides, (value, sides)
        return value

    return rng


def test_growth_check_and_exclusions():
    assert growth.growth_check(50, roll=51, gain_roll=7)["after"] == 57
    assert growth.growth_check(50, roll=50, gain_roll=7)["gain"] == 0
    assert growth.growth_check(94, roll=96, gain_roll=5)["gain"] == 5
    assert not growth.is_growth_eligible("克苏鲁神话")
    assert not growth.is_growth_eligible("信用评级")
    assert growth.should_mark("侦查", 50, success=True)
    assert not growth.should_mark("侦查", 50, success=False)
    assert not growth.should_mark("侦查", 50, success=True, bonus_dice=1)


def test_request_only_proposes_and_start_settles_growth_and_san(tmp_path):
    room_dir, info = _write_room(tmp_path)
    request = room_interlude.request_interlude(room_dir, info, actor="tester", actor_id=1, source="player", reason="结局了")
    assert request["reason"] == "结局了"
    assert room_interlude.is_active(info) is False

    outcome = room_interlude.start_interlude(
        room_dir,
        info,
        actor="tester",
        actor_id=1,
        rng=_queue_rng([60, 5, 3, 4]),
    )
    assert outcome["state"]["active"] is True
    assert info["completed_at"]
    card = info["members"][0]["character_card"]
    assert next(row for row in card["skills"] if row["name"] == "侦查")["value"] == 55
    # 聆听 92 → 2D6(3+4)=7，SAN 50 → 57
    assert info["members"][0]["character_state"]["current_san"] == 57
    assert info["members"][0]["character_state"]["growth_marks"] == []
    assert outcome["settlement"]["growth_done"] is True
    assert outcome["settlement"]["members"][0]["growth"][0]["gain"] == 5
    assert outcome["settlement"]["members"][0]["san_recovery"][0]["skill"] == "聆听"


def test_growth_mark_survives_separate_member_snapshot(tmp_path):
    room_dir, info = _write_room(tmp_path)
    member_copy = json.loads(json.dumps(info["members"][0]))
    record = room_rules.record_growth_mark_for_check(
        room_dir,
        info,
        member_copy,
        {"name": "聆听", "success": True, "bonus_dice": 0},
    )
    assert record is not None
    saved = read_json(room_dir / "info.json", default={})
    marks = saved["members"][0]["character_state"]["growth_marks"]
    assert any(row.get("skill") == "聆听" for row in marks)
    # 属性检定不打勾
    assert (
        room_rules.record_growth_mark_for_check(
            room_dir,
            info,
            member_copy,
            {"name": "力量", "success": True, "bonus_dice": 0},
        )
        is None
    )


def test_growth_mark_not_recorded_when_check_is_during_interlude(tmp_path):
    room_dir, info = _write_room(tmp_path)
    room_interlude.start_interlude(room_dir, info, rng=_queue_rng([1, 1, 1, 1]))
    mark = room_interlude.record_growth_mark(
        room_dir,
        info,
        info["members"][0],
        skill="侦查",
        value=55,
    )
    assert mark is None


def test_end_requires_confirmation_and_archives(tmp_path):
    room_dir, _ = _write_room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    started = client.post("/api/rooms/r-1/interlude/start", headers={"X-CSRF-Token": "csrf"}, json={})
    assert started.status_code == 200, started.get_json()

    blocked = client.post("/api/rooms/r-1/interlude/end", headers={"X-CSRF-Token": "csrf"}, json={})
    assert blocked.status_code == 409
    assert "confirm" in blocked.get_json()["message"].lower() or "确认" in blocked.get_json()["message"]

    ended = client.post(
        "/api/rooms/r-1/interlude/end",
        headers={"X-CSRF-Token": "csrf"},
        json={"confirm": True},
    )
    assert ended.status_code == 200, ended.get_json()
    assert (tmp_path / "room-archives").exists()
    assert list((tmp_path / "room-archives").iterdir())


def test_console_end_accepts_pure_word_yes(tmp_path):
    room_dir, info = _write_room(tmp_path)
    room_interlude.start_interlude(room_dir, info, actor="tester", actor_id=1, rng=_queue_rng([60, 5, 3, 4]))

    ok, preview = room_interlude.handle_console_command(
        ["end"],
        room_id="r-1",
        room_dir=room_dir,
        actor="tester",
        actor_id=1,
        actor_rank=3,
    )
    assert ok is True
    assert "yes" in preview

    ok, message = room_interlude.handle_console_command(
        ["end", "yes"],
        room_id="r-1",
        room_dir=room_dir,
        actor="tester",
        actor_id=1,
        actor_rank=3,
    )
    assert ok is True, message
    assert "幕间已结束" in message


def test_restore_node_blocked_during_interlude(tmp_path):
    room_dir, info = _write_room(tmp_path)
    room_interlude.start_interlude(room_dir, info, rng=_queue_rng([1, 1, 1, 1]))
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/nodes/whatever.json/restore",
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 409
