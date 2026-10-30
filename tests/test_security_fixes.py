"""P0-P3 安全修复的回归测试：静态路径、Socket 授权、登录退避、模拟会话、上传校验等。"""

import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from trpg_server.agents.narrative.coerce import as_bool, as_str_list, as_str_list_or_scalar
from trpg_server.app_factory import create_app, socketio
from trpg_server.security import build_impersonation_token, impersonation_token_is_valid, session_is_current
from trpg_server.setup_embedding import EmbeddingSetupError, _extract_zip
from trpg_server.users.database import UserDatabase
from trpg_server.users.service import LOGIN_FAILURE_LIMIT, UserService


class FakeSessionUserManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": f"user{user_id}", "role": "USER"}


def _make_app(tmp_path: Path, **overrides):
    base = {
        "TESTING": True,
        "SECRET_KEY": "test",
        "USERS_DIR": tmp_path / "users",
        "LOGS_DIR": tmp_path / "logs",
        "CONFIG_DIR": tmp_path / "config",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOMS_DIR": tmp_path / "rooms",
        "OCCUPATIONS_DIR": tmp_path / "occupations",
        "WEAPONS_DIR": tmp_path / "weapons",
        "USER_MANAGER": FakeSessionUserManager(),
    }
    base.update(overrides)
    return create_app(base)


def _login_client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_id"] = str(user_id)
        flask_session["username"] = f"user{user_id}"
        flask_session["role"] = "USER"
        flask_session["session_token"] = f"token{user_id}"
        flask_session["csrf_token"] = "csrf"
    return client


