"""团录导出的纯函数测试（设计稿 §5.6）。

渲染与接口测试在 P1 / P2 追加到本文件；这里只覆盖归约、Markdown 与字体探测。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import pymupdf

from trpg_server.app_factory import create_app
from trpg_server.json_store import write_json_atomic
from trpg_server.room_log import fonts as room_log_fonts
from trpg_server.room_log import jobs as room_log_jobs
from trpg_server.room_log.jobs import RoomLogExportStore
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


def test_build_document_filters_prop_messages_by_viewer(tmp_path):
    """团录不能把只发给部分玩家的定向道具卡导出给所有人。"""
    room = _write_room(
        tmp_path / "room",
        [
            _msg("所有人都能看到的记录"),
            _msg(
                "他递来一封密信",
                msg_type="prop",
                metadata={"prop_id": "prop-1", "revealed_to": [2]},
            ),
        ],
        members=[
            {"user_id": "1", "username": "owner", "room_role": "owner", "is_active": True},
            {"user_id": "2", "username": "reader", "room_role": "member", "is_active": True},
            {"user_id": "3", "username": "other", "room_role": "member", "is_active": True},
        ],
    )
    recipient = json.dumps(
        build_document(room, data_dir=tmp_path / "data", viewer_id=2), ensure_ascii=False
    )
    bystander = json.dumps(
        build_document(room, data_dir=tmp_path / "data", viewer_id=3), ensure_ascii=False
    )
    assert "密信" in recipient
    assert "密信" not in bystander
    assert "所有人都能看到的记录" in bystander


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


@pytest.mark.parametrize("template", ["archive", "letter", "minimal"])
def test_all_templates_render_chinese(tmp_path, template):
    room = _write_room(
        tmp_path / "room",
        [
            _msg("第一天，夜色沉沉。", created_at="2026-10-01 20:00:00"),
            _msg("1d100 = 42 成功", msg_type="dice", created_at="2026-10-01 20:01:00"),
            _msg("我推开门。", msg_type="player", sender_id=1, created_at="2026-10-01 20:02:00"),
        ],
    )
    document = build_document(room, {"template": template}, data_dir=tmp_path / "data")
    output = tmp_path / f"{template}.pdf"
    result = render_pdf(
        document, output, template=template, room_dir=room, data_dir=tmp_path / "data"
    )
    assert result["pages"] >= 1
    pdf = pymupdf.open(output)
    try:
        text = "".join(page.get_text() for page in pdf)
        assert "夜色沉沉" in text
        assert "\ufffd" not in text
    finally:
        pdf.close()


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


def _create_done_export(client, room_id, options=None):
    """创建一次导出并等到终态，返回 (export_id, 最终状态)。"""
    created = client.post(
        f"/api/rooms/{room_id}/log/exports",
        json=options or {"template": "archive"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert created.status_code == 202
    export_id = created.get_json()["data"]["export_id"]
    for _ in range(200):
        payload = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}").get_json()["data"]
        if payload["status"] in {"done", "failed"}:
            return export_id, payload
        time.sleep(0.05)
    raise AssertionError("export did not reach a terminal status")


def test_preview_returns_contract_fields(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client, user_id=2, username="player")

    response = client.get(f"/api/rooms/{room_id}/log/preview")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_export"] is True
    assert data["can_export_reason"] is None
    assert [item["id"] for item in data["templates"]] == ["archive", "letter", "minimal"]
    names = {item["id"]: item["name"] for item in data["templates"]}
    assert names["archive"] and names["letter"] and names["minimal"]
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


# ---------------------------------------------------------------- 硬化：任务恢复 / 超时 / 清理


def test_delete_export_requires_creator_or_manager(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)
    export_id, payload = _create_done_export(client, room_id)
    assert payload["status"] == "done"

    # 同房间普通成员不是发起人：403。
    _login(client, user_id=2, username="player")
    denied = client.delete(f"/api/rooms/{room_id}/log/exports/{export_id}", headers={"X-CSRF-Token": "csrf"})
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "log.forbidden"

    # 发起人（房主）可以删除；删除后同一 id 变为 404（幂等语义收敛为「不存在」）。
    _login(client, user_id=1, username="owner")
    deleted = client.delete(f"/api/rooms/{room_id}/log/exports/{export_id}", headers={"X-CSRF-Token": "csrf"})
    assert deleted.status_code == 200
    assert deleted.get_json()["data"]["deleted"] is True
    again = client.delete(f"/api/rooms/{room_id}/log/exports/{export_id}", headers={"X-CSRF-Token": "csrf"})
    assert again.status_code == 404
    assert again.get_json()["error"] == "log.export_not_found"


def test_delete_export_does_not_leak_existence_to_non_members(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)
    export_id, _payload = _create_done_export(client, room_id)

    stranger = _api_app(tmp_path).test_client()
    _login(stranger, user_id=99, username="stranger")
    known = stranger.delete(f"/api/rooms/{room_id}/log/exports/{export_id}", headers={"X-CSRF-Token": "csrf"})
    unknown = stranger.delete(f"/api/rooms/{room_id}/log/exports/{'f' * 32}", headers={"X-CSRF-Token": "csrf"})

    # 非成员对「存在」与「不存在」的 id 都得到同一个 403，无法据此探测。
    for response in (known, unknown):
        assert response.status_code == 403
        assert response.get_json()["error"] == "log.forbidden"


def test_download_export_is_limited_to_creator_and_managers(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)
    export_id, payload = _create_done_export(client, room_id)
    assert payload["status"] == "done"

    _login(client, user_id=2, username="player")
    denied = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}/file")
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "log.forbidden"

    _login(client, user_id=1, username="owner")
    allowed = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}/file")
    assert allowed.status_code == 200
    assert allowed.mimetype == "application/pdf"


def test_export_status_survives_store_restart(tmp_path):
    room_id = _api_room(tmp_path)
    app = _api_app(tmp_path)
    client = app.test_client()
    _login(client)
    export_id, payload = _create_done_export(client, room_id)
    assert payload["status"] == "done"

    # 模拟进程重启：内存任务表清空，但磁盘 job.json / PDF 仍在。
    with app.app_context():
        room_log_jobs.get_store()._jobs.clear()

    status = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}").get_json()["data"]
    assert status["status"] == "done"
    download = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}/file")
    assert download.status_code == 200


def test_interrupted_export_is_recovered_as_failed(tmp_path):
    room_id = _api_room(tmp_path)
    app = _api_app(tmp_path)
    client = app.test_client()
    _login(client)
    export_id, _payload = _create_done_export(client, room_id)

    with app.app_context():
        store = room_log_jobs.get_store()
        metadata_path = store.job_path(room_id, export_id)
        assert metadata_path is not None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(status="running", error=None, progress=10)
        write_json_atomic(metadata_path, metadata)
        store._jobs.clear()

    status = client.get(f"/api/rooms/{room_id}/log/exports/{export_id}").get_json()["data"]
    assert status["status"] == "failed"
    assert status["error"] == "log.export_interrupted"
    assert status["trace_id"]


def test_cleanup_removes_expired_exports_only(tmp_path):
    store = RoomLogExportStore(tmp_path / "exports-cleanup", max_workers=1)
    job = store.create("r-log", {}, 1)
    export_id = job["export_id"]
    metadata_path = store.job_path("r-log", export_id)
    pdf_path = store.pdf_path("r-log", export_id)
    assert metadata_path is not None and pdf_path is not None
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4")
    old = time.time() - 10 * 86400
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(status="done", created_at=old)
    write_json_atomic(metadata_path, metadata)
    with store._lock:
        store._jobs[("r-log", export_id)].update(status="done", created_at=old)

    fresh = store.create("r-log", {}, 1)
    store.cleanup(7)

    assert not metadata_path.parent.exists()
    assert store.get("r-log", export_id) is None
    assert store.get("r-log", fresh["export_id"]) is not None

    # 旧布局（PDF 直接放在房间目录）同样按 mtime 清理。
    legacy = tmp_path / "exports-cleanup" / "r-log" / f"{'a' * 32}.pdf"
    legacy.write_bytes(b"%PDF-1.4")
    os.utime(legacy, (old, old))
    store.cleanup(7)
    assert not legacy.exists()


def test_export_job_timeout_marks_failed_with_trace_id(tmp_path, monkeypatch):
    app = _api_app(tmp_path)
    store = RoomLogExportStore(tmp_path / "exports-timeout", max_workers=1, timeout_seconds=0.05)
    job = store.create("r-log", {"template": "archive"}, 1)

    def slow_render(*_args, **_kwargs):
        time.sleep(0.3)
        return {"bytes": 1, "pages": 1}

    monkeypatch.setattr(room_log_jobs, "render_pdf", slow_render)
    store.start(app, tmp_path / "rooms" / "r-log", {"options": {"template": "archive"}}, job)

    current = None
    for _ in range(100):
        current = store.get("r-log", job["export_id"])
        if current and current["status"] == "failed":
            break
        time.sleep(0.02)

    assert current is not None and current["status"] == "failed"
    assert current["error"] == "log.timeout"
    assert current["trace_id"]
    public = store.public(current)
    assert public["trace_id"] == current["trace_id"]


def test_export_concurrency_respects_max_workers(tmp_path, monkeypatch):
    app = _api_app(tmp_path)
    store = RoomLogExportStore(tmp_path / "exports-concurrency", max_workers=2, timeout_seconds=10)
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def fake_render(_document, output, **_kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.1)
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4")
        with guard:
            state["active"] -= 1
        return {"bytes": 8, "pages": 1}

    monkeypatch.setattr(room_log_jobs, "render_pdf", fake_render)
    jobs = [store.create("r-log", {}, 1) for _ in range(4)]
    for job in jobs:
        store.start(app, tmp_path / "rooms" / "r-log", {"options": {}}, job)

    deadline = time.time() + 10
    while time.time() < deadline:
        statuses = [store.get("r-log", job["export_id"])["status"] for job in jobs]
        if all(status in {"done", "failed"} for status in statuses):
            break
        time.sleep(0.02)

    assert state["peak"] == 2
    assert all(store.get("r-log", job["export_id"])["status"] == "done" for job in jobs)


def test_recover_interrupted_scans_disk_on_startup(tmp_path):
    root = tmp_path / "exports-recover"
    store = RoomLogExportStore(root, max_workers=1)
    job = store.create("r-log", {}, 1)
    metadata_path = store.job_path("r-log", job["export_id"])
    assert metadata_path is not None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(status="running", error=None, progress=40)
    write_json_atomic(metadata_path, metadata)

    # 新进程：内存为空，启动恢复把遗留的运行中任务标为中断。
    restarted = RoomLogExportStore(root, max_workers=1)
    assert restarted.recover_interrupted() == 1
    current = restarted.get("r-log", job["export_id"])
    assert current is not None and current["status"] == "failed"
    assert current["error"] == "log.export_interrupted"
    assert current["trace_id"]


def test_delete_unfinished_export_returns_not_ready(tmp_path):
    room_id = _api_room(tmp_path)
    app = _api_app(tmp_path)
    client = app.test_client()
    _login(client)
    with app.app_context():
        queued = room_log_jobs.get_store().create(room_id, {}, 1)

    response = client.delete(
        f"/api/rooms/{room_id}/log/exports/{queued['export_id']}",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "log.not_ready"

