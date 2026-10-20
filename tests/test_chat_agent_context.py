from pathlib import Path
from types import SimpleNamespace

from flask import Flask, session

from trpg_server.routes.chat import (
    _build_knowledge_usage,
    _can_start_scenario,
    _compact_history_with_ai,
    _load_debug_kp_prompt,
    _format_user_content,
    _history_needs_compaction,
    _is_compact_command,
    _post_ai_request,
    _room_snapshot_system_message,
    _speaker_for_user,
    _strip_compact_command,
    _mark_scenario_started,
    _profile_for_request,
)
from trpg_server.routes.chat import bp as chat_blueprint
from trpg_server.routes.chat_helpers import (
    _build_messages,
    _compact_history_entries,
    _get_debug_kp_prompt_file,
    _history_filename,
)
from trpg_server.agents.profiles import AgentProfile
from trpg_server.routes.characters import _runtime_to_test_character, _test_character_to_runtime
from trpg_server.socket_events import register_socket_events
from trpg_server.routes.rooms import create_room_message


def test_chat_blocks_kp_self_start_until_scenario_started():
    # 兜底硬门控：剧本未开始时，非正式开场请求不得让 KP 自行开场/推进。
    source = Path("backend/trpg_server/routes/chat.py").read_text(encoding="utf-8")

    assert 'agent_profile.id == "kp" and room_id and not scenario_start' in source
    assert 'not _start_gate_info.get("scenario_started_at")' in source
    assert "本房间剧本尚未开始" in source


def test_chat_blueprint_registers_http_routes():
    # 拆分 chat.py 时若漏掉 @bp.route 装饰器，路由会静默消失（无测试覆盖），
    # 因此显式校验三个入口都已注册。
    app = Flask(__name__)
    app.register_blueprint(chat_blueprint)
    rules = {str(rule) for rule in app.url_map.iter_rules()}
    assert "/api/chat" in rules
    assert "/api/messages" in rules
    assert "/api/scenarios/<int:script_id>/messages" in rules


def test_history_filename_is_room_scoped_when_room_is_available():
    assert _history_filename("user-1", "room-alpha", "kp") == "room-room-alpha-kp.json"
    assert _history_filename("user-1", "room-beta", "kp") == "room-room-beta-kp.json"


def test_request_profile_keeps_capabilities_and_removes_only_alias_schemas():
    profile = AgentProfile(
        id="kp",
        name="KP",
        prompt="prompt",
        tool_names=["room.get_scenario_module", "check.roll_room_check", "dice.roll", "dice.roll_dice", "san.roll_sanity_check", "sanity.roll_sanity_check"],
    )
    selected = _profile_for_request(profile, {"allow_checks": False})
    assert selected.tool_names == ["room.get_scenario_module", "check.roll_room_check", "dice.roll", "san.roll_sanity_check"]


def test_knowledge_usage_distinguishes_ruleset_tool_from_scenario_retrieval():
    result = SimpleNamespace(
        knowledge_usage={
            "ruleset_chunks": 2,
            "ruleset_sources": 1,
            "ruleset_ids": ["coc7"],
            "knowledge_versions": ["4"],
            "topics": ["keeper_guidance"],
            "citations": ["Keeper Rulebook p.42"],
            "tool_calls": 1,
        }
    )
    scenario_results = [{"chunk_id": "scene-1"}, {"chunk_id": "npc-1"}]

    usage = _build_knowledge_usage(result, scenario_results)

    assert usage["used"] is True
    assert usage["total_chunks"] == 4
    assert usage["ruleset"]["called"] is True
    assert usage["ruleset"]["chunks"] == 2
    assert usage["ruleset"]["sources"] == 1
    assert usage["scenario"]["chunks"] == 2


def test_scenario_start_permission_allows_room_managers_but_not_members():
    room = {
        "creator_id": 1,
        "members": [
            {"user_id": 1, "room_role": "owner", "status": "active"},
            {"user_id": 2, "room_role": "admin", "status": "active"},
            {"user_id": 3, "room_role": "member", "status": "active"},
        ],
    }

    assert _can_start_scenario(room, 1, "USER") is True
    assert _can_start_scenario(room, 2, "USER") is True
    assert _can_start_scenario(room, 3, "USER") is False
    assert _can_start_scenario(room, 99, "ADMIN") is True


