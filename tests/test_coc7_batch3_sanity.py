"""CoC7 批次 3：SAN 结构化、习惯恐惧、现实认知、发作计时与恐惧表。"""

import json
import random
from pathlib import Path

from trpg_server import character_service, room_rules, room_sanity
from trpg_server.app_factory import create_app
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.ruleset import get_plugin
from trpg_server.ruleset.capabilities import sanity_of
from trpg_server.ruleset.coc7 import sanity_state, traits
from trpg_server.ruleset.coc7.options import Coc7RuleOptions
from trpg_server.room_rules import load_room_info, save_room_info

SWITCHES = {
    "coc7_sanity_state": True,
    "coc7_sanity_mythos": True,
    "coc7_sanity_habituation": True,
    "coc7_sanity_reality": True,
    "coc7_sanity_bout": True,
    "coc7_sanity_traits": True,
}


def _attributes(**overrides):
    values = {"STR": 60, "DEX": 60, "CON": 60, "APP": 50, "POW": 50, "SIZ": 70, "EDU": 60, "INT": 60, "LUC": 50}
    values.update(overrides)
    return values


def _card(card_id="card-1", name="哈维", san=50, **extra):
    card = {
        "id": card_id,
        "name": name,
        "rulesetId": "coc7",
        "attributes": _attributes(),
        "maxSan": san,
        "initialSan": san,
        "currentSan": san,
        "skills": [{"name": "侦查", "value": 60}, {"name": "克苏鲁神话", "value": 0}],
        "equipment": [],
        "weapons": [],
        "status": {},
        "armor": 0,
    }
    card.update(extra)
    return card


def _member(card=None, state=None, username="tester"):
    return {
        "user_id": 1,
        "username": username,
        "role": "USER",
        "room_role": "member",
        "status": "active",
        "is_active": True,
        "character_card": card or _card(),
        "character_state": state or {"current_san": 50, "max_san": 99, "mental_status": {}},
    }


def _info(extra=None, member=None):
    info = {"room_id": "san-batch3", "ruleset_id": "coc7", "members": [member or _member()], "house_rules": {}}
    info["house_rules"].update(SWITCHES)
    if extra:
        info["house_rules"].update(extra)
    return info


def _sanity():
    return sanity_of(get_plugin("coc7"))


def _run(words, tmp_path, rank=3):
    return handle_room_rule_command(
        words,
        room_id="san-batch3",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=rank,
    )


# ---------------------------------------------------------------------------
# 开关与旧卡兼容
# ---------------------------------------------------------------------------
def test_sanity_switches_default_off():
    options = Coc7RuleOptions.from_house_rules({})
    assert options.sanity_state is False
    assert options.sanity_mythos is False
    assert options.sanity_habituation is False
    assert options.sanity_reality is False
    assert options.sanity_bout is False
    assert options.sanity_traits is False


def test_old_card_reads_default_structure_without_writing():
    card = _card()
    state = {"current_san": 50, "max_san": 99}
    snapshot = _sanity().mental_snapshot(card, state, _info())
    assert snapshot["phobias"] == [] and snapshot["manias"] == []
    assert snapshot["bout"] == {} and snapshot["habituations"] == {}
    assert snapshot["realityImmunity"] is False
    assert "extensions" not in card  # 只读快照不污染旧卡

    block = _sanity().mental_block(card, state)
    assert card["extensions"]["mental"] is block
    assert state["mental"] is block


def test_switch_off_keeps_sanity_flow_unchanged():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    outcome = _sanity().resolve_check(
        player_name="哈维",
        card=card,
        state=state,
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="-1d4",
        failure_expr="-1d6",
        roller=lambda sides: 3,
        day="2026-11-10",
        room_info={"house_rules": {}},
        source="深潜者",
    )
    assert "mental" not in outcome
    assert "extensions" not in card
    assert outcome["delta"] == -3


# ---------------------------------------------------------------------------
# 习惯恐惧
# ---------------------------------------------------------------------------
def test_habituation_records_max_and_waives_repeat_loss():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    info = _info()
    first = _sanity().resolve_check(
        player_name="哈维",
        card=card,
        state=state,
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="1d4",
        failure_expr="1d6",
        roller=lambda sides: 3,
        day="2026-11-10",
        room_info=info,
        source="深潜者",
    )
    assert first["delta"] == -3
    assert first["habituation"]["immune"] is False
    assert state["mental"]["habituations"]["深潜者"]["max_loss"] == 3
    assert any(change["type"] == "extensions" for change in first["pending_changes"])

    second = _sanity().resolve_check(
        player_name="哈维",
        card=card,
        state=state,
        current=state["current_san"],
        maximum=99,
        roll=80,
        success=False,
        success_expr="1d4",
        failure_expr="1d6",
        roller=lambda sides: 2,
        day="2026-11-10",
        room_info=info,
        source="深潜者",
    )
    assert second["habituation"]["immune"] is True
    assert second["delta"] == 0
    assert second["new_san"] == 47
    assert "不再损失" in second["summary"]


