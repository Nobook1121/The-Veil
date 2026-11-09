import base64
import json
import zlib
from pathlib import Path

from trpg_server import character_card_io
from trpg_server.app_factory import create_app


class FakeSessionUserManager:
    def __init__(self, roles=None):
        self.roles = roles or {}

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        role = self.roles.get(int(user_id), "USER")
        return {"id": user_id, "username": f"user{user_id}", "role": role}


def _character_payload(character_id: str, player_id: str, name: str = "测试调查员") -> dict:
    return {
        "id": character_id,
        "name": name,
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


def _create_app(tmp_path: Path, characters_dir: Path):
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
            "USER_MANAGER": FakeSessionUserManager(),
        }
    )


def _login(client, user_id: int, username: str):
    with client.session_transaction() as session:
        session["user_id"] = str(user_id)
        session["username"] = username
        session["role"] = "USER"
        session["session_token"] = "token"
        session["csrf_token"] = "csrf"


def _prepare_character(characters_dir: Path, character_id: str, player_id: str, name: str = "测试调查员") -> Path:
    card_dir = characters_dir / character_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "character.json").write_text(
        json.dumps(_character_payload(character_id, player_id, name), ensure_ascii=False),
        encoding="utf-8",
    )
    (card_dir / "weapons.json").write_text(
        json.dumps({"version": 1, "weapons": [{"name": "自制撬棍", "damage": "1D6"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_dir / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return card_dir


def _export_bundle(client, character_id: str) -> dict:
    response = client.get(f"/api/characters/{character_id}/export")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _import(client, payload: str, mode: str = "auto"):
    return client.post(
        "/api/characters/import",
        json={"payload": payload, "mode": mode},
        headers={"X-CSRF-Token": "csrf"},
    )


def _card_dirs(characters_dir: Path) -> list[str]:
    if not characters_dir.exists():
        return []
    return sorted(path.name for path in characters_dir.iterdir() if path.is_dir())


def test_import_json_creates_new_card_with_avatar_and_custom_weapons(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-source", "7")
    app = _create_app(tmp_path, characters_dir)

    owner = app.test_client()
    _login(owner, 7, "owner")
    bundle = _export_bundle(owner, "investigator-source")

    importer = app.test_client()
    _login(importer, 8, "importer")
    response = _import(importer, bundle["jsonText"])
    assert response.status_code == 201, response.get_json()
    result = response.get_json()["data"]
    imported = result["character"]
    assert result["renamed"] is False
    assert imported["id"] != "investigator-source"
    assert imported["name"] == "测试调查员"
    assert str(imported["playerId"]) == "8"
    assert imported["playerName"] == "user8"
    assert imported["currentSan"] == 40
    assert imported["maxSan"] == 50
    assert imported["skills"][0]["name"] == "侦查"
    assert imported["skills"][0]["value"] == 40
    assert imported["weapons"][0]["name"] == "旧式手枪"

    # 头像与自定义武器库要在新卡目录中真实落盘。
    new_dir = characters_dir / imported["id"]
    assert (new_dir / "avatar.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    weapons = json.loads((new_dir / "weapons.json").read_text(encoding="utf-8"))
    assert weapons["weapons"][0]["name"] == "自制撬棍"

    # 源卡完全不受影响。
    assert (characters_dir / "investigator-source" / "character.json").exists()
    listed = importer.get("/api/characters").get_json()["data"]
    assert {card["id"] for card in listed} == {imported["id"]}


def test_import_share_text_roundtrip(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-source", "7")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")
    bundle = _export_bundle(client, "investigator-source")

    response = _import(client, bundle["encoded"], mode="text")
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]["character"]
    assert imported["name"] == "测试调查员（2）"
    assert response.get_json()["data"]["renamed"] is True


def test_import_duplicate_name_does_not_overwrite(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-1", "7", name="同名调查员")
    _prepare_character(characters_dir, "investigator-2", "7", name="同名调查员")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")
    bundle = _export_bundle(client, "investigator-1")

    response = _import(client, bundle["jsonText"])
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]["character"]
    assert imported["name"] == "同名调查员（2）"
    assert (characters_dir / "investigator-1" / "character.json").exists()
    assert (characters_dir / "investigator-2" / "character.json").exists()


def test_import_legacy_test_shape_is_supported(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")

    legacy = _character_payload("legacy-id", "7", name="旧格式角色")
    response = _import(client, json.dumps(legacy, ensure_ascii=False))
    assert response.status_code == 201, response.get_json()
    imported = response.get_json()["data"]["character"]
    assert imported["name"] == "旧格式角色"
    assert imported["id"] != "legacy-id"
    assert imported["currentSan"] == 40
    assert imported["maxHp"] == 10


def test_import_rejects_bad_payloads_without_writing(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-source", "7")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")
    bundle = _export_bundle(client, "investigator-source")
    before = _card_dirs(characters_dir)

    # 非 base64。
    response = _import(client, "这不是分享串!!")
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_share_text"

    # 截断的 zlib 数据。
    truncated = bundle["encoded"][: max(8, len(bundle["encoded"]) // 2)]
    response = _import(client, truncated)
    assert response.status_code == 400
    assert response.get_json()["error"] in {"truncated_share_text", "invalid_share_text"}

    # 合法 base64 但不是 zlib。
    response = _import(client, base64.b64encode(b"not zlib at all").decode("ascii"))
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_share_text"

    # JSON 语法错误。
    response = _import(client, '{"name": "x",')
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_json"

    # 类型不符。
    broken = json.loads(bundle["jsonText"])
    broken["attributes"]["STR"] = "五十"
    response = _import(client, json.dumps(broken, ensure_ascii=False))
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_field_type"

    # 缺少必填字段。
    missing = json.loads(bundle["jsonText"])
    missing.pop("name")
    response = _import(client, json.dumps(missing, ensure_ascii=False))
    assert response.status_code == 400
    assert response.get_json()["error"] == "missing_field"

    assert _card_dirs(characters_dir) == before


def test_import_rejects_unsupported_ruleset(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    _prepare_character(characters_dir, "investigator-source", "7")
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")
    bundle = _export_bundle(client, "investigator-source")

    payload = json.loads(bundle["jsonText"])
    payload["rulesetId"] = "unknown-ruleset"
    before = _card_dirs(characters_dir)
    response = _import(client, json.dumps(payload, ensure_ascii=False))
    assert response.status_code == 400
    assert response.get_json()["error"] == "unsupported_ruleset"
    assert _card_dirs(characters_dir) == before


def test_import_rejects_decompression_bomb(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    app = _create_app(tmp_path, characters_dir)
    client = app.test_client()
    _login(client, 7, "owner")

    bomb = base64.b64encode(zlib.compress(b" " * (character_card_io.MAX_IMPORT_JSON_BYTES + 1))).decode("ascii")
    response = _import(client, bomb, mode="text")
    assert response.status_code == 413
    assert response.get_json()["error"] == "import_too_large"
    assert _card_dirs(characters_dir) == []


def test_import_requires_login(tmp_path: Path):
    characters_dir = tmp_path / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    app = _create_app(tmp_path, characters_dir)
    response = app.test_client().post("/api/characters/import", json={"payload": "{}"})
    assert response.status_code == 401