def test_mark_scenario_started_persists_once(tmp_path):
    room_dir = tmp_path / "room-1"
    room_dir.mkdir()
    info_path = room_dir / "info.json"
    info_path.write_text('{"id":"room-1","scenario_started_at":"2026-01-01 10:00:00"}', encoding="utf-8")

    started_at = _mark_scenario_started(room_dir, user_id=7)

    saved = __import__("json").loads(info_path.read_text(encoding="utf-8"))
    assert started_at == "2026-01-01 10:00:00"
    assert saved["scenario_started_at"] == "2026-01-01 10:00:00"
    assert saved["scenario_started_by"] == 7


def test_debug_prompt_uses_configured_config_directory(tmp_path):
    prompt_file = tmp_path / "roles" / "debug-kp.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("# Debug\ncheck.roll_room_check only", encoding="utf-8")

    app = Flask(__name__)
    app.config["CONFIG_DIR"] = tmp_path
    with app.app_context():
        assert _get_debug_kp_prompt_file() == prompt_file
        assert _load_debug_kp_prompt() == "check.roll_room_check only"


def test_history_filename_falls_back_to_user_for_non_room_chat():
    assert _history_filename("user-1", None, "kp") == "user-user-1-kp.json"


def test_build_messages_includes_room_snapshot_context():
    snapshot = {
        "room": {"id": "room-1", "name": "测试房间"},
        "scenario": {"id": 7, "title": "长生俑", "scenes": [{"id": 1, "content": "西安高铁站"}]},
        "members": [
            {
                "username": "ADMIN",
                "character_card": {"name": "吴明山", "background": {"story": "失踪记者"}},
                "character_state": {"current_san": 50},
            }
        ],
    }

    message = _room_snapshot_system_message(snapshot)
    messages = _build_messages("KP prompt", [], "@KP 开始", room_snapshot_message=message)

    assert messages[0] == {"role": "system", "content": "KP prompt"}
    assert messages[1]["role"] == "system"
    assert "room.get_room_snapshot" in messages[1]["content"]
    assert "长生俑" in messages[1]["content"]
    assert "吴明山" in messages[1]["content"]
    assert messages[-1]["role"] == "user"
    assert "---->USER_INPUT<----" in messages[-1]["content"]
    assert "@KP 开始" in messages[-1]["content"]
    assert "---->END_USER_INPUT<----" in messages[-1]["content"]


def test_room_snapshot_system_message_omits_large_character_and_scenario_details():
    snapshot = {
        "room": {"id": "room-1", "name": "\u6d4b\u8bd5\u623f\u95f4"},
        "scenario": {
            "id": 7,
            "title": "\u957f\u751f\u4fd1",
            "description": "\u897f\u5b89\u8c03\u67e5",
            "background": "\u4e0d\u5e94\u8be5\u8fdb\u5165\u6bcf\u8f6e AI \u8bf7\u6c42\u7684\u5b8c\u6574\u80cc\u666f",
            "scenes": [{"id": 1, "content": "\u897f\u5b89\u9ad8\u94c1\u7ad9"}],
        },
        "members": [
            {
                "username": "ADMIN",
                "character_card": {
                    "id": "investigator-1",
                    "name": "\u5434\u660e\u5c71",
                    "attributes": {"DEX": 55, "STR": 60},
                    "skills": [{"name": "\u4fa6\u5bdf", "value": 70}],
                    "background": {"story": "\u5931\u8e2a\u8bb0\u8005"},
                },
                "character_state": {"current_san": 50},
            }
        ],
    }

    message = _room_snapshot_system_message(snapshot)

    assert "room.get_room_snapshot" in message
    assert "\u957f\u751f\u4fd1" in message
    assert "\u5434\u660e\u5c71" in message
    assert "skills" not in message
    assert "attributes" not in message
    assert "\u4fa6\u5bdf" not in message
    assert "DEX" not in message
    assert "\u897f\u5b89\u9ad8\u94c1\u7ad9" not in message