def test_habituation_interlude_decay_and_console(tmp_path):
    member = _member(_card(), {"current_san": 50, "max_san": 99, "mental_status": {}})
    info = _info(member=member)
    save_room_info(tmp_path, info)
    ok, text = _run(["san", "habituation", "add", "哈维", "深潜者", "4"], tmp_path)
    assert ok is True, text
    assert "更新为 4" in text
    ok, text = _run(["san", "habituation", "哈维"], tmp_path)
    assert "深潜者（4" in text
    ok, text = _run(["san", "habituation", "tick", "哈维"], tmp_path)
    assert ok is True, text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["habituations"]["深潜者"]["max_loss"] == 3


# ---------------------------------------------------------------------------
# 神话 +5 / +1、发作 1D10 小时
# ---------------------------------------------------------------------------
def _madness(roller, info, card, state, loss=6):
    return _sanity().resolve_madness(
        player_name="哈维",
        card=card,
        state=state,
        arguments={"loss": loss, "cause": "目击深潜者"},
        roller=roller,
        bonus=0,
        penalty=0,
        critical_threshold=1,
        fumble_threshold=100,
        success_level=lambda roll, target, crit, fumble: "regular" if roll <= target else "failure",
        room_info=info,
    )


def test_mythos_gain_five_then_one():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    info = _info()
    roller = lambda sides: 1  # 智力成功、发作 1、时长 1 回合 1
    first = _madness(roller, info, card, state)
    assert first["insane"] is True
    assert first["mythos"]["gain"] == 5
    assert first["mythos"]["after"] == 5
    assert state["max_san"] == 94
    assert card["skills"][1]["value"] == 5

    second = _madness(roller, info, card, state)
    assert second["mythos"]["gain"] == 1
    assert second["mythos"]["after"] == 6
    assert state["mental"]["mythosMadnessCount"] == 2


def test_bout_records_1d10_hours_and_symptoms(tmp_path):
    member = _member()
    info = _info(member=member)
    save_room_info(tmp_path, info)
    info = load_room_info(tmp_path)
    member = info["members"][0]
    outcome = _madness(lambda sides: 1, info, member["character_card"], member["character_state"])
    record = outcome["bout_record"]
    assert record["duration_hours"] == 1
    assert record["expires_at"]
    assert record["symptoms"]
    assert "持续 1 小时" in record["summary"]
    # 直接调用插件不会落盘；控制台查询前需要把内存房间写回。
    save_room_info(tmp_path, info)

    ok, text = _run(["san", "bout", "哈维"], tmp_path)
    assert ok is True
    assert "进行中" in text
    ok, text = _run(["san", "bout", "end", "哈维"], tmp_path)
    assert "已结束" in text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["bout"]["active"] is False


# ---------------------------------------------------------------------------
# 现实认知
# ---------------------------------------------------------------------------
def test_reality_check_success_grants_immunity(tmp_path):
    member = _member()
    save_room_info(tmp_path, _info(member=member))
    ok, text = _run(["san", "reality", "哈维", "成功"], tmp_path)
    assert ok is True, text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["realityImmunity"] is True


def test_reality_check_failure_costs_one_san(tmp_path):
    member = _member()
    save_room_info(tmp_path, _info(member=member))
    ok, text = _run(["san", "reality", "哈维", "失败"], tmp_path)
    assert ok is True, text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_san"] == 49
    assert saved["character_card"]["extensions"]["mental"]["realityImmunity"] is False
    # 规则书：现实认知失败后立即进入发作，时长 1D10 小时。
    bout = saved["character_card"]["extensions"]["mental"]["bout"]
    assert bout["active"] is True
    assert 1 <= bout["duration_hours"] <= 10
    assert bout["symptoms"]


def test_reality_immunity_waives_next_san_loss():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    _sanity().mental_block(card, state)["realityImmunity"] = True
    outcome = _sanity().resolve_check(
        player_name="哈维",
        card=card,
        state=state,
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="-1d4",
        failure_expr="-1d6",
        roller=lambda sides: 3,
        day="2026-11-10",
        room_info=_info(),
        source="幻象",
    )
    assert outcome["reality_waived"] is True
    assert outcome["delta"] == 0
    assert outcome["new_san"] == 50
    assert state["mental"]["realityImmunity"] is False
    assert "现实认知免疫" in outcome["summary"]


def test_next_san_loss_clears_reality_immunity():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    block = _sanity().mental_block(card, state)
    block["realityImmunity"] = True
    _sanity().resolve_check(
        player_name="哈维",
        card=card,
        state=state,
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="1d4",
        failure_expr="1d4",
        roller=lambda sides: 2,
        day="2026-11-10",
        room_info=_info(),
        source="深潜者",
    )
    # mental_block 会重建 block，旧引用可能已过期，必须重新读取房间状态。
    assert state["mental"]["realityImmunity"] is False


