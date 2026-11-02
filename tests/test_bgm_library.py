import io

from trpg_server.app_factory import create_app

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(role),
            "CONFIG_DIR": tmp_path / "config",
            "BUILTIN_BGM_DIR": tmp_path / "assets" / "bgm",
            "BGM_LIBRARY_DIR": tmp_path / "library",
        }
    )


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _write_builtin(tmp_path, name="01-calm.wav"):
    target = tmp_path / "assets" / "bgm" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(WAV_BYTES)


def test_list_tracks_includes_builtin_and_marks_management_rights(tmp_path):
    _write_builtin(tmp_path)
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()
    _login(client, role="ADMIN")

    response = client.get("/api/bgm/tracks")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_manage"] is True
    assert [track["name"] for track in data["tracks"]] == ["01-calm.wav"]
    assert data["tracks"][0]["url"] == "/assets/bgm/builtin/01-calm.wav"
    assert data["tracks"][0]["builtin"] is True


def test_list_tracks_rejects_anonymous(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    response = client.get("/api/bgm/tracks")

    assert response.status_code == 401


def test_regular_user_cannot_upload_or_delete(tmp_path):
    app = _app(tmp_path, role="USER")
    client = app.test_client()
    _login(client, role="USER")

    upload = client.post(
        "/api/bgm/tracks",
        data={"file": (io.BytesIO(WAV_BYTES), "calm.wav")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert upload.status_code == 403

    delete = client.delete("/api/bgm/tracks/calm.wav", headers={"X-CSRF-Token": "csrf"})
    assert delete.status_code == 403


def test_admin_can_upload_list_and_delete_track(tmp_path):
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()
    _login(client, role="ADMIN")

    upload = client.post(
        "/api/bgm/tracks",
        data={"file": (io.BytesIO(WAV_BYTES), "calm.wav")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert upload.status_code == 201
    track = upload.get_json()["data"]
    assert track["name"] == "calm"
    assert track["url"].startswith("/assets/bgm/library/")

    listed = client.get("/api/bgm/tracks").get_json()["data"]["tracks"]
    assert any(item["url"] == track["url"] and item["builtin"] is False for item in listed)

    filename = track["url"].rsplit("/", 1)[-1]
    delete = client.delete(f"/api/bgm/tracks/{filename}", headers={"X-CSRF-Token": "csrf"})
    assert delete.status_code == 200
    assert not (tmp_path / "library" / filename).exists()


def test_upload_rejects_unsupported_extension_and_fake_audio(tmp_path):
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()
    _login(client, role="ADMIN")

    text_file = client.post(
        "/api/bgm/tracks",
        data={"file": (io.BytesIO(b"hello"), "lyrics.txt")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert text_file.status_code == 400

    fake_audio = client.post(
        "/api/bgm/tracks",
        data={"file": (io.BytesIO(b"not a wav"), "fake.wav")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert fake_audio.status_code == 400


def test_builtin_tracks_are_not_deletable_by_design(tmp_path):
    """删除接口只作用于上传目录，内置文件即使同名也不能被删除。"""

    _write_builtin(tmp_path)
    app = _app(tmp_path, role="ADMIN")
    client = app.test_client()
    _login(client, role="ADMIN")

    delete = client.delete("/api/bgm/tracks/01-calm.wav", headers={"X-CSRF-Token": "csrf"})

    assert delete.status_code == 404
    assert (tmp_path / "assets" / "bgm" / "01-calm.wav").exists()
