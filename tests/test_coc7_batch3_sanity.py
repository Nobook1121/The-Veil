"""CoC7 批次 3：SAN 结构化、习惯恐惧、现实认知、发作计时与恐惧表。"""

import json
import random
import time
from datetime import datetime
from pathlib import Path

from trpg_server import character_service, room_clock, room_rules, room_sanity
from trpg_server.app_factory import create_app
from trpg_server.room_console_commands import execute_room_command
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
def test_habituation_accumulates_until_cap():
    """规则书：同一来源累计损失，上限为该存在单次遭遇的最大损失。"""
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
    assert first["habituation"]["cap"] == 6
    assert first["habituation"]["applied"] == 3
    assert state["mental"]["habituations"]["深潜者"]["total_loss"] == 3
    assert state["mental"]["habituations"]["深潜者"]["max_loss"] == 6
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
    # 累计 3 + 2 = 5，仍未达上限 6，本次照常损失。
    assert second["habituation"]["immune"] is False
    assert second["habituation"]["applied"] == 2
    assert second["delta"] == -2
    assert state["mental"]["habituations"]["深潜者"]["total_loss"] == 5


def test_habituation_rulebook_example_cap_immune_and_decay():
    """累计 6 免疫；幕间 −1 回到 5；再遇损失 1 时只扣 1。"""
    block = sanity_state.default_block()
    assert sanity_state.note_habituation(block, "深潜者", 4, max_loss=6)["applied"] == 4
    assert sanity_state.note_habituation(block, "深潜者", 2, max_loss=6)["applied"] == 2
    third = sanity_state.note_habituation(block, "深潜者", 3, max_loss=6)
    assert third["immune"] is True
    assert third["applied"] == 0
    assert block["habituations"]["深潜者"]["total_loss"] == 6

    sanity_state.decay_habituations(block)
    assert block["habituations"]["深潜者"]["total_loss"] == 5
    assert block["habituations"]["深潜者"]["max_loss"] == 6
    again = sanity_state.note_habituation(block, "深潜者", 1, max_loss=6)
    assert again["immune"] is False
    assert again["applied"] == 1
    assert block["habituations"]["深潜者"]["total_loss"] == 6


def test_habituation_interlude_decay_and_console(tmp_path):
    member = _member(_card(), {"current_san": 50, "max_san": 99, "mental_status": {}})
    info = _info(member=member)
    save_room_info(tmp_path, info)
    ok, text = _run(["san", "habituation", "add", "哈维", "深潜者", "4", "6"], tmp_path)
    assert ok is True, text
    assert "4/6" in text
    ok, text = _run(["san", "habituation", "哈维"], tmp_path)
    assert "深潜者（累计 4/6" in text
    ok, text = _run(["san", "habituation", "tick", "哈维"], tmp_path)
    assert ok is True, text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["habituations"]["深潜者"]["total_loss"] == 3
    assert saved["character_card"]["extensions"]["mental"]["habituations"]["深潜者"]["max_loss"] == 6


# ---------------------------------------------------------------------------
# 神话 +5 / +1、发作时长（即时 1D10 轮 / 总结 1D10 小时）
# ---------------------------------------------------------------------------
def _madness(roller, info, card, state, loss=6, **extra):
    arguments = {"loss": loss, "cause": "目击深潜者"}
    arguments.update(extra)
    return _sanity().resolve_madness(
        player_name="哈维",
        card=card,
        state=state,
        arguments=arguments,
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


def test_bout_records_duration_by_mode(tmp_path):
    member = _member()
    info = _info(member=member)
    save_room_info(tmp_path, info)
    info = load_room_info(tmp_path)
    member = info["members"][0]
    immediate = _madness(lambda sides: 1, info, member["character_card"], member["character_state"])
    record = immediate["bout_record"]
    assert immediate["bout_mode"] == "immediate"
    assert record["mode"] == "immediate"
    assert record["duration_rounds"] == 1
    assert record["expires_at"] == ""  # 即时症状按战斗轮推进，没有挂钟结束时间
    assert record["symptoms"]
    assert "持续 1 轮" in record["summary"]
    # 直接调用插件不会落盘；控制台查询前需要把内存房间写回。
    save_room_info(tmp_path, info)

    ok, text = _run(["san", "bout", "哈维"], tmp_path)
    assert ok is True
    assert "进行中" in text and "持续 1 轮" in text
    ok, text = _run(["san", "bout", "end", "哈维"], tmp_path)
    assert "已结束" in text
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["bout"] == {}

    # 总结症状：1D10 小时，带挂钟结束时间。
    info = load_room_info(tmp_path)
    member = info["members"][0]
    summary = _madness(
        lambda sides: 1,
        info,
        member["character_card"],
        member["character_state"],
        bout_mode="summary",
    )
    record = summary["bout_record"]
    assert summary["bout_mode"] == "summary"
    assert record["mode"] == "summary"
    assert record["duration_hours"] == 1
    assert record["expires_at"]
    assert "持续 1 小时" in record["summary"]


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
    # 规则书：现实认知失败后立即进入发作；默认是即时症状（1D10 战斗轮）。
    bout = saved["character_card"]["extensions"]["mental"]["bout"]
    assert bout["active"] is True
    assert bout["mode"] == "immediate"
    assert 1 <= bout["duration_rounds"] <= 10
    assert bout["expires_at"] == ""
    assert bout["symptoms"]


def test_reality_immunity_waives_illusion_loss_only():
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
        illusion=True,
    )
    assert outcome["reality_waived"] is True
    assert outcome["delta"] == 0
    assert outcome["new_san"] == 50
    # 本次损失被免疫抵消，并未真正失去理智值，因此抗性延续到下一次真实损失。
    assert state["mental"]["realityImmunity"] is True
    assert "现实认知免疫" in outcome["summary"]


