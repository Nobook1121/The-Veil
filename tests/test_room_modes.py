"""房间模式（scripted / freeform / manual）的创建、读取与归档契约。"""
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


def _write_scenario(tmp_path, scenario_id, title, scene_id, scenario_version="1.0.0"):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": scenario_id,
        "title": title,
        "scenario_version": scenario_version,
        "modules": [{"id": scene_id, "module_type": "scene", "title": f"{title} 开场"}],
    }
    (scenarios_dir / f"{scenario_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "Test Room",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "tester", "role": "ADMIN", "status": "active", "is_active": True, "room_role": "owner"},
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_room_info(tmp_path, room_id):
    return json.loads((tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8"))


def _post_room(client, payload):
    return client.post("/api/rooms", json=payload, headers={"X-CSRF-Token": "csrf"})


def test_create_scripted_room_still_requires_scenario(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "无剧本的剧本房"})
    assert response.status_code == 400, response.get_json()


def test_create_freeform_room_rejects_scenario(tmp_path):
    _write_scenario(tmp_path, 1, "剧本", 101)
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "自由房", "room_mode": "freeform", "scenario_id": 1})
    assert response.status_code == 400, response.get_json()


def test_create_freeform_room_has_no_scenario_and_ai_enabled(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "自由房", "room_mode": "freeform"})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["room_mode"] == "freeform"
    assert data["ai_kp_enabled"] is True
    assert data["scenario_binding"] == "none"
    assert data["scenario_id"] is None
    stored = _read_room_info(tmp_path, data["id"])
    assert "scenario_id" not in stored
    assert stored["room_mode"] == "freeform"


def test_create_manual_room_allows_cardless_owner_and_disables_ai(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "玩家自跑房", "room_mode": "manual"})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["room_mode"] == "manual"
    assert data["ai_kp_enabled"] is False
    assert data["scenario_binding"] == "optional"
    assert data["scenario_id"] is None
    owner = next(member for member in data["members"] if member["user_id"] == 1)
    assert "character_card" not in owner
    # 无 AI 房间没有场景指针。
    stored = _read_room_info(tmp_path, data["id"])
    assert "active_scene_id" not in stored


def test_manual_room_allows_cardless_join(tmp_path):
    _write_room(tmp_path, room_code="ABC123", room_mode="manual", ai_kp_enabled=False, scenario_binding="optional")
    client = _app(tmp_path).test_client()
    _login(client, user_id=2)
    response = client.post(
        "/api/rooms/join",
        json={"room_code": "ABC123"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    joined = next(member for member in data["members"] if member["user_id"] == 2)
    assert "character_card" not in joined
    assert data["ai_kp_enabled"] is False


def test_legacy_room_infers_scripted_mode(tmp_path):
    _write_room(tmp_path, scenario_id=1, scenario_title="旧剧本", scenario_version="1.0.0")
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.get("/api/rooms/r-1")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["room_mode"] == "scripted"
    assert data["ai_kp_enabled"] is True
    assert data["scenario_binding"] == "required"
    # 读取补默认值不应写回 info.json。
    stored = _read_room_info(tmp_path, "r-1")
    assert "room_mode" not in stored


def test_create_freeform_room_persists_default_ruleset(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "自由房", "room_mode": "freeform"})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "coc7"
    stored = _read_room_info(tmp_path, data["id"])
    assert stored["ruleset_id"] == "coc7"


def test_create_room_persists_scenario_ruleset(tmp_path):
    _write_scenario(tmp_path, 1, "剧本", 101)
    scenario_path = tmp_path / "scenarios" / "1.json"
    payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    payload["ruleset_id"] = "coc7"
    scenario_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "剧本房", "scenario_id": 1})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "coc7"
    stored = _read_room_info(tmp_path, data["id"])
    assert stored["ruleset_id"] == "coc7"


def test_create_room_rejects_development_ruleset(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)
    response = _post_room(client, {"name": "占位规则房", "room_mode": "freeform", "ruleset_id": "dnd5e"})
    assert response.status_code == 400, response.get_json()
    payload = response.get_json()
    assert payload["error"] == "ruleset_in_development"
    assert "开发中" in payload["message"]


def test_legacy_room_falls_back_to_coc7_ruleset(tmp_path):
    # 旧房间没有 ruleset_id，场景文件也没有规则集字段：读取时回退 coc7。
    _write_scenario(tmp_path, 1, "旧剧本", 101)
    _write_room(tmp_path, scenario_id=1, scenario_title="旧剧本", scenario_version="1.0.0")
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.get("/api/rooms/r-1")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["ruleset_id"] == "coc7"


