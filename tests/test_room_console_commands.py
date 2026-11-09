"""房间级控制台命令执行器（room_console_commands）的单元测试。"""
import json

from trpg_server import room_log
from trpg_server.room_console_commands import execute_room_command


def _rooms_dir(tmp_path, *room_ids):
    rooms_dir = tmp_path / "rooms"
    for room_id in room_ids:
        room_dir = rooms_dir / room_id
        room_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "id": room_id,
            "name": f"房间 {room_id}",
            "room_code": f"CODE-{room_id}",
            "creator_id": 1,
            "members": [
                {"user_id": 1, "username": "tester", "role": "ADMIN", "room_role": "owner", "status": "active", "is_active": True},
                {"user_id": 2, "username": "player", "role": "USER", "room_role": "member", "status": "active", "is_active": True},
            ],
        }
        (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return rooms_dir


def _run(rooms_dir, room_id, line, **kwargs):
    return execute_room_command(
        line,
        room_id=room_id,
        room_dir=rooms_dir / room_id,
        rooms_dir=rooms_dir,
        **kwargs,
    )


def test_help_lists_music_external_commands(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "help")

    assert result.ok is True
    assert "immersive music search" in result.message
    assert "immersive music switch" in result.message


def test_music_show_reports_engine_and_bed_state(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "immersive music show")

    assert result.ok is True
    assert "引擎:" in result.message
    assert "底床:" in result.message
    assert "当前外链曲目:" in result.message


def test_music_switch_without_selection_fails(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "immersive music switch")

    assert result.ok is False
    assert "尚无外链检索结果" in result.message


def test_music_search_uses_external_engine_and_reports_track(tmp_path, monkeypatch):
    from trpg_server.immersive.external_music import service as external_music

    rooms_dir = _rooms_dir(tmp_path, "r-1")
    monkeypatch.setattr(external_music, "resolve_engine", lambda settings, rules: ("external", "site"))
    monkeypatch.setattr(
        external_music,
        "search_and_select",
        lambda **kwargs: {
            "ok": True,
            "status": "selected",
            "provider": "jamendo",
            "trigger": "room_console",
            "track": {
                "track_key": "jamendo:t-1",
                "title": "Rainy Alley",
                "artist": "Field Artist",
                "license": "by-sa",
                "attribution": "Field Artist",
                "provider": "jamendo",
            },
            "alternatives": [{"track_key": "jamendo:t-2", "title": "Second", "artist": "A"}],
        },
    )
    published = {}
    monkeypatch.setattr(
        external_music,
        "publish_selection",
        lambda payload, **kwargs: published.update(payload),
    )

    result = _run(rooms_dir, "r-1", "immersive music search 雨夜街道 --tags=dark,ambient")

    assert result.ok is True
    assert "已重新检索并切曲" in result.message
    assert "Rainy Alley" in result.message
    assert "备选: 1 首" in result.message
    assert published["trigger"] == "room_console"


def test_music_search_rejects_unknown_profile(tmp_path, monkeypatch):
    from trpg_server.immersive.external_music import service as external_music

    rooms_dir = _rooms_dir(tmp_path, "r-1")
    monkeypatch.setattr(external_music, "resolve_engine", lambda settings, rules: ("external", "site"))

    result = _run(rooms_dir, "r-1", "immersive music search --profile=nope")

    assert result.ok is False
    assert "未知档位" in result.message


def test_music_switch_selects_alternative_by_index(tmp_path, monkeypatch):
    from trpg_server.immersive.external_music import service as external_music
    from trpg_server.json_store import write_json_atomic

    rooms_dir = _rooms_dir(tmp_path, "r-1")
    write_json_atomic(
        rooms_dir / "r-1" / external_music.EXTERNAL_STATE_FILENAME,
        {
            "track": {"track_key": "jamendo:t-1", "title": "First", "artist": "A"},
            "alternatives": [{"track_key": "jamendo:t-2", "title": "Second", "artist": "B"}],
        },
    )
    published = {}
    monkeypatch.setattr(
        external_music,
        "publish_selection",
        lambda payload, **kwargs: published.update(payload),
    )

    result = _run(rooms_dir, "r-1", "immersive music switch 2")

    assert result.ok is True
    assert "Second" in result.message
    assert published["track"]["track_key"] == "jamendo:t-2"
    assert published["trigger"] == "room_console"


def test_console_route_executes_narrative_insert_and_reports_music_status(tmp_path):
    from trpg_server.app_factory import create_app
    from trpg_server.json_store import read_json

    class FakeManager:
        def is_session_current(self, user_id, token):
            return True

        def get_user_by_id(self, user_id):
            return {"id": user_id, "username": "tester", "role": "ADMIN"}

    rooms_dir = _rooms_dir(tmp_path, "r-1")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "room-r-1-kp.json").write_text("[]", encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": rooms_dir,
            "HISTORY_DIR": history_dir,
        }
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=1,
            username="tester",
            role="ADMIN",
            session_token="token",
            csrf_token="csrf",
        )

    insert_response = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "narrative insert 雕像的眼睛动了一下"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert insert_response.status_code == 200
    payload = insert_response.get_json()["data"]
    assert payload["ok"] is True
    assert "已广播给所有玩家" in payload["message"]

    messages = read_json(rooms_dir / "r-1" / "messages.json", default=[])
    assert messages[-1]["type"] == "gm"
    history = read_json(history_dir / "room-r-1-kp.json", default=[])
    assert history[-1]["role"] == "system"

    status_response = client.post(
        "/api/rooms/r-1/console/command",
        json={"command": "immersive music show"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert status_response.status_code == 200
    assert "引擎:" in status_response.get_json()["data"]["message"]


def test_help_lists_room_commands(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "help")

    assert result.ok is True
    assert "房间控制台命令" in result.message
    assert "archive" in result.message


def test_info_reports_room_details(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "info")

    assert result.ok is True
    assert "房间 r-1" in result.message
    assert "CODE-r-1" in result.message
    assert "成员: 2 人" in result.message


def test_members_lists_room_roles(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members")

    assert result.ok is True
    assert "tester" in result.message
    assert "房间角色=owner" in result.message
    assert "player" in result.message
    assert "房间角色=member" in result.message


def test_state_reports_counts(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "state")

    assert result.ok is True
    assert "线索 0" in result.message
    assert "任务 0" in result.message


def test_event_add_records_event(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "event add combat 玩家开火", actor="tester")

    assert result.ok is True
    assert "已记录事件" in result.message
    state = json.loads((rooms_dir / "r-1" / "state.json").read_text(encoding="utf-8"))
    assert state["event_log"][-1]["kind"] == "combat"
    assert state["event_log"][-1]["content"] == "玩家开火"


def test_event_add_requires_kind_and_content(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "event add only_kind")

    assert result.ok is False
    assert "Usage" in result.message


def test_log_tail_shows_entries(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    room_log.append_room_log(rooms_dir / "r-1", "创建了房间", room_id="r-1", max_entries=50)

    result = _run(rooms_dir, "r-1", "log tail 10")

    assert result.ok is True
    assert "创建了房间" in result.message


def test_log_clear_is_dry_run_without_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    room_log.append_room_log(rooms_dir / "r-1", "创建了房间", room_id="r-1", max_entries=50)

    result = _run(rooms_dir, "r-1", "log clear")

    assert result.ok is True
    assert "预演" in result.message
    assert len(room_log.read_room_log(rooms_dir / "r-1")["entries"]) == 1


def test_log_clear_executes_with_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    room_log.append_room_log(rooms_dir / "r-1", "创建了房间", room_id="r-1", max_entries=50)

    result = _run(rooms_dir, "r-1", "log clear --yes")

    assert result.ok is True
    assert room_log.read_room_log(rooms_dir / "r-1")["entries"] == []


def test_log_clear_executes_with_pure_word_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    room_log.append_room_log(rooms_dir / "r-1", "创建了房间", room_id="r-1", max_entries=50)

    result = _run(rooms_dir, "r-1", "log clear yes")

    assert result.ok is True
    assert room_log.read_room_log(rooms_dir / "r-1")["entries"] == []


def test_log_clear_confirms_then_executes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    room_log.append_room_log(rooms_dir / "r-1", "创建了房间", room_id="r-1", max_entries=50)
    prompts = []

    def _confirm(prompt):
        prompts.append(prompt)
        return True

    result = _run(rooms_dir, "r-1", "log clear", confirm=_confirm)

    assert result.ok is True
    assert prompts
    assert room_log.read_room_log(rooms_dir / "r-1")["entries"] == []


def test_clear_only_affects_target_room(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1", "r-2")
    room_log.append_room_log(rooms_dir / "r-1", "房间一事件", room_id="r-1", max_entries=50)
    room_log.append_room_log(rooms_dir / "r-2", "房间二事件", room_id="r-2", max_entries=50)

    _run(rooms_dir, "r-1", "log clear --yes")

    assert room_log.read_room_log(rooms_dir / "r-1")["entries"] == []
    assert len(room_log.read_room_log(rooms_dir / "r-2")["entries"]) == 1


def _member(rooms_dir, room_id, username):
    info = json.loads((rooms_dir / room_id / "info.json").read_text(encoding="utf-8"))
    return next(item for item in info["members"] if item["username"] == username)


def test_members_without_subcommand_still_lists(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members")

    assert result.ok is True
    assert "房间角色=member" in result.message


def test_members_kick_is_dry_run_without_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members kick player")

    assert result.ok is True
    assert "预演" in result.message
    assert _member(rooms_dir, "r-1", "player").get("is_active", True) is not False


def test_members_kick_executes_with_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members kick player --yes")

    assert result.ok is True
    target = _member(rooms_dir, "r-1", "player")
    assert target["is_active"] is False
    assert target["status"] == "removed"


def test_members_kick_accepts_user_id(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members kick 2 --yes")

    assert result.ok is True
    assert _member(rooms_dir, "r-1", "player")["is_active"] is False


def test_members_kick_rejects_owner(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members kick tester --yes")

    assert result.ok is False
    assert "所有者" in result.message
    assert _member(rooms_dir, "r-1", "tester").get("is_active", True) is not False


def test_members_kick_unknown_target(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members kick nobody --yes")

    assert result.ok is False
    assert "未找到" in result.message


def test_members_promote_executes_with_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members promote player --yes")

    assert result.ok is True
    target = _member(rooms_dir, "r-1", "player")
    assert target["room_role"] == "admin"
    # 只调整房间内角色，不影响账号角色（不得授予网站级权限）。
    assert target["role"] == "USER"


def test_members_promote_is_dry_run_without_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members promote player")

    assert result.ok is True
    assert "预演" in result.message
    assert _member(rooms_dir, "r-1", "player")["room_role"] == "member"


def test_members_promote_rejects_owner_target(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members promote tester --yes")

    assert result.ok is False
    assert "所有者" in result.message


def test_members_promote_rejects_non_admin_role(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members promote player owner --yes")

    assert result.ok is False
    assert "只能提升为房间管理员" in result.message
    assert _member(rooms_dir, "r-1", "player")["room_role"] == "member"


def test_members_promote_rejects_site_role_argument(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members promote player user --yes")

    assert result.ok is False
    assert "只能提升为房间管理员" in result.message
    assert _member(rooms_dir, "r-1", "player")["room_role"] == "member"


def test_members_promote_twice_second_is_rejected(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    _run(rooms_dir, "r-1", "members promote player --yes")

    result = _run(rooms_dir, "r-1", "members promote player --yes")

    assert result.ok is False
    assert "已是房间管理员" in result.message


def test_members_kick_only_affects_target_room(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1", "r-2")

    _run(rooms_dir, "r-1", "members kick player --yes")

    assert _member(rooms_dir, "r-1", "player")["is_active"] is False
    assert _member(rooms_dir, "r-2", "player").get("is_active", True) is not False


def test_members_unknown_subcommand_is_rejected(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "members ban player")

    assert result.ok is False
    assert "不属于房间范围" in result.message


def test_global_commands_are_rejected(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    for line in ("user list", "shutdown", "scenario list", "backup list", "room list"):
        result = _run(rooms_dir, "r-1", line)
        assert result.ok is False, line
        assert "不属于房间范围" in result.message


def test_unknown_subcommand_is_rejected(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "log purge")

    assert result.ok is False
    assert "不属于房间范围" in result.message


def test_command_rejected_without_room_dir(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = execute_room_command("info", room_id="r-1", room_dir=None, rooms_dir=rooms_dir)

    assert result.ok is False
    assert "房间目录不可用" in result.message


def test_archive_dry_run_without_yes(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "archive")

    assert result.ok is True
    assert "预演" in result.message
    assert json.loads((rooms_dir / "r-1" / "info.json").read_text(encoding="utf-8")).get("archived") is not True


def test_archive_executes_when_room_finished(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")
    info_path = rooms_dir / "r-1" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["completed_at"] = "2026-01-01 00:00:00"
    info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    result = _run(rooms_dir, "r-1", "archive --yes")

    assert result.ok is True
    assert json.loads(info_path.read_text(encoding="utf-8"))["archived"] is True


def test_archive_rejected_when_room_unfinished(tmp_path):
    rooms_dir = _rooms_dir(tmp_path, "r-1")

    result = _run(rooms_dir, "r-1", "archive --yes")

    assert result.ok is False
    assert "无法归档" in result.message or "尚未" in result.message