def test_reality_immunity_does_not_block_real_loss():
    """规则书：免疫只针对幻觉，真实怪物造成的损失照常结算。"""
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
        source="深潜者",
    )
    assert "reality_waived" not in outcome
    assert outcome["delta"] == -3
    assert state["mental"]["realityImmunity"] is False


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


def test_reality_check_rolls_current_san_not_pow(tmp_path):
    """规则书 p.135 原文是“投一个理智”，因此目标值取当前 SAN。"""
    member = _member(_card(), {"current_san": 10, "max_san": 99, "mental_status": {}})
    save_room_info(tmp_path, _info(member=member))
    payload = room_sanity.reality_check(tmp_path, _info(member=member), member, rng=lambda sides: 11)
    assert payload["check"]["name"] == "SAN"
    assert payload["check"]["target"] == 10
    assert payload["result"] == "failure"
    assert payload["san_loss"] == 1
    # 若误用 POW 50，掷 11 会成功；这里失败证明取的是 SAN。


def test_habituation_absorbed_loss_keeps_reality_immunity():
    """被习惯恐惧完全吸收的损失没有真正失去理智值，不应消耗现实抗性。"""
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    block = _sanity().mental_block(card, state)
    block["realityImmunity"] = True
    sanity_state.note_habituation(block, "深潜者", 6, max_loss=6)
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
        source="深潜者",
    )
    assert outcome["habituation"]["applied"] == 0
    assert outcome["delta"] == 0
    assert state["mental"]["realityImmunity"] is True


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


def test_exposure_penalty_requires_exact_trait_and_switch():
    """规则书由 KP 判断直接暴露，因此只接受条目原名的显式指认。"""
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    block = _sanity().mental_block(card, state)
    sanity_state.add_trait(block, kind="phobia", name="蜘蛛恐惧", tag="spider", en="arachnophobia")
    info = _info()
    assert _sanity().fear_penalty(card, state, "蜘蛛恐惧", room_info=info)["penalty"] == 1
    assert _sanity().fear_penalty(card, state, "arachnophobia", room_info=info)["penalty"] == 1
    # 不做关键词猜测：外观描述与来源名都不算命中。
    assert _sanity().fear_penalty(card, state, "巨型蜘蛛", room_info=info)["penalty"] == 0
    assert _sanity().fear_penalty(card, state, "深潜者", room_info=info)["penalty"] == 0
    off = _info({"coc7_sanity_traits": False})
    assert _sanity().fear_penalty(card, state, "蜘蛛恐惧", room_info=off)["penalty"] == 0


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
        sanity_state.note_habituation(block, "深潜者", 5, max_loss=6)
    save_room_info(tmp_path, info)
    payload = room_sanity.interlude_decay(tmp_path, load_room_info(tmp_path))
    assert len(payload["members"]) == 2
    saved = load_room_info(tmp_path)
    for member in saved["members"]:
        row = member["character_card"]["extensions"]["mental"]["habituations"]["深潜者"]
        assert row["total_loss"] == 4
        assert row["max_loss"] == 6