def _write_room(rooms_dir: Path, room_id: str, member_ids: list[int]):
    room_dir = rooms_dir / room_id
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps(
            {
                "id": room_id,
                "name": room_id,
                "members": [
                    {"user_id": str(member_id), "username": f"user{member_id}", "status": "active", "is_active": True}
                    for member_id in member_ids
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- P0 /config


def test_config_static_path_cannot_bypass_blocklist(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text("auth_token = 'secret'", encoding="utf-8")
    (config_dir / "public.txt").write_text("ok", encoding="utf-8")
    app = _make_app(tmp_path, CONFIG_DIR=config_dir)
    client = app.test_client()

    assert client.get("/config/general.toml").status_code == 404
    assert client.get("/config/./general.toml").status_code == 404
    assert client.get("/config/General.TOML").status_code == 404
    assert client.get("/config//general.toml").status_code == 404
    assert b"auth_token" not in client.get("/config/general.toml").data

    assert client.get("/config/public.txt").status_code == 200


# --------------------------------------------------------- H2 角色数据静态目录


def test_character_data_only_serves_images(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    (characters_dir / "c1").mkdir(parents=True)
    (characters_dir / "c1" / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nxxx")
    (characters_dir / "c1" / "character.json").write_text('{"secret": true}', encoding="utf-8")
    (characters_dir / "investigator-1.json").write_text('{"secret": true}', encoding="utf-8")
    app = _make_app(tmp_path, CHARACTERS_DIR=characters_dir)
    client = app.test_client()

    assert client.get("/data/characters/c1/avatar.png").status_code == 200
    assert client.get("/data/characters/c1/character.json").status_code == 404
    assert client.get("/data/characters/investigator-1.json").status_code == 404


# ---------------------------------------------------------------- H1 Socket 授权


def test_socket_join_requires_room_membership(tmp_path: Path):
    app = _make_app(tmp_path)
    _write_room(tmp_path / "rooms", "r-secret", [2, 3])
    outsider = _login_client(app, 1)
    socket_client = socketio.test_client(app, flask_test_client=outsider)
    assert socket_client.is_connected()

    socket_client.emit("join_room", {"room_id": "r-secret"})
    received = socket_client.get_received()
    assert [item for item in received if item["name"] == "room_error"]

    # 非成员即使携带 room_id 也不能把消息投递到房间。
    member = _login_client(app, 2)
    member_socket = socketio.test_client(app, flask_test_client=member)
    member_socket.emit("join_room", {"room_id": "r-secret"})
    member_socket.get_received()

    socket_client.emit("send_message", {"room_id": "r-secret", "text": "spam"})
    assert [item for item in member_socket.get_received() if item["name"] == "new_message"] == []

    # 缺失 room_id 时不再广播到所有在线用户。
    socket_client.emit("send_message", {"text": "broadcast-spam"})
    assert [item for item in member_socket.get_received() if item["name"] == "new_message"] == []

    socket_client.disconnect()
    member_socket.disconnect()


def test_socket_members_can_exchange_messages(tmp_path: Path):
    app = _make_app(tmp_path)
    _write_room(tmp_path / "rooms", "r-open", [2, 3])
    sender = socketio.test_client(app, flask_test_client=_login_client(app, 2))
    receiver = socketio.test_client(app, flask_test_client=_login_client(app, 3))
    sender.emit("join_room", {"room_id": "r-open"})
    receiver.emit("join_room", {"room_id": "r-open"})
    sender.get_received()
    receiver.get_received()

    sender.emit("send_message", {"room_id": "r-open", "text": "hello"})
    messages = [item for item in receiver.get_received() if item["name"] == "new_message"]
    assert messages and messages[0]["args"][0]["text"] == "hello"

    sender.disconnect()
    receiver.disconnect()


# ---------------------------------------------------------------- M1 登录退避


def _make_user_service(tmp_path: Path) -> UserService:
    database = UserDatabase(tmp_path / "users.db")
    database.initialize()
    return UserService(database, ip_config_dir=tmp_path / "ip_configs")


def test_login_backoff_after_repeated_failures(tmp_path: Path):
    service = _make_user_service(tmp_path)
    ok, _, _ = service.register("alice", "Passw0rd!", "alice@example.com", True)
    assert ok

    for _ in range(LOGIN_FAILURE_LIMIT):
        ok, message, _, _ = service.login("alice", "wrong-password")
        assert not ok
        assert "Too many" not in message

    ok, message, _, _ = service.login("alice", "wrong-password")
    assert not ok
    assert "Too many failed attempts" in message


def test_login_success_clears_failure_counter(tmp_path: Path):
    service = _make_user_service(tmp_path)
    assert service.register("bob", "Passw0rd!", "bob@example.com", True)[0]

    for _ in range(LOGIN_FAILURE_LIMIT - 1):
        service.login("bob", "wrong-password")

    ok, _, _, token = service.login("bob", "Passw0rd!")
    assert ok and token
    assert service._login_retry_after("bob") == 0


# ---------------------------------------------------------------- M3 模拟会话


def test_impersonation_token_binds_initiator_target_and_ttl():
    token = build_impersonation_token(1, 2)
    assert impersonation_token_is_valid(token, initiator_id=1, target_id=2)
    assert not impersonation_token_is_valid(token, initiator_id=2, target_id=2)
    assert not impersonation_token_is_valid(token, initiator_id=1, target_id=1)

    expired = f"impersonation:1:2:{int(time.time()) - 1}:nonce"
    assert not impersonation_token_is_valid(expired, initiator_id=1, target_id=2)
    assert not impersonation_token_is_valid("impersonation:1:2", initiator_id=1, target_id=2)


def test_session_is_current_rejects_tampered_impersonation(tmp_path: Path):
    app = _make_app(tmp_path)
    token = build_impersonation_token(1, 2)
    with app.test_request_context():
        from flask import session

        session["user_id"] = "2"
        session["impersonation_mode"] = True
        session["impersonation_original_user_id"] = "1"
        session["session_token"] = token
        assert session_is_current()

        session["impersonation_original_user_id"] = "3"
        assert not session_is_current()


# ---------------------------------------------------------------- M4 CORS


def test_cors_default_is_same_origin(tmp_path: Path):
    app = _make_app(tmp_path)
    response = app.test_client().get("/api/setup/status", headers={"Origin": "http://evil.example"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_cors_uses_configured_origin_whitelist(tmp_path: Path):
    app = _make_app(tmp_path, CORS_ALLOWED_ORIGINS=["http://allowed.example"])
    response = app.test_client().get("/api/setup/status", headers={"Origin": "http://allowed.example"})
    assert response.headers["Access-Control-Allow-Origin"] == "http://allowed.example"


# ---------------------------------------------------------------- L1 上传与解压


def test_image_upload_requires_matching_magic_bytes(tmp_path: Path):
    app = _make_app(tmp_path, CHAT_UPLOAD_DIR=tmp_path / "uploads")
    client = _login_client(app, 1)

    rejected = client.post(
        "/api/assets/upload",
        data={"file": (io.BytesIO(b"<?php echo 1;"), "fake.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert rejected.status_code == 400

    accepted = client.post(
        "/api/assets/upload",
        data={"file": (io.BytesIO(b"\x89PNG\r\n\x1a\npayload"), "real.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert accepted.status_code == 201


def test_zip_extraction_rejects_oversized_archive(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("trpg_server.setup_embedding.EXTRACT_MAX_BYTES", 10)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("model.bin", b"x" * 20)
    buffer.seek(0)

    with pytest.raises(EmbeddingSetupError, match="体积超过上限"):
        _extract_zip(buffer, tmp_path / "model")


def test_narrative_coerce_helpers():
    assert as_bool("yes") is True
    assert as_bool("no") is False
    assert as_str_list([" a ", "a", "", None]) == ["a"]
    assert as_str_list("a") == []
    assert as_str_list_or_scalar("x") == ["x"]