def test_freeform_room_rejects_scenario_switch(tmp_path):
    _write_room(tmp_path, room_mode="freeform", ai_kp_enabled=True, scenario_binding="none")
    _write_scenario(tmp_path, 2, "新剧本", 202)
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.post(
        "/api/rooms/r-1/scenario-switch",
        json={"scenario_id": 2},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 400, response.get_json()


def test_archive_freeform_room_skips_scenario_fields(tmp_path):
    _write_room(tmp_path, room_mode="freeform", ai_kp_enabled=True, scenario_binding="none")
    (tmp_path / "characters").mkdir(parents=True, exist_ok=True)
    (tmp_path / "characters" / "card-1.json").write_text(
        json.dumps({"id": "card-1", "name": "调查员"}, ensure_ascii=False),
        encoding="utf-8",
    )
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.post("/api/rooms/r-1/archive", headers={"X-CSRF-Token": "csrf"})
    assert response.status_code == 200, response.get_json()
    archive = json.loads((tmp_path / "room-archives" / "r-1.json").read_text(encoding="utf-8"))
    assert archive["room_mode"] == "freeform"
    assert "scenario_id" not in archive
    # 无剧本房间不往角色卡写「参与过的模组」。
    card = json.loads((tmp_path / "characters" / "card-1.json").read_text(encoding="utf-8"))
    assert "experiencedModules" not in card


def test_manual_room_rejects_ai_chat_before_platform_lookup(tmp_path):
    _write_room(tmp_path, room_mode="manual", ai_kp_enabled=False, scenario_binding="optional")
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.post(
        "/api/chat",
        json={"room_id": "r-1", "user_id": 1, "content": "测试"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"] == "AI KP disabled"


def test_world_override_is_available_in_freeform_rooms(tmp_path):
    _write_room(tmp_path, room_mode="freeform", ai_kp_enabled=True, scenario_binding="none")
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.get("/api/rooms/r-1/world-override")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["allowed"] is True
    assert data["supported"] is True
    assert data["enabled"] is True
    assert data["rate"]["cooldown_seconds"] > 0


def test_world_override_is_disabled_in_manual_rooms(tmp_path):
    _write_room(tmp_path, room_mode="manual", ai_kp_enabled=False, scenario_binding="optional")
    client = _app(tmp_path).test_client()
    _login(client)
    response = client.get("/api/rooms/r-1/world-override")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["allowed"] is False
    assert data["supported"] is False


def test_manual_room_console_rejects_ai_domains_but_allows_dice(tmp_path):
    from trpg_server.room_console_commands import execute_room_command

    rooms_dir = tmp_path / "rooms"
    _write_room(tmp_path, room_mode="manual", ai_kp_enabled=False, scenario_binding="optional")
    common = {"room_id": "r-1", "room_dir": rooms_dir / "r-1", "rooms_dir": rooms_dir}
    for line in ("narrative status", "immersive status", "voice status"):
        result = execute_room_command(line, **common)
        assert result.ok is False, line
        assert "未启用 AI KP" in result.message
    info = execute_room_command("info", **common)
    assert info.ok is True
    assert "模式: 无 AI KP" in info.message
    dice = execute_room_command("dice status", **common)
    assert dice.ok is True


def test_freeform_room_console_rejects_scenario_opening(tmp_path):
    from trpg_server.room_console_commands import execute_room_command

    rooms_dir = tmp_path / "rooms"
    _write_room(tmp_path, room_mode="freeform", ai_kp_enabled=True, scenario_binding="none")
    result = execute_room_command(
        "narrative opening",
        room_id="r-1",
        room_dir=rooms_dir / "r-1",
        rooms_dir=rooms_dir,
    )
    assert result.ok is False
    assert "没有剧本开场" in result.message


def test_console_room_mode_is_dry_run_by_default(tmp_path):
    from trpg_server.console_commands import execute_console_command

    _write_room(
        tmp_path,
        room_mode="scripted",
        ai_kp_enabled=True,
        scenario_binding="required",
        scenario_id=1,
        scenario_title="旧剧本",
    )
    rooms_dir = tmp_path / "rooms"
    preview = execute_console_command("room mode r-1 manual", rooms_dir=rooms_dir)
    assert preview.ok is True, preview.message
    assert "预演" in preview.message
    assert _read_room_info(tmp_path, "r-1")["room_mode"] == "scripted"

    applied = execute_console_command("room mode r-1 manual --yes", rooms_dir=rooms_dir)
    assert applied.ok is True, applied.message
    stored = _read_room_info(tmp_path, "r-1")
    assert stored["room_mode"] == "manual"
    assert stored["ai_kp_enabled"] is False
    # manual 保留剧本作为自查资料。
    assert stored["scenario_id"] == 1

    to_freeform = execute_console_command("room mode r-1 freeform --yes", rooms_dir=rooms_dir)
    assert to_freeform.ok is True, to_freeform.message
    stored = _read_room_info(tmp_path, "r-1")
    assert stored["room_mode"] == "freeform"
    assert stored["scenario_binding"] == "none"
    assert "scenario_id" not in stored


def test_immersive_turn_short_circuits_without_ai(tmp_path):
    from trpg_server.immersive.service import on_narrative_turn

    result = on_narrative_turn(
        room_id="r-1",
        room_dir=tmp_path,
        house_rules={},
        text="玩家自跑的叙事",
        ai_kp_enabled=False,
        config_dir=tmp_path / "config",
    )
    assert result == {"music": None, "illustration": None}


def test_freeform_prompt_drops_scenario_constraints_and_tools():
    from trpg_server.routes.chat import FREEFORM_EXCLUDED_TOOLS, _freeform_system_prompt_suffix

    prompt = _freeform_system_prompt_suffix()
    assert "自由叙事模式" in prompt
    assert "不得创造剧本未写出的" not in prompt
    assert "剧本、房间快照、角色卡和工具返回值是唯一事实来源" not in prompt
    assert {
        "room.get_scenario_context",
        "room.get_scenario_module",
        "room.activate_scenario_scene",
        "trigger.reveal_scenario_trigger",
    } <= set(FREEFORM_EXCLUDED_TOOLS)

