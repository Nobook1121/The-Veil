import json
from pathlib import Path

from trpg_server import room_rules, world_override
from trpg_server.app_factory import create_app
from trpg_server.room_console_commands import execute_room_command


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester" if user_id == 1 else "player", "role": "ADMIN" if user_id == 1 else "USER"}


def _card(card_id: str, name: str, san: int = 50) -> dict:
    return {
        "id": card_id,
        "name": name,
        "maxSan": san,
        "currentSan": san,
        "attributes": {"INT": 50},
        "skills": [{"name": "侦查", "value": 60}],
        "equipment": [],
        "weapons": [],
        "status": {},
    }


def _room(tmp_path, **extra):
    room_dir = tmp_path / "rooms" / "r-1"
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": "r-1",
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
                "character_state": {"max_san": 50, "current_san": 50},
            },
            {
                "user_id": 2,
                "username": "player",
                "role": "USER",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": _card("card-2", "Bob", 40),
                "character_state": {"max_san": 40, "current_san": 40},
            },
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _run(room_dir, line, **kwargs):
    return execute_room_command(
        line,
        room_id="r-1",
        room_dir=room_dir,
        rooms_dir=room_dir.parent,
        actor="tester",
        actor_id=1,
        actor_rank=3,
        **kwargs,
    )


def _read(room_dir) -> dict:
    return json.loads((room_dir / "info.json").read_text(encoding="utf-8"))


def test_san_add_defaults_to_preview(tmp_path):
    room_dir, _ = _room(tmp_path)

    preview = _run(room_dir, "rule san add player -5")

    assert preview.ok is True
    assert "预演" in preview.message
    assert _read(room_dir)["members"][1]["character_state"]["current_san"] == 40


