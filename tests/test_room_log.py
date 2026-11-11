"""团录导出的纯函数测试（设计稿 §5.6）。

渲染与接口测试在 P1 / P2 追加到本文件；这里只覆盖归约、Markdown 与字体探测。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pymupdf

from trpg_server.app_factory import create_app
from trpg_server.room_log import fonts as room_log_fonts
from trpg_server.room_log.markdown import to_html
from trpg_server.room_log.model import build_document, resolve_image_path
from trpg_server.room_log.render import RenderError, render_pdf
from trpg_server.routes import room_log as room_log_routes


def _write_room(root: Path, messages: list[dict], members: list[dict] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    info = {
        "id": root.name,
        "name": "测试房间",
        "room_code": "TEST0001",
        "scenario_title": "《测试剧本》",
        "created_at": "2026-10-01 19:00:00",
        "creator_name": "tester",
        "members": members
        if members is not None
        else [{"user_id": "1", "username": "tester", "room_role": "owner", "is_active": True}],
    }
    (root / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (root / "messages.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return root


def _msg(
    content: str,
    *,
    msg_type: str = "kp",
    created_at: str = "2026-10-01 19:10:00",
    sender_id: int | None = None,
    sender_name: str = "KP",
    metadata: dict | None = None,
) -> dict:
    return {
        "id": f"m-{created_at}-{content[:6]}",
        "type": msg_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
        "time": created_at[11:16],
        "created_at": created_at,
        "metadata": metadata or {},
    }


def test_empty_room_yields_empty_document(tmp_path):
    room = _write_room(tmp_path / "room", [])
    document = build_document(room, data_dir=tmp_path / "data")
    assert document["chapters"] == []
    assert document["stats"]["messages_total"] == 0
    assert document["stats"]["messages_exported"] == 0
    assert document["room"]["name"] == "测试房间"


def test_single_message_becomes_one_chapter(tmp_path):
    room = _write_room(tmp_path / "room", [_msg("夜色沉沉，雨点敲打着车窗。")])
    document = build_document(room, data_dir=tmp_path / "data")
    assert len(document["chapters"]) == 1
    chapter = document["chapters"][0]
    assert chapter["title"] == "2026-10-01"
    assert chapter["entries"][0]["kind"] == "narration"
    assert "<p>" in chapter["entries"][0]["html"]
    assert document["stats"]["duration_days"] == 1


def test_cross_day_splits_chapters(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("第一天。", created_at="2026-10-01 20:00:00"),
            _msg("第二天。", created_at="2026-10-02 08:00:00"),
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    assert [chapter["title"] for chapter in document["chapters"]] == ["2026-10-01", "2026-10-02"]
    assert document["stats"]["duration_days"] == 2


def test_same_day_over_250_messages_splits_again(tmp_path):
    messages = [
        _msg(f"第 {index} 条叙述。", created_at=f"2026-10-01 20:{index % 60:02d}:00")
        for index in range(251)
    ]
    room = _write_room(tmp_path / "room", messages)
    document = build_document(room, data_dir=tmp_path / "data")
    assert [chapter["title"] for chapter in document["chapters"]] == [
        "2026-10-01（一）",
        "2026-10-01（二）",
    ]
    assert len(document["chapters"][0]["entries"]) == 250
    assert len(document["chapters"][1]["entries"]) == 1


def test_ooc_messages_are_filtered(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("正常叙述。"),
            _msg("(( 我去倒杯水 ))"),
            _msg("（（场外讨论））"),
            _msg("标记场外", metadata={"ooc": True}),
        ],
    )
    document = build_document(room, {"include_ooc": False}, data_dir=tmp_path / "data")
    assert document["stats"]["messages_total"] == 4
    assert document["stats"]["messages_exported"] == 1
    kept = build_document(room, {"include_ooc": True}, data_dir=tmp_path / "data")
    assert kept["stats"]["messages_exported"] == 4


def test_missing_image_degrades_to_system_entry(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg(
                "触发图片",
                msg_type="trigger",
                metadata={"asset_url": "/assets/scenes/missing.png"},
            )
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    entry = document["chapters"][0]["entries"][0]
    assert entry["kind"] == "system"
    assert "图片缺失" in entry["html"]
    assert document["stats"]["images_missing"] == 1
    assert document["stats"]["images"] == 0


def test_existing_image_is_exported(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "assets" / "scenes").mkdir(parents=True)
    (data_dir / "assets" / "scenes" / "scene.png").write_bytes(b"not-a-real-png")
    room = _write_room(
        tmp_path / "room",
        [
            _msg(
                "触发图片",
                msg_type="trigger",
                metadata={"asset_url": "/assets/scenes/scene.png", "asset_name": "山道"},
            )
        ],
    )
    document = build_document(room, data_dir=data_dir)
    entry = document["chapters"][0]["entries"][0]
    assert entry["kind"] == "image"
    assert entry["caption"] == "山道"
    assert entry["available"] is True
    assert document["stats"]["images"] == 1
    assert resolve_image_path("/assets/scenes/scene.png", data_dir=data_dir) is not None


def test_image_path_traversal_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "assets").mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"x")
    assert resolve_image_path("/assets/../secret.png", data_dir=data_dir) is None
    assert resolve_image_path("C:/Windows/win.ini", data_dir=data_dir) is None


def test_dice_message_maps_roll_metadata(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg(
                "驾驶 d%: [92] = 92 / 51 失败",
                msg_type="dice",
                sender_name="骰娘",
                metadata={"check_type": "room", "name": "驾驶", "player_name": "Charlotte"},
            )
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    entry = document["chapters"][0]["entries"][0]
    assert entry["kind"] == "dice"
    assert entry["roll"]["expr"] == "d%"
    assert entry["roll"]["total"] == 92
    assert entry["roll"]["judgement"] == "失败"
    assert entry["roll"]["label"] == "驾驶"
    assert document["stats"]["dice_rolls"] == 1


def test_dice_can_be_excluded(tmp_path):
    room = _write_room(tmp_path / "room", [_msg("1d100 = 3 成功", msg_type="dice")])
    document = build_document(room, {"include_dice": False}, data_dir=tmp_path / "data")
    assert document["chapters"] == []
    assert document["stats"]["dice_rolls"] == 0


def test_unknown_type_becomes_system(tmp_path):
    room = _write_room(tmp_path / "room", [_msg("系统提示", msg_type="mystery")])
    document = build_document(room, data_dir=tmp_path / "data")
    assert document["chapters"][0]["entries"][0]["kind"] == "system"


def test_player_entry_uses_character_card_name(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [_msg("我推开门。", msg_type="player", sender_id=7, sender_name="migu")],
        members=[
            {"user_id": "7", "username": "migu", "room_role": "member", "is_active": True,
             "character_card": {"name": "何清越"}},
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    entry = document["chapters"][0]["entries"][0]
    assert entry["kind"] == "speaker"
    assert entry["speaker"] == "migu"
    assert entry["character"] == "何清越"


def test_kp_quotation_is_dialogue(tmp_path):
    room = _write_room(tmp_path / "room", [_msg("“你终于来了。”他低声说。")])
    document = build_document(room, data_dir=tmp_path / "data")
    assert document["chapters"][0]["entries"][0]["kind"] == "dialogue"


def test_date_range_filters_messages(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("第一天。", created_at="2026-10-01 20:00:00"),
            _msg("第三天。", created_at="2026-10-03 20:00:00"),
        ],
    )
    document = build_document(
        room,
        {"date_from": "2026-10-02", "date_to": "2026-10-02"},
        data_dir=tmp_path / "data",
    )
    assert document["chapters"] == []
    only_third = build_document(
        room, {"date_from": "2026-10-03"}, data_dir=tmp_path / "data"
    )
    assert [chapter["title"] for chapter in only_third["chapters"]] == ["2026-10-03"]


def test_max_messages_truncates_and_counts(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [_msg(f"第 {index} 条。", created_at=f"2026-10-01 20:{index:02d}:00") for index in range(5)],
    )
    document = build_document(room, {"max_messages": 3}, data_dir=tmp_path / "data")
    assert document["stats"]["messages_exported"] == 3
    assert document["stats"]["messages_truncated"] == 2


def test_markdown_subset_and_script_escape():
    html = to_html("# 标题\n\n**粗体**与*斜体*，还有 `code`\n\n---\n\n> 引用\n\n- 一\n- 二\n\n1. 甲\n2. 乙")
    assert "<h1>标题</h1>" in html
    assert "<strong>粗体</strong>" in html
    assert "<em>斜体</em>" in html
    assert "<code>code</code>" in html
    assert "<hr/>" in html
    assert "<blockquote>" in html
    assert html.count("<ul>") == 1 and html.count("<li>") == 4
    assert "<ol>" in html

    injected = to_html("<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>")
    assert "<script>" not in injected
    assert "&lt;script&gt;" in injected
    assert "<img" not in injected


def test_markdown_attributes_are_escaped():
    # 原始 HTML 不做直通：整段转义，属性不可能进入最终文档。
    html = to_html('<p class="evil">文本</p>')
    assert "<p>" in html
    assert "&lt;p class=&quot;evil&quot;&gt;" in html
    assert '<p class="evil">' not in html


def test_resolve_font_raises_when_no_font(monkeypatch, tmp_path):
    monkeypatch.setattr(room_log_fonts.os.path, "exists", lambda _path: False)
    with pytest.raises(room_log_fonts.FontMissing):
        room_log_fonts.resolve_font("", data_dir=tmp_path)


def test_resolve_font_rejects_bad_override(tmp_path):
    bogus = tmp_path / "bogus.ttf"
    bogus.write_bytes(b"not a font")
    with pytest.raises(room_log_fonts.FontMissing):
        room_log_fonts.resolve_font(str(bogus), data_dir=tmp_path)


def test_resolve_font_finds_system_font():
    family, path = room_log_fonts.resolve_font()
    assert family == room_log_fonts.FONT_FAMILY
    assert Path(path).is_file()


def test_estimated_pages_is_close_to_actual(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("夜色沉沉，雨点敲打着车窗。" * 12, created_at="2026-10-01 20:00:00"),
            _msg("我推开门。", msg_type="player", sender_id=1, created_at="2026-10-01 20:01:00"),
            _msg("1d100 = 42 成功", msg_type="dice", created_at="2026-10-01 20:02:00"),
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    result = render_pdf(document, tmp_path / "estimate.pdf", room_dir=room, data_dir=tmp_path / "data")
    estimated = document["stats"]["estimated_pages"]
    assert abs(estimated - result["pages"]) <= max(3, result["pages"] // 2)


def test_render_pdf_contains_chinese_and_footer(tmp_path):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("第一天，夜色沉沉，雨点敲打着车窗。", created_at="2026-10-01 20:00:00"),
            _msg("“你终于来了。”他低声说。", created_at="2026-10-01 20:01:00"),
            _msg("我推开门。", msg_type="player", sender_id=1, created_at="2026-10-01 20:02:00"),
        ],
        members=[
            {"user_id": "1", "username": "tester", "room_role": "owner", "is_active": True,
             "character_card": {"name": "何清越"}}
        ],
    )
    document = build_document(room, data_dir=tmp_path / "data")
    output = tmp_path / "room-log.pdf"
    result = render_pdf(document, output, room_dir=room, data_dir=tmp_path / "data")

    assert result["pages"] >= 3
    assert output.stat().st_size < 25 * 1024 * 1024
    pdf = pymupdf.open(output)
    try:
        assert pdf.metadata["title"] == "测试房间 · 团录"
        full_text = "".join(page.get_text() for page in pdf)
        assert "夜色沉沉" in full_text
        assert "何清越" in full_text
        assert "第 3 页 / 共" in full_text
        # 目录里的页码必须与章节实际起始页一致。
        chapter_page = next(
            index + 1 for index, page in enumerate(pdf) if index > 1 and "夜色沉沉" in page.get_text()
        )
        toc_text = pdf[1].get_text()
        assert str(chapter_page) in toc_text
    finally:
        pdf.close()


def test_render_pdf_rejects_empty_document(tmp_path):
    document = build_document(_write_room(tmp_path / "room", []), data_dir=tmp_path / "data")
    with pytest.raises(RenderError):
        render_pdf(document, tmp_path / "empty.pdf", data_dir=tmp_path / "data")


# ---------------------------------------------------------------- P2 接口


class _FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, _user_id, _token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _api_app(tmp_path, role="USER"):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": _FakeManager(role),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
            "ROOM_LOG_EXPORT_DIR": tmp_path / "exports",
        }
    )


def _login(client, user_id=1, username="tester", role="USER"):
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=username,
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def _api_room(tmp_path, *, messages=None, members_can_export=None):
    room_id = "r-log"
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "谢娘娘点化",
        "creator_id": 1,
        "scenario_title": "《谢娘娘点化》",
        "members": [
            {"user_id": 1, "username": "owner", "role": "USER", "status": "active",
             "is_active": True, "room_role": "owner"},
            {"user_id": 2, "username": "player", "role": "USER", "status": "active",
             "is_active": True, "room_role": "member"},
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    default_messages = [
        _msg("第一天，夜色沉沉。", created_at="2026-10-01 20:00:00"),
        _msg("(( 场外讨论 ))", created_at="2026-10-01 20:01:00"),
        _msg("我推开门。", msg_type="player", sender_id=2, sender_name="player",
             created_at="2026-10-01 20:02:00"),
    ]
    (room_dir / "messages.json").write_text(
        json.dumps(messages if messages is not None else default_messages, ensure_ascii=False),
        encoding="utf-8",
    )
    if members_can_export is not None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "general.toml").write_text(
            f"[room_log]\nmembers_can_export = {str(members_can_export).lower()}\n",
            encoding="utf-8",
        )
    return room_id


def test_preview_returns_contract_fields(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client, user_id=2, username="player")

    response = client.get(f"/api/rooms/{room_id}/log/preview")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_export"] is True
    assert data["can_export_reason"] is None
    assert [item["id"] for item in data["templates"]] == ["archive"]
    assert data["templates"][0]["name"]
    assert data["templates"][0]["available"] is True
    assert data["stats"]["estimated_pages"] > 0


def test_preview_options_change_stats(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)

    with_ooc = client.get(f"/api/rooms/{room_id}/log/preview?include_ooc=1").get_json()["data"]
    without_ooc = client.get(f"/api/rooms/{room_id}/log/preview?include_ooc=0").get_json()["data"]
    assert with_ooc["stats"]["messages_exported"] == 3
    assert without_ooc["stats"]["messages_exported"] == 2


def test_preview_rejects_non_member(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client, user_id=99, username="stranger")

    response = client.get(f"/api/rooms/{room_id}/log/preview")

    assert response.status_code == 403
    assert response.get_json()["error"] == "log.forbidden"


def test_preview_members_disabled_reports_reason(tmp_path):
    room_id = _api_room(tmp_path, members_can_export=False)

    player_client = _api_app(tmp_path).test_client()
    _login(player_client, user_id=2, username="player")
    data = player_client.get(f"/api/rooms/{room_id}/log/preview").get_json()["data"]
    assert data["can_export"] is False
    assert data["can_export_reason"] == "members_disabled"

    owner_client = _api_app(tmp_path).test_client()
    _login(owner_client, user_id=1, username="owner")
    owner_data = owner_client.get(f"/api/rooms/{room_id}/log/preview").get_json()["data"]
    assert owner_data["can_export"] is True


def test_preview_reports_font_missing_without_503(tmp_path, monkeypatch):
    room_id = _api_room(tmp_path)
    monkeypatch.setattr(
        room_log_routes, "resolve_font", lambda *_a, **_k: (_ for _ in ()).throw(room_log_fonts.FontMissing())
    )
    client = _api_app(tmp_path).test_client()
    _login(client)

    response = client.get(f"/api/rooms/{room_id}/log/preview")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_export"] is False
    assert data["can_export_reason"] == "font_missing"
    assert data["templates"][0]["available"] is False


def test_create_export_rejects_normal_member_when_disabled(tmp_path):
    room_id = _api_room(tmp_path, members_can_export=False)
    client = _api_app(tmp_path).test_client()
    _login(client, user_id=2, username="player")

    response = client.post(
        f"/api/rooms/{room_id}/log/exports",
        json={"template": "archive"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "log.forbidden"


def test_create_export_empty_room_reports_empty(tmp_path):
    room_id = _api_room(tmp_path, messages=[])
    client = _api_app(tmp_path).test_client()
    _login(client)

    response = client.post(
        f"/api/rooms/{room_id}/log/exports",
        json={"template": "archive"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "log.empty"


def test_create_export_returns_503_when_font_missing(tmp_path, monkeypatch):
    room_id = _api_room(tmp_path)
    monkeypatch.setattr(
        room_log_routes, "resolve_font", lambda *_a, **_k: (_ for _ in ()).throw(room_log_fonts.FontMissing())
    )
    client = _api_app(tmp_path).test_client()
    _login(client)

    response = client.post(
        f"/api/rooms/{room_id}/log/exports",
        json={"template": "archive"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "log.font_missing"


def test_export_job_completes_and_downloads_pdf(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)

    created = client.post(
        f"/api/rooms/{room_id}/log/exports",
        json={"template": "archive", "include_ooc": False},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert created.status_code == 202
    job = created.get_json()["data"]
    export_id = job["export_id"]
    assert job["status"] == "queued"

    status = None
    for _ in range(200):
        payload = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}").get_json()["data"]
        if payload["status"] in {"done", "failed"}:
            status = payload
            break
        time.sleep(0.05)

    assert status is not None and status["status"] == "done", status
    assert status["file"]["pages"] > 0
    assert status["file"]["bytes"] > 0

    download = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}/file")
    assert download.status_code == 200
    assert download.mimetype == "application/pdf"
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.headers["Content-Disposition"].endswith(".pdf")

    stranger = _api_app(tmp_path).test_client()
    _login(stranger, user_id=99, username="stranger")
    denied = stranger.get(f"/api/rooms/{room_id}/log/exports/{export_id}/file")
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "log.forbidden"


def test_export_job_not_found(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)

    response = client.get(f"/api/rooms/{room_id}/log/exports/deadbeef")

    assert response.status_code == 404
    assert response.get_json()["error"] == "log.export_not_found"

