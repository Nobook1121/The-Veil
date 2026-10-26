import json
from pathlib import Path

import pymupdf

from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.tools.trigger import reveal_scenario_trigger
from trpg_server.agents.trigger_system import (
    delete_resource,
    load_resources,
    normalize_resource,
    persist_uploaded_resource,
    replace_uploaded_resource,
    resource_identifier,
    resource_matches,
    validate_trigger,
)


class Upload:
    filename = "clue.txt"
    mimetype = "text/plain"

    def __init__(self, data=b"secret"):
        self.data = data

    def read(self):
        return self.data


def _scenario(tmp_path: Path):
    resource = persist_uploaded_resource(tmp_path, 10, Upload(), "A secret clue")
    return {
        "id": 10,
        "scenario_version": "1.0.0",
        "trigger_cards": [{
            "id": "trg-1",
            "scriptId": "10",
            "scriptVersion": "1.0.0",
            "cardType": "trigger",
            "sceneId": "scene-1",
            "spoilerLevel": 1,
            "visibility": "player_visible",
            "attachments": [{
                "triggerId": "trg-1",
                "resourceRef": resource,
                "condition": {"type": "clue_found", "clueId": "clue-1"},
                "spoilerLevel": 1,
                "visibility": "player_visible",
                "repeatable": False,
                "enabled": True,
            }],
        }],
    }


def test_trigger_validation_enforces_condition_and_repeatability(tmp_path):
    scenario = _scenario(tmp_path)
    state = {"scenario_version": "1.0.0", "active_scene_id": "scene-1", "clues": []}
    assert validate_trigger("trg-1", state, scenario)["reason"] == "condition_not_met"
    state["clues"] = ["clue-1"]
    assert validate_trigger("trg-1", state, scenario)["ok"] is True
    state["triggeredFiles"] = ["trg-1"]
    assert validate_trigger("trg-1", state, scenario)["reason"] == "already_triggered"


def test_trigger_validation_rejects_wrong_script_version(tmp_path):
    scenario = _scenario(tmp_path)
    state = {"scenario_version": "2.0.0", "active_scene_id": "scene-1", "clues": ["clue-1"]}
    assert validate_trigger("trg-1", state, scenario)["reason"] == "scenario_version_mismatch"


def _library_scenario() -> dict:
    """触发器资源库（trigger_cards）场景，无场景栏位触发器。"""
    return {
        "id": 10,
        "scenario_version": "1.0.0",
        "modules": [],
        "trigger_cards": [
            {
                "id": "trg-1",
                "scriptId": "10",
                "scriptVersion": "1.0.0",
                "cardType": "trigger",
                "text": "图书馆线索",
                "attachments": [
                    {
                        "triggerId": "trg-1",
                        "resourceRef": {
                            "type": "richtext",
                            "path": "assets/clue.txt",
                            "url": "/assets/scenarios/scenario-10/assets/clue.txt",
                            "alt": "线索",
                            "mime": "text/plain",
                            "size": 6,
                            "hash": "abc123",
                            "content": "秘密线索内容",
                        },
                        "condition": {"type": "custom"},
                        "spoilerLevel": 1,
                        "visibility": "player_visible",
                        "repeatable": False,
                        "enabled": True,
                    }
                ],
            }
        ],
    }


def _write_library_scenario(scenarios_dir: Path) -> None:
    scenario_dir = scenarios_dir / "scenario-10"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text(
        json.dumps(_library_scenario(), ensure_ascii=False), encoding="utf-8"
    )


