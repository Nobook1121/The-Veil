import json
import re
from pathlib import Path


from trpg_server.app_factory import create_app


class FakeSessionUserManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


def test_gallery_listing_renormalizes_public_ids(tmp_path: Path):
    gallery_dir = tmp_path / "character_gallery"
    gallery_dir.mkdir()
    (gallery_dir / "legacy.json").write_text(
        json.dumps(
            {
                "id": "legacy",
                "name": "旧广场角色",
                "public_id": "legacy-public-id",
                "playerId": "",
                "occupationName": "记者",
                "attributes": {"STR": 50, "DEX": 50, "SIZ": 50, "APP": 50, "CON": 50, "INT": 50, "POW": 50, "EDU": 50, "LUC": 50, "AGE": 25},
                "maxHp": 10,
                "currentHp": 10,
                "maxSan": 50,
                "initialSan": 50,
                "currentSan": 50,
                "currentMp": 10,
                "maxMp": 10,
                "magicPoints": 10,
                "damageBonus": "0",
                "build": 0,
                "mov": 8,
                "armor": 0,
                "status": {},
                "occupationSkillPoints": 0,
                "personalInterestPoints": 0,
                "skillSuccessLimits": {"occupation": 75, "other": 50},
                "skills": [],
                "weapons": [],
                "equipment": [],
                "assets": {"cash": 0, "spendingLevel": 0, "assetsText": ""},
                "background": {"appearance": "", "ideology": "", "significantPeople": "", "meaningfulLocations": "", "treasuredPossessions": "", "traits": "", "injuriesScars": "", "phobiasManias": "", "arcaneTomes": "", "spells": "", "encounters": "", "story": "", "education": "", "raceType": "人类"},
                "relationships": [],
                "experiencedScenarios": [],
                "createdAt": "2026-08-20T00:00:00.000Z",
                "updatedAt": "2026-08-20T00:00:00.000Z",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "CHARACTER_GALLERY_DIR": gallery_dir,
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
    )

    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "1"
        session["username"] = "tester"
        session["role"] = "ADMIN"
        session["session_token"] = "token"

    response = client.get("/api/character-gallery")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert len(payload["data"]) == 1
    public_id = payload["data"][0]["public_id"]
    assert re.fullmatch(r"[A-Za-z0-9]{6}", public_id)
    stored = json.loads((gallery_dir / "legacy.json").read_text(encoding="utf-8"))
    assert stored["public_id"] == public_id


class GalleryUserManager:
    """会话校验时按用户 ID 返回角色，避免所有测试账号都被当成管理员。"""

    def __init__(self, roles):
        self.roles = roles

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        key = str(user_id)
        return {"id": key, "username": "tester" if key == "1" else f"user{key}", "role": self.roles.get(key, "USER")}


def _make_gallery_app(tmp_path: Path):
    gallery_dir = tmp_path / "character_gallery"
    gallery_dir.mkdir()
    (gallery_dir / "abc123.json").write_text(
        json.dumps(
            {
                "id": "abc123",
                "public_id": "abc123",
                "publisher_id": "1",
                "publisher_name": "tester",
                "name": "广场角色",
                "attributes": {"STR": 50, "DEX": 50, "SIZ": 50, "APP": 50, "CON": 50, "INT": 50, "POW": 50, "EDU": 50, "LUC": 50, "AGE": 25},
                "maxHp": 10,
                "currentHp": 10,
                "maxSan": 50,
                "initialSan": 50,
                "currentSan": 50,
                "currentMp": 10,
                "maxMp": 10,
                "skills": [{"name": "侦查", "value": 60}],
                "weapons": [],
                "equipment": [],
                "status": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "CHARACTER_GALLERY_DIR": gallery_dir,
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOMS_DIR": tmp_path / "rooms",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
            "WEAPONS_DIR": tmp_path / "weapons",
            "USER_MANAGER": GalleryUserManager({"1": "USER", "3": "ADMIN"}),
        }
    )


def _login(client, user_id: str, role: str = "USER"):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["username"] = "tester" if user_id == "1" else "other"
        session["role"] = role
        session["session_token"] = "token"


def test_gallery_export_returns_same_bundle_as_personal_export(tmp_path: Path):
    app = _make_gallery_app(tmp_path)
    client = app.test_client()
    _login(client, "1")

    response = client.get("/api/character-gallery/abc123/export")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    bundle = payload["data"]
    assert bundle["filename"].endswith(".json")
    assert json.loads(bundle["jsonText"])["name"] == "广场角色"
    assert bundle["encoded"]

    _login(client, "2")
    assert client.get("/api/character-gallery/abc123/export").status_code == 403

    _login(client, "3", role="ADMIN")
    assert client.get("/api/character-gallery/abc123/export").status_code == 200
    assert client.get("/api/character-gallery/missing/export").status_code == 404
