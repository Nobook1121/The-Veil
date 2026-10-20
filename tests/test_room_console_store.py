"""房间内嵌控制台日志存储（room_log）的单元测试。"""
import json
import logging

from trpg_server import room_log


def _room_dir(tmp_path, room_id="r-1"):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    (room_dir / "info.json").write_text(
        json.dumps({"id": room_id, "name": "Test Room"}, ensure_ascii=False), encoding="utf-8"
    )
    return room_dir


def _settings(**overrides):
    base = dict(room_log.DEFAULT_ROOM_CONSOLE_SETTINGS)
    base.update(overrides)
    return base


def test_append_writes_jsonl_and_reads_back(tmp_path):
    room_dir = _room_dir(tmp_path)

    entry = room_log.append_room_log(
        room_dir, "创建了房间", actor="tester", room_id="r-1", details={"房间ID": "r-1"}, max_entries=500
    )

    assert entry is not None
    log_file = room_dir / "console.jsonl"
    assert log_file.exists()
    data = room_log.read_room_log(room_dir)
    assert len(data["entries"]) == 1
    assert data["entries"][0]["message"] == "创建了房间"
    assert data["entries"][0]["actor"] == "tester"
    assert data["entries"][0]["level"] == "info"
    assert data["entries"][0]["details"] == {"房间ID": "r-1"}


def test_append_trims_to_max_entries(tmp_path):
    room_dir = _room_dir(tmp_path)
    for index in range(5):
        room_log.append_room_log(room_dir, f"事件 {index}", max_entries=3)

    entries = room_log.read_room_log(room_dir)["entries"]

    assert [item["message"] for item in entries] == ["事件 2", "事件 3", "事件 4"]


def test_read_filters_by_level(tmp_path):
    room_dir = _room_dir(tmp_path)
    room_log.append_room_log(room_dir, "普通", level="info", max_entries=50)
    room_log.append_room_log(room_dir, "警告", level="warning", max_entries=50)
    room_log.append_room_log(room_dir, "错误", level="error", max_entries=50)

    data = room_log.read_room_log(room_dir, level="warning")

    assert [item["message"] for item in data["entries"]] == ["警告"]


def test_read_filters_by_search(tmp_path):
    room_dir = _room_dir(tmp_path)
    room_log.append_room_log(room_dir, "玩家掷骰成功", max_entries=50)
    room_log.append_room_log(room_dir, "KP 给出线索", max_entries=50)

    data = room_log.read_room_log(room_dir, search="掷骰")

    assert [item["message"] for item in data["entries"]] == ["玩家掷骰成功"]


def test_read_limit_marks_truncated(tmp_path):
    room_dir = _room_dir(tmp_path)
    for index in range(10):
        room_log.append_room_log(room_dir, f"事件 {index}", max_entries=50)

    data = room_log.read_room_log(room_dir, limit=4)

    assert len(data["entries"]) == 4
    assert data["truncated"] is True
    assert data["entries"][-1]["message"] == "事件 9"


def test_read_tolerates_missing_file(tmp_path):
    room_dir = tmp_path / "rooms" / "r-missing"
    room_dir.mkdir(parents=True)

    data = room_log.read_room_log(room_dir)

    assert data == {"entries": [], "truncated": False}


def test_read_skips_corrupted_lines(tmp_path):
    room_dir = _room_dir(tmp_path)
    log_file = room_dir / "console.jsonl"
    log_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": "t1", "level": "info", "message": "ok"}),
                "{ not valid json",
                json.dumps({"ts": "t2", "level": "info", "message": "still ok"}),
            ]
        ),
        encoding="utf-8",
    )

    entries = room_log.read_room_log(room_dir)["entries"]

    assert [item["message"] for item in entries] == ["ok", "still ok"]


def test_append_skips_when_room_dir_missing(tmp_path):
    missing = tmp_path / "rooms" / "nope"

    assert room_log.append_room_log(missing, "事件", max_entries=50) is None