def _library_context(tmp_path: Path) -> AgentRequestContext:
    scenarios_dir = tmp_path / "scenarios"
    _write_library_scenario(scenarios_dir)
    room_dir = tmp_path / "rooms" / "room-1"
    room_dir.mkdir(parents=True)
    (room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "scenario_id": 10, "scenario_version": "1.0.0"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (room_dir / "state.json").write_text(json.dumps({"active_scene_id": None}), encoding="utf-8")
    return AgentRequestContext(room_id="room-1", room_dir=room_dir, scenarios_dir=scenarios_dir, agent_id="kp")


def test_trigger_tool_reveals_library_trigger_card(tmp_path):
    # KP 通过工具应能揭示「触发器资源库」中的卡片，而不仅是场景栏位触发器。
    context = _library_context(tmp_path)

    result = reveal_scenario_trigger({"trigger_id": "trg-1"}, context)

    assert result.get("triggered") is True
    assert result["direct_message"]["content"] == "秘密线索内容"


def test_trigger_tool_rejects_unapproved_library_trigger(tmp_path):
    # 资源库触发器同样受剧本版本校验约束，不能绕过规则任意揭示。
    context = _library_context(tmp_path)
    (context.room_dir / "info.json").write_text(
        json.dumps({"id": "room-1", "scenario_id": 10, "scenario_version": "2.0.0"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = reveal_scenario_trigger({"trigger_id": "trg-1"}, context)

    assert "error" in result
    assert result.get("reason") == "scenario_version_mismatch"


def _png_bytes(width: int = 800, height: int = 600) -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.clear_with(120)
    return pixmap.tobytes("png")


class ImageUpload(Upload):
    filename = "battlemap.png"
    mimetype = "image/png"


def test_image_upload_generates_server_thumbnail(tmp_path):
    # §8.5：图片上传时生成长边 640 的缩略图，并把尺寸写回资源引用。
    ref = persist_uploaded_resource(tmp_path, 11, ImageUpload(_png_bytes()), "战场地图")

    assert (ref["width"], ref["height"]) == (800, 600)
    assert ref["thumb_path"] == f"assets/{ref['hash'][:16]}_thumb.jpg"
    root = tmp_path / "scenario-11"
    assert (root / ref["thumb_path"]).is_file()

    stored = load_resources(tmp_path, 11)[0]
    assert stored["thumb_path"] == ref["thumb_path"]
    assert stored["width"] == 800


def test_normalize_resource_keeps_derived_fields_and_rejects_unsafe_paths(tmp_path):
    ref = persist_uploaded_resource(tmp_path, 11, ImageUpload(_png_bytes()), "战场地图")

    normalized = normalize_resource({
        **ref,
        "thumb_path": "../escape.jpg",
        "poster_path": "assets/poster.jpg",
        "duration_ms": 1200,
    })

    assert "thumb_path" not in normalized
    assert normalized["poster_path"] == "assets/poster.jpg"
    assert normalized["duration_ms"] == 1200
    assert normalized["width"] == 800


def test_delete_resource_cleans_derived_thumbnail(tmp_path):
    ref = persist_uploaded_resource(tmp_path, 11, ImageUpload(_png_bytes()), "战场地图")
    root = tmp_path / "scenario-11"
    original = root / ref["path"]
    thumb = root / ref["thumb_path"]
    assert original.is_file() and thumb.is_file()

    assert delete_resource(tmp_path, 11, ref["hash"]) is True

    assert not original.exists()
    assert not thumb.exists()


def test_replace_resource_keeps_asset_id_and_cleans_old_files(tmp_path):
    # §8.6：替换文件保留 asset_id，避免触发器引用失效；旧文件与派生图一并清理。
    first = persist_uploaded_resource(tmp_path, 12, ImageUpload(_png_bytes()), "旧图")
    assert first["thumb_path"]
    second = ImageUpload(_png_bytes(200, 200))
    second.filename = "new.png"

    replaced = replace_uploaded_resource(tmp_path, 12, first["asset_id"], second)

    assert replaced["asset_id"] == first["asset_id"]
    assert replaced["hash"] != first["hash"]
    assert (replaced["width"], replaced["height"]) == (200, 200)
    root = tmp_path / "scenario-12"
    assert not (root / first["path"]).exists()
    assert not (root / first["thumb_path"]).exists()
    assert (root / replaced["path"]).is_file()
    stored = load_resources(tmp_path, 12)
    assert len(stored) == 1
    assert resource_identifier(stored[0]) == first["asset_id"]


def test_resource_matches_accepts_legacy_hash_and_asset_id(tmp_path):
    ref = persist_uploaded_resource(tmp_path, 13, Upload(), "线索")
    assert resource_matches(ref, ref["hash"])
    assert resource_matches(ref, ref["asset_id"])
    assert resource_matches(ref, resource_identifier(ref))