def test_san_add_with_yes_persists_and_records(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule san add player -5 --yes --reason=剧情")

    assert result.ok is True
    info = _read(room_dir)
    member = info["members"][1]
    assert member["character_state"]["current_san"] == 35
    assert member["character_card"]["currentSan"] == 35
    record = member["character_state"]["sanity_records"][0]
    assert record["delta"] == -5
    assert record["reason"] == "剧情"
    assert record["created_by"] == "tester"
    log_text = (room_dir / "console.jsonl").read_text(encoding="utf-8")
    assert "SAN 40 → 35" in log_text


def test_item_writes_room_snapshot_only(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule item player 手电筒 --qty=2")

    assert result.ok is True
    member = _read(room_dir)["members"][1]
    assert member["character_card"]["equipment"][0]["name"] == "手电筒"
    assert member["character_card"]["equipment"][0]["quantity"] == 2
    assert member["character_state"]["item_changes"][0]["persisted"] is False


def test_pure_word_options_match_legacy_flags(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule item player 手电筒 qty 2")

    assert result.ok is True
    member = _read(room_dir)["members"][1]
    assert member["character_card"]["equipment"][0]["name"] == "手电筒"
    assert member["character_card"]["equipment"][0]["quantity"] == 2

    weapon = _run(room_dir, "rule item player 撬棍 weapon")
    assert weapon.ok is True
    assert _read(room_dir)["members"][1]["character_card"]["weapons"][0]["name"] == "撬棍"


def test_pure_word_san_yes_and_reason_still_parses_negative_number(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule san add player -5 yes reason 剧情")

    assert result.ok is True
    member = _read(room_dir)["members"][1]
    assert member["character_state"]["current_san"] == 35
    record = member["character_state"]["sanity_records"][0]
    assert record["delta"] == -5
    assert record["reason"] == "剧情"


def test_pure_word_skill_change_persist_preview(tmp_path):
    room_dir, _ = _room(tmp_path)

    preview = _run(room_dir, "rule skill player 侦查 +5 reason 成长")
    assert preview.ok is True
    assert "预演" in preview.message
    assert _read(room_dir)["members"][1]["character_card"]["skills"][0]["value"] == 60

    result = _run(room_dir, "rule skill player 侦查 +5 yes reason 成长")
    assert result.ok is True
    member = _read(room_dir)["members"][1]
    assert member["character_card"]["skills"][0]["value"] == 65
    assert member["character_state"]["skill_changes"][0]["reason"] == "成长"


def test_rule_check_reports_missing_skill_in_chinese(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule check player 不存在")

    assert result.ok is False
    assert "标准属性或技能" in result.message


def test_rule_limits_cannot_loosen_site_policy(tmp_path):
    room_dir, _ = _room(tmp_path)

    result = _run(room_dir, "rule limits cooldown 5")

    assert result.ok is True
    assert _read(room_dir)["house_rules"]["world_override_cooldown_seconds"] == 20
    assert "按站点值生效" in result.message


def test_resolve_policy_combines_site_and_room(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "[world_override]\ncooldown_seconds = 20\nhourly_limit = 30\n",
        encoding="utf-8",
    )

    assert world_override.resolve_policy(config_dir) == {"cooldown_seconds": 20, "hourly_limit": 30}
    tightened = world_override.resolve_policy(
        config_dir,
        {"world_override_cooldown_seconds": 60, "world_override_hourly_limit": 10},
    )
    assert tightened == {"cooldown_seconds": 60, "hourly_limit": 10}
    loosened = world_override.resolve_policy(
        config_dir,
        {"world_override_cooldown_seconds": 1, "world_override_hourly_limit": 100},
    )
    assert loosened == {"cooldown_seconds": 20, "hourly_limit": 30}


def test_unlimited_site_policy_allows_room_to_restrict(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "[world_override]\ncooldown_seconds = 0\nhourly_limit = 0\n",
        encoding="utf-8",
    )

    policy = world_override.resolve_policy(
        config_dir,
        {"world_override_cooldown_seconds": 10, "world_override_hourly_limit": 5},
    )

    assert policy == {"cooldown_seconds": 10, "hourly_limit": 5}


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


def _login(client, user_id=2, role="USER"):
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username="player" if user_id == 2 else "tester",
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def test_sanity_check_route_requires_permission_for_others(tmp_path):
    _room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    denied = client.post(
        "/api/rooms/r-1/rules/sanity-check",
        json={"player_name": "tester", "san_change": "1d4/1d10"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/api/rooms/r-1/rules/sanity-check",
        json={"player_name": "player", "san_change": "1d4/1d10"},
        headers={"X-CSRF-Token": "csrf"},
    )
    payload = allowed.get_json()
    assert allowed.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["san_after"] <= 40
    assert payload["data"]["record"]["created_by_id"] == "2"


def test_item_route_adds_weapon_to_room_snapshot(tmp_path):
    _room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/rooms/r-1/rules/items",
        json={"player_name": "player", "name": ".38 左轮", "kind": "weapon", "damage": "1d10"},
        headers={"X-CSRF-Token": "csrf"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["data"]["persisted"] is False
    member = _read(tmp_path / "rooms" / "r-1")["members"][1]
    assert member["character_card"]["weapons"][0]["name"] == ".38 左轮"


def test_ai_sanity_check_marks_room_snapshot_for_archive(tmp_path):
    """AI KP 的理智检定只改房间快照，但必须留下待写回标记。"""
    from trpg_server.agents.tools import dice

    room_dir, info = _room(tmp_path)
    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=1, username="tester")

    result = dice.roll_sanity_check(
        {"player_name": "tester", "san_change": "-5/-5"},
        context,
        rng=lambda _sides: 1,
    )

    assert result["current_san"] == 45
    assert result["madness_check_required"] is True
    state = info["members"][0]["character_state"]
    assert state["pending_persist"] is True
    assert _read(room_dir)["members"][0]["character_state"]["pending_persist"] is True


def test_ai_madness_sets_temporary_flag_and_pending(tmp_path):
    """INT 检定成功时进入临时疯狂，并把恐惧/躁狂表现写回待同步状态。"""
    from trpg_server.agents.tools import dice

    room_dir, info = _room(tmp_path)
    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=1, username="tester")
    dice.roll_sanity_check({"player_name": "tester", "san_change": "-5/-5"}, context, rng=lambda _sides: 1)

    madness = dice.roll_madness({"player_name": "tester", "cause": "测试"}, context, rng=lambda _sides: 1)

    assert madness["insane"] is True
    assert madness["madness"]["type"] == "temporary"
    member = info["members"][0]
    assert member["character_state"]["mental_status"]["temporaryInsanity"] is True
    assert member["character_state"]["pending_persist"] is True


def test_archive_persists_pending_san_without_item_changes(tmp_path, monkeypatch):
    """归档时即便没有物品变化，也要把 SAN / 疯狂写回持久角色卡。"""
    room_dir, info = _room(tmp_path)
    info["members"][1]["character_state"]["pending_persist"] = True
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    calls = []

    def fake_persist(member, *, actor, item_record_id=None):
        calls.append(member.get("user_id"))
        member["character_state"].pop("pending_persist", None)
        return {"character_id": "card-2"}

    monkeypatch.setattr(room_rules, "persist_member_card", fake_persist)
    app = _app(tmp_path)
    with app.app_context():
        result = room_rules.persist_room_items_on_archive(room_dir, info)

    assert result == {"persisted": 1, "skipped": ""}
    assert calls == [2]


def test_archive_skips_members_without_pending_changes(tmp_path, monkeypatch):
    room_dir, info = _room(tmp_path)
    calls = []
    monkeypatch.setattr(
        room_rules,
        "persist_member_card",
        lambda member, *, actor, item_record_id=None: calls.append(member.get("user_id")) or {},
    )
    app = _app(tmp_path)
    with app.app_context():
        result = room_rules.persist_room_items_on_archive(room_dir, info)

    assert result == {"persisted": 0, "skipped": "no_changes"}
    assert calls == []


def test_cardless_member_can_only_roll_dice(tmp_path):
    """无卡玩家（观战）仍可留在房间，但检定 / SAN / 物品必须被拒绝。"""
    room_dir, info = _room(tmp_path)
    info["members"].append(
        {
            "user_id": 3,
            "username": "viewer",
            "role": "USER",
            "room_role": "member",
            "status": "active",
            "is_active": True,
            "character_card": None,
            "character_state": {},
        }
    )
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, user_id=3)

    for path, payload in (
        ("/api/rooms/r-1/rules/sanity-check", {"player_name": "viewer", "san_change": "1d4/1d10"}),
        ("/api/rooms/r-1/rules/check", {"player_name": "viewer", "name": "侦查"}),
        ("/api/rooms/r-1/rules/items", {"player_name": "viewer", "name": "手电筒"}),
    ):
        response = client.post(path, json=payload, headers={"X-CSRF-Token": "csrf"})
        assert response.status_code == 400, path
        assert response.get_json()["error"] == "no_card", path


def test_non_member_cannot_use_room_rules(tmp_path):
    """非房间成员不能借规则接口操作房间内的角色卡。"""
    _room(tmp_path)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, user_id=99)

    response = client.post(
        "/api/rooms/r-1/rules/sanity-check",
        json={"player_name": "player", "san_change": "1d4/1d10"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 403


def test_manual_room_allows_sanity_check(tmp_path):
    """无 AI KP 房间仍可使用房间规则接口。"""
    _room(tmp_path, room_mode="manual", ai_kp_enabled=False, scenario_binding="optional")
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, user_id=2)

    response = client.post(
        "/api/rooms/r-1/rules/sanity-check",
        json={"player_name": "player", "san_change": "-2/-2"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    member = _read(tmp_path / "rooms" / "r-1")["members"][1]
    assert member["character_state"]["current_san"] == 38


def test_archive_writes_ai_san_change_to_persistent_card(tmp_path):
    """AI KP 的 SAN 结算最终会随归档写进持久角色卡，而不只停在房间快照。"""
    room_dir, info = _room(tmp_path)
    app = _app(tmp_path)
    card_dir = tmp_path / "characters" / "card-2"
    card_dir.mkdir(parents=True)
    stored = {
        "id": "card-2",
        "name": "Bob",
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": {key: 50 for key in ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")},
        "deriveAttributes": {
            "sanity": {"start": 40, "current": 40, "max": 40},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 10, "max": 10},
        },
        "skills": [],
        "equipment": [],
        "weapons": [],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
    }
    (card_dir / "character.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    from trpg_server.agents.tools import dice

    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=2, username="player")
    dice.roll_sanity_check({"player_name": "player", "san_change": "-5/-5"}, context, rng=lambda _sides: 1)

    with app.app_context():
        result = room_rules.persist_room_items_on_archive(room_dir, info)

    assert result["persisted"] == 1
    saved = json.loads((card_dir / "character.json").read_text(encoding="utf-8"))
    assert int(saved["deriveAttributes"]["sanity"]["current"]) == 35
    assert "pending_persist" not in info["members"][1]["character_state"]


def _stored_card(tmp_path, *, skill_values=None, attributes=None):
    card_dir = tmp_path / "characters" / "card-2"
    card_dir.mkdir(parents=True)
    skill_values = skill_values or {"侦查": 60}
    stored = {
        "id": "card-2",
        "name": "Bob",
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": attributes or {key: 50 for key in ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")},
        "deriveAttributes": {
            "sanity": {"start": 40, "current": 40, "max": 40},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 10, "max": 10},
        },
        "skillGroups": {
            "explore": [
                {"name": name, "base": value, "job": 0, "interest": 0, "growth": 0}
                for name, value in skill_values.items()
            ]
        },
        "equipment": [],
        "weapons": [],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
    }
    (card_dir / "character.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return card_dir


def test_rule_skill_preview_then_apply(tmp_path):
    room_dir, _ = _room(tmp_path)

    preview = _run(room_dir, "rule skill player 侦查 +5")
    assert preview.ok is True
    assert "预演" in preview.message
    assert _read(room_dir)["members"][1]["character_card"]["skills"][0]["value"] == 60

    result = _run(room_dir, "rule skill player 侦查 +5 --yes --reason=成长")
    assert result.ok is True
    info = _read(room_dir)
    member = info["members"][1]
    assert member["character_card"]["skills"][0]["value"] == 65
    assert member["character_state"]["pending_changes"][0]["type"] == "skill"
    record = member["character_state"]["skill_changes"][0]
    assert record["before"] == 60 and record["after"] == 65 and record["reason"] == "成长"


def test_rule_attr_relative_and_absolute(tmp_path):
    room_dir, _ = _room(tmp_path)

    preview = _run(room_dir, "rule attr player INT +5")
    assert preview.ok is True and "预演" in preview.message

    result = _run(room_dir, "rule attr player INT +5 --yes")
    assert result.ok is True
    member = _read(room_dir)["members"][1]
    assert member["character_card"]["attributes"]["INT"] == 55

    absolute = _run(room_dir, "rule attr player 力量 60 --yes")
    assert absolute.ok is True
    assert _read(room_dir)["members"][1]["character_card"]["attributes"]["STR"] == 60


def test_attribute_writes_clamp_to_ruleset_cap(tmp_path):
    """属性上限由规则集插件提供（CoC7 = 99），预演、控制台与 KP 工具口径一致。"""
    from trpg_server.agents.tools.character_stats import execute_set_character_attribute

    room_dir, info = _room(tmp_path)
    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=2, username="player")

    preview = _run(room_dir, "rule attr player INT +200")
    assert preview.ok is True
    assert "99" in preview.message

    result = _run(room_dir, "rule attr player INT +200 --yes")
    assert result.ok is True
    assert _read(room_dir)["members"][1]["character_card"]["attributes"]["INT"] == 99
    assert "99" in result.message

    tool = execute_set_character_attribute({"player_name": "player", "name": "INT", "value": 150}, context)
    assert tool["ok"] is True
    assert tool["after"] == 99


