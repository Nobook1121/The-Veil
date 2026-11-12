"""道具工坊单测（设计稿 §11）。

覆盖：``apply_redaction`` / 可见性、模板注册与字段校验、并发存储与删除清理、
PDF 渲染与缓存，以及接口层的 404 防泄露与写权限。
"""
from __future__ import annotations

import base64
import json
import tempfile
import threading
from pathlib import Path

import pymupdf
import pytest

from trpg_server.app_factory import create_app
from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.tools.props import (
    execute_create_prop,
    execute_reveal_prop,
    execute_update_prop,
)
from trpg_server.props import (
    draft as props_draft,
    image as props_image,
    policy,
    prepare as props_prepare,
    prompt as props_prompt,
    registry,
    render as props_render,
    store,
)
from trpg_server.props.render import RenderError, fill_page_html, render_cached, render_prop
from trpg_server.room_log.fonts import FontMissing, resolve_font


# ---------------------------------------------------------------- 构造工具


def _letter_spec(**overrides):
    spec = {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": "r-props",
        "template_id": "letter-1920",
        "title": "冯亦阳的私人书信",
        "fields": {
            "salutation": "致 何清越",
            "body": "码头那晚的事，我至今不敢细想。钥匙藏在书房。",
            "signature": "冯亦阳",
            "date": "1926 年 6 月 11 日",
            "postscript": "",
        },
        "style": {"paper": "ivory", "ink": "#2b2b2b", "accent": "#7a5c3e"},
        "wear": {"stain": 0.35, "fold": 0.6, "burn": 0.0, "tear": 0.2},
        "redaction": [],
        "visibility": {"mode": "hidden", "revealed_to": []},
        "source": {"kind": "manual"},
        "assets": {},
        "created_by": 1,
        "created_at": "2026-11-10 20:00:00",
        "updated_at": "2026-11-10 20:00:00",
    }
    spec.update(overrides)
    return spec


def _newspaper_spec():
    return {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": "r-props",
        "template_id": "newspaper",
        "title": "申报",
        "fields": {
            "city": "上海",
            "date": "民国十五年六月十二日",
            "issue": "1147",
            "headline": "十六铺码头深夜火起",
            "col1": "本报讯 昨夜十一时许，十六铺码头东栈突起大火。",
            "col2": "巡捕房于凌晨二时封锁现场，未允记者入内。",
            "ad": "永安公司 夏季大减价",
        },
        "style": {"paper": "aged"},
        "wear": {"stain": 0.3, "fold": 0.5, "tear": 0.4},
        "redaction": [],
        "visibility": {"mode": "hidden", "revealed_to": []},
        "source": {"kind": "manual"},
        "assets": {},
        "created_by": 1,
        "created_at": "2026-11-10 20:00:00",
        "updated_at": "2026-11-10 20:00:00",
    }


def _font_path():
    try:
        return resolve_font()[1]
    except FontMissing:  # pragma: no cover - 无中文字体的机器
        pytest.skip("本机没有可用的中文字体")


# ---------------------------------------------------------------- policy


def test_apply_redaction_replaces_range():
    spec = _letter_spec(fields={"body": "0123456789"})
    spec["redaction"] = [{"field": "body", "start": 2, "end": 5, "mode": "burn"}]
    assert policy.apply_redaction(spec)["fields"]["body"] == "01███56789"


def test_apply_redaction_clips_and_skips_empty():
    spec = _letter_spec(fields={"body": "abcdef"})
    spec["redaction"] = [
        {"field": "body", "start": -3, "end": 99, "mode": "burn"},
        {"field": "body", "start": 4, "end": 4, "mode": "ink"},
    ]
    assert policy.apply_redaction(spec)["fields"]["body"] == "██████"


def test_apply_redaction_multiple_segments_backwards():
    spec = _letter_spec(fields={"body": "abcdefghij"})
    spec["redaction"] = [
        {"field": "body", "start": 1, "end": 3, "mode": "burn"},
        {"field": "body", "start": 6, "end": 8, "mode": "ink"},
    ]
    assert policy.apply_redaction(spec)["fields"]["body"] == "a██def██ij"


def test_apply_redaction_skips_unknown_field():
    spec = _letter_spec(fields={"body": "正文"})
    spec["redaction"] = [{"field": "nope", "start": 0, "end": 2, "mode": "burn"}]
    assert policy.apply_redaction(spec)["fields"]["body"] == "正文"


def test_ai_mask_is_four_blocks():
    spec = _letter_spec(fields={"body": "钥匙藏在书房"})
    spec["redaction"] = [{"field": "body", "start": 0, "end": 2, "mode": "burn"}]
    assert policy.redacted_for_ai(spec)["fields"]["body"] == "████藏在书房"