# ---------------------------------------------------------------------------
# 到期收敛（房间时钟接口）
# ---------------------------------------------------------------------------
def test_expire_bouts_closes_summary_bout_and_is_idempotent(tmp_path):
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    record = sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="失忆",
        description="记忆断片",
        mode="summary",
        duration_rounds=3,
        duration_hours=2,
        started_at="2026-11-10 00:00:00",
    )
    assert record["expires_at"] == "2026-11-10 02:00:00"
    save_room_info(tmp_path, _info(member=member))

    early = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 01:00:00"))
    assert early["expired"] == []
    loaded = load_room_info(tmp_path)["members"][0]
    assert loaded["character_card"]["extensions"]["mental"]["bout"]["active"] is True

    due = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 03:00:00"))
    assert len(due["expired"]) == 1
    assert due["messages"] and "到期结束" in due["messages"][0]
    saved = load_room_info(tmp_path)["members"][0]
    mental = saved["character_card"]["extensions"]["mental"]
    assert mental["bout"] == {}
    assert mental["bouts"][-1]["end_reason"] == "到期"

    again = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 04:00:00"))
    assert again["expired"] == []
    assert again["insanity_ended"] == []


def test_expire_bouts_ends_temporary_insanity_phase(tmp_path):
    member = _member()
    member["character_state"]["mental_status"] = {"temporaryInsanity": True, "temporary_insanity": True}
    member["character_card"]["mental_status"] = {"temporaryInsanity": True, "temporary_insanity": True}
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_insanity(block, "temporary", duration_hours=3, source="深潜者", started_at="2026-11-10 00:00:00")
    save_room_info(tmp_path, _info(member=member))

    # 房间时钟传入的是 datetime，与时间推进后的游戏时刻一致。
    payload = room_sanity.expire_bouts(tmp_path, now=datetime(2026, 11, 10, 4, 0, 0))
    assert len(payload["insanity_ended"]) == 1
    assert "临时性疯狂已恢复" in payload["messages"][0]
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["insanity"] == {}
    assert saved["character_card"]["mental_status"]["temporaryInsanity"] is False
    assert saved["character_state"]["mental_status"]["temporaryInsanity"] is False


def test_active_bout_waives_further_san_loss():
    """规则书：疯狂发作期间免疫进一步的理智值损失。"""
    card = _card()
    state = {"current_san": 50, "max_san": 99, "mental_status": {}}
    block = _sanity().mental_block(card, state)
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
    )
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
        source="深潜者",
    )
    assert outcome["bout_immunity"] is True
    assert outcome["delta"] == 0
    assert outcome["new_san"] == 50
    assert "免疫进一步的理智损失" in outcome["summary"]


def test_unclosed_immediate_bout_times_out_instead_of_locking_san(tmp_path):
    """即时症状没有轮推进入口：超过安全阀即失效，由时钟兜底清空，不会永久免疫。"""
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
        started_at="2026-11-10 00:00:00",
    )
    save_room_info(tmp_path, _info(member=member))
    assert sanity_state.has_active_bout(block, sanity_state.parse_ts("2026-11-10 00:30:00")) is True
    due = sanity_state.parse_ts("2026-11-10 02:00:00")
    assert sanity_state.has_active_bout(block, due) is False

    payload = room_sanity.expire_bouts(tmp_path, timestamp=due)
    assert len(payload["expired"]) == 1
    assert payload["expired"][0]["bout"]["end_reason"] == "超时"
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_card"]["extensions"]["mental"]["bout"] == {}
    assert room_sanity.expire_bouts(tmp_path, timestamp=due)["expired"] == []

    # 收尾后理智检定恢复正常扣减，不存在“永远免疫”的卡死状态。
    outcome = _sanity().resolve_check(
        player_name="哈维",
        card=saved["character_card"],
        state=saved["character_state"],
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="-1d4",
        failure_expr="-1d6",
        roller=lambda sides: 3,
        day="2026-11-10",
        room_info=_info(),
        source="深潜者",
    )
    assert "bout_immunity" not in outcome
    assert outcome["delta"] == -3


# ---------------------------------------------------------------------------
# 即时症状收尾安全阀（房规可调）
# ---------------------------------------------------------------------------
def test_bout_safety_minutes_option_parsing():
    """默认 60；留空 / null 沿用 60；0 表示关闭；超范围按上限收敛。"""
    default = Coc7RuleOptions.from_house_rules({})
    assert default.sanity_bout_safety_minutes == 60
    assert Coc7RuleOptions.from_house_rules({"coc7_sanity_bout_safety_minutes": None}).sanity_bout_safety_minutes == 60
    assert Coc7RuleOptions.from_house_rules({"coc7_sanity_bout_safety_minutes": ""}).sanity_bout_safety_minutes == 60
    assert Coc7RuleOptions.from_house_rules({"coc7_sanity_bout_safety_minutes": 0}).sanity_bout_safety_minutes == 0
    assert Coc7RuleOptions.from_house_rules({"coc7_sanity_bout_safety_minutes": 30}).sanity_bout_safety_minutes == 30
    assert Coc7RuleOptions.from_house_rules({"coc7_sanity_bout_safety_minutes": 99999}).sanity_bout_safety_minutes == 1440