def test_legacy_over_cap_attribute_stays_readable_until_written(tmp_path):
    """旧卡超限属性读取 / 展示不报错，写入时才收敛到规则集上限。"""
    room_dir, _ = _room(tmp_path)
    stored = _read(room_dir)
    stored["members"][1]["character_card"]["attributes"]["INT"] = 120
    (room_dir / "info.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    preview = _run(room_dir, "rule attr player INT -5")
    assert preview.ok is True
    assert _read(room_dir)["members"][1]["character_card"]["attributes"]["INT"] == 120
    assert "115" in preview.message


def test_ai_tools_update_skill_and_attribute_in_room_snapshot(tmp_path):
    from trpg_server.agents.tools.character_stats import (
        execute_set_character_attribute,
        execute_set_character_skill,
    )

    room_dir, info = _room(tmp_path)
    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=2, username="player")

    skill_result = execute_set_character_skill({"player_name": "player", "name": "侦查", "delta": 10}, context)
    attr_result = execute_set_character_attribute({"player_name": "player", "name": "DEX", "value": 65}, context)

    assert skill_result["ok"] is True and skill_result["after"] == 70
    assert attr_result["ok"] is True and attr_result["after"] == 65
    member = _read(room_dir)["members"][1]
    assert member["character_card"]["skills"][0]["value"] == 70
    assert member["character_card"]["attributes"]["DEX"] == 65
    types = [change["type"] for change in member["character_state"]["pending_changes"]]
    assert set(types) == {"skill", "attr"}