def test_visibility_and_visible_spec():
    hidden = _letter_spec()
    assert not policy.can_view(hidden, 2, is_kp=False)
    assert policy.can_view(hidden, 2, is_kp=True)

    partial = _letter_spec(visibility={"mode": "partial", "revealed_to": [2]})
    assert policy.can_view(partial, 2, is_kp=False)
    assert not policy.can_view(partial, 3, is_kp=False)

    revealed = _letter_spec(visibility={"mode": "revealed", "revealed_to": []})
    assert policy.can_view(revealed, 99, is_kp=False)
    assert policy.visible_spec(revealed, 2, is_kp=False)["redacted"] is False

    redacted = _letter_spec(visibility={"mode": "revealed", "revealed_to": []})
    redacted["redaction"] = [{"field": "body", "start": 0, "end": 2, "mode": "burn"}]
    view = policy.visible_spec(redacted, 2, is_kp=False)
    assert view["redacted"] is True
    assert view["fields"]["body"].startswith("██")
    assert policy.visible_spec(hidden, 2, is_kp=False) is None


# ---------------------------------------------------------------- registry


def test_unknown_template_lists_available_ids():
    with pytest.raises(registry.UnknownTemplate) as info:
        registry.get_template("no-such-template")
    message = str(info.value)
    assert "letter-1920" in message and "newspaper" in message


def test_validate_fields_truncates_and_rejects_unknown():
    fields = registry.validate_fields("letter-1920", {"body": "字" * 50}, max_field_chars=10)
    assert len(fields["body"]) == 10
    with pytest.raises(registry.FieldError):
        registry.validate_fields("letter-1920", {"body": "x", "nope": "y"})
    with pytest.raises(registry.FieldError):
        registry.validate_fields("letter-1920", {})


def test_manifest_missing_required_key(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    (templates / "broken").mkdir(parents=True)
    (templates / "broken" / "manifest.json").write_text(
        json.dumps({"id": "broken", "name": "坏模板"}), encoding="utf-8"
    )
    monkeypatch.setattr(registry, "TEMPLATES_DIR", templates)
    registry._CACHE.clear()
    try:
        assert registry.list_templates() == []
        with pytest.raises(registry.UnknownTemplate):
            registry.get_template("broken")
    finally:
        registry._CACHE.clear()


# ---------------------------------------------------------------- store


def test_store_concurrent_writes_keep_index(tmp_path):
    room = tmp_path / "room"
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            spec = _letter_spec(id=store.new_prop_id(), created_at=f"2026-11-10 20:00:{index:02d}")
            store.write_prop(room, spec)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    index = store.list_index(room)
    assert len(index) == 12
    assert len({entry["id"] for entry in index}) == 12
    raw = json.loads((room / "props" / "index.json").read_text(encoding="utf-8"))
    assert len(raw) == 12


def test_delete_prop_clears_assets_and_renders(tmp_path):
    room = tmp_path / "room"
    spec = _letter_spec()
    store.write_prop(room, spec)
    asset_dir = store.assets_dir(room, spec["id"])
    asset_dir.mkdir(parents=True)
    (asset_dir / "photo.png").write_bytes(b"png")
    render_dir = store.renders_dir(room)
    render_dir.mkdir(parents=True)
    rendered = store.render_path(room, spec["id"], "deadbeef", "pdf")
    rendered.write_bytes(b"pdf")

    assert store.delete_prop(room, spec["id"]) is True
    assert not store.prop_path(room, spec["id"]).exists()
    assert not asset_dir.exists()
    assert not rendered.exists()
    assert store.list_index(room) == []
    assert store.delete_prop(room, spec["id"]) is False


# ---------------------------------------------------------------- render


def test_render_pdf_contains_chinese(tmp_path):
    font = _font_path()
    output = tmp_path / "letter.pdf"
    result = render_prop(_letter_spec(), output, font_path=font)
    assert result["pages"] >= 1
    doc = pymupdf.open(str(output))
    try:
        text = "".join(page.get_text() for page in doc)
    finally:
        doc.close()
    assert "何清越" in text and "书房" in text


def test_render_cache_reuses_file(tmp_path):
    font = _font_path()
    room = tmp_path / "room"
    spec = _letter_spec()
    first = render_cached(room, spec, "pdf", settings={"render_cache_days": 0}, font_path=font)
    second = render_cached(room, spec, "pdf", settings={"render_cache_days": 0}, font_path=font)
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["path"] == second["path"]


def test_render_redacted_spec_hides_text(tmp_path):
    font = _font_path()
    spec = _letter_spec()
    spec["redaction"] = [{"field": "body", "start": 0, "end": 2, "mode": "burn"}]
    output = tmp_path / "redacted.pdf"
    render_prop(policy.apply_redaction(spec), output, font_path=font)
    doc = pymupdf.open(str(output))
    try:
        text = "".join(page.get_text() for page in doc)
    finally:
        doc.close()
    assert "码头" not in text


def test_newspaper_renders_single_page(tmp_path):
    font = _font_path()
    output = tmp_path / "news.pdf"
    result = render_prop(_newspaper_spec(), output, font_path=font)
    assert result["pages"] == 1
    doc = pymupdf.open(str(output))
    try:
        text = doc[0].get_text()
    finally:
        doc.close()
    assert "十六铺" in text and "永安公司" in text


def test_render_unknown_template():
    with pytest.raises(registry.UnknownTemplate):
        render_prop(_letter_spec(template_id="nope"), Path("unused.pdf"))


def test_render_rejects_unknown_format(tmp_path):
    with pytest.raises(RenderError):
        render_prop(_letter_spec(), tmp_path / "x", fmt="svg")


def _temp_render_entries() -> set[str]:
    """工作目录与临时根里所有 ``props-render-*`` 的名字，用于比较渲染前后。"""
    roots = {Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve()}
    names: set[str] = set()
    for root in roots:
        names.update(path.name for path in root.glob("props-render-*"))
    return names


def test_render_success_leaves_no_temp_artifacts(tmp_path):
    font = _font_path()
    before = _temp_render_entries()
    render_prop(_letter_spec(), tmp_path / "letter.pdf", font_path=font)
    render_prop(_letter_spec(), tmp_path / "letter.png", font_path=font, fmt="png")
    assert _temp_render_entries() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["letter.pdf", "letter.png"]


def test_render_failure_leaves_no_temp_artifacts(tmp_path, monkeypatch):
    font = _font_path()
    before = _temp_render_entries()

    def _boom(*_args, **_kwargs):
        raise RenderError("模拟渲染失败")

    monkeypatch.setattr(props_render, "_story_to_pdf_bytes", _boom)
    output = tmp_path / "broken.pdf"
    with pytest.raises(RenderError):
        render_prop(_letter_spec(), output, font_path=font)
    assert _temp_render_entries() == before
    assert list(tmp_path.iterdir()) == []


def test_render_unavailable_template_leaves_no_temp_artifacts(tmp_path):
    before = _temp_render_entries()
    with pytest.raises(registry.UnknownTemplate):
        render_prop(_letter_spec(template_id="missing-template"), tmp_path / "unused.pdf")
    assert _temp_render_entries() == before
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- 接口


class _FakeManager:
    def __init__(self, role="USER"):
        self.role = role

    def is_session_current(self, _user_id, _token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _api_app(tmp_path, props_toml: str | None = None):
    config_dir = tmp_path / "config"
    if props_toml is not None:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "general.toml").write_text(props_toml, encoding="utf-8")
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": _FakeManager(),
            "CONFIG_DIR": config_dir,
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
            "ROOM_LOG_EXPORT_DIR": tmp_path / "exports",
        }
    )