def test_bout_safety_default_expires_at_sixty_minutes(tmp_path):
    """默认 60 分钟：59 分钟仍进行中，60 分钟到期由时钟兜底收尾。"""
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
        started_at="2026-11-10 00:00:00",
    )
    # 即时症状不写挂钟结束时间，避免关闭兜底后残留超时时间戳。
    assert block["bout"]["expires_at"] == ""
    save_room_info(tmp_path, _info(member=member))
    assert sanity_state.has_active_bout(block, sanity_state.parse_ts("2026-11-10 00:59:00")) is True
    assert sanity_state.has_active_bout(block, sanity_state.parse_ts("2026-11-10 01:00:00")) is False

    payload = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 01:00:00"))
    assert len(payload["expired"]) == 1
    assert payload["expired"][0]["bout"]["end_reason"] == "超时"


def test_bout_safety_minutes_are_configurable(tmp_path):
    """房规改成其它分钟数即时生效：30 分钟到点，29 分钟仍进行中。"""
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
        started_at="2026-11-10 00:00:00",
    )
    info = _info(extra={"coc7_sanity_bout_safety_minutes": 30}, member=member)
    save_room_info(tmp_path, info)
    snapshot = room_sanity.mental_snapshot(member, info)
    assert sanity_state.has_active_bout(
        snapshot, sanity_state.parse_ts("2026-11-10 00:29:00"), safety_minutes=30
    ) is True
    assert sanity_state.has_active_bout(
        snapshot, sanity_state.parse_ts("2026-11-10 00:30:00"), safety_minutes=30
    ) is False

    early = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 00:29:00"))
    assert early["expired"] == []
    due = room_sanity.expire_bouts(tmp_path, timestamp=sanity_state.parse_ts("2026-11-10 00:30:00"))
    assert len(due["expired"]) == 1
    assert due["expired"][0]["bout"]["end_reason"] == "超时"


def test_bout_safety_zero_disables_timeout_and_requires_explicit_end(tmp_path):
    """设为 0：完全按原逻辑，不做超时兜底，需要 KP 显式收尾。"""
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
        started_at="2026-11-10 00:00:00",
    )
    info = _info(extra={"coc7_sanity_bout_safety_minutes": 0}, member=member)
    save_room_info(tmp_path, info)
    snapshot = room_sanity.mental_snapshot(member, info)
    # 十年后仍是进行中，不会自动超时。
    far_future = sanity_state.parse_ts("2036-11-10 00:00:00")
    assert sanity_state.has_active_bout(snapshot, far_future, safety_minutes=0) is True
    assert room_sanity.expire_bouts(tmp_path, timestamp=far_future)["expired"] == []
    # 期间仍然免疫理智损失；需要显式收尾。
    outcome = _sanity().resolve_check(
        player_name="哈维",
        card=member["character_card"],
        state=member["character_state"],
        current=50,
        maximum=99,
        roll=80,
        success=False,
        success_expr="-1d4",
        failure_expr="-1d6",
        roller=lambda sides: 3,
        day="2036-11-10",
        room_info=info,
        source="深潜者",
    )
    assert outcome["bout_immunity"] is True and outcome["delta"] == 0
    ended = room_sanity.end_bout(tmp_path, info, member, actor="房主", actor_id="owner-id")
    assert ended["bout"]["active"] is False
    assert load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"] == {}


def test_disabling_bout_safety_ignores_residual_timeout(tmp_path):
    """已有即时症状在关闭兜底后不残留旧的超时时间戳。"""
    member = _member()
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    sanity_state.start_bout(
        block,
        madness_type="temporary",
        name="暴力",
        description="无差别攻击",
        mode="immediate",
        duration_rounds=3,
        started_at="2026-11-10 00:00:00",
    )
    # 兼容早期数据：即便残留 expires_at，关闭兜底后也必须忽略。
    block["bout"]["expires_at"] = "2026-11-10 02:00:00"
    info = _info(extra={"coc7_sanity_bout_safety_minutes": 0}, member=member)
    save_room_info(tmp_path, info)
    far_future = sanity_state.parse_ts("2036-11-10 00:00:00")
    assert sanity_state.has_active_bout(block, far_future, safety_minutes=0) is True
    assert room_sanity.expire_bouts(tmp_path, timestamp=far_future)["expired"] == []