def test_archive_writes_skill_and_attr_without_clobbering_editor_changes(tmp_path):
    """归档只写实际变更的技能 / 属性，保留玩家在卡面编辑器里的其它修改。"""
    room_dir, info = _room(tmp_path)
    info["members"][1]["character_card"]["skills"].append({"name": "聆听", "value": 70})
    info["members"][1]["character_card"]["attributes"]["STR"] = 55
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    _stored_card(tmp_path, skill_values={"侦查": 60, "聆听": 75}, attributes={"STR": 55, "DEX": 50, "INT": 50})
    app = _app(tmp_path)

    from trpg_server.agents.tools.character_stats import execute_set_character_skill

    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=2, username="player")
    execute_set_character_skill({"player_name": "player", "name": "侦查", "value": 68}, context)
    room_rules.apply_attribute_change(room_dir, info, info["members"][1], key="INT", value=60, source="rule_command")

    with app.app_context():
        result = room_rules.persist_room_items_on_archive(room_dir, info)
        from trpg_server import character_service

        saved = character_service.get_for_actor(room_rules.system_actor(), "card-2")

    assert result["persisted"] == 1
    skills = {row["name"]: int(row["value"]) for row in saved["skills"]}
    assert skills["侦查"] == 68
    assert skills["聆听"] == 75
    saved_attributes = saved["attributes"]
    assert int(saved_attributes["INT"]) == 60
    assert int(saved_attributes["STR"]) == 55
    state = info["members"][1]["character_state"]
    assert "pending_changes" not in state and "pending_persist" not in state