# ---------------------------------------------------------------------------
# 100 条表与暴露惩罚骰
# ---------------------------------------------------------------------------
def test_trait_catalogs_have_one_hundred_entries():
    traits.reload()
    assert traits.count("phobia") == 100
    assert traits.count("mania") == 100


def test_trait_query_is_on_demand_and_keyword_scoped():
    payload = traits.query("phobia", "蜘蛛")
    assert payload["total"] == 100
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["name"] == "蜘蛛恐惧"


def test_trait_command_queries_table(tmp_path):
    save_room_info(tmp_path, _info())
    ok, text = _run(["san", "trait", "mania", "火"], tmp_path)
    assert ok is True
    assert "纵火癖" in text


def test_san_subcommands_are_invisible_when_switches_off(tmp_path):
    save_room_info(tmp_path, {"room_id": "san-batch3", "members": [_member()], "house_rules": {}})
    for words in (["san", "trait", "mania", "火"], ["san", "bout"], ["san", "habituation", "哈维"]):
        ok, text = _run(words, tmp_path)
        assert ok is False
        assert "纵火癖" not in text


def test_exposure_penalty_requires_matching_trait_and_switch():
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    block = _sanity().mental_block(card, state)
    sanity_state.add_trait(block, kind="phobia", name="蜘蛛恐惧", tag="spider", en="arachnophobia")
    info = _info()
    assert _sanity().fear_penalty(card, state, "巨型蜘蛛", room_info=info)["penalty"] == 1
    assert _sanity().fear_penalty(card, state, "深潜者", room_info=info)["penalty"] == 0
    off = _info({"coc7_sanity_traits": False})
    assert _sanity().fear_penalty(card, state, "蜘蛛", room_info=off)["penalty"] == 0


# ---------------------------------------------------------------------------
# 持久化链路
# ---------------------------------------------------------------------------
def _stored_card(tmp_path):
    card_dir = Path(tmp_path) / "characters" / "card-2"
    card_dir.mkdir(parents=True)
    stored = {
        "id": "card-2",
        "name": "Bob",
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": _attributes(),
        "deriveAttributes": {
            "sanity": {"start": 50, "current": 50, "max": 50},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 10, "max": 10},
        },
        "skillGroups": {},
        "equipment": [],
        "weapons": [],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
        "battleAttributes": {"armor": "0"},
    }
    path = card_dir / "character.json"
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return path


def test_extensions_round_trip_through_persistent_card(tmp_path):
    from trpg_server.site_agent_auth import Actor, permission_set_for_role

    card_path = _stored_card(tmp_path)
    app = create_app(
        {
            "TESTING": True,
            "CONFIG_DIR": Path(tmp_path) / "config",
            "ROOMS_DIR": Path(tmp_path) / "rooms",
            "CHARACTERS_DIR": Path(tmp_path) / "characters",
        }
    )
    mental = sanity_state.normalize({"phobias": [{"name": "蜘蛛恐惧", "tag": "spider"}]})
    with app.app_context():
        actor = Actor(
            user_id=1,
            username="tester",
            role="ADMIN",
            permissions=permission_set_for_role("ADMIN"),
            room_ids=frozenset(),
        )
        character_service.apply_room_changes(actor, "card-2", [{"type": "extensions", "value": {"mental": mental}}])

    saved = json.loads(card_path.read_text(encoding="utf-8"))
    assert saved["extensions"]["mental"]["phobias"][0]["name"] == "蜘蛛恐惧"
    assert saved["sheet"]["extensions"]["mental"]["phobias"][0]["name"] == "蜘蛛恐惧"


# ---------------------------------------------------------------------------
# 工具与时钟接口
# ---------------------------------------------------------------------------
def test_sanity_tools_are_registered():
    from trpg_server.agents.tools import SANITY_TOOLS

    names = {tool.name for tool in SANITY_TOOLS}
    assert names == {"san.bout", "san.habituation", "san.reality_check", "san.traits"}


def test_interlude_decay_interface_handles_all_members(tmp_path):
    first = _member(_card(name="哈维"))
    second = _member(_card(card_id="card-9", name="露娜"), {"current_san": 40, "max_san": 99, "mental_status": {}})
    info = _info(member=first)
    info["members"] = [first, second]
    for member in (first, second):
        block = _sanity().mental_block(member["character_card"], member["character_state"])
        sanity_state.note_habituation(block, "深潜者", 5)
    save_room_info(tmp_path, info)
    payload = room_sanity.interlude_decay(tmp_path, load_room_info(tmp_path))
    assert len(payload["members"]) == 2
    saved = load_room_info(tmp_path)
    for member in saved["members"]:
        assert member["character_card"]["extensions"]["mental"]["habituations"]["深潜者"]["max_loss"] == 4


def test_random_trait_uses_full_table():
    picked = traits.random_entry("phobia", lambda sides: sides)
    assert picked["id"] == 100
    rng = random.Random(7)
    picked = traits.random_entry("mania", lambda sides: rng.randint(1, sides))
    assert 1 <= picked["id"] <= 100