def test_room_snapshot_system_message_preserves_compact_snapshot_details():
    snapshot = {
        "room": {"id": "room-1", "name": "test room"},
        "scenario": {
            "id": 7,
            "title": "Long Life Figurine",
            "description": "scenario summary",
            "found": True,
            "available_sections": {"scenes": 3},
        },
        "members": [
            {
                "user_id": 9,
                "username": "ADMIN",
                "active": True,
                "character": {"id": "investigator-1", "name": "Wu Mingshan"},
                "character_state": {"current_san": 50},
            }
        ],
    }

    message = _room_snapshot_system_message(snapshot)

    assert "Wu Mingshan" in message
    assert '"scenes": 3' in message


def test_current_user_message_is_tagged_with_compact_speaker_identity():
    room_info = {
        "members": [
            {
                "user_id": 9,
                "username": "ADMIN",
                "character_card": {"name": "\u65e0\u4e66"},
            },
            {
                "user_id": 11,
                "username": "test1",
                "character_card": {"name": "\u5415\u5357\u679d"},
            },
        ]
    }

    speaker = _speaker_for_user(room_info, 11)
    content = _format_user_content("@KP \u8be2\u95ee\u66f4\u591a\u7ec6\u8282", speaker)

    assert content.splitlines()[0] == "[speaker=test1; character=\u5415\u5357\u679d]"
    assert "\u65e0\u4e66" not in content
    assert "ADMIN" not in content


def test_build_messages_formats_historical_user_speakers():
    history = [
        {
            "role": "user",
            "content": "@KP look",
            "speaker": {"username": "test1", "character_name": "\u5415\u5357\u679d"},
        },
        {"role": "assistant", "content": "response"},
    ]

    messages = _build_messages("KP prompt", history, "@KP continue")

    assert "[speaker=test1; character=\u5415\u5357\u679d]" in messages[1]["content"]
    assert "---->USER_INPUT<----" in messages[1]["content"]
    assert messages[-1]["role"] == "user"
    assert "@KP continue" in messages[-1]["content"]
    assert "---->END_USER_INPUT<----" in messages[-1]["content"]


def test_compact_command_detection_and_response_stripping():
    assert _is_compact_command("/compact") is True
    assert _is_compact_command("@KP /compact") is True
    assert _is_compact_command("@KP keep going") is False

    content, requested = _strip_compact_command("scene ends\n/compact\n")

    assert requested is True
    assert content == "scene ends"


def test_compact_history_with_ai_replaces_history_with_summary():
    calls = []

    def fake_requester(payload):
        calls.append(payload)
        assert payload["model"] == "model-a"
        assert "old user line" in payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": "compressed summary"}}]}

    history = [
        {"role": "user", "content": "old user line"},
        {"role": "assistant", "content": "old assistant line"},
    ]

    compacted = _compact_history_with_ai(fake_requester, "model-a", history)

    assert len(calls) == 1
    assert compacted == _compact_history_entries("compressed summary")
    assert compacted[0]["role"] == "system"
    assert compacted[0]["compact"] is True


def test_history_needs_compaction_uses_character_budget():
    assert _history_needs_compaction([{"content": "x" * 10}], threshold=20) is False
    assert _history_needs_compaction([{"content": "x" * 21}], threshold=20) is True


def test_post_ai_request_logs_full_request_and_response_payload(monkeypatch, caplog):
    class FakeResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "完整回复"}}], "usage": {"total_tokens": 3}}

    def fake_post(base_url, headers, json, timeout):
        assert base_url == "https://example.test/chat"
        assert headers["Authorization"] == "Bearer secret"
        assert json["messages"][0]["content"] == "系统提示"
        return FakeResponse()

    monkeypatch.setattr("trpg_server.routes.chat.requests.post", fake_post)
    caplog.set_level("INFO", logger="trpg_server.routes.chat")

    # 完整请求/响应日志只在 debug 模式下输出（问题 5），因此测试显式开启。
    requester = _post_ai_request("https://example.test/chat", {"Authorization": "Bearer secret"}, debug=True)
    response = requester({"model": "model-a", "messages": [{"role": "system", "content": "系统提示"}]})

    assert response["choices"][0]["message"]["content"] == "完整回复"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "AI API request payload" in log_text
    assert "系统提示" in log_text
    assert "AI API response payload" in log_text
    assert "完整回复" in log_text