def test_unified_queue_writes_back_item_and_san_together(tmp_path):
    """物品与 SAN 共用同一个待写回队列，归档时一并写回。"""
    room_dir, info = _room(tmp_path)
    _stored_card(tmp_path)
    app = _app(tmp_path)

    room_rules.add_item(room_dir, info, info["members"][1], name="手电筒", quantity=2, source="rule")
    from trpg_server.agents.tools import dice

    context = room_rules.RoomRuleContext(room_dir, info, room_id="r-1", user_id=2, username="player")
    dice.roll_sanity_check({"player_name": "player", "san_change": "-5/-5"}, context, rng=lambda _sides: 1)

    with app.app_context():
        result = room_rules.persist_room_items_on_archive(room_dir, info)
        from trpg_server import character_service

        saved = character_service.get_for_actor(room_rules.system_actor(), "card-2")

    assert result["persisted"] == 1
    raw = json.loads((tmp_path / "characters" / "card-2" / "character.json").read_text(encoding="utf-8"))
    assert int(raw["deriveAttributes"]["sanity"]["current"]) == 35
    assert raw["assets"]["items"] == "手电筒 x2"
    assert saved["equipment"][0]["name"].startswith("手电筒")
    state = info["members"][1]["character_state"]
    assert "pending_changes" not in state
    assert all(record.get("persisted") for record in state["item_changes"])
