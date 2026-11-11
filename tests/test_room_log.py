"""团录导出的纯函数测试（设计稿 §5.6）。

渲染与接口测试在 P1 / P2 追加到本文件；这里只覆盖归约、Markdown 与字体探测。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.room_log import fonts as room_log_fonts
from trpg_server.room_log.markdown import to_html
from trpg_server.room_log.model import build_document, resolve_image_path


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