def test_post_ai_request_retries_json_object_when_provider_requires_json(monkeypatch, caplog):
    calls = []

    class FakeResponse:
        def __init__(self, ok, status_code, body):
            self.ok = ok
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    def fake_post(base_url, headers, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return FakeResponse(False, 400, {
                "message": "'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'."
            })
        return FakeResponse(True, 200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("trpg_server.routes.chat.requests.post", fake_post)
    caplog.set_level("INFO", logger="trpg_server.routes.chat")
    requester = _post_ai_request("https://example.test/chat", {"Authorization": "Bearer secret"})

    response = requester({
        "model": "model-a",
        "messages": [{"role": "system", "content": "ordinary prompt"}],
        "response_format": {"type": "json_object"},
    })

    assert response["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert any("JSON" in str(message.get("content")) for message in calls[1]["messages"])
    assert "retry" in "\n".join(record.getMessage() for record in caplog.records).lower()


def test_room_message_logs_display_name_without_content(monkeypatch, caplog, tmp_path):
    room_info = {
        "members": [
            {
                "user_id": 1,
                "username": "alice",
                "avatar": "/avatar.jpg",
                "status": "active",
            }
        ]
    }
    messages = []
    room_dir = tmp_path / "room-1"
    room_dir.mkdir(parents=True, exist_ok=True)

    import trpg_server.routes.rooms as rooms_routes
    from trpg_server import logging_config, room_log

    def write_messages(room_dir, saved):
        messages[:] = saved

    monkeypatch.setattr(rooms_routes, "_find_room", lambda room_id: (room_dir, room_info))
    monkeypatch.setattr(rooms_routes, "_read_messages", lambda room_dir: messages.copy())
    monkeypatch.setattr(rooms_routes, "_write_messages", write_messages)
    monkeypatch.setattr(rooms_routes, "_write_room", lambda room_dir, info: None)
    # 房间事件现在写入房间内嵌控制台日志（房间目录内），不再刷服务器控制台。
    monkeypatch.setattr(
        room_log, "room_console_settings", lambda *args, **kwargs: dict(room_log.DEFAULT_ROOM_CONSOLE_SETTINGS)
    )
    caplog.set_level("INFO", logger="trpg_server.routes.rooms")
    # 锁定日志语言：指向空的配置目录即回落到默认语言（英文），
    # 否则会受其他测试残留的配置目录影响。
    monkeypatch.setattr(logging_config, "_log_config_dir", tmp_path)
    monkeypatch.setattr(logging_config, "_language_cache", None)

    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context(
        "/api/rooms/room-1/messages",
        method="POST",
        json={"content": "hello from player", "type": "player"},
    ):
        session["user_id"] = 1
        session["username"] = "alice"
        response, status = create_room_message("room-1")

    assert status == 201
    assert response.get_json()["data"]["content"] == "hello from player"

    entries = room_log.read_room_log(room_dir)["entries"]
    assert len(entries) == 1
    entry = entries[0]
    # 房间消息日志与其他房间接口一致：只记录操作者与结构化元数据，不记录聊天正文。
    assert "alice" in entry["message"]
    assert "sent a room message" in entry["message"]
    assert entry["details"]["内容长度"] == len("hello from player")
    assert "hello from player" not in str(entry)
    # 房间事件不再进入服务器控制台日志。
    server_log = "\n".join(record.getMessage() for record in caplog.records)
    assert "sent a room message" not in server_log


def test_socket_broadcast_does_not_log_persisted_room_message(monkeypatch, caplog):
    class FakeSocketIO:
        def __init__(self):
            self.handlers = {}
            self._trpg_events_registered = False

        def on(self, event_name):
            def decorator(handler):
                self.handlers[event_name] = handler
                return handler

            return decorator

    fake_socket = FakeSocketIO()
    register_socket_events(fake_socket)
    import trpg_server.socket_events as socket_events

    monkeypatch.setattr(socket_events, "emit", lambda *args, **kwargs: None)
    caplog.set_level("INFO", logger="trpg_server.socket_events")

    app = Flask(__name__)
    app.secret_key = "test"
    app.config["ACTIVE_SESSIONS"] = {}
    with app.test_request_context("/socket.io/"):
        session["user_id"] = 1
        session["username"] = "alice"
        fake_socket.handlers["send_message"](
            {
                "room_id": "room-1",
                "message": {
                    "sender_name": "alice",
                    "content": "hello from player",
                    "type": "player",
                },
            }
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "alice:hello from player" not in log_text


def test_login_success_does_not_report_post_login_initialization_errors_as_login_failure():
    source = Path("frontend/src/app/auth/login-view.ts").read_text(encoding="utf-8")

    assert "async function restorePostLoginState" in source
    assert "await restorePostLoginState();" in source

    restore_block = source.split("async function restorePostLoginState", 1)[1]
    assert 'showMessage("loginMessage"' not in restore_block
    assert "await window.autoLoadLastRoom?.();" in restore_block


def test_character_template_exports_skill_base_instead_of_legacy_fields():
    character = {
        "id": "investigator-1",
        "name": "Alice",
        "skills": [
            {
                "id": "libraryUse",
                "skillKey": "libraryUse",
                "name": "Library Use",
                "base": 20,
                "value": 45,
                "category": "explore",
            }
        ],
    }

    payload = _runtime_to_test_character(character)
    skill = payload["skillGroups"]["explore"][0]

    assert skill["base"] == 20
    assert "baseKey" not in skill
    assert "value" not in skill
    assert "skillKey" not in skill


def test_character_template_import_keeps_skill_base_and_omits_legacy_fields():
    payload = {
        "id": "investigator-1",
        "name": "Alice",
        "skillGroups": {
            "explore": [
                {
                    "name": "Library Use",
                    "base": 20,
                    "job": 20,
                    "interest": 5,
                    "growth": 0,
                }
            ]
        },
    }

    character = _test_character_to_runtime(payload)
    skill = character["skills"][0]

    assert skill["base"] == 20
    assert "baseKey" not in skill
    assert "specialtyKey" not in skill
    assert "rank" not in skill


def test_character_template_import_assigns_unique_ids_to_repeated_skills():
    payload = {
        "id": "investigator-1",
        "name": "Alice",
        "skillGroups": {
            "social": [
                {"name": "外语", "base": 1, "job": 0, "interest": 0, "growth": 0},
                {"name": "外语", "base": 1, "job": 0, "interest": 0, "growth": 0},
            ]
        },
    }

    character = _test_character_to_runtime(payload)
    skills = character["skills"]

    assert [skill["id"] for skill in skills] == ["languageOther", "languageOther__2"]


def test_character_template_clears_placeholder_weapon_rows():
    character = {
        "id": "investigator-1",
        "name": "Alice",
        "weapons": [
            {
                "name": "选择武器",
                "skill": "格斗(斗殴)",
                "damage": "1D3",
                "range": "接触",
                "attacks": "1",
                "ammo": "N/A",
                "malfunction": "N/A",
            },
            {"name": "徒手格斗", "skill": "格斗(斗殴)", "damage": "1D3+DB"},
        ],
    }

    payload = _runtime_to_test_character(character)
    empty_weapon, actual_weapon = payload["weapons"]

    assert empty_weapon == {
        "name": "",
        "skill": "",
        "damage": "",
        "range": "",
        "round": "",
        "tho": "",
        "num": "",
        "err": "",
        "weight": "",
        "note": "",
    }
    assert actual_weapon["name"] == "徒手格斗"
    assert actual_weapon["damage"] == "1D3+DB"


def test_frontend_character_template_exports_base_not_base_key():
    source = Path("frontend/src/app/character-sheet.ts").read_text(encoding="utf-8")

    export_block = source.split("function convertCardToTestCharacterJson", 1)[1].split(
        "function convertTestCharacterJsonToCardInput",
        1,
    )[0]
    assert "base: skill.base" in export_block
    assert "baseKey:" not in export_block
    assert "allowSkillBaseEdit" in source