def test_append_invokes_broadcast_callback(tmp_path):
    room_dir = _room_dir(tmp_path)
    received = []

    room_log.append_room_log(
        room_dir,
        "发送了房间消息",
        room_id="r-1",
        max_entries=50,
        broadcast=lambda room_id, entry: received.append((room_id, entry["message"])),
    )

    assert received == [("r-1", "发送了房间消息")]


def test_clear_room_log_returns_count_and_empties(tmp_path):
    room_dir = _room_dir(tmp_path)
    room_log.append_room_log(room_dir, "事件 1", max_entries=50)
    room_log.append_room_log(room_dir, "事件 2", max_entries=50)

    cleared = room_log.clear_room_log(room_dir)

    assert cleared == 2
    assert room_log.read_room_log(room_dir)["entries"] == []


def test_load_room_log_respects_limit(tmp_path):
    room_dir = _room_dir(tmp_path)
    for index in range(5):
        room_log.append_room_log(room_dir, f"事件 {index}", max_entries=50)

    assert [item["message"] for item in room_log.load_room_log(room_dir, limit=2)] == ["事件 3", "事件 4"]


def test_room_console_settings_reads_section_and_clamps(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.toml").write_text(
        "[room_console]\nenabled = false\nmirror_to_server_console = true\nmax_entries = 99999\n",
        encoding="utf-8",
    )

    settings = room_log.room_console_settings(config_dir)

    assert settings["enabled"] is False
    assert settings["mirror_to_server_console"] is True
    assert settings["max_entries"] == 5000


def test_room_console_settings_falls_back_to_defaults(tmp_path):
    settings = room_log.room_console_settings(tmp_path / "absent")

    assert settings == room_log.DEFAULT_ROOM_CONSOLE_SETTINGS


def test_log_room_event_writes_room_log_without_server_log(tmp_path, caplog, monkeypatch):
    room_dir = _room_dir(tmp_path)
    monkeypatch.setattr(room_log, "room_console_settings", lambda *a, **k: _settings())
    caplog.set_level(logging.INFO)

    entry = room_log.log_room_event(
        room_dir, "发送了房间消息", actor="tester", room_id="r-1", logger_=room_log.logger
    )

    assert entry is not None
    assert room_log.read_room_log(room_dir)["entries"][0]["message"] == "发送了房间消息"
    assert "发送了房间消息" not in caplog.text


def test_log_room_event_mirrors_to_server_log_when_enabled(tmp_path, caplog, monkeypatch):
    room_dir = _room_dir(tmp_path)
    monkeypatch.setattr(
        room_log, "room_console_settings", lambda *a, **k: _settings(mirror_to_server_console=True)
    )
    caplog.set_level(logging.INFO)

    room_log.log_room_event(room_dir, "发送了房间消息", room_id="r-1", logger_=room_log.logger)

    assert "发送了房间消息" in caplog.text


def test_log_room_event_falls_back_when_room_unresolved(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(room_log, "room_console_settings", lambda *a, **k: _settings())
    caplog.set_level(logging.INFO)

    result = room_log.log_room_event(
        message="刷新了邀请链接",
        room_id="missing",
        rooms_dir=tmp_path / "rooms",
        logger_=room_log.logger,
        房间ID="missing",
    )

    assert result is None
    assert "刷新了邀请链接" in caplog.text


def test_log_room_event_falls_back_when_disabled(tmp_path, caplog, monkeypatch):
    room_dir = _room_dir(tmp_path)
    monkeypatch.setattr(room_log, "room_console_settings", lambda *a, **k: _settings(enabled=False))
    caplog.set_level(logging.INFO)

    result = room_log.log_room_event(room_dir, "创建了房间", room_id="r-1", logger_=room_log.logger)

    assert result is None
    assert "创建了房间" in caplog.text
    assert room_log.read_room_log(room_dir)["entries"] == []


def test_append_redacts_sensitive_details(tmp_path):
    room_dir = _room_dir(tmp_path)

    entry = room_log.append_room_log(
        room_dir, "更新了密码", details={"password": "secret"}, max_entries=50
    )

    assert entry is not None
    assert entry["details"]["password"] == "***"
