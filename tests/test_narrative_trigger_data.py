"""触发器数据层：新旧数据归一化与旧格式适配。"""
from __future__ import annotations

from trpg_server.agents.narrative.triggers import (
    adapt_triggers,
    asset_from_resource_ref,
    normalize_asset,
    normalize_trigger_spec,
)


def test_normalize_trigger_spec_full_and_defaults() -> None:
    spec = normalize_trigger_spec({
        "id": "trg_note",
        "label": "便签",
        "binding": {"kind": "interaction", "object_id": "obj_note", "result": "success"},
        "condition": {"flag": "read_letter", "value": True},
        "attachments": [{"id": "att_note", "kind": "richtext", "body": "内容", "order": 2}],
        "group": "study",
        "visibility": "kp_only",
        "spoiler_level": 9,
        "once": False,
        "cooldown": 2,
        "priority": 5,
    }, index=0)
    assert spec is not None
    assert spec["binding"]["kind"] == "interaction"
    assert spec["binding"]["result"] == "success"
    assert spec["attachments"][0]["kind"] == "richtext"
    assert spec["attachments"][0]["body"] == "内容"
    assert spec["visibility"] == "kp_only"
    assert spec["spoiler_level"] == 5
    assert spec["once"] is False
    assert spec["cooldown"] == 2
    assert spec["enabled"] is True


def test_normalize_trigger_spec_rejects_unknown_binding() -> None:
    assert normalize_trigger_spec({"id": "bad", "binding": {"kind": "teleport"}}) is None
    assert normalize_trigger_spec({"id": "bad2"}) is None


def test_structured_triggers_win_over_legacy_modules() -> None:
    scenario = {
        "id": "s1",
        "triggers": [{
            "id": "trg_new",
            "label": "新触发器",
            "binding": {"kind": "enter", "location_id": "room_b"},
            "attachments": [{"id": "att_new", "kind": "richtext", "body": "新正文"}],
        }],
        "modules": [{
            "id": "m1", "scene_id": "room_a",
            "triggers": [{"id": "trg_old", "type": "scene_enter"}],
        }],
    }
    result = adapt_triggers(scenario)
    assert [item["id"] for item in result["triggers"]] == ["trg_new"]


def test_legacy_module_trigger_types_map_to_bindings() -> None:
    scenario = {
        "id": "s1",
        "modules": [{
            "id": "m1", "scene_id": "room_a",
            "triggers": [
                {"id": "t1", "type": "scene_enter", "display_name": "进入"},
                {"id": "t2", "type": "clue_found", "clueId": "clue_1"},
                {"id": "t3", "type": "npc_dialogue", "npcId": "npc_1"},
                {"id": "t4", "type": "event_triggered", "eventId": "event_1"},
                {"id": "t5", "type": "player_action", "keyword": "敲门"},
                {"id": "t6", "type": "custom", "condition": "自然语言条件"},
            ],
        }],
    }
    result = adapt_triggers(scenario)
    specs = {item["id"]: item for item in result["triggers"]}
    assert specs["t1"]["binding"] == {"kind": "enter", "location_id": "room_a"}
    assert specs["t2"]["binding"] == {"kind": "clue", "clue_id": "clue_1"}
    assert specs["t3"]["binding"] == {"kind": "npc", "npc_id": "npc_1", "event": "dialogue"}
    assert specs["t4"]["binding"] == {"kind": "event", "event_id": "event_1"}
    assert specs["t5"]["binding"] == {"kind": "manual", "keyword": "敲门"}
    assert specs["t6"]["binding"]["kind"] == "manual"
    assert specs["t6"]["needs_review"] is True
    assert any(item["code"] == "trigger.review_needed" for item in result["warnings"])


def test_legacy_text_and_image_content_become_attachments() -> None:
    scenario = {
        "id": "s1",
        "modules": [{
            "id": "m1", "scene_id": "room_a",
            "triggers": [
                {"id": "text", "type": "scene_enter", "content_mode": "text", "content": "你看到一行字。"},
                {
                    "id": "image", "type": "scene_enter", "content_mode": "image",
                    "asset_url": "http://example.test/a.png", "asset_name": "照片",
                },
            ],
        }],
    }
    result = adapt_triggers(scenario)
    specs = {item["id"]: item for item in result["triggers"]}
    assert specs["text"]["content"] == "你看到一行字。"
    assert specs["text"]["attachments"] == []
    assert specs["image"]["attachments"][0]["kind"] == "image"
    assert specs["image"]["attachments"][0]["asset_id"]
    assert any(item["kind"] == "image" for item in result["assets"])


def test_legacy_trigger_card_conditions_merge() -> None:
    scenario = {
        "id": "s1",
        "trigger_cards": [{
            "id": "card_1",
            "text": "卡片",
            "visibility": "player_visible",
            "attachments": [
                {
                    "triggerId": "card_1_a",
                    "resourceRef": {"type": "image", "url": "http://example.test/b.png", "alt": "图"},
                    "condition": {"type": "scene_enter", "sceneId": "room_a"},
                },
                {
                    "triggerId": "card_1_b",
                    "resourceRef": {"type": "file", "url": "http://example.test/c.pdf"},
                    "condition": {"type": "clue_found", "clueId": "clue_1"},
                },
            ],
        }],
    }
    result = adapt_triggers(scenario)
    spec = result["triggers"][0]
    assert spec["binding"]["kind"] == "manual"
    assert spec["condition"] == {"all": [{"type": "location", "id": "room_a"}, {"clue": "clue_1"}]}
    assert len(spec["attachments"]) == 2
    assert spec["needs_review"] is True


def test_asset_from_resource_ref_is_content_addressed() -> None:
    asset = asset_from_resource_ref({
        "type": "image",
        "url": "http://example.test/a.png",
        "hash": "abcdef0123456789deadbeef",
        "size": 12,
        "mime": "image/png",
    })
    assert asset is not None
    assert asset["id"] == "asset_abcdef0123456789"
    assert asset["kind"] == "image"
    assert asset["bytes"] == 12


def test_normalize_asset_guesses_kind_from_url() -> None:
    asset = normalize_asset({"url": "http://example.test/video.mp4"})
    assert asset is not None
    assert asset["kind"] == "video"
