from trpg_server.agents.structured_output import parse_kp_response, validate_state_updates, validate_structured_response


def test_parse_kp_response_accepts_fenced_json_and_normalizes_fields():
    result = parse_kp_response(
        "```json\n"
        '{"narration":"门开了","options":["进入"],"state_updates":{"items":["key"]},'
        '"next_scene":"scene-2","npc_actions":[{"id":"guard","action":"待命"}],"extra":true}'
        "\n```"
    )

    assert result is not None
    assert result.narration == "门开了"
    assert result.options == ["进入"]
    assert result.state_updates == {"items": ["key"]}
    assert result.next_scene == "scene-2"
    assert result.npc_actions == [{"id": "guard", "action": "待命"}]


def test_validate_state_updates_rejects_unknown_fields_and_unlisted_scene():
    result = validate_state_updates(
        {"items": ["key"], "admin": True, "active_scene_id": "scene-x"},
        {},
        {"scene_manifest": [{"id": "scene-1"}]},
    )

    assert result == {"items": ["key"]}


def test_validate_state_updates_rejects_entities_not_in_current_scenario():
    result = validate_state_updates(
        {"clues": ["clue-1", "clue-secret"], "items": ["item-1", "item-forged"], "npc_attitudes": {"npc-1": "friendly", "npc-x": "hostile"}},
        {},
        {
            "scene_manifest": [{"id": "scene-1"}],
            "modules": [
                {"id": "npc-1", "module_type": "npc"},
                {"id": "clue-1", "module_type": "custom", "card_type": "clue"},
                {"id": "item-1", "module_type": "custom", "card_type": "item"},
            ],
        },
    )
    assert result == {"clues": ["clue-1"], "items": ["item-1"], "npc_attitudes": {"npc-1": "friendly"}}


def test_parse_kp_response_filters_npc_actions_to_known_npcs():
    result = parse_kp_response('{"narration":"ok","npc_actions":[{"npc_id":"guard","action":"wait"},{"npc_id":"forged","action":"steal"}]}')
    validated = validate_structured_response(result, {"modules": [{"id": "guard", "module_type": "npc"}]})
    assert validated.npc_actions == [{"npc_id": "guard", "action": "wait"}]


def test_validate_state_updates_uses_compact_entity_manifest():
    result = validate_state_updates(
        {"clues": ["clue-1", "forged"], "items": ["item-1", "fake"]},
        {},
        {"entity_manifest": [{"id": "clue-1", "type": "clue"}, {"id": "item-1", "type": "item"}]},
    )
    assert result == {"clues": ["clue-1"], "items": ["item-1"]}


def test_validate_structured_response_rejects_unknown_next_scene():
    response = parse_kp_response('{"narration":"ok","next_scene":"forged"}')
    validated = validate_structured_response(response, {"scene_manifest": [{"id": "scene-1"}]})
    assert validated.next_scene is None


def test_validate_structured_response_logs_dropped_out_of_scenario_references(caplog):
    """KP 引用剧本外的场景/NPC 被丢弃时必须留痕，便于管理员发现编造。"""
    response = parse_kp_response(
        '{"narration":"ok","next_scene":"forged","npc_actions":[{"npc_id":"guard","action":"wait"},{"npc_id":"forged","action":"steal"}]}'
    )
    with caplog.at_level("WARNING"):
        validate_structured_response(response, {"scene_manifest": [{"id": "scene-1"}], "modules": [{"id": "guard", "module_type": "npc"}]})
    assert "剧本外的场景" in caplog.text
    assert "剧本外的 NPC 行动" in caplog.text


def test_validate_state_updates_logs_dropped_out_of_scenario_entities(caplog):
    """状态更新丢弃剧本外实体时必须留痕。"""
    with caplog.at_level("WARNING"):
        validate_state_updates(
            {"active_scene_id": "scene-x", "clues": ["clue-1", "forged"]},
            {},
            {"scene_manifest": [{"id": "scene-1"}], "modules": [{"id": "clue-1", "module_type": "custom", "card_type": "clue"}]},
        )
    assert "剧本外的 active_scene_id" in caplog.text
    assert "剧本外的线索/物品/NPC" in caplog.text
