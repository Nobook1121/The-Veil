import base64
import json
import zlib
from pathlib import Path

from trpg_server.app_factory import create_app


class FakeSessionUserManager:
    def __init__(self, roles=None):
        self.roles = roles or {}

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = self.roles.get(int(user_id), "USER")
        return {"id": user_id, "username": f"user{user_id}", "role": role}


def _character_payload(character_id: str, player_id: str) -> dict:
    return {
        "id": character_id,
        "name": "测试调查员",
        "playerId": player_id,
        "deriveAttributes": {
            "sanity": {"current": "40", "start": "50", "max": "50"},
            "hp": {"current": "9", "max": "10"},
            "mp": {"current": "10", "max": "10"},
        },
        "battleAttributes": {"db": "0", "build": "0", "mov": "8", "armor": "0"},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
        "attributes": {
            "str": 50,
            "dex": 50,
            "con": 50,
            "app": 50,
            "pow": 50,
            "siz": 50,
            "edu": 50,
            "int": 50,
            "luc": 50,
        },
        "skillGroups": {"explore": [{"name": "侦查", "base": 25, "job": 10, "interest": 5, "growth": 0}]},
        "stories": {"app": "留着风衣", "desc": "一名记者"},
        "assets": {"cash": "120", "consumption": "10", "assets": "", "items": ""},
        "weapons": [{"name": "旧式手枪", "skill": "射击(手枪)", "damage": "1D10"}],
        "friends": "[]",
        "experiencedModules": "[]",
        "createdAt": "2026-11-01T00:00:00.000Z",
        "updatedAt": "2026-11-02T00:00:00.000Z",
    }


def _create_app(tmp_path: Path, characters_dir: Path, roles=None):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "CHARACTER_GALLERY_DIR": tmp_path / "character_gallery",
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": characters_dir,
            "ROOMS_DIR": tmp_path / "rooms",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
            "WEAPONS_DIR": tmp_path / "weapons",
            "USER_MANAGER": FakeSessionUserManager(roles),
        }
    )


def _login(client, user_id: int, username: str, role: str = "USER"):
    with client.session_transaction() as session:
        session["user_id"] = str(user_id)
        session["username"] = username
        session["role"] = role
        session["session_token"] = "token"


def _prepare_character(characters_dir: Path, character_id: str, player_id: str) -> Path:
    card_dir = characters_dir / character_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "character.json").write_text(
        json.dumps(_character_payload(character_id, player_id), ensure_ascii=False),
        encoding="utf-8",
    )
    (card_dir / "weapons.json").write_text(
        json.dumps({"version": 1, "weapons": [{"name": "自制撬棍", "damage": "1D6"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_dir / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return card_dir


def test_export_returns_full_card_and_zlib_base64_text(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-1001", "7")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")

    response = client.get("/api/characters/investigator-1001/export")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True

    bundle = payload["data"]
    assert bundle["filename"].startswith("测试调查员-")
    json_text = bundle["jsonText"]
    exported = json.loads(json_text)
    assert exported["id"] == "investigator-1001"
    assert exported["name"] == "测试调查员"
    assert exported["currentSan"] == 40
    assert exported["maxSan"] == 50
    assert exported["skills"][0]["name"] == "侦查"
    assert exported["skills"][0]["value"] == 40
    assert exported["weapons"][0]["name"] == "旧式手枪"
    assert exported["customWeapons"][0]["name"] == "自制撬棍"
    assert exported["avatarData"].startswith("data:image/png;base64,")
    assert base64.b64decode(exported["avatarData"].split(",", 1)[1]) == b"\x89PNG\r\n\x1a\nfake"

    # 分享文本串是裸的 zlib + base64，解码后与下载用的 JSON 文本完全一致。
    encoded = bundle["encoded"]
    assert encoded.startswith("eN")
    assert not encoded.startswith("{")
    assert zlib.decompress(base64.b64decode(encoded)).decode("utf-8") == json_text


def test_export_rejects_other_users_card(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-1002", "7")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 8, "stranger")

    response = client.get("/api/characters/investigator-1002/export")
    assert response.status_code == 403


def test_export_allows_admin_and_requires_login(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-1003", "7")
    app = _create_app(tmp_path, characters_dir, roles={1: "ADMIN"})

    anonymous = app.test_client()
    response = anonymous.get("/api/characters/investigator-1003/export")
    assert response.status_code == 401

    admin = app.test_client()
    _login(admin, 1, "admin", role="ADMIN")
    response = admin.get("/api/characters/investigator-1003/export")
    assert response.status_code == 200
    assert response.get_json()["data"]["jsonText"]


def test_export_missing_card_returns_404(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")

    response = client.get("/api/characters/not-exist/export")
    assert response.status_code == 404