# ---------------------------------------------------------------------------
# 计时时钟口径：游戏时钟优先，旧墙钟数据平滑折算
# ---------------------------------------------------------------------------
def _clock_info(current, member=None):
    info = _info(extra={"coc7_game_clock": True}, member=member)
    info["game_clock"] = {"current": current, "elapsed_seconds": 0, "revision": 0, "corrections": []}
    return info


def test_begin_bout_writes_room_game_clock():
    member = _member()
    info = _clock_info("2050-06-01T12:00:00+08:00", member)
    result = _sanity().begin_bout(
        card=member["character_card"],
        state=member["character_state"],
        room_info=info,
        mode="summary",
        roller=lambda sides: 1,
    )
    bout = result["bout"]
    assert bout["clock"] == "game"
    assert bout["started_at"] == "2050-06-01 12:00:00"
    assert bout["expires_at"] == "2050-06-01 13:00:00"


def test_new_bout_after_clock_ahead_does_not_expire_immediately(tmp_path):
    """时钟被校正到未来后新建的发作不会在下一次检查里立刻到期。"""
    member = _member()
    info = _clock_info("2050-06-01T12:00:00+08:00", member)
    save_room_info(tmp_path, info)
    _sanity().begin_bout(
        card=member["character_card"],
        state=member["character_state"],
        room_info=info,
        mode="summary",
        roller=lambda sides: 1,
    )
    save_room_info(tmp_path, info)

    result = room_sanity.expire_bouts(tmp_path, now=None)
    assert result["expired"] == []
    bout = load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"]
    assert bout["active"] is True


def test_clock_rollback_keeps_existing_bout_expiring(tmp_path):
    """time correct 回拨到历史日期后，已有发作仍按游戏时间正常到期。"""
    member = _member()
    info = _clock_info("2026-11-10T12:00:00+08:00", member)
    save_room_info(tmp_path, info)
    _sanity().begin_bout(
        card=member["character_card"],
        state=member["character_state"],
        room_info=info,
        mode="summary",
        roller=lambda sides: 1,
    )
    save_room_info(tmp_path, info)

    corrected = execute_room_command(
        "time correct 1920-01-01T00:00:00+08:00 reason 回到过去 yes",
        room_id="san-batch3",
        room_dir=tmp_path,
        actor_rank=3,
    )
    assert corrected.ok, corrected.message
    bout = load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"]
    # 计时被平移，获得的是「剩余时长」而不是永久免疫。
    assert bout["active"] is True
    assert bout["started_at"].startswith("1920-01-01")
    assert bout["expires_at"].startswith("1920-01-01 01:00")

    advanced = execute_room_command(
        "time advance 2h yes", room_id="san-batch3", room_dir=tmp_path, actor_rank=3
    )
    assert advanced.ok, advanced.message
    assert "到期" in advanced.message
    assert load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"] == {}


def test_legacy_wall_bout_is_rebased_not_permanent(tmp_path):
    """旧记录只有墙钟时间戳：折算到游戏时钟后既不会永久免疫，也不会立刻结束。"""
    member = _member()
    info = _clock_info("1920-01-01T00:00:00+08:00", member)
    block = _sanity().mental_block(member["character_card"], member["character_state"])
    # 30 分钟前开始的旧记录（墙钟），没有 clock 标记。
    started = sanity_state.now_text(time.time() - 30 * 60)
    block["bout"] = {
        "active": True,
        "mode": "summary",
        "type": "temporary",
        "name": "暴力",
        "description": "旧数据",
        "bout_roll": 1,
        "duration_rounds": 1,
        "duration_hours": 2,
        "started_at": started,
        "expires_at": sanity_state.add_hours(started, 2),
        "symptoms": [],
        "trait": "",
        "source": "",
        "san_loss": 0,
    }
    save_room_info(tmp_path, info)

    first = room_sanity.expire_bouts(tmp_path, now=None)
    assert first["expired"] == []
    bout = load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"]
    assert bout["active"] is True
    assert bout["clock"] == "game"
    # 剩余 ≈ 1.5 小时，锚定在 1919-12-31 23:30，而不是 2026 年的开始时间。
    assert bout["started_at"].startswith("1919-12-31")

    second = room_sanity.expire_bouts(tmp_path, now="1920-01-01T01:30:00")
    assert second["expired"]
    assert load_room_info(tmp_path)["members"][0]["character_card"]["extensions"]["mental"]["bout"] == {}


def test_random_trait_uses_full_table():
    picked = traits.random_entry("phobia", lambda sides: sides)
    assert picked["id"] == 100
    rng = random.Random(7)
    picked = traits.random_entry("mania", lambda sides: rng.randint(1, sides))
    assert 1 <= picked["id"] <= 100