def _login(client, user_id=1, username="owner", role="USER"):
    with client.session_transaction() as session:
        session.update(
            user_id=user_id,
            username=username,
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def _headers():
    return {"X-CSRF-Token": "csrf"}


def _api_room(tmp_path) -> str:
    room_id = "r-props"
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "道具测试房",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "owner", "role": "USER", "status": "active",
             "is_active": True, "room_role": "owner"},
            {"user_id": 2, "username": "player2", "role": "USER", "status": "active",
             "is_active": True, "room_role": "member"},
            {"user_id": 3, "username": "player3", "role": "USER", "status": "active",
             "is_active": True, "room_role": "member"},
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (room_dir / "messages.json").write_text("[]", encoding="utf-8")
    return room_id


def _create_body(**overrides):
    body = {
        "template_id": "letter-1920",
        "title": "一封信",
        "fields": {"salutation": "致 何清越", "body": "钥匙藏在书房。", "signature": "冯亦阳"},
        "wear": {"stain": 0.2},
        "redaction": [{"field": "body", "start": 0, "end": 2, "mode": "burn"}],
    }
    body.update(overrides)
    return body


def _create(client, room_id):
    response = client.post(
        f"/api/rooms/{room_id}/props", json=_create_body(), headers=_headers()
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def test_templates_endpoint_lists_two(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client, user_id=2, username="player2")
    response = client.get(f"/api/rooms/{room_id}/props/templates")
    assert response.status_code == 200
    ids = {item["id"] for item in response.get_json()["data"]["templates"]}
    assert {"letter-1920", "newspaper"} <= ids


def test_player_cannot_see_hidden_prop(tmp_path):
    room_id = _api_room(tmp_path)
    owner = _api_app(tmp_path).test_client()
    _login(owner, user_id=1, username="owner")
    created = _create(owner, room_id)

    player = _api_app(tmp_path).test_client()
    _login(player, user_id=2, username="player2")
    single = player.get(f"/api/rooms/{room_id}/props/{created['id']}")
    assert single.status_code == 404
    assert single.get_json()["error"] == "prop.not_found"
    listing = player.get(f"/api/rooms/{room_id}/props").get_json()["data"]["props"]
    assert listing == []


def test_partial_reveal_only_listed_player(tmp_path):
    room_id = _api_room(tmp_path)
    owner = _api_app(tmp_path).test_client()
    _login(owner, user_id=1, username="owner")
    created = _create(owner, room_id)
    reveal = owner.post(
        f"/api/rooms/{room_id}/props/{created['id']}/reveal",
        json={"to": [2]},
        headers=_headers(),
    )
    assert reveal.status_code == 200

    player2 = _api_app(tmp_path).test_client()
    _login(player2, user_id=2, username="player2")
    data = player2.get(f"/api/rooms/{room_id}/props/{created['id']}").get_json()["data"]
    assert data["redacted"] is True
    assert data["fields"]["body"].startswith("██")

    player3 = _api_app(tmp_path).test_client()
    _login(player3, user_id=3, username="player3")
    assert player3.get(f"/api/rooms/{room_id}/props/{created['id']}").status_code == 404


def test_reveal_all_writes_prop_message(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)
    created = _create(client, room_id)
    response = client.post(
        f"/api/rooms/{room_id}/props/{created['id']}/reveal",
        json={"all": True, "message": "他递来一封信。"},
        headers=_headers(),
    )
    assert response.status_code == 200
    messages = json.loads((tmp_path / "rooms" / room_id / "messages.json").read_text(encoding="utf-8"))
    assert messages[-1]["type"] == "prop"
    assert messages[-1]["content"] == "他递来一封信。"
    assert messages[-1]["metadata"]["prop_id"] == created["id"]


def test_player_cannot_write(tmp_path):
    room_id = _api_room(tmp_path)
    owner = _api_app(tmp_path).test_client()
    _login(owner)
    created = _create(owner, room_id)

    player = _api_app(tmp_path).test_client()
    _login(player, user_id=2, username="player2")
    assert player.post(
        f"/api/rooms/{room_id}/props", json=_create_body(), headers=_headers()
    ).status_code == 403
    assert player.patch(
        f"/api/rooms/{room_id}/props/{created['id']}", json={"title": "改"}, headers=_headers()
    ).status_code == 403
    assert player.delete(
        f"/api/rooms/{room_id}/props/{created['id']}", headers=_headers()
    ).status_code == 403


def test_render_endpoint_png_and_permissions(tmp_path):
    _font_path()
    room_id = _api_room(tmp_path)
    owner = _api_app(tmp_path).test_client()
    _login(owner)
    created = _create(owner, room_id)

    player = _api_app(tmp_path).test_client()
    _login(player, user_id=2, username="player2")
    denied = player.get(f"/api/rooms/{room_id}/props/{created['id']}/render?format=png")
    assert denied.status_code == 404

    owner.post(
        f"/api/rooms/{room_id}/props/{created['id']}/reveal",
        json={"all": True},
        headers=_headers(),
    )
    response = player.get(f"/api/rooms/{room_id}/props/{created['id']}/render?format=png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_disabled_blocks_everything(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path, props_toml="[props]\nenabled = false\n").test_client()
    _login(client)
    response = client.get(f"/api/rooms/{room_id}/props/templates")
    assert response.status_code == 403
    assert response.get_json()["error"] == "prop.disabled"


def test_redact_and_patch(tmp_path):
    room_id = _api_room(tmp_path)
    client = _api_app(tmp_path).test_client()
    _login(client)
    created = _create(client, room_id)

    redact = client.post(
        f"/api/rooms/{room_id}/props/{created['id']}/redact",
        json={"field": "signature", "start": 0, "end": 3, "mode": "ink"},
        headers=_headers(),
    )
    assert redact.status_code == 200
    assert len(redact.get_json()["data"]["redaction"]) == 2

    patch = client.patch(
        f"/api/rooms/{room_id}/props/{created['id']}",
        json={"title": "改名后的信", "wear": {"stain": 0.9}},
        headers=_headers(),
    )
    assert patch.status_code == 200
    data = patch.get_json()["data"]
    assert data["title"] == "改名后的信"
    assert data["wear"]["stain"] == 0.9


# ---------------------------------------------------------------- AI 工具（P4）


def _tool_context(tmp_path, *, max_props: int = 200, max_field_chars: int = 4000):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "general.toml").write_text(
        "[props]\nenabled = true\n"
        f"max_props_per_room = {max_props}\nmax_field_chars = {max_field_chars}\n",
        encoding="utf-8",
    )
    room_dir = tmp_path / "rooms" / "r-props"
    room_dir.mkdir(parents=True)
    info = {
        "id": "r-props",
        "name": "道具测试房",
        "creator_id": 1,
        "members": [
            {"user_id": 1, "username": "owner", "is_active": True, "room_role": "owner"},
            {"user_id": 2, "username": "player2", "is_active": True, "room_role": "member"},
        ],
    }
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (room_dir / "messages.json").write_text("[]", encoding="utf-8")
    return AgentRequestContext(
        room_id="r-props",
        room_dir=room_dir,
        config_dir=config_dir,
        user_id=1,
    )


def _tool_create_args(**overrides):
    args = {
        "template_id": "letter-1920",
        "title": "一封密信",
        "fields": {
            "salutation": "致 何清越",
            "body": "钥匙藏在书房。",
            "signature": "冯亦阳",
        },
    }
    args.update(overrides)
    return args


def test_tool_create_defaults_hidden(tmp_path):
    context = _tool_context(tmp_path)
    result = execute_create_prop(_tool_create_args(), context)
    assert result["ok"] is True
    assert result["visibility"] == "hidden"
    assert result["prop_id"].startswith("prop-")
    assert "仅 KP 可见" in result["summary"]
    assert "props.reveal" in result["summary"]
    spec = store.read_prop(context.room_dir, result["prop_id"])
    assert spec["visibility"] == {"mode": "hidden", "revealed_to": []}
    assert spec["source"]["kind"] == "ai"


def test_tool_create_unknown_template_lists_available(tmp_path):
    context = _tool_context(tmp_path)
    result = execute_create_prop(_tool_create_args(template_id="nope"), context)
    assert result["ok"] is False
    assert "letter-1920" in result["error"] and "newspaper" in result["error"]


def test_tool_create_truncates_and_reports(tmp_path):
    # max_field_chars 的配置下限是 100（config/schema.py），用 150 字触发截断。
    context = _tool_context(tmp_path, max_field_chars=100)
    result = execute_create_prop(_tool_create_args(fields={"body": "字" * 150}), context)
    assert result["ok"] is True
    assert result["truncated"]["body"]["limit"] == 100
    assert "截断" in result["summary"] and "body(150→100)" in result["summary"]
    spec = store.read_prop(context.room_dir, result["prop_id"])
    assert len(spec["fields"]["body"]) == 100


def test_tool_create_with_reveal_to_all(tmp_path):
    context = _tool_context(tmp_path)
    result = execute_create_prop(_tool_create_args(reveal_to="all"), context)
    assert result["ok"] is True
    assert result["visibility"] == "revealed"
    assert "已投递给全体成员" in result["summary"]
    messages = json.loads((context.room_dir / "messages.json").read_text(encoding="utf-8"))
    assert messages[-1]["type"] == "prop"
    assert messages[-1]["metadata"]["prop_id"] == result["prop_id"]


def test_tool_single_create_per_turn(tmp_path):
    context = _tool_context(tmp_path)
    assert execute_create_prop(_tool_create_args(), context)["ok"] is True
    second = execute_create_prop(_tool_create_args(title="第二封"), context)
    assert second["ok"] is False
    assert "本回合" in second["error"]


def test_tool_room_limit_refuses(tmp_path):
    context = _tool_context(tmp_path, max_props=1)
    assert execute_create_prop(_tool_create_args(), context)["ok"] is True
    # 单回合配额优先于房间上限，重置计数模拟下一回合，才能测到上限文案。
    context.tool_state["props_created"] = 0
    second = execute_create_prop(_tool_create_args(title="第二封"), context)
    assert second["ok"] is False
    assert "上限" in second["error"]


def test_tool_missing_prop_text(tmp_path):
    context = _tool_context(tmp_path)
    missing = "prop-1787137557179-zzzzzz"
    assert execute_reveal_prop({"prop_id": missing, "to": "all"}, context)["error"] == "该道具不存在或不可见。"
    assert execute_update_prop({"prop_id": missing, "fields": {"body": "x"}}, context)["error"] == "该道具不存在或不可见。"
    assert execute_reveal_prop({"prop_id": "../../etc", "to": "all"}, context)["error"] == "该道具不存在或不可见。"


def test_tool_update_fields_wear_and_redaction(tmp_path):
    context = _tool_context(tmp_path)
    created = execute_create_prop(_tool_create_args(), context)
    prop_id = created["prop_id"]
    result = execute_update_prop(
        {
            "prop_id": prop_id,
            "fields": {"signature": "冯亦阳 绝笔"},
            "wear": {"stain": 2, "burn": 0.4},
            "add_redaction": [{"field": "body", "start": 0, "end": 2, "mode": "burn"}],
        },
        context,
    )
    assert result["ok"] is True
    assert result["redaction_count"] == 1
    spec = store.read_prop(context.room_dir, prop_id)
    assert spec["fields"]["signature"] == "冯亦阳 绝笔"
    assert spec["wear"]["stain"] == 1.0 and spec["wear"]["burn"] == 0.4
    assert spec["redaction"][0]["field"] == "body"


def test_tool_reveal_partial_recipients(tmp_path):
    context = _tool_context(tmp_path)
    created = execute_create_prop(_tool_create_args(), context)
    result = execute_reveal_prop(
        {"prop_id": created["prop_id"], "to": [2, 99], "message": "他递来一封信。"},
        context,
    )
    assert result["ok"] is True
    spec = store.read_prop(context.room_dir, created["prop_id"])
    assert spec["visibility"] == {"mode": "partial", "revealed_to": [2]}
    messages = json.loads((context.room_dir / "messages.json").read_text(encoding="utf-8"))
    assert messages[-1]["content"] == "他递来一封信。"
    assert messages[-1]["metadata"]["revealed_to"] == [2]


def test_prompt_directive_only_revealed_and_redacted(tmp_path):
    context = _tool_context(tmp_path)
    settings = {"enabled": True}
    templates = registry.list_templates()
    hidden = execute_create_prop(_tool_create_args(), context)
    hidden_spec = store.read_prop(context.room_dir, hidden["prop_id"])

    directive = props_prompt.build_props_directive(settings, templates, [hidden_spec])
    assert "钥匙藏在书房" not in directive
    assert hidden_spec["title"] not in directive
    assert "letter-1920" in directive and "私人书信" in directive

    execute_reveal_prop({"prop_id": hidden["prop_id"], "to": "all"}, context)
    revealed_spec = store.read_prop(context.room_dir, hidden["prop_id"])
    revealed_spec["redaction"] = [{"field": "body", "start": 0, "end": 2, "mode": "burn"}]
    store.write_prop(context.room_dir, revealed_spec)

    directive = props_prompt.build_props_directive(settings, templates, [revealed_spec])
    expected = policy.apply_redaction(revealed_spec, mask=policy.AI_MASK)["fields"]["body"]
    assert expected in directive
    assert "钥匙" not in directive
    assert "不得编造" in directive
    assert "████" in directive


def test_prompt_directive_disabled_is_empty(tmp_path):
    assert props_prompt.build_props_directive({"enabled": False}, registry.list_templates(), []) == ""


# ---------------------------------------------------------------- P5：AI 起草 / 备道具 / 生图


def test_draft_fields_normalizes_and_keeps_prompt_small():
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return (
            "```json\n"
            '{"title":"烧焦的密信","fields":{"body":"钥匙藏在书房。","salutation":"致 何清越",'
            '"signature":"冯亦阳"},"wear":{"stain":2,"tear":-1},'
            '"style":{"paper":"aged","font":"serif"},"extra":"ignored"}\n```'
        )

    result = props_draft.draft_fields(
        "letter-1920",
        {"era": "1926", "place": "上海", "clues": "钥匙藏在书房", "tone": "克制"},
        request=fake,
    )
    assert result["fields"]["body"] == "钥匙藏在书房。"
    assert "extra" not in result["fields"]
    assert result["wear"] == {"stain": 1.0, "tear": 0.0}
    assert result["style"]["paper"] == "aged"
    assert len(calls) == 1
    # 只带模板 schema 与简短上下文，不包含整份剧本。
    assert "letter-1920" in calls[0] and "钥匙藏在书房" in calls[0]
    assert len(calls[0]) < 3000


def test_draft_fields_retries_once_then_succeeds():
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "这不是 JSON"
        return '{"fields":{"body":"第二次成功。"},"wear":{},"style":{}}'

    result = props_draft.draft_fields("letter-1920", request=fake)
    assert result["fields"]["body"] == "第二次成功。"
    assert len(calls) == 2
    assert "上一次输出不是合法 JSON" in calls[1]


def test_draft_fields_fails_after_single_retry():
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return "仍然不是 JSON"

    with pytest.raises(props_draft.DraftError):
        props_draft.draft_fields("letter-1920", request=fake)
    assert len(calls) == 2


def test_prepare_scenario_props_writes_pack(tmp_path):
    scenario = {
        "id": 42,
        "modules": [
            {"id": "clue-1", "title": "烧焦的信", "module_type": "clue", "summary": "半封信", "content": "钥匙在书房"},
            {"id": "scene-1", "title": "书房", "module_type": "scene", "content": "不应被映射"},
        ],
    }
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(
            {
                "props": [
                    {
                        "clue_id": "clue-1",
                        "template_id": "letter-1920",
                        "title": "烧焦的信",
                        "fields": {"body": "钥匙在书房。"},
                        "wear": {"burn": 0.8},
                    },
                    {"clue_id": "clue-1", "template_id": "nope", "fields": {}},
                ]
            },
            ensure_ascii=False,
        )

    result = props_prepare.prepare_scenario_props(
        scenario,
        scenario_dir=tmp_path,
        settings={"enabled": True, "auto_prepare_on_import": True},
        request=fake,
    )
    assert result["ok"] is True
    assert result["count"] == 1 and result["skipped"] == 1
    assert "scene-1" not in calls[0]
    pack = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    spec = pack["props"][0]
    assert spec["source"]["kind"] == "imported"
    assert spec["source"]["clue_id"] == "clue-1"
    assert spec["visibility"] == {"mode": "hidden", "revealed_to": []}
    assert spec["wear"]["burn"] == 0.8


def test_prepare_scenario_props_disabled_and_failure(tmp_path):
    scenario = {"id": 1, "modules": [{"id": "c1", "module_type": "clue", "content": "x"}]}
    disabled = props_prepare.prepare_scenario_props(
        scenario,
        scenario_dir=tmp_path,
        settings={"enabled": True, "auto_prepare_on_import": False},
        request=lambda prompt: "{}",
    )
    assert disabled["count"] == 0
    assert not (tmp_path / "prop-packs").exists()

    failed = props_prepare.prepare_scenario_props(
        scenario,
        scenario_dir=tmp_path,
        settings={"enabled": True, "auto_prepare_on_import": True},
        request=lambda prompt: "不是 JSON",
    )
    assert failed["ok"] is False and failed["count"] == 0


def _photo_spec(**overrides):
    spec = {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": "r-props",
        "template_id": "photo",
        "title": "旧照片",
        "fields": {"scene": "码头边的两个人", "caption": "民国十五年", "back_text": "勿念。", "date": "1926"},
        "style": {},
        "wear": {},
        "redaction": [],
        "visibility": {"mode": "hidden", "revealed_to": []},
        "assets": {},
    }
    spec.update(overrides)
    return spec


def test_photo_template_blank_frame_and_asset():
    spec = _photo_spec()
    blank = fill_page_html(spec, "photo")
    assert "photo-frame" in blank
    assert "photo_img" not in blank and "<img" not in blank

    spec["assets"] = {"photo": f"props/{spec['id']}/photo.png"}
    with_photo = fill_page_html(spec, "photo")
    assert 'src="photo.png"' in with_photo


def test_photo_generation_budget_and_hash_reuse(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    spec = _photo_spec()
    store.write_prop(room_dir, spec)
    calls: list[str] = []

    def fake_generate(prompt: str, platform):
        calls.append(prompt)
        return b"\x89PNG\r\n\x1a\nfake", "png"

    kwargs = {
        "settings": {"image_daily_budget": 1},
        "usage_file": tmp_path / "usage.json",
        "platform": {"id": "fake"},
        "generate": fake_generate,
    }
    first = props_image.ensure_photo(room_dir, spec, **kwargs)
    assert first["ok"] is True and first["cached"] is False
    stored = store.read_prop(room_dir, spec["id"])
    assert stored["assets"]["photo"] == f"props/{spec['id']}/photo.png"
    assert (room_dir / "props" / spec["id"] / "photo.png").is_file()

    second = props_image.ensure_photo(room_dir, stored, **kwargs)
    assert second["ok"] is True and second["cached"] is True
    assert len(calls) == 1

    # 换一个画面描述会重新消耗预算；预算只有 1 张，因此降级而不是继续生成。
    changed = {**stored, "fields": {**stored["fields"], "scene": "雨夜的码头"}}
    third = props_image.ensure_photo(room_dir, changed, **kwargs)
    assert third["ok"] is False
    assert third["reason"] == "daily_budget_exhausted"
    assert len(calls) == 1
    # 降级后不再引用旧图，渲染回落为空白照片框。
    assert not (store.read_prop(room_dir, spec["id"])["assets"] or {}).get("photo")


_TEMPLATE_SAMPLES = {
    "salutation": "致 何清越",
    "body": "见字如面。码头的事我已听说，明日午后在书房等你。",
    "signature": "冯亦阳",
    "date": "民国十五年三月",
    "postscript": "阅后即焚。",
    "city": "上海",
    "issue": "第一百二十期",
    "headline": "码头疑案再起波澜",
    "col1": "本埠新闻：十六铺码头昨夜发生一起失踪案。",
    "col2": "巡捕房已介入调查，尚未公布更多细节。",
    "ad": "本埠绸缎庄新到货品。",
    "struck": "今天本不该去码头。",
    "agency": "淞沪警察厅",
    "ref": "C-1926-041",
    "summary": "关于码头失踪案的初步调查摘要。",
    "stamp": "淞沪警察厅",
    "route": "上海 — 南京",
    "seat": "二等 12 号",
    "time": "1926 年 4 月 3 日 19:40",
    "serial": "A-004182",
    "fare": "大洋 2 元",
    "note": "单程",
    "shop": "老凤祥银楼",
    "items": "银镯一只 12 元\n长命锁一件 6 元",
    "total": "大洋 18 元",
    "case_no": "E-1926-019",
    "location": "十六铺码头三号仓库",
    "officer": "巡捕房 陈国梁",
    "description": "带血迹的男式手套。",
    "guest": "沈先生",
    "occasion": "谨定于四月三日假座一品香饭店举行订婚仪式，恭请光临。",
    "place": "一品香饭店二楼",
    "recipient": "上海 法租界 霞飞路 何公馆",
    "sender": "冯",
    "party_a": "冯亦阳",
    "party_b": "何清越",
    "terms": "第一条 甲方须于四月三日前交付货品。\n第二条 乙方须一次付清款项。",
    "signature_a": "冯亦阳",
    "signature_b": "何清越",
    "seal": "冯记",
    "name": "冯亦阳",
    "title": "沪上进出口行 经理",
    "line": "明晚八点，老地方见。",
    "scene": "码头边的两个人",
    "caption": "民国十五年",
    "back_text": "勿念。",
}

_EXPECTED_TEMPLATE_ORDER = [
    "letter-1920",
    "diary-page",
    "note",
    "newspaper",
    "telegram",
    "invitation",
    "dossier",
    "ticket-stub",
    "receipt",
    "evidence-tag",
    "photo",
    "contract",
]


def _sample_spec(template_id: str, wear: dict[str, float] | None = None) -> dict:
    manifest = registry.get_template(template_id)
    fields = {
        field["key"]: _TEMPLATE_SAMPLES.get(field["key"], f"[{field['key']}]")
        for field in manifest.get("fields") or []
    }
    return {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": "",
        "template_id": template_id,
        "title": manifest.get("name") or template_id,
        "fields": fields,
        "style": {},
        "wear": wear or {},
        "redaction": [],
        "assets": {},
    }


def test_all_twelve_templates_render_without_tofu_or_runaway_pages(tmp_path):
    manifests = registry.list_templates()
    assert [manifest["id"] for manifest in manifests] == _EXPECTED_TEMPLATE_ORDER
    for manifest in manifests:
        template_id = manifest["id"]
        result = render_prop(_sample_spec(template_id), tmp_path / f"{template_id}.pdf")
        assert 1 <= result["pages"] <= 3, f"{template_id} 页数异常：{result['pages']}"
        text = "".join(page.get_text() for page in pymupdf.open(result["path"]))
        assert text.strip(), f"{template_id} 没有可提取文本"
        assert "\ufffd" not in text, f"{template_id} 出现方块字"


def test_template_groups_are_stable_and_cover_all_templates():
    groups = registry.list_template_groups()
    assert [group["name"] for group in groups] == ["书信日记", "报刊公告", "档案凭证", "影像契约"]
    covered = [template_id for group in groups for template_id in group["template_ids"]]
    assert covered == _EXPECTED_TEMPLATE_ORDER


def test_wear_strength_changes_output(tmp_path):
    checked = 0
    for template_id in ("letter-1920", "diary-page", "receipt"):
        manifest = registry.get_template(template_id)
        wear_keys = manifest.get("wear_supported") or ["stain"]
        weak = {key: 0.05 for key in wear_keys}
        strong = {key: 0.95 for key in wear_keys}
        assert fill_page_html(_sample_spec(template_id, weak), template_id) != fill_page_html(
            _sample_spec(template_id, strong), template_id
        )
        weak_bytes = render_prop(_sample_spec(template_id, weak), tmp_path / f"{template_id}-weak.pdf")["bytes"]
        strong_bytes = render_prop(_sample_spec(template_id, strong), tmp_path / f"{template_id}-strong.pdf")["bytes"]
        assert weak_bytes != strong_bytes
        checked += 1
    assert checked == 3


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@pytest.fixture
def user_templates(tmp_path, monkeypatch):
    base = tmp_path / "user_templates"
    base.mkdir()
    monkeypatch.setattr(registry, "USER_TEMPLATES_DIR", base)
    registry.rescan()
    try:
        yield base
    finally:
        monkeypatch.undo()
        registry.rescan()


def _write_user_template(base, template_id, *, page_html=None, css=None, manifest=None, assets=None):
    root = base / template_id
    root.mkdir(parents=True)
    payload = {
        "id": template_id,
        "name": f"用户模板 {template_id}",
        "order": 900,
        "group": "user",
        "group_name": "用户模板",
        "fields": [{"key": "body", "label": "正文", "type": "textarea", "max": 200, "required": True}],
    }
    if manifest:
        payload.update(manifest)
    (root / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (root / "page.html").write_text(
        page_html or '<div class="user-note"><p>{{body}}</p></div>',
        encoding="utf-8",
    )
    (root / "style.css").write_text(css or "body { color: #222; }", encoding="utf-8")
    if assets:
        asset_dir = root / "assets"
        asset_dir.mkdir()
        for name, content in assets.items():
            (asset_dir / name).write_bytes(content)
    return root


def test_user_template_listed_with_source_and_renders(user_templates, tmp_path):
    _write_user_template(user_templates, "user-note", assets={"dot.png": _TINY_PNG})
    manifests = {item["id"]: item for item in registry.list_templates()}
    assert manifests["user-note"]["source"] == "user"
    assert manifests["letter-1920"]["source"] == "builtin"
    assert registry.template_dir("user-note") == user_templates / "user-note"

    spec = {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": "",
        "template_id": "user-note",
        "title": "用户便条",
        "fields": {"body": "这是一张用户模板。"},
        "style": {},
        "wear": {},
        "redaction": [],
        "assets": {},
    }
    result = render_prop(spec, tmp_path / "user-note.pdf")
    assert result["pages"] == 1


def test_user_template_cannot_override_builtin(user_templates):
    _write_user_template(user_templates, "letter-1920", manifest={"name": "伪造书信"})
    manifests = {item["id"]: item for item in registry.list_templates()}
    assert manifests["letter-1920"]["source"] == "builtin"
    assert manifests["letter-1920"]["name"] == "私人书信"
    reasons = [item for item in registry.list_unavailable() if item["id"] == "letter-1920"]
    assert reasons and "冲突" in reasons[0]["reason"]


def test_user_template_unsafe_html_and_assets_are_isolated(user_templates):
    _write_user_template(
        user_templates,
        "evil-script",
        page_html='<div><script>alert(1)</script><p>{{body}}</p></div>',
    )
    _write_user_template(
        user_templates,
        "evil-asset",
        assets={"fake.png": b"not an image"},
    )
    _write_user_template(user_templates, "good-user")
    ids = registry.available_ids()
    assert "evil-script" not in ids and "evil-asset" not in ids
    assert "good-user" in ids
    assert any("禁止使用 <script>" in item["reason"] for item in registry.list_unavailable())
    assert any("不是有效的 png/jpg" in item["reason"] for item in registry.list_unavailable())


def test_user_template_rescan_picks_up_new_template(user_templates):
    assert "late-user" not in registry.available_ids()
    _write_user_template(user_templates, "late-user")
    result = registry.rescan()
    assert "late-user" in registry.available_ids()
    assert result["templates"] >= 13


def test_photo_generation_platform_failure_degrades(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    spec = _photo_spec()
    store.write_prop(room_dir, spec)

    def boom(prompt: str, platform):
        raise RuntimeError("upstream down")

    result = props_image.ensure_photo(
        room_dir,
        spec,
        settings={"image_daily_budget": 5},
        usage_file=tmp_path / "usage.json",
        platform={"id": "fake"},
        generate=boom,
    )
    assert result["ok"] is False and result["degraded"] is True
    assert "upstream down" in result["reason"]
